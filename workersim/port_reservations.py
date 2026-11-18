"""Worker-local advisory inventory and fenced distributed port reservations."""

from uuid import uuid4

from aiohttp import web

from workersim.hardware import local_member


class PortReservations:
    def __init__(self, worker):
        self.worker = worker
        self.generation = str(uuid4())
        self.claims = {}

    def busy(self, exclude=None):
        ports = set()
        ports.update(
            port
            for (worker_id, port), identity in self.worker.fleet.logical_ports.items()
            if worker_id == self.worker.worker_id and identity[0] != exclude
        )
        nodes = (
            [self.worker]
            if self.worker.config.network.worker_ip_cidr
            else self.worker.fleet.workers
        )
        for node in nodes:
            for key, record in node.runtime.records.items():
                if key != exclude:
                    ports.update(p for _, p in record.reserved)
        for key, claim in self.claims.items():
            if key != exclude:
                ports.update(claim["ports"])
        return ports

    async def handle(self, request):
        key = request.match_info.get("instance_id")
        key = int(key) if key else None
        worker = self.worker
        instance = None
        if key is not None:
            try:
                instance = await worker.client.request("GET", f"/model-instances/{key}")
            except Exception:
                raise web.HTTPNotFound() from None
            if local_member(instance, worker.worker_id) is None:
                raise web.HTTPNotFound()
            worker.instances.setdefault(key, instance)
        low, high = worker.config.network.instance_ports_for_slot(worker.spec.slot)
        if not worker.config.network.worker_ip_cidr:
            # Distributed admission requires a common range. Logical Workers
            # share the host's physical pool; ordinary launches prefer their block.
            low = worker.config.network.instance_port_start
            high = worker.config.network.instance_ports_for_slot(len(worker.fleet.workers) - 1)[1]
        if request.method == "GET":
            exclude = key or int(request.query.get("exclude_instance_id", "0"))
            return web.json_response(
                {
                    "worker_id": worker.worker_id,
                    "generation": self.generation,
                    "claim_version": self.claims.get(key, {}).get("token"),
                    "port_range": f"{low}-{high}",
                    "unavailable_ports": sorted(self.busy(exclude)),
                    "pending_cleanup": False,
                }
            )
        if request.method == "DELETE":
            claim = self.claims.get(key)
            if claim and claim["token"] == request.headers.get("X-Claim-Token"):
                self.claims.pop(key)
                self.generation = str(uuid4())
            return web.Response(status=204)
        body = await request.json()
        servers = instance.get("distributed_servers") or {}
        if servers.get("workloads") is not None and body.get("restart_count") != (
            instance.get("restart_count") or 0
        ):
            raise web.HTTPConflict()
        old = self.claims.get(key)
        if old and old["token"] == body.get("token") and old["ports"] == body.get("ports"):
            return web.json_response({"accepted": True, "unavailable_ports": []})
        if (body.get("generations") or {}).get(str(worker.worker_id)) != self.generation:
            raise web.HTTPConflict()
        if old and (body.get("replaces") or {}).get(str(worker.worker_id)) != old["token"]:
            raise web.HTTPConflict()
        ports = body.get("ports")
        if not isinstance(ports, list) or not ports or any(type(p) is not int for p in ports):
            raise web.HTTPBadRequest()
        conflicts = set(ports) & self.busy(key)
        conflicts.update(p for p in ports if not low <= p <= high)
        if conflicts:
            return web.json_response({"accepted": False, "unavailable_ports": sorted(conflicts)})
        if instance.get("ports") and not set(ports).issubset(instance["ports"]):
            raise web.HTTPConflict()
        self.claims[key] = {"token": body["token"], "ports": ports}
        self.generation = str(uuid4())
        return web.json_response({"accepted": True, "unavailable_ports": []})

    def reap(self):
        for key in set(self.claims) - set(self.worker.instances):
            self.claims.pop(key)
            self.generation = str(uuid4())
        for key, identity in list(self.worker.fleet.logical_ports.items()):
            instance = self.worker.instances.get(identity[0])
            if key[0] == self.worker.worker_id and (
                instance is None or identity[1] != (instance.get("restart_count") or 0)
            ):
                self.worker.fleet.logical_ports.pop(key, None)
