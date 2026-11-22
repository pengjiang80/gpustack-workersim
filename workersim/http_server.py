"""Synthetic Worker APIs and engine endpoints; never access host model files."""

import json
import secrets
import time
from pathlib import PurePosixPath

from aiohttp import web

from workersim import NATIVE_VERSION, TARGET_COMMIT
from workersim.metrics import render, render_raw
from workersim.model_metrics import render_families
from workersim.router import membership, registered_peers
from workersim.synthetic_api import response as synthetic_response

MODEL_PATH = "/models/workersim-llama"
MODEL_CONFIG = {
    "architectures": ["LlamaForCausalLM"],
    "model_type": "llama",
    "torch_dtype": "float16",
    "hidden_size": 2048,
    "intermediate_size": 5632,
    "num_attention_heads": 16,
    "num_key_value_heads": 8,
    "num_hidden_layers": 16,
    "vocab_size": 32000,
    "max_position_embeddings": 4096,
    "rms_norm_eps": 1e-5,
}


async def listen(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, port).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner


def worker_app(worker) -> web.Application:
    @web.middleware
    async def authenticate(request, handler):
        if request.path in ("/healthz", "/readyz", "/version"):
            if not worker.online:
                raise web.HTTPServiceUnavailable()
            return await handler(request)
        authorization = request.headers.get("Authorization", "")
        token = (
            authorization[7:]
            if authorization.lower().startswith("bearer ")
            else request.headers.get("X-API-Key", "")
        )
        accepted = (worker.identity.get("token", ""), worker.registration_token)
        if not token or not any(
            secrets.compare_digest(token, value) for value in accepted if value
        ):
            raise web.HTTPUnauthorized()
        if not worker.online:
            raise web.HTTPServiceUnavailable()
        if request.headers.get("X-GPUStack-Model-Instance"):
            return await proxy(request)
        return await handler(request)

    app = web.Application(middlewares=[authenticate])

    async def health(request):
        # Native probes answer the JSON string "ok".
        return web.json_response("ok")

    async def version(request):
        return web.json_response({"version": NATIVE_VERSION, "git_commit": TARGET_COMMIT})

    async def files(request):
        path = request.query.get("path", "")
        # Only the explicit virtual fixture is visible. No normalization of traversal paths.
        virtual = worker.model_files.paths()
        directory = path.removesuffix("/config.json")
        item = virtual.get(directory)
        known = path in (MODEL_PATH, MODEL_PATH + "/config.json") or item is not None
        known = known and ".." not in PurePosixPath(path).parts
        if request.path.endswith("file-exists"):
            return web.json_response(
                {
                    "path": path,
                    "exists": known,
                    "is_file": known and path.endswith(".json"),
                    "is_dir": known and not path.endswith(".json"),
                }
            )
        if not known or ".." in PurePosixPath(path).parts:
            raise web.HTTPNotFound()
        if request.path.endswith("model-config") and path.endswith("/config.json"):
            return web.json_response(MODEL_CONFIG)
        if request.path.endswith("model-weight-size") and not path.endswith(".json"):
            return web.json_response({"size": (item or {}).get("size") or 2 * 1024**3})
        raise web.HTTPNotFound()

    async def logs(request):
        instance_id = int(request.match_info["id"])
        record = worker.runtime.records.get(instance_id)
        file_id = request.query.get("model_file_id")
        if file_id and int(file_id) in worker.model_files.items:
            item = worker.model_files.items[int(file_id)]
            return web.Response(
                text=f"[WorkerSim] Virtual download {item['state']} "
                f"{item.get('download_progress', 0)}%; no weights downloaded.\n",
                content_type="text/plain",
            )
        if record is None:
            raise web.HTTPNotFound()
        if request.path.startswith("/serveLogOptions"):
            return web.json_response(
                {"restarts": [{"previous": False, "sources": [], "containers": []}]}
            )
        return web.Response(
            text="\n".join(record.logs) + "\n",
            content_type="text/plain",
            headers={"X-WorkerSim": "true"},
        )

    async def proxy(request):
        # GPUStack's gateway selects an instance using this native header.
        target = request.headers.get("X-GPUStack-Model-Instance", "")
        import re

        match = re.fullmatch(r"model-(\d+)-(\d+)(?:-dp(\d+)|-w([1-9]\d*))?\.(?:static|dns)", target)
        if not match:
            raise web.HTTPBadRequest(text="Missing X-GPUStack-Model-Instance")
        model_id, instance_id, rank, member_id = match.groups()
        record = worker.runtime.records.get(int(instance_id))
        if record is None or record.phase != "running":
            raise web.HTTPServiceUnavailable()
        if record.instance.get("model_id") != int(model_id):
            raise web.HTTPNotFound()
        if member_id is not None and int(member_id) != worker.worker_id:
            raise web.HTTPNotFound()
        if rank is not None and not any(
            p["dp_rank"] == int(rank) and p["node_rank"] == 0
            for w in record.workloads
            for p in w.get("dp_rank_placements", [])
        ):
            raise web.HTTPNotFound()
        if not worker.online:
            raise web.HTTPServiceUnavailable()
        return await engine_response(request, record.instance, record.traffic, record=record)

    app.router.add_get("/healthz", health)
    app.router.add_get("/readyz", health)
    app.router.add_get("/version", version)
    app.router.add_get("/port-reservations", worker.port_reservations.handle)
    app.router.add_route(
        "*", "/port-reservations/{instance_id:\\d+}", worker.port_reservations.handle
    )
    for path in ("model-config", "file-exists", "model-weight-size"):
        app.router.add_get("/files/" + path, files)
    app.router.add_get("/serveLogs/{id:\\d+}", logs)
    app.router.add_get("/serveLogOptions/{id:\\d+}", logs)
    app.router.add_route("*", "/proxy/{path:.*}", proxy)
    app.router.add_route("*", "/v1/{path:.*}", proxy)
    return app


def metrics_app(worker) -> web.Application:
    app = web.Application()

    async def metrics(request):
        if not worker.online:
            raise web.HTTPServiceUnavailable()
        return web.Response(
            text=render_raw(worker) if request.path.endswith("/raw") else render(worker),
            content_type="text/plain",
            headers={"X-WorkerSim": "true"},
        )

    app.router.add_get("/metrics", metrics)
    app.router.add_get("/metrics/raw", metrics)
    return app


async def engine_response(request, instance, traffic=None, *, record=None):
    path = request.path.removeprefix("/proxy")
    if path == "/workers" or path.startswith("/workers/"):
        return await membership(request, record, path)
    model_name = instance.get("model_name", "workersim")
    if path in ("/health", "/healthz", "/ready", "/v1/health"):
        return web.json_response({"status": "ok", "simulated": True})
    if path == "/v1/models":
        return web.json_response(
            {
                "object": "list",
                "data": [{"id": model_name, "object": "model", "owned_by": "workersim"}],
            }
        )
    if path == "/version":
        from workersim.runtime_metrics import engine_version

        return web.json_response(
            {"version": engine_version(instance, getattr(record, "model", None))}
        )
    if path == "/metrics":
        # The runtime's own native exposition, which the Worker aggregator normalizes.
        text = ""
        if record is not None and traffic is not None:
            from workersim.runtime_metrics import engine_source, source_families

            port = (request.transport.get_extra_info("sockname") or (None, None))[1]
            source = engine_source(record, port)
            if source is not None:
                text = render_families(source_families(record, source))
        return web.Response(text=text, content_type="text/plain", headers={"X-WorkerSim": "true"})
    if path == "/v1/chat/completions" and request.method == "POST":
        if record is not None and instance.get("role") == "router":
            roles = {row["worker_type"] for row in registered_peers(record).values()}
            if not {"prefill", "decode"} <= roles:
                raise web.HTTPServiceUnavailable(text="PD router has no registered live P/D pair")
        body = await request.json()
        text = "[WorkerSim] This is a simulated response; no model inference was performed."
        common = {"id": "chatcmpl-workersim", "model": model_name, "created": int(time.time())}
        if body.get("stream"):
            chunks = [
                {
                    **common,
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": text},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    **common,
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
            ]
            payload = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
            return web.Response(
                text=payload + "data: [DONE]\n\n",
                content_type="text/event-stream",
                headers={"X-WorkerSim": "true"},
            )
        return web.json_response(
            {
                **common,
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "simulated": True,
            },
            headers={"X-WorkerSim": "true"},
        )
    result = await synthetic_response(request, path, model_name)
    if result is not None:
        return result
    raise web.HTTPNotFound(text="Endpoint is not simulated")


def engine_app(record) -> web.Application:
    app = web.Application()

    async def handle(request):
        if record.phase != "running" or (record.online and not record.online()):
            raise web.HTTPServiceUnavailable()
        return await engine_response(request, record.instance, record.traffic, record=record)

    app.router.add_route("*", "/{path:.*}", handle)
    return app
