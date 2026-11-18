"""Per-fleet identity persistence and an exclusive OS-held process lock."""

import json
import os
from pathlib import Path


class StateStore:
    def __init__(self, directory: str, namespace: str, server_url: str):
        self.directory = Path(directory) / namespace
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "identities.json"
        self.server_url = server_url.rstrip("/")
        self.workers: dict[str, dict] = {}
        self.lock_file = None

    def acquire(self):
        self.lock_file = (self.directory / "fleet.lock").open("a+b")
        self.lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            self.lock_file.write(b"0")
            self.lock_file.flush()
            self.lock_file.seek(0)
            try:
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.lock_file.close()
                self.lock_file = None
                raise RuntimeError("Another process owns this fleet state directory") from None
        else:
            import fcntl

            try:
                fcntl.flock(self.lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.lock_file.close()
                self.lock_file = None
                raise RuntimeError("Another process owns this fleet state directory") from None
        try:
            if self.path.exists():
                state = json.loads(self.path.read_text())
                if state["server_url"] != self.server_url:
                    raise ValueError("State belongs to a different Server URL; use a new namespace")
                self.workers = state["workers"]
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.lock_file:
            if os.name == "nt":
                import msvcrt

                self.lock_file.seek(0)
                msvcrt.locking(self.lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            self.lock_file.close()
            self.lock_file = None

    def save(self, name: str, identity: dict):
        self.workers[name] = identity
        self.write()

    def write(self):
        temporary = self.path.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"server_url": self.server_url, "workers": self.workers}, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)
