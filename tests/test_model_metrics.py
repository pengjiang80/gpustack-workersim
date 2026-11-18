import re
from types import SimpleNamespace

import pytest

from workersim.model_metrics import SyntheticTraffic, render_models
from workersim.runtime import Record


def samples(text):
    return {
        (name, labels): float(value)
        for name, labels, value in re.findall(r"^([^#\s{]+)\{(.*)\} ([^\n]+)$", text, re.M)
    }


def test_traffic_counters_and_histograms_are_cumulative():
    traffic = SyntheticTraffic(42, 2, started=0)
    old = samples(traffic.render({"workersim": "true"}, now=60))
    new = samples(traffic.render({"workersim": "true"}, now=180))
    for key, value in old.items():
        if key[0].endswith(("_total", "_count", "_sum", "_bucket")):
            assert new[key] >= value
    count = new[("gpustack:request_success_total", 'workersim="true"')]
    for name in ("time_to_first_token_seconds", "request_prompt_tokens"):
        family = "gpustack:" + name
        assert new[(family + "_count", 'workersim="true"')] == count
        assert new[(family + "_bucket", 'workersim="true",le="+Inf"')] == count
        buckets = [value for (key, _), value in new.items() if key == family + "_bucket"]
        assert buckets == sorted(buckets)
    assert (
        new[("gpustack:request_prompt_tokens_sum", 'workersim="true"')]
        == new[("gpustack:prompt_tokens_total", 'workersim="true"')]
    )
    assert traffic.values(60)[1] != traffic.values(180)[1]
    assert SyntheticTraffic(42, 0, started=0).values(180)[2]["request_success"] == 0


def test_model_identity_family_deduplication_and_stop_cleanup():
    records = {}
    for key in (10, 11):
        records[key] = Record(
            {"id": key, "name": f"model-{key}", "model_id": 1, "model_name": "test"},
            generation=2,
            phase="running",
            traffic=SyntheticTraffic(key, 2, started=0),
        )
    worker = SimpleNamespace(
        worker_id=7, spec=SimpleNamespace(name="node-007"), runtime=SimpleNamespace(records=records)
    )
    text = render_models(worker)
    assert 'model_instance_id="10"' in text and 'model_instance_id="11"' in text
    assert 'restart_count="2"' in text and 'source_kind="legacy"' in text
    types = [line for line in text.splitlines() if line.startswith("# TYPE")]
    assert len(types) == len(set(types))
    raw = render_models(worker, raw=True)
    assert "vllm:generation_tokens_total" in raw
    assert "vllm:request_time_per_output_token_seconds_bucket" in raw
    records[10].phase = "stopped"
    del records[11]
    assert render_models(worker) == ""


@pytest.mark.parametrize("slot,expected", [(0, "10.250.0.1"), (99, "10.250.0.100")])
def test_worker_cidr_maps_to_real_node_address(config, slot, expected):
    from workersim.config import WorkerGroup

    config.worker_groups = [WorkerGroup(count=100)]
    config.network.worker_ip_cidr = "10.250.0.0/24"
    config.network.api_port_base = 32100
    config.network.metrics_port_base = 32200
    config.network.control_port = 32300
    config.validate()
    assert config.network.ip_for_slot(slot) == expected


@pytest.mark.parametrize("cidr", ["10.0.0.0/32", "8.8.8.0/24", "2001:db8::/64"])
def test_invalid_node_subnets(config, cidr):
    config.network.worker_ip_cidr = cidr
    with pytest.raises(ValueError):
        config.validate()


def test_virtual_network_refuses_overlapping_routes():
    from scripts.configure_virtual_network import plan

    routes = [{"dst": "172.17.0.0/16", "dev": "docker0"}]
    addresses = plan("10.250.0.0/24", 100, routes, "workersim0")
    assert addresses[0] == "10.250.0.1" and addresses[-1] == "10.250.0.100"
    with pytest.raises(ValueError, match="overlaps"):
        plan("172.17.1.0/24", 100, routes, "workersim0")
    with pytest.raises(ValueError, match="RFC1918"):
        plan("8.8.8.0/24", 100, routes, "workersim0")


def test_default_shared_ip_port_blocks_and_unique_ip_reuse():
    from workersim.config import NetworkConfig

    network = NetworkConfig()
    assert network.instance_ports_for_slot(0) == (40000, 40063)
    assert network.instance_ports_for_slot(1) == (40064, 40127)
    assert network.instance_ports_for_slot(99) == (46336, 46399)
    network.worker_ip_cidr = "10.250.0.0/24"
    assert network.instance_ports_for_slot(99) == (40000, 40063)


def test_smg_metrics_include_current_component_attempt():
    from workersim.model_metrics import metric_sources

    worker = SimpleNamespace(worker_id=7)
    record = Record(
        {"id": 10, "worker_id": 7},
        generation=0,
        workloads=[
            {
                "name": "sim-smg",
                "state": "running",
                "component": {"kind": "sglang_smg", "api_port": 40000, "attempt": 3},
            }
        ],
    )
    sources = metric_sources(worker, record)
    assert sources[0]["component_attempt"] == "3"
    assert sources[0]["source_kind"] == "sglang_smg"
