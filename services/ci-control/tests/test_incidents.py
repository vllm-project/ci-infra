from datetime import UTC, datetime, timedelta

import pytest

from vllm_ci_control.evidence import (
    GroupId,
    GroupObservation,
    MainPosition,
    Outcome,
)
from vllm_ci_control.incidents import (
    IncidentState,
    IncidentThresholds,
    reduce_incident,
    reduce_registry,
)

GROUP = GroupId("ci", "attention")
OTHER = GroupId("ci", "kernels")
START = datetime(2026, 7, 20, tzinfo=UTC)


def observation(
    index: int,
    outcome: Outcome,
    *,
    group: GroupId = GROUP,
    commit: str | None = None,
    observed_at: datetime | None = None,
    trusted_main: bool = True,
    position: MainPosition | None = None,
    fingerprint: str = "test::boom",
    definition: str = "definition-v1",
) -> GroupObservation:
    return GroupObservation(
        group=group,
        commit=commit or f"{index:040x}",
        outcome=outcome,
        build_number=index + 1,
        observed_at=observed_at or START + timedelta(hours=index),
        build_url=f"https://buildkite.example/{index + 1}",
        trusted_main=trusted_main,
        main_position=(
            position
            if position is not None
            else (MainPosition(1, index + 1) if trusted_main else None)
        ),
        execution_profile="cuda-h100",
        definition_digest=definition,
        failure_fingerprints=(
            frozenset({fingerprint})
            if outcome in {Outcome.FAILED, Outcome.FLAKY}
            else frozenset()
        ),
    )


def states(*outcomes: Outcome):
    evidence = [observation(index, outcome) for index, outcome in enumerate(outcomes)]
    return reduce_incident(GROUP, evidence)


def test_first_failure_is_candidate_and_second_failed_sha_is_known() -> None:
    assert states(Outcome.FAILED).state is IncidentState.CANDIDATE
    known = states(Outcome.FAILED, Outcome.FAILED)
    assert known.state is IncidentState.KNOWN
    assert known.ever_known


def test_flaky_sha_does_not_confirm_a_current_failure() -> None:
    incident = states(Outcome.FAILED, Outcome.FLAKY)
    assert incident.state is IncidentState.CANDIDATE
    assert incident.failure_count == 1


def test_candidate_clears_without_becoming_a_known_incident() -> None:
    incident = states(Outcome.FAILED, Outcome.CLEAN)
    assert incident.state is IncidentState.HEALTHY
    assert not incident.ever_known


def test_known_failure_recovers_and_resolves_only_from_clean_first_attempts() -> None:
    recovering = states(
        Outcome.FAILED,
        Outcome.FAILED,
        Outcome.CLEAN,
        Outcome.CLEAN,
    )
    resolved = states(
        Outcome.FAILED,
        Outcome.FAILED,
        Outcome.CLEAN,
        Outcome.CLEAN,
        Outcome.CLEAN,
    )
    assert recovering.state is IncidentState.RECOVERING
    assert resolved.state is IncidentState.RESOLVED


def test_latest_inconclusive_is_retained_without_changing_lifecycle() -> None:
    incident = states(
        Outcome.FAILED,
        Outcome.FAILED,
        Outcome.INCONCLUSIVE,
    )
    assert incident.state is IncidentState.KNOWN
    assert incident.latest_attempted_observation is not None
    assert incident.latest_attempted_observation.outcome is Outcome.INCONCLUSIVE
    assert incident.last_conclusive_observation is not None
    assert incident.last_conclusive_observation.outcome is Outcome.FAILED


def test_flaky_and_inconclusive_observations_cannot_resolve_an_incident() -> None:
    incident = states(
        Outcome.FAILED,
        Outcome.FAILED,
        Outcome.CLEAN,
        Outcome.INCONCLUSIVE,
        Outcome.FLAKY,
    )
    assert incident.state is IncidentState.KNOWN
    assert incident.clean_count == 0


def test_failure_during_recovery_moves_back_to_known() -> None:
    incident = states(
        Outcome.FAILED,
        Outcome.FAILED,
        Outcome.CLEAN,
        Outcome.FAILED,
    )
    assert incident.state is IncidentState.KNOWN


def test_resolved_recurrence_requires_two_new_hard_failures() -> None:
    history = [
        Outcome.FAILED,
        Outcome.FAILED,
        Outcome.CLEAN,
        Outcome.CLEAN,
        Outcome.CLEAN,
    ]
    candidate = states(*history, Outcome.FAILED)
    reopened = states(*history, Outcome.FAILED, Outcome.FAILED)
    assert candidate.state is IncidentState.CANDIDATE
    assert candidate.ever_known
    assert reopened.state is IncidentState.KNOWN


def test_rebuild_of_same_sha_does_not_satisfy_confirmation_threshold() -> None:
    incident = reduce_incident(
        GROUP,
        [
            observation(
                0,
                Outcome.FAILED,
                commit="a" * 40,
                position=MainPosition(1, 1),
            ),
            observation(
                1,
                Outcome.FAILED,
                commit="a" * 40,
                position=MainPosition(1, 1),
            ),
        ],
    )
    assert incident.state is IncidentState.CANDIDATE


def test_canonical_position_not_finish_time_drives_transitions() -> None:
    first_failure_finishes_late = observation(
        0,
        Outcome.FAILED,
        observed_at=START + timedelta(hours=2),
        position=MainPosition(1, 1),
    )
    later_clean_finishes_early = observation(
        1,
        Outcome.CLEAN,
        observed_at=START + timedelta(hours=1),
        position=MainPosition(1, 2),
    )
    incident = reduce_incident(
        GROUP,
        [later_clean_finishes_early, first_failure_finishes_late],
    )
    assert incident.state is IncidentState.HEALTHY
    assert incident.latest_attempted_observation == later_clean_finishes_early


def test_failure_confirmation_does_not_cross_main_epochs() -> None:
    incident = reduce_incident(
        GROUP,
        [
            observation(
                0,
                Outcome.FAILED,
                position=MainPosition(1, 1),
            ),
            observation(
                1,
                Outcome.FAILED,
                position=MainPosition(2, 1),
            ),
        ],
    )
    assert incident.state is IncidentState.CANDIDATE
    assert incident.failure_count == 1


def test_failure_confirmation_does_not_cross_definition_revisions() -> None:
    incident = reduce_incident(
        GROUP,
        [
            observation(0, Outcome.FAILED, definition="definition-v1"),
            observation(1, Outcome.FAILED, definition="definition-v2"),
        ],
    )
    assert incident.state is IncidentState.CANDIDATE
    assert incident.failure_count == 1
    assert not incident.confirmed_failure_fingerprints


def test_new_clean_definition_does_not_inherit_old_recovery_state() -> None:
    incident = reduce_incident(
        GROUP,
        [
            observation(0, Outcome.FAILED, definition="definition-v1"),
            observation(1, Outcome.FAILED, definition="definition-v1"),
            observation(2, Outcome.CLEAN, definition="definition-v2"),
        ],
    )
    assert incident.state is IncidentState.HEALTHY
    assert incident.clean_count == 1
    assert incident.ever_known


def test_recovery_count_restarts_but_known_state_carries_to_new_epoch() -> None:
    incident = reduce_incident(
        GROUP,
        [
            observation(0, Outcome.FAILED, position=MainPosition(1, 1)),
            observation(1, Outcome.FAILED, position=MainPosition(1, 2)),
            observation(2, Outcome.CLEAN, position=MainPosition(1, 3)),
            observation(3, Outcome.CLEAN, position=MainPosition(2, 1)),
            observation(4, Outcome.CLEAN, position=MainPosition(2, 2)),
        ],
    )
    assert incident.state is IncidentState.RECOVERING
    assert incident.clean_count == 2
    assert incident.ever_known


def test_noncanonical_evidence_is_rejected_by_reducer() -> None:
    value = observation(
        0,
        Outcome.FAILED,
        commit="feature-head",
        trusted_main=False,
    )
    with pytest.raises(ValueError, match="trusted canonical-main"):
        reduce_incident(GROUP, [value])


def test_thresholds_are_configurable() -> None:
    incident = reduce_incident(
        GROUP,
        [observation(0, Outcome.FAILED)],
        IncidentThresholds(
            confirm_failures=1,
            begin_recovery=2,
            resolve_clean=2,
        ),
    )
    assert incident.state is IncidentState.KNOWN


def test_registry_is_sorted_and_group_scoped() -> None:
    registry = reduce_registry(
        [
            observation(0, Outcome.FAILED, group=OTHER),
            observation(1, Outcome.CLEAN, group=GROUP),
        ]
    )
    assert [item.group for item in registry] == [GROUP, OTHER]
