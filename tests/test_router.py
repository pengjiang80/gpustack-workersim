import asyncio
from urllib.parse import quote

import aiohttp
import pytest
from conftest import eventually

from workersim.fleet import Fleet


@pytest.mark.asyncio
async def test_native_router_registry_readback_fencing_and_chat(config, control_plane, monkeypatch):
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    fleet = Fleet(config)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: fleet.workers and fleet.workers[0].watch_connected)
        worker = fleet.workers[0]
        for key, role in enumerate(("prefill", "decode", "router"), 10):
            instance = await control_plane.assign(key=key)
            instance.update(role=role, group_id="one-generation")
            await control_plane.emit("UPDATED", instance)
        await eventually(lambda: len(worker.runtime.running_ids) == 3)
        records = worker.runtime.records
        base = f"http://127.0.0.1:{records[12].port}"
        async with aiohttp.ClientSession() as client:
            async with client.get(base + "/workers") as response:
                assert response.status == 200
                assert (await response.json())["workers"] == []
            async with client.post(base + "/v1/chat/completions", json={}) as response:
                assert response.status == 503
            urls = []
            for key, role in ((10, "prefill"), (11, "decode")):
                url = f"http://127.0.0.1:{records[key].port}"
                urls.append(url)
                async with client.post(
                    base + "/workers", json={"url": url, "worker_type": role}
                ) as response:
                    assert response.status == 200
            async with client.get(base + "/workers") as response:
                rows = (await response.json())["workers"]
                assert {row["url"] for row in rows} == set(urls)
                assert {row["worker_type"] for row in rows} == {"prefill", "decode"}
            async with client.post(base + "/v1/chat/completions", json={}) as response:
                body = await response.json()
                assert response.status == 200 and body["simulated"]
                assert isinstance(body["created"], int)
            # Unallocated and cross-group URLs may not be invented by the simulator.
            async with client.post(
                base + "/workers",
                json={"url": "http://outside.invalid:9", "worker_type": "prefill"},
            ) as response:
                assert response.status == 409
            # A fresh engine at the same URL cannot use a previous epoch's registration.
            records[10].generation += 1
            async with client.post(base + "/v1/chat/completions", json={}) as response:
                assert response.status == 503
            async with client.delete(base + "/workers/" + quote(urls[0], safe="")) as response:
                assert response.status == 200
            async with client.get(base + "/workers") as response:
                assert len((await response.json())["workers"]) == 1
    finally:
        fleet.stop.set()
        await task
