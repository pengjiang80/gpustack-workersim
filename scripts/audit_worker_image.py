"""Record the actual Worker sources shipped in the Server image before acceptance."""

import hashlib
import json
import subprocess
from importlib.resources import files


def audit_image(image, output):
    reference = json.loads(
        files("workersim").joinpath("data/native_worker_sources.json").read_text()
    )
    names = list(reference["sha256"])
    command = (
        "import importlib.util,json,pathlib,sys;"
        "root=pathlib.Path(importlib.util.find_spec('gpustack').origin).parent;"
        "print(json.dumps({n:(root/n).read_text() for n in json.loads(sys.argv[1])}))"
    )
    raw = subprocess.check_output(
        ["docker", "run", "--rm", "--entrypoint", "python3", image,
         "-c", command, json.dumps(names)], text=True, stderr=subprocess.PIPE,
    )
    sources = json.loads(raw)
    result = {
        "reference_commit": reference["target_commit"],
        "sha256": {name: hashlib.sha256(text.encode()).hexdigest()
                   for name, text in sources.items()},
    }
    result["reference_matches"] = {
        name: digest == reference["sha256"][name] for name, digest in result["sha256"].items()
    }
    (output / "native-worker-source.json").write_text(json.dumps(sources))
    (output / "native-source-audit.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
