# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Publish a built coverage table so the selector can fetch it.

Checks the table, writes it under the commit it was recorded at, then moves
`latest.json`, so a reader never finds a half-written table. Needs `aws` and
credentials for the bucket. Exit 1 on any problem, having changed nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ..coverage.source import TABLE_NAME
from ..coverage.table import load

BUCKET_ENV = "CI_SELECTOR_BUCKET"
DEFAULT_BUCKET = "vllm-ci-selector"
# Each recorder gets its own prefix in the bucket.
RECORDER = "fnrec"
POINTER = "latest.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("table", type=Path, help="the table to publish")
    ap.add_argument(
        "--bucket",
        default=os.environ.get(BUCKET_ENV, DEFAULT_BUCKET),
        help=f"default: ${BUCKET_ENV} or {DEFAULT_BUCKET}",
    )
    ap.add_argument("--dry-run", action="store_true", help="print, upload nothing")
    args = ap.parse_args(argv)

    table = load(args.table)
    if not table.available:
        print(f"error: {table.unavailable}", file=sys.stderr)
        return 1
    if not table.commit:
        commits = table.source.get("commits") or []
        why = (
            f"spans {len(commits)} commits, so there is no one commit to "
            "publish it under"
            if len(commits) > 1
            else "records no commit; rebuild it with a current ci-build-table"
        )
        print(f"error: {args.table} {why}", file=sys.stderr)
        return 1

    pipelines = table.source.get("pipelines") or []
    if len(pipelines) > 1:
        print(
            f"error: {args.table} spans {len(pipelines)} pipelines, so there is "
            "no one prefix to publish it under",
            file=sys.stderr,
        )
        return 1
    pipeline = table.pipeline or "ci"
    prefix = f"s3://{args.bucket}/{pipeline}/{RECORDER}"
    pointer = {
        "commit": table.commit,
        "build": table.build,
        "pipeline": pipeline,
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "files": [TABLE_NAME],
    }
    print(
        f"{args.table} -> {prefix}/{table.commit}/{TABLE_NAME}\n"
        f"  {len(table)} step rows, build {table.build or '?'}, "
        f"builds {table.source.get('builds') or []}"
    )
    if args.dry_run:
        print(f"  would move {prefix}/{POINTER} to {table.commit}")
        return 0

    # The table first, the pointer only once it is there.
    if not _cp(args.table, f"{prefix}/{table.commit}/{TABLE_NAME}"):
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / POINTER
        local.write_text(json.dumps(pointer) + "\n")
        if not _cp(local, f"{prefix}/{POINTER}"):
            print(
                "error: the table uploaded but the pointer did not; "
                "nothing reads it until this is re-run",
                file=sys.stderr,
            )
            return 1
    print(f"  published, {POINTER} now names {table.commit[:10]}")
    return 0


def _cp(src: Path, dest: str) -> bool:
    try:
        proc = subprocess.run(
            ["aws", "s3", "cp", str(src), dest, "--only-show-errors"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("error: no aws cli on PATH", file=sys.stderr)
        return False
    if proc.returncode != 0:
        print(f"error: {dest}: {proc.stderr.strip()}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    raise SystemExit(main())
