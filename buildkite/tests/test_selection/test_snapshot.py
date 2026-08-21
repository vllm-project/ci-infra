import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import test_selection.snapshot as snapshot
from test_selection.graph import GraphError, SCHEMA, write_checksum
from test_selection.snapshot import (
    build_snapshot_manifest,
    fetch_snapshot,
    promote_snapshot,
    publish_built_graph,
    publish_snapshot,
    select_snapshot,
)


SHA = "a" * 40
NOW = datetime.now(timezone.utc)


class MemoryStore:
    def __init__(self):
        self.objects = {}
        self.upload_calls = []

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
        self.upload_calls.append(
            {
                "if_match": if_match,
                "if_none_match": if_none_match,
                "key": key,
            }
        )
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
    assert json.loads(manifest.read_text())["schema_version"] == 2
    root = f"test-selection/vllm/snapshots/{repository_sha}"
    assert f"{root}/graph.sqlite.gz" in store.objects
    assert f"{root}/graph.sqlite" not in store.objects


def test_publish_built_graph_constructs_and_publishes_manifest(tmp_path: Path):
    graph = _graph(tmp_path / "graph.sqlite")
    store = MemoryStore()

    result = publish_built_graph(graph, store, "test-selection/vllm/canary")

    assert result["snapshot"]["repository_sha"] == SHA
    assert result["metadata"]["repository_sha"] == SHA
    manifest_key = result["snapshot"]["manifest_key"]
    manifest = json.loads(store.objects[manifest_key]["data"])
    assert manifest["collector_sha256"] == "c" * 64
    assert manifest["collector_sha256s"] == ["c" * 64]
    assert "test-selection/vllm/canary/index.json" in store.objects


def test_promote_snapshot_pins_source_and_cas_updates_production(tmp_path: Path):
    repo, repository_sha = _repo(tmp_path)
    graph = _graph(tmp_path / "graph.sqlite", repository_sha)
    store = MemoryStore()
    source_prefix = "test-selection/vllm/canary/verified"
    source = publish_built_graph(graph, store, source_prefix)

    old_graph = _graph(tmp_path / "old.sqlite", "d" * 40)
    old_manifest = tmp_path / "old-manifest.json"
    build_snapshot_manifest(old_graph, old_manifest)
    publish_snapshot(store, "test-selection/vllm", old_graph, old_manifest)

    result = promote_snapshot(
        store,
        source_prefix,
        repo,
        repository_sha,
        source["snapshot"]["manifest_sha256"],
        snapshot.sha256_file(graph),
    )

    assert result["source"] == source["snapshot"]
    assert result["snapshot"] == result["readback"]
    assert result["destination_prefix"] == "test-selection/vllm"
    assert result["graph_sha256"] == snapshot.sha256_file(graph)
    destination_root = f"test-selection/vllm/snapshots/{repository_sha}"
    assert (
        store.objects[f"{destination_root}/graph.sqlite.gz"]["data"]
        == (
            store.objects[
                f"{source_prefix}/snapshots/{repository_sha}/graph.sqlite.gz"
            ]["data"]
        )
    )
    production_index_uploads = [
        call
        for call in store.upload_calls
        if call["key"] == "test-selection/vllm/index.json"
    ]
    assert production_index_uploads[-1]["if_match"]
    assert production_index_uploads[-1]["if_none_match"] is False


@pytest.mark.parametrize(
    ("source_prefix", "manifest_sha256", "graph_sha256", "error"),
    [
        (
            "test-selection/vllm/staging/verified",
            None,
            None,
            "canary source",
        ),
        (
            "test-selection/vllm/canary/verified",
            "0" * 64,
            None,
            "source manifest checksum",
        ),
        (
            "test-selection/vllm/canary/verified",
            None,
            "0" * 64,
            "source graph checksum",
        ),
    ],
)
def test_promote_snapshot_fails_before_production_write(
    tmp_path: Path,
    source_prefix: str,
    manifest_sha256: str | None,
    graph_sha256: str | None,
    error: str,
):
    repo, repository_sha = _repo(tmp_path)
    graph = _graph(tmp_path / "graph.sqlite", repository_sha)
    store = MemoryStore()
    canonical_source = "test-selection/vllm/canary/verified"
    source = publish_built_graph(graph, store, canonical_source)
    before = set(store.objects)

    with pytest.raises(GraphError, match=error):
        promote_snapshot(
            store,
            source_prefix,
            repo,
            repository_sha,
            manifest_sha256 or source["snapshot"]["manifest_sha256"],
            graph_sha256 or snapshot.sha256_file(graph),
        )

    assert set(store.objects) == before
    assert not any(
        call["key"].startswith("test-selection/vllm/snapshots/")
        for call in store.upload_calls
    )


@pytest.mark.parametrize(
    "prefix",
    [
        "test-selection/vllm",
        "test-selection/vllm/staging",
        "custom/production",
        "test-selection/vllm/canaryish",
    ],
)
def test_publish_built_graph_rejects_non_canary_prefix(tmp_path: Path, prefix: str):
    graph = _graph(tmp_path / "graph.sqlite")

    with pytest.raises(GraphError, match="explicit canary prefix"):
        publish_built_graph(graph, MemoryStore(), prefix)


def test_snapshot_manifest_records_mixed_collector_identity(tmp_path: Path):
    graph = _graph(tmp_path / "graph.sqlite")
    collectors = ["c" * 64, "d" * 64]
    connection = sqlite3.connect(graph)
    connection.execute(
        "UPDATE metadata SET value=? WHERE key='collector_sha256'",
        (json.dumps(None),),
    )
    connection.execute(
        "INSERT INTO metadata VALUES ('collector_sha256s', ?)",
        (json.dumps(collectors),),
    )
    connection.commit()
    connection.close()
    write_checksum(graph)
    manifest_path = tmp_path / "manifest.json"

    manifest = build_snapshot_manifest(graph, manifest_path)

    assert manifest["collector_sha256"] is None
    assert manifest["collector_sha256s"] == collectors


def test_compressed_graph_is_byte_deterministic(tmp_path: Path):
    graph = _graph(tmp_path / "graph.sqlite")
    first_manifest = tmp_path / "first-manifest.json"
    second_manifest = tmp_path / "second-manifest.json"

    build_snapshot_manifest(graph, first_manifest)
    first_bytes = graph.with_suffix(".sqlite.gz").read_bytes()
    graph.with_suffix(".sqlite.gz").unlink()
    build_snapshot_manifest(graph, second_manifest)

    assert graph.with_suffix(".sqlite.gz").read_bytes() == first_bytes
    assert first_manifest.read_bytes() == second_manifest.read_bytes()


def test_compression_keeps_large_logical_graph_under_single_put_limit(
    tmp_path: Path, monkeypatch
):
    graph = _graph(tmp_path / "graph.sqlite")
    connection = sqlite3.connect(graph)
    connection.execute("CREATE TABLE padding (value BLOB NOT NULL)")
    connection.execute("INSERT INTO padding VALUES (zeroblob(2097152))")
    connection.commit()
    connection.close()
    write_checksum(graph)
    manifest = tmp_path / "manifest.json"
    build_snapshot_manifest(graph, manifest)
    compressed = graph.with_suffix(".sqlite.gz")
    assert graph.stat().st_size > 1024 * 1024
    assert compressed.stat().st_size < 1024 * 1024
    monkeypatch.setattr(snapshot, "S3_SINGLE_PUT_MAX_BYTES", 1024 * 1024)

    publish_snapshot(MemoryStore(), "test-selection/vllm", graph, manifest)


def test_publish_rejects_compressed_graph_over_single_put_limit(
    tmp_path: Path, monkeypatch
):
    graph = _graph(tmp_path / "graph.sqlite")
    manifest = tmp_path / "manifest.json"
    build_snapshot_manifest(graph, manifest)
    monkeypatch.setattr(snapshot, "S3_SINGLE_PUT_MAX_BYTES", 1)

    with pytest.raises(GraphError, match="single-PUT size limit"):
        publish_snapshot(MemoryStore(), "test-selection/vllm", graph, manifest)


def test_publish_rejects_conflicting_immutable_graph(tmp_path: Path):
    graph = _graph(tmp_path / "graph.sqlite")
    manifest = tmp_path / "manifest.json"
    build_snapshot_manifest(graph, manifest)
    store = MemoryStore()
    publish_snapshot(store, "test-selection/vllm", graph, manifest)
    key = f"test-selection/vllm/snapshots/{SHA}/graph.sqlite.gz"
    store.objects[key]["data"] += b"corrupt"

    with pytest.raises(GraphError, match="immutable"):
        publish_snapshot(store, "test-selection/vllm", graph, manifest)


def test_fetch_verifies_compressed_bytes_before_decompression(
    tmp_path: Path, monkeypatch
):
    repo, repository_sha = _repo(tmp_path)
    graph = _graph(tmp_path / "graph.sqlite", repository_sha)
    manifest = tmp_path / "manifest.json"
    build_snapshot_manifest(graph, manifest)
    store = MemoryStore()
    publish_snapshot(store, "test-selection/vllm", graph, manifest)
    key = f"test-selection/vllm/snapshots/{repository_sha}/graph.sqlite.gz"
    corrupt = bytearray(store.objects[key]["data"])
    corrupt[-1] ^= 1
    store.objects[key]["data"] = bytes(corrupt)
    monkeypatch.setattr(
        snapshot,
        "_decompress_graph",
        lambda *_args, **_kwargs: pytest.fail("unverified object was decompressed"),
    )

    with pytest.raises(GraphError, match="compressed graph checksum mismatch"):
        fetch_snapshot(
            store,
            "test-selection/vllm",
            repo,
            repository_sha,
            tmp_path / "out.sqlite",
        )


def test_decompression_verifies_logical_checksum(tmp_path: Path):
    graph = _graph(tmp_path / "graph.sqlite")
    manifest = tmp_path / "manifest.json"
    build_snapshot_manifest(graph, manifest)

    with pytest.raises(GraphError, match="decompressed snapshot graph checksum"):
        snapshot._decompress_graph(
            graph.with_suffix(".sqlite.gz"),
            tmp_path / "out.sqlite",
            expected_bytes=graph.stat().st_size,
            expected_sha256="0" * 64,
        )


def test_fetch_supports_legacy_uncompressed_manifest(tmp_path: Path):
    repo, repository_sha = _repo(tmp_path)
    graph = _graph(tmp_path / "graph.sqlite", repository_sha)
    generated = tmp_path / "generated-manifest.json"
    manifest = tmp_path / "manifest.json"
    document = build_snapshot_manifest(graph, generated)
    document["schema_version"] = 1
    document["files"].pop("graph.sqlite.gz")
    document["files"]["graph.sqlite"] = {
        "bytes": graph.stat().st_size,
        "sha256": snapshot.sha256_file(graph),
    }
    manifest.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    store = MemoryStore()
    entry = publish_snapshot(store, "test-selection/vllm", graph, manifest)

    output = tmp_path / "legacy.sqlite"
    assert (
        fetch_snapshot(
            store,
            "test-selection/vllm",
            repo,
            repository_sha,
            output,
        )
        == entry
    )
    assert output.read_bytes() == graph.read_bytes()


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
