"""Create a source-only archive with an explicit allowlist, excluding credentials and caches."""

import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workersim import __version__  # noqa: E402


def main():
    output = ROOT / "dist" / f"gpustack-workersim-{__version__}-source.tar.gz"
    output.parent.mkdir(exist_ok=True)
    roots = ["workersim", "tests", "scripts", "configs", "docs", ".github"]
    files = [
        "README.md",
        "README.zh-CN.md",
        "LICENSE",
        "Dockerfile",
        "compose.yaml",
        "pyproject.toml",
        "uv.lock",
        "requirements.lock",
        ".gitignore",
        ".dockerignore",
    ]
    for directory in roots:
        base = ROOT / directory
        if base.exists():
            files.extend(
                str(path.relative_to(ROOT))
                for path in base.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
            )
    with tarfile.open(output, "w:gz") as archive:
        for name in sorted(files):
            archive.add(ROOT / name, arcname=name, recursive=False)
    print(f"Source archive: {output} ({len(files)} files, {output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
