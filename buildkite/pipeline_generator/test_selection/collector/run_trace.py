# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Run a bounded pytest pilot and export per-test Python line coverage."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from coverage import CoverageData

from . import COLLECTOR_VERSION


def _atomic_json(path: Path, document: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _atomic_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")))
            stream.write("\n")
    temporary.replace(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _shell_exit_code(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def _git_sha(repo_root: Path) -> str:
    configured = os.environ.get("BUILDKITE_COMMIT")
    if configured:
        return configured
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def normalize_repository_path(filename: str, repo_root: Path) -> str | None:
    """Map source/install paths to a repository-relative ``vllm/...`` path."""

    path = Path(filename).resolve()
    try:
        relative = path.relative_to(repo_root.resolve())
    except ValueError:
        relative = None
    if relative is not None and relative.parts and relative.parts[0] == "vllm":
        return relative.as_posix()

    parts = path.parts
    for index, part in enumerate(parts):
        if part == "vllm":
            candidate = Path(*parts[index:]).as_posix()
            if candidate.startswith("vllm/"):
                return candidate
    return None


def _node_id(context: str) -> str | None:
    if not context or context == "":
        return None
    node_id, separator, phase = context.rpartition("|")
    if separator and phase in {"run", "setup", "teardown"}:
        return node_id
    return None


def coverage_rows(
    coverage_file: Path,
    repo_root: Path,
    *,
    repository_sha: str,
    job_key: str,
) -> list[dict[str, Any]]:
    data = CoverageData(basename=str(coverage_file))
    data.read()
    rows: set[tuple[str, str, int]] = set()
    for filename in data.measured_files():
        repository_path = normalize_repository_path(filename, repo_root)
        if repository_path is None:
            continue
        for line, contexts in data.contexts_by_lineno(filename).items():
            for context in contexts:
                node_id = _node_id(context)
                if node_id:
                    rows.add((node_id, repository_path, int(line)))

    return [
        {
            "collector_version": COLLECTOR_VERSION,
            "file": file,
            "job_key": job_key,
            "line": line,
            "repository_sha": repository_sha,
            "test_id": test_id,
        }
        for test_id, file, line in sorted(rows)
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--job-key", required=True)
    parser.add_argument("--represented-job-key", required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--command-base64")
    parser.add_argument("--command-cwd", type=Path)
    parser.add_argument("tests", nargs=argparse.REMAINDER)
    return parser


def pytest_command(tests: list[str]) -> list[str]:
    package = __package__ or "ci_test_selection"
    return [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        f"{package}.pytest_trace_plugin",
        "-p",
        f"{package}.nvtx_test_ranges",
        "--cov=vllm",
        "--cov-context=test",
        "--cov-report=",
        *tests,
    ]


def _decode_command(value: str) -> str:
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as error:
        message = "--command-base64 must contain base64-encoded UTF-8"
        raise SystemExit(message) from error


def _command_environment(
    *,
    coverage_file: Path,
    node_file: Path,
    repo_root: Path,
    auto_load_pytest: bool,
) -> dict[str, str]:
    environment = dict(os.environ)
    environment["COVERAGE_FILE"] = str(coverage_file)
    environment["VLLM_CI_TEST_SELECTION_NODEIDS"] = str(node_file)

    if auto_load_pytest:
        package = __package__ or "ci_test_selection"
        environment["VLLM_CI_TEST_SELECTION_PACKAGE"] = package
        plugins = [
            f"{package}.pytest_trace_plugin",
            f"{package}.nvtx_test_ranges",
        ]
        configured_plugins = environment.get("PYTEST_PLUGINS")
        if configured_plugins:
            plugins.append(configured_plugins)
        environment["PYTEST_PLUGINS"] = ",".join(plugins)

    trace_options = (
        "--cov=vllm --cov-context=test --cov-append --cov-report="
        if auto_load_pytest
        else ""
    )
    existing_options = environment.get("PYTEST_ADDOPTS", "")
    environment["PYTEST_ADDOPTS"] = " ".join(
        option for option in (existing_options, trace_options) if option
    )
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(repo_root), existing_pythonpath) if value
    )
    return environment


_IMPORT_PREFLIGHT = r"""
import importlib
import json
import os
from pathlib import Path
import sys

document = {
    "error": None,
    "sys_executable": sys.executable,
    "sys_path": sys.path,
    "vllm_file": None,
}
try:
    import coverage
    import pytest
    import pytest_cov

    package = os.environ.get("VLLM_CI_TEST_SELECTION_PACKAGE")
    if package:
        importlib.import_module(f"{package}.pytest_trace_plugin")
        importlib.import_module(f"{package}.nvtx_test_ranges")
    import vllm

    resolved = Path(vllm.__file__).resolve()
    document["vllm_file"] = str(resolved)
    checkout_value = os.environ.get("BUILDKITE_BUILD_CHECKOUT_PATH")
    expected_root_value = os.environ.get("VLLM_CI_TEST_SELECTION_REPO_ROOT")
    if checkout_value and expected_root_value:
        checkout = Path(checkout_value).resolve()
        expected_root = Path(expected_root_value).resolve()
        try:
            resolved.relative_to(checkout)
        except ValueError:
            pass
        else:
            if expected_root != checkout:
                document["error"] = "image job imported vllm from checkout source"
except Exception as error:
    document["error"] = f"{type(error).__name__}: {error}"

print(json.dumps(document, sort_keys=True, separators=(",", ":")))
raise SystemExit(1 if document["error"] else 0)
"""


def validate_import_environment(
    *,
    command_cwd: Path,
    environment: dict[str, str],
    output_path: Path,
    repo_root: Path,
) -> int:
    """Prove the traced command imports the image's vLLM, not checkout source."""

    preflight_environment = dict(environment)
    preflight_environment["VLLM_CI_TEST_SELECTION_REPO_ROOT"] = str(repo_root.resolve())
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PREFLIGHT],
        cwd=command_cwd,
        env=preflight_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if stdout:
        print(f"trace import preflight: {stdout}")
    if stderr:
        print(stderr, file=sys.stderr)
    try:
        document = json.loads(stdout.splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        document = {
            "error": "import preflight did not emit valid JSON",
            "stderr": stderr,
            "stdout": stdout,
        }
    status = _shell_exit_code(result.returncode)
    document["import_exit_code"] = status
    document["pytest_plugin_exit_code"] = None
    if status == 0:
        pytest_environment = dict(preflight_environment)
        # ``pytest --help`` exits before pluggy validates hooks and before
        # pytest parses every injected option. Collect one inert test under the
        # exact command environment so a bad plugin or unavailable option
        # triggers the uninstrumented fallback before the production command
        # is marked started. Keep all preflight evidence in a temporary
        # directory so collection cannot contaminate the real trace outputs.
        with tempfile.TemporaryDirectory(
            prefix=".pytest-preflight-", dir=output_path.parent
        ) as temporary_directory:
            pytest_preflight_dir = Path(temporary_directory)
            pytest_target = pytest_preflight_dir / "test_preflight.py"
            pytest_target.write_text(
                "def test_vllm_ci_test_selection_preflight():\n    pass\n",
                encoding="utf-8",
            )
            pytest_environment["COVERAGE_FILE"] = str(
                pytest_preflight_dir / ".coverage"
            )
            pytest_environment["VLLM_CI_TEST_SELECTION_NODEIDS"] = str(
                pytest_preflight_dir / "pytest-nodes.json"
            )
            pytest_result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "--collect-only",
                    "--quiet",
                    str(pytest_target),
                ],
                cwd=command_cwd,
                env=pytest_environment,
                capture_output=True,
                text=True,
                check=False,
            )
        status = _shell_exit_code(pytest_result.returncode)
        document["pytest_plugin_exit_code"] = status
        if status != 0:
            stderr = pytest_result.stderr.strip()
            document["error"] = "pytest plugin/options preflight failed"
            document["pytest_stderr"] = stderr
            if stderr:
                print(stderr, file=sys.stderr)
    document["exit_code"] = status
    _atomic_json(output_path, document)
    return status


def _merge_node_documents(output_dir: Path, node_file: Path) -> dict[str, Any]:
    documents = []
    for path in sorted(output_dir.glob("pytest-nodes*.json")):
        documents.append(json.loads(path.read_text(encoding="utf-8")))
    if not documents:
        return {"collected": [], "exit_status": None, "outcomes": {}}

    collected: set[str] = set()
    outcomes: dict[str, dict[str, str]] = {}
    exit_statuses: list[int] = []
    for document in documents:
        collected.update(document.get("collected", []))
        for node_id, phases in document.get("outcomes", {}).items():
            outcomes.setdefault(node_id, {}).update(phases)
        if document.get("exit_status") is not None:
            exit_statuses.append(int(document["exit_status"]))

    merged = {
        "collected": sorted(collected),
        "exit_status": max(exit_statuses, default=None),
        "outcomes": {node_id: outcomes[node_id] for node_id in sorted(outcomes)},
    }
    _atomic_json(node_file, merged)
    return merged


def main() -> int:
    args = _parser().parse_args()
    tests = args.tests[1:] if args.tests[:1] == ["--"] else args.tests
    if bool(tests) == bool(args.command_base64):
        raise SystemExit(
            "provide exactly one of --command-base64 or pytest targets after --"
        )

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    coverage_file = output_dir / ".coverage"
    node_file = output_dir / "pytest-nodes.json"
    trace_file = output_dir / "python-trace.jsonl"
    job_file = output_dir / "job.json"
    command_status_file = output_dir / "command-status.json"
    repository_sha = _git_sha(args.repo_root)

    environment = _command_environment(
        coverage_file=coverage_file,
        node_file=node_file,
        repo_root=args.repo_root,
        auto_load_pytest=bool(args.command_base64),
    )
    command_cwd = (args.command_cwd or args.repo_root).resolve()
    if args.command_base64:
        command_text = _decode_command(args.command_base64)
        command = ["bash", "-lc", command_text]
    else:
        command_text = None
        command = pytest_command(tests)
    preflight_status = validate_import_environment(
        command_cwd=command_cwd,
        environment=environment,
        output_path=output_dir / "import-environment.json",
        repo_root=args.repo_root,
    )
    if preflight_status == 0:
        _atomic_json(
            command_status_file,
            {
                "command_executed": True,
                "created_at": datetime.now(UTC).isoformat(),
                "exit_code": None,
                "phase": "started",
            },
        )
        result = subprocess.run(
            command,
            cwd=command_cwd,
            env=environment,
            check=False,
        )
        command_exit_code = _shell_exit_code(result.returncode)
        _atomic_json(
            command_status_file,
            {
                "command_executed": True,
                "created_at": datetime.now(UTC).isoformat(),
                "exit_code": command_exit_code,
                "phase": "finished",
            },
        )
    else:
        result = subprocess.CompletedProcess(command, preflight_status)
        command_exit_code = preflight_status
    command_executed = preflight_status == 0

    rows = (
        coverage_rows(
            coverage_file,
            args.repo_root,
            repository_sha=repository_sha,
            job_key=args.represented_job_key,
        )
        if coverage_file.exists()
        else []
    )
    _atomic_jsonl(trace_file, rows)
    node_document = _merge_node_documents(output_dir, node_file)
    healthy = (
        command_exit_code == 0
        and bool(node_document["collected"])
        and coverage_file.exists()
    )
    _atomic_json(
        job_file,
        {
            "child_process_attribution": False,
            "collector_version": COLLECTOR_VERSION,
            "command_sha256": (
                hashlib.sha256(command_text.encode("utf-8")).hexdigest()
                if command_text is not None
                else None
            ),
            "command_cwd": str(command_cwd),
            "command_executed": command_executed,
            "created_at": datetime.now(UTC).isoformat(),
            "healthy": healthy,
            "image_tag": os.environ.get("IMAGE_TAG"),
            "import_preflight_exit_code": preflight_status,
            "job_key": args.job_key,
            "node_ids": node_document["collected"],
            "pytest_exit_code": command_exit_code,
            "python_trace": trace_file.name,
            "python_trace_rows": len(rows),
            "python_trace_sha256": _sha256(trace_file),
            "repository_sha": repository_sha,
            "represented_job_key": args.represented_job_key,
        },
    )
    coverage_file.unlink(missing_ok=True)
    return command_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
