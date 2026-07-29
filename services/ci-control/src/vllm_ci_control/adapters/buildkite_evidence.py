"""Translate complete Buildkite job pages into provider-neutral evidence."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..catalog import Catalog, ExecutionKind, JobDefinition
from ..evidence import (
    GroupExpectation,
    GroupId,
    JobAttempt,
    ProviderReadAttestation,
)
from .buildkite import BuildkiteProtocolError


@dataclass(frozen=True, slots=True)
class NormalizedBuildkiteJobs:
    """Typed input for the pure evidence normalizer."""

    attempts: tuple[JobAttempt, ...]
    expectations: tuple[GroupExpectation, ...]
    read_attestation: ProviderReadAttestation


def normalize_buildkite_jobs(
    *,
    catalog: Catalog,
    pipeline: str,
    expected_job_ids: Iterable[str],
    jobs: Iterable[Mapping[str, Any]],
    all_pages_fetched: bool,
    retry_history_complete: bool,
    fingerprints_by_job_id: Mapping[str, Iterable[str]] | None = None,
) -> NormalizedBuildkiteJobs:
    """Map one planned/observed job set without inferring absent execution.

    ``expected_job_ids`` must come from the immutable build plan or trusted
    provider metadata. It must never be reconstructed from whatever jobs happen
    to be present in a selective build.
    """

    catalog_jobs = {job.job_id: job for job in catalog.jobs}
    expected = tuple(sorted(set(expected_job_ids)))
    if not expected:
        raise ValueError("expected_job_ids must not be empty")

    definitions = []
    for job_id in expected:
        try:
            definition = catalog_jobs[job_id]
        except KeyError as error:
            raise ValueError(f"unknown expected catalog job: {job_id}") from error
        if definition.pipeline != pipeline:
            raise ValueError(
                f"job {job_id!r} belongs to pipeline "
                f"{definition.pipeline!r}, not {pipeline!r}"
            )
        if definition.execution_kind is not ExecutionKind.STEP:
            raise ValueError(f"job {job_id!r} requires the opaque-pipeline normalizer")
        definitions.append(definition)

    expected_by_id = {item.job_id: item for item in definitions}
    fingerprints = fingerprints_by_job_id or {}
    attempts = []
    for raw in jobs:
        if raw.get("type", "script") != "script":
            continue
        step_key = raw.get("step_key")
        if step_key not in expected_by_id:
            continue
        definition = expected_by_id[step_key]
        shard = _shard(raw, definition)
        job_id = _required_text(raw.get("id"), label="Buildkite job id")
        attempts.append(
            JobAttempt(
                job_id=job_id,
                group=_group_id(definition),
                state=_state(raw),
                shard=shard,
                retried_in_job_id=_optional_text(
                    raw.get("retried_in_job_id"),
                    label="retried_in_job_id",
                ),
                retries_count=_retries_count(raw),
                soft_failed=_boolean(
                    raw.get("soft_failed", False),
                    label="soft_failed",
                ),
                failure_fingerprints=frozenset(fingerprints.get(job_id, ())),
            )
        )

    expectations = tuple(
        GroupExpectation(
            group=_group_id(definition),
            shards=frozenset(str(index) for index in range(definition.shards)),
            execution_profile=definition.execution_profile,
            definition_digest=definition.definition_digest,
        )
        for definition in definitions
    )
    return NormalizedBuildkiteJobs(
        attempts=tuple(attempts),
        expectations=expectations,
        read_attestation=ProviderReadAttestation(
            all_pages_fetched=all_pages_fetched,
            retry_history_complete=retry_history_complete,
        ),
    )


def normalize_opaque_buildkite_lane(
    *,
    catalog: Catalog,
    pipeline: str,
    lane_job_id: str,
    jobs: Iterable[Mapping[str, Any]],
    all_pages_fetched: bool,
    retry_history_complete: bool,
    fingerprints_by_job_id: Mapping[str, Iterable[str]] | None = None,
) -> NormalizedBuildkiteJobs:
    """Aggregate one deliberately opaque whole-pipeline target.

    Native AMD currently has no stable provider step keys. Its catalog entry
    therefore records the expected number of executable units, and this
    adapter maps complete retry lineages for the whole build into one logical
    group. Direct step selection must use :func:`normalize_buildkite_jobs`.
    """

    definitions = {job.job_id: job for job in catalog.jobs}
    try:
        definition = definitions[lane_job_id]
    except KeyError as error:
        raise ValueError(f"unknown opaque lane job: {lane_job_id}") from error
    if definition.pipeline != pipeline:
        raise ValueError(
            f"job {lane_job_id!r} belongs to pipeline "
            f"{definition.pipeline!r}, not {pipeline!r}"
        )
    if definition.execution_kind is not ExecutionKind.OPAQUE_PIPELINE:
        raise ValueError(f"job {lane_job_id!r} is not an opaque pipeline")

    script_jobs = [raw for raw in jobs if raw.get("type", "script") == "script"]
    raw_by_id: dict[str, Mapping[str, Any]] = {}
    for raw in script_jobs:
        job_id = _required_text(raw.get("id"), label="Buildkite job id")
        if job_id in raw_by_id:
            raise BuildkiteProtocolError("Buildkite job ids must be unique")
        raw_by_id[job_id] = raw

    predecessor_ids = {
        job_id
        for job_id, raw in raw_by_id.items()
        if raw.get("retried_in_job_id") is not None
    }
    terminal_ids = sorted(set(raw_by_id) - predecessor_ids)
    shard_by_terminal = {
        job_id: str(index) for index, job_id in enumerate(terminal_ids)
    }
    fingerprints = fingerprints_by_job_id or {}
    group = _group_id(definition)
    attempts = []
    for job_id, raw in raw_by_id.items():
        terminal_id = _terminal_job_id(job_id, raw_by_id)
        shard = shard_by_terminal.get(
            terminal_id,
            f"invalid:{job_id}",
        )
        attempts.append(
            JobAttempt(
                job_id=job_id,
                group=group,
                state=_state(raw),
                shard=shard,
                retried_in_job_id=_optional_text(
                    raw.get("retried_in_job_id"),
                    label="retried_in_job_id",
                ),
                retries_count=_retries_count(raw),
                soft_failed=_boolean(
                    raw.get("soft_failed", False),
                    label="soft_failed",
                ),
                failure_fingerprints=frozenset(fingerprints.get(job_id, ())),
            )
        )

    return NormalizedBuildkiteJobs(
        attempts=tuple(attempts),
        expectations=(
            GroupExpectation(
                group=group,
                shards=frozenset(str(index) for index in range(definition.shards)),
                execution_profile=definition.execution_profile,
                definition_digest=definition.definition_digest,
            ),
        ),
        read_attestation=ProviderReadAttestation(
            all_pages_fetched=all_pages_fetched,
            retry_history_complete=retry_history_complete,
        ),
    )


def _group_id(definition: JobDefinition) -> GroupId:
    return GroupId(
        pipeline=definition.pipeline,
        step_key=definition.job_id,
        variant=definition.execution_profile,
    )


def _shard(raw: Mapping[str, Any], definition: JobDefinition) -> str:
    index = raw.get("parallel_group_index")
    total = raw.get("parallel_group_total")
    if definition.shards == 1:
        if index not in {None, 0} or total not in {None, 1}:
            raise BuildkiteProtocolError(
                f"job {definition.job_id!r} has unexpected shard metadata"
            )
        return "0"
    if (
        isinstance(index, bool)
        or not isinstance(index, int)
        or index < 0
        or index >= definition.shards
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total != definition.shards
    ):
        raise BuildkiteProtocolError(
            f"job {definition.job_id!r} shard metadata disagrees with catalog"
        )
    return str(index)


def _state(raw: Mapping[str, Any]) -> str:
    value = _required_text(raw.get("state"), label="job state")
    if value != "finished":
        return value
    exit_status = raw.get("exit_status")
    if exit_status == 0:
        return "passed"
    if isinstance(exit_status, int) and not isinstance(exit_status, bool):
        return "failed"
    return "unknown"


def _required_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BuildkiteProtocolError(f"{label} must be non-empty text")
    return value


def _optional_text(value: Any, *, label: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, label=label)


def _optional_non_negative_int(value: Any, *, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BuildkiteProtocolError(f"{label} must be a non-negative integer or null")
    return value


def _retries_count(raw: Mapping[str, Any]) -> int | None:
    """Normalize Buildkite's explicit null for an initial attempt to zero.

    A missing field is different: without the provider's retry counter the
    evidence reducer cannot prove that it received a complete lineage.
    """

    if "retries_count" not in raw:
        return None
    value = raw["retries_count"]
    if value is None:
        return 0
    return _optional_non_negative_int(value, label="retries_count")


def _terminal_job_id(
    job_id: str,
    raw_by_id: Mapping[str, Mapping[str, Any]],
) -> str:
    seen = set()
    current = job_id
    while current not in seen:
        seen.add(current)
        successor = raw_by_id[current].get("retried_in_job_id")
        if successor is None:
            return current
        if not isinstance(successor, str) or successor not in raw_by_id:
            return f"invalid:{job_id}"
        current = successor
    return f"invalid:{job_id}"


def _boolean(value: Any, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise BuildkiteProtocolError(f"{label} must be boolean")
    return value
