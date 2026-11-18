"""Reserve shared-host inference ports against Linux automatic source-port allocation.

Run on a dedicated Linux test host, outside the unprivileged simulator container.
Existing reserved ranges are retained. Dry-run unless --apply is specified.
"""

import argparse
import json
import subprocess
from pathlib import Path


def merge_ranges(value, low, high):
    intervals = [(low, high)]
    for item in filter(None, value.strip().split(",")):
        first, _, last = item.partition("-")
        intervals.append((int(first), int(last or first)))
    if any(not 1 <= a <= b <= 65535 for a, b in intervals):
        raise ValueError("Invalid reserved port range")
    merged = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in merged)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=100)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.workers <= 100:
        raise ValueError("Worker count must be 1..100")
    key = "net.ipv4.ip_local_reserved_ports"
    old = subprocess.check_output(["sysctl", "-n", key], text=True).strip()
    new = merge_ranges(old, 40000, 40000 + args.workers * 64 - 1)
    path = Path("/etc/sysctl.d/90-gpustack-workersim-ports.conf")
    owner = "# Owned by GPUStack WorkerSim test-host setup\n"
    if args.apply:
        if path.exists() and not path.read_text().startswith(owner):
            raise RuntimeError("Refusing to replace an unowned sysctl file")
        subprocess.run(["sysctl", "-w", f"{key}={new}"], check=True, stdout=subprocess.DEVNULL)
        path.write_text(owner + f"{key} = {new}\n")
    print(
        json.dumps(
            {
                "before": old,
                "after": new,
                "applied": args.apply,
                "existing_connections": "Existing connections/TIME_WAIT are not removed",
            }
        )
    )


if __name__ == "__main__":
    main()
