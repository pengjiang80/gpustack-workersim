"""Six-size GLM-5.3 PD experiment on a dedicated, disposable Linux lab host.

Uses the original Server and real scheduler. Only Worker execution is simulated.
Run with a mode-0600 /opt/workersim/lab/server-config.json bootstrap config.
Evidence excludes credentials; each size gets a fresh Server and identity volume.
"""

import argparse
import base64
import hashlib
import json
import os
import subprocess
import time
import traceback
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

BASE = "http://127.0.0.1:18080"
ROOT = Path("/opt/workersim")
LAB = ROOT / "lab"
OUT = ROOT / "bench"
SERVER_IMAGE = (
    "gpustack/gpustack@sha256:2573799fd23f9973838323e93ca0c23b41ea29b8fd8a28bdb1f12ba93c5d0f6a"
)
SIM_IMAGE = "gpustack-workersim:0.4.3"
OWNER = "workersim-glm53-20261117"
ROUND = "r6"
REPO = "zai-org/GLM-5.3"
HEADERS = {}


def now():
    return datetime.now(UTC).isoformat()


def emit(event, **data):
    print(json.dumps({"at": now(), "event": event, **data}), flush=True)


def write(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def private_write(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(data, stream)


def docker(*args, check=True, timeout=180):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=check
    )
    return result.stdout.strip()


def api(method, path, body=None, root=False, timeout=45):
    request = urllib.request.Request(
        BASE + ("" if root else "/v2") + path,
        headers=HEADERS,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        # Model responses have no credentials; do not include request headers.
        raise RuntimeError(f"{method} {path}: HTTP {error.code}: {error.read()[:1600]!r}") from None


def rows(path):
    page, result = 1, []
    while True:
        response = api("GET", f"{path}?page={page}&perPage=100")
        result.extend(response["items"])
        if page >= response.get("pagination", {}).get("totalPage", 1):
            return result
        page += 1


def control():
    with urllib.request.urlopen("http://127.0.0.1:20099/status", timeout=15) as response:
        return json.load(response)


def remove_owned(name):
    raw = docker("container", "inspect", name, check=False)
    items = json.loads(raw or "[]")
    if not items:
        return
    item = items[0]
    if item["Config"].get("Labels", {}).get("workersim.benchmark") != OWNER:
        raise RuntimeError(f"Refusing to replace a container not owned by this experiment: {name}")
    docker("stop", "--time", "30", name)
    docker("rm", name)


def metadata(directory):
    result = {"repo": REPO, "fetched_at": now()}
    for name, url in (
        ("model-info", "https://huggingface.co/api/models/" + REPO),
        ("config", f"https://huggingface.co/{REPO}/raw/main/config.json"),
    ):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                content = response.read()
            (directory / f"hf-{name}.json").write_bytes(content)
            result[name + "_sha256"] = hashlib.sha256(content).hexdigest()
            if name == "model-info":
                result["revision"] = json.loads(content).get("sha")
        except Exception as error:
            result[name + "_error"] = str(error)
    write(directory / "metadata.json", result)
    return result


def prepare(count, directory):
    global HEADERS
    remove_owned("workersim")
    remove_owned("gpustack")
    password = json.loads((LAB / "server-config.json").read_text())["bootstrap_password"]
    HEADERS = {
        "Authorization": "Basic " + base64.b64encode(("admin:" + password).encode()).decode(),
        "Content-Type": "application/json",
    }
    server_volume = f"gpustack-glm53-{ROUND}-{count}-data"
    sim_volume = f"workersim-glm53-{ROUND}-{count}-data"
    for volume in (server_volume, sim_volume):
        if json.loads(docker("volume", "inspect", volume, check=False) or "[]"):
            raise RuntimeError(f"Expected a fresh volume; already exists: {volume}")
        docker("volume", "create", "--label", "workersim.benchmark=" + OWNER, volume)
    config_path = LAB / "server-config.json"
    docker(
        "run",
        "-d",
        "--name",
        "gpustack",
        "--label",
        "workersim.benchmark=" + OWNER,
        "--restart",
        "unless-stopped",
        "--ulimit",
        "nofile=65536:65536",
        "-p",
        "18080:80",
        "-v",
        server_volume + ":/var/lib/gpustack",
        "-v",
        str(config_path) + ":/config/server.json:ro",
        SERVER_IMAGE,
        "--config-file",
        "/config/server.json",
        "--disable-update-check",
    )
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            version = api("GET", "/version", root=True, timeout=5)
            if str(version["version"]).lstrip("v") == "2.3.1":
                api("GET", "/users/me", timeout=5)
                break
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(2)
    else:
        raise TimeoutError("Original GPUStack Server did not start within 300 seconds")
    cluster = api(
        "POST",
        "/clusters",
        {
            "name": "workersim-lab",
            "provider": "Docker",
            "description": f"GLM-5.3 5P3D scheduler experiment: {count} simulated B300 Workers",
        },
    )
    token = api("GET", f"/clusters/{cluster['id']}/registration-token")["token"]
    api_key = api("POST", "/api-keys", {"name": OWNER})["value"]
    private_write(LAB / "secrets.json", {"api_key": api_key, "admin_password": password})
    descriptor = os.open(LAB / "secrets.env", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write("GPUSTACK_REGISTRATION_TOKEN=" + token + "\n")
    HEADERS["Authorization"] = "Bearer " + api_key
    bridge = docker(
        "inspect", "gpustack", "--format", "{{range .NetworkSettings.Networks}}{{.Gateway}}{{end}}"
    )
    config = {
        "server": {"url": BASE, "cluster_id": cluster["id"], "request_timeout": 120},
        "network": {"advertise_address": bridge, "worker_ip": bridge},
        "simulation": {
            "namespace": "workersim-glm53",
            "data_dir": "/data",
            "registration_rate": 2,
            "reconcile_interval": 10,
            "download_seconds": 1,
            "startup_seconds": 2,
        },
        "worker_groups": [
            {
                "name_prefix": "sim-b300",
                "count": count,
                "gpu_profile": "b300-288gb",
                "gpu_count": 8,
                "cpu_cores": 192,
                "memory_gib": 2048,
                "disk_gib": 8192,
            }
        ],
    }
    write(LAB / "fleet.yaml", config)
    write(directory / "fleet-config.json", config)
    started = time.monotonic()
    docker(
        "run",
        "-d",
        "--name",
        "workersim",
        "--label",
        "workersim.benchmark=" + OWNER,
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
        sim_volume + ":/data",
        SIM_IMAGE,
    )
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            workers, status = rows("/workers"), control()
            if (
                len(workers) == count
                and all(w["state"] == "ready" for w in workers)
                and (
                    len(status["workers"]) == count
                    and all(
                        w["registered"] and w["watch_connected"] and w["file_watch_connected"]
                        for w in status["workers"]
                    )
                )
            ):
                break
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(2)
    else:
        raise TimeoutError("Fleet did not converge to the requested Ready count and dual watches")
    # Registration reports Ready before the Server's next periodic health probe.
    # Explicitly verify container-to-host networking before starting the clock.
    for port in (20100, 20100 + count - 1):
        health = json.loads(
            docker(
                "exec",
                "gpustack",
                "curl",
                "-fsS",
                "--max-time",
                "5",
                f"http://{bridge}:{port}/healthz",
            )
        )
        if health.get("status") != "ok":
            raise RuntimeError("Server-to-Worker health preflight failed")
    result = {
        "ready_workers": count,
        "gpus": sum(len(w["status"]["gpu_devices"]) for w in workers),
        "gpu_models": sorted({g["name"] for w in workers for g in w["status"]["gpu_devices"]}),
        "unique_ips": len({w["ip"] for w in workers}),
        "registration_seconds": round(time.monotonic() - started, 3),
        "instance_watches": sum(w["watch_connected"] for w in status["workers"]),
        "file_watches": sum(w["file_watch_connected"] for w in status["workers"]),
        "server_image": SERVER_IMAGE,
        "simulator_image": docker("image", "inspect", SIM_IMAGE, "--format", "{{.Id}}"),
        "server_volume": server_volume,
        "simulator_volume": sim_volume,
    }
    assert result["gpus"] == count * 8
    write(directory / "ready.json", result)
    time.sleep(10)
    return result


def model_body(count):
    return {
        "name": f"glm-5.3-5p3d-{count:03d}",
        "source": "huggingface",
        "huggingface_repo_id": REPO,
        "backend": "vLLM",
        "categories": ["llm"],
        "replicas": 1,
        "cluster_id": 1,
        "enable_model_route": True,
        "placement_strategy": "spread",
        "distributed_inference_across_workers": True,
        "backend_parameters": ["--tensor-parallel-size=8", "--gpu-memory-utilization=0.9"],
        "disaggregation": {"mode": "vllm-nixl", "vendor": "nvidia", "readiness": "all"},
        "roles": [
            {"name": "prefill", "replicas": 5},
            {"name": "decode", "replicas": 3},
            {"name": "router", "replicas": 1},
        ],
    }


def sample_resources(directory, elapsed):
    data = {
        "at": now(),
        "elapsed_seconds": round(elapsed, 3),
        "docker": docker("stats", "--no-stream", "--format", "{{json .}}", timeout=20),
        "meminfo": Path("/proc/meminfo").read_text(),
        "loadavg": Path("/proc/loadavg").read_text(),
    }
    with (directory / "resources.jsonl").open("a") as stream:
        stream.write(json.dumps(data) + "\n")


def run_case(count, directory, limit):
    ready = prepare(count, directory)
    hf = metadata(directory)
    body = model_body(count)
    write(directory / "model-request.json", body)
    started_at, started = now(), time.monotonic()
    model = api("POST", "/models", body, timeout=120)
    result = {
        "workers": count,
        "gpus": count * 8,
        "started_at": started_at,
        "model_id": model["id"],
        "model_name": model["name"],
        "metadata": hf,
        "submit_response_seconds": round(time.monotonic() - started, 3),
        "observation_limit_seconds": limit,
        "poll_interval_seconds": 0.5,
        "gpu_assignment_seconds": None,
        "gpu_assignment_lower_bound_seconds": 0,
        "all_assignment_seconds": None,
        "all_running_seconds": None,
        "model_ready_seconds": None,
        "all_running_lower_bound_seconds": 0,
        "first_analyzing_seconds": None,
        "errors_seen": [],
        "result": "observing",
        "ready": ready,
    }
    emit("submitted", workers=count, model_id=model["id"])
    last_signature, last_progress, last_resource = None, -60, -30
    seen_errors = set()
    with (directory / "timeline.jsonl").open("a") as timeline:
        while time.monotonic() - started < limit:
            elapsed = time.monotonic() - started
            read_started = elapsed
            try:
                instances = [i for i in rows("/model-instances") if i["model_id"] == model["id"]]
                model = api("GET", f"/models/{model['id']}")
            except (OSError, RuntimeError) as error:
                timeline.write(
                    json.dumps({"at": now(), "elapsed": elapsed, "api_error": str(error)}) + "\n"
                )
                timeline.flush()
                time.sleep(1)
                continue
            elapsed = time.monotonic() - started
            gpu = [i for i in instances if i.get("role") in ("prefill", "decode")]
            roles = Counter(i.get("role") for i in instances)

            def assigned(instance):
                return bool(instance.get("worker_id")) and instance.get("state") not in (
                    "pending",
                    "analyzing",
                    "error",
                    "unreachable",
                )

            if result["first_analyzing_seconds"] is None and any(
                i["state"] == "analyzing" for i in instances
            ):
                result["first_analyzing_seconds"] = round(elapsed, 3)
            if (
                result["gpu_assignment_seconds"] is None
                and len(gpu) == 8
                and all(assigned(i) for i in gpu)
            ):
                result["gpu_assignment_seconds"] = round(elapsed, 3)
                emit("gpu_group_assigned", workers=count, seconds=result["gpu_assignment_seconds"])
            elif result["gpu_assignment_seconds"] is None:
                result["gpu_assignment_lower_bound_seconds"] = round(read_started, 3)
            if (
                result["all_assignment_seconds"] is None
                and roles == {"prefill": 5, "decode": 3, "router": 1}
                and all(assigned(i) for i in instances)
            ):
                result["all_assignment_seconds"] = round(elapsed, 3)
            for instance in instances:
                if instance["state"] in ("error", "unreachable"):
                    identity = (
                        instance["id"],
                        instance.get("restart_count"),
                        instance["state"],
                        instance.get("state_message"),
                    )
                    if identity not in seen_errors:
                        seen_errors.add(identity)
                        result["errors_seen"].append(
                            {
                                "elapsed": round(elapsed, 3),
                                "id": instance["id"],
                                "role": instance.get("role"),
                                "state": instance["state"],
                                "message": instance.get("state_message"),
                            }
                        )
            compact = [
                {
                    k: i.get(k)
                    for k in (
                        "id",
                        "name",
                        "role",
                        "state",
                        "state_message",
                        "worker_id",
                        "worker_name",
                        "gpu_indexes",
                        "restart_count",
                        "created_at",
                        "updated_at",
                        "computed_resource_claim",
                        "port",
                        "ports",
                        "named_ports",
                        "download_progress",
                        "group_id",
                        "distributed_servers",
                    )
                }
                for i in instances
            ]
            signature = json.dumps(
                {"instances": compact, "model_state": model.get("state")}, sort_keys=True
            )
            if signature != last_signature:
                timeline.write(
                    json.dumps(
                        {
                            "at": now(),
                            "elapsed": round(elapsed, 3),
                            "model_state": model.get("state"),
                            "role_status": model.get("role_status"),
                            "instances": compact,
                        }
                    )
                    + "\n"
                )
                timeline.flush()
                last_signature = signature
                write(
                    directory / "latest.json",
                    {"elapsed": round(elapsed, 3), "model": model, "instances": compact},
                )
            if elapsed - last_progress >= 30:
                emit(
                    "progress",
                    workers=count,
                    elapsed=round(elapsed, 1),
                    model_state=model.get("state"),
                    instances=dict(Counter(i["state"] for i in instances)),
                    errors=len(result["errors_seen"]),
                )
                write(directory / "result.json", result | {"observed_seconds": round(elapsed, 3)})
                last_progress = elapsed
            if roles == {"prefill": 5, "decode": 3, "router": 1} and all(
                i["state"] == "running" for i in instances
            ):
                if result["all_running_seconds"] is None:
                    result["all_running_seconds"] = round(time.monotonic() - started, 3)
                if model.get("state") != "running":
                    time.sleep(0.5)
                    continue
                router = next(i for i in instances if i.get("role") == "router")
                try:
                    with urllib.request.urlopen(
                        f"http://{router['worker_ip']}:{router['port']}/workers", timeout=5
                    ) as response:
                        registry = json.load(response)
                    actual = {(row["url"], row["worker_type"]) for row in registry["workers"]}
                    expected = {(f"http://{i['worker_ip']}:{i['port']}", i["role"]) for i in gpu}
                    if len(registry["workers"]) != 8 or actual != expected:
                        time.sleep(0.5)
                        continue
                except (OSError, ValueError, KeyError):
                    time.sleep(0.5)
                    continue
                write(directory / "final-router-registry.json", registry)
                result["model_ready_seconds"] = round(time.monotonic() - started, 3)
                result["result"] = "success_with_errors" if result["errors_seen"] else "success"
                result["gpu_worker_count"] = len({i["worker_id"] for i in gpu})
                result["allocated_gpus"] = sum(len(i.get("gpu_indexes") or []) for i in gpu)
                result["final_model_state"] = model.get("state")
                result["model_degradations"] = model.get("degradations")
                write(directory / "final-model.json", model)
                write(directory / "final-instances.json", instances)
                break
            result["all_running_lower_bound_seconds"] = round(read_started, 3)
            if elapsed - last_resource >= 30:
                sample_resources(directory, elapsed)
                last_resource = elapsed
            time.sleep(0.5)
        else:
            result["result"] = "timeout"
    result["finished_at"] = now()
    result["observed_seconds"] = round(time.monotonic() - started, 3)
    if (
        result["gpu_assignment_seconds"] is not None
        and result["first_analyzing_seconds"] is not None
    ):
        result["analyzing_to_assignment_seconds"] = round(
            result["gpu_assignment_seconds"] - result["first_analyzing_seconds"], 3
        )
    write(directory / "final-workers.json", rows("/workers"))
    write(directory / "final-files.json", rows("/model-files"))
    write(directory / "runtime.json", control())
    # This is a synthetic backend readiness check, not evidence of actual inference.
    if result["all_running_seconds"] is not None:
        try:
            response = api(
                "POST",
                "/v1/chat/completions",
                {
                    "model": model["name"],
                    "messages": [{"role": "user", "content": "WorkerSim readiness probe"}],
                    "max_tokens": 8,
                },
                root=True,
                timeout=30,
            )
            write(directory / "gateway.json", response)
            result["gateway_simulated"] = response.get("simulated") is True
        except Exception as error:
            result["gateway_error"] = str(error)
    write(directory / "result.json", result)
    emit(
        "finished",
        workers=count,
        result=result["result"],
        gpu_assignment_seconds=result["gpu_assignment_seconds"],
        all_running_seconds=result["all_running_seconds"],
        errors=len(result["errors_seen"]),
    )
    return result


def collect_logs(directory):
    for name in ("gpustack", "workersim"):
        process = subprocess.run(
            ["docker", "logs", "--timestamps", name], capture_output=True, text=True, timeout=90
        )
        (directory / f"{name}.log").write_text(process.stdout + process.stderr)
    sample_resources(directory, -1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[10, 20, 40, 60, 80, 100])
    parser.add_argument("--timeout", type=float, default=1800)
    args = parser.parse_args()
    if any(size not in (10, 20, 40, 60, 80, 100) for size in args.sizes):
        raise ValueError("Only the six requested sizes may be created")
    OUT.mkdir(parents=True, exist_ok=True)
    write(
        OUT / "experiment.json",
        {
            "started_at": now(),
            "sizes": args.sizes,
            "timeout": args.timeout,
            "server_image": SERVER_IMAGE,
            "simulator_image": SIM_IMAGE,
            "model_repo": REPO,
            "roles": {"prefill": 5, "decode": 3, "router": 1},
            "success_requires": "9 Running instances, Model Running, exact 5P3D router registry",
            "tp": 8,
            "memory_utilization": 0.9,
            "fresh_server_and_worker_volumes_per_size": True,
            "trials_per_size": 1,
            "benchmark_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "network_mode": "same IP, independent Worker credentials and API ports",
            "timing": "POST to 8 GPU assignments / all 9 Running; monotonic clock",
            "inference": "synthetic WorkerSim; HF metadata and Server scheduler are real",
        },
    )
    results = []
    for count in args.sizes:
        directory = OUT / f"workers-{count:03d}"
        directory.mkdir(exist_ok=True)
        if (directory / "result.json").exists():
            previous = json.loads((directory / "result.json").read_text())
            if previous.get("result") in (
                "success",
                "success_with_errors",
                "timeout",
                "infrastructure_error",
            ):
                results.append(previous)
                continue
            raise RuntimeError("An interrupted case exists; retain its evidence before rerunning")
        emit("preparing", workers=count)
        try:
            result = run_case(count, directory, args.timeout)
        except Exception as error:
            result = {
                "workers": count,
                "result": "infrastructure_error",
                "finished_at": now(),
                "error": str(error),
                "traceback": traceback.format_exc(),
            }
            write(directory / "result.json", result)
            emit("infrastructure_error", workers=count, error=str(error))
        finally:
            try:
                collect_logs(directory)
            except Exception as error:
                emit("evidence_error", workers=count, error=str(error))
        results.append(result)
        write(OUT / "results.json", results)
    emit("experiment_complete", outcomes={str(r["workers"]): r["result"] for r in results})


if __name__ == "__main__":
    main()
