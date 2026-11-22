"""Run with the unmodified GPUStack 2.3.1 image to validate simulator wire contracts."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gpustack.schemas.models import ModelInstanceMemberReport  # noqa: E402
from gpustack.schemas.workers import WorkerCreate  # noqa: E402
from gpustack.utils.metrics import get_builtin_metrics_config  # noqa: E402
from gpustack.worker.runtime_metrics_aggregator import (  # noqa: E402
    RuntimeMetricsAggregator as NativeAggregator,
)
from gpustack.worker.runtime_metrics_aggregator import (  # noqa: E402
    get_unified_metric_family_name,
)
from prometheus_client.parser import text_string_to_metric_families  # noqa: E402

from workersim.config import Config, WorkerGroup  # noqa: E402
from workersim.metrics import render, render_raw  # noqa: E402
from workersim.model_metrics import (  # noqa: E402
    SyntheticTraffic,
    engine_families,
    render_families,
    router_families,
)
from workersim.runtime import Record  # noqa: E402
from workersim.runtime_metrics import (  # noqa: E402
    RuntimeMetricsAggregator,
    builtin_metrics_config,
    unified_name,
)
from workersim.worker import VirtualWorker  # noqa: E402


class FakeClient:
    async def request(self, method, path, **kwargs):
        if path == "/workers/1":
            return {"id": 1, "name": "sim-h100-001", "cluster_id": 1, "me": True}
        return {}


def check_metric_mapping():
    """Every synthetic engine family must normalize exactly as the native aggregator does."""
    ours, native = builtin_metrics_config(), get_builtin_metrics_config()
    assert ours == native, "Vendored metrics_config.yaml differs from the target release"
    checked = 0
    for runtime in ("vLLM", "SGLang", "MindIE"):
        for role in (None, "prefill", "decode"):
            traffic = SyntheticTraffic(1, 2, started=0)
            for family in engine_families(traffic, runtime, {}, role=role):
                expected = get_unified_metric_family_name(native, family.name, runtime, None)
                assert expected is not None, family.name
                assert unified_name(ours, family.name, runtime, None) == expected, family.name
                checked += 1
    return checked


def sample_set(families):
    return {
        (sample.name, tuple(sorted(sample.labels.items())), float(sample.value))
        for family in families
        for sample in family.samples
    }


def check_normalization():
    """Feed the same exposition to the native and simulated aggregators; outputs must match."""
    config = builtin_metrics_config()
    native = NativeAggregator(cache={}, worker_id_getter=lambda: 1)
    ours = RuntimeMetricsAggregator(SimpleNamespace())
    base = {
        "worker_id": "1", "worker_name": "w", "model_id": "1", "model_name": "m",
        "model_instance_id": "10", "model_instance_name": "m-10", "role": "",
        "restart_count": "0", "source_kind": "legacy", "source_name": "legacy",
        "workload_name": "",
    }
    peers = [{"worker_type": "prefill", "worker": "10.0.0.1:1"},
             {"worker_type": "decode", "worker": "10.0.0.2:2"}]
    compared = 0
    for runtime, version in (("vLLM", "0.21.0"), ("SGLang", "0.5.15"), ("MindIE", "2.3.0")):
        for role in (None, "prefill", "decode", "router"):
            traffic = SyntheticTraffic(3, 2, started=0)
            if role == "router":
                families = router_families(traffic, runtime, {}, peers, now=100)
            else:
                labels = {"model_name": "m"} | ({"engine": "0"} if runtime == "vLLM" else {})
                families = engine_families(traffic, runtime, labels, role=role, now=100)
            if not families:
                continue
            labels = base | {"runtime": runtime, "role": role or ""}
            parsed = {
                f.name: f for f in text_string_to_metric_families(render_families(families))
            }
            native_unified, native_raw = {}, {}
            native._process_endpoint_metrics(
                parsed, labels, runtime, version, native_unified, native_raw, config
            )
            unified, raw = {}, {}
            for family in families:
                ours.process_family(family, labels, runtime, version, config, unified, raw)
            expected = sample_set(native_unified.values())
            actual = sample_set(
                text_string_to_metric_families(render_families(unified.values()))
            )
            assert expected == actual, (
                runtime, role, sorted(expected - actual)[:3], sorted(actual - expected)[:3]
            )
            assert sample_set(native_raw.values()) == sample_set(
                text_string_to_metric_families(render_families(raw.values()))
            ), (runtime, role)
            compared += len(native_unified)
    return compared


def main():
    config = Config(worker_groups=[
        WorkerGroup(nvlink_domain_workers=2, kv_transfer_ifname="eth1"),
        WorkerGroup(name_prefix="sim-npu", count=1, gpu_profile="ascend-910b4-64gb"),
    ])
    fleet = SimpleNamespace(config=config, store=SimpleNamespace(workers={}))
    for spec in config.workers():
        node = VirtualWorker(fleet, spec)
        creation = node.status_payload() | {"name": spec.name, "worker_version": "v2.3.1"}
        validated = WorkerCreate.model_validate(creation)
        if spec.group.nvlink_domain_workers:
            assert validated.status.topology_facts and validated.status.kv_transfer_ifname
        else:
            assert all(d.network.status == "up" for d in validated.status.gpu_devices)
    worker = VirtualWorker(fleet, config.workers()[0])
    mappings = check_metric_mapping()
    normalized = check_normalization()
    preparation = {"restart_count": 0, "preparation": {"port": 40000, "ports": [40000]}}
    ModelInstanceMemberReport.model_validate(preparation)
    ModelInstanceMemberReport.model_validate(
        {
            "restart_count": 0,
            "workloads": [
                {
                    "name": "sim-1-0",
                    "worker_id": 1,
                    "state": "pending",
                    "dp_rank_placements": [
                        {
                            "dp_rank": 0,
                            "node_rank": 0,
                            "gpu_indexes": [0],
                            "api_port": 40000,
                            "ready": False,
                        }
                    ],
                }
            ],
        }
    )
    ModelInstanceMemberReport.model_validate(
        {
            "restart_count": 0,
            "observations": [
                {
                    "name": "sim-1-0",
                    "state": "running",
                    "rank_readiness": {"0": True},
                }
            ],
        }
    )
    model = {"id": 1, "name": "test", "backend": "vLLM"}
    instance = {
        "id": 10, "model_id": 1, "model_name": "test", "name": "test-instance", "worker_id": 1,
        "cluster_id": 1, "state": "running", "port": 40000, "ports": [40000],
        "restart_count": 0, "api_detected_backend_version": "0.21.0",
    }
    record = Record(instance, generation=0, phase="running",
                    traffic=SyntheticTraffic(10, 2, started=0))
    record.worker, record.model = worker, model
    worker.runtime.records[10] = record
    worker.worker_id, worker.client = 1, FakeClient()
    worker.watch_connected = True
    worker.watch_states["models"] = True
    worker.watch_caches["model-instances"][10] = instance
    worker.watch_caches["models"][1] = model
    asyncio.run(worker.runtime_metrics.aggregate())
    list(text_string_to_metric_families(render_raw(worker)))
    families = list(text_string_to_metric_families(render(worker)))
    assert len(families) > 15
    names = {family.name: family.type for family in families}
    assert names["gpustack:prompt_tokens"] == "counter"
    assert names["gpustack:time_to_first_token_seconds"] == "histogram"
    print(
        json.dumps(
            {
                "native_contract": "passed",
                "worker_create": True,
                "preparation": True,
                "workload_registration": True,
                "workload_observations": True,
                "prometheus_families": len(families),
                "native_metric_mappings": mappings,
                "native_normalized_families": normalized,
                "topology_and_npu_network": True,
            }
        )
    )


if __name__ == "__main__":
    main()
