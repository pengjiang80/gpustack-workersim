"""Create and verify the separate three-node Ascend demo using normal public APIs."""

import argparse
import json
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml
from demo2_lab import ROOT, Api, private_write

NAME = "workersim-910b4"
CONTAINER = "gpustack-workersim-demo2-910b4"
PRIVATE = ROOT / ".workersim/private-demo2-910b4"
REPORTS = ROOT / "outputs/demo2-910b4-20261117"


def ssh(*command):
    return subprocess.check_output(
        [
            "ssh",
            "-i",
            str(Path.home() / ".ssh/id_ed25519"),
            "-o",
            "BatchMode=yes",
            "-o",
            f"UserKnownHostsFile={ROOT / '.workersim/known_hosts-demo2'}",
            "ubuntu@demo2.gpustack.cn",
            shlex.join(command),
        ],
        text=True,
        timeout=45,
    )


def prepare(api):
    clusters = api.items("/v2/clusters")
    matches = [c for c in clusters if c["name"] == NAME]
    if matches:
        cluster = matches[0]
        if "WorkerSim" not in (cluster.get("description") or ""):
            raise RuntimeError("Refusing to reuse an unrelated cluster")
    else:
        cluster = api.call(
            "POST",
            "/v2/clusters",
            {
                "name": NAME,
                "provider": "Docker",
                "description": (
                    "WorkerSim: 3 servers, 8 Ascend 910B4 CANN devices per server, "
                    "64 GiB per device; synthetic inventory and monitoring."
                ),
            },
        )
    token = api.call("GET", f"/v2/clusters/{cluster['id']}/registration-token")["token"]
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_write(PRIVATE / "secrets.env", f"GPUSTACK_REGISTRATION_TOKEN={token}\n")
    config = yaml.safe_load((ROOT / "configs/demo2-3-910b4.yaml").read_text())
    config["server"]["cluster_id"] = cluster["id"]
    (PRIVATE / "fleet.yaml").write_text(json.dumps(config, indent=2), encoding="utf-8")
    (PRIVATE / "metadata.json").write_text(json.dumps({"cluster_id": cluster["id"]}))
    baseline = {
        "b300_workers": sorted(w["id"] for w in api.items("/v2/workers") if w["cluster_id"] == 3),
        "b300_models": {
            str(m["id"]): m["replicas"] for m in api.items("/v2/models") if m["cluster_id"] == 3
        },
    }
    (PRIVATE / "baseline.json").write_text(json.dumps(baseline, indent=2))
    print(
        json.dumps(
            {
                "cluster_id": cluster["id"],
                "cluster_name": NAME,
                "workers": 3,
                "npu_devices": 24,
                "memory_gib_per_device": 64,
            }
        ),
        flush=True,
    )


def verify(api):
    cluster_id = json.loads((PRIVATE / "metadata.json").read_text())["cluster_id"]
    deadline = time.monotonic() + 180
    while True:
        all_workers = api.items("/v2/workers")
        workers = [w for w in all_workers if w["cluster_id"] == cluster_id]
        if len(workers) == 3 and all(w["state"] == "ready" for w in workers):
            control = json.loads(ssh("curl", "-fsS", "http://127.0.0.1:20399/status"))
            if len(control["workers"]) == 3 and all(
                w["registered"]
                and w["watch_connected"]
                and w["file_watch_connected"]
                and not w["report_errors"]
                for w in control["workers"]
            ):
                break
        if time.monotonic() >= deadline:
            raise TimeoutError("Ascend cluster did not become Ready with both watches")
        time.sleep(2)
    devices = [g for w in workers for g in w["status"]["gpu_devices"]]
    assert all(len(w["status"]["gpu_devices"]) == 8 for w in workers)
    assert len(devices) == 24 and len({g["uuid"] for g in devices}) == 24
    assert all(
        g["vendor"] == "ascend"
        and g["type"] == "cann"
        and g["name"] == "Ascend 910B4"
        and g["memory"]["total"] == 64 * 1024**3
        and not g.get("compute_capability")
        for g in devices
    )
    metric_deadline = time.monotonic() + 120
    while True:
        metrics = json.loads(
            ssh(
                "sudo",
                "docker",
                "exec",
                "gpustack-ent",
                "curl",
                "-fsSG",
                "http://127.0.0.1:19090/prometheus/api/v1/query",
                "--data-urlencode",
                f'query=count(gpustack:worker_node_gpu_utilization_rate{{cluster_id="{cluster_id}"}})',
            )
        )
        samples = metrics["data"]["result"]
        if samples and float(samples[0]["value"][1]) == 24:
            break
        if time.monotonic() >= metric_deadline:
            raise TimeoutError("Ascend monitoring did not reach 24 device series")
        time.sleep(5)
    restart_policy = ssh(
        "sudo", "docker", "inspect", CONTAINER, "--format", "{{.HostConfig.RestartPolicy.Name}}"
    ).strip()
    assert restart_policy == "unless-stopped"
    baseline = json.loads((PRIVATE / "baseline.json").read_text())
    b300_workers = [w for w in all_workers if w["cluster_id"] == 3]
    models = [m for m in api.items("/v2/models") if m["cluster_id"] == 3]
    assert sorted(w["id"] for w in b300_workers) == baseline["b300_workers"]
    assert {str(m["id"]): m["replicas"] for m in models} == baseline["b300_models"]
    report = {
        "at": datetime.now(timezone.utc).isoformat(),
        "cluster_id": cluster_id,
        "cluster_name": NAME,
        "workers_ready": 3,
        "devices": 24,
        "device_name": "Ascend 910B4",
        "vendor": "ascend",
        "runtime": "cann",
        "memory_gib_per_device": 64,
        "total_memory_gib": 1536,
        "worker_ids": sorted(w["id"] for w in workers),
        "worker_names": sorted(w["name"] for w in workers),
        "instance_watches": 3,
        "file_watches": 3,
        "prometheus_device_series": samples,
        "restart_policy": restart_policy,
        "simulator_version": control["version"],
        "b300_workers_ready": sum(w["state"] == "ready" for w in b300_workers),
        "b300_ready_replicas": sum(m["ready_replicas"] for m in models),
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "acceptance.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "verify"))
    args = parser.parse_args()
    api = Api("http://demo2.gpustack.cn:8080", ROOT.parent / "Info_Shared/GPUStack.txt")
    if args.action == "prepare":
        prepare(api)
    else:
        verify(api)


if __name__ == "__main__":
    main()
