"""Fleet lifecycle and a loopback-only control API."""

import asyncio
import json
import logging
import os
import signal
from importlib.resources import files

from aiohttp import web

from workersim import TARGET_VERSION, __version__
from workersim.client import Client, cancel_tasks
from workersim.http_server import listen
from workersim.state import StateStore
from workersim.worker import VirtualWorker

log = logging.getLogger(__name__)


class Fleet:
    def __init__(self, config):
        self.config = config
        self.store = StateStore(
            config.simulation.data_dir, config.simulation.namespace, config.server.url
        )
        self.workers = []
        self.ports: set[tuple[str, int]] = set()
        self.logical_ports = {}
        self.stop = asyncio.Event()
        self.failure = None
        self.pd_catalog = json.loads(files("workersim").joinpath("data/pd_ports.json").read_text())

    async def run(self):
        self.store.acquire()
        control = None
        starters = []
        try:
            token = os.environ.get(self.config.server.registration_token_env, "")
            if not token and not self.store.workers:
                raise ValueError(f"Set {self.config.server.registration_token_env} before starting")
            probe = await Client(self.config.server, token).open()
            try:
                version = await probe.request("GET", "/version", root=True)
            finally:
                await probe.close()
            actual = str(version.get("version", "")).removeprefix("v")
            if actual != TARGET_VERSION and not self.config.server.allow_version_mismatch:
                raise ValueError(f"Expected GPUStack {TARGET_VERSION}, received {actual!r}")
            self.workers = [VirtualWorker(self, spec) for spec in self.config.workers()]
            control = await listen(
                self.control_app(), "127.0.0.1", self.config.network.control_port
            )
            loop = asyncio.get_running_loop()
            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(signum, self.stop.set)
                except (NotImplementedError, RuntimeError):
                    pass

            async def start(worker):
                await asyncio.sleep(worker.spec.slot / self.config.simulation.registration_rate)
                await worker.start(token)

            starters = [asyncio.create_task(start(worker)) for worker in self.workers]
            await asyncio.gather(*starters)
            log.info(
                "Fleet ready: %s Workers; control API 127.0.0.1:%s",
                len(self.workers),
                self.config.network.control_port,
            )
            await self.stop.wait()
            if self.failure:
                raise RuntimeError("A Worker background loop failed") from self.failure
        finally:
            await cancel_tasks(starters)
            for worker in self.workers:
                await worker.close()
            if control:
                await control.cleanup()
            self.store.close()

    def control_app(self):
        app = web.Application()

        async def status(request):
            return web.json_response(
                {
                    "version": __version__,
                    "target_version": TARGET_VERSION,
                    "namespace": self.config.simulation.namespace,
                    "workers": [worker.summary() for worker in self.workers],
                }
            )

        async def scale(request):
            body = await request.json()
            count = body.get("count")
            if type(count) is not int or not 0 <= count <= len(self.workers):
                raise web.HTTPBadRequest(text="count must be within configured fleet capacity")
            for index, worker in enumerate(self.workers):
                worker.online = index < count
            return web.json_response({"online": count, "configured": len(self.workers)})

        async def stop(request):
            self.stop.set()
            return web.json_response({"stopping": True})

        async def fault(request):
            body = await request.json()
            worker = next((w for w in self.workers if w.spec.name == body.get("worker")), None)
            if worker is None:
                raise web.HTTPNotFound(text="Unknown simulated Worker")
            key = body.get("instance_id")
            record = worker.runtime.records.get(key)
            if record is None or body.get("action") != "fail-instance":
                raise web.HTTPBadRequest(text="Use fail-instance with a local instance_id")
            record.injected_failure = "WorkerSim: injected runtime failure"
            return web.json_response(
                {"worker": worker.spec.name, "instance_id": key, "fault": "fail-instance"}
            )

        app.router.add_get("/status", status)
        app.router.add_post("/scale", scale)
        app.router.add_post("/stop", stop)
        app.router.add_post("/fault", fault)
        return app
