"""Inject a local runtime fault into an isolated lab model and request native restart."""

import json
import time
import urllib.error
import urllib.request

from scenario_acceptance import LAB, api, rows


def main():
    model = next(m for m in rows("/models") if m["name"] == "workersim-040-hybrid")
    instance = next(
        i
        for i in rows("/model-instances")
        if i["model_id"] == model["id"] and i["state"] == "running"
    )
    with urllib.request.urlopen("http://127.0.0.1:20099/status", timeout=10) as response:
        status = json.load(response)
    worker = next(w for w in status["workers"] if w["worker_id"] == instance["worker_id"])
    request = urllib.request.Request(
        "http://127.0.0.1:20099/fault",
        method="POST",
        data=json.dumps(
            {"worker": worker["name"], "instance_id": instance["id"], "action": "fail-instance"}
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        assert response.status == 200
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current = api("GET", f"/model-instances/{instance['id']}")
        if current["state"] == "error":
            break
        time.sleep(0.2)
    else:
        raise TimeoutError("Server did not observe the injected runtime failure")
    endpoint = f"http://{instance['worker_ip']}:{instance['port']}/health"
    try:
        with urllib.request.urlopen(endpoint, timeout=3) as response:
            health = response.status
    except urllib.error.HTTPError as error:
        health = error.code
    except OSError:
        health = "connection closed"
    assert health != 200, "A failed simulated engine must not report healthy"
    restart = api("POST", f"/models/{model['id']}/restart")
    report = {
        "model_id": model["id"],
        "old_instance_id": instance["id"],
        "server_observed_error": current["state_message"],
        "failed_endpoint_status": health,
        "native_restart_phase": restart.get("phase"),
        "replacement_running": False,
    }
    (LAB / "acceptance-040" / "lifecycle.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == "__main__":
    main()
