from datetime import UTC, datetime

import pytest

from vllm_ci_control.adapters.buildkite import BuildkiteProtocolError
from vllm_ci_control.adapters.buildkite_evidence import (
    normalize_buildkite_jobs,
    normalize_opaque_buildkite_lane,
)
from vllm_ci_control.catalog import Catalog, ExecutionKind, JobDefinition
from vllm_ci_control.evidence import MainPosition, Outcome, observe_attempts

CATALOG = Catalog(
    version="v1",
    jobs=(
        JobDefinition(
            job_id="attention",
            groups=frozenset({"upstream"}),
            areas=frozenset({"attention"}),
            execution_profile="h100",
            shards=2,
            definition_digest="a" * 64,
        ),
    ),
)
OPAQUE_CATALOG = Catalog(
    version="v1",
    jobs=(
        JobDefinition(
            job_id="native-amd-lane",
            groups=frozenset({"amd"}),
            areas=frozenset({"native-amd"}),
            execution_profile="amd-native",
            execution_kind=ExecutionKind.OPAQUE_PIPELINE,
            pipeline="amd-ci",
            shards=2,
            definition_digest="b" * 64,
        ),
    ),
)
NOW = datetime(2026, 7, 29, tzinfo=UTC)


def raw(job_id: str, shard: int, **overrides):
    value = {
        "id": job_id,
        "type": "script",
        "step_key": "attention",
        "state": "passed",
        "parallel_group_index": shard,
        "parallel_group_total": 2,
        "retried_in_job_id": None,
        # Buildkite represents an initial attempt with an explicit null.
        "retries_count": None,
        "soft_failed": False,
    }
    value.update(overrides)
    return value


def observe(normalized):
    return observe_attempts(
        attempts=normalized.attempts,
        expectations=normalized.expectations,
        read_attestation=normalized.read_attestation,
        build_number=10,
        build_url="https://buildkite.example/10",
        commit="a" * 40,
        observed_at=NOW,
        trusted_main=True,
        main_position=MainPosition(1, 1),
    )[0]


def test_complete_expected_shards_can_prove_clean() -> None:
    normalized = normalize_buildkite_jobs(
        catalog=CATALOG,
        pipeline="ci",
        expected_job_ids=["attention"],
        jobs=[raw("one", 0), raw("two", 1)],
        all_pages_fetched=True,
        retry_history_complete=True,
    )

    assert observe(normalized).outcome is Outcome.CLEAN


def test_missing_retry_counter_is_inconclusive() -> None:
    one = raw("one", 0)
    two = raw("two", 1)
    del one["retries_count"]
    normalized = normalize_buildkite_jobs(
        catalog=CATALOG,
        pipeline="ci",
        expected_job_ids=["attention"],
        jobs=[one, two],
        all_pages_fetched=True,
        retry_history_complete=True,
    )

    assert observe(normalized).outcome is Outcome.INCONCLUSIVE


def test_real_retry_shape_keeps_the_complete_lineage() -> None:
    normalized = normalize_buildkite_jobs(
        catalog=CATALOG,
        pipeline="ci",
        expected_job_ids=["attention"],
        jobs=[
            raw(
                "first",
                0,
                state="failed",
                retried_in_job_id="retry",
                retries_count=None,
            ),
            raw("retry", 0, retries_count=1),
            raw("two", 1),
        ],
        all_pages_fetched=True,
        retry_history_complete=True,
    )

    assert observe(normalized).outcome is Outcome.FLAKY


def test_missing_expected_shard_is_inconclusive() -> None:
    normalized = normalize_buildkite_jobs(
        catalog=CATALOG,
        pipeline="ci",
        expected_job_ids=["attention"],
        jobs=[raw("one", 0)],
        all_pages_fetched=True,
        retry_history_complete=True,
    )

    assert observe(normalized).outcome is Outcome.INCONCLUSIVE


def test_incomplete_provider_read_is_inconclusive() -> None:
    normalized = normalize_buildkite_jobs(
        catalog=CATALOG,
        pipeline="ci",
        expected_job_ids=["attention"],
        jobs=[raw("one", 0), raw("two", 1)],
        all_pages_fetched=False,
        retry_history_complete=True,
    )

    assert observe(normalized).outcome is Outcome.INCONCLUSIVE


def test_shard_metadata_must_match_catalog() -> None:
    with pytest.raises(BuildkiteProtocolError, match="disagrees"):
        normalize_buildkite_jobs(
            catalog=CATALOG,
            pipeline="ci",
            expected_job_ids=["attention"],
            jobs=[
                raw(
                    "one",
                    0,
                    parallel_group_total=3,
                )
            ],
            all_pages_fetched=True,
            retry_history_complete=True,
        )


def test_finished_state_is_derived_from_exit_status() -> None:
    normalized = normalize_buildkite_jobs(
        catalog=CATALOG,
        pipeline="ci",
        expected_job_ids=["attention"],
        jobs=[
            raw("one", 0, state="finished", exit_status=0),
            raw(
                "two",
                1,
                state="finished",
                exit_status=1,
            ),
        ],
        all_pages_fetched=True,
        retry_history_complete=True,
        fingerprints_by_job_id={"two": ["pytest:test_failure"]},
    )

    observation = observe(normalized)
    assert observation.outcome is Outcome.FAILED
    assert observation.failure_fingerprints == frozenset({"pytest:test_failure"})


def test_opaque_pipeline_uses_a_dedicated_whole_lane_adapter() -> None:
    jobs = [
        {
            "id": "native-one",
            "type": "script",
            "state": "passed",
            "retried_in_job_id": None,
            "retries_count": None,
            "soft_failed": False,
        },
        {
            "id": "native-two",
            "type": "script",
            "state": "passed",
            "retried_in_job_id": None,
            "retries_count": None,
            "soft_failed": False,
        },
    ]
    normalized = normalize_opaque_buildkite_lane(
        catalog=OPAQUE_CATALOG,
        pipeline="amd-ci",
        lane_job_id="native-amd-lane",
        jobs=jobs,
        all_pages_fetched=True,
        retry_history_complete=True,
    )

    assert observe(normalized).outcome is Outcome.CLEAN
    with pytest.raises(ValueError, match="opaque-pipeline normalizer"):
        normalize_buildkite_jobs(
            catalog=OPAQUE_CATALOG,
            pipeline="amd-ci",
            expected_job_ids=["native-amd-lane"],
            jobs=jobs,
            all_pages_fetched=True,
            retry_history_complete=True,
        )


def test_opaque_pipeline_missing_execution_unit_is_inconclusive() -> None:
    normalized = normalize_opaque_buildkite_lane(
        catalog=OPAQUE_CATALOG,
        pipeline="amd-ci",
        lane_job_id="native-amd-lane",
        jobs=[
            {
                "id": "native-one",
                "type": "script",
                "state": "passed",
                "retried_in_job_id": None,
                "retries_count": None,
                "soft_failed": False,
            }
        ],
        all_pages_fetched=True,
        retry_history_complete=True,
    )

    assert observe(normalized).outcome is Outcome.INCONCLUSIVE
