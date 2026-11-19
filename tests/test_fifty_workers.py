import asyncio

import aiohttp
import pytest
from conftest import eventually

from workersim.config import WorkerGroup
from workersim.fleet import Fleet


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [50, 100])
async def test_independent_workers_and_scale_to_zero(config, control_plane, monkeypatch, count):
    config.worker_groups = (
        [WorkerGroup(name_prefix="sim-b300", gpu_profile="b300-288gb", count=100)]
        if count == 100
        else [
            WorkerGroup(name_prefix="sim-h100", count=25),
            WorkerGroup(name_prefix="sim-a800", gpu_profile="a800-80gb", count=25),
        ]
    )
    config.network.api_port_base = 32100
    # Keep test listeners off the source address used by ephemeral client sockets on Windows.
    config.network.bind_host = "127.252.0.1"
    config.network.metrics_port_base = 32200
    config.network.control_port = 32300
    config.network.instance_port_start = 32400
    config.network.instance_port_end = 32999
    if count == 100:
        config.network.worker_ip_cidr = "127.250.0.0/24"
    config.validate()
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: len(control_plane.statuses) == count, timeout=15)
        await eventually(lambda: len(control_plane.watchers) == count * 2, timeout=15)
        assert len({w.identity["token"] for w in fleet.workers}) == count
        assert len({w.identity["worker_uuid"] for w in fleet.workers}) == count
        if count == 100:
            assert len({w["ip"] for w in control_plane.statuses.values()}) == 100
            assert all(w["ip"] == w["advertise_address"] for w in control_plane.statuses.values())
        for key in range(1, count + 1):
            await control_plane.assign(worker_id=key, key=key)
        await eventually(
            lambda: sum(len(w.runtime.running_ids) for w in fleet.workers) == count, timeout=15
        )
        assert len(fleet.ports) == count
        if count == 100:
            assert len({port for _, port in fleet.ports}) == 1
        # Scale models to zero: a dropped watch deletion must also be reaped by snapshots.
        control_plane.instances.clear()
        await eventually(lambda: not fleet.ports)
        assert all(not w.runtime.records for w in fleet.workers)
        assert all(w.hardware.allocated(w.worker_id, w.instances) == (0, {}) for w in fleet.workers)
        async with aiohttp.ClientSession() as session:
            async with session.post("http://127.0.0.1:32300/scale", json={"count": 10}) as response:
                assert response.status == 200
            await eventually(lambda: sum(w.online for w in fleet.workers) == 10)
            async with session.get(f"http://127.252.0.1:{32100 + count - 1}/healthz") as response:
                assert response.status == 503
            async with session.post(
                "http://127.0.0.1:32300/scale", json={"count": count}
            ) as response:
                assert response.status == 200
        assert sum(w.online for w in fleet.workers) == count
    finally:
        fleet.stop.set()
        await task


@pytest.mark.asyncio
async def test_registration_backpressure(config, control_plane, monkeypatch):
    config.worker_groups = [WorkerGroup(count=8)]
    config.network.api_port_base = 33100
    config.network.metrics_port_base = 33200
    config.network.control_port = 33300
    config.simulation.registration_concurrency = 2
    config.simulation.registration_rate = 1000
    config.validate()
    control_plane.registration_delay = 0.05
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: len(control_plane.statuses) == 8)
        assert control_plane.max_registrations_inflight == 2
    finally:
        fleet.stop.set()
        await task
