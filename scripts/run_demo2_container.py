"""Run as root on demo2: replace only the owned simulator, retain its data and rollback."""

import argparse
import json
import subprocess
from pathlib import Path

DIRECTORY = Path("/opt/gpustack-workersim-demo2")
NAME = "gpustack-workersim-demo2"
IMAGE = "gpustack/workersim:0.1.1"


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replace", action="store_true", help="Reload a changed fleet config")
    parser.add_argument("--image", default=IMAGE, help="Pre-pulled image tag or immutable digest")
    args = parser.parse_args()
    image_id = json.loads(docker("image", "inspect", args.image).stdout)[0]["Id"]
    old = docker("inspect", NAME, check=False)
    rollback = None
    if old.returncode == 0:
        info = json.loads(old.stdout)[0]
        if info["Config"].get("Labels", {}).get("gpustack.workersim.owner") != "demo2":
            raise RuntimeError("Refusing to replace an unowned container")
        if info["Image"] == image_id and info["State"]["Running"] and not args.replace:
            print(json.dumps({"running": True, "image_id": image_id, "already_current": True}))
            return
        rollback = NAME + "-rollback-" + info["Id"][:12]
        docker("stop", "-t", "30", NAME)
        docker("rename", NAME, rollback)
        docker("update", "--restart=no", rollback)
    try:
        result = docker(
            "run",
            "-d",
            "--name",
            NAME,
            "--label",
            "gpustack.workersim.owner=demo2",
            "--restart",
            "unless-stopped",
            "--network",
            "host",
            "--ulimit",
            "nofile=65536:65536",
            "--env-file",
            str(DIRECTORY / "secrets.env"),
            "-v",
            f"{DIRECTORY}/fleet.yaml:/config/fleet.yaml:ro",
            "-v",
            f"{DIRECTORY}/data:/data",
            args.image,
        )
    except subprocess.CalledProcessError:
        if rollback:
            failed = docker("inspect", NAME, check=False)
            if failed.returncode == 0:
                info = json.loads(failed.stdout)[0]
                if info["Config"].get("Labels", {}).get("gpustack.workersim.owner") == "demo2":
                    docker("rm", "-f", NAME)
            docker("rename", rollback, NAME)
            docker("update", "--restart=unless-stopped", NAME)
            docker("start", NAME)
        raise
    print(
        json.dumps(
            {
                "container_id": result.stdout.strip(),
                "image_id": image_id,
                "restart_policy": "unless-stopped",
                "rollback": rollback,
            }
        )
    )


if __name__ == "__main__":
    main()
