#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Convert a minimal CUDA/NVTX capture to unordered kernel/test edges.

Attribution is by LAUNCH SITE, not execution time: each GPU kernel row is
joined via correlationId to its CUDA runtime launch row, and the launch is
attributed to the innermost citest NVTX range covering it on the same
thread. Async execution and stream overlap therefore cannot misattribute;
the known loss is CUDA-graph capture (kernels inside a replayed graph
attribute to the launching test, which is correct for selection purposes,
but capture-time structure is not recovered).

Outputs only the unordered kernel-identity -> test/job set on --out and a
capture summary on stderr. The raw timeline is temporary implementation detail
and is deleted by the caller after this set is materialized.
"""

import argparse
import json
import pathlib
import sqlite3
import sys
import tempfile
from bisect import bisect_right
from collections import Counter, defaultdict
from heapq import heappop, heappush

try:
    from .nsys_runtime import load_runtime_launches
except ImportError:  # direct script execution from run_traced.sh
    from nsys_runtime import load_runtime_launches

CALL_PREFIX = "citest::"
AUX_PREFIXES = ("citest-setup::", "citest-teardown::")


def columns(con, table):
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def string_ids(con):
    return {i: v for i, v in con.execute("SELECT id, value FROM StringIds")}


def load_ranges(con, strings):
    """citest NVTX push/pop ranges: (globalTid, start, end, nodeid, phase)."""
    cols = columns(con, "NVTX_EVENTS")
    if not cols:
        raise SystemExit("no NVTX_EVENTS table: was --trace=nvtx enabled?")
    text_col = "text" if "text" in cols else "NULL"
    text_id_col = "textId" if "textId" in cols else "NULL"
    ranges = []
    for row in con.execute(
        f"SELECT start, end, globalTid, {text_col}, {text_id_col}"
        " FROM NVTX_EVENTS WHERE end IS NOT NULL"
    ):
        start, end, gtid, text, text_id = row
        label = text if text is not None else strings.get(text_id)
        if not label:
            continue
        if label.startswith(CALL_PREFIX):
            phase, nodeid = "call", label[len(CALL_PREFIX) :]
        else:
            for p in AUX_PREFIXES:
                if label.startswith(p):
                    phase, nodeid = p.split("::")[0], label[len(p) :]
                    break
            else:
                continue
        ranges.append((gtid, start, end, nodeid, phase))
    return ranges


def kernel_name_column(con):
    cols = columns(con, "CUPTI_ACTIVITY_KIND_KERNEL")
    if not cols:
        raise SystemExit(
            "no CUPTI_ACTIVITY_KIND_KERNEL table: was --trace=cuda enabled?"
        )
    if "mangledName" not in cols:
        raise SystemExit(
            "kernel table has no mangledName; stable kernel identity is unavailable"
        )
    return "mangledName"


def temporal_attributions(ranges, launches):
    """Attribute launch times only when exactly one test range is active.

    This is the bounded fallback for child processes whose CUDA launch thread
    cannot carry the parent's NVTX range. Concurrent/ambiguous launches do not
    receive test precision.
    """

    ordered_ranges = sorted(ranges, key=lambda span: span[1])
    ordered_launches = sorted(launches.items(), key=lambda item: item[1][1])
    active = Counter()
    endings = []
    range_index = 0
    result = {}
    for launch_key, (_gtid, launch_time) in ordered_launches:
        while (
            range_index < len(ordered_ranges)
            and ordered_ranges[range_index][1] <= launch_time
        ):
            _gtid, start, end, nodeid, phase = ordered_ranges[range_index]
            key = (nodeid, phase)
            active[key] += 1
            heappush(endings, (end, key))
            range_index += 1
        while endings and endings[0][0] < launch_time:
            _end, key = heappop(endings)
            active[key] -= 1
            if active[key] == 0:
                del active[key]
        if sum(active.values()) == 1:
            result[launch_key] = next(iter(active))
        elif active:
            result[launch_key] = False
        else:
            result[launch_key] = None
    return result


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("sqlite", help="nsys export --type sqlite output")
    ap.add_argument("--out", default="-", help="edge JSONL output")
    ap.add_argument(
        "--job-key", required=True, help="represented production Buildkite step key"
    )
    ap.add_argument("--repository-sha", required=True)
    ap.add_argument("--created-at", required=True)
    args = ap.parse_args(argv)

    con = sqlite3.connect(f"file:{args.sqlite}?mode=ro", uri=True)
    try:
        strings = string_ids(con)
        name_col = kernel_name_column(con)

        # per-thread launch attribution index: sorted range starts, innermost win
        ranges = load_ranges(con, strings)
        by_tid = defaultdict(list)
        for gtid, start, end, nodeid, phase in ranges:
            by_tid[gtid].append((start, end, nodeid, phase))
        for tid in by_tid:
            by_tid[tid].sort()

        starts_by_tid = {
            tid: [span[0] for span in spans] for tid, spans in by_tid.items()
        }

        def attribute(gtid, t):
            """Return the innermost test range covering launch time ``t``.

            Push/pop ranges on one thread are properly nested, and inner ranges
            start later. The first covering span found while scanning backwards
            is therefore the innermost range.
            """
            spans = by_tid.get(gtid, ())
            starts = starts_by_tid.get(gtid, ())
            for i in range(bisect_right(starts, t) - 1, -1, -1):
                start, end, nodeid, phase = spans[i]
                if end >= t:
                    return nodeid, phase
            return None

        # CUPTI correlation IDs are process-scoped: with fork tracing enabled
        # two processes can reuse the same correlationId, so the kernel->launch
        # join must include process identity. Recent Nsight versions can also
        # emit nested versioned/unversioned API aliases for one launch; the
        # shared loader collapses only aliases proven by name/thread/interval.
        runtime_launches, runtime_aliases = load_runtime_launches(con, strings)
        launches = {
            key: (launch["global_tid"], launch["start_ns"])
            for key, launch in runtime_launches.items()
        }
        temporal_by_launch = temporal_attributions(ranges, launches)

        kernel_cols = columns(con, "CUPTI_ACTIVITY_KIND_KERNEL")
        kernel_pid_col = (
            "globalPid"
            if "globalPid" in kernel_cols
            else "globalTid"
            if "globalTid" in kernel_cols
            else None
        )
        process_scoped = kernel_pid_col is not None
        if not process_scoped:
            # single-process fallback: correlation-only join, flagged in summary
            by_cid_only = {}
            by_cid_launch_key = {}
            collided = set()
            for launch_key, value in launches.items():
                _pid, cid = launch_key
                if cid in by_cid_only:
                    collided.add(cid)
                by_cid_only[cid] = value
                by_cid_launch_key[cid] = launch_key

        stats = defaultdict(int)
        seen_pairs = set()
        rows = []
        pid_select = f", {kernel_pid_col}" if process_scoped else ""
        for row in con.execute(
            f"SELECT correlationId, {name_col}{pid_select}"
            " FROM CUPTI_ACTIVITY_KIND_KERNEL"
        ):
            cid, name_id = row[0], row[1]
            stats["kernel_rows"] += 1
            name = strings.get(name_id, name_id)
            if process_scoped:
                launch_key = (row[2] >> 24, cid)
                launch = launches.get(launch_key)
            else:
                if cid in collided:
                    raise SystemExit(
                        "kernel table has no process column and the referenced "
                        "correlation ID is not unique: join would be unsound"
                    )
                launch = by_cid_only.get(cid)
                launch_key = by_cid_launch_key.get(cid)
            if launch is None:
                stats["no_launch_row"] += 1
                continue
            hit = attribute(*launch)
            if hit is not None:
                destination_kind = "test"
                nodeid, phase = hit
                destination = nodeid
                attribution_mode = "exact_test"
                stats["attribution_exact_test"] += 1
            else:
                fallback = temporal_by_launch.get(launch_key)
                if fallback:
                    destination_kind = "test"
                    nodeid, phase = fallback
                    destination = nodeid
                    attribution_mode = "temporal_test"
                    stats["attribution_temporal_test"] += 1
                else:
                    destination_kind = "job"
                    nodeid, phase = None, "job"
                    destination = args.job_key
                    attribution_mode = "job_union"
                    stats["attribution_job_union"] += 1
                    if fallback is False:
                        stats["ambiguous_active_test_ranges"] += 1
                    else:
                        stats["outside_any_test_range"] += 1
            pair = (name, destination_kind, destination, phase, attribution_mode)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            rows.append(
                {
                    "source_kind": "kernel",
                    "source": name,
                    "edge_kind": (
                        "executed_by"
                        if destination_kind == "test"
                        else "executed_by_job"
                    ),
                    "destination_kind": destination_kind,
                    "destination": destination,
                    "phase": phase,
                    "attribution_mode": attribution_mode,
                    "created_at": args.created_at,
                    "job_key": args.job_key,
                    "repository_sha": args.repository_sha,
                    **({"test_id": nodeid} if nodeid is not None else {}),
                }
            )
    finally:
        con.close()

    rows.sort(
        key=lambda row: (
            row["source"],
            row["destination"],
            row["phase"],
        )
    )

    if args.out == "-":
        for row in rows:
            print(json.dumps(row, sort_keys=True))
    else:
        output = pathlib.Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", dir=output.parent, suffix=".tmp", delete=False
        ) as stream:
            temporary = pathlib.Path(stream.name)
            for row in rows:
                stream.write(json.dumps(row, sort_keys=True) + "\n")
        temporary.replace(output)

    summary = dict(stats)
    summary["kernel_name_column"] = name_col
    summary["mangled_identity"] = True
    summary["process_scoped_join"] = process_scoped
    summary["unique_kernel_test_pairs"] = len(seen_pairs)
    summary["unique_kernel_destination_pairs"] = len(seen_pairs)
    summary["citest_ranges"] = len(ranges)
    summary["runtime_alias_rows_deduplicated"] = runtime_aliases
    summary["attributed_kernel_rows"] = (
        stats["attribution_exact_test"]
        + stats["attribution_temporal_test"]
        + stats["attribution_job_union"]
    )
    test_attributed = (
        stats["attribution_exact_test"] + stats["attribution_temporal_test"]
    )
    summary["test_attribution_rate"] = (
        test_attributed / stats["kernel_rows"] if stats["kernel_rows"] else 0.0
    )
    summary["job_union_rate"] = (
        stats["attribution_job_union"] / stats["kernel_rows"]
        if stats["kernel_rows"]
        else 0.0
    )
    print(json.dumps(summary, indent=2, sort_keys=True), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
