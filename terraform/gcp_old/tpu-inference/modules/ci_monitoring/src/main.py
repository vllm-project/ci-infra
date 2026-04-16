# modules/ci_monitoring/src/main.py

import os
import datetime
import requests
import functions_framework
from google.cloud import bigquery

# Global clients
client = bigquery.Client()
TABLE_ID = os.environ.get("BQ_TABLE_ID")
BK_API_TOKEN = os.environ.get("WEBHOOK_SECRET") 
ORG_SLUG = os.environ.get("ORG_SLUG")
PIPELINE_SLUG = os.environ.get("PIPELINE_SLUG")

@functions_framework.http
def handle_webhook(request):
    """
    Triggered by Cloud Scheduler to poll a SPECIFIC Buildkite pipeline.
    """
    # Define time window: look back 15 mins to ensure no gaps with 10-min cron
    now = datetime.datetime.now(datetime.timezone.utc)
    finished_from = (now - datetime.timedelta(minutes=15)).isoformat()

    headers = {"Authorization": f"Bearer {BK_API_TOKEN}"}
    
    # Updated URL to filter by single pipeline
    url = f"https://api.buildkite.com/v2/organizations/{ORG_SLUG}/pipelines/{PIPELINE_SLUG}/builds"
    params = {
        "finished_from": finished_from,
        "state": "finished"
    }

    response = requests.get(url, headers=headers, params=params)
    if response.status_code != 200:
        print(f"Failed to fetch from Buildkite: {response.text}")
        return "Error", 500

    builds = response.json()
    rows_to_insert = []

    for build in builds:
        # 1. Capture E2E Summary
        rows_to_insert.append(construct_bq_row(build, "E2E_SUMMARY", build))
        
        # 2. Capture Individual Steps
        for job in build.get("jobs", []):
            if job.get("type") == "script" and job.get("finished_at"):
                rows_to_insert.append(construct_bq_row(build, job.get("name"), job))

    if rows_to_insert:
        # Generate Deterministic Row IDs for Idempotency
        # Format: {build_uuid}_{step_name_hash}
        row_ids = [f"{row['build_id']}_{row['step_name']}" for row in rows_to_insert]
        
        # Stream to BigQuery with deduplication
        errors = client.insert_rows_json(TABLE_ID, rows_to_insert, row_ids=row_ids)
        if errors:
            print(f"BigQuery Errors: {errors}")
            return "Partial Success", 500

    return f"Processed {len(rows_to_insert)} items for pipeline {PIPELINE_SLUG}", 200

def construct_bq_row(build, step_name, timing_source):
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
