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
from workersim.native_contract import (
    CACHE_PROVIDER_INTERVAL,
    METRICS_CONFIG_TTL,
    REGISTER_ATTEMPTS,
    REGISTER_RETRY_SECONDS,
    RUNTIME_METRICS_INITIAL_DELAY,
    RUNTIME_METRICS_INTERVAL,
    STATE_SYNC_INTERVAL,
    WATCH_RECONNECT_SECONDS,
    WATCH_RESOURCES,
)
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
        self.watch_connections = dict.fromkeys(WATCH_RESOURCES, 0)
        self.event_counts = dict.fromkeys(WATCH_RESOURCES, 0)
        self.watch_states = dict.fromkeys(WATCH_RESOURCES, False)
        self.watch_caches = {resource: {} for resource in WATCH_RESOURCES}
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
        probe = await Client(self.config.server, "").open()
        try:
            version = await probe.request("GET", "/version", root=True, timeout=5)
        finally:
            await probe.close()
        actual = str(version.get("version", "")).removeprefix("v")
        if actual != TARGET_VERSION and not self.config.server.allow_version_mismatch:
            raise ValueError(f"Expected GPUStack {TARGET_VERSION}, received {actual!r}")
        n = self.config.network
        self.listeners.append(
            await listen(worker_app(self), n.bind_host, n.api_port_base + self.spec.slot)
        )
        self.listeners.append(
            await listen(metrics_app(self), n.bind_host, n.metrics_port_base + self.spec.slot)
        )
        for attempt in range(REGISTER_ATTEMPTS):
            try:
                await self.register(registration_token)
                break
            except (APIError, aiohttp.ClientError, asyncio.TimeoutError):
                if attempt == REGISTER_ATTEMPTS - 1:
                    raise
                await asyncio.sleep(REGISTER_RETRY_SECONDS)
        log.info("Registered %s, Worker %s", self.spec.name, self.worker_id)
        self.tasks = [
            asyncio.create_task(
                self.periodic("heartbeat", self.config.simulation.heartbeat_interval)
            ),
            asyncio.create_task(self.periodic("status", self.config.simulation.status_interval)),
            asyncio.create_task(self.auxiliary_reports(
                "runtime-metrics", RUNTIME_METRICS_INTERVAL, RUNTIME_METRICS_INITIAL_DELAY
            )),
        ]
        for task in self.tasks:
            task.add_done_callback(self.background_done)
        # Native heartbeat/status threads are running while the Backend manager initializes.
        rows = await self.client.request("GET", "/inference-backends/all")
        self.watch_caches["inference-backends"] = {row["id"]: row for row in rows}
        later_tasks = [
            asyncio.create_task(self.reconcile()),
            asyncio.create_task(self.auxiliary_reports("providers", CACHE_PROVIDER_INTERVAL, 0)),
            asyncio.create_task(self.auxiliary_reports("benchmarks", STATE_SYNC_INTERVAL, 15)),
            asyncio.create_task(self.auxiliary_reports("cache-services", STATE_SYNC_INTERVAL, 3)),
            *[asyncio.create_task(self.watch(resource)) for resource in WATCH_RESOURCES],
        ]
        self.tasks.extend(later_tasks)
        for task in later_tasks:
            task.add_done_callback(self.background_done)

    async def register(self, registration_token):
        # A bound Worker re-registers using its own credential, as required by ownership checks.
        register = await Client(
            self.config.server,
            self.identity.get("token", registration_token),
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
            # Native initial collection detects system info; GPU detection is in the first status.
            body["status"]["gpu_devices"] = None
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
        if self.client:
            await self.client.close()
        self.client = await Client(self.config.server, self.identity["token"]).open()
        me = await self.client.request("GET", "/workers", params={"me": "true"})
        if len(me.get("items", [])) != 1 or me["items"][0]["id"] != self.worker_id:
            raise RuntimeError("Native Worker self lookup did not return this identity")

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
        # Native run_periodically_in_thread starts immediately, then sleeps after each call.
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
        attribute = "file_watch_connected" if resource == "model-files" else "watch_connected"

        def connected():
            if resource in ("model-files", "model-instances"):
                setattr(self, attribute, True)
            self.watch_states[resource] = True
            self.watch_caches[resource].clear()
            if resource == "model-files":
                self.model_files.items.clear()
            self.watch_connections[resource] += 1

        while True:
            if not self.online:
                await asyncio.sleep(0.5)
                continue
            try:
                async for event in self.client.watch(on_connect=connected, path="/" + resource):
                    if resource in ("model-files", "model-instances"):
                        setattr(self, attribute, self.online)
                    if not self.online:
                        break
                    kind = str(event.get("type", "")).upper()
                    data = event.get("data") or {}
                    if kind == "HEARTBEAT":
                        continue
                    self.event_counts[resource] += 1
                    key = data.get("id") or event.get("id")
                    if key is None:
                        continue
                    cache = self.watch_caches[resource]
                    if kind == "DELETED":
                        cached = cache.pop(key, None)
                        if cached and set(data) <= {"id"}:
                            data = cached
                    else:
                        cache[key] = data
                    if resource == "model-files":
                        self.model_files.event(kind, data)
                        continue
                    if resource != "model-instances":
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
                self.last_error = str(error) or type(error).__name__
                log.warning("%s watch reconnect: %s", self.spec.name, self.last_error)
            finally:
                if resource in ("model-files", "model-instances"):
                    setattr(self, attribute, False)
                self.watch_states[resource] = False
                self.watch_caches[resource].clear()
            await asyncio.sleep(WATCH_RECONNECT_SECONDS)

    async def reconcile(self):
        interval = self.config.simulation.reconcile_interval
        next_refresh = time.monotonic() + interval if interval else float("inf")
        next_state_sync = 0
        while True:
            if self.online:
                now = time.monotonic()
                if now >= next_state_sync:
                    try:
                        cache = self.watch_caches["model-instances"]
                        missing = set(self.instances) - set(cache)
                        epoch = self.event_counts["model-instances"]
                        authoritative = missing or now >= next_refresh
                        if authoritative or not self.watch_connected:
                            params = {"page": -1} if authoritative else None
                            response = await self.client.request(
                                "GET", "/model-instances", params=params
                            )
                            items = response.get("items", [])
                        else:
                            items = list(cache.values())
                        if epoch == self.event_counts["model-instances"]:
                            self.instances = {
                                i["id"]: i for i in items if local_member(i, self.worker_id)
                            }
                        if authoritative:
                            next_refresh = now + interval if interval else float("inf")
                    except (APIError, aiohttp.ClientError, asyncio.TimeoutError) as error:
                        self.report_errors += 1
                        self.last_error = type(error).__name__
                    next_state_sync = time.monotonic() + self.config.simulation.model_state_interval
                try:
                    await self.model_files.tick()
                except (APIError, aiohttp.ClientError, asyncio.TimeoutError) as error:
                    self.report_errors += 1
                    self.last_error = type(error).__name__
                await self.runtime.tick()
                self.port_reservations.reap()
            await asyncio.sleep(0.2)

    async def auxiliary_reports(self, kind, interval, initial_delay):
        """Native idle-worker REST traffic; local engine metric collection stays synthetic."""
        await asyncio.sleep(initial_delay)
        config_due = 0
        while True:
            if self.online:
                try:
                    if kind == "providers":
                        await self.client.request("GET", "/cache-providers", params={"page": 0})
                    elif kind == "runtime-metrics":
                        if time.monotonic() >= config_due:
                            try:
                                await self.client.request("GET", "/metrics/config", timeout=2)
                            except (APIError, aiohttp.ClientError, asyncio.TimeoutError):
                                # Native aggregator also falls back to its built-in config.
                                pass
                            config_due = time.monotonic() + METRICS_CONFIG_TTL
                        await self.client.request("GET", f"/workers/{self.worker_id}")
                        if not self.watch_states["models"]:
                            await self.client.request("GET", "/models")
                        if not self.watch_connected:
                            await self.client.request(
                                "GET", "/model-instances",
                                params={"cluster_id": self.identity["cluster_id"]},
                            )
                    elif kind == "benchmarks" and not self.watch_states["benchmarks"]:
                        await self.client.request(
                            "GET", "/benchmarks",
                            params={"worker_id": self.worker_id, "state": "running"}
                        )
                    elif kind == "cache-services" and not self.watch_states[
                        "cache-service-instances"
                    ]:
                        await self.client.request(
                            "GET", "/cache-service-instances", params={"worker_id": self.worker_id}
                        )
                except (APIError, aiohttp.ClientError, asyncio.TimeoutError) as error:
                    self.report_errors += 1
                    self.last_error = str(error) or type(error).__name__
                await asyncio.sleep(0)  # let independent heartbeat/status tasks run
            await asyncio.sleep(interval)

    def summary(self):
        return {
            "name": self.spec.name,
            "worker_id": self.worker_id,
            "online": self.online,
            "registered": self.client is not None,
            "watch_connected": self.watch_connected,
            "file_watch_connected": self.file_watch_connected,
            "watch_connections": self.watch_connections,
            "watch_states": self.watch_states,
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
