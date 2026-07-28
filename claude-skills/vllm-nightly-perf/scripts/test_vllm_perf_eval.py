"""Tests for the deterministic vLLM nightly perf/eval automation."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from vllm_perf_eval import (
    Observation,
    ReportRow,
    Statistic,
    _validate_payload_limits,
    build_slack_payload,
    calculate_statistic,
    run_adopt,
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
