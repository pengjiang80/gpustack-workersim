import importlib.util
import json
import sys
from pathlib import Path

import pytest


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts" / "vultr_lab.py"
    spec = importlib.util.spec_from_file_location("vultr_lab_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STATE = tmp_path / "vultr.json"
    module.STATE.write_text(
        json.dumps({"id": "this-lab-only", "label": module.LABEL, "status": "active"})
    )
    monkeypatch.setattr(sys, "argv", ["vultr_lab.py", "release"])
    return module


def test_release_checks_label_before_deletion(cloud, monkeypatch):
    calls = []

    def api(method, path, body=None):
        calls.append((method, path))
        return {"instance": {"label": "unrelated-instance"}}

    monkeypatch.setattr(cloud, "api", api)
    with pytest.raises(RuntimeError, match="identity mismatch"):
        cloud.main()
    assert calls == [("GET", "/instances/this-lab-only")]


@pytest.mark.parametrize("deleted", [True, False])
def test_release_verifies_gone_and_only_targets_recorded_id(cloud, monkeypatch, deleted):
    calls = []

    def api(method, path, body=None):
        calls.append((method, path))
        if method == "DELETE":
            return {}
        if len(calls) == 3 and deleted:
            raise cloud.VultrError(404, "not found")
        return {"instance": {"label": cloud.LABEL}}

    monkeypatch.setattr(cloud, "api", api)
    cloud.main()
    assert calls == [
        ("GET", "/instances/this-lab-only"),
        ("DELETE", "/instances/this-lab-only"),
        ("GET", "/instances/this-lab-only"),
    ]
    assert json.loads(cloud.STATE.read_text())["status"] == ("released" if deleted else "deleting")
