"""Least-privilege GitHub REST adapter for repository and PR facts."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from typing import Any

from ..models import RepositoryPermission
from .buildkite import JsonTransport
from .http import HttpError, JsonHttpTransport

_OWNER_OR_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
_TEAM_SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,99})")
_API_ORIGIN = "https://api.github.com"


class GitHubProtocolError(RuntimeError):
    """GitHub returned a response that violates the expected schema."""


class GitHubClient:
    """Fetch live authorization/PR state and update bot-owned projections."""

    def __init__(
        self,
        *,
        token: str,
        repository: str,
        transport: JsonTransport | None = None,
        max_pages: int = 100,
    ) -> None:
        if not token:
            raise ValueError("GitHub token must not be empty")
        parts = repository.split("/")
        if len(parts) != 2 or any(
            _OWNER_OR_REPO.fullmatch(part) is None for part in parts
        ):
            raise ValueError("repository must be a normalized owner/name")
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        self._owner, self._repo = parts
        self._transport = transport or JsonHttpTransport()
        self._max_pages = max_pages
        self._headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "vllm-ci-control",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        owner_path = urllib.parse.quote(self._owner, safe="")
        repo_path = urllib.parse.quote(self._repo, safe="")
        self._base_path = f"/repos/{owner_path}/{repo_path}"

    def get_pull_request(self, number: int) -> dict[str, Any]:
        """Return the authoritative current PR object."""

        response = self._request(f"{self._base_path}/pulls/{_positive_number(number)}")
        if not isinstance(response, dict):
            raise GitHubProtocolError("pull request response must be an object")
        return response

    def repository_permission(self, username: str) -> RepositoryPermission:
        """Return the caller's live base repository permission."""

        if _LOGIN.fullmatch(username) is None:
            raise ValueError("invalid GitHub login")
        login_path = urllib.parse.quote(username, safe="")
        try:
            response = self._request(
                f"{self._base_path}/collaborators/{login_path}/permission"
            )
        except HttpError as error:
            if error.status == 404:
                return RepositoryPermission.NONE
            raise
        if not isinstance(response, Mapping):
            raise GitHubProtocolError("permission response must be an object")
        for value in (response.get("role_name"), response.get("permission")):
            try:
                return RepositoryPermission(value)
            except ValueError:
                continue
        raise GitHubProtocolError("GitHub returned an unknown repository permission")

    def is_team_member(
        self,
        team_slug: str,
        username: str,
    ) -> bool:
        """Return active membership in one team owned by the repository org."""

        if _TEAM_SLUG.fullmatch(team_slug) is None:
            raise ValueError("invalid GitHub team slug")
        if _LOGIN.fullmatch(username) is None:
            raise ValueError("invalid GitHub login")
        owner_path = urllib.parse.quote(self._owner, safe="")
        team_path = urllib.parse.quote(team_slug, safe="")
        login_path = urllib.parse.quote(username, safe="")
        try:
            response = self._request(
                f"/orgs/{owner_path}/teams/{team_path}/memberships/{login_path}"
            )
        except HttpError as error:
            if error.status == 404:
                return False
            raise
        if not isinstance(response, Mapping):
            raise GitHubProtocolError("team membership response must be an object")
        state = response.get("state")
        if state not in {"active", "pending"}:
            raise GitHubProtocolError(
                "GitHub returned an unknown team membership state"
            )
        return state == "active"

    def list_comment_reactions(
        self,
        comment_id: int,
    ) -> tuple[dict[str, Any], ...]:
        """List all reactions so comment delivery can be deduplicated."""

        return self._paginate(
            f"{self._base_path}/issues/comments/"
            f"{_positive_number(comment_id)}/reactions"
        )

    def add_comment(self, issue_number: int, body: str) -> dict[str, Any]:
        """Create a user-facing projection comment."""

        if not body or len(body) > 65_536:
            raise ValueError("comment body must be non-empty and bounded")
        response = self._request(
            f"{self._base_path}/issues/{_positive_number(issue_number)}/comments",
            method="POST",
            body={"body": body},
        )
        if not isinstance(response, dict):
            raise GitHubProtocolError("comment response must be an object")
        return response

    def add_reaction(
        self,
        comment_id: int,
        content: str,
    ) -> dict[str, Any]:
        """Add a bounded command receipt/result reaction."""

        allowed = {
            "+1",
            "-1",
            "laugh",
            "confused",
            "heart",
            "hooray",
            "rocket",
            "eyes",
        }
        if content not in allowed:
            raise ValueError("unsupported GitHub reaction")
        response = self._request(
            f"{self._base_path}/issues/comments/"
            f"{_positive_number(comment_id)}/reactions",
            method="POST",
            body={"content": content},
        )
        if not isinstance(response, dict):
            raise GitHubProtocolError("reaction response must be an object")
        return response

    def _paginate(self, path: str) -> tuple[dict[str, Any], ...]:
        items = []
        for page in range(1, self._max_pages + 1):
            response = self._request(
                path,
                query=[("per_page", "100"), ("page", str(page))],
            )
            if not isinstance(response, list) or not all(
                isinstance(item, dict) for item in response
            ):
                raise GitHubProtocolError(
                    "paginated response must be an array of objects"
                )
            items.extend(response)
            if len(response) < 100:
                return tuple(items)
        raise GitHubProtocolError(
            "GitHub pagination exceeded the configured page bound"
        )

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Mapping[str, Any] | None = None,
        query: list[tuple[str, str]] | None = None,
    ) -> Any:
        url = f"{_API_ORIGIN}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return self._transport.request(
            url,
            method=method,
            body=body,
            headers=self._headers,
        )


def _positive_number(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("GitHub numeric id must be a positive integer")
    return value
