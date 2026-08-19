import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from test_selection.graph import GraphError, SCHEMA, write_checksum
from test_selection.snapshot import (
    build_snapshot_manifest,
    fetch_snapshot,
    publish_snapshot,
    select_snapshot,
)


SHA = "a" * 40
NOW = datetime.now(timezone.utc)


class MemoryStore:
    def __init__(self):
        self.objects = {}

    def head(self, key):
        item = self.objects.get(key)
        if item is None:
            return None
        return {
            "etag": item["etag"],
            "metadata": {"sha256": item["sha256"]},
            "size": len(item["data"]),
        }

    def download(self, key, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.objects[key]["data"])

    def upload(self, key, source, *, sha256, if_match=None, if_none_match=False):
        existing = self.objects.get(key)
        if if_none_match and existing is not None:
            raise GraphError("precondition failed")
        if if_match and (existing is None or existing["etag"] != if_match):
            raise GraphError("precondition failed")
        data = source.read_bytes()
        self.objects[key] = {
            "data": data,
            "etag": hashlib.md5(data).hexdigest(),  # nosec: fake S3 ETag
            "sha256": sha256,
        }


def _graph(path: Path, repository_sha: str = SHA) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    timestamp = NOW.isoformat()
    metadata = {
        "always_run": [],
        "ci_infra_revision": "b" * 40,
        "collector_sha256": "c" * 64,
        "created_at": timestamp,
        "data_through": timestamp,
        "expected_jobs": ["unit-tests"],
        "healthy_jobs": ["unit-tests"],
        "missing_jobs": [],
        "missing_reasons": {},
        "repository_sha": repository_sha,
        "schema_version": 1,
        "unhealthy_jobs": [],
        "unhealthy_reasons": {},
        "wait_results": {},
    }
    for key, value in metadata.items():
        connection.execute(
            "INSERT INTO metadata VALUES (?, ?)", (key, json.dumps(value))
        )
    connection.execute("INSERT INTO jobs VALUES ('unit-tests', 'healthy', NULL)")
    connection.commit()
    connection.close()
    write_checksum(path)
    return path


def _git(command, repo):
    return subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True)


def _repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["git", "init", "-q"], repo)
    _git(["git", "config", "user.name", "Test"], repo)
    _git(["git", "config", "user.email", "test@example.com"], repo)
    (repo / "file").write_text("base\n", encoding="utf-8")
    _git(["git", "add", "."], repo)
    _git(["git", "commit", "-qm", "base"], repo)
    return repo, _git(["git", "rev-parse", "HEAD"], repo).stdout.strip()


def test_publish_and_fetch_round_trip(tmp_path: Path):
    repo, repository_sha = _repo(tmp_path)
    graph = _graph(tmp_path / "graph.sqlite", repository_sha)
    manifest = tmp_path / "manifest.json"
    build_snapshot_manifest(graph, manifest)
    store = MemoryStore()

    entry = publish_snapshot(store, "test-selection/vllm", graph, manifest)
    output = tmp_path / "downloaded.sqlite"
    fetched = fetch_snapshot(
        store,
        "test-selection/vllm",
        repo,
        repository_sha,
        output,
        max_age_days=1,
    )

    assert fetched == entry
    assert output.read_bytes() == graph.read_bytes()
    assert output.with_suffix(".sqlite.sha256").is_file()


def test_publish_rejects_conflicting_immutable_graph(tmp_path: Path):
    graph = _graph(tmp_path / "graph.sqlite")
    manifest = tmp_path / "manifest.json"
    build_snapshot_manifest(graph, manifest)
    store = MemoryStore()
    publish_snapshot(store, "test-selection/vllm", graph, manifest)
    key = f"test-selection/vllm/snapshots/{SHA}/graph.sqlite"
    store.objects[key]["data"] += b"corrupt"

    with pytest.raises(GraphError, match="immutable"):
        publish_snapshot(store, "test-selection/vllm", graph, manifest)


def test_fetch_rejects_corrupt_manifest(tmp_path: Path):
    repo, repository_sha = _repo(tmp_path)
    graph = _graph(tmp_path / "graph.sqlite", repository_sha)
    manifest = tmp_path / "manifest.json"
    build_snapshot_manifest(graph, manifest)
    store = MemoryStore()
    entry = publish_snapshot(store, "test-selection/vllm", graph, manifest)
    store.objects[entry["manifest_key"]]["data"] += b"corrupt"

    with pytest.raises(GraphError, match="manifest object checksum"):
        fetch_snapshot(
            store,
            "test-selection/vllm",
            repo,
            repository_sha,
            tmp_path / "out.sqlite",
        )


def test_select_snapshot_requires_fresh_ancestor(tmp_path: Path):
    repo, repository_sha = _repo(tmp_path)
    fresh = {
        "created_at": NOW.isoformat(),
        "data_through": NOW.isoformat(),
        "manifest_key": "manifest.json",
        "repository_sha": repository_sha,
    }
    stale = {
        **fresh,
        "created_at": (NOW - timedelta(days=20)).isoformat(),
        "data_through": (NOW - timedelta(days=20)).isoformat(),
    }

    assert select_snapshot({"snapshots": [stale, fresh]}, repo, repository_sha) == fresh
    with pytest.raises(GraphError, match="no fresh ancestral"):
        select_snapshot({"snapshots": [stale]}, repo, repository_sha)
