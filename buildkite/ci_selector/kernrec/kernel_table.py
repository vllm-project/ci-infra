#!/usr/bin/env python3
"""The kernel coverage table: which GPU kernels each CI step launched.

    kernel_table.py build <recordings-dir> --build N --commit SHA [--jobs jobs.json] --out kernel_table.json.gz
    kernel_table.py query <kernel_table.json.gz> <kernel_symbol_map.json.gz> --file csrc/x.cu [...]
    kernel_table.py show  <kernel_table.json.gz>

<recordings-dir> is what analyze_build.py writes: <step_key>/<job-id>/kern.*.txt.

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
          "jobs": 1, "passed": true, "processes": 4, "dropped": 0,
          "kernels": [0, 17, 42, ...]        # indexes into names
        },
        ...
      }
    }

A query joins it with a kernel symbol map for the same commit: a changed
file -> the symbols compiled from it or from objects that included it ->
every row whose kernel set meets them. A row that is usable (all jobs
passed, nothing dropped) and meets none of them is a step the change
cannot reach through any kernel. A step with no row is a step the record
knows nothing about, and stays with the static map.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

TABLE_VERSION = 1


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


def build(a) -> int:
    root: Path = a.recordings
    job_state: dict[str, str] = {}
    if a.jobs:
        for j in json.loads(Path(a.jobs).read_text()):
            job_state[j["id"]] = j.get("state", "")

    names_index: dict[str, int] = {}
    names: list[str] = []
    rows: dict[str, dict] = {}
    for step_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        kernels: set[int] = set()
        jobs = 0
        procs = 0
        dropped = 0
        passed = True
        for job_dir in sorted(p for p in step_dir.iterdir() if p.is_dir()):
            files = sorted(job_dir.glob("kern.*.txt"))
            if not files:
                continue
            jobs += 1
            state = job_state.get(job_dir.name)
            if state and state != "passed":
                passed = False
            for f in files:
                procs += 1
                got, d, _clean = read_recording(f)
                dropped += d
                for n in got:
                    i = names_index.get(n)
                    if i is None:
                        i = names_index[n] = len(names)
                        names.append(n)
                    kernels.add(i)
        if jobs:
            rows[step_dir.name] = {
                "jobs": jobs,
                "passed": passed,
                "processes": procs,
                "dropped": dropped,
                "kernels": sorted(kernels),
            }

    table = {
        "version": TABLE_VERSION,
        "source": {
            "org": a.org,
            "pipeline": a.pipeline,
            "build": a.build,
            "commit": a.commit,
            "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(
                timespec="seconds"
            ),
        },
        "names": names,
        "rows": rows,
        "stats": {
            "steps": len(rows),
            "unique_kernels": len(names),
            "processes": sum(r["processes"] for r in rows.values()),
            "rows_passed": sum(1 for r in rows.values() if r["passed"]),
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


def load_map(path: Path) -> dict[str, set[str]]:
    """file (source or header) -> symbols reachable from it."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        d = json.load(f)
    reach: dict[str, set[str]] = defaultdict(set)
    for e in d["objects"]:
        for s in e["symbols"]:
            reach[e["source"]].add(s)
            for dep in e["deps"]:
                reach[dep].add(s)
    reach["__commit__"] = {d.get("commit", "")}
    return reach


def query(a) -> int:
    t = load_table(a.table)
    reach = load_map(a.map)
    map_commit = next(iter(reach.pop("__commit__")))
    names = t["names"]
    if map_commit and t["source"]["commit"] and map_commit != t["source"]["commit"]:
        print(
            f"note: map is for {map_commit[:12]}, table for {t['source']['commit'][:12]}; "
            "file-level attribution tolerates symbol churn, but treat drops as advisory"
        )
    row_sets = {k: {names[i] for i in r["kernels"]} for k, r in t["rows"].items()}
    for path in a.file:
        syms = reach.get(path, set())
        print(f"\n--- {path}: {len(syms)} symbols reachable")
        if not syms:
            print(
                "    not in the map: nothing compiled from it or included it -> static rule"
            )
            continue
        select, drop, unusable = [], [], []
        for k, r in t["rows"].items():
            if row_sets[k] & syms:
                select.append(k)
            elif r["passed"] and not r["dropped"]:
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
            f"{k[:48]:48} {r['jobs']:>4} {r['processes']:>5} {len(r['kernels']):>7} {'yes' if r['passed'] and not r['dropped'] else 'NO':>3}"
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("recordings", type=Path)
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
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
