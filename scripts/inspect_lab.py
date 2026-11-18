"""On-host, read-only lab inspection with credentials omitted from output."""

import json
import urllib.request
from pathlib import Path


def main():
    secrets = json.loads(Path("/opt/workersim/lab/secrets.json").read_text())

    def get(path):
        request = urllib.request.Request(
            "http://127.0.0.1:18080/v2" + path,
            headers={"Authorization": "Bearer " + secrets["api_key"]},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    report = {}
    selections = {
        "workers": ("id", "name", "state", "unreachable", "state_message"),
        "models": ("id", "name", "state", "state_message", "ready_replicas", "replicas"),
        "model-instances": (
            "id",
            "name",
            "state",
            "state_message",
            "worker_id",
            "port",
            "ports",
            "download_progress",
            "resolved_path",
            "restart_count",
        ),
    }
    for resource, keys in selections.items():
        data = get("/" + resource + "?perPage=100")
        report[resource] = [{key: item.get(key) for key in keys} for item in data["items"]]
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
