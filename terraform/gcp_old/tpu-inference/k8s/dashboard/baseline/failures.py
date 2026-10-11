#!/usr/bin/env python3
"""Builds the Migration page's infrastructure-failure comparison.

A job that fails because its VM ran out of disk, its TPU would not start or
its agent died ends with an ordinary exit code, so the cause is only in the
log. This fetches each failed TPU job's log with `bk api`, sorts it by the
patterns below into a cause, and writes failures.json beside this file:

    ./failures.py ~/premigration-snapshot-2026-09-29 --kube-days 7

Bare metal: every failed attempt on the TPU queues in the baseline window,
from the snapshot's Buildkite dumps. Kube: every failed job on the kube queue
in the last --kube-days days, from the step log table, with its chips and
shape from the workload timing table (bq, with TIMING_TABLE set). Logs are
classified once and kept in --cache, so a rerun fetches only new failures.
The result is a snapshot: rerun to refresh the kube side.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import datetime as dt
import glob
import gzip
import json
import os
import re
import subprocess
import time
from pathlib import Path

QUEUES = {
    "tpu_v7x_2_queue": ("v7x", 1),
    "tpu_v7x_8_queue": ("v7x", 4),
    "tpu_v7x_16_queue": ("v7x", 8),
    "tpu_v7x_32_queue": ("v7x", 16),
    "tpu_v6e_queue": ("v6e", 1),
    "tpu_v6e_8_queue": ("v6e", 8),
}
START = dt.datetime(2026, 9, 5, tzinfo=dt.timezone.utc)
END = dt.datetime(2026, 9, 28, tzinfo=dt.timezone.utc)
OUT = Path(__file__).with_name("failures.json")

# Matched against the last 200 KB of a log, line by line: the tail is where a
# job says why it died, and a whole log can hold a test's expected errors.
PATTERNS = {
    "disk_full": r"no space left on device|enospc|errno 28|disk quota exceeded",
    "tpu": r"tpu initialization failed|unable to initialize backend 'tpu'|tpu (is )?(already )?in use|failed to open /dev/vfio|device or resource busy",
    "oom": r"exit code 137|killed process|oom-kill|cannot allocate memory",
    "bus": r"bus error|sigbus",
    "image": r"failed to pull|manifest unknown|manifest for .* not found|pull access denied|toomanyrequests|error pulling image|errimagepull|imagepullbackoff",
    "docker": r"docker\.sock|cannot connect to the docker daemon",
    "network": r"huggingface_hub\.(errors|utils)|cas client error|5\d\d server error|429 client error|connection reset|read timed out|temporary failure in name resolution|max retries exceeded with url",
    "terminated": r"signal: terminated",
}
COMPILED = {k: re.compile(v, re.I) for k, v in PATTERNS.items()}
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|_bk;t=\d+\x07?")
# The page's names, in the order it lists them.
CAUSES = {
    "disk_full": "Disk full",
    "tpu": "TPU failed to start",
    "bus": "Bus error (shared memory)",
    "oom": "Killed for memory",
    "agent_lost": "Agent lost",
    "image": "CI image missing",
    "docker": "Docker daemon",
    "network": "Download or network",
}


def cause(job: dict) -> str:
    """One cause a failure: the first that matches, in CAUSES' order. A job
    whose agent was terminated was cancelled, not failed by the fleet; an
    exit of -1 otherwise is an agent that died."""
    hits, code = set(job["hits"]), job["exit"]
    if "terminated" in hits and code == -1:
        return "cancelled"
    if "disk_full" in hits:
        return "disk_full"
    if "tpu" in hits:
        return "tpu"
    if code == 135 or "bus" in hits:
        return "bus"
    if code == 137 or "oom" in hits:
        return "oom"
    if code == -1:
        return "agent_lost"
    for k in ("image", "docker", "network"):
        if k in hits:
            return k
    return "test"


def when(value: str | None) -> float | None:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None


def bare_failures(snap: Path) -> list[dict]:
    lo, hi = START.timestamp(), END.timestamp()
    out, seen = [], set()
    for path in glob.glob(str(snap / "buildkite/org=vllm/pipeline=*/builds_page_*.json.gz")):
        pipeline = Path(path).parent.name.split("=", 1)[1]
        if pipeline.endswith("-kube"):
            continue
        with gzip.open(path) as f:
            builds = json.load(f)
        for b in builds:
            for j in b.get("jobs") or []:
                if j.get("type") != "script" or j["id"] in seen:
                    continue
                queue = next(
                    (r.split("=", 1)[1] for r in j.get("agent_query_rules") or [] if r.startswith("queue=")),
                    None,
                )
                s, e = when(j.get("started_at")), when(j.get("finished_at"))
                if queue not in QUEUES or not s or not e or not lo <= s < hi:
                    continue
                seen.add(j["id"])
                if j.get("exit_status") in (0, None) or j.get("state") not in ("failed", "broken", "timed_out"):
                    continue
                generation, chips = QUEUES[queue]
                out.append({"job": j["id"], "pipeline": pipeline, "build": b["number"],
                            "generation": generation, "exit": j["exit_status"],
                            "chip_hours": chips * (e - s) / 3600})
    return out


def bq(sql: str) -> list[dict]:
    out = subprocess.run(
        ["bq", "query", "--use_legacy_sql=false", "--format=json", "--max_rows=100000", sql],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout or "[]")


def kube_failures(days: int) -> tuple[list[dict], dict]:
    timing = os.environ["TIMING_TABLE"]
    steps = timing.rsplit(".", 1)[0] + ".step_execution_logs"
    rows = bq(f"""
WITH s AS (SELECT job_id, ANY_VALUE(pipeline_slug) pipeline, ANY_VALUE(build_number) build,
    ANY_VALUE(exit_status) exit_status
  FROM `{steps}` WHERE created_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
    AND queue = 'kube' GROUP BY job_id),
w AS (SELECT job_id, ANY_VALUE(queue) shape,
    SUM(chips * TIMESTAMP_DIFF(COALESCE(ended_at, finished_at), admitted_at, SECOND)) / 3600 chip_hours
  FROM `{timing}` WHERE COALESCE(ended_at, finished_at) >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days + 1} DAY)
  GROUP BY job_id)
SELECT s.*, w.shape, w.chip_hours FROM s LEFT JOIN w USING (job_id)
WHERE s.exit_status IS NOT NULL AND s.exit_status != 0""")
    gen = {"tpu7x": "v7x", "ct6e": "v6e"}
    failures = [
        {"job": r["job_id"], "pipeline": r["pipeline"], "build": int(r["build"]),
         "generation": next((g for p, g in gen.items() if (r.get("shape") or "").startswith(p)), "none"),
         "exit": int(r["exit_status"]), "chip_hours": float(r.get("chip_hours") or 0)}
        for r in rows
    ]
    totals = {
        gen[r["prefix"]]: (float(r["chip_hours"]), int(r["jobs"]))
        for r in bq(f"""
SELECT SPLIT(queue, '-')[OFFSET(0)] prefix, COUNT(DISTINCT job_id) jobs,
  SUM(chips * TIMESTAMP_DIFF(COALESCE(ended_at, finished_at), admitted_at, SECOND)) / 3600 chip_hours
FROM `{timing}` WHERE COALESCE(ended_at, finished_at) >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
  AND admitted_at IS NOT NULL GROUP BY prefix""")
        if r["prefix"] in gen
    }
    return failures, totals


def classify(jobs: list[dict], cache: Path) -> list[dict]:
    known = {}
    if cache.exists():
        for line in cache.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                known[row["job"]] = row["hits"]
    todo = [j for j in jobs if j["job"] not in known]
    print(f"{cache.name}: {len(jobs)} failures, {len(todo)} to fetch", flush=True)

    def fetch(j: dict) -> list[str]:
        path = f"/pipelines/{j['pipeline']}/builds/{j['build']}/jobs/{j['job']}/log"
        for attempt in range(6):
            r = subprocess.run(["bk", "api", path], capture_output=True, text=True)
            if r.returncode == 0:
                text = ANSI.sub("", json.loads(r.stdout).get("content", ""))[-200_000:]
                return [k for k, rx in COMPILED.items() if rx.search(text)]
            if "429" not in r.stderr:
                return ["fetch_error"]
            time.sleep(30 * (attempt + 1))
        return ["fetch_error"]

    with open(cache, "a") as fh, concurrent.futures.ThreadPoolExecutor(4) as pool:
        for j, hits in zip(todo, pool.map(fetch, todo)):
            known[j["job"]] = hits
            fh.write(json.dumps({"job": j["job"], "hits": hits}) + "\n")
    return [{**j, "hits": known[j["job"]]} for j in jobs]


def summarize(jobs: list[dict], totals: dict, days: float) -> dict:
    """totals: generation -> (chip-hours, jobs) over the window, failed or not."""
    out = {}
    for generation in sorted({j["generation"] for j in jobs} | set(totals)):
        mine = [j for j in jobs if j["generation"] == generation]
        by = collections.defaultdict(lambda: {"n": 0, "chip_hours": 0.0})
        for j in mine:
            c = by[cause(j)]
            c["n"] += 1
            c["chip_hours"] += j["chip_hours"]
        chip_hours, count = totals.get(generation, (0, 0))
        out[generation] = {
            "days": days,
            "chip_hours": round(chip_hours),
            "jobs": count,
            "causes": {k: {"n": v["n"], "chip_hours": round(v["chip_hours"], 1)} for k, v in by.items()},
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshot")
    ap.add_argument("--kube-days", type=int, default=7)
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/tpu-ci-failures"))
    args = ap.parse_args()
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    snap = Path(os.path.expanduser(args.snapshot))
    baseline = json.loads(Path(__file__).with_name("premigration-2026-09.json").read_text())

    bare = classify(bare_failures(snap), cache / "bare.jsonl")
    kube, kube_totals = kube_failures(args.kube_days)
    kube = classify(kube, cache / "kube.jsonl")
    now = dt.datetime.now(dt.timezone.utc)
    OUT.write_text(json.dumps({
        "causes": CAUSES,
        "bare": {
            "window": f"{START:%Y-%m-%d} to {END:%Y-%m-%d}",
            "by_generation": summarize(
                bare,
                {g["name"]: (g["chip_hours"], g["jobs"]) for g in baseline["generations"]},
                (END - START).days,
            ),
        },
        "kube": {
            "window": f"{now - dt.timedelta(days=args.kube_days):%Y-%m-%d} to {now:%Y-%m-%d}",
            "by_generation": summarize(kube, kube_totals, args.kube_days),
        },
    }, indent=1))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
