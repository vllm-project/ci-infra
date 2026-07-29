from typing import Any

import pytest

from vllm_ci_control.adapters.github import (
    GitHubClient,
    GitHubProtocolError,
)
from vllm_ci_control.adapters.http import HttpError
from vllm_ci_control.models import RepositoryPermission


class FakeTransport:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(transport: FakeTransport, *, max_pages: int = 100):
    return GitHubClient(
        token="secret",
        repository="vllm-project/vllm",
        transport=transport,
        max_pages=max_pages,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"permission": "write"}, RepositoryPermission.WRITE),
        (
            {"permission": "write", "role_name": "maintain"},
            RepositoryPermission.MAINTAIN,
        ),
        (
            {"permission": "read", "role_name": "triage"},
            RepositoryPermission.TRIAGE,
        ),
        (
            {"permission": "write", "role_name": "custom-ci-role"},
            RepositoryPermission.WRITE,
        ),
        ({"permission": "admin"}, RepositoryPermission.ADMIN),
        ({"permission": "read"}, RepositoryPermission.READ),
    ],
)
def test_repository_permission_uses_live_github_value(
    raw: dict[str, str],
    expected: RepositoryPermission,
) -> None:
    transport = FakeTransport(raw)
    assert client(transport).repository_permission("alice") is expected


def test_missing_collaborator_has_no_permission() -> None:
    transport = FakeTransport(HttpError(404, "Not Found"))
    assert (
        client(transport).repository_permission("external") is RepositoryPermission.NONE
    )


def test_unknown_permission_fails_closed() -> None:
    transport = FakeTransport({"permission": "owner"})
    with pytest.raises(GitHubProtocolError, match="unknown"):
        client(transport).repository_permission("alice")


def test_team_membership_requires_an_active_github_membership() -> None:
    active = FakeTransport({"state": "active", "role": "member"})
    pending = FakeTransport({"state": "pending", "role": "member"})
    missing = FakeTransport(HttpError(404, "Not Found"))

    assert client(active).is_team_member("vllm-committers", "alice")
    assert not client(pending).is_team_member("vllm-committers", "alice")
    assert not client(missing).is_team_member("vllm-committers", "alice")


def test_reactions_are_completely_paginated() -> None:
    first = [{"id": index} for index in range(100)]
    transport = FakeTransport(first, [{"id": 101}])

    result = client(transport).list_comment_reactions(42)

    assert len(result) == 101
    assert "page=2" in transport.calls[1]["url"]


def test_invalid_repository_is_rejected_before_http() -> None:
    with pytest.raises(ValueError, match="owner/name"):
        GitHubClient(
            token="secret",
            repository="https://github.com/vllm-project/vllm",
        )


def test_reaction_content_is_allowlisted() -> None:
    transport = FakeTransport({})
    with pytest.raises(ValueError, match="unsupported"):
        client(transport).add_reaction(42, "fire")
    assert transport.calls == []


def test_pagination_bound_fails_closed() -> None:
    transport = FakeTransport([{"id": index} for index in range(100)])
    with pytest.raises(GitHubProtocolError, match="page bound"):
        client(transport, max_pages=1).list_comment_reactions(42)
