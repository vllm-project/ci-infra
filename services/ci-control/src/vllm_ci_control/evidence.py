"""Normalize provider job attempts into conservative, comparable observations."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_FAILED_STATES = frozenset({"failed", "timed_out", "expired"})
_CLEAN_STATES = frozenset({"passed"})
_MAX_METADATA_LENGTH = 240


class Outcome(StrEnum):
    """A conservative outcome for one stable test group on one commit."""

    CLEAN = "clean"
    FAILED = "failed"
    FLAKY = "flaky"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, order=True)
class GroupId:
    """Stable identity shared by main and pull-request observations."""

    pipeline: str
    step_key: str
    variant: str = "default"

    def __post_init__(self) -> None:
        for name, value in (
            ("pipeline", self.pipeline),
            ("step_key", self.step_key),
            ("variant", self.variant),
        ):
            _validate_metadata(value, label=name)

    @property
    def display(self) -> str:
        """Return a compact, unambiguous representation for user output."""

        suffix = "" if self.variant == "default" else f" [{self.variant}]"
        return f"{self.pipeline}/{self.step_key}{suffix}"


@dataclass(frozen=True, order=True)
class MainPosition:
    """Canonical ordering assigned by the trusted default-branch registry."""

    epoch: int
    sequence: int

    def __post_init__(self) -> None:
        for label, value in (("epoch", self.epoch), ("sequence", self.sequence)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"main {label} must be a positive integer")


@dataclass(frozen=True)
class ProviderReadAttestation:
    """Provider-read facts required before an execution can be conclusive."""

    all_pages_fetched: bool
    retry_history_complete: bool

    def __post_init__(self) -> None:
        if not isinstance(self.all_pages_fetched, bool):
            raise TypeError("all_pages_fetched must be boolean")
        if not isinstance(self.retry_history_complete, bool):
            raise TypeError("retry_history_complete must be boolean")

    @property
    def complete(self) -> bool:
        return self.all_pages_fetched and self.retry_history_complete


@dataclass(frozen=True)
class GroupExpectation:
    """Trusted execution metadata and exact shards expected from one group."""

    group: GroupId
    shards: frozenset[str]
    execution_profile: str | None = None
    definition_digest: str | None = None

    def __post_init__(self) -> None:
        shards = frozenset(self.shards)
        if not shards:
            raise ValueError("expected shard set must not be empty")
        for shard in shards:
            _validate_metadata(shard, label="expected shard")
        _validate_optional_metadata(
            self.execution_profile,
            label="execution profile",
        )
        _validate_optional_metadata(
            self.definition_digest,
            label="definition digest",
        )
        object.__setattr__(self, "shards", shards)


@dataclass(frozen=True)
class JobAttempt:
    """Provider-neutral facts about one execution attempt."""

    job_id: str
    group: GroupId
    state: str
    shard: str = "0"
    retried_in_job_id: str | None = None
    retries_count: int | None = None
    soft_failed: bool = False
    failure_fingerprints: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        _validate_metadata(self.job_id, label="job id")
        _validate_metadata(self.shard, label="shard")
        _validate_metadata(self.state, label="job state")
        if self.retried_in_job_id is not None:
            _validate_metadata(
                self.retried_in_job_id,
                label="retried-in job id",
            )
        if self.retries_count is not None and (
            isinstance(self.retries_count, bool)
            or not isinstance(self.retries_count, int)
            or self.retries_count < 0
        ):
            raise ValueError("retries_count must be a non-negative integer")
        if not isinstance(self.soft_failed, bool):
            raise TypeError("soft_failed must be boolean")
        fingerprints = _fingerprint_set(self.failure_fingerprints)
        object.__setattr__(self, "failure_fingerprints", fingerprints)


@dataclass(frozen=True)
class GroupObservation:
    """Evidence for one group in one completed provider build."""

    group: GroupId
    commit: str
    outcome: Outcome
    build_number: int
    observed_at: datetime
    build_url: str
    trusted_main: bool
    main_position: MainPosition | None
    execution_profile: str | None = None
    definition_digest: str | None = None
    failure_fingerprints: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.commit:
            raise ValueError("commit must not be empty")
        if self.trusted_main and _COMMIT_PATTERN.fullmatch(self.commit) is None:
            raise ValueError("trusted main commit must be a full lowercase SHA")
        if self.build_number < 1:
            raise ValueError("build_number must be positive")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        _validate_metadata(self.build_url, label="build URL")
        if not isinstance(self.trusted_main, bool):
            raise TypeError("trusted_main must be boolean")
        if self.trusted_main != (self.main_position is not None):
            raise ValueError("trusted main evidence requires a canonical main position")
        _validate_optional_metadata(
            self.execution_profile,
            label="execution profile",
        )
        _validate_optional_metadata(
            self.definition_digest,
            label="definition digest",
        )
        fingerprints = _fingerprint_set(self.failure_fingerprints)
        if self.outcome in {Outcome.CLEAN, Outcome.INCONCLUSIVE} and fingerprints:
            raise ValueError("only failed or flaky observations may carry fingerprints")
        object.__setattr__(self, "failure_fingerprints", fingerprints)


def observe_attempts(
    *,
    attempts: Iterable[JobAttempt],
    expectations: Iterable[GroupExpectation],
    read_attestation: ProviderReadAttestation,
    build_number: int,
    build_url: str,
    commit: str,
    observed_at: datetime,
    trusted_main: bool,
    main_position: MainPosition | None,
) -> tuple[GroupObservation, ...]:
    """Aggregate complete retry lineages and exact shard sets.

    A clean result requires a complete provider read, every expected current
    shard, and a zero-retry passing attempt for every shard. Missing expected
    groups are inconclusive; callers omit groups that were intentionally not
    selected.
    """

    by_group: dict[GroupId, list[JobAttempt]] = defaultdict(list)
    for attempt in attempts:
        by_group[attempt.group].append(attempt)

    expected_by_group: dict[GroupId, GroupExpectation] = {}
    for expectation in expectations:
        if expectation.group in expected_by_group:
            raise ValueError("group expectations must be unique")
        expected_by_group[expectation.group] = expectation

    observations = []
    for group in sorted(set(by_group) | set(expected_by_group)):
        expectation = expected_by_group.get(group)
        if expectation is None:
            outcome, fingerprints = Outcome.INCONCLUSIVE, frozenset()
            execution_profile = None
            definition_digest = None
        else:
            outcome, fingerprints = _observe_group(
                by_group.get(group, []),
                expected_shards=expectation.shards,
                read_attestation=read_attestation,
            )
            execution_profile = expectation.execution_profile
            definition_digest = expectation.definition_digest
        observations.append(
            GroupObservation(
                group=group,
                commit=commit,
                outcome=outcome,
                build_number=build_number,
                observed_at=observed_at,
                build_url=build_url,
                trusted_main=trusted_main,
                main_position=main_position,
                execution_profile=execution_profile,
                definition_digest=definition_digest,
                failure_fingerprints=fingerprints,
            )
        )
    return tuple(observations)


def _observe_group(
    attempts: list[JobAttempt],
    *,
    expected_shards: frozenset[str],
    read_attestation: ProviderReadAttestation,
) -> tuple[Outcome, frozenset[str]]:
    if not read_attestation.complete or not attempts:
        return Outcome.INCONCLUSIVE, frozenset()

    by_id = {attempt.job_id: attempt for attempt in attempts}
    if len(by_id) != len(attempts):
        raise ValueError("job_id values must be unique within a build")
    if any(attempt.retries_count is None for attempt in attempts):
        return Outcome.INCONCLUSIVE, frozenset()
    if any(attempt.shard not in expected_shards for attempt in attempts):
        return Outcome.INCONCLUSIVE, frozenset()
    if any(
        attempt.retried_in_job_id is not None and attempt.retried_in_job_id not in by_id
        for attempt in attempts
    ):
        return Outcome.INCONCLUSIVE, frozenset()

    predecessor_ids = {
        attempt.job_id for attempt in attempts if attempt.retried_in_job_id is not None
    }
    current = [attempt for attempt in attempts if attempt.job_id not in predecessor_ids]
    if {attempt.shard for attempt in current} != expected_shards:
        return Outcome.INCONCLUSIVE, frozenset()
    if len(current) != len(expected_shards):
        return Outcome.INCONCLUSIVE, frozenset()

    shard_results = []
    covered_ids: set[str] = set()
    for attempt in current:
        previous = _find_predecessors(attempt.job_id, by_id)
        if previous is None:
            return Outcome.INCONCLUSIVE, frozenset()
        expected_counts = tuple(
            range(attempt.retries_count - 1, -1, -1)  # type: ignore[operator]
        )
        actual_counts = tuple(item.retries_count for item in previous)
        if len(previous) != attempt.retries_count or actual_counts != expected_counts:
            return Outcome.INCONCLUSIVE, frozenset()
        lineage_ids = {attempt.job_id, *(item.job_id for item in previous)}
        if covered_ids & lineage_ids:
            return Outcome.INCONCLUSIVE, frozenset()
        covered_ids.update(lineage_ids)
        shard_results.append(_observe_current_attempt(attempt, previous))

    if covered_ids != set(by_id):
        return Outcome.INCONCLUSIVE, frozenset()

    outcomes = [outcome for outcome, _ in shard_results]
    if Outcome.FAILED in outcomes:
        outcome = Outcome.FAILED
    elif Outcome.INCONCLUSIVE in outcomes:
        outcome = Outcome.INCONCLUSIVE
    elif Outcome.FLAKY in outcomes:
        outcome = Outcome.FLAKY
    else:
        outcome = Outcome.CLEAN

    if outcome in {Outcome.FAILED, Outcome.FLAKY}:
        fingerprints = frozenset(
            fingerprint
            for shard_outcome, shard_fingerprints in shard_results
            if shard_outcome in {Outcome.FAILED, Outcome.FLAKY}
            for fingerprint in shard_fingerprints
        )
    else:
        fingerprints = frozenset()
    return outcome, fingerprints


def _observe_current_attempt(
    current: JobAttempt,
    previous: tuple[JobAttempt, ...],
) -> tuple[Outcome, frozenset[str]]:
    if current.soft_failed or current.state in _FAILED_STATES:
        return Outcome.FAILED, current.failure_fingerprints
    if current.state not in _CLEAN_STATES:
        return Outcome.INCONCLUSIVE, frozenset()
    if not previous:
        return Outcome.CLEAN, frozenset()

    failed_predecessors = [
        attempt
        for attempt in previous
        if attempt.soft_failed or attempt.state in _FAILED_STATES
    ]
    if failed_predecessors:
        return (
            Outcome.FLAKY,
            frozenset(
                fingerprint
                for attempt in failed_predecessors
                for fingerprint in attempt.failure_fingerprints
            ),
        )
    # A retried pass is never clean first-attempt evidence. If the preceding
    # state was not a recognized failure, the lineage is inconclusive.
    return Outcome.INCONCLUSIVE, frozenset()


def _find_predecessors(
    current_job_id: str,
    attempts_by_id: Mapping[str, JobAttempt],
) -> tuple[JobAttempt, ...] | None:
    """Follow reverse Buildkite retry links while rejecting malformed graphs."""

    predecessors = []
    seen = {current_job_id}
    cursor = current_job_id
    while True:
        matches = [
            attempt
            for attempt in attempts_by_id.values()
            if attempt.retried_in_job_id == cursor
        ]
        if not matches:
            return tuple(predecessors)
        if len(matches) != 1:
            return None
        predecessor = matches[0]
        if predecessor.job_id in seen:
            return None
        successor = attempts_by_id[cursor]
        if predecessor.group != successor.group or predecessor.shard != successor.shard:
            return None
        seen.add(predecessor.job_id)
        predecessors.append(predecessor)
        cursor = predecessor.job_id


def collapse_distinct_commits(
    observations: Iterable[GroupObservation],
) -> tuple[GroupObservation, ...]:
    """Collapse rebuilds so one main SHA contributes once per main epoch."""

    values = tuple(observations)
    _validate_trusted_main_positions(values)
    by_identity: dict[
        tuple[GroupId, int, str],
        list[GroupObservation],
    ] = defaultdict(list)
    for observation in values:
        position = observation.main_position
        assert position is not None
        by_identity[(observation.group, position.epoch, observation.commit)].append(
            observation
        )

    collapsed = []
    for (group, _epoch, commit), items in by_identity.items():
        positions = {item.main_position for item in items}
        if len(positions) != 1:
            raise ValueError("one main commit cannot have multiple positions")
        items.sort(key=lambda item: (item.observed_at, item.build_number))
        latest = items[-1]
        outcomes = {item.outcome for item in items}
        profiles = {item.execution_profile for item in items}
        definitions = {item.definition_digest for item in items}
        if latest.outcome is Outcome.INCONCLUSIVE:
            # Preserve the newest attempted rebuild as incomplete. Otherwise
            # its timestamp could make older conclusive evidence look current.
            outcome = Outcome.INCONCLUSIVE
        elif len(profiles) != 1 or len(definitions) != 1:
            outcome = Outcome.INCONCLUSIVE
        elif Outcome.FAILED in outcomes:
            outcome = (
                Outcome.FLAKY
                if Outcome.CLEAN in outcomes or Outcome.FLAKY in outcomes
                else Outcome.FAILED
            )
        elif Outcome.FLAKY in outcomes:
            outcome = Outcome.FLAKY
        elif Outcome.CLEAN in outcomes:
            outcome = Outcome.CLEAN
        else:
            outcome = Outcome.INCONCLUSIVE
        fingerprints = (
            frozenset(
                fingerprint
                for item in items
                if item.outcome in {Outcome.FAILED, Outcome.FLAKY}
                for fingerprint in item.failure_fingerprints
            )
            if outcome in {Outcome.FAILED, Outcome.FLAKY}
            else frozenset()
        )
        collapsed.append(
            GroupObservation(
                group=group,
                commit=commit,
                outcome=outcome,
                build_number=latest.build_number,
                observed_at=latest.observed_at,
                build_url=latest.build_url,
                trusted_main=True,
                main_position=latest.main_position,
                execution_profile=(
                    latest.execution_profile if len(profiles) == 1 else None
                ),
                definition_digest=(
                    latest.definition_digest if len(definitions) == 1 else None
                ),
                failure_fingerprints=fingerprints,
            )
        )
    return tuple(
        sorted(
            collapsed,
            key=lambda item: (
                item.main_position,
                item.group,
            ),
        )
    )


def _validate_trusted_main_positions(
    observations: tuple[GroupObservation, ...],
) -> None:
    commits_by_position: dict[MainPosition, str] = {}
    for observation in observations:
        if not observation.trusted_main or observation.main_position is None:
            raise ValueError(
                "incident evidence must have trusted canonical-main provenance"
            )
        existing = commits_by_position.setdefault(
            observation.main_position,
            observation.commit,
        )
        if existing != observation.commit:
            raise ValueError("one main position cannot identify multiple commits")


def _fingerprint_set(values: Iterable[str]) -> frozenset[str]:
    result = frozenset(values)
    for value in result:
        _validate_metadata(value, label="failure fingerprint")
    return result


def _validate_optional_metadata(value: str | None, *, label: str) -> None:
    if value is not None:
        _validate_metadata(value, label=label)


def _validate_metadata(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_METADATA_LENGTH
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be bounded normalized printable text")
    return value
