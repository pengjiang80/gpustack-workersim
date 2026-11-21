import asyncio

import aiohttp
import pytest
from conftest import eventually, model_metrics

from workersim.fleet import Fleet


@pytest.mark.asyncio
@pytest.mark.parametrize("managed", [False, True])
async def test_deploy_metrics_scale_down_and_restore_identity(
    config, control_plane, monkeypatch, managed
):
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: fleet.workers and fleet.workers[0].client)
        await eventually(lambda: len(control_plane.watchers) == 6)
        instance = await control_plane.assign(managed=managed)
        await eventually(lambda: control_plane.instances[10]["state"] == "running")
        worker = fleet.workers[0]
        await eventually(lambda: 10 in worker.runtime.running_ids)
        port = worker.runtime.records[10].port
        # Native aggregation runs 30 s after registration; run one pass now.
        await worker.runtime_metrics.aggregate()
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"http://127.0.0.1:{config.network.metrics_port_base}/metrics"
            ) as response:
                metrics = await response.text()
                assert "gpustack:worker_node_gram_allocated_bytes" in metrics
                assert "8589934592" in metrics
                assert 'worker_id="1"' in metrics
                assert "gpustack:generation_tokens_total" in metrics
                assert 'model_instance_id="10"' in metrics
                assert "gpustack:runtime_metrics_scrape_status" in metrics
            async with session.get(f"http://127.0.0.1:{port}/metrics") as response:
                assert "vllm:generation_tokens_total" in await response.text()
            async with session.get(f"http://127.0.0.1:{port}/health") as response:
                assert response.status == 200
            async with session.get(
                f"http://127.0.0.1:{config.network.api_port_base}/files/model-config",
                params={"path": "/etc/passwd"},
            ) as response:
                assert response.status == 401
        control_plane.instances.pop(10)
        await control_plane.emit("DELETED", {"id": 10})
        await eventually(lambda: not worker.runtime.records)
        assert worker.runtime.port_key(port) not in fleet.ports
        assert worker.hardware.allocated(worker.worker_id, worker.instances) == (0, {})
        
        assert await model_metrics(worker) == ""
        if managed:
            assert any("preparation" in r for r in control_plane.managed_reports)
            assert any("observations" in r for r in control_plane.managed_reports)
            assert all(r.get("state") != "running" for r in control_plane.managed_reports)
        assert instance["computed_resource_claim"]["vram"] == {"0": 8 * 1024**3}
        if not managed:
            assert instance["worker_ip"] == worker.ip
            assert instance["worker_advertise_address"] == worker.advertise_address
    finally:
        fleet.stop.set()
        await task
    # A new process object restores the saved server-issued identity and per-Worker token.
    second = Fleet(config)
    second_task = asyncio.create_task(second.run())
    try:
        await eventually(lambda: second.workers and second.workers[0].client)
        assert len(control_plane.workers) == 1
        assert second.workers[0].identity["worker_uuid"] == "server-assigned-1"
    finally:
        second.stop.set()
        await second_task


@pytest.mark.asyncio
async def test_snapshot_reaps_missed_deletion_and_generation_releases_port(
    config, control_plane, monkeypatch
):
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: fleet.workers and fleet.workers[0].client)
        await control_plane.assign()
        worker = fleet.workers[0]
        await eventually(lambda: 10 in worker.runtime.running_ids)
        old = worker.runtime.records[10]
        control_plane.instances[10].update(restart_count=1, state="scheduled", port=None, ports=[])
        await control_plane.emit("UPDATED", control_plane.instances[10])
        await eventually(
            lambda: (
                worker.runtime.records.get(10)
                and worker.runtime.records[10].generation == 1
                and 10 in worker.runtime.running_ids
            )
        )
        assert worker.runtime.records[10] is not old
        control_plane.instances.pop(10)  # Deliberately omit the deletion event.
        await eventually(lambda: not worker.runtime.records)
        assert not fleet.ports
    finally:
        fleet.stop.set()
        await task


@pytest.mark.asyncio
async def test_rejected_reports_never_become_ready(config, control_plane, monkeypatch):
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    control_plane.reject_report = True
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: fleet.workers and fleet.workers[0].client)
        await control_plane.assign(managed=True)
        worker = fleet.workers[0]
        await eventually(lambda: worker.stale_reports > 0)
        assert worker.report_errors == 0
        assert not worker.runtime.running_ids
        assert control_plane.instances[10]["state"] == "scheduled"
        assert len(fleet.ports) <= 1
    finally:
        fleet.stop.set()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("cidr", [None, "127.251.0.0/24"])
async def test_native_port_blocks_and_per_ip_reuse(config, control_plane, monkeypatch, cidr):
    from workersim.config import WorkerGroup

    config.worker_groups = [WorkerGroup(count=2)]
    config.network.bind_host = "127.252.0.1"
    config.network.worker_ip = "127.252.0.1"
    config.network.advertise_address = "127.252.0.1"
    config.network.api_port_base = 33100
    config.network.metrics_port_base = 33200
    config.network.control_port = 30300
    config.network.worker_ip_cidr = cidr
    config.network.instance_port_start = 40000
    config.network.instance_port_end = 40063
    config.network.instance_port_stride = None
    config.validate()
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    # Simulate restoring instances that published an older simulator's port range.
    for key in (1, 2):
        instance = await control_plane.assign(worker_id=key, key=key)
        instance.update(port=21000 + key, ports=[21000 + key])
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: sum(len(w.runtime.running_ids) for w in fleet.workers) == 2)
        endpoints = []
        async with aiohttp.ClientSession() as session:
            for worker in fleet.workers:
                instance = control_plane.instances[worker.worker_id]
                low, high = config.network.instance_ports_for_slot(worker.spec.slot)
                assert low <= instance["port"] <= high
                assert instance["ports"] == [instance["port"]]
                endpoints.append((worker.ip, instance["port"]))
                async with session.get(f"http://{worker.ip}:{instance['port']}/health") as response:
                    assert response.status == 200
        assert len(set(endpoints)) == 2
        if cidr:
            assert endpoints[0][1] == endpoints[1][1]
        else:
            assert endpoints[0][1] != endpoints[1][1]
        assert sum(w.report_errors for w in fleet.workers) == 0
    finally:
        fleet.stop.set()
        await task
