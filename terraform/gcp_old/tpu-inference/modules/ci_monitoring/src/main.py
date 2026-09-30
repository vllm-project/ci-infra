# modules/ci_monitoring/src/main.py

import os
import json
import datetime
import concurrent.futures
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
    TIMESTAMP(JSON_VALUE(r, '$.created_at')) AS created_at,
    JSON_VALUE(r, '$.job_id') AS job_id,
    JSON_VALUE(r, '$.step_key') AS step_key,
    JSON_VALUE(r, '$.queue') AS queue,
    JSON_VALUE(r, '$.agent_name') AS agent_name,
    JSON_VALUE(r, '$.agent_hostname') AS agent_hostname,
    SAFE_CAST(JSON_VALUE(r, '$.exit_status') AS INT64) AS exit_status,
    SAFE_CAST(JSON_VALUE(r, '$.soft_failed') AS BOOL) AS soft_failed,
    SAFE_CAST(JSON_VALUE(r, '$.retried') AS BOOL) AS retried,
    JSON_VALUE(r, '$.retry_type') AS retry_type,
    SAFE_CAST(JSON_VALUE(r, '$.build_number') AS INT64) AS build_number,
    JSON_VALUE(r, '$.build_source') AS build_source,
    TIMESTAMP(JSON_VALUE(r, '$.runnable_at')) AS runnable_at,
    TIMESTAMP(JSON_VALUE(r, '$.started_at')) AS started_at,
    TIMESTAMP(JSON_VALUE(r, '$.finished_at')) AS finished_at,
    TIMESTAMP(JSON_VALUE(r, '$.build_created_at')) AS build_created_at,
    TIMESTAMP(JSON_VALUE(r, '$.build_finished_at')) AS build_finished_at
  FROM UNNEST(@rows) AS r
) S
ON T.row_key = S.row_key
WHEN NOT MATCHED THEN INSERT (
  row_key, build_id, org_slug, commit_hash, step_name, pipeline_slug,
  branch, state, wait_duration_sec, run_duration_sec, created_at,
  job_id, step_key, queue, agent_name, agent_hostname, exit_status,
  soft_failed, retried, retry_type, build_number, build_source,
  runnable_at, started_at, finished_at, build_created_at, build_finished_at
) VALUES (
  row_key, build_id, org_slug, commit_hash, step_name, pipeline_slug,
  branch, state, wait_duration_sec, run_duration_sec, created_at,
  job_id, step_key, queue, agent_name, agent_hostname, exit_status,
  soft_failed, retried, retry_type, build_number, build_source,
  runnable_at, started_at, finished_at, build_created_at, build_finished_at
)
"""

# Every pipeline is polled in every org; a pipeline absent from an org 404s.
PIPELINE_SLUGS = json.loads(os.environ.get("PIPELINE_SLUGS", "[]"))

# [{"org": ..., "token_env": ...}]. A Buildkite token is scoped to one org, so
# each names the env var holding its own, injected by Terraform.
ORGS = json.loads(os.environ.get("ORGS_JSON", "[]"))

FETCH_WORKERS = 8

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

    targets = []
    for entry in ORGS:
        org = entry["org"]
        token = os.environ.get(entry["token_env"])
        if not token:
            failures.append(f"{org}: {entry['token_env']} is unset")
            continue
        targets.extend((org, token, pipeline) for pipeline in PIPELINE_SLUGS)

    # Concurrently: each target is a request of up to 30s, and one after
    # another two dozen of them outlast the function's timeout.
    with concurrent.futures.ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {
            pool.submit(fetch_rows, org, token, pipeline, finished_from): (org, pipeline)
            for org, token, pipeline in targets
        }
        for future in concurrent.futures.as_completed(futures):
            org, pipeline = futures[future]
            try:
                pairs.extend(future.result())
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
        "state": "finished",
        # Every attempt, not only the last: a failure a retry rescued is what
        # a first-attempt pass rate and a flake rate are made of.
        "include_retried_jobs": "true",
        "per_page": 100,
    }

    # Row keys dedup the builds the lookback re-sends. Key on the job UUID, not
    # the step name: parallel jobs share a name and would collapse into one.
    rows = []
    while url:
        response = requests.get(url, headers=headers, params=params, timeout=30)
        response.raise_for_status()

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
                        construct_bq_row(org, build, job.get("name"), job, job=job),
                    ))

        # The next page's URL carries the query string itself.
        url = response.links.get("next", {}).get("url")
        params = None

    return rows

def construct_bq_row(org, build, step_name, timing_source, job=None):
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

    job = job or {}
    agent = job.get("agent") or {}
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
        "created_at": parse_ts(timing_source.get("created_at")).isoformat() if timing_source.get("created_at") else None,
        # The raw times as well as the durations above, so a window, a busy
        # share or a time to signal can be computed after the fact. The agent
        # and queue outlive the agent itself, which a torn-down fleet needs.
        "job_id": job.get("id"),
        "step_key": job.get("step_key"),
        "queue": queue_of(job),
        "agent_name": agent.get("name"),
        "agent_hostname": agent.get("hostname"),
        "exit_status": job.get("exit_status"),
        "soft_failed": job.get("soft_failed") if job else None,
        "retried": job.get("retried") if job else None,
        "retry_type": job.get("retry_type"),
        "build_number": build.get("number"),
        "build_source": build.get("source"),
        "runnable_at": iso(runnable_at),
        "started_at": iso(started_at),
        "finished_at": iso(finished_at),
        "build_created_at": iso(parse_ts(build.get("created_at"))),
        "build_finished_at": iso(parse_ts(build.get("finished_at"))),
    }

def queue_of(job):
    """The queue a job asked for, from its agent query rules."""
    for rule in job.get("agent_query_rules") or []:
        if rule.startswith("queue="):
            return rule[len("queue="):]
    return None

def iso(ts):
    return ts.isoformat() if ts else None

def parse_ts(ts_str):
    if not ts_str: return None
    return datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
