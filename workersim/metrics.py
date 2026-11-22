"""GPUStack 2.3.1 Worker exporter: node families plus the aggregator's runtime snapshot."""

from workersim.model_metrics import Family, render_families

# (name, help) in native exporter order; all are gauges keyed by worker (+ gpu / mountpoint).
_NODE = (
    ("uptime_seconds", "Uptime in seconds of the worker node"),
    ("cpu_cores", "Total CPUs cores of the worker node"),
    ("cpu_utilization_rate", "Rate of CPU utilization on the worker node"),
    ("memory_total_bytes", "Total memory in bytes of the worker node"),
    ("memory_used_bytes", "Memory used in bytes of the worker node"),
    ("memory_utilization_rate", "Rate of memory utilization on the worker node"),
)
_GPU = (
    ("gpu_cores", "Total GPUs cores of the worker node"),
    ("gpu_utilization_rate", "Rate of GPU utilization on the worker node"),
    ("gpu_temperature_celsius", "GPU temperature in celsius of the worker node"),
    ("gpu_power_limit_watts", "GPU power management limit in watts of the worker node"),
    ("gpu_power_used_watts", "GPU power usage in watts of the worker node"),
    ("gram_total_bytes", "Total GPU RAM in bytes of the worker node"),
    ("gram_allocated_bytes", "Allocated GPU RAM in bytes of the worker node"),
    ("gram_used_bytes", "GPU RAM used in bytes of the worker node"),
    ("gram_utilization_rate", "Rate of GPU RAM utilization on the worker node"),
)
_FS = (
    ("filesystem_total_bytes", "Total filesystem in bytes of the worker node"),
    ("filesystem_used_bytes", "Total filesystem used in bytes of the worker node"),
    ("filesystem_utilization_rate", "Rate of filesystem utilization on the worker node"),
)


def _rate(used, total):
    return round(used / total, 6) * 100 if total else 0


def node_families(worker) -> list[Family]:
    status = worker.status_payload()["status"]
    _, allocated_vram = worker.hardware.allocated(worker.worker_id, worker.instances)
    base = {"worker_id": str(worker.worker_id), "worker_name": worker.spec.name,
            "instance": worker.ip}

    def family(name, doc, kind="gauge"):
        return Family("gpustack:worker_node_" + name, kind, doc)

    os_info = Family("gpustack:worker_node_os", "info", "Operating system information")
    os_info.samples.append((os_info.name + "_info", base | {
        "name": status["os"]["name"], "version": status["os"]["version"]}, 1.0))
    kernel = Family("gpustack:worker_node_kernel", "info", "Kernel information")
    kernel.samples.append((kernel.name + "_info", base | status["kernel"], 1.0))
    node = {key: family(key, doc) for key, doc in _NODE}
    node["uptime_seconds"].samples.append((node["uptime_seconds"].name, base,
                                           float(status["uptime"]["uptime"])))
    for key, value in (
        ("cpu_cores", status["cpu"]["total"]),
        ("cpu_utilization_rate", status["cpu"]["utilization_rate"]),
        ("memory_total_bytes", status["memory"]["total"]),
        ("memory_used_bytes", status["memory"]["used"]),
        ("memory_utilization_rate", _rate(status["memory"]["used"], status["memory"]["total"])),
    ):
        node[key].samples.append((node[key].name, base, float(value)))
    # Native InfoMetricFamily("worker_node_gpu") is not prefixed with "gpustack:".
    gpu_info = Family("worker_node_gpu", "info", "GPU information")
    gpu = {key: family(key, doc) for key, doc in _GPU}
    for index, device in enumerate(status["gpu_devices"] or []):
        labels = base | {"gpu_index": str(index), "gpu_name": device["name"],
                         "gpu_chip_index": "0"}
        gpu_info.samples.append(("worker_node_gpu_info", labels, 1.0))
        memory = device["memory"]
        for key, value in (
            ("gpu_cores", device["core"]["total"]),
            ("gpu_utilization_rate", device["core"]["utilization_rate"]),
            ("gpu_temperature_celsius", device["temperature"]),
            ("gpu_power_limit_watts", device["power"]),
            ("gpu_power_used_watts", device["power_used"]),
            ("gram_total_bytes", memory["total"]),
            ("gram_allocated_bytes", allocated_vram.get(device["index"], 0)),
            ("gram_used_bytes", memory["used"]),
            ("gram_utilization_rate", _rate(memory["used"], memory["total"])),
        ):
            gpu[key].samples.append((gpu[key].name, labels, float(value)))
    fs = {key: family(key, doc) for key, doc in _FS}
    for disk in status["filesystem"]:
        labels = base | {"mountpoint": disk["mount_point"]}
        for key, value in (
            ("filesystem_total_bytes", disk["total"]),
            ("filesystem_used_bytes", disk["used"]),
            ("filesystem_utilization_rate", _rate(disk["used"], disk["total"])),
        ):
            fs[key].samples.append((fs[key].name, labels, float(value)))
    return [os_info, kernel, *node.values(), gpu_info, *gpu.values(), *fs.values()]


def simulator_families(worker) -> list[Family]:
    """WorkerSim-only health series, kept separate from every native family."""
    base = {"worker_id": str(worker.worker_id), "worker_name": worker.spec.name,
            "instance": worker.ip}
    allocated_ram, _ = worker.hardware.allocated(worker.worker_id, worker.instances)
    rows = [
        ("workersim_worker_simulated", "gauge", base, 1),
        ("workersim_worker_online", "gauge", base, int(worker.online)),
        ("workersim_worker_watch_connected", "gauge", base, int(worker.watch_connected)),
        ("workersim_worker_file_watch_connected", "gauge", base,
         int(worker.file_watch_connected)),
        ("workersim_worker_report_errors", "counter", base, worker.report_errors),
        ("workersim_worker_stale_reports", "counter", base, worker.stale_reports),
        ("workersim_worker_deleted_assignments", "counter", base, worker.deleted_assignments),
        ("workersim_worker_restarts_requested", "counter", base, worker.restarts_requested),
        ("workersim_worker_instances_running", "gauge", base, len(worker.runtime.running_ids)),
        ("workersim_worker_ram_allocated_bytes", "gauge", base, allocated_ram),
    ]
    families = {}
    for name, kind, labels, value in rows:
        family = families.setdefault(name, Family(name, kind, "WorkerSim simulator state"))
        family.samples.append((name + ("_total" if kind == "counter" else ""), labels, value))
    events = Family("workersim_worker_watch_events", "counter", "WorkerSim watch events")
    for resource, count in worker.event_counts.items():
        events.samples.append((events.name + "_total", base | {"resource": resource}, count))
    return [*families.values(), events]


def render(worker) -> str:
    return render_families(
        [*node_families(worker), *worker.runtime_metrics.unified.fresh(),
         *simulator_families(worker)]
    )


def render_raw(worker) -> str:
    return render_families(worker.runtime_metrics.raw.fresh())
