# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Turn a recording build's own artifacts into a sweep directory, no API.

`ci-fetch-build` asks the Buildkite API for jobs, artifacts and logs. The
collect step at the end of a recording build has no token and needs none: the
agent downloads the build's artifacts, and each job's recording carries what
the table builder needs.

    .fnrec/<job>.tar.gz or .fnrec/<job>/    the Python recorder (recorders/fnrec)
        fn.*.txt                            one file per process
        fnrec.json                          step, shard and exit status, from pack.sh
        pytest.*.txt, pytest.installed      test outcomes (fnrec_pytest.py)
    .kernrec/<job>/kernrec.json             the kernel recorder's sidecar, if it ran

This writes the layout `ci-fetch-build` writes, so `ci-build-table` reads it
unchanged: index.json, build.json and jobs/<id>/{meta.json, fnrec/, job.log.gz}.

Identity and exit status come from fnrec.json, else from the kernel
recorder's sidecar, else from the recorder's own file headers (step key and
shard, but no exit status). No exit status reads as failed, since the
builder needs that to refuse the row a drop: a job with no fnrec.json was
killed or stopped at a failed command before its pack step ran.

The job log is put together from pytest.*.txt, the two lines of a real log the
builder reads. A job with the plugin marker and no pytest file ran no pytest,
and gets an empty log, which reads as a non-pytest step. A job without the
marker gets no log at all, which the builder counts as unreadable, keeping the
row thin: nothing then says whether its tests ran.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
import tarfile
from pathlib import Path

from ..coverage.model import RECORD_GLOB
from .build import WORLD_ENV

FNREC_SIDECAR = "fnrec.json"
KERNREC_SIDECAR = "kernrec.json"
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


def unpack(fnrec_dir: Path) -> list[str]:
    """Unpack every <job>.tar.gz beside its raw directory. Returns the ones that
    would not open, which are left as they are and counted, not guessed at."""
    bad = []
    for tar in sorted(fnrec_dir.glob("*.tar.gz")):
        try:
            with tarfile.open(tar) as tf:
                tf.extractall(fnrec_dir, filter="data")
        except (tarfile.TarError, OSError, EOFError):
            bad.append(tar.name)
    return bad


def _header_identity(job_dir: Path) -> dict | None:
    """Step key and shard from the recorder's own #start line, when neither
    sidecar exists. It carries no exit status."""
    for f in sorted(job_dir.glob(RECORD_GLOB)):
        try:
            with open(f, errors="replace") as fh:
                first = fh.readline().rstrip("\n").split("\t")
        except OSError:
            continue
        if not first or first[0] != "#start":
            continue
        kv = dict(part.split("=", 1) for part in first[1:] if "=" in part)
        if not (kv.get("BUILDKITE_STEP_KEY") or kv.get("BUILDKITE_LABEL")):
            continue
        return {
            "step_key": kv.get("BUILDKITE_STEP_KEY", ""),
            "label": kv.get("BUILDKITE_LABEL", ""),
            "parallel_job": kv.get("BUILDKITE_PARALLEL_JOB", ""),
            "parallel_job_count": kv.get("BUILDKITE_PARALLEL_JOB_COUNT", ""),
            "exit_status": None,
            "host": kv.get("host"),
        }
    return None


def identity(job_dir: Path, kernrec_dir: Path) -> tuple[dict | None, str]:
    """(what files this job, where it came from)."""
    for path, origin in (
        (job_dir / FNREC_SIDECAR, FNREC_SIDECAR),
        (kernrec_dir / job_dir.name / KERNREC_SIDECAR, KERNREC_SIDECAR),
    ):
        try:
            return json.loads(path.read_text()), origin
        except (OSError, ValueError):
            continue
    found = _header_identity(job_dir)
    return found, ("header" if found else "")


def convert(
    root: Path,
    out: Path,
    *,
    build: str,
    commit: str,
    pipeline: str,
    source: str = "",
    env: dict[str, str] | None = None,
    fnrec_dir: str = ".fnrec",
    kernrec_dir: str = ".kernrec",
) -> dict:
    """Write the sweep directory from the downloaded artifacts under `root`.
    Returns the index it wrote."""
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

    fn_root = root / fnrec_dir
    bad_tars = unpack(fn_root) if fn_root.is_dir() else []
    job_dirs = (
        sorted(p for p in fn_root.iterdir() if p.is_dir()) if fn_root.is_dir() else []
    )

    jobs, no_identity, commits = [], [], set()
    for job_dir in job_dirs:
        sidecar, origin = identity(job_dir, root / kernrec_dir)
        if sidecar is None:
            no_identity.append(job_dir.name)
            continue
        if sidecar.get("commit"):
            commits.add(sidecar["commit"])
        meta = job_meta(job_dir, sidecar, build=build, commit=commit)
        meta["identity_from"] = origin
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
        "errors": [f"unreadable tarball {name}" for name in bad_tars],
        "no_identity": no_identity,
        # A sidecar from another commit means artifacts from some other build
        # got mixed in; the builder would stamp them with this commit.
        "foreign_commits": sorted(commits - {commit}),
    }
    (out / "index.json").write_text(json.dumps(index, indent=2))
    return index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="ci-sweep-from-artifacts",
        description="Recording-build artifacts (.fnrec/, .kernrec/) to a sweep directory",
    )
    ap.add_argument(
        "root", type=Path, help="where the artifacts were downloaded (holds .fnrec/)"
    )
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
    if not (args.root / ".fnrec").is_dir():
        print(f"no .fnrec directory under {args.root}", file=sys.stderr)
        return 1
    index = convert(
        args.root,
        args.out,
        build=str(args.build),
        commit=args.commit,
        pipeline=args.pipeline,
        source=args.source,
    )
    with_log = sum(1 for j in index["jobs"] if j["log"] != "no pytest plugin")
    print(
        f"{index['n_jobs']} jobs, {index['n_with_record']} with a Python record, "
        f"{with_log} with test counts; {len(index['no_identity'])} with no step to file under; "
        f"{len(index['errors'])} unreadable tarballs"
    )
    if index["foreign_commits"]:
        print(
            f"sidecars from other commits: {index['foreign_commits']}", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
