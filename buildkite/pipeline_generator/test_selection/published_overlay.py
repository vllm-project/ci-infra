"""Overlay healthy retry evidence onto an immutable published fleet graph.

This is a recovery-only companion to ``merge-fleet-graph`` for the case where
the accepted base graph is still available but some of its raw Buildkite
artifacts are no longer retrievable.  It deliberately materializes the retry
from raw evidence, while treating the checksum-verified published graph as the
only base source of truth.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from test_selection.graph import (
    GraphError,
    _json,
    _materialize,
    _sha,
    _sha256,
    _uuid,
    graph_metadata,
    sha256_file,
    verify_checksum,
    write_checksum,
)


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _partition(metadata: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    healthy = set(map(str, metadata.get("healthy_jobs", [])))
    missing = set(map(str, metadata.get("missing_jobs", [])))
    unhealthy = set(map(str, metadata.get("unhealthy_jobs", [])))
    if healthy & missing or healthy & unhealthy or missing & unhealthy:
        raise GraphError("graph job partitions overlap")
    return healthy, missing, unhealthy


def _collector_identity(
    inventory: dict[str, Any], label: str
) -> tuple[str | None, list[str]]:
    raw_primary = inventory.get("collector_sha256")
    primary = (
        None
        if raw_primary is None
        else _sha256(raw_primary, f"{label} collector SHA-256")
    )
    raw_collectors = inventory.get("collector_sha256s")
    if raw_collectors is None:
        if primary is None:
            raise GraphError(f"{label} collector identity is missing")
        collectors = [primary]
    else:
        if not isinstance(raw_collectors, list) or not raw_collectors:
            raise GraphError(f"{label} collector SHA-256 set is invalid")
        collectors = sorted(
            {_sha256(value, f"{label} collector SHA-256") for value in raw_collectors}
        )
        if collectors != raw_collectors:
            raise GraphError(f"{label} collector SHA-256 set is not canonical")
        if primary is not None and collectors != [primary]:
            raise GraphError(f"{label} collector identity is inconsistent")
    return primary, collectors


def _validate_base_graph(
    graph: Path,
    inventory: dict[str, Any],
    *,
    expected_healthy_count: int,
    expected_missing_count: int,
    expected_unhealthy_count: int,
) -> dict[str, Any]:
    verify_checksum(graph)
    metadata = graph_metadata(graph)
    policies = {str(row.get("key")): row for row in inventory.get("jobs", [])}
    repository_sha = _sha(inventory.get("repository_sha"))
    collector_sha, collector_sha256s = _collector_identity(inventory, "base")
    if metadata.get("repository_sha") != repository_sha:
        raise GraphError("published base graph repository SHA mismatch")
    if metadata.get("ci_infra_revision") != inventory.get("ci_infra_revision"):
        raise GraphError("published base graph ci-infra revision mismatch")
    if metadata.get("always_run") != inventory.get("always_run", []):
        raise GraphError("published base graph always-run policy mismatch")
    if metadata.get("wait_results") != inventory.get("wait_results", {}):
        raise GraphError("published base graph wait results mismatch")
    if set(map(str, metadata.get("expected_jobs", []))) != set(policies):
        raise GraphError("published base graph expected-job inventory mismatch")
    if (
        metadata.get("collector_sha256") != collector_sha
        or metadata.get("collector_sha256s") != collector_sha256s
    ):
        raise GraphError("published base graph collector identity mismatch")

    healthy, missing, unhealthy = _partition(metadata)
    if healthy | missing | unhealthy != set(policies):
        raise GraphError("published base graph partitions do not cover inventory")
    if (
        len(healthy) != expected_healthy_count
        or len(missing) != expected_missing_count
        or len(unhealthy) != expected_unhealthy_count
    ):
        raise GraphError("published base graph accounting mismatch")
    with sqlite3.connect(f"file:{graph.resolve()}?mode=ro", uri=True) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise GraphError("published base graph SQLite integrity check failed")
        rows = {
            str(key): (str(status), reason)
            for key, status, reason in connection.execute(
                "SELECT job_key, status, reason FROM jobs"
            )
        }
    expected_rows = {
        **{key: ("healthy", None) for key in healthy},
        **{key: ("missing", metadata["missing_reasons"][key]) for key in missing},
        **{key: ("unhealthy", metadata["unhealthy_reasons"][key]) for key in unhealthy},
    }
    if rows != expected_rows:
        raise GraphError("published base graph jobs table disagrees with metadata")
    return metadata


def _job_digests(
    graph: Path, selected: set[str]
) -> tuple[dict[str, str], dict[str, int]]:
    digests = {key: hashlib.sha256() for key in selected}
    counts = {key: 0 for key in selected}
    with sqlite3.connect(f"file:{graph.resolve()}?mode=ro", uri=True) as connection:
        for row in connection.execute(
            "SELECT source_kind, source, test_id, job_key, line "
            "FROM evidence ORDER BY source_kind, source, test_id, job_key, line"
        ):
            key = str(row[3])
            if key not in selected:
                continue
            digests[key].update(json.dumps(row, separators=(",", ":")).encode())
            digests[key].update(b"\n")
            counts[key] += 1
    if any(not count for count in counts.values()):
        empty = sorted(key for key, count in counts.items() if not count)
        raise GraphError(f"healthy job has no digestible evidence: {empty}")
    return {key: digests[key].hexdigest() for key in sorted(selected)}, counts


def _replace_metadata(
    connection: sqlite3.Connection,
    base_metadata: dict[str, Any],
    retry_metadata: dict[str, Any],
    retry_inventory: dict[str, Any],
    replacements: set[str],
    base_collector_shas: Iterable[str],
    retry_collector_sha: str,
) -> dict[str, Any]:
    merged = json.loads(json.dumps(base_metadata))
    healthy, missing, unhealthy = _partition(base_metadata)
    healthy |= replacements
    missing -= replacements
    unhealthy -= replacements
    merged["healthy_jobs"] = sorted(healthy)
    merged["missing_jobs"] = sorted(missing)
    merged["unhealthy_jobs"] = sorted(unhealthy)
    merged["missing_reasons"] = {
        key: value
        for key, value in base_metadata.get("missing_reasons", {}).items()
        if key not in replacements
    }
    merged["unhealthy_reasons"] = {
        key: value
        for key, value in base_metadata.get("unhealthy_reasons", {}).items()
        if key not in replacements
    }
    merged["collector_sha256"] = None
    merged["collector_sha256s"] = sorted({*base_collector_shas, retry_collector_sha})
    wait_results = dict(base_metadata.get("wait_results", {}))
    for key in replacements:
        wait_result = retry_inventory.get("wait_results", {}).get(key)
        if not isinstance(wait_result, dict) or wait_result.get("status") != "terminal":
            raise GraphError(f"healthy retry job {key} has no terminal wait result")
        wait_results[key] = wait_result
    merged["wait_results"] = wait_results
    if merged.get("repository_sha") != retry_metadata.get("repository_sha"):
        raise GraphError("retry graph repository SHA disagrees with base graph")
    for key, value in sorted(merged.items()):
        connection.execute(
            "INSERT OR REPLACE INTO metadata VALUES (?, ?)",
            (key, json.dumps(value, sort_keys=True, separators=(",", ":"))),
        )
    return merged


def overlay_published_graph(
    base_graph: Path,
    base_inventory_path: Path,
    retry_input: Path,
    retry_inventory_path: Path,
    output: Path,
    provenance_output: Path,
    *,
    base_source_build_id: str,
    retry_source_build_id: str | None = None,
    retry_source_builds: dict[str, str] | None = None,
    merge_revision: str,
    base_manifest_key: str,
    base_manifest_sha256: str,
    expected_base_healthy_count: int,
    expected_base_missing_count: int,
    expected_base_unhealthy_count: int,
    expected_replacements: Iterable[str],
    expected_retry_missing: Iterable[str],
    expected_policy_downgrades: Iterable[str] = (),
) -> dict[str, Any]:
    base_source_build_id = _uuid(base_source_build_id, "base source build ID")
    merge_revision = _sha(merge_revision)
    base_manifest_sha256 = _sha256(base_manifest_sha256, "base manifest SHA-256")
    if not base_manifest_key or base_manifest_key.startswith("/"):
        raise GraphError("base manifest key must be a nonempty relative S3 key")

    base_graph = base_graph.resolve()
    base_inventory_path = base_inventory_path.resolve()
    retry_input = retry_input.resolve()
    retry_inventory_path = retry_inventory_path.resolve()
    output = output.resolve()
    provenance_output = provenance_output.resolve()
    output_sidecar = output.with_suffix(output.suffix + ".sha256")
    if output.exists() or output_sidecar.exists() or provenance_output.exists():
        raise GraphError("overlay outputs must not already exist")

    base_inventory = _json(base_inventory_path)
    retry_inventory = _json(retry_inventory_path)
    if base_inventory.get("repository_sha") != retry_inventory.get("repository_sha"):
        raise GraphError("base and retry inventories disagree on repository SHA")
    base_collector_sha, base_collector_shas = _collector_identity(
        base_inventory, "base"
    )
    retry_collector_sha, retry_collector_shas = _collector_identity(
        retry_inventory, "retry"
    )
    if retry_collector_sha is None or retry_collector_shas != [retry_collector_sha]:
        raise GraphError("retry inventory must have one primary collector")
    base_policies = {str(row.get("key")): row for row in base_inventory.get("jobs", [])}
    retry_policies = {
        str(row.get("key")): row for row in retry_inventory.get("jobs", [])
    }
    if not retry_policies or not set(retry_policies) <= set(base_policies):
        raise GraphError("retry inventory jobs must be a nonempty base subset")
    policy_downgrades = set(expected_policy_downgrades)
    actual_policy_changes = {
        key for key in retry_policies if retry_policies[key] != base_policies[key]
    }
    if actual_policy_changes != policy_downgrades:
        raise GraphError("retry inventory policy changes are not exact")
    for key in actual_policy_changes:
        base_policy = dict(base_policies[key])
        retry_policy = dict(retry_policies[key])
        if (
            base_policy.pop("mode", None) != "kernel-set"
            or retry_policy.pop("mode", None) != "python-only"
            or base_policy != retry_policy
        ):
            raise GraphError("retry inventory policy downgrade is not trusted")

    base_metadata = _validate_base_graph(
        base_graph,
        base_inventory,
        expected_healthy_count=expected_base_healthy_count,
        expected_missing_count=expected_base_missing_count,
        expected_unhealthy_count=expected_base_unhealthy_count,
    )
    expected_replacement_set = set(expected_replacements)
    expected_retry_missing_set = set(expected_retry_missing)
    if not expected_replacement_set:
        raise GraphError("expected replacement set must be nonempty")
    if expected_replacement_set & expected_retry_missing_set:
        raise GraphError("expected retry healthy and missing sets overlap")
    if expected_replacement_set | expected_retry_missing_set != set(retry_policies):
        raise GraphError("expected retry accounting does not cover retry inventory")
    if not policy_downgrades <= expected_replacement_set:
        raise GraphError("policy downgrades must be healthy replacements")
    if (retry_source_build_id is None) == (retry_source_builds is None):
        raise GraphError("exactly one retry source-build mode is required")
    if retry_source_build_id is not None:
        source_build_id = _uuid(retry_source_build_id, "retry source build ID")
        normalized_retry_sources = {
            key: source_build_id for key in sorted(retry_policies)
        }
        normalized_retry_source_build_id = source_build_id
    else:
        assert retry_source_builds is not None
        if set(retry_source_builds) != set(retry_policies):
            raise GraphError("retry source-build map does not cover retry inventory")
        normalized_retry_sources = {
            key: _uuid(retry_source_builds[key], f"retry source build ID for {key}")
            for key in sorted(retry_source_builds)
        }
        normalized_retry_source_build_id = None
    if base_source_build_id in set(normalized_retry_sources.values()):
        raise GraphError("base and retry source build IDs must differ")

    try:
        with tempfile.TemporaryDirectory(
            prefix="published-graph-overlay-"
        ) as directory_name:
            directory = Path(directory_name)
            retry_graph = directory / "retry.sqlite"
            retry_metadata = _materialize(retry_input, retry_graph, retry_inventory)
            replacements = set(map(str, retry_metadata["healthy_jobs"]))
            retry_missing = set(map(str, retry_metadata["missing_jobs"]))
            retry_unhealthy = set(map(str, retry_metadata["unhealthy_jobs"]))
            if replacements != expected_replacement_set:
                raise GraphError("materialized retry healthy set is not exact")
            if retry_missing != expected_retry_missing_set or retry_unhealthy:
                raise GraphError(
                    "materialized retry unresolved accounting is not exact"
                )
            base_healthy, base_missing, base_unhealthy = _partition(base_metadata)
            if not replacements <= base_missing | base_unhealthy:
                raise GraphError(
                    "retry may replace only missing or unhealthy base jobs"
                )

            base_healthy_digests, base_healthy_counts = _job_digests(
                base_graph, base_healthy
            )
            retry_digests, retry_counts = _job_digests(retry_graph, replacements)

            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix(output.suffix + ".tmp")
            temporary.unlink(missing_ok=True)
            shutil.copyfile(base_graph, temporary)
            with sqlite3.connect(temporary) as connection:
                connection.execute("ATTACH DATABASE ? AS retry", (str(retry_graph),))
                for key in sorted(replacements):
                    if connection.execute(
                        "SELECT COUNT(*) FROM main.evidence WHERE job_key = ?", (key,)
                    ).fetchone()[0]:
                        raise GraphError(
                            f"unresolved base job unexpectedly retains evidence: {key}"
                        )
                    connection.execute(
                        "INSERT INTO main.evidence "
                        "SELECT source_kind, source, test_id, job_key, line "
                        "FROM retry.evidence WHERE job_key = ?",
                        (key,),
                    )
                    status, reason = connection.execute(
                        "SELECT status, reason FROM retry.jobs WHERE job_key = ?",
                        (key,),
                    ).fetchone()
                    if status != "healthy" or reason is not None:
                        raise GraphError(
                            f"retry job is not healthy in retry graph: {key}"
                        )
                    connection.execute(
                        "UPDATE main.jobs SET status = 'healthy', reason = NULL "
                        "WHERE job_key = ?",
                        (key,),
                    )
                    if connection.execute("SELECT changes()").fetchone() != (1,):
                        raise GraphError(
                            f"base jobs table is missing replacement: {key}"
                        )
                merged_metadata = _replace_metadata(
                    connection,
                    base_metadata,
                    retry_metadata,
                    retry_inventory,
                    replacements,
                    base_collector_shas,
                    retry_collector_sha,
                )
                connection.commit()
                connection.execute("DETACH DATABASE retry")
                if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise GraphError("merged SQLite integrity check failed")
            temporary.replace(output)
            write_checksum(output)

            merged_base_digests, merged_base_counts = _job_digests(output, base_healthy)
            merged_retry_digests, merged_retry_counts = _job_digests(
                output, replacements
            )
            if merged_base_digests != base_healthy_digests:
                raise GraphError("merged graph changed healthy base evidence")
            if merged_base_counts != base_healthy_counts:
                raise GraphError("merged graph changed healthy base row counts")
            if merged_retry_digests != retry_digests:
                raise GraphError(
                    "merged graph evidence disagrees with retry materialization"
                )
            if merged_retry_counts != retry_counts:
                raise GraphError("merged graph retry row counts disagree")

            provenance = {
                "base_collector_sha256": base_collector_sha,
                "base_collector_sha256s": base_collector_shas,
                "base_graph_sha256": sha256_file(base_graph),
                "base_healthy_evidence_sha256": base_healthy_digests,
                "base_healthy_row_counts": base_healthy_counts,
                "base_inventory_sha256": sha256_file(base_inventory_path),
                "base_manifest_key": base_manifest_key,
                "base_manifest_sha256": base_manifest_sha256,
                "base_metadata": base_metadata,
                "base_metadata_sha256": _canonical_sha256(base_metadata),
                "base_source_build_id": base_source_build_id,
                "collector_sha256s": sorted(
                    {*base_collector_shas, retry_collector_sha}
                ),
                "kind": "vllm-test-selection-published-base-evidence-overlay",
                "merge_revision": merge_revision,
                "merged_graph_sha256": sha256_file(output),
                "merged_metadata": merged_metadata,
                "merged_metadata_sha256": _canonical_sha256(merged_metadata),
                "overlay_script_sha256": sha256_file(Path(__file__).resolve()),
                "policy_downgrade_jobs": sorted(policy_downgrades),
                "policy_downgrades": {
                    key: {
                        "base": base_policies[key],
                        "retry": retry_policies[key],
                    }
                    for key in sorted(policy_downgrades)
                },
                "replacement_evidence_sha256": retry_digests,
                "replacement_jobs": sorted(replacements),
                "replacement_row_counts": retry_counts,
                "repository_sha": base_inventory.get("repository_sha"),
                "retry_collector_sha256": retry_collector_sha,
                "retry_inventory_sha256": sha256_file(retry_inventory_path),
                "retry_metadata": retry_metadata,
                "retry_metadata_sha256": _canonical_sha256(retry_metadata),
                "retry_source_build_id": normalized_retry_source_build_id,
                "retry_source_builds": normalized_retry_sources,
                "schema_version": 2,
            }
            provenance_output.parent.mkdir(parents=True, exist_ok=True)
            provenance_temporary = provenance_output.with_suffix(
                provenance_output.suffix + ".tmp"
            )
            provenance_temporary.write_text(
                json.dumps(provenance, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            provenance_temporary.replace(provenance_output)
    except Exception:
        output.unlink(missing_ok=True)
        output_sidecar.unlink(missing_ok=True)
        output.with_suffix(output.suffix + ".tmp").unlink(missing_ok=True)
        provenance_output.with_suffix(provenance_output.suffix + ".tmp").unlink(
            missing_ok=True
        )
        raise
    return {"metadata": merged_metadata, "provenance": provenance}


def _parse_retry_source_builds(values: Iterable[str]) -> dict[str, str] | None:
    result: dict[str, str] = {}
    for value in values:
        key, separator, build_id = value.partition("=")
        if not separator or not key or not build_id or key in result:
            raise GraphError("retry source build must be unique JOB_KEY=BUILD_UUID")
        result[key] = build_id
    return result or None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-graph", type=Path, required=True)
    parser.add_argument("--base-inventory", type=Path, required=True)
    parser.add_argument("--retry-input", type=Path, required=True)
    parser.add_argument("--retry-inventory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provenance-output", type=Path, required=True)
    parser.add_argument("--base-source-build-id", required=True)
    parser.add_argument("--retry-source-build-id")
    parser.add_argument("--retry-source-build", action="append", default=[])
    parser.add_argument("--merge-revision", required=True)
    parser.add_argument("--base-manifest-key", required=True)
    parser.add_argument("--base-manifest-sha256", required=True)
    parser.add_argument("--expected-base-healthy-count", type=int, required=True)
    parser.add_argument("--expected-base-missing-count", type=int, required=True)
    parser.add_argument("--expected-base-unhealthy-count", type=int, required=True)
    parser.add_argument("--expected-replacement-job", action="append", default=[])
    parser.add_argument("--expected-retry-missing-job", action="append", default=[])
    parser.add_argument("--expected-policy-downgrade-job", action="append", default=[])
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        retry_source_builds = _parse_retry_source_builds(args.retry_source_build)
        result = overlay_published_graph(
            args.base_graph,
            args.base_inventory,
            args.retry_input,
            args.retry_inventory,
            args.output,
            args.provenance_output,
            base_source_build_id=args.base_source_build_id,
            retry_source_build_id=args.retry_source_build_id,
            retry_source_builds=retry_source_builds,
            merge_revision=args.merge_revision,
            base_manifest_key=args.base_manifest_key,
            base_manifest_sha256=args.base_manifest_sha256,
            expected_base_healthy_count=args.expected_base_healthy_count,
            expected_base_missing_count=args.expected_base_missing_count,
            expected_base_unhealthy_count=args.expected_base_unhealthy_count,
            expected_replacements=args.expected_replacement_job,
            expected_retry_missing=args.expected_retry_missing_job,
            expected_policy_downgrades=args.expected_policy_downgrade_job,
        )
        print(json.dumps(result, sort_keys=True, indent=2))
    except Exception as error:  # noqa: BLE001 - CLI must fail closed on every defect.
        print(f"published graph overlay: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
