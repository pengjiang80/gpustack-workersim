"""Explicit, validated fleet configuration. Credentials are resolved only at run time."""

from dataclasses import dataclass, field, fields
from ipaddress import IPv4Network, ip_network
from pathlib import Path
from urllib.parse import urlparse

import yaml

GIB = 1024**3
MAX_WORKERS = 1000
PROFILES = {
    "ascend-910b4-64gb": {
        "name": "Ascend 910B4",
        "vendor": "ascend",
        "type": "cann",
        "memory_gib": 64,
        "arch_family": "Ascend910B4",
        # Synthetic envelope, not a claim about physical board or core specifications.
        "power": 300,
        "core_count": 0,
        "driver_version": "24.1.0",
        "runtime_version": "8.0",
    },
    "b300-288gb": {
        "name": "NVIDIA B300",
        "vendor": "NVIDIA",
        "type": "cuda",
        "memory_gib": 288,
        "compute_capability": "10.3",
        "arch_family": "Blackwell",
        # Synthetic power envelope; zero means no physical core inventory is asserted.
        "power": 1100,
        "core_count": 0,
        "driver_version": "580.95.05",
        "runtime_version": "13.0",
    },
    "h100-80gb": {
        "name": "NVIDIA H100 80GB HBM3",
        "vendor": "NVIDIA",
        "type": "cuda",
        "memory_gib": 80,
        "compute_capability": "9.0",
        "arch_family": "Hopper",
        "power": 700,
        "core_count": 16896,
        "driver_version": "550.127.05",
        "runtime_version": "12.4",
    },
    "a800-80gb": {
        "name": "NVIDIA A800-SXM4-80GB",
        "vendor": "NVIDIA",
        "type": "cuda",
        "memory_gib": 80,
        "compute_capability": "8.0",
        "arch_family": "Ampere",
        "power": 400,
        "core_count": 6912,
        "driver_version": "550.127.05",
        "runtime_version": "12.4",
    },
    "rtx4090-24gb": {
        "name": "NVIDIA GeForce RTX 4090",
        "vendor": "NVIDIA",
        "type": "cuda",
        "memory_gib": 24,
        "compute_capability": "8.9",
        "arch_family": "Ada",
        "power": 450,
        "core_count": 16384,
        "driver_version": "550.127.05",
        "runtime_version": "12.4",
    },
}


@dataclass
class ServerConfig:
    url: str = "http://127.0.0.1:80"
    registration_token_env: str = "GPUSTACK_REGISTRATION_TOKEN"
    api_key_env: str = "GPUSTACK_API_KEY"
    cluster_id: int | None = None
    verify_ssl: bool = True
    request_timeout: float = 20
    allow_version_mismatch: bool = False


@dataclass
class NetworkConfig:
    bind_host: str = "0.0.0.0"
    advertise_address: str = "127.0.0.1"
    worker_ip: str = "127.0.0.1"
    worker_ip_cidr: str | None = None
    api_port_base: int = 20100
    metrics_port_base: int = 21100
    instance_port_start: int = 40000
    instance_port_end: int = 40019
    instance_port_stride: int | None = None
    control_port: int = 20099

    def ip_for_slot(self, slot: int) -> str:
        if self.worker_ip_cidr is None:
            return self.worker_ip
        return str(ip_network(self.worker_ip_cidr).network_address + slot + 1)

    def instance_ports_for_slot(self, slot: int) -> tuple[int, int]:
        stride = self.instance_port_stride
        if stride is None:
            stride = self.instance_port_end - self.instance_port_start + 1
        offset = 0 if self.worker_ip_cidr else slot * stride
        return self.instance_port_start + offset, self.instance_port_end + offset


@dataclass
class SimulationConfig:
    namespace: str = "workersim"
    data_dir: str = ".workersim"
    seed: int = 42
    heartbeat_interval: float = 30
    status_interval: float = 30
    reconcile_interval: float = 10
    registration_rate: float = 5
    download_seconds: float = 1
    startup_seconds: float = 2
    model_requests_per_second: float = 2


@dataclass
class WorkerGroup:
    name_prefix: str = "sim-h100"
    count: int = 2
    gpu_profile: str = "h100-80gb"
    gpu_count: int = 8
    cpu_cores: int = 128
    memory_gib: int = 1024
    disk_gib: int = 4096
    reserved_ram_gib: int = 2
    reserved_vram_gib: int = 1
    labels: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    slot: int
    group: WorkerGroup


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    worker_groups: list[WorkerGroup] = field(default_factory=lambda: [WorkerGroup()])

    def workers(self) -> list[WorkerSpec]:
        result = []
        for group in self.worker_groups:
            for number in range(1, group.count + 1):
                result.append(WorkerSpec(f"{group.name_prefix}-{number:03d}", len(result), group))
        return result

    def validate(self) -> None:
        import math
        import re

        url = urlparse(self.server.url)
        if url.scheme not in ("http", "https") or not url.hostname or url.query or url.fragment:
            raise ValueError("server.url must be an HTTP(S) server root URL")
        if url.username or url.password or url.path.rstrip("/").endswith("/v2"):
            raise ValueError("Use a server root URL without credentials or the /v2 suffix")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", self.simulation.namespace):
            raise ValueError("simulation.namespace must contain only letters, digits, _ or -")
        for name in (
            "heartbeat_interval",
            "status_interval",
            "reconcile_interval",
            "registration_rate",
        ):
            if getattr(self.simulation, name) <= 0:
                raise ValueError(f"simulation.{name} must be positive")
        if min(self.simulation.download_seconds, self.simulation.startup_seconds) < 0:
            raise ValueError("Simulation delays cannot be negative")
        rate = self.simulation.model_requests_per_second
        if type(rate) not in (int, float) or not math.isfinite(rate) or rate < 0:
            raise ValueError("simulation.model_requests_per_second must be finite and nonnegative")
        if self.server.request_timeout <= 0:
            raise ValueError("server.request_timeout must be positive")
        for name in ("advertise_address", "worker_ip"):
            value = getattr(self.network, name)
            if not value or any(c in value for c in "/: \t\n") or value == "0.0.0.0":
                raise ValueError(f"network.{name} must be a reachable IPv4 address or hostname")
        for group in self.worker_groups:
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", group.name_prefix):
                raise ValueError("Worker name prefixes must contain only letters, digits, _ or -")
            if group.gpu_profile not in PROFILES:
                raise ValueError(f"Unknown GPU profile: {group.gpu_profile}")
            integer_fields = (
                "count",
                "gpu_count",
                "cpu_cores",
                "memory_gib",
                "disk_gib",
                "reserved_ram_gib",
                "reserved_vram_gib",
            )
            if any(type(getattr(group, k)) is not int for k in integer_fields):
                raise ValueError("Worker counts and capacities must be integers")
            if group.count < 0 or not 1 <= group.gpu_count <= 64:
                raise ValueError("Worker count must be nonnegative; GPU count must be 1..64")
            if min(group.cpu_cores, group.memory_gib, group.disk_gib) <= 0:
                raise ValueError("Worker CPU, memory and disk capacities must be positive")
            if not 0 <= group.reserved_ram_gib < group.memory_gib:
                raise ValueError("Reserved RAM must be smaller than worker memory")
            if not 0 <= group.reserved_vram_gib < PROFILES[group.gpu_profile]["memory_gib"]:
                raise ValueError("Reserved VRAM must be smaller than GPU memory")
            if not isinstance(group.labels, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) for k, v in group.labels.items()
            ):
                raise ValueError("Worker labels must be string pairs")
        workers = self.workers()
        if not 1 <= len(workers) <= MAX_WORKERS:
            raise ValueError(f"This release supports 1..{MAX_WORKERS} configured Workers")
        if len({w.name for w in workers}) != len(workers):
            raise ValueError("Worker names must be unique across groups")
        count = len(workers)
        n = self.network
        if n.worker_ip_cidr is not None:
            network = ip_network(n.worker_ip_cidr)
            if not isinstance(network, IPv4Network) or not any(
                network.subnet_of(ip_network(private))
                for private in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8")
            ):
                raise ValueError("network.worker_ip_cidr must be a private or loopback IPv4 subnet")
            if network.num_addresses - 2 < count:
                raise ValueError("network.worker_ip_cidr has insufficient usable node addresses")
        ports = [
            n.control_port,
            *range(n.api_port_base, n.api_port_base + count),
            *range(n.metrics_port_base, n.metrics_port_base + count),
        ]
        if any(type(p) is not int or not 1024 <= p <= 65535 for p in ports):
            raise ValueError("Worker, metrics and control ports must be in 1024..65535")
        if len(set(ports)) != len(ports):
            raise ValueError("Worker, metrics and control port ranges overlap")
        if not 1024 <= n.instance_port_start <= n.instance_port_end <= 65535:
            raise ValueError("Invalid instance port range")
        if n.instance_port_stride is not None and (
            type(n.instance_port_stride) is not int or not 0 <= n.instance_port_stride <= 65535
        ):
            raise ValueError("instance_port_stride must be a nonnegative integer")
        width = n.instance_port_end - n.instance_port_start + 1
        if (
            not n.worker_ip_cidr
            and count > 1
            and n.instance_port_stride is not None
            and 0 < n.instance_port_stride < width
        ):
            raise ValueError(
                "Per-Worker instance port blocks overlap; use stride 0 for a shared pool"
            )
        for worker in workers:
            low, high = n.instance_ports_for_slot(worker.slot)
            if high > 65535:
                raise ValueError("Per-Worker instance port blocks exceed port 65535")
            if any(low <= port <= high for port in ports):
                raise ValueError("Instance port range overlaps another listener")


def _section(cls, value):
    if not isinstance(value, dict):
        raise ValueError(f"{cls.__name__} must be a mapping")
    unknown = set(value) - {f.name for f in fields(cls)}
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} fields: {', '.join(sorted(unknown))}")
    return cls(**value)


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or set(raw) - {"server", "network", "simulation", "worker_groups"}:
        raise ValueError("Invalid top-level configuration fields")
    config = Config(
        server=_section(ServerConfig, raw.get("server", {})),
        network=_section(NetworkConfig, raw.get("network", {})),
        simulation=_section(SimulationConfig, raw.get("simulation", {})),
        worker_groups=[_section(WorkerGroup, g) for g in raw.get("worker_groups", [{}])],
    )
    data_dir = Path(config.simulation.data_dir)
    if not data_dir.is_absolute():
        config.simulation.data_dir = str(path.parent / data_dir)
    config.validate()
    return config
