"""Line-level evidence: a step that entered a changed function but never
reached its change is no longer read as running it."""

from __future__ import annotations

from pathlib import Path

import pytest
from ci_selector.coverage.changed_funcs import build
from ci_selector.coverage.lines import attach_line_probes, line_map, probes_at_base
from ci_selector.coverage.table import Evidence

from .helpers import Repo, make_table

SCHED = """\
def schedule(x, rare):
    total = 0
    for i in range(x):
        total += i
    if rare:
        total = -1
    return total
"""
# Line numbers in SCHED that run for a common and for a rare call.
COMMON_RUN = [1, 2, 3, 4, 5, 7]
RARE_RUN = [1, 2, 3, 5, 6, 7]


def _repo(tmp_path: Path, text: str = SCHED) -> Repo:
    root = tmp_path / "repo"
    root.mkdir()
    repo = Repo(root)
    repo.write("vllm/sched.py", text)
    repo.commit("base")
    return repo


def _table(tmp_path, repo, path="vllm/sched.py"):
    entries = [
        (path.removeprefix("vllm/"), "schedule"),
        (path.removeprefix("vllm/"), "<module>"),
    ]
    rel = path.removeprefix("vllm/")
    return make_table(
        tmp_path,
        repo,
        {"common": entries, "rare": entries},
        ran_lines={
            "common": [(rel, n) for n in COMMON_RUN],
            "rare": [(rel, n) for n in RARE_RUN],
        },
    )


def _query(repo, base, head, table):
    query = build(repo.root, base, head)
    attached = attach_line_probes(query, repo.root, base, head, table)
    return query, attached


def test_a_change_in_an_untaken_branch_drops_the_steps_that_never_took_it(tmp_path):
    """vllm#59029 changed one condition inside Scheduler.schedule and added
    112 steps, 2 of them needed: every step enters it, few take the branch."""
    repo = _repo(tmp_path)
    table = _table(tmp_path, repo)
    base = repo.head()
    repo.write("vllm/sched.py", SCHED.replace("total = -1", "total = -2"))
    head = repo.commit("edit")
    query, attached = _query(repo, base, head, table)
    assert attached == 1 and query.files[0].line_probes == {"schedule": {6}}
    assert table.look_up("common", query).evidence is Evidence.ABSENT_FROM_ROW
    assert table.look_up("rare", query).evidence is Evidence.EXECUTES_CHANGE


def test_without_line_records_the_function_grain_reading_stands(tmp_path):
    repo = _repo(tmp_path)
    entries = [("sched.py", "schedule"), ("sched.py", "<module>")]
    table = make_table(tmp_path, repo, {"common": entries})
    base = repo.head()
    repo.write("vllm/sched.py", SCHED.replace("total = -1", "total = -2"))
    head = repo.commit("edit")
    query, attached = _query(repo, base, head, table)
    assert attached == 0
    assert table.look_up("common", query).evidence is Evidence.EXECUTES_CHANGE


def test_lines_shifted_since_the_record_map_back_to_it(tmp_path):
    """The record is a day older than the PR's base. Lines added above the
    change move it, and it still maps to the line the record saw."""
    repo = _repo(tmp_path)
    table = _table(tmp_path, repo)  # recorded at this commit
    shifted = "import os\nimport sys\n\n\n" + SCHED
    repo.write("vllm/sched.py", shifted)
    base = repo.commit("later main")
    repo.write("vllm/sched.py", shifted.replace("total = -1", "total = -2"))
    head = repo.commit("edit")
    query, attached = _query(repo, base, head, table)
    assert query.files[0].line_probes == {"schedule": {6}}
    assert table.look_up("common", query).evidence is Evidence.ABSENT_FROM_ROW


def test_a_line_changed_since_the_record_falls_back_to_function_grain(tmp_path):
    repo = _repo(tmp_path)
    table = _table(tmp_path, repo)
    repo.write("vllm/sched.py", SCHED.replace("total = -1", "total = -3"))
    base = repo.commit("later main")
    repo.write("vllm/sched.py", SCHED.replace("total = -1", "total = -2"))
    head = repo.commit("edit")
    query, attached = _query(repo, base, head, table)
    assert attached == 0
    assert table.look_up("common", query).evidence is Evidence.EXECUTES_CHANGE


def test_line_map_shifts_unchanged_lines_and_refuses_changed_ones(tmp_path):
    repo = _repo(tmp_path)
    record = repo.head()
    repo.write(
        "vllm/sched.py", "# one\n" + SCHED.replace("return total", "return -total")
    )
    base = repo.commit("b")
    to_record = line_map(repo.root, record, base, "vllm/sched.py")
    assert to_record(7) == 6 and to_record(2) == 1
    assert to_record(8) is None  # the line that changed in between
    assert to_record(1) is None  # the line added in between


def _insert(after: int, text: str) -> str:
    lines = SCHED.splitlines(keepends=True)
    return "".join(lines[:after] + [text] + lines[after:])


@pytest.mark.parametrize(
    "hunk, head, probes",
    [
        # an insertion inside the branch, after its first statement
        ((6, 0, 7, 1), _insert(6, "        total -= 1\n"), {6}),
        # the same spot one level out: after the branch, not in it
        ((6, 0, 7, 1), _insert(6, "    total -= 1\n"), {5}),
        # an insertion that starts the branch: reached through its header
        ((5, 0, 6, 1), _insert(5, "        total -= 1\n"), {5}),
        # an insertion that starts the function: the next statement runs first
        ((1, 0, 2, 1), _insert(1, "    total = 1\n"), {2}),
        # the return line itself
        ((7, 1, 7, 1), SCHED.replace("return total", "return -total"), {7}),
        # no head text: every block touching the spot counts
        ((6, 0, 7, 1), None, {5, 6}),
    ],
    ids=[
        "inside-a-branch",
        "after-a-branch",
        "starts-a-branch",
        "starts-the-function",
        "changed-line",
        "ambiguous-without-head",
    ],
)
def test_probes_are_the_lines_a_run_reaching_the_change_runs(hunk, head, probes):
    assert probes_at_base(SCHED, "vllm/sched.py", [hunk], {"schedule"}, head) == {
        "schedule": probes
    }


def test_a_signature_change_is_read_at_function_grain():
    """A changed default changes every call, so entering the function is
    the right question."""
    assert probes_at_base(SCHED, "vllm/sched.py", [(1, 1, 1, 1)], {"schedule"}) == {}


def test_a_function_new_at_base_or_run_at_import_gets_no_probes():
    assert (
        probes_at_base(SCHED, "vllm/sched.py", [(6, 1, 6, 1)], {"fresh", "<module>"})
        == {}
    )
