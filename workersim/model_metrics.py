"""Synthetic engine expositions using each runtime's native metric names.

Engines (vLLM, SGLang, MindIE, their PD routers and the SGLang gateway) expose
their own families, exactly as the native Worker scrapes them. The Worker-side
aggregator in ``runtime_metrics`` normalizes them with GPUStack's metrics config.
"""

import json
import math
import time
from dataclasses import dataclass, field

TIME_BUCKETS = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)
TOKEN_BUCKETS = (1, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 32768)
MS_BUCKETS = tuple(b * 1000 for b in TIME_BUCKETS)
BYTE_BUCKETS = (1024, 16384, 65536, 262144, 1048576, 4194304, 16777216, 67108864)
MB_BUCKETS = tuple(b / 1048576 for b in BYTE_BUCKETS)
COUNT_BUCKETS = (1, 2, 4, 8, 16, 32, 64, 128)

# Engine version reported by the synthetic version API when a deployment does not pin one.
DEFAULT_RUNTIME_VERSIONS = {"vLLM": "0.21.0", "SGLang": "0.5.15.post1", "MindIE": "2.3.0"}


@dataclass
class Family:
    name: str
    type: str
    documentation: str = ""
    samples: list = field(default_factory=list)  # (sample name, labels, value)


def sample_line(name, labels, value):
    encoded = ",".join(f"{k}={json.dumps(str(v), ensure_ascii=False)}" for k, v in labels.items())
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    text = repr(value) if isinstance(value, float) else str(value)
    return f"{name}{{{encoded}}} {text}" if labels else f"{name} {text}"


def render_families(families) -> str:
    lines = []
    for family in families:
        if not family.samples:
            continue
        # Same header conventions as prometheus_client.generate_latest (text format 0.0.4).
        name, kind = family.name, family.type
        if kind == "counter":
            name += "_total"
        elif kind == "info":
            name, kind = name + "_info", "gauge"
        elif kind == "unknown":
            kind = "untyped"
        doc = (family.documentation or family.name).replace("\\", "\\\\").replace("\n", "\\n")
        lines.append(f"# HELP {name} {doc}")
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(sample_line(*sample) for sample in family.samples)
    return "\n".join(lines) + ("\n" if lines else "")


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

    def histogram(self, family, labels, average, requests, buckets, *, exact=False):
        values = [
            average if exact else average * self.factor * x
            for x in (0.5, 0.7, 0.85, 0.95, 1.05, 1.15, 1.3, 1.5)
        ]
        cycles, remainder = divmod(requests, len(values))
        counts = [cycles + (index < remainder) for index in range(len(values))]
        for bucket in buckets:
            count = sum(n for value, n in zip(values, counts, strict=True) if value <= bucket)
            bucket_labels = labels | {"le": repr(float(bucket))}
            family.samples.append((family.name + "_bucket", bucket_labels, count))
        family.samples.append((family.name + "_bucket", labels | {"le": "+Inf"}, requests))
        family.samples.append((family.name + "_count", labels, requests))
        total = sum(value * count for value, count in zip(values, counts, strict=True))
        family.samples.append((family.name + "_sum", labels, float(total)))


# Native exposition names per runtime. Keys are the simulator's internal quantities.
_ENGINE = {
    "vLLM": {
        "gauge": {
            "num_requests_running": "vllm:num_requests_running",
            "num_requests_waiting": "vllm:num_requests_waiting",
            "kv_cache_usage_ratio": "vllm:kv_cache_usage_perc",
        },
        "counter": {
            "request_success": "vllm:request_success",
            "prompt_tokens": "vllm:prompt_tokens",
            "generation_tokens": "vllm:generation_tokens",
            "prefix_cache_queries": "vllm:prefix_cache_queries",
            "prefix_cache_hits": "vllm:prefix_cache_hits",
            "prompt_tokens_cached": "vllm:prompt_tokens_cached",
            "request_preemptions": "vllm:num_preemptions",
        },
        "histogram": {
            "time_to_first_token_seconds": "vllm:time_to_first_token_seconds",
            "time_per_output_token_seconds": "vllm:request_time_per_output_token_seconds",
            "inter_token_latency_seconds": "vllm:inter_token_latency_seconds",
            "e2e_request_latency_seconds": "vllm:e2e_request_latency_seconds",
            "request_queue_time_seconds": "vllm:request_queue_time_seconds",
            "request_inference_time_seconds": "vllm:request_inference_time_seconds",
            "request_prefill_time_seconds": "vllm:request_prefill_time_seconds",
            "request_decode_time_seconds": "vllm:request_decode_time_seconds",
            "request_prompt_tokens": "vllm:request_prompt_tokens",
            "request_generation_tokens": "vllm:request_generation_tokens",
        },
    },
    "SGLang": {
        "gauge": {
            "num_requests_running": "sglang:num_running_reqs",
            "num_requests_waiting": "sglang:num_queue_reqs",
            "kv_cache_usage_ratio": "sglang:token_usage",
            "prefix_cache_hit_rate": "sglang:cache_hit_rate",
            "request_preemptions": "sglang:num_retracted_reqs",
        },
        "counter": {
            "prompt_tokens": "sglang:prompt_tokens",
            "generation_tokens": "sglang:generation_tokens",
            "prompt_tokens_cached": "sglang:cached_tokens",
        },
        "histogram": {
            "time_to_first_token_seconds": "sglang:time_to_first_token_seconds",
            "inter_token_latency_seconds": "sglang:inter_token_latency_seconds",
            "e2e_request_latency_seconds": "sglang:e2e_request_latency_seconds",
            "request_queue_time_seconds": "sglang:queue_time_seconds",
            "request_prompt_tokens": "sglang:prompt_tokens_histogram",
            "request_generation_tokens": "sglang:generation_tokens_histogram",
        },
    },
    "MindIE": {
        "gauge": {
            "num_requests_running": "num_requests_running",
            "num_requests_waiting": "num_requests_waiting",
            "num_requests_swapped": "num_requests_swapped",
            "prefix_cache_hit_rate": "npu_prefix_cache_hit_rate",
            "kv_cache_usage_ratio": "npu_cache_usage_perc",
        },
        "counter": {
            "request_success": "request_success",
            "prompt_tokens": "prompt_tokens",
            "generation_tokens": "generation_tokens",
            "request_preemptions": "num_preemptions",
        },
        "histogram": {
            "time_to_first_token_seconds": "time_to_first_token_seconds",
            "time_per_output_token_seconds": "time_per_output_token_seconds",
            "e2e_request_latency_seconds": "e2e_request_latency_seconds",
            "request_prompt_tokens": "request_prompt_tokens",
            "request_generation_tokens": "request_generation_tokens",
        },
    },
}
TOKEN_HISTOGRAMS = ("request_prompt_tokens", "request_generation_tokens")


class _Builder:
    def __init__(self):
        self.families: dict[str, Family] = {}

    def family(self, name, kind, doc=""):
        if name not in self.families:
            self.families[name] = Family(name, kind, doc or name)
        return self.families[name]

    def gauge(self, name, labels, value):
        self.family(name, "gauge").samples.append((name, labels, value))

    def counter(self, name, labels, value):
        self.family(name, "counter").samples.append((name + "_total", labels, value))


def engine_families(traffic, runtime, labels, *, role=None, now=None) -> list[Family]:
    """One engine API's native exposition; empty for runtimes without metrics support."""
    names = _ENGINE.get(runtime)
    if traffic is None or names is None:
        return []
    requests, gauges, counters, histograms = traffic.values(now)
    out = _Builder()
    for key, name in names["gauge"].items():
        value = counters.get(key, 0) if key == "request_preemptions" else gauges.get(key, 0)
        out.gauge(name, labels, value)
    for key, name in names["counter"].items():
        sample_labels = labels
        if name == "sglang:cached_tokens":
            sample_labels = labels | {"cache_source": "device"}
        out.counter(name, sample_labels, counters[key])
    for key, name in names["histogram"].items():
        tokens = key in TOKEN_HISTOGRAMS
        traffic.histogram(
            out.family(name, "histogram"), labels, histograms[key], requests,
            TOKEN_BUCKETS if tokens else TIME_BUCKETS, exact=tokens,
        )
    if role in ("prefill", "decode"):
        _pd_engine(out, traffic, runtime, labels, requests, gauges)
    return list(out.families.values())


def _pd_engine(out, traffic, runtime, labels, requests, gauges):
    if runtime == "vLLM":
        # NIXL connector families, already in base units.
        for name, average, buckets in (
            ("vllm:nixl_bytes_transferred", 524288, BYTE_BUCKETS),
            ("vllm:nixl_xfer_time_seconds", 0.02, TIME_BUCKETS),
            ("vllm:nixl_post_time_seconds", 0.003, TIME_BUCKETS),
            ("vllm:nixl_num_descriptors", 8, COUNT_BUCKETS),
        ):
            traffic.histogram(out.family(name, "histogram"), labels, average, requests, buckets)
        for name in (
            "vllm:nixl_num_failed_transfers",
            "vllm:nixl_num_kv_expired_reqs",
            "vllm:nixl_num_failed_notifications",
        ):
            out.counter(name, labels, 0)
    elif runtime == "SGLang":
        for name, value in (
            ("sglang:num_prefill_bootstrap_queue_reqs", 1 if traffic.rate else 0),
            ("sglang:num_prefill_inflight_queue_reqs", gauges["num_requests_running"]),
            ("sglang:num_decode_prealloc_queue_reqs", 0),
            ("sglang:num_decode_transfer_queue_reqs", 1),
            ("sglang:pending_prealloc_token_usage", 0),
        ):
            out.gauge(name, labels, value)
        for name in (
            "sglang:num_transfer_failed_reqs",
            "sglang:num_bootstrap_failed_reqs",
            "sglang:num_prefill_retries",
        ):
            out.counter(name, labels, 0)
        # SGLang reports PD transfer time in milliseconds and volume in megabytes.
        for name, average, buckets in (
            ("sglang:kv_transfer_bootstrap_ms", 20, MS_BUCKETS),
            ("sglang:kv_transfer_latency_ms", 20, MS_BUCKETS),
            ("sglang:kv_transfer_alloc_ms", 2, MS_BUCKETS),
            ("sglang:kv_transfer_total_mb", 0.5, MB_BUCKETS),
        ):
            traffic.histogram(out.family(name, "histogram"), labels, average, requests, buckets)


def router_families(traffic, runtime, labels, peers=(), *, now=None) -> list[Family]:
    """PD router exposition: the vLLM router fork or the SGLang model gateway."""
    if traffic is None or runtime not in ("vLLM", "SGLang"):
        return []
    requests = traffic.values(now)[0]
    out = _Builder()
    if runtime == "vLLM":
        out.counter("vllm_router_pd_requests", labels, requests)
        for peer in peers:
            peer_labels = labels | {"worker": "http://" + peer["worker"]}
            kind = peer["worker_type"]
            out.counter(f"vllm_router_pd_{kind}_requests", peer_labels, requests // len(peers))
            if kind == "decode":
                out.counter("vllm_router_pd_decode_errors", peer_labels, 0)
        traffic.histogram(
            out.family("vllm_router_pd_request_duration_seconds", "histogram"),
            labels, 2, requests, TIME_BUCKETS,
        )
        return list(out.families.values())
    _smg(out, traffic, labels, peers, requests)
    traffic.histogram(
        out.family("smg_router_request_duration_seconds", "histogram"),
        labels, 2, requests, TIME_BUCKETS,
    )
    for peer in peers:
        out.gauge("smg_worker_health", labels | _peer(peer), 1)
    return list(out.families.values())


def gateway_families(traffic, labels, peers=(), *, now=None) -> list[Family]:
    """The ordinary SGLang gateway component (SGLangSMG mapping domain)."""
    if traffic is None:
        return []
    out = _Builder()
    _smg(out, traffic, labels, peers, traffic.values(now)[0])
    return list(out.families.values())


def _peer(peer):
    return {"worker": "http://" + peer["worker"], "worker_type": peer["worker_type"]}


def _smg(out, traffic, labels, peers, requests):
    out.counter("smg_router_requests", labels, requests)
    for peer in peers:
        peer_labels = labels | _peer(peer)
        out.counter("smg_worker_selection", peer_labels, requests // max(1, len(peers)))
        out.gauge("smg_worker_requests_active", peer_labels, 1 if traffic.rate else 0)
