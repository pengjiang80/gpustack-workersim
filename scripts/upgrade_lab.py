"""Upgrade only the dedicated lab's simulator; preserve Server, fleet and identities."""

import argparse
import json
import subprocess
import time
import urllib.request
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="gpustack-workersim:0.4.3")
    args = parser.parse_args()
    lab = Path("/opt/workersim/lab")
    config = yaml.safe_load((lab / "fleet.yaml").read_text())
    (lab / "fleet-before-040.yaml").write_text((lab / "fleet.yaml").read_text())
    config["network"].update(
        instance_port_start=40000, instance_port_end=40063, instance_port_stride=None
    )
    (lab / "fleet.yaml").write_text(json.dumps(config, indent=2))
    subprocess.run(["docker", "stop", "workersim"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["docker", "rm", "workersim"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        [
            "docker",
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
            str(lab / "secrets.env"),
            "-v",
            str(lab / "fleet.yaml") + ":/config/fleet.yaml:ro",
            "-v",
            "workersim-data:/data",
            args.image,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    expected = sum(g["count"] for g in config["worker_groups"])
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            port = config["network"].get("control_port", 20099)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5) as response:
                status = json.load(response)
            if len(status["workers"]) == expected and all(
                w["registered"] and w["watch_connected"] and w["file_watch_connected"]
                for w in status["workers"]
            ):
                break
        except (OSError, ValueError):
            pass
        time.sleep(1)
    else:
        raise TimeoutError("Updated fleet did not reconnect both native watch streams")
    print(
        json.dumps(
            {
                "updated": "workersim",
                "image": args.image,
                "server_modified": False,
                "identity_volume_preserved": True,
            }
        )
    )


if __name__ == "__main__":
    main()
