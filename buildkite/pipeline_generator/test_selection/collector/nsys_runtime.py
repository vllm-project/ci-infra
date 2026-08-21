#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Soundly canonicalize Nsight CUDA runtime launch rows."""

from __future__ import annotations

import sqlite3
import re
from collections import defaultdict
from typing import Any

_VERSION_SUFFIX = re.compile(r"_v[0-9]+$")


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _api_name(row: dict[str, Any], strings: dict[int, str]) -> str | None:
    identifier = row.get("nameId")
    if identifier is None:
        return None
    return strings.get(identifier, str(identifier))


def _canonical_api(name: str) -> str:
    return _VERSION_SUFFIX.sub("", name)


def _canonical_alias(
    rows: list[dict[str, Any]], strings: dict[int, str]
) -> dict[str, Any]:
    """Collapse the versioned/unversioned wrapper pair emitted by Nsight.

    Recent Nsight releases can record both ``cudaLaunchKernel`` and its ABI
    alias (for example ``cudaLaunchKernel_v7000``) with the same process-local
    correlation ID. They are one nested API call, not two launches. Accept the
    pair only when thread, canonical API name, and overlapping intervals prove
    that relationship; every other duplicate remains a fail-closed error.
    """

    threads = {row["global_tid"] for row in rows}
    names = [_api_name(row, strings) for row in rows]
    if len(threads) != 1 or any(name is None for name in names):
        raise SystemExit(
            "duplicate CUDA runtime correlation ID is not a proven API alias"
        )
    if len({_canonical_api(name) for name in names if name is not None}) != 1:
        raise SystemExit(
            "duplicate CUDA runtime correlation ID has different API names"
        )
    if any(row.get("end") is None for row in rows):
        raise SystemExit(
            "duplicate CUDA runtime correlation ID lacks interval evidence"
        )
    if max(row["start_ns"] for row in rows) > min(int(row["end"]) for row in rows):
        raise SystemExit(
            "duplicate CUDA runtime correlation ID has disjoint API intervals"
        )

    # The unversioned wrapper carries Nsight's Python/native launch callchain
    # in current exports. Prefer any row with a callchain, then the outermost
    # (earliest-starting, latest-ending) wrapper for attribution time.
    return min(
        rows,
        key=lambda row: (
            row.get("callchainId") is None,
            row["start_ns"],
            -int(row["end"]),
        ),
    )


def load_runtime_launches(
    connection: sqlite3.Connection, strings: dict[int, str]
) -> tuple[dict[tuple[int, int], dict[str, Any]], int]:
    """Return one sound CUDA runtime launch per process/correlation key."""

    columns = _columns(connection, "CUPTI_ACTIVITY_KIND_RUNTIME")
    required = {"correlationId", "globalTid", "start"}
    if not required <= columns:
        raise SystemExit("CUDA runtime table lacks launch correlation columns")
    optional = [name for name in ("end", "nameId", "callchainId") if name in columns]
    select = ", ".join(["correlationId", "globalTid", "start", *optional])
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for correlation_id, global_tid, start, *values in connection.execute(
        f"SELECT {select} FROM CUPTI_ACTIVITY_KIND_RUNTIME"
    ):
        launch = {
            "correlation_id": int(correlation_id),
            "global_tid": int(global_tid),
            "process_key": int(global_tid) >> 24,
            "start_ns": int(start),
        }
        launch.update(dict(zip(optional, values)))
        grouped[(launch["process_key"], launch["correlation_id"])].append(launch)

    launches = {}
    aliases = 0
    for key, rows in grouped.items():
        if len(rows) == 1:
            launches[key] = rows[0]
        else:
            launches[key] = _canonical_alias(rows, strings)
            aliases += len(rows) - 1
    return launches, aliases
