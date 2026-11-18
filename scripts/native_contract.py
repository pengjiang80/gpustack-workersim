"""Run with the unmodified GPUStack 2.3.1 image to validate simulator wire contracts."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gpustack.schemas.models import ModelInstanceMemberReport  # noqa: E402
from gpustack.schemas.workers import WorkerCreate  # noqa: E402
from prometheus_client.parser import text_string_to_metric_families  # noqa: E402

from workersim.config import Config  # noqa: E402
from workersim.metrics import render  # noqa: E402
from workersim.model_metrics import SyntheticTraffic  # noqa: E402
from workersim.runtime import Record  # noqa: E402
from workersim.worker import VirtualWorker  # noqa: E402


def main():
    config = Config()
    fleet = SimpleNamespace(config=config, store=SimpleNamespace(workers={}))
    worker = VirtualWorker(fleet, config.workers()[0])
    creation = worker.status_payload() | {"name": worker.spec.name, "worker_version": "2.3.1"}
    WorkerCreate.model_validate(creation)
    preparation = {"restart_count": 0, "preparation": {"port": 40000, "ports": [40000]}}
    ModelInstanceMemberReport.model_validate(preparation)
    ModelInstanceMemberReport.model_validate(
        {
            "restart_count": 0,
            "workloads": [
                {
                    "name": "sim-1-0",
                    "worker_id": 1,
                    "state": "pending",
                    "dp_rank_placements": [
                        {
                            "dp_rank": 0,
                            "node_rank": 0,
                            "gpu_indexes": [0],
                            "api_port": 40000,
                            "ready": False,
                        }
                    ],
                }
            ],
        }
    )
    ModelInstanceMemberReport.model_validate(
        {
            "restart_count": 0,
            "observations": [
                {
                    "name": "sim-1-0",
                    "state": "running",
                    "rank_readiness": {"0": True},
                }
            ],
        }
    )
    worker.runtime.records[10] = Record(
        {"id": 10, "model_id": 1, "model_name": "test", "name": "test-instance"},
        generation=0,
        phase="running",
        traffic=SyntheticTraffic(10, 2, started=0),
    )
    families = list(text_string_to_metric_families(render(worker)))
    assert len(families) > 15
    names = {family.name: family.type for family in families}
    assert names["gpustack:prompt_tokens"] == "counter"
    assert names["gpustack:time_to_first_token_seconds"] == "histogram"
    print(
        json.dumps(
            {
                "native_contract": "passed",
                "worker_create": True,
                "preparation": True,
                "workload_registration": True,
                "workload_observations": True,
                "prometheus_families": len(families),
            }
        )
    )


if __name__ == "__main__":
    main()
