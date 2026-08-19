"""Build, publish, and fetch immutable fleet trace snapshots."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from test_selection.graph import (
    GraphError,
    build_fleet_graph,
    graph_metadata,
    sha256_file,
    verify_checksum,
)


class ObjectStore(Protocol):
    def head(self, key: str) -> Optional[dict[str, Any]]: ...

    def download(self, key: str, destination: Path) -> None: ...

    def upload(
        self,
        key: str,
        source: Path,
        *,
        sha256: str,
        if_match: Optional[str] = None,
        if_none_match: bool = False,
    ) -> None: ...


class Boto3ObjectStore:
    """S3 client shared by snapshot publication and fetch."""

    def __init__(
        self,
        bucket: str,
        *,
        session=None,
        client=None,
    ):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
            raise GraphError("invalid S3 bucket name")
        self.bucket = bucket
        self.session = session or boto3.session.Session()
        self.client = client or self.session.client("s3")

    @staticmethod
    def _raise(operation: str, error: Exception) -> None:
        if isinstance(error, ClientError):
            code = str(error.response.get("Error", {}).get("Code", "unknown"))
        else:
            code = type(error).__name__
        raise GraphError(f"AWS {operation} failed: {code}") from error

    def head(self, key: str) -> Optional[dict[str, Any]]:
        try:
            document = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            self._raise("s3:HeadObject", error)
        except BotoCoreError as error:
            self._raise("s3:HeadObject", error)
        return {
            "etag": str(document.get("ETag", "")).strip('"'),
            "metadata": document.get("Metadata", {}),
            "size": document.get("ContentLength"),
        }

    def download(self, key: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        try:
            document = self.client.get_object(Bucket=self.bucket, Key=key)
            body = document["Body"]
            try:
                with temporary.open("wb") as output:
                    shutil.copyfileobj(body, output)
            finally:
                body.close()
            os.replace(temporary, destination)
        except (BotoCoreError, ClientError, KeyError, OSError) as error:
            temporary.unlink(missing_ok=True)
            if isinstance(error, (BotoCoreError, ClientError)):
                self._raise("s3:GetObject", error)
            raise GraphError("S3 get-object returned an invalid response") from error

    def upload(
        self,
        key: str,
        source: Path,
        *,
        sha256: str,
        if_match: Optional[str] = None,
        if_none_match: bool = False,
    ) -> None:
        arguments: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": key,
            "Metadata": {"sha256": sha256},
        }
        if if_match:
            arguments["IfMatch"] = if_match
        if if_none_match:
            arguments["IfNoneMatch"] = "*"
        try:
            with source.open("rb") as body:
                self.client.put_object(Body=body, **arguments)
        except (BotoCoreError, ClientError, OSError) as error:
            if isinstance(error, (BotoCoreError, ClientError)):
                self._raise("s3:PutObject", error)
            raise GraphError("S3 put-object could not read its source") from error


def _atomic_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _prefix(value: str) -> str:
    value = value.strip("/")
    if not value or not re.fullmatch(r"[a-zA-Z0-9._/-]+", value):
        raise GraphError("invalid snapshot prefix")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise GraphError("invalid snapshot prefix")
    return value


def build_snapshot_manifest(graph: Path, output: Path) -> dict[str, Any]:
    verify_checksum(graph)
    metadata = graph_metadata(graph)
    checksum = sha256_file(graph)
    document = {
        "always_run": metadata.get("always_run", []),
        "ci_infra_revision": metadata["ci_infra_revision"],
        # A retry of the same generation must produce byte-identical immutable
        # objects, so use the evidence watermark rather than wall-clock time.
        "created_at": metadata["data_through"],
        "data_through": metadata["data_through"],
        "expected_jobs": metadata["expected_jobs"],
        "files": {
            "graph.sqlite": {
                "bytes": graph.stat().st_size,
                "sha256": checksum,
            },
            "graph.sqlite.sha256": {
                "bytes": graph.with_suffix(graph.suffix + ".sha256").stat().st_size,
                "sha256": sha256_file(graph.with_suffix(graph.suffix + ".sha256")),
            },
        },
        "healthy_jobs": metadata["healthy_jobs"],
        "kind": "vllm-test-selection-snapshot",
        "missing_jobs": metadata.get("missing_jobs", []),
        "missing_reasons": metadata.get("missing_reasons", {}),
        "repository_sha": metadata["repository_sha"],
        "schema_version": 1,
        "unhealthy_jobs": metadata.get("unhealthy_jobs", []),
        "unhealthy_reasons": metadata.get("unhealthy_reasons", {}),
        "wait_results": metadata.get("wait_results", {}),
    }
    _atomic_json(output, document)
    return document


def _read_index(store: ObjectStore, key: str, directory: Path):
    path = directory / "current-index.json"
    for attempt in range(2):
        head = store.head(key)
        if head is None:
            return {"schema_version": 1, "snapshots": []}, None
        store.download(key, path)
        if head.get("size") == path.stat().st_size and head.get("metadata", {}).get(
            "sha256"
        ) == sha256_file(path):
            break
        if attempt == 1:
            raise GraphError("snapshot index object checksum mismatch")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise GraphError("snapshot index is not valid JSON") from error
    if document.get("schema_version") != 1 or not isinstance(
        document.get("snapshots"), list
    ):
        raise GraphError("unsupported snapshot index schema")
    for entry in document["snapshots"]:
        _validate_index_entry(entry, require_manifest_checksum=True)
    etag = head.get("etag")
    if not isinstance(etag, str) or not etag:
        raise GraphError("snapshot index object has no ETag")
    return document, etag


def _validate_index_entry(
    entry: Any, *, require_manifest_checksum: bool = False
) -> None:
    if not isinstance(entry, dict):
        raise GraphError("snapshot index entry is invalid")
    repository_sha = entry.get("repository_sha")
    if not isinstance(repository_sha, str) or not re.fullmatch(
        r"[0-9a-f]{40}", repository_sha
    ):
        raise GraphError("snapshot index repository SHA is invalid")
    for field in ("created_at", "data_through"):
        if not isinstance(entry.get(field), str):
            raise GraphError(f"snapshot index {field} is invalid")
        _timestamp(entry[field])
    manifest_key = entry.get("manifest_key")
    if not isinstance(manifest_key, str) or not manifest_key:
        raise GraphError("snapshot index manifest key is invalid")
    if require_manifest_checksum:
        if not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("manifest_sha256", ""))):
            raise GraphError("snapshot index manifest checksum is invalid")
        if (
            not isinstance(entry.get("manifest_bytes"), int)
            or entry["manifest_bytes"] < 1
        ):
            raise GraphError("snapshot index manifest byte count is invalid")


def _validate_file_record(document: Any, path: Path, label: str) -> None:
    if not isinstance(document, dict):
        raise GraphError(f"snapshot manifest {label} record is invalid")
    if document.get("bytes") != path.stat().st_size:
        raise GraphError(f"snapshot manifest {label} byte count mismatch")
    if document.get("sha256") != sha256_file(path):
        raise GraphError(f"snapshot manifest {label} checksum mismatch")


def _publish_immutable(
    store: ObjectStore, key: str, source: Path, checksum: str
) -> None:
    existing = store.head(key)
    if existing is not None:
        metadata = existing.get("metadata", {})
        if (
            metadata.get("sha256") != checksum
            or existing.get("size") != source.stat().st_size
        ):
            raise GraphError(
                "immutable snapshot object already exists with different bytes"
            )
        return
    try:
        store.upload(key, source, sha256=checksum, if_none_match=True)
    except GraphError:
        existing = store.head(key)
        metadata = existing.get("metadata", {}) if existing else {}
        if (
            existing is not None
            and metadata.get("sha256") == checksum
            and existing.get("size") == source.stat().st_size
        ):
            return
        raise


def publish_snapshot(
    store: ObjectStore,
    prefix: str,
    graph: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    prefix = _prefix(prefix)
    verify_checksum(graph)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise GraphError("snapshot manifest is not valid JSON") from error
    if (
        manifest.get("kind") != "vllm-test-selection-snapshot"
        or manifest.get("schema_version") != 1
    ):
        raise GraphError("unsupported snapshot manifest schema")
    repository_sha = str(manifest.get("repository_sha", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", repository_sha):
        raise GraphError("snapshot manifest repository_sha is invalid")
    metadata = graph_metadata(graph)
    if metadata["repository_sha"] != repository_sha:
        raise GraphError("snapshot graph and manifest repository SHA disagree")
    sidecar = graph.with_suffix(graph.suffix + ".sha256")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise GraphError("snapshot manifest files are invalid")
    _validate_file_record(files.get("graph.sqlite"), graph, "graph")
    _validate_file_record(files.get("graph.sqlite.sha256"), sidecar, "checksum sidecar")
    if manifest.get("data_through") != metadata["data_through"]:
        raise GraphError("snapshot graph and manifest evidence watermark disagree")
    for field in ("created_at", "data_through"):
        if not isinstance(manifest.get(field), str):
            raise GraphError(f"snapshot manifest {field} is invalid")
        _timestamp(manifest[field])
    root = f"{prefix}/snapshots/{repository_sha}"
    objects = {
        f"{root}/graph.sqlite": graph,
        f"{root}/graph.sqlite.sha256": sidecar,
        f"{root}/manifest.json": manifest_path,
    }
    for key, source in objects.items():
        _publish_immutable(store, key, source, sha256_file(source))

    with tempfile.TemporaryDirectory(prefix="snapshot-index-") as directory_name:
        directory = Path(directory_name)
        index_key = f"{prefix}/index.json"
        entry = {
            "created_at": manifest["created_at"],
            "data_through": manifest["data_through"],
            "manifest_bytes": manifest_path.stat().st_size,
            "manifest_key": f"{root}/manifest.json",
            "manifest_sha256": sha256_file(manifest_path),
            "repository_sha": repository_sha,
        }
        index_path = directory / "index.json"
        for attempt in range(3):
            index, etag = _read_index(store, index_key, directory)
            snapshots = [
                row
                for row in index["snapshots"]
                if row.get("repository_sha") != repository_sha
            ]
            snapshots.append(entry)
            snapshots.sort(key=lambda row: row["created_at"], reverse=True)
            updated = {"schema_version": 1, "snapshots": snapshots}
            _atomic_json(index_path, updated)
            try:
                store.upload(
                    index_key,
                    index_path,
                    sha256=sha256_file(index_path),
                    if_match=etag,
                    if_none_match=etag is None,
                )
            except GraphError:
                if attempt == 2:
                    raise
            else:
                break
        published_manifest = directory / "published-manifest.json"
        manifest_head = store.head(entry["manifest_key"])
        if (
            manifest_head is None
            or manifest_head.get("size") != entry["manifest_bytes"]
            or manifest_head.get("metadata", {}).get("sha256")
            != entry["manifest_sha256"]
        ):
            raise GraphError("published snapshot manifest metadata mismatch")
        store.download(entry["manifest_key"], published_manifest)
        if published_manifest.read_bytes() != manifest_path.read_bytes():
            raise GraphError("published snapshot manifest read-back mismatch")
        published_index, _etag = _read_index(store, index_key, directory)
        if entry not in published_index["snapshots"]:
            raise GraphError("published snapshot is missing from index read-back")
    print(
        "Snapshot index promoted and round-trip verified: key=%s entries=%d "
        "manifest=%s sha256=%s"
        % (
            index_key,
            len(published_index["snapshots"]),
            entry["manifest_key"],
            entry["manifest_sha256"],
        )
    )
    return entry


def _timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise GraphError("snapshot index timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise GraphError("snapshot index timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def select_snapshot(
    index: dict[str, Any],
    repo: Path,
    base: str,
    *,
    max_age_days: int = 11,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if max_age_days < 1:
        raise GraphError("snapshot maximum age must be at least one day")
    snapshots = index.get("snapshots")
    if not isinstance(snapshots, list):
        raise GraphError("snapshot index snapshots must be a list")
    for entry in sorted(
        (row for row in snapshots if isinstance(row, dict)),
        key=lambda row: str(row.get("created_at", "")),
        reverse=True,
    ):
        repository_sha = str(entry.get("repository_sha", ""))
        if not re.fullmatch(r"[0-9a-f]{40}", repository_sha):
            continue
        data_through = _timestamp(entry.get("data_through"))
        if data_through > now + timedelta(minutes=5):
            continue
        if now - data_through > timedelta(days=max_age_days):
            continue
        ancestry = subprocess.run(
            ["git", "merge-base", "--is-ancestor", repository_sha, base],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        if ancestry.returncode == 0:
            return entry
    raise GraphError("no fresh ancestral test-selection snapshot is available")


def fetch_snapshot(
    store: ObjectStore,
    prefix: str,
    repo: Path,
    base: str,
    output: Path,
    *,
    max_age_days: int = 11,
) -> dict[str, Any]:
    prefix = _prefix(prefix)
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="snapshot-fetch-", dir=output.parent
    ) as directory_name:
        directory = Path(directory_name)
        index_key = f"{prefix}/index.json"
        index, _etag = _read_index(store, index_key, directory)
        if not index["snapshots"]:
            raise GraphError("snapshot index does not exist")
        entry = select_snapshot(index, repo, base, max_age_days=max_age_days)
        expected_manifest_key = (
            f"{prefix}/snapshots/{entry['repository_sha']}/manifest.json"
        )
        if entry.get("manifest_key") != expected_manifest_key:
            raise GraphError("snapshot index manifest key is invalid")
        manifest_path = directory / "manifest.json"
        manifest_head = store.head(entry["manifest_key"])
        if manifest_head is None:
            raise GraphError("snapshot manifest does not exist")
        store.download(entry["manifest_key"], manifest_path)
        if (
            manifest_path.stat().st_size != entry["manifest_bytes"]
            or manifest_head.get("size") != entry["manifest_bytes"]
            or sha256_file(manifest_path) != entry["manifest_sha256"]
            or manifest_head.get("metadata", {}).get("sha256")
            != entry["manifest_sha256"]
        ):
            raise GraphError("snapshot manifest object checksum mismatch")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise GraphError("snapshot manifest is not valid JSON") from error
        if (
            manifest.get("kind") != "vllm-test-selection-snapshot"
            or manifest.get("schema_version") != 1
        ):
            raise GraphError("unsupported snapshot manifest schema")
        if manifest.get("repository_sha") != entry["repository_sha"]:
            raise GraphError("snapshot manifest disagrees with index")
        if any(
            manifest.get(field) != entry[field]
            for field in ("created_at", "data_through")
        ):
            raise GraphError("snapshot manifest watermark disagrees with index")
        root = entry["manifest_key"].removesuffix("/manifest.json")
        graph = directory / "graph.sqlite"
        sidecar = directory / "graph.sqlite.sha256"
        store.download(f"{root}/graph.sqlite", graph)
        store.download(f"{root}/graph.sqlite.sha256", sidecar)
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise GraphError("snapshot manifest files are invalid")
        expected = files.get("graph.sqlite", {})
        expected_sidecar = files.get("graph.sqlite.sha256", {})
        if not isinstance(expected, dict) or not isinstance(expected_sidecar, dict):
            raise GraphError("snapshot manifest file records are invalid")
        if graph.stat().st_size != expected.get("bytes"):
            raise GraphError("downloaded graph byte count mismatch")
        if sha256_file(graph) != expected.get("sha256"):
            raise GraphError("downloaded graph checksum mismatch")
        if sidecar.stat().st_size != expected_sidecar.get("bytes"):
            raise GraphError("downloaded checksum sidecar byte count mismatch")
        if sha256_file(sidecar) != expected_sidecar.get("sha256"):
            raise GraphError("downloaded checksum sidecar mismatch")
        verify_checksum(graph)
        metadata = graph_metadata(graph)
        if metadata["repository_sha"] != entry["repository_sha"]:
            raise GraphError("downloaded graph repository SHA mismatch")
        if metadata["data_through"] != manifest.get("data_through"):
            raise GraphError("downloaded graph evidence watermark mismatch")
        os.replace(graph, output)
        os.replace(sidecar, output.with_suffix(output.suffix + ".sha256"))
    return entry


def build_and_publish(
    input_dir: Path,
    inventory: Path,
    store: ObjectStore,
    prefix: str,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="fleet-snapshot-") as directory_name:
        directory = Path(directory_name)
        graph = directory / "graph.sqlite"
        metadata = build_fleet_graph(input_dir, inventory, graph)
        manifest_path = directory / "manifest.json"
        build_snapshot_manifest(graph, manifest_path)
        entry = publish_snapshot(store, prefix, graph, manifest_path)
    return {"metadata": metadata, "snapshot": entry}
