"""Collect non-secret acceptance evidence on the dedicated test host."""

import json
import subprocess
import time
from pathlib import Path

from inspect_lab import main as inspect_api

DIRECTORY = Path("/opt/workersim/lab/reports")


def command(*args):
    return subprocess.check_output(list(args), text=True).strip()


def main():
    DIRECTORY.mkdir(exist_ok=True)
    status = json.loads(
        command(
            "docker", "exec", "workersim", "workersim", "--config", "/config/fleet.yaml", "status"
        )
    )
    workers = status["workers"]
    summary = {
        "observed_at_unix": time.time(),
        "workers": len(workers),
        "watch_connected": sum(w["watch_connected"] for w in workers),
        "running": sum(w["running"] for w in workers),
        "protocol_errors": sum(w["report_errors"] for w in workers),
        "stale_reports": sum(w.get("stale_reports", 0) for w in workers),
        "deleted_assignments": sum(w.get("deleted_assignments", 0) for w in workers),
    }
    (DIRECTORY / "simulator-status.json").write_text(json.dumps(summary, indent=2))
    for name, query in {
        "prometheus-gpu-count": "count(gpustack:worker_node_gpu_utilization_rate)",
        "prometheus-gpu-memory": "sum(gpustack:worker_node_gram_allocated_bytes)",
    }.items():
        text = command(
            "docker",
            "exec",
            "gpustack",
            "curl",
            "-sf",
            "-G",
            "http://127.0.0.1:19090/prometheus/api/v1/query",
            "--data-urlencode",
            "query=" + query,
        )
        result = json.loads(text)
        assert result["status"] == "success", result
        (DIRECTORY / (name + ".json")).write_text(json.dumps(result, indent=2))
    now = int(time.time())
    history = command(
        "docker",
        "exec",
        "gpustack",
        "curl",
        "-sf",
        "-G",
        "http://127.0.0.1:19090/prometheus/api/v1/query_range",
        "--data-urlencode",
        "query=sum(gpustack:worker_node_gpu_utilization_rate)",
        "--data-urlencode",
        f"start={now - 180}",
        "--data-urlencode",
        f"end={now}",
        "--data-urlencode",
        "step=15",
    )
    (DIRECTORY / "prometheus-gpu-history.json").write_text(history + "\n")
    stats = command(
        "docker", "stats", "--no-stream", "--format", "{{json .}}", "gpustack", "workersim"
    )
    (DIRECTORY / "container-resource-sample.json").write_text(
        json.dumps(
            {
                "note": "One instantaneous sample; not peak, average or inference performance.",
                "containers": [json.loads(line) for line in stats.splitlines()],
            },
            indent=2,
        )
    )
    print(json.dumps(summary))
    inspect_api()


if __name__ == "__main__":
    main()
