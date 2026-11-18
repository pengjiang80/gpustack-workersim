"""Exercise the unmodified Server's model creation and replica scaling APIs."""

import asyncio
import json
import os
import time
from pathlib import Path

from workersim.client import Client
from workersim.config import GIB
from workersim.http_server import MODEL_PATH


async def admin_client(config):
    token = os.environ.get(config.server.api_key_env, "")
    if not token:
        raise ValueError(f"Set {config.server.api_key_env} to a GPUStack platform admin API key")
    return await Client(config.server, token).open()


async def wait_model(client, model_id, replicas, timeout):  # noqa: ASYNC109
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        model = await client.request("GET", f"/models/{model_id}")
        instances = await client.list_all("/model-instances", model_id=model_id)
        ready = sum(i.get("state") == "running" for i in instances)
        if (
            len(instances) == replicas
            and ready == replicas
            and model.get("ready_replicas") == replicas
        ):
            return {
                "replicas": replicas,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "instance_ids": [i["id"] for i in instances],
                "worker_ids": sorted({i["worker_id"] for i in instances}),
            }
        await asyncio.sleep(1)
    states = {i["id"]: i["state"] for i in instances}
    raise TimeoutError(f"Model {model_id} did not converge to {replicas} replicas: {states}")


async def run_scenario(config, replicas, timeout, output):  # noqa: ASYNC109
    if any(type(n) is not int or n < 0 for n in replicas) or not replicas or replicas[0] < 1:
        raise ValueError("Replica sequence must start positive and contain nonnegative integers")
    client = await admin_client(config)
    model_id = None
    report = {
        "target": "GPUStack 2.3.1",
        "simulated_inference": True,
        "phases": [],
        "deleted": False,
    }
    try:
        workers = await client.list_all("/workers")
        selected = [
            w
            for w in workers
            if (w.get("labels") or {}).get("workersim.namespace") == config.simulation.namespace
            and (w.get("labels") or {}).get("simulated") == "true"
        ]
        cluster_ids = {w["cluster_id"] for w in selected}
        if len(cluster_ids) != 1:
            raise ValueError("Expected registered simulated Workers in exactly one cluster")
        if config.server.cluster_id is not None and cluster_ids != {config.server.cluster_id}:
            raise ValueError("Simulated Workers belong to a different cluster")
        model = await client.request(
            "POST",
            "/models",
            body={
                "name": f"{config.simulation.namespace}-smoke-{int(time.time())}",
                "source": "local_path",
                "source_id": MODEL_PATH,
                "local_path": MODEL_PATH,
                "backend": "vLLM",
                "categories": ["llm"],
                "replicas": replicas[0],
                "cluster_id": next(iter(cluster_ids)),
                "placement_strategy": "spread",
                "worker_selector": {
                    "simulated": "true",
                    "workersim.namespace": config.simulation.namespace,
                },
                "env": {
                    "GPUSTACK_MODEL_VRAM_CLAIM": str(8 * GIB),
                    "GPUSTACK_MODEL_RAM_CLAIM": str(GIB),
                },
                "backend_parameters": ["--tensor-parallel-size=1", "--gpu-memory-utilization=0.9"],
            },
        )
        model_id = model["id"]
        report["model_id"] = model_id
        for index, count in enumerate(replicas):
            if index:
                current = await client.request("GET", f"/models/{model_id}")
                await client.request(
                    "PUT", f"/models/{model_id}", body=current | {"replicas": count}
                )
            report["phases"].append(await wait_model(client, model_id, count, timeout))
        report["status"] = "passed"
    except BaseException:
        report["status"] = "failed"
        raise
    finally:
        try:
            if model_id is not None:
                await client.request("DELETE", f"/models/{model_id}")
                report["deleted"] = True
        finally:
            await client.close()
            path = Path(output)
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                path.write_text, json.dumps(report, indent=2) + "\n", encoding="utf-8"
            )
    return report
