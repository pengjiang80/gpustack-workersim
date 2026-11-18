"""Fill the owned 50x8 B300 demo cluster with varied, explicitly simulated models."""

import json

from demo2_lab import ROOT, Api

PRIVATE = ROOT / ".workersim/private-demo2"


def main():
    api = Api("http://demo2.gpustack.cn:8080", ROOT.parent / "Info_Shared/GPUStack.txt")
    cluster_id = json.loads((PRIVATE / "metadata.json").read_text())["cluster_id"]
    base_names = {
        "workersim-deepseek-v3.2",
        "workersim-glm-5.3",
        "workersim-kimi-k2.5",
        "workersim-qwen3.5-397b-a17b",
        "workersim-bge-m3",
        "workersim-bge-reranker-v2-m3",
        "workersim-qwen3-asr-0.6b",
        "workersim-qwen3-tts-12hz-1.7b-customvoice",
    }
    original = [
        m
        for m in json.loads((PRIVATE / "model-manifest.json").read_text())
        if m["name"] in base_names
    ]
    assert len(original) == 8
    for model in original:
        if model["categories"] == ["llm"]:
            model["replicas"] = 10
    additions = []
    for name, repo, category, replicas, tp in (
        ("qwen3-32b", "Qwen/Qwen3-32B", "llm", 16, 1),
        ("qwen3-8b", "Qwen/Qwen3-8B", "llm", 16, 1),
        ("deepseek-r1-distill-qwen-32b", "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B", "llm", 12, 1),
        ("qwen3-vl-32b-instruct", "Qwen/Qwen3-VL-32B-Instruct", "llm", 4, 2),
        ("qwen3-embedding-8b", "Qwen/Qwen3-Embedding-8B", "embedding", 8, 1),
        ("qwen3-reranker-8b", "Qwen/Qwen3-Reranker-8B", "reranker", 8, 1),
    ):
        additions.append(
            {
                "name": "workersim-" + name,
                "source": "huggingface",
                "huggingface_repo_id": repo,
                "categories": [category],
                "replicas": replicas,
                "backend": "vLLM",
                "backend_version": "0.33.0",
                "backend_parameters": [
                    f"--tensor-parallel-size={tp}",
                    "--gpu-memory-utilization=0.9",
                    "--max-model-len=8192",
                    "--trust-remote-code",
                ],
            }
        )
    for category, repo, name in (
        ("speech_to_text", "Qwen/Qwen3-ASR-1.7B", "qwen3-asr-1.7b"),
        (
            "text_to_speech",
            "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
            "qwen3-tts-12hz-0.6b-customvoice",
        ),
    ):
        template = next(m for m in original if m["categories"] == [category])
        params = [p for p in template["backend_parameters"] if "gpu-memory-utilization" not in p]
        additions.append(
            template
            | {
                "name": "workersim-" + name,
                "huggingface_repo_id": repo,
                "backend_parameters": params + ["--gpu-memory-utilization=0.9"],
                "replicas": 2,
            }
        )
    # Order the four full-host deployments first, then fill remaining cards with smaller models.
    manifest = original + additions
    assert len(manifest) == 16
    manifest_path = PRIVATE / "model-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    schemas = api.call("GET", "/openapi.json")["components"]["schemas"]
    allowed = set(schemas["ModelUpdate"]["properties"]) - {"ready_replicas"}
    existing = api.items("/v2/models")
    for model in manifest:
        model.update(
            cluster_id=cluster_id,
            placement_strategy="spread",
            cpu_offloading=False,
            description="WorkerSim: synthetic deployment and traffic, no real inference.",
            enable_model_route=True,
        )
        matches = [m for m in existing if m["name"] == model["name"]]
        if matches:
            current = matches[0]
            if current["cluster_id"] != cluster_id:
                raise RuntimeError("Refusing to update an unrelated model")
            if current["replicas"] != model["replicas"]:
                body = {k: v for k, v in current.items() if k in allowed}
                body["replicas"] = model["replicas"]
                result = api.call("PUT", f"/v2/models/{current['id']}", body)
            else:
                result = current
        else:
            result = api.call("POST", "/v2/models", model)
        print(
            json.dumps(
                {"id": result["id"], "name": result["name"], "replicas": result["replicas"]}
            ),
            flush=True,
        )
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
