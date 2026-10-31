from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from .collector import collect_shard
from .discovery import discover
from .publish import publish


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily India and remote technology jobs")
    sub = parser.add_subparsers(dest="command", required=True)
    discovery = sub.add_parser("discover")
    discovery.add_argument("--config", default="config")
    discovery.add_argument("--output", default="run")
    discovery.add_argument("--new-limit", type=int, default=80)
    discovery.add_argument("--mode", choices=["full", "smoke"], default="full")
    collect = sub.add_parser("collect")
    collect.add_argument("--plan", default="run/plan.json")
    collect.add_argument("--shard", required=True)
    collect.add_argument("--output", default="run/results")
    collect.add_argument("--concurrency", type=int, default=4)
    merge = sub.add_parser("publish")
    merge.add_argument("--plan", default="run/plan.json")
    merge.add_argument("--results", default="run/results")
    merge.add_argument("--root", default=".")
    merge.add_argument("--registry", default="run/registry.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "discover":
        plan = asyncio.run(discover(args.config, args.output, new_limit=args.new_limit, mode=args.mode))
        matrix = (Path(args.output) / "matrix.json").read_text()
        if os.getenv("GITHUB_OUTPUT"):
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as handle:
                handle.write(f"matrix={matrix}\n")
        print(json.dumps({"sources": plan["sources"], "shards": len(plan["shards"]),
                          "profiles": plan["discovered_profiles"], "errors": plan["discovery_errors"]}))
    elif args.command == "collect":
        result = asyncio.run(collect_shard(args.plan, args.shard, args.output, args.concurrency))
        print(json.dumps({"shard": result["shard"], "jobs": result["job_count"]}))
    else:
        print(json.dumps(publish(args.plan, args.results, root_dir=args.root, registry_path=args.registry)))


if __name__ == "__main__":
    main()
