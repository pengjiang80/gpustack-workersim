"""Validate native scale-down, zero replicas, resource retirement and restoration."""

import json
import time
import urllib.request

from scenario_acceptance import LAB, api, rows


def main():
    model = next(m for m in rows("/models") if m["name"] == "workersim-040-single")
    stages = []
    for count in (4, 1, 0, 1):
        model = api("GET", f"/models/{model['id']}")
        if model["replicas"] != count:
            api("PUT", f"/models/{model['id']}", model | {"replicas": count})
        deadline = time.monotonic() + 120
        started = time.monotonic()
        while time.monotonic() < deadline:
            model = api("GET", f"/models/{model['id']}")
            instances = api("GET", f"/model-instances?model_id={model['id']}&perPage=100")["items"]
            with urllib.request.urlopen("http://127.0.0.1:20099/status", timeout=15) as response:
                status = json.load(response)
            local = [
                i
                for w in status["workers"]
                for i in w["assignments"]
                if i["model_id"] == model["id"]
            ]
            if (
                model["ready_replicas"] == count
                and len(instances) == count
                and all(i["state"] == "running" for i in instances)
                and len(local) == count
            ):
                break
            time.sleep(1)
        else:
            raise TimeoutError(f"Scale to {count} did not converge")
        stages.append(
            {
                "replicas": count,
                "ready": model["ready_replicas"],
                "instance_ids": [i["id"] for i in instances],
                "local_assignments": len(local),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
        )
        print(json.dumps(stages[-1]), flush=True)
    (LAB / "acceptance-040" / "scaling.json").write_text(json.dumps(stages, indent=2))


if __name__ == "__main__":
    main()
