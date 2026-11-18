"""Synthetic traffic, exported with the unmodified 2.3.1 runtime metric contract."""

import json
import math
import time

# Native vLLM names mapped by GPUStack's built-in metrics_config.yaml.
RAW_NAMES = {
    "kv_cache_usage_ratio": "kv_cache_usage_perc",
    "time_per_output_token_seconds": "request_time_per_output_token_seconds",
    "request_preemptions": "num_preemptions",
}
TIME_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
TOKEN_BUCKETS = (1, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 32768)


def sample_line(name, labels, value):
    encoded = ",".join(f"{k}={json.dumps(str(v), ensure_ascii=False)}" for k, v in labels.items())
    return f"{name}{{{encoded}}} {value:.9g}"


class SyntheticTraffic:
    def __init__(self, seed, rate, *, started=None):
        self.started = time.monotonic() if started is None else started
        self.rate = rate * (0.8 + seed % 5 * 0.1)
        self.phase = seed % 37 / 37 * math.tau
        self.prompt = 512 + seed % 5 * 128
        self.output = 128 + seed % 3 * 64
        self.factor = 0.8 + seed % 7 * 0.07

    def values(self, now=None):
        elapsed = max(0, (time.monotonic() if now is None else now) - self.started)
        angle = self.phase + elapsed / 23
        wave = (1 + math.sin(angle)) / 2
        # Integral of a strictly positive rate: counters never decrease when load varies.
        requests = int(self.rate * (elapsed + 0.35 * 23 * (math.cos(self.phase) - math.cos(angle))))
        qps = self.rate * (1 + 0.35 * math.sin(angle))
        gauges = {
            "num_requests_running": math.ceil(qps * 1.8),
            "num_requests_waiting": int(wave * 3) if self.rate else 0,
            "num_requests_swapped": 0,
            "kv_cache_usage_ratio": (0.2 + wave * 0.5) if self.rate else 0,
            "prefix_cache_hit_rate": 0.65 if self.rate else 0,
        }
        counters = {
            "request_success": requests,
            "prompt_tokens": requests * self.prompt,
            "generation_tokens": requests * self.output,
            "prefix_cache_queries": requests * self.prompt,
            "prefix_cache_hits": requests * self.prompt * 0.65,
            "prompt_tokens_cached": requests * self.prompt * 0.65,
            "request_preemptions": 0,
        }
        histograms = {
            "time_to_first_token_seconds": 0.08,
            "time_per_output_token_seconds": 0.015,
            "inter_token_latency_seconds": 0.015,
            "e2e_request_latency_seconds": 2,
            "request_queue_time_seconds": 0.01,
            "request_inference_time_seconds": 1.99,
            "request_prefill_time_seconds": 0.07,
            "request_decode_time_seconds": 1.92,
            "request_prompt_tokens": self.prompt,
            "request_generation_tokens": self.output,
        }
        return requests, gauges, counters, histograms

    def render(self, labels, *, raw=False, now=None, router=False, pd=False, peers=()):
        requests, gauges, counters, histograms = self.values(now)
        if router:
            # A routing component is not another token-producing engine.
            gauges, counters, histograms = {}, {}, {}
        if pd:
            gauges.update(
                pd_prefill_bootstrap_queue_requests=1 if self.rate else 0,
                pd_prefill_inflight_queue_requests=gauges["num_requests_running"],
                pd_decode_prealloc_queue_requests=0,
                pd_decode_transfer_queue_requests=1,
                pd_decode_pending_prealloc_token_usage=0,
            )
            counters.update(
                pd_kv_transfer_failed=0,
                pd_bootstrap_failed=0,
                pd_prefill_retries=0,
                pd_kv_lease_expired=0,
                pd_kv_notification_failed=0,
            )
            histograms.update(
                pd_kv_transfer_seconds=0.02,
                pd_kv_transfer_latency_seconds=0.02,
                pd_kv_transfer_alloc_seconds=0.002,
                pd_kv_transfer_post_seconds=0.003,
                pd_kv_transfer_bytes=524288,
                pd_kv_transfer_descriptors=8,
            )
        if router:
            gauges.update(pd_router_worker_health=1, pd_router_worker_requests_active=2)
            counters.update(
                pd_router_requests=requests,
                pd_router_prefill_requests=requests,
                pd_router_decode_requests=requests,
                pd_router_prefill_errors=0,
                pd_router_decode_errors=0,
                pd_router_worker_selections=requests * 2,
            )
            histograms.update(pd_router_request_duration_seconds=2)
        lines = ["# Synthetic background traffic; no real inference or client requests implied."]

        def name(key):
            return ("vllm:" + RAW_NAMES.get(key, key)) if raw else "gpustack:" + key

        if router and peers:
            gauges.pop("pd_router_worker_health", None)
            gauges.pop("pd_router_worker_requests_active", None)
            counters.pop("pd_router_worker_selections", None)
            for peer in peers:
                peer_labels = labels | peer
                for key, value in (
                    ("pd_router_worker_health", 1),
                    ("pd_router_worker_requests_active", 1 if self.rate else 0),
                ):
                    lines.append(f"# TYPE {name(key)} gauge")
                    lines.append(sample_line(name(key), peer_labels, value))
                key = "pd_router_worker_selections"
                lines.append(f"# TYPE {name(key)} counter")
                lines.append(sample_line(name(key) + "_total", peer_labels, requests // len(peers)))

        for kind, values in (("gauge", gauges), ("counter", counters)):
            for key, value in values.items():
                metric = name(key)
                lines.append(f"# TYPE {metric} {kind}")
                lines.append(
                    sample_line(metric + ("_total" if kind == "counter" else ""), labels, value)
                )
        for key, average in histograms.items():
            metric = name(key)
            lines.append(f"# TYPE {metric} histogram")
            tokens = key in ("request_prompt_tokens", "request_generation_tokens")
            values = [
                average if tokens else average * self.factor * x
                for x in (0.5, 0.7, 0.85, 0.95, 1.05, 1.15, 1.3, 1.5)
            ]
            cycles, remainder = divmod(requests, len(values))
            counts = [cycles + (index < remainder) for index in range(len(values))]
            for bucket in TOKEN_BUCKETS if tokens else TIME_BUCKETS:
                count = sum(n for value, n in zip(values, counts, strict=True) if value <= bucket)
                lines.append(sample_line(metric + "_bucket", labels | {"le": str(bucket)}, count))
            lines.append(sample_line(metric + "_bucket", labels | {"le": "+Inf"}, requests))
            lines.append(sample_line(metric + "_count", labels, requests))
            total = sum(value * count for value, count in zip(values, counts, strict=True))
            lines.append(sample_line(metric + "_sum", labels, total))
        return "\n".join(lines) + "\n"


def instance_labels(worker, record, source=None):
    instance = record.instance
    labels = {
        "worker_id": str(worker.worker_id),
        "worker_name": worker.spec.name,
        "model_id": str(instance["model_id"]),
        "model_name": instance.get("model_name", ""),
        "model_instance_id": str(instance["id"]),
        "model_instance_name": instance["name"],
        "runtime": record.model.get("backend") or instance.get("backend") or "vLLM",
        "role": instance.get("role") or "",
        "restart_count": str(record.generation),
        "source_kind": "legacy",
        "source_name": "legacy",
        "workload_name": "",
        "exposition_scope": "legacy",
        "workersim": "true",
    }
    if source:
        labels.update(source)
    return labels


def metric_sources(worker, record):
    """One series per real native metrics source; headless members have no token series."""
    if not record.workloads:
        return (
            [{}] if record.instance.get("worker_id", worker.worker_id) == worker.worker_id else []
        )
    result = []
    for workload in record.workloads:
        if workload.get("state") in ("stopped", "failed"):
            continue
        common = {"workload_name": workload["name"], "exposition_scope": "engine"}
        component = workload.get("component")
        if component:
            if not component.get("api_port") or component.get("stop_requested"):
                continue
            source = common | {"source_kind": component["kind"], "source_name": workload["name"]}
            if component["kind"] == "sglang_smg":
                source["component_attempt"] = str(component.get("attempt", 0))
            result.append(source)
        else:
            for placement in workload.get("dp_rank_placements", []):
                if placement["node_rank"] != 0:
                    continue
                rank = placement["dp_rank"]
                result.append(
                    common
                    | {
                        "source_kind": "vllm_engine",
                        "source_name": f"{workload['name']}/main-{rank}",
                        "endpoint_dp_rank": str(rank),
                        "gpu_indexes": ",".join(map(str, placement["gpu_indexes"])),
                    }
                )
    return result


def render_models(worker, *, raw=False):
    lines = []
    for record in worker.runtime.records.values():
        if record.phase != "running" or record.traffic is None:
            continue
        for source in metric_sources(worker, record):
            labels = instance_labels(worker, record, source)
            if raw:
                labels["engine"] = labels.get("endpoint_dp_rank", "0")
            router = (
                record.instance.get("role") == "router" or labels["source_kind"] == "sglang_smg"
            )
            peers = []
            if record.instance.get("role") == "router" and record.worker is not None:
                from workersim.router import registered_peers

                peers = [
                    {"worker_type": row["worker_type"], "worker": url.removeprefix("http://")}
                    for url, row in registered_peers(record).items()
                ]
            lines.append(
                record.traffic.render(
                    labels,
                    raw=raw,
                    router=router,
                    pd=record.instance.get("role") in ("prefill", "decode"),
                    peers=peers,
                )
            )
            if raw:
                continue
            # "success" describes successful generation of synthetic exposition, not inference.
            lines.append(
                sample_line(
                    "gpustack:runtime_metrics_scrape_status", labels | {"status": "success"}, 1
                )
                + "\n"
            )
            lines.append(sample_line("workersim_model_simulated", labels, 1) + "\n")
    # Multiple instances on one exporter share families; TYPE must occur only once.
    seen = set()
    result = []
    for line in "".join(lines).splitlines():
        if line.startswith("# TYPE "):
            if line in seen:
                continue
            seen.add(line)
        result.append(line)
    return "\n".join(result) + ("\n" if result else "")
