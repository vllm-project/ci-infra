# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Replay the confirmed selection leaks: would we have run the job that broke main?

`test-selection/selection-leaks.json` at the ci-infra root is the curated set
of jobs that did not run on a pull request and then failed on main because
of it, each with a narrow fix or revert confirming the cause. That is the
failure direction that matters, measured on real incidents rather than on
what happened to run, so it is the recall half of `crosscheck`.

Per row the question is whether the selector, at the culprit PR's base and
head, would have RUN the leaked job. Most leaked jobs are optional steps
that CI leaves blocked on a PR, so there are three answers:

    selected           the step is in the emitted selection: it runs
    optional reached   a rule reached the step, but it is optional and the
                       emitter leaves optional steps out, so it would not
                       run. The generator runs any step named in
                       VLLM_CI_ONLY_STEP_KEYS, optional or not, so emitting
                       these is a one-line policy in the selector; whether
                       static reach is precise enough to justify running an
                       8xH200 eval is the question that policy has to answer
    missed             nothing reached it

Today's rules score zero on this corpus by construction. Re-run after any
change to selection; the reach must never fall.

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
    """One culprit PR: select at merge^..merge and score each leaked job."""
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
    by_python = {key_of[s] for s in decision.added_by_coverage if s in key_of}
    by_kernels = {key_of[s] for s in decision.added_by_kernels if s in key_of}
    out_rows = []
    for r in rows:
        k = r["job_key"]
        sid = by_key.get(k)
        rules = (
            sorted(set(sel.selected_rules.get(sid, []) + sel.manual_rules.get(sid, [])))
            if sid
            else []
        )
        if k in final:
            verdict = "selected"
        elif k in manual:
            verdict = "optional reached"
        elif sid:
            verdict = "missed"
        else:
            verdict = "step absent at base"
        out_rows.append(
            {
                "id": r["id"],
                "job_key": k,
                "pr_ci_state": r["pr_ci"]["state"],
                "verdict": verdict,
                "codemap": k in auto or k in manual,
                # Which record put it in the final selection, if one did.
                "added_by": "python record"
                if k in by_python
                else "kernel record"
                if k in by_kernels
                else None,
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
        "added_by_python": len(decision.added_by_coverage),
        "dropped_by_python": len(decision.dropped_by_coverage),
        "rows": out_rows,
    }


def add_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--leaks", type=Path, default=DEFAULT_LEAKS)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument(
        "--table", type=Path, help="coverage table (default: the configured one)"
    )
    parser.add_argument(
        "--kernel-table", type=Path, help="kernel table (default: the configured one)"
    )
    parser.add_argument(
        "--kernel-symbol-map",
        type=Path,
        help="kernel symbol map (default: the configured one)",
    )


def run(args) -> int:
    repo = args.repo.resolve()
    records = json.loads(args.leaks.read_text())["records"]
    if not records:
        print("no records in the corpus; nothing to replay")
        return 1
    table = fetch_table(args.table)
    if not table.available:
        print(f"NOTE: {table.unavailable}")
    kernels = fetch_kernel_evidence(args.kernel_table, args.kernel_symbol_map)
    if kernels.unavailable:
        print(f"NOTE: {kernels.unavailable}")
    by_pr: OrderedDict[int, dict] = OrderedDict()
    for r in records:
        pr = r["culprit_pr"]["number"]
        by_pr.setdefault(pr, {"merge": r["culprit_pr"]["merge_commit"], "rows": []})[
            "rows"
        ].append(r)
    results = []
    tally = {
        "selected": 0,
        "optional reached": 0,
        "missed": 0,
        "step absent at base": 0,
    }
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
            if row.get("added_by"):
                rules += f" (added by the {row['added_by']})"
            print(
                f"    {row['verdict']:<20} {row['job_key']}  [pr ci: {row['pr_ci_state']}]{rules}",
                flush=True,
            )
    if args.json_out:
        args.json_out.write_text(json.dumps(results, indent=1))
    scored = sum(tally.values())
    print(
        f"\nTOTAL {scored} leaked jobs: selected {tally['selected']}, optional reached "
        f"{tally['optional reached']}, missed {tally['missed']}, step absent at base "
        f"{tally['step absent at base']}"
    )
    if scored:
        run = tally["selected"]
        reach = run + tally["optional reached"]
        print(
            f"  would run: {run}/{scored} ({run / scored:.0%}); with optional steps emitted: "
            f"{reach}/{scored} ({reach / scored:.0%}); today's rules by definition: 0/{scored}"
        )
    return 0 if scored else 1
