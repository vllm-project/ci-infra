import hashlib
import json
from pathlib import Path

import pytest
from test_selection import published_overlay as MODULE
from test_selection.graph import (
    GraphError,
    build_fleet_graph,
    graph_metadata,
    paths_to_jobs,
)

SHA = "a" * 40
BASE_COLLECTOR = "b" * 64
RETRY_COLLECTOR = "e" * 64
BASE_BUILD = "11111111-1111-4111-8111-111111111111"
RETRY_BUILD = "22222222-2222-4222-8222-222222222222"


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
