"""Create/release only this lab's Vultr Shared CPU instance; never print credentials."""

import argparse
import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / ".workersim" / "vultr.json"
LABEL = "gpustack-workersim-231-lab"


class VultrError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def api(method, path, body=None):
    key = os.environ.get("VULTR_API_KEY")
    if not key:
        content = (ROOT.parent / "Info_Shared" / "Vultr.txt").read_text(encoding="utf-8-sig")
        lines = "\n".join(line for line in content.splitlines() if "ssh-" not in line)
        candidates = re.findall(r"(?<![A-Za-z0-9+/])[A-Za-z0-9]{30,100}(?![A-Za-z0-9+/])", lines)
        if len(candidates) != 1:
            raise RuntimeError("Set VULTR_API_KEY; credential file has ambiguous key format")
        key = candidates[0]
    request = urllib.request.Request(
        "https://api.vultr.com/v2" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = response.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as error:
        try:
            reason = json.loads(error.read()).get("error", "")
            reason = str(reason).replace(key, "[redacted]")[:160]
        except (ValueError, AttributeError):
            reason = ""
        raise VultrError(
            error.code, f"Vultr {method} {path} returned HTTP {error.code}: {reason}"
        ) from None


def inventory():
    plans = api("GET", "/plans?per_page=500")["plans"]
    plans = [
        p
        for p in plans
        if p.get("type") in ("vc2", "vhf", "vhp")
        and p["vcpu_count"] >= 8
        and 16000 <= p["ram"] <= 32768
    ]
    operating_systems = api("GET", "/os?per_page=100")["os"]
    systems = [s for s in operating_systems if "Ubuntu 24.04" in s["name"] and s["arch"] == "x64"]
    keys = api("GET", "/ssh-keys")["ssh_keys"]
    selected_keys = [k for k in keys if k["name"].lower() == "peng-xps"]
    if len(selected_keys) != 1:
        raise RuntimeError("Expected exactly one Vultr SSH key named peng-xps")
    public_path = Path.home() / ".ssh" / "id_ed25519.pub"
    local_public = public_path.read_text().split()[:2]
    if selected_keys[0]["ssh_key"].split()[:2] != local_public:
        raise RuntimeError("peng-xps does not match the local id_ed25519 public key")
    return plans, systems, selected_keys[0]["id"]


def create(region):
    if STATE.exists() and json.loads(STATE.read_text()).get("status") != "released":
        raise RuntimeError("A recorded lab instance already exists; inspect status first")
    existing = api("GET", "/instances?per_page=100")["instances"]
    if any(item.get("label") == LABEL for item in existing):
        raise RuntimeError("A same-label lab instance exists; reconcile the local ledger first")
    plans, systems, key_id = inventory()
    region_plans = api("GET", f"/regions/{region}/availability")["available_plans"]
    available = [p for p in plans if p["id"] in region_plans and p["vcpu_count"] >= 8]
    if not available:
        raise RuntimeError("No 8-core Shared CPU plan available in requested region")
    plan = min(available, key=lambda p: p["monthly_cost"])
    if not systems:
        raise RuntimeError("Ubuntu 24.04 x64 OS not found")
    bootstrap = """#cloud-config
package_update: true
packages:
  - docker.io
  - python3-venv
  - curl
runcmd:
  - [systemctl, enable, --now, docker]
"""
    result = api(
        "POST",
        "/instances",
        {
            "region": region,
            "plan": plan["id"],
            "os_id": systems[0]["id"],
            "sshkey_id": [key_id],
            "label": LABEL,
            "hostname": LABEL,
            "tags": ["workersim", "gpustack-2.3.1", "temporary-test"],
            "backups": "disabled",
            "enable_ipv6": False,
            "user_data": base64.b64encode(bootstrap.encode()).decode(),
        },
    )["instance"]
    state = {
        k: result.get(k) for k in ("id", "main_ip", "status", "region", "plan", "date_created")
    }
    state.update(
        label=LABEL, hourly_cost=plan.get("hourly_cost"), purpose="GPUStack 2.3.1 WorkerSim test"
    )
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    print(json.dumps(state))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inventory", "create", "status", "release"])
    parser.add_argument("--region", default="sgp")
    args = parser.parse_args()
    if args.action == "inventory":
        plans, systems, _ = inventory()
        print(json.dumps({"plans": plans, "os": systems, "ssh_key_matches_local": True}))
        return
    if args.action == "create":
        create(args.region)
        return
    state = json.loads(STATE.read_text())
    if state["label"] != LABEL or not state.get("id"):
        raise RuntimeError("Lab resource identity mismatch")
    if state["status"] == "released":
        print("Lab instance already released")
        return
    try:
        instance = api("GET", "/instances/" + state["id"])["instance"]
    except VultrError as error:
        if state["status"] != "deleting" or error.status != 404:
            raise
        state["status"] = "released"
        STATE.write_text(json.dumps(state, indent=2) + "\n")
        print(json.dumps(state))
        return
    if instance["label"] != LABEL:
        raise RuntimeError("Remote resource identity mismatch; refusing to modify it")
    if args.action == "release":
        api("DELETE", "/instances/" + state["id"])
        try:
            api("GET", "/instances/" + state["id"])
        except VultrError as error:
            if error.status != 404:
                raise
            state["status"] = "released"
        else:
            state["status"] = "deleting"
    else:
        state.update(
            {k: instance.get(k) for k in ("main_ip", "status", "server_status", "power_status")}
        )
    STATE.write_text(json.dumps(state, indent=2) + "\n")
    print(json.dumps(state))


if __name__ == "__main__":
    main()
