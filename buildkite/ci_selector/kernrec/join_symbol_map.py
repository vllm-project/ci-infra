#!/usr/bin/env python3
"""Join kernel recordings with a kernel symbol map: the edge the selector needs.

    join_symbol_map.py <kernel_symbol_map.json.gz> <recordings-dir> [--file csrc/x.cu ...]

<recordings-dir> is what analyze_build.py writes: <step_key>/<job-id>/kern.*.txt.

Reports, per step, how many recorded kernel names the map attributes to a
source file in the tree, and which files. Then, for each --file, the steps
whose recorded set contains a symbol from an object compiled from that file
(or from an object that included it as a header): the steps a change to that
file would select. With no --file it uses the file behind vLLM PR #55755.

Names the map cannot attribute are grouped by prefix (nccl, cublas, Triton,
deep_gemm, ...) so the unattributed remainder is legible rather than a number.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

DEFAULT_FILES = ["csrc/libtorch_stable/fused_qknorm_rope_kernel.cu"]

FAMILIES = [
    ("nccl", re.compile(r"nccl", re.IGNORECASE)),
    (
        "cublas/cublasLt",
        re.compile(
            r"cublas|gemmSN|gemvNSP|cutlass_80_|sm90_xmma|sm100_xmma|nvjet",
            re.IGNORECASE,
        ),
    ),
    ("cudnn", re.compile(r"cudnn", re.IGNORECASE)),
    ("deep_gemm", re.compile(r"deep_gemm|fp8_gemm|sm90_fp8|sm100_fp8", re.IGNORECASE)),
    ("flashinfer", re.compile(r"flashinfer", re.IGNORECASE)),
    (
        "torch/aten",
        re.compile(
            r"at_cuda_detail|at::native|^_ZN2at|^_ZN3c10|vectorized_elementwise|reduce_kernel|indexSelect|index_elementwise|elementwise_kernel",
            re.IGNORECASE,
        ),
    ),
    ("triton (plain name)", re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")),
]


def load_map(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        d = json.load(f)
    sym_to_files: dict[str, set[str]] = defaultdict(set)
    file_to_syms: dict[str, set[str]] = defaultdict(
        set
    )  # source or header -> symbols reachable
    for e in d["objects"]:
        for s in e["symbols"]:
            sym_to_files[s].add(e["source"])
            file_to_syms[e["source"]].add(s)
            for dep in e["deps"]:
                file_to_syms[dep].add(s)
    return d, sym_to_files, file_to_syms


def load_recordings(root: Path) -> dict[str, set[str]]:
    steps: dict[str, set[str]] = defaultdict(set)
    for f in root.glob("*/*/kern.*.txt"):
        step = f.parts[-3]
        steps[step].update(
            l
            for l in f.read_text(errors="replace").splitlines()
            if l and not l.startswith("#")
        )
    return steps


def family(name: str) -> str:
    for label, rx in FAMILIES:
        if rx.search(name):
            return label
    return "other"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("map", type=Path)
    ap.add_argument("recordings", type=Path)
    ap.add_argument("--file", action="append", default=[])
    ap.add_argument(
        "--show-unattributed",
        type=int,
        default=0,
        help="print N sample unattributed names per step",
    )
    a = ap.parse_args()

    d, sym_to_files, file_to_syms = load_map(a.map)
    steps = load_recordings(a.recordings)
    st = d.get("stats", {})
    print(
        f"map: commit={d.get('commit') or '?'} cuda={d.get('cuda') or '?'} objects={st.get('objects')} "
        f"device={st.get('device_objects')} symbols={st.get('symbols')}"
        + (f"  REASON: {d['reason']}" if d.get("reason") else "")
    )
    if not steps:
        sys.exit(f"no kern.*.txt under {a.recordings}")

    print(
        f"\n{'step':42} {'recorded':>8} {'attributed':>10} {'files':>5}  unattributed by family"
    )
    step_files: dict[str, Counter] = {}
    for step in sorted(steps):
        names = steps[step]
        hit = {n for n in names if n in sym_to_files}
        files = Counter(f for n in hit for f in sym_to_files[n])
        step_files[step] = files
        fam = Counter(family(n) for n in names - hit)
        fam_s = ", ".join(f"{k}={v}" for k, v in fam.most_common(4))
        print(f"{step:42} {len(names):>8} {len(hit):>10} {len(files):>5}  {fam_s}")
        if a.show_unattributed:
            for n in sorted(names - hit)[: a.show_unattributed]:
                print(f"      ? {n[:120]}")

    for path in a.file or DEFAULT_FILES:
        syms = file_to_syms.get(path, set())
        print(
            f"\n--- change to {path}: {len(syms)} symbols reachable (compiled from it or including it)"
        )
        if not syms:
            print("    not in the map: no object was compiled from it or included it")
            continue
        selected = sorted(s for s in steps if steps[s] & syms)
        dropped = sorted(s for s in steps if not (steps[s] & syms))
        print(f"    SELECT ({len(selected)}): " + ", ".join(selected))
        print(f"    drop   ({len(dropped)}): " + ", ".join(dropped))
    return 0


if __name__ == "__main__":
    sys.exit(main())
