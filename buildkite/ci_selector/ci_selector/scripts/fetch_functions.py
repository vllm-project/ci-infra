# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Download the latest published coverage table into coverage-data/.

Follows `latest.json` over HTTPS from a public-read bucket, so no credentials
are needed. The table is checked before it replaces the one on disk, so a bad
download leaves the old one in place. Exit 1 on any problem.
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

from ..coverage.source import COVERAGE_DIR, TABLE_NAME, FetchError
from ..coverage.table import Table, load

DEFAULT_URL = "https://vllm-ci-selector.s3.us-west-2.amazonaws.com/ci/fnrec"
URL_ENV = "CI_SELECTOR_FUNCTION_RECORD_URL"
TIMEOUT = 60


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:  # noqa: S310 - https, fixed host
        return r.read()


def fetch(
    url: str, commit: str | None = None, out: Path = COVERAGE_DIR
) -> tuple[Table, str, str]:
    """Download the table published for `commit`, or the one `latest.json`
    names, into `out`, and return `(table, commit, build)`.

    The table is parsed once, here, and checked before it replaces the one on
    disk, so a bad download leaves the old one in place. A caller that goes on
    to use the table takes the parsed one rather than reading the file again:
    it is about 1 GB of JSON. Raises FetchError on any problem.
    """
    url = url.rstrip("/")
    try:
        if commit:
            build = "?"
        else:
            latest = json.loads(_get(f"{url}/latest.json"))
            commit, build = latest["commit"], latest.get("build", "?")
        out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=out) as tmp:
            staged = Path(tmp) / TABLE_NAME
            staged.write_bytes(_get(f"{url}/{commit}/{TABLE_NAME}"))
            table = load(staged)
            if not table.available:
                raise FetchError(f"table for {commit[:10]}: {table.unavailable}")
            # A mismatch means table and pointer were published out of step.
            if table.commit and table.commit != commit:
                raise FetchError(
                    f"asked for {commit[:10]}, table records "
                    f"{table.commit[:10]}; refusing it"
                )
            os.replace(staged, out / TABLE_NAME)
    except urllib.error.HTTPError as exc:
        what = "nothing published yet" if exc.code in (403, 404) else "download failed"
        raise FetchError(f"{what}: HTTP {exc.code} for {exc.url}") from exc
    except (
        urllib.error.URLError,
        OSError,
        KeyError,
        ValueError,
        TypeError,  # latest.json that is not an object
        AttributeError,
    ) as exc:
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc
    return table, commit, build


def fetch_latest() -> Table:
    """The latest published table, downloaded into coverage-data/ and parsed
    once, for `ci-select` run without a table. Raises FetchError."""
    return fetch(os.environ.get(URL_ENV, DEFAULT_URL), out=COVERAGE_DIR)[0]


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
    try:
        table, commit, build = fetch(args.url, args.commit, args.out)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"function record for {commit[:10]} (build {build}): "
        f"{len(table)} step rows -> {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
