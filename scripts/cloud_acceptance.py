"""Run on the dedicated lab host; use ordinary Server APIs and record real results."""

import argparse
import json
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path("/opt/workersim")
LAB = ROOT / "lab"


def api(path):
    secrets = json.loads((LAB / "secrets.json").read_text())
    request = urllib.request.Request(
        "http://127.0.0.1:18080/v2" + path,
        headers={"Authorization": "Bearer " + secrets["api_key"]},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def stage(count, gpu_profile="mixed", worker_ip_cidr=None):
    config = {
        "server": {"url": "http://127.0.0.1:18080", "cluster_id": 1},
        "network": {"advertise_address": "172.17.0.1", "worker_ip": "172.17.0.1"},
        "simulation": {"namespace": "workersim-vultr", "data_dir": "/data"},
        "worker_groups": [
            {
                "name_prefix": "sim-h100",
                "count": min(count, 25),
                "gpu_profile": "h100-80gb",
                "gpu_count": 8,
            }
        ],
    }
    if count > 25:
        config["worker_groups"].append(
            {
                "name_prefix": "sim-a800",
                "count": count - 25,
                "gpu_profile": "a800-80gb",
                "gpu_count": 8,
            }
        )
    if gpu_profile == "b300-288gb":
        config["server"]["request_timeout"] = 120
        config["simulation"].update(registration_rate=2, reconcile_interval=30)
        config["worker_groups"] = [
            {
                "name_prefix": "sim-b300",
                "count": count,
                "gpu_profile": gpu_profile,
                "gpu_count": 8,
                "cpu_cores": 192,
                "memory_gib": 2048,
                "disk_gib": 8192,
            }
        ]
    if worker_ip_cidr:
        config["network"]["worker_ip_cidr"] = worker_ip_cidr
    (LAB / "fleet.yaml").write_text(json.dumps(config, indent=2))
    exists = (
        subprocess.run(
            ["docker", "container", "inspect", "workersim"], capture_output=True, check=False
        ).returncode
        == 0
    )
    if exists:
        docker("stop", "workersim")
        docker("rm", "workersim")
    started = time.monotonic()
    docker(
        "run",
        "-d",
        "--name",
        "workersim",
        "--network",
        "host",
        "--restart",
        "unless-stopped",
        "--ulimit",
        "nofile=65536:65536",
        "--env-file",
        str(LAB / "secrets.env"),
        "-v",
        str(LAB / "fleet.yaml") + ":/config/fleet.yaml:ro",
        "-v",
        "workersim-data:/data",
        "gpustack-workersim:0.4.3",
    )
    deadline = started + 300
    while time.monotonic() < deadline:
        try:
            workers = api("/workers?perPage=100")["items"]
        except TimeoutError:
            continue
        selected = [
            w
            for w in workers
            if (w.get("labels") or {}).get("workersim.namespace") == "workersim-vultr"
        ]
        ready = [w for w in selected if w["state"] == "ready"]
        if len(selected) == count and len(ready) == count:
            try:
                status = json.loads(
                    docker(
                        "exec", "workersim", "workersim", "--config", "/config/fleet.yaml", "status"
                    )
                )
                if len(status["workers"]) == count and all(
                    w["registered"] and w["watch_connected"] for w in status["workers"]
                ):
                    break
            except (subprocess.CalledProcessError, ValueError):
                pass
        time.sleep(2)
    else:
        raise TimeoutError(f"Fleet did not converge to {count} Ready Workers")
    report = {
        "workers": count,
        "ready": len(ready),
        "registration_convergence_seconds": round(time.monotonic() - started, 3),
        "gpu_count": sum(len(w["status"]["gpu_devices"]) for w in selected),
        "unique_ips": len({w["ip"] for w in selected}),
        "first_ip": selected[0]["ip"],
        "last_ip": selected[-1]["ip"],
        "gpu_models": sorted({gpu["name"] for w in selected for gpu in w["status"]["gpu_devices"]}),
        "unique_uuids": int(
            docker(
                "exec",
                "workersim",
                "python",
                "-c",
                "import json; from pathlib import Path; "
                "d=json.loads(Path('/data/workersim-vultr/identities.json').read_text()); "
                "print(len({w['worker_uuid'] for w in d['workers'].values()}))",
            )
        ),
        "watch_connected": sum(w["watch_connected"] for w in status["workers"]),
        "server_image": json.loads(docker("image", "inspect", "gpustack/gpustack:v2.3.1"))[0][
            "RepoDigests"
        ],
        "simulator_image_id": docker(
            "image", "inspect", "gpustack-workersim:0.4.3", "--format", "{{.Id}}"
        ),
    }
    (LAB / f"fleet-{count}.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, required=True, choices=[2, 10, 50, 100])
    parser.add_argument("--gpu-profile", choices=["mixed", "b300-288gb"], default="mixed")
    parser.add_argument("--worker-ip-cidr")
    args = parser.parse_args()
    stage(args.workers, args.gpu_profile, args.worker_ip_cidr)


if __name__ == "__main__":
    main()
