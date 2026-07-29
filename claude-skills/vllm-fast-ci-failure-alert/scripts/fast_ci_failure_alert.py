"""Alert Slack when vLLM CI jobs fail unusually quickly."""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

FAILURE_STATES = ("failed", "failing", "broken", "timed_out")
SLACK_BATCH_SIZE = 8
STATE_RETENTION_DAYS = 90
STALE_RESERVATION_MINUTES = 10


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def request_json(
    method: str,
    url: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "vllm-fast-ci-failure-alert/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        path = urllib.parse.urlsplit(url).path
        raise RuntimeError(
            f"{method} {path} failed with HTTP {exc.code}: {response_body[:1000]}"
        ) from exc


def query_databricks(sql: str) -> list[dict[str, Any]]:
    host = required_env("DATABRICKS_HOST").rstrip("/")
    token = required_env("DATABRICKS_TOKEN")
    warehouse_id = required_env("DATABRICKS_WAREHOUSE_ID")
    response = request_json(
        "POST",
        f"{host}/api/2.0/sql/statements",
        token=token,
        payload={
            "warehouse_id": warehouse_id,
            "statement": sql,
            "wait_timeout": "50s",
            "disposition": "INLINE",
            "format": "JSON_ARRAY",
        },
    )

    statement_id = response.get("statement_id")
    deadline = time.monotonic() + 110
    while response.get("status", {}).get("state") in {"PENDING", "RUNNING"}:
        if not statement_id or time.monotonic() >= deadline:
            raise RuntimeError("Databricks query did not complete within 110 seconds")
        time.sleep(2)
        response = request_json(
            "GET",
            f"{host}/api/2.0/sql/statements/{statement_id}",
            token=token,
        )

    status = response.get("status", {})
    if status.get("state") != "SUCCEEDED":
        error = status.get("error", {})
        detail = error.get("message", status.get("state", "unknown"))
        raise RuntimeError(f"Databricks query failed: {detail}")

    columns = [
        column["name"]
        for column in response.get("manifest", {}).get("schema", {}).get("columns", [])
    ]
    data_rows = response.get("result", {}).get("data_array", [])
    return [dict(zip(columns, row, strict=False)) for row in data_rows]


def alert_query(lookback_minutes: int, max_duration_seconds: int) -> str:
    states = ", ".join(f"'{state}'" for state in FAILURE_STATES)
    return f"""
      SELECT
        j.id AS job_id,
        j.name AS job_name,
        j.web_url AS job_url,
        j.state,
        j.soft_failed,
        TIMESTAMPDIFF(SECOND, j.started_at, j.finished_at) AS duration_secs,
        j.finished_at,
        b.web_url AS build_url,
        b.message,
        b.commit AS commit_sha,
        b.branch,
        b.github_author_username AS author,
        b.pr_number,
        p.name AS pipeline
      FROM vllm_data_warehouse.buildkite.build_job AS j
      INNER JOIN vllm_data_warehouse.buildkite.build AS b ON j.build_id = b.id
      INNER JOIN vllm_data_warehouse.buildkite.pipeline AS p ON b.pipeline_id = p.id
      WHERE j._fivetran_deleted = false
        AND b._fivetran_deleted = false
        AND j.type = 'script'
        AND j.name IS NOT NULL
        AND p.name = 'CI'
        AND j.state IN ({states})
        AND j.started_at IS NOT NULL
        AND j.finished_at IS NOT NULL
        AND j.finished_at >= current_timestamp() - INTERVAL {lookback_minutes} MINUTE
        AND TIMESTAMPDIFF(SECOND, j.started_at, j.finished_at)
          BETWEEN 0 AND {max_duration_seconds}
      ORDER BY j.finished_at ASC
    """


def slack_escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def slack_code(value: Any) -> str:
    escaped = slack_escape(value).replace("`", "'")
    return f"`{escaped}`"


def safe_https_url(value: Any) -> str | None:
    text = str(value or "")
    parsed = urllib.parse.urlsplit(text)
    return text if parsed.scheme == "https" and parsed.netloc else None


def slack_label(value: Any) -> str:
    return slack_escape(value).replace("|", "¦").replace("`", "'")


def slack_link(url: Any, label: Any) -> str:
    safe_url = safe_https_url(url)
    safe_label = slack_label(label)
    return f"<{safe_url}|{safe_label}>" if safe_url else safe_label


def one_line(value: Any, limit: int = 120) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def truthy(value: Any) -> bool:
    return str(value or "").lower() in {"1", "true"}


def build_number(build_url: Any) -> str:
    match = re.search(r"/builds/(\d+)", str(build_url or ""))
    return match.group(1) if match else "?"


def build_message(
    rows: list[dict[str, Any]],
    batch_number: int,
    batch_count: int,
    max_duration_seconds: int,
) -> str:
    suffix = f" — batch {batch_number}/{batch_count}" if batch_count > 1 else ""
    lines = [
        (
            f":rotating_light: *Fast CI job failure alert* — {len(rows)} "
            f"job{'s' if len(rows) != 1 else ''} failed in "
            f"{max_duration_seconds}s or less{suffix}"
        ),
        "",
    ]
    for row in rows:
        details = [
            slack_code(f"{row.get('duration_secs', '?')}s"),
            slack_link(
                row.get("build_url"),
                f"{row.get('pipeline') or 'CI'} #{build_number(row.get('build_url'))}",
            ),
            f"branch {slack_code(row.get('branch') or '?')}",
        ]
        if row.get("commit_sha"):
            details.append(f"commit {slack_code(str(row['commit_sha'])[:8])}")
        if row.get("pr_number"):
            details.append(f"PR #{slack_escape(row['pr_number'])}")
        if row.get("author"):
            details.append(f"by {slack_escape(row['author'])}")
        if truthy(row.get("soft_failed")):
            details.append("_soft fail_")
        job = slack_link(row.get("job_url"), row.get("job_name") or "Unknown job")
        lines.append(f":red_circle: {job} — {' · '.join(details)}")
        if row.get("message"):
            lines.append(f"> {slack_escape(one_line(row['message']))}")
    return "\n".join(lines)


def post_slack(message: str) -> str:
    token = required_env("SLACK_BOT_TOKEN")
    channel = required_env("SLACK_CHANNEL_ID")
    response = request_json(
        "POST",
        "https://slack.com/api/chat.postMessage",
        token=token,
        payload={"channel": channel, "text": message},
    )
    if not response.get("ok") or not response.get("ts"):
        error = response.get("error", "unknown error")
        raise RuntimeError(f"Slack post failed: {error}")
    return str(response["ts"])


def open_state(state_path: Path) -> sqlite3.Connection:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(state_path)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS alerted_jobs (
          job_id TEXT PRIMARY KEY,
          finished_at TEXT NOT NULL,
          reserved_at TEXT NOT NULL,
          alerted_at TEXT,
          slack_message_ts TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS alerted_jobs_finished_at_idx "
        "ON alerted_jobs(finished_at DESC)"
    )
    connection.commit()
    return connection


def reserve_new_rows(
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    now = dt.datetime.now(dt.UTC)
    stale_before = (now - dt.timedelta(minutes=STALE_RESERVATION_MINUTES)).isoformat()
    connection.execute(
        "DELETE FROM alerted_jobs WHERE slack_message_ts IS NULL AND reserved_at < ?",
        (stale_before,),
    )
    reserved: list[dict[str, Any]] = []
    for row in rows:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO alerted_jobs(job_id, finished_at, reserved_at)
            VALUES (?, ?, ?)
            """,
            (str(row["job_id"]), str(row["finished_at"]), now.isoformat()),
        )
        if cursor.rowcount:
            reserved.append(row)
    connection.commit()
    return reserved


def release_unsent(
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
) -> None:
    connection.executemany(
        "DELETE FROM alerted_jobs WHERE job_id = ? AND slack_message_ts IS NULL",
        [(str(row["job_id"]),) for row in rows],
    )
    connection.commit()


def mark_alerted(
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
    slack_message_ts: str,
) -> None:
    alerted_at = dt.datetime.now(dt.UTC).isoformat()
    connection.executemany(
        """
        UPDATE alerted_jobs
        SET alerted_at = ?, slack_message_ts = ?
        WHERE job_id = ?
        """,
        [(alerted_at, slack_message_ts, str(row["job_id"])) for row in rows],
    )
    connection.commit()


def batched(rows: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [rows[index : index + size] for index in range(0, len(rows), size)]


def run(*, dry_run: bool, state_path: Path) -> dict[str, Any]:
    lookback_minutes = env_int("LOOKBACK_MINUTES", 30, 15, 1440)
    max_duration_seconds = env_int("MAX_DURATION_SECONDS", 30, 1, 600)
    rows = query_databricks(alert_query(lookback_minutes, max_duration_seconds))
    connection = open_state(state_path)
    try:
        existing_ids = (
            {
                row[0]
                for row in connection.execute(
                    "SELECT job_id FROM alerted_jobs WHERE job_id IN "
                    f"({','.join('?' for _ in rows)})",
                    [str(row["job_id"]) for row in rows],
                )
            }
            if rows
            else set()
        )
        candidates = [row for row in rows if str(row["job_id"]) not in existing_ids]

        if dry_run:
            return {
                "mode": "dry-run",
                "matched": len(rows),
                "new": len(candidates),
                "duplicates": len(rows) - len(candidates),
                "jobs": [
                    {
                        "job_id": row["job_id"],
                        "job_name": row["job_name"],
                        "duration_secs": row["duration_secs"],
                        "finished_at": row["finished_at"],
                        "job_url": row["job_url"],
                    }
                    for row in candidates
                ],
            }

        reserved = reserve_new_rows(connection, rows)
        batches = batched(reserved, SLACK_BATCH_SIZE)
        alerted = 0
        for index, batch in enumerate(batches):
            try:
                message = build_message(
                    batch,
                    index + 1,
                    len(batches),
                    max_duration_seconds,
                )
                message_ts = post_slack(message)
                mark_alerted(connection, batch, message_ts)
                alerted += len(batch)
            except Exception:
                release_unsent(connection, reserved[alerted:])
                raise

        retention_cutoff = (
            dt.datetime.now(dt.UTC) - dt.timedelta(days=STATE_RETENTION_DAYS)
        ).isoformat()
        connection.execute(
            "DELETE FROM alerted_jobs WHERE alerted_at IS NOT NULL AND alerted_at < ?",
            (retention_cutoff,),
        )
        connection.commit()
        return {
            "ok": True,
            "matched": len(rows),
            "alerted": alerted,
            "duplicates": len(rows) - alerted,
            "batches": len(batches),
        }
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path(".logs/state.sqlite3"),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-slack", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.test_slack:
            message_ts = post_slack(
                ":test_tube: *TEST ONLY — fast CI failure automation*\n"
                "The portable 15-minute scheduler can post to this channel "
                "successfully."
            )
            result: dict[str, Any] = {
                "ok": True,
                "slack_message_ts": message_ts,
            }
        else:
            result = run(dry_run=args.dry_run, state_path=args.state_path)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
