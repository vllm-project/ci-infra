"""Strict parser for the public ``/ci`` comment grammar."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeAlias

from .models import DomainValidationError, Selector, validate_stable_id

MAX_COMMAND_LENGTH = 1_000
MAX_REASON_LENGTH = 240
MAX_SELECTOR_VALUES = 100
_GITHUB_LOGIN_PATTERN = re.compile(r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


class CommandParseError(ValueError):
    """A comment starts with ``/ci`` but is not a valid command."""


class UnrelatedComment(ValueError):
    """A comment is not addressed to the CI control plane."""


class CommandKind(StrEnum):
    """Supported command operations."""

    HELP = "help"
    STATUS = "status"
    STATUS_REFRESH = "status_refresh"
    STATUS_REQUEST = "status_request"
    LIST = "list"
    PLAN = "plan"
    RUN = "run"
    RETRY_FAILURES = "retry_failures"
    CREDITS = "credits"
    CREDITS_ADD = "credits_add"


class StatusTarget(StrEnum):
    """Optional status view."""

    SUMMARY = "summary"
    PR = "pr"
    MAIN = "main"


class ListTarget(StrEnum):
    """Optional catalog list view."""

    ALL = "all"
    GROUPS = "groups"
    AREAS = "areas"
    JOBS = "jobs"


@dataclass(frozen=True, slots=True)
class HelpCommand:
    kind: CommandKind = field(default=CommandKind.HELP, init=False)


@dataclass(frozen=True, slots=True)
class StatusCommand:
    target: StatusTarget = StatusTarget.SUMMARY
    selector: Selector | None = None
    kind: CommandKind = field(default=CommandKind.STATUS, init=False)

    def __post_init__(self) -> None:
        if self.target is StatusTarget.SUMMARY and self.selector is not None:
            raise DomainValidationError("summary status cannot include a selector")


@dataclass(frozen=True, slots=True)
class StatusRefreshCommand:
    kind: CommandKind = field(
        default=CommandKind.STATUS_REFRESH,
        init=False,
    )


@dataclass(frozen=True, slots=True)
class StatusRequestCommand:
    request_id: str
    kind: CommandKind = field(
        default=CommandKind.STATUS_REQUEST,
        init=False,
    )

    def __post_init__(self) -> None:
        validate_stable_id(self.request_id, label="request id")


@dataclass(frozen=True, slots=True)
class ListCommand:
    target: ListTarget = ListTarget.ALL
    kind: CommandKind = field(default=CommandKind.LIST, init=False)


@dataclass(frozen=True, slots=True)
class PlanCommand:
    selector: Selector
    kind: CommandKind = field(default=CommandKind.PLAN, init=False)


@dataclass(frozen=True, slots=True)
class RunCommand:
    selector: Selector
    kind: CommandKind = field(default=CommandKind.RUN, init=False)


@dataclass(frozen=True, slots=True)
class RetryFailuresCommand:
    selector: Selector | None = None
    kind: CommandKind = field(
        default=CommandKind.RETRY_FAILURES,
        init=False,
    )


@dataclass(frozen=True, slots=True)
class CreditsCommand:
    kind: CommandKind = field(default=CommandKind.CREDITS, init=False)


@dataclass(frozen=True, slots=True)
class CreditsAddCommand:
    username: str
    amount: int
    reason: str
    kind: CommandKind = field(default=CommandKind.CREDITS_ADD, init=False)

    def __post_init__(self) -> None:
        if _GITHUB_LOGIN_PATTERN.fullmatch(f"@{self.username}") is None:
            raise DomainValidationError("username is not a valid GitHub login")
        if self.amount <= 0:
            raise DomainValidationError("credit amount must be positive")
        _validate_reason(self.reason)


ParsedCommand: TypeAlias = (
    HelpCommand
    | StatusCommand
    | StatusRefreshCommand
    | StatusRequestCommand
    | ListCommand
    | PlanCommand
    | RunCommand
    | RetryFailuresCommand
    | CreditsCommand
    | CreditsAddCommand
)
StatusParsedCommand: TypeAlias = (
    StatusCommand | StatusRefreshCommand | StatusRequestCommand
)


def parse_command(body: object) -> ParsedCommand:
    """Parse one exact command from a GitHub comment body.

    Leading whitespace, newlines, quoting, shell syntax, unknown fields, and
    extra arguments are rejected. ASCII spaces and tabs may separate tokens or
    trail the command.
    """

    if not isinstance(body, str):
        raise UnrelatedComment("comment body is not text")
    if not body.startswith("/ci"):
        raise UnrelatedComment("comment is not addressed to /ci")
    if len(body) > MAX_COMMAND_LENGTH:
        raise CommandParseError("command is too long")
    if "\r" in body or "\n" in body:
        raise CommandParseError("commands must occupy exactly one line")

    normalized = body.rstrip(" \t")
    if not normalized.startswith("/ci") or (
        len(normalized) > 3 and normalized[3] not in {" ", "\t"}
    ):
        raise CommandParseError("expected /ci followed by a command")
    tokens = re.split(r"[ \t]+", normalized)
    if not tokens or tokens[0] != "/ci" or len(tokens) == 1:
        raise CommandParseError("missing /ci command")

    operation = tokens[1]
    arguments = tokens[2:]
    if operation == "help":
        _require_arity(arguments, 0, "/ci help")
        return HelpCommand()
    if operation == "status":
        return _parse_status(arguments)
    if operation == "list":
        return _parse_list(arguments)
    if operation == "plan":
        return PlanCommand(_parse_selector(arguments))
    if operation == "run":
        return RunCommand(_parse_selector(arguments))
    if operation == "retry":
        if not arguments or arguments[0] != "failures":
            raise CommandParseError("expected /ci retry failures")
        selector = _parse_selector(arguments[1:]) if len(arguments) > 1 else None
        return RetryFailuresCommand(selector)
    if operation == "credits":
        return _parse_credits(arguments)
    raise CommandParseError(f"unknown /ci command: {operation}")


def _parse_status(arguments: list[str]) -> StatusParsedCommand:
    if not arguments:
        return StatusCommand()
    if arguments == ["refresh"]:
        return StatusRefreshCommand()
    if arguments[0].startswith("request:"):
        _require_arity(arguments, 1, "/ci status request:<id>")
        request_id = arguments[0].removeprefix("request:")
        try:
            return StatusRequestCommand(request_id)
        except DomainValidationError as exc:
            raise CommandParseError(str(exc)) from exc
    if arguments[0] not in {"pr", "main"}:
        raise CommandParseError(
            "status target must be pr, main, refresh, or request:<id>"
        )
    target = StatusTarget(arguments[0])
    selector = _parse_selector(arguments[1:]) if len(arguments) > 1 else None
    return StatusCommand(target, selector)


def _parse_list(arguments: list[str]) -> ListCommand:
    if not arguments:
        return ListCommand()
    _require_arity(arguments, 1, "/ci list [groups|areas|jobs]")
    try:
        return ListCommand(ListTarget(arguments[0]))
    except ValueError as exc:
        raise CommandParseError("list target must be groups, areas, or jobs") from exc


def _parse_credits(arguments: list[str]) -> ParsedCommand:
    if not arguments:
        return CreditsCommand()
    if len(arguments) < 4 or arguments[0] != "add":
        raise CommandParseError("expected /ci credits add @user <amount> <reason>")
    login = arguments[1]
    if _GITHUB_LOGIN_PATTERN.fullmatch(login) is None:
        raise CommandParseError("credit recipient must be a GitHub @user")
    amount_text = arguments[2]
    if not amount_text.isascii() or not amount_text.isdecimal():
        raise CommandParseError("credit amount must be a positive integer")
    amount = int(amount_text)
    if amount <= 0 or amount > 1_000_000_000:
        raise CommandParseError("credit amount is outside the accepted range")
    reason = " ".join(arguments[3:])
    try:
        _validate_reason(reason)
    except DomainValidationError as exc:
        raise CommandParseError(str(exc)) from exc
    return CreditsAddCommand(
        username=login[1:].lower(),
        amount=amount,
        reason=reason,
    )


def _parse_selector(arguments: list[str]) -> Selector:
    if not arguments:
        raise CommandParseError("plan and run require a selector")
    if arguments == ["all"]:
        return Selector.all()
    if "all" in arguments:
        raise CommandParseError("all cannot be combined with another selector")

    parsed: dict[str, frozenset[str]] = {}
    for token in arguments:
        if ":" not in token:
            raise CommandParseError(f"invalid selector token: {token}")
        dimension, raw_values = token.split(":", 1)
        if dimension not in {"groups", "areas", "jobs"}:
            raise CommandParseError(f"unknown selector dimension: {dimension}")
        if dimension in parsed:
            raise CommandParseError(f"selector dimension repeated: {dimension}")
        values = raw_values.split(",")
        if not values or any(not value for value in values):
            raise CommandParseError(f"{dimension} selector contains an empty value")
        if len(values) > MAX_SELECTOR_VALUES:
            raise CommandParseError(f"{dimension} selector has too many values")
        if len(set(values)) != len(values):
            raise CommandParseError(f"{dimension} selector contains duplicate values")
        try:
            parsed[dimension] = frozenset(
                validate_stable_id(value, label=dimension[:-1]) for value in values
            )
        except DomainValidationError as exc:
            raise CommandParseError(str(exc)) from exc
    return Selector(
        groups=parsed.get("groups", frozenset()),
        areas=parsed.get("areas", frozenset()),
        jobs=parsed.get("jobs", frozenset()),
    )


def _validate_reason(reason: object) -> str:
    if (
        not isinstance(reason, str)
        or not reason
        or len(reason) > MAX_REASON_LENGTH
        or reason.strip(" \t") != reason
        or any(ord(character) < 32 or ord(character) == 127 for character in reason)
    ):
        raise DomainValidationError("credit reason must be normalized printable text")
    return reason


def _require_arity(
    arguments: list[str],
    expected: int,
    syntax: str,
) -> None:
    if len(arguments) != expected:
        raise CommandParseError(f"expected {syntax}")
