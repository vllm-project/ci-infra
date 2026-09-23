# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Turn a recording build's own artifacts into a sweep directory, no API.

`ci-fetch-build` asks the Buildkite API for jobs, artifacts and logs. The
collect step at the end of a recording build has no token and needs none:
`buildkite-agent artifact download ".fnrec/**/*"` hands it every job's
directory, and each one carries

    kernrec.json       step key, label, shard, exit status (kernrec/ci_setup.sh)
    fn.*.txt           the Python record (fnrec/fnrec.py)
    pytest.*.txt       each pytest run's collected and summary lines
                       (fnrec/fnrec_pytest.py)
    pytest.installed   the pytest plugin was in place

This writes the layout `ci-fetch-build` writes, so `ci-build-table` reads it
unchanged: index.json, build.json and jobs/<id>/{meta.json, fnrec/, job.log.gz}.

The job log is put together from pytest.*.txt, the two lines of a real log the
builder reads. A job with the plugin marker and no pytest file ran no pytest,
and gets an empty log, which reads as a non-pytest step. A job without the
marker gets no log at all, which the builder counts as unreadable, keeping the
row thin: nothing then says whether its tests ran.

State comes from the sidecar's exit status. Zero is passed. Anything else is
failed, including a sidecar still at null because the job was killed before
its finish ran, since the builder needs that to refuse the row a drop. A job
directory without a sidecar cannot be filed under a step and is left out and
counted; that happens when the Python recorder runs without the kernel one,
which is what writes the sidecar.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
from pathlib import Path

from ..coverage.model import RECORD_GLOB
from .build import WORLD_ENV

SIDECAR = "kernrec.json"
PYTEST_GLOB = "pytest.*.txt"
PYTEST_MARKER = "pytest.installed"
STARTED_UNKNOWN = "unknown (from artifacts)"


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def job_meta(job_dir: Path, sidecar: dict, *, build: str, commit: str) -> dict:
    exit_status = sidecar.get("exit_status")
    total = _int_or_none(sidecar.get("parallel_job_count"))
    return {
        "job": job_dir.name,
        "step_key": sidecar.get("step_key") or None,
        "label": sidecar.get("label") or "",
        "state": "passed" if exit_status == 0 else "failed",
        "exit_status": exit_status,
        "parallel_index": _int_or_none(sidecar.get("parallel_job")) if total else None,
        "parallel_total": total,
        "agent": sidecar.get("host"),
        # Non-null: the job ran, since it wrote a sidecar. The builder only
        # asks whether a job started, to keep blocked jobs out of the rate.
        "started_at": STARTED_UNKNOWN,
        "finished_at": None,
        "build": build,
        "commit": commit,
        "artifact": "artifact-download",
        "n_artifacts": 0,
        "n_files": 0,
        "n_records": 0,
        "duplicate_names": 0,
        "log": None,
    }


def convert(
    fnrec_root: Path,
    out: Path,
    *,
    build: str,
    commit: str,
    pipeline: str,
    source: str = "",
    env: dict[str, str] | None = None,
) -> dict:
    """Write the sweep directory. Returns the index it wrote."""
    jobs_dir = out / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ if env is None else env
    (out / "build.json").write_text(
        json.dumps(
            {
                "number": build,
                "commit": commit,
                "source": source,
                # The allowlist only: a sweep is published, and the job env
                # holds the agent token.
                "env": {k: env[k] for k in WORLD_ENV if k in env},
            },
            indent=2,
        )
    )

    jobs, no_sidecar, commits = [], [], set()
    for job_dir in sorted(p for p in fnrec_root.iterdir() if p.is_dir()):
        try:
            sidecar = json.loads((job_dir / SIDECAR).read_text())
        except (OSError, ValueError):
            no_sidecar.append(job_dir.name)
            continue
        if sidecar.get("commit"):
            commits.add(sidecar["commit"])
        meta = job_meta(job_dir, sidecar, build=build, commit=commit)
        target = jobs_dir / job_dir.name
        records = sorted(job_dir.glob(RECORD_GLOB))
        if records:
            (target / "fnrec").mkdir(parents=True, exist_ok=True)
            for f in records:
                shutil.copyfile(f, target / "fnrec" / f.name)
        else:
            target.mkdir(parents=True, exist_ok=True)
        meta["n_files"] = meta["n_records"] = meta["n_artifacts"] = len(records)

        summaries = sorted(job_dir.glob(PYTEST_GLOB))
        if summaries or (job_dir / PYTEST_MARKER).exists():
            with gzip.open(target / "job.log.gz", "wt") as fh:
                for f in summaries:
                    fh.write(f.read_text(errors="replace"))
            meta["log"] = f"from {len(summaries)} pytest summaries"
        else:
            meta["log"] = "no pytest plugin"
        (target / "meta.json").write_text(json.dumps(meta, indent=2))
        jobs.append(meta)

    index = {
        "org": "",
        "pipeline": pipeline,
        "build": build,
        "commit": commit,
        "branch": env.get("BUILDKITE_BRANCH"),
        "n_jobs": len(jobs),
        "n_attempted": len(jobs),
        "n_with_record": sum(1 for j in jobs if j["n_records"]),
        "foreign_artifacts": [],
        "jobs": sorted(
            jobs, key=lambda m: (m["step_key"] or "", m["parallel_index"] or 0)
        ),
        "errors": [],
        "no_sidecar": no_sidecar,
        # A sidecar from another commit means artifacts from some other build
        # got mixed in; the builder would stamp them with this commit.
        "foreign_commits": sorted(commits - {commit}),
    }
    (out / "index.json").write_text(json.dumps(index, indent=2))
    return index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ci-sweep-from-artifacts",
        description="Recording-build artifacts (.fnrec/<job>/...) to a sweep directory",
    )
    ap.add_argument("fnrec", type=Path, help="the downloaded .fnrec directory")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--build", default=os.environ.get("BUILDKITE_BUILD_NUMBER"))
    ap.add_argument("--commit", default=os.environ.get("BUILDKITE_COMMIT"))
    ap.add_argument(
        "--pipeline", default=os.environ.get("BUILDKITE_PIPELINE_SLUG", "ci")
    )
    ap.add_argument("--source", default=os.environ.get("BUILDKITE_SOURCE", ""))
    args = ap.parse_args(argv)
    if not args.build or not args.commit:
        ap.error("--build and --commit are required outside Buildkite")
    if not args.fnrec.is_dir():
        print(f"no such directory: {args.fnrec}", file=sys.stderr)
        return 1
    index = convert(
        args.fnrec,
        args.out,
        build=str(args.build),
        commit=args.commit,
        pipeline=args.pipeline,
        source=args.source,
    )
    with_log = sum(1 for j in index["jobs"] if j["log"] != "no pytest plugin")
    print(
        f"{index['n_jobs']} jobs, {index['n_with_record']} with a Python record, "
        f"{with_log} with test counts; {len(index['no_sidecar'])} without a sidecar"
    )
    if index["foreign_commits"]:
        print(
            f"sidecars from other commits: {index['foreign_commits']}", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
