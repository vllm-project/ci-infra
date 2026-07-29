from __future__ import annotations

import pytest

from vllm_ci_control.commands import (
    CommandParseError,
    CreditsAddCommand,
    CreditsCommand,
    HelpCommand,
    ListCommand,
    ListTarget,
    PlanCommand,
    RetryFailuresCommand,
    RunCommand,
    StatusCommand,
    StatusRefreshCommand,
    StatusRequestCommand,
    StatusTarget,
    UnrelatedComment,
    parse_command,
)
from vllm_ci_control.models import Selector


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("/ci help", HelpCommand()),
        ("/ci status", StatusCommand()),
        ("/ci status pr", StatusCommand(StatusTarget.PR)),
        (
            "/ci status main groups:cpu,amd",
            StatusCommand(
                StatusTarget.MAIN,
                Selector(groups=frozenset({"cpu", "amd"})),
            ),
        ),
        ("/ci status refresh", StatusRefreshCommand()),
        (
            "/ci status request:req:123",
            StatusRequestCommand("req:123"),
        ),
        ("/ci list", ListCommand()),
        ("/ci list jobs", ListCommand(ListTarget.JOBS)),
        ("/ci plan all", PlanCommand(Selector.all())),
        (
            "/ci run groups:amd areas:attention jobs:amd.full",
            RunCommand(
                Selector(
                    groups=frozenset({"amd"}),
                    areas=frozenset({"attention"}),
                    jobs=frozenset({"amd.full"}),
                )
            ),
        ),
        ("/ci retry failures", RetryFailuresCommand()),
        (
            "/ci retry failures groups:upstream,cpu",
            RetryFailuresCommand(Selector(groups=frozenset({"upstream", "cpu"}))),
        ),
        ("/ci credits", CreditsCommand()),
        (
            "/ci credits add @Example-User 25 incident recovery",
            CreditsAddCommand(
                username="example-user",
                amount=25,
                reason="incident recovery",
            ),
        ),
    ],
)
def test_parse_supported_commands(body: str, expected: object) -> None:
    assert parse_command(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        "/ci",
        "/ci\nhelp",
        "/ci help extra",
        "/ci status summary groups:cpu",
        "/ci status refresh groups:cpu",
        "/ci status request:",
        "/ci retry",
        "/ci retry failures all groups:cpu",
        "/ci run",
        "/ci run groups:cpu groups:amd",
        "/ci run groups:cpu,cpu",
        "/ci run unknown:cpu",
        "/ci credits add @user 0 reason",
        "/ci credits add user 1 reason",
        "/ci credits add @user 1",
        "/ci unknown",
    ],
)
def test_rejects_malformed_ci_commands(body: str) -> None:
    with pytest.raises(CommandParseError):
        parse_command(body)


@pytest.mark.parametrize("body", ["looks good", "", " /ci help", None])
def test_unrelated_comments_are_distinct(body: object) -> None:
    with pytest.raises(UnrelatedComment):
        parse_command(body)
