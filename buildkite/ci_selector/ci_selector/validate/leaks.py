# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Replay the confirmed selection leaks: would we have run the job that broke main?

`test-selection/selection-leaks.json` at the ci-infra root is the curated set
of jobs that did not run on a pull request and then failed on main because
of it, each with a narrow fix or revert confirming the cause. That is the
failure direction that matters, measured on real incidents rather than on
what happened to run, so it is the recall half of `crosscheck`.

Per row the question is whether the selector, at the culprit PR's base and
head, names the leaked job. Two answers, because most leaked jobs are
optional steps CI never runs on a PR without a manual unblock:

    selected     the step is in the auto selection: it would have run
    manual hit   a rule reached the step, but it is optional, so the
                 selector can only say "unblock this one"
    missed       nothing reached it

Usage:
  ci-validate leaks --repo /path/to/vllm [--leaks FILE] [--json-out out.json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import OrderedDict
from pathlib import Path

from ..codemap.classify import select
from ..codemap.worktree import git_out, state_for
from ..coverage.source import fetch_kernel_evidence, fetch_table
from ..decide import decide
from ..gitdiff import changed_paths, diff_files
from .crosscheck import base_in_window

DEFAULT_LEAKS = (
    Path(__file__).resolve().parents[4] / "test-selection" / "selection-leaks.json"
)


def _spellings(state) -> dict[str, str]:
    """step_id -> the key Buildkite runs it under, every pipeline."""
    return {s.step_id: s.buildkite_key for p in state.pipelines for s in p.steps}


def replay(repo: Path, merge: str, rows: list[dict], table=None, kernels=None) -> dict:
    base = git_out(repo, "rev-parse", f"{merge}^")
    head = merge
    if not base_in_window(repo, base):
        return {"merge": merge, "skip": "pre-restructure base", "rows": rows}
    paths = changed_paths(diff_files(repo, base, head))
    state = state_for(repo, base)
    sel = select(state, paths, base=base, head=head)
    decision = decide(state, sel, repo, base, head, table=table, kernels=kernels)
    key_of = _spellings(state)
    by_key: dict[str, str] = {}
    for sid, key in key_of.items():
        by_key.setdefault(key, sid)
    auto = {key_of[s] for s in sel.selected if s in key_of}
    manual = {key_of[s] for s in sel.manual_hits if s in key_of}
    final = {key_of[s] for s in decision.steps if s in key_of}
    out_rows = []
    for r in rows:
        k = r["job_key"]
        sid = by_key.get(k)
        rules = (
            sorted(set(sel.selected_rules.get(sid, []) + sel.manual_rules.get(sid, [])))
            if sid
            else []
        )
        verdict = (
            "selected"
            if k in final
            else "manual hit"
            if k in manual
            else "missed"
            if sid
            else "step absent at base"
        )
        out_rows.append(
            {
                "id": r["id"],
                "job_key": k,
                "pr_ci_state": r["pr_ci"]["state"],
                "verdict": verdict,
                "codemap": k in auto or k in manual,
                "rules": rules,
                "signature": r["main_failure"]["signature"],
            }
        )
    return {
        "merge": merge,
        "base": base,
        "files": len(paths),
        "csrc_files": sum(p.startswith("csrc/") for p in paths),
        "selected_steps": len(sel.selected),
        "final_steps": len(decision.steps),
        "run_all": bool(sel.run_all),
        "coverage_note": decision.coverage_note,
        "kernel_note": decision.kernel_note,
        "rows": out_rows,
    }


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--leaks", type=Path, default=DEFAULT_LEAKS)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument(
        "--table", type=Path, help="coverage table (default: the configured one)"
    )
    parser.add_argument("--kernel-table", type=Path)
    parser.add_argument("--kernel-symbol-map", type=Path)


def run(args) -> int:
    repo = args.repo.resolve()
    records = json.loads(args.leaks.read_text())["records"]
    if not records:
        print("no records in the corpus; nothing to replay")
        return 1
    table = fetch_table(args.table)
    kernels = fetch_kernel_evidence(args.kernel_table, args.kernel_symbol_map)
    for half, note in (
        ("coverage", table.unavailable),
        ("kernels", kernels.unavailable),
    ):
        if note:
            print(f"NOTE ({half}): {note}")
    by_pr: OrderedDict[int, dict] = OrderedDict()
    for r in records:
        pr = r["culprit_pr"]["number"]
        by_pr.setdefault(pr, {"merge": r["culprit_pr"]["merge_commit"], "rows": []})[
            "rows"
        ].append(r)
    results = []
    tally = {"selected": 0, "manual hit": 0, "missed": 0, "step absent at base": 0}
    print(f"{len(records)} leaked jobs across {len(by_pr)} pull requests\n")
    for pr, d in by_pr.items():
        try:
            res = replay(repo, d["merge"], d["rows"], table=table, kernels=kernels)
        except subprocess.CalledProcessError as e:
            res = {
                "merge": d["merge"],
                "skip": f"command failed: {(e.stderr or '')[:100]}",
                "rows": d["rows"],
            }
        res["pr"] = pr
        results.append(res)
        if "skip" in res:
            print(f"PR #{pr}: SKIP ({res['skip']})", flush=True)
            continue
        head = f"PR #{pr}  files {res['files']} (csrc {res['csrc_files']})  steps {res['selected_steps']}->{res['final_steps']}"
        head += "  RUN_ALL" if res["run_all"] else ""
        print(head, flush=True)
        for row in res["rows"]:
            tally[row["verdict"]] += 1
            rules = f" via {','.join(row['rules'])}" if row["rules"] else ""
            print(
                f"    {row['verdict']:<20} {row['job_key']}  [pr ci: {row['pr_ci_state']}]{rules}",
                flush=True,
            )
    if args.json_out:
        args.json_out.write_text(json.dumps(results, indent=1))
    scored = sum(tally.values())
    print(
        f"\nTOTAL {scored} leaked jobs: selected {tally['selected']}, manual hit {tally['manual hit']}, "
        f"missed {tally['missed']}, step absent at base {tally['step absent at base']}"
    )
    caught = tally["selected"] + tally["manual hit"]
    if scored:
        print(
            f"  reached by a rule: {caught}/{scored} ({caught / scored:.0%}); today's rules by definition: 0/{scored}"
        )
    return 0 if scored else 1
