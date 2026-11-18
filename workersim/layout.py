"""Translate native allocation/configuration into simulated local tasks and ports.

No scheduler decisions are made here: devices, roles and membership always come
from the Server. Pure vLLM topology helpers are pinned to the target release.
"""

from workersim._vendor.command import (
    find_bool_parameter,
    find_last_int_parameter,
    find_last_parameter,
)
from workersim._vendor.vllm_topology import (
    external_lb_fragments,
    managed_lb_parallelism,
    managed_lb_port_requests,
    normalize_vllm_parameters,
    parse_user_parallelism,
    validate_multinode_topology,
    vllm_lb_mode,
)


def members(instance):
    return [
        instance,
        *((instance.get("distributed_servers") or {}).get("subordinate_workers") or []),
    ]


def effective_model(model, role):
    result = dict(model)
    override = next((r for r in model.get("roles") or [] if r["name"] == role), {})
    for key in ("backend", "backend_version", "backend_parameters", "env", "image_name"):
        if override.get(key) is not None:
            result[key] = override[key]
    return result


def sglang_parallelism(parameters):
    dp = find_last_int_parameter(parameters, ["dp", "dp-size", "data-parallel-size"]) or 1
    attention_dp = (
        find_last_int_parameter(parameters, ["attn-dp-size", "attention-data-parallel-size"]) or 1
    )
    return dp * attention_dp, attention_dp > 1 or find_bool_parameter(
        parameters, ["enable-dp-attention"]
    )


def port_requests(instance, model, catalog=None):
    """Ordered bands; connector declarations are read from the real Server catalog."""
    role = instance.get("role")
    parameters = normalize_vllm_parameters(model.get("backend_parameters"))
    backend = model.get("backend", "vLLM")
    counts = [len(m.get("gpu_indexes") or []) for m in members(instance)]
    mode = vllm_lb_mode(parameters, tokenized=True) if backend == "vLLM" else "internal"
    managed = (instance.get("distributed_servers") or {}).get("workloads") is not None
    requests = []
    if managed and backend == "vLLM" and mode != "internal":
        requests = [(n, c) for n, c in managed_lb_port_requests(parameters, counts, tokenized=True)]
    elif backend == "SGLang" and role != "router":
        dp, attention = sglang_parallelism(parameters)
        components = managed and role is None and dp > 1
        if components:
            requests.append(("_sglang_engine_api", 1))
        if components or attention or len(counts) > 1:
            rust = str((model.get("env") or {}).get("SGLANG_RUST_SERVER", "")).lower() in (
                "1",
                "true",
                "yes",
                "y",
            )
            requests.append(
                (
                    "_sglang_connecting",
                    max(14, dp + 8) if attention and rust else 14 if attention else 1,
                )
            )
        if components:
            requests.extend([("_sglang_smg_api", 1), ("_sglang_smg_metrics", 1)])
        else:
            requests.append(("_api", 1))
    else:
        requests.append(("_api", 1))
        if len(counts) > 1:
            connecting, cross = 1, 0
            if backend == "vLLM":
                executor = find_last_parameter(parameters, ["distributed-executor-backend"])
                if executor == "mp":
                    topology = validate_multinode_topology(
                        counts, parse_user_parallelism(parameters)
                    )
                    connecting = 10 if topology.shape in ("dp_only", "nested") else 1
                    cross = 3
                elif (find_last_int_parameter(parameters, ["dp", "data-parallel-size"]) or 1) > 1:
                    cross = 1
            requests.extend((f"_cross_{i}", 1) for i in range(cross))
            requests.append(
                ("_vllm_mp_connecting" if connecting > 1 else "_connecting", connecting)
            )
    disaggregation = model.get("disaggregation") or {}
    mode_config = next((m for m in catalog or [] if m["name"] == disaggregation.get("mode")), {})
    holder = (
        (mode_config.get("router") or {})
        if role == "router"
        else next((r for r in mode_config.get("roles") or [] if r["name"] == role), {})
    )
    for spec in holder.get("ports") or []:
        if isinstance(spec, str):
            spec = {"name": spec}
        if spec.get("scope", "instance") == "role":
            raise ValueError("Role-scoped connector ports require a Server registry")
        count = spec.get("count", 1)
        if isinstance(count, str):
            if "accelerator_count" in count:
                count = sum(counts)
            elif "data_parallel_size" in count:
                count = (
                    managed_lb_parallelism(parameters, counts).dp
                    if managed and backend == "vLLM" and mode != "internal"
                    else find_last_int_parameter(parameters, ["dp", "data-parallel-size"]) or 1
                )
            else:
                raise ValueError("Unresolved PD connector port width")
        if mode_config.get("name") == "vllm-mooncake" and role == "prefill" and mode == "external":
            count = managed_lb_parallelism(parameters, counts).dp
        requests.append((spec["name"], count))
    return requests


def preparation(instance, model, base, catalog=None):
    bands, ports = {}, []
    for name, count in port_requests(instance, model, catalog):
        if not isinstance(count, int) or count < 1:
            raise ValueError("Invalid native port band width")
        bands[name] = {"base": base + len(ports), "count": count}
        ports.extend(range(base + len(ports), base + len(ports) + count))
    api = next(
        bands[n]["base"] for n in ("_vllm_external_api", "_sglang_engine_api", "_api") if n in bands
    )
    # Generic planning aliases are positional in the original Worker protocol.
    named = {k: v for k, v in bands.items() if k not in ("_api", "_connecting")}
    return {"port": api, "ports": ports, "named_ports": named}


def workload_plan(instance, model, worker_id):
    all_members = members(instance)
    index = next(i for i, m in enumerate(all_members) if m["worker_id"] == worker_id)
    member = all_members[index]
    devices = sorted(member.get("gpu_indexes") or [])
    name = f"sim-{instance['id']}-r{instance.get('restart_count') or 0}-w{worker_id}"
    common = {"name": name, "worker_id": worker_id, "state": "pending"}
    backend = model.get("backend", "vLLM")
    parameters = model.get("backend_parameters") or []
    bands = instance.get("named_ports") or {}
    if backend == "SGLang":
        dp, _ = sglang_parallelism(parameters)
        result = [
            common
            | {
                "component": {
                    "kind": "sglang_engine",
                    **({"api_port": instance["port"], "dp_size": dp} if index == 0 else {}),
                }
            }
        ]
        if index == 0:
            result.append(
                common
                | {
                    "name": name + "-smg",
                    "component": {
                        "kind": "sglang_smg",
                        "api_port": bands["_sglang_smg_api"]["base"],
                        "metrics_port": bands["_sglang_smg_metrics"]["base"],
                    },
                }
            )
        return result
    mode = vllm_lb_mode(parameters)
    if mode in ("external", "multi_port_external", "hybrid"):
        counts = [len(m.get("gpu_indexes") or []) for m in all_members]
        topology = managed_lb_parallelism(parameters, counts)
        fragments = external_lb_fragments(counts, topology, index)
        api = bands["_vllm_external_api"]["base"]
        if mode == "hybrid":
            return [
                common
                | {
                    "component": {
                        "kind": "vllm_hybrid",
                        **(
                            {"api_port": api, "dp_size": len(fragments)}
                            if fragments[0].node_rank == 0
                            else {}
                        ),
                    }
                }
            ]
        placements = []
        for fragment in fragments:
            value = {
                "dp_rank": fragment.dp_rank,
                "node_rank": fragment.node_rank,
                "gpu_indexes": [devices[i] for i in fragment.positions],
            }
            if fragment.node_rank == 0:
                value.update(api_port=api + fragment.dp_rank, ready=False)
            placements.append(value)
        return [common | {"dp_rank_placements": placements}]
    # Used by protocol fixtures and generic managed allocations.
    value = {"dp_rank": 0, "node_rank": index, "gpu_indexes": devices}
    if index == 0:
        value.update(api_port=instance["port"], ready=False)
    return [common | {"dp_rank_placements": [value]}]


def observation(workload, state):
    result = {
        "name": workload["name"],
        "state": state,
        "rank_readiness": {
            str(p["dp_rank"]): state == "running"
            for p in workload.get("dp_rank_placements", [])
            if p["node_rank"] == 0
        },
    }
    component = workload.get("component")
    if component:
        result.update(
            component_attempt=component.get("attempt", 0),
            component_ready=state == "running" if component.get("api_port") else None,
        )
    return result
