# modules/ci_monitoring/src/main.py

import os
import json
import datetime
import requests
import functions_framework
from google.cloud import bigquery

# Global clients
client = bigquery.Client()
TABLE_ID = os.environ.get("BQ_TABLE_ID")

# Every pipeline is polled in every org. A pipeline that does not exist in a
# given org just 404s and is skipped.
PIPELINE_SLUGS = json.loads(os.environ.get("PIPELINE_SLUGS", "[]"))

# One entry per Buildkite org to poll: {"org": ..., "token_env": ...}. A
# Buildkite API token is scoped to a single org, so each org names the env var
# holding its own token, injected from Secret Manager by Terraform.
ORGS = json.loads(os.environ.get("ORGS_JSON", "[]"))

@functions_framework.http
def handle_webhook(request):
    """
    Triggered by Cloud Scheduler to poll every configured Buildkite pipeline in
    every configured org.
    """
    # Define time window: look back 15 mins to ensure no gaps with 10-min cron
    now = datetime.datetime.now(datetime.timezone.utc)
    finished_from = (now - datetime.timedelta(minutes=15)).isoformat()

    # (row_id, row) pairs; the id is what BigQuery dedups on.
    pairs = []
    failures = []

    for entry in ORGS:
        org = entry["org"]
        token = os.environ.get(entry["token_env"])
        if not token:
            failures.append(f"{org}: {entry['token_env']} is unset")
            continue

        for pipeline in PIPELINE_SLUGS:
            try:
                pairs.extend(fetch_rows(org, token, pipeline, finished_from))
            except requests.RequestException as e:
                # Keep going: one org or pipeline being unreachable should not
                # stop the others from landing, and the 15-min lookback
                # re-covers this window on the next run.
                failures.append(f"{org}/{pipeline}: {e}")

    rows_to_insert = [row for _, row in pairs]

    if rows_to_insert:
        # Stream to BigQuery with deduplication
        row_ids = [row_id for row_id, _ in pairs]
        errors = client.insert_rows_json(TABLE_ID, rows_to_insert, row_ids=row_ids)
        if errors:
            print(f"BigQuery Errors: {errors}")
            return "Partial Success", 500

    if failures:
        print(f"Failed: {'; '.join(failures)}")
        return f"Processed {len(rows_to_insert)} items, {len(failures)} target(s) failed", 500

    targets = len(ORGS) * len(PIPELINE_SLUGS)
    return f"Processed {len(rows_to_insert)} items across {targets} org/pipeline pair(s)", 200

def fetch_rows(org, token, pipeline, finished_from):
    headers = {"Authorization": f"Bearer {token}"}

    url = f"https://api.buildkite.com/v2/organizations/{org}/pipelines/{pipeline}/builds"
    params = {
        "finished_from": finished_from,
        "state": "finished"
    }

    response = requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()

    # Deterministic row IDs for idempotency, since the 15-min lookback re-sends
    # builds the previous run already inserted. Keyed on the job UUID, not the
    # step name: a step with parallelism emits several jobs sharing one name,
    # and a name-keyed ID makes BigQuery dedup all but one of them away.
    rows = []
    for build in response.json():
        # 1. Capture E2E Summary
        rows.append((
            f"{build['id']}_E2E_SUMMARY",
            construct_bq_row(org, build, "E2E_SUMMARY", build),
        ))

        # 2. Capture Individual Steps
        for job in build.get("jobs", []):
            if job.get("type") == "script" and job.get("finished_at"):
                rows.append((
                    f"{build['id']}_{job['id']}",
                    construct_bq_row(org, build, job.get("name"), job),
                ))

    return rows

def construct_bq_row(org, build, step_name, timing_source):
    runnable_at = parse_ts(timing_source.get("runnable_at"))
    started_at = parse_ts(timing_source.get("started_at"))
    finished_at = parse_ts(timing_source.get("finished_at"))

    wait_sec = 0
    if runnable_at and started_at:
        wait_sec = (started_at - runnable_at).total_seconds()
    elif started_at:
        created_at = parse_ts(timing_source.get("created_at"))
        if created_at:
            wait_sec = (started_at - created_at).total_seconds()

    run_sec = 0
    if started_at and finished_at:
        run_sec = (finished_at - started_at).total_seconds()

    return {
        "build_id": build.get("id"),
        "org_slug": org,
        "commit_hash": build.get("commit"),
        "step_name": step_name,
        "pipeline_slug": build.get("pipeline", {}).get("slug"),
        "branch": build.get("branch"),
        "state": timing_source.get("state"),
        "wait_duration_sec": max(0, wait_sec),
        "run_duration_sec": max(0, run_sec),
        "created_at": parse_ts(timing_source.get("created_at")).isoformat() if timing_source.get("created_at") else None
    }

def parse_ts(ts_str):
    if not ts_str: return None
    return datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
