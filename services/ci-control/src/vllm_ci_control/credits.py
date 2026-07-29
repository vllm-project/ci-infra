"""Pure immutable credit ledger and reservation transitions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

MAX_CREDIT_VALUE = 2**63 - 1
MAX_KEY_LENGTH = 240
MAX_TEXT_LENGTH = 240


class CreditError(ValueError):
    """Base error for credit state or a requested transition."""


class ConcurrentCreditUpdate(CreditError):
    """The caller planned a transition from an obsolete account version."""


class InsufficientCredits(CreditError):
    """The account cannot cover a requested reservation."""


class IdempotencyConflict(CreditError):
    """An idempotency key was reused for a different operation."""


class InvalidCreditTransition(CreditError):
    """A reservation cannot undergo the requested state transition."""


class ReservationStatus(StrEnum):
    """Terminal state of one credit reservation."""

    ACTIVE = "active"
    SETTLED = "settled"
    REFUNDED = "refunded"


class CreditOperationKind(StrEnum):
    """Append-only credit ledger operation types."""

    INITIAL_GRANT = "initial_grant"
    GRANT = "grant"
    RESERVE = "reserve"
    SETTLE = "settle"
    REFUND = "refund"


@dataclass(frozen=True, slots=True)
class CreditReservation:
    """Reserved maximum cost for one idempotent CI request."""

    key: str
    amount: int
    status: ReservationStatus = ReservationStatus.ACTIVE
    charged: int = 0

    def __post_init__(self) -> None:
        _validate_key(self.key, label="reservation key")
        _validate_amount(self.amount, label="reservation amount", positive=True)
        _validate_amount(self.charged, label="charged amount", positive=False)
        if self.charged > self.amount:
            raise CreditError("charged amount cannot exceed reservation")
        if self.status is ReservationStatus.ACTIVE and self.charged:
            raise CreditError("active reservation cannot already be charged")
        if self.status is ReservationStatus.REFUNDED and self.charged:
            raise CreditError("refunded reservation cannot be charged")


@dataclass(frozen=True, slots=True)
class CreditOperation:
    """One immutable, idempotent ledger operation."""

    idempotency_key: str
    kind: CreditOperationKind
    amount: int
    reservation_key: str | None = None
    actor_id: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        _validate_key(self.idempotency_key, label="idempotency key")
        _validate_amount(self.amount, label="operation amount", positive=False)
        if self.reservation_key is not None:
            _validate_key(self.reservation_key, label="reservation key")
        if self.actor_id is not None:
            _validate_text(self.actor_id, label="actor id")
        if self.reason is not None:
            _validate_text(self.reason, label="reason")


@dataclass(frozen=True, slots=True)
class CreditAccount:
    """Immutable account state derived through append-only operations."""

    user_id: str
    version: int
    granted: int
    spent: int
    reservations: tuple[CreditReservation, ...]
    operations: tuple[CreditOperation, ...]

    def __post_init__(self) -> None:
        _validate_text(self.user_id, label="user id")
        if self.version < 1:
            raise CreditError("account version must be positive")
        _validate_amount(self.granted, label="granted credits", positive=False)
        _validate_amount(self.spent, label="spent credits", positive=False)
        if self.spent > self.granted:
            raise CreditError("spent credits cannot exceed granted credits")
        reservation_keys = [item.key for item in self.reservations]
        if len(reservation_keys) != len(set(reservation_keys)):
            raise CreditError("reservation keys must be unique")
        operation_keys = [item.idempotency_key for item in self.operations]
        if len(operation_keys) != len(set(operation_keys)):
            raise CreditError("idempotency keys must be unique")
        if self.version != len(self.operations):
            raise CreditError(
                "account version must match the number of ledger operations"
            )
        initial_operations = [
            item
            for item in self.operations
            if item.kind is CreditOperationKind.INITIAL_GRANT
        ]
        if len(initial_operations) != 1:
            raise CreditError("ledger must contain exactly one initial grant")
        if not self.operations or self.operations[0] is not initial_operations[0]:
            raise CreditError("initial grant must be the first operation")
        if self.granted != sum(
            item.amount
            for item in self.operations
            if item.kind
            in {
                CreditOperationKind.INITIAL_GRANT,
                CreditOperationKind.GRANT,
            }
        ):
            raise CreditError("granted total does not match ledger operations")
        if self.spent != sum(
            item.amount
            for item in self.operations
            if item.kind is CreditOperationKind.SETTLE
        ):
            raise CreditError("spent total does not match ledger operations")
        if self.available < 0:
            raise CreditError("active reservations overdraw the account")
        self._validate_reservation_ledger()

    @classmethod
    def open(
        cls,
        *,
        user_id: str,
        initial_credits: int = 300,
        idempotency_key: str | None = None,
    ) -> CreditAccount:
        """Create a new account with one idempotent initial grant."""

        _validate_text(user_id, label="user id")
        _validate_amount(
            initial_credits,
            label="initial credits",
            positive=False,
        )
        key = idempotency_key or f"initial:{user_id}:v1"
        operation = CreditOperation(
            idempotency_key=key,
            kind=CreditOperationKind.INITIAL_GRANT,
            amount=initial_credits,
            actor_id="system",
            reason="initial credit grant",
        )
        return cls(
            user_id=user_id,
            version=1,
            granted=initial_credits,
            spent=0,
            reservations=(),
            operations=(operation,),
        )

    @property
    def reserved(self) -> int:
        """Credits held by active requests."""

        return sum(
            item.amount
            for item in self.reservations
            if item.status is ReservationStatus.ACTIVE
        )

    @property
    def available(self) -> int:
        """Credits available for a new reservation."""

        return self.granted - self.spent - self.reserved

    def grant(
        self,
        *,
        expected_version: int,
        idempotency_key: str,
        amount: int,
        actor_id: str,
        reason: str,
    ) -> CreditAccount:
        """Add an audited grant, or return unchanged on exact replay."""

        _validate_amount(amount, label="grant amount", positive=True)
        operation = CreditOperation(
            idempotency_key=idempotency_key,
            kind=CreditOperationKind.GRANT,
            amount=amount,
            actor_id=actor_id,
            reason=reason,
        )
        if self._is_replay(operation):
            return self
        self._require_version(expected_version)
        _checked_add(self.granted, amount)
        return replace(
            self,
            version=self.version + 1,
            granted=self.granted + amount,
            operations=(*self.operations, operation),
        )

    def reserve(
        self,
        *,
        expected_version: int,
        idempotency_key: str,
        amount: int,
    ) -> CreditAccount:
        """Reserve a server-derived maximum request cost atomically."""

        _validate_amount(amount, label="reservation amount", positive=True)
        operation = CreditOperation(
            idempotency_key=idempotency_key,
            kind=CreditOperationKind.RESERVE,
            amount=amount,
            reservation_key=idempotency_key,
        )
        if self._is_replay(operation):
            return self
        self._require_version(expected_version)
        if amount > self.available:
            raise InsufficientCredits(
                f"reservation requires {amount} credits; {self.available} available"
            )
        reservation = CreditReservation(
            key=idempotency_key,
            amount=amount,
        )
        return replace(
            self,
            version=self.version + 1,
            reservations=(*self.reservations, reservation),
            operations=(*self.operations, operation),
        )

    def settle(
        self,
        *,
        expected_version: int,
        idempotency_key: str,
        reservation_key: str,
        charged: int,
    ) -> CreditAccount:
        """Charge accepted work and release the unused reservation."""

        _validate_amount(charged, label="settlement charge", positive=False)
        operation = CreditOperation(
            idempotency_key=idempotency_key,
            kind=CreditOperationKind.SETTLE,
            amount=charged,
            reservation_key=reservation_key,
        )
        if self._is_replay(operation):
            return self
        self._require_version(expected_version)
        reservation = self._reservation(reservation_key)
        if reservation.status is not ReservationStatus.ACTIVE:
            raise InvalidCreditTransition("only an active reservation can be settled")
        if charged > reservation.amount:
            raise InvalidCreditTransition("settlement cannot exceed the reservation")
        _checked_add(self.spent, charged)
        updated = replace(
            reservation,
            status=ReservationStatus.SETTLED,
            charged=charged,
        )
        return replace(
            self,
            version=self.version + 1,
            spent=self.spent + charged,
            reservations=self._replace_reservation(updated),
            operations=(*self.operations, operation),
        )

    def refund(
        self,
        *,
        expected_version: int,
        idempotency_key: str,
        reservation_key: str,
    ) -> CreditAccount:
        """Release a reservation after confirmed undispatched work."""

        operation = CreditOperation(
            idempotency_key=idempotency_key,
            kind=CreditOperationKind.REFUND,
            amount=0,
            reservation_key=reservation_key,
        )
        if self._is_replay(operation):
            return self
        self._require_version(expected_version)
        reservation = self._reservation(reservation_key)
        if reservation.status is not ReservationStatus.ACTIVE:
            raise InvalidCreditTransition("only an active reservation can be refunded")
        updated = replace(
            reservation,
            status=ReservationStatus.REFUNDED,
        )
        return replace(
            self,
            version=self.version + 1,
            reservations=self._replace_reservation(updated),
            operations=(*self.operations, operation),
        )

    def _require_version(self, expected_version: int) -> None:
        if expected_version != self.version:
            raise ConcurrentCreditUpdate(
                f"expected account version {expected_version}; "
                f"current version is {self.version}"
            )

    def _is_replay(self, operation: CreditOperation) -> bool:
        for existing in self.operations:
            if existing.idempotency_key != operation.idempotency_key:
                continue
            if existing != operation:
                raise IdempotencyConflict(
                    "idempotency key was reused with different operation data"
                )
            return True
        return False

    def _reservation(self, key: str) -> CreditReservation:
        _validate_key(key, label="reservation key")
        for reservation in self.reservations:
            if reservation.key == key:
                return reservation
        raise InvalidCreditTransition(f"unknown reservation: {key}")

    def _replace_reservation(
        self,
        replacement: CreditReservation,
    ) -> tuple[CreditReservation, ...]:
        return tuple(
            replacement if item.key == replacement.key else item
            for item in self.reservations
        )

    def _validate_reservation_ledger(self) -> None:
        reservations = {item.key: item for item in self.reservations}
        reserve_operations: dict[str, CreditOperation] = {}
        terminal_operations: dict[str, list[CreditOperation]] = {}

        for operation in self.operations:
            if operation.kind in {
                CreditOperationKind.INITIAL_GRANT,
                CreditOperationKind.GRANT,
            }:
                if operation.reservation_key is not None:
                    raise CreditError("grant operations cannot name a reservation")
                continue
            if operation.reservation_key is None:
                raise CreditError("reservation operations must name a reservation")
            if operation.kind is CreditOperationKind.RESERVE:
                if operation.reservation_key in reserve_operations:
                    raise CreditError(
                        "a reservation must have exactly one reserve operation"
                    )
                reserve_operations[operation.reservation_key] = operation
            else:
                terminal_operations.setdefault(
                    operation.reservation_key,
                    [],
                ).append(operation)

        if set(reserve_operations) != set(reservations):
            raise CreditError("reservations and reserve operations must correspond")
        for key, reservation in reservations.items():
            reserve = reserve_operations[key]
            if reserve.amount != reservation.amount:
                raise CreditError("reservation amount does not match reserve operation")
            terminals = terminal_operations.get(key, [])
            if reservation.status is ReservationStatus.ACTIVE:
                if terminals:
                    raise CreditError(
                        "active reservation cannot have a terminal operation"
                    )
                continue
            if len(terminals) != 1:
                raise CreditError(
                    "terminal reservation must have one terminal operation"
                )
            terminal = terminals[0]
            if reservation.status is ReservationStatus.SETTLED:
                if (
                    terminal.kind is not CreditOperationKind.SETTLE
                    or terminal.amount != reservation.charged
                ):
                    raise CreditError(
                        "settled reservation does not match its operation"
                    )
            elif (
                terminal.kind is not CreditOperationKind.REFUND or terminal.amount != 0
            ):
                raise CreditError("refunded reservation does not match its operation")

        unknown_terminal_keys = set(terminal_operations) - set(reservations)
        if unknown_terminal_keys:
            raise CreditError("terminal operation references an unknown reservation")


def _validate_amount(
    value: object,
    *,
    label: str,
    positive: bool,
) -> int:
    minimum = 1 if positive else 0
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > MAX_CREDIT_VALUE
    ):
        qualifier = "positive" if positive else "non-negative"
        raise CreditError(f"{label} must be a bounded {qualifier} integer")
    return value


def _validate_key(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_KEY_LENGTH
        or value.strip() != value
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise CreditError(f"{label} must be bounded printable text without whitespace")
    return value


def _validate_text(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TEXT_LENGTH
        or value.strip() != value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CreditError(f"{label} must be bounded normalized printable text")
    return value


def _checked_add(left: int, right: int) -> int:
    result = left + right
    if result > MAX_CREDIT_VALUE:
        raise CreditError("credit total exceeds the supported range")
    return result
