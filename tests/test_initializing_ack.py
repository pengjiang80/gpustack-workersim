import asyncio
import copy

import pytest
from conftest import eventually

from workersim.client import Client
from workersim.fleet import Fleet


@pytest.mark.asyncio
async def test_atomic_start_and_unacknowledged_initializing_retry(
    config, control_plane, monkeypatch
):
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    original = Client.request
    writes = []
    ignored = 0

    async def request(self, method, path, body=None, **kwargs):
        nonlocal ignored
        if method == "PUT" and path == "/model-instances/10":
            writes.append(copy.deepcopy(body))
            if body.get("state") == "initializing" and ignored < 2:
                ignored += 1
                body = body | {"state": "scheduled"}
        return await original(self, method, path, body=body, **kwargs)

    monkeypatch.setattr(Client, "request", request)
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: fleet.workers and fleet.workers[0].watch_connected)
        instance = await control_plane.assign()
        instance.update(source="huggingface", huggingface_repo_id="simulated/model")
        await control_plane.emit("UPDATED", instance)
        await eventually(lambda: instance["state"] == "initializing")
        assert writes[0]["state"] == "initializing"
        assert writes[0]["port"] and writes[0]["pid"]
        assert ignored == 2 and len(writes) >= 3
        assert not fleet.workers[0].runtime.running_ids
        control_plane.files[7] = {
            "id": 7,
            "worker_id": 1,
            "source": "huggingface",
            "huggingface_repo_id": "simulated/model",
            "state": "downloading",
            "download_progress": 0,
        }
        await control_plane.emit("CREATED", control_plane.files[7], "model-files")
        await eventually(lambda: 10 in fleet.workers[0].runtime.running_ids)
        assert control_plane.files[7]["state"] == "ready"
        # A healthy runtime continues to reconcile an older control-plane state.
        instance["state"] = "scheduled"
        await control_plane.emit("UPDATED", instance)
        await eventually(lambda: instance["state"] == "running")
        # A late Server ModelFile handler can replay an intermediate progress value.
        instance["download_progress"] = 25
        await control_plane.emit("UPDATED", instance)
        await eventually(lambda: instance["download_progress"] == 100)
        assert fleet.workers[0].report_errors == 0
    finally:
        fleet.stop.set()
        await task
