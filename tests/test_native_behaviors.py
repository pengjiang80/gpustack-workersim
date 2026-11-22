"""Native Worker behaviours that generate Server load or drive recovery."""

import asyncio
import logging

import aiohttp
import pytest
from conftest import eventually

from workersim.config import WorkerGroup
from workersim.fleet import Fleet
from workersim.hardware import NVIDIA_CLIQUE_KEY


def cleanup_reads(plane):
    return [
        r for r in plane.request_times
        if r[1] == "GET" and r[2] == "/v2/model-instances" and r[3].get("page") == "-1"
    ]


@pytest.mark.asyncio
async def test_orphan_cleanup_reads_full_instance_list_only_with_workloads(
    config, control_plane, monkeypatch
):
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    monkeypatch.setattr("workersim.worker.ORPHAN_CLEANUP_INITIAL_DELAY", 0)
    monkeypatch.setattr("workersim.worker.ORPHAN_CLEANUP_INTERVAL", 0.1)
    config.simulation.reconcile_interval = 0
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: fleet.workers and len(control_plane.watchers) == 6)
        await asyncio.sleep(0.3)
        assert cleanup_reads(control_plane) == []  # native list_workloads() is empty
        await control_plane.assign()
        await eventually(lambda: 10 in fleet.workers[0].runtime.running_ids)
        await eventually(lambda: len(cleanup_reads(control_plane)) >= 2)
        assert fleet.workers[0].orphan_cleanups >= 2
    finally:
        fleet.stop.set()
        await task


@pytest.mark.asyncio
async def test_worker_restarts_error_instance_with_native_backoff(
    config, control_plane, monkeypatch
):
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    monkeypatch.setattr("workersim.worker.ERROR_RESTART_LOOP_SECONDS", 0.05)
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: fleet.workers and len(control_plane.watchers) == 6)
        await control_plane.assign()
        worker = fleet.workers[0]
        await eventually(lambda: 10 in worker.runtime.running_ids)

        async def fail():
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{config.network.control_port}/fault",
                    json={"worker": worker.spec.name, "instance_id": 10,
                          "action": "fail-instance"},
                ) as response:
                    assert response.status == 200

        await fail()
        # First restart is immediate: restart_count + 1, back to SCHEDULED, then running again.
        await eventually(lambda: control_plane.instances[10]["restart_count"] == 1)
        assert control_plane.instances[10]["last_restart_time"]
        await eventually(lambda: 10 in worker.runtime.running_ids)
        assert worker.restarts_requested == 1
        # Serving again clears the backoff, as the native health sync does.
        assert worker.restart_backoff.get(10) is None
        worker.restart_backoff[10] = 1  # simulate a crash loop: next delay is 10 s
        await fail()
        await eventually(lambda: control_plane.instances[10]["state"] == "error")
        await asyncio.sleep(0.4)
        assert control_plane.instances[10]["restart_count"] == 1
        assert 10 in worker.error_instances
        # Models with restart_on_error disabled are left in ERROR.
        control_plane.models[1]["restart_on_error"] = False
        worker.watch_caches["models"][1] = control_plane.models[1]
        worker.restart_backoff.pop(10)
        await eventually(lambda: 10 not in worker.error_instances)
        assert control_plane.instances[10]["restart_count"] == 1
    finally:
        fleet.stop.set()
        await task


@pytest.mark.asyncio
async def test_version_mismatch_only_warns(config, control_plane, monkeypatch, caplog):
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    control_plane.server_version = "v9.9.9"
    caplog.set_level(logging.WARNING, logger="workersim.worker")
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: fleet.workers and fleet.workers[0].client)
        assert "Version mismatch detected" in caplog.text
        # Native workers register the release build string, which carries the tag's "v".
        assert control_plane.workers[1]["worker_version"] == "v2.3.1"
    finally:
        fleet.stop.set()
        await task


@pytest.mark.asyncio
async def test_status_reports_topology_npu_network_and_kv_ifname(
    config, control_plane, monkeypatch
):
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    config.worker_groups = [
        WorkerGroup(name_prefix="nvl", count=3, gpu_count=2, nvlink_domain_workers=2,
                    kv_transfer_ifname="ib0"),
        WorkerGroup(name_prefix="npu", count=1, gpu_count=2, gpu_profile="ascend-910b4-64gb",
                    reserved_vram_gib=1),
    ]
    config.network.api_port_base = 32500
    config.network.metrics_port_base = 32600
    config.validate()
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: len(control_plane.statuses) == 4)
        statuses = {
            control_plane.workers[k]["name"]: v["status"]
            for k, v in control_plane.statuses.items()
        }
        cliques = [statuses[f"nvl-00{i}"]["topology_facts"][NVIDIA_CLIQUE_KEY] for i in (1, 2, 3)]
        assert cliques[0] == cliques[1] != cliques[2] and cliques[0].endswith(".0")
        assert statuses["nvl-001"]["kv_transfer_ifname"] == "ib0"
        devices = statuses["nvl-001"]["gpu_devices"]
        assert all(d["topology_hints"] == {NVIDIA_CLIQUE_KEY: cliques[0]} for d in devices)
        npu = statuses["npu-001"]
        assert npu["topology_facts"] is None and npu["kv_transfer_ifname"] is None
        inets = [d["network"]["inet"] for d in npu["gpu_devices"]]
        assert all(d["network"]["status"] == "up" for d in npu["gpu_devices"])
        assert len(set(inets)) == 2 and all(i.startswith("10.200.") for i in inets)
        # Registration carries no GPU-derived facts, like the native initial collection.
        registered = next(w for w in control_plane.workers.values() if w["name"] == "nvl-001")
        assert registered["status"]["topology_facts"] is None or registered["status"][
            "gpu_devices"
        ]
    finally:
        fleet.stop.set()
        await task


def test_topology_config_validation(config):
    config.worker_groups = [WorkerGroup(gpu_profile="ascend-910b4-64gb", nvlink_domain_workers=2)]
    with pytest.raises(ValueError, match="NVIDIA"):
        config.validate()
    config.worker_groups = [WorkerGroup(nvlink_domain_workers=0)]
    with pytest.raises(ValueError, match="positive"):
        config.validate()
    config.worker_groups = [WorkerGroup()]
    config.network.npu_roce_cidr = "10.0.0.0/30"
    with pytest.raises(ValueError, match="npu_roce_cidr"):
        config.validate()
