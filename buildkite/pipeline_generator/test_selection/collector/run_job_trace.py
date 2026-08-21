# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Run an enrolled Buildkite pytest job under generic trace collection."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import COLLECTOR_VERSION


def _atomic_json(path: Path, document: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _shell_exit_code(returncode: int) -> int:
    return 128 - returncode if returncode < 0 else returncode


def decode_commands(value: str) -> list[str]:
    try:
        document = json.loads(base64.b64decode(value, validate=True).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        message = "--commands-base64 must contain base64-encoded JSON"
        raise SystemExit(message) from error
    if not isinstance(document, list) or not document:
        raise SystemExit("trace command payload must be a non-empty JSON list")
    if not all(isinstance(command, str) and command.strip() for command in document):
        raise SystemExit("every trace command must be a non-empty string")
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--job-key", required=True)
    parser.add_argument("--represented-job-key", required=True)
    parser.add_argument("--commands-base64", required=True)
    parser.add_argument("--repo-root", type=Path, default=Path("/vllm-workspace"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture-gpu", action="store_true")
    mode.add_argument("--python-only", action="store_true")
    parser.add_argument(
        "--preserve-command-exit-code",
        action="store_true",
        help=(
            "Return the existing job command's status even when collection or "
            "export fails. If collection fails before starting the command, run "
            "that command once without instrumentation."
        ),
    )
    return parser


def _encoded_command(command: str) -> str:
    return base64.b64encode(command.encode("utf-8")).decode("ascii")


def _run_command(
    command: str,
    *,
    command_index: int,
    job_key: str,
    command_cwd: Path,
    output_dir: Path,
    repo_root: Path,
    represented_job_key: str,
    capture_gpu: bool,
) -> subprocess.CompletedProcess[Any]:
    shard_dir = output_dir / "commands" / f"{command_index:03d}"
    shard_dir.mkdir(parents=True, exist_ok=True)
    package = __package__ or "ci_test_selection"
    runner = [
        sys.executable,
        "-m",
        f"{package}.run_trace",
        "--output-dir",
        str(shard_dir),
        "--job-key",
        job_key,
        "--represented-job-key",
        represented_job_key,
        "--repo-root",
        str(repo_root),
        "--command-cwd",
        str(command_cwd),
        "--command-base64",
        _encoded_command(command),
    ]
    environment = dict(os.environ)
    environment["VLLM_CI_TEST_SELECTION_NVTX"] = "1" if capture_gpu else "0"
    if capture_gpu:
        wrapper = Path(__file__).with_name("run_traced.sh")
        runner = [
            "bash",
            str(wrapper),
            str(shard_dir),
            represented_job_key,
            *runner,
        ]
    return subprocess.run(runner, cwd=command_cwd, env=environment, check=False)


def _job_document(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _run_uninstrumented(command: str, cwd: Path) -> subprocess.CompletedProcess[Any]:
    print(
        "Trace collector did not start the command; running it uninstrumented.",
        file=sys.stderr,
    )
    return subprocess.run(
        ["bash", "-lc", command],
        cwd=cwd,
        env=dict(os.environ),
        check=False,
    )


def main() -> int:
    args = _parser().parse_args()
    commands = decode_commands(args.commands_base64)
    output_dir = args.output_dir.resolve()
    repo_root = args.repo_root.resolve()
    command_cwd = Path.cwd().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    command_results = []
    return_code = 0
    for index, command in enumerate(commands):
        collector_error = None
        try:
            result = _run_command(
                command,
                command_index=index,
                job_key=args.job_key,
                command_cwd=command_cwd,
                output_dir=output_dir,
                repo_root=repo_root,
                represented_job_key=args.represented_job_key,
                capture_gpu=args.capture_gpu,
            )
        except Exception as error:
            collector_error = f"{type(error).__name__}: {error}"
            result = subprocess.CompletedProcess([], 1)
        command_output = output_dir / "commands" / f"{index:03d}"
        shard_job = _job_document(command_output / "job.json")
        command_status = _job_document(command_output / "command-status.json")
        command_executed = bool(shard_job and shard_job.get("command_executed"))
        command_exit_code = (
            _shell_exit_code(int(shard_job["pytest_exit_code"]))
            if command_executed and isinstance(shard_job.get("pytest_exit_code"), int)
            else None
        )
        if (
            command_exit_code is None
            and command_status
            and command_status.get("command_executed") is True
            and command_status.get("phase") == "finished"
            and isinstance(command_status.get("exit_code"), int)
        ):
            command_executed = True
            command_exit_code = _shell_exit_code(int(command_status["exit_code"]))
        command_started = bool(
            command_status
            and command_status.get("command_executed") is True
            and command_status.get("phase") in ("started", "finished")
        )
        failure_reason = (
            str(shard_job["failure_reason"])
            if shard_job and shard_job.get("failure_reason")
            else "collector_runtime_error"
            if collector_error
            else None
        )
        fallback_uninstrumented = False
        if (
            args.preserve_command_exit_code
            and command_exit_code is None
            and not command_started
        ):
            fallback = _run_uninstrumented(command, command_cwd)
            command_exit_code = _shell_exit_code(fallback.returncode)
            fallback_uninstrumented = True
        collector_exit_code = _shell_exit_code(result.returncode)
        effective_exit_code = (
            command_exit_code
            if args.preserve_command_exit_code and command_exit_code is not None
            else collector_exit_code
        )
        command_results.append(
            {
                "command_index": index,
                "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
                "collector_error": collector_error,
                "collector_exit_code": collector_exit_code,
                "command_exit_code": command_exit_code,
                "failure_reason": failure_reason,
                "fallback_uninstrumented": fallback_uninstrumented,
                "healthy": bool(
                    collector_exit_code == 0
                    and shard_job
                    and shard_job.get("healthy") is True
                ),
            }
        )
        if effective_exit_code != 0:
            return_code = effective_exit_code
            break

    healthy = len(command_results) == len(commands) and all(
        result["command_exit_code"] == 0 and result["healthy"]
        for result in command_results
    )
    failure_reasons = sorted(
        {
            result["failure_reason"]
            for result in command_results
            if result["failure_reason"]
        }
    )
    failure_reason = None
    if not healthy:
        failure_reason = (
            failure_reasons[0] if len(failure_reasons) == 1 else "collector_unhealthy"
        )
    summary_path = output_dir / "trace-job.json"
    try:
        parallel_job = int(os.environ.get("BUILDKITE_PARALLEL_JOB", "0"))
        parallel_job_count = int(os.environ.get("BUILDKITE_PARALLEL_JOB_COUNT", "1"))
        _atomic_json(
            summary_path,
            {
                "capture_mode": "gpu" if args.capture_gpu else "python-only",
                "collector_sha256": os.environ.get(
                    "VLLM_CI_TEST_SELECTION_COLLECTOR_SHA256"
                ),
                "collector_version": COLLECTOR_VERSION,
                "command_count": len(commands),
                "command_results": command_results,
                "created_at": datetime.now(UTC).isoformat(),
                "failure_reason": failure_reason,
                "healthy": healthy,
                "job_key": args.job_key,
                "parallel_job": parallel_job,
                "parallel_job_count": parallel_job_count,
                "repository_sha": os.environ.get("BUILDKITE_COMMIT"),
                "represented_job_key": args.represented_job_key,
            },
        )
    except Exception as error:
        print(
            "test-selection collector job summary failed: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        healthy = False
    if not healthy and return_code == 0 and not args.preserve_command_exit_code:
        return_code = 1
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
