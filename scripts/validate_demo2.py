"""Read-only acceptance and synthetic gateway calls on demo2; never prints credentials."""

import io
import json
import subprocess
import time
import urllib.error
import urllib.request
import wave
from datetime import datetime, timezone
from pathlib import Path

DIRECTORY = Path("/opt/gpustack-workersim-demo2")
BASE = "http://127.0.0.1:8080"


def main():
    env = dict(line.split("=", 1) for line in (DIRECTORY / "secrets.env").read_text().splitlines())
    headers = {"Authorization": "Bearer " + env["GPUSTACK_API_KEY"]}

    def call(path, body=None, content_type="application/json"):
        raw = json.dumps(body).encode() if isinstance(body, dict) else body
        request = urllib.request.Request(
            BASE + path, data=raw, headers=headers | {"Content-Type": content_type}
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
            return response.status, {k.lower(): v for k, v in response.headers.items()}, data

    def get(path):
        return json.loads(call(path)[2])

    def rows(path):
        result, page = [], 1
        while True:
            data = get(path + ("&" if "?" in path else "?") + f"perPage=100&page={page}")
            result.extend(data["items"])
            if page >= data.get("pagination", {}).get("totalPage", 1):
                return result
            page += 1

    cluster = next(c for c in rows("/v2/clusters") if c["name"] == "workersim-demo2")
    cluster_id = cluster["id"]
    config = json.loads((DIRECTORY / "fleet.yaml").read_text())
    manifest = json.loads((DIRECTORY / "model-manifest.json").read_text())
    expected_workers = sum(g["count"] for g in config["worker_groups"])
    expected_gpus = sum(g["count"] * g["gpu_count"] for g in config["worker_groups"])
    expected_replicas = {m["name"]: m["replicas"] for m in manifest}
    expected_instances = sum(expected_replicas.values())
    deadline = time.monotonic() + 300
    next_progress = 0
    while True:
        workers = [w for w in rows("/v2/workers") if w["cluster_id"] == cluster_id]
        models = [m for m in rows("/v2/models") if m["cluster_id"] == cluster_id]
        instances = [i for i in rows("/v2/model-instances") if i["cluster_id"] == cluster_id]
        try:
            with urllib.request.urlopen("http://127.0.0.1:20099/status", timeout=10) as response:
                control = json.load(response)
        except urllib.error.URLError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)
            continue
        if (
            len(workers) == expected_workers
            and all(w["state"] == "ready" for w in workers)
            and len(models) == len(expected_replicas)
            and all(m["ready_replicas"] == expected_replicas[m["name"]] for m in models)
            and len(instances) == expected_instances
            and all(i["state"] == "running" for i in instances)
            and all(w["watch_connected"] and w["file_watch_connected"] for w in control["workers"])
            and sum(w["running"] for w in control["workers"]) == expected_instances
        ):
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("Fleet/model/watch convergence failed")
        if time.monotonic() >= next_progress:
            print(
                json.dumps(
                    {
                        "stage": "waiting",
                        "workers": len(workers),
                        "running": sum(i["state"] == "running" for i in instances),
                        "expected": expected_instances,
                    }
                ),
                flush=True,
            )
            next_progress = time.monotonic() + 30
        time.sleep(2)
    worker_ids = {w["id"] for w in workers}
    files = [f for f in rows("/v2/model-files") if f["worker_id"] in worker_ids]
    assert files and all(f["state"] == "ready" and f["download_progress"] == 100 for f in files)
    assert all(w["report_errors"] == 0 and not w["last_error"] for w in control["workers"])
    assert all(len(w["status"]["gpu_devices"]) == 8 for w in workers)
    print(
        json.dumps({"stage": "fleet_ready", "workers": len(workers), "instances": len(instances)}),
        flush=True,
    )
    gateway = {}
    for model in models:
        category = model["categories"][0]
        body = {"model": model["name"]}
        content_type = "application/json"
        if category == "llm":
            path = "/v1/chat/completions"
            body.update(messages=[{"role": "user", "content": "WorkerSim test"}], max_tokens=64)
        elif category == "embedding":
            path = "/v1/embeddings"
            body.update(input=["example one", "example two"])
        elif category == "reranker":
            path = "/v1/rerank"
            body.update(query="example", documents=["one", "two"], top_n=2)
        elif category == "text_to_speech":
            path = "/v1/audio/speech"
            body.update(input="WorkerSim test", voice="Vivian", response_format="wav")
        elif category == "speech_to_text":
            path = "/v1/audio/transcriptions"
            output = io.BytesIO()
            with wave.open(output, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(b"\0\0" * 4000)
            boundary = "workersim-demo2-boundary"
            content_type = "multipart/form-data; boundary=" + boundary
            body = (
                (
                    f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n'
                    f"{model['name']}\r\n"
                    f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                    'filename="fixture.wav"\r\nContent-Type: audio/wav\r\n\r\n'
                ).encode()
                + output.getvalue()
                + f"\r\n--{boundary}--\r\n".encode()
            )
        else:
            raise ValueError(category)
        status, response_headers, raw = call(path, body, content_type)
        if category == "text_to_speech":
            assert raw.startswith(b"RIFF") and "audio/wav" in response_headers.get(
                "content-type", ""
            )
        else:
            assert json.loads(raw)["simulated"]
        gateway[model["name"]] = {"path": path, "status": status, "bytes": len(raw)}
        print(
            json.dumps({"stage": "gateway", "model": model["name"], "status": status}), flush=True
        )
    allocated = [
        g
        for w in workers
        for g in w["status"]["gpu_devices"]
        if (g.get("memory") or {}).get("allocated", 0) > 0
    ]
    assert allocated, "No simulated GPU allocations reported"
    metrics = subprocess.check_output(
        [
            "docker",
            "exec",
            "gpustack-ent",
            "curl",
            "-fsSG",
            "http://127.0.0.1:19090/prometheus/api/v1/query",
            "--data-urlencode",
            f'query=count(gpustack:generation_tokens_total{{cluster_id="{cluster_id}"}})',
        ],
        text=True,
    )
    metric_data = json.loads(metrics)
    assert metric_data["status"] == "success" and metric_data["data"]["result"]
    info = json.loads(subprocess.check_output(["docker", "inspect", "gpustack-workersim-demo2"]))[0]
    assert info["HostConfig"]["RestartPolicy"]["Name"] == "unless-stopped"
    original = [m for m in rows("/v2/models") if m["id"] in range(1, 7)]
    assert len(original) == 6 and all(m["cluster_id"] == 1 and m["replicas"] == 0 for m in original)
    report = {
        "at": datetime.now(timezone.utc).isoformat(),
        "server": get("/version"),
        "cluster_id": cluster_id,
        "cluster_name": cluster["name"],
        "workers_ready": len(workers),
        "worker_ids": sorted(worker_ids),
        "gpus": expected_gpus,
        "allocated_gpu_count": len(allocated),
        "simulator_version": control["version"],
        "instance_watches": sum(w["watch_connected"] for w in control["workers"]),
        "file_watches": sum(w["file_watch_connected"] for w in control["workers"]),
        "model_files_ready": len(files),
        "instance_progress": {str(i["id"]): i.get("download_progress") for i in instances},
        "running_instances": len(instances),
        "models": [
            {
                k: m[k]
                for k in (
                    "id",
                    "name",
                    "huggingface_repo_id",
                    "replicas",
                    "ready_replicas",
                    "categories",
                )
            }
            for m in models
        ],
        "gateway": gateway,
        "prometheus_model_counter_series": metric_data["data"]["result"],
        "restart_policy": info["HostConfig"]["RestartPolicy"]["Name"],
        "image_id": info["Image"],
        "existing_zero_replica_models_preserved": True,
    }
    destination = DIRECTORY / "acceptance.json"
    destination.write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
