"""Command-line entry point for fleet control and real Server smoke tests."""

import argparse
import asyncio
import json
import logging
import sys

import aiohttp

from workersim import __version__
from workersim.config import load_config
from workersim.fleet import Fleet
from workersim.scenario import run_scenario


async def execute(args):
    config = load_config(args.config)
    if args.command == "validate":
        print(
            json.dumps(
                {
                    "valid": True,
                    "workers": len(config.workers()),
                    "gpus": sum(w.group.gpu_count for w in config.workers()),
                    "target_version": "2.3.1",
                },
                indent=2,
            )
        )
    elif args.command == "run":
        from workersim.quickstart import drop_privileges

        drop_privileges(config.simulation.data_dir)
        await Fleet(config).run()
    elif args.command == "scenario":
        result = await run_scenario(config, args.replicas, args.timeout, args.output)
        print(json.dumps(result, indent=2))
    else:
        url = f"http://127.0.0.1:{config.network.control_port}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            if args.command == "status":
                response = await session.get(url + "/status")
            elif args.command == "fleet-scale":
                response = await session.post(url + "/scale", json={"count": args.count})
            else:
                response = await session.post(url + "/stop")
            async with response:
                response.raise_for_status()
                print(json.dumps(await response.json(), indent=2))


def native_main(argv):
    """GPUStack Add Worker style: `workersim --server-url ... [--workers N --profile P]`."""
    from workersim.quickstart import start

    logging.basicConfig(
        level=logging.DEBUG if "--verbose" in argv else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    try:
        raise SystemExit(asyncio.run(start(argv)))
    except KeyboardInterrupt:
        pass
    except (ValueError, RuntimeError, OSError) as error:
        print(f"workersim: {str(error) or type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


def main():
    from workersim.quickstart import is_native_invocation

    if is_native_invocation(sys.argv[1:]):
        return native_main(sys.argv[1:])
    parser = argparse.ArgumentParser(
        description="Independent CPU-only GPUStack 2.3.1 Worker simulator"
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", "-c", default="configs/local.yaml")
    parser.add_argument("--verbose", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "run", "status", "stop"):
        commands.add_parser(command)
    scale = commands.add_parser(
        "fleet-scale", help="Enable N of the configured Workers; others simulate offline nodes"
    )
    scale.add_argument("count", type=int)
    scenario = commands.add_parser(
        "scenario", help="Create, scale and delete a model on a real Server"
    )
    scenario.add_argument("--replicas", nargs="+", type=int, default=[2, 5, 1, 0])
    scenario.add_argument("--timeout", type=float, default=180)
    scenario.add_argument("--output", default="outputs/scenario.json")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # aiohttp does not log credentials; suppress third-party debug request output.
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    try:
        asyncio.run(execute(args))
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(f"workersim: {str(error) or type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
