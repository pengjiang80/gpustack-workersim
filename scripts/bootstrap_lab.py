"""Run on the temporary host: bootstrap normal API resources without echoing secrets."""

import base64
import json
import os
import subprocess
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:18080"
DIRECTORY = Path("/opt/workersim/lab")


def main():
    DIRECTORY.mkdir(mode=0o700, exist_ok=True)
    password = os.environ.get("GPUSTACK_BOOTSTRAP_PASSWORD")
    if not password:
        password = subprocess.check_output(
            ["docker", "exec", "gpustack", "cat", "/var/lib/gpustack/initial_admin_password"],
            text=True,
        ).strip()
    headers = {
        "Authorization": "Basic " + base64.b64encode(("admin:" + password).encode()).decode(),
        "Content-Type": "application/json",
    }

    def call(method, path, body=None):
        request = urllib.request.Request(
            BASE + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            data = response.read()
            return json.loads(data) if data else None

    version = call("GET", "/version")
    assert str(version["version"]).lstrip("v") == "2.3.1", version
    clusters = call("GET", "/v2/clusters")["items"]
    matches = [c for c in clusters if c["name"] == "workersim-lab"]
    cluster = (
        matches[0]
        if matches
        else call(
            "POST",
            "/v2/clusters",
            {
                "name": "workersim-lab",
                "provider": "Docker",
                "description": "Temporary CPU-only WorkerSim acceptance test",
            },
        )
    )
    token = call("GET", f"/v2/clusters/{cluster['id']}/registration-token")["token"]
    secret_path = DIRECTORY / "secrets.json"
    if secret_path.exists():
        secrets = json.loads(secret_path.read_text())
        api_key = secrets["api_key"]
    else:
        api_key = call("POST", "/v2/api-keys", {"name": "workersim-lab-test"})["value"]
        descriptor = os.open(secret_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump({"api_key": api_key, "admin_password": password}, stream)
    descriptor = os.open(DIRECTORY / "secrets.env", os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(f"GPUSTACK_REGISTRATION_TOKEN={token}\nGPUSTACK_API_KEY={api_key}\n")
    bridge = subprocess.check_output(
        [
            "docker",
            "inspect",
            "gpustack",
            "--format",
            "{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}",
        ],
        text=True,
    ).strip()
    config = f"""server:
  url: {BASE}
  cluster_id: {cluster["id"]}
network:
  advertise_address: {bridge}
  worker_ip: {bridge}
simulation:
  namespace: workersim-vultr
  data_dir: /data
worker_groups:
  - name_prefix: sim-h100
    count: 2
    gpu_profile: h100-80gb
    gpu_count: 8
"""
    (DIRECTORY / "fleet.yaml").write_text(config)
    print(
        json.dumps(
            {
                "version": version,
                "cluster_id": cluster["id"],
                "worker_address": bridge,
                "credentials_written": True,
            }
        )
    )


if __name__ == "__main__":
    main()
