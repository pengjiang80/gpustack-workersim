"""Wire-level checks against the pinned native Worker communication contract."""

import asyncio
from dataclasses import fields

import pytest
from conftest import eventually

from workersim import native_contract
from workersim.config import Config, ServerConfig
from workersim.fleet import Fleet


def test_native_defaults_are_not_changed_to_make_scale_tests_pass():
    config = Config()
    assert config.simulation.heartbeat_interval == native_contract.HEARTBEAT_INTERVAL == 30
    assert config.simulation.status_interval == native_contract.STATUS_INTERVAL == 30
    assert config.simulation.model_state_interval == native_contract.STATE_SYNC_INTERVAL == 3
    assert config.simulation.reconcile_interval == 0
    assert config.server.request_timeout == 60
    assert native_contract.WATCH_RECONNECT_SECONDS == 5
    assert (native_contract.REGISTER_ATTEMPTS, native_contract.REGISTER_RETRY_SECONDS) == (10, 3)
    assert len(native_contract.WATCH_RESOURCES) == 6
    assert "request_concurrency" not in {f.name for f in fields(ServerConfig)}


@pytest.mark.asyncio
async def test_native_idle_request_mix(config, control_plane, monkeypatch):
    config.simulation.reconcile_interval = 0
    config.simulation.heartbeat_interval = 30
    config.simulation.status_interval = 30
    monkeypatch.setattr("workersim.worker.RUNTIME_METRICS_INITIAL_DELAY", 0.02)
    monkeypatch.setattr("workersim.worker.RUNTIME_METRICS_INTERVAL", 0.05)
    monkeypatch.setenv("GPUSTACK_REGISTRATION_TOKEN", "registration-secret")
    fleet = Fleet(config)
    started = asyncio.get_running_loop().time()
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: len(control_plane.watchers) == 6)
        await eventually(lambda: control_plane.heartbeats.get(1, 0) >= 1)
        assert control_plane.version_requests == 1
        assert control_plane.workers[1]["status"]["gpu_devices"] is None
        for path in ("/v2/worker-heartbeat", "/v2/worker-status"):
            calls = [r for r in control_plane.request_times if r[2] == path]
            assert calls[0][-1] - started < 0.5
        watches = {
            path.removeprefix("/v2/"): query
            for _, _, path, query, _ in control_plane.request_times if query.get("watch") == "true"
        }
        assert set(watches) == set(native_contract.WATCH_RESOURCES)
        assert all(query == {"watch": "true"} for query in watches.values())
        assert any(r[2:] == ("/v2/workers", {"me": "true"}, r[-1])
                   for r in control_plane.request_times)
        assert any(r[2] == "/v2/cache-providers" and r[3] == {"page": "0"}
                   for r in control_plane.request_times)
        mark = len(control_plane.request_times)
        await asyncio.sleep(0.25)
        recent = control_plane.request_times[mark:]
        assert sum(r[2] == "/v2/workers/1" for r in recent) >= 2
        assert not any(r[2] in ("/v2/model-instances", "/v2/model-files", "/v2/models")
                       for r in recent)
        assert sum(r[2] == "/v2/metrics/config" for r in control_plane.request_times) == 1
    finally:
        fleet.stop.set()
        await task
