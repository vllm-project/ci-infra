# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Run a bounded pytest pilot and export per-test Python line coverage."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from coverage import CoverageData

from . import COLLECTOR_VERSION
from . import subprocess_coverage as _subprocess_coverage

_SUBPROCESS_CONTEXT_LABEL = _subprocess_coverage.CONTEXT_LABEL


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


def _serve_markers(output_dir: Path) -> list[dict[str, Any]]:
    """Per-process hook receipts that identify a `vllm serve` interpreter."""

    markers = []
    for marker_path in sorted(output_dir.glob("subprocess-hook-ran.*.json")):
        try:
            markers.append(json.loads(marker_path.read_text("utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return [
        marker
        for marker in markers
        if len(marker.get("argv", [])) >= 2
        and "vllm" in marker["argv"][0]
        and marker["argv"][1] == "serve"
    ]


def _subprocess_health(
    *,
    hook_state: dict[str, Any] | None,
    combine_ok: bool,
    checkout_state: dict[str, Any],
    serve_markers: list[dict[str, Any]],
    has_serve_rows: bool,
) -> str | None:
    """Fail-closed reason for subprocess coverage, or None when healthy.

    Hook skipped, combine failure, a drifted/dirty checkout, no hooked serve
    interpreter, and no serve-side rows each get a distinct reason so a green
    pytest client can never mask missing server evidence.
    """

    if not (hook_state or {}).get("installed"):
        return "subprocess_hook_not_installed"
    if not combine_ok:
        return "subprocess_combine_failed"
    if checkout_state and not checkout_state.get("ok"):
        return "checkout_" + str(checkout_state.get("reason"))
    if "status_after_error" in checkout_state:
        return "checkout_status_failed_after"
    if checkout_state.get("dirty_after"):
        return "checkout_dirty_after"
    if not serve_markers:
        return "subprocess_no_serve_interpreter"
    if not has_serve_rows:
        return "subprocess_no_serve_evidence"
    return None


def _verify_pristine_checkout(repo_root: Path, expected_sha: str) -> dict[str, Any]:
    """Prove the checkout under trace is exactly the claimed commit, clean.

    BUILDKITE_COMMIT is only an environment claim; the evidence rows carry it
    as repository_sha, so subprocess mode (which exists to merge into a graph
    pinned at that SHA) verifies the real tree. Fail closed on any drift.
    """

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )

    head = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain")
    state: dict[str, Any] = {
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "dirty": status.stdout.splitlines() if status.returncode == 0 else None,
        "expected": expected_sha,
        "ok": False,
    }
    if head.returncode != 0:
        state["reason"] = "git_rev_parse_failed"
    elif status.returncode != 0:
        state["reason"] = "git_status_failed"
    elif state["head"] != expected_sha:
        state["reason"] = "repository_sha_mismatch"
    elif state["dirty"]:
        state["reason"] = "checkout_dirty_before"
    else:
        state["ok"] = True
    return state


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


def _subprocess_test_id(context: str, job_key: str) -> str | None:
    """Map subprocess-hook coverage contexts to evidence test ids.

    Serve-side rows carry the bare static label and become job-level
    evidence. Pytest client rows stack as ``<label>|<nodeid>|<phase>`` when
    pytest-cov piggybacks on the already-active coverage instance; the label
    prefix is stripped so the node id parses as usual.
    """

    from . import subprocess_coverage

    label = subprocess_coverage.CONTEXT_LABEL
    if context == label:
        return f"job::{job_key}"
    if context.startswith(f"{label}|"):
        return _node_id(context[len(label) + 1 :])
    return None


def coverage_rows(
    coverage_file: Path,
    repo_root: Path,
    *,
    repository_sha: str,
    job_key: str,
    subprocess_contexts: bool = False,
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
                if subprocess_contexts and (
                    context == _SUBPROCESS_CONTEXT_LABEL
                    or context.startswith(_SUBPROCESS_CONTEXT_LABEL + "|")
                ):
                    # Subprocess-hook contexts: never fall through to the
                    # plain parser, which would keep the label prefix.
                    node_id = _subprocess_test_id(context, job_key)
                else:
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
    parser.add_argument(
        "--subprocess-coverage",
        action="store_true",
        help="Record coverage in every Python subprocess of the command "
        "(site-packages .pth hook + COVERAGE_PROCESS_START), for "
        "shell-harness jobs whose code under test runs in serve processes.",
    )
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


def _collector_import_root() -> Path:
    """Return the path that makes this collector's top-level package importable."""

    package = __package__ or "ci_test_selection"
    return Path(__file__).resolve().parents[len(package.split("."))]


def _install_pytest_launcher(
    directory: Path,
    environment: dict[str, str],
) -> None:
    """Keep collector plugins importable across command-local PYTHONPATH changes."""

    real_pytest = shutil.which("pytest", path=environment.get("PATH"))
    if real_pytest:
        target = f'{shlex.quote(real_pytest)} "$@"'
    else:
        target = f'{shlex.quote(sys.executable)} -m pytest "$@"'
    collector_root = shlex.quote(str(_collector_import_root()))
    launcher = directory / "pytest"
    launcher.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        f"export PYTHONPATH={collector_root}${{PYTHONPATH:+:$PYTHONPATH}}\n"
        "node_file=${VLLM_CI_TEST_SELECTION_NODEIDS:?}\n"
        'counter_file="${node_file}.invocations"\n'
        'lock_dir="${counter_file}.lock"\n'
        "attempt=0\n"
        'while ! mkdir "$lock_dir" 2>/dev/null; do\n'
        "  attempt=$((attempt + 1))\n"
        '  if [ "$attempt" -ge 500 ]; then\n'
        '    echo "timed out allocating pytest invocation id" >&2\n'
        "    exit 70\n"
        "  fi\n"
        "  sleep 0.01\n"
        "done\n"
        "trap 'rmdir \"$lock_dir\" 2>/dev/null || true' EXIT HUP INT TERM\n"
        "count=0\n"
        'if [ -s "$counter_file" ]; then IFS= read -r count < "$counter_file"; fi\n'
        'case "$count" in\n'
        "  ''|*[!0-9]*) echo \"invalid pytest invocation counter\" >&2; exit 70 ;;\n"
        "esac\n"
        "next=$((count + 1))\n"
        'temporary="${counter_file}.tmp.$$"\n'
        'printf \'%s\\n\' "$next" > "$temporary"\n'
        'mv "$temporary" "$counter_file"\n'
        'rmdir "$lock_dir"\n'
        "trap - EXIT HUP INT TERM\n"
        "VLLM_CI_TEST_SELECTION_PYTEST_INVOCATION=$(printf '%03d' \"$count\")\n"
        "export VLLM_CI_TEST_SELECTION_PYTEST_INVOCATION\n"
        f"exec {target}\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    environment["PATH"] = os.pathsep.join(
        value for value in (str(directory), environment.get("PATH")) if value
    )


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
            # Exercise the same direct ``pytest`` launcher used by production
            # commands after replacing PYTHONPATH, which catches command-local
            # assignments such as ``PYTHONPATH=/vllm-workspace pytest ...``.
            pytest_environment["PYTHONPATH"] = str(repo_root.resolve())
            pytest_launcher = shutil.which(
                "pytest", path=pytest_environment.get("PATH")
            )
            preflight_command = (
                [pytest_launcher]
                if pytest_launcher
                else [sys.executable, "-m", "pytest"]
            )
            pytest_result = subprocess.run(
                [
                    *preflight_command,
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


def _pytest_invocations_started(node_file: Path) -> int:
    counter = Path(str(node_file) + ".invocations")
    if not counter.is_file():
        return 0
    try:
        value = int(counter.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as error:
        raise RuntimeError("pytest invocation counter is invalid") from error
    if value < 0:
        raise RuntimeError("pytest invocation counter is negative")
    return value


def _merge_node_documents(
    output_dir: Path, node_file: Path
) -> tuple[dict[str, Any], set[str]]:
    documents = []
    invocation_ids: set[str] = set()
    for path in sorted(output_dir.glob("pytest-nodes*.json")):
        if path == node_file:
            invocation_ids.add("direct")
        else:
            tail = path.name[len(node_file.stem) + 1 : -len(node_file.suffix)]
            invocation_ids.add(tail.split(".", 1)[0])
        documents.append(json.loads(path.read_text(encoding="utf-8")))
    if not documents:
        return {"collected": [], "exit_status": None, "outcomes": {}}, set()

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
    return merged, invocation_ids


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

    with tempfile.TemporaryDirectory(
        prefix=".pytest-launcher-", dir=output_dir
    ) as launcher_directory:
        environment = _command_environment(
            coverage_file=coverage_file,
            node_file=node_file,
            repo_root=args.repo_root,
            auto_load_pytest=bool(args.command_base64),
        )
        command_cwd = (args.command_cwd or args.repo_root).resolve()
        if args.command_base64:
            command_text = _decode_command(args.command_base64)
            launcher_path = Path(launcher_directory)
            _install_pytest_launcher(launcher_path, environment)
            command = [
                "bash",
                "-lc",
                f'export PATH={shlex.quote(str(launcher_path))}:"$PATH"; '
                + command_text,
            ]
        else:
            command_text = None
            command = pytest_command(tests)
        preflight_status = validate_import_environment(
            command_cwd=command_cwd,
            environment=environment,
            output_path=output_dir / "import-environment.json",
            repo_root=args.repo_root,
        )
        checkout_state: dict[str, Any] = {}
        if args.subprocess_coverage:
            # The evidence claims the checkout's repository SHA; prove the
            # tree is actually that commit and pristine before and after.
            checkout_state = _verify_pristine_checkout(command_cwd, repository_sha)
            if not checkout_state["ok"] and preflight_status == 0:
                preflight_status = 70
        if preflight_status == 0 and args.subprocess_coverage:
            # Installed after the preflight so preflight output cannot land in
            # the real shard. The job env (COVERAGE_PROCESS_START) is only set
            # for the command itself, never for the preflight.
            from . import subprocess_coverage

            environment["COVERAGE_PROCESS_START"] = str(
                subprocess_coverage.write_rc(output_dir)
            )
            environment["VLLM_CI_TEST_SELECTION_HOOK_DIR"] = str(output_dir)
            subprocess_coverage.enable(output_dir)
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
            command_finished_at = datetime.now(UTC).isoformat()
            if args.subprocess_coverage:
                after = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=command_cwd,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if after.returncode == 0:
                    checkout_state["dirty_after"] = after.stdout.splitlines()
                else:
                    checkout_state["dirty_after"] = None
                    checkout_state["status_after_error"] = after.stderr.strip()
            invocations_at_finish = (
                _pytest_invocations_started(node_file) if args.command_base64 else 1
            )
            _atomic_json(
                command_status_file,
                {
                    "command_executed": True,
                    "created_at": command_finished_at,
                    "exit_code": command_exit_code,
                    "phase": "finished",
                    "pytest_invocations_started": invocations_at_finish,
                },
            )
        else:
            result = subprocess.CompletedProcess(command, preflight_status)
            command_exit_code = preflight_status
    command_executed = preflight_status == 0

    subprocess_hook_state: dict[str, Any] | None = None
    combine_ok = True
    if args.subprocess_coverage:
        from . import subprocess_coverage

        state_path = output_dir / subprocess_coverage.HOOK_STATE_NAME
        if state_path.is_file():
            subprocess_hook_state = json.loads(state_path.read_text("utf-8"))
        # Merge per-process parallel data files (serve workers) into the
        # shard data file the pytest client appends to. With no parallel
        # files there is nothing to combine (and combine would exit 1).
        if list(output_dir.glob(".coverage.*")):
            combine = subprocess.run(
                [sys.executable, "-m", "coverage", "combine"],
                cwd=output_dir,
                env={**os.environ, "COVERAGE_FILE": str(coverage_file)},
                capture_output=True,
                text=True,
                check=False,
            )
            combine_ok = combine.returncode == 0
            if not combine_ok:
                print(
                    "test-selection: coverage combine failed: "
                    + combine.stderr.strip(),
                    file=sys.stderr,
                )

    rows = (
        coverage_rows(
            coverage_file,
            args.repo_root,
            repository_sha=repository_sha,
            job_key=args.represented_job_key,
            subprocess_contexts=args.subprocess_coverage,
        )
        if coverage_file.exists()
        else []
    )
    _atomic_jsonl(trace_file, rows)
    node_document, exported_invocations = _merge_node_documents(output_dir, node_file)
    if args.subprocess_coverage and any(
        row["test_id"] == f"job::{args.represented_job_key}" for row in rows
    ):
        node_document["collected"] = sorted(
            set(node_document["collected"])
            | {f"job::{args.represented_job_key}"}
        )
        _atomic_json(node_file, node_document)
    if args.command_base64:
        started_invocations = _pytest_invocations_started(node_file)
        expected_invocations = {f"{index:03d}" for index in range(started_invocations)}
    else:
        started_invocations = 1
        expected_invocations = {"direct"}
    node_exports_complete = exported_invocations == expected_invocations
    if command_executed:
        _atomic_json(
            command_status_file,
            {
                "command_executed": True,
                "created_at": command_finished_at,
                "exit_code": command_exit_code,
                "phase": "finished",
                "pytest_invocations_exported": len(exported_invocations),
                "pytest_invocations_started": started_invocations,
                "pytest_node_exports_complete": node_exports_complete,
            },
        )
    subprocess_reason = None
    serve_markers: list[dict[str, Any]] = []
    if args.subprocess_coverage:
        serve_markers = _serve_markers(output_dir)
        subprocess_reason = _subprocess_health(
            hook_state=subprocess_hook_state,
            combine_ok=combine_ok,
            checkout_state=checkout_state,
            serve_markers=serve_markers,
            has_serve_rows=any(
                row["test_id"] == f"job::{args.represented_job_key}" for row in rows
            ),
        )
    subprocess_ok = subprocess_reason is None
    healthy = (
        command_exit_code == 0
        and bool(node_document["collected"])
        and coverage_file.exists()
        and node_exports_complete
        and subprocess_ok
    )
    failure_reason = None
    if preflight_status != 0:
        failure_reason = (
            "checkout_" + str(checkout_state.get("reason"))
            if checkout_state and not checkout_state.get("ok")
            else "collector_import_failed"
        )
    elif not node_exports_complete:
        failure_reason = "pytest_node_export_incomplete"
    elif subprocess_reason:
        failure_reason = subprocess_reason
    elif not healthy:
        failure_reason = "collector_unhealthy"
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
            "failure_reason": failure_reason,
            "healthy": healthy,
            "image_tag": os.environ.get("IMAGE_TAG"),
            "import_preflight_exit_code": preflight_status,
            "job_key": args.job_key,
            "node_ids": node_document["collected"],
            "pytest_exit_code": command_exit_code,
            "pytest_invocations_exported": len(exported_invocations),
            "pytest_invocations_started": started_invocations,
            "pytest_node_exports_complete": node_exports_complete,
            "python_trace": trace_file.name,
            "python_trace_rows": len(rows),
            "python_trace_sha256": _sha256(trace_file),
            "retry_count": int(os.environ.get("BUILDKITE_RETRY_COUNT", "0")),
            "repository_sha": repository_sha,
            "represented_job_key": args.represented_job_key,
            "subprocess_hook": subprocess_hook_state,
            "subprocess_serve_markers": serve_markers,
            "checkout_state": checkout_state or None,
        },
    )
    coverage_file.unlink(missing_ok=True)
    return command_exit_code


if __name__ == "__main__":
    raise SystemExit(main())
