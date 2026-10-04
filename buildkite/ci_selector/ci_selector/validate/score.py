# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""score labelled PRs: precision and misses of the selection, against reviewers' verdicts

Each case in the labels file is one PR at a pinned base and head, the record
commit its review was made against, and the reviewers' verdict on steps: the
ones the change needs, the uncertain ones, and the unneeded ones. Steps nobody
named count as unneeded; reviewers looked for misses among everything skipped.

The selection is replayed with the code as it is now, so a change to the
selector can be scored before it merges. Only counted steps are scored: tests
on the PR pipeline, AMD mirrors and build steps left out, as the PR comment
counts them.

`--baseline` compares with an earlier run's JSON and lists every needed step
the new run drops, which is the number that matters.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

DEFAULT_LABELS = Path(__file__).resolve().parents[2] / "eval" / "labels.json"


def add_args(p) -> None:
    p.add_argument(
        "--repo",
        type=Path,
        required=True,
        help="vLLM clone holding every case's commits",
    )
    p.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    p.add_argument(
        "--records",
        type=Path,
        default=Path("coverage-data/by-commit"),
        help="cache of published records, one directory per record commit (fetched when missing)",
    )
    p.add_argument("--jobs", type=int, default=8, help="cases replayed in parallel")
    p.add_argument("--only", nargs="*", help="case ids or PR numbers to run")
    p.add_argument("--json-out", type=Path, help="write per-case results here")
    p.add_argument(
        "--baseline", type=Path, help="an earlier --json-out to compare with"
    )


def _records(cache: Path, commit: str) -> Path:
    from ..scripts import fetch_functions, fetch_kernels

    out = cache / commit
    if not (out / "table.json.gz").exists():
        fetch_functions.main(["--commit", commit, "--out", str(out)])
    if not (out / "kernel_table.json.gz").exists():
        fetch_kernels.main(["--commit", commit, "--out", str(out)])
    return out


def _counted(state):
    """name -> jobs for every counted step at this base, as the PR comment
    names and counts them."""
    from ..handwritten import PR_PIPELINE
    from ..pr_comment import not_counted

    steps = {
        s.step_id: s
        for p in state.pipelines
        if p.config.name == PR_PIPELINE
        for s in p.steps
        if not s.mirror_hw and not not_counted(s)
    }
    return steps, {i: (s.key or s.label) for i, s in steps.items()}


def score_case(case: dict, repo: Path, cache: Path) -> dict:
    from ..codemap.classify import select
    from ..codemap.worktree import state_for
    from ..coverage.source import fetch_kernel_evidence, fetch_table
    from ..decide import decide
    from ..gitdiff import changed_paths, diff_files
    from ..handwritten import PR_PIPELINE

    rec = _records(cache, case["record"])
    table = fetch_table(rec / "table.json.gz")
    kernels = fetch_kernel_evidence(
        rec / "kernel_table.json.gz", rec / "kernel_symbol_map.json.gz"
    )
    base, head = case["base"], case["head"]
    paths = changed_paths(diff_files(repo, base, head))
    state = state_for(repo, base)
    sel = select(state, paths, base=base, head=head)
    d = decide(state, sel, repo, base, head, table=table, kernels=kernels)
    steps, names = _counted(state)
    run_all = sel.run_all.get(PR_PIPELINE, "")
    ids = set(steps) if run_all else {i for i in d.steps if i in steps}
    chosen = {names[i]: steps[i].parallelism or 1 for i in ids}
    known = set(names.values())
    needed = set(case["needed"]) & known
    uncertain = set(case.get("uncertain", ())) & known - needed
    return {
        "id": case["id"],
        "pr": case["pr"],
        "run_all": run_all,
        "notes": {"python": d.coverage_note, "kernel": d.kernel_note},
        "selected": sorted(chosen),
        "jobs": sum(chosen.values()),
        "needed": sorted(needed),
        "needed_selected": sorted(needed & chosen.keys()),
        "missed": sorted(needed - chosen.keys()),
        "uncertain_selected": sorted(uncertain & chosen.keys()),
        "unneeded_selected": sorted(chosen.keys() - needed - uncertain),
        "unknown_labels": sorted(
            (set(case["needed"]) | set(case.get("uncertain", ()))) - known
        ),
    }


def _run_group(group: list[dict], repo: str, cache: str) -> list[dict]:
    out = []
    for case in group:
        try:
            out.append(score_case(case, Path(repo), Path(cache)))
        except Exception as exc:  # noqa: BLE001 - one bad case must not sink the run
            out.append(
                {
                    "id": case["id"],
                    "pr": case["pr"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return out


def summarize(results: list[dict]) -> dict:
    ok = [r for r in results if "error" not in r]
    narrow = [r for r in ok if not r["run_all"]]
    t = defaultdict(int)
    for r in narrow:
        t["steps"] += len(r["selected"])
        t["jobs"] += r["jobs"]
        t["needed"] += len(r["needed"])
        t["needed_selected"] += len(r["needed_selected"])
        t["uncertain_selected"] += len(r["uncertain_selected"])
        t["unneeded_selected"] += len(r["unneeded_selected"])
        t["missed"] += len(r["missed"])
    t["cases"] = len(ok)
    t["run_all_cases"] = len(ok) - len(narrow)
    t["errors"] = len(results) - len(ok)
    t["precision"] = round(t["needed_selected"] / t["steps"], 4) if t["steps"] else 0.0
    t["recall"] = round(t["needed_selected"] / t["needed"], 4) if t["needed"] else 0.0
    return dict(t)


def run(args) -> int:
    labels = json.loads(args.labels.read_text())["cases"]
    if args.only:
        want = set(args.only)
        labels = [c for c in labels if c["id"] in want or str(c["pr"]) in want]
    args.records.mkdir(parents=True, exist_ok=True)
    for commit in sorted({c["record"] for c in labels}):
        _records(args.records, commit)
    # Cases sharing a base run in one worker: the base worktree is built once
    # and never by two processes at the same time.
    by_base: dict[str, list[dict]] = defaultdict(list)
    for c in labels:
        by_base[c["base"]].append(c)
    results: list[dict] = []
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futs = [
            pool.submit(
                _run_group, g, str(args.repo.resolve()), str(args.records.resolve())
            )
            for g in by_base.values()
        ]
        for f in as_completed(futs):
            for r in f.result():
                results.append(r)
                tag = r.get("error") or (
                    f"{len(r['selected'])} steps, {len(r['needed_selected'])}/{len(r['needed'])} needed"
                )
                print(f"{r['id']}: {tag}", file=sys.stderr, flush=True)
    results.sort(key=lambda r: r["id"])
    summary = summarize(results)
    print(json.dumps(summary, indent=1))
    if args.baseline:
        before = {r["id"]: r for r in json.loads(args.baseline.read_text())["results"]}
        lost = []
        for r in results:
            b = before.get(r["id"])
            if b and "error" not in r and "error" not in b:
                lost += [
                    f"{r['id']}:{s}"
                    for s in set(b["needed_selected"]) - set(r["needed_selected"])
                ]
        print(f"needed steps dropped against the baseline: {len(lost)}")
        for x in sorted(lost):
            print(f"  {x}")
        print(f"baseline: {json.dumps(summarize(list(before.values())))}")
    if args.json_out:
        args.json_out.write_text(
            json.dumps({"summary": summary, "results": results}, indent=1)
        )
    return 1 if summary["errors"] else 0
