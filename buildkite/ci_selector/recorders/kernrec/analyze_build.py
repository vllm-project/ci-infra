#!/usr/bin/env python3
"""Pull every kernrec recording out of one Buildkite build and score it.

    BUILDKITE_TOKEN=... analyze_build.py <build-number> [--out DIR]
                        [--expect STEP_KEY=SUBSTR ...] [--org vllm] [--pipeline ci]

Downloads each job's `.fnrec/**/kern.*.txt` artifacts into
<out>/<step_key>/<job-id>/, then prints one line per step: processes,
unique kernels, dropped records, and whether each expected name showed up.
Jobs with no recording are listed too, since silence is the failure mode
that matters most.

Token needs read_builds and read_artifacts. The spike's expectations:

    --expect fusion-e2e-quick-h100=fusedQKNormRopeKernel
    --expect fusion-e2e-tp2-quick-h100=fusedQKNormRopeKernel
    --expect kernels-core-operation-test=fusedQKNormRopeKernel
    --expect pytorch-compilation-passes-unit-tests=fusedQKNormRopeKernel
    --expect kernels-moe-test=fused_moe_kernel
    --expect kernels-deepgemm-test-h100=deep_gemm
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

API = "https://api.buildkite.com/v2/organizations/{org}/pipelines/{pipeline}/builds/{build}"
HERE = Path(__file__).resolve().parent


def _curl(args: list[str]) -> bytes:
    # curl rather than urllib: it uses the system trust store, which a
    # python.org interpreter on macOS often lacks.
    return subprocess.run(
        ["curl", "-sSf", *args], check=True, capture_output=True
    ).stdout


def api(url: str, token: str):
    return json.loads(_curl(["-H", f"Authorization: Bearer {token}", url]))


def download(url: str, token: str, dest: Path) -> None:
    # The download endpoint answers 302 to a presigned S3 URL. S3 rejects a
    # request that also carries our bearer header, so resolve the redirect
    # first and fetch the target without it.
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = (
        _curl(
            [
                "-o",
                os.devnull,
                "-w",
                "%{redirect_url}",
                "-H",
                f"Authorization: Bearer {token}",
                url,
            ]
        )
        .decode()
        .strip()
    )
    if not target:
        raise RuntimeError(f"no redirect from {url}")
    dest.write_bytes(_curl([target]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("build")
    ap.add_argument("--org", default="vllm")
    ap.add_argument("--pipeline", default="ci")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--expect", action="append", default=[], metavar="STEP_KEY=SUBSTR")
    a = ap.parse_args()
    token = os.environ.get("BUILDKITE_TOKEN") or os.environ.get("BK_TOKEN")
    if not token:
        sys.exit("set BUILDKITE_TOKEN (read_builds, read_artifacts)")
    out = a.out or Path(f"/tmp/kernrec-build-{a.build}")
    expects: dict[str, list[str]] = defaultdict(list)
    for e in a.expect:
        k, _, v = e.partition("=")
        expects[k].append(v)

    base = API.format(org=a.org, pipeline=a.pipeline, build=a.build)
    build = api(base, token)
    print(f"build {a.build}: state={build['state']} branch={build['branch']}")
    jobs = [j for j in build["jobs"] if j.get("type") == "script" and j.get("step_key")]

    by_step: dict[str, list[dict]] = defaultdict(list)
    for j in jobs:
        by_step[j["step_key"]].append(j)

    failures = 0
    for step_key in sorted(by_step):
        if step_key in ("bootstrap", "image-build"):
            continue
        for j in by_step[step_key]:
            jdir = out / step_key / j["id"]
            arts = api(f"{base}/jobs/{j['id']}/artifacts?per_page=100", token)
            # removeprefix, not lstrip: lstrip takes a character set and would eat
            # the leading dot of `.fnrec` (the same trap fetch.py documents).
            recs = [
                x
                for x in arts
                if x["path"].removeprefix("./").startswith(".fnrec/")
                and "/kern." in x["path"]
            ]
            for x in recs:
                download(x["download_url"], token, jdir / Path(x["path"]).name)
            label = f"{step_key} [{j.get('name', '')[:48]}] job={j['state']}"
            if not recs:
                print(f"--- {label} : NO RECORDING ({len(arts)} artifacts)")
                failures += 1
                continue
            cmd = [
                sys.executable,
                str(HERE / "summarize.py"),
                str(jdir),
                "--label",
                label,
                "--top",
                "6",
            ]
            for e in expects.get(step_key, []):
                cmd += ["--expect", e]
            rc = subprocess.run(cmd, check=False).returncode
            failures += rc != 0
    print(
        f"\n{'all expectations held' if not failures else f'{failures} step(s) failed expectations'}; files under {out}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
