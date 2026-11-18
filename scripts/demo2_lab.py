"""Prepare owned WorkerSim demo resources through the existing Server's public API.

Credentials stay in the user's shared file; generated tokens stay in an ignored directory.
Never updates the Server, existing clusters, or unrelated deployments.
"""

import argparse
import http.cookiejar
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "workersim-demo2"


class Api:
    def __init__(self, server, credential_file):
        self.server = server.rstrip("/")
        password = re.split(
            r"password\s*[:\uff1a]\s*",
            Path(credential_file).read_text(encoding="utf-8-sig"),
            maxsplit=1,
            flags=re.I,
        )[-1].strip()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        request = urllib.request.Request(
            self.server + "/auth/login",
            data=urllib.parse.urlencode({"username": "admin", "password": password}).encode(),
        )
        with self.opener.open(request, timeout=30) as response:
            response.read()

    def call(self, method, path, body=None):
        request = urllib.request.Request(
            self.server + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with self.opener.open(request, timeout=120) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"{method} {path}: {error.code} {error.read(2000).decode()}"
            ) from None

    def items(self, path):
        rows = []
        page = 1
        while True:
            result = self.call(
                "GET", path + ("&" if "?" in path else "?") + f"perPage=100&page={page}"
            )
            if isinstance(result, list):
                return result
            rows.extend(result["items"])
            if page >= result.get("pagination", {}).get("totalPage", 1):
                return rows
            page += 1


def private_write(path, text):
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def inspect(api):
    print(json.dumps({"version": api.call("GET", "/version")}), flush=True)
    me = api.call("GET", "/v2/users/me")
    print(
        json.dumps(
            {"account": {k: me.get(k) for k in ("id", "username", "is_admin", "principal_id")}}
        )
    )
    for path in ("/v2/clusters", "/v2/workers", "/v2/models", "/v2/inference-backends"):
        rows = api.items(path)
        selected = (
            "id",
            "name",
            "state",
            "cluster_id",
            "backend",
            "categories",
            "replicas",
            "ready_replicas",
            "default_version",
            "versions",
        )
        print(
            json.dumps(
                {"path": path, "items": [{k: r[k] for k in selected if k in r} for r in rows]}
            ),
            flush=True,
        )


def bootstrap(api, private_dir):
    version = api.call("GET", "/version")
    if version["version"] != "v2.3.1-ent2":
        raise RuntimeError(f"Review protocol compatibility before using {version}")
    clusters = api.items("/v2/clusters")
    matches = [c for c in clusters if c["name"] == NAME]
    if matches:
        cluster = matches[0]
        if "WorkerSim" not in (cluster.get("description") or ""):
            raise RuntimeError("Refusing to reuse an unowned cluster")
    else:
        cluster = api.call(
            "POST",
            "/v2/clusters",
            {
                "name": NAME,
                "provider": "Docker",
                "description": (
                    "WorkerSim: 50 CPU-simulated 8xB300 nodes for UI and scheduling tests; "
                    "no real inference."
                ),
            },
        )
    token = api.call("GET", f"/v2/clusters/{cluster['id']}/registration-token")["token"]
    private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_file = private_dir / "api.json"
    if key_file.exists():
        key = json.loads(key_file.read_text(encoding="utf-8"))
    else:
        key = api.call(
            "POST",
            "/v2/api-keys",
            {
                "name": NAME,
                "description": "WorkerSim demo lifecycle and synthetic gateway verification",
                "scope": ["*"],
            },
        )
        private_write(key_file, json.dumps(key))
    private_write(
        private_dir / "secrets.env",
        f"GPUSTACK_REGISTRATION_TOKEN={token}\nGPUSTACK_API_KEY={key['value']}\n",
    )
    config = {
        "server": {
            "url": "http://127.0.0.1:8080",
            "cluster_id": cluster["id"],
            "allow_version_mismatch": True,
            "request_timeout": 60,
        },
        "network": {"advertise_address": "172.17.0.1", "worker_ip": "172.17.0.1"},
        "simulation": {
            "namespace": NAME,
            "data_dir": "/data",
            "registration_rate": 2,
            "reconcile_interval": 30,
        },
        "worker_groups": [
            {
                "name_prefix": "sim-b300",
                "count": 50,
                "gpu_profile": "b300-288gb",
                "gpu_count": 8,
                "cpu_cores": 192,
                "memory_gib": 2048,
                "disk_gib": 8192,
                "labels": {"environment": NAME},
            }
        ],
    }
    (private_dir / "fleet.yaml").write_text(json.dumps(config, indent=2), encoding="utf-8")
    (private_dir / "metadata.json").write_text(
        json.dumps(
            {
                "cluster_id": cluster["id"],
                "version": version,
                "preexisting_cluster_ids": [c["id"] for c in clusters if c["id"] != cluster["id"]],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps({"cluster_id": cluster["id"], "tokens_saved": True, "workers_configured": 50}),
        flush=True,
    )


def models(api, private_dir):
    cluster_id = json.loads((private_dir / "metadata.json").read_text())["cluster_id"]
    existing = api.items("/v2/models")
    manifest = []
    for short, repo in (
        ("deepseek-v3.2", "deepseek-ai/DeepSeek-V3.2"),
        ("glm-5.3", "zai-org/GLM-5.3"),
        ("kimi-k2.5", "moonshotai/Kimi-K2.5"),
        ("qwen3.5-397b-a17b", "Qwen/Qwen3.5-397B-A17B"),
    ):
        manifest.append(
            {
                "name": "workersim-" + short,
                "source": "huggingface",
                "huggingface_repo_id": repo,
                "categories": ["llm"],
                "backend": "vLLM",
                "backend_version": "0.33.0",
                "distributed_inference_across_workers": True,
                "backend_parameters": [
                    "--tensor-parallel-size=8",
                    "--gpu-memory-utilization=0.9",
                    "--max-model-len=32768",
                    "--trust-remote-code",
                ],
            }
        )
    for original_name in (
        "bge-m3",
        "bge-reranker-v2-m3",
        "qwen3-asr-0.6b",
        "qwen3-tts-12hz-1.7b-customvoice",
    ):
        original = next(m for m in existing if m["name"] == original_name and m["cluster_id"] == 1)
        body = {
            k: original[k]
            for k in (
                "source",
                "huggingface_repo_id",
                "backend",
                "backend_version",
                "backend_parameters",
                "categories",
                "meta",
            )
        }
        body["name"] = "workersim-" + original_name
        body["distributed_inference_across_workers"] = False
        if not any("gpu-memory-utilization" in p for p in body["backend_parameters"]):
            body["backend_parameters"] = body["backend_parameters"] + [
                "--gpu-memory-utilization=0.1"
            ]
        manifest.append(body)
    for body in manifest:
        body.update(
            cluster_id=cluster_id,
            replicas=2,
            placement_strategy="spread",
            cpu_offloading=False,
            description=(
                "WorkerSim demo: synthetic deployment, metrics and responses; no real inference."
            ),
            enable_model_route=True,
        )
        matches = [m for m in existing if m["name"] == body["name"]]
        if matches:
            result = matches[0]
            if result["cluster_id"] != cluster_id:
                raise RuntimeError("Name collision with unrelated model")
        else:
            result = api.call("POST", "/v2/models", body)
        print(
            json.dumps(
                {"model_id": result["id"], "name": result["name"], "replicas": result["replicas"]}
            ),
            flush=True,
        )
    (private_dir / "model-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def status(api, private_dir):
    cluster_id = json.loads((private_dir / "metadata.json").read_text())["cluster_id"]
    workers = [w for w in api.items("/v2/workers") if w["cluster_id"] == cluster_id]
    print(
        json.dumps(
            {
                "workers": len(workers),
                "ready": sum(w["state"] == "ready" for w in workers),
                "gpus": sum(len(w.get("status", {}).get("gpu_devices") or []) for w in workers),
            }
        ),
        flush=True,
    )
    for resource in ("models", "model-instances", "model-files"):
        rows = api.items("/v2/" + resource)
        if resource == "model-files":
            rows = [r for r in rows if r.get("worker_id") in {w["id"] for w in workers}]
        else:
            rows = [r for r in rows if r.get("cluster_id") == cluster_id]
        print(
            json.dumps(
                {
                    "resource": resource,
                    "items": [
                        {
                            k: r[k]
                            for k in (
                                "id",
                                "name",
                                "state",
                                "state_message",
                                "replicas",
                                "ready_replicas",
                                "download_progress",
                                "worker_id",
                                "port",
                                "gpu_indexes",
                            )
                            if k in r
                        }
                        for r in rows
                    ],
                }
            ),
            flush=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "bootstrap", "models", "status"))
    parser.add_argument("--server", default="http://demo2.gpustack.cn:8080")
    parser.add_argument("--credential-file", default=str(ROOT.parent / "Info_Shared/GPUStack.txt"))
    parser.add_argument("--private-dir", type=Path, default=ROOT / ".workersim/private-demo2")
    args = parser.parse_args()
    api = Api(args.server, args.credential_file)
    if args.action == "inspect":
        inspect(api)
    elif args.action == "bootstrap":
        bootstrap(api, args.private_dir)
    elif args.action == "models":
        models(api, args.private_dir)
    else:
        status(api, args.private_dir)


if __name__ == "__main__":
    main()
