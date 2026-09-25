# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""`ci-select pr N`: what the selector would run for a vLLM pull request.

Shadow only: nothing here changes what CI runs. It prints the comment it would
post; `--post` puts it on the pull request, editing its own earlier comment
(found by a marker) rather than adding one per run.

The comparison is against today's rules, through the same replica of the
generator the crosscheck scores against, at the PR's merge base.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .codemap.classify import select
from .codemap.worktree import state_for
from .coverage.source import fetch_kernel_evidence, fetch_table
from .decide import decide
from .gitdiff import changed_paths, diff_files
from .handwritten import IMAGE_BUILD_KEY_PREFIX, PR_PIPELINE
from .validate.crosscheck import GH_REPO, _gh_pr, _resolve_range, _upstream_remote
from .validate.generator_replica import today_select

MARKER = "<!-- ci-selector-shadow -->"
# The generator's DeviceType.A100: the fleet is retired and only AMD mirrors
# of those steps are still emitted.
RETIRED_DEVICE = "a100"


def not_counted(step) -> str:
    """Why a step stays out of the counts, or "" when it is a test that runs.

    Build steps (every image build, not only the always-run ones) are the same
    on both sides and are not tests. A100 steps are still declared but the
    generator never emits them, so listing one would name a job nobody runs.
    """
    if step.always_runs or IMAGE_BUILD_KEY_PREFIX in (step.key or ""):
        return "build"
    if step.device == RETIRED_DEVICE and not step.mirror_hw:
        return "not emitted"
    return ""


@dataclass
class StepView:
    name: str
    jobs: int
    # an AMD (or other) mirror of an NVIDIA step, counted apart: no record
    # covers those yet, so they almost never change
    mirror: str = ""


@dataclass
class PrSelection:
    pr: int
    title: str
    base: str
    head: str
    files: int
    today: list[StepView] = field(default_factory=list)
    selector: list[StepView] = field(default_factory=list)
    # build steps, the same on both sides, and declared steps never emitted
    plumbing: int = 0
    never_emitted: int = 0
    today_run_all: bool = False
    run_all: str = ""
    docs_only: bool = False
    # (name, mirror) -> "kernel record" / "Python record" for record adds
    added_by: dict[tuple[str, str], str] = field(default_factory=dict)
    records: list[str] = field(default_factory=list)


def select_for_pr(
    repo: Path, pr: int, remote: str | None = None, table=None, kernels=None
) -> PrSelection:
    data = _gh_pr(pr)
    base, head = _resolve_range(repo, pr, data, _upstream_remote(repo, remote))
    if base is None:
        raise RuntimeError(f"PR #{pr} is {data['state']} with no head to select for")
    paths = changed_paths(diff_files(repo, base, head))
    state = state_for(repo, base)
    sel = select(state, paths, base=base, head=head)
    today = today_select([(p.config, p.steps) for p in state.pipelines], paths)
    d = decide(state, sel, repo, base, head, table=table, kernels=kernels)

    steps = {
        s.step_id: s
        for p in state.pipelines
        if p.config.name == PR_PIPELINE
        for s in p.steps
    }

    def name(step) -> str:
        # The key when the yaml has one; otherwise the label reads better than
        # the slug the generator derives from it.
        base = step.key or step.label
        return base.removesuffix(f"-{step.mirror_hw}") if step.mirror_hw else base

    def view(ids) -> list[StepView]:
        out = [
            StepView(
                name(steps[i]), steps[i].parallelism or 1, steps[i].mirror_hw or ""
            )
            for i in ids
            if i in steps and not not_counted(steps[i])
        ]
        return sorted(out, key=lambda v: (v.mirror, v.name))

    run_all = sel.run_all.get(PR_PIPELINE, "")
    f_ids = set(steps) if run_all else {s for s in d.steps if s in steps}
    t_ids = today.selected.get(PR_PIPELINE, set())
    added_by = {}
    for ids, who in (
        (d.added_by_kernels, "kernel record"),
        (d.added_by_coverage, "Python record"),
    ):
        for i in ids:
            if i in steps:
                added_by[(name(steps[i]), steps[i].mirror_hw or "")] = who
    if d.coverage_note:
        py = f"Python record: not used ({d.coverage_note})"
    else:
        py = f"Python record: build {table.build or '?'} at `{table.commit[:10]}`"
    if d.kernel_note:
        kern = f"kernel record: not used ({d.kernel_note})"
    else:
        kern = f"kernel record: {d.kernel_pair}"
    records = [py, kern]
    return PrSelection(
        pr=pr,
        title=data.get("title", ""),
        base=base,
        head=head,
        files=len(paths),
        today=view(t_ids),
        selector=view(f_ids),
        plumbing=sum(1 for s in steps.values() if not_counted(s) == "build"),
        never_emitted=sum(1 for s in steps.values() if not_counted(s) == "not emitted"),
        today_run_all=bool(today.run_all.get(PR_PIPELINE)),
        run_all=run_all,
        docs_only=today.docs_only,
        added_by=added_by,
        records=records,
    )


def _line(v: StepView, note: str = "") -> str:
    jobs = f" ×{v.jobs}" if v.jobs > 1 else ""
    return f"- `{v.name}`{jobs}" + (f" ({note})" if note else "")


def _details(summary: str, views: list[StepView], note=None) -> str:
    body = "\n".join(_line(v, note(v) if note else "") for v in views) or "none"
    return f"<details><summary>{summary} ({len(views)})</summary>\n\n{body}\n</details>"


def render(s: PrSelection) -> str:
    """The comment body. Markdown, with the long lists folded."""

    def jobs(vs):
        return sum(v.jobs for v in vs)

    def split(vs):
        return [v for v in vs if not v.mirror], [v for v in vs if v.mirror]

    key = lambda v: (v.name, v.mirror)  # noqa: E731
    today_keys = {key(v) for v in s.today}
    sel_keys = {key(v) for v in s.selector}
    skipped = [v for v in s.today if key(v) not in sel_keys]
    added = [v for v in s.selector if key(v) not in today_keys]
    (t_main, t_mir), (s_main, s_mir) = split(s.today), split(s.selector)
    (k_main, k_mir), (a_main, a_mir) = split(skipped), split(added)

    if s.run_all:
        head = f"### CI selector (shadow): would run everything ({s.run_all})"
    else:
        head = (
            f"### CI selector (shadow): {len(s_main)} test steps ({jobs(s_main)} jobs) "
            f"instead of {len(t_main)} ({jobs(t_main)} jobs)"
        )

    def row(label, t, sl, k, a):
        cell = lambda vs: f"{len(vs)} ({jobs(vs)})"  # noqa: E731
        return f"| {label} | {cell(t)} | {cell(sl)} | {cell(k)} | {cell(a)} |"

    lines = [
        MARKER,
        head,
        "",
        "Shadow mode: this changes nothing about what CI runs. It shows what the "
        "evidence-based selector would pick for this PR, next to today's rules.",
        "",
        "| steps (jobs) | Today's rules | Selector | Would skip | Would add |",
        "|---|---|---|---|---|",
        row("NVIDIA, CPU and others", t_main, s_main, k_main, a_main),
        row("AMD mirrors", t_mir, s_mir, k_mir, a_mir),
        "",
    ]
    if s.docs_only:
        lines += ["Docs-only change: today's rules run no tests.", ""]
    if s.today_run_all:
        lines += ["Today's rules run everything for this diff.", ""]
    lines += [
        _details("Selector would run", s_main),
        "",
        _details("Would skip (today's rules run them)", k_main),
        "",
        _details(
            "Would add (today's rules do not run them)",
            a_main,
            note=lambda v: s.added_by.get(key(v), "code map"),
        ),
        "",
        _details("AMD mirrors: would skip", k_mir),
        "",
        _details(
            "AMD mirrors: would add",
            a_mir,
            note=lambda v: s.added_by.get(key(v), "code map"),
        ),
        "",
        f"<sub>{s.files} changed files · base `{s.base[:10]}` · head `{s.head[:10]}` · "
        f"{' · '.join(s.records)} · not counted: {s.plumbing} build steps, "
        f"{s.never_emitted} A100 steps the generator no longer emits</sub>",
    ]
    return "\n".join(lines) + "\n"


def _gh(*args: str, input_text: str | None = None) -> str:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=True, input=input_text
    ).stdout


def post(pr: int, body: str, gh=_gh) -> str:
    """Create the comment, or edit the one this account posted before.

    Returns the comment's URL. Only a comment by the same login carrying the
    marker is edited, so nobody else's comment is ever touched.
    """
    login = gh("api", "user", "--jq", ".login").strip()
    raw = gh("api", "--paginate", f"repos/{GH_REPO}/issues/{pr}/comments")
    # --paginate concatenates one JSON array per page
    comments = []
    decoder = json.JSONDecoder()
    i = 0
    raw = raw.strip()
    while i < len(raw):
        page, j = decoder.raw_decode(raw, i)
        comments.extend(page)
        i = j
        while i < len(raw) and raw[i].isspace():
            i += 1
    mine = [
        c
        for c in comments
        if c.get("user", {}).get("login") == login and MARKER in (c.get("body") or "")
    ]
    payload = json.dumps({"body": body})
    if mine:
        out = gh(
            "api",
            "-X",
            "PATCH",
            f"repos/{GH_REPO}/issues/comments/{mine[-1]['id']}",
            "--input",
            "-",
            input_text=payload,
        )
    else:
        out = gh(
            "api",
            "-X",
            "POST",
            f"repos/{GH_REPO}/issues/{pr}/comments",
            "--input",
            "-",
            input_text=payload,
        )
    return json.loads(out).get("html_url", "")


def refresh_records() -> None:
    """Pull the latest published records into coverage-data/, as CI would."""
    from .scripts import fetch_functions, fetch_kernels

    for mod in (fetch_functions, fetch_kernels):
        rc = mod.main([])
        if rc:
            print(
                f"warning: {mod.__name__} exited {rc}; using what is on disk",
                file=sys.stderr,
            )


def run(args) -> int:
    repo = args.repo.resolve()
    if not args.no_fetch:
        refresh_records()
    table = fetch_table(args.table)
    kernels = fetch_kernel_evidence(args.kernel_table, args.kernel_symbol_map)
    result = select_for_pr(repo, args.number, args.remote, table=table, kernels=kernels)
    body = render(result)
    if args.out:
        Path(args.out).write_text(body)
    if not args.post:
        print(body)
        return 0
    url = post(args.number, body)
    print(f"posted: {url}")
    return 0


def add_args(p) -> None:
    p.add_argument("number", type=int, help="vLLM pull request number")
    p.add_argument(
        "--post", action="store_true", help="post (or update) the comment on the PR"
    )
    p.add_argument("--out", help="also write the comment body to this file")
    p.add_argument("--remote", help="git remote for vllm-project/vllm (auto-detected)")
    p.add_argument(
        "--no-fetch",
        action="store_true",
        help="use the records already in coverage-data/ instead of fetching the latest",
    )
