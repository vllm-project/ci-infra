# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import json
import os
import sys
from pathlib import Path

import pytest
from coverage import CoverageData

from test_selection.collector import subprocess_coverage
from test_selection.collector.run_trace import coverage_rows


def test_boot_is_noop_without_env(monkeypatch):
    monkeypatch.delenv("COVERAGE_PROCESS_START", raising=False)
    subprocess_coverage.boot()  # must not raise or start anything


def test_enable_writes_pth_and_state(tmp_path, monkeypatch):
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    monkeypatch.setattr(subprocess_coverage, "_site_packages", lambda: [site_dir])

    assert subprocess_coverage.enable(tmp_path) is True
    pth = site_dir / subprocess_coverage.PTH_NAME
    assert "boot()" in pth.read_text(encoding="utf-8")

    state = json.loads((tmp_path / subprocess_coverage.HOOK_STATE_NAME).read_text())
    assert state["installed"] is True
    assert state["path"] == str(pth)


def test_enable_soft_fails_without_writable_site(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess_coverage, "_site_packages", lambda: [tmp_path / "missing"]
    )
    assert subprocess_coverage.enable(tmp_path) is False
    state = json.loads((tmp_path / subprocess_coverage.HOOK_STATE_NAME).read_text())
    assert state["installed"] is False


def test_enable_soft_fails_without_coverage(tmp_path, monkeypatch):
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    monkeypatch.setattr(subprocess_coverage, "_site_packages", lambda: [site_dir])
    monkeypatch.setitem(sys.modules, "coverage", None)

    assert subprocess_coverage.enable(tmp_path) is False
    assert not (site_dir / subprocess_coverage.PTH_NAME).exists()


def test_disable_is_idempotent(tmp_path, monkeypatch):
    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    monkeypatch.setattr(subprocess_coverage, "_site_packages", lambda: [site_dir])

    subprocess_coverage.enable(tmp_path)
    assert subprocess_coverage.disable() is True
    assert subprocess_coverage.disable() is False


def test_rc_path_writes_required_settings():
    rc = subprocess_coverage.rc_path()
    try:
        text = rc.read_text(encoding="utf-8")
    finally:
        rc.unlink(missing_ok=True)
    assert "parallel = true" in text
    assert "sigterm = true" in text


def _write_contexts(coverage_file: Path, contexts: dict[str, set[int]]) -> None:
    source = coverage_file.parent / "repo" / "vllm" / "engine.py"
    source.parent.mkdir(parents=True)
    source.write_text("one = 1\ntwo = 2\nthree = 3\n", encoding="utf-8")
    data = CoverageData(basename=str(coverage_file))
    for context, lines in contexts.items():
        data.set_context(context)
        data.add_lines({str(source): lines})
    data.write()


def test_coverage_rows_map_stacked_and_serve_contexts(tmp_path: Path):
    coverage_file = tmp_path / ".coverage"
    _write_contexts(
        coverage_file,
        {
            "harness-subprocess|tests/test_api.py::test_serve|run": {1, 2},
            "harness-subprocess": {3},
        },
    )

    rows = coverage_rows(
        coverage_file,
        tmp_path / "repo",
        repository_sha="a" * 40,
        job_key="some-job",
        subprocess_contexts=True,
    )

    by_line = {row["line"]: row["test_id"] for row in rows}
    assert by_line == {
        1: "tests/test_api.py::test_serve",
        2: "tests/test_api.py::test_serve",
        3: "job::some-job",
    }


def test_coverage_rows_drop_subprocess_contexts_without_flag(tmp_path: Path):
    coverage_file = tmp_path / ".coverage"
    _write_contexts(coverage_file, {"harness-subprocess": {3}})

    rows = coverage_rows(
        coverage_file,
        tmp_path / "repo",
        repository_sha="a" * 40,
        job_key="some-job",
    )

    assert rows == []


def test_coverage_rows_unaffected_contexts_still_parse_with_flag(tmp_path: Path):
    coverage_file = tmp_path / ".coverage"
    _write_contexts(
        coverage_file, {"tests/test_api.py::test_plain|run": {1}}
    )

    rows = coverage_rows(
        coverage_file,
        tmp_path / "repo",
        repository_sha="a" * 40,
        job_key="some-job",
        subprocess_contexts=True,
    )

    assert [row["test_id"] for row in rows] == ["tests/test_api.py::test_plain"]


def test_boot_starts_coverage_when_env_set(monkeypatch, tmp_path):
    rc = tmp_path / "coveragerc"
    rc.write_text("[run]\nparallel = true\n", encoding="utf-8")
    monkeypatch.setenv("COVERAGE_PROCESS_START", str(rc))
    monkeypatch.setenv("COVERAGE_FILE", str(tmp_path / ".coverage"))
    import coverage

    subprocess_coverage.boot()
    try:
        assert coverage.Coverage.current() is not None
    finally:
        current = coverage.Coverage.current()
        if current is not None:
            current.stop()


# --- fail-closed health gate (blocker 1) ---

from test_selection.collector.run_trace import _serve_markers, _subprocess_health


def _write_marker(directory: Path, pid: int, argv: list[str]) -> None:
    (directory / f"subprocess-hook-ran.{pid}.json").write_text(
        json.dumps(
            {"argv": argv, "executable": "/opt/venv/bin/python3", "pid": pid,
             "prefix": "/opt/venv", "time": 0.0}
        )
        + "\n",
        encoding="utf-8",
    )


_SERVE_MARKER = {"argv": ["/opt/venv/bin/vllm", "serve", "m"], "pid": 1}
_OK_KWARGS = dict(
    hook_state={"installed": True},
    combine_ok=True,
    checkout_state={"ok": True},
    serve_markers=[_SERVE_MARKER],
    has_serve_rows=True,
)


def test_subprocess_health_ok():
    assert _subprocess_health(**_OK_KWARGS) is None


def test_subprocess_health_hook_skipped_is_unhealthy():
    kwargs = {**_OK_KWARGS, "hook_state": {"installed": False}}
    assert _subprocess_health(**kwargs) == "subprocess_hook_not_installed"
    kwargs = {**_OK_KWARGS, "hook_state": None}
    assert _subprocess_health(**kwargs) == "subprocess_hook_not_installed"


def test_subprocess_health_combine_failure_is_unhealthy():
    kwargs = {**_OK_KWARGS, "combine_ok": False}
    assert _subprocess_health(**kwargs) == "subprocess_combine_failed"


def test_subprocess_health_checkout_drift_is_unhealthy():
    kwargs = {**_OK_KWARGS, "checkout_state": {"ok": False, "reason": "repository_sha_mismatch"}}
    assert _subprocess_health(**kwargs) == "checkout_repository_sha_mismatch"
    kwargs = {**_OK_KWARGS, "checkout_state": {"ok": True, "dirty_after": ["M x"]}}
    assert _subprocess_health(**kwargs) == "checkout_dirty_after"


def test_subprocess_health_no_serve_interpreter_is_unhealthy():
    kwargs = {**_OK_KWARGS, "serve_markers": []}
    assert _subprocess_health(**kwargs) == "subprocess_no_serve_interpreter"


def test_subprocess_health_no_serve_rows_is_unhealthy():
    kwargs = {**_OK_KWARGS, "has_serve_rows": False}
    assert _subprocess_health(**kwargs) == "subprocess_no_serve_evidence"


def test_serve_markers_identify_serve_argv(tmp_path: Path):
    _write_marker(tmp_path, 1, ["/opt/venv/bin/vllm", "serve", "model"])
    _write_marker(tmp_path, 2, ["python3", "-m", "pytest", "tests/"])
    _write_marker(tmp_path, 3, ["python3", "-c", "import vllm"])  # helper, not serve
    (tmp_path / "subprocess-hook-ran.4.json").write_text("not json")
    serve = _serve_markers(tmp_path)
    assert len(serve) == 1
    assert serve[0]["pid"] == 1


# --- pristine checkout proof (blocker 3) ---

from test_selection.collector.run_trace import _verify_pristine_checkout


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _make_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "vllm").mkdir()
    (repo / "vllm" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "vllm" / "engine.py").write_text(
        "one = 1\ntwo = 2\nthree = 3\n", encoding="utf-8"
    )
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit",
         "-q", "-m", "init")
    import subprocess

    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True, check=True).stdout.strip()
    return repo, sha


def test_pristine_checkout_ok(tmp_path: Path):
    repo, sha = _make_repo(tmp_path)
    state = _verify_pristine_checkout(repo, sha)
    assert state["ok"] is True
    assert state["head"] == sha


def test_pristine_checkout_rejects_sha_mismatch(tmp_path: Path):
    repo, _sha = _make_repo(tmp_path)
    state = _verify_pristine_checkout(repo, "0" * 40)
    assert state["ok"] is False
    assert state["reason"] == "repository_sha_mismatch"


def test_pristine_checkout_rejects_dirty_tree(tmp_path: Path):
    repo, sha = _make_repo(tmp_path)
    (repo / "stray.txt").write_text("x")
    state = _verify_pristine_checkout(repo, sha)
    assert state["ok"] is False
    assert state["reason"] == "checkout_dirty_before"


def test_pristine_checkout_rejects_non_repo(tmp_path: Path):
    state = _verify_pristine_checkout(tmp_path, "0" * 40)
    assert state["ok"] is False
    assert state["reason"] == "git_rev_parse_failed"


# --- immutable image pinning (blocker 2) ---

from utils_lib.docker_utils import pin_image_digest

GOOD_DIGEST = "sha256:" + "a" * 64


def test_pin_image_digest_converts_tag():
    assert (
        pin_image_digest("reg.example/repo:$BUILDKITE_COMMIT", GOOD_DIGEST)
        == f"reg.example/repo@{GOOD_DIGEST}"
    )


def test_pin_image_digest_rejects_variants_and_malformed():
    import pytest as _pytest

    with _pytest.raises(ValueError):
        pin_image_digest("reg.example/repo:tag-cpu", GOOD_DIGEST)
    with _pytest.raises(ValueError):
        pin_image_digest("reg.example/repo:tag-torch-nightly", GOOD_DIGEST)
    with _pytest.raises(ValueError):
        pin_image_digest("reg.example/repo", GOOD_DIGEST)
    with _pytest.raises(ValueError):
        pin_image_digest("reg.example/repo:tag", "not-a-digest")
    with _pytest.raises(ValueError):
        pin_image_digest("reg.example/repo:tag", "sha256:" + "A" * 64)


def test_subprocess_health_after_status_error_fails_closed():
    kwargs = {
        **_OK_KWARGS,
        "checkout_state": {"ok": True, "status_after_error": "fatal: not a repo"},
    }
    assert _subprocess_health(**kwargs) == "checkout_status_failed_after"


# --- end-to-end through run_trace.main: command cwd inside a real repo,
# --- collector output outside it (blocker 1's integration shape) ---

import base64 as _base64
import subprocess as _subprocess

from test_selection.collector import run_trace


def _run_main(tmp_path: Path, command: str, monkeypatch) -> dict:
    repo, sha = _make_repo(tmp_path)
    out = tmp_path / "outside" / "trace"
    monkeypatch.setenv("BUILDKITE_COMMIT", sha)
    monkeypatch.setenv("BUILDKITE_BUILD_CHECKOUT_PATH", str(repo))
    # The traced wrapper sets this before any collector import (the
    # h200-ci-2-28 incident); mirror that contract or the preflight's vllm
    # import writes __pycache__ into the checkout and trips the dirty guard.
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    # The preflight subprocess needs the collector importable, as the real
    # wrapper provides via PYTHONPATH=$TRACE_COLLECTOR_DIR/src.
    collector_root = str(Path(run_trace.__file__).resolve().parents[3])
    monkeypatch.setenv("PYTHONPATH", collector_root)
    # Never let the hook install into this environment's real site-packages.
    hook_site = tmp_path / "hook-site"
    hook_site.mkdir(exist_ok=True)
    monkeypatch.setattr(
        subprocess_coverage, "_site_packages", lambda: [hook_site]
    )
    argv = [
        "run_trace",
        "--output-dir", str(out),
        "--job-key", "k",
        "--represented-job-key", "k",
        "--repo-root", str(repo),
        "--command-cwd", str(repo),
        "--command-base64",
        _base64.b64encode(command.encode()).decode(),
        "--subprocess-coverage",
    ]
    monkeypatch.setattr("sys.argv", argv)
    exit_code = run_trace.main()
    return json.loads((out / "job.json").read_text()), exit_code


def test_main_subprocess_missing_hook_evidence_fails_closed(tmp_path, monkeypatch):
    # The command runs clean but no hook/marker/serve evidence exists.
    job, exit_code = _run_main(tmp_path, "true", monkeypatch)
    assert exit_code == 0  # command itself succeeded
    assert job["healthy"] is False
    assert job["failure_reason"] in (
        "subprocess_hook_not_installed",
        "subprocess_no_serve_interpreter",
    )


def test_main_subprocess_complete_evidence_is_healthy(tmp_path, monkeypatch):
    out = tmp_path / "outside" / "trace"
    out.mkdir(parents=True)
    # Simulate what the .pth hook produces: an installed-hook sentinel, a
    # serve-argv marker, and a coverage shard with a bare-context row.
    (out / "subprocess-hook.json").write_text(
        json.dumps({"installed": True, "path": "/x"}) + "\n"
    )
    _write_marker(out, 4242, ["/opt/venv/bin/vllm", "serve", "m"])
    _write_contexts(out / ".coverage", {"harness-subprocess": {1}})

    job, exit_code = _run_main(tmp_path, "true", monkeypatch)
    assert exit_code == 0
    assert job["healthy"] is True, job["failure_reason"]
    assert job["subprocess_hook"]["installed"] is True
    assert job["subprocess_serve_markers"][0]["pid"] == 4242
    assert job["checkout_state"]["ok"] is True
    rows = (out / "python-trace.jsonl").read_text().strip().splitlines()
    assert json.loads(rows[0])["test_id"] == "job::k"
