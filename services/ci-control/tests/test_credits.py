from __future__ import annotations

from dataclasses import replace

import pytest

from vllm_ci_control.credits import (
    ConcurrentCreditUpdate,
    CreditAccount,
    CreditError,
    IdempotencyConflict,
    InsufficientCredits,
    InvalidCreditTransition,
    ReservationStatus,
)


def test_default_account_reserve_and_partial_settlement() -> None:
    account = CreditAccount.open(user_id="octocat")
    assert account.available == 300

    reserved = account.reserve(
        expected_version=1,
        idempotency_key="request:1",
        amount=40,
    )
    assert reserved.version == 2
    assert reserved.reserved == 40
    assert reserved.available == 260

    settled = reserved.settle(
        expected_version=2,
        idempotency_key="settle:1",
        reservation_key="request:1",
        charged=25,
    )
    assert settled.spent == 25
    assert settled.reserved == 0
    assert settled.available == 275
    assert settled.reservations[0].status is ReservationStatus.SETTLED
    assert settled.reservations[0].charged == 25


def test_exact_replay_is_noop_and_conflicting_replay_fails() -> None:
    account = CreditAccount.open(user_id="octocat").reserve(
        expected_version=1,
        idempotency_key="request:1",
        amount=10,
    )

    assert (
        account.reserve(
            expected_version=1,
            idempotency_key="request:1",
            amount=10,
        )
        is account
    )
    with pytest.raises(IdempotencyConflict):
        account.reserve(
            expected_version=account.version,
            idempotency_key="request:1",
            amount=11,
        )


def test_compare_and_swap_version_and_available_balance_are_enforced() -> None:
    account = CreditAccount.open(user_id="octocat")

    with pytest.raises(ConcurrentCreditUpdate):
        account.reserve(
            expected_version=0,
            idempotency_key="request:1",
            amount=1,
        )
    with pytest.raises(InsufficientCredits):
        account.reserve(
            expected_version=1,
            idempotency_key="request:1",
            amount=301,
        )


def test_top_up_and_refund_are_audited_transitions() -> None:
    account = CreditAccount.open(user_id="octocat", initial_credits=0)
    account = account.grant(
        expected_version=1,
        idempotency_key="grant:1",
        amount=50,
        actor_id="maintainer",
        reason="approved incident recovery",
    )
    account = account.reserve(
        expected_version=2,
        idempotency_key="request:1",
        amount=50,
    )
    account = account.refund(
        expected_version=3,
        idempotency_key="refund:1",
        reservation_key="request:1",
    )

    assert account.granted == 50
    assert account.spent == 0
    assert account.available == 50
    assert account.reservations[0].status is ReservationStatus.REFUNDED
    with pytest.raises(InvalidCreditTransition):
        account.settle(
            expected_version=account.version,
            idempotency_key="settle:1",
            reservation_key="request:1",
            charged=1,
        )


def test_constructor_rejects_state_not_supported_by_ledger() -> None:
    account = CreditAccount.open(user_id="octocat").reserve(
        expected_version=1,
        idempotency_key="request:1",
        amount=10,
    )
    inconsistent_reservation = replace(
        account.reservations[0],
        status=ReservationStatus.SETTLED,
    )

    with pytest.raises(CreditError, match="terminal operation"):
        replace(account, reservations=(inconsistent_reservation,))
