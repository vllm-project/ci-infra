"""Build and query the compact file/test/job trace database."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
import tempfile
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from test_selection import SCHEMA_VERSION


SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE evidence (
    source_kind TEXT NOT NULL,
    source TEXT NOT NULL,
    test_id TEXT NOT NULL,
    job_key TEXT NOT NULL,
    line INTEGER NOT NULL,
    PRIMARY KEY (source_kind, source, test_id, job_key, line)
) WITHOUT ROWID;
CREATE INDEX evidence_source ON evidence(source_kind, source);
CREATE TABLE jobs (
    job_key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    reason TEXT
) WITHOUT ROWID;
"""


class GraphError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_checksum(path: Path) -> Path:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    sidecar.write_text(sha256_file(path) + "\n", encoding="ascii")
    return sidecar


def verify_checksum(path: Path) -> None:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.is_file() or sidecar.read_text(
        encoding="ascii"
    ).strip() != sha256_file(path):
        raise GraphError("graph checksum mismatch")


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GraphError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise GraphError(f"{path} must contain a JSON object")
    return value


def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise GraphError(f"{path} contains a non-object row")
                rows.append(row)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GraphError(f"cannot read {path}: {error}") from error
    return rows


def _timestamp(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise GraphError("trace timestamp is invalid") from error
    if result.tzinfo is None:
        raise GraphError("trace timestamp has no timezone")
    return result.astimezone(timezone.utc)


def _sha(value: Any) -> str:
    value = str(value)
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise GraphError("repository SHA must be exact lowercase 40-hex")
    return value


def _sha256(value: Any, field: str) -> str:
    value = str(value)
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise GraphError(f"{field} must be exact lowercase 64-hex")
    return value


def _uuid(value: Any, field: str) -> str:
    value = str(value)
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise GraphError(f"{field} must be a canonical UUID") from error
    if str(parsed) != value:
        raise GraphError(f"{field} must be a canonical UUID")
    return value


def _insert_python(
    connection: sqlite3.Connection,
    manifest_path: Path,
    repository_sha: str,
    job_key: str,
    retry_count: int,
) -> None:
    manifest = _json(manifest_path)
    if manifest.get("repository_sha") != repository_sha:
        raise GraphError(f"{manifest_path} repository SHA mismatch")
    if manifest.get("retry_count", 0) != retry_count:
        raise GraphError(f"{manifest_path} retry count mismatch")
    if manifest.get("represented_job_key") != job_key or not manifest.get("healthy"):
        raise GraphError(f"{manifest_path} is not healthy evidence for {job_key}")
    started = manifest.get("pytest_invocations_started")
    exported = manifest.get("pytest_invocations_exported")
    if (
        type(started) is not int
        or started < 1
        or type(exported) is not int
        or exported != started
        or manifest.get("pytest_node_exports_complete") is not True
    ):
        raise GraphError(f"{manifest_path} has incomplete pytest node exports")
    trace = manifest_path.parent / str(manifest.get("python_trace", ""))
    if not trace.is_file() or sha256_file(trace) != manifest.get("python_trace_sha256"):
        raise GraphError(f"{manifest_path} Python trace checksum mismatch")
    rows = _jsonl(trace)
    if not rows:
        raise GraphError(f"{trace} contains no Python evidence")
    for row in rows:
        if row.get("repository_sha") != repository_sha or row.get("job_key") != job_key:
            raise GraphError(f"{trace} identity mismatch")
        connection.execute(
            "INSERT OR IGNORE INTO evidence VALUES ('file', ?, ?, ?, ?)",
            (str(row["file"]), str(row["test_id"]), job_key, int(row["line"])),
        )
    for test_id in sorted(set(manifest.get("node_ids", []))):
        connection.execute(
            "INSERT OR IGNORE INTO evidence VALUES ('file', ?, ?, ?, -1)",
            (str(test_id).split("::", 1)[0], str(test_id), job_key),
        )


def _insert_gpu(
    connection: sqlite3.Connection,
    directory: Path,
    repository_sha: str,
    job_key: str,
) -> int:
    count = 0
    for path in sorted(directory.rglob("gpu-trace.jsonl")):
        for row in _jsonl(path):
            if (
                row.get("repository_sha") != repository_sha
                or row.get("job_key") != job_key
            ):
                raise GraphError(f"{path} identity mismatch")
            destination_kind = row.get("destination_kind")
            destination = str(row.get("destination", ""))
            test_id = destination if destination_kind == "test" else ""
            connection.execute(
                "INSERT OR IGNORE INTO evidence VALUES ('kernel', ?, ?, ?, -1)",
                (str(row["source"]), test_id, job_key),
            )
            count += 1
    return count


def _identity_values(
    document: dict[str, Any], singular_field: str, plural_field: str
) -> set[str]:
    """The identity values a summary claims; singular/plural must agree."""

    singular = document.get(singular_field)
    plural = document.get(plural_field) or []
    values = {str(value) for value in plural}
    if singular:
        if plural and str(singular) not in {str(value) for value in plural}:
            raise GraphError(
                f"summary {singular_field}/{plural_field} fields disagree"
            )
        values.add(str(singular))
    return values


def _collect_summaries(
    input_dir: Path,
    policies: dict[str, Any],
    repository_sha: str,
    collector_sha: str,
) -> tuple[
    dict[str, dict[int, tuple[Path, dict[str, Any]]]],
    dict[str, str],
]:
    """Select the highest automatic retry for every represented shard."""

    candidates: dict[
        str, dict[int, dict[int, list[tuple[Path, dict[str, Any], bool]]]]
    ] = {key: {} for key in policies}
    invalid: dict[str, str] = {}
    for path in sorted(input_dir.rglob("trace-job.json")):
        document = _json(path)
        key = str(document.get("represented_job_key", ""))
        if key not in policies:
            continue
        shard = document.get("parallel_job")
        retry_count = document.get("retry_count", 0)
        if (
            not isinstance(shard, int)
            or isinstance(shard, bool)
            or not isinstance(retry_count, int)
            or isinstance(retry_count, bool)
            or retry_count < 0
        ):
            invalid[key] = "invalid_summary"
            continue
        expected = policies[key].get("expected_shards")
        identity_valid = bool(
            document.get("repository_sha") == repository_sha
            and document.get("collector_sha256") == collector_sha
            and document.get("parallel_job_count") == expected
        )
        # A summary claiming several image digests or baselines is mixed
        # provenance; the compact writer already fails those, defense here.
        try:
            mixed_identity = (
                len(_identity_values(document, "image_digest", "image_digests")) > 1
                or len(
                    _identity_values(
                        document,
                        "worktree_baseline_sha256",
                        "worktree_baseline_sha256s",
                    )
                )
                > 1
            )
        except GraphError:
            # singular/plural disagreement within one summary
            mixed_identity = True
        if mixed_identity:
            identity_valid = False
        candidates[key].setdefault(shard, {}).setdefault(retry_count, []).append(
            (path, document, identity_valid)
        )

    summaries: dict[str, dict[int, tuple[Path, dict[str, Any]]]] = {
        key: {} for key in policies
    }
    for key, shard_candidates in candidates.items():
        for shard, retries in shard_candidates.items():
            latest = max(retries)
            rows = retries[latest]
            if len(rows) != 1:
                invalid[key] = "duplicate_retry_summary"
                continue
            path, document, identity_valid = rows[0]
            if not identity_valid:
                invalid[key] = "invalid_summary"
                continue
            summaries[key][shard] = (path, document)
    return summaries, invalid


def _materialize(
    input_dir: Path,
    output: Path,
    inventory: dict[str, Any],
    replacement_inputs: dict[str, tuple[Path, str]] | None = None,
) -> dict[str, Any]:
    repository_sha = _sha(inventory.get("repository_sha"))
    collector_sha = _sha256(inventory.get("collector_sha256", ""), "collector SHA-256")
    policies = {str(row["key"]): row for row in inventory.get("jobs", [])}
    if not policies:
        raise GraphError("trace inventory contains no jobs")
    wait_results = inventory.get("wait_results", {})
    summaries, invalid = _collect_summaries(
        input_dir, policies, repository_sha, collector_sha
    )
    replacements_by_source: dict[tuple[Path, str], set[str]] = {}
    for key, (root, replacement_collector_sha) in (replacement_inputs or {}).items():
        if key not in policies:
            raise GraphError(f"replacement evidence contains unknown job {key}")
        source = (
            root,
            _sha256(replacement_collector_sha, "replacement collector SHA-256"),
        )
        replacements_by_source.setdefault(source, set()).add(key)
    for (root, replacement_collector_sha), keys in replacements_by_source.items():
        replacement_policies = {key: policies[key] for key in keys}
        replacement_summaries, replacement_invalid = _collect_summaries(
            root,
            replacement_policies,
            repository_sha,
            replacement_collector_sha,
        )
        for key in keys:
            summaries[key] = replacement_summaries[key]
            invalid.pop(key, None)
            if key in replacement_invalid:
                invalid[key] = replacement_invalid[key]

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    connection = sqlite3.connect(temporary)
    healthy = []
    missing = []
    unhealthy = []
    reasons: dict[str, str] = {}
    evidence_times = []
    try:
        connection.executescript(SCHEMA)
        for key, policy in sorted(policies.items()):
            expected = set(range(int(policy["expected_shards"])))
            actual = set(summaries[key])
            wait = wait_results.get(key, {})
            if wait.get("status") == "poll_timeout":
                status, reason = "missing", "poll_timeout"
                missing.append(key)
            elif key in invalid:
                status, reason = "unhealthy", invalid[key]
                unhealthy.append(key)
            elif actual != expected:
                status = "missing"
                reason = (
                    "terminal_missing_shard"
                    if wait.get("status") == "terminal"
                    else "missing_shard"
                )
                missing.append(key)
            elif any(
                document.get("healthy") is not True
                for _path, document in summaries[key].values()
            ):
                summary_reasons = {
                    str(document["failure_reason"])
                    for _path, document in summaries[key].values()
                    if document.get("failure_reason")
                }
                reason = (
                    next(iter(summary_reasons))
                    if len(summary_reasons) == 1
                    else "collector_unhealthy"
                )
                status = "unhealthy"
                unhealthy.append(key)
            else:
                try:
                    connection.execute("SAVEPOINT job")
                    for summary_path, document in summaries[key].values():
                        evidence_times.append(_timestamp(document.get("created_at")))
                        for manifest in sorted(summary_path.parent.rglob("job.json")):
                            _insert_python(
                                connection,
                                manifest,
                                repository_sha,
                                key,
                                document.get("retry_count", 0),
                            )
                        if policy.get("mode") == "kernel-set" and not _insert_gpu(
                            connection, summary_path.parent, repository_sha, key
                        ):
                            raise GraphError("GPU job produced no kernel evidence")
                    count = connection.execute(
                        "SELECT COUNT(*) FROM evidence WHERE job_key = ?", (key,)
                    ).fetchone()[0]
                    if not count:
                        raise GraphError("job produced no compact evidence")
                    connection.execute("RELEASE job")
                except Exception as error:
                    connection.execute("ROLLBACK TO job")
                    connection.execute("RELEASE job")
                    status, reason = "unhealthy", type(error).__name__
                    unhealthy.append(key)
                else:
                    status, reason = "healthy", None
                    healthy.append(key)
            reasons[key] = reason or ""
            connection.execute(
                "INSERT INTO jobs VALUES (?, ?, ?)", (key, status, reason)
            )

        if not healthy:
            raise GraphError("trace generation contains no healthy jobs")
        # One generation = one image + one worktree baseline. Jobs that predate
        # the receipts carry nothing and are ungated; every carrier must agree.
        generation_image_digests = sorted(
            {
                value
                for _path, document in (
                    summary for job in summaries.values() for summary in job.values()
                )
                for value in _identity_values(document, "image_digest", "image_digests")
            }
        )
        generation_baselines = sorted(
            {
                value
                for _path, document in (
                    summary for job in summaries.values() for summary in job.values()
                )
                for value in _identity_values(
                    document, "worktree_baseline_sha256", "worktree_baseline_sha256s"
                )
            }
        )
        if len(generation_image_digests) > 1:
            raise GraphError("image digest disagreement across generation")
        if len(generation_baselines) > 1:
            raise GraphError("worktree baseline disagreement across generation")
        data_through = min(evidence_times).isoformat()
        collector_sha256s = sorted(
            {collector_sha}
            | {
                replacement_collector_sha
                for _root, replacement_collector_sha in replacements_by_source
            }
        )
        metadata = {
            "always_run": inventory.get("always_run", []),
            "ci_infra_revision": inventory.get("ci_infra_revision"),
            "collector_sha256": (
                collector_sha256s[0] if len(collector_sha256s) == 1 else None
            ),
            "collector_sha256s": collector_sha256s,
            "created_at": data_through,
            "data_through": data_through,
            "expected_jobs": sorted(policies),
            "healthy_jobs": sorted(healthy),
            "image_digest": (
                generation_image_digests[0]
                if len(generation_image_digests) == 1
                else None
            ),
            "image_digests": generation_image_digests,
            "missing_jobs": sorted(missing),
            "missing_reasons": {key: reasons[key] for key in sorted(missing)},
            "repository_sha": repository_sha,
            "schema_version": SCHEMA_VERSION,
            "unhealthy_jobs": sorted(unhealthy),
            "unhealthy_reasons": {key: reasons[key] for key in sorted(unhealthy)},
            "wait_results": wait_results,
            "worktree_baseline_sha256": (
                generation_baselines[0] if len(generation_baselines) == 1 else None
            ),
            "worktree_baseline_sha256s": generation_baselines,
        }
        for key, value in sorted(metadata.items()):
            connection.execute(
                "INSERT INTO metadata VALUES (?, ?)",
                (key, json.dumps(value, sort_keys=True, separators=(",", ":"))),
            )
        connection.commit()
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise GraphError("SQLite integrity check failed")
    except Exception:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    connection.close()
    temporary.replace(output)
    write_checksum(output)
    return metadata


def build_fleet_graph(input_dir: Path, inventory: Path, output: Path) -> dict[str, Any]:
    return _materialize(input_dir.resolve(), output.resolve(), _json(inventory))


def _job_evidence_digest(graph: Path, job_key: str) -> str:
    digest = hashlib.sha256()
    with sqlite3.connect(f"file:{graph.resolve()}?mode=ro", uri=True) as connection:
        for row in connection.execute(
            "SELECT source_kind, source, test_id, job_key, line "
            "FROM evidence WHERE job_key = ? ORDER BY 1, 2, 3, 4, 5",
            (job_key,),
        ):
            digest.update(json.dumps(row, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


def merge_fleet_graph(
    base_input: Path,
    base_inventory: Path,
    retry_input: Path,
    retry_inventory: Path,
    output: Path,
    provenance_output: Path,
    *,
    base_source_build_id: str,
    retry_source_build_id: str,
    merge_revision: str,
) -> dict[str, Any]:
    """Overlay only materializer-proven healthy retry jobs onto a base fleet."""

    base_source_build_id = _uuid(base_source_build_id, "base source build ID")
    retry_source_build_id = _uuid(retry_source_build_id, "retry source build ID")
    merge_revision = _sha(merge_revision)
    if base_source_build_id == retry_source_build_id:
        raise GraphError("base and retry source build IDs must differ")
    base_document = _json(base_inventory)
    retry_document = _json(retry_inventory)
    if base_document.get("repository_sha") != retry_document.get("repository_sha"):
        raise GraphError("base and retry inventories disagree on repository_sha")
    base_collector_sha = _sha256(
        base_document.get("collector_sha256"), "base collector SHA-256"
    )
    retry_collector_sha = _sha256(
        retry_document.get("collector_sha256"), "retry collector SHA-256"
    )
    base_policies = {str(row.get("key")): row for row in base_document.get("jobs", [])}
    retry_policies = {
        str(row.get("key")): row for row in retry_document.get("jobs", [])
    }
    if not retry_policies or not set(retry_policies) <= set(base_policies):
        raise GraphError("retry inventory jobs must be a nonempty base subset")
    if any(retry_policies[key] != base_policies[key] for key in retry_policies):
        raise GraphError("retry inventory changes a base job policy")

    output = output.resolve()
    provenance_output = provenance_output.resolve()
    output_sidecar = output.with_suffix(output.suffix + ".sha256")
    if output.exists() or output_sidecar.exists() or provenance_output.exists():
        raise GraphError("merge outputs must not already exist")
    try:
        with tempfile.TemporaryDirectory(prefix="fleet-graph-merge-") as directory_name:
            directory = Path(directory_name)
            base_graph = directory / "base.sqlite"
            retry_graph = directory / "retry.sqlite"
            base_metadata = _materialize(
                base_input.resolve(), base_graph, base_document
            )
            retry_metadata = _materialize(
                retry_input.resolve(), retry_graph, retry_document
            )
            replacements = set(retry_metadata["healthy_jobs"])
            unresolved_base = set(base_metadata["missing_jobs"]) | set(
                base_metadata["unhealthy_jobs"]
            )
            if not replacements <= unresolved_base:
                raise GraphError(
                    "retry may replace only missing or unhealthy base jobs"
                )

            merged_inventory = deepcopy(base_document)
            for key in replacements:
                wait_result = retry_document.get("wait_results", {}).get(key)
                if (
                    not isinstance(wait_result, dict)
                    or wait_result.get("status") != "terminal"
                ):
                    raise GraphError(
                        f"healthy retry job {key} has no terminal wait result"
                    )
                merged_inventory["wait_results"][key] = wait_result
            merged_metadata = _materialize(
                base_input.resolve(),
                output,
                merged_inventory,
                {
                    key: (retry_input.resolve(), retry_collector_sha)
                    for key in replacements
                },
            )

            expected_healthy = set(base_metadata["healthy_jobs"]) | replacements
            if set(merged_metadata["healthy_jobs"]) != expected_healthy:
                raise GraphError(
                    "merged healthy job set is not the exact expected union"
                )
            for key in base_metadata["healthy_jobs"]:
                if _job_evidence_digest(base_graph, key) != _job_evidence_digest(
                    output, key
                ):
                    raise GraphError(f"merged evidence changed healthy base job {key}")
            for key in replacements:
                if _job_evidence_digest(retry_graph, key) != _job_evidence_digest(
                    output, key
                ):
                    raise GraphError(f"merged evidence disagrees with retry job {key}")

            provenance = {
                "base_source_build_id": base_source_build_id,
                "base_inventory_sha256": sha256_file(base_inventory),
                "base_collector_sha256": base_collector_sha,
                "base_metadata": base_metadata,
                "collector_sha256s": sorted({base_collector_sha, retry_collector_sha}),
                "kind": "vllm-test-selection-evidence-merge",
                "merge_revision": merge_revision,
                "merged_graph_sha256": sha256_file(output),
                "merged_metadata": merged_metadata,
                "replacement_jobs": sorted(replacements),
                "repository_sha": base_document.get("repository_sha"),
                "retry_source_build_id": retry_source_build_id,
                "retry_collector_sha256": retry_collector_sha,
                "retry_inventory_sha256": sha256_file(retry_inventory),
                "retry_metadata": retry_metadata,
                "schema_version": 1,
            }
            provenance_output.parent.mkdir(parents=True, exist_ok=True)
            temporary = provenance_output.with_suffix(provenance_output.suffix + ".tmp")
            temporary.write_text(
                json.dumps(provenance, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            temporary.replace(provenance_output)
    except Exception:
        output.unlink(missing_ok=True)
        output_sidecar.unlink(missing_ok=True)
        provenance_output.with_suffix(provenance_output.suffix + ".tmp").unlink(
            missing_ok=True
        )
        raise
    return {"metadata": merged_metadata, "provenance": provenance}


def build_graph(input_dir: Path, output: Path) -> dict[str, Any]:
    summaries = [_json(path) for path in sorted(input_dir.rglob("trace-job.json"))]
    if not summaries:
        raise GraphError("input contains no trace-job summaries")
    first = summaries[0]
    jobs = {}
    for document in summaries:
        key = str(document.get("represented_job_key", ""))
        jobs[key] = {
            "expected_shards": document.get("parallel_job_count", 1),
            "key": key,
            "mode": "kernel-set"
            if document.get("capture_mode") == "gpu"
            else "python-only",
        }
    inventory = {
        "always_run": [],
        "ci_infra_revision": "local",
        "collector_sha256": first.get("collector_sha256"),
        "jobs": list(jobs.values()),
        "repository_sha": first.get("repository_sha"),
        "wait_results": {},
    }
    return _materialize(input_dir.resolve(), output.resolve(), inventory)


def _connect(graph: Path) -> sqlite3.Connection:
    verify_checksum(graph)
    return sqlite3.connect(f"file:{graph.resolve()}?mode=ro", uri=True)


def graph_metadata(graph: Path) -> dict[str, Any]:
    with _connect(graph) as connection:
        result = {
            key: json.loads(value)
            for key, value in connection.execute("SELECT key, value FROM metadata")
        }
    if result.get("schema_version") != SCHEMA_VERSION:
        raise GraphError("unsupported graph schema")
    return result


def paths_to_jobs(graph: Path, source: str) -> list[list[dict[str, Any]]]:
    with _connect(graph) as connection:
        rows = connection.execute(
            """SELECT DISTINCT test_id, job_key FROM evidence
               WHERE source_kind = 'file' AND source = ? ORDER BY job_key, test_id""",
            (source,),
        ).fetchall()
    return [
        [
            {"kind": "file", "name": source},
            {"edge": "executed_by", "kind": "test", "name": test_id},
            {"edge": "run_by", "kind": "job", "name": job_key},
        ]
        for test_id, job_key in rows
    ]


def changed_files(repo: Path, base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACDMRTUXB", base, head],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(set(result.stdout.splitlines()))


def read_current_jobs(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = [line.strip() for line in text.splitlines() if line.strip()]
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise GraphError("current jobs must be a JSON array or newline list")
    if len(value) != len(set(value)):
        raise GraphError("current jobs contain duplicates")
    return set(value)


def current_jobs_from_pipeline(path: Path) -> list[str]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("steps"), list):
        raise GraphError("rendered pipeline must contain steps")
    keys = []
    for item in document["steps"]:
        for step in item.get("steps", [item]) if isinstance(item, dict) else []:
            if isinstance(step, dict) and step.get("key") and "commands" in step:
                keys.append(str(step["key"]))
    if not keys or len(keys) != len(set(keys)):
        raise GraphError("rendered pipeline has missing or duplicate command keys")
    return sorted(keys)


def _healthy_jobs(graph: Path) -> set[str]:
    with _connect(graph) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT job_key FROM jobs WHERE status='healthy'"
            )
        }


def job_coverage(graph: Path, current_jobs_path: Path) -> dict[str, Any]:
    current = read_current_jobs(current_jobs_path)
    healthy = _healthy_jobs(graph)
    return {
        "covered_step_keys": sorted(current & healthy),
        "current_count": len(current),
        "dead_graph_step_keys": sorted(healthy - current),
        "graph_count": len(healthy),
        "uncovered_step_keys": sorted(current - healthy),
    }


def _docs_only(paths: list[str]) -> bool:
    return bool(paths) and all(
        path.startswith("docs/")
        or path.endswith(".md")
        or path in {"mkdocs.yml", "mkdocs.yaml"}
        for path in paths
    )


def select_jobs(
    graph: Path,
    repo: Path,
    base: str,
    head: str,
    current_jobs_path: Path,
    max_snapshot_age_days: int = 11,
) -> dict[str, Any]:
    files = changed_files(repo, base, head)
    current = read_current_jobs(current_jobs_path)
    if not files:
        return {
            "changed_files": [],
            "fallback": True,
            "fallback_reason": "empty_diff",
            "reasons": [],
            "status": "fallback",
            "step_keys": [],
        }
    if _docs_only(files):
        return {
            "changed_files": files,
            "fallback": False,
            "reasons": [],
            "status": "docs_only",
            "step_keys": [],
            "uncovered_step_keys": sorted(current),
        }

    metadata = graph_metadata(graph)
    snapshot_sha = str(metadata["repository_sha"])
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", snapshot_sha, base],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if ancestry.returncode:
        reason = "snapshot_not_ancestor"
    else:
        age = datetime.now(timezone.utc) - _timestamp(metadata["data_through"])
        reason = (
            "snapshot_stale" if age > timedelta(days=max_snapshot_age_days) else None
        )
    if reason:
        return {
            "changed_files": files,
            "fallback": True,
            "fallback_reason": reason,
            "reasons": [],
            "status": "fallback",
            "step_keys": [],
        }

    selected = set()
    reasons = []
    unmapped = []
    for filename in files:
        paths = [
            path
            for path in paths_to_jobs(graph, filename)
            if path[-1]["name"] in current
        ]
        if not paths:
            unmapped.append(filename)
            continue
        for path in paths:
            job = str(path[-1]["name"])
            selected.add(job)
            reasons.append(
                {
                    "analysis": "trace_presence",
                    "changed_file": filename,
                    "job": job,
                    "path": [row["kind"] for row in path],
                    "test": path[-2]["name"],
                }
            )
    if unmapped:
        return {
            "changed_files": files,
            "fallback": True,
            "fallback_reason": "unmapped_changed_file",
            "reasons": reasons,
            "status": "fallback",
            "step_keys": [],
            "unmapped_files": sorted(unmapped),
        }
    uncovered = current - _healthy_jobs(graph)
    selected.update(uncovered)
    return {
        "changed_files": files,
        "fallback": False,
        "reasons": sorted(
            reasons, key=lambda row: (row["job"], row["changed_file"], row["test"])
        ),
        "status": "selected",
        "step_keys": sorted(selected),
        "uncovered_step_keys": sorted(uncovered),
    }
