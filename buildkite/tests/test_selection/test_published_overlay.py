import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from test_selection import published_overlay as MODULE
from test_selection.graph import (
    GraphError,
    build_fleet_graph,
    graph_metadata,
    paths_to_jobs,
    write_checksum,
)

SHA = "a" * 40
BASE_COLLECTOR = "b" * 64
RETRY_COLLECTOR = "e" * 64
BASE_BUILD = "11111111-1111-4111-8111-111111111111"
RETRY_BUILD = "22222222-2222-4222-8222-222222222222"
SECOND_RETRY_BUILD = "33333333-3333-4333-8333-333333333333"
SECOND_BASE_COLLECTOR = "d" * 64


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, list):
        text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in value)
    else:
        text = json.dumps(value, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")


def _job(
    root: Path,
    key: str,
    *,
    healthy: bool = True,
    collector: str = BASE_COLLECTOR,
    source: str,
) -> None:
    directory = root / key / "0"
    trace = directory / "commands/000/python-trace.jsonl"
    _write(
        trace,
        [
            {
                "file": source,
                "job_key": key,
                "line": 7,
                "repository_sha": SHA,
                "test_id": f"tests/{key}.py::test_case",
            }
        ],
    )
    _write(
        directory / "commands/000/job.json",
        {
            "created_at": "2026-08-20T00:00:00+00:00",
            "failure_reason": None if healthy else "collector_unhealthy",
            "healthy": healthy,
            "node_ids": [f"tests/{key}.py::test_case"],
            "pytest_invocations_exported": 1,
            "pytest_invocations_started": 1,
            "pytest_node_exports_complete": True,
            "python_trace": trace.name,
            "python_trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
            "repository_sha": SHA,
            "represented_job_key": key,
            "retry_count": 0,
        },
    )
    _write(
        directory / "trace-job.json",
        {
            "capture_mode": "python-only",
            "collector_sha256": collector,
            "created_at": "2026-08-20T00:00:00+00:00",
            "failure_reason": None if healthy else "collector_unhealthy",
            "healthy": healthy,
            "parallel_job": 0,
            "parallel_job_count": 1,
            "repository_sha": SHA,
            "represented_job_key": key,
            "retry_count": 0,
        },
    )


def _inventory(path: Path, keys, collector, wait_results) -> Path:
    _write(
        path,
        {
            "always_run": [],
            "ci_infra_revision": "c" * 40,
            "collector_sha256": collector,
            "jobs": [
                {"expected_shards": 1, "key": key, "mode": "python-only"}
                for key in keys
            ],
            "repository_sha": SHA,
            "wait_results": wait_results,
        },
    )
    return path


def _fixture(tmp_path: Path):
    base = tmp_path / "base"
    retry = tmp_path / "retry"
    _job(base, "stable", source="vllm/stable.py")
    _job(base, "fixed", healthy=False, source="vllm/old.py")
    _job(
        retry,
        "fixed",
        collector=RETRY_COLLECTOR,
        source="vllm/fixed.py",
    )
    base_inventory = _inventory(
        tmp_path / "base-inventory.json",
        ["stable", "fixed", "deferred"],
        BASE_COLLECTOR,
        {
            "stable": {"status": "terminal"},
            "fixed": {"status": "terminal"},
            "deferred": {"status": "poll_timeout"},
        },
    )
    retry_inventory = _inventory(
        tmp_path / "retry-inventory.json",
        ["fixed", "deferred"],
        RETRY_COLLECTOR,
        {
            "fixed": {"status": "terminal"},
            "deferred": {"status": "poll_timeout"},
        },
    )
    base_graph = tmp_path / "base.sqlite"
    build_fleet_graph(base, base_inventory, base_graph)
    return base_graph, base_inventory, retry, retry_inventory


def _overlay(tmp_path: Path, **changes):
    base_graph, base_inventory, retry, retry_inventory = _fixture(tmp_path)
    arguments = {
        "base_source_build_id": BASE_BUILD,
        "retry_source_build_id": RETRY_BUILD,
        "merge_revision": "d" * 40,
        "base_manifest_key": "canary/snapshots/a/manifest.json",
        "base_manifest_sha256": "f" * 64,
        "expected_base_healthy_count": 1,
        "expected_base_missing_count": 1,
        "expected_base_unhealthy_count": 1,
        "expected_replacements": ["fixed"],
        "expected_retry_missing": ["deferred"],
    }
    arguments.update(changes)
    output = tmp_path / "merged.sqlite"
    provenance = tmp_path / "provenance.json"
    result = MODULE.overlay_published_graph(
        base_graph,
        base_inventory,
        retry,
        retry_inventory,
        output,
        provenance,
        **arguments,
    )
    return result, output, provenance


def test_overlay_preserves_base_and_replaces_only_materialized_retry(tmp_path: Path):
    result, output, provenance = _overlay(tmp_path)

    assert result["metadata"]["healthy_jobs"] == ["fixed", "stable"]
    assert result["metadata"]["missing_jobs"] == ["deferred"]
    assert result["metadata"]["unhealthy_jobs"] == []
    assert paths_to_jobs(output, "vllm/stable.py")[0][-1]["name"] == "stable"
    assert paths_to_jobs(output, "vllm/fixed.py")[0][-1]["name"] == "fixed"
    assert paths_to_jobs(output, "vllm/old.py") == []
    document = json.loads(provenance.read_text(encoding="utf-8"))
    assert document["replacement_jobs"] == ["fixed"]
    assert document["base_healthy_evidence_sha256"]["stable"]
    assert document["replacement_evidence_sha256"]["fixed"]
    assert document["base_manifest_sha256"] == "f" * 64
    assert graph_metadata(output) == result["metadata"]


def test_overlay_rejects_retry_attempt_to_replace_healthy_base(tmp_path: Path):
    base_graph, base_inventory, retry, _retry_inventory = _fixture(tmp_path)
    _job(
        retry,
        "stable",
        collector=RETRY_COLLECTOR,
        source="vllm/retry-stable.py",
    )
    retry_inventory = _inventory(
        tmp_path / "retry-stable-inventory.json",
        ["stable"],
        RETRY_COLLECTOR,
        {"stable": {"status": "terminal"}},
    )
    with pytest.raises(GraphError, match="missing or unhealthy"):
        MODULE.overlay_published_graph(
            base_graph,
            base_inventory,
            retry,
            retry_inventory,
            tmp_path / "merged.sqlite",
            tmp_path / "provenance.json",
            base_source_build_id=BASE_BUILD,
            retry_source_build_id=RETRY_BUILD,
            merge_revision="d" * 40,
            base_manifest_key="canary/snapshots/a/manifest.json",
            base_manifest_sha256="f" * 64,
            expected_base_healthy_count=1,
            expected_base_missing_count=1,
            expected_base_unhealthy_count=1,
            expected_replacements=["stable"],
            expected_retry_missing=[],
        )


def test_overlay_fails_closed_on_base_accounting_mismatch(tmp_path: Path):
    with pytest.raises(GraphError, match="accounting mismatch"):
        _overlay(tmp_path, expected_base_healthy_count=2)


def test_overlay_accepts_exact_mixed_collector_base(tmp_path: Path):
    base_graph, base_inventory, retry, retry_inventory = _fixture(tmp_path)
    inventory = json.loads(base_inventory.read_text(encoding="utf-8"))
    inventory["collector_sha256"] = None
    inventory["collector_sha256s"] = [BASE_COLLECTOR, SECOND_BASE_COLLECTOR]
    _write(base_inventory, inventory)
    with sqlite3.connect(base_graph) as connection:
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='collector_sha256'",
            (json.dumps(None),),
        )
        connection.execute(
            "UPDATE metadata SET value=? WHERE key='collector_sha256s'",
            (json.dumps([BASE_COLLECTOR, SECOND_BASE_COLLECTOR]),),
        )
    write_checksum(base_graph)

    result = MODULE.overlay_published_graph(
        base_graph,
        base_inventory,
        retry,
        retry_inventory,
        tmp_path / "merged.sqlite",
        tmp_path / "provenance.json",
        base_source_build_id=BASE_BUILD,
        retry_source_build_id=RETRY_BUILD,
        merge_revision="d" * 40,
        base_manifest_key="production/snapshots/a/m-f/manifest.json",
        base_manifest_sha256="f" * 64,
        expected_base_healthy_count=1,
        expected_base_missing_count=1,
        expected_base_unhealthy_count=1,
        expected_replacements=["fixed"],
        expected_retry_missing=["deferred"],
    )

    assert result["provenance"]["base_collector_sha256"] is None
    assert result["provenance"]["base_collector_sha256s"] == [
        BASE_COLLECTOR,
        SECOND_BASE_COLLECTOR,
    ]
    assert result["metadata"]["collector_sha256s"] == [
        BASE_COLLECTOR,
        SECOND_BASE_COLLECTOR,
        RETRY_COLLECTOR,
    ]


def test_overlay_records_exact_policy_downgrade_and_source_map(tmp_path: Path):
    base_graph, base_inventory, retry, retry_inventory = _fixture(tmp_path)
    inventory = json.loads(base_inventory.read_text(encoding="utf-8"))
    next(row for row in inventory["jobs"] if row["key"] == "fixed")["mode"] = (
        "kernel-set"
    )
    _write(base_inventory, inventory)

    result = MODULE.overlay_published_graph(
        base_graph,
        base_inventory,
        retry,
        retry_inventory,
        tmp_path / "merged.sqlite",
        tmp_path / "provenance.json",
        base_source_build_id=BASE_BUILD,
        retry_source_builds={
            "fixed": RETRY_BUILD,
            "deferred": SECOND_RETRY_BUILD,
        },
        merge_revision="d" * 40,
        base_manifest_key="production/snapshots/a/m-f/manifest.json",
        base_manifest_sha256="f" * 64,
        expected_base_healthy_count=1,
        expected_base_missing_count=1,
        expected_base_unhealthy_count=1,
        expected_replacements=["fixed"],
        expected_retry_missing=["deferred"],
        expected_policy_downgrades=["fixed"],
    )

    assert result["provenance"]["policy_downgrade_jobs"] == ["fixed"]
    assert result["provenance"]["retry_source_build_id"] is None
    assert result["provenance"]["retry_source_builds"] == {
        "deferred": SECOND_RETRY_BUILD,
        "fixed": RETRY_BUILD,
    }


def test_overlay_rejects_unapproved_policy_change(tmp_path: Path):
    base_graph, base_inventory, retry, retry_inventory = _fixture(tmp_path)
    inventory = json.loads(base_inventory.read_text(encoding="utf-8"))
    next(row for row in inventory["jobs"] if row["key"] == "fixed")["mode"] = (
        "kernel-set"
    )
    _write(base_inventory, inventory)

    with pytest.raises(GraphError, match="policy changes are not exact"):
        MODULE.overlay_published_graph(
            base_graph,
            base_inventory,
            retry,
            retry_inventory,
            tmp_path / "merged.sqlite",
            tmp_path / "provenance.json",
            base_source_build_id=BASE_BUILD,
            retry_source_build_id=RETRY_BUILD,
            merge_revision="d" * 40,
            base_manifest_key="production/snapshots/a/m-f/manifest.json",
            base_manifest_sha256="f" * 64,
            expected_base_healthy_count=1,
            expected_base_missing_count=1,
            expected_base_unhealthy_count=1,
            expected_replacements=["fixed"],
            expected_retry_missing=["deferred"],
        )
