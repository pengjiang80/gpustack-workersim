"""GPUStack 2.3.1 Prometheus metric names and label shapes."""

import json

from workersim.model_metrics import render_models


def render(worker) -> str:
    status = worker.status_payload()["status"]
    allocated_ram, allocated_vram = worker.hardware.allocated(worker.worker_id, worker.instances)
    base = {
        "worker_id": str(worker.worker_id),
        "worker_name": worker.spec.name,
        "instance": worker.ip,
    }
    lines = []

    def emit(name, value, labels=None):
        labels = base | (labels or {})
        encoded = ",".join(
            f"{k}={json.dumps(str(v), ensure_ascii=False)}" for k, v in labels.items()
        )
        lines.append(f"{name}{{{encoded}}} {value}")

    def metric(name, value, labels=None):
        emit("gpustack:worker_node_" + name, value, labels)

    metric("os_info", 1, status["os"])
    metric("kernel_info", 1, status["kernel"])
    metric("uptime_seconds", status["uptime"]["uptime"])
    metric("cpu_cores", status["cpu"]["total"])
    metric("cpu_utilization_rate", status["cpu"]["utilization_rate"])
    for name in ("total", "used", "utilization_rate"):
        suffix = "_bytes" if name != "utilization_rate" else ""
        metric("memory_" + name + suffix, status["memory"][name])
    emit("workersim_worker_simulated", 1)
    emit("workersim_worker_online", int(worker.online))
    emit("workersim_worker_watch_connected", int(worker.watch_connected))
    emit("workersim_worker_file_watch_connected", int(worker.file_watch_connected))
    for resource, count in worker.event_counts.items():
        emit("workersim_worker_watch_events_total", count, {"resource": resource})
    emit("workersim_worker_report_errors_total", worker.report_errors)
    emit("workersim_worker_stale_reports_total", worker.stale_reports)
    emit("workersim_worker_deleted_assignments_total", worker.deleted_assignments)
    emit("workersim_worker_instances_running", len(worker.runtime.running_ids))
    emit("workersim_worker_ram_allocated_bytes", allocated_ram)
    for gpu in status["gpu_devices"]:
        labels = {"gpu_index": str(gpu["index"]), "gpu_name": gpu["name"], "gpu_chip_index": "0"}
        emit("worker_node_gpu_info", 1, labels)
        for name, value in {
            "gpu_cores": gpu["core"]["total"],
            "gpu_utilization_rate": gpu["core"]["utilization_rate"],
            "gpu_temperature_celsius": gpu["temperature"],
            "gpu_power_limit_watts": gpu["power"],
            "gpu_power_used_watts": gpu["power_used"],
            "gram_total_bytes": gpu["memory"]["total"],
            "gram_used_bytes": gpu["memory"]["used"],
            "gram_allocated_bytes": allocated_vram.get(gpu["index"], 0),
            "gram_utilization_rate": gpu["memory"]["utilization_rate"],
        }.items():
            metric(name, value, labels)
    for disk in status["filesystem"]:
        labels = {"mountpoint": disk["mount_point"]}
        metric("filesystem_total_bytes", disk["total"], labels)
        metric("filesystem_used_bytes", disk["used"], labels)
        metric("filesystem_utilization_rate", disk["used"] / disk["total"] * 100, labels)
    return "\n".join(lines) + "\n" + render_models(worker)
