"""Release naming: independent WorkerSim version plus explicit GPUStack compatibility."""

import json
import os
import re
import tomllib
from pathlib import Path

from workersim import TARGET_COMMIT, TARGET_VERSION, __version__

DEFAULT_IMAGE = "gpustack/workersim"


def release_metadata(
    image=DEFAULT_IMAGE, *, event="workflow_dispatch", ref="", update_latest=False
):
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    if version != __version__ or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Package and runtime versions must match and have three numeric parts")
    if not re.fullmatch(r"\d+\.\d+\.\d+", TARGET_VERSION):
        raise ValueError("GPUStack target must have three numeric parts")
    tag = f"v{version}-gpustack{TARGET_VERSION}"
    if event == "push" and ref != tag:
        raise ValueError(f"Expected release tag {tag}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9._-]*", image):
        raise ValueError("Set DOCKERHUB_IMAGE to namespace/repository")
    tags = [f"{image}:{version}", f"{image}:{tag[1:]}"]
    if update_latest:
        tags.append(f"{image}:latest")
    return {
        "version": version,
        "target_version": TARGET_VERSION,
        "target_commit": TARGET_COMMIT,
        "release_tag": tag,
        "title": f"WorkerSim {version} / GPUStack {TARGET_VERSION}",
        "tags": "\n".join(tags),
    }


def main():
    if not os.environ.get("LOGIN"):
        raise ValueError("Set DOCKERHUB_USERNAME")
    data = release_metadata(
        os.environ.get("IMAGE") or DEFAULT_IMAGE,
        event=os.environ["EVENT"],
        ref=os.environ["REF"],
        update_latest=os.environ.get("UPDATE_LATEST") == "true",
    )
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
        for key, value in data.items():
            stream.write(f"{key}<<METADATA\n{value}\nMETADATA\n")
    print(json.dumps(data))


if __name__ == "__main__":
    main()
