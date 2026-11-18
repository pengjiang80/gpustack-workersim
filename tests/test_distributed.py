import asyncio
import copy

import aiohttp
import pytest
from conftest import eventually

from workersim.config import WorkerGroup
from workersim.fleet import Fleet
from workersim.model_metrics import render_models


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["run_first", "initialize_later", "delegated", "external", "hybrid", "sglang"]
)
async def test_native_local_members_and_rank_sources(config, control_plane, monkeypatch, mode):
    config.worker_groups = [WorkerGroup(count=2, gpu_count=2)]
    config.network.bind_host = "127.252.0.1"
    config.network.worker_ip = "127.252.0.1"
    config.network.api_port_base = 33400
    config.network.metrics_port_base = 33500
    config.network.control_port = 33600
    config.network.instance_port_start = 33700
    config.network.instance_port_end = 33763
    config.validate()
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    managed = mode in ("external", "hybrid", "sglang")
    model = {"backend": "vLLM", "backend_parameters": []}
    if mode in ("external", "hybrid"):
        model["backend_parameters"] = [f"--data-parallel-{mode}-lb", "--dp=2", "--tp=2"]
    if mode == "sglang":
        model = {"backend": "SGLang", "backend_parameters": ["--dp=2", "--tp=2"]}
    control_plane.models[1] = model
    instance = await control_plane.assign(managed=managed)
    instance["gpu_indexes"] = [0, 1]
    follower = {
        "worker_id": 2,
        "gpu_indexes": [0, 1],
        "state": "scheduled",
        "computed_resource_claim": {"ram": 1024**3, "vram": {"0": 1024**3, "1": 1024**3}},
    }
    instance["distributed_servers"].update(
        subordinate_workers=[follower],
        group_recovery=not managed,
        mode=mode if not managed else "delegated",
    )
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(
            lambda: (
                len(fleet.workers) == 2 and all(10 in w.runtime.running_ids for w in fleet.workers)
            )
        )
        assert instance["state"] == "running"
        assert all(w.report_errors == 0 for w in fleet.workers)
        if mode == "external":
            async with aiohttp.ClientSession() as client:
                inventories = []
                for index, node in enumerate(fleet.workers):
                    async with client.get(
                        f"http://127.252.0.1:{33400 + index}/port-reservations",
                        headers={"Authorization": "Bearer " + node.identity["token"]},
                    ) as response:
                        inventories.append(await response.json())
                assert len({i["port_range"] for i in inventories}) == 1
        follower_metrics = render_models(fleet.workers[1])
        if mode == "external":
            assert 'endpoint_dp_rank="1"' in follower_metrics
            assert 'source_kind="vllm_engine"' in follower_metrics
        elif mode == "hybrid":
            assert 'source_kind="vllm_hybrid"' in follower_metrics
        else:
            assert follower_metrics == ""  # no duplicated token counters on headless members
        for worker in fleet.workers:
            allocated = worker.hardware.allocated(worker.worker_id, worker.instances)
            assert allocated[0] == 1024**3
        # Native registry identity carries model_id and instance_id, plus a DP/Worker suffix.
        suffix = "-dp1" if mode == "external" else "-w2" if mode == "hybrid" else ""
        worker = fleet.workers[1] if suffix else fleet.workers[0]
        async with aiohttp.ClientSession(
            headers={"Authorization": "Bearer " + worker.identity["token"]}
        ) as client:
            async with client.get(
                f"http://127.252.0.1:{33400 + worker.spec.slot}/health",
                headers={"X-GPUStack-Model-Instance": f"model-1-10{suffix}.static"},
            ) as response:
                assert response.status == 200
        control_plane.instances.pop(10)
        await control_plane.emit("DELETED", {"id": 10})
        await eventually(lambda: all(not w.runtime.records for w in fleet.workers))
        assert not fleet.ports
        await eventually(lambda: not fleet.logical_ports)
    finally:
        fleet.stop.set()
        await task


@pytest.mark.asyncio
async def test_restored_legacy_endpoints_repair_duplicate_ports(config, control_plane, monkeypatch):
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    for key in (10, 11):
        instance = await control_plane.assign(key=key)
        instance.update(state="running", port=31000, ports=[31000])
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: fleet.workers and len(fleet.workers[0].runtime.running_ids) == 2)
        assert len({i["port"] for i in control_plane.instances.values()}) == 2
        assert not fleet.workers[0].report_errors
    finally:
        fleet.stop.set()
        await task


@pytest.mark.asyncio
async def test_injected_failure_retires_endpoint_and_metric_series(
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
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"http://127.0.0.1:{config.network.control_port}/fault",
                json={"worker": worker.spec.name, "instance_id": 10, "action": "fail-instance"},
            ) as response:
                assert response.status == 200
        await eventually(lambda: control_plane.instances[10]["state"] == "error")
        await eventually(lambda: not fleet.ports)
        assert "injected runtime failure" in control_plane.instances[10]["state_message"]
        assert render_models(worker) == ""
    finally:
        fleet.stop.set()
        await task


@pytest.mark.asyncio
async def test_pd_roles_and_file_progress_reconnect_and_delete(config, control_plane, monkeypatch):
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    config.simulation.download_seconds = 0.3
    control_plane.files[7] = {
        "id": 7,
        "worker_id": 1,
        "source": "huggingface",
        "huggingface_repo_id": "simulated/model",
        "state": "downloading",
        "download_progress": 0,
    }
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: fleet.workers and fleet.workers[0].file_watch_connected)
        worker = fleet.workers[0]
        for key, role in enumerate(("prefill", "decode", "router"), 10):
            instance = await control_plane.assign(key=key)
            instance["role"] = role
            instance["distributed_servers"]["group_recovery"] = True
            await control_plane.emit("UPDATED", instance)
        await eventually(lambda: len(worker.runtime.running_ids) == 3)
        await eventually(lambda: control_plane.files[7]["state"] == "ready")
        assert control_plane.files[7]["download_progress"] == 100
        assert control_plane.files[7]["resolved_paths"]
        text = render_models(worker)
        assert 'role="prefill"' in text and "gpustack:pd_kv_transfer_seconds_bucket" in text
        assert 'role="router"' in text and "gpustack:pd_router_requests_total" in text
        connections = worker.watch_connections["model-files"]
        await control_plane.emit("DISCONNECT", {}, "model-files")
        await eventually(lambda: worker.watch_connections["model-files"] > connections)
        # A deletion lost during disconnection is repaired by the full snapshot.
        control_plane.files.pop(7)
        await eventually(lambda: not worker.model_files.items)
        # Stop barrier must be confirmed with the original Server token.
        instance = control_plane.instances[10]
        instance["distributed_servers"]["legacy_stop"] = {"token": "frozen-stop"}
        await control_plane.emit("UPDATED", copy.deepcopy(instance))
        await eventually(
            lambda: instance["distributed_servers"]["legacy_stop"].get("confirmed_worker_ids")
        )
        assert 10 not in worker.runtime.running_ids
    finally:
        fleet.stop.set()
        await task


@pytest.mark.asyncio
async def test_late_model_file_creation_gates_startup(config, control_plane, monkeypatch):
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    instance = await control_plane.assign()
    instance.update(
        source="huggingface",
        huggingface_repo_id="sim/late-file",
        model_config_snapshot={"model": {"source": "huggingface"}},
    )
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: fleet.workers and fleet.workers[0].runtime.records)
        await asyncio.sleep(0.5)
        assert instance["state"] != "running"
        control_plane.files[8] = {
            "id": 8,
            "worker_id": 1,
            "source": "huggingface",
            "huggingface_repo_id": "sim/late-file",
            "state": "downloading",
        }
        await control_plane.emit("CREATED", control_plane.files[8], "model-files")
        await eventually(lambda: instance["state"] == "running")
        assert control_plane.files[8]["state"] == "ready"
        assert control_plane.files[8]["download_progress"] == 100
    finally:
        fleet.stop.set()
        await task


@pytest.mark.asyncio
async def test_native_worker_and_gateway_registration_credentials(
    config, control_plane, monkeypatch
):
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: fleet.workers and fleet.workers[0].client)
        await control_plane.assign()
        await eventually(lambda: 10 in fleet.workers[0].runtime.running_ids)
        async with aiohttp.ClientSession() as client:
            for auth, expected in (
                ({"Authorization": "Bearer registration-secret"}, 200),
                ({"X-API-Key": "registration-secret"}, 200),
                ({"Authorization": "Bearer wrong-client-token"}, 401),
            ):
                headers = auth | {"X-GPUStack-Model-Instance": "model-1-10.static"}
                async with client.get(
                    f"http://127.0.0.1:{config.network.api_port_base}/v1/models", headers=headers
                ) as response:
                    assert response.status == expected
        assert (
            fleet.store.workers[fleet.workers[0].spec.name]["registration_token"]
            == "registration-secret"
        )
    finally:
        fleet.stop.set()
        await task
