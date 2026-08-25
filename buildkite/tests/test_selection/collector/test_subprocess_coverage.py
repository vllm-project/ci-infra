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
