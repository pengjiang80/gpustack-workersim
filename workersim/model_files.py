"""Native ModelFile watch and simulated downloads; never touch host model files."""

import hashlib
import time

from workersim.client import APIError


def virtual_paths(item):
    if item.get("resolved_paths"):
        return item["resolved_paths"]
    if item.get("source") == "local_path" and item.get("local_path"):
        return [item["local_path"]]
    source = item.get("huggingface_repo_id") or item.get("model_scope_model_id") or "model"
    digest = hashlib.sha256(source.encode()).hexdigest()[:12]
    return [f"/var/lib/workersim/models/{digest}"]


def identity(item):
    return tuple(
        item.get(k)
        for k in (
            "worker_id",
            "source",
            "huggingface_repo_id",
            "huggingface_filename",
            "model_scope_model_id",
            "model_scope_file_path",
            "local_path",
            "local_dir",
        )
    )


class ModelFiles:
    def __init__(self, worker):
        self.worker = worker
        self.items = {}
        self.downloads = {}
        self.completed = 0

    def event(self, kind, data):
        key = data.get("id")
        if key is None:
            return
        if kind == "DELETED" or data.get("worker_id") != self.worker.worker_id:
            self.items.pop(key, None)
            self.downloads.pop(key, None)
        else:
            self.items[key] = data

    async def refresh(self):
        epoch = self.worker.event_counts.get("model-files", 0)
        items = await self.worker.client.list_all("/model-files", worker_id=self.worker.worker_id)
        # Do not overwrite events that arrived while the HTTP snapshot was in flight.
        if epoch == self.worker.event_counts.get("model-files", 0):
            self.items = {i["id"]: i for i in items if i.get("worker_id") == self.worker.worker_id}
            self.downloads = {k: v for k, v in self.downloads.items() if k in self.items}

    async def tick(self):
        duration = self.worker.config.simulation.download_seconds
        for key, item in list(self.items.items()):
            if item.get("state") != "downloading":
                self.downloads.pop(key, None)
                continue
            signature = identity(item)
            started, previous, sent = self.downloads.get(key, (time.monotonic(), signature, -1))
            if previous != signature:
                started, sent = time.monotonic(), -1
            elapsed = time.monotonic() - started
            progress = 100 if duration <= 0 else min(100, int(elapsed / duration * 100))
            # A bounded number of writes per file, including visible intermediate progress.
            bucket = progress // 25 * 25
            self.downloads[key] = started, signature, sent
            if bucket <= sent:
                continue
            try:
                latest = self.worker.watch_caches["model-files"].get(key)
                if latest is None:
                    latest = await self.worker.client.request("GET", f"/model-files/{key}")
                if latest.get("state") != "downloading" or identity(latest) != signature:
                    self.event("UPDATED", latest)
                    continue
                body = {
                    k: latest[k]
                    for k in (
                        "worker_id",
                        "source",
                        "huggingface_repo_id",
                        "huggingface_filename",
                        "model_scope_model_id",
                        "model_scope_file_path",
                        "local_path",
                        "local_dir",
                        "cleanup_on_delete",
                        "is_lora",
                        "base_model",
                    )
                    if k in latest
                }
                body.update(
                    download_progress=bucket,
                    size=latest.get("size") or 2 * 1024**3,
                    resolved_paths=virtual_paths(latest),
                    state="ready" if bucket == 100 else "downloading",
                    state_message=None,
                )
                result = await self.worker.client.request("PUT", f"/model-files/{key}", body=body)
                self.items[key] = result
                self.downloads[key] = started, signature, bucket
                if bucket == 100:
                    self.completed += 1
            except APIError as error:
                if error.status == 404:
                    self.event("DELETED", {"id": key})
                else:
                    raise

    def paths(self):
        return {
            p: item
            for item in self.items.values()
            if item.get("state") == "ready"
            for p in virtual_paths(item)
        }

    def pending_for(self, instance):
        servers = instance.get("distributed_servers") or {}
        if instance.get("role") == "router" or (
            instance.get("worker_id") != self.worker.worker_id
            and servers.get("download_model_files") is False
        ):
            return False
        # Configuration snapshots are not flat ModelSource objects. Also wait
        # for late file creation, before the first watch event has arrived.
        for source in (instance, instance.get("draft_model_source") or {}):
            for key in ("huggingface_repo_id", "model_scope_model_id", "local_path"):
                if not source.get(key):
                    continue
                matching = [i for i in self.items.values() if i.get(key) == source[key]]
                if not matching or any(i.get("state") != "ready" for i in matching):
                    return True
        return False
