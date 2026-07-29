"""Tests for the portable fast CI failure alert."""

import datetime as dt
import sqlite3

from fast_ci_failure_alert import (
    alert_query,
    batched,
    build_message,
    mark_alerted,
    open_state,
    release_unsent,
    reserve_new_rows,
    run,
)


def job(job_id: str, **overrides):
    row = {
        "job_id": job_id,
        "job_name": "GPU <fast> `test`",
        "job_url": f"https://buildkite.com/vllm/ci/builds/1#{job_id}",
        "state": "failed",
        "soft_failed": "false",
        "duration_secs": "19",
        "finished_at": "2026-07-28T17:33:00Z",
        "build_url": "https://buildkite.com/vllm/ci/builds/1",
        "message": "A failure\nwith whitespace",
        "commit_sha": "abcdef0123456789",
        "branch": "main",
        "author": "tester",
        "pr_number": "123",
        "pipeline": "CI",
    }
    row.update(overrides)
    return row


def test_alert_query_filters_ci_failures_by_lookback_and_duration():
    query = alert_query(30, 30)

    assert "p.name = 'CI'" in query
    assert "INTERVAL 30 MINUTE" in query
    assert "BETWEEN 0 AND 30" in query
    assert "'failed', 'failing', 'broken', 'timed_out'" in query
    assert "j.type = 'script'" in query


def test_build_message_escapes_values_and_uses_configured_threshold():
    message = build_message(
        [job("job-1", soft_failed="true")],
        batch_number=1,
        batch_count=1,
        max_duration_seconds=45,
    )

    assert "failed in 45s or less" in message
    assert "GPU &lt;fast&gt; 'test'" in message
    assert "`19s`" in message
    assert "CI #1" in message
    assert "PR #123" in message
    assert "_soft fail_" in message
    assert "> A failure with whitespace" in message


def test_reservation_deduplicates_and_mark_alerted_persists(tmp_path):
    state_path = tmp_path / "state.sqlite3"
    connection = open_state(state_path)
    rows = [job("job-1")]

    assert reserve_new_rows(connection, rows) == rows
    assert reserve_new_rows(connection, rows) == []

    mark_alerted(connection, rows, "123.456")
    stored = connection.execute(
        "SELECT slack_message_ts FROM alerted_jobs WHERE job_id = 'job-1'"
    ).fetchone()
    connection.close()

    assert stored == ("123.456",)


def test_release_unsent_allows_retry(tmp_path):
    connection = open_state(tmp_path / "state.sqlite3")
    rows = [job("job-1")]

    assert reserve_new_rows(connection, rows) == rows
    release_unsent(connection, rows)
    assert reserve_new_rows(connection, rows) == rows
    connection.close()


def test_dry_run_reports_duplicates_without_reserving(monkeypatch, tmp_path):
    state_path = tmp_path / "state.sqlite3"
    connection = open_state(state_path)
    existing = job("existing")
    reserve_new_rows(connection, [existing])
    mark_alerted(connection, [existing], "123.456")
    connection.close()

    monkeypatch.setenv("LOOKBACK_MINUTES", "30")
    monkeypatch.setenv("MAX_DURATION_SECONDS", "30")
    monkeypatch.setattr(
        "fast_ci_failure_alert.query_databricks",
        lambda _sql: [existing, job("new")],
    )
    monkeypatch.setattr(
        "fast_ci_failure_alert.post_slack",
        lambda _message: (_ for _ in ()).throw(AssertionError("Slack called")),
    )

    result = run(dry_run=True, state_path=state_path)

    assert result["matched"] == 2
    assert result["duplicates"] == 1
    assert result["new"] == 1
    assert result["jobs"][0]["job_id"] == "new"
    connection = sqlite3.connect(state_path)
    assert connection.execute("SELECT count(*) FROM alerted_jobs").fetchone() == (1,)
    connection.close()


def test_live_run_batches_and_records_every_alert(monkeypatch, tmp_path):
    rows = [job(f"job-{index}") for index in range(10)]
    messages = []

    monkeypatch.setenv("LOOKBACK_MINUTES", "30")
    monkeypatch.setenv("MAX_DURATION_SECONDS", "30")
    monkeypatch.setattr(
        "fast_ci_failure_alert.query_databricks",
        lambda _sql: rows,
    )

    def fake_post(message):
        messages.append(message)
        return f"{len(messages)}.000"

    monkeypatch.setattr("fast_ci_failure_alert.post_slack", fake_post)
    state_path = tmp_path / "state.sqlite3"

    result = run(dry_run=False, state_path=state_path)

    assert result == {
        "ok": True,
        "matched": 10,
        "alerted": 10,
        "duplicates": 0,
        "batches": 2,
    }
    assert len(messages) == 2
    assert "batch 1/2" in messages[0]
    assert "batch 2/2" in messages[1]
    connection = sqlite3.connect(state_path)
    assert connection.execute(
        "SELECT count(*) FROM alerted_jobs WHERE slack_message_ts IS NOT NULL"
    ).fetchone() == (10,)
    connection.close()


def test_stale_reservation_is_reclaimed(tmp_path):
    connection = open_state(tmp_path / "state.sqlite3")
    stale = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=11)).isoformat()
    connection.execute(
        "INSERT INTO alerted_jobs(job_id, finished_at, reserved_at) "
        "VALUES ('job-1', '2026-07-28T00:00:00Z', ?)",
        (stale,),
    )
    connection.commit()

    rows = [job("job-1")]
    assert reserve_new_rows(connection, rows) == rows
    connection.close()


def test_batched_splits_rows_without_empty_tail():
    assert [len(batch) for batch in batched(list(range(16)), 8)] == [8, 8]
