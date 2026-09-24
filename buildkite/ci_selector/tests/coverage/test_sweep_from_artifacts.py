# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""A recording build's own artifacts, turned into a sweep and merged.

The collect step builds the Python table from the downloaded `.fnrec/` (and
`.kernrec/`, when the kernel recorder ran too), with no API and no logs. These
tests lay out those directories the way the recorders and pack.sh leave them,
convert them, and run the real `merge_build` on the result, so the stamp
fields that gate drops are checked end to end.
"""

from __future__ import annotations

import json
import tarfile
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
    commit: str,
    exit_status=0,
    identity: str = "fnrec.json",
    pytest_lines: str | None = HEALTHY,
    marker: bool = True,
    shard: tuple[int, int] | None = None,
    packed: bool = True,
) -> None:
    """One job's recording as pack.sh leaves it: packed into <job>.tar.gz, or
    raw when packing never ran."""
    d = root / ".fnrec" / job_id
    process_file(d / "fn.host.abcd.10.txt", [("mod.py", "plain")], job=job_id)
    sidecar = {
        "step_key": step,
        "label": f":nvidia: {step}",
        "job_id": job_id,
        "build": "7",
        "commit": commit,
        "parallel_job": str(shard[0]) if shard else "",
        "parallel_job_count": str(shard[1]) if shard else "",
        "exit_status": exit_status,
    }
    if identity == "fnrec.json":
        (d / "fnrec.json").write_text(json.dumps(sidecar))
    elif identity == "kernrec.json":
        k = root / ".kernrec" / job_id
        k.mkdir(parents=True)
        (k / "kernrec.json").write_text(json.dumps(sidecar))
    elif identity == "header":
        # The recorder's own #start line names the step; no exit status.
        f = d / "fn.host.abcd.10.txt"
        lines = f.read_text().splitlines()
        lines[0] += f"\tBUILDKITE_STEP_KEY={step}\tBUILDKITE_LABEL=:nvidia: {step}"
        f.write_text("\n".join(lines) + "\n")
    if pytest_lines is not None:
        (d / "pytest.10.txt").write_text(pytest_lines)
    if marker:
        (d / "pytest.installed").write_text("")
    if packed:
        with tarfile.open(root / ".fnrec" / f"{job_id}.tar.gz", "w:gz") as tf:
            tf.add(d, arcname=job_id)
        for f in d.iterdir():
            f.unlink()
        d.rmdir()


def _merge(tmp_path, tmp_repo, jobs):
    root = tmp_path / "artifacts"
    (root / ".fnrec").mkdir(parents=True)
    commit = tmp_repo.head()
    for args in jobs:
        _job(root, commit=commit, **args)
    out = tmp_path / "sweep" / "ci-7"
    index = convert(
        root,
        out,
        build="7",
        commit=commit,
        pipeline="ci",
        source="schedule",
        env={"NIGHTLY": "1", "BUILDKITE_AGENT_ACCESS_TOKEN": "secret"},
    )
    return index, merge_build(out, tmp_repo.root), out


def test_a_packed_job_with_counts_makes_a_readable_row(tmp_path, tmp_repo):
    index, rows, out = _merge(
        tmp_path, tmp_repo, [dict(job_id="j1", step="good", exit_status=0)]
    )
    row = rows["good"]
    assert row.functions == {"vllm/mod.py": frozenset({"plain"})}
    assert not row.stamp.failed_jobs
    assert row.stamp.tests_passed == 2 and row.stamp.pytest_invocations == 1
    assert not row.stamp.thin
    assert row.stamp.sources == ["schedule"]
    assert row.stamp.build_env["NIGHTLY"] == "1"
    assert "secret" not in (out / "build.json").read_text()
    assert index["jobs"][0]["identity_from"] == "fnrec.json"


def test_a_raw_job_is_read_as_well(tmp_path, tmp_repo):
    _, rows, _ = _merge(
        tmp_path, tmp_repo, [dict(job_id="j1", step="raw", packed=False)]
    )
    assert rows["raw"].functions == {"vllm/mod.py": frozenset({"plain"})}


def test_the_kernel_sidecar_stands_in_when_pack_never_ran(tmp_path, tmp_repo):
    index, rows, _ = _merge(
        tmp_path,
        tmp_repo,
        [dict(job_id="j1", step="k", identity="kernrec.json", packed=False)],
    )
    assert not rows["k"].stamp.failed_jobs
    assert index["jobs"][0]["identity_from"] == "kernrec.json"


def test_header_identity_files_the_job_but_reads_as_failed(tmp_path, tmp_repo):
    """Only the recorder's header survived: the step is known, the outcome is
    not, and an unknown outcome must not authorize a drop."""
    index, rows, _ = _merge(
        tmp_path,
        tmp_repo,
        [dict(job_id="j1", step="h", identity="header", packed=False)],
    )
    assert rows["h"].stamp.failed_jobs == ["j1"]
    assert index["jobs"][0]["identity_from"] == "header"


def test_no_identity_is_left_out_and_counted(tmp_path, tmp_repo):
    index, rows, _ = _merge(
        tmp_path,
        tmp_repo,
        [
            dict(job_id="j1", step="good"),
            dict(job_id="j2", step="lost", identity="none", packed=False),
        ],
    )
    assert index["no_identity"] == ["j2"]
    assert set(rows) == {"good"}


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
        [dict(job_id="j1", step="noplug", pytest_lines=None, marker=False)],
    )
    assert rows["noplug"].stamp.logs_unreadable == 1
    assert rows["noplug"].stamp.thin


def test_plugin_present_and_no_pytest_is_a_non_pytest_step(tmp_path, tmp_repo):
    _, rows, _ = _merge(
        tmp_path, tmp_repo, [dict(job_id="j1", step="script", pytest_lines=None)]
    )
    stamp = rows["script"].stamp
    assert stamp.logs_unreadable == 0 and stamp.pytest_invocations == 0
    assert not stamp.thin


def test_all_skipped_is_thin(tmp_path, tmp_repo):
    _, rows, _ = _merge(
        tmp_path,
        tmp_repo,
        [dict(job_id="j1", step="skippy", pytest_lines=ALL_SKIPPED)],
    )
    assert rows["skippy"].stamp.jobs_ran_no_tests == 1 and rows["skippy"].stamp.thin


def test_shards_fold_into_one_complete_row(tmp_path, tmp_repo):
    _, rows, _ = _merge(
        tmp_path,
        tmp_repo,
        [
            dict(job_id="j1", step="sharded", shard=(0, 2)),
            dict(job_id="j2", step="sharded", shard=(1, 2)),
        ],
    )
    stamp = rows["sharded"].stamp
    assert sorted(stamp.shards_seen["7"]) == [0, 1]
    assert stamp.shards_expected["7"] == 2
    assert stamp.shards_complete


def test_a_corrupt_tarball_is_reported_not_guessed(tmp_path, tmp_repo):
    root = tmp_path / "artifacts"
    (root / ".fnrec").mkdir(parents=True)
    (root / ".fnrec" / "j9.tar.gz").write_bytes(b"not a tarball")
    index = convert(
        root, tmp_path / "out", build="7", commit=tmp_repo.head(), pipeline="ci", env={}
    )
    assert index["errors"] == ["unreadable tarball j9.tar.gz"]
    assert index["jobs"] == []


def test_a_sidecar_from_another_commit_is_reported(tmp_path, tmp_repo):
    root = tmp_path / "artifacts"
    (root / ".fnrec").mkdir(parents=True)
    _job(root, "j1", step="s", commit="deadbeef")
    index = convert(
        root, tmp_path / "out", build="7", commit=tmp_repo.head(), pipeline="ci", env={}
    )
    assert index["foreign_commits"] == ["deadbeef"]
