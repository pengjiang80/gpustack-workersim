"""Native runtime-metrics contract: engine expositions, normalization and Server traffic."""

import asyncio
import re
from types import SimpleNamespace

import pytest

from workersim.metrics import render_raw
from workersim.model_metrics import SyntheticTraffic, engine_families, render_families
from workersim.runtime import Record
from workersim.runtime_metrics import (
    RuntimeMetricsAggregator,
    builtin_metrics_config,
    in_range,
    metrics_sources,
    should_skip,
    unified_name,
)


def samples(text):
    return {
        (name, labels): float(value)
        for name, labels, value in re.findall(r"^([^#\s{]+)\{(.*)\} ([^\n]+)$", text, re.M)
    }


def test_engine_counters_and_histograms_are_cumulative():
    traffic = SyntheticTraffic(42, 2, started=0)
    labels = {"model_name": "m", "engine": "0"}
    old = samples(render_families(engine_families(traffic, "vLLM", labels, now=60)))
    new = samples(render_families(engine_families(traffic, "vLLM", labels, now=180)))
    for key, value in old.items():
        if key[0].endswith(("_total", "_count", "_sum", "_bucket")):
            assert new[key] >= value
    count = new[("vllm:request_success_total", 'model_name="m",engine="0"')]
    assert new[("vllm:time_to_first_token_seconds_count", 'model_name="m",engine="0"')] == count
    assert SyntheticTraffic(42, 0, started=0).values(180)[2]["request_success"] == 0


@pytest.mark.parametrize(
    "runtime,expected",
    [
        ("vLLM", {"vllm:kv_cache_usage_perc", "vllm:num_preemptions"}),
        ("SGLang", {"sglang:num_running_reqs", "sglang:token_usage", "sglang:cached_tokens"}),
        ("MindIE", {"npu_cache_usage_perc", "num_requests_running", "request_success"}),
    ],
)
def test_engines_expose_their_native_names(runtime, expected):
    names = {f.name for f in engine_families(SyntheticTraffic(1, 2, started=0), runtime, {})}
    assert expected <= names
    # Each native name must be one the built-in config can normalize.
    config = builtin_metrics_config()
    for name in names:
        assert unified_name(config, name, runtime, None), name


def test_version_selectors_and_mapping_scale():
    assert in_range("0.10.2", "<0.11.0") and not in_range("0.21.0", "<0.11.1")
    assert in_range("0.4.4", "<=0.4.4") and in_range("0.5.15.post1", ">=0.5.8")
    config = builtin_metrics_config()
    assert unified_name(config, "sglang:kv_transfer_bootstrap_ms", "SGLang", None) == (
        "gpustack:pd_kv_transfer_seconds", 0.001,
    )
    assert unified_name(config, "vllm:time_per_output_token_seconds", "vLLM", "0.10.0") == (
        "gpustack:time_per_output_token_seconds", 1.0,
    )


def test_native_skip_rules():
    config = builtin_metrics_config()
    assert not should_skip({"backend": "vLLM"}, config)
    assert should_skip(None, config)
    assert should_skip({"backend": "Custom"}, config)
    assert should_skip({"backend": "VoxBox"}, config)
    assert should_skip({"backend": "vLLM", "categories": ["image"]}, config)
    assert should_skip({"backend": "vLLM", "backend_parameters": ["--disable-log-stats"]}, config)
    assert should_skip({"backend": "vLLM", "env": {"GPUSTACK_DISABLE_METRICS": "1"}}, config)


def test_sources_follow_native_readiness():
    legacy = {"id": 1, "model_id": 1, "worker_id": 7, "state": "running", "ports": [40000],
              "port": 40000, "named_ports": {"prometheus": {"base": 40005}}}
    [source] = metrics_sources(legacy)
    assert (source.kind, source.name, source.metrics_port) == ("legacy", "legacy", 40005)
    assert metrics_sources(legacy | {"state": "starting"}) == []
    assert metrics_sources(legacy | {"draining_since": "now"}) == []
    managed = {"id": 2, "model_id": 1, "state": "running", "distributed_servers": {"workloads": [{
        "name": "w0", "worker_id": 7, "state": "running", "dp_rank_placements": [
            {"dp_rank": 0, "node_rank": 0, "api_port": 40010, "ready": True, "gpu_indexes": [1]},
            {"dp_rank": 1, "node_rank": 0, "api_port": 40011, "ready": False, "gpu_indexes": [2]},
        ]}]}}
    [rank] = metrics_sources(managed)
    assert (rank.kind, rank.name, rank.dp_rank) == ("vllm_engine", "w0/main-0", 0)


class FakeClient:
    def __init__(self, config=None, row=None):
        self.calls = []
        self.config = config
        self.row = row or {"id": 7, "name": "node-7", "cluster_id": 1, "me": True}
        self.instances = {}

    async def request(self, method, path, *, body=None, params=None, **_):
        self.calls.append((method, path))
        await asyncio.sleep(0)
        if path == "/metrics/config":
            return self.config or {}
        if path == "/workers/7":
            return self.row
        if path.startswith("/model-instances/"):
            key = int(path.rsplit("/", 1)[1])
            if method == "PUT":
                self.instances[key] = body
            return self.instances[key]
        raise AssertionError(path)


def fake_worker(model, instance, client, online=True):
    record = Record(instance, generation=0, phase="running",
                    traffic=SyntheticTraffic(instance["id"], 2, started=0))
    worker = SimpleNamespace(
        worker_id=7, ip="10.0.0.7", online=online, client=client,
        spec=SimpleNamespace(name="node-7"),
        watch_connected=True, watch_states={"models": True},
        watch_caches={"model-instances": {instance["id"]: instance}, "models": {1: model}},
        runtime=SimpleNamespace(records={instance["id"]: record}),
    )
    record.worker = worker
    record.model = model
    client.instances[instance["id"]] = dict(instance)
    worker.runtime_metrics = RuntimeMetricsAggregator(worker)
    return worker


def instance_row(**fields):
    return {"id": 10, "name": "m-10", "model_id": 1, "model_name": "m", "worker_id": 7,
            "cluster_id": 1, "state": "running", "port": 40000, "ports": [40000],
            "restart_count": 0, "distributed_servers": {"workloads": None}} | fields


@pytest.mark.asyncio
async def test_aggregation_traffic_labels_and_version_write():
    client = FakeClient()
    worker = fake_worker({"id": 1, "name": "m", "backend": "vLLM"}, instance_row(), client)
    await worker.runtime_metrics.aggregate()
    # config (falls back to built-in), two uncached Worker reads, one fenced version write.
    assert client.calls == [
        ("GET", "/metrics/config"), ("GET", "/workers/7"),
        ("GET", "/model-instances/10"), ("PUT", "/model-instances/10"), ("GET", "/workers/7"),
    ]
    assert client.instances[10]["api_detected_backend_version"] == "0.21.0"
    worker.watch_caches["model-instances"][10] = client.instances[10]
    client.calls.clear()
    await worker.runtime_metrics.aggregate()
    assert client.calls == [("GET", "/workers/7"), ("GET", "/workers/7")]
    unified = render_families(worker.runtime_metrics.unified.fresh())
    [line] = [x for x in unified.splitlines() if x.startswith("gpustack:prompt_tokens_total{")]
    for pair in (
        'worker_id="7"', 'worker_name="node-7"', 'model_id="1"', 'model_name="m"',
        'model_instance_id="10"', 'model_instance_name="m-10"', 'runtime="vLLM"', 'role=""',
        'restart_count="0"', 'source_kind="legacy"', 'source_name="legacy"',
        'workload_name=""', 'engine="0"',
    ):
        assert pair in line, pair
    assert 'gpustack:runtime_metrics_scrape_status{' in unified and 'status="success"' in unified
    assert "exposition_scope" not in unified  # native adds it only to managed sources
    raw = render_raw(worker)
    assert "vllm:prompt_tokens_total{" in raw and "gpustack:prompt_tokens" not in raw


@pytest.mark.asyncio
async def test_server_metrics_config_drives_normalization():
    config = {
        "gpustack_metrics": {"gpustack:custom_running": {"type": "Gauge", "description": "x"}},
        "runtime_mapping": {
            "vLLM": {"*": {"vllm:num_requests_running": "gpustack:custom_running"}}
        },
    }
    row = instance_row(api_detected_backend_version="0.21.0")
    worker = fake_worker({"id": 1, "name": "m", "backend": "vLLM"}, row, FakeClient(config))
    await worker.runtime_metrics.aggregate()
    names = {f.name for f in worker.runtime_metrics.unified.fresh()}
    assert names == {"gpustack:custom_running", "gpustack:runtime_metrics_scrape_status"}


@pytest.mark.asyncio
async def test_sglang_pd_units_are_scaled_and_offline_scrape_reports_error():
    row = instance_row(role="decode", api_detected_backend_version="0.5.15")
    model = {"id": 1, "name": "m", "backend": "SGLang"}
    worker = fake_worker(model, row, FakeClient())
    await worker.runtime_metrics.aggregate()
    unified = render_families(worker.runtime_metrics.unified.fresh())
    assert re.search(r'gpustack:pd_kv_transfer_seconds_bucket\{[^}]*le="0\.001"', unified)
    assert "gpustack:pd_decode_transfer_queue_requests{" in unified
    worker.online = False
    await worker.runtime_metrics.aggregate()
    unified = render_families(worker.runtime_metrics.unified.fresh())
    assert 'status="error"' in unified and "gpustack:prompt_tokens" not in unified


@pytest.mark.asyncio
async def test_exporter_has_native_headers_and_no_models_after_ttl(monkeypatch):
    row = instance_row(api_detected_backend_version="0.21.0")
    worker = fake_worker({"id": 1, "name": "m", "backend": "vLLM"}, row, FakeClient())
    worker.config = None
    await worker.runtime_metrics.aggregate()
    assert worker.runtime_metrics.unified.fresh()
    monkeypatch.setattr("workersim.runtime_metrics.time.monotonic", lambda: 10**9)
    assert worker.runtime_metrics.unified.fresh() == []
