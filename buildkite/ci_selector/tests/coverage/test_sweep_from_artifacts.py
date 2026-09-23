# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""A recording build's own artifacts, turned into a sweep and merged.

The collect step builds the Python table from `.fnrec/<job>/` alone, with no
API and no logs. These tests lay out that directory the way the recorders
write it, convert it, and run the real `merge_build` on the result, so the
stamp fields that gate drops are checked end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

from ci_selector.scripts.artifacts import convert
from ci_selector.scripts.build import merge_build

from .helpers import process_file

HEALTHY = "collected 2 items\n========== 2 passed in 0.50s ==========\n"
ALL_SKIPPED = "collected 2 items\n========== 2 skipped in 0.10s ==========\n"


def _job(
    root: Path,
    job_id: str,
    *,
    step: str,
    exit_status,
    commit: str,
    pytest_lines: str | None = HEALTHY,
    marker: bool = True,
    shard: tuple[int, int] | None = None,
    sidecar: bool = True,
) -> None:
    d = root / job_id
    process_file(d / "fn.10.txt", [("mod.py", "plain")], job=job_id)
    if sidecar:
        (d / "kernrec.json").write_text(
            json.dumps(
                {
                    "step_key": step,
                    "label": f":nvidia: {step}",
                    "job_id": job_id,
                    "build": "7",
                    "commit": commit,
                    "parallel_job": str(shard[0]) if shard else "",
                    "parallel_job_count": str(shard[1]) if shard else "",
                    "exit_status": exit_status,
                    "host": "agent-1",
                }
            )
        )
    if pytest_lines is not None:
        (d / "pytest.10.txt").write_text(pytest_lines)
    if marker:
        (d / "pytest.installed").write_text("")
    (d / "kern.10.txt").write_text("# kernrec v1 pid=10\nsome_kernel\n")


def _merge(tmp_path, tmp_repo, build_jobs):
    fnrec = tmp_path / "artifacts" / ".fnrec"
    fnrec.mkdir(parents=True)
    commit = tmp_repo.head()
    for args in build_jobs:
        _job(fnrec, commit=commit, **args)
    out = tmp_path / "sweep" / "ci-7"
    index = convert(
        fnrec,
        out,
        build="7",
        commit=commit,
        pipeline="ci",
        source="schedule",
        env={"NIGHTLY": "1", "BUILDKITE_AGENT_ACCESS_TOKEN": "secret"},
    )
    return index, merge_build(out, tmp_repo.root), out


def test_a_passed_job_with_counts_makes_a_readable_row(tmp_path, tmp_repo):
    index, rows, out = _merge(
        tmp_path,
        tmp_repo,
        [
            dict(job_id="j1", step="good", exit_status=0),
        ],
    )
    row = rows["good"]
    assert row.functions == {"vllm/mod.py": frozenset({"plain"})}
    assert not row.stamp.failed_jobs
    assert row.stamp.tests_passed == 2 and row.stamp.pytest_invocations == 1
    assert not row.stamp.thin
    assert row.stamp.sources == ["schedule"]
    assert row.stamp.build_env["NIGHTLY"] == "1"
    assert "secret" not in (out / "build.json").read_text()


def test_failed_and_killed_jobs_are_failed(tmp_path, tmp_repo):
    _, rows, _ = _merge(
        tmp_path,
        tmp_repo,
        [
            dict(job_id="j1", step="red", exit_status=1),
            dict(
                job_id="j2",
                step="killed",
                exit_status=None,
                pytest_lines="collected 2 items\n",
            ),
        ],
    )
    assert rows["red"].stamp.failed_jobs == ["j1"]
    assert rows["killed"].stamp.failed_jobs == ["j2"]
    assert rows["killed"].stamp.jobs_summary_unparsed == 1


def test_without_the_plugin_the_row_stays_thin(tmp_path, tmp_repo):
    _, rows, _ = _merge(
        tmp_path,
        tmp_repo,
        [
            dict(
                job_id="j1",
                step="noplug",
                exit_status=0,
                pytest_lines=None,
                marker=False,
            ),
        ],
    )
    assert rows["noplug"].stamp.logs_unreadable == 1
    assert rows["noplug"].stamp.thin


def test_plugin_present_and_no_pytest_is_a_non_pytest_step(tmp_path, tmp_repo):
    _, rows, _ = _merge(
        tmp_path,
        tmp_repo,
        [
            dict(job_id="j1", step="script", exit_status=0, pytest_lines=None),
        ],
    )
    stamp = rows["script"].stamp
    assert stamp.logs_unreadable == 0 and stamp.pytest_invocations == 0
    assert not stamp.thin


def test_all_skipped_is_thin(tmp_path, tmp_repo):
    _, rows, _ = _merge(
        tmp_path,
        tmp_repo,
        [
            dict(job_id="j1", step="skippy", exit_status=0, pytest_lines=ALL_SKIPPED),
        ],
    )
    assert rows["skippy"].stamp.jobs_ran_no_tests == 1 and rows["skippy"].stamp.thin


def test_shards_fold_into_one_complete_row(tmp_path, tmp_repo):
    _, rows, _ = _merge(
        tmp_path,
        tmp_repo,
        [
            dict(job_id="j1", step="sharded", exit_status=0, shard=(0, 2)),
            dict(job_id="j2", step="sharded", exit_status=0, shard=(1, 2)),
        ],
    )
    stamp = rows["sharded"].stamp
    assert sorted(stamp.shards_seen["7"]) == [0, 1] and stamp.shards_expected["7"] == 2
    assert stamp.shards_complete


def test_a_missing_shard_is_incomplete(tmp_path, tmp_repo):
    _, rows, _ = _merge(
        tmp_path,
        tmp_repo,
        [
            dict(job_id="j1", step="sharded", exit_status=0, shard=(0, 2)),
        ],
    )
    assert not rows["sharded"].stamp.shards_complete


def test_no_sidecar_is_left_out_and_counted(tmp_path, tmp_repo):
    index, rows, _ = _merge(
        tmp_path,
        tmp_repo,
        [
            dict(job_id="j1", step="good", exit_status=0),
            dict(job_id="j2", step="lost", exit_status=0, sidecar=False),
        ],
    )
    assert index["no_sidecar"] == ["j2"]
    assert set(rows) == {"good"}


def test_a_sidecar_from_another_commit_is_reported(tmp_path, tmp_repo):
    fnrec = tmp_path / ".fnrec"
    _job(fnrec, "j1", step="s", exit_status=0, commit="deadbeef")
    index = convert(
        fnrec,
        tmp_path / "out",
        build="7",
        commit=tmp_repo.head(),
        pipeline="ci",
        env={},
    )
    assert index["foreign_commits"] == ["deadbeef"]
