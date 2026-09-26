"""`ci-select pr N`: the comment body and the create-or-edit logic, offline."""

import json

from types import SimpleNamespace

from ci_selector.pr_comment import (
    MARKER,
    PrSelection,
    StepView,
    not_counted,
    post,
    render,
)


def _selection(**over):
    fields = dict(
        pr=1,
        title="t",
        base="b" * 40,
        head="h" * 40,
        files=2,
        today=[StepView("a", 1), StepView("b", 2), StepView("c", 1, "amd")],
        selector=[StepView("a", 1), StepView("d", 3), StepView("c", 1, "amd")],
        plumbing=8,
        added_by={("d", ""): "Python record"},
        records=["Python record: build 1 at `abc`", "kernel record: x"],
    )
    fields.update(over)
    return PrSelection(**fields)


def test_the_body_carries_the_marker_and_the_counts():
    body = render(_selection())
    assert body.startswith(MARKER), "the marker is how --post finds its own comment"
    assert "2 test steps (4 jobs) instead of 2 (3 jobs)" in body
    assert "| NVIDIA, CPU and others | 2 (3) | 2 (4) | 1 (2) | 1 (3) |" in body
    assert "| AMD mirrors | 1 (1) | 1 (1) | 0 (0) | 0 (0) |" in body


def test_skips_and_adds_are_listed_with_who_added_them():
    body = render(_selection())
    skip = body.split("Would skip (today's rules run them) (1)")[1].split("</details>")[
        0
    ]
    add = body.split("Would add (today's rules do not run them) (1)")[1].split(
        "</details>"
    )[0]
    assert "`b` ×2" in skip
    assert "`d` ×3 (Python record)" in add


def test_an_amd_mirror_is_never_confused_with_its_parent():
    """The same name on both sides, one a mirror: skipping the mirror is not
    skipping the parent."""
    s = _selection(
        today=[StepView("a", 1), StepView("a", 1, "amd")],
        selector=[StepView("a", 1)],
        added_by={},
    )
    body = render(s)
    assert "| NVIDIA, CPU and others | 1 (1) | 1 (1) | 0 (0) | 0 (0) |" in body
    assert "| AMD mirrors | 1 (1) | 0 (0) | 1 (1) | 0 (0) |" in body


def test_run_all_says_so():
    body = render(_selection(run_all="a changed file runs everything"))
    assert "would run everything (a changed file runs everything)" in body


class FakeGh:
    def __init__(self, comments_pages, login="me"):
        self.pages = comments_pages
        self.login = login
        self.calls = []

    def __call__(self, *args, input_text=None):
        self.calls.append((args, input_text))
        if args[:2] == ("api", "user"):
            return self.login + "\n"
        if "--paginate" in args:
            # gh prints one JSON array per page, back to back
            return "".join(json.dumps(p) for p in self.pages)
        return json.dumps({"html_url": "https://example/c"})


def _write_call(gh):
    ((args, payload),) = [c for c in gh.calls if "-X" in c[0]]
    return args, json.loads(payload)


def test_post_edits_its_own_marked_comment():
    gh = FakeGh(
        [
            [{"id": 1, "user": {"login": "someone"}, "body": MARKER + " theirs"}],
            [{"id": 2, "user": {"login": "me"}, "body": MARKER + " mine"}],
        ]
    )
    assert post(5, "new body", gh=gh) == "https://example/c"
    args, payload = _write_call(gh)
    assert args[2:4] == ("PATCH", "repos/vllm-project/vllm/issues/comments/2")
    assert payload == {"body": "new body"}


def test_post_creates_when_it_has_no_comment_yet():
    """Another account's marked comment is never edited."""
    gh = FakeGh([[{"id": 1, "user": {"login": "someone"}, "body": MARKER}], []])
    post(5, "body", gh=gh)
    args, _ = _write_call(gh)
    assert args[2:4] == ("POST", "repos/vllm-project/vllm/issues/5/comments")


def _step(key=None, device="h100", mirror_hw=None, always_runs=False):
    return SimpleNamespace(
        key=key, device=device, mirror_hw=mirror_hw, always_runs=always_runs
    )


def test_build_steps_and_retired_a100_steps_are_not_counted():
    assert not_counted(_step("image-build", always_runs=True)) == "build"
    assert not_counted(_step("arm64-image-build")) == "build", (
        "an image build the generator does not always run is still not a test"
    )
    assert not_counted(_step("batch-invariance-a100", device="a100")) == "not emitted"
    assert not_counted(_step("x-a100", device="a100", mirror_hw="amd")) == "", (
        "the AMD mirror of an A100 step is still emitted"
    )
    assert not_counted(_step("kernels-core-operation-test")) == ""


# ---- --results: the PR's CI outcome against the selection -----------------

from datetime import datetime, timedelta, timezone  # noqa: E402

from ci_selector.pr_comment import (  # noqa: E402
    FailedJob,
    Results,
    ci_results,
    ledger_record,
    main_failures,
)


def _job_step(key):
    return SimpleNamespace(
        step_id=f"vllm_ci:{key}",
        key=key,
        label=key,
        mirror_hw=None,
        mirror_label=None,
        device=None,
    )


def _pr_data(**states):
    return {
        "statusCheckRollup": [
            {"context": f"buildkite/ci/pr/{slug}", "state": st}
            for slug, st in states.items()
        ]
        + [{"context": "buildkite/ci/pr/bootstrap", "state": "FAILURE"}]
    }


BASE = datetime(2026, 9, 8, 3, 0, tzinfo=timezone.utc)


class MainGh:
    """Main's commit list and per-commit statuses, newest status first."""

    def __init__(self, commits, statuses):
        self.commits = commits  # [(sha, datetime)]
        self.statuses = statuses  # sha -> [(context, state)], newest first
        self.urls = []

    def __call__(self, *args, input_text=None):
        url = args[-1]
        self.urls.append(url)
        if "/commits?" in url:
            return json.dumps(
                [
                    {"sha": sha, "commit": {"committer": {"date": when.isoformat()}}}
                    for sha, when in self.commits
                ]
            )
        sha = url.split("/commits/")[1].split("/")[0]
        return json.dumps(
            [{"context": c, "state": s} for c, s in self.statuses.get(sha, [])]
        )


def test_failed_jobs_are_split_by_what_the_selector_would_run():
    data = _pr_data(kept="FAILURE", dropped="FAILURE", odd="FAILURE", fine="SUCCESS")
    gh = MainGh([("m1" * 20, BASE)], {"m1" * 20: [("buildkite/ci/dropped", "failure")]})
    r = ci_results(
        data,
        [_job_step("kept")],
        [_job_step("kept"), _job_step("dropped"), _job_step("fine")],
        BASE,
        gh,
    )
    got = {f.slug: (f.verdict, f.main_failing) for f in r.failed}
    assert got == {
        "kept": ("ran", []),
        "dropped": ("skipped", ["m1m1m1m1m1"]),
        "odd": ("unmapped", []),
    }, "bootstrap is not a step, and a job no step explains is not judged"
    assert (r.passed, r.pending) == (1, 0)


def test_only_the_latest_main_status_counts():
    """A job that failed on main and then passed on a rerun is not failing."""
    gh = MainGh(
        [("a" * 40, BASE)],
        {"a" * 40: [("buildkite/ci/x", "success"), ("buildkite/ci/x", "failure")]},
    )
    assert main_failures(["x"], BASE, gh) == {}


def test_main_after_the_merge_is_never_read():
    """Main after the merge carries the PR's change; a failure it caused would
    read as pre-existing there."""
    merged = BASE + timedelta(hours=2)
    gh = MainGh([], {})
    main_failures(["x"], BASE, gh, merged_at=merged)
    (listing,) = [u for u in gh.urls if "/commits?" in u]
    assert "until=2026-09-08T04:59:59Z" in listing


def test_the_nearest_main_commits_are_the_ones_read():
    commits = [(f"{i:040d}", BASE + timedelta(hours=i)) for i in range(60)]
    gh = MainGh(commits, {})
    main_failures(["x"], BASE, gh)
    read = [u for u in gh.urls if "/statuses" in u]
    assert len(read) == 40
    assert any(f"{0:040d}" in u for u in read), "the base's own commit is read"
    assert not any(f"{59:040d}" in u for u in read), "the farthest is not"


def test_the_results_section_names_misses_and_pre_existing_failures():
    s = _selection(
        results=Results(
            passed=10,
            failed=[
                FailedJob("kept", "ran"),
                FailedJob("old", "skipped", ["abc1234567"]),
                FailedJob("new", "skipped"),
            ],
            checked_at="now",
        )
    )
    body = render(s)
    assert "**1 possible miss(es):**" in body
    assert "`old`: selector would skip it; also failing on main (`abc1234567`)" in body
    assert "`new`: **selector would skip it; not failing on main**" in body
    rec = ledger_record(s)
    assert rec["possible_misses"] == ["new"]
    assert rec["selector_steps"] == 2 and rec["today_steps"] == 2


def test_no_failures_reads_as_nothing_to_judge():
    body = render(_selection(results=Results(passed=3, checked_at="now")))
    assert "No failures to judge." in body


# ---- the PR's range: main is fetched first, and drift is refused ------------

import pytest  # noqa: E402

from ci_selector import pr_comment  # noqa: E402
from ci_selector.pr_comment import check_drift  # noqa: E402


def test_drift_far_beyond_the_pr_is_refused():
    check_drift(1, 5, 3)  # renames count twice locally: fine
    check_drift(1, 400, None)  # GitHub unreachable: nothing to compare with
    with pytest.raises(RuntimeError, match="not the PR's diff"):
        check_drift(1, 400, 2)


def test_an_open_prs_base_comes_from_github_not_the_clones_main(monkeypatch, tmp_path):
    """2026-09-25: a clone a day behind put a day of main into every diff.
    The base is GitHub's merge base for the head; the clone's main is never
    consulted, and a diff that still dwarfs the PR is refused."""
    monkeypatch.setattr(
        pr_comment, "_gh_pr", lambda pr: {"state": "OPEN", "headRefOid": "h" * 40}
    )
    monkeypatch.setattr(pr_comment, "_upstream_remote", lambda repo, r: "origin")

    def no_local_main(*a):
        raise AssertionError("the clone's main must not decide the base")

    monkeypatch.setattr(pr_comment, "_resolve_range", no_local_main)
    fetched = []
    monkeypatch.setattr(
        pr_comment, "_ensure_commit", lambda repo, remote, sha, ref: fetched.append(sha)
    )
    monkeypatch.setattr(pr_comment, "github_merge_base", lambda head: "m" * 40)
    seen = {}

    def diff_files(repo, base, head):
        seen["range"] = (base, head)

    monkeypatch.setattr(pr_comment, "diff_files", diff_files)
    monkeypatch.setattr(
        pr_comment, "changed_paths", lambda d: [f"f{i}.py" for i in range(300)]
    )
    monkeypatch.setattr(pr_comment, "_changed_files", lambda pr: 2)
    with pytest.raises(
        RuntimeError, match="300 files but GitHub says the PR changes 2"
    ):
        pr_comment.select_for_pr(tmp_path, 1)
    assert seen["range"] == ("m" * 40, "h" * 40)
    assert fetched == ["h" * 40, "m" * 40], "both ends are made local by sha"


def test_a_merged_pr_keeps_its_merge_commit_against_its_parent(monkeypatch):
    data = {"state": "MERGED", "mergeCommit": {"oid": "c" * 40}}
    monkeypatch.setattr(
        pr_comment, "_resolve_range", lambda repo, pr, d, remote: ("p" * 40, "c" * 40)
    )
    monkeypatch.setattr(
        pr_comment,
        "github_merge_base",
        lambda head: (_ for _ in ()).throw(AssertionError("not for a merged PR")),
    )
    assert pr_comment.pr_range(None, 1, data, "origin") == ("p" * 40, "c" * 40)
