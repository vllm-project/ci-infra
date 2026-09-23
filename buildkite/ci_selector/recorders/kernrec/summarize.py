#!/usr/bin/env python3
"""Summarize kernrec output for one run: processes seen, unique kernels,
dropped records, and whether an expected kernel name showed up.

    summarize.py <dir-with-kern.*.txt> [--expect SUBSTR ...] [--min-procs N]
                 [--label NAME] [--pytest-rc RC] [--seconds S] [--json OUT]

Exit status is 0 when every expectation held, 1 otherwise, so spike.sh can
tally passes. Names are shown demangled when c++filt is on PATH.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HEADER = re.compile(r"^# kernrec v1 pid=(\d+) ppid=(\d+) exe=(.*)$")
DROPPED = re.compile(r"^# dropped=(\d+)$")
END = re.compile(r"^# end records=(\d+) unique=(\d+) dropped=(\d+)$")


def read_dir(d: Path) -> dict:
    procs = []
    names: set[str] = set()
    for f in sorted(d.glob("kern.*.txt")):
        p = {
            "file": f.name,
            "pid": None,
            "exe": None,
            "unique": 0,
            "records": None,
            "dropped": 0,
            "clean_exit": False,
        }
        for line in f.read_text(errors="replace").splitlines():
            if not line:
                continue
            if line.startswith("#"):
                if m := HEADER.match(line):
                    p["pid"], p["exe"] = int(m[1]), m[3]
                elif m := DROPPED.match(line):
                    p["dropped"] += int(m[1])
                elif m := END.match(line):
                    p["records"], p["clean_exit"] = int(m[1]), True
                continue
            names.add(line)
            p["unique"] += 1
        procs.append(p)
    return {"procs": procs, "names": sorted(names)}


def demangle(names: list[str]) -> dict[str, str]:
    filt = shutil.which("c++filt")
    if not filt or not names:
        return {n: n for n in names}
    out = subprocess.run(
        [filt],
        input="\n".join(names) + "\n",
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    return dict(zip(names, out)) if len(out) == len(names) else {n: n for n in names}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir", type=Path)
    ap.add_argument(
        "--expect",
        action="append",
        default=[],
        help="substring that must appear in some recorded name",
    )
    ap.add_argument("--min-procs", type=int, default=1)
    ap.add_argument("--label", default="")
    ap.add_argument("--pytest-rc", type=int, default=None)
    ap.add_argument("--seconds", type=int, default=None)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--top", type=int, default=12)
    a = ap.parse_args()

    r = read_dir(a.dir)
    names, procs = r["names"], r["procs"]
    dm = demangle(names)
    checks = {}
    for e in a.expect:
        hits = [n for n in names if e in n or e in dm.get(n, "")]
        checks[f"expect:{e}"] = (bool(hits), f"{len(hits)} matching names")
    checks["min_procs"] = (
        len(procs) >= a.min_procs,
        f"{len(procs)} process files, need {a.min_procs}",
    )
    dropped = sum(p["dropped"] for p in procs)
    checks["no_dropped_records"] = (dropped == 0, f"{dropped} dropped")
    ok = all(v[0] for v in checks.values())

    print(
        f"--- {a.label or a.dir} : {'PASS' if ok else 'FAIL'}"
        + (f"  pytest rc={a.pytest_rc}" if a.pytest_rc is not None else "")
        + (f"  {a.seconds}s" if a.seconds is not None else "")
    )
    print(f"    processes={len(procs)} unique_kernels={len(names)}")
    for p in procs:
        exe = Path(p["exe"]).name if p["exe"] else "?"
        tail = "" if p["clean_exit"] else "  (no end marker: killed?)"
        print(
            f"      pid={p['pid']} exe={exe} unique={p['unique']} "
            f"records={p['records']} dropped={p['dropped']}{tail}"
        )
    for k, (good, why) in checks.items():
        print(f"    [{'ok' if good else 'XX'}] {k}: {why}")
    if names:
        print(f"    sample of {min(a.top, len(names))} names:")
        for n in names[: a.top]:
            print(f"      {dm.get(n, n)[:150]}")
    if a.json:
        a.json.write_text(
            json.dumps(
                {
                    "label": a.label,
                    "ok": ok,
                    "pytest_rc": a.pytest_rc,
                    "seconds": a.seconds,
                    "checks": checks,
                    "procs": procs,
                    "names": names,
                    "demangled": dm,
                },
                indent=1,
            )
        )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
