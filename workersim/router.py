"""PD router membership state, driven by the Server's real reconciliation calls."""

from urllib.parse import unquote

from aiohttp import web


def live_peers(record):
    result = {}
    for node in record.worker.fleet.workers:
        if not node.online:
            continue
        for peer in node.runtime.records.values():
            item = peer.instance
            if (
                peer.phase != "running"
                or item.get("state") != "running"
                or item.get("role") not in ("prefill", "decode")
                or item.get("model_id") != record.instance.get("model_id")
                or item.get("group_id") != record.instance.get("group_id")
                or item.get("draining_since")
                or item.get("deleted_at")
                or peer.generation != (item.get("restart_count") or 0)
            ):
                continue
            ports = set()
            for workload in peer.workloads:
                if workload.get("state") in ("stopped", "failed"):
                    continue
                component = workload.get("component") or {}
                if component.get("api_port") and not component.get("stop_requested"):
                    ports.add(component["api_port"])
                ports.update(
                    rank["api_port"]
                    for rank in workload.get("dp_rank_placements", [])
                    if rank.get("node_rank") == 0 and rank.get("api_port")
                )
            if not peer.workloads and item.get("worker_id") == node.worker_id and peer.port:
                ports.add(peer.port)
            for port in ports:
                url = f"http://{node.ip}:{port}"
                result[url] = {
                    "url": url,
                    "worker_type": item["role"],
                    "epoch": (item["id"], peer.generation),
                    "bootstrap_port": (item.get("named_ports") or {})
                    .get("bootstrap", {})
                    .get("base"),
                }
    return result


def registered_peers(record):
    live = live_peers(record)
    return {
        url: row
        for url, row in record.router_registry.items()
        if url in live and row["epoch"] == live[url]["epoch"]
    }


async def membership(request, record, path):
    if record is None or record.instance.get("role") != "router":
        raise web.HTTPNotFound()
    if path == "/workers" and request.method == "GET":
        rows = [
            {k: v for k, v in row.items() if k != "epoch"}
            for row in record.router_registry.values()
        ]
        return web.json_response({"workers": rows, "total": len(rows), "simulated": True})
    if path == "/workers" and request.method == "POST":
        body = await request.json()
        peer = live_peers(record).get(body.get("url"))
        if not peer or body.get("worker_type") != peer["worker_type"]:
            raise web.HTTPConflict(text="No matching live peer in this model and group")
        if (
            body.get("bootstrap_port") is not None
            and body["bootstrap_port"] != peer["bootstrap_port"]
        ):
            raise web.HTTPConflict(text="Peer bootstrap port does not match its allocation")
        # No outbound requests or arbitrary URLs: only actual local simulated endpoints.
        row = {
            "id": peer["url"],
            "url": peer["url"],
            "worker_type": peer["worker_type"],
            "model_id": body.get("model_id"),
            "epoch": peer["epoch"],
        }
        if peer["bootstrap_port"] is not None:
            row["bootstrap_port"] = peer["bootstrap_port"]
        record.router_registry[peer["url"]] = row
        return web.json_response({"status": "ok", "simulated": True})
    if path.startswith("/workers/") and request.method == "DELETE":
        record.router_registry.pop(unquote(path[len("/workers/") :]), None)
        return web.json_response({"status": "ok", "simulated": True})
    raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST", "DELETE"])
