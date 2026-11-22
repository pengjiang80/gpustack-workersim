"""Worker-side runtime metrics aggregation, following GPUStack 2.3.1's native aggregator.

Per pass (every 3 s after a 30 s delay) the native Worker:

1. reads the metrics config: ``GET /v2/metrics/config`` (2 s timeout, 300 s cache,
   built-in fallback);
2. lists its endpoints: ``GET /v2/workers/{id}`` uncached, plus cached instance and
   model lists;
3. scrapes each engine source and probes runtime versions, writing a newly detected
   ``api_detected_backend_version`` back to legacy instances (cached read + ``PUT``);
4. lists the endpoints again to drop sources replaced during the scrape;
5. normalizes raw families into unified ``gpustack:*`` families.

The scrape is in-process: each synthetic engine produces the same families its
``/metrics`` endpoint serves. Server-visible traffic and exported series match the
native Worker.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from importlib.resources import files

import aiohttp
import yaml

from workersim.client import APIError
from workersim.model_metrics import (
    DEFAULT_RUNTIME_VERSIONS,
    Family,
    engine_families,
    gateway_families,
    router_families,
)
from workersim.native_contract import METRICS_CONFIG_TTL

log = logging.getLogger(__name__)

METRICS_CONFIG_TIMEOUT = 2
SNAPSHOT_TTL = 30
VERSION_CACHE_TTL = 300
SCRAPE_STATUS = "gpustack:runtime_metrics_scrape_status"

_builtin = None


def builtin_metrics_config() -> dict:
    global _builtin
    if _builtin is None:
        text = files("workersim").joinpath("data/metrics_config.yaml").read_text(encoding="utf-8")
        _builtin = yaml.safe_load(text)
    return _builtin


# ---------------------------------------------------------------- native helpers


def _version_key(text):
    parts = []
    for piece in re.split(r"[.+-]", str(text).lstrip("vV")):
        match = re.match(r"\d+", piece)
        if not match:
            break
        parts.append(int(match.group()))
    if not parts:
        raise ValueError(text)
    return tuple(parts) + (0,) * (4 - len(parts))


def in_range(version, condition) -> bool:
    current = _version_key(version)
    for cond in (c.strip() for c in condition.split(",")):
        for op, check in (
            (">=", lambda a, b: a >= b), ("<=", lambda a, b: a <= b),
            (">", lambda a, b: a > b), ("<", lambda a, b: a < b),
        ):
            if cond.startswith(op):
                if not check(current, _version_key(cond[len(op):])):
                    return False
                break
        else:
            if current != _version_key(cond):
                return False
    return True


def _parse_entry(entry):
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry, 1.0
    if isinstance(entry, dict) and entry.get("name"):
        try:
            return entry["name"], float(entry.get("scale", 1.0))
        except (TypeError, ValueError):
            return entry["name"], 1.0
    return None


def unified_name(config, family, runtime, runtime_version):
    runtime_cfg = (config.get("runtime_mapping") or {}).get(runtime)
    if not runtime_cfg:
        return None
    entry = (runtime_cfg.get("*") or {}).get(family)
    if runtime_version:
        try:
            _version_key(runtime_version)
            valid = True
        except ValueError:
            valid = False
        for selector, mapping in runtime_cfg.items():
            if selector == "*":
                continue
            try:
                matched = in_range(runtime_version, selector) if valid else (
                    runtime_version == selector
                )
            except ValueError:
                matched = False
            if matched and (mapping or {}).get(family) is not None:
                return _parse_entry(mapping[family])
    return _parse_entry(entry)


def scale_sample(sample_name, labels, value, scale):
    if scale == 1.0 or sample_name.endswith(("_count", "_created")):
        return value, labels
    if sample_name.endswith("_sum"):
        return value * scale, labels
    if sample_name.endswith("_bucket"):
        upper = labels.get("le")
        if upper is None or upper in ("+Inf", "Inf"):
            return value, labels
        return value, labels | {"le": repr(float(upper) * scale)}
    return value * scale, labels


def runtime_metric_labels(native, platform):
    labels = dict(native)
    for key, value in platform.items():
        if key in native and native[key] != value:
            native_key = f"native_{key}"
            if native_key in native or native_key in platform:
                raise ValueError("Native metric label conflicts with platform identity")
            labels[native_key] = native[key]
        labels[key] = value
    return labels


def workloads_of(instance):
    return (instance.get("distributed_servers") or {}).get("workloads")


@dataclass(frozen=True)
class Source:
    instance_id: int
    model_id: int
    worker_id: int
    restart_count: int
    workload_name: str
    kind: str
    api_port: int
    metrics_port: int
    dp_rank: int | None = None
    attempt: int | None = None
    gpu_indexes: tuple = ()

    @property
    def name(self):
        if self.dp_rank is not None:
            return f"{self.workload_name}/main-{self.dp_rank}"
        return self.workload_name or self.kind


def metrics_sources(instance) -> list[Source]:
    """Native model_instance_metrics_sources: current scrape-ready main APIs only."""
    if instance.get("state") not in ("running", "starting", "error") or instance.get(
        "draining_since"
    ):
        return []
    servers = instance.get("distributed_servers") or {}
    if servers.get("legacy_stop"):
        return []
    workloads = workloads_of(instance)
    ports = instance.get("ports") or []
    common = {
        "instance_id": instance["id"],
        "model_id": instance.get("model_id"),
        "restart_count": instance.get("restart_count") or 0,
    }
    if workloads is None:
        if not instance.get("worker_id") or not ports or instance.get("state") != "running":
            return []
        band = (instance.get("named_ports") or {}).get("prometheus") or {}
        return [
            Source(
                **common, worker_id=instance["worker_id"], workload_name="", kind="legacy",
                api_port=instance.get("port") or ports[0],
                metrics_port=(band.get("base") if isinstance(band, dict) else None) or ports[0],
            )
        ]
    result = []
    for task in workloads:
        if task.get("state") != "running":
            continue
        component = task.get("component")
        fields = {"worker_id": task["worker_id"], "workload_name": task["name"]}
        if component is not None:
            if (
                component.get("api_port") is None
                or not component.get("ready")
                or component.get("stop_requested")
            ):
                continue
            result.append(
                Source(
                    **common, **fields, kind=component["kind"],
                    api_port=component["api_port"],
                    metrics_port=component.get("metrics_port") or component["api_port"],
                    attempt=component.get("attempt") if component["kind"] == "sglang_smg" else None,
                )
            )
            continue
        for placement in task.get("dp_rank_placements") or []:
            if placement.get("node_rank") == 0 and placement.get("api_port") and placement.get(
                "ready"
            ):
                result.append(
                    Source(
                        **common, **fields, kind="vllm_engine",
                        api_port=placement["api_port"], metrics_port=placement["api_port"],
                        dp_rank=placement["dp_rank"],
                        gpu_indexes=tuple(sorted(placement.get("gpu_indexes") or [])),
                    )
                )
    return result


def should_skip(model, config) -> bool:
    if not model:
        return True
    categories = model.get("categories") or []
    backend = model.get("backend")
    if "image" in categories or backend == "VoxBox" or (
        "speech_to_text" in categories or "text_to_speech" in categories
    ):
        return True
    if not backend or not (config.get("runtime_mapping") or {}).get(backend):
        return True
    if backend == "vLLM" and any(
        str(p).split("=", 1)[0].lstrip("-") == "disable-log-stats"
        for p in model.get("backend_parameters") or []
    ):
        return True
    return bool((model.get("env") or {}).get("GPUSTACK_DISABLE_METRICS"))


class Snapshot(dict):
    """A published batch that stops being readable after the native 30 s TTL."""

    def __init__(self, families=None, collected_at=0.0):
        super().__init__(families or {})
        self.expires = collected_at + SNAPSHOT_TTL

    def fresh(self):
        return list(self.values()) if time.monotonic() < self.expires else []


class RuntimeMetricsAggregator:
    def __init__(self, worker):
        self.worker = worker
        self.config = None
        self.config_expires = 0.0
        self.versions: dict = {}
        self.unified = Snapshot()
        self.raw = Snapshot()
        self.worker_row: dict = {}

    # -- Server calls --------------------------------------------------------

    async def metrics_config(self):
        if self.config is not None and time.monotonic() < self.config_expires:
            return self.config
        online = None
        try:
            data = await self.worker.client.request(
                "GET", "/metrics/config", timeout=METRICS_CONFIG_TIMEOUT
            )
            online = data if isinstance(data, dict) and data else None
        except (APIError, aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            online = None
        self.config = online or builtin_metrics_config()
        self.config_expires = time.monotonic() + METRICS_CONFIG_TTL
        return self.config

    async def list_worker_models(self):
        worker = self.worker
        row = await worker.client.request("GET", f"/workers/{worker.worker_id}")
        if row.get("id") != worker.worker_id or row.get("cluster_id") is None or row.get(
            "me"
        ) is not True:
            raise ValueError("Runtime metrics require the current cluster-bound Worker")
        self.worker_row = row
        if worker.watch_connected:
            instances = list(worker.watch_caches["model-instances"].values())
        else:
            instances = (
                await worker.client.request(
                    "GET", "/model-instances", params={"cluster_id": row["cluster_id"]}
                )
            ).get("items", [])
        if worker.watch_states["models"]:
            models = list(worker.watch_caches["models"].values())
        else:
            models = (await worker.client.request("GET", "/models")).get("items", [])
        return row, instances, models

    async def active_endpoints(self, config):
        row, instances, models = await self.list_worker_models()
        by_id = {m.get("id"): m for m in models}
        endpoints, conflicting = {}, set()
        for instance in instances:
            model = by_id.get(instance.get("model_id"))
            if instance.get("cluster_id", row["cluster_id"]) != row["cluster_id"] or should_skip(
                model, config
            ):
                continue
            for source in metrics_sources(instance):
                if source.worker_id != self.worker.worker_id:
                    continue
                endpoint = f"{row.get('ip') or self.worker.ip}:{source.metrics_port}"
                if endpoint in conflicting:
                    continue
                previous = endpoints.get(endpoint)
                if previous is not None and previous[1] != source:
                    conflicting.add(endpoint)
                    endpoints.pop(endpoint)
                    continue
                endpoints[endpoint] = (instance, source, model)
        return endpoints

    async def update_detected_version(self, instance, version):
        """Native fenced write: re-read the instance, then PUT the full snapshot."""
        try:
            current = await self.worker.client.request(
                "GET", f"/model-instances/{instance['id']}"
            )
            if workloads_of(current) is not None or metrics_sources(current) != metrics_sources(
                instance
            ):
                return
            await self.worker.client.request(
                "PUT", f"/model-instances/{instance['id']}",
                body=current | {"api_detected_backend_version": version},
            )
        except (APIError, aiohttp.ClientError, asyncio.TimeoutError) as error:
            log.debug("Version write for instance %s failed: %s", instance["id"], error)

    # -- Engine scrape (in-process) -------------------------------------------

    def scrape(self, instance, source, model):
        """Families the source's synthetic /metrics serves; None models a refused scrape."""
        record = self.worker.runtime.records.get(instance["id"])
        if (
            not self.worker.online
            or record is None
            or record.phase != "running"
            or record.traffic is None
            or record.generation != source.restart_count
        ):
            return None
        return source_families(record, source, model)

    def runtime_versions(self, endpoints, scraped):
        versions, writes = {}, []
        now = time.monotonic()
        for endpoint, (instance, source, model) in endpoints.items():
            key = instance["id"]
            if source.kind == "sglang_smg" or key in versions or not scraped.get(endpoint):
                continue
            if instance.get("api_detected_backend_version") is not None:
                versions[key] = instance["api_detected_backend_version"]
                continue
            if instance.get("role") == "router":
                versions[key] = instance.get("backend_version")
                continue
            cache_key = (key, source, self.worker.worker_id, instance.get("backend"))
            cached = self.versions.get(cache_key)
            if cached and cached[1] > now:
                versions[key] = cached[0]
                continue
            detected = engine_version(instance, model)
            versions[key] = detected or instance.get("backend_version")
            if detected:
                self.versions[cache_key] = (detected, now + VERSION_CACHE_TTL)
                if workloads_of(instance) is None:
                    writes.append((instance, detected))
        for cache_key in [k for k, v in self.versions.items() if v[1] <= now]:
            self.versions.pop(cache_key, None)
        return versions, writes

    # -- Aggregation ------------------------------------------------------------

    async def aggregate(self):
        if not self.worker.worker_id:
            return
        config = await self.metrics_config()
        endpoints = await self.active_endpoints(config)
        if not endpoints:
            self.unified, self.raw = Snapshot(), Snapshot()
            return
        scraped = {
            endpoint: self.scrape(*context) for endpoint, context in endpoints.items()
        }
        versions, writes = self.runtime_versions(endpoints, scraped)
        for instance, version in writes:
            await self.update_detected_version(instance, version)
        current = await self.active_endpoints(config)
        unified, raw = {}, {}
        for endpoint, (instance, source, model) in endpoints.items():
            now = current.get(endpoint)
            if now is None or now[1] != source:
                continue
            instance = now[0]
            runtime = model.get("backend")
            labels = self.base_labels(instance, model, runtime, source)
            metrics = scraped.get(endpoint)
            status = "error" if metrics is None else "success" if metrics else "empty"
            if SCRAPE_STATUS not in raw:
                raw[SCRAPE_STATUS] = Family(
                    SCRAPE_STATUS, "gauge", "Runtime source scrape outcome"
                )
                unified[SCRAPE_STATUS] = raw[SCRAPE_STATUS]
            raw[SCRAPE_STATUS].samples.append((SCRAPE_STATUS, labels | {"status": status}, 1))
            for family in metrics or []:
                try:
                    self.process_family(
                        family, labels, runtime, versions.get(instance["id"]), config,
                        unified, raw,
                    )
                except ValueError as error:
                    log.debug("Skipping metric family %s: %s", family.name, error)
        collected = time.monotonic()
        self.unified = Snapshot(unified, collected)
        self.raw = Snapshot(raw, collected)

    def base_labels(self, instance, model, runtime, source):
        labels = {
            "worker_id": str(source.worker_id),
            "worker_name": self.worker_row.get("name") or self.worker.spec.name,
            "model_id": str(model.get("id", "")) if model else "",
            "model_name": model.get("name", "") if model else "",
            "model_instance_id": str(instance["id"]),
            "model_instance_name": instance.get("name", ""),
            "runtime": runtime,
            "role": instance.get("role") or "",
            "restart_count": str(source.restart_count),
            "source_kind": source.kind,
            "source_name": source.name,
            "workload_name": source.workload_name,
        }
        if source.dp_rank is not None:
            labels["endpoint_dp_rank"] = str(source.dp_rank)
            labels["gpu_indexes"] = ",".join(str(i) for i in source.gpu_indexes)
        if source.attempt is not None:
            labels["component_attempt"] = str(source.attempt)
        return labels

    @staticmethod
    def sample_scope(sample_labels, base, runtime):
        labels = dict(base)
        kind = labels.get("source_kind")
        if kind == "sglang_smg":
            labels["exposition_scope"] = "router"
            return labels, True
        if kind == "sglang_engine":
            labels["exposition_scope"] = "shared_engine"
            return labels, True
        if kind != "vllm_engine":
            return labels, True
        engine = sample_labels.get("engine")
        known = runtime == "vLLM" and isinstance(engine, str) and engine.isdecimal()
        if known and int(engine) != int(labels["endpoint_dp_rank"]):
            return None, False
        labels["exposition_scope"] = "engine" if known else "unknown"
        if not known:
            labels.pop("endpoint_dp_rank", None)
            labels.pop("gpu_indexes", None)
        return labels, known

    def process_family(self, family, base, runtime, version, config, unified, raw):
        domain = "SGLangSMG" if base.get("source_kind") == "sglang_smg" else runtime
        name, scale = unified_name(config, family.name, domain, version) or (None, 1.0)
        cfg = (config.get("gpustack_metrics") or {}).get(name) or {} if name else {}
        keeps = family.type in ("histogram", "summary", "info")
        values = {family.name, family.name + "_total"}
        for sample_name, sample_labels, value in family.samples:
            if not keeps and sample_name not in values:
                continue
            platform, can_unify = self.sample_scope(sample_labels, base, runtime)
            if platform is None:
                continue
            labels = runtime_metric_labels(sample_labels, platform)
            if family.name not in raw:
                raw[family.name] = Family(family.name, family.type, family.documentation)
            raw_name = family.name + "_total" if family.type == "counter" else sample_name
            raw[family.name].samples.append((raw_name, labels, value))
            if not (can_unify and name and cfg):
                continue
            if name not in unified:
                unified[name] = Family(
                    name, str(cfg.get("type", "untyped")).lower(), cfg.get("description", "")
                )
            target = unified[name]
            scaled, scaled_labels = scale_sample(sample_name, labels, value, scale)
            if keeps:
                sample = raw_name.replace(family.name, target.name, 1)
            else:
                sample = target.name + ("_total" if target.type == "counter" else "")
            target.samples.append((sample, scaled_labels, scaled))


def engine_version(instance, model):
    """What the synthetic engine's version API reports."""
    runtime = (model or {}).get("backend") or instance.get("backend")
    return (
        instance.get("backend_version")
        or (model or {}).get("backend_version")
        or DEFAULT_RUNTIME_VERSIONS.get(runtime)
    )


def source_families(record, source, model=None, *, now=None):
    """Native exposition for one engine/router/gateway source of a running record."""
    instance = record.instance
    runtime = (model or record.model or {}).get("backend") or instance.get("backend") or "vLLM"
    served = instance.get("model_name") or (model or {}).get("name") or ""
    if source.kind == "sglang_smg":
        return gateway_families(record.traffic, {}, peers(record), now=now)
    if instance.get("role") == "router":
        return router_families(record.traffic, runtime, {}, peers(record), now=now)
    labels = {"model_name": served}
    if runtime == "vLLM":
        labels["engine"] = str(source.dp_rank or 0)
    return engine_families(record.traffic, runtime, labels, role=instance.get("role"), now=now)


def peers(record):
    if record.instance.get("role") != "router" or record.worker is None:
        return []
    from workersim.router import registered_peers

    return [
        {"worker_type": row["worker_type"], "worker": url.removeprefix("http://")}
        for url, row in registered_peers(record).items()
    ]


def engine_source(record, port):
    """The metrics source served on a local listener port, as the scrape would reach it."""
    worker_id = record.worker.worker_id if record.worker is not None else None
    instance = record.instance | {"state": "running"} if workloads_of(record.instance) is None \
        else record.instance
    candidates = [
        s for s in metrics_sources(instance) if worker_id is None or s.worker_id == worker_id
    ]
    if workloads_of(record.instance) is not None:
        # Local managed workloads may not be marked running/ready on the Server yet.
        candidates = candidates or [
            s for s in _local_inventory(record) if s.worker_id == worker_id
        ]
    for source in candidates:
        if port in (source.metrics_port, source.api_port):
            return source
    return candidates[0] if candidates else None


def _local_inventory(record):
    instance = dict(record.instance)
    servers = dict(instance.get("distributed_servers") or {})
    servers["workloads"] = [
        w | {"state": "running"}
        | ({"component": (w.get("component") or {}) | {"ready": True}} if w.get("component")
           else {"dp_rank_placements": [p | {"ready": True}
                                        for p in w.get("dp_rank_placements") or []]})
        for w in record.workloads
    ]
    instance["distributed_servers"] = servers
    instance["state"] = "running"
    return metrics_sources(instance)
