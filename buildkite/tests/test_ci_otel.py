# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

from __future__ import annotations

import binascii
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS_DIR = (
    Path(__file__).resolve().parents[1] / "pipeline_generator" / "otel_helpers"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import ci_otel  # noqa: E402
from ci_otel import (  # noqa: E402
    Span,
    encode_request,
    export_spans,
    load_spans,
    new_context,
    record_spans,
)


def _encoded(value: str) -> str:
    return binascii.b2a_base64(value.encode(), newline=False).decode()


def test_new_context_continues_w3c_traceparent(monkeypatch):
    monkeypatch.setenv(
        "TRACEPARENT",
        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    )

    trace_id, span_id, parent_span_id = new_context()

    assert trace_id == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert len(span_id) == 16
    assert parent_span_id == "00f067aa0ba902b7"


def test_otlp_payload_contains_span_and_build_identity(monkeypatch):
    monkeypatch.setenv("BUILDKITE_ORGANIZATION_SLUG", "vllm")
    monkeypatch.setenv("BUILDKITE_PIPELINE_SLUG", "ci")
    monkeypatch.setenv("BUILDKITE_BUILD_ID", "build-id")
    monkeypatch.setenv("BUILDKITE_BUILD_NUMBER", "42")
    monkeypatch.setenv("BUILDKITE_BRANCH", "main")
    monkeypatch.setenv("BUILDKITE_JOB_ID", "job-id")
    span = Span(
        trace_id="01" * 16,
        span_id="02" * 8,
        parent_span_id="03" * 8,
        name="ci.command",
        start_ns=100,
        end_ns=200,
        attributes={"ci.span.kind": "command"},
    )

    payload = encode_request([span])

    assert b"ci.command" in payload
    assert b"ci.span.kind" in payload
    assert b"buildkite.job.id" in payload
    assert b"job-id" in payload


def test_export_is_disabled_outside_buildkite(monkeypatch):
    monkeypatch.delenv("BUILDKITE", raising=False)

    assert export_spans([]) is False


def test_spans_are_spooled_without_requesting_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("VLLM_CI_OTEL_SPOOL_DIR", str(tmp_path))
    monkeypatch.setattr(
        ci_otel,
        "_oidc_token",
        lambda deadline: (_ for _ in ()).throw(AssertionError("unexpected upload")),
    )
    span = Span(
        trace_id="01" * 16,
        span_id="02" * 8,
        parent_span_id=None,
        name="ci.command",
        start_ns=100,
        end_ns=200,
        attributes={"ci.command.index": 1},
    )

    assert record_spans([span]) is True
    assert load_spans() == [span]


def test_export_mints_one_token_for_multiple_batches(monkeypatch):
    for name, value in {
        "BUILDKITE": "true",
        "BUILDKITE_ORGANIZATION_SLUG": "vllm",
        "BUILDKITE_PIPELINE_SLUG": "ci",
        "BUILDKITE_BUILD_ID": "build-id",
        "BUILDKITE_BUILD_NUMBER": "42",
        "BUILDKITE_JOB_ID": "job-id",
        "BUILDKITE_BRANCH": "main",
    }.items():
        monkeypatch.setenv(name, value)
    token_calls = []
    request_timeouts = []
    monkeypatch.setattr(ci_otel, "MAX_BATCH_SIZE", 1)
    monkeypatch.setattr(
        ci_otel,
        "_oidc_token",
        lambda deadline: token_calls.append(deadline) or "token",
    )

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def open_request(request, timeout):
        request_timeouts.append(timeout)
        return Response()

    monkeypatch.setattr(ci_otel.urllib.request, "urlopen", open_request)
    spans = [
        Span(
            trace_id="01" * 16,
            span_id=f"{index:016x}",
            parent_span_id=None,
            name="pytest.test",
            start_ns=100,
            end_ns=200,
            attributes={"test.nodeid": f"test_{index}"},
        )
        for index in (1, 2)
    ]

    assert export_spans(spans, timeout_seconds=0.5) is True
    assert len(token_calls) == 1
    assert len(request_timeouts) == 2
    assert all(0 < timeout <= 0.5 for timeout in request_timeouts)


def test_export_deadline_bounds_oidc_request(monkeypatch, tmp_path):
    for name, value in {
        "BUILDKITE": "true",
        "BUILDKITE_ORGANIZATION_SLUG": "vllm",
        "BUILDKITE_PIPELINE_SLUG": "ci",
        "BUILDKITE_BUILD_ID": "build-id",
        "BUILDKITE_BUILD_NUMBER": "42",
        "BUILDKITE_JOB_ID": "job-id",
        "BUILDKITE_BRANCH": "main",
    }.items():
        monkeypatch.setenv(name, value)
    agent = tmp_path / "buildkite-agent"
    agent.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    agent.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    span = Span(
        trace_id="01" * 16,
        span_id="02" * 8,
        parent_span_id=None,
        name="ci.command",
        start_ns=100,
        end_ns=200,
        attributes={},
    )

    started = time.monotonic()
    assert export_spans([span], timeout_seconds=0.1) is False

    assert time.monotonic() - started < 0.5


def test_shell_wrapper_preserves_command_state_and_quoting(tmp_path):
    script = SCRIPTS_DIR / "ci_otel.sh"
    first = "ci_otel_run 1 {} {}".format(
        _encoded("export VALUE=ready"),
        _encoded("export VALUE=ready"),
    )
    second_command = 'test "$VALUE" = ready'
    second = f"ci_otel_run 2 {_encoded('check VALUE')} {_encoded(second_command)}"
    result = subprocess.run(
        ["bash", "-c", f'source "{script}"; {first}; {second}'],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "VLLM_CI_OTEL_DIR": str(SCRIPTS_DIR),
            "VLLM_CI_OTEL_SPOOL_DIR": str(tmp_path),
        },
    )

    assert result.returncode == 0, result.stderr


def test_shell_wrapper_expands_runtime_environment_arguments(tmp_path):
    script = SCRIPTS_DIR / "ci_otel.sh"
    command_text = 'test "$REGISTRY" = registry.example.com && test "$COMMIT" = abc123'
    command = "ci_otel_run 1 {} {}".format(
        _encoded("check generated variables"),
        _encoded(command_text),
    )
    result = subprocess.run(
        ["bash", "-c", f'source "{script}"; {command}'],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "REGISTRY": "registry.example.com",
            "COMMIT": "abc123",
            "VLLM_CI_OTEL_DIR": str(SCRIPTS_DIR),
            "VLLM_CI_OTEL_SPOOL_DIR": str(tmp_path),
        },
    )

    assert result.returncode == 0, result.stderr


def test_shell_wrapper_preserves_failure_status(tmp_path):
    script = SCRIPTS_DIR / "ci_otel.sh"
    command = "ci_otel_run 1 {} {}".format(
        _encoded("false"),
        _encoded("false"),
    )
    result = subprocess.run(
        ["bash", "-c", f'source "{script}"; {command}'],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "VLLM_CI_OTEL_DIR": str(SCRIPTS_DIR),
            "VLLM_CI_OTEL_SPOOL_DIR": str(tmp_path),
        },
    )

    assert result.returncode == 1, result.stderr
