from datetime import UTC, datetime, timedelta

import pytest

from vllm_ci_control.evidence import (
    GroupExpectation,
    GroupId,
    GroupObservation,
    JobAttempt,
    MainPosition,
    Outcome,
    ProviderReadAttestation,
    collapse_distinct_commits,
    observe_attempts,
)

GROUP = GroupId("ci", "attention")
NOW = datetime(2026, 7, 29, tzinfo=UTC)
POSITION = MainPosition(1, 10)
COMPLETE_READ = ProviderReadAttestation(
    all_pages_fetched=True,
    retry_history_complete=True,
)


def expectation(*shards: str) -> GroupExpectation:
    return GroupExpectation(
        group=GROUP,
        shards=frozenset(shards or {"0"}),
        execution_profile="cuda-h100",
        definition_digest="definition-v1",
    )


def observe(
    *attempts: JobAttempt,
    expected: GroupExpectation | None = None,
    read: ProviderReadAttestation = COMPLETE_READ,
) -> GroupObservation:
    return observe_attempts(
        attempts=attempts,
        expectations=[expected or expectation("0")],
        read_attestation=read,
        build_number=10,
        build_url="https://buildkite.example/10",
        commit="a" * 40,
        observed_at=NOW,
        trusted_main=True,
        main_position=POSITION,
    )[0]


def attempt(
    job_id: str,
    state: str,
    *,
    next_id: str | None = None,
    retries_count: int | None = 0,
    shard: str = "0",
    soft_failed: bool = False,
    fingerprints: frozenset[str] = frozenset(),
) -> JobAttempt:
    return JobAttempt(
        job_id=job_id,
        group=GROUP,
        state=state,
        shard=shard,
        retried_in_job_id=next_id,
        retries_count=retries_count,
        soft_failed=soft_failed,
        failure_fingerprints=fingerprints,
    )


def test_clean_requires_every_exact_expected_shard_to_pass_first_try() -> None:
    result = observe(
        attempt("one", "passed", shard="0"),
        attempt("two", "passed", shard="1"),
        expected=expectation("0", "1"),
    )
    assert result.outcome is Outcome.CLEAN
    assert result.execution_profile == "cuda-h100"


def test_missing_expected_shard_is_inconclusive() -> None:
    result = observe(
        attempt("one", "passed", shard="0"),
        expected=expectation("0", "1"),
    )
    assert result.outcome is Outcome.INCONCLUSIVE


@pytest.mark.parametrize(
    "read",
    [
        ProviderReadAttestation(False, True),
        ProviderReadAttestation(True, False),
    ],
)
def test_incomplete_provider_read_is_inconclusive(
    read: ProviderReadAttestation,
) -> None:
    assert observe(attempt("one", "passed"), read=read).outcome is (
        Outcome.INCONCLUSIVE
    )


def test_latest_retry_without_predecessor_is_not_clean() -> None:
    result = observe(attempt("retry", "passed", retries_count=1))
    assert result.outcome is Outcome.INCONCLUSIVE


def test_missing_retries_count_is_inconclusive() -> None:
    result = observe(attempt("one", "passed", retries_count=None))
    assert result.outcome is Outcome.INCONCLUSIVE


@pytest.mark.parametrize("state", ["failed", "timed_out", "expired"])
def test_a_current_hard_failure_makes_the_group_failed(state: str) -> None:
    result = observe(attempt("one", state, fingerprints=frozenset({"test::boom"})))
    assert result.outcome is Outcome.FAILED
    assert result.failure_fingerprints == frozenset({"test::boom"})


def test_soft_failure_is_failure_evidence() -> None:
    result = observe(
        attempt(
            "one",
            "passed",
            soft_failed=True,
            fingerprints=frozenset({"test::soft"}),
        )
    )
    assert result.outcome is Outcome.FAILED


def test_retry_pass_is_flaky_and_keeps_failed_fingerprint() -> None:
    result = observe(
        attempt(
            "first",
            "failed",
            next_id="retry",
            fingerprints=frozenset({"test::boom"}),
        ),
        attempt("retry", "passed", retries_count=1),
    )
    assert result.outcome is Outcome.FLAKY
    assert result.failure_fingerprints == frozenset({"test::boom"})


def test_retry_after_non_failure_is_never_clean() -> None:
    result = observe(
        attempt("first", "canceled", next_id="retry"),
        attempt("retry", "passed", retries_count=1),
    )
    assert result.outcome is Outcome.INCONCLUSIVE


@pytest.mark.parametrize(
    "state", ["blocked", "canceled", "scheduled", "running", "skipped"]
)
def test_non_terminal_or_non_executed_state_is_inconclusive(state: str) -> None:
    assert observe(attempt("one", state)).outcome is Outcome.INCONCLUSIVE


def test_duplicate_job_ids_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        observe(attempt("same", "passed"), attempt("same", "passed"))


def test_multiple_current_attempts_for_one_shard_are_inconclusive() -> None:
    result = observe(attempt("one", "passed"), attempt("two", "passed"))
    assert result.outcome is Outcome.INCONCLUSIVE


def test_ambiguous_retry_lineage_is_inconclusive() -> None:
    result = observe(
        attempt("first", "failed", next_id="retry"),
        attempt("also-first", "failed", next_id="retry"),
        attempt("retry", "passed", retries_count=1),
    )
    assert result.outcome is Outcome.INCONCLUSIVE


def test_dangling_retry_successor_is_inconclusive() -> None:
    result = observe(
        attempt("first", "failed", next_id="missing"),
        attempt("unrelated", "passed"),
    )
    assert result.outcome is Outcome.INCONCLUSIVE


def test_expected_but_absent_group_is_inconclusive() -> None:
    assert observe().outcome is Outcome.INCONCLUSIVE


def test_intentionally_unselected_group_produces_no_observation() -> None:
    assert (
        observe_attempts(
            attempts=[],
            expectations=[],
            read_attestation=COMPLETE_READ,
            build_number=10,
            build_url="https://buildkite.example/10",
            commit="a" * 40,
            observed_at=NOW,
            trusted_main=True,
            main_position=POSITION,
        )
        == ()
    )


def test_rebuilds_of_one_sha_do_not_create_independent_evidence() -> None:
    failed = observe(
        attempt(
            "first",
            "failed",
            fingerprints=frozenset({"test::boom"}),
        )
    )
    clean = observe_attempts(
        attempts=[attempt("second", "passed")],
        expectations=[expectation("0")],
        read_attestation=COMPLETE_READ,
        build_number=11,
        build_url="https://buildkite.example/11",
        commit=failed.commit,
        observed_at=NOW + timedelta(hours=1),
        trusted_main=True,
        main_position=POSITION,
    )[0]

    collapsed = collapse_distinct_commits([failed, clean])

    assert len(collapsed) == 1
    assert collapsed[0].outcome is Outcome.FLAKY
    assert collapsed[0].failure_fingerprints == frozenset({"test::boom"})


def test_same_sha_in_a_new_main_epoch_is_new_evidence() -> None:
    first_epoch = observe(attempt("first", "failed"))
    second_epoch = GroupObservation(
        group=GROUP,
        commit=first_epoch.commit,
        outcome=Outcome.FAILED,
        build_number=11,
        observed_at=NOW + timedelta(hours=1),
        build_url="https://buildkite.example/11",
        trusted_main=True,
        main_position=MainPosition(2, 1),
        execution_profile="cuda-h100",
        definition_digest="definition-v1",
    )

    collapsed = collapse_distinct_commits([first_epoch, second_epoch])

    assert len(collapsed) == 2
    assert [item.main_position for item in collapsed] == [
        MainPosition(1, 10),
        MainPosition(2, 1),
    ]


def test_noncanonical_evidence_is_rejected_by_main_collapse() -> None:
    observation = GroupObservation(
        group=GROUP,
        commit="feature-head",
        outcome=Outcome.CLEAN,
        build_number=10,
        observed_at=NOW,
        build_url="https://buildkite.example/10",
        trusted_main=False,
        main_position=None,
        execution_profile="cuda-h100",
        definition_digest="definition-v1",
    )
    with pytest.raises(ValueError, match="trusted canonical-main"):
        collapse_distinct_commits([observation])
