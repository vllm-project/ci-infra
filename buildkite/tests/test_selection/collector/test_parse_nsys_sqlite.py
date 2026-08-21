# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""Tests for reducing CUDA/NVTX timelines to unordered kernel/test sets."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "pipeline_generator/test_selection/collector/parse_nsys_sqlite.py"
)
REPOSITORY_SHA = "a" * 40
CREATED_AT = "2026-08-19T00:00:00Z"
K_INNER = "_ZinnerK"
K_OUTER = "_ZouterK"
K_SETUP = "_ZsetupK"
K_ORPHAN = "_ZorphanK"
K_PROC2 = "_Zproc2K"
K_TEMPORAL = "_ZtemporalK"
PID1 = 5
PID2 = 6
TID1 = (PID1 << 24) | 7
GPID1 = PID1 << 24


def _build_fixture(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE StringIds (id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute(
        "CREATE TABLE NVTX_EVENTS (start INT, end INT, "
        "globalTid INT, text TEXT, textId INT)"
    )
    connection.execute(
        "CREATE TABLE CUPTI_ACTIVITY_KIND_RUNTIME (start INT, end INT, "
        "correlationId INT, globalTid INT)"
    )
    connection.execute(
        "CREATE TABLE CUPTI_ACTIVITY_KIND_KERNEL (start INT, end INT, "
        "correlationId INT, mangledName INT, globalPid INT)"
    )
    connection.executemany(
        "INSERT INTO StringIds VALUES (?, ?)",
        {
            1: K_INNER,
            2: K_OUTER,
            3: K_SETUP,
            4: K_ORPHAN,
            5: K_PROC2,
            6: K_TEMPORAL,
        }.items(),
    )
    connection.executemany(
        "INSERT INTO NVTX_EVENTS VALUES (?, ?, ?, ?, ?)",
        [
            (100, 900, TID1, "citest-setup::tests/a.py::test_x", None),
            (1000, 5000, TID1, "citest::tests/a.py::test_x", None),
            (2000, 3000, TID1, "citest::tests/a.py::test_x_inner", None),
            (6000, 9000, TID1, "citest::tests/a.py::test_y", None),
            (0, 99999, TID1, "torch-internal", None),
        ],
    )
    connection.executemany(
        "INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES (?, ?, ?, ?)",
        [
            (150, 160, 11, TID1),
            (2500, 2510, 12, TID1),
            (4900, 4910, 13, TID1),
            (5500, 5510, 14, TID1),
        ],
    )
    connection.executemany(
        "INSERT INTO CUPTI_ACTIVITY_KIND_KERNEL VALUES (?, ?, ?, ?, ?)",
        [
            (200, 300, 11, 3, GPID1),
            (2600, 2700, 12, 1, GPID1),
            (7000, 8000, 13, 2, GPID1),
            (5600, 5700, 14, 4, GPID1),
        ],
    )
    connection.commit()
    connection.close()


@pytest.fixture
def trace_files(tmp_path: Path) -> tuple[Path, Path]:
    database = tmp_path / "trace.sqlite"
    output = tmp_path / "edges.jsonl"
    _build_fixture(database)
    return database, output


def _run_parser(
    database: Path, output: Path, *, check: bool = True
) -> tuple[subprocess.CompletedProcess[str], list[dict], dict]:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(database),
            "--out",
            str(output),
            "--job-key",
            "kernels-flashmla-test-h100",
            "--repository-sha",
            REPOSITORY_SHA,
            "--created-at",
            CREATED_AT,
        ],
        capture_output=True,
        text=True,
        check=check,
    )
    rows = (
        [json.loads(line) for line in output.read_text().splitlines()]
        if output.exists() and result.returncode == 0
        else []
    )
    summary = json.loads(result.stderr) if result.returncode == 0 else {}
    return result, rows, summary


def test_reduces_to_kernel_test_and_conservative_job_sets(trace_files):
    database, output = trace_files

    _result, rows, summary = _run_parser(database, output)

    by_kernel = {row["source"]: row for row in rows}
    assert by_kernel[K_SETUP]["destination"] == "tests/a.py::test_x"
    assert by_kernel[K_SETUP]["phase"] == "citest-setup"
    assert by_kernel[K_INNER]["destination"] == "tests/a.py::test_x_inner"
    assert by_kernel[K_OUTER]["destination"] == "tests/a.py::test_x"
    assert by_kernel[K_ORPHAN]["destination_kind"] == "job"
    assert by_kernel[K_ORPHAN]["attribution_mode"] == "job_union"
    assert summary["outside_any_test_range"] == 1
    assert summary["kernel_rows"] == 4
    assert summary["unique_kernel_destination_pairs"] == 4
    assert summary["mangled_identity"] is True
    assert all(row["repository_sha"] == REPOSITORY_SHA for row in rows)
    assert all(row["created_at"] == CREATED_AT for row in rows)
    assert not any("artifact_class" in row for row in rows)


def test_child_launch_thread_uses_unambiguous_temporal_range(trace_files):
    database, output = trace_files
    child_tid = (PID1 << 24) | 19
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES (4000, 4010, 21, ?)",
        (child_tid,),
    )
    connection.execute(
        "INSERT INTO CUPTI_ACTIVITY_KIND_KERNEL VALUES (4100, 4200, 21, 6, ?)",
        (GPID1,),
    )
    connection.commit()
    connection.close()

    _result, rows, summary = _run_parser(database, output)

    temporal = next(row for row in rows if row["source"] == K_TEMPORAL)
    assert temporal["destination"] == "tests/a.py::test_x"
    assert temporal["attribution_mode"] == "temporal_test"
    assert summary["attribution_temporal_test"] == 1


def test_ambiguous_temporal_range_falls_back_to_job(trace_files):
    database, output = trace_files
    child_tid = (PID1 << 24) | 19
    concurrent_tid = (PID1 << 24) | 20
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO NVTX_EVENTS VALUES "
        "(3500, 4500, ?, 'citest::tests/b.py::test_concurrent', NULL)",
        (concurrent_tid,),
    )
    connection.execute(
        "INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES (4000, 4010, 21, ?)",
        (child_tid,),
    )
    connection.execute(
        "INSERT INTO CUPTI_ACTIVITY_KIND_KERNEL VALUES (4100, 4200, 21, 6, ?)",
        (GPID1,),
    )
    connection.commit()
    connection.close()

    _result, rows, summary = _run_parser(database, output)

    fallback = next(row for row in rows if row["source"] == K_TEMPORAL)
    assert fallback["destination_kind"] == "job"
    assert fallback["attribution_mode"] == "job_union"
    assert summary["ambiguous_active_test_ranges"] == 1


def test_repeated_launches_are_deduplicated_as_a_set(trace_files):
    database, output = trace_files
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES (2520, 2530, 15, ?)",
        (TID1,),
    )
    connection.execute(
        "INSERT INTO CUPTI_ACTIVITY_KIND_KERNEL VALUES (2800, 2900, 15, 1, ?)",
        (GPID1,),
    )
    connection.commit()
    connection.close()

    _result, rows, summary = _run_parser(database, output)

    assert len([row for row in rows if row["source"] == K_INNER]) == 1
    assert summary["kernel_rows"] == 5


def test_process_scoped_correlation_ids_do_not_cross_attribute(trace_files):
    database, output = trace_files
    tid2 = (PID2 << 24) | 9
    gpid2 = PID2 << 24
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO NVTX_EVENTS VALUES "
        "(2000, 3000, ?, 'citest::tests/b.py::test_p2', NULL)",
        (tid2,),
    )
    connection.execute(
        "INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES (2400, 2410, 12, ?)",
        (tid2,),
    )
    connection.execute(
        "INSERT INTO CUPTI_ACTIVITY_KIND_KERNEL VALUES (2650, 2750, 12, 5, ?)",
        (gpid2,),
    )
    connection.commit()
    connection.close()

    _result, rows, summary = _run_parser(database, output)

    by_kernel = {row["source"]: row for row in rows}
    assert by_kernel[K_PROC2]["destination"] == "tests/b.py::test_p2"
    assert by_kernel[K_INNER]["destination"] == "tests/a.py::test_x_inner"
    assert summary["process_scoped_join"] is True


def test_duplicate_correlation_in_one_process_fails_without_overwriting(trace_files):
    database, output = trace_files
    output.write_text("last-good\n", encoding="utf-8")
    connection = sqlite3.connect(database)
    connection.execute(
        "INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES (2400, 2410, 12, ?)",
        (TID1,),
    )
    connection.commit()
    connection.close()

    result, _rows, _summary = _run_parser(database, output, check=False)

    assert result.returncode != 0
    assert output.read_text() == "last-good\n"


def test_versioned_cuda_api_alias_is_soundly_deduplicated(trace_files):
    database, output = trace_files
    connection = sqlite3.connect(database)
    connection.execute("ALTER TABLE CUPTI_ACTIVITY_KIND_RUNTIME ADD COLUMN nameId INT")
    connection.execute(
        "ALTER TABLE CUPTI_ACTIVITY_KIND_RUNTIME ADD COLUMN callchainId INT"
    )
    connection.executemany(
        "INSERT INTO StringIds VALUES (?, ?)",
        [(30, "cudaLaunchKernel"), (31, "cudaLaunchKernel_v7000")],
    )
    connection.execute(
        "UPDATE CUPTI_ACTIVITY_KIND_RUNTIME SET nameId=30, callchainId=7 "
        "WHERE correlationId=12"
    )
    connection.execute(
        "INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME "
        "(start, end, correlationId, globalTid, nameId, callchainId) "
        "VALUES (2501, 2509, 12, ?, 31, NULL)",
        (TID1,),
    )
    connection.commit()
    connection.close()

    _result, rows, summary = _run_parser(database, output)

    inner = next(row for row in rows if row["source"] == K_INNER)
    assert inner["destination"] == "tests/a.py::test_x_inner"
    assert summary["runtime_alias_rows_deduplicated"] == 1
