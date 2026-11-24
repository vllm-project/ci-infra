"""Tests for pytest_retry: JSONL records, active retry chain, 3-tier skip plan,
and pre-pytest preparation."""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

import pytest_retry as pr


def _write_jsonl(folder: Path, job_id: str, position: int, lines: list) -> Path:
    path = folder / ".runtime-shard-coverage" / job_id / f"{position}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for line in lines:
            if isinstance(line, str):
                f.write(line)
            else:
                f.write(json.dumps(line) + "\n")
    return path


def _start(
    step="k",
    position=0,
    shard=0,
    retry=0,
    build_id="b1",
    mode="full",
    expected=None,
    planned=None,
    skipped_command=False,
):
    return {
        "event": "start",
        "v": 2,
        "build_id": build_id,
        "job_id": f"job-{retry}",
        "step": step,
        "position": position,
        "shard": shard,
        "retry": retry,
        "mode": mode,
        "expected": expected,
        "planned": planned,
        "skipped_command": skipped_command,
    }


def _test(nodeid: str, passed: bool, outcome: str = "passed"):
    return {"event": "test", "nodeid": nodeid, "passed": passed, "outcome": outcome}


def _finish(finished: bool = True, at: float = 10.0):
    return {"event": "finish", "finished": finished, "finished_at": at}


# --- JSONL & legacy JSON parsing ---


def test_parse_jsonl_complete_record(tmp_path):
    path = _write_jsonl(
        tmp_path,
        "job-0",
        0,
        [
            _start(expected=["a.py::t1", "a.py::t2"], planned=["a.py::t1", "a.py::t2"]),
            _test("a.py::t1", True, "passed"),
            _test("a.py::t2", False, "failed"),
            _finish(True, 123.5),
        ],
    )
    record = pr.parse_record_file(str(path))
    assert record == {
        "step": "k",
        "position": 0,
        "shard": 0,
        "retry": 0,
        "build_id": "b1",
        "mode": "full",
        "finished": True,
        "finished_at": 123.5,
        "exitstatus": None,
        "expected": ["a.py::t1", "a.py::t2"],
        "planned": ["a.py::t1", "a.py::t2"],
        "ran": ["a.py::t1", "a.py::t2"],
        "passed": ["a.py::t1"],
        "passed_counts": {"a.py::t1": 1},
        "failed": ["a.py::t2"],
        "skipped_command": False,
    }


def test_parse_jsonl_mid_kill_preserves_flushed_passes_and_drops_truncated_tail(tmp_path):
    path = _write_jsonl(
        tmp_path,
        "job-0",
        0,
        [
            _start(planned=["a.py::t1", "a.py::t2", "a.py::t3"]),
            _test("a.py::t1", True, "passed"),
            '{"event": "test", "nodeid": "a.py::t2", "pas',  # killed mid-write
        ],
    )
    record = pr.parse_record_file(str(path))
    assert record is not None
    assert record["finished"] is False
    assert record["ran"] == ["a.py::t1"]
    assert record["passed"] == ["a.py::t1"]
    assert record["planned"] == ["a.py::t1", "a.py::t2", "a.py::t3"]


def test_parse_jsonl_rejects_missing_start_or_corrupt_header(tmp_path):
    p1 = _write_jsonl(tmp_path, "j1", 0, [_test("a.py::t1", True)])
    p2 = _write_jsonl(tmp_path, "j2", 0, ["not json\n"])
    p3 = _write_jsonl(tmp_path, "j3", 0, [{"event": "start", "step": "k", "position": True}])
    assert pr.parse_record_file(str(p1)) is None
    assert pr.parse_record_file(str(p2)) is None
    assert pr.parse_record_file(str(p3)) is None


def test_parse_legacy_json_record_is_backwards_compatible(tmp_path):
    path = tmp_path / ".runtime-shard-coverage" / "job-0" / "0.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "step": "k",
                "position": 0,
                "shard": 1,
                "retry": 0,
                "build_id": "b1",
                "finished": True,
                "finished_at": 50.0,
                "expected": ["a.py::t1"],
                "planned": ["a.py::t1"],
                "ran": ["a.py::t1"],
            }
        )
    )
    record = pr.parse_record_file(str(path))
    assert record is not None
    assert record["mode"] == "full"
    assert record["ran"] == ["a.py::t1"]
    # Legacy #884 records did not prove 3-phase pass, so passed must stay empty.
    assert record["passed"] == []
    assert record["skipped_command"] is False


def test_read_records_reads_both_jsonl_and_json_and_skips_bad_files(tmp_path):
    _write_jsonl(tmp_path, "job-0", 0, [_start(), _test("a.py::t1", True), _finish()])
    bad = tmp_path / ".runtime-shard-coverage" / "job-bad" / "0.jsonl"
    bad.parent.mkdir(parents=True)
    bad.write_text("corrupt")
    legacy = tmp_path / ".runtime-shard-coverage" / "job-leg" / "1.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps(
            {
                "step": "k",
                "position": 1,
                "shard": 0,
                "retry": 0,
                "finished": True,
                "finished_at": 1.0,
                "ran": ["b.py::t1"],
            }
        )
    )
    records = pr.read_records(str(tmp_path))
    assert sorted(r["position"] for r in records) == [0, 1]


# --- Active retry chain & 3-tier retry plan ---


def _rec(retry, mode="full", finished=True, passed=(), ran=None, planned=None, expected=None, build_id="b1"):
    return {
        "step": "k",
        "position": 0,
        "shard": 0,
        "retry": retry,
        "build_id": build_id,
        "mode": mode,
        "finished": finished,
        "finished_at": float(retry + 1),
        "expected": expected,
        "planned": list(planned) if planned is not None else None,
        "ran": list(passed) if ran is None else list(ran),
        "passed": list(passed),
        "failed": [],
        "skipped_command": False,
    }


def test_active_chain_walks_back_to_newest_full_attempt():
    records = [
        _rec(0, "full", passed=["a.py::t1"], planned=["a.py::t1", "a.py::t2", "a.py::t3"]),
        _rec(1, "full", passed=["a.py::t2"], planned=["a.py::t1", "a.py::t2", "a.py::t3"]),  # /ci retry resets pass history!
        _rec(2, "test-retry", passed=["a.py::t3"], planned=["a.py::t1", "a.py::t3"]),
    ]
    chain = pr.active_chain(records, "k", 0, 0, build_id="b1", before_retry=3)
    assert [r["retry"] for r in chain] == [1, 2]
    plan = pr.build_retry_plan(records, "k", 0, 0, build_id="b1", current_retry=3)
    assert plan["passed"] == {"a.py::t2", "a.py::t3"}


def test_active_chain_stops_at_missing_retry_gap_and_runs_full():
    # Attempt 1 died before writing any record -> Attempt 2 must not reuse Attempt 0!
    records = [_rec(0, "full", passed=["a.py::t1"], planned=["a.py::t1", "a.py::t2"])]
    chain = pr.active_chain(records, "k", 0, 0, build_id="b1", before_retry=2)
    assert chain == []
    plan = pr.build_retry_plan(records, "k", 0, 0, build_id="b1", current_retry=2)
    assert plan["passed"] == set()
    assert plan["skip_command"] is False
    assert plan["skip_files"] == set()


def test_active_chain_ignores_other_build_ids():
    records = [_rec(0, "full", passed=["a.py::t1"], build_id="old-build")]
    plan = pr.build_retry_plan(records, "k", 0, 0, build_id="new-build", current_retry=1)
    assert plan["passed"] == set()


def test_build_retry_plan_tier1_skips_command_only_when_finished_and_all_planned_passed():
    records = [
        _rec(
            0,
            "full",
            finished=True,
            passed=["a.py::t1", "b.py::t2"],
            planned=["a.py::t1", "b.py::t2"],
            expected=["a.py::t1", "b.py::t2"],
        )
    ]
    plan = pr.build_retry_plan(records, "k", 0, 0, build_id="b1", current_retry=1)
    assert plan["skip_command"] is True
    assert plan["skip_files"] == {"a.py", "b.py"}
    assert plan["has_expected"] is True


def test_build_retry_plan_does_not_skip_command_when_test_was_skipped_or_failed():
    # t2 ran (e.g. pytest.skip) so it's in ran/planned, NOT in passed -> must rerun!
    records = [
        _rec(
            0,
            "full",
            finished=True,
            passed=["a.py::t1"],
            ran=["a.py::t1", "b.py::t2"],
            planned=["a.py::t1", "b.py::t2"],
        )
    ]
    plan = pr.build_retry_plan(records, "k", 0, 0, build_id="b1", current_retry=1)
    assert plan["skip_command"] is False
    # Tier 2: a.py passed completely so it skips; b.py did not so it collects!
    assert plan["skip_files"] == {"a.py"}


def test_build_retry_plan_unfinished_attempt_skips_completed_files_not_command():
    # Attempt 0 killed mid-run after finishing a.py, before reaching b.py
    records = [
        _rec(
            0,
            "full",
            finished=False,
            passed=["a.py::t1", "a.py::t2"],
            planned=["a.py::t1", "a.py::t2", "b.py::t1"],
        )
    ]
    plan = pr.build_retry_plan(records, "k", 0, 0, build_id="b1", current_retry=1)
    assert plan["skip_command"] is False
    assert plan["skip_files"] == {"a.py"}
    assert plan["passed"] == {"a.py::t1", "a.py::t2"}


def test_build_retry_plan_collection_failed_planned_none_falls_back_to_full():
    records = [_rec(0, "full", finished=True, passed=["a.py::t1"], planned=None)]
    plan = pr.build_retry_plan(records, "k", 0, 0, build_id="b1", current_retry=1)
    assert plan["active"] is False
    assert plan["skip_command"] is False
    assert plan["skip_files"] == set()
    assert plan["passed"] == set()


def test_maybe_skip_command_writes_skipped_jsonl_record_with_0777_dirs(tmp_path):
    retry_in = tmp_path / "in"
    cov_out = tmp_path / "out"
    _write_jsonl(
        retry_in,
        "job-0",
        0,
        [
            _start(planned=["a.py::t1"], expected=["a.py::t1"]),
            _test("a.py::t1", True),
            _finish(True),
        ],
    )
    env = {"BUILDKITE_BUILD_ID": "b1", "BUILDKITE_JOB_ID": "job-1", "BUILDKITE_RETRY_COUNT": "1"}
    skipped = pr.maybe_skip_command("k", 0, 0, str(retry_in), str(cov_out), env=env)
    assert skipped is True
    out_file = cov_out / "job-1" / "0.jsonl"
    assert out_file.exists()
    assert stat.S_IMODE(cov_out.stat().st_mode) == 0o777
    assert stat.S_IMODE((cov_out / "job-1").stat().st_mode) == 0o777
    rec = pr.parse_record_file(str(out_file))
    assert rec["mode"] == "test-retry"
    assert rec["retry"] == 1
    assert rec["finished"] is True
    assert rec["exitstatus"] == 0
    assert rec["skipped_command"] is True
    assert rec["planned"] == ["a.py::t1"]


# --- prepare_retry_dir (pre-pytest loader seam) ---


def test_prepare_skips_on_attempt_zero(tmp_path):
    called = []
    ok = pr.prepare_retry_dir(
        dest_dir=str(tmp_path / "in"),
        env={"BUILDKITE_RETRY_COUNT": "0", "BUILDKITE_JOB_ID": "j0"},
        run_cmd=lambda *a, **kw: called.append(a),
        which=lambda _: "/usr/bin/buildkite-agent",
    )
    assert ok is False
    assert called == []


def test_prepare_honors_prepopulated_host_hook_dir(tmp_path):
    hook_dir = tmp_path / "host-hook-in"
    hook_dir.mkdir()
    (hook_dir / pr.TAG_FILE).write_text("1")
    called = []
    ok = pr.prepare_retry_dir(
        dest_dir=str(tmp_path / "in"),
        env={"BUILDKITE_RETRY_COUNT": "1", pr.RETRY_IN_ENV: str(hook_dir)},
        run_cmd=lambda *a, **kw: called.append(a),
        which=lambda _: None,
    )
    assert ok is True
    assert called == []


def test_prepare_returns_false_when_untagged_retry(tmp_path):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found")

    ok = pr.prepare_retry_dir(
        dest_dir=str(tmp_path / "in"),
        env={"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_JOB_ID": "j1"},
        run_cmd=fake_run,
        which=lambda _: "/usr/bin/buildkite-agent",
    )
    assert ok is False
    assert not (tmp_path / "in" / pr.TAG_FILE).exists()


def test_prepare_downloads_with_include_retried_env_var_when_tagged(tmp_path):
    dest = tmp_path / "in"
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env", {})))
        if cmd[:3] == ["buildkite-agent", "meta-data", "get"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="1\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    ok = pr.prepare_retry_dir(
        dest_dir=str(dest),
        env={"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_JOB_ID": "j1"},
        run_cmd=fake_run,
        which=lambda _: "/usr/bin/buildkite-agent",
    )
    assert ok is True
    assert (dest / pr.TAG_FILE).read_text().strip() == "1"
    assert calls[0][0] == ["buildkite-agent", "meta-data", "get", "test-retry:j1"]
    assert calls[1][0] == [
        "buildkite-agent",
        "artifact",
        "download",
        pr.ARTIFACTS_JSONL,
        str(dest),
    ]
    assert calls[1][1]["BUILDKITE_AGENT_INCLUDE_RETRIED_JOBS"] == "true"


def test_prepare_fails_safe_on_download_error(tmp_path):
    dest = tmp_path / "in"

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["buildkite-agent", "meta-data", "get"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="1\n", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="network error")

    ok = pr.prepare_retry_dir(
        dest_dir=str(dest),
        env={"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_JOB_ID": "j1"},
        run_cmd=fake_run,
        which=lambda _: "/usr/bin/buildkite-agent",
    )
    assert ok is False
    assert not dest.exists()


# --- Slice 2: Real pytest plugin integration (JSONL + Tier 2/3 skip + 1-job) ---


def _make_project(tmp_path: Path, files: dict) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    for rel, text in files.items():
        path = tmp_path / "tests" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _run_plugin_pytest(
    tmp_path: Path,
    plan: dict,
    command_args: list,
    shard: int,
    job_id: str,
    coverage_dir: Path,
    retry: int = 0,
    build_id: str = "b1",
    retry_in: Path = None,
    extra_env: dict = None,
):
    import runtime_shard as rs

    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update(
        PYTEST_ADDOPTS="-p runtime_shard_plugin",
        PYTHONPATH=os.path.dirname(pr.__file__),
        RUNTIME_SHARD_PLAN=rs.pack(plan),
        RUNTIME_SHARD_COMMAND="0",
        RUNTIME_SHARD_COVERAGE_DIR=str(coverage_dir),
        BUILDKITE_PARALLEL_JOB=str(shard),
        BUILDKITE_JOB_ID=job_id,
        BUILDKITE_BUILD_ID=build_id,
        BUILDKITE_RETRY_COUNT=str(retry),
        **(extra_env or {}),
    )
    if retry_in is not None:
        env[pr.RETRY_IN_ENV] = str(retry_in)
    run = subprocess.run(
        [pr.sys.executable, "-m", "pytest", *command_args],
        cwd=tmp_path / "tests",
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    rec_path = coverage_dir / job_id / "0.jsonl"
    rec = pr.parse_record_file(str(rec_path)) if rec_path.exists() else None
    return run, rec


def _simple_plan(files_map: dict, shards: int = 2, single_job: bool = False):
    cmd = {
        "args": ["-v", "pkg"],
        "prefix": "tests/",
        "label": "Command (1/1)",
        "files": files_map,
        "split": {},
        "position": 0,
        "fallback": 0,
    }
    return {"shards": shards, "key": "k", "single_job": single_job, "commands": [cmd]}


def test_plugin_records_three_phase_pass_and_reruns_teardown_failure_and_skips_passed_file(
    tmp_path,
):
    _make_project(
        tmp_path,
        {
            "pkg/test_clean.py": (
                "open('imported_clean', 'a').write('x')\n"
                "def test_ok(): pass\n"
            ),
            "pkg/test_mixed.py": (
                "import os, pytest\n"
                "@pytest.fixture\n"
                "def bad_teardown():\n"
                "    yield\n"
                "    if os.environ.get('BUILDKITE_RETRY_COUNT') == '0':\n"
                "        raise RuntimeError('teardown boom')\n"
                "def test_pass_body_fail_teardown(bad_teardown): pass\n"
                "def test_always_ok(): pass\n"
                "@pytest.mark.skip(reason='env')\n"
                "def test_skipped(): pass\n"
            ),
        },
    )
    plan = _simple_plan(
        {"tests/pkg/test_clean.py": 0, "tests/pkg/test_mixed.py": 0},
        shards=1,
        single_job=True,
    )
    artifacts_root = tmp_path / "artifacts"
    cov_dir = artifacts_root / ".runtime-shard-coverage"

    # Attempt 0: full run -> test_clean::test_ok and test_mixed::test_always_ok pass;
    # test_pass_body_fail_teardown fails in teardown; test_skipped is skipped.
    run0, rec0 = _run_plugin_pytest(
        tmp_path, plan, ["-v", "pkg"], shard=0, job_id="job-0", coverage_dir=cov_dir, retry=0
    )
    assert run0.returncode != 0
    assert rec0 is not None
    assert rec0["mode"] == "full"
    assert rec0["finished"] is True
    assert rec0["passed"] == [
        "tests/pkg/test_clean.py::test_ok",
        "tests/pkg/test_mixed.py::test_always_ok",
    ]
    assert "tests/pkg/test_mixed.py::test_pass_body_fail_teardown" in rec0["failed"]
    assert (tmp_path / "tests" / "imported_clean").read_text() == "x"

    # Attempt 1: tagged test retry ->
    # Tier 2: test_clean.py is NOT EVEN IMPORTED!
    # Tier 3: test_always_ok is deselected; teardown failure AND skipped test rerun!
    run1, rec1 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-1",
        coverage_dir=cov_dir,
        retry=1,
        retry_in=artifacts_root,
    )
    assert run1.returncode == 0, run1.stdout + run1.stderr
    assert (tmp_path / "tests" / "imported_clean").read_text() == "x"  # never re-imported!
    assert rec1["mode"] == "test-retry"
    assert rec1["ran"] == [
        "tests/pkg/test_mixed.py::test_pass_body_fail_teardown",
        "tests/pkg/test_mixed.py::test_skipped",
    ]
    assert rec1["passed"] == ["tests/pkg/test_mixed.py::test_pass_body_fail_teardown"]


def test_plugin_flushes_passes_before_os_exit_and_reruns_crasher_and_never_run(tmp_path):
    _make_project(
        tmp_path,
        {
            "pkg/test_crash.py": (
                "import os\n"
                "def test_1(): pass\n"
                "def test_2_crash():\n"
                "    if os.environ.get('BUILDKITE_RETRY_COUNT') == '0':\n"
                "        os._exit(137)\n"
                "def test_3_never_run(): pass\n"
            )
        },
    )
    plan = _simple_plan({"tests/pkg/test_crash.py": 0}, shards=1, single_job=True)
    artifacts_root = tmp_path / "artifacts"
    cov_dir = artifacts_root / ".runtime-shard-coverage"

    run0, rec0 = _run_plugin_pytest(
        tmp_path, plan, ["-v", "pkg"], shard=0, job_id="job-0", coverage_dir=cov_dir, retry=0
    )
    assert run0.returncode == 137
    assert rec0 is not None
    assert rec0["finished"] is False
    assert rec0["passed"] == ["tests/pkg/test_crash.py::test_1"]
    assert rec0["planned"] == [
        "tests/pkg/test_crash.py::test_1",
        "tests/pkg/test_crash.py::test_2_crash",
        "tests/pkg/test_crash.py::test_3_never_run",
    ]

    run1, rec1 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-1",
        coverage_dir=cov_dir,
        retry=1,
        retry_in=artifacts_root,
    )
    assert run1.returncode == 0, run1.stdout + run1.stderr
    assert rec1["ran"] == [
        "tests/pkg/test_crash.py::test_2_crash",
        "tests/pkg/test_crash.py::test_3_never_run",
    ]
    assert rec1["passed"] == rec1["ran"]


def test_plugin_skips_redundant_inventory_on_test_retry_when_full_saved_expected(tmp_path):
    _make_project(
        tmp_path,
        {
            "pkg/test_a.py": (
                "import os\n"
                "def test_1():\n"
                "    assert os.environ.get('BUILDKITE_RETRY_COUNT') != '0'\n"
            ),
            "pkg/test_b.py": (
                "open('imported_b', 'a').write('b')\n"
                "def test_2(): pass\n"
            ),
        },
    )
    plan = _simple_plan(
        {"tests/pkg/test_a.py": 0, "tests/pkg/test_b.py": 1}, shards=2, single_job=False
    )
    artifacts_root = tmp_path / "artifacts"
    cov_dir = artifacts_root / ".runtime-shard-coverage"

    # Attempt 0 on fallback shard 0: runs inventory subprocess (imports test_b.py once).
    run0, rec0 = _run_plugin_pytest(
        tmp_path, plan, ["-v", "pkg"], shard=0, job_id="job-0", coverage_dir=cov_dir, retry=0
    )
    assert run0.returncode != 0
    assert rec0["expected"] == [
        "tests/pkg/test_a.py::test_1",
        "tests/pkg/test_b.py::test_2",
    ]
    assert (tmp_path / "tests" / "imported_b").read_text() == "b"

    # Attempt 1 test retry on fallback shard 0: skips inventory (test_b.py not imported again!).
    run1, rec1 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-1",
        coverage_dir=cov_dir,
        retry=1,
        retry_in=artifacts_root,
    )
    assert run1.returncode == 0, run1.stdout + run1.stderr
    assert rec1["expected"] is None
    assert (tmp_path / "tests" / "imported_b").read_text() == "b"


def test_plugin_untagged_retry_runs_full_and_ignores_prior_passes(tmp_path):
    _make_project(
        tmp_path,
        {"pkg/test_a.py": "def test_1(): pass\ndef test_2(): pass\n"},
    )
    plan = _simple_plan({"tests/pkg/test_a.py": 0}, shards=1, single_job=True)
    artifacts_root = tmp_path / "artifacts"
    cov_dir = artifacts_root / ".runtime-shard-coverage"

    run0, _ = _run_plugin_pytest(
        tmp_path, plan, ["-v", "pkg"], shard=0, job_id="job-0", coverage_dir=cov_dir, retry=0
    )
    assert run0.returncode == 0
    # retry=1 WITHOUT retry_in (simulates /ci retry or manual per-job Retry)
    run1, rec1 = _run_plugin_pytest(
        tmp_path, plan, ["-v", "pkg"], shard=0, job_id="job-1", coverage_dir=cov_dir, retry=1
    )
    assert run1.returncode == 0
    assert rec1["mode"] == "full"
    assert rec1["ran"] == ["tests/pkg/test_a.py::test_1", "tests/pkg/test_a.py::test_2"]


# --- Slice 3: Retry-chain-aware #884 coverage checker ---


def test_coverage_accepts_test_retry_that_skips_earlier_passed_tests():
    import runtime_shard_coverage as coverage

    steps = {
        "k": [{"position": 0, "label": "Command (1/1)", "fallback": 0, "owners": [0, 1]}]
    }
    records = [
        _rec(
            0,
            "full",
            finished=True,
            passed=["a.py::t1"],
            ran=["a.py::t1", "a.py::t2"],
            planned=["a.py::t1", "a.py::t2"],
            expected=["a.py::t1", "a.py::t2", "b.py::t1"],
        ),
        _rec(
            1,
            "test-retry",
            finished=True,
            passed=["a.py::t2"],
            ran=["a.py::t2"],
            planned=["a.py::t2"],
        ),
        {
            **_rec(0, "full", finished=True, passed=["b.py::t1"], planned=["b.py::t1"]),
            "shard": 1,
        },
    ]
    [result] = coverage.compare(steps, records, build_id="b1")
    assert result["missed"] == []
    assert result["unfinished"] == []
    assert result["unverified"] is None
    assert coverage.report([result])[0] == "success"


def test_coverage_catches_false_green_when_test_retry_skips_unpassed_test():
    import runtime_shard_coverage as coverage

    steps = {
        "k": [{"position": 0, "label": "Command (1/1)", "fallback": 0, "owners": [0]}]
    }
    # Attempt 0: t1 passed, t2 failed (NOT in passed).
    # Buggy Attempt 1 test-retry skips t2 and finishes -> coverage MUST fail RED!
    records = [
        _rec(
            0,
            "full",
            finished=True,
            passed=["a.py::t1"],
            ran=["a.py::t1", "a.py::t2"],
            planned=["a.py::t1", "a.py::t2"],
            expected=["a.py::t1", "a.py::t2"],
        ),
        _rec(1, "test-retry", finished=True, passed=[], ran=[], planned=[]),
    ]
    [result] = coverage.compare(steps, records, build_id="b1")
    assert result["missed"] == ["a.py::t2"]
    assert coverage.report([result])[0] == "error"


def test_coverage_accepts_recovery_after_mid_run_crash_via_test_retry():
    import runtime_shard_coverage as coverage

    steps = {
        "k": [{"position": 0, "label": "Command (1/1)", "fallback": 0, "owners": [0]}]
    }
    records = [
        _rec(
            0,
            "full",
            finished=False,
            passed=["a.py::t1"],
            ran=["a.py::t1"],
            planned=["a.py::t1", "a.py::t2"],
            expected=["a.py::t1", "a.py::t2"],
        ),
        _rec(
            1,
            "test-retry",
            finished=True,
            passed=["a.py::t2"],
            ran=["a.py::t2"],
            planned=["a.py::t2"],
        ),
    ]
    [result] = coverage.compare(steps, records, build_id="b1")
    assert result["missed"] == []
    assert result["unfinished"] == []
    assert coverage.report([result])[0] == "success"


def test_coverage_full_retry_resets_chain_and_does_not_credit_attempt_zero():
    import runtime_shard_coverage as coverage

    steps = {
        "k": [{"position": 0, "label": "Command (1/1)", "fallback": 0, "owners": [0]}]
    }
    # Attempt 0 passed t1; Attempt 1 was a FULL retry (/ci retry) where t1 failed;
    # Attempt 2 (buggy test-retry) skips t1 -> must fail RED because Attempt 1 reset the chain!
    records = [
        _rec(
            0,
            "full",
            finished=True,
            passed=["a.py::t1"],
            ran=["a.py::t1", "a.py::t2"],
            planned=["a.py::t1", "a.py::t2"],
            expected=["a.py::t1", "a.py::t2"],
        ),
        _rec(
            1,
            "full",
            finished=True,
            passed=["a.py::t2"],
            ran=["a.py::t1", "a.py::t2"],
            planned=["a.py::t1", "a.py::t2"],
        ),
        _rec(2, "test-retry", finished=True, passed=[], ran=[], planned=[]),
    ]
    [result] = coverage.compare(steps, records, build_id="b1")
    assert result["missed"] == ["a.py::t1"]
    assert coverage.report([result])[0] == "error"


def test_plugin_marks_failing_subtest_parent_as_not_passed_so_it_reruns(tmp_path):
    _make_project(
        tmp_path,
        {
            "pkg/test_sub.py": (
                "import os\n"
                "def test_parent(subtests):\n"
                "    with subtests.test(case='sub'):\n"
                "        assert os.environ.get('BUILDKITE_RETRY_COUNT') != '0'\n"
            )
        },
    )
    plan = _simple_plan({"tests/pkg/test_sub.py": 0}, shards=1, single_job=True)
    artifacts_root = tmp_path / "artifacts"
    cov_dir = artifacts_root / ".runtime-shard-coverage"

    run0, rec0 = _run_plugin_pytest(
        tmp_path, plan, ["-v", "pkg"], shard=0, job_id="job-0", coverage_dir=cov_dir, retry=0
    )
    assert run0.returncode != 0
    assert rec0["ran"] == ["tests/pkg/test_sub.py::test_parent"]
    assert rec0["passed"] == []
    assert rec0["failed"] == ["tests/pkg/test_sub.py::test_parent"]

    run1, rec1 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-1",
        coverage_dir=cov_dir,
        retry=1,
        retry_in=artifacts_root,
    )
    assert run1.returncode == 0
    assert rec1["passed"] == ["tests/pkg/test_sub.py::test_parent"]


def test_plugin_all_deselected_on_test_retry_exits_zero(tmp_path):
    _make_project(tmp_path, {"pkg/test_a.py": "def test_1(): pass\n"})
    plan = _simple_plan({"tests/pkg/test_a.py": 0}, shards=1, single_job=True)
    artifacts_root = tmp_path / "artifacts"
    cov_dir = artifacts_root / ".runtime-shard-coverage"

    run0, _ = _run_plugin_pytest(
        tmp_path, plan, ["-v", "pkg"], shard=0, job_id="job-0", coverage_dir=cov_dir, retry=0
    )
    assert run0.returncode == 0
    run1, rec1 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-1",
        coverage_dir=cov_dir,
        retry=1,
        retry_in=artifacts_root,
    )
    assert run1.returncode == 0, run1.stdout + run1.stderr
    assert rec1["ran"] == []
    assert rec1["finished"] is True


def test_tier1_shell_guard_skips_passed_command_without_launching_pytest(tmp_path):
    import runtime_shard as rs

    retry_in = tmp_path / "retry-in"
    cov_out = tmp_path / "cov-out"
    _write_jsonl(
        retry_in,
        "job-0",
        0,
        [
            _start(step="my-step", position=0, shard=0, retry=0, planned=["a.py::t1"]),
            _test("a.py::t1", True),
            _finish(True),
        ],
    )
    planned = {"position": 0, "files": {"a.py": 0}, "split": {}}
    guarded = rs.guard(
        "echo SHOULD_NOT_RUN; exit 99",
        planned,
        shards=1,
        step_key="my-step",
        enable_retry=True,
    )
    # Run the real shell guard with pytest_retry.py unpacked into /tmp/runtime-shard.
    setup_cmd = (
        f"mkdir -p {rs.PLUGIN_DIR}"
        f" && cp {pr.__file__} {rs.PLUGIN_DIR}/{rs.RETRY_HELPER_MODULE}.py"
    )
    script = f"{setup_cmd}\n{guarded}".replace("$$", "$")
    env = dict(
        os.environ,
        CI_TEST_RETRY_IN=str(retry_in),
        RUNTIME_SHARD_COVERAGE_DIR=str(cov_out),
        BUILDKITE_BUILD_ID="b1",
        BUILDKITE_JOB_ID="job-1",
        BUILDKITE_RETRY_COUNT="1",
        BUILDKITE_PARALLEL_JOB="0",
    )
    run = subprocess.run(
        ["bash", "-ec", script], env=env, capture_output=True, text=True, check=False
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "SHOULD_NOT_RUN" not in run.stdout
    assert "pytest-retry: command 1 already passed (1 tests); skipping" in run.stdout
    rec = pr.parse_record_file(str(cov_out / "job-1" / "0.jsonl"))
    assert rec["skipped_command"] is True
    assert rec["mode"] == "test-retry"


# --- Slice 4: Pipeline generator wiring (flag off vs VLLM_CI_TEST_RETRY=1) ---


def test_generator_flag_off_keeps_single_job_and_docker_mount_untouched(
    fake_global_config, monkeypatch, tmp_path
):
    import buildkite_step
    import runtime_shard as rs
    from plugin.docker_plugin import get_docker_plugin
    from step import Step

    monkeypatch.delenv(rs.TEST_RETRY_ENV_VAR, raising=False)
    monkeypatch.setattr(rs, "fetch_timings", lambda *a, **kw: None)
    fake_global_config["run_all"] = True
    (tmp_path / "tests" / "pkg").mkdir(parents=True)
    (tmp_path / "tests" / "pkg" / "test_a.py").write_text("def test_1(): pass\n")
    monkeypatch.chdir(tmp_path)

    step = Step(
        label="Single job",
        key="single-job",
        automatic_shard=True,
        working_dir="/vllm-workspace/tests",
        commands=["pytest -v pkg"],
    )
    assert "mount_buildkite_agent" not in get_docker_plugin(step, "img")
    [group] = buildkite_step.convert_group_step_to_buildkite_step({"G": [step]})
    [rendered] = group.steps
    assert rendered.parallelism is None
    assert rs.PLAN_ENV not in (rendered.env or {})
    assert buildkite_step.shard_coverage_check_step([group]) is None


def test_generator_flag_on_wires_single_job_retry_and_docker_mount_without_coverage_job(
    fake_global_config, monkeypatch, tmp_path
):
    import buildkite_step
    import runtime_shard as rs
    from plugin.docker_plugin import get_docker_plugin
    from step import Step

    monkeypatch.setenv(rs.TEST_RETRY_ENV_VAR, "1")
    monkeypatch.setattr(rs, "fetch_timings", lambda *a, **kw: None)
    fake_global_config["run_all"] = True
    (tmp_path / "tests" / "pkg").mkdir(parents=True)
    (tmp_path / "tests" / "pkg" / "test_a.py").write_text("def test_1(): pass\n")
    monkeypatch.chdir(tmp_path)

    step = Step(
        label="Single job",
        key="single-job",
        automatic_shard=True,
        working_dir="/vllm-workspace/tests",
        commands=["pytest -v pkg"],
    )
    [group] = buildkite_step.convert_group_step_to_buildkite_step({"G": [step]})
    [rendered] = group.steps
    assert rendered.parallelism is None
    assert rendered.label == "Single job"  # no "Shard %N/%t" suffix
    docker_cfg = next(iter(rendered.plugins[0].values()))
    assert docker_cfg["mount_buildkite_agent"] is True
    assert rs.COVERAGE_ARTIFACT_PATH in rendered.artifact_paths
    plan = rs.unpack(rendered.env[rs.PLAN_ENV])
    assert plan["single_job"] is True and plan["shards"] == 1
    assert any("pytest_retry.py prepare" in c for c in rendered.commands)
    assert any("maybe-skip-command single-job" in c for c in rendered.commands)
    # 1-job steps do not create a multi-shard coverage job.
    assert buildkite_step.shard_coverage_check_step([group]) is None

    # Non-pytest Docker steps do NOT get mount_buildkite_agent even with flag on.
    script_step = Step(
        label="Script job",
        key="script-job",
        automatic_shard=True,
        working_dir="/vllm-workspace/tests",
        commands=["bash run_something.sh"],
    )
    [script_group] = buildkite_step.convert_group_step_to_buildkite_step(
        {"G": [script_step]}
    )
    [script_rendered] = script_group.steps
    script_docker_cfg = next(iter(script_rendered.plugins[0].values()))
    assert "mount_buildkite_agent" not in script_docker_cfg


# --- Mutation-hardening tests for pytest_retry.py ---


def test_constants_and_cli_exit_codes(tmp_path, monkeypatch, capsys):
    assert pr.RETRY_DIR == "/tmp/ci-test-retry-in"
    assert pr.TAG_FILE == ".tag"
    assert pr.META_TIMEOUT_SECONDS == 15
    assert pr.DOWNLOAD_TIMEOUT_SECONDS == 60
    assert pr.SKIP_COMMAND_EXIT == 0
    assert pr.RUN_COMMAND_EXIT == 10

    assert pr._main(["pytest_retry.py"]) == 2
    assert pr._main(["pytest_retry.py", "unknown"]) == 2
    assert pr._main(["pytest_retry.py", "maybe-skip-command", "k", "0"]) == 2

    called_dests = []
    monkeypatch.setattr(pr, "prepare_retry_dir", lambda dest_dir: called_dests.append(dest_dir) or True)
    assert pr._main(["pytest_retry.py", "prepare"]) == 0
    assert pr._main(["pytest_retry.py", "prepare", str(tmp_path)]) == 0
    assert called_dests == [pr.RETRY_DIR, str(tmp_path)]

    retry_in = tmp_path / "in"
    cov_out = tmp_path / "out"
    _write_jsonl(
        retry_in,
        "job-0",
        1,
        [
            _start(step="k", position=1, shard=0, retry=0, planned=["a.py::t1"]),
            _test("a.py::t1", True),
            _finish(True),
        ],
    )
    monkeypatch.setenv(pr.RETRY_IN_ENV, str(retry_in))
    monkeypatch.setenv("RUNTIME_SHARD_COVERAGE_DIR", str(cov_out))
    monkeypatch.setenv("BUILDKITE_BUILD_ID", "b1")
    monkeypatch.setenv("BUILDKITE_RETRY_COUNT", "1")
    monkeypatch.delenv("BUILDKITE_JOB_ID", raising=False)
    assert pr._main(["pytest_retry.py", "maybe-skip-command", "k", "1", "0"]) == 0
    assert "pytest-retry: command 2 already passed (1 tests); skipping" in capsys.readouterr().out
    assert (cov_out / "no-job-id" / "1.jsonl").exists()
    # Different position has no record -> must run (exit 10).
    assert pr._main(["pytest_retry.py", "maybe-skip-command", "k", "2", "0"]) == 10


def test_parse_validation_edge_cases_json_and_jsonl(tmp_path, capsys):
    # Empty files
    empty_jsonl = _write_jsonl(tmp_path, "j_empty", 0, ["   \n"])
    assert pr.parse_record_file(str(empty_jsonl)) is None

    # Legacy JSON with bools, invalid mode/build_id/passed/failed, and valid extras
    base_json = {
        "step": "k",
        "position": 0,
        "shard": 0,
        "retry": 0,
        "finished": True,
        "finished_at": 1.0,
        "ran": ["a"],
    }
    bad_jsons = [
        {**base_json, "position": True},
        {**base_json, "finished_at": True},
        {**base_json, "build_id": 123},
        {**base_json, "mode": "bogus"},
        {**base_json, "passed": "not-list"},
        {**base_json, "passed": [1]},
        {**base_json, "failed": "not-list"},
        {**base_json, "failed": [1]},
    ]
    for i, payload in enumerate(bad_jsons):
        p = tmp_path / ".runtime-shard-coverage" / "jbad" / f"{i}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload))
        assert pr.parse_record_file(str(p)) is None, payload

    good_json = tmp_path / ".runtime-shard-coverage" / "jgood" / "0.json"
    good_json.parent.mkdir(parents=True, exist_ok=True)
    good_json.write_text(
        json.dumps(
            {
                **base_json,
                "mode": "test-retry",
                "passed": ["a"],
                "failed": ["b"],
                "skipped_command": True,
            }
        )
    )
    parsed_good = pr.parse_record_file(str(good_json))
    assert parsed_good["mode"] == "test-retry"
    assert parsed_good["passed"] == ["a"]
    assert parsed_good["failed"] == ["b"]
    assert parsed_good["skipped_command"] is True

    # JSONL edge cases: blank lines, interior corrupt line, bad field types, unfinished finished_at==0.0
    p_blank = _write_jsonl(
        tmp_path,
        "jblank",
        0,
        [_start(skipped_command=True), "\n", _test("a", True), "\n"],
    )
    rec_blank = pr.parse_record_file(str(p_blank))
    assert rec_blank["finished_at"] == 0.0
    assert rec_blank["skipped_command"] is True
    assert rec_blank["passed"] == ["a"]

    bad_jsonls = [
        [_start(expected="bad")],
        [_start(planned=[1])],
        [_start(build_id=999)],
        [_start(), "corrupt interior\n", _finish()],
        [_start(), "[]\n"],
        [_start(), {"event": "test", "nodeid": 1, "passed": True}],
        [_start(), {"event": "test", "nodeid": "a", "passed": "yes"}],
        [_start(), {"event": "finish", "finished": "yes", "finished_at": 1.0}],
        [_start(), {"event": "finish", "finished": True, "finished_at": True}],
    ]
    for i, lines in enumerate(bad_jsonls):
        p = _write_jsonl(tmp_path, "jbadl", i, lines)
        assert pr.parse_record_file(str(p)) is None, lines


def test_active_chain_filtering_dedup_and_multi_hop_retry():
    records = [
        # Other shard / step / retry >= before_retry must be ignored
        {**_rec(0, "full", passed=["other_shard"]), "shard": 1},
        {**_rec(0, "full", passed=["other_step"]), "step": "other"},
        _rec(0, "full", finished=False, passed=["stale"], planned=["a", "b", "c"], expected=[]),
        {**_rec(0, "full", finished=True, passed=["a"], planned=["a", "b", "c"], expected=[]), "finished_at": 5.0},
        _rec(1, "test-retry", finished=True, passed=["b"], planned=["b", "c"]),
        _rec(2, "test-retry", finished=True, passed=["c"], planned=["c"]),
    ]
    assert pr.active_chain(records, "k", 0, 0, build_id="b1", before_retry=0) == []
    chain_all = pr.active_chain(records, "k", 0, 0, build_id="b1", before_retry=None)
    assert [r["retry"] for r in chain_all] == [0, 1, 2]
    assert chain_all[0]["passed"] == ["a"]  # finished=True beat finished=False

    plan = pr.build_retry_plan(records, "k", 0, 0, build_id="b1", current_retry=3)
    assert plan["passed"] == {"a", "b", "c"}
    assert plan["skip_command"] is True
    assert plan["has_expected"] is False  # expected=[] is not a valid inventory

    # Chain whose earliest attempt is test-retry (missing full attempt 0) -> fail-safe empty plan
    orphan = [_rec(1, "test-retry", finished=True, passed=["a"], planned=["a"], expected=["a"])]
    orphan_plan = pr.build_retry_plan(orphan, "k", 0, 0, build_id="b1", current_retry=2)
    assert orphan_plan == {
        "active": False,
        "passed": set(),
        "skip_command": False,
        "skip_files": set(),
        "has_expected": False,
        "base_planned": None,
    }


def test_jsonl_writer_metadata_permissions_and_error_handling(tmp_path, capsys):
    cov_dir = tmp_path / "cov"
    path = pr.write_jsonl_start(
        coverage_dir=str(cov_dir),
        job_id="job-x",
        step="k",
        position=0,
        shard=0,
        retry=0,
        build_id="b1",
        mode="full",
        expected=None,
        planned=["a"],
    )
    assert path is not None
    header = json.loads(Path(path).read_text().splitlines()[0])
    assert header["v"] == 2
    assert header["job_id"] == "job-x"
    assert stat.S_IMODE(Path(path).stat().st_mode) == 0o666

    # Unwritable path logs warning and returns None / swallows error.
    blocker = tmp_path / "blocker"
    blocker.write_text("file")
    assert (
        pr.write_jsonl_start(
            str(blocker), "j", "k", 0, 0, 0, "b1", "full", None, None
        )
        is None
    )
    assert "runtime-shard: no coverage record:" in capsys.readouterr().out

    pr.append_jsonl_event(str(tmp_path / "missing-dir" / "x.jsonl"), {"event": "test"})
    assert "runtime-shard: couldn't append coverage event:" in capsys.readouterr().out


def test_prepare_retry_dir_edge_cases_and_kwargs(tmp_path):
    dest = tmp_path / "in"
    dest.mkdir()
    (dest / "stale.txt").write_text("old")

    # Invalid retry count, missing job id, missing agent
    assert pr.prepare_retry_dir(str(dest), env={"BUILDKITE_RETRY_COUNT": "bad"}) is False
    assert pr.prepare_retry_dir(str(dest), env={"BUILDKITE_RETRY_COUNT": "1"}) is False
    assert (
        pr.prepare_retry_dir(
            str(dest),
            env={"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_JOB_ID": "j1"},
            which=lambda _: None,
        )
        is False
    )

    # Timeout on meta-data get
    def timeout_meta(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 15)

    assert (
        pr.prepare_retry_dir(
            str(dest),
            env={"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_JOB_ID": "j1"},
            run_cmd=timeout_meta,
            which=lambda _: "/bin/bk",
        )
        is False
    )

    # Blank tag -> False
    assert (
        pr.prepare_retry_dir(
            str(dest),
            env={"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_JOB_ID": "j1"},
            run_cmd=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="  \n", stderr=""),
            which=lambda _: "/bin/bk",
        )
        is False
    )

    # Timeout on artifact download -> cleans dest_dir and returns False
    def timeout_dl(cmd, **kw):
        if cmd[1] == "meta-data":
            return subprocess.CompletedProcess(cmd, 0, stdout="1\n", stderr="")
        raise subprocess.TimeoutExpired(cmd, 60)

    assert (
        pr.prepare_retry_dir(
            str(dest),
            env={"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_JOB_ID": "j1"},
            run_cmd=timeout_dl,
            which=lambda _: "/bin/bk",
        )
        is False
    )
    assert not dest.exists()

    # "no artifacts found for downloading" is allowed (still tagged, stale files cleared),
    # and verify exact subprocess kwargs (check=False, capture_output=True, text=True, timeouts).
    dest.mkdir()
    (dest / "stale.txt").write_text("old")
    seen_kwargs = []

    def no_artifacts(cmd, **kw):
        seen_kwargs.append((cmd[1], kw))
        if cmd[1] == "meta-data":
            return subprocess.CompletedProcess(cmd, 0, stdout="press-2\n", stderr="")
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="no artifacts found for downloading"
        )

    assert (
        pr.prepare_retry_dir(
            str(dest),
            env={"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_JOB_ID": "j1"},
            run_cmd=no_artifacts,
            which=lambda _: "/bin/bk",
        )
        is True
    )
    assert not (dest / "stale.txt").exists()
    assert (dest / pr.TAG_FILE).read_text().strip() == "press-2"
    assert seen_kwargs[0][1] == {
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": 15,
    }
    assert seen_kwargs[1][1]["check"] is False
    assert seen_kwargs[1][1]["capture_output"] is True
    assert seen_kwargs[1][1]["text"] is True
    assert seen_kwargs[1][1]["timeout"] == 60


def test_maybe_skip_command_edge_cases(tmp_path, capsys):
    assert pr.maybe_skip_command("k", 0, 0, str(tmp_path), None, env={}) is False
    assert pr.maybe_skip_command("k", 0, 0, str(tmp_path), None, env={"BUILDKITE_RETRY_COUNT": "bad"}) is False
    assert pr.maybe_skip_command("k", 0, 0, str(tmp_path), None, env={"BUILDKITE_RETRY_COUNT": "0"}) is False
    assert pr.maybe_skip_command("k", 0, 0, str(tmp_path / "missing"), None, env={"BUILDKITE_RETRY_COUNT": "1"}) is False
    assert pr.maybe_skip_command("k", 0, 0, str(tmp_path), None, env={"BUILDKITE_RETRY_COUNT": "1"}) is False

    # Passing prior record under build b1: retry 0 still runs, wrong build_id still runs,
    # matching build_id skips and preserves build_id in the output record.
    retry_in = tmp_path / "in"
    cov_out = tmp_path / "out"
    _write_jsonl(
        retry_in,
        "job-0",
        0,
        [_start(build_id="b1", planned=["a.py::t1"]), _test("a.py::t1", True), _finish(True)],
    )
    assert pr.maybe_skip_command("k", 0, 0, str(retry_in), str(cov_out), env={"BUILDKITE_RETRY_COUNT": "0", "BUILDKITE_BUILD_ID": "b1"}) is False
    assert pr.maybe_skip_command("k", 0, 0, str(retry_in), str(cov_out), env={"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_BUILD_ID": "other"}) is False
    assert pr.maybe_skip_command("k", 0, 0, str(retry_in), str(cov_out), env={"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_BUILD_ID": "b1", "BUILDKITE_JOB_ID": "j1"}) is True
    assert capsys.readouterr().out.strip() == "pytest-retry: command 1 already passed (1 tests); skipping"
    out_rec = pr.parse_record_file(str(cov_out / "j1" / "0.jsonl"))
    assert out_rec["build_id"] == "b1"


def test_remaining_mutant_killers(tmp_path, capsys):
    # Corrupt JSONL header prints "skipped unreadable"; non-dict header prints "skipped malformed"
    _write_jsonl(tmp_path, "j_corrupt", 0, ["{bad\n"])
    _write_jsonl(tmp_path, "j_list", 0, ["[]\n"])
    assert pr.read_records(str(tmp_path)) == []
    out_lines = capsys.readouterr().out.splitlines()
    assert any(l.startswith("runtime-shard coverage: skipped unreadable ") for l in out_lines)
    assert any(l.startswith("runtime-shard coverage: skipped malformed ") for l in out_lines)

    # Header omitting skipped_command defaults to False
    hdr = _start()
    hdr.pop("skipped_command")
    p_no_skip = _write_jsonl(tmp_path, "j_noskip", 0, [hdr, _finish()])
    assert pr.parse_record_file(str(p_no_skip))["skipped_command"] is False

    # Records after skipped build_id or skipped retry >= before_retry are still included;
    # dedup keeps first when higher-ranked and keeps earlier when equal rank.
    rec_high = {**_rec(0, "full", finished=True, passed=["win"], planned=["win"], expected=["win"]), "finished_at": 5.0}
    rec_eq = {**_rec(0, "full", finished=True, passed=["lose"], planned=["lose"], expected=["lose"]), "finished_at": 5.0}
    rec_low = {**_rec(0, "full", finished=False, passed=["low"]), "finished_at": 1.0}
    rec_other_build = {**_rec(0, "full", passed=["ob"]), "build_id": "other"}
    rec_future = _rec(2, "full", passed=["future"])
    # Also test a record omitting "mode" key (defaults to "full")
    rec_no_mode = dict(rec_high)
    rec_no_mode.pop("mode")
    chain = pr.active_chain(
        [rec_other_build, rec_future, rec_no_mode, rec_eq, rec_low],
        "k",
        0,
        0,
        build_id="b1",
        before_retry=1,
    )
    assert len(chain) == 1 and chain[0]["passed"] == ["win"]
    plan = pr.build_retry_plan([rec_no_mode], "k", 0, 0, build_id="b1", current_retry=1)
    assert plan["has_expected"] is True  # len(expected) == 1 is valid!

    # write_jsonl_start into existing job_dir (exist_ok=True) and default skipped_command=False
    cov = tmp_path / "cov2"
    (cov / "j1").mkdir(parents=True)
    p1 = pr.write_jsonl_start(str(cov), "j1", "k", 0, 0, 0, "b1", "full", None, None)
    assert pr.parse_record_file(p1)["skipped_command"] is False

    # Exact warning prefixes on writer errors
    blocker = tmp_path / "bfile"
    blocker.write_text("x")
    pr.write_jsonl_start(str(blocker), "j", "k", 0, 0, 0, "b1", "full", None, None)
    assert capsys.readouterr().out.startswith("runtime-shard: no coverage record: ")
    pr.append_jsonl_event(str(tmp_path / "nodir" / "x.jsonl"), {"event": "test"})
    assert capsys.readouterr().out.startswith("runtime-shard: couldn't append coverage event: ")

    # prepare_retry_dir: missing BUILDKITE_RETRY_COUNT, which arg check, empty job_id with agent,
    # stdout=None on tag, non-zero returncode with non-empty stdout, "no artifacts" in stdout.
    assert (
        pr.prepare_retry_dir(
            str(tmp_path / "d0"),
            env={"BUILDKITE_JOB_ID": "j1"},
            run_cmd=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="1\n", stderr=""),
            which=lambda _: "/bin/bk",
        )
        is False
    )
    assert (
        pr.maybe_skip_command("k", 0, 0, str(tmp_path / "in"), None, env={"BUILDKITE_BUILD_ID": "b1"})
        if (tmp_path / "in").exists()
        else False
    ) is False
    seen_which = []
    assert (
        pr.prepare_retry_dir(
            str(tmp_path / "d1"),
            env={"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_JOB_ID": ""},
            run_cmd=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="1\n", stderr=""),
            which=lambda name: seen_which.append(name) or "/bin/bk",
        )
        is False
    )
    assert (
        pr.prepare_retry_dir(
            str(tmp_path / "d2"),
            env={"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_JOB_ID": "j1"},
            which=lambda name: seen_which.append(name) or None,
        )
        is False
    )
    assert seen_which == ["buildkite-agent"]
    assert (
        pr.prepare_retry_dir(
            str(tmp_path / "d3"),
            env={"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_JOB_ID": "j1"},
            run_cmd=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=None, stderr=""),
            which=lambda _: "/bin/bk",
        )
        is False
    )
    # Non-zero meta-data returncode must NOT fall through to download even if download would pass.
    assert (
        pr.prepare_retry_dir(
            str(tmp_path / "d4"),
            env={"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_JOB_ID": "j1"},
            run_cmd=lambda cmd, **kw: (
                subprocess.CompletedProcess(cmd, 1, stdout="1\n", stderr="")
                if cmd[1] == "meta-data"
                else subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            ),
            which=lambda _: "/bin/bk",
        )
        is False
    )
    dest5 = tmp_path / "d5"
    assert (
        pr.prepare_retry_dir(
            str(dest5),
            env={"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_JOB_ID": "j1"},
            run_cmd=lambda cmd, **kw: (
                subprocess.CompletedProcess(cmd, 0, stdout="1\n", stderr="")
                if cmd[1] == "meta-data"
                else subprocess.CompletedProcess(
                    cmd, 1, stdout="no artifacts found for downloading", stderr=""
                )
            ),
            which=lambda _: "/bin/bk",
        )
        is True
    )
    # Record omitting "mode" stops active_chain at that retry (defaults to "full").
    r0_no_mode = _rec(0, "full", passed=["old"])
    r0_no_mode.pop("mode")
    r1_no_mode = _rec(1, "full", passed=["new"])
    r1_no_mode.pop("mode")
    assert [r["retry"] for r in pr.active_chain([r0_no_mode, r1_no_mode], "k", 0, 0, build_id="b1", before_retry=2)] == [1]


def test_coverage_broken_chain_and_unfinished_test_retry():
    import runtime_shard_coverage as coverage

    steps = {"k": [{"position": 0, "label": "Command (1/1)", "fallback": 0, "owners": [0]}]}
    # Broken chain: attempt 0 missing, attempt 1 is test-retry -> fails closed!
    orphan = [
        _rec(1, "test-retry", finished=True, passed=["a"], ran=["a"], planned=["a", "b"], expected=["a", "b"]),
    ]
    [res_orphan] = coverage.compare(steps, orphan, build_id="b1")
    assert res_orphan["missed"] == ["b"]

    # Unfinished test-retry inherits full attempt's planned list so unreached tests are not_run.
    unfinished_retry = [
        _rec(0, "full", finished=False, passed=["a"], ran=["a"], planned=["a", "b", "c"], expected=["a", "b", "c"]),
        _rec(1, "test-retry", finished=False, passed=["b"], ran=["b"], planned=["b"]),
    ]
    [res_unf] = coverage.compare(steps, unfinished_retry, build_id="b1")
    assert res_unf["unfinished"] == [0]
    assert res_unf["not_run"] == ["c"]
    assert res_unf["missed"] == []

    # Broken chain unfinished: attempt 0 missing, attempt 1 is unfinished test-retry -> uses record planned for not_run.
    orphan_unf = [
        _rec(1, "test-retry", finished=False, passed=["a"], ran=["a"], planned=["a", "b"], expected=["a", "b"]),
    ]
    [res_ou] = coverage.compare(steps, orphan_unf, build_id="b1")
    assert res_ou["unfinished"] == [0]
    assert res_ou["not_run"] == ["b"]

    # When full attempt had planned=None (collection failed), chain_passed fails
    # closed (does not credit Attempt 0) and unfinished retry falls back to record planned.
    no_base_plan_unf = [
        _rec(0, "full", finished=False, passed=["a"], ran=["a"], planned=None, expected=["a", "b", "c"]),
        _rec(1, "test-retry", finished=False, passed=[], ran=[], planned=["b"]),
    ]
    [res_fb_unf] = coverage.compare(steps, no_base_plan_unf, build_id="b1")
    assert res_fb_unf["not_run"] == ["b"]
    assert res_fb_unf["missed"] == ["a", "c"]


def test_plugin_classify_phases_and_finish_leftovers_and_retry_log(tmp_path, monkeypatch):
    import runtime_shard_plugin as plugin

    # Complete truth table for _classify_phases
    assert plugin._classify_phases(
        {"setup": ("passed", False), "call": ("passed", False), "teardown": ("passed", False)}
    ) == (True, "passed")
    assert plugin._classify_phases(
        {"setup": ("failed", False), "teardown": ("passed", False)}
    ) == (False, "error")
    assert plugin._classify_phases(
        {"setup": ("passed", False), "call": ("passed", False), "teardown": ("failed", False)}
    ) == (False, "error")
    assert plugin._classify_phases(
        {"setup": ("passed", False), "call": ("failed", False), "teardown": ("passed", False)}
    ) == (False, "failed")
    assert plugin._classify_phases(
        {"setup": ("skipped", True), "teardown": ("passed", False)}
    ) == (False, "xfailed")
    assert plugin._classify_phases(
        {"setup": ("passed", False), "call": ("skipped", True), "teardown": ("passed", False)}
    ) == (False, "xfailed")
    assert plugin._classify_phases(
        {"setup": ("passed", False), "call": ("passed", True), "teardown": ("passed", False)}
    ) == (False, "xfailed")
    assert plugin._classify_phases(
        {"setup": ("skipped", False), "teardown": ("passed", False)}
    ) == (False, "skipped")
    assert plugin._classify_phases(
        {"setup": ("passed", False), "call": ("skipped", False), "teardown": ("passed", False)}
    ) == (False, "skipped")
    assert plugin._classify_phases({"teardown": ("passed", False)}) == (False, "failed")

    # _finish_record flushes any un-torndown phases as passed=False, outcome="error"
    monkeypatch.setenv(plugin.COVERAGE_DIR_ENV, str(tmp_path / "cov"))
    monkeypatch.setenv("BUILDKITE_JOB_ID", "job-left")
    monkeypatch.setenv("BUILDKITE_BUILD_ID", "b1")
    cmd = {"files": {}, "split": {}, "prefix": "", "label": "L", "position": 0}
    sp = plugin.ShardPlugin(cmd, 0, 1, "k", single_job=True)
    sp._start_record()
    sp._phases["t.py::hung"] = {"setup": ("passed", False)}
    sp._finish_record(finished=False)
    parsed = pr.parse_record_file(sp.record_path)
    assert parsed["ran"] == ["t.py::hung"]
    assert parsed["passed"] == []
    assert parsed["failed"] == ["t.py::hung"]


def test_plugin_retry_log_xfail_build_id_and_site_packages_install(tmp_path):
    _make_project(
        tmp_path,
        {
            "pkg/test_clean.py": "def test_c(): pass\n",
            "pkg/test_mix.py": (
                "import os, pytest\n"
                "def test_ok(): pass\n"
                "@pytest.mark.xfail(reason='expected')\n"
                "def test_xpass(): pass\n"
                "def test_fail():\n"
                "    assert os.environ.get('BUILDKITE_RETRY_COUNT') != '0'\n"
            ),
        },
    )
    plan = _simple_plan(
        {"tests/pkg/test_clean.py": 0, "tests/pkg/test_mix.py": 0},
        shards=1,
        single_job=True,
    )
    artifacts_root = tmp_path / "artifacts"
    cov_dir = artifacts_root / ".runtime-shard-coverage"

    # Attempt 0: even if CI_TEST_RETRY_IN is passed, retry_count=0 must stay mode="full".
    run0, rec0 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-0",
        coverage_dir=cov_dir,
        retry=0,
        retry_in=artifacts_root,
    )
    assert run0.returncode != 0
    assert rec0["mode"] == "full"
    # Non-strict xpass (wasxfail=True) must NOT be marked passed!
    assert "tests/pkg/test_mix.py::test_xpass" not in rec0["passed"]

    # Attempt 1 with WRONG build_id must NOT skip passed tests.
    run_wrong, rec_wrong = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-wrong",
        coverage_dir=cov_dir,
        retry=1,
        build_id="other-build",
        retry_in=artifacts_root,
    )
    assert run_wrong.returncode == 0
    assert "tests/pkg/test_clean.py::test_c" in rec_wrong["ran"]

    # Attempt 1 with matching build_id prints exact skipped summary and skips clean file + test_ok.
    run1, rec1 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-1",
        coverage_dir=cov_dir,
        retry=1,
        build_id="b1",
        retry_in=artifacts_root,
    )
    assert run1.returncode == 0, run1.stdout + run1.stderr
    assert "pytest-retry: skipped 1 passed file and 1 passed test from earlier attempts" in run1.stdout
    assert "tests/pkg/test_mix.py::test_xpass" in rec1["ran"]
    assert "tests/pkg/test_mix.py::test_fail" in rec1["ran"]
    assert "tests/pkg/test_mix.py::test_ok" not in rec1["ran"]
    # Attempt 1 skipping ONLY tests (2 passed tests in 1 file, 0 skipped files)
    # verifies pluralization, counter increment (>1), and `or` in log condition.
    _make_project(
        tmp_path / "only_tests",
        {
            "pkg/test_one.py": (
                "import os\n"
                "def test_ok1(): pass\n"
                "def test_ok2(): pass\n"
                "def test_flaky():\n"
                "    assert os.environ.get('BUILDKITE_RETRY_COUNT') != '0'\n"
            )
        },
    )
    ot_plan = _simple_plan({"tests/pkg/test_one.py": 0}, shards=1, single_job=True)
    ot_root = tmp_path / "only_tests" / "artifacts"
    ot_cov = ot_root / ".runtime-shard-coverage"
    _run_plugin_pytest(tmp_path / "only_tests", ot_plan, ["-v", "pkg"], 0, "j0", ot_cov, retry=0)
    ot_run1, _ = _run_plugin_pytest(
        tmp_path / "only_tests", ot_plan, ["-v", "pkg"], 0, "j1", ot_cov, retry=1, retry_in=ot_root
    )
    assert ot_run1.returncode == 0
    assert "pytest-retry: skipped 0 passed files and 2 passed tests from earlier attempts" in ot_run1.stdout


def test_runtime_shard_single_job_plan_guard_and_setup(tmp_path, monkeypatch):
    import runtime_shard as rs

    assert rs.TEST_RETRY_ENV_VAR == "VLLM_CI_TEST_RETRY"
    assert rs.single_job_plan("k", ["bash run.sh"], None, str(tmp_path)) is None
    assert rs.single_job_plan("k", ["pytest a.py"], "/outside", str(tmp_path)) is None

    plan_root = rs.single_job_plan(
        "k",
        ["export FOO=1", "pytest -v tests/a.py", "pytest tests/b.py"],
        "/vllm-workspace",
        str(tmp_path),
    )
    assert plan_root == {
        "shards": 1,
        "key": "k",
        "single_job": True,
        "commands": [
            {
                "args": ["-v", "tests/a.py"],
                "prefix": "",
                "label": "Command (2/3)",
                "position": 1,
                "files": {},
                "split": {},
                "fallback": 0,
            },
            {
                "args": ["tests/b.py"],
                "prefix": "",
                "label": "Command (3/3)",
                "position": 2,
                "files": {},
                "split": {},
                "fallback": 0,
            },
        ],
    }

    plan_sub = rs.single_job_plan(
        "k", ["pytest pkg"], "/vllm-workspace/tests", str(tmp_path)
    )
    assert plan_sub["commands"][0]["prefix"] == "tests/"

    # guard() defaults to enable_retry=False, skips case for shards=1, emits case for 2-shard partial owner.
    planned_one = {"position": 0, "files": {"a.py": 0}, "split": {}}
    g_off = rs.guard("pytest a.py", planned_one, shards=2, step_key="k")
    assert "maybe-skip-command" not in g_off
    assert 'case "$${BUILDKITE_PARALLEL_JOB:-0}" in' in g_off
    g_single = rs.guard("pytest a.py", planned_one, shards=1, step_key="k", enable_retry=True)
    assert 'maybe-skip-command k 0 "$${BUILDKITE_PARALLEL_JOB:-0}" 0 1' in g_single
    assert 'case "$${BUILDKITE_PARALLEL_JOB:-0}" in' not in g_single
    g_fallback = rs.guard(
        "pytest a.py",
        {"position": 0, "files": {"a.py": 0}, "split": {}, "fallback": 1},
        shards=2,
        step_key="k",
        enable_retry=True,
    )
    assert 'maybe-skip-command k 0 "$${BUILDKITE_PARALLEL_JOB:-0}" 1 2' in g_fallback

    # shard_job_setup(enable_retry=True) emits exact prepare command and unpacks both modules.
    setup = rs.shard_job_setup("/checkout", enable_retry=True)
    assert setup[-1] == (
        "python3 /tmp/runtime-shard/pytest_retry.py prepare /tmp/ci-test-retry-in"
        ' && if [ -f /tmp/ci-test-retry-in/.tag ]; then'
        ' export CI_TEST_RETRY_IN="/tmp/ci-test-retry-in"; fi'
    )


def test_generator_coverage_skips_single_job_and_keeps_k8s_and_amd_clean(
    fake_global_config, monkeypatch, tmp_path
):
    import buildkite_step
    import runtime_shard as rs
    from constants import DeviceType
    from step import Step

    monkeypatch.setenv(rs.TEST_RETRY_ENV_VAR, "1")
    fake_global_config["run_all"] = True
    (tmp_path / "tests" / "pkg").mkdir(parents=True)
    (tmp_path / "tests" / "pkg" / "test_a.py").write_text("def test_1(): pass\n")
    (tmp_path / "tests" / "pkg" / "test_b.py").write_text("def test_2(): pass\n")
    monkeypatch.chdir(tmp_path)

    # Mock timings: 2 shards for sharded step, 1 shard (None) for single-job steps.
    def fake_timings(key, queue):
        if "two-shard" in key:
            return {
                "buildNumber": 7,
                "buildNumbers": [7, 6],
                "setupMs": 30_000,
                "files": [
                    {
                        "command": rs.command_preview("pytest -v pkg"),
                        "file": "tests/pkg/test_a.py",
                        "observedMs": 800_000,
                        "timingStatus": "passed",
                    },
                    {
                        "command": rs.command_preview("pytest -v pkg"),
                        "file": "tests/pkg/test_b.py",
                        "observedMs": 800_000,
                        "timingStatus": "passed",
                    },
                ],
            }
        return None

    monkeypatch.setattr(rs, "fetch_timings", fake_timings)

    # K8s single-job step must NOT get mount_buildkite_agent injected into kubernetes plugin!
    k8s_single = Step(
        label="K8s single",
        key="k8s-single",
        device=DeviceType.L4,
        automatic_shard=True,
        working_dir="/vllm-workspace/tests",
        commands=["pytest -v pkg"],
    )
    # 2-shard step (plus AMD mirror, 1-job vs 2-shard)
    two_shard = Step(
        label="Two shard",
        key="two-shard",
        automatic_shard=True,
        working_dir="/vllm-workspace/tests",
        commands=["pytest -v pkg"],
        mirror={
            "amd": {
                "device": "mi325_1",
                "dind": False,
                "automatic_shard": True,
                "depends_on": ["image-build-amd"],
            }
        },
    )
    amd_single = Step(
        label="AMD standalone",
        key="amd-standalone",
        device="mi325_1",
        dind=False,
        automatic_shard=True,
        working_dir="/vllm-workspace/tests",
        commands=["pytest -v pkg"],
    )
    amd_two_shard = Step(
        label="AMD two shard",
        key="two-shard-amd",
        device="mi325_1",
        dind=False,
        automatic_shard=True,
        working_dir="/vllm-workspace/tests",
        commands=["pytest -v pkg"],
    )
    one_job_mirror = Step(
        label="One job mirrored",
        key="one-job-mirrored",
        automatic_shard=True,
        working_dir="/vllm-workspace/tests",
        commands=["pytest -v pkg"],
        mirror={
            "amd": {
                "device": "mi325_1",
                "dind": False,
                "automatic_shard": True,
                "depends_on": ["image-build-amd"],
            }
        },
    )
    # Default-on sharding enabled too (exercises _shard_by_default() + enable_retry)
    fake_global_config["automatic_shard_default"] = True
    groups = buildkite_step.convert_group_step_to_buildkite_step(
        {"G": [k8s_single, two_shard, amd_single, amd_two_shard, one_job_mirror]}
    )
    by_key = {s.key: s for g in groups for s in g.steps}

    # K8s plugin has no mount_buildkite_agent key
    k8s_cfg = next(iter(by_key["k8s-single"].plugins[0].values()))
    assert "mount_buildkite_agent" not in k8s_cfg
    assert by_key["k8s-single"].parallelism is None

    # 2-shard steps have parallelism=2 and Shard %N/%t; 1-job AMD standalone & mirror have parallelism=None
    assert by_key["two-shard"].parallelism == 2
    assert "Shard %N/%t" in by_key["two-shard"].label
    assert by_key["amd-two-shard"].parallelism == 2
    assert "Shard %N/%t" in by_key["amd-two-shard"].label
    assert by_key["two-shard-amd"].parallelism == 2
    assert "Shard %N/%t" in by_key["two-shard-amd"].label
    assert by_key["amd-standalone"].parallelism is None
    assert "Shard %N/%t" not in by_key["amd-standalone"].label
    assert by_key["amd-one-job-mirrored"].parallelism is None
    assert "Shard %N/%t" not in by_key["amd-one-job-mirrored"].label

    # Coverage job skips single-job steps and includes all 2-shard steps!
    cov = buildkite_step.shard_coverage_check_step(groups)
    assert cov is not None
    assert set(cov.depends_on) == {"two-shard", "amd-two-shard", "two-shard-amd"}

    # With automatic_shard_default=True and flag OFF, 1-job step stays unattached.
    monkeypatch.delenv(rs.TEST_RETRY_ENV_VAR, raising=False)
    off_groups = buildkite_step.convert_group_step_to_buildkite_step({"G": [k8s_single]})
    assert rs.PLAN_ENV not in (off_groups[0].steps[0].env or {})


# --- Peer review round 1 regression tests ---


def test_review_empty_collection_never_skips_command_on_retry(tmp_path):
    # Finding 1: a 1-job command that collects 0 tests (exit 5) records
    # planned=[] and finished=True; retry must NOT skip it and turn it green.
    records = [
        _rec(0, "full", finished=True, passed=[], ran=[], planned=[]),
    ]
    plan = pr.build_retry_plan(records, "k", 0, 0, build_id="b1", current_retry=1)
    assert plan["skip_command"] is False

    _make_project(tmp_path, {"pkg/test_a.py": "def test_ok(): pass\n"})
    p = _simple_plan({"tests/pkg/test_a.py": 0}, shards=1, single_job=True)
    p["commands"][0]["args"] = ["-v", "pkg", "-k", "does_not_exist"]
    artifacts_root = tmp_path / "artifacts"
    cov_dir = artifacts_root / ".runtime-shard-coverage"
    run0, rec0 = _run_plugin_pytest(
        tmp_path,
        p,
        ["-v", "pkg", "-k", "does_not_exist"],
        shard=0,
        job_id="job-0",
        coverage_dir=cov_dir,
        retry=0,
    )
    assert run0.returncode == pytest.ExitCode.NO_TESTS_COLLECTED
    assert rec0["planned"] == []
    assert (
        pr.maybe_skip_command(
            "k",
            0,
            0,
            str(artifacts_root),
            str(cov_dir),
            env={"BUILDKITE_BUILD_ID": "b1", "BUILDKITE_JOB_ID": "job-1", "BUILDKITE_RETRY_COUNT": "1"},
        )
        is False
    )


def test_review_duplicate_nodeid_pass_then_fail_is_not_skippable(tmp_path):
    # Finding 2: if a node ID executes twice and fails once (in either order),
    # it must NOT be recorded in `passed` and must NOT trigger skip_command.
    p = _write_jsonl(
        tmp_path,
        "job-0",
        0,
        [
            _start(planned=["a.py::t1", "a.py::t2"]),
            _test("a.py::t1", True, "passed"),
            _test("a.py::t1", False, "failed"),
            _test("a.py::t2", False, "failed"),
            _test("a.py::t2", True, "passed"),
            _finish(True),
        ],
    )
    rec = pr.parse_record_file(str(p))
    assert rec["passed"] == []
    assert rec["failed"] == ["a.py::t1", "a.py::t2"]
    plan = pr.build_retry_plan([rec], "k", 0, 0, build_id="b1", current_retry=1)
    assert plan["passed"] == set()
    assert plan["skip_command"] is False


def test_review_crashed_test_retry_is_not_hidden_by_earlier_finished_attempt():
    # Finding 3: Attempt 0 finished (a passed, b failed); Attempt 1 test-retry
    # crashed before running b -> coverage MUST flag shard 0 unfinished + b not_run.
    import runtime_shard_coverage as coverage

    steps = {"k": [{"position": 0, "label": "Command (1/1)", "fallback": 0, "owners": [0]}]}
    records = [
        _rec(
            0,
            "full",
            finished=True,
            passed=["a.py::a"],
            ran=["a.py::a", "a.py::b"],
            planned=["a.py::a", "a.py::b"],
            expected=["a.py::a", "a.py::b"],
        ),
        _rec(1, "test-retry", finished=False, passed=[], ran=[], planned=["a.py::b"]),
    ]
    [result] = coverage.compare(steps, records, build_id="b1")
    assert result["unfinished"] == [0]
    assert result["not_run"] == ["a.py::b"]
    assert coverage.report([result])[0] == "error"


def test_review_prepare_clears_stale_tag_on_every_early_exit(tmp_path):
    # Finding 4: stale dest_dir/.tag from an earlier job must be removed on
    # Attempt 0, missing agent, untagged retry, and metadata timeout.
    cases = [
        ({"BUILDKITE_RETRY_COUNT": "0", "BUILDKITE_JOB_ID": "j0"}, None, lambda _: "/bin/bk"),
        ({"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_JOB_ID": "j1"}, None, lambda _: None),
        (
            {"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_JOB_ID": "j1"},
            lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr=""),
            lambda _: "/bin/bk",
        ),
        (
            {"BUILDKITE_RETRY_COUNT": "1", "BUILDKITE_JOB_ID": "j1"},
            lambda cmd, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd, 15)),
            lambda _: "/bin/bk",
        ),
    ]
    for idx, (env, run_cmd, which) in enumerate(cases):
        dest = tmp_path / f"stale-{idx}"
        dest.mkdir()
        (dest / pr.TAG_FILE).write_text("stale-tag\n")
        ok = pr.prepare_retry_dir(
            str(dest),
            env=env,
            run_cmd=run_cmd or subprocess.run,
            which=which,
        )
        assert ok is False
        assert not dest.exists(), f"stale dir survived case {idx}"


def test_review_reasonless_xfail_xpass_is_not_marked_passed(tmp_path):
    # Finding 5: bare `@pytest.mark.xfail` sets report.wasxfail = "" (falsy);
    # hasattr(report, "wasxfail") ensures XPASS is NOT recorded as passed.
    _make_project(
        tmp_path,
        {
            "pkg/test_xf.py": (
                "import pytest\n"
                "@pytest.mark.xfail\n"
                "def test_reasonless_xpass():\n"
                "    assert True\n"
            )
        },
    )
    plan = _simple_plan({"tests/pkg/test_xf.py": 0}, shards=1, single_job=True)
    cov_dir = tmp_path / "artifacts" / ".runtime-shard-coverage"
    run0, rec0 = _run_plugin_pytest(
        tmp_path, plan, ["-v", "pkg"], shard=0, job_id="job-0", coverage_dir=cov_dir, retry=0
    )
    assert run0.returncode == 0
    assert rec0["ran"] == ["tests/pkg/test_xf.py::test_reasonless_xpass"]
    assert rec0["passed"] == []


# --- Peer review round 2 regression tests ---


def test_review2_fallback_full_run_after_missing_artifacts_records_full_and_blocks_stale_passes(
    tmp_path,
):
    # Finding 1: Attempt 0 passes A and fails B. Attempt 1 runs with an empty
    # retry_in dir (missing artifacts -> fallback full run), fails A, passes B.
    # Because Attempt 1 fell back to a full run, it MUST record mode="full" so
    # Attempt 2 (when Attempt 0 + Attempt 1 artifacts are both present) does
    # NOT walk back to Attempt 0 and treat A as passed.
    _make_project(
        tmp_path,
        {
            "pkg/test_flip.py": (
                "import os\n"
                "def test_a():\n"
                "    assert os.environ.get('BUILDKITE_RETRY_COUNT') != '1'\n"
                "def test_b():\n"
                "    assert os.environ.get('BUILDKITE_RETRY_COUNT') != '0'\n"
            )
        },
    )
    plan = _simple_plan({"tests/pkg/test_flip.py": 0}, shards=1, single_job=True)
    artifacts_root = tmp_path / "artifacts"
    cov_dir = artifacts_root / ".runtime-shard-coverage"
    empty_retry_in = tmp_path / "empty-retry-in"
    empty_retry_in.mkdir()

    run0, rec0 = _run_plugin_pytest(
        tmp_path, plan, ["-v", "pkg"], shard=0, job_id="job-0", coverage_dir=cov_dir, retry=0
    )
    assert run0.returncode != 0
    assert rec0["mode"] == "full"
    assert rec0["passed"] == ["tests/pkg/test_flip.py::test_a"]

    run1, rec1 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-1",
        coverage_dir=cov_dir,
        retry=1,
        retry_in=empty_retry_in,
    )
    assert run1.returncode != 0
    assert rec1["mode"] == "full"
    assert rec1["passed"] == ["tests/pkg/test_flip.py::test_b"]

    # Attempt 2 now sees BOTH job-0 and job-1: must NOT skip command, must rerun test_a!
    env2 = {"BUILDKITE_BUILD_ID": "b1", "BUILDKITE_JOB_ID": "job-2", "BUILDKITE_RETRY_COUNT": "2"}
    assert pr.maybe_skip_command("k", 0, 0, str(artifacts_root), str(cov_dir), env=env2) is False
    run2, rec2 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-2",
        coverage_dir=cov_dir,
        retry=2,
        retry_in=artifacts_root,
    )
    assert run2.returncode == 0, run2.stdout + run2.stderr
    assert rec2["mode"] == "test-retry"
    assert rec2["ran"] == ["tests/pkg/test_flip.py::test_a"]
    assert rec2["passed"] == ["tests/pkg/test_flip.py::test_a"]


def test_review2_duplicate_planned_nodeid_crash_is_not_skippable_and_all_passed_is(tmp_path):
    # Finding 2: With --keep-duplicates, occurrence 1 passes and occurrence 2
    # crashes (os._exit(137)) before emitting a result. Neither the node ID nor
    # its file may be skipped, and two separate crashed attempts (1 pass each)
    # must never sum to 2. Only when a single attempt passes all occurrences
    # may the ID and file be skipped.
    _make_project(
        tmp_path,
        {
            "pkg/test_dup.py": (
                "import os\n"
                "seen = 0\n"
                "def test_x():\n"
                "    global seen\n"
                "    seen += 1\n"
                "    if os.environ.get('CRASH_SECOND') == '1' and seen == 2:\n"
                "        os._exit(137)\n"
            )
        },
    )
    args = ["-v", "--keep-duplicates", "pkg/test_dup.py", "pkg/test_dup.py"]
    plan = _simple_plan({"tests/pkg/test_dup.py": 0}, shards=1, single_job=True)
    plan["commands"][0]["args"] = args
    artifacts_root = tmp_path / "artifacts"
    cov_dir = artifacts_root / ".runtime-shard-coverage"

    # Attempt 0: occurrence 1 passes, occurrence 2 crashes with 137.
    run0, rec0 = _run_plugin_pytest(
        tmp_path,
        plan,
        args,
        shard=0,
        job_id="job-0",
        coverage_dir=cov_dir,
        retry=0,
        extra_env={"CRASH_SECOND": "1"},
    )
    assert run0.returncode == 137
    assert rec0["planned"] == ["tests/pkg/test_dup.py::test_x", "tests/pkg/test_dup.py::test_x"]
    assert rec0["passed_counts"] == {"tests/pkg/test_dup.py::test_x": 1}

    plan1 = pr.build_retry_plan([rec0], "k", 0, 0, build_id="b1", current_retry=1)
    assert plan1["active"] is True
    assert plan1["passed"] == set()
    assert plan1["skip_files"] == set()
    assert plan1["skip_command"] is False

    # Attempt 1: crashes on occurrence 2 AGAIN -> 1 + 1 across attempts must NOT sum to 2!
    run1, rec1 = _run_plugin_pytest(
        tmp_path,
        plan,
        args,
        shard=0,
        job_id="job-1",
        coverage_dir=cov_dir,
        retry=1,
        retry_in=artifacts_root,
        extra_env={"CRASH_SECOND": "1"},
    )
    assert run1.returncode == 137
    plan2 = pr.build_retry_plan([rec0, rec1], "k", 0, 0, build_id="b1", current_retry=2)
    assert plan2["passed"] == set()
    assert plan2["skip_files"] == set()

    # Attempt 2: both occurrences pass in one attempt -> now skippable on Attempt 3!
    run2, rec2 = _run_plugin_pytest(
        tmp_path,
        plan,
        args,
        shard=0,
        job_id="job-2",
        coverage_dir=cov_dir,
        retry=2,
        retry_in=artifacts_root,
        extra_env={"CRASH_SECOND": "0"},
    )
    assert run2.returncode == 0, run2.stdout + run2.stderr
    assert rec2["passed_counts"] == {"tests/pkg/test_dup.py::test_x": 2}
    plan3 = pr.build_retry_plan([rec0, rec1, rec2], "k", 0, 0, build_id="b1", current_retry=3)
    assert plan3["passed"] == {"tests/pkg/test_dup.py::test_x"}
    assert plan3["skip_files"] == {"tests/pkg/test_dup.py"}
    assert plan3["skip_command"] is True

    # An unfulfilled duplicate ID before a distinct passed ID must `continue`, not `break`,
    # and a legacy JSON record with passed=["a"] never proves count >= 2 for duplicate planned IDs.
    mixed = _rec(
        0,
        "full",
        finished=False,
        passed=["a.py::dup", "b.py::ok"],
        planned=["a.py::dup", "a.py::dup", "b.py::ok"],
    )
    mixed["passed_counts"] = {"a.py::dup": 1, "b.py::ok": 1}
    assert pr.chain_passed([mixed]) == {"b.py::ok"}
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text(
        json.dumps(
            {
                "step": "k",
                "position": 0,
                "shard": 0,
                "retry": 0,
                "build_id": "b1",
                "finished": True,
                "finished_at": 1.0,
                "planned": ["a.py::dup", "a.py::dup"],
                "ran": ["a.py::dup"],
                "passed": ["a.py::dup"],
            }
        )
    )
    legacy_rec = pr.parse_record_file(str(legacy_path))
    assert legacy_rec["passed_counts"] == {"a.py::dup": 1}
    assert pr.chain_passed([legacy_rec]) == set()


def test_review2_crashed_full_retry_after_completed_test_retry_is_not_hidden():
    # Finding 3: Attempt 0 full ran a (passed) + b (failed); Attempt 1 test-retry
    # finished b (passed); Attempt 2 full crashed before running a or b.
    # Coverage MUST select Attempt 2 and fail red as unfinished.
    import runtime_shard_coverage as coverage

    steps = {"k": [{"position": 0, "label": "Command (1/1)", "fallback": 0, "owners": [0]}]}
    records = [
        _rec(
            0,
            "full",
            finished=True,
            passed=["a.py::a"],
            ran=["a.py::a", "a.py::b"],
            planned=["a.py::a", "a.py::b"],
            expected=["a.py::a", "a.py::b"],
        ),
        _rec(
            1,
            "test-retry",
            finished=True,
            passed=["a.py::b"],
            ran=["a.py::b"],
            planned=["a.py::b"],
        ),
        _rec(
            2,
            "full",
            finished=False,
            passed=[],
            ran=[],
            planned=["a.py::a", "a.py::b"],
        ),
    ]
    [result] = coverage.compare(steps, records, build_id="b1")
    assert result["unfinished"] == [0]
    assert result["not_run"] == ["a.py::a", "a.py::b"]
    assert coverage.report([result])[0] == "error"


# --- Peer review round 3 regression tests ---


def test_review3_collection_failed_base_falls_back_to_full_preserving_duplicate_protection(
    tmp_path,
):
    # Finding 1: If Attempt 0 failed collection (planned=None), Attempt 1 MUST
    # fall back to mode="full" so its real planned list (including duplicates
    # under --keep-duplicates) becomes the chain base for Attempt 2.
    _make_project(
        tmp_path,
        {
            "pkg/test_dup.py": (
                "import os\n"
                "seen = 0\n"
                "def test_x():\n"
                "    global seen\n"
                "    seen += 1\n"
                "    if os.environ.get('CRASH_SECOND') == '1' and seen == 2:\n"
                "        os._exit(137)\n"
            )
        },
    )
    args = ["-v", "--keep-duplicates", "pkg/test_dup.py", "pkg/test_dup.py"]
    plan = _simple_plan({"tests/pkg/test_dup.py": 0}, shards=1, single_job=True)
    plan["commands"][0]["args"] = args
    artifacts_root = tmp_path / "artifacts"
    cov_dir = artifacts_root / ".runtime-shard-coverage"

    # Attempt 0: collection failure left planned=None.
    _write_jsonl(
        artifacts_root,
        "job-0",
        0,
        [_start(retry=0, mode="full", planned=None), _finish(True)],
    )
    # Attempt 1: tagged retry falls back to full (planned=None base), passes occ 1, crashes occ 2.
    run1, rec1 = _run_plugin_pytest(
        tmp_path,
        plan,
        args,
        shard=0,
        job_id="job-1",
        coverage_dir=cov_dir,
        retry=1,
        retry_in=artifacts_root,
        extra_env={"CRASH_SECOND": "1"},
    )
    assert run1.returncode == 137
    assert rec1["mode"] == "full"
    assert rec1["planned"] == ["tests/pkg/test_dup.py::test_x", "tests/pkg/test_dup.py::test_x"]

    # Attempt 2: must NOT deselect test_x because Attempt 1 only passed 1 of 2 occurrences.
    run2, rec2 = _run_plugin_pytest(
        tmp_path,
        plan,
        args,
        shard=0,
        job_id="job-2",
        coverage_dir=cov_dir,
        retry=2,
        retry_in=artifacts_root,
        extra_env={"CRASH_SECOND": "0"},
    )
    assert run2.returncode == 0, run2.stdout + run2.stderr
    assert rec2["mode"] == "test-retry"
    assert rec2["passed_counts"] == {"tests/pkg/test_dup.py::test_x": 2}


def test_review3_tier1_does_not_skip_inventory_owner_when_expected_is_missing(tmp_path):
    # Finding 2: When the inventory-owning shard (fallback) passed all its tests
    # on Attempt 0 but failed to capture expected (expected=None), Tier 1 MUST
    # NOT skip the command on Attempt 1 so pytest can rebuild the inventory
    # while skipping the already-passed tests.
    import runtime_shard_coverage as coverage

    _make_project(
        tmp_path,
        {
            "pkg/test_a.py": "def test_a(): pass\n",
            "pkg/test_b.py": "def test_b(): pass\n",
        },
    )
    plan = _simple_plan(
        {"tests/pkg/test_a.py": 0, "tests/pkg/test_b.py": 1}, shards=2, single_job=False
    )
    artifacts_root = tmp_path / "artifacts"
    cov_dir = artifacts_root / ".runtime-shard-coverage"

    # Attempt 0: shard 0 passed test_a but inventory failed (expected=None); shard 1 passed test_b.
    _write_jsonl(
        artifacts_root,
        "job-0-s0",
        0,
        [
            _start(shard=0, retry=0, mode="full", expected=None, planned=["tests/pkg/test_a.py::test_a"]),
            _test("tests/pkg/test_a.py::test_a", True),
            _finish(True),
        ],
    )
    _write_jsonl(
        artifacts_root,
        "job-0-s1",
        0,
        [
            _start(shard=1, retry=0, mode="full", expected=None, planned=["tests/pkg/test_b.py::test_b"]),
            _test("tests/pkg/test_b.py::test_b", True),
            _finish(True),
        ],
    )

    env1 = {
        "BUILDKITE_BUILD_ID": "b1",
        "BUILDKITE_JOB_ID": "job-1-s0",
        "BUILDKITE_RETRY_COUNT": "1",
        pr.RETRY_IN_ENV: str(artifacts_root),
        "RUNTIME_SHARD_COVERAGE_DIR": str(cov_dir),
    }
    # Non-owner shard 1 (fallback=0, shards=2) CAN Tier-1 skip, owner shard 0 CANNOT!
    assert (
        pr.maybe_skip_command("k", 0, 1, str(artifacts_root), str(cov_dir), env=env1, needs_expected=False)
        is True
    )
    assert (
        pr.maybe_skip_command("k", 0, 0, str(artifacts_root), str(cov_dir), env=env1, needs_expected=True)
        is False
    )
    # CLI parses `<step> <pos> <shard> <fallback> <shards>` accurately:
    cli_owner = subprocess.run(
        [pr.sys.executable, pr.__file__, "maybe-skip-command", "k", "0", "0", "0", "2"],
        env={**os.environ, **env1},
        capture_output=True,
        text=True,
    )
    assert cli_owner.returncode == pr.RUN_COMMAND_EXIT
    cli_single = subprocess.run(
        [pr.sys.executable, pr.__file__, "maybe-skip-command", "k", "0", "0", "0", "1"],
        env={**os.environ, **env1},
        capture_output=True,
        text=True,
    )
    assert cli_single.returncode == pr.SKIP_COMMAND_EXIT

    # Launching pytest on shard 0 recovers expected without re-running test_a, and #884 goes green!
    (cov_dir / "job-1-s0" / "0.jsonl").unlink()
    run1, rec1 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-1-s0",
        coverage_dir=cov_dir,
        retry=1,
        retry_in=artifacts_root,
    )
    assert run1.returncode == 0, run1.stdout + run1.stderr
    assert rec1["mode"] == "test-retry"
    assert rec1["ran"] == []
    assert rec1["expected"] == ["tests/pkg/test_a.py::test_a", "tests/pkg/test_b.py::test_b"]

    steps = {"k": [{"position": 0, "label": "Command (1/1)", "fallback": 0, "owners": [0, 1]}]}
    records = pr.read_records(str(artifacts_root))
    [result] = coverage.compare(steps, records, build_id="b1")
    assert result["unverified"] is None
    assert result["missed"] == []
    assert coverage.report([result])[0] == "success"


# --- Peer review round 4 regression tests ---


def test_review4_later_attempt_introducing_new_tests_or_extra_duplicates_invalidates_plan(
    tmp_path,
):
    # Finding 1: Attempt 0 collects [A] and fails; Attempt 1 collects [A, B],
    # passes A and fails B. Attempt 2 MUST NOT skip the command or file; it
    # must invalidate the chain and run a fresh full attempt covering A and B.
    _make_project(
        tmp_path,
        {
            "pkg/test_dyn.py": (
                "import os, pytest\n"
                "params = ['a'] if os.environ.get('BUILDKITE_RETRY_COUNT') == '0' else ['a', 'b']\n"
                "@pytest.mark.parametrize('x', params)\n"
                "def test_dyn(x):\n"
                "    retry = os.environ.get('BUILDKITE_RETRY_COUNT')\n"
                "    if retry == '0':\n"
                "        assert False\n"
                "    if retry == '1' and x == 'b':\n"
                "        assert False\n"
            )
        },
    )
    plan = _simple_plan({"tests/pkg/test_dyn.py": 0}, shards=1, single_job=True)
    artifacts_root = tmp_path / "artifacts"
    cov_dir = artifacts_root / ".runtime-shard-coverage"

    run0, rec0 = _run_plugin_pytest(
        tmp_path, plan, ["-v", "pkg"], shard=0, job_id="job-0", coverage_dir=cov_dir, retry=0
    )
    assert run0.returncode != 0
    assert rec0["planned"] == ["tests/pkg/test_dyn.py::test_dyn[a]"]

    run1, rec1 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-1",
        coverage_dir=cov_dir,
        retry=1,
        retry_in=artifacts_root,
    )
    assert run1.returncode != 0
    assert rec1["passed"] == ["tests/pkg/test_dyn.py::test_dyn[a]"]
    assert rec1["failed"] == ["tests/pkg/test_dyn.py::test_dyn[b]"]

    # Attempt 2: Tier 1 must NOT skip; plugin invalidates chain and runs full!
    env2 = {"BUILDKITE_BUILD_ID": "b1", "BUILDKITE_JOB_ID": "job-2", "BUILDKITE_RETRY_COUNT": "2"}
    assert pr.maybe_skip_command("k", 0, 0, str(artifacts_root), str(cov_dir), env=env2) is False
    run2, rec2 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-2",
        coverage_dir=cov_dir,
        retry=2,
        retry_in=artifacts_root,
    )
    assert run2.returncode == 0, run2.stdout + run2.stderr
    assert rec2["mode"] == "full"
    assert rec2["ran"] == [
        "tests/pkg/test_dyn.py::test_dyn[a]",
        "tests/pkg/test_dyn.py::test_dyn[b]",
    ]

    # Extra duplicate occurrence or failed collection (planned=None) or unexpected ran in retry also invalidates:
    dup_extra = [
        _rec(0, "full", finished=True, passed=[], ran=["a.py::t1"], planned=["a.py::t1"]),
        _rec(1, "test-retry", finished=False, passed=["a.py::t1"], ran=["a.py::t1"], planned=["a.py::t1", "a.py::t1"]),
    ]
    assert pr.build_retry_plan(dup_extra, "k", 0, 0, build_id="b1", current_retry=2)["active"] is False
    retry_coll_fail = [
        _rec(0, "full", finished=True, passed=[], ran=["a.py::t1"], planned=["a.py::t1"]),
        _rec(1, "test-retry", finished=True, passed=[], ran=[], planned=None),
    ]
    assert pr.build_retry_plan(retry_coll_fail, "k", 0, 0, build_id="b1", current_retry=2)["active"] is False
    extra_ran = [
        _rec(0, "full", finished=True, passed=[], ran=["a.py::t1"], planned=["a.py::t1"]),
        _rec(1, "test-retry", finished=True, passed=["a.py::t1"], ran=["a.py::t1", "a.py::t2"], planned=["a.py::t1"]),
    ]
    assert pr.build_retry_plan(extra_ran, "k", 0, 0, build_id="b1", current_retry=2)["active"] is False


def test_review4_session_level_failure_with_passing_tests_forces_full_rerun(tmp_path):
    # Finding 2: A session-finish hook sets session.exitstatus = 1 after all
    # tests pass. The plugin records exitstatus=1, and the next retry MUST NOT
    # skip the command or tests (forces full rerun).
    _make_project(
        tmp_path,
        {
            "pkg/conftest.py": (
                "import os\n"
                "def pytest_sessionfinish(session, exitstatus):\n"
                "    if os.environ.get('FAIL_SESSION') == '1':\n"
                "        session.exitstatus = 1\n"
            ),
            "pkg/test_ok.py": "def test_passes(): pass\n",
        },
    )
    plan = _simple_plan({"tests/pkg/test_ok.py": 0}, shards=1, single_job=True)
    artifacts_root = tmp_path / "artifacts"
    cov_dir = artifacts_root / ".runtime-shard-coverage"

    run0, rec0 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-0",
        coverage_dir=cov_dir,
        retry=0,
        extra_env={"FAIL_SESSION": "1"},
    )
    assert run0.returncode == 1
    assert rec0["finished"] is True
    assert rec0["exitstatus"] == 1
    assert rec0["passed"] == ["tests/pkg/test_ok.py::test_passes"]
    assert rec0["failed"] == []

    # Tier 1 must NOT skip; Attempt 1 runs full and still catches the session failure!
    env1 = {"BUILDKITE_BUILD_ID": "b1", "BUILDKITE_JOB_ID": "job-1", "BUILDKITE_RETRY_COUNT": "1"}
    assert pr.maybe_skip_command("k", 0, 0, str(artifacts_root), str(cov_dir), env=env1) is False
    run1, rec1 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-1",
        coverage_dir=cov_dir,
        retry=1,
        retry_in=artifacts_root,
        extra_env={"FAIL_SESSION": "1"},
    )
    assert run1.returncode == 1
    assert rec1["mode"] == "full"
    assert rec1["ran"] == ["tests/pkg/test_ok.py::test_passes"]

    # Reject non-int exitstatus in malformed JSONL finish event:
    bad_finish = _write_jsonl(
        tmp_path,
        "bad-exit",
        0,
        [_start(), {"event": "finish", "finished": True, "finished_at": 1.0, "exitstatus": "1"}],
    )
    assert pr.parse_record_file(str(bad_finish)) is None

    # Legacy JSON exitstatus parsing (int preserved, non-int normalized to None):
    leg_ok = tmp_path / "leg_ok.json"
    leg_ok.write_text(
        json.dumps(
            {
                "step": "k",
                "position": 0,
                "shard": 0,
                "retry": 0,
                "finished": True,
                "finished_at": 1.0,
                "exitstatus": 2,
                "ran": [],
            }
        )
    )
    assert pr.parse_record_file(str(leg_ok))["exitstatus"] == 2
    leg_bad = tmp_path / "leg_bad.json"
    leg_bad.write_text(
        json.dumps(
            {
                "step": "k",
                "position": 0,
                "shard": 0,
                "retry": 0,
                "finished": True,
                "finished_at": 1.0,
                "exitstatus": "bad",
                "ran": [],
            }
        )
    )
    assert pr.parse_record_file(str(leg_bad))["exitstatus"] is None

    # Duplicate planned count preserved across retry (2 == 2 stays active) and
    # skipped_command record in chain stays active even when planned is None:
    dup_same = [
        _rec(0, "full", finished=False, passed=[], ran=[], planned=["a.py::t1", "a.py::t1"]),
        _rec(1, "test-retry", finished=True, passed=["a.py::t1"], ran=["a.py::t1"], planned=["a.py::t1", "a.py::t1"]),
    ]
    dup_same[1]["passed_counts"] = {"a.py::t1": 2}
    assert pr.build_retry_plan(dup_same, "k", 0, 0, build_id="b1", current_retry=2)["skip_command"] is True
    skipped_mid = [
        _rec(0, "full", finished=True, passed=["a.py::t1"], ran=["a.py::t1"], planned=["a.py::t1"]),
        {**_rec(1, "test-retry", finished=True, passed=[], ran=[], planned=None), "skipped_command": True},
    ]
    assert pr.build_retry_plan(skipped_mid, "k", 0, 0, build_id="b1", current_retry=2)["skip_command"] is True


# --- Peer review round 5 regression tests ---


def test_review5_sessionfinish_exception_is_recorded_as_failure_and_blocks_skip(tmp_path):
    # Finding 1: Another plugin's pytest_sessionfinish hook raises an exception
    # after every test passes (leaving session.exitstatus == 0). The hookwrapper
    # MUST inspect outcome.excinfo, record INTERNAL_ERROR / finished=False, and
    # prevent Tier 1 from skipping the command on retry.
    _make_project(
        tmp_path,
        {
            "pkg/conftest.py": (
                "import os\n"
                "def pytest_sessionfinish(session, exitstatus):\n"
                "    if os.environ.get('RAISE_FINALIZER') == '1':\n"
                "        raise RuntimeError('finalizer boom')\n"
            ),
            "pkg/test_ok.py": "def test_passes(): pass\n",
        },
    )
    plan = _simple_plan({"tests/pkg/test_ok.py": 0}, shards=1, single_job=True)
    artifacts_root = tmp_path / "artifacts"
    cov_dir = artifacts_root / ".runtime-shard-coverage"

    run0, rec0 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-0",
        coverage_dir=cov_dir,
        retry=0,
        extra_env={"RAISE_FINALIZER": "1"},
    )
    assert run0.returncode != 0
    assert rec0 is not None
    assert rec0["finished"] is False
    assert rec0["exitstatus"] == int(pytest.ExitCode.INTERNAL_ERROR)

    env1 = {"BUILDKITE_BUILD_ID": "b1", "BUILDKITE_JOB_ID": "job-1", "BUILDKITE_RETRY_COUNT": "1"}
    assert pr.maybe_skip_command("k", 0, 0, str(artifacts_root), str(cov_dir), env=env1) is False

    # Executing Attempt 1 MUST also run full (not skip test_passes via Tiers 2/3)!
    run1, rec1 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-1",
        coverage_dir=cov_dir,
        retry=1,
        retry_in=artifacts_root,
        extra_env={"RAISE_FINALIZER": "0"},
    )
    assert run1.returncode == 0, run1.stdout + run1.stderr
    assert rec1["mode"] == "full"
    assert rec1["ran"] == ["tests/pkg/test_ok.py::test_passes"]


def test_review6_outer_tryfirst_sessionfinish_wrapper_failure_is_captured(tmp_path):
    # Round 6 Finding 2: A conftest pytest_sessionfinish hookwrapper with
    # tryfirst=True resumes AFTER our wrapper. Both mutating session.exitstatus
    # and raising after yield MUST be recorded and prevent skipping on retry.
    for mode in ("exitstatus", "raise"):
        case_dir = tmp_path / mode
        _make_project(
            case_dir,
            {
                "pkg/conftest.py": (
                    "import os, pytest\n"
                    "@pytest.hookimpl(hookwrapper=True, tryfirst=True)\n"
                    "def pytest_sessionfinish(session, exitstatus):\n"
                    "    yield\n"
                    "    fail = os.environ.get('WRAPPER_FAIL')\n"
                    "    if fail == 'exitstatus':\n"
                    "        session.exitstatus = 1\n"
                    "    elif fail == 'raise':\n"
                    "        raise RuntimeError('outer wrapper boom')\n"
                ),
                "pkg/test_ok.py": "def test_passes(): pass\n",
            },
        )
        plan = _simple_plan({"tests/pkg/test_ok.py": 0}, shards=1, single_job=True)
        artifacts_root = case_dir / "artifacts"
        cov_dir = artifacts_root / ".runtime-shard-coverage"

        run0, rec0 = _run_plugin_pytest(
            case_dir,
            plan,
            ["-v", "pkg"],
            shard=0,
            job_id="job-0",
            coverage_dir=cov_dir,
            retry=0,
            extra_env={"WRAPPER_FAIL": mode},
        )
        assert run0.returncode != 0
        assert rec0["exitstatus"] != 0
        plan1 = pr.build_retry_plan([rec0], "k", 0, 0, build_id="b1", current_retry=1)
        assert plan1["active"] is False
        assert plan1["skip_command"] is False
        assert plan1["passed"] == set()


def test_review5_shrinking_collection_losing_unpassed_test_fails_retry_instead_of_false_green(
    tmp_path,
):
    # Finding 2: Attempt 0 collects [a, b], a passes and b fails. Attempt 1
    # shrinks collection to only [a]. After deselecting a, pytest must NOT
    # convert NO_TESTS_COLLECTED to OK — and even when another unpassed test c
    # remains and passes, losing b must still fail the retry!
    _make_project(
        tmp_path,
        {
            "pkg/test_shrink.py": (
                "import os, pytest\n"
                "cases = os.environ.get('CASES', 'a,b').split(',')\n"
                "@pytest.mark.parametrize('x', cases)\n"
                "def test_case(x):\n"
                "    if os.environ.get('BUILDKITE_RETRY_COUNT') == '0' and x != 'a':\n"
                "        assert False\n"
            )
        },
    )
    plan = _simple_plan({"tests/pkg/test_shrink.py": 0}, shards=1, single_job=True)
    artifacts_root = tmp_path / "artifacts"
    cov_dir = artifacts_root / ".runtime-shard-coverage"

    # Case 1: Attempt 0 runs [a, b] (a passes, b fails); Attempt 1 collects only [a].
    run0, rec0 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-0",
        coverage_dir=cov_dir,
        retry=0,
        extra_env={"CASES": "a,b"},
    )
    assert run0.returncode != 0
    assert rec0["passed"] == ["tests/pkg/test_shrink.py::test_case[a]"]

    run1, rec1 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job-1",
        coverage_dir=cov_dir,
        retry=1,
        retry_in=artifacts_root,
        extra_env={"CASES": "a"},
    )
    assert run1.returncode == pytest.ExitCode.NO_TESTS_COLLECTED
    assert "disappeared from collection" in run1.stderr
    assert "test_case[b]" in run1.stderr

    # Case 2: Attempt 0 runs [a, b, c] (a passes, b & c fail); Attempt 1 collects [a, c] (c passes, b vanished!).
    artifacts2 = tmp_path / "artifacts2"
    cov_dir2 = artifacts2 / ".runtime-shard-coverage"
    run2_0, _ = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job2-0",
        coverage_dir=cov_dir2,
        retry=0,
        extra_env={"CASES": "a,b,c"},
    )
    assert run2_0.returncode != 0
    run2_1, rec2_1 = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job2-1",
        coverage_dir=cov_dir2,
        retry=1,
        retry_in=artifacts2,
        extra_env={"CASES": "a,c"},
    )
    assert run2_1.returncode == pytest.ExitCode.TESTS_FAILED
    assert rec2_1["exitstatus"] == int(pytest.ExitCode.TESTS_FAILED)
    assert "test_case[b]" in run2_1.stderr

    # Case 3: Two missing unpassed tests join with ", ", and shrinking a
    # duplicate unpassed test from 2 occurrences down to 1 also fails.
    run3_1, _ = _run_plugin_pytest(
        tmp_path,
        plan,
        ["-v", "pkg"],
        shard=0,
        job_id="job2-2",
        coverage_dir=cov_dir2,
        retry=1,
        retry_in=artifacts2,
        extra_env={"CASES": "a"},
    )
    assert run3_1.returncode == pytest.ExitCode.NO_TESTS_COLLECTED
    assert (
        "tests/pkg/test_shrink.py::test_case[b], tests/pkg/test_shrink.py::test_case[c]"
        in run3_1.stderr
    )

    dup_dir = tmp_path / "dup-shrink"
    _make_project(
        dup_dir,
        {
            "pkg/test_d.py": (
                "import os\n"
                "def test_d():\n"
                "    assert os.environ.get('BUILDKITE_RETRY_COUNT') != '0'\n"
            )
        },
    )
    dup_args = ["-v", "--keep-duplicates", "pkg/test_d.py", "pkg/test_d.py"]
    dup_plan = _simple_plan({"tests/pkg/test_d.py": 0}, shards=1, single_job=True)
    dup_plan["commands"][0]["args"] = dup_args
    dup_art = dup_dir / "artifacts"
    dup_cov = dup_art / ".runtime-shard-coverage"
    r0, _ = _run_plugin_pytest(
        dup_dir, dup_plan, dup_args, shard=0, job_id="d0", coverage_dir=dup_cov, retry=0
    )
    assert r0.returncode != 0
    # Attempt 1 collects only 1 occurrence instead of 2 -> must fail despite test_d passing!
    dup_plan_one = _simple_plan({"tests/pkg/test_d.py": 0}, shards=1, single_job=True)
    dup_plan_one["commands"][0]["args"] = ["-v", "pkg/test_d.py"]
    r1, _ = _run_plugin_pytest(
        dup_dir,
        dup_plan_one,
        ["-v", "pkg/test_d.py"],
        shard=0,
        job_id="d1",
        coverage_dir=dup_cov,
        retry=1,
        retry_in=dup_art,
    )
    assert r1.returncode == pytest.ExitCode.TESTS_FAILED
    assert "test_d" in r1.stderr











