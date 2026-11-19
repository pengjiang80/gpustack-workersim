"""Disposable Linux/Docker acceptance against an unmodified GPUStack Server.

Run only on a dedicated test runner. Creates its own Server, volume, and fleet,
then removes them. Generated credentials stay in a private temporary directory.
"""

import argparse
import base64
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

import yaml

from workersim import TARGET_VERSION, __version__

from .audit_worker_image import audit_image


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.STDOUT).strip()


def call(url, *, method="GET", body=None, auth=None):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    request = urllib.request.Request(
        url, method=method, headers=headers,
        data=json.dumps(body).encode() if body is not None else None,
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def wait_until(probe, timeout=600):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = probe()
            if result:
                return result
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(5)
    raise TimeoutError("Acceptance condition did not converge")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=1000)
    parser.add_argument("--output", default="outputs/scale-acceptance")
    parser.add_argument("--db-pool-size", type=int, default=80)
    parser.add_argument("--db-max-overflow", type=int, default=10)
    parser.add_argument("--db-pool-timeout", type=int, default=120)
    parser.add_argument("--external-postgres", action="store_true")
    parser.add_argument("--postgres-max-connections", type=int, default=768)
    args = parser.parse_args()
    if not 1 <= args.workers <= 1000:
        raise ValueError("Worker count must be 1..1000")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    name = "workersim-scale-" + secrets.token_hex(4)
    base = "http://127.0.0.1:18080"
    password = secrets.token_urlsafe(32)
    auth = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()
    credentials = [password]
    process = None
    report = {
        "workersim_version": __version__, "gpustack_version": TARGET_VERSION,
        "communication": "native", "watches_per_worker": 6, "registration_concurrency": 8,
        "registration_rate": 10, "host_cpu_count": os.cpu_count(),
        "db_pool_size": args.db_pool_size, "db_max_overflow": args.db_max_overflow,
        "db_pool_timeout": args.db_pool_timeout,
        "external_postgres": args.external_postgres,
        "postgres_max_connections": (
            args.postgres_max_connections if args.external_postgres else None
        ),
    }
    with tempfile.TemporaryDirectory(prefix="workersim-scale-") as temp:
        private = Path(temp)
        server_config = private / "server.json"
        server_config.write_text(json.dumps({
            "bootstrap_password": password, "disable_update_check": True, "enable_worker": False,
        }))
        server_config.chmod(0o600)
        try:
            report["stage"] = "source_audit"
            report["worker_source_audit"] = audit_image(
                f"gpustack/gpustack:v{TARGET_VERSION}", output
            )
            if args.external_postgres:
                db_password = secrets.token_urlsafe(32)
                credentials.append(db_password)
                db_env = private / "postgres.env"
                db_env.write_text(
                    "POSTGRES_USER=workersim\nPOSTGRES_DB=workersim\n"
                    f"POSTGRES_PASSWORD={db_password}\n"
                )
                db_env.chmod(0o600)
                docker(
                    "run", "-d", "--name", name + "-db", "--env-file", str(db_env),
                    "--shm-size", "512m", "-v", f"{name}-db:/var/lib/postgresql/data",
                    "postgres:16-alpine", "-c", f"max_connections={args.postgres_max_connections}",
                )

                def db_ready():
                    result = subprocess.run(
                        ["docker", "exec", name + "-db", "pg_isready", "-U", "workersim"],
                        capture_output=True,
                    )
                    return result.returncode == 0

                wait_until(db_ready, timeout=120)
                db_ip = docker(
                    "inspect", name + "-db", "--format",
                    "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                )
                settings = json.loads(server_config.read_text())
                settings["database_url"] = (
                    f"postgresql://workersim:{db_password}@{db_ip}:5432/workersim"
                )
                server_config.write_text(json.dumps(settings))
            docker(
                "run", "-d", "--name", name, "--ulimit", "nofile=65536:65536",
                "-p", "127.0.0.1:18080:80", "-v", f"{name}:/var/lib/gpustack",
                "-v", f"{server_config}:/config/server.json:ro",
                "-e", f"GPUSTACK_DB_POOL_SIZE={args.db_pool_size}",
                "-e", f"GPUSTACK_DB_MAX_OVERFLOW={args.db_max_overflow}",
                "-e", f"GPUSTACK_DB_POOL_TIMEOUT={args.db_pool_timeout}",
                f"gpustack/gpustack:v{TARGET_VERSION}",
                "--config-file", "/config/server.json", "--disable-update-check",
            )
            report["actual_server_version"] = wait_until(lambda: call(base + "/version"))
            report["stage"] = "registration_and_watches"

            def admin(method, path, body=None):
                return call(base + "/v2" + path, method=method, body=body, auth=auth)

            cluster = admin("POST", "/clusters", {
                "name": name, "provider": "Docker", "description": "Disposable scale acceptance",
            })
            token = admin("GET", f"/clusters/{cluster['id']}/registration-token")["token"]
            api_key = admin("POST", "/api-keys", {"name": name})["value"]
            credentials.extend([token, api_key])
            bridge = docker(
                "inspect", name, "--format",
                "{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}",
            )
            config = yaml.safe_load(Path("configs/fleet-1000-b300.yaml").read_text())
            config["server"]["cluster_id"] = cluster["id"]
            config["network"].update(advertise_address=bridge, worker_ip=bridge)
            config["simulation"].update(namespace=name, data_dir=str(private / "state"))
            config["worker_groups"][0]["count"] = args.workers
            config_path = private / "fleet.yaml"
            config_path.write_text(yaml.safe_dump(config))
            env = os.environ | {"GPUSTACK_REGISTRATION_TOKEN": token, "GPUSTACK_API_KEY": api_key}
            command = [sys.executable, "-m", "workersim", "--config", str(config_path)]
            started = time.monotonic()
            with (private / "worker.log").open("w") as log:
                process = subprocess.Popen([*command, "run"], env=env, stdout=log, stderr=log)

            def fleet_ready():
                if process.poll() is not None:
                    raise RuntimeError("WorkerSim process exited")
                workers = call("http://127.0.0.1:20099/status")["workers"]
                report["last_progress"] = {
                    "registered": sum(w["registered"] for w in workers),
                    "watches": sum(sum(w["watch_states"].values()) for w in workers),
                    "report_errors": sum(w["report_errors"] for w in workers),
                }
                print(json.dumps(report["last_progress"]), flush=True)
                return workers if len(workers) == args.workers and all(
                    w["registered"] and all(w["watch_states"].values())
                    for w in workers
                ) else None

            wait_until(fleet_ready, timeout=900)
            report["registered_with_six_watches_seconds"] = round(time.monotonic() - started, 3)
            report["stage"] = "all_workers_ready"

            def server_workers():
                workers, page = [], 1
                while True:
                    data = admin("GET", f"/workers?page={page}&perPage=100")
                    workers.extend(data["items"])
                    if page >= data["pagination"]["totalPage"]:
                        return workers
                    page += 1

            def all_ready():
                workers = server_workers()
                report["last_server_states"] = dict(Counter(w["state"] for w in workers))
                return workers if len(workers) == args.workers and all(
                    w["state"] == "ready" for w in workers
                ) else None

            workers = wait_until(all_ready)
            report["server_ready_seconds"] = round(time.monotonic() - started, 3)
            report["ready_workers"] = len(workers)
            report["gpu_devices"] = sum(len(w["status"]["gpu_devices"]) for w in workers)
            assert report["gpu_devices"] == args.workers * 8
            # Observe another heartbeat/status cycle, then verify native API and scrape endpoints.
            report["stage"] = "heartbeat_soak"
            time.sleep(35)
            assert all_ready()
            for slot in (0, args.workers - 1):
                assert call(f"http://127.0.0.1:{20100 + slot}/healthz")["status"] == "ok"
                with urllib.request.urlopen(f"http://127.0.0.1:{21100 + slot}/metrics") as response:
                    assert b"worker_node_gpu_info" in response.read()
            report["stage"] = "replicas_2_1_0"
            subprocess.run([
                *command, "scenario", "--replicas", "2", "1", "0", "--timeout", "180",
                "--output", str(output / "replicas.json"),
            ], env=env, check=True, stdout=subprocess.DEVNULL)
            summaries = fleet_ready()
            assert summaries
            report["active_watches"] = sum(sum(w["watch_states"].values()) for w in summaries)
            report["report_errors"] = sum(w["report_errors"] for w in summaries)
            report["worker_process_rss_kib"] = int(
                next(line.split()[1] for line in Path(f"/proc/{process.pid}/status").read_text()
                     .splitlines() if line.startswith("VmRSS:"))
            )
            report["server_container_stats"] = docker(
                "stats", "--no-stream", "--format", "{{json .}}", name
            )
            report["status"] = "passed"
            report["stage"] = "complete"
        except Exception as error:
            report["error"] = type(error).__name__
            raise
        finally:
            try:
                report["server_container_stats"] = docker(
                    "stats", "--no-stream", "--format", "{{json .}}", name
                )
            except subprocess.CalledProcessError:
                pass
            if args.external_postgres:
                try:
                    activity = docker(
                        "exec", name + "-db", "psql", "-U", "workersim", "-d", "workersim",
                        "-At", "-c", "SELECT json_build_object('state',state,'wait_type',"
                        "wait_event_type,'wait_event',wait_event,'count',count(*)) "
                        "FROM pg_stat_activity GROUP BY state,wait_event_type,wait_event;",
                    )
                    (output / "database-activity.jsonl").write_text(activity + "\n")
                except subprocess.CalledProcessError:
                    pass
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            try:
                logs = docker("logs", name)
            except subprocess.CalledProcessError:
                logs = "Server container was not created"
            identities = private / "state" / name / "identities.json"
            if identities.exists():
                for identity in json.loads(identities.read_text())["workers"].values():
                    credentials.extend(
                        identity[key]
                        for key in ("token", "registration_token") if identity.get(key)
                    )
            for credential in credentials:
                logs = logs.replace(credential, "[redacted]")
            # Server startup can include private generated credentials unrelated to our test keys.
            logs = "\n".join(line for line in logs.splitlines() if not any(
                word in line.lower() for word in ("password", "token", "secret", "api key")
            ))
            (output / "server.log").write_text(logs)
            if (private / "worker.log").exists():
                logs = (private / "worker.log").read_text()
                for credential in credentials:
                    logs = logs.replace(credential, "[redacted]")
                (output / "worker.log").write_text(logs)
            cleanup = [("rm", "-f", name), ("volume", "rm", name)]
            if args.external_postgres:
                cleanup.extend([("rm", "-f", name + "-db"), ("volume", "rm", name + "-db")])
            for command in cleanup:
                subprocess.run(
                    ["docker", *command], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            report.setdefault("status", "failed")
            (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
