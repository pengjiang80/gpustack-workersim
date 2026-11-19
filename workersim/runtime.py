"""Generation-fenced simulation of native roles, ranks and distributed members."""

import asyncio
import copy
import logging
import time
from dataclasses import dataclass, field

import aiohttp

from workersim.client import APIError
from workersim.hardware import local_member
from workersim.http_server import engine_app, listen
from workersim.layout import effective_model, members, observation, preparation, workload_plan
from workersim.model_metrics import SyntheticTraffic

log = logging.getLogger(__name__)


def managed(instance):
    servers = instance.get("distributed_servers") or {}
    return servers.get("orchestration_mode") == "managed" or servers.get("workloads") is not None


@dataclass
class Record:
    instance: dict
    generation: int
    phase: str = "new"
    port: int | None = None
    runner: object = None
    workloads: list[dict] = field(default_factory=list)
    due: float = 0
    logs: list[str] = field(default_factory=list)
    traffic: SyntheticTraffic | None = None
    listeners: dict = field(default_factory=dict)
    reserved: set = field(default_factory=set)
    model: dict = field(default_factory=dict)
    component_stopped: set = field(default_factory=set)
    online: object = None
    injected_failure: str | None = None
    setup_failures: int = 0
    worker: object = None
    router_registry: dict = field(default_factory=dict)

    def note(self, text):
        self.logs.append(f"[WorkerSim] {text}")
        self.logs[:] = self.logs[-100:]


class Runtime:
    def __init__(self, worker):
        self.worker = worker
        self.records: dict[int, Record] = {}

    @property
    def engine_host(self):
        network = self.worker.config.network
        return self.worker.ip if network.worker_ip_cidr else network.bind_host

    def port_key(self, port):
        return self.engine_host, port

    @property
    def running_ids(self):
        return {key for key, record in self.records.items() if record.phase == "running"}

    async def close(self):
        for key in list(self.records):
            await self.forget(key)

    async def release(self, record):
        for runner in record.listeners.values():
            await runner.cleanup()
        record.listeners.clear()
        record.runner = None
        for key in record.reserved:
            self.worker.fleet.ports.discard(key)
        record.reserved.clear()
        record.port = None
        record.traffic = None
        record.router_registry.clear()
        identity = (record.instance["id"], record.generation)
        for key, owner in list(self.worker.fleet.logical_ports.items()):
            if key[0] == self.worker.worker_id and owner == identity:
                self.worker.fleet.logical_ports.pop(key, None)

    async def forget(self, key):
        record = self.records.get(key)
        if record:
            await self.release(record)
            self.records.pop(key, None)

    async def tick(self):
        for key in set(self.records) - set(self.worker.instances):
            await self.forget(key)
        for instance in list(self.worker.instances.values()):
            key = instance["id"]
            generation = instance.get("restart_count") or 0
            record = self.records.get(key)
            if record and record.generation != generation:
                await self.forget(key)
                record = None
            if record is None:
                if instance.get("state") not in (
                    "scheduled",
                    "initializing",
                    "downloading",
                    "starting",
                    "running",
                ) and not (instance.get("distributed_servers") or {}).get("legacy_stop"):
                    continue
                record = Record(copy.deepcopy(instance), generation)
                record.worker = self.worker
                record.online = lambda: self.worker.online
                self.records[key] = record
            record.instance = instance
            if record.due > time.monotonic():
                continue
            try:
                await self.advance(record)
            except APIError as error:
                if error.status == 404:
                    self.worker.deleted_assignments += 1
                    self.worker.instances.pop(key, None)
                    await self.forget(key)
                elif error.status == 409:
                    self.worker.stale_reports += 1
                    # The Server fences stale generations; refresh without adopting success.
                    record.due = time.monotonic() + 2
                else:
                    self.worker.report_errors += 1
                    self.worker.last_error = str(error)
                    log.warning("%s instance %s: %s", self.worker.spec.name, key, error)
                    record.due = time.monotonic() + 5
            except ValueError as error:
                record.note(str(error))
                await self.fail(record, str(error))
            except (OSError, ConnectionError, asyncio.TimeoutError, aiohttp.ClientError) as error:
                self.worker.report_errors += 1
                detail = str(error) if isinstance(error, OSError) else type(error).__name__
                self.worker.last_error = detail
                record.note(detail)
                log.warning("%s instance %s: %s", self.worker.spec.name, key, detail)
                record.setup_failures += 1
                if (
                    isinstance(error, OSError)
                    and not isinstance(error, (aiohttp.ClientError, TimeoutError, ConnectionError))
                    and record.setup_failures >= 3
                    and record.phase
                    in (
                        "new",
                        "initializing",
                        "starting",
                    )
                ):
                    await self.fail(record, detail)
                record.due = time.monotonic() + 5

    async def fail(self, record, detail):
        try:
            accepted = await self.report(
                record, {"state": "error", "state_message": "WorkerSim: " + detail}
            )
        except (APIError, aiohttp.ClientError, OSError, asyncio.TimeoutError):
            record.due = time.monotonic() + 5
            return
        if accepted:
            await self.release(record)
            record.phase = "error"

    async def current(self, record):
        latest = await self.worker.client.request(
            "GET", f"/model-instances/{record.instance['id']}"
        )
        member = local_member(latest, self.worker.worker_id)
        if member is None or (latest.get("restart_count") or 0) != record.generation:
            self.worker.instances.pop(record.instance["id"], None)
            return None
        self.worker.instances[latest["id"]] = latest
        record.instance = latest
        return latest

    async def report(self, record, fields, *, fenced=False):
        instance = await self.current(record)
        if instance is None:
            return False
        servers = instance.get("distributed_servers") or {}
        if servers.get("legacy_stop") and not fenced:
            return False
        key = instance["id"]
        if fenced or managed(instance) or servers.get("group_recovery"):
            body = {"restart_count": record.generation, **fields}
            result = await self.worker.client.request(
                "PUT", f"/model-instances/{key}/workload-report", body=body
            )
        else:
            # The native legacy CRUD client sends a fresh complete instance snapshot.
            if (
                instance.get("worker_id") == self.worker.worker_id
                and fields.get("state") in ("starting", "running")
                and not self.worker.model_files.pending_for(instance)
            ):
                fields = fields | {"download_progress": 100}
            if instance.get("worker_id") != self.worker.worker_id:
                # Fresh snapshot merge for older, non-fenced legacy contracts.
                instance = copy.deepcopy(instance)
                member = local_member(instance, self.worker.worker_id)
                member.update(fields.pop("preparation", {}) | fields)
                fields = {}
            result = await self.worker.client.request(
                "PUT", f"/model-instances/{key}", body=instance | fields
            )
        if result:
            self.worker.instances[key] = result
            record.instance = result
        return True

    async def load_model(self, record):
        # The snapshot is immutable for a generation; fetch the role definitions once.
        try:
            model = self.worker.watch_caches["models"].get(record.instance["model_id"])
            if model is None:
                model = await self.worker.client.request(
                    "GET", f"/models/{record.instance['model_id']}"
                )
        except APIError as error:
            if error.status != 404:
                raise
            model = record.instance.get("model_config_snapshot") or {}
        record.model = effective_model(model, record.instance.get("role"))

    async def allocate_engine(self, record):
        instance = record.instance
        primary = instance["worker_id"] == self.worker.worker_id
        n = self.worker.config.network
        low, high = n.instance_ports_for_slot(self.worker.spec.slot)
        if not primary and not instance.get("ports"):
            return False
        existing = instance.get("port")
        plan = {k: instance.get(k) for k in ("port", "ports", "named_ports")}
        identity = (instance["id"], record.generation)

        def conflicts(candidate):
            for member in members(instance):
                for port in candidate["ports"]:
                    owner = self.worker.fleet.logical_ports.get((member["worker_id"], port))
                    if owner is not None and owner != identity:
                        return True
            return False

        restore = existing and (managed(instance) or not primary or low <= existing <= high)
        occupied = any(
            self.port_key(p) in self.worker.fleet.ports - record.reserved
            for p in plan.get("ports") or []
        )
        if primary and not managed(instance) and (occupied or conflicts(plan)):
            restore = False
        if not restore:
            for base in range(low, high + 1):
                candidate = preparation(instance, record.model, base, self.worker.fleet.pd_catalog)
                if max(candidate["ports"]) > high:
                    break
                if not conflicts(candidate) and not any(
                    self.port_key(p) in self.worker.fleet.ports for p in candidate["ports"]
                ):
                    plan = candidate
                    break
            else:
                raise OSError("No simulation instance port band available")
            if not plan.get("port") or max(plan["ports"]) > high:
                raise OSError("No complete simulation instance port band available")
        if conflicts(plan):
            raise OSError("A distributed member port band is reserved by another task")
        for member in members(instance):
            for port in plan["ports"]:
                self.worker.fleet.logical_ports[(member["worker_id"], port)] = identity
        # Shared-IP followers use the Worker's authenticated proxy, so common logical
        # rank ports need no second OS bind. Separate-IP followers expose their own APIs.
        physical = primary or bool(n.worker_ip_cidr)
        if physical:
            available = {self.port_key(p) for p in plan["ports"]} - record.reserved
            if available & self.worker.fleet.ports:
                raise OSError("Instance port band is reserved by another local task")
            record.reserved.update(available)
            self.worker.fleet.ports.update(available)
        record.port = plan["port"]
        pid = 10000 + instance["id"] * 128 + self.worker.spec.slot
        local = [
            w
            for w in (instance.get("distributed_servers") or {}).get("workloads") or []
            if w["worker_id"] == self.worker.worker_id
        ]
        if primary:
            if managed(instance) or (instance.get("distributed_servers") or {}).get(
                "group_recovery"
            ):
                if not instance.get("ports") or not restore:
                    fields = {"preparation": plan | {"pid": pid}}
                    if not managed(instance) and instance.get("state") == "scheduled":
                        fields["state"] = "initializing"
                    if not await self.report(record, fields):
                        return False
            else:
                if not await self.report(
                    record,
                    plan
                    | {
                        "pid": pid,
                        "worker_ip": self.worker.ip,
                        "worker_advertise_address": self.worker.advertise_address,
                        **(
                            {"state": "initializing"}
                            if instance.get("state") == "scheduled"
                            else {}
                        ),
                    },
                ):
                    return False
        elif not local:
            fields = {"preparation": {"pid": pid}}
            if not managed(instance):
                fields["preparation"]["ports"] = plan["ports"]
            if not await self.report(record, fields):
                return False
        if managed(record.instance):
            local = [
                w
                for w in (record.instance.get("distributed_servers") or {}).get("workloads", [])
                if w["worker_id"] == self.worker.worker_id
            ]
            record.workloads = local or workload_plan(
                record.instance, record.model, self.worker.worker_id
            )
            if not local:
                if not await self.report(record, {"workloads": record.workloads}, fenced=True):
                    return False
        api_ports = set()
        if managed(record.instance):
            for workload in record.workloads:
                component = workload.get("component") or {}
                api_ports.update(
                    p for p in (component.get("api_port"), component.get("metrics_port")) if p
                )
                api_ports.update(
                    p["api_port"]
                    for p in workload.get("dp_rank_placements", [])
                    if p.get("api_port")
                )
        elif primary:
            api_ports.add(plan["port"])
            prometheus = (plan.get("named_ports") or {}).get("prometheus")
            if prometheus:
                api_ports.add(prometheus["base"])
        if physical:
            try:
                for port in api_ports:
                    if port not in record.listeners:
                        record.listeners[port] = await listen(
                            engine_app(record), self.engine_host, port
                        )
                record.runner = next(iter(record.listeners.values()), None)
            except BaseException:
                await self.release(record)
                raise
        return True

    async def advance(self, record):
        instance = record.instance
        servers = instance.get("distributed_servers") or {}
        stop = servers.get("legacy_stop")
        if stop or (managed(instance) and servers.get("stop_requested")):
            await self.release(record)
            if stop:
                await self.worker.client.request(
                    "POST",
                    f"/model-instances/{instance['id']}/stop-confirmation",
                    body={"token": stop["token"], "restart_count": record.generation},
                )
            else:
                local = [
                    w
                    for w in servers.get("workloads", [])
                    if w["worker_id"] == self.worker.worker_id
                ]
                await self.report(
                    record,
                    {"observations": [observation(w, "stopped") for w in local]},
                    fenced=True,
                )
            record.phase = "stopped"
            record.due = time.monotonic() + self.worker.config.simulation.model_state_interval
            return
        if instance.get("state") == "error":
            await self.release(record)
            record.phase = "error"
            return
        if record.injected_failure:
            if await self.report(
                record, {"state": "error", "state_message": record.injected_failure}
            ):
                await self.release(record)
                record.phase = "error"
            return
        if record.phase == "running":
            if managed(instance):
                await self.reconcile_components(record)
            elif (
                (local_member(instance, self.worker.worker_id) or {}).get("state")
                in ("scheduled", "initializing", "downloading", "starting")
                or (
                    instance.get("worker_id") == self.worker.worker_id
                    and instance.get("download_progress") not in (None, 100)
                )
            ) and instance.get("port") == record.port:
                # Like the native Worker's health monitor, reconcile the actual
                # live runtime after a missed/reverted control-plane update.
                # current()/report() still fence owner, generation and stop barriers.
                if not self.worker.model_files.pending_for(instance):
                    await self.report(record, {"state": "running", "state_message": None})
                record.due = time.monotonic() + self.worker.config.simulation.model_state_interval
            record.due = time.monotonic() + self.worker.config.simulation.model_state_interval
            return
        if record.phase in ("stopped", "error"):
            return
        primary = instance.get("worker_id") == self.worker.worker_id
        mode = servers.get("mode", "delegated")
        if record.phase == "new":
            if not primary and not instance.get("ports"):
                return
            if not primary and mode == "initialize_later" and instance.get("state") == "scheduled":
                return
            if await self.current(record) is None:
                return
            if not record.model:
                await self.load_model(record)
            if not await self.allocate_engine(record):
                return
            member = local_member(record.instance, self.worker.worker_id)
            restored = (
                all(w.get("state") == "running" for w in record.workloads)
                if managed(record.instance) and record.workloads
                else member.get("state") == "running"
            )
            if restored:
                self.ready(record)
                record.note("Restored this generation's local simulated runtime")
                return
            if not managed(record.instance) and record.instance.get("state") == "scheduled":
                if not await self.report(record, {"state": "initializing"}):
                    return
            record.phase = "initializing"
            record.note("Accepted native role/member allocation; preparing virtual model files")
            record.due = time.monotonic() + self.worker.config.simulation.download_seconds
            return
        if record.phase == "initializing":
            if not managed(instance) and instance.get("state") == "scheduled":
                # Reconcile an unacknowledged/reverted transition using a fresh,
                # generation-checked report. Never bypass ModelFile readiness.
                if not await self.report(record, {"state": "initializing"}):
                    return
                record.due = time.monotonic() + self.worker.config.simulation.model_state_interval
                return
            if self.worker.model_files.pending_for(instance):
                return
            if not managed(instance):
                if not await self.report(record, {"state": "starting"}):
                    return
            record.phase = "starting"
            record.note("Virtual model files ready; simulated runtime starting")
            record.due = time.monotonic() + self.worker.config.simulation.startup_seconds
            return
        if record.phase == "starting":
            if (
                primary
                and mode == "run_first"
                and any(
                    m.get("state") != "running" for m in servers.get("subordinate_workers") or []
                )
                and not managed(instance)
            ):
                return
            if managed(instance):
                local = [
                    w
                    for w in servers.get("workloads", [])
                    if w["worker_id"] == self.worker.worker_id
                ] or record.workloads
                if not await self.report(
                    record,
                    {"observations": [observation(w, "running") for w in local]},
                    fenced=True,
                ):
                    return
                record.workloads = local
            elif not await self.report(record, {"state": "running", "state_message": None}):
                return
            self.ready(record)
            record.note("Simulated runtime ready; role, generation and local member reported")

    def ready(self, record):
        record.phase = "running"
        record.traffic = SyntheticTraffic(
            record.instance["id"] + record.generation + self.worker.config.simulation.seed,
            self.worker.config.simulation.model_requests_per_second,
        )

    async def reconcile_components(self, record):
        local = [
            w
            for w in (record.instance.get("distributed_servers") or {}).get("workloads", [])
            if w["worker_id"] == self.worker.worker_id
        ]
        record.workloads = local
        for workload in local:
            component = workload.get("component") or {}
            if component.get("kind") != "sglang_smg":
                continue
            name = workload["name"]
            if component.get("stop_requested"):
                for port in (component.get("api_port"), component.get("metrics_port")):
                    runner = record.listeners.pop(port, None)
                    if runner:
                        await runner.cleanup()
                observed = [
                    observation(w, "stopped" if w["name"] == name else "running") for w in local
                ]
                if workload.get("state") != "stopped":
                    await self.report(record, {"observations": observed}, fenced=True)
                record.component_stopped.add(name)
            elif name in record.component_stopped:
                # The Server increments the component attempt after it observes STOPPED.
                # Recreate only that component and report using its new attempt fence.
                for port in (component.get("api_port"), component.get("metrics_port")):
                    if port and port not in record.listeners:
                        record.listeners[port] = await listen(
                            engine_app(record), self.engine_host, port
                        )
                if await self.report(
                    record,
                    {"observations": [observation(w, "running") for w in local]},
                    fenced=True,
                ):
                    record.component_stopped.discard(name)

    observation = staticmethod(observation)
