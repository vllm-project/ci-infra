"""Trigger vLLM nightly perf/eval runs and report rolling regressions."""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

BUILDKITE_API = "https://api.buildkite.com/v2/organizations/vllm/pipelines"
DASHBOARD_API = "https://ci.vllm.ai/api"
RELEASE_IMAGE_PREFIX = "public.ecr.aws/q9t5s3a7/vllm-release-repo:"
RELEASE_IMAGE_JOB_NAME = "Build release image - x86_64 - CUDA 13.0"
RELEASE_BUILD_LOOKBACK_PAGES = 10
TERMINAL_BUILD_STATES = {"passed", "failed"}
REGRESSION_SIGMA = 2.0
REGRESSION_RELATIVE = 0.01
HTTP_ATTEMPTS = 3
HTTP_TIMEOUT = 90
LATENCY_METRICS = {"p99_ttft": ("p99 TTFT", 1000.0)}
HistoryRows = tuple[list[dict[str, Any]], list[dict[str, Any]]]


@dataclass(frozen=True)
class Observation:
    """One nightly value for a metric series."""

    commit: str
    timestamp: datetime
    value: float


@dataclass(frozen=True)
class Statistic:
    """Rolling comparison for one current metric value."""

    peak: float
    average: float
    sigma: float
    delta_average: float
    delta_peak: float
    status: str
    baseline_count: int


@dataclass(frozen=True)
class ReportRow:
    """A formatted perf or eval row."""

    area: str
    model: str
    dimension: str
    metric: str
    current: float
    statistic: Statistic


def _request(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    expect_json: bool = True,
) -> Any:
    headers = {"User-Agent": "vllm-perf-eval-automation/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"

    for attempt in range(1, HTTP_ATTEMPTS + 1):
        try:
            response = httpx.request(
                method,
                url,
                headers=headers,
                json=payload,
                timeout=HTTP_TIMEOUT,
                follow_redirects=True,
            )
            body = response.text
            if response.is_success:
                return response.json() if expect_json else body
            retryable = response.status_code == 429 or response.status_code >= 500
            if retryable and attempt < HTTP_ATTEMPTS:
                retry_after = response.headers.get("Retry-After")
                delay = (
                    int(retry_after)
                    if retry_after and retry_after.isdigit()
                    else 5 * attempt
                )
                time.sleep(delay)
                continue
            raise RuntimeError(f"HTTP {response.status_code} from {url}: {body[:500]}")
        except httpx.RequestError as error:
            if attempt < HTTP_ATTEMPTS:
                time.sleep(5 * attempt)
                continue
            raise RuntimeError(f"Request failed for {url}: {error}") from error

    raise AssertionError("HTTP retry loop exhausted")


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip().replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _commit_from_image(image: str | None) -> str | None:
    if not image:
        return None
    release_match = re.search(r"vllm-release-repo:([0-9a-f]{40})", image, re.IGNORECASE)
    if release_match:
        return release_match.group(1).lower()
    nightly_match = re.search(r"nightly-([0-9a-f]{7,40})", image, re.IGNORECASE)
    if nightly_match:
        return nightly_match.group(1).lower()
    return None


def _deduplicate_observations(values: list[Observation]) -> list[Observation]:
    by_commit: dict[str, Observation] = {}
    for value in values:
        previous = by_commit.get(value.commit)
        if previous is None or value.timestamp > previous.timestamp:
            by_commit[value.commit] = value
    return sorted(by_commit.values(), key=lambda item: item.timestamp)


def calculate_statistic(
    observations: list[Observation],
    *,
    current: Observation,
    higher_is_better: bool,
) -> Statistic | None:
    """Calculate a 7-day baseline and 30-day peak, excluding current from baseline."""
    values = _deduplicate_observations([*observations, current])
    seven_days_ago = current.timestamp - timedelta(days=7)
    thirty_days_ago = current.timestamp - timedelta(days=30)
    baseline = [
        item.value
        for item in values
        if seven_days_ago <= item.timestamp < current.timestamp
    ]
    peak_window = [
        item.value
        for item in values
        if thirty_days_ago <= item.timestamp <= current.timestamp
    ]
    if not baseline or not peak_window:
        return None

    average = statistics.fmean(baseline)
    sigma = statistics.pstdev(baseline) if len(baseline) > 1 else 0.0
    peak = max(peak_window) if higher_is_better else min(peak_window)
    delta_average = current.value / average - 1 if average else 0.0
    delta_peak = current.value / peak - 1 if peak else 0.0
    difference = current.value - average
    worse = difference < 0 if higher_is_better else difference > 0
    better = difference > 0 if higher_is_better else difference < 0
    significant = abs(difference) >= REGRESSION_SIGMA * sigma
    material = abs(delta_average) >= REGRESSION_RELATIVE

    if worse and significant and material:
        status = "regression"
    elif better and significant and material:
        status = "improvement"
    else:
        status = "steady"

    return Statistic(
        peak=peak,
        average=average,
        sigma=sigma,
        delta_average=delta_average,
        delta_peak=delta_peak,
        status=status,
        baseline_count=len(baseline),
    )


def _series_parts(delta: dict[str, Any]) -> list[str]:
    return str(delta["key"]).split("|")


def _perf_observations(
    delta: dict[str, Any], rows: list[dict[str, Any]]
) -> list[Observation]:
    parts = _series_parts(delta)
    if len(parts) != 8:
        return []
    model, device, tp, conc, isl, osl, precision, metric = parts
    observations: list[Observation] = []
    for row in rows:
        commit = _commit_from_image(row.get("image"))
        raw_value = row.get(metric)
        if not commit or raw_value is None:
            continue
        if (
            str(row.get("model")) != model
            or str(row.get("device")) != device
            or str(row.get("tp")) != tp
            or str(row.get("conc")) != conc
            or str(row.get("isl")) != isl
            or str(row.get("osl")) != osl
            or str(row.get("precision")) != precision
        ):
            continue
        try:
            observations.append(
                Observation(
                    commit, _parse_timestamp(str(row["date"])), float(raw_value)
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return observations


def _eval_observations(
    delta: dict[str, Any], rows: list[dict[str, Any]]
) -> list[Observation]:
    parts = _series_parts(delta)
    if len(parts) != 5:
        return []
    model, task, n_shot, metric_name, metric_filter = parts
    observations: list[Observation] = []
    for row in rows:
        commit = row.get("vllm_commit") or _commit_from_image(row.get("image"))
        if not commit:
            continue
        if (
            str(row.get("model")) != model
            or str(row.get("task")) != task
            or str(row.get("n_shot")) != n_shot
        ):
            continue
        metric = next(
            (
                item
                for item in row.get("metrics", [])
                if item.get("name") == metric_name
                and item.get("filter") == metric_filter
            ),
            None,
        )
        if metric is None:
            continue
        try:
            observations.append(
                Observation(
                    str(commit).lower(),
                    _parse_timestamp(str(row["run_date"])),
                    float(metric["value"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return observations


def _fetch_model_history(
    model: str,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    encoded = urllib.parse.quote(model, safe="")
    perf = _request(f"{DASHBOARD_API}/perf?model={encoded}&device=h200")["rows"]
    evaluation = _request(f"{DASHBOARD_API}/eval?model={encoded}")["rows"]
    return model, perf, evaluation


def _load_histories(models: set[str]) -> dict[str, HistoryRows]:
    histories: dict[str, HistoryRows] = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(_fetch_model_history, model) for model in sorted(models)
        ]
        for future in as_completed(futures):
            model, perf, evaluation = future.result()
            histories[model] = (perf, evaluation)
    return histories


def _load_full_perf_deltas(current: dict[str, Any]) -> list[dict[str, Any]]:
    """Load complete H200 deltas; the nightly endpoint contains an 8-row UI preview."""
    source_image = str(current.get("sourceImage", ""))
    previous_image = str(current.get("deltaVsPrev", {}).get("prevSourceImage", ""))
    if not source_image or not previous_image:
        raise RuntimeError("Nightly data is missing perf comparison image references")

    query = urllib.parse.urlencode(
        {
            "baseline": previous_image,
            "candidate": source_image,
            "device": "h200",
        }
    )
    comparison = _request(f"{DASHBOARD_API}/compare?{query}")
    deltas = comparison.get("perf", {}).get("deltas")
    if not isinstance(deltas, list):
        raise RuntimeError("Dashboard comparison is missing full perf deltas")
    return deltas


def _current_observation(delta: dict[str, Any], commit: str) -> Observation:
    return Observation(
        commit=commit,
        timestamp=_parse_timestamp(str(delta["candidateRun"])),
        value=float(delta["candidateValue"]),
    )


def _scale_statistic(statistic: Statistic, factor: float) -> Statistic:
    return Statistic(
        peak=statistic.peak * factor,
        average=statistic.average * factor,
        sigma=statistic.sigma * factor,
        delta_average=statistic.delta_average,
        delta_peak=statistic.delta_peak,
        status=statistic.status,
        baseline_count=statistic.baseline_count,
    )


def _make_rows(
    current: dict[str, Any],
    histories: dict[str, HistoryRows],
    *,
    perf_deltas: list[dict[str, Any]] | None = None,
) -> tuple[list[ReportRow], list[ReportRow], list[ReportRow]]:
    commit = str(current["commit"]).lower()
    deltas = current["deltaVsPrev"]
    all_perf_deltas = (
        perf_deltas if perf_deltas is not None else deltas.get("perfDeltas", [])
    )
    throughput_deltas = [
        item
        for item in all_perf_deltas
        if item.get("metric") == "tput_per_gpu" and "|h200|" in str(item.get("key"))
    ]
    latency_deltas = [
        item
        for item in all_perf_deltas
        if item.get("metric") in LATENCY_METRICS and "|h200|" in str(item.get("key"))
    ]
    eval_deltas = list(deltas.get("evalDeltas", []))
    throughput_rows: list[ReportRow] = []
    latency_rows: list[ReportRow] = []
    eval_rows: list[ReportRow] = []

    for delta in throughput_deltas:
        model = str(delta["model"])
        raw_perf = histories.get(model, ([], []))[0]
        current_value = _current_observation(delta, commit)
        statistic = calculate_statistic(
            _perf_observations(delta, raw_perf),
            current=current_value,
            higher_is_better=bool(delta.get("higherIsBetter", True)),
        )
        if statistic is None:
            continue
        dimension_parts = str(delta["dimension"]).split(" - ")
        config = " · ".join(dimension_parts[:2]).upper()
        throughput_rows.append(
            ReportRow(
                "throughput",
                model,
                config,
                "token/s/gpu",
                current_value.value,
                statistic,
            )
        )

    for delta in latency_deltas:
        model = str(delta["model"])
        raw_perf = histories.get(model, ([], []))[0]
        current_value = _current_observation(delta, commit)
        statistic = calculate_statistic(
            _perf_observations(delta, raw_perf),
            current=current_value,
            higher_is_better=bool(delta.get("higherIsBetter", False)),
        )
        if statistic is None:
            continue
        metric_label, scale = LATENCY_METRICS[str(delta["metric"])]
        dimension_parts = str(delta["dimension"]).split(" - ")
        config = " · ".join(dimension_parts[:2]).upper()
        latency_rows.append(
            ReportRow(
                "latency",
                model,
                config,
                metric_label,
                current_value.value * scale,
                _scale_statistic(statistic, scale),
            )
        )

    for delta in eval_deltas:
        model = str(delta["model"])
        raw_eval = histories.get(model, ([], []))[1]
        current_value = _current_observation(delta, commit)
        statistic = calculate_statistic(
            _eval_observations(delta, raw_eval),
            current=current_value,
            higher_is_better=bool(delta.get("higherIsBetter", True)),
        )
        if statistic is None:
            continue
        dimension_parts = str(delta["dimension"]).split(" - ")
        task = dimension_parts[0]
        metric = str(delta.get("metricLabel", delta.get("metric", "accuracy")))
        eval_rows.append(
            ReportRow("eval", model, task, metric, current_value.value, statistic)
        )

    status_order = {"regression": 0, "improvement": 1, "steady": 2}
    throughput_rows.sort(
        key=lambda row: (status_order[row.statistic.status], row.model)
    )
    latency_rows.sort(
        key=lambda row: (status_order[row.statistic.status], row.model, row.metric)
    )
    eval_rows.sort(
        key=lambda row: (status_order[row.statistic.status], row.model, row.metric)
    )
    return throughput_rows, latency_rows, eval_rows


def _short_model(model: str, width: int) -> str:
    label = model.rsplit("/", 1)[-1]
    if len(label) <= width:
        return label
    return f"{label[: width - 1]}…"


def _icon(status: str) -> str:
    return "🔴" if status == "regression" else "🟢"


def _format_number(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 100:
        return f"{value:,.1f}"
    return f"{value:,.2f}"


def _format_percent(value: float) -> str:
    return f"{value:+.1%}"


def _perf_table(rows: list[ReportRow]) -> list[str]:
    lines = [
        "●  Model                        Config     Best    7d avg ±σ       "
        "Current  Δ avg   Δ best",
        "-- ---------------------------- ---------- ------- --------------- "
        "-------- ------- --------",
    ]
    for row in rows:
        stat = row.statistic
        average = f"{_format_number(stat.average)} ±{_format_number(stat.sigma)}"
        lines.append(
            f"{_icon(stat.status)} {_short_model(row.model, 28):<28} "
            f"{row.dimension:<10} {_format_number(stat.peak):>7} "
            f"{average:>15} {_format_number(row.current):>8} "
            f"{_format_percent(stat.delta_average):>7} "
            f"{_format_percent(stat.delta_peak):>8}"
        )
    return lines


def _latency_table(rows: list[ReportRow]) -> list[str]:
    lines = [
        "●  Model                        Config       Best    7d avg ±σ       "
        "Current  Δ avg   Δ best",
        "-- ---------------------------- ---------- ------- --------------- "
        "-------- ------- --------",
    ]
    for row in rows:
        stat = row.statistic
        average = f"{_format_number(stat.average)} ±{_format_number(stat.sigma)}"
        lines.append(
            f"{_icon(stat.status)} {_short_model(row.model, 28):<28} "
            f"{row.dimension:<10} {_format_number(stat.peak):>7} "
            f"{average:>15} {_format_number(row.current):>8} "
            f"{_format_percent(stat.delta_average):>7} "
            f"{_format_percent(stat.delta_peak):>8}"
        )
    return lines


def _eval_table(rows: list[ReportRow]) -> list[str]:
    lines = [
        "●  Model                    Task · Metric                    Best  "
        "7d avg ±σ     Now  Δ avg  Δ best",
        "-- ------------------------ -------------------------------- ------- "
        "------------- ------- ------ -------",
    ]
    for row in rows:
        stat = row.statistic
        metric = row.metric.replace("exact_match", "exact").replace("-extract", "")
        dimension = f"{row.dimension} · {metric}"
        average = f"{stat.average:.2%} ±{stat.sigma:.2%}"
        lines.append(
            f"{_icon(stat.status)} {_short_model(row.model, 24):<24} "
            f"{dimension[:32]:<32} {stat.peak:>6.2%} {average:>13} "
            f"{row.current:>6.2%} {_format_percent(stat.delta_average):>6} "
            f"{_format_percent(stat.delta_peak):>7}"
        )
    return lines


def _table_blocks(lines: list[str], limit: int = 2800) -> list[dict[str, Any]]:
    header = lines[:2]
    chunks: list[str] = []
    current = list(header)
    for line in lines[2:]:
        candidate = "\n".join([*current, line])
        if len(candidate) > limit and len(current) > len(header):
            chunks.append("\n".join(current))
            current = [*header, line]
        else:
            current.append(line)
    chunks.append("\n".join(current))
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"```{chunk}```"}}
        for chunk in chunks
    ]


def _counts(rows: list[ReportRow]) -> dict[str, int]:
    counts = {"regression": 0, "improvement": 0, "steady": 0}
    for row in rows:
        counts[row.statistic.status] += 1
    return counts


def build_slack_payload(
    current: dict[str, Any],
    previous: dict[str, Any],
    throughput_rows: list[ReportRow],
    latency_rows: list[ReportRow],
    eval_rows: list[ReportRow],
) -> dict[str, Any]:
    """Build a webhook-compatible Block Kit rendering of the Canvas template."""
    run_time = _parse_timestamp(str(current["date"])).astimezone(
        ZoneInfo("America/Los_Angeles")
    )
    display_date = run_time.strftime("%b %-d, %Y")
    iso_date = run_time.strftime("%Y-%m-%d")
    commit = str(current["commit"])
    previous_commit = str(previous["commit"])
    build = current["perfEval"]["build"]
    throughput_counts = _counts(throughput_rows)
    latency_counts = _counts(latency_rows)
    eval_counts = _counts(eval_rows)
    summary = (
        f"> *Throughput:* 🔴 {throughput_counts['regression']} · 🟢 "
        f"{throughput_counts['improvement']} improved · "
        f"{throughput_counts['steady']} steady · "
        f"*Latency:* 🔴 {latency_counts['regression']} · 🟢 "
        f"{latency_counts['improvement']} improved · {latency_counts['steady']} steady · "
        f"*Eval:* 🔴 {eval_counts['regression']} · 🟢 "
        f"{eval_counts['improvement']} improved · {eval_counts['steady']} steady"
    )
    commit_url = f"https://github.com/vllm-project/vllm/commit/{commit}"
    previous_url = f"https://github.com/vllm-project/vllm/commit/{previous_commit}"
    build_url = str(build["web_url"])
    commit_line = (
        f"*Commit* <{commit_url}|`{commit[:7]}`> vs previous nightly "
        f"<{previous_url}|`{previous_commit[:7]}`> · latest nightly {iso_date} · "
        f"<{build_url}|Build #{build['number']}>"
    )
    legend = (
        "🔴 = regression vs 7-day avg (≥2σ & ≥1%), 🟢 = otherwise. "
        "Δ vs avg and Δ vs best are relative %. Best over 30d; avg ±σ over "
        "the prior 7 days."
    )
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Nightly Perf / Eval — {display_date}",
                "emoji": True,
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": commit_line}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": legend}]},
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Throughput / GPU*\n_token/s/gpu · higher is better_",
            },
        },
    ]
    blocks.extend(_table_blocks(_perf_table(throughput_rows)))
    blocks.extend(
        [
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Latency — p99 TTFT*\n_ms · lower is better_",
                },
            },
        ]
    )
    blocks.extend(_table_blocks(_latency_table(latency_rows)))
    blocks.extend(
        [
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Eval accuracy*\n_% correct · higher is better · "
                        "±σ in percentage points_"
                    ),
                },
            },
        ]
    )
    blocks.extend(_table_blocks(_eval_table(eval_rows)))
    min_throughput = min(
        (row.statistic.baseline_count for row in throughput_rows), default=0
    )
    min_latency = min((row.statistic.baseline_count for row in latency_rows), default=0)
    min_eval = min((row.statistic.baseline_count for row in eval_rows), default=0)
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Source: <https://ci.vllm.ai/nightly|vLLM CI dashboard> · "
                        f"minimum 7d samples: throughput {min_throughput}, "
                        f"latency {min_latency}, eval {min_eval}"
                    ),
                }
            ],
        }
    )
    return {
        "text": f"Nightly Perf / Eval — {iso_date}: {summary.replace('>', '').strip()}",
        "blocks": blocks,
    }


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"reported_builds": []}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"reported_builds": []}


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(f"{json.dumps(value, indent=2, sort_keys=True)}\n")
    temporary.replace(path)


def _post_slack(webhook: str, payload: dict[str, Any]) -> None:
    response = _request(
        webhook,
        method="POST",
        payload=payload,
        expect_json=False,
    )
    if response.strip().lower() != "ok":
        raise RuntimeError(f"Slack webhook returned: {response[:500]}")


def _select_reportable_nightly(
    nightlies: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    current_index = next(
        (
            index
            for index, item in enumerate(nightlies[:-1])
            if item.get("perfEval", {}).get("build", {}).get("state")
            in TERMINAL_BUILD_STATES
            and (
                item.get("deltaVsPrev", {}).get("perfDeltas")
                or item.get("deltaVsPrev", {}).get("evalDeltas")
            )
        ),
        None,
    )
    if current_index is None:
        return None
    return nightlies[current_index], nightlies[current_index + 1]


def _record_reported_build(state_dir: Path, build_number: int) -> None:
    state_path = state_dir / "vllm_perf_eval_report_state.json"
    state = _load_state(state_path)
    reported = [build_number, *state.get("reported_builds", [])]
    state["reported_builds"] = list(dict.fromkeys(reported))[:30]
    state["last_reported_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(state_path, state)


def run_report(state_dir: Path, *, dry_run: bool) -> int:
    nightly = _request(f"{DASHBOARD_API}/nightly?limit=30")
    selected = _select_reportable_nightly(nightly.get("nightlies", []))
    if selected is None:
        print("SKIP: no completed nightly with reportable data")
        return 0

    current, previous = selected
    build_number = int(current["perfEval"]["build"]["number"])
    state_path = state_dir / "vllm_perf_eval_report_state.json"
    state = _load_state(state_path)
    if build_number in state.get("reported_builds", []):
        print(f"SKIP: perf-eval build #{build_number} was already reported")
        return 0

    perf_deltas = _load_full_perf_deltas(current)
    eval_deltas = current["deltaVsPrev"].get("evalDeltas", [])
    all_deltas = [*perf_deltas, *eval_deltas]
    models = {str(item["model"]) for item in all_deltas}
    histories = _load_histories(models)
    throughput_rows, latency_rows, eval_rows = _make_rows(
        current,
        histories,
        perf_deltas=perf_deltas,
    )
    if not throughput_rows and not latency_rows and not eval_rows:
        raise RuntimeError(f"No rolling statistics available for build #{build_number}")

    payload = build_slack_payload(
        current,
        previous,
        throughput_rows,
        latency_rows,
        eval_rows,
    )
    report_path = state_dir / "vllm_perf_eval_report_payload.json"
    details_path = state_dir / "vllm_perf_eval_report_rows.json"
    _write_json(report_path, payload)
    _write_json(
        details_path,
        {
            "build_number": build_number,
            "commit": current["commit"],
            "perf": [asdict(row) for row in throughput_rows],
            "latency": [asdict(row) for row in latency_rows],
            "eval": [asdict(row) for row in eval_rows],
        },
    )

    if dry_run:
        print(
            f"DRY RUN: generated build #{build_number} report with "
            f"{len(throughput_rows)} throughput, {len(latency_rows)} latency, "
            f"and {len(eval_rows)} eval rows"
        )
        return 0

    webhook = os.environ.get("SLACK_WEBHOOK_URL") or os.environ.get(
        "VLLM_CI_SLACK_URL", ""
    )
    if not webhook.startswith("https://hooks.slack.com/"):
        raise RuntimeError("SLACK_WEBHOOK_URL is missing or is not a Slack webhook")
    _post_slack(webhook, payload)
    _record_reported_build(state_dir, build_number)
    print(f"Posted perf-eval build #{build_number} report to Slack")
    return 0


def run_adopt(state_dir: Path, *, build_number: int) -> int:
    nightly = _request(f"{DASHBOARD_API}/nightly?limit=30")
    selected = _select_reportable_nightly(nightly.get("nightlies", []))
    if selected is None:
        raise RuntimeError("No completed nightly with reportable data")
    current, _ = selected
    latest_build_number = int(current["perfEval"]["build"]["number"])
    if build_number != latest_build_number:
        raise RuntimeError(
            f"Build #{build_number} is not the latest reportable build "
            f"(#{latest_build_number})"
        )
    _record_reported_build(state_dir, build_number)
    print(f"Adopted perf-eval build #{build_number} as already reported")
    return 0


def _buildkite_get(path: str, token: str) -> Any:
    return _request(f"{BUILDKITE_API}/{path}", token=token)


def _latest_release_with_cuda_image(token: str) -> dict[str, Any] | None:
    for page in range(1, RELEASE_BUILD_LOOKBACK_PAGES + 1):
        release_builds = _buildkite_get(
            f"release-v2/builds?branch=main&per_page=100&exclude_jobs=true&page={page}",
            token,
        )
        if not release_builds:
            break
        for build in release_builds:
            if (
                str(build.get("message", "")).strip().lower() != "nightly release"
                or build.get("source") != "schedule"
            ):
                continue
            details = _buildkite_get(f"release-v2/builds/{build['number']}", token)
            image_job = next(
                (
                    job
                    for job in details.get("jobs", [])
                    if job.get("name") == RELEASE_IMAGE_JOB_NAME
                ),
                None,
            )
            if image_job is not None and image_job.get("state") == "passed":
                return build
    return None


def run_trigger(state_dir: Path, *, dry_run: bool) -> int:
    token = os.environ.get("BUILDKITE_API_TOKEN", "")
    if not token:
        raise RuntimeError("BUILDKITE_API_TOKEN is required")
    nightly = _latest_release_with_cuda_image(token)
    if nightly is None:
        print(
            "SKIP: no scheduled Nightly release has a passed "
            f"{RELEASE_IMAGE_JOB_NAME} job"
        )
        return 0

    vllm_commit = str(nightly["commit"])
    perf_builds = _buildkite_get(
        "perf-eval/builds?branch=main&per_page=100&exclude_jobs=true",
        token,
    )
    existing = next(
        (
            build
            for build in perf_builds
            if str(build.get("env", {}).get("VLLM_COMMIT", "")) == vllm_commit
            and str(build.get("env", {}).get("NIGHTLY", "")) == "1"
        ),
        None,
    )
    if existing is not None:
        print(
            f"SKIP: commit {vllm_commit[:7]} already has perf-eval "
            f"build #{existing['number']} ({existing['state']})"
        )
        return 0

    local_date = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    payload = {
        "commit": "HEAD",
        "branch": "main",
        "message": f"Nightly run {local_date}: commit {vllm_commit}",
        "env": {
            "VLLM_COMMIT": vllm_commit,
            "VLLM_IMAGE": f"{RELEASE_IMAGE_PREFIX}{vllm_commit}-x86_64",
            "NIGHTLY": "1",
        },
    }
    _write_json(state_dir / "vllm_perf_eval_trigger_payload.json", payload)
    if dry_run:
        print(
            f"DRY RUN: would trigger perf-eval for release-v2 build "
            f"#{nightly['number']} ({vllm_commit[:7]})"
        )
        return 0

    created = _request(
        f"{BUILDKITE_API}/perf-eval/builds",
        method="POST",
        token=token,
        payload=payload,
    )
    _write_json(state_dir / "vllm_perf_eval_trigger_result.json", created)
    print(
        f"Triggered perf-eval build #{created['number']} for {vllm_commit[:7]}: "
        f"{created['web_url']}"
    )
    return 0


def _validate_payload_limits(payload: dict[str, Any]) -> None:
    blocks = payload.get("blocks", [])
    if len(blocks) > 50:
        raise ValueError(f"Slack payload has {len(blocks)} blocks; maximum is 50")
    for block in blocks:
        text = block.get("text", {}).get("text")
        if isinstance(text, str) and len(text) > 3000:
            raise ValueError("Slack section text exceeds 3000 characters")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("trigger", "report", "adopt"))
    parser.add_argument("--state-dir", type=Path, default=Path(".logs"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--build-number", type=int)
    args = parser.parse_args(argv)
    args.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        if args.mode == "trigger":
            return run_trigger(args.state_dir, dry_run=args.dry_run)
        if args.mode == "adopt":
            if args.build_number is None:
                parser.error("adopt requires --build-number")
            if args.dry_run:
                parser.error("adopt does not support --dry-run")
            return run_adopt(args.state_dir, build_number=args.build_number)
        result = run_report(args.state_dir, dry_run=args.dry_run)
        payload_path = args.state_dir / "vllm_perf_eval_report_payload.json"
        if payload_path.exists():
            _validate_payload_limits(json.loads(payload_path.read_text()))
        return result
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
