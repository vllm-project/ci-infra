#!/usr/bin/env python3
"""Builds the Baseline page's data from the pre-migration snapshot.

The bare-metal TPU fleet is being torn down, so its utilization can no longer be
read live; this turns the snapshot taken before the migration into one small
JSON file the dashboard serves as a static page.

    ./build_baseline.py ~/premigration-snapshot-2026-09-29

Reads buildkite/org=vllm/pipeline=*/builds_page_*.json.gz (every job's queue,
agent and times) and, for capacity, Buildkite's per-minute connected-agent
counts (gcp/monitoring/buildkite_exporter/org=vllm/TotalAgentCount).

The window is 2026-09-05 to 2026-09-28 UTC. It starts where the dumps do: the
TPU CI moved from the tpu_commons org to vllm on 2026-09-04, and the vllm
dumps hold only stray builds before it. It ends the day the inferact project's
tpu7x-8 VMs ran their first jobs on tpu_v7x_8_queue, before which that queue
was 18 VMs in the CI project - so the baseline is the fleet as it ran before
any of the migration's moves. Capacity is each queue's median connected agents
over the window, times the chips a job holds.

baseline/bare_lanes_2026-08-30_to_09-29.md in the snapshot was computed from
the same dumps; over its 30-day window the per-queue totals match it exactly,
but its busy shares divide by six days the dumps do not cover, so they read
about a fifth low.
"""

from __future__ import annotations

import collections
import datetime as dt
import glob
import gzip
import json
import os
import statistics
import sys
from pathlib import Path

# Chips a job on each bare-metal queue holds: Buildkite queue names count
# TensorCores (two per chip), so tpu_v7x_8_queue is a 4-chip VM.
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
GCS = "gs://mhhua-tt-dev/premigration-snapshot-2026-09-29/"
OUT = Path(__file__).with_name("premigration-2026-09.json")


def when(value: str | None) -> float | None:
    return (
        dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        if value
        else None
    )


def kind(build: dict) -> str:
    if build.get("source") == "schedule":
        return "schedule"
    return "main" if build.get("branch") == "main" else "pr/other"


def quantile(values: list, q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


def main() -> None:
    snap = Path(os.path.expanduser(sys.argv[1]))
    start, end = START.timestamp(), END.timestamp()
    hours = int((end - start) // 3600)

    held = {q: [0.0] * hours for q in QUEUES}
    chip_hours = collections.Counter()
    jobs = collections.Counter()
    by_kind = collections.defaultdict(collections.Counter)
    waits = collections.defaultdict(list)
    agents_by_day = collections.defaultdict(set)
    seen = set()
    for path in glob.glob(
        str(snap / "buildkite/org=vllm/pipeline=*/builds_page_*.json.gz")
    ):
        if Path(path).parent.name.endswith("-kube"):
            continue
        for build in json.load(gzip.open(path)):
            for job in build.get("jobs") or []:
                if job.get("type") != "script" or job["id"] in seen:
                    continue
                queue = next(
                    (
                        r.split("=", 1)[1]
                        for r in job.get("agent_query_rules") or []
                        if r.startswith("queue=")
                    ),
                    None,
                )
                if queue not in QUEUES:
                    continue
                s, e = when(job.get("started_at")), when(job.get("finished_at"))
                if not s or not e or e <= s:
                    continue
                seen.add(job["id"])
                lo, hi = max(s, start), min(e, end)
                if hi <= lo:
                    continue
                chips = QUEUES[queue][1]
                chip_hours[queue] += chips * (hi - lo) / 3600
                by_kind[queue][kind(build)] += chips * (hi - lo) / 3600
                jobs[queue] += 1
                # Spread the job over the hours it ran in.
                t = lo
                while t < hi:
                    i = int((t - start) // 3600)
                    edge = min(hi, start + (i + 1) * 3600)
                    held[queue][i] += chips * (edge - t) / 3600
                    t = edge
                runnable = when(job.get("runnable_at"))
                if runnable and s >= start:
                    waits[queue].append((s - runnable) / 60)
                agent = (job.get("agent") or {}).get("name")
                if agent:
                    day = dt.datetime.fromtimestamp(lo, dt.timezone.utc).date()
                    agents_by_day[(queue, day)].add(agent)

    # Capacity from the agents connected over the window: the fleet as it ran,
    # not as the snapshot day found it.
    connected = collections.defaultdict(list)
    by_hour = collections.defaultdict(lambda: collections.defaultdict(list))
    for path in sorted(
        glob.glob(
            str(
                snap
                / "gcp/monitoring/buildkite_exporter/org=vllm/TotalAgentCount/2026-*.json.gz"
            )
        )
    ):
        for series in json.load(gzip.open(path)):
            if series["queue"] not in QUEUES:
                continue
            for t, v in series["points"]:
                ts = when(t)
                if start <= ts < end:
                    connected[series["queue"]].append(v)
                    by_hour[series["queue"]][int((ts - start) // 3600)].append(v)
    agents = {
        q: round(statistics.median(connected[q])) if connected[q] else 0 for q in QUEUES
    }
    # Hour by hour too, at the hour's most: a VM that served a queue for a
    # while - a second tpu7x-32 on 2026-09-07 - raises that queue's line rather
    # than its held chips crossing a flat one.
    connected_chips = {
        q: [
            round(max(by_hour[q][i]) * QUEUES[q][1], 1) if by_hour[q].get(i) else None
            for i in range(hours)
        ]
        for q in QUEUES
    }
    # And the hour's average, for every share: chip-hours held over chip-hours
    # the connected agents had, so a VM that was there for a while counts on
    # both sides and one that was down on neither.
    connected_mean = {
        q: [
            statistics.mean(by_hour[q][i]) * QUEUES[q][1] if by_hour[q].get(i) else 0.0
            for i in range(hours)
        ]
        for q in QUEUES
    }

    days = (END - START).days
    queues = []
    for queue, (generation, chips) in QUEUES.items():
        capacity = agents[queue] * chips
        active = [
            len(agents_by_day.get((queue, (START + dt.timedelta(d)).date()), ()))
            for d in range(days)
        ]
        queues.append(
            {
                "queue": queue,
                "generation": generation,
                "chips_per_job": chips,
                "agents": agents[queue],
                "agents_active": round(statistics.mean(active), 1),
                "capacity": capacity,
                "jobs": jobs[queue],
                "chip_hours": round(chip_hours[queue]),
                "busy_share": chip_hours[queue] / sum(connected_mean[queue])
                if sum(connected_mean[queue])
                else None,
                "wait_p50": quantile(waits[queue], 0.5),
                "wait_p90": quantile(waits[queue], 0.9),
                "by_kind": {k: round(v) for k, v in by_kind[queue].most_common()},
                "held": [round(v, 2) for v in held[queue]],
                "connected": connected_chips[queue],
                "connected_mean": [round(v, 2) for v in connected_mean[queue]],
            }
        )

    generations = []
    for generation in ("v7x", "v6e"):
        members = [q for q in queues if q["generation"] == generation]
        series = [round(sum(v), 2) for v in zip(*(q["held"] for q in members))]
        had = [sum(v) for v in zip(*(q["connected_mean"] for q in members))]
        capacity = sum(q["capacity"] for q in members)
        # Hour of day in Pacific time, when the schedules are set: chips held
        # over chips connected, summed over the days at that hour.
        pacific = dt.timezone(dt.timedelta(hours=-7))
        by_hour = collections.defaultdict(list)
        held_at, had_at = collections.Counter(), collections.Counter()
        for i, (v, c) in enumerate(zip(series, had)):
            h = dt.datetime.fromtimestamp(start + i * 3600, pacific).hour
            by_hour[h].append(v)
            held_at[h] += v
            had_at[h] += c
        generations.append(
            {
                "name": generation,
                "capacity": capacity,
                "held": series,
                "mean_held": statistics.mean(series),
                "mean_connected": statistics.mean(had),
                "held_share": sum(series) / sum(had) if sum(had) else None,
                "by_hour_share_pt": [
                    round(100 * held_at[h] / had_at[h], 1) if had_at[h] else None
                    for h in range(24)
                ],
                "peak_held": max(series),
                "chip_hours": round(sum(series)),
                "jobs": sum(q["jobs"] for q in members),
                "hours_over_80": sum(1 for v in series if v >= 0.8 * capacity),
                "hours_under_20": sum(1 for v in series if v <= 0.2 * capacity),
                "by_hour_pt": [
                    round(statistics.mean(by_hour[h]), 1) for h in range(24)
                ],
            }
        )

    OUT.write_text(
        json.dumps(
            {
                "start": int(start),
                "end": int(end),
                "step": 3600,
                "snapshot": {"taken": "2026-09-29", "gcs": GCS},
                "window": f"{START:%Y-%m-%d} to {END:%Y-%m-%d} UTC",
                "generations": generations,
                "queues": queues,
            },
            separators=(",", ":"),
        )
    )
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KiB)")
    for q in queues:
        print(
            f"  {q['queue']:<18} jobs {q['jobs']:>6}  chip-hours {q['chip_hours']:>6}  "
            f"busy {q['busy_share']:.0%}  wait {q['wait_p50']:.0f}/{q['wait_p90']:.0f} min  {q['by_kind']}"
        )


if __name__ == "__main__":
    main()
