"""Start an original 2.3.1 CPU test Server with private bootstrap configuration.

Run on the lab host. Supply GPUSTACK_BOOTSTRAP_PASSWORD in the environment;
the password is written into a mode-0600 config, never printed or passed in argv.
"""

import argparse
import json
import os
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="gpustack")
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--directory", default="/opt/workersim/lab")
    args = parser.parse_args()
    password = os.environ["GPUSTACK_BOOTSTRAP_PASSWORD"]
    if len(password) < 12 or "\n" in password:
        raise ValueError("Use a bootstrap password with at least 12 characters")
    directory = Path(args.directory).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "server-config.json"
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(
            {"bootstrap_password": password, "disable_update_check": True, "enable_worker": False},
            stream,
        )
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            args.name,
            "--restart",
            "unless-stopped",
            "--ulimit",
            "nofile=65536:65536",
            "-p",
            f"127.0.0.1:{args.port}:80",
            "-v",
            f"{args.name}-data:/var/lib/gpustack",
            "-v",
            f"{path}:/config/server.json:ro",
            "gpustack/gpustack:v2.3.1",
            "--config-file",
            "/config/server.json",
            "--disable-update-check",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    print(
        json.dumps(
            {
                "container": args.name,
                "port": args.port,
                "version": "2.3.1",
                "bootstrap_password_configured": True,
                "disable_update_check": True,
            }
        )
    )


if __name__ == "__main__":
    main()
