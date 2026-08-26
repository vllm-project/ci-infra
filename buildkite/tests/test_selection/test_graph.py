import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from test_selection.graph import (
    GraphError,
    build_fleet_graph,
    current_jobs_from_pipeline,
    graph_metadata,
    job_coverage,
    merge_fleet_graph,
    paths_to_jobs,
    select_jobs,
)


SHA = "a" * 40
COLLECTOR = "b" * 64
RETRY_COLLECTOR = "e" * 64
CREATED = "2026-08-19T09:00:00+00:00"
BASE_BUILD_ID = "11111111-1111-4111-8111-111111111111"
RETRY_BUILD_ID = "22222222-2222-4222-8222-222222222222"


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
    shard: int = 0,
    failure_reason: str | None = None,
    retry_count: int = 0,
    attempt_scoped: bool = False,
    source: str = "vllm/model.py",
    collector_sha256: str = COLLECTOR,
) -> None:
    directory = root / key
    if attempt_scoped:
        directory /= f"attempt-{retry_count}"
    directory /= str(shard)
    trace = directory / "commands/000/python-trace.jsonl"
    rows = [
        {
            "file": source,
            "job_key": key,
            "line": 7,
            "repository_sha": SHA,
            "test_id": "tests/test_model.py::test_forward",
        }
    ]
    _write(trace, rows)
    _write(
        directory / "commands/000/job.json",
        {
            "created_at": CREATED,
            "failure_reason": failure_reason,
            "healthy": healthy,
            "node_ids": ["tests/test_model.py::test_forward"],
            "pytest_invocations_exported": 1,
            "pytest_invocations_started": 1,
            "pytest_node_exports_complete": True,
            "python_trace": trace.name,
            "python_trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
            "repository_sha": SHA,
            "represented_job_key": key,
            "retry_count": retry_count,
        },
    )
    _write(
        directory / "trace-job.json",
        {
            "capture_mode": "python-only",
            "collector_sha256": collector_sha256,
            "created_at": CREATED,
            "failure_reason": failure_reason,
            "healthy": healthy,
            "parallel_job": shard,
            "parallel_job_count": 1,
            "repository_sha": SHA,
            "represented_job_key": key,
            "retry_count": retry_count,
        },
    )


def _inventory(
    path: Path,
    *keys: str,
    wait_results: dict | None = None,
    collector_sha256: str = COLLECTOR,
) -> Path:
    _write(
        path,
        {
            "always_run": [],
            "ci_infra_revision": "c" * 40,
            "collector_sha256": collector_sha256,
            "jobs": [
                {"expected_shards": 1, "key": key, "mode": "python-only"}
                for key in keys
            ],
            "repository_sha": SHA,
            "wait_results": wait_results or {},
        },
    )
    return path


def _graph(tmp_path: Path, *keys: str) -> Path:
    evidence = tmp_path / "evidence"
    for key in keys:
        _job(evidence, key)
    graph = tmp_path / "graph.sqlite"
    build_fleet_graph(evidence, _inventory(tmp_path / "inventory.json", *keys), graph)
    return graph


def _git(command: list[str], repo: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True)


def _repo(tmp_path: Path, *, unknown: bool = False) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["git", "init", "-q"], repo)
    _git(["git", "config", "user.name", "Test"], repo)
    _git(["git", "config", "user.email", "test@example.com"], repo)
    path = repo / "vllm/model.py"
    path.parent.mkdir()
    path.write_text("VALUE = 1\n", encoding="utf-8")
    _git(["git", "add", "."], repo)
    _git(["git", "commit", "-qm", "base"], repo)
    if unknown:
        (repo / "unknown.txt").write_text("new\n", encoding="utf-8")
    else:
        path.write_text("VALUE = 2\n", encoding="utf-8")
    _git(["git", "add", "."], repo)
    _git(["git", "commit", "-qm", "head"], repo)
    return repo


def test_builds_compact_graph_and_explains_file_to_job(tmp_path: Path):
    graph = _graph(tmp_path, "unit-tests")

    assert graph_metadata(graph)["healthy_jobs"] == ["unit-tests"]
    assert paths_to_jobs(graph, "vllm/model.py")[0][-1]["name"] == "unit-tests"


def test_missing_job_is_recorded_and_always_runs(tmp_path: Path):
    evidence = tmp_path / "evidence"
    _job(evidence, "healthy")
    graph = tmp_path / "graph.sqlite"
    inventory = _inventory(tmp_path / "inventory.json", "healthy", "missing")

    metadata = build_fleet_graph(evidence, inventory, graph)
    current = tmp_path / "jobs.json"
    current.write_text('["healthy","missing"]\n', encoding="utf-8")

    assert metadata["missing_jobs"] == ["missing"]
    assert job_coverage(graph, current)["uncovered_step_keys"] == ["missing"]


def test_unhealthy_summary_preserves_specific_failure_reason(tmp_path: Path):
    evidence = tmp_path / "evidence"
    _job(evidence, "healthy")
    _job(
        evidence,
        "import-failed",
        healthy=False,
        failure_reason="collector_import_failed",
    )

    metadata = build_fleet_graph(
        evidence,
        _inventory(tmp_path / "inventory.json", "healthy", "import-failed"),
        tmp_path / "graph.sqlite",
    )

    assert metadata["unhealthy_reasons"] == {"import-failed": "collector_import_failed"}


def test_materializer_uses_latest_automatic_retry_attempt(tmp_path: Path):
    evidence = tmp_path / "evidence"
    _job(
        evidence,
        "retried",
        healthy=False,
        failure_reason="collector_unhealthy",
        retry_count=0,
        attempt_scoped=True,
    )
    _job(
        evidence,
        "retried",
        retry_count=1,
        attempt_scoped=True,
    )

    metadata = build_fleet_graph(
        evidence,
        _inventory(tmp_path / "inventory.json", "retried"),
        tmp_path / "graph.sqlite",
    )

    assert metadata["healthy_jobs"] == ["retried"]
    assert metadata["unhealthy_jobs"] == []


def test_materializer_latest_invalid_retry_fails_closed(tmp_path: Path):
    evidence = tmp_path / "evidence"
    _job(evidence, "healthy-control")
    _job(evidence, "retried", retry_count=0, attempt_scoped=True)
    _job(evidence, "retried", retry_count=1, attempt_scoped=True)
    summary_path = evidence / "retried/attempt-1/0/trace-job.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["repository_sha"] = "d" * 40
    _write(summary_path, summary)

    metadata = build_fleet_graph(
        evidence,
        _inventory(tmp_path / "inventory.json", "healthy-control", "retried"),
        tmp_path / "graph.sqlite",
    )

    assert metadata["healthy_jobs"] == ["healthy-control"]
    assert metadata["unhealthy_reasons"] == {"retried": "invalid_summary"}


def test_terminal_job_without_artifact_is_not_reported_as_poll_timeout(tmp_path: Path):
    evidence = tmp_path / "evidence"
    _job(evidence, "healthy")
    inventory = _inventory(
        tmp_path / "inventory.json",
        "healthy",
        "terminal-missing",
        wait_results={
            "healthy": {"status": "terminal"},
            "terminal-missing": {"status": "terminal"},
        },
    )

    metadata = build_fleet_graph(evidence, inventory, tmp_path / "graph.sqlite")

    assert metadata["missing_reasons"] == {"terminal-missing": "terminal_missing_shard"}


def test_merge_fleet_graph_replaces_only_healthy_retry_jobs(tmp_path: Path):
    base = tmp_path / "base"
    retry = tmp_path / "retry"
    _job(base, "stable", source="vllm/stable.py")
    _job(base, "fixed", healthy=False, failure_reason="collector_unhealthy")
    _job(base, "still-red", healthy=False, failure_reason="collector_unhealthy")
    _job(
        retry,
        "fixed",
        source="vllm/fixed.py",
        collector_sha256=RETRY_COLLECTOR,
    )
    _job(
        retry,
        "still-red",
        healthy=False,
        failure_reason="collector_unhealthy",
        source="vllm/retry-red.py",
        collector_sha256=RETRY_COLLECTOR,
    )
    base_inventory = _inventory(
        tmp_path / "base-inventory.json",
        "stable",
        "fixed",
        "still-red",
        wait_results={
            key: {"status": "terminal"} for key in ("stable", "fixed", "still-red")
        },
    )
    retry_inventory = _inventory(
        tmp_path / "retry-inventory.json",
        "fixed",
        "still-red",
        wait_results={key: {"status": "terminal"} for key in ("fixed", "still-red")},
        collector_sha256=RETRY_COLLECTOR,
    )
    graph = tmp_path / "merged.sqlite"
    provenance = tmp_path / "merge-provenance.json"

    result = merge_fleet_graph(
        base,
        base_inventory,
        retry,
        retry_inventory,
        graph,
        provenance,
        base_source_build_id=BASE_BUILD_ID,
        retry_source_build_id=RETRY_BUILD_ID,
        merge_revision="d" * 40,
    )

    assert result["metadata"]["healthy_jobs"] == ["fixed", "stable"]
    assert result["metadata"]["unhealthy_jobs"] == ["still-red"]
    assert result["metadata"]["collector_sha256"] is None
    assert result["metadata"]["collector_sha256s"] == [COLLECTOR, RETRY_COLLECTOR]
    assert paths_to_jobs(graph, "vllm/fixed.py")[0][-1]["name"] == "fixed"
    assert paths_to_jobs(graph, "vllm/stable.py")[0][-1]["name"] == "stable"
    assert paths_to_jobs(graph, "vllm/retry-red.py") == []
    provenance_document = json.loads(provenance.read_text(encoding="utf-8"))
    assert provenance_document["base_source_build_id"] == BASE_BUILD_ID
    assert provenance_document["retry_source_build_id"] == RETRY_BUILD_ID
    assert provenance_document["base_collector_sha256"] == COLLECTOR
    assert provenance_document["retry_collector_sha256"] == RETRY_COLLECTOR
    assert provenance_document["collector_sha256s"] == [COLLECTOR, RETRY_COLLECTOR]
    assert provenance_document["merge_revision"] == "d" * 40
    assert (
        provenance_document["merged_graph_sha256"]
        == hashlib.sha256(graph.read_bytes()).hexdigest()
    )
    assert provenance_document["replacement_jobs"] == ["fixed"]


def test_merge_fleet_graph_requires_distinct_canonical_source_build_ids(
    tmp_path: Path,
):
    with pytest.raises(GraphError, match="must differ"):
        merge_fleet_graph(
            tmp_path / "base",
            tmp_path / "base-inventory.json",
            tmp_path / "retry",
            tmp_path / "retry-inventory.json",
            tmp_path / "merged.sqlite",
            tmp_path / "provenance.json",
            base_source_build_id=BASE_BUILD_ID,
            retry_source_build_id=BASE_BUILD_ID,
            merge_revision="d" * 40,
        )


def test_materializer_rejects_incomplete_pytest_node_exports(tmp_path: Path):
    evidence = tmp_path / "evidence"
    _job(evidence, "unit-tests")
    _job(evidence, "healthy-control")
    manifest_path = evidence / "unit-tests/0/commands/000/job.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "pytest_invocations_exported": 1,
            "pytest_invocations_started": 2,
            "pytest_node_exports_complete": False,
        }
    )
    _write(manifest_path, manifest)

    metadata = build_fleet_graph(
        evidence,
        _inventory(tmp_path / "inventory.json", "unit-tests", "healthy-control"),
        tmp_path / "graph.sqlite",
    )

    assert metadata["healthy_jobs"] == ["healthy-control"]
    assert metadata["unhealthy_reasons"] == {"unit-tests": "GraphError"}


def _rewrite_manifest_and_trace(evidence: Path, key: str, **manifest_updates) -> None:
    """Reshape an evidence job as a serve job: job::-only nodes, zero pytest
    invocations, subprocess hook installed, trace rows carrying the job-level
    test id."""
    command_dir = evidence / key / "0/commands/000"
    node_id = f"job::{key}"
    trace = command_dir / "python-trace.jsonl"
    _write(
        trace,
        [
            {
                "file": "vllm/model.py",
                "job_key": key,
                "line": 7,
                "repository_sha": SHA,
                "test_id": node_id,
            }
        ],
    )
    manifest_path = command_dir / "job.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "node_ids": [node_id],
            "pytest_invocations_exported": 0,
            "pytest_invocations_started": 0,
            "pytest_node_exports_complete": True,
            "python_trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
            "subprocess_hook": {"installed": True},
        }
    )
    manifest.update(manifest_updates)
    _write(manifest_path, manifest)


def _inventory_with_serve(path: Path, *keys: str, serve: tuple = (),
                          serverless: tuple = ()) -> Path:
    inventory = _inventory(path, *keys)
    document = json.loads(inventory.read_text(encoding="utf-8"))
    for job in document["jobs"]:
        if job["key"] in serve:
            job["capture_class"] = "serve"
        elif job["key"] in serverless:
            job["capture_class"] = "serverless"
    _write(inventory, document)
    return inventory


def test_materializer_accepts_declared_serve_job_level_evidence(tmp_path: Path):
    evidence = tmp_path / "evidence"
    _job(evidence, "serve-job")
    _rewrite_manifest_and_trace(evidence, "serve-job")

    metadata = build_fleet_graph(
        evidence,
        _inventory_with_serve(
            tmp_path / "inventory.json", "serve-job", serve=("serve-job",)
        ),
        tmp_path / "graph.sqlite",
    )

    assert metadata["healthy_jobs"] == ["serve-job"]


def test_materializer_rejects_undeclared_zero_invocation_manifest(tmp_path: Path):
    """Same manifest shape, but the inventory does NOT declare the serve
    class: a pytest-expected job that silently lost its plugin must still
    fail closed."""
    evidence = tmp_path / "evidence"
    _job(evidence, "unit-tests")
    _job(evidence, "healthy-control")
    _rewrite_manifest_and_trace(evidence, "unit-tests")

    metadata = build_fleet_graph(
        evidence,
        _inventory(tmp_path / "inventory.json", "unit-tests", "healthy-control"),
        tmp_path / "graph.sqlite",
    )

    assert metadata["healthy_jobs"] == ["healthy-control"]
    assert metadata["unhealthy_reasons"] == {"unit-tests": "GraphError"}


def test_materializer_rejects_serve_class_without_hook(tmp_path: Path):
    """Declared serve class but the subprocess hook never installed: the
    manifest does not carry the declaration's runtime proof."""
    evidence = tmp_path / "evidence"
    _job(evidence, "serve-job")
    _job(evidence, "healthy-control")
    _rewrite_manifest_and_trace(evidence, "serve-job", subprocess_hook={"installed": False})

    metadata = build_fleet_graph(
        evidence,
        _inventory_with_serve(
            tmp_path / "inventory.json",
            "serve-job",
            "healthy-control",
            serve=("serve-job",),
        ),
        tmp_path / "graph.sqlite",
    )

    assert metadata["healthy_jobs"] == ["healthy-control"]
    assert metadata["unhealthy_reasons"] == {"serve-job": "GraphError"}


def test_materializer_accepts_declared_serverless_job_level_evidence(tmp_path: Path):
    evidence = tmp_path / "evidence"
    _job(evidence, "examples")
    _rewrite_manifest_and_trace(evidence, "examples")

    metadata = build_fleet_graph(
        evidence,
        _inventory_with_serve(
            tmp_path / "inventory.json", "examples", serverless=("examples",)
        ),
        tmp_path / "graph.sqlite",
    )

    assert metadata["healthy_jobs"] == ["examples"]


def test_bad_checksum_fails_closed(tmp_path: Path):
    graph = _graph(tmp_path, "unit-tests")
    graph.write_bytes(graph.read_bytes() + b"corrupt")
    with pytest.raises(GraphError, match="checksum"):
        graph_metadata(graph)


def test_selector_maps_changed_file_and_adds_uncovered_jobs(tmp_path: Path):
    graph = _graph(tmp_path, "unit-tests")
    repo = _repo(tmp_path)
    current = tmp_path / "jobs.json"
    current.write_text('["unit-tests","new-job"]\n', encoding="utf-8")
    # Make the fixture graph's snapshot SHA the real base commit.
    base = _git(["git", "rev-parse", "HEAD~1"], repo).stdout.strip()
    import sqlite3

    connection = sqlite3.connect(graph)
    connection.execute(
        "UPDATE metadata SET value=? WHERE key='repository_sha'", (json.dumps(base),)
    )
    connection.commit()
    connection.close()
    from test_selection.graph import write_checksum

    write_checksum(graph)

    result = select_jobs(graph, repo, base, "HEAD", current, 10000)

    assert result["fallback"] is False
    assert result["step_keys"] == ["new-job", "unit-tests"]
    assert result["reasons"][0]["analysis"] == "trace_presence"


def test_selector_falls_back_for_unmapped_change(tmp_path: Path):
    graph = _graph(tmp_path, "unit-tests")
    repo = _repo(tmp_path, unknown=True)
    current = tmp_path / "jobs.json"
    current.write_text('["unit-tests"]\n', encoding="utf-8")
    base = _git(["git", "rev-parse", "HEAD~1"], repo).stdout.strip()
    import sqlite3

    connection = sqlite3.connect(graph)
    connection.execute(
        "UPDATE metadata SET value=? WHERE key='repository_sha'", (json.dumps(base),)
    )
    connection.commit()
    connection.close()
    from test_selection.graph import write_checksum

    write_checksum(graph)

    result = select_jobs(graph, repo, base, "HEAD", current, 10000)
    assert result["fallback"] is True
    assert result["fallback_reason"] == "unmapped_changed_file"


def test_reads_current_jobs_from_rendered_pipeline(tmp_path: Path):
    pipeline = tmp_path / "pipeline.yaml"
    pipeline.write_text(
        "steps:\n- group: Tests\n  steps:\n  - key: unit-tests\n    commands: [pytest]\n",
        encoding="utf-8",
    )
    assert current_jobs_from_pipeline(pipeline) == ["unit-tests"]


def _job_with_identity(root: Path, key: str, digest: str, baseline: str) -> None:
    directory = root / key / "0"
    trace = directory / "commands/000/python-trace.jsonl"
    rows = [
        {
            "file": "vllm/model.py",
            "job_key": key,
            "line": 7,
            "repository_sha": SHA,
            "test_id": "tests/test_model.py::test_forward",
        }
    ]
    _write(trace, rows)
    _write(
        directory / "commands/000/job.json",
        {
            "created_at": CREATED,
            "failure_reason": None,
            "healthy": True,
            "image_digest": digest,
            "node_ids": ["tests/test_model.py::test_forward"],
            "pytest_invocations_exported": 1,
            "pytest_invocations_started": 1,
            "pytest_node_exports_complete": True,
            "python_trace": trace.name,
            "python_trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
            "repository_sha": SHA,
            "represented_job_key": key,
            "retry_count": 0,
            "worktree_baseline_sha256": baseline,
        },
    )
    _write(
        directory / "trace-job.json",
        {
            "capture_mode": "python-only",
            "collector_sha256": COLLECTOR,
            "created_at": CREATED,
            "failure_reason": None,
            "healthy": True,
            "image_digest": digest,
            "image_digests": [digest],
            "parallel_job": 0,
            "parallel_job_count": 1,
            "repository_sha": SHA,
            "represented_job_key": key,
            "retry_count": 0,
            "worktree_baseline_sha256": baseline,
            "worktree_baseline_sha256s": [baseline],
        },
    )


def test_generation_image_identity_flows_to_metadata(tmp_path: Path):
    evidence = tmp_path / "evidence"
    for key in ("job-a", "job-b"):
        _job_with_identity(evidence, key, "sha256:" + "d" * 64, "b" * 64)
    graph = tmp_path / "graph.sqlite"
    build_fleet_graph(
        evidence, _inventory(tmp_path / "inventory.json", "job-a", "job-b"), graph
    )
    metadata = graph_metadata(graph)
    assert metadata["image_digest"] == "sha256:" + "d" * 64
    assert metadata["image_digests"] == ["sha256:" + "d" * 64]
    assert metadata["worktree_baseline_sha256"] == "b" * 64
    assert metadata["worktree_baseline_sha256s"] == ["b" * 64]


def test_mixed_image_digest_generation_fails_closed(tmp_path: Path):
    evidence = tmp_path / "evidence"
    _job_with_identity(evidence, "job-a", "sha256:" + "d" * 64, "b" * 64)
    _job_with_identity(evidence, "job-b", "sha256:" + "e" * 64, "b" * 64)
    with pytest.raises(GraphError, match="image/baseline identity disagreement"):
        build_fleet_graph(
            evidence,
            _inventory(tmp_path / "inventory.json", "job-a", "job-b"),
            tmp_path / "graph.sqlite",
        )


def test_mixed_baseline_generation_fails_closed(tmp_path: Path):
    evidence = tmp_path / "evidence"
    _job_with_identity(evidence, "job-a", "sha256:" + "d" * 64, "b" * 64)
    _job_with_identity(evidence, "job-b", "sha256:" + "d" * 64, "c" * 64)
    with pytest.raises(GraphError, match="image/baseline identity disagreement"):
        build_fleet_graph(
            evidence,
            _inventory(tmp_path / "inventory.json", "job-a", "job-b"),
            tmp_path / "graph.sqlite",
        )


def test_summary_singular_plural_disagreement_is_invalid(tmp_path: Path):
    evidence = tmp_path / "evidence"
    _job(evidence, "healthy")
    _job_with_identity(evidence, "bad", "sha256:" + "d" * 64, "b" * 64)
    bad_summary = evidence / "bad" / "0" / "trace-job.json"
    document = json.loads(bad_summary.read_text())
    document["image_digests"] = ["sha256:" + "f" * 64]  # disagrees with singular
    bad_summary.write_text(json.dumps(document))
    metadata = build_fleet_graph(
        evidence,
        _inventory(tmp_path / "inventory.json", "healthy", "bad"),
        tmp_path / "graph.sqlite",
    )
    assert metadata["healthy_jobs"] == ["healthy"]
    assert metadata["unhealthy_reasons"] == {"bad": "invalid_summary"}


def test_snapshot_manifest_carries_image_and_baseline_identity(tmp_path: Path):
    from test_selection.snapshot import build_snapshot_manifest

    evidence = tmp_path / "evidence"
    _job_with_identity(evidence, "job-a", "sha256:" + "d" * 64, "b" * 64)
    graph = tmp_path / "graph.sqlite"
    build_fleet_graph(
        evidence, _inventory(tmp_path / "inventory.json", "job-a"), graph
    )
    manifest = build_snapshot_manifest(graph, tmp_path / "manifest.json")
    assert manifest["image_digest"] == "sha256:" + "d" * 64
    assert manifest["image_digests"] == ["sha256:" + "d" * 64]
    assert manifest["worktree_baseline_sha256"] == "b" * 64
    assert manifest["worktree_baseline_sha256s"] == ["b" * 64]


def test_manifest_identity_fields_default_empty_for_legacy_jobs(tmp_path: Path):
    from test_selection.snapshot import build_snapshot_manifest

    graph = _graph(tmp_path, "unit-tests")
    manifest = build_snapshot_manifest(graph, tmp_path / "manifest.json")
    assert manifest["image_digest"] is None
    assert manifest["image_digests"] == []
    assert manifest["worktree_baseline_sha256"] is None
    assert manifest["worktree_baseline_sha256s"] == []


def _inventory_two_shard(path: Path, key: str) -> Path:
    _write(
        path,
        {
            "always_run": [],
            "ci_infra_revision": "c" * 64,
            "collector_sha256": COLLECTOR,
            "jobs": [{"expected_shards": 2, "key": key, "mode": "python-only"}],
            "repository_sha": SHA,
            "wait_results": {},
        },
    )
    return path


def test_generation_gate_is_per_shard_not_per_key(tmp_path: Path):
    # One job, two shards: shard 0 carries the pair, shard 1 is legacy.
    evidence = tmp_path / "evidence"
    _job_with_identity(evidence, "job-a", "sha256:" + "d" * 64, "b" * 64)
    _job(evidence, "job-a", shard=1)
    # Both shard summaries must claim the two-shard shape to be accepted.
    for shard_doc in (evidence / "job-a").rglob("trace-job.json"):
        document = json.loads(shard_doc.read_text())
        document["parallel_job_count"] = 2
        shard_doc.write_text(json.dumps(document))
    with pytest.raises(GraphError, match="mixes image-pinned and legacy"):
        build_fleet_graph(
            evidence,
            _inventory_two_shard(tmp_path / "inventory.json", "job-a"),
            tmp_path / "graph.sqlite",
        )


def test_plural_only_identity_is_rejected_not_synthesized():
    from test_selection.graph import image_baseline_identity

    with pytest.raises(GraphError, match="image_digest is missing"):
        image_baseline_identity(
            {
                "image_digests": ["sha256:" + "d" * 64],
                "worktree_baseline_sha256": "b" * 64,
                "worktree_baseline_sha256s": ["b" * 64],
            },
            "test",
        )
    # Singular-only remains the allowed compatibility form.
    pair = image_baseline_identity(
        {"image_digest": "sha256:" + "d" * 64, "worktree_baseline_sha256": "b" * 64},
        "test",
    )
    assert pair == ("sha256:" + "d" * 64, "b" * 64)
