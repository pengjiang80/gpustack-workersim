"""1000 real listener pairs and 2000 authenticated watches against the protocol fixture."""

import asyncio
import json
import time

import aiohttp
import pytest
from conftest import eventually

from workersim.config import NetworkConfig, WorkerGroup
from workersim.fleet import Fleet


@pytest.mark.asyncio
async def test_thousand_workers_shared_ip(config, control_plane, monkeypatch):
    config.worker_groups = [WorkerGroup(count=1000, gpu_profile="b300-288gb", gpu_count=8)]
    config.network = NetworkConfig(
        bind_host="127.252.0.1",
        advertise_address="127.252.0.1",
        worker_ip="127.252.0.1",
    )
    config.simulation.registration_rate = 100
    config.simulation.heartbeat_interval = 5
    config.simulation.status_interval = 5
    config.simulation.reconcile_interval = 5
    config.validate()
    control_plane.watch_heartbeat_interval = 5
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    started = time.monotonic()
    try:
        await eventually(lambda: len(control_plane.statuses) == 1000, timeout=120)
        await eventually(lambda: len(control_plane.watchers) == 2000, timeout=30)
        assert len({w.identity["token"] for w in fleet.workers}) == 1000
        assert len({w.identity["worker_uuid"] for w in fleet.workers}) == 1000
        assert sum(len(s["status"]["gpu_devices"]) for s in control_plane.statuses.values()) == 8000
        persisted = json.loads(fleet.store.path.read_text())
        assert len(persisted["workers"]) == 1000
        await eventually(lambda: len(control_plane.heartbeats) == 1000, timeout=30)
        # Exercise both ends of the port map, using each Worker's own authenticated reports.
        edges = [fleet.workers[0], fleet.workers[-1]]
        for key, worker in enumerate(edges, 1):
            await control_plane.assign(worker_id=worker.worker_id, key=key)
        await eventually(lambda: all(w.runtime.running_ids for w in edges), timeout=20)
        async with aiohttp.ClientSession() as session:
            for key, worker in enumerate(edges, 1):
                instance = control_plane.instances[key]
                low, high = config.network.instance_ports_for_slot(worker.spec.slot)
                assert low <= instance["port"] <= high
                async with session.get(f"http://{worker.ip}:{instance['port']}/health") as response:
                    assert response.status == 200
                assert worker.hardware.allocated(worker.worker_id, worker.instances)[1]
            async with session.post("http://127.0.0.1:20099/scale", json={"count": 10}) as response:
                assert response.status == 200
            async with session.get("http://127.252.0.1:21099/healthz") as response:
                assert response.status == 503
            async with session.post(
                "http://127.0.0.1:20099/scale", json={"count": 1000}
            ) as response:
                assert response.status == 200
        control_plane.instances.clear()
        await eventually(lambda: not fleet.ports, timeout=20)
        assert sum(w.report_errors for w in fleet.workers) == 0
        print(f"1000 Workers / 2000 watches validated in {time.monotonic() - started:.2f}s")
    finally:
        fleet.stop.set()
        await task
