"""Loopback protocol fixture. This is not the real GPUStack scheduler."""

import asyncio
import copy
import json
import socket

import pytest
import pytest_asyncio
from aiohttp import web

from workersim.config import Config, NetworkConfig, SimulationConfig, WorkerGroup
from workersim.http_server import listen

_allocated_ports = set()


def free_port():
    while True:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in _allocated_ports and not 31000 <= port <= 32999:
            _allocated_ports.add(port)
            return port


@pytest.fixture
def config(tmp_path):
    config = Config(
        network=NetworkConfig(
            api_port_base=free_port(),
            metrics_port_base=free_port(),
            control_port=free_port(),
            instance_port_start=31000,
            instance_port_end=31999,
            instance_port_stride=0,
        ),
        simulation=SimulationConfig(
            data_dir=str(tmp_path),
            heartbeat_interval=0.1,
            status_interval=0.1,
            reconcile_interval=0.2,
            download_seconds=0.01,
            startup_seconds=0.01,
            registration_rate=500,
        ),
        worker_groups=[WorkerGroup(count=1, gpu_count=2)],
    )
    config.validate()
    return config


class ControlPlane:
    def __init__(self):
        self.workers = {}
        self.instances = {}
        self.files = {}
        self.models = {}
        self.watchers = {}
        self.statuses = {}
        self.heartbeats = {}
        self.requests = []
        self.managed_reports = []
        self.reject_report = None

    async def handler(self, request):
        path = request.path
        if path == "/version":
            return web.json_response({"version": "2.3.1", "git_commit": "458030e8"})
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if path == "/v2/workers" and request.method == "POST":
            body = await request.json()
            existing = next((w for w in self.workers.values() if w["name"] == body["name"]), None)
            if existing:
                assert token == existing["token"]
                worker = existing | body
            else:
                assert token == "registration-secret"
                key = len(self.workers) + 1
                worker = body | {
                    "id": key,
                    "token": f"worker-secret-{key}",
                    "worker_uuid": f"server-assigned-{key}",
                    "cluster_id": 1,
                }
            self.workers[worker["id"]] = worker
            return web.json_response(worker)
        worker = next((w for w in self.workers.values() if w["token"] == token), None)
        if worker is None:
            raise web.HTTPUnauthorized()
        worker_id = worker["id"]
        self.requests.append((worker_id, request.method, path))
        if path == "/v2/worker-status":
            self.statuses[worker_id] = await request.json()
            return web.Response(status=204)
        if path == "/v2/worker-heartbeat":
            self.heartbeats[worker_id] = self.heartbeats.get(worker_id, 0) + 1
            return web.Response(status=204)
        resource = "model-files" if path.startswith("/v2/model-files") else "model-instances"
        rows = self.files if resource == "model-files" else self.instances
        if path.startswith("/v2/models/"):
            return web.json_response(self.models.get(int(path.split("/")[3]), {"backend": "vLLM"}))
        if path in ("/v2/model-instances", "/v2/model-files"):
            if request.query.get("watch") == "true":
                queue = asyncio.Queue()
                self.watchers[(worker_id, resource)] = queue
                response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await response.prepare(request)
                for instance in list(rows.values()):
                    await response.write(
                        (json.dumps({"type": "CREATED", "data": instance}) + "\n\n").encode()
                    )
                try:
                    while True:
                        try:
                            event = await asyncio.wait_for(queue.get(), 0.1)
                        except asyncio.TimeoutError:
                            event = {"type": "HEARTBEAT", "data": None}
                        if event.get("type") == "DISCONNECT":
                            await response.write_eof()
                            return response
                        await response.write((json.dumps(event) + "\n\n").encode())
                except (ConnectionError, asyncio.CancelledError):
                    return response
                finally:
                    if self.watchers.get((worker_id, resource)) is queue:
                        self.watchers.pop((worker_id, resource), None)
            return web.json_response({"items": list(rows.values()), "pagination": {"totalPage": 1}})
        if path.startswith("/v2/model-files/"):
            key = int(path.split("/")[3])
            if key not in self.files:
                raise web.HTTPNotFound()
            item = self.files[key]
            assert item["worker_id"] == worker_id
            if request.method == "GET":
                return web.json_response(item)
            item.update(await request.json())
            await self.emit("UPDATED", item, "model-files")
            return web.json_response(item)
        if path.startswith("/v2/model-instances/"):
            key = int(path.split("/")[3])
            if key not in self.instances:
                raise web.HTTPNotFound()
            instance = self.instances[key]
            from workersim.hardware import local_member

            member = local_member(instance, worker_id)
            assert member is not None
            if request.method == "GET":
                return web.json_response(instance)
            body = await request.json()
            if self.reject_report:
                raise web.HTTPConflict()
            if path.endswith("workload-report"):
                assert body["restart_count"] == instance["restart_count"]
                if instance["distributed_servers"].get("workloads") is not None:
                    assert body.get("state") in (None, "error")
                else:
                    fields = body.copy()
                    fields.pop("restart_count")
                    fields.update(fields.pop("preparation", {}))
                    member.update(fields)
                    await self.emit("UPDATED", instance)
                    return web.json_response(instance)
                self.managed_reports.append(copy.deepcopy(body))
                if "preparation" in body:
                    instance.update(body["preparation"])
                    instance["state"] = "initializing"
                if "workloads" in body:
                    instance["distributed_servers"]["workloads"] = [
                        w
                        for w in instance["distributed_servers"]["workloads"]
                        if w["worker_id"] != worker_id
                    ] + body["workloads"]
                if "observations" in body:
                    for observation in body["observations"]:
                        for workload in instance["distributed_servers"]["workloads"]:
                            if observation["name"] == workload["name"]:
                                workload["state"] = observation["state"]
                                if workload.get("component"):
                                    workload["component"]["ready"] = observation["component_ready"]
                                for placement in workload.get("dp_rank_placements", []):
                                    if placement["node_rank"] == 0:
                                        placement["ready"] = observation["rank_readiness"].get(
                                            str(placement["dp_rank"]), False
                                        )
                    if all(
                        w["state"] == "running"
                        for w in instance["distributed_servers"]["workloads"]
                    ):
                        instance["state"] = "running"
            elif path.endswith("stop-confirmation"):
                instance["distributed_servers"]["legacy_stop"]["confirmed_worker_ids"] = [worker_id]
            else:
                instance.update(body)
            await self.emit("UPDATED", instance)
            return web.json_response(instance)
        raise web.HTTPNotFound()

    async def emit(self, kind, instance, resource="model-instances"):
        for (_, watched), queue in list(self.watchers.items()):
            if watched == resource:
                await queue.put({"type": kind, "data": copy.deepcopy(instance)})

    async def assign(self, worker_id=1, key=10, managed=False):
        instance = {
            "id": key,
            "name": f"smoke-{key}",
            "model_id": 1,
            "model_name": "smoke",
            "worker_id": worker_id,
            "restart_count": 0,
            "state": "scheduled",
            "gpu_indexes": [0],
            "port": None,
            "ports": [],
            "computed_resource_claim": {"ram": 1024**3, "vram": {"0": 8 * 1024**3}},
            "distributed_servers": {
                "orchestration_mode": "managed" if managed else "legacy",
                "subordinate_workers": [],
                "workloads": [] if managed else None,
            },
        }
        self.instances[key] = instance
        await self.emit("CREATED", instance)
        return instance


@pytest_asyncio.fixture
async def control_plane(config):
    plane = ControlPlane()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", plane.handler)
    port = free_port()
    runner = await listen(app, "127.0.0.1", port)
    config.server.url = f"http://127.0.0.1:{port}"
    yield plane
    await runner.cleanup()


async def eventually(predicate, timeout=5):  # noqa: ASYNC109
    async with asyncio.timeout(timeout):
        while not predicate():  # noqa: ASYNC110 - poll externally observable test conditions
            await asyncio.sleep(0.02)
