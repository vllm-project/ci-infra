from datetime import UTC, datetime, timedelta

from vllm_ci_control.evidence import (
    GroupId,
    GroupObservation,
    MainPosition,
    Outcome,
)
from vllm_ci_control.incidents import Incident, IncidentState
from vllm_ci_control.validation import (
    Classification,
    LaneCompleteness,
    LaneSnapshot,
    PullRequestFailure,
    PullRequestRunSubject,
    create_snapshot,
    validate_pull_request,
)

NOW = datetime(2026, 7, 29, tzinfo=UTC)
GROUP = GroupId("ci", "attention")
PROFILE = "cuda-h100"
DEFINITION = "definition-v1"
FINGERPRINT = "test_attention::assertion-a"
HEAD = "b" * 40
BASE = "c" * 40
MAIN_COMMIT = "a" * 40
WATERMARK = MainPosition(1, 10)


def observation(
    outcome: Outcome,
    *,
    group: GroupId = GROUP,
    commit: str = MAIN_COMMIT,
    position: MainPosition = WATERMARK,
    age: timedelta = timedelta(hours=1),
    fingerprint: str = FINGERPRINT,
    profile: str | None = PROFILE,
    definition: str | None = DEFINITION,
    build_number: int = 10,
) -> GroupObservation:
    return GroupObservation(
        group=group,
        commit=commit,
        outcome=outcome,
        build_number=build_number,
        observed_at=NOW - age,
        build_url=f"https://buildkite.example/{build_number}",
        trusted_main=True,
        main_position=position,
        execution_profile=profile,
        definition_digest=definition,
        failure_fingerprints=(
            frozenset({fingerprint})
            if outcome in {Outcome.FAILED, Outcome.FLAKY}
            else frozenset()
        ),
    )


def incident(
    state: IncidentState,
    *,
    outcome: Outcome,
    group: GroupId = GROUP,
    age: timedelta = timedelta(hours=1),
    fingerprint: str = FINGERPRINT,
    profile: str | None = PROFILE,
    definition: str | None = DEFINITION,
) -> Incident:
    latest = observation(
        outcome,
        group=group,
        age=age,
        fingerprint=fingerprint,
        profile=profile,
        definition=definition,
    )
    if outcome is Outcome.INCONCLUSIVE:
        last_conclusive = observation(
            Outcome.FAILED,
            group=group,
            position=MainPosition(1, 9),
            fingerprint=fingerprint,
            profile=profile,
            definition=definition,
            build_number=9,
        )
    else:
        last_conclusive = latest
    if outcome in {Outcome.FAILED, Outcome.FLAKY}:
        last_failure = latest
    elif state in {IncidentState.RECOVERING, IncidentState.RESOLVED}:
        last_failure = observation(
            Outcome.FAILED,
            group=group,
            commit="d" * 40,
            position=MainPosition(1, 5),
            fingerprint=fingerprint,
            profile=profile,
            definition=definition,
            build_number=5,
        )
    elif outcome is Outcome.INCONCLUSIVE:
        last_failure = last_conclusive
    else:
        last_failure = None
    return Incident(
        group=group,
        state=state,
        failure_count=2 if state is IncidentState.KNOWN else 0,
        clean_count=(
            3
            if state is IncidentState.RESOLVED
            else (1 if state is IncidentState.RECOVERING else 0)
        ),
        ever_known=state
        in {
            IncidentState.KNOWN,
            IncidentState.RECOVERING,
            IncidentState.RESOLVED,
        },
        latest_attempted_observation=latest,
        last_conclusive_observation=last_conclusive,
        last_failure_observation=last_failure,
        confirmed_failure_fingerprints=(
            frozenset({fingerprint})
            if state
            in {
                IncidentState.KNOWN,
                IncidentState.RECOVERING,
                IncidentState.RESOLVED,
            }
            else frozenset()
        ),
    )


def subject(
    *,
    head: str = HEAD,
    tested_head: str = HEAD,
    base: str = BASE,
    ancestors: frozenset[str] = frozenset({MAIN_COMMIT, BASE}),
) -> PullRequestRunSubject:
    return PullRequestRunSubject(
        repository="vllm-project/vllm",
        pull_request_number=7,
        head_sha=head,
        tested_head_sha=tested_head,
        base_sha=base,
        base_ancestor_shas=ancestors,
    )


def failure(
    *,
    group: GroupId | None = GROUP,
    fingerprint: str | None = FINGERPRINT,
    profile: str | None = PROFILE,
    definition: str | None = DEFINITION,
) -> PullRequestFailure:
    return PullRequestFailure(
        label="attention tests",
        group=group,
        build_url="https://buildkite.example/pr",
        failure_fingerprint=fingerprint,
        execution_profile=profile,
        definition_digest=definition,
    )


def snapshot(
    *incidents: Incident,
    completeness: LaneCompleteness = LaneCompleteness.COMPLETE,
    watermark: MainPosition | None = WATERMARK,
    generated_at: datetime = NOW,
    policy_revision: str = "policy-v1",
    algorithm_revision: str = "validation-v1",
    catalog_version: str = "schema-1",
    catalog_digest: str = "catalog-v1",
    observations: tuple[GroupObservation, ...] = (),
):
    return create_snapshot(
        catalog_version=catalog_version,
        catalog_digest=catalog_digest,
        policy_revision=policy_revision,
        algorithm_revision=algorithm_revision,
        generated_at=generated_at,
        max_evidence_age=timedelta(days=2),
        lanes=[LaneSnapshot("ci", completeness, watermark)],
        incidents=incidents,
        observations=observations,
    )


def validate(
    main_incident: Incident,
    *,
    pr_failure: PullRequestFailure | None = None,
    run_subject: PullRequestRunSubject | None = None,
    current_head: str = HEAD,
    current_base: str = BASE,
    pr_version: str = "schema-1",
    pr_digest: str = "catalog-v1",
    completeness: LaneCompleteness = LaneCompleteness.COMPLETE,
):
    return validate_pull_request(
        subject=run_subject or subject(),
        current_head=current_head,
        current_base=current_base,
        pr_catalog_version=pr_version,
        pr_catalog_digest=pr_digest,
        failures=[pr_failure or failure()],
        snapshot=snapshot(main_incident, completeness=completeness),
        now=NOW,
    )


def test_exact_known_and_candidate_fingerprints_are_distinct() -> None:
    known = validate(incident(IncidentState.KNOWN, outcome=Outcome.FAILED))
    candidate = validate(incident(IncidentState.CANDIDATE, outcome=Outcome.FAILED))
    assert known.results[0].classification is Classification.KNOWN_ON_MAIN
    assert candidate.results[0].classification is Classification.CANDIDATE_ON_MAIN


def test_group_only_red_match_is_explicitly_unverified() -> None:
    report = validate(
        incident(IncidentState.KNOWN, outcome=Outcome.FAILED),
        pr_failure=failure(fingerprint=None),
    )
    assert report.results[0].classification is Classification.GROUP_ALSO_RED_ON_MAIN
    assert "underlying failure is unverified" in report.results[0].explanation


def test_different_normalized_fingerprints_are_not_called_known() -> None:
    report = validate(
        incident(
            IncidentState.KNOWN,
            outcome=Outcome.FAILED,
            fingerprint="test_attention::different",
        )
    )
    assert report.results[0].classification is Classification.DIFFERENT_FAILURE_ON_MAIN


def test_flaky_latest_is_distinct_and_never_currently_failing() -> None:
    report = validate(incident(IncidentState.KNOWN, outcome=Outcome.FLAKY))
    result = report.results[0]
    assert result.classification is Classification.SEEN_FLAKY_ON_MAIN
    assert "later pass" in result.explanation
    assert "currently failing" not in result.explanation


def test_exact_recovering_and_resolved_incidents_are_distinct() -> None:
    recovering = validate(incident(IncidentState.RECOVERING, outcome=Outcome.CLEAN))
    resolved = validate(incident(IncidentState.RESOLVED, outcome=Outcome.CLEAN))
    assert recovering.results[0].classification is Classification.RECOVERING_ON_MAIN
    assert resolved.results[0].classification is Classification.RESOLVED_ON_MAIN


def test_clean_regression_candidate_requires_compatible_base_control() -> None:
    report = validate(incident(IncidentState.HEALTHY, outcome=Outcome.CLEAN))
    assert report.results[0].classification is Classification.NOT_MATCHED_ON_MAIN
    assert "possible regression" in report.results[0].explanation


def test_clean_evidence_without_compatible_metadata_is_unable() -> None:
    report = validate(
        incident(
            IncidentState.HEALTHY,
            outcome=Outcome.CLEAN,
            profile=None,
            definition=None,
        )
    )
    assert report.results[0].classification is Classification.UNABLE_TO_CLASSIFY
    assert "execution profile" in report.results[0].explanation


def test_clean_commit_newer_than_pinned_base_is_not_a_regression_control() -> None:
    report = validate(
        incident(IncidentState.HEALTHY, outcome=Outcome.CLEAN),
        run_subject=subject(ancestors=frozenset({BASE})),
    )
    assert report.results[0].classification is Classification.UNABLE_TO_CLASSIFY
    assert "trusted ancestor" in report.results[0].explanation


def test_latest_inconclusive_attempt_overrides_older_known_context() -> None:
    report = validate(incident(IncidentState.KNOWN, outcome=Outcome.INCONCLUSIVE))
    assert report.results[0].classification is Classification.UNABLE_TO_CLASSIFY
    assert "latest main attempt" in report.results[0].explanation
    assert report.results[0].main_evidence is not None
    assert report.results[0].main_evidence.outcome is Outcome.INCONCLUSIVE


def test_partial_lane_is_unable_even_with_conclusive_group_evidence() -> None:
    report = validate(
        incident(IncidentState.KNOWN, outcome=Outcome.FAILED),
        completeness=LaneCompleteness.PARTIAL,
    )
    assert report.results[0].classification is Classification.UNABLE_TO_CLASSIFY
    assert "partial or unavailable" in report.results[0].explanation


def test_missing_group_and_unkeyed_failure_are_unable() -> None:
    main = incident(IncidentState.HEALTHY, outcome=Outcome.CLEAN)
    missing = validate(
        main,
        pr_failure=failure(group=GroupId("ci", "not-on-main")),
    )
    unkeyed = validate(main, pr_failure=failure(group=None))
    assert missing.results[0].classification is Classification.UNABLE_TO_CLASSIFY
    assert unkeyed.results[0].classification is Classification.UNABLE_TO_CLASSIFY


def test_stale_evidence_is_unable() -> None:
    report = validate(
        incident(
            IncidentState.HEALTHY,
            outcome=Outcome.CLEAN,
            age=timedelta(days=3),
        )
    )
    assert "stale" in report.results[0].explanation


def test_global_catalog_digest_change_does_not_hide_compatible_job() -> None:
    report = validate(
        incident(IncidentState.KNOWN, outcome=Outcome.FAILED),
        pr_digest="different",
    )
    assert report.results[0].classification is Classification.KNOWN_ON_MAIN


def test_catalog_schema_mismatch_makes_the_whole_validation_unable() -> None:
    report = validate(
        incident(IncidentState.KNOWN, outcome=Outcome.FAILED),
        pr_version="schema-2",
    )
    assert "catalog schemas" in report.results[0].explanation


def test_head_base_and_tested_checkout_are_pinned_independently() -> None:
    main = incident(IncidentState.KNOWN, outcome=Outcome.FAILED)

    stale_head = validate(main, current_head="e" * 40)
    assert stale_head.stale_head

    stale_base = validate(main, current_base="f" * 40)
    assert stale_base.stale_base

    wrong_checkout = validate(
        main,
        run_subject=subject(tested_head="e" * 40),
    )
    assert "not produced from the exact" in wrong_checkout.results[0].explanation


def test_snapshot_revision_changes_with_every_classification_input() -> None:
    stale = snapshot(
        incident(
            IncidentState.KNOWN,
            outcome=Outcome.FAILED,
            age=timedelta(days=3),
        )
    )
    fresh = snapshot(incident(IncidentState.KNOWN, outcome=Outcome.FAILED))
    new_generation_time = snapshot(
        incident(IncidentState.KNOWN, outcome=Outcome.FAILED),
        generated_at=NOW + timedelta(minutes=1),
    )
    new_policy = snapshot(
        incident(IncidentState.KNOWN, outcome=Outcome.FAILED),
        policy_revision="policy-v2",
    )
    new_algorithm = snapshot(
        incident(IncidentState.KNOWN, outcome=Outcome.FAILED),
        algorithm_revision="validation-v2",
    )
    partial = snapshot(
        incident(IncidentState.KNOWN, outcome=Outcome.FAILED),
        completeness=LaneCompleteness.PARTIAL,
    )

    revisions = {
        stale.revision,
        fresh.revision,
        new_generation_time.revision,
        new_policy.revision,
        new_algorithm.revision,
        partial.revision,
    }
    assert len(revisions) == 6


def test_snapshot_revision_is_deterministic_for_identical_content() -> None:
    main = incident(IncidentState.KNOWN, outcome=Outcome.FAILED)
    assert snapshot(main).revision == snapshot(main).revision


def test_clean_control_uses_newest_fresh_observation_in_base_ancestry() -> None:
    older = observation(
        Outcome.CLEAN,
        commit="d" * 40,
        position=MainPosition(1, 8),
        build_number=8,
    )
    latest = observation(
        Outcome.CLEAN,
        commit=MAIN_COMMIT,
        position=WATERMARK,
    )
    main = incident(IncidentState.HEALTHY, outcome=Outcome.CLEAN)
    frozen = snapshot(main, observations=(older, latest))
    report = validate_pull_request(
        subject=subject(ancestors=frozenset({BASE, older.commit})),
        current_head=HEAD,
        current_base=BASE,
        pr_catalog_version="schema-1",
        pr_catalog_digest="different-but-compatible",
        failures=[failure()],
        snapshot=frozen,
        now=NOW,
    )

    result = report.results[0]
    assert result.classification is Classification.NOT_MATCHED_ON_MAIN
    assert result.main_evidence == older
