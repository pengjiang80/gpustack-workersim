"""Synthetic observations driven by the Server's assigned resource claims."""

import hashlib
import math
import time
from datetime import datetime, timezone

from workersim.config import GIB, Config, WorkerSpec


def local_member(instance: dict, worker_id: int) -> dict | None:
    if instance.get("worker_id") == worker_id:
        return instance
    for member in (instance.get("distributed_servers") or {}).get("subordinate_workers") or []:
        if member.get("worker_id") == worker_id:
            return member
    return None


class Hardware:
    def __init__(self, config: Config, spec: WorkerSpec):
        self.config = config
        self.spec = spec
        self.started = time.time()
        digest = hashlib.sha256(f"{config.simulation.seed}/{spec.name}".encode()).digest()
        self.phase = int.from_bytes(digest[:4], "big") / 2**32 * 2 * math.pi

    def allocated(self, worker_id: int, instances: dict) -> tuple[int, dict[int, int]]:
        ram, vram = 0, {}
        for instance in instances.values():
            member = local_member(instance, worker_id)
            if member is None:
                continue
            claim = member.get("computed_resource_claim") or {}
            ram += claim.get("ram") or 0
            for index, amount in (claim.get("vram") or {}).items():
                vram[int(index)] = vram.get(int(index), 0) + amount
        return ram, vram

    def sample(self, worker_id: int, instances: dict, running_ids: set[int]) -> dict:
        group = self.spec.group
        profile = group.resolved_gpu_profile()
        ram, _ = self.allocated(worker_id, instances)
        active_vram = {}
        for instance_id in running_ids:
            member = local_member(instances.get(instance_id, {}), worker_id)
            if member:
                for index, amount in (
                    (member.get("computed_resource_claim") or {}).get("vram") or {}
                ).items():
                    index = int(index)
                    active_vram[index] = active_vram.get(index, 0) + amount
        wave = (1 + math.sin(time.monotonic() / 17 + self.phase)) / 2
        devices = []
        for index in range(group.gpu_count):
            total = int(profile["memory_gib"] * GIB)
            occupied = active_vram.get(index, 0)
            load = (40 + wave * 35 + index % 5) if occupied else 0.5 + wave
            used = min(total, int(total * 0.004 + occupied * 0.92))
            static = {k: v for k, v in profile.items() if k not in ("memory_gib", "core_count")}
            devices.append(
                {
                    **static,
                    "index": index,
                    "device_index": index,
                    "device_chip_index": 0,
                    "uuid": f"GPU-SIM-{self.config.simulation.namespace}-{self.spec.name}-{index}",
                    "core": {"total": profile["core_count"], "utilization_rate": round(load, 2)},
                    "memory": {
                        "total": total,
                        "used": used,
                        "utilization_rate": used / total * 100,
                        "is_unified_memory": False,
                    },
                    "temperature": round(32 + load * 0.45, 1),
                    "power_used": round(profile["power"] * (0.12 + load * 0.008), 1),
                }
            )
        total_ram = group.memory_gib * GIB
        used_ram = min(total_ram, int(total_ram * (0.08 + wave * 0.01) + ram * 0.8))
        disk_total = group.disk_gib * GIB
        disk_used = int(disk_total * 0.12)
        return {
            "cpu": {"total": group.cpu_cores, "utilization_rate": 3 + wave * 4 + len(running_ids)},
            "memory": {
                "total": total_ram,
                "used": used_ram,
                "utilization_rate": used_ram / total_ram * 100,
                "is_unified_memory": False,
            },
            "swap": {"total": 0, "used": 0, "utilization_rate": 0},
            "filesystem": [
                {
                    "name": "sim-disk",
                    "mount_point": "/",
                    "mount_from": "/dev/sim0",
                    "total": disk_total,
                    "used": disk_used,
                    "free": disk_total - disk_used,
                    "available": disk_total - disk_used,
                }
            ],
            "os": {"name": "Ubuntu", "version": "24.04"},
            "kernel": {
                "name": "Linux",
                "release": "6.8.0-sim",
                "version": "WorkerSim",
                "architecture": "x86_64",
            },
            "uptime": {
                "uptime": time.time() - self.started,
                "boot_time": datetime.fromtimestamp(self.started, timezone.utc).isoformat(),
            },
            "gpu_devices": devices,
        }
