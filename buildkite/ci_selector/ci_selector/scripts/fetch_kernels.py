# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Download the latest published kernel record into coverage-data/.

The recording build's collect step (`recorders/kernrec/collect.sh`) publishes, per
commit, the kernel table and the kernel symbol map, and moves `latest.json`
only once both are in place. This follows that pointer over plain HTTPS: the
bucket is public-read, so no credentials are involved.

    ci-fetch-kernel-record                    # follow latest.json
    ci-fetch-kernel-record --commit <sha>     # one published commit
    ci-fetch-kernel-record --out DIR          # somewhere other than coverage-data/

Both files are validated with the selector's own loaders before they replace
whatever is on disk, so an interrupted or bad download leaves the previous
pair in place. Exit 1 on any problem; the selector then runs csrc on the
code map alone and says so.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from ..coverage.kernels import load_symbol_map, load_table
from ..coverage.source import COVERAGE_DIR, KERNEL_MAP_NAME, KERNEL_TABLE_NAME

DEFAULT_URL = "https://vllm-ci-selector.s3.us-west-2.amazonaws.com/ci"
URL_ENV = "CI_SELECTOR_KERNEL_RECORD_URL"
TIMEOUT = 60


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:  # noqa: S310 - https, fixed host
        return r.read()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--url",
        default=os.environ.get(URL_ENV, DEFAULT_URL),
        help=f"bucket prefix holding latest.json (default: ${URL_ENV} or {DEFAULT_URL})",
    )
    ap.add_argument(
        "--commit", help="a published commit instead of the one latest.json names"
    )
    ap.add_argument(
        "--out", type=Path, default=COVERAGE_DIR, help="directory to write into"
    )
    args = ap.parse_args(argv)
    url = args.url.rstrip("/")

    try:
        if args.commit:
            commit, build = args.commit, "?"
        else:
            latest = json.loads(_get(f"{url}/latest.json"))
            commit, build = latest["commit"], latest.get("build", "?")
        args.out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=args.out) as tmp:
            staged = {}
            for name in (KERNEL_TABLE_NAME, KERNEL_MAP_NAME):
                dest = Path(tmp) / name
                dest.write_bytes(_get(f"{url}/{commit}/{name}"))
                staged[name] = dest
            table = load_table(staged[KERNEL_TABLE_NAME])
            symbol_map = load_symbol_map(staged[KERNEL_MAP_NAME])
            for half in (table, symbol_map):
                if not half.available:
                    print(f"error: {half.unavailable}", file=sys.stderr)
                    return 1
            if table.commit != symbol_map.commit:
                print(
                    f"error: table is for {table.commit[:10]}, map for "
                    f"{symbol_map.commit[:10]}; refusing an unmatched pair",
                    file=sys.stderr,
                )
                return 1
            for name, src in staged.items():
                os.replace(src, args.out / name)
    except urllib.error.HTTPError as exc:
        what = "nothing published yet" if exc.code in (403, 404) else "download failed"
        print(f"error: {what}: HTTP {exc.code} for {exc.url}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(
        f"kernel record for {commit[:10]} (build {build}): {len(table)} step rows, "
        f"{symbol_map.objects} objects -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
