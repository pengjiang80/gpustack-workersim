import pytest

from scripts.release_metadata import release_metadata


def test_release_separates_product_and_protocol_versions():
    data = release_metadata("gpustack/gpustack-workersim")
    assert data["version"] == "0.1.0"
    assert data["release_tag"] == "v0.1.0-gpustack2.3.1"
    assert data["tags"].splitlines() == [
        "gpustack/gpustack-workersim:0.1.0",
        "gpustack/gpustack-workersim:0.1.0-gpustack2.3.1",
    ]
    release_metadata("gpustack/gpustack-workersim", event="push", ref=data["release_tag"])
    with pytest.raises(ValueError, match="Expected release tag"):
        release_metadata("gpustack/gpustack-workersim", event="push", ref="v0.1.0")
    assert release_metadata("gpustack/gpustack-workersim", update_latest=True)["tags"].endswith(
        ":latest"
    )
