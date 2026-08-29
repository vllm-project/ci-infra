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
PIPELINE_SLUG = os.environ.get("PIPELINE_SLUG")

# One entry per Buildkite org to poll: {"org": ..., "token_env": ...}. A
# Buildkite API token is scoped to a single org, so each org names the env var
# holding its own token, injected from Secret Manager by Terraform.
ORGS = json.loads(os.environ.get("ORGS_JSON", "[]"))

@functions_framework.http
def handle_webhook(request):
    """
    Triggered by Cloud Scheduler to poll a SPECIFIC Buildkite pipeline in every
    configured org.
    """
    # Define time window: look back 15 mins to ensure no gaps with 10-min cron
    now = datetime.datetime.now(datetime.timezone.utc)
    finished_from = (now - datetime.timedelta(minutes=15)).isoformat()

    rows_to_insert = []
    failures = []

    for entry in ORGS:
        org = entry["org"]
        token = os.environ.get(entry["token_env"])
        if not token:
            failures.append(f"{org}: {entry['token_env']} is unset")
            continue
        try:
            rows_to_insert.extend(fetch_org_rows(org, token, finished_from))
        except requests.RequestException as e:
            # Keep going: one org being down should not stop the others from
            # landing, and the 15-min lookback re-covers this window next run.
            failures.append(f"{org}: {e}")

    if rows_to_insert:
        # Generate Deterministic Row IDs for Idempotency
        # Format: {build_uuid}_{step_name_hash}. build_id is a UUID, so it is
        # already unique across orgs.
        row_ids = [f"{row['build_id']}_{row['step_name']}" for row in rows_to_insert]

        # Stream to BigQuery with deduplication
        errors = client.insert_rows_json(TABLE_ID, rows_to_insert, row_ids=row_ids)
        if errors:
            print(f"BigQuery Errors: {errors}")
            return "Partial Success", 500

    if failures:
        print(f"Failed orgs: {'; '.join(failures)}")
        return f"Processed {len(rows_to_insert)} items, {len(failures)} org(s) failed", 500

    return f"Processed {len(rows_to_insert)} items for pipeline {PIPELINE_SLUG}", 200

def fetch_org_rows(org, token, finished_from):
    headers = {"Authorization": f"Bearer {token}"}

    # Filtered by single pipeline
    url = f"https://api.buildkite.com/v2/organizations/{org}/pipelines/{PIPELINE_SLUG}/builds"
    params = {
        "finished_from": finished_from,
        "state": "finished"
    }

    response = requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()

    rows = []
    for build in response.json():
        # 1. Capture E2E Summary
        rows.append(construct_bq_row(org, build, "E2E_SUMMARY", build))

        # 2. Capture Individual Steps
        for job in build.get("jobs", []):
            if job.get("type") == "script" and job.get("finished_at"):
                rows.append(construct_bq_row(org, build, job.get("name"), job))

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
