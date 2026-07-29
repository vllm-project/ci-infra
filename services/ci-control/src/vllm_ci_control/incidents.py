"""Deterministic lifecycle reduction for trusted canonical-main evidence."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from itertools import groupby

from .evidence import (
    GroupId,
    GroupObservation,
    Outcome,
    collapse_distinct_commits,
)


class IncidentState(StrEnum):
    """Lifecycle of one stable main-branch test group."""

    HEALTHY = "healthy"
    CANDIDATE = "candidate"
    KNOWN = "known"
    RECOVERING = "recovering"
    RESOLVED = "resolved"


@dataclass(frozen=True)
class IncidentThresholds:
    """Distinct-main-position thresholds for failure and recovery."""

    confirm_failures: int = 2
    begin_recovery: int = 1
    resolve_clean: int = 3

    def __post_init__(self) -> None:
        for label, value in (
            ("confirm_failures", self.confirm_failures),
            ("begin_recovery", self.begin_recovery),
            ("resolve_clean", self.resolve_clean),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if self.resolve_clean < self.begin_recovery:
            raise ValueError(
                "resolve_clean must be greater than or equal to begin_recovery"
            )


_DEFAULT_THRESHOLDS = IncidentThresholds()


@dataclass(frozen=True)
class Incident:
    """Current lifecycle plus the latest attempted and conclusive evidence."""

    group: GroupId
    state: IncidentState
    failure_count: int
    clean_count: int
    ever_known: bool
    latest_attempted_observation: GroupObservation | None
    last_conclusive_observation: GroupObservation | None
    last_failure_observation: GroupObservation | None
    confirmed_failure_fingerprints: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for label, value in (
            ("failure_count", self.failure_count),
            ("clean_count", self.clean_count),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{label} must be a non-negative integer")
        for observation in (
            self.latest_attempted_observation,
            self.last_conclusive_observation,
            self.last_failure_observation,
        ):
            if observation is not None and observation.group != self.group:
                raise ValueError("incident observation identities must match")
        if self.last_failure_observation is not None and (
            self.last_failure_observation.outcome not in {Outcome.FAILED, Outcome.FLAKY}
        ):
            raise ValueError("last failure observation must be failure-bearing")
        fingerprints = frozenset(self.confirmed_failure_fingerprints)
        object.__setattr__(
            self,
            "confirmed_failure_fingerprints",
            fingerprints,
        )


def reduce_incident(
    group: GroupId,
    observations: Iterable[GroupObservation],
    thresholds: IncidentThresholds = _DEFAULT_THRESHOLDS,
) -> Incident:
    """Replay one lifecycle in canonical ``(main epoch, sequence)`` order."""

    relevant = [
        observation
        for observation in collapse_distinct_commits(observations)
        if observation.group == group
    ]
    relevant.sort(key=lambda item: item.main_position)

    state = IncidentState.HEALTHY
    failure_count = 0
    clean_count = 0
    ever_known = False
    last_conclusive: GroupObservation | None = None
    last_failure: GroupObservation | None = None
    current_epoch: int | None = None
    current_definition: str | None = None
    definition_initialized = False
    fingerprint_failure_counts: dict[str, int] = {}
    confirmed_fingerprints: set[str] = set()

    for observation in relevant:
        position = observation.main_position
        assert position is not None
        if current_epoch is None:
            current_epoch = position.epoch
        elif position.epoch != current_epoch:
            (
                state,
                failure_count,
                clean_count,
            ) = _enter_main_epoch(state)
            fingerprint_failure_counts.clear()
            current_epoch = position.epoch

        if not definition_initialized:
            current_definition = observation.definition_digest
            definition_initialized = True
        elif observation.definition_digest != current_definition:
            # A changed test definition is a new incident identity. Historical
            # evidence remains available for audit, but it cannot confirm or
            # recover the new executable definition.
            state = IncidentState.HEALTHY
            failure_count = 0
            clean_count = 0
            fingerprint_failure_counts.clear()
            confirmed_fingerprints.clear()
            current_definition = observation.definition_digest

        if observation.outcome is Outcome.INCONCLUSIVE:
            continue
        last_conclusive = observation

        if observation.outcome is Outcome.FLAKY:
            clean_count = 0
            last_failure = observation
            if state in {IncidentState.KNOWN, IncidentState.RECOVERING}:
                # Preserve a previously confirmed incident, but do not treat a
                # retry-pass as a currently failing or confirming observation.
                state = IncidentState.KNOWN
                failure_count = max(
                    failure_count,
                    thresholds.confirm_failures,
                )
            elif state is IncidentState.RESOLVED:
                state = IncidentState.CANDIDATE
                failure_count = 0
            else:
                state = IncidentState.CANDIDATE
            continue

        if observation.outcome is Outcome.FAILED:
            clean_count = 0
            last_failure = observation
            if state is IncidentState.RESOLVED:
                fingerprint_failure_counts.clear()
            for fingerprint in observation.failure_fingerprints:
                count = fingerprint_failure_counts.get(fingerprint, 0) + 1
                fingerprint_failure_counts[fingerprint] = count
                if count >= thresholds.confirm_failures:
                    confirmed_fingerprints.add(fingerprint)
            if state in {IncidentState.KNOWN, IncidentState.RECOVERING}:
                state = IncidentState.KNOWN
                failure_count = max(
                    failure_count,
                    thresholds.confirm_failures,
                )
                continue
            if state is IncidentState.RESOLVED:
                failure_count = 0
            failure_count += 1
            if failure_count >= thresholds.confirm_failures:
                state = IncidentState.KNOWN
                ever_known = True
            else:
                state = IncidentState.CANDIDATE
            continue

        # A complete clean first attempt is the only recovery evidence.
        failure_count = 0
        fingerprint_failure_counts.clear()
        if state is IncidentState.CANDIDATE:
            state = IncidentState.RESOLVED if ever_known else IncidentState.HEALTHY
            clean_count = 1
        elif state in {IncidentState.KNOWN, IncidentState.RECOVERING}:
            clean_count += 1
            if clean_count >= thresholds.resolve_clean:
                state = IncidentState.RESOLVED
            elif clean_count >= thresholds.begin_recovery:
                state = IncidentState.RECOVERING
        else:
            clean_count += 1

    return Incident(
        group=group,
        state=state,
        failure_count=failure_count,
        clean_count=clean_count,
        ever_known=ever_known,
        latest_attempted_observation=relevant[-1] if relevant else None,
        last_conclusive_observation=last_conclusive,
        last_failure_observation=last_failure,
        confirmed_failure_fingerprints=frozenset(confirmed_fingerprints),
    )


def reduce_registry(
    observations: Iterable[GroupObservation],
    thresholds: IncidentThresholds = _DEFAULT_THRESHOLDS,
) -> tuple[Incident, ...]:
    """Reduce all trusted main groups into a stable registry snapshot."""

    collapsed = collapse_distinct_commits(observations)
    ordered = sorted(collapsed, key=lambda item: item.group)
    return tuple(
        reduce_incident(group, items, thresholds)
        for group, grouped in groupby(ordered, key=lambda item: item.group)
        for items in [tuple(grouped)]
    )


def _enter_main_epoch(
    state: IncidentState,
) -> tuple[IncidentState, int, int]:
    """Reset per-epoch evidence without erasing confirmed history.

    An unconfirmed candidate cannot cross a force-push boundary. A previously
    confirmed incident remains known, but recovery must restart entirely in the
    new epoch. Resolved history stays resolved and requires fresh confirmation
    before reopening.
    """

    if state is IncidentState.CANDIDATE:
        state = IncidentState.HEALTHY
    elif state is IncidentState.RECOVERING:
        state = IncidentState.KNOWN
    return state, 0, 0
