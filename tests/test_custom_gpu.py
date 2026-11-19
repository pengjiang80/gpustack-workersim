import asyncio
import copy
import json

import aiohttp
import pytest
from conftest import eventually

from workersim.config import GIB, PROFILES, WorkerGroup, load_config
from workersim.fleet import Fleet
from workersim.hardware import Hardware


@pytest.mark.parametrize("template", ["h100-80gb", "ascend-910b4-64gb"])
@pytest.mark.parametrize("overrides", [
    {}, {"gpu_name": " Test GPU 测试 "}, {"gpu_memory_gib": 96.5},
    {"gpu_name": "Test GPU", "gpu_memory_gib": 48},
])
def test_yaml_overrides_keep_template_properties_and_other_groups(tmp_path, template, overrides):
    import yaml

    before = copy.deepcopy(PROFILES)
    path = tmp_path / "custom.yaml"
    path.write_text(yaml.safe_dump({"worker_groups": [
        {"name_prefix": "custom", "count": 1, "gpu_profile": template, **overrides},
        {"name_prefix": "default", "count": 1, "gpu_profile": template,
         "gpu_name": None, "gpu_memory_gib": None},
    ]}), encoding="utf-8")
    config = load_config(path)
    custom, default = [Hardware(config, spec).sample(1, {}, set()) for spec in config.workers()]
    gpu, original = custom["gpu_devices"][0], default["gpu_devices"][0]
    assert gpu["name"] == overrides.get("gpu_name", before[template]["name"]).strip()
    assert gpu["memory"]["total"] == int(
        overrides.get("gpu_memory_gib", before[template]["memory_gib"]) * GIB
    )
    assert type(gpu["memory"]["total"]) is int
    assert custom["memory"]["total"] == 1024 * GIB
    for key in ("vendor", "type", "arch_family", "driver_version", "runtime_version", "power"):
        assert gpu[key] == before[template][key]
    if template.startswith("ascend"):
        assert "compute_capability" not in gpu
    else:
        assert gpu["compute_capability"] == before[template]["compute_capability"]
    assert original["name"] == before[template]["name"]
    assert original["memory"]["total"] == before[template]["memory_gib"] * GIB
    assert PROFILES == before


@pytest.mark.parametrize("value", ["", " \t\n", 123, False, [], {}])
def test_reject_invalid_custom_gpu_name(config, value):
    config.worker_groups[0].gpu_name = value
    with pytest.raises(ValueError, match="gpu_name"):
        config.validate()


@pytest.mark.parametrize("value", [
    0, -1, "96", True, False, [], {}, float("nan"), float("inf"), float("-inf"), 1e308,
])
def test_reject_invalid_custom_gpu_memory(config, value):
    config.worker_groups[0].gpu_memory_gib = value
    with pytest.raises(ValueError, match="gpu_memory_gib"):
        config.validate()


def test_reserved_vram_uses_effective_capacity(config):
    group = config.worker_groups[0]
    # The template's 80 GiB must not constrain a larger override.
    group.gpu_memory_gib = 96.5
    group.reserved_vram_gib = 90
    config.validate()
    # A smaller override must not be checked against the original 80 GiB.
    group.gpu_memory_gib = 16
    group.reserved_vram_gib = 16
    with pytest.raises(ValueError, match="Reserved VRAM"):
        config.validate()
    group.reserved_vram_gib = 0
    group.gpu_memory_gib = 0.5
    config.validate()
    assert Hardware(config, config.workers()[0]).sample(1, {}, set())["gpu_devices"][0][
        "memory"
    ]["total"] == GIB // 2
    group.gpu_profile = "unrecognized-template"
    group.gpu_name = "Custom name does not replace a template"
    with pytest.raises(ValueError, match="Unknown GPU profile"):
        config.validate()


@pytest.mark.asyncio
async def test_custom_inventory_status_metrics_and_deployment(config, control_plane, monkeypatch):
    config.worker_groups = [
        WorkerGroup(name_prefix="custom", count=1, gpu_count=2,
                    gpu_name='Test "GPU" 集群', gpu_memory_gib=48),
        WorkerGroup(name_prefix="memory-only", count=1, gpu_count=2, gpu_memory_gib=96.5),
        WorkerGroup(name_prefix="template", count=1, gpu_count=2),
    ]
    config.network.bind_host = "127.252.0.1"
    config.network.api_port_base = 24100
    config.network.metrics_port_base = 24200
    config.network.control_port = 24300
    config.validate()
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: len(control_plane.statuses) == 3)
        await eventually(lambda: len(control_plane.watchers) == 18)
        expected = [('Test "GPU" 集群', 48), (PROFILES["h100-80gb"]["name"], 96.5),
                    (PROFILES["h100-80gb"]["name"], 80)]
        async with aiohttp.ClientSession() as session:
            for worker, (name, memory) in zip(fleet.workers, expected, strict=True):
                status = control_plane.statuses[worker.worker_id]["status"]
                assert all(d["name"] == name for d in status["gpu_devices"])
                assert all(d["memory"]["total"] == int(memory * GIB)
                           for d in status["gpu_devices"])
                async with session.get(
                    f"http://127.252.0.1:{24200 + worker.spec.slot}/metrics"
                ) as response:
                    assert response.status == 200
                    metrics = await response.text()
                lines = [line for line in metrics.splitlines() if line.startswith(
                    "gpustack:worker_node_gram_total_bytes{"
                )]
                assert len(lines) == 2
                assert all(f"gpu_name={json.dumps(name, ensure_ascii=False)}" in line
                           for line in lines)
                assert all(int(line.rsplit(" ", 1)[1]) == int(memory * GIB) for line in lines)
        worker = fleet.workers[0]
        await control_plane.assign(worker_id=worker.worker_id)
        await eventually(lambda: bool(worker.runtime.running_ids))
        assert worker.hardware.allocated(worker.worker_id, worker.instances)[1] == {0: 8 * GIB}
        assert worker.status_payload()["status"]["gpu_devices"][0]["memory"]["used"] > 0
        control_plane.instances.pop(10)
        await control_plane.emit("DELETED", {"id": 10})
        await eventually(lambda: not worker.runtime.records)
        assert worker.hardware.allocated(worker.worker_id, worker.instances) == (0, {})
    finally:
        fleet.stop.set()
        await task
