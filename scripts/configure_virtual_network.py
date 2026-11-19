"""Configure reachable node aliases on a dedicated Linux host, outside the simulator."""

import argparse
import ipaddress
import json
import re
import subprocess
from pathlib import Path

OWNER = "gpustack-workersim"


def plan(cidr, count, routes, interface):
    network = ipaddress.ip_network(cidr)
    if not isinstance(network, ipaddress.IPv4Network) or not any(
        network.subnet_of(ipaddress.ip_network(private))
        for private in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    ):
        raise ValueError("Use an RFC1918 IPv4 subnet")
    if not 1 <= count <= 1000 or network.num_addresses - 2 < count:
        raise ValueError("Subnet must have enough usable addresses for 1..1000 nodes")
    if not re.fullmatch(r"workersim[a-z0-9]{0,6}", interface):
        raise ValueError("Interface must have a workersim prefix and at most 15 characters")
    for route in routes:
        destination = route.get("dst", "default")
        if destination == "default" or route.get("dev") == interface:
            continue
        existing = ipaddress.ip_network(destination, strict=False)
        if existing.version == 4 and network.overlaps(existing):
            raise ValueError(f"Subnet overlaps existing route {destination} on {route.get('dev')}")
    return [str(network.network_address + slot + 1) for slot in range(count)]


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cidr", default="10.250.0.0/24")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--interface", default="workersim0")
    parser.add_argument("--persist", action="store_true")
    args = parser.parse_args()
    routes = json.loads(command("ip", "-j", "-4", "route", "show", "table", "all"))
    addresses = plan(args.cidr, args.count, routes, args.interface)
    links = json.loads(command("ip", "-d", "-j", "link", "show"))
    link = next((item for item in links if item["ifname"] == args.interface), None)
    if link:
        if link.get("ifalias") != OWNER or link.get("linkinfo", {}).get("info_kind") != "dummy":
            raise RuntimeError(
                "Existing interface is not owned by WorkerSim; refusing to change it"
            )
    else:
        command("ip", "link", "add", args.interface, "type", "dummy")
        command("ip", "link", "set", "dev", args.interface, "alias", OWNER)
    command("ip", "link", "set", "dev", args.interface, "up")
    for address in addresses:
        command("ip", "address", "replace", address + "/32", "dev", args.interface)
    if args.persist:
        script = Path(__file__).resolve()
        if any(char.isspace() for char in str(script)):
            raise RuntimeError("Install script in a path without whitespace before persisting")
        start = (
            f"/usr/bin/python3 {script} --cidr {args.cidr} "
            f"--count {args.count} --interface {args.interface}"
        )
        unit = f"""[Unit]
Description=GPUStack WorkerSim node IP aliases
After=network.target
Before=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart={start}

[Install]
WantedBy=multi-user.target
"""
        Path("/etc/systemd/system/workersim-network.service").write_text(unit)
        command("systemctl", "daemon-reload")
        command("systemctl", "enable", "--now", "workersim-network.service")
    print(
        json.dumps(
            {
                "interface": args.interface,
                "count": len(addresses),
                "first": addresses[0],
                "last": addresses[-1],
                "persistent": args.persist,
            }
        )
    )


if __name__ == "__main__":
    main()
