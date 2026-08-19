"""Command-line entry point for the minimal trace-guided CI selector."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

from test_selection.graph import (
    build_graph,
    current_jobs_from_pipeline,
    graph_metadata,
    job_coverage,
    paths_to_jobs,
    select_jobs,
)
from test_selection.snapshot import (
    Boto3ObjectStore,
    build_and_publish,
    fetch_snapshot,
)
from test_selection.wait import wait_for_steps


def _print(document: Dict[str, Any]) -> None:
    print(json.dumps(document, sort_keys=True, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vllm-test-selection")
    commands = parser.add_subparsers(dest="command", required=True)

    materialize = commands.add_parser("build-graph")
    materialize.add_argument("--input", type=Path, required=True)
    materialize.add_argument("--output", type=Path, required=True)

    inventory = commands.add_parser("current-jobs")
    inventory.add_argument("--pipeline", type=Path, required=True)
    inventory.add_argument("--output", type=Path, required=True)

    coverage = commands.add_parser("job-coverage")
    coverage.add_argument("--graph", type=Path, required=True)
    coverage.add_argument("--current-jobs", type=Path, required=True)

    why = commands.add_parser("why")
    why.add_argument("--graph", type=Path, required=True)
    why.add_argument("--source", required=True)

    select = commands.add_parser("select")
    select.add_argument("--graph", type=Path, required=True)
    select.add_argument("--repo", type=Path, required=True)
    select.add_argument("--base", required=True)
    select.add_argument("--head", required=True)
    select.add_argument("--current-jobs", type=Path, required=True)
    select.add_argument("--max-snapshot-age-days", type=int, default=11)
    select.add_argument("--format", choices=("json", "step-keys-json"), default="json")

    inspect = commands.add_parser("inspect-graph")
    inspect.add_argument("--graph", type=Path, required=True)
    inspect.add_argument("--metadata", action="store_true")

    publish = commands.add_parser("publish-snapshot")
    publish.add_argument("--input", type=Path, required=True)
    publish.add_argument("--inventory", type=Path, required=True)
    publish.add_argument("--bucket", required=True)
    publish.add_argument("--prefix", default="test-selection/vllm")

    wait = commands.add_parser("wait-for-steps")
    wait.add_argument("--inventory", type=Path, required=True)
    wait.add_argument("--timeout-seconds", type=float, required=True)
    wait.add_argument("--poll-seconds", type=float, default=120)

    fetch = commands.add_parser("fetch-snapshot")
    fetch.add_argument("--bucket", required=True)
    fetch.add_argument("--prefix", default="test-selection/vllm")
    fetch.add_argument("--repo", type=Path, required=True)
    fetch.add_argument("--base", required=True)
    fetch.add_argument("--output", type=Path, required=True)
    fetch.add_argument("--max-snapshot-age-days", type=int, default=11)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "build-graph":
            _print(build_graph(args.input, args.output))
        elif args.command == "current-jobs":
            keys = current_jobs_from_pipeline(args.pipeline)
            temporary = args.output.with_suffix(args.output.suffix + ".tmp")
            temporary.write_text(
                json.dumps(keys, separators=(",", ":")) + "\n", encoding="utf-8"
            )
            temporary.replace(args.output)
            _print({"count": len(keys), "output": str(args.output)})
        elif args.command == "job-coverage":
            _print(job_coverage(args.graph, args.current_jobs))
        elif args.command == "why":
            paths = paths_to_jobs(args.graph, args.source)
            _print({"paths": paths, "source": args.source})
            if not paths:
                return 2
        elif args.command == "select":
            result = select_jobs(
                args.graph,
                args.repo,
                args.base,
                args.head,
                args.current_jobs,
                args.max_snapshot_age_days,
            )
            if args.format == "step-keys-json":
                if result["fallback"]:
                    print("null")
                    return 2
                print(json.dumps(result["step_keys"], separators=(",", ":")))
            else:
                _print(result)
            if result["fallback"]:
                return 2
        elif args.command == "inspect-graph":
            _print(graph_metadata(args.graph))
        elif args.command == "publish-snapshot":
            _print(
                build_and_publish(
                    args.input,
                    args.inventory,
                    Boto3ObjectStore(args.bucket),
                    args.prefix,
                )
            )
        elif args.command == "wait-for-steps":
            _print(
                wait_for_steps(
                    args.inventory,
                    timeout_seconds=args.timeout_seconds,
                    poll_seconds=args.poll_seconds,
                )
            )
        elif args.command == "fetch-snapshot":
            _print(
                fetch_snapshot(
                    Boto3ObjectStore(args.bucket),
                    args.prefix,
                    args.repo,
                    args.base,
                    args.output,
                    max_age_days=args.max_snapshot_age_days,
                )
            )
    except Exception as error:
        print("vllm-test-selection: %s" % error, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
