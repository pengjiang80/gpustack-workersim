import pytest

from workersim.config import MAX_WORKERS, load_config
from workersim.hardware import Hardware
from workersim.state import StateStore


def test_fifty_worker_configuration():
    config = load_config("configs/fleet-50.yaml")
    assert len(config.workers()) == 50
    assert sum(w.group.gpu_count for w in config.workers()) == 400


def test_hundred_b300_configuration():
    config = load_config("configs/fleet-100-b300.yaml")
    assert len(config.workers()) == 100
    assert sum(w.group.gpu_count for w in config.workers()) == 800
    devices = Hardware(config, config.workers()[0]).sample(1, {}, set())["gpu_devices"]
    assert len(devices) == 8
    assert all(d["name"] == "NVIDIA B300" for d in devices)
    assert all(d["memory"]["total"] == 288 * 1024**3 for d in devices)
    assert all(d["compute_capability"] == "10.3" for d in devices)


def test_thousand_b300_configuration_and_port_limits():
    config = load_config("configs/fleet-1000-b300.yaml")
    assert len(config.workers()) == 1000
    assert sum(w.group.gpu_count for w in config.workers()) == 8000
    assert config.network.instance_ports_for_slot(999) == (59980, 59999)
    config.network.metrics_port_base = 20200
    with pytest.raises(ValueError, match="overlap"):
        config.validate()
    config.network.metrics_port_base = 21100
    config.network.instance_port_end = 40063
    with pytest.raises(ValueError, match="65535"):
        config.validate()
    config.network.instance_port_end = 40019
    config.network.instance_port_stride = 10
    with pytest.raises(ValueError, match="overlap"):
        config.validate()


@pytest.mark.parametrize("change", ["duplicate_names", "overlap", "too_many", "zero_interval"])
def test_invalid_configuration(config, change):
    if change == "duplicate_names":
        config.worker_groups.append(config.worker_groups[0])
    elif change == "overlap":
        config.network.metrics_port_base = config.network.api_port_base
    elif change == "too_many":
        config.worker_groups[0].count = MAX_WORKERS + 1
    else:
        config.simulation.status_interval = 0
    with pytest.raises(ValueError):
        config.validate()


def test_exclusive_state_and_server_binding(tmp_path):
    first = StateStore(str(tmp_path), "fleet", "http://one")
    second = StateStore(str(tmp_path), "fleet", "http://one")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="Another process"):
            second.acquire()
        first.save("node", {"id": 1, "token": "secret", "worker_uuid": "uuid"})
    finally:
        first.close()
        second.close()
    second.acquire()
    assert second.workers["node"]["id"] == 1
    second.close()
    other = StateStore(str(tmp_path), "fleet", "http://other")
    try:
        with pytest.raises(ValueError, match="different Server"):
            other.acquire()
    finally:
        other.close()
