from __future__ import annotations

import pytest

from vllm_ci_control.models import RepositoryPermission
from vllm_ci_control.policy import (
    Policy,
    PolicyValidationError,
    RetryLimit,
)

OVERLAY = """
api_version = 1

[catalog]
groups = ["upstream", "cpu", "amd"]
aliases = {}
tombstones = []
area_aliases = {}
area_tombstones = []
native_amd_selection = "whole_lane"

[authorization]
minimum_compute_permission = "write"
minimum_refresh_permission = "write"
minimum_credit_grant_permission = "maintain"
committer_teams = ["vllm-committers"]
credit_admin_teams = ["vllm-ci-admins"]

[credits]
initial_grant = 300
reset = "none"

[retry]
failures_limit = "inf"
include_states = ["failed", "timed_out", "expired"]

[main_status]
confirmation_distinct_shas = 2
resolution_clean_distinct_shas = 3
evidence_max_age_hours = 72
"""


def test_loads_actual_overlay_shape_and_defaults() -> None:
    policy = Policy.from_toml(OVERLAY)

    assert policy.api_version == 1
    assert policy.catalog_groups == frozenset({"upstream", "cpu", "amd"})
    assert policy.initial_credits == 300
    assert policy.retry_limit.is_infinite
    assert policy.retry_states == frozenset({"failed", "timed_out", "expired"})
    assert policy.confirmation_distinct_shas == 2
    assert policy.resolution_clean_distinct_shas == 3
    assert policy.evidence_max_age_hours == 72


def test_compute_requires_write_or_committer_membership() -> None:
    policy = Policy.from_toml(OVERLAY)

    assert policy.can_mutate(RepositoryPermission.WRITE)
    assert policy.can_mutate(RepositoryPermission.ADMIN)
    assert not policy.can_mutate(RepositoryPermission.READ)
    assert policy.can_mutate(
        RepositoryPermission.READ,
        frozenset({"vllm-committers"}),
    )
    assert policy.can_receive_compute_credits(RepositoryPermission.WRITE)
    assert policy.can_receive_compute_credits(
        RepositoryPermission.READ,
        frozenset({"vllm-committers"}),
    )
    assert not policy.can_receive_compute_credits(RepositoryPermission.READ)


def test_credit_grants_require_elevated_permission_or_admin_team() -> None:
    policy = Policy.from_toml(OVERLAY)

    assert policy.can_top_up(RepositoryPermission.MAINTAIN)
    assert policy.can_top_up(RepositoryPermission.ADMIN)
    assert not policy.can_top_up(RepositoryPermission.WRITE)
    assert policy.can_top_up(
        RepositoryPermission.READ,
        frozenset({"vllm-ci-admins"}),
    )


def test_retry_limit_supports_bounded_and_infinite_values() -> None:
    assert RetryLimit.parse("inf").allows(1_000_000)
    bounded = RetryLimit.parse(2)
    assert bounded.allows(0)
    assert bounded.allows(1)
    assert not bounded.allows(2)


def test_policy_loader_rejects_unknown_or_unsafe_configuration() -> None:
    with pytest.raises(PolicyValidationError, match="unknown fields"):
        Policy.from_mapping(
            {
                "api_version": 1,
                "catalog": {},
                "authorization": {},
                "credits": {},
                "retry": {},
                "main_status": {},
                "surprise": True,
            }
        )
    with pytest.raises(PolicyValidationError, match="write or higher"):
        Policy(minimum_compute_permission=RepositoryPermission.READ)
    with pytest.raises(PolicyValidationError, match="invalid policy TOML"):
        Policy.from_toml("not = [valid")
