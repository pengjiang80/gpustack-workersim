"""Summarize immutable GLM-5.3 experiment evidence; never connect to the lab."""

import argparse
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

COMMIT = re.compile(
    r"^(\S+Z) .*Scheduled group member (\S+) \(role (prefill|decode)\) "
    r"to worker (\S+) gpu (\[[^\]]*\])$",
    re.MULTILINE,
)


def summarize(directory):
    result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    if "started_at" not in result:
        return result  # Infrastructure failures must remain in the result table.
    log = (directory / "gpustack.log").read_text(encoding="utf-8")
    started = datetime.fromisoformat(result["started_at"])
    commits = [
        {
            "timestamp": match[0],
            "name": match[1],
            "role": match[2],
            "worker": match[3],
            "gpu_indexes": json.loads(match[4]),
        }
        for match in COMMIT.findall(log)
    ]
    commit_roles = Counter(item["role"] for item in commits)
    exact = None
    if commit_roles == {"prefill": 5, "decode": 3} and len({i["name"] for i in commits}) == 8:
        exact = round(
            (
                max(datetime.fromisoformat(i["timestamp"]) for i in commits) - started
            ).total_seconds(),
            3,
        )
    runtime = json.loads((directory / "runtime.json").read_text(encoding="utf-8"))
    files = json.loads((directory / "final-files.json").read_text(encoding="utf-8"))
    instances_path = directory / "final-instances.json"
    if instances_path.exists():
        instances = json.loads(instances_path.read_text(encoding="utf-8"))
    else:
        instances = json.loads((directory / "latest.json").read_text(encoding="utf-8"))["instances"]
    result.update(
        simulator_version=runtime.get("version"),
        scheduler_commit_seconds=exact,
        commit_log_members=commits,
        commit_log_matches_expected_roles=commit_roles == {"prefill": 5, "decode": 3},
        hf_config_read_starts=log.count(
            "Trying to get pretrained config from SourceEnum.HUGGING_FACE"
        ),
        glm_claim_log_count=log.count("Calculated resource claim for model zai-org/GLM-5.3"),
        group_placed_log_count=log.count(f"Group {result['model_name']} placed"),
        protocol_errors=sum(w.get("report_errors", 0) for w in runtime["workers"]),
        stale_reports=sum(w.get("stale_reports", 0) for w in runtime["workers"]),
        instance_watches=sum(w.get("watch_connected", False) for w in runtime["workers"]),
        file_watches=sum(w.get("file_watch_connected", False) for w in runtime["workers"]),
        files_ready=len(files) > 0
        and all(f["state"] == "ready" and f["download_progress"] == 100 for f in files),
        files_count=len(files),
        final_running_roles=dict(
            Counter(i.get("role") for i in instances if i["state"] == "running")
        ),
        final_instance_states=dict(Counter(i["state"] for i in instances)),
        final_instance_messages=[
            {k: i.get(k) for k in ("id", "role", "state", "state_message")}
            for i in instances
            if i["state"] != "running"
        ],
        usage_probe_validation_error="Error processing model usage" in log,
    )
    model_path = directory / "final-model.json"
    final_model = json.loads(model_path.read_text(encoding="utf-8")) if model_path.exists() else {}
    registry_path = directory / "final-router-registry.json"
    registry = (
        json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists() else {}
    )
    expected = {
        (f"http://{i['worker_ip']}:{i['port']}", i["role"])
        for i in instances
        if i.get("role") in ("prefill", "decode") and i.get("worker_ip") and i.get("port")
    }
    registered = {(row["url"], row["worker_type"]) for row in registry.get("workers", [])}
    result["strict_model_ready"] = (
        final_model.get("state") == "running" and len(expected) == 8 and expected == registered
    )
    result["final_model_degradations"] = final_model.get("degradations")
    if result["result"] in ("success", "success_with_errors"):
        result["success_evidence_verified"] = all(
            (
                len(commits) == 8,
                len({i["worker"] for i in commits}) == 8,
                all(i["gpu_indexes"] == list(range(8)) for i in commits),
                result["final_running_roles"] == {"prefill": 5, "decode": 3, "router": 1},
                result["allocated_gpus"] == 64,
                result["ready"]["ready_workers"] == result["workers"],
                result["ready"]["gpus"] == result["workers"] * 8,
                result["strict_model_ready"],
                exact is not None
                and result["gpu_assignment_lower_bound_seconds"]
                <= exact
                <= result["gpu_assignment_seconds"],
            )
        )
    samples = []
    for line in (directory / "resources.jsonl").read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        for item in record["docker"].splitlines():
            row = json.loads(item)
            if row["Name"] == "gpustack":
                samples.append(
                    {
                        "elapsed": record["elapsed_seconds"],
                        "cpu_percent": float(row["CPUPerc"].rstrip("%")),
                        "memory_usage": row["MemUsage"],
                    }
                )
    result["server_resource_samples"] = samples
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    results = []
    for directory in sorted(args.directory.glob("workers-*")):
        if (directory / "result.json").exists() and json.loads(
            (directory / "result.json").read_text(encoding="utf-8")
        ).get("result") != "observing":
            result = summarize(directory)
            results.append(result)
            print(
                json.dumps(
                    {
                        k: result.get(k)
                        for k in (
                            "workers",
                            "result",
                            "scheduler_commit_seconds",
                            "gpu_assignment_seconds",
                            "all_running_seconds",
                            "model_ready_seconds",
                            "allocated_gpus",
                            "hf_config_read_starts",
                            "protocol_errors",
                            "files_ready",
                            "success_evidence_verified",
                            "strict_model_ready",
                        )
                    }
                )
            )
    output = {
        "generated_at": datetime.now(UTC).isoformat(),
        "results": results,
        "outcomes": dict(Counter(r["result"] for r in results)),
        "timing_note": "Scheduler: post-commit native logs. API: monotonic polling.",
    }
    (args.directory / "summary.json").write_text(
        json.dumps(output, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
