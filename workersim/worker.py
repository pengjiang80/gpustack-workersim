"""One independently authenticated virtual Worker and its background loops."""

import asyncio
import logging
import time

import aiohttp

from workersim import TARGET_VERSION
from workersim.client import APIError, Client, cancel_tasks
from workersim.config import GIB
from workersim.hardware import Hardware, local_member
from workersim.http_server import listen, metrics_app, worker_app
from workersim.model_files import ModelFiles
from workersim.port_reservations import PortReservations
from workersim.runtime import Runtime

log = logging.getLogger(__name__)


class VirtualWorker:
    def __init__(self, fleet, spec):
        self.fleet, self.config, self.spec = fleet, fleet.config, spec
        self.ip = self.config.network.ip_for_slot(spec.slot)
        self.advertise_address = (
            self.ip if self.config.network.worker_ip_cidr else self.config.network.advertise_address
        )
        self.identity = dict(fleet.store.workers.get(spec.name, {}))
        self.worker_id = self.identity.get("id", 0)
        self.registration_token = self.identity.get("registration_token", "")
        self.hardware = Hardware(self.config, spec)
        self.runtime = Runtime(self)
        self.model_files = ModelFiles(self)
        self.port_reservations = PortReservations(self)
        self.instances: dict[int, dict] = {}
        self.online = True
        self.watch_connected = False
        self.file_watch_connected = False
        self.watch_connections = {"model-instances": 0, "model-files": 0}
        self.event_counts = {"model-instances": 0, "model-files": 0}
        self.snapshot_requested = True
        self.report_errors = 0
        self.stale_reports = 0
        self.deleted_assignments = 0
        self.last_error: str | None = None
        self.client: Client | None = None
        self.listeners = []
        self.tasks = []

    def status_payload(self):
        n, g = self.config.network, self.spec.group
        return {
            "advertise_address": self.advertise_address,
            "hostname": self.spec.name,
            "ip": self.ip,
            "ifname": "eth0",
            "port": n.api_port_base + self.spec.slot,
            "metrics_port": n.metrics_port_base + self.spec.slot,
            "worker_uuid": self.identity.get("worker_uuid", ""),
            "machine_id": f"workersim-{self.config.simulation.namespace}-{self.spec.name}",
            "proxy_mode": "worker",
            "state_message": None,
            "system_reserved": {"ram": g.reserved_ram_gib * GIB, "vram": g.reserved_vram_gib * GIB},
            "status": self.hardware.sample(
                self.worker_id, self.instances, self.runtime.running_ids
            ),
        }

    async def start(self, registration_token):
        self.registration_token = registration_token or self.registration_token
        n = self.config.network
        self.listeners.append(
            await listen(worker_app(self), n.bind_host, n.api_port_base + self.spec.slot)
        )
        self.listeners.append(
            await listen(metrics_app(self), n.bind_host, n.metrics_port_base + self.spec.slot)
        )
        # A bound Worker re-registers using its own credential, as required by ownership checks.
        register = await Client(
            self.config.server,
            self.identity.get("token", registration_token),
            connector=self.fleet.connector,
        ).open()
        try:
            body = self.status_payload() | {
                "name": self.spec.name,
                "worker_version": TARGET_VERSION,
                "labels": {
                    **self.spec.group.labels,
                    "os": "Linux",
                    "arch": "amd64",
                    "worker-name": self.spec.name,
                    "simulated": "true",
                    "workersim.namespace": self.config.simulation.namespace,
                },
            }
            if self.config.server.cluster_id is not None:
                body["cluster_id"] = self.config.server.cluster_id
            result = await register.request("POST", "/workers", body=body)
        finally:
            await register.close()
        if not result.get("token") or not result.get("worker_uuid") or not result.get("id"):
            raise RuntimeError("Registration response is missing Worker identity or token")
        self.identity = {k: result[k] for k in ("id", "token", "worker_uuid", "cluster_id")}
        if self.registration_token:
            self.identity["registration_token"] = self.registration_token
        self.worker_id = self.identity["id"]
        self.fleet.store.save(self.spec.name, self.identity)
        self.client = await Client(
            self.config.server, self.identity["token"], connector=self.fleet.connector
        ).open()
        await self.client.request("POST", "/worker-status", body=self.status_payload())
        log.info("Registered %s, Worker %s", self.spec.name, self.worker_id)
        self.tasks = [
            asyncio.create_task(
                self.periodic("heartbeat", self.config.simulation.heartbeat_interval)
            ),
            asyncio.create_task(self.periodic("status", self.config.simulation.status_interval)),
            asyncio.create_task(self.watch()),
            asyncio.create_task(self.watch("model-files")),
            asyncio.create_task(self.reconcile()),
        ]
        for task in self.tasks:
            task.add_done_callback(self.background_done)

    def background_done(self, task):
        if not task.cancelled() and task.exception():
            self.last_error = type(task.exception()).__name__
            log.error("Worker %s background loop failed: %s", self.spec.name, self.last_error)
            self.fleet.failure = task.exception()
            self.fleet.stop.set()

    async def close(self):
        await cancel_tasks(self.tasks)
        await self.runtime.close()
        if self.client:
            await self.client.close()
        for runner in self.listeners:
            await runner.cleanup()
        self.listeners.clear()

    async def periodic(self, kind, interval):
        # Deterministic staggering avoids synchronizing all Worker heartbeat writes.
        await asyncio.sleep(interval * (self.spec.slot + 1) / len(self.fleet.workers))
        while True:
            if self.online:
                try:
                    path = "/worker-heartbeat" if kind == "heartbeat" else "/worker-status"
                    body = {} if kind == "heartbeat" else self.status_payload()
                    await self.client.request("POST", path, body=body)
                except (APIError, aiohttp.ClientError, asyncio.TimeoutError) as error:
                    self.report_errors += 1
                    self.last_error = (
                        str(error) if isinstance(error, APIError) else type(error).__name__
                    )
                    log.warning("%s %s: %s", self.spec.name, kind, self.last_error)
            await asyncio.sleep(interval)

    async def watch(self, resource="model-instances"):
        delay = 1
        attribute = "file_watch_connected" if resource == "model-files" else "watch_connected"

        def connected():
            setattr(self, attribute, True)
            self.watch_connections[resource] += 1
            self.snapshot_requested = True

        while True:
            if not self.online:
                await asyncio.sleep(0.5)
                continue
            try:
                async for event in self.client.watch(on_connect=connected, path="/" + resource):
                    setattr(self, attribute, self.online)
                    if not self.online:
                        break
                    delay = 1
                    kind = str(event.get("type", "")).upper()
                    data = event.get("data") or {}
                    if kind == "HEARTBEAT":
                        continue
                    self.event_counts[resource] += 1
                    if resource == "model-files":
                        self.model_files.event(kind, data)
                        continue
                    key = data.get("id") or event.get("id")
                    if key is None:
                        continue
                    if kind == "DELETED":
                        self.instances.pop(key, None)
                    elif local_member(data, self.worker_id) is not None:
                        self.instances[key] = data
                    else:
                        self.instances.pop(key, None)
            except (
                APIError,
                aiohttp.ClientError,
                ConnectionError,
                asyncio.TimeoutError,
                ValueError,
            ) as error:
                self.report_errors += 1
                self.last_error = (
                    str(error) if isinstance(error, APIError) else type(error).__name__
                )
                log.warning("%s watch reconnect: %s", self.spec.name, self.last_error)
            finally:
                setattr(self, attribute, False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)

    async def reconcile(self):
        next_refresh = 0
        while True:
            if self.online:
                now = time.monotonic()
                if now >= next_refresh or self.snapshot_requested:
                    self.snapshot_requested = False
                    try:
                        # Watch reconnects do not replay deletions. A complete paginated snapshot
                        # reaps disappeared assignments even if their deletion events were missed.
                        epoch = self.event_counts["model-instances"]
                        items = await self.client.list_all("/model-instances")
                        if epoch == self.event_counts["model-instances"]:
                            self.instances = {
                                i["id"]: i for i in items if local_member(i, self.worker_id)
                            }
                        await self.model_files.refresh()
                    except (APIError, aiohttp.ClientError, asyncio.TimeoutError) as error:
                        self.report_errors += 1
                        self.last_error = type(error).__name__
                    next_refresh = now + self.config.simulation.reconcile_interval
                try:
                    await self.model_files.tick()
                except (APIError, aiohttp.ClientError, asyncio.TimeoutError) as error:
                    self.report_errors += 1
                    self.last_error = type(error).__name__
                await self.runtime.tick()
                self.port_reservations.reap()
            await asyncio.sleep(0.2)

    def summary(self):
        return {
            "name": self.spec.name,
            "worker_id": self.worker_id,
            "online": self.online,
            "registered": self.client is not None,
            "watch_connected": self.watch_connected,
            "file_watch_connected": self.file_watch_connected,
            "watch_connections": self.watch_connections,
            "events_received": self.event_counts,
            "model_files": len(self.model_files.items),
            "model_files_ready": sum(
                i.get("state") == "ready" for i in self.model_files.items.values()
            ),
            "simulated_downloads_completed": self.model_files.completed,
            "instances": len(self.instances),
            "running": len(self.runtime.running_ids),
            "report_errors": self.report_errors,
            "stale_reports": self.stale_reports,
            "deleted_assignments": self.deleted_assignments,
            "last_error": self.last_error,
            "assignments": [
                {
                    "id": r.instance["id"],
                    "model_id": r.instance["model_id"],
                    "role": r.instance.get("role"),
                    "generation": r.generation,
                    "phase": r.phase,
                    "port": r.port,
                    "listening_ports": sorted(r.listeners),
                    "workloads": len(r.workloads),
                    "last_log": r.logs[-1] if r.logs else None,
                }
                for r in self.runtime.records.values()
            ],
        }
