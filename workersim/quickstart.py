"""Start a fleet from GPUStack's own "Add Worker" docker command.

Users copy the command the GPUStack UI shows, replace the image with WorkerSim's and
drop ``--runtime``; every other native argument is accepted. Fleet size and hardware
come from built-in presets, overridable per field by flags or ``WORKERSIM_*`` variables.
"""

import argparse
import json
import logging
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlparse

from workersim.config import MAX_WORKERS, PROFILES, Config, WorkerGroup, load_config

log = logging.getLogger(__name__)

# Typical host shape for each accelerator profile: cards per Worker, CPU threads,
# RAM and disk. Synthetic defaults for scheduling tests, not vendor specifications.
HOST_PRESETS = {
    "b300-288gb": {"gpu_count": 8, "cpu_cores": 224, "memory_gib": 2048, "disk_gib": 8192},
    "h100-80gb": {"gpu_count": 8, "cpu_cores": 224, "memory_gib": 2048, "disk_gib": 8192},
    "a800-80gb": {"gpu_count": 8, "cpu_cores": 128, "memory_gib": 1024, "disk_gib": 4096},
    "rtx4090-24gb": {"gpu_count": 8, "cpu_cores": 64, "memory_gib": 512, "disk_gib": 2048},
    "ascend-910b4-64gb": {"gpu_count": 8, "cpu_cores": 192, "memory_gib": 1536, "disk_gib": 4096},
}
DEFAULT_PROFILE = "h100-80gb"
CONTAINER_DATA_DIR = "/var/lib/gpustack"
RUN_AS_UID = 10001

# (option, dest, type, help). Each is also read from WORKERSIM_<DEST upper>.
FLEET_OPTIONS = (
    ("--workers", "workers", int, "Number of simulated Workers (default 1)"),
    ("--profile", "profile", str, f"Hardware preset (default {DEFAULT_PROFILE})"),
    ("--gpus-per-worker", "gpus_per_worker", int, "Cards per Worker (preset default)"),
    ("--gpu-name", "gpu_name", str, "Override the reported device name"),
    ("--gpu-memory-gib", "gpu_memory_gib", float, "Override memory per card, GiB"),
    ("--cpu-cores", "cpu_cores", int, "CPU threads per Worker (preset default)"),
    ("--memory-gib", "memory_gib", int, "Host RAM per Worker, GiB (preset default)"),
    ("--disk-gib", "disk_gib", int, "Disk per Worker, GiB (preset default)"),
    ("--nvlink-domain-workers", "nvlink_domain_workers", int,
     "Workers per multi-node NVLink domain (NVIDIA)"),
    ("--model-rps", "model_rps", float, "Synthetic background requests/s per model (default 2)"),
)
# Native gpustack start flags given a WorkerSim meaning.
NATIVE_OPTIONS = (
    ("--server-url", "server_url", str),
    ("--token", "token", str),
    ("--worker-ip", "worker_ip", str),
    ("--advertise-address", "advertise_address", str),
    ("--worker-name", "worker_name", str),
    ("--data-dir", "data_dir", str),
    ("--worker-port", "worker_port", int),
    ("--worker-metrics-port", "worker_metrics_port", int),
    ("--kv-transfer-ifname", "kv_transfer_ifname", str),
)
GROUP_KEYS = {
    "workers": ("count", int), "profile": ("gpu_profile", str), "gpus": ("gpu_count", int),
    "prefix": ("name_prefix", str), "gpu-name": ("gpu_name", str),
    "gpu-memory-gib": ("gpu_memory_gib", float), "cpu-cores": ("cpu_cores", int),
    "memory-gib": ("memory_gib", int), "disk-gib": ("disk_gib", int),
    "nvlink-domain-workers": ("nvlink_domain_workers", int),
}


def is_native_invocation(argv) -> bool:
    """True when the arguments come from a native-style docker command, not a subcommand."""
    subcommands = {"validate", "run", "status", "stop", "fleet-scale", "scenario"}
    return not any(arg in subcommands for arg in argv) and any(
        arg.split("=", 1)[0] in ("--server-url", "--workers", "--profile", "--list-profiles",
                                 "--group", "--token", "--dry-run")
        for arg in argv
    )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="workersim",
        description="Start simulated Workers with GPUStack's Add Worker arguments.",
    )
    for option, dest, kind, help_text in FLEET_OPTIONS:
        p.add_argument(option, dest=dest, type=kind, help=help_text)
    for option, dest, kind in NATIVE_OPTIONS:
        p.add_argument(option, dest=dest, type=kind, help=argparse.SUPPRESS)
    p.add_argument("--group", action="append", default=[], metavar="KEY=VALUE,...",
                   help="Add a Worker group, e.g. workers=10,profile=b300-288gb,gpus=4")
    p.add_argument("--label", action="append", default=[], metavar="KEY=VALUE",
                   help="Worker label (repeatable)")
    p.add_argument("--config", "-c", help="Base YAML configuration; flags override it")
    p.add_argument("--no-reserve-ports", action="store_true",
                   help="Do not adjust the host's reserved port list")
    p.add_argument("--list-profiles", action="store_true", help="Print hardware presets")
    p.add_argument("--dry-run", action="store_true", help="Print the resolved fleet and exit")
    p.add_argument("--verbose", action="store_true")
    return p


def parse(argv, environ=None):
    environ = os.environ if environ is None else environ
    args, unknown = parser().parse_known_args(argv)
    for _, dest, kind, _ in FLEET_OPTIONS:
        value = environ.get("WORKERSIM_" + dest.upper())
        if getattr(args, dest) is None and value not in (None, ""):
            setattr(args, dest, kind(value))
    if not args.group and environ.get("WORKERSIM_GROUPS"):
        args.group = [g for g in environ["WORKERSIM_GROUPS"].split(";") if g.strip()]
    if not args.label and environ.get("WORKERSIM_LABELS"):
        args.label = [x for x in environ["WORKERSIM_LABELS"].split(",") if x.strip()]
    return args, unknown


def profiles_table() -> list[dict]:
    rows = []
    for name, preset in HOST_PRESETS.items():
        gpu = PROFILES[name]
        rows.append({
            "profile": name, "device": gpu["name"], "vendor": gpu["vendor"], "type": gpu["type"],
            "memory_gib_per_card": gpu["memory_gib"], **preset,
        })
    return rows


def default_prefix(worker_name=None) -> str:
    raw = worker_name or f"{socket.gethostname()}-sim"
    prefix = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-_") or "workersim"
    return prefix[:48]


def detect_ip(server_url: str) -> str:
    """The local address used to reach the Server, as native detection prefers."""
    host = urlparse(server_url).hostname or "8.8.8.8"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((host, 80))
            address = sock.getsockname()[0]
            if not address.startswith("127."):
                return address
    except OSError:
        pass
    return "127.0.0.1"


def make_group(base: dict, prefix: str, labels: dict, overrides: dict) -> WorkerGroup:
    profile = overrides.get("gpu_profile") or base["gpu_profile"]
    if profile not in HOST_PRESETS:
        choices = ", ".join(HOST_PRESETS)
        raise ValueError(f"Unknown profile {profile!r}; choose one of: {choices}")
    fields = {**HOST_PRESETS[profile], **{k: v for k, v in base.items() if v is not None}}
    fields.update({k: v for k, v in overrides.items() if v is not None})
    fields["gpu_profile"] = profile
    fields.setdefault("name_prefix", prefix)
    if fields.get("gpu_memory_gib") is not None and float(fields["gpu_memory_gib"]).is_integer():
        fields["gpu_memory_gib"] = int(fields["gpu_memory_gib"])
    reserved_vram = 1 if fields.get("gpu_memory_gib", PROFILES[profile]["memory_gib"]) > 1 else 0
    return WorkerGroup(labels=dict(labels), reserved_vram_gib=reserved_vram, **fields)


def parse_group(spec: str) -> dict:
    result = {}
    for item in filter(None, (x.strip() for x in spec.split(","))):
        key, sep, value = item.partition("=")
        if not sep or key not in GROUP_KEYS:
            raise ValueError(
                f"Invalid --group item {item!r}; keys: {', '.join(sorted(GROUP_KEYS))}"
            )
        field, kind = GROUP_KEYS[key]
        result[field] = kind(value)
    return result


def build_config(args, environ=None) -> Config:
    environ = os.environ if environ is None else environ
    config = load_config(args.config) if args.config else Config()
    if args.server_url:
        config.server.url = args.server_url.rstrip("/")
    if "GPUSTACK_TOKEN" in environ or args.token:
        config.server.registration_token_env = "GPUSTACK_TOKEN"
    if not args.config:
        data_dir = args.data_dir or (
            CONTAINER_DATA_DIR if Path(CONTAINER_DATA_DIR).is_dir() else ".workersim"
        )
        # A subdirectory, so a volume shared with a real Worker is never rewritten.
        config.simulation.data_dir = str(Path(data_dir) / "workersim")
    if args.model_rps is not None:
        config.simulation.model_requests_per_second = args.model_rps
    n = config.network
    if args.worker_ip or not args.config:
        n.worker_ip = args.worker_ip or detect_ip(config.server.url)
    if args.advertise_address or not args.config:
        n.advertise_address = args.advertise_address or n.worker_ip
    if args.worker_port:
        n.api_port_base = args.worker_port
    if args.worker_metrics_port:
        n.metrics_port_base = args.worker_metrics_port
    labels = {}
    for item in args.label:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"Invalid --label {item!r}; use KEY=VALUE")
        labels[key] = value
    base = {
        "gpu_profile": args.profile or DEFAULT_PROFILE,
        "gpu_count": args.gpus_per_worker, "gpu_name": args.gpu_name,
        "gpu_memory_gib": args.gpu_memory_gib, "cpu_cores": args.cpu_cores,
        "memory_gib": args.memory_gib, "disk_gib": args.disk_gib,
        "nvlink_domain_workers": args.nvlink_domain_workers,
        "kv_transfer_ifname": args.kv_transfer_ifname,
    }
    prefix = default_prefix(args.worker_name)
    if args.group:
        groups = []
        for index, spec in enumerate(args.group, 1):
            overrides = parse_group(spec)
            overrides.setdefault("name_prefix", f"{prefix}-g{index}")
            groups.append(make_group(base, prefix, labels, overrides))
        config.worker_groups = groups
    elif not args.config or args.workers or args.profile:
        count = args.workers if args.workers is not None else 1
        config.worker_groups = [make_group(base, prefix, labels, {"count": count})]
    total = sum(g.count for g in config.worker_groups)
    if not 1 <= total <= MAX_WORKERS:
        raise ValueError(f"One WorkerSim container supports 1..{MAX_WORKERS} Workers")
    width = n.instance_port_end - n.instance_port_start + 1
    if not n.worker_ip_cidr and n.instance_port_start + total * width - 1 > 65535:
        raise ValueError("The inference port pool does not fit; reduce --workers")
    config.validate()
    return config


def describe(config: Config) -> dict:
    n = config.network
    total = len(config.workers())
    low, _ = n.instance_ports_for_slot(0)
    _, high = n.instance_ports_for_slot(total - 1)
    return {
        "server_url": config.server.url,
        "token_env": config.server.registration_token_env,
        "worker_ip": n.worker_ip, "advertise_address": n.advertise_address,
        "data_dir": config.simulation.data_dir,
        "workers": total,
        "devices": sum(w.group.gpu_count for w in config.workers()),
        "groups": [
            {
                "prefix": g.name_prefix, "workers": g.count, "profile": g.gpu_profile,
                "device": g.resolved_gpu_profile()["name"], "gpus": g.gpu_count,
                "gpu_memory_gib": g.resolved_gpu_profile()["memory_gib"],
                "cpu_cores": g.cpu_cores, "memory_gib": g.memory_gib, "disk_gib": g.disk_gib,
            }
            for g in config.worker_groups
        ],
        "ports": {
            "worker_api": f"{n.api_port_base}-{n.api_port_base + total - 1}",
            "metrics": f"{n.metrics_port_base}-{n.metrics_port_base + total - 1}",
            "inference": f"{low}-{high}", "control": n.control_port,
        },
    }


# ------------------------------------------------------------------ host setup


def merge_ranges(value: str, ranges) -> str:
    intervals = list(ranges)
    for item in filter(None, value.strip().split(",")):
        first, _, last = item.strip().partition("-")
        intervals.append((int(first), int(last or first)))
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in merged)


def fleet_ranges(config: Config):
    n = config.network
    total = len(config.workers())
    _, high = n.instance_ports_for_slot(total - 1)
    return [
        (n.instance_ports_for_slot(0)[0], high),
        (n.api_port_base, n.api_port_base + total - 1),
        (n.metrics_port_base, n.metrics_port_base + total - 1),
        (n.control_port, n.control_port),
    ]


def reserve_ports(config: Config, proc="/proc/sys/net/ipv4") -> str:
    """Reserve the fleet's listener ports from automatic source-port selection.

    With ``--network=host`` and ``--privileged`` (as in GPUStack's command), this writes
    the host's network namespace settings. The change is not persisted across reboots;
    the container reapplies it on every start. Returns a short outcome for the log.
    """
    reserved = Path(proc, "ip_local_reserved_ports")
    ephemeral = Path(proc, "ip_local_port_range")
    ranges = fleet_ranges(config)
    try:
        before = reserved.read_text().strip()
        after = merge_ranges(before, ranges)
        if after != before:
            reserved.write_text(after + "\n")
        low, high = map(int, ephemeral.read_text().split())
        claimed = sum(
            max(0, min(b, high) - max(a, low) + 1) for a, b in ranges
        )
        # Keep at least 20000 automatic source ports for the fleet's own connections.
        if high - low + 1 - claimed < 20000 and low > 10240:
            ephemeral.write_text(f"10240 {max(high, 65535)}\n")
            return f"reserved {after}; widened source-port range to 10240-{max(high, 65535)}"
        return f"reserved {after}"
    except OSError as error:
        return (
            f"not changed ({error.strerror or type(error).__name__}); run with --privileged "
            "and --network=host for large fleets"
        )


def drop_privileges(data_dir: str):
    """Leave root after host setup; only WorkerSim's own state directory is chowned."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(path):
        for name in [root, *(os.path.join(root, d) for d in dirs),
                     *(os.path.join(root, f) for f in files)]:
            os.chown(name, RUN_AS_UID, RUN_AS_UID)
    os.setgroups([])
    os.setgid(RUN_AS_UID)
    os.setuid(RUN_AS_UID)


async def start(argv) -> int:
    from workersim.fleet import Fleet

    args, unknown = parse(argv)
    if args.list_profiles:
        print(json.dumps(profiles_table(), indent=2))
        return 0
    if unknown:
        log.info("Ignoring native arguments without a simulator meaning: %s", " ".join(unknown))
    if not args.server_url and not args.config:
        raise ValueError("--server-url is required (copy it from GPUStack's Add Worker command)")
    config = build_config(args)
    summary = describe(config)
    if args.dry_run:
        print(json.dumps(summary, indent=2))
        return 0
    if os.environ.get(config.server.registration_token_env) is None and args.token:
        os.environ[config.server.registration_token_env] = args.token
    if not args.no_reserve_ports:
        log.info("Host port reservation: %s", reserve_ports(config))
    drop_privileges(config.simulation.data_dir)
    log.info("Starting WorkerSim fleet: %s", json.dumps(summary))
    await Fleet(config).run()
    return 0
