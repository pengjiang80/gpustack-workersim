"""Exercise the actual unmodified lab Server; fixtures never replace its scheduler.

Creates only models prefixed workersim-040-. Existing user deployments are retained.
Run on the dedicated host; API credentials are read privately from lab/secrets.json.
"""

import argparse
import json
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

LAB = Path("/opt/workersim/lab")
PREFIX = "workersim-040-"


def api(method, path, body=None):
    secrets = json.loads((LAB / "secrets.json").read_text())
    request = urllib.request.Request(
        "http://127.0.0.1:18080/v2" + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": "Bearer " + secrets["api_key"],
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = response.read()
            return json.loads(data) if data else None
    except urllib.error.HTTPError as error:
        # Validation messages are useful; never print a submitted configuration or credentials.
        try:
            detail = json.loads(error.read()).get("message", "")
        except ValueError:
            detail = ""
        raise RuntimeError(f"{method} {path} HTTP {error.code}: {str(detail)[:500]}") from None


def rows(path):
    result, page = [], 1
    while True:
        data = api("GET", path + f"?perPage=100&page={page}")
        result.extend(data["items"])
        if page >= data.get("pagination", {}).get("totalPage", 1):
            return result
        page += 1


def create():
    existing = {m["name"]: m for m in rows("/models")}
    base = {
        "source": "local_path",
        "local_path": "/models/workersim-llama",
        "backend": "vLLM",
        "categories": ["llm"],
        "replicas": 1,
        "enable_model_route": True,
        "cluster_id": 1,
        "placement_strategy": "spread",
        "distributed_inference_across_workers": True,
        "env": {
            "GPUSTACK_MODEL_VRAM_CLAIM": str(8 * 1024**3),
            "GPUSTACK_MODEL_RAM_CLAIM": str(1024**3),
        },
        "backend_parameters": ["--tp=1", "--gpu-memory-utilization=0.5"],
    }
    examples = {"single": {"replicas": 2, "worker_selector": {"worker-name": "sim-b300-079"}}}
    for i, mode in enumerate(("mp", "external", "hybrid", "multi-port-external", "sglang")):
        first = 80 + i * 2
        params = (
            ["--tp=2", "--distributed-executor-backend=mp"]
            if mode == "mp"
            else ["--tp=1", "--dp=2"]
        )
        if mode not in ("mp", "sglang"):
            params.append(f"--data-parallel-{mode}-lb")
        examples[mode] = {
            "gpu_selector": {"gpu_ids": [f"sim-b300-{w:03d}:cuda:0" for w in (first, first + 1)]},
            "backend_parameters": params,
        }
        if mode == "sglang":
            examples[mode].update(
                backend="SGLang", backend_parameters=["--tp=2", "--dp=2", "--enable-dp-attention"]
            )
        if mode == "multi-port-external":
            examples[mode].update(
                gpu_selector={
                    "gpu_ids": [
                        f"sim-b300-{w:03d}:cuda:{g}" for w in (first, first + 1) for g in (0, 1)
                    ]
                },
                backend_parameters=["--tp=1", "--dp=4", "--data-parallel-multi-port-external-lb"],
            )
    modes = rows("/pd-modes")
    pd_mode = next(m["name"] for m in modes if m["name"] == "vllm-nixl")
    examples["pd"] = {
        "disaggregation": {"enabled": True, "mode": pd_mode},
        "worker_selector": {"worker-name": "sim-b300-090"},
        "roles": [
            {"name": "prefill", "replicas": 2},
            {"name": "decode", "replicas": 1},
            {"name": "router", "replicas": 1},
        ],
    }
    for scenario, nodes, parameters in (
        ("pp", (92, 93), ["--tp=1", "--pp=2", "--distributed-executor-backend=mp"]),
        ("ray", (94, 95), ["--tp=2", "--distributed-executor-backend=ray"]),
    ):
        examples[scenario] = {
            "gpu_selector": {"gpu_ids": [f"sim-b300-{w:03d}:cuda:0" for w in nodes]},
            "backend_parameters": parameters,
        }
    examples["sglang-pd"] = {
        "backend": "SGLang",
        "backend_parameters": ["--tp=1"],
        "worker_selector": {"worker-name": "sim-b300-096"},
        "disaggregation": {"enabled": True, "mode": "sglang-mooncake"},
        "roles": [
            {"name": "prefill", "replicas": 1},
            {"name": "decode", "replicas": 1},
            {"name": "router", "replicas": 1},
        ],
    }
    for scenario, settings in examples.items():
        name = PREFIX + scenario
        if name not in existing:
            model = api("POST", "/models", base | settings | {"name": name})
            print(json.dumps({"created": name, "id": model["id"]}), flush=True)


def snapshot():
    models, instances, files, workers = (
        rows(p) for p in ("/models", "/model-instances", "/model-files", "/workers")
    )
    output = {
        "time": time.time(),
        "workers": dict(Counter(w["state"] for w in workers)),
        "files": dict(Counter(f["state"] for f in files)),
        "models": [],
    }
    for model in models:
        selected = [i for i in instances if i["model_id"] == model["id"]]
        output["models"].append(
            {
                k: model.get(k)
                for k in (
                    "id",
                    "name",
                    "state",
                    "state_message",
                    "replicas",
                    "ready_replicas",
                    "role_status",
                )
            }
            | {
                "instances": [
                    {
                        k: i.get(k)
                        for k in (
                            "id",
                            "state",
                            "state_message",
                            "worker_id",
                            "role",
                            "port",
                            "ports",
                            "named_ports",
                            "distributed_servers",
                            "restart_count",
                            "download_progress",
                        )
                    }
                    for i in selected
                ]
            }
        )
    directory = LAB / "acceptance-040"
    directory.mkdir(exist_ok=True)
    (directory / "state.json").write_text(json.dumps(output, indent=2))
    print(
        json.dumps(
            {
                "workers": output["workers"],
                "files": output["files"],
                "models": [
                    {
                        "name": m["name"],
                        "state": m["state"],
                        "ready": m["ready_replicas"],
                        "desired": m["replicas"],
                        "instances": dict(Counter(i["state"] for i in m["instances"])),
                        "errors": [
                            i["state_message"] for i in m["instances"] if i["state"] == "error"
                        ][:1],
                    }
                    for m in output["models"]
                ],
            }
        ),
        flush=True,
    )
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action", choices=["create", "check", "routes", "restart", "scale", "delete"]
    )
    parser.add_argument("--name", default="single")
    parser.add_argument("--replicas", type=int, default=1)
    args = parser.parse_args()
    if args.action == "create":
        create()
    elif args.action == "routes":
        existing = {r["name"] for r in rows("/model-routes")}
        for model in rows("/models"):
            if model["name"].startswith(PREFIX) and model["name"] not in existing:
                api(
                    "POST",
                    "/model-routes",
                    {
                        "name": model["name"],
                        "categories": ["llm"],
                        "targets": [{"model_id": model["id"], "weight": 100}],
                    },
                )
                print(json.dumps({"created_route": model["name"]}), flush=True)
    elif args.action != "check":
        name = PREFIX + args.name
        model = next(m for m in rows("/models") if m["name"] == name)
        if args.action == "restart":
            result = api("POST", f"/models/{model['id']}/restart")
            print(json.dumps({"restart": name, "phase": result.get("phase")}))
        elif args.action == "scale":
            api("PUT", f"/models/{model['id']}", model | {"replicas": args.replicas})
        else:
            api("DELETE", f"/models/{model['id']}")
    snapshot()


if __name__ == "__main__":
    main()
