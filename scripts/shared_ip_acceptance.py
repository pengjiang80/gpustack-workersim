"""Set up/check/remove two same-IP Workers on the existing lab Server."""

import argparse
import json
import subprocess
import time

from scenario_acceptance import LAB, api, rows

NAME = "workersim-shared-check"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["setup", "check", "cleanup"])
    args = parser.parse_args()
    if args.action == "setup":
        config = {
            "server": {"url": "http://127.0.0.1:18080", "cluster_id": 1, "request_timeout": 120},
            "network": {
                "worker_ip": "172.17.0.1",
                "advertise_address": "172.17.0.1",
                "api_port_base": 21100,
                "metrics_port_base": 21200,
                "control_port": 21099,
                "instance_port_start": 50000,
                "instance_port_end": 50063,
            },
            "simulation": {"namespace": NAME, "data_dir": "/data", "registration_rate": 2},
            "worker_groups": [{"name_prefix": "sim-shared", "count": 2, "gpu_count": 8}],
        }
        path = LAB / "shared-check.json"
        path.write_text(json.dumps(config))
        exists = (
            subprocess.run(["docker", "inspect", NAME], capture_output=True, check=False).returncode
            == 0
        )
        if not exists:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "--name",
                    NAME,
                    "--network",
                    "host",
                    "--ulimit",
                    "nofile=65536:65536",
                    "--env-file",
                    str(LAB / "secrets.env"),
                    "-v",
                    f"{path}:/config/fleet.yaml:ro",
                    "-v",
                    NAME + "-data:/data",
                    "gpustack-workersim:0.4.0",
                ],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            registered = [
                w
                for w in rows("/workers")
                if (w.get("labels") or {}).get("workersim.namespace") == NAME
            ]
            if len(registered) == 2 and all(w["state"] == "ready" for w in registered):
                break
            time.sleep(1)
        else:
            raise TimeoutError("Shared-IP Workers were not registered")
        model = {
            "name": "workersim-040-shared-mp",
            "source": "local_path",
            "local_path": "/models/workersim-llama",
            "backend": "vLLM",
            "categories": ["llm"],
            "replicas": 1,
            "cluster_id": 1,
            "distributed_inference_across_workers": True,
            "backend_parameters": ["--tp=2", "--distributed-executor-backend=mp"],
            "gpu_selector": {"gpu_ids": [f"sim-shared-{i:03d}:cuda:0" for i in (1, 2)]},
        }
        if not any(m["name"] == model["name"] for m in rows("/models")):
            api("POST", "/models", model)
    elif args.action == "cleanup":
        for model in rows("/models"):
            if model["name"] == "workersim-040-shared-mp":
                api("DELETE", f"/models/{model['id']}")
        for worker in rows("/workers"):
            if (worker.get("labels") or {}).get("workersim.namespace") == NAME:
                api("DELETE", f"/workers/{worker['id']}")
        subprocess.run(["docker", "rm", "-f", NAME], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(
            ["docker", "volume", "rm", NAME + "-data"], check=True, stdout=subprocess.DEVNULL
        )
        return
    status = json.loads(
        subprocess.check_output(
            ["docker", "exec", NAME, "workersim", "--config", "/config/fleet.yaml", "status"],
            text=True,
        )
    )
    workers = [
        w for w in rows("/workers") if (w.get("labels") or {}).get("workersim.namespace") == NAME
    ]
    result = {
        "workers": [{k: w[k] for k in ("id", "name", "ip", "port", "state")} for w in workers],
        "simulator": status,
    }
    directory = LAB / "acceptance-040"
    directory.mkdir(exist_ok=True)
    (directory / "shared-ip.json").write_text(json.dumps(result, indent=2))
    print(
        json.dumps(
            {
                "shared_ip_workers": len(workers),
                "ips": sorted({w["ip"] for w in workers}),
                "api_ports": [w["port"] for w in workers],
                "local_running_members": sum(w["running"] for w in status["workers"]),
            }
        )
    )


if __name__ == "__main__":
    main()
