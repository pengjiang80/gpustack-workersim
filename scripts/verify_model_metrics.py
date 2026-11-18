"""Read-only verification against the live Server, Prometheus and original dashboard queries."""

import json
import re
import subprocess
import urllib.request
from pathlib import Path

LAB = Path("/opt/workersim/lab")


def main():
    secret = json.loads((LAB / "secrets.json").read_text())

    def get(path):
        request = urllib.request.Request(
            "http://127.0.0.1:18080" + path,
            headers={"Authorization": "Bearer " + secret["api_key"]},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    def query(expression):
        result = subprocess.check_output(
            [
                "docker",
                "exec",
                "gpustack",
                "curl",
                "-sf",
                "-G",
                "http://127.0.0.1:19090/prometheus/api/v1/query",
                "--data-urlencode",
                "query=" + expression,
            ],
            text=True,
        )
        data = json.loads(result)
        assert data["status"] == "success", data
        return data["data"]["result"]

    workers = get("/v2/workers?perPage=100")["items"]
    expected = {f"10.250.0.{index}" for index in range(1, 101)}
    assert {worker["ip"] for worker in workers} == expected
    assert all(worker["advertise_address"] == worker["ip"] for worker in workers)
    assert all(worker["state"] == "ready" for worker in workers)
    model = next(
        m for m in get("/v2/models?perPage=100")["items"] if m["name"] == "workersim-ui-proof"
    )
    instances = get(f"/v2/model-instances?model_id={model['id']}&perPage=100")["items"]
    assert all(instance["worker_ip"] in expected for instance in instances)
    dashboard = get("/grafana/api/dashboards/uid/gpustack-model")["dashboard"]
    queries = []

    def collect(panels):
        for panel in panels:
            queries.extend(
                (panel.get("title", ""), t["expr"])
                for t in panel.get("targets", [])
                if t.get("expr")
            )
            collect(panel.get("panels", []))

    collect(dashboard.get("panels", []))
    core = {
        "Running Requests",
        "Waiting Requests",
        "Model Token Generation Throughput",
        "End-to-End Request Latency",
        "Token Throughput",
        "Time To First Token Latency",
        "Source Scrape Status",
    }
    values = {
        "scope_cluster_id": f'"{model["cluster_id"]}"',
        "scope_model_id": f'"{model["id"]}"',
        "scope_bound": "1",
        "scope_legacy_enabled": "0",
        "selected_cluster_id": str(model["cluster_id"]),
        "selected_model_id": str(model["id"]),
        "selected_model_instance_id": ".*",
        "cluster_name": "workersim-lab",
        "model_name": model["name"],
        "model_instance_name": ".*",
    }

    def expand(expression):
        expression = expression.replace("$__rate_interval", "1m").replace("$__interval", "15s")
        return re.sub(
            r"\$\{([^}:]+)(?::[^}]+)?\}",
            lambda match: values.get(match[1], '".*"' if match[1].startswith("scope_") else ".*"),
            expression,
        )

    results = []
    for title, expression in queries:
        if title in core:
            data = query(expand(expression))
            results.append({"title": title, "series": len(data), "values": data})
    assert results and all(result["series"] > 0 for result in results), results
    assert all(
        str(sample["value"][1]) != "NaN" for result in results for sample in result["values"]
    )
    instance = instances[0]
    values["scope_model_instance_id"] = f'"{instance["id"]}"'
    title, expression = next((t, e) for t, e in queries if t == "Token Throughput")
    scoped = query(expand(expression))
    assert scoped and all(
        item["metric"].get("model_instance_id") == str(instance["id"]) for item in scoped
    )
    report = {
        "workers_ready": len(workers),
        "unique_ips": len(expected),
        "ip_range": ["10.250.0.1", "10.250.0.100"],
        "model_id": model["id"],
        "ready_replicas": model["ready_replicas"],
        "replicas": model["replicas"],
        "native_dashboard_queries": results,
        "single_instance_filter": {"id": instance["id"], "series": len(scoped)},
        "gpu_series": query("count(gpustack:worker_node_gpu_utilization_rate)"),
        "note": "Synthetic background traffic; no inference was performed.",
    }
    (LAB / "reports" / "model-metrics-verification.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: v for k, v in report.items() if k != "native_dashboard_queries"}))
    print(
        json.dumps([{k: value for k, value in item.items() if k != "values"} for item in results])
    )


if __name__ == "__main__":
    main()
