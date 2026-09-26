# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""`ci-select pr N`: what the selector would run for a vLLM pull request.

Shadow only: nothing here changes what CI runs. It prints the comment it would
post; `--post` puts it on the pull request, editing its own earlier comment
(found by a marker) rather than adding one per run.

The comparison is against today's rules, through the same replica of the
generator the crosscheck scores against, at the PR's merge base.

`--results`, once the PR's CI has run, adds what happened: every failed job,
and whether the selector would have run it. A failed job it would have skipped
is checked against main's statuses around the PR's base, so a failure main
already had reads as pre-existing, not as a miss. GitHub statuses only; no
Buildkite token.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .codemap.classify import select
from .codemap.pipeline.match import match_jobs
from .codemap.worktree import git_out, state_for
from .coverage.source import fetch_kernel_evidence, fetch_table
from .decide import decide
from .gitdiff import changed_paths, diff_files
from .handwritten import IMAGE_BUILD_KEY_PREFIX, PR_PIPELINE
from .validate.crosscheck import (
    BUILDKITE_CONTEXT_PREFIX,
    GH_DEFAULT_BRANCH,
    GH_PR_REFSPEC,
    GH_REPO,
    GH_STATE_FAILED,
    NON_STEP_CONTEXTS,
    _gh_pr,
    _resolve_range,
    _upstream_remote,
)
from .validate.generator_replica import today_select

MARKER = "<!-- ci-selector-shadow -->"
EXPLAINER = "https://github.com/vllm-project/ci-infra/tree/main/buildkite/ci_selector"
# Main builds post each job as `buildkite/ci/<slug>`; PR builds as
# `buildkite/ci/pr/<slug>`, with the same slug.
MAIN_CONTEXT_PREFIX = "buildkite/ci/"
# Main commits whose statuses are read for a pre-existing failure: the ones
# nearest the main commit the PR's head branched from, since PR CI builds the
# head as is and so tests that tree. Not the selection's base: for a merged PR
# that is main at merge time, which can already carry the fix. vllm#55755's
# head predated both fixes its failures needed; its merge base was one of them.
MAIN_WINDOW_BEFORE = timedelta(hours=12)
MAIN_WINDOW_AFTER = timedelta(hours=36)
MAIN_COMMITS = 40
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
    results: Results | None = None


@dataclass
class FailedJob:
    slug: str
    # "ran": the selector would run it; "skipped": it would not; "unmapped":
    # no step explains the job, so neither side can be judged
    verdict: str
    # main commits (short) where the same job failed, for a skipped one
    main_failing: list[str] = field(default_factory=list)


@dataclass
class Results:
    passed: int = 0
    pending: int = 0
    failed: list[FailedJob] = field(default_factory=list)
    checked_at: str = ""


def select_for_pr(
    repo: Path,
    pr: int,
    remote: str | None = None,
    table=None,
    kernels=None,
    results: bool = False,
    gh=None,
) -> PrSelection:
    data = _gh_pr(pr)
    upstream = _upstream_remote(repo, remote)
    base, head = pr_range(repo, pr, data, upstream)
    if base is None:
        raise RuntimeError(f"PR #{pr} is {data['state']} with no head to select for")
    paths = changed_paths(diff_files(repo, base, head))
    check_drift(pr, len(set(paths)), _changed_files(pr))
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
    outcome = None
    if results:
        tested = _tested_base(repo, pr, data, _upstream_remote(repo, remote)) or base
        merge = (data.get("mergeCommit") or {}).get("oid")
        outcome = ci_results(
            data,
            [steps[i] for i in f_ids],
            list(steps.values()),
            _commit_time(repo, tested),
            gh or _gh,
            merged_at=_commit_time(repo, merge) if merge else None,
        )
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
        results=outcome,
    )


def pr_range(repo: Path, pr: int, data: dict, remote: str):
    """The PR's own diff: its head against the commit it branched from.

    The branch point comes from GitHub's compare API, not from the clone's
    main. Computed locally it is merge-base(head, <remote>/main), and a clone
    whose main is behind the PR's branch point turns the stale main tip into
    the base: on 2026-09-25 a clone a day behind put a day of main drift into
    10 of 11 one-to-three-file PRs, and its pyproject.toml made each "run
    everything". A merged PR keeps its merge commit against its parent, which
    is exactly the change it landed.
    """
    if data.get("state") == "MERGED" and data.get("mergeCommit"):
        return _resolve_range(repo, pr, data, remote)
    head = data.get("headRefOid")
    if not head:
        return None, None
    _ensure_commit(repo, remote, head, GH_PR_REFSPEC.format(pr=pr))
    base = github_merge_base(head)
    _ensure_commit(repo, remote, base, base)
    return base, head


def github_merge_base(head: str) -> str:
    return _gh(
        "api",
        f"repos/{GH_REPO}/compare/{GH_DEFAULT_BRANCH}...{head}",
        "--jq",
        ".merge_base_commit.sha",
    ).strip()


def _ensure_commit(repo: Path, remote: str, sha: str, refspec: str) -> None:
    """Fetch a commit the clone lacks. GitHub serves any reachable commit by
    sha, so the base needs no branch to be current."""
    have = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True,
    )
    if have.returncode:
        subprocess.run(
            ["git", "-C", str(repo), "fetch", "-q", remote, refspec],
            capture_output=True,
            check=True,
        )


def _changed_files(pr: int) -> int | None:
    try:
        return int(
            _gh(
                "pr",
                "view",
                str(pr),
                "--repo",
                GH_REPO,
                "--json",
                "changedFiles",
                "--jq",
                ".changedFiles",
            )
        )
    except (subprocess.CalledProcessError, ValueError):
        return None


def check_drift(pr: int, local: int, github: int | None) -> None:
    """Refuse a diff far bigger than the PR, whatever caused it: a comment
    comparing someone else's changes is worse than none. A rename counts twice
    locally, so some slack; drift is hundreds of files, not a handful."""
    if github is not None and local > 2 * github + 5:
        raise RuntimeError(
            f"PR #{pr}: the local diff has {local} files but GitHub says the PR "
            f"changes {github}, so this is not the PR's diff. Not posting it; "
            "send the command and output to the selector's owner."
        )


def _tested_base(repo: Path, pr: int, data: dict, remote: str) -> str | None:
    """The main commit the PR's head branched from: the tree its CI tested.

    GitHub's merge base, as for the diff, so a stale local main cannot move
    the window of main commits read for pre-existing failures.
    """
    head = data.get("headRefOid")
    if not head:
        return None
    try:
        _ensure_commit(repo, remote, head, GH_PR_REFSPEC.format(pr=pr))
        base = github_merge_base(head)
        _ensure_commit(repo, remote, base, base)
        return base
    except (subprocess.CalledProcessError, OSError):
        return None


def _commit_time(repo: Path, sha: str) -> datetime:
    return datetime.fromisoformat(
        git_out(repo, "show", "-s", "--format=%cI", sha).strip()
    )


def ran_on_pr(data: dict) -> dict[str, str]:
    """job slug -> state, from the PR's Buildkite status contexts."""
    ran = {}
    for c in data.get("statusCheckRollup") or []:
        ctx = c.get("context") or c.get("name") or ""
        if ctx.startswith(BUILDKITE_CONTEXT_PREFIX):
            slug = ctx[len(BUILDKITE_CONTEXT_PREFIX) :]
            if slug not in NON_STEP_CONTEXTS:
                ran[slug] = c.get("state") or c.get("conclusion") or ""
    return ran


def ci_results(
    data, selected_steps, all_steps, base_time, gh, merged_at=None
) -> Results:
    """What the PR's CI did, against what the selector would have run."""
    ran = ran_on_pr(data)
    failed = {r: st for r, st in ran.items() if st in GH_STATE_FAILED}
    out = Results(
        passed=sum(st == "SUCCESS" for st in ran.values()),
        pending=sum(st in ("PENDING", "EXPECTED", "") for st in ran.values()),
        checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )
    kept, not_kept, _ = match_jobs(failed, selected_steps)
    _, unmapped, _ = match_jobs(dict(not_kept), all_steps)
    skipped = sorted(r for r in not_kept if r not in unmapped)
    on_main = main_failures(skipped, base_time, gh, merged_at) if skipped else {}
    out.failed = (
        [FailedJob(r, "ran") for r in sorted(kept)]
        + [FailedJob(r, "skipped", on_main.get(r, [])) for r in skipped]
        + [FailedJob(r, "unmapped") for r in sorted(unmapped)]
    )
    return out


def _pages(raw: str) -> list:
    """`gh api --paginate` prints one JSON array per page, back to back."""
    items, decoder, i, raw = [], json.JSONDecoder(), 0, raw.strip()
    while i < len(raw):
        page, i = decoder.raw_decode(raw, i)
        items.extend(page)
        while i < len(raw) and raw[i].isspace():
            i += 1
    return items


def main_failures(
    slugs, base_time: datetime, gh, merged_at: datetime | None = None
) -> dict[str, list[str]]:
    """slug -> short shas of main commits whose latest status for that job is a
    failure, in a window around the PR's base.

    For a merged PR the window stops before its merge commit: main after the
    merge carries the PR's own change, so a failure the PR caused would fail
    there too and read as pre-existing.
    """
    since = (base_time - MAIN_WINDOW_BEFORE).astimezone(timezone.utc)
    until = min(base_time + MAIN_WINDOW_AFTER, datetime.now(timezone.utc))
    if merged_at is not None:
        until = min(until, merged_at - timedelta(seconds=1))
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    listed = _pages(
        gh(
            "api",
            "--paginate",
            f"repos/{GH_REPO}/commits?sha=main&per_page=100"
            f"&since={since.strftime(fmt)}&until={until.astimezone(timezone.utc).strftime(fmt)}",
        )
    )

    def distance(c) -> float:
        when = datetime.fromisoformat(
            c["commit"]["committer"]["date"].replace("Z", "+00:00")
        )
        return abs((when - base_time).total_seconds())

    shas = [c["sha"] for c in sorted(listed, key=distance)[:MAIN_COMMITS]]
    wanted = {MAIN_CONTEXT_PREFIX + s: s for s in slugs}
    hits: dict[str, list[str]] = {}
    for sha in shas:
        statuses = _pages(
            gh(
                "api",
                "--paginate",
                f"repos/{GH_REPO}/commits/{sha}/statuses?per_page=100",
            )
        )
        latest: dict[str, str] = {}
        for st in statuses:  # newest first, so the first per context is its state
            latest.setdefault(st.get("context", ""), st.get("state", ""))
        for ctx, slug in wanted.items():
            if latest.get(ctx) in ("failure", "error"):
                hits.setdefault(slug, []).append(sha[:10])
    return hits


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
        "evidence-based selector would pick for this PR, next to today's rules. "
        f"[How it works]({EXPLAINER}).",
        "",
        "**Feedback welcome:** reply here if it would skip a step this change "
        "needs, or runs something unrelated.",
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
        *(_render_results(s.results) if s.results else []),
        f"<sub>{s.files} changed files · base `{s.base[:10]}` · head `{s.head[:10]}` · "
        f"{' · '.join(s.records)} · not counted: {s.plumbing} build steps, "
        f"{s.never_emitted} A100 steps the generator no longer emits</sub>",
    ]
    return "\n".join(lines) + "\n"


def _render_results(r: Results) -> list[str]:
    ran = [f for f in r.failed if f.verdict == "ran"]
    skipped = [f for f in r.failed if f.verdict == "skipped"]
    unmapped = [f for f in r.failed if f.verdict == "unmapped"]
    misses = [f for f in skipped if not f.main_failing]
    lines = [
        f"#### CI results ({r.checked_at})",
        "",
        f"{r.passed} passed, {len(r.failed)} failed, {r.pending} pending.",
    ]
    if r.pending:
        lines.append("CI is still running; the picture below is not final.")
    if not r.failed:
        lines += ["", "No failures to judge.", ""]
        return lines
    if misses:
        verdict = f"**{len(misses)} possible miss(es):** failed here, the selector would have skipped them, and main was not failing them."
    elif skipped:
        verdict = "No misses: every failed job the selector would skip was also failing on main."
    else:
        verdict = "No misses: the selector would have run every failed job."
    lines += ["", verdict, ""]
    for f in ran:
        lines.append(f"- `{f.slug}`: selector runs it")
    for f in skipped:
        if f.main_failing:
            shas = ", ".join(f"`{x}`" for x in f.main_failing[:3])
            lines.append(
                f"- `{f.slug}`: selector would skip it; also failing on main ({shas}), pre-existing"
            )
        else:
            lines.append(
                f"- `{f.slug}`: **selector would skip it; not failing on main**"
            )
    for f in unmapped:
        lines.append(f"- `{f.slug}`: no step matches this job, not judged")
    lines.append("")
    return lines


def ledger_record(s: PrSelection) -> dict:
    """One JSON line per run, for tallying the trial across PRs."""
    key = lambda v: (v.name, v.mirror)  # noqa: E731
    sel = {key(v) for v in s.selector}
    today = {key(v) for v in s.today}
    main = lambda vs: [v for v in vs if not v.mirror]  # noqa: E731
    jobs = lambda vs: sum(v.jobs for v in vs)  # noqa: E731
    rec = {
        "pr": s.pr,
        "title": s.title,
        "base": s.base,
        "head": s.head,
        "files": s.files,
        "run_all": s.run_all,
        "today_steps": len(main(s.today)),
        "today_jobs": jobs(main(s.today)),
        "selector_steps": len(main(s.selector)),
        "selector_jobs": jobs(main(s.selector)),
        "would_skip": sorted(v.name for v in main(s.today) if key(v) not in sel),
        "would_add": sorted(v.name for v in main(s.selector) if key(v) not in today),
        "records": s.records,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if s.results:
        rec["results"] = asdict(s.results)
        rec["possible_misses"] = [
            f.slug
            for f in s.results.failed
            if f.verdict == "skipped" and not f.main_failing
        ]
    return rec


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
    comments = _pages(gh("api", "--paginate", f"repos/{GH_REPO}/issues/{pr}/comments"))
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
    result = select_for_pr(
        repo,
        args.number,
        args.remote,
        table=table,
        kernels=kernels,
        results=args.results,
    )
    body = render(result)
    if args.out:
        Path(args.out).write_text(body)
    if args.json_out:
        with open(args.json_out, "a") as f:
            f.write(json.dumps(ledger_record(result)) + "\n")
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
    p.add_argument(
        "--results",
        action="store_true",
        help="add the PR's CI outcome: failed jobs, and whether the selector would have run them",
    )
    p.add_argument(
        "--json-out", help="append one JSON line per run to this ledger file"
    )
    p.add_argument("--remote", help="git remote for vllm-project/vllm (auto-detected)")
    p.add_argument(
        "--no-fetch",
        action="store_true",
        help="use the records already in coverage-data/ instead of fetching the latest",
    )
