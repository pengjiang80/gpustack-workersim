"""GPUStack Add Worker style startup: argument mapping, presets and host setup."""

import asyncio

import pytest
from conftest import eventually

from workersim.fleet import Fleet
from workersim.quickstart import (
    HOST_PRESETS,
    build_config,
    describe,
    is_native_invocation,
    merge_ranges,
    parse,
    reserve_ports,
)

# Arguments after the image in GPUStack 2.3.1's NVIDIA Add Worker command.
NATIVE = ["--server-url", "http://10.0.0.5", "--worker-ip", "10.0.0.9",
          "--advertise-address", "10.0.0.8"]


def config_for(argv, environ=None):
    args, unknown = parse(argv, environ or {})
    return build_config(args, environ or {}), unknown


def test_native_arguments_map_to_a_one_worker_default_fleet():
    assert is_native_invocation(NATIVE)
    assert not is_native_invocation(["--config", "x.yaml", "run"])
    config, unknown = config_for(NATIVE + ["--some-future-flag", "1"], {"GPUSTACK_TOKEN": "t"})
    assert unknown == ["--some-future-flag", "1"]
    assert config.server.url == "http://10.0.0.5"
    assert config.server.registration_token_env == "GPUSTACK_TOKEN"
    assert (config.network.worker_ip, config.network.advertise_address) == (
        "10.0.0.9", "10.0.0.8"
    )
    [group] = config.worker_groups
    assert group.count == 1 and group.gpu_profile == "h100-80gb"
    assert group.gpu_count == HOST_PRESETS["h100-80gb"]["gpu_count"]
    assert config.simulation.data_dir.replace("\\", "/").endswith("/workersim")


def test_profile_preset_with_field_overrides_and_worker_name_prefix():
    config, _ = config_for(NATIVE + [
        "--workers", "20", "--profile", "b300-288gb", "--gpus-per-worker", "4",
        "--gpu-memory-gib", "144", "--worker-name", "lab", "--label", "rack=a",
    ])
    [group] = config.worker_groups
    assert (group.count, group.gpu_count, group.gpu_memory_gib) == (20, 4, 144)
    assert group.cpu_cores == HOST_PRESETS["b300-288gb"]["cpu_cores"]
    assert group.labels == {"rack": "a"}
    assert config.workers()[0].name == "lab-001"
    assert describe(config)["devices"] == 80


def test_environment_variables_and_mixed_groups():
    environ = {
        "WORKERSIM_WORKERS": "3", "WORKERSIM_PROFILE": "rtx4090-24gb",
        "WORKERSIM_GROUPS": "workers=2,profile=ascend-910b4-64gb;workers=1,gpus=2,prefix=x",
    }
    config, _ = config_for(NATIVE + ["--worker-name", "lab"], environ)
    first, second = config.worker_groups
    assert (first.count, first.gpu_profile, first.name_prefix) == (2, "ascend-910b4-64gb", "lab-g1")
    assert (second.count, second.gpu_profile, second.gpu_count) == (1, "rtx4090-24gb", 2)
    assert [w.name for w in config.workers()] == ["lab-g1-001", "lab-g1-002", "x-001"]
    environ = {"WORKERSIM_WORKERS": "3", "WORKERSIM_PROFILE": "rtx4090-24gb"}
    config, _ = config_for(NATIVE, environ)
    assert config.worker_groups[0].count == 3


@pytest.mark.parametrize("argv,match", [
    (["--profile", "mi300x"], "Unknown profile"),
    (["--group", "workers=1,colour=red"], "Invalid --group"),
    (["--workers", "1001"], "1..1000"),
    (["--label", "novalue"], "Invalid --label"),
])
def test_invalid_quickstart_arguments(argv, match):
    with pytest.raises(ValueError, match=match):
        config_for(NATIVE + argv)


def test_port_reservation_merges_and_widens_source_range(tmp_path):
    (tmp_path / "ip_local_reserved_ports").write_text("8080,40000-40010\n")
    (tmp_path / "ip_local_port_range").write_text("32768 60999\n")
    config, _ = config_for(NATIVE + ["--workers", "1000"])
    outcome = reserve_ports(config, proc=tmp_path)
    reserved = (tmp_path / "ip_local_reserved_ports").read_text().strip()
    assert reserved == merge_ranges("8080", [(20099, 22099), (40000, 59999)])
    assert "widened" in outcome
    assert (tmp_path / "ip_local_port_range").read_text().split() == ["10240", "65535"]
    # Small fleets keep the host's source range.
    (tmp_path / "ip_local_port_range").write_text("32768 60999\n")
    config, _ = config_for(NATIVE + ["--workers", "2"])
    assert "widened" not in reserve_ports(config, proc=tmp_path)
    assert "not changed" in reserve_ports(config, proc=tmp_path / "missing")


@pytest.mark.asyncio
async def test_native_command_registers_fleet(config, control_plane, monkeypatch, tmp_path):
    monkeypatch.setenv("GPUSTACK_TOKEN", "registration-secret")
    args, _ = parse([
        "--server-url", config.server.url, "--worker-ip", "127.0.0.1", "--workers", "2",
        "--profile", "ascend-910b4-64gb", "--gpus-per-worker", "2", "--worker-name", "quick",
        "--data-dir", str(tmp_path), "--worker-port", str(config.network.api_port_base),
    ], {})
    quick = build_config(args, {"GPUSTACK_TOKEN": "registration-secret"})
    quick.network.api_port_base = 32700
    quick.network.metrics_port_base = 32800
    quick.network.control_port = config.network.control_port
    quick.network.instance_port_start, quick.network.instance_port_end = 31000, 31019
    quick.validate()
    fleet = Fleet(quick)
    task = asyncio.create_task(fleet.run())
    try:
        await eventually(lambda: len(control_plane.statuses) == 2)
        names = sorted(w["name"] for w in control_plane.workers.values())
        assert names == ["quick-001", "quick-002"]
        status = next(iter(control_plane.statuses.values()))["status"]
        assert [d["name"] for d in status["gpu_devices"]] == ["Ascend 910B4"] * 2
    finally:
        fleet.stop.set()
        await task
