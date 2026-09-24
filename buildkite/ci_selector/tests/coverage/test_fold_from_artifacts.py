# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Folding a build from its artifacts, with no Buildkite API.

The collect step has no API token, so it reads the job list, step keys and job
states out of the artifacts instead and runs the same merge. Both ways of
folding a build have to produce the same rows.
"""

from __future__ import annotations

import gzip
import json
import tarfile
from pathlib import Path

import pytest
from ci_selector.coverage.joblog import PLUGIN_MARKER
from ci_selector.coverage.table import load
from ci_selector.scripts.build import (
    merge_build,
    merge_builds,
    sweep_from_artifacts,
    write_table,
)

from .helpers import Build, process_file

PASSED = (
    '{"event":"collected","selected":3,"deselected":0}\n'
    '{"event":"session","passed":3,"failed":0,"skipped":0,"selected":3,'
    '"exitstatus":0}\n'
)
FAILED = (
    '{"event":"collected","selected":3,"deselected":0}\n'
    '{"event":"session","passed":1,"failed":2,"skipped":0,"selected":3,'
    '"exitstatus":1}\n'
)


@pytest.fixture
def built(tmp_path: Path, tmp_repo):
    """One build, laid out both ways from the same recordings.

    `sweep` is what ci-fetch-build produces from the API. `artifacts` is what
    buildkite-agent downloads: a tarball per packed job, a directory otherwise.
    """
    sweep = Build(tmp_path / "sweep" / "b1", "77", tmp_repo.head())
    jobs = {
        "job-a": ("runs-it", [("mod.py", "plain")], PASSED, True),
        "job-b": ("runs-other", [("mod.py", "Holder.method")], PASSED, True),
        "job-c": ("was-killed", [("mod.py", "plain")], None, False),
        "job-d": ("tests-failed", [("mod.py", "plain")], FAILED, True),
    }
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    for job_id, (key, entries, sessions, packed) in jobs.items():
        # The API side: step key from index.json, state from the API.
        api_dir = sweep.job(job_id, step_key=key)
        process_file(
            api_dir / "fn.a.txt",
            entries,
            job=job_id,
            step_key=key,
            # A killed job leaves no #end marker.
            clean_exit=packed,
        )
        if sessions:
            (api_dir / "pytest.1.jsonl").write_text(sessions)
        (api_dir / PLUGIN_MARKER).write_text("")

        # The artifact side: the same files, as CI delivers them.
        staging = artifacts / job_id
        staging.mkdir()
        for f in sorted(api_dir.iterdir()):
            (staging / f.name).write_bytes(f.read_bytes())
        if packed:
            with tarfile.open(f"{artifacts / job_id}.tar.gz", "w:gz") as tf:
                for f in sorted(staging.iterdir()):
                    tf.add(f, arcname=f"{job_id}/{f.name}")
            for f in list(staging.iterdir()):
                f.unlink()
            staging.rmdir()

    return sweep.finish(), artifacts, tmp_repo


def rows_of(table_path: Path) -> dict:
    payload = json.loads(gzip.decompress(table_path.read_bytes()))
    return payload["rows"]


class TestEquivalence:
    def test_both_folds_produce_the_same_rows(self, built, tmp_path, tmp_repo):
        sweep_root, artifacts, repo = built
        offline = merge_build(sweep_root, repo.root)

        staged = tmp_path / "staged"
        sweep_from_artifacts(artifacts, staged, "77", repo.head(), "ci")
        in_ci = merge_builds([staged], repo.root)

        assert sorted(in_ci) == sorted(offline), "the same steps, keyed the same way"
        for key in offline:
            assert in_ci[key].functions == offline[key].functions, key
            assert in_ci[key].stamp.thin == offline[key].stamp.thin, (
                f"{key}: a row thin in one fold and not the other would be "
                "dropped by one table and run by the other"
            )

    def test_step_keys_come_out_of_the_recordings(self, built, tmp_path, tmp_repo):
        """No index.json, so the only source is each recording's #start line."""
        sweep_root, artifacts, repo = built
        staged = tmp_path / "staged"
        built_index = sweep_from_artifacts(artifacts, staged, "77", repo.head(), "ci")
        assert {j["step_key"] for j in built_index["jobs"]} == {
            "runs-it",
            "runs-other",
            "was-killed",
            "tests-failed",
        }


class TestPassFail:
    def index_for(self, artifacts, tmp_path, repo, name="staged"):
        staged = tmp_path / name
        return sweep_from_artifacts(artifacts, staged, "77", repo.head(), "ci")

    def test_a_job_that_never_packed_is_not_passed(self, built, tmp_path):
        """pack.sh names the tarball only once the step finished, so raw files
        mean it stopped early."""
        _, artifacts, repo = built
        jobs = {j["job"]: j for j in self.index_for(artifacts, tmp_path, repo)["jobs"]}
        assert jobs["job-c"]["state"] != "passed"
        assert jobs["job-a"]["state"] == "passed"

    def test_pytest_failures_mark_the_job_failed(self, built, tmp_path):
        """A step can exit 0 with failed tests, so pytest's own count decides."""
        _, artifacts, repo = built
        jobs = {j["job"]: j for j in self.index_for(artifacts, tmp_path, repo)["jobs"]}
        assert jobs["job-d"]["state"] != "passed", "packed cleanly, but tests failed"


class TestDelivery:
    def test_both_delivery_shapes_fold(self, built, tmp_path):
        """A step killed mid-tar leaves raw files, and both shapes have to fold
        the same."""
        _, artifacts, repo = built
        index = sweep_from_artifacts(artifacts, tmp_path / "s", "77", repo.head(), "ci")
        shapes = {j["job"]: j["artifact"] for j in index["jobs"]}
        assert shapes == {
            "job-a": "tar",
            "job-b": "tar",
            "job-c": "raw",
            "job-d": "tar",
        }
        for job in index["jobs"]:
            recs = list((tmp_path / "s" / "jobs" / job["job"] / "fnrec").iterdir())
            assert any(f.name.startswith("fn.") for f in recs), job["job"]

    def test_the_staged_sweep_builds_a_table(self, built, tmp_path):
        _, artifacts, repo = built
        staged = tmp_path / "s"
        sweep_from_artifacts(artifacts, staged, "77", repo.head(), "ci")
        out = tmp_path / "table.json.gz"
        write_table(merge_builds([staged], repo.root), out)
        table = load(out)
        assert table.available
        assert table.commit == repo.head()
        assert table.build == "77"


class TestPluginMarker:
    """Without the marker, a job that wrote no session file looks the same as
    one whose plugin never installed. Only the first is safe to drop from."""

    def fold(self, tmp_path, repo, marker: bool):
        art = tmp_path / f"art-{marker}"
        job = art / "job-x"
        job.mkdir(parents=True)
        process_file(job / "fn.a.txt", [("mod.py", "plain")], job="job-x", step_key="s")
        if marker:
            (job / PLUGIN_MARKER).write_text("")
        with tarfile.open(f"{art / 'job-x'}.tar.gz", "w:gz") as tf:
            for f in sorted(job.iterdir()):
                tf.add(f, arcname=f"job-x/{f.name}")
        for f in list(job.iterdir()):
            f.unlink()
        job.rmdir()
        staged = tmp_path / f"s-{marker}"
        sweep_from_artifacts(art, staged, "77", repo.head(), "ci")
        return merge_builds([staged], repo.root)["s"]

    def test_the_marker_makes_a_no_pytest_step_readable(self, tmp_path, tmp_repo):
        """A shell step runs no pytest and is still droppable."""
        assert not self.fold(tmp_path, tmp_repo, marker=True).stamp.thin

    def test_without_it_the_row_stays_thin(self, tmp_path, tmp_repo):
        """The plugin installs best-effort, so a partial payload must not
        publish rows that look healthy."""
        assert self.fold(tmp_path, tmp_repo, marker=False).stamp.thin


class TestFailureShapes:
    """A job the fold calls passed can drop steps. pytest reports several
    kinds of failure that never touch its `failed` count."""

    def fold_one(self, tmp_path, repo, session: str, name: str):
        art = tmp_path / name
        job = art / "job-x"
        job.mkdir(parents=True)
        process_file(job / "fn.a.txt", [("mod.py", "plain")], job="job-x", step_key="s")
        (job / "pytest.1.jsonl").write_text(session)
        (job / PLUGIN_MARKER).write_text("")
        with tarfile.open(f"{art / 'job-x'}.tar.gz", "w:gz") as tf:
            for f in sorted(job.iterdir()):
                tf.add(f, arcname=f"job-x/{f.name}")
        for f in list(job.iterdir()):
            f.unlink()
        job.rmdir()
        index = sweep_from_artifacts(
            art, tmp_path / f"s-{name}", "77", repo.head(), "ci"
        )
        return index["jobs"][0]

    def test_a_collection_error_is_not_passed(self, tmp_path, tmp_repo):
        """An import error in a test module: pytest exits 2 and reports one
        error, and no failures at all."""
        job = self.fold_one(
            tmp_path,
            tmp_repo,
            '{"event":"collected","selected":0,"deselected":0}\n'
            '{"event":"session","passed":0,"failed":0,"errors":1,"skipped":0,'
            '"selected":0,"testsfailed":1,"exitstatus":2}\n',
            "collect-error",
        )
        assert job["state"] != "passed"

    def test_a_nonzero_exit_is_not_passed(self, tmp_path, tmp_repo):
        """pytest exits 4 on a usage error and 5 when it collects nothing.
        Neither shows up in any outcome count."""
        job = self.fold_one(
            tmp_path,
            tmp_repo,
            '{"event":"collected","selected":0,"deselected":0}\n'
            '{"event":"session","passed":0,"failed":0,"errors":0,"skipped":0,'
            '"selected":0,"testsfailed":0,"exitstatus":4}\n',
            "usage-error",
        )
        assert job["state"] != "passed"

    def test_a_clean_run_is_still_passed(self, tmp_path, tmp_repo):
        job = self.fold_one(
            tmp_path,
            tmp_repo,
            '{"event":"collected","selected":3,"deselected":0}\n'
            '{"event":"session","passed":3,"failed":0,"errors":0,"skipped":0,'
            '"selected":3,"testsfailed":0,"exitstatus":0}\n',
            "clean",
        )
        assert job["state"] == "passed"


def test_a_recording_with_no_root_does_not_kill_the_fold(tmp_path, tmp_repo):
    """A forked child that recorded nothing writes a header with no root, and
    the reader returns nothing for it. One such file must not end the build."""
    art = tmp_path / "art"
    job = art / "job-y"
    job.mkdir(parents=True)
    (job / "fn.a-rootless.txt").write_text("#start\tpid=1\troot=\tpy=3.12.13\n")
    # Sorts after the rootless one, so the fold meets that first.
    process_file(job / "fn.b.txt", [("mod.py", "plain")], job="job-y", step_key="s")
    index = sweep_from_artifacts(art, tmp_path / "s", "77", tmp_repo.head(), "ci")
    assert index["jobs"][0]["step_key"] == "s", "the usable recording still files it"
