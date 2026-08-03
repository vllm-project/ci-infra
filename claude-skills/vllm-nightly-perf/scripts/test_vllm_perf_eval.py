"""Tests for the deterministic vLLM nightly perf/eval automation."""

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from vllm_perf_eval import (
    Observation,
    ReportRow,
    Statistic,
    _validate_payload_limits,
    build_slack_payload,
    calculate_statistic,
    run_adopt,
    run_report,
    run_trigger,
)


def test_calculate_statistic_flags_material_two_sigma_regression():
    current_time = datetime(2026, 7, 18, tzinfo=timezone.utc)
    history = [
        Observation(f"commit-{day}", current_time - timedelta(days=day), 100.0)
        for day in range(1, 7)
    ]

    result = calculate_statistic(
        history,
        current=Observation("current", current_time, 95.0),
        higher_is_better=True,
    )

    assert result is not None
    assert result.status == "regression"
    assert result.average == 100.0
    assert result.sigma == 0.0
    assert result.delta_average == pytest.approx(-0.05)
    assert result.baseline_count == 6


def test_calculate_statistic_excludes_current_from_baseline_and_old_peak():
    current_time = datetime(2026, 7, 18, tzinfo=timezone.utc)
    history = [
        Observation("recent", current_time - timedelta(days=1), 100.0),
        Observation("old", current_time - timedelta(days=31), 1000.0),
    ]

    result = calculate_statistic(
        history,
        current=Observation("current", current_time, 110.0),
        higher_is_better=True,
    )

    assert result is not None
    assert result.average == 100.0
    assert result.peak == 110.0
    assert result.status == "improvement"


def test_canvas_shaped_slack_payload_stays_within_block_limits():
    current = {
        "commit": "a" * 40,
        "date": "2026-07-18T06:00:00Z",
        "perfEval": {
            "build": {
                "number": "303",
                "web_url": "https://buildkite.com/vllm/perf-eval/builds/303",
            }
        },
    }
    previous = {"commit": "b" * 40}
    statistic = Statistic(105.0, 100.0, 1.0, 0.05, 0.0, "improvement", 6)
    perf_rows = [
        ReportRow(
            "perf", f"org/model-{index}", "H200 · TP8", "token/s/gpu", 105.0, statistic
        )
        for index in range(8)
    ]
    eval_rows = [
        ReportRow(
            "eval",
            f"org/model-{index}",
            "gsm8k",
            "exact_match (strict-match)",
            0.95,
            Statistic(0.95, 0.94, 0.005, 0.01, 0.0, "steady", 6),
        )
        for index in range(16)
    ]

    payload = build_slack_payload(current, previous, perf_rows, eval_rows)

    _validate_payload_limits(payload)
    rendered = "\n".join(
        block.get("text", {}).get("text", "") for block in payload["blocks"]
    )
    assert "Throughput / GPU" in rendered
    assert "Eval accuracy" in rendered
    assert "7d avg" in rendered
    assert "Δ peak" in rendered


def test_adopt_records_only_the_latest_reportable_build(monkeypatch, tmp_path):
    nightlies = [
        {
            "perfEval": {"build": {"number": 310, "state": "failed"}},
            "deltaVsPrev": {"perfDeltas": [{"model": "model"}]},
        },
        {"perfEval": {"build": {"number": 308, "state": "passed"}}},
    ]
    monkeypatch.setattr(
        "vllm_perf_eval._request",
        lambda _url: {"nightlies": nightlies},
    )

    assert run_adopt(tmp_path, build_number=310) == 0
    state = json.loads((tmp_path / "vllm_perf_eval_report_state.json").read_text())
    assert state["reported_builds"] == [310]

    with pytest.raises(RuntimeError, match="not the latest reportable build"):
        run_adopt(tmp_path, build_number=308)


def test_report_uses_full_comparison_when_nightly_perf_preview_omits_throughput(
    monkeypatch, tmp_path
):
    commit = "c" * 40
    previous_commit = "b" * 40
    model = "org/model"
    image = f"public.ecr.aws/vllm-release-repo:{commit}-x86_64"
    previous_image = f"public.ecr.aws/vllm-release-repo:{previous_commit}-x86_64"
    current = {
        "commit": commit,
        "date": "2026-08-03T06:00:00Z",
        "sourceImage": image,
        "perfEval": {
            "build": {
                "number": "361",
                "state": "failed",
                "web_url": "https://buildkite.com/vllm/perf-eval/builds/361",
            }
        },
        "deltaVsPrev": {
            "prevSourceImage": previous_image,
            "perfDeltas": [
                {
                    "model": model,
                    "metric": "p99_ttft",
                    "key": f"{model}|h200|8|128|8192|1024|fp8|p99_ttft",
                }
            ],
            "evalDeltas": [],
        },
    }
    previous = {
        "commit": previous_commit,
        "sourceImage": previous_image,
        "perfEval": {"build": {"number": "360", "state": "failed"}},
    }
    throughput_delta = {
        "model": model,
        "metric": "tput_per_gpu",
        "key": f"{model}|h200|8|128|8192|1024|fp8|tput_per_gpu",
        "dimension": "h200 - TP 8 - conc 128 - ISL 8192 - OSL 1024 - fp8",
        "candidateRun": "2026-08-03 09:34:32",
        "candidateValue": 105.0,
        "higherIsBetter": True,
    }

    def fake_request(url, **_kwargs):
        if url.endswith("/nightly?limit=30"):
            return {"nightlies": [current, previous]}
        if "/compare?" in url:
            query = parse_qs(urlparse(url).query)
            assert query == {
                "baseline": [previous_image],
                "candidate": [image],
                "device": ["h200"],
            }
            return {"perf": {"deltas": [throughput_delta]}}
        raise AssertionError(f"Unexpected dashboard URL: {url}")

    history = [
        {
            "model": model,
            "device": "h200",
            "tp": "8",
            "conc": "128",
            "isl": "8192",
            "osl": "1024",
            "precision": "fp8",
            "date": "2026-08-02 09:34:32",
            "image": previous_image,
            "tput_per_gpu": 100.0,
        },
        {
            "model": model,
            "device": "h200",
            "tp": "8",
            "conc": "128",
            "isl": "8192",
            "osl": "1024",
            "precision": "fp8",
            "date": "2026-08-03 09:34:32",
            "image": image,
            "tput_per_gpu": 105.0,
        },
    ]

    monkeypatch.setattr("vllm_perf_eval._request", fake_request)
    monkeypatch.setattr(
        "vllm_perf_eval._load_histories",
        lambda models: {model: (history, [])} if models == {model} else {},
    )

    assert run_report(tmp_path, dry_run=True) == 0

    rows = json.loads((tmp_path / "vllm_perf_eval_report_rows.json").read_text())
    assert rows["build_number"] == 361
    assert len(rows["perf"]) == 1
    assert rows["perf"][0]["model"] == model
    assert rows["perf"][0]["current"] == 105.0


def test_trigger_accepts_failed_release_when_cuda_image_job_passed(
    monkeypatch, tmp_path, capsys
):
    commit = "a" * 40

    def fake_buildkite_get(path, _token):
        if path.endswith("page=1"):
            return [
                {
                    "number": 400,
                    "state": "failed",
                    "source": "schedule",
                    "message": "Nightly release",
                    "commit": commit,
                }
            ]
        if path == "release-v2/builds/400":
            return {
                "jobs": [
                    {
                        "name": "Build release image - x86_64 - CUDA 13.0",
                        "state": "passed",
                    }
                ]
            }
        if path.startswith("perf-eval/builds?"):
            return []
        raise AssertionError(f"Unexpected Buildkite path: {path}")

    monkeypatch.setenv("BUILDKITE_API_TOKEN", "test-token")
    monkeypatch.setattr("vllm_perf_eval._buildkite_get", fake_buildkite_get)

    assert run_trigger(tmp_path, dry_run=True) == 0

    payload = json.loads((tmp_path / "vllm_perf_eval_trigger_payload.json").read_text())
    assert payload["env"]["VLLM_COMMIT"] == commit
    assert payload["env"]["VLLM_IMAGE"].endswith(f"{commit}-x86_64")
    assert "release-v2 build #400" in capsys.readouterr().out


def test_trigger_uses_older_nightly_when_newest_cuda_image_failed(
    monkeypatch, tmp_path
):
    newest_commit = "a" * 40
    eligible_commit = "b" * 40

    def fake_buildkite_get(path, _token):
        if path.endswith("page=1"):
            return [
                {
                    "number": 401,
                    "source": "schedule",
                    "message": "Nightly release",
                    "commit": newest_commit,
                },
                {
                    "number": 400,
                    "source": "schedule",
                    "message": "Nightly release",
                    "commit": eligible_commit,
                },
            ]
        if path.startswith("release-v2/builds/"):
            build_number = int(path.rsplit("/", 1)[1])
            return {
                "jobs": [
                    {
                        "name": "Build release image - x86_64 - CUDA 13.0",
                        "state": "failed" if build_number == 401 else "passed",
                    }
                ]
            }
        if path.startswith("perf-eval/builds?"):
            return []
        raise AssertionError(f"Unexpected Buildkite path: {path}")

    monkeypatch.setenv("BUILDKITE_API_TOKEN", "test-token")
    monkeypatch.setattr("vllm_perf_eval._buildkite_get", fake_buildkite_get)

    assert run_trigger(tmp_path, dry_run=True) == 0

    payload = json.loads((tmp_path / "vllm_perf_eval_trigger_payload.json").read_text())
    assert payload["env"]["VLLM_COMMIT"] == eligible_commit
