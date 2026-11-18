"""Check actual Server state, native gateway responses and synthetic source metrics."""

import json
import urllib.error
import urllib.request
from collections import Counter

from scenario_acceptance import LAB, rows


def main():
    models, instances, workers, files = (
        rows(p) for p in ("/models", "/model-instances", "/workers", "/model-files")
    )
    selected = [m for m in models if m["name"].startswith("workersim-040-") or m["id"] == 2]
    pending = []
    for model in selected:
        members = [i for i in instances if i["model_id"] == model["id"]]
        desired = sum(r["replicas"] for r in model.get("roles") or []) or model["replicas"]
        if len(members) != desired or any(i["state"] != "running" for i in members):
            pending.append(
                {"name": model["name"], "states": dict(Counter(i["state"] for i in members))}
            )
    if pending:
        print(json.dumps({"pending": pending}))
        return
    assert all(f["state"] == "ready" and f["download_progress"] == 100 for f in files)
    secret = json.loads((LAB / "secrets.json").read_text())
    gateway = []
    for model in selected:
        request = urllib.request.Request(
            "http://127.0.0.1:18080/v1/chat/completions",
            method="POST",
            data=json.dumps(
                {
                    "model": model["name"],
                    "messages": [{"role": "user", "content": "simulated smoke test"}],
                }
            ).encode(),
            headers={
                "Authorization": "Bearer " + secret["api_key"],
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.load(response)
                assert data.get("simulated") is True
                gateway.append(
                    {"model": model["name"], "status": response.status, "simulated": True}
                )
        except urllib.error.HTTPError as error:
            reason = error.read().decode(errors="replace").replace(secret["api_key"], "[redacted]")
            raise RuntimeError(
                f"Gateway {model['name']} HTTP {error.code}: {reason[:400]}"
            ) from None
    with urllib.request.urlopen("http://127.0.0.1:20099/status", timeout=20) as response:
        status = json.load(response)
    assert len(status["workers"]) == 100
    assert all(w["watch_connected"] and w["file_watch_connected"] for w in status["workers"])
    result = {
        "version": status["version"],
        "models_running": len(selected),
        "main_workers_ready": sum(
            w["state"] == "ready" and w["name"].startswith("sim-b300-") for w in workers
        ),
        "instance_members_running": sum(i["state"] == "running" for i in instances),
        "model_files_ready": len(files),
        "gateway": gateway,
        "model_watch_connected": sum(w["watch_connected"] for w in status["workers"]),
        "file_watch_connected": sum(w["file_watch_connected"] for w in status["workers"]),
        "report_errors": sum(w["report_errors"] for w in status["workers"]),
        "stale_reports": sum(w["stale_reports"] for w in status["workers"]),
    }
    path = LAB / "acceptance-040" / "verification.json"
    path.write_text(json.dumps(result, indent=2))
    lifecycle = LAB / "acceptance-040" / "lifecycle.json"
    if lifecycle.exists():
        value = json.loads(lifecycle.read_text())
        replacement = [i for i in instances if i["model_id"] == value["model_id"]]
        assert replacement and all(
            i["id"] != value["old_instance_id"] and i["state"] == "running" for i in replacement
        )
        value.update(
            replacement_running=True, replacement_instance_ids=[i["id"] for i in replacement]
        )
        lifecycle.write_text(json.dumps(value, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
