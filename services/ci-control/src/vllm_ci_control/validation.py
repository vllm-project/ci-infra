"""Compare an exact PR run with one immutable canonical-main snapshot."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from .evidence import (
    GroupId,
    GroupObservation,
    MainPosition,
    Outcome,
    collapse_distinct_commits,
)
from .incidents import Incident, IncidentState

_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_MAX_TEXT_LENGTH = 500


class Classification(StrEnum):
    """Advisory relationship between a PR failure and current main evidence."""

    KNOWN_ON_MAIN = "known_on_main"
    CANDIDATE_ON_MAIN = "candidate_on_main"
    SEEN_FLAKY_ON_MAIN = "seen_flaky_on_main"
    RECOVERING_ON_MAIN = "recovering_on_main"
    RESOLVED_ON_MAIN = "resolved_on_main"
    DIFFERENT_FAILURE_ON_MAIN = "different_failure_on_main"
    GROUP_ALSO_RED_ON_MAIN = "group_also_red_on_main"
    NOT_MATCHED_ON_MAIN = "not_matched_on_main"
    UNABLE_TO_CLASSIFY = "unable_to_classify"


class LaneCompleteness(StrEnum):
    """Completeness of one provider lane at a frozen scan watermark."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, order=True)
class LaneSnapshot:
    """One lane's frozen completeness and canonical-main watermark."""

    lane: str
    completeness: LaneCompleteness
    watermark: MainPosition | None

    def __post_init__(self) -> None:
        _validate_text(self.lane, label="lane")
        if self.completeness is LaneCompleteness.COMPLETE and self.watermark is None:
            raise ValueError("a complete lane requires a main watermark")


@dataclass(frozen=True)
class SnapshotGroup:
    """One incident and its evidence captured atomically."""

    incident: Incident
    history: tuple[GroupObservation, ...] = ()

    def __post_init__(self) -> None:
        if self.incident.latest_attempted_observation is None:
            raise ValueError("snapshot incidents require attempted evidence")
        history = tuple(self.history)
        latest = self.incident.latest_attempted_observation
        assert latest is not None
        if latest not in history:
            history = (*history, latest)
        for observation in history:
            if observation.group != self.incident.group:
                raise ValueError("snapshot history identities must match")
            if not observation.trusted_main or observation.main_position is None:
                raise ValueError(
                    "snapshot history must have trusted canonical-main provenance"
                )
        object.__setattr__(
            self,
            "history",
            tuple(sorted(history, key=lambda item: item.main_position)),
        )

    @property
    def latest_observation(self) -> GroupObservation:
        observation = self.incident.latest_attempted_observation
        assert observation is not None
        return observation


@dataclass(frozen=True)
class MainSnapshot:
    """A revisioned, immutable view used for one complete validation."""

    revision: str
    catalog_version: str
    catalog_digest: str
    policy_revision: str
    algorithm_revision: str
    generated_at: datetime
    max_evidence_age: timedelta
    lanes: tuple[LaneSnapshot, ...]
    groups: tuple[SnapshotGroup, ...]

    def __post_init__(self) -> None:
        for label, value in (
            ("revision", self.revision),
            ("catalog version", self.catalog_version),
            ("catalog digest", self.catalog_digest),
            ("policy revision", self.policy_revision),
            ("algorithm revision", self.algorithm_revision),
        ):
            _validate_text(value, label=label)
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if self.max_evidence_age < timedelta(0):
            raise ValueError("max_evidence_age must not be negative")

        lane_ids = [item.lane for item in self.lanes]
        if len(lane_ids) != len(set(lane_ids)):
            raise ValueError("snapshot contains duplicate lanes")
        identities = [item.incident.group for item in self.groups]
        if len(identities) != len(set(identities)):
            raise ValueError("snapshot contains duplicate group identities")

        lanes = self.by_lane
        for item in self.groups:
            lane = lanes.get(item.incident.group.pipeline)
            if lane is None:
                raise ValueError("snapshot group has no corresponding lane")
            for observation in item.history:
                if (
                    lane.watermark is not None
                    and observation.main_position > lane.watermark
                ):
                    raise ValueError("group evidence is newer than its lane watermark")

    @property
    def by_group(self) -> dict[GroupId, SnapshotGroup]:
        return {item.incident.group: item for item in self.groups}

    @property
    def by_lane(self) -> dict[str, LaneSnapshot]:
        return {item.lane: item for item in self.lanes}


@dataclass(frozen=True)
class PullRequestRunSubject:
    """Immutable identity of the PR and provider run being classified."""

    repository: str
    pull_request_number: int
    head_sha: str
    tested_head_sha: str
    base_sha: str
    base_ancestor_shas: frozenset[str]

    def __post_init__(self) -> None:
        _validate_text(self.repository, label="repository")
        if self.pull_request_number < 1:
            raise ValueError("pull_request_number must be positive")
        for label, value in (
            ("head SHA", self.head_sha),
            ("tested head SHA", self.tested_head_sha),
            ("base SHA", self.base_sha),
        ):
            _validate_sha(value, label=label)
        ancestors = frozenset(self.base_ancestor_shas)
        for value in ancestors:
            _validate_sha(value, label="base ancestor SHA")
        if self.base_sha not in ancestors:
            raise ValueError("base_ancestor_shas must include base_sha")
        object.__setattr__(self, "base_ancestor_shas", ancestors)


@dataclass(frozen=True)
class PullRequestFailure:
    """One current failing PR group, optionally with normalized identity."""

    label: str
    group: GroupId | None
    build_url: str
    failure_fingerprint: str | None = None
    execution_profile: str | None = None
    definition_digest: str | None = None

    def __post_init__(self) -> None:
        _validate_text(self.label, label="failure label")
        _validate_text(self.build_url, label="failure build URL")
        for label, value in (
            ("failure fingerprint", self.failure_fingerprint),
            ("execution profile", self.execution_profile),
            ("definition digest", self.definition_digest),
        ):
            if value is not None:
                _validate_text(value, label=label)


@dataclass(frozen=True)
class ClassifiedFailure:
    """Validation result for one PR failure."""

    failure: PullRequestFailure
    classification: Classification
    explanation: str
    main_evidence: GroupObservation | None = None


@dataclass(frozen=True)
class ValidationReport:
    """All classifications produced against exactly one snapshot revision."""

    pull_request_head: str
    pull_request_base: str
    snapshot_revision: str
    evaluated_at: datetime
    stale_head: bool
    stale_base: bool
    results: tuple[ClassifiedFailure, ...]


def create_snapshot(
    *,
    catalog_version: str,
    catalog_digest: str,
    policy_revision: str,
    algorithm_revision: str,
    generated_at: datetime,
    max_evidence_age: timedelta,
    lanes: Iterable[LaneSnapshot],
    incidents: Iterable[Incident],
    observations: Iterable[GroupObservation] = (),
) -> MainSnapshot:
    """Create a digest over every frozen field that affects classification."""

    lane_values = tuple(sorted(lanes, key=lambda item: item.lane))
    history_by_group: dict[GroupId, list[GroupObservation]] = {}
    for observation in collapse_distinct_commits(observations):
        history_by_group.setdefault(observation.group, []).append(observation)
    groups = tuple(
        SnapshotGroup(
            incident=incident,
            history=tuple(history_by_group.get(incident.group, ())),
        )
        for incident in sorted(incidents, key=lambda item: item.group)
        if incident.latest_attempted_observation is not None
    )
    payload = {
        "algorithm_revision": algorithm_revision,
        "catalog_digest": catalog_digest,
        "catalog_version": catalog_version,
        "generated_at": generated_at.isoformat(),
        "groups": [
            {
                "history": [
                    _serialize_observation(observation) for observation in item.history
                ],
                "incident": _serialize_incident(item.incident),
            }
            for item in groups
        ],
        "lanes": [
            {
                "completeness": item.completeness.value,
                "lane": item.lane,
                "watermark": _serialize_position(item.watermark),
            }
            for item in lane_values
        ],
        "max_evidence_age_microseconds": _timedelta_microseconds(max_evidence_age),
        "policy_revision": policy_revision,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    revision = hashlib.sha256(encoded).hexdigest()
    return MainSnapshot(
        revision=revision,
        catalog_version=catalog_version,
        catalog_digest=catalog_digest,
        policy_revision=policy_revision,
        algorithm_revision=algorithm_revision,
        generated_at=generated_at,
        max_evidence_age=max_evidence_age,
        lanes=lane_values,
        groups=groups,
    )


def validate_pull_request(
    *,
    subject: PullRequestRunSubject,
    current_head: str,
    current_base: str,
    pr_catalog_version: str,
    pr_catalog_digest: str,
    failures: Iterable[PullRequestFailure],
    snapshot: MainSnapshot,
    now: datetime,
) -> ValidationReport:
    """Classify failures without mixing heads, bases, lanes, or revisions."""

    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    _validate_sha(current_head, label="current head SHA")
    _validate_sha(current_base, label="current base SHA")
    _validate_text(pr_catalog_version, label="PR catalog version")
    _validate_text(pr_catalog_digest, label="PR catalog digest")
    failure_list = tuple(failures)

    if subject.head_sha != current_head:
        return _unable_report(
            subject=subject,
            snapshot=snapshot,
            failures=failure_list,
            evaluated_at=now,
            stale_head=True,
            stale_base=False,
            explanation=(
                "The pull-request head changed during validation; run status "
                "again for the current commit."
            ),
        )
    if subject.base_sha != current_base:
        return _unable_report(
            subject=subject,
            snapshot=snapshot,
            failures=failure_list,
            evaluated_at=now,
            stale_head=False,
            stale_base=True,
            explanation=(
                "The pull-request base changed during validation; refresh the "
                "exact-head run subject."
            ),
        )
    if subject.tested_head_sha != subject.head_sha:
        return _unable_report(
            subject=subject,
            snapshot=snapshot,
            failures=failure_list,
            evaluated_at=now,
            stale_head=False,
            stale_base=False,
            explanation=(
                "The provider result was not produced from the exact requested "
                "pull-request head."
            ),
        )
    if pr_catalog_version != snapshot.catalog_version:
        return _unable_report(
            subject=subject,
            snapshot=snapshot,
            failures=failure_list,
            evaluated_at=now,
            stale_head=False,
            stale_base=False,
            explanation=("The PR and main builds use incompatible CI catalog schemas."),
        )

    snapshot_groups = snapshot.by_group
    snapshot_lanes = snapshot.by_lane
    results = []
    for failure in failure_list:
        if failure.group is None:
            results.append(
                _unable(
                    failure,
                    "The failing job has no stable step key.",
                )
            )
            continue

        lane = snapshot_lanes.get(failure.group.pipeline)
        if lane is None or lane.completeness is not LaneCompleteness.COMPLETE:
            results.append(
                _unable(
                    failure,
                    "The comparable main lane is partial or unavailable.",
                )
            )
            continue

        main = snapshot_groups.get(failure.group)
        if main is None:
            results.append(
                _unable(
                    failure,
                    "The stable group has no comparable main observation.",
                )
            )
            continue

        latest = main.latest_observation
        age = now - latest.observed_at
        if age < timedelta(0) or age > snapshot.max_evidence_age:
            results.append(
                _unable(
                    failure,
                    "The latest comparable main evidence is stale.",
                    latest,
                )
            )
            continue
        if latest.outcome is Outcome.INCONCLUSIVE:
            results.append(
                _unable(
                    failure,
                    "The latest main attempt is incomplete or inconclusive.",
                    latest,
                )
            )
            continue

        results.append(
            _classify(
                failure,
                main,
                subject,
                now=now,
                max_evidence_age=snapshot.max_evidence_age,
            )
        )

    return ValidationReport(
        pull_request_head=subject.head_sha,
        pull_request_base=subject.base_sha,
        snapshot_revision=snapshot.revision,
        evaluated_at=now,
        stale_head=False,
        stale_base=False,
        results=tuple(results),
    )


class _FingerprintRelation(StrEnum):
    EXACT = "exact"
    DIFFERENT = "different"
    UNVERIFIED = "unverified"


def _classify(
    failure: PullRequestFailure,
    main: SnapshotGroup,
    subject: PullRequestRunSubject,
    *,
    now: datetime,
    max_evidence_age: timedelta,
) -> ClassifiedFailure:
    incident = main.incident
    latest = main.latest_observation
    if not _execution_compatible(failure, latest):
        return _unable(
            failure,
            "Main evidence lacks a compatible execution profile or "
            "test-definition digest.",
            latest,
        )
    relation = _fingerprint_relation(failure, latest)

    if latest.outcome is Outcome.FAILED:
        if relation is _FingerprintRelation.EXACT:
            fingerprint_confirmed = (
                failure.failure_fingerprint in incident.confirmed_failure_fingerprints
            )
            if incident.state is IncidentState.KNOWN and fingerprint_confirmed:
                return _classified(
                    failure,
                    Classification.KNOWN_ON_MAIN,
                    "The exact compatible failure fingerprint is confirmed "
                    "and currently failing on main.",
                    latest,
                )
            if incident.state in {
                IncidentState.CANDIDATE,
                IncidentState.KNOWN,
            }:
                return _classified(
                    failure,
                    Classification.CANDIDATE_ON_MAIN,
                    "The exact compatible failure fingerprint is currently "
                    "failing on main but is below the confirmation threshold.",
                    latest,
                )
        if relation is _FingerprintRelation.DIFFERENT:
            return _classified(
                failure,
                Classification.DIFFERENT_FAILURE_ON_MAIN,
                "The compatible group is red on main, but its latest normalized "
                "failure fingerprint differs from the PR failure.",
                latest,
            )
        return _classified(
            failure,
            Classification.GROUP_ALSO_RED_ON_MAIN,
            "The same job group is also red on main; the underlying failure is "
            "unverified.",
            latest,
        )

    if latest.outcome is Outcome.FLAKY:
        if relation is _FingerprintRelation.DIFFERENT:
            return _classified(
                failure,
                Classification.DIFFERENT_FAILURE_ON_MAIN,
                "The main group retried to pass, and its observed failure "
                "fingerprint differs from the PR failure.",
                latest,
            )
        explanation = (
            "The exact compatible failure fingerprint was seen on main before "
            "a later pass on the same SHA."
            if relation is _FingerprintRelation.EXACT
            else (
                "The same group failed and later passed on one main SHA; the "
                "underlying failure is unverified."
            )
        )
        return _classified(
            failure,
            Classification.SEEN_FLAKY_ON_MAIN,
            explanation,
            latest,
        )

    if latest.outcome is Outcome.CLEAN:
        last_failure = incident.last_failure_observation
        if (
            last_failure is not None
            and _fingerprint_relation(failure, last_failure)
            is _FingerprintRelation.EXACT
            and failure.failure_fingerprint in incident.confirmed_failure_fingerprints
            and _execution_compatible(failure, latest)
        ):
            if incident.state is IncidentState.RECOVERING:
                return _classified(
                    failure,
                    Classification.RECOVERING_ON_MAIN,
                    "The exact compatible main failure is now passing but has "
                    "not reached the clean-resolution threshold.",
                    latest,
                )
            if incident.state is IncidentState.RESOLVED:
                return _classified(
                    failure,
                    Classification.RESOLVED_ON_MAIN,
                    "The exact compatible main failure reached the configured "
                    "clean-resolution threshold.",
                    latest,
                )

        if incident.state in {
            IncidentState.RECOVERING,
            IncidentState.RESOLVED,
        }:
            return _unable(
                failure,
                "The main group has recovering or resolved history, but the "
                "underlying failure identity is unverified.",
                latest,
            )
        control = _latest_base_control(
            failure=failure,
            main=main,
            subject=subject,
            now=now,
            max_evidence_age=max_evidence_age,
        )
        if control is None:
            return _unable(
                failure,
                "No fresh compatible clean observation is a trusted ancestor "
                "of the pinned pull-request base.",
                latest,
            )
        return _classified(
            failure,
            Classification.NOT_MATCHED_ON_MAIN,
            "The compatible group ran clean on a trusted ancestor of the "
            "pinned PR base. This is a possible regression, not proof of "
            "causation.",
            control,
        )

    return _unable(
        failure,
        "Current main evidence cannot prove a comparable result.",
        latest,
    )


def _latest_base_control(
    *,
    failure: PullRequestFailure,
    main: SnapshotGroup,
    subject: PullRequestRunSubject,
    now: datetime,
    max_evidence_age: timedelta,
) -> GroupObservation | None:
    candidates = []
    for observation in main.history:
        age = now - observation.observed_at
        if (
            observation.outcome is Outcome.CLEAN
            and observation.commit in subject.base_ancestor_shas
            and timedelta(0) <= age <= max_evidence_age
            and _execution_compatible(failure, observation)
        ):
            candidates.append(observation)
    return max(candidates, key=lambda item: item.main_position, default=None)


def _fingerprint_relation(
    failure: PullRequestFailure,
    observation: GroupObservation,
) -> _FingerprintRelation:
    if not _execution_compatible(failure, observation):
        return _FingerprintRelation.UNVERIFIED
    if failure.failure_fingerprint is None or not observation.failure_fingerprints:
        return _FingerprintRelation.UNVERIFIED
    if failure.failure_fingerprint in observation.failure_fingerprints:
        return _FingerprintRelation.EXACT
    return _FingerprintRelation.DIFFERENT


def _execution_compatible(
    failure: PullRequestFailure,
    observation: GroupObservation,
) -> bool:
    return (
        failure.execution_profile is not None
        and failure.definition_digest is not None
        and failure.execution_profile == observation.execution_profile
        and failure.definition_digest == observation.definition_digest
    )


def _classified(
    failure: PullRequestFailure,
    classification: Classification,
    explanation: str,
    evidence: GroupObservation,
) -> ClassifiedFailure:
    return ClassifiedFailure(
        failure=failure,
        classification=classification,
        explanation=explanation,
        main_evidence=evidence,
    )


def _unable(
    failure: PullRequestFailure,
    explanation: str,
    evidence: GroupObservation | None = None,
) -> ClassifiedFailure:
    return ClassifiedFailure(
        failure=failure,
        classification=Classification.UNABLE_TO_CLASSIFY,
        explanation=explanation,
        main_evidence=evidence,
    )


def _unable_report(
    *,
    subject: PullRequestRunSubject,
    snapshot: MainSnapshot,
    failures: tuple[PullRequestFailure, ...],
    evaluated_at: datetime,
    stale_head: bool,
    stale_base: bool,
    explanation: str,
) -> ValidationReport:
    return ValidationReport(
        pull_request_head=subject.head_sha,
        pull_request_base=subject.base_sha,
        snapshot_revision=snapshot.revision,
        evaluated_at=evaluated_at,
        stale_head=stale_head,
        stale_base=stale_base,
        results=tuple(_unable(failure, explanation) for failure in failures),
    )


def _serialize_incident(incident: Incident) -> dict[str, object]:
    return {
        "clean_count": incident.clean_count,
        "ever_known": incident.ever_known,
        "failure_count": incident.failure_count,
        "confirmed_failure_fingerprints": sorted(
            incident.confirmed_failure_fingerprints
        ),
        "group": _serialize_group(incident.group),
        "last_conclusive": _serialize_observation(incident.last_conclusive_observation),
        "last_failure": _serialize_observation(incident.last_failure_observation),
        "latest_attempted": _serialize_observation(
            incident.latest_attempted_observation
        ),
        "state": incident.state.value,
    }


def _serialize_observation(
    observation: GroupObservation | None,
) -> dict[str, object] | None:
    if observation is None:
        return None
    return {
        "build_number": observation.build_number,
        "build_url": observation.build_url,
        "commit": observation.commit,
        "definition_digest": observation.definition_digest,
        "execution_profile": observation.execution_profile,
        "failure_fingerprints": sorted(observation.failure_fingerprints),
        "group": _serialize_group(observation.group),
        "main_position": _serialize_position(observation.main_position),
        "observed_at": observation.observed_at.isoformat(),
        "outcome": observation.outcome.value,
        "trusted_main": observation.trusted_main,
    }


def _serialize_group(group: GroupId) -> list[str]:
    return [group.pipeline, group.step_key, group.variant]


def _serialize_position(
    position: MainPosition | None,
) -> list[int] | None:
    return [position.epoch, position.sequence] if position is not None else None


def _timedelta_microseconds(value: timedelta) -> int:
    return (
        value.days * 24 * 60 * 60 * 1_000_000
        + value.seconds * 1_000_000
        + value.microseconds
    )


def _validate_sha(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _SHA_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must be a full lowercase SHA")
    return value


def _validate_text(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_TEXT_LENGTH
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} must be bounded normalized printable text")
    return value
