"""Create or remove only the dedicated model used for visual lab acceptance."""

import argparse
import json
import urllib.request
from pathlib import Path

LAB = Path("/opt/workersim/lab")
NAME = "workersim-ui-proof"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["create", "delete"])
    parser.add_argument("--replicas", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.replicas <= 800:
        parser.error("replicas must be in 1..800")
    secret = json.loads((LAB / "secrets.json").read_text())

    def api(method, path, body=None):
        request = urllib.request.Request(
            "http://127.0.0.1:18080/v2" + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + secret["api_key"],
                "Content-Type": "application/json",
            },
            method=method,
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read()
            return json.loads(data) if data else None

    models = api("GET", "/models?perPage=100")["items"]
    matching = [m for m in models if m["name"] == NAME and m["cluster_id"] == 1]
    if args.action == "delete":
        for model in matching:
            api("DELETE", f"/models/{model['id']}")
        print(json.dumps({"deleted_proof_models": [m["id"] for m in matching]}))
        return
    if matching:
        model = matching[0]
        if model["replicas"] != args.replicas:
            model = api("PUT", f"/models/{model['id']}", model | {"replicas": args.replicas})
    else:
        model = api(
            "POST",
            "/models",
            {
                "name": NAME,
                "source": "local_path",
                "source_id": "/models/workersim-llama",
                "local_path": "/models/workersim-llama",
                "backend": "vLLM",
                "categories": ["llm"],
                "replicas": args.replicas,
                "cluster_id": 1,
                "placement_strategy": "spread",
                "worker_selector": {"simulated": "true", "workersim.namespace": "workersim-vultr"},
                "env": {
                    "GPUSTACK_MODEL_VRAM_CLAIM": str(8 * 1024**3),
                    "GPUSTACK_MODEL_RAM_CLAIM": str(1024**3),
                },
                "backend_parameters": ["--tensor-parallel-size=1", "--gpu-memory-utilization=0.9"],
            },
        )
    report = {key: model.get(key) for key in ("id", "name", "state", "replicas", "ready_replicas")}
    (LAB / "reports" / "ui-proof-model.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
