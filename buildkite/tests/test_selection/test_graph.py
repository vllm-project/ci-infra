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
    paths_to_jobs,
    select_jobs,
)


SHA = "a" * 40
COLLECTOR = "b" * 64
CREATED = "2026-08-19T09:00:00+00:00"


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, list):
        text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in value)
    else:
        text = json.dumps(value, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8")


def _job(root: Path, key: str, *, healthy: bool = True, shard: int = 0) -> None:
    directory = root / key / str(shard)
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
            "healthy": healthy,
            "node_ids": ["tests/test_model.py::test_forward"],
            "python_trace": trace.name,
            "python_trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
            "repository_sha": SHA,
            "represented_job_key": key,
        },
    )
    _write(
        directory / "trace-job.json",
        {
            "capture_mode": "python-only",
            "collector_sha256": COLLECTOR,
            "created_at": CREATED,
            "healthy": healthy,
            "parallel_job": shard,
            "parallel_job_count": 1,
            "repository_sha": SHA,
            "represented_job_key": key,
        },
    )


def _inventory(path: Path, *keys: str) -> Path:
    _write(
        path,
        {
            "always_run": [],
            "ci_infra_revision": "c" * 40,
            "collector_sha256": COLLECTOR,
            "jobs": [
                {"expected_shards": 1, "key": key, "mode": "python-only"}
                for key in keys
            ],
            "repository_sha": SHA,
            "wait_results": {},
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
