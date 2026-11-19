import pytest

from scripts.release_metadata import release_metadata


def test_release_separates_product_and_protocol_versions():
    data = release_metadata()
    assert data["version"] == "0.1.1"
    assert data["release_tag"] == "v0.1.1-gpustack2.3.1"
    assert data["tags"].splitlines() == [
        "gpustack/workersim:0.1.1",
        "gpustack/workersim:0.1.1-gpustack2.3.1",
    ]
    release_metadata("gpustack/workersim", event="push", ref=data["release_tag"])
    with pytest.raises(ValueError, match="Expected release tag"):
        release_metadata("gpustack/workersim", event="push", ref="v0.1.1")
    assert release_metadata("gpustack/workersim", update_latest=True)["tags"].endswith(
        ":latest"
    )


def test_image_override_remains_available_for_forks():
    assert release_metadata("example/workersim")["tags"].splitlines() == [
        "example/workersim:0.1.1", "example/workersim:0.1.1-gpustack2.3.1",
    ]
