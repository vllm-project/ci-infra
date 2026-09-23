#!/usr/bin/env python3
"""The kernel coverage table: which GPU kernels each CI step launched.

    kernel_table.py build <recordings-dir> --build N --commit SHA [--jobs jobs.json] --out kernel_table.json.gz
    kernel_table.py query <kernel_table.json.gz> <kernel_symbol_map.json.gz> --file csrc/x.cu [...]
    kernel_table.py show  <kernel_table.json.gz>

Two input layouts:

    <recordings-dir>/<step_key>/<job-id>/kern.*.txt   what analyze_build.py writes
    --kernrec <dir>: <dir>/<job-id>/kern.*.txt + kernrec.json
                                                      what `buildkite-agent artifact
                                                      download ".kernrec/**/*"` yields
                                                      inside the build; the sidecar
                                                      (written by ci_setup.sh on exit)
                                                      carries step key and exit status

A job without a sidecar died before its shell could exit (timeout, OOM kill)
and is filed under its step as not passed; if its step is unknown it is
counted and skipped.

One row per step key, the same unit the pipeline generator selects. Parallel
shards and every process of every job fold into the row, because a change
selects the step, not a shard. A row carries what the consumer needs to
decide whether to trust it: how many jobs fed it, whether they all passed,
and whether any recorder reported dropped records.

    {
      "version": 1,
      "source": {"org": "vllm", "pipeline": "ci", "build": 90039,
                 "commit": "<sha the steps ran>", "recorded_at": "..."},
      "names": ["_Z21fusedQKNormRopeKernel...", "fused_moe_kernel", ...],
      "rows": {
        "fusion-e2e-quick-h100": {
          "jobs": 1, "passed": true, "complete": true,
          "shards": {"expected": 3, "seen": 3},   # null expected: not parallel
          "processes": 4, "dropped": 0,
          "kernels": [0, 17, 42, ...]        # indexes into names
        },
        ...
      }
    }

A query joins it with a kernel symbol map for the same commit: a changed
file -> the symbols compiled from it or from objects that included it ->
every row whose kernel set meets them. A row that is usable (all jobs
passed, every shard present, nothing dropped) and meets none of them is a
step the change cannot reach through any kernel. A step with no row is a step the record
knows nothing about, and stays with the static map.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

TABLE_VERSION = 2


def read_recording(path: Path) -> tuple[set[str], int, bool]:
    """(names, dropped, clean_exit) from one kern.<pid>.txt."""
    names: set[str] = set()
    dropped = 0
    clean = False
    for line in path.read_text(errors="replace").splitlines():
        if not line:
            continue
        if line.startswith("#"):
            if line.startswith("# dropped="):
                dropped += int(line.split("=", 1)[1])
            elif line.startswith("# end "):
                clean = True
                for kv in line[6:].split():
                    k, _, v = kv.partition("=")
                    if k == "dropped":
                        dropped = max(dropped, int(v))
            continue
        names.add(line)
    return names, dropped, clean


def _jobs_from_step_layout(root: Path, job_state: dict[str, str]):
    """Jobs under <step>/<job>/kern.*.txt (the analyze_build.py layout).

    Shard coverage is unknown here, so rows are complete when every job the
    API reported as passed is present; the API states are the evidence.
    """
    for step_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for job_dir in sorted(p for p in step_dir.iterdir() if p.is_dir()):
            files = sorted(job_dir.glob("kern.*.txt"))
            if not files:
                continue
            state = job_state.get(job_dir.name)
            yield {
                "step_key": step_dir.name,
                "job_id": job_dir.name,
                "files": files,
                "passed": state in (None, "passed"),
                "shard": None,
                "shard_count": None,
            }


def _int_or_none(v):
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _jobs_from_kernrec_layout(root: Path, job_state: dict[str, str]):
    """Jobs under <job>/kern.*.txt + kernrec.json (the artifact download layout).

    Every job directory is read, whether or not it holds recordings: a shard
    that failed before launching a kernel still has its sidecar, and its
    exit status must count against the row. ci_setup.sh writes the sidecar
    at setup with `exit_status: null` and rewrites it on exit, so a job
    killed before its trap ran keeps its step key and is simply not passed.
    A job with recordings but no sidecar at all cannot be filed and is
    reported with step_key None.
    """
    for job_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        files = sorted(job_dir.glob("kern.*.txt"))
        meta: dict = {}
        side = job_dir / "kernrec.json"
        if side.exists():
            try:
                meta = json.loads(side.read_text())
            except ValueError:
                meta = {}
        if not files and not meta:
            continue
        step_key = (
            meta.get("step_key") or job_state.get(f"{job_dir.name}:step_key") or None
        )
        state = job_state.get(job_dir.name)
        if state:
            passed = state == "passed"
        else:
            passed = meta.get("exit_status") == 0
        yield {
            "step_key": step_key,
            "job_id": job_dir.name,
            "files": files,
            "passed": passed,
            "shard": _int_or_none(meta.get("parallel_job")),
            "shard_count": _int_or_none(meta.get("parallel_job_count")),
        }


def usable(row: dict) -> bool:
    """A row the selector may drop a step on.

    Every job passed, every shard is accounted for, and no recorder dropped
    records. Anything less is evidence for selecting, never for dropping.
    """
    return (
        bool(row.get("passed"))
        and bool(row.get("complete", True))
        and not row.get("dropped")
    )


def build(a) -> int:
    job_state: dict[str, str] = {}
    if a.jobs:
        for j in json.loads(Path(a.jobs).read_text()):
            job_state[j["id"]] = j.get("state", "")
            if j.get("step_key"):
                job_state[f"{j['id']}:step_key"] = j["step_key"]

    if a.kernrec:
        job_iter = _jobs_from_kernrec_layout(a.kernrec, job_state)
    else:
        job_iter = _jobs_from_step_layout(a.recordings, job_state)

    names_index: dict[str, int] = {}
    names: list[str] = []
    rows: dict[str, dict] = {}
    unfiled = 0
    for job in job_iter:
        if not job["step_key"]:
            unfiled += 1
            continue
        row = rows.setdefault(
            job["step_key"],
            {
                "jobs": 0,
                "passed": True,
                "complete": True,
                "processes": 0,
                "dropped": 0,
                "shards": {"expected": None, "seen": set()},
                "kernels": set(),
            },
        )
        row["jobs"] += 1
        row["passed"] = row["passed"] and job["passed"]
        if job["shard_count"] is not None:
            row["shards"]["expected"] = max(
                row["shards"]["expected"] or 0, job["shard_count"]
            )
        if job["shard"] is not None:
            row["shards"]["seen"].add(job["shard"])
        for f in job["files"]:
            row["processes"] += 1
            got, d, _clean = read_recording(f)
            row["dropped"] += d
            for n in got:
                i = names_index.get(n)
                if i is None:
                    i = names_index[n] = len(names)
                    names.append(n)
                row["kernels"].add(i)
    for row in rows.values():
        row["kernels"] = sorted(row["kernels"])
        expected, seen = row["shards"]["expected"], row["shards"]["seen"]
        # A parallel step is complete only when every shard reported in.
        # Non-parallel steps have no shard fields and stay complete.
        if expected is not None and len(seen) < expected:
            row["complete"] = False
        row["shards"] = {"expected": expected, "seen": len(seen)}
    if unfiled:
        print(f"warning: {unfiled} job(s) with recordings but no step key; skipped")

    table = {
        "version": TABLE_VERSION,
        "source": {
            "org": a.org,
            "pipeline": a.pipeline,
            "build": a.build,
            "commit": a.commit,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        "names": names,
        "rows": rows,
        "stats": {
            "steps": len(rows),
            "unique_kernels": len(names),
            "processes": sum(r["processes"] for r in rows.values()),
            "rows_usable": sum(1 for r in rows.values() if usable(r)),
            "rows_incomplete": sum(1 for r in rows.values() if not r["complete"]),
            "rows_with_drops": sum(1 for r in rows.values() if r["dropped"]),
        },
    }
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(a.out, "wt", encoding="utf-8") as f:
        json.dump(table, f, separators=(",", ":"))
    print(f"wrote {a.out} ({a.out.stat().st_size // 1024} KiB): {table['stats']}")
    return 0


def load_table(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def load_map(path: Path) -> tuple[dict[str, set[str]], set[str], dict]:
    """(file -> symbols reachable from it, files that are unknown, header).

    A file is unknown when an object compiled from it or including it failed
    symbol extraction: the map cannot say the file is kernel-free, so a change
    to it must not drop anything. Same for every file when the map is empty.
    """
    with gzip.open(path, "rt", encoding="utf-8") as f:
        d = json.load(f)
    reach: dict[str, set[str]] = defaultdict(set)
    unknown: set[str] = set()
    for e in d["objects"]:
        if e.get("error"):
            unknown.add(e["source"])
            unknown.update(e["deps"])
            continue
        for s in e["symbols"]:
            reach[e["source"]].add(s)
            for dep in e["deps"]:
                reach[dep].add(s)
    return reach, unknown, d


def query(a) -> int:
    t = load_table(a.table)
    reach, unknown, m = load_map(a.map)
    map_commit = m.get("commit", "")
    names = t["names"]
    if m.get("reason"):
        print(f"map is empty ({m['reason']}): every file falls back to the static rule")
        return 0
    if m.get("incomplete"):
        print(
            f"note: map incomplete, {len(m.get('errors', []))} objects unreadable; "
            f"{len(unknown)} files fall back to the static rule"
        )
    if map_commit and t["source"]["commit"] and map_commit != t["source"]["commit"]:
        print(
            f"note: map is for {map_commit[:12]}, table for {t['source']['commit'][:12]}; "
            "file-level attribution tolerates symbol churn, but treat drops as advisory"
        )
    row_sets = {k: {names[i] for i in r["kernels"]} for k, r in t["rows"].items()}
    for path in a.file:
        syms = reach.get(path, set())
        print(f"\n--- {path}: {len(syms)} symbols reachable")
        if path in unknown:
            print(
                "    UNKNOWN: an object compiled from or including this file failed "
                "extraction -> static rule, nothing dropped"
            )
            continue
        if not syms:
            print(
                "    not in the map: nothing compiled from it or included it -> static rule"
            )
            continue
        select, drop, unusable = [], [], []
        for k, r in t["rows"].items():
            if row_sets[k] & syms:
                select.append(k)
            elif usable(r):
                drop.append(k)
            else:
                unusable.append(k)
        print(f"    SELECT   ({len(select)}): " + ", ".join(sorted(select)))
        print(f"    drop     ({len(drop)})")
        if unusable:
            print(
                f"    keep, row not usable ({len(unusable)}): "
                + ", ".join(sorted(unusable)[:8])
                + (" ..." if len(unusable) > 8 else "")
            )
    return 0


def show(a) -> int:
    t = load_table(a.table)
    print(
        json.dumps({k: v for k, v in t.items() if k not in ("names", "rows")}, indent=1)
    )
    names = t["names"]
    fam = Counter()
    for n in names:
        if "nccl" in n:
            fam["nccl"] += 1
        elif "cublas" in n.lower() or "gemmSN" in n or "gemvNSP" in n:
            fam["cublas"] += 1
        elif n.startswith("_Z"):
            fam["mangled C++ (csrc, torch, cutlass, ...)"] += 1
        else:
            fam["plain name (Triton)"] += 1
    print("name families:", dict(fam))
    print(f"\n{'step':48} {'jobs':>4} {'procs':>5} {'kernels':>7} {'ok':>3}")
    for k, r in sorted(t["rows"].items(), key=lambda kv: -len(kv[1]["kernels"]))[
        : a.top
    ]:
        print(
            f"{k[:48]:48} {r['jobs']:>4} {r['processes']:>5} {len(r['kernels']):>7} {'yes' if usable(r) else 'NO':>3}"
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("recordings", type=Path, nargs="?", default=None)
    b.add_argument(
        "--kernrec",
        type=Path,
        default=None,
        help="<job-id>/kern.*.txt + kernrec.json layout (artifact download)",
    )
    b.add_argument("--build", type=int, required=True)
    b.add_argument("--commit", default="")
    b.add_argument("--org", default="vllm")
    b.add_argument("--pipeline", default="ci")
    b.add_argument(
        "--jobs",
        default=None,
        help="JSON list of {id, state} to mark rows from failed jobs",
    )
    b.add_argument("--out", type=Path, required=True)
    b.set_defaults(fn=build)
    q = sub.add_parser("query")
    q.add_argument("table", type=Path)
    q.add_argument("map", type=Path)
    q.add_argument("--file", action="append", required=True)
    q.set_defaults(fn=query)
    s = sub.add_parser("show")
    s.add_argument("table", type=Path)
    s.add_argument("--top", type=int, default=15)
    s.set_defaults(fn=show)
    a = ap.parse_args()
    if a.cmd == "build" and not (a.kernrec or a.recordings):
        ap.error("build needs a <recordings-dir> or --kernrec <dir>")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
