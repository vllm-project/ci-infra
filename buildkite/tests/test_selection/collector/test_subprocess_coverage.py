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


def test_rc_text_has_required_settings():
    # RC_TEXT is the source of truth; rc_path() materializes it at runtime.
    assert "parallel = true" in subprocess_coverage.RC_TEXT
    assert "sigterm = true" in subprocess_coverage.RC_TEXT
    assert "context = harness-subprocess" in subprocess_coverage.RC_TEXT


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


def test_subprocess_health_worktree_drift_is_unhealthy():
    kwargs = {**_OK_KWARGS, "checkout_state": {"ok": False, "reason": "repository_sha_mismatch"}}
    assert _subprocess_health(**kwargs) == "repository_sha_mismatch"
    kwargs = {**_OK_KWARGS, "checkout_state": {"ok": True, "after_mismatch": True}}
    assert _subprocess_health(**kwargs) == "worktree_shape_mismatch_after"


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


# --- worktree-shape baseline proof (blocker 3, revised after the 85480
# --- clean negative: CI images deliberately move vllm/ to src/vllm) ---

import gzip as _gzip
from test_selection.collector.run_trace import (
    _baseline_file_name,
    _load_worktree_baseline,
    _verify_worktree_shape,
    _worktree_shape,
)

_DIGEST = "sha256:" + "530a18df" + "0" * 56
_REPO_SHA = "e" * 40


def _baseline_doc(entries: list[str], digest: str = _DIGEST,
                  repo_sha: str = _REPO_SHA) -> dict:
    payload = (chr(0).join(sorted(entries)) + chr(0)).encode()
    return {
        "image_digest": digest,
        "repository_sha": repo_sha,
        "untracked_mode": "normal",
        "raw_sha256": __import__("hashlib").sha256(payload).hexdigest(),
        "entry_count": len(entries),
        "payload_b64gz": _base64.b64encode(_gzip.compress(payload)).decode(),
        "_entries": sorted(entries),
    }


def _write_baseline(directory: Path, doc: dict, digest: str = _DIGEST) -> None:
    (directory / _baseline_file_name(digest)).write_text(
        json.dumps({k: v for k, v in doc.items() if not k.startswith("_")})
    )


def _image_ref(digest: str = _DIGEST) -> str:
    return f"registry/repo@{digest}"


def _image_shaped_repo(tmp_path: Path) -> tuple[Path, str, list[str]]:
    """A repo in the CI image's deliberate shape: tracked vllm/ moved to
    untracked src/, plus an untracked build leftover."""

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "vllm").mkdir()
    (repo / "vllm" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "vllm" / "engine.py").write_text("one = 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit",
         "-q", "-m", "init")
    sha = _subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True
                          ).stdout.strip()
    # The image build's deliberate `mv vllm src/vllm` + COPY'd leftover.
    (repo / "src").mkdir()
    (repo / "vllm").rename(repo / "src" / "vllm")
    (repo / "torch_lib_versions.txt").write_text("torch=2.0\n")
    entries = _worktree_shape(repo)["entries"]
    return repo, sha, entries


def test_worktree_shape_matches_image_baseline(tmp_path: Path):
    repo, sha, entries = _image_shaped_repo(tmp_path)
    doc = _baseline_doc(entries, repo_sha=sha)
    directory = tmp_path / "bundle"
    directory.mkdir()
    _write_baseline(directory, doc)

    state = _verify_worktree_shape_with(directory, repo, sha)
    assert state["ok"] is True
    assert state["baseline_count"] == len(entries)


def _verify_worktree_shape_with(directory, repo, sha):
    from unittest import mock

    with mock.patch(
        "test_selection.collector.run_trace._load_worktree_baseline",
        lambda repository_sha, image_ref: _load_worktree_baseline(
            repository_sha, image_ref, directory=directory
        ),
    ):
        return _verify_worktree_shape(repo, sha, _image_ref())


def test_worktree_shape_rejects_tracked_edit(tmp_path: Path):
    repo, sha, entries = _image_shaped_repo(tmp_path)
    directory = tmp_path / "bundle"
    directory.mkdir()
    _write_baseline(directory, _baseline_doc(entries, repo_sha=sha))
    # A tracked test file changes under the shape-stable image layout.
    (repo / "src" / "vllm" / "__init__.py").write_text("")  # untracked area
    (repo / "new_test.py").write_text("x = 1\n")  # untracked addition
    state = _verify_worktree_shape_with(directory, repo, sha)
    assert state["ok"] is False
    assert state["reason"] == "worktree_shape_mismatch"
    assert state["added_total"] == 1
    assert "?? new_test.py" in state["added_sample"]


def test_worktree_shape_blindness_documented(tmp_path: Path):
    # Edits INSIDE the untracked src/ tree are invisible to git status:
    # this invariant is shape, not bytes. Executable-code provenance rests
    # on the pinned image digest + import preflight, not this check.
    repo, sha, entries = _image_shaped_repo(tmp_path)
    directory = tmp_path / "bundle"
    directory.mkdir()
    _write_baseline(directory, _baseline_doc(entries, repo_sha=sha))
    (repo / "src" / "vllm" / "engine.py").write_text("tampered = 1\n")
    state = _verify_worktree_shape_with(directory, repo, sha)
    assert state["ok"] is True  # shape unchanged; documented blindness


def test_worktree_shape_rejects_head_mismatch(tmp_path: Path):
    repo, sha, entries = _image_shaped_repo(tmp_path)
    directory = tmp_path / "bundle"
    directory.mkdir()
    _write_baseline(directory, _baseline_doc(entries, repo_sha=sha))
    state = _verify_worktree_shape(repo, "0" * 40, _image_ref())
    assert state["ok"] is False
    assert state["reason"] == "repository_sha_mismatch"


def test_baseline_binding_failures_are_distinct(tmp_path: Path):
    repo, sha, entries = _image_shaped_repo(tmp_path)
    directory = tmp_path / "bundle"
    directory.mkdir()
    _write_baseline(directory, _baseline_doc(entries, repo_sha=sha))

    # No baseline for a different digest -> missing
    other = "sha256:" + "f" * 64
    doc, err = _load_worktree_baseline(sha, _image_ref(other), directory)
    assert doc is None and err == "worktree_baseline_missing"
    # Unpinned image reference
    doc, err = _load_worktree_baseline(sha, "registry/repo:tag", directory)
    assert doc is None and err == "worktree_baseline_unpinned_image"
    # Baseline bound to a different image
    doc2 = _baseline_doc(entries, digest="sha256:" + "b" * 64, repo_sha=sha)
    _write_baseline(directory, doc2, digest=_DIGEST)  # file name says _DIGEST
    doc, err = _load_worktree_baseline(sha, _image_ref(), directory)
    assert doc is None and err == "worktree_baseline_image_mismatch"
    # Baseline bound to a different repository SHA
    _write_baseline(
        directory, _baseline_doc(entries, repo_sha="0" * 40), digest=_DIGEST
    )
    doc, err = _load_worktree_baseline(sha, _image_ref(), directory)
    assert doc is None and err == "worktree_baseline_sha_mismatch"
    # Corrupt payload
    bad = _baseline_doc(entries, repo_sha=sha)
    bad["raw_sha256"] = "0" * 64
    _write_baseline(directory, bad, digest=_DIGEST)
    doc, err = _load_worktree_baseline(sha, _image_ref(), directory)
    assert doc is None and err == "worktree_baseline_corrupt"


def test_bundled_baseline_loads_and_binds():
    # The real bundled baseline for the eac636a7 pilot image.
    doc, err = _load_worktree_baseline(
        "eac636a7fa476983cdae34b45a984e9852aad375",
        "public.ecr.aws/q9t5s3a7/vllm-ci-test-repo@sha256:530a18dfb04c66cdb4ebb939b111d84c47b902abf21b3e7d3fded2deac8b556a",
    )
    assert err is None
    assert doc["entry_count"] == 2868
    assert len(doc["_entries"]) == 2868
    assert doc["raw_sha256"] == (
        "4baa54f37a7498939362267b1d88b89212b0b9d9f2830dd9d01516cb9fcd87b1"
    )



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
    # Baseline binding: clean-tree baseline for this repo, image-pinned.
    image_ref = "registry/repo@" + _DIGEST
    monkeypatch.setenv("IMAGE_TAG", image_ref)
    baseline = _baseline_doc(_worktree_shape(repo)["entries"], repo_sha=sha)
    monkeypatch.setattr(
        run_trace, "_load_worktree_baseline",
        lambda repository_sha, ref: (baseline, None),
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
    # The hook installs (into the patched test site dir) and the command runs
    # clean, but nothing proves a serve interpreter ran or produced rows.
    job, exit_code = _run_main(tmp_path, "true", monkeypatch)
    assert exit_code == 0  # command itself succeeded
    assert job["healthy"] is False
    assert job["failure_reason"] == "subprocess_no_serve_interpreter"


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
