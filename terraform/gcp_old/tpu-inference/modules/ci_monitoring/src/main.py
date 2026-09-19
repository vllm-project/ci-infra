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

# The batch arrives as one array of JSON strings, so the MERGE needs no table of
# its own to read from. Keying on row_key means a build that two runs both pick
# up is written once; the lookback deliberately overruns the cron interval to
# survive a failed run, so re-sends are routine.
MERGE_SQL = """
MERGE `{target}` T
USING (
  SELECT
    JSON_VALUE(r, '$.row_key') AS row_key,
    JSON_VALUE(r, '$.build_id') AS build_id,
    JSON_VALUE(r, '$.org_slug') AS org_slug,
    JSON_VALUE(r, '$.commit_hash') AS commit_hash,
    JSON_VALUE(r, '$.step_name') AS step_name,
    JSON_VALUE(r, '$.pipeline_slug') AS pipeline_slug,
    JSON_VALUE(r, '$.branch') AS branch,
    JSON_VALUE(r, '$.state') AS state,
    CAST(JSON_VALUE(r, '$.wait_duration_sec') AS FLOAT64) AS wait_duration_sec,
    CAST(JSON_VALUE(r, '$.run_duration_sec') AS FLOAT64) AS run_duration_sec,
    TIMESTAMP(JSON_VALUE(r, '$.created_at')) AS created_at
  FROM UNNEST(@rows) AS r
) S
ON T.row_key = S.row_key
WHEN NOT MATCHED THEN INSERT (
  row_key, build_id, org_slug, commit_hash, step_name, pipeline_slug,
  branch, state, wait_duration_sec, run_duration_sec, created_at
) VALUES (
  row_key, build_id, org_slug, commit_hash, step_name, pipeline_slug,
  branch, state, wait_duration_sec, run_duration_sec, created_at
)
"""

# Every pipeline is polled in every org; a pipeline absent from an org 404s.
PIPELINE_SLUGS = json.loads(os.environ.get("PIPELINE_SLUGS", "[]"))

# [{"org": ..., "token_env": ...}]. A Buildkite token is scoped to one org, so
# each names the env var holding its own, injected by Terraform.
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

    # (row_key, row) pairs; the key is what the MERGE dedups on.
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
                # Keep going so one bad target cannot drop the others; the
                # lookback re-covers this window next run.
                failures.append(f"{org}/{pipeline}: {e}")

    # A MERGE cannot match one target row from two source rows, so collapse any
    # key the poll returned twice before handing the batch over.
    unique = {row_key: row for row_key, row in pairs}
    rows_to_insert = [dict(row, row_key=row_key) for row_key, row in unique.items()]

    if rows_to_insert:
        try:
            merge_rows(rows_to_insert)
        except Exception as e:
            print(f"BigQuery merge failed: {e}")
            return "Merge failed", 500

    if failures:
        print(f"Failed: {'; '.join(failures)}")
        return f"Processed {len(rows_to_insert)} items, {len(failures)} target(s) failed", 500

    targets = len(ORGS) * len(PIPELINE_SLUGS)
    return f"Processed {len(rows_to_insert)} items across {targets} org/pipeline pair(s)", 200

def merge_rows(rows):
    """MERGE the batch into the target; a row_key already present is skipped."""
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter(
                "rows", "STRING", [json.dumps(row) for row in rows]
            )
        ]
    )
    client.query(MERGE_SQL.format(target=TABLE_ID), job_config=job_config).result()

def fetch_rows(org, token, pipeline, finished_from):
    headers = {"Authorization": f"Bearer {token}"}

    url = f"https://api.buildkite.com/v2/organizations/{org}/pipelines/{pipeline}/builds"
    params = {
        "finished_from": finished_from,
        "state": "finished"
    }

    response = requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()

    # Row keys dedup the builds the lookback re-sends. Key on the job UUID, not
    # the step name: parallel jobs share a name and would collapse into one.
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
