"""Buildkite REST adapter with bounded and authenticated pagination."""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from .http import JsonHttpTransport

_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
_JOB_ID = re.compile(r"[A-Za-z0-9-]{1,100}")
_API_ORIGIN = "https://api.buildkite.com"


class JsonTransport(Protocol):
    """Transport seam used by provider adapter tests."""

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        body: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Perform one JSON request."""


class BuildkiteProtocolError(RuntimeError):
    """Buildkite returned a response that violates the expected schema."""


class BuildkiteClient:
    """Read authoritative jobs and perform explicitly planned mutations."""

    def __init__(
        self,
        *,
        token: str,
        organization: str,
        pipeline: str,
        transport: JsonTransport | None = None,
        max_pages: int = 100,
    ) -> None:
        if not token:
            raise ValueError("Buildkite token must not be empty")
        if _SLUG.fullmatch(organization) is None:
            raise ValueError("invalid Buildkite organization slug")
        if _SLUG.fullmatch(pipeline) is None:
            raise ValueError("invalid Buildkite pipeline slug")
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        self._organization = organization
        self._pipeline = pipeline
        self._transport = transport or JsonHttpTransport()
        self._max_pages = max_pages
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "vllm-ci-control",
        }
        organization_path = urllib.parse.quote(organization, safe="")
        pipeline_path = urllib.parse.quote(pipeline, safe="")
        self._base_path = (
            f"/v2/organizations/{organization_path}/pipelines/{pipeline_path}/builds"
        )

    def list_builds(
        self,
        *,
        branch: str | None = None,
        commit: str | None = None,
        states: Sequence[str] = (),
        metadata: Mapping[str, str] | None = None,
        exclude_jobs: bool = True,
    ) -> tuple[dict[str, Any], ...]:
        """List matching builds completely within the configured page bound."""

        query: list[tuple[str, str]] = [
            ("exclude_jobs", str(exclude_jobs).lower()),
            ("exclude_pipeline", "true"),
            ("per_page", "100"),
        ]
        if branch is not None:
            query.append(("branch", branch))
        if commit is not None:
            query.append(("commit", commit))
        for state in states:
            query.append(("state[]", state))
        for key, value in sorted((metadata or {}).items()):
            query.append((f"meta_data[{key}]", value))

        builds = []
        for page in range(1, self._max_pages + 1):
            response = self._request(
                self._base_path,
                query=[*query, ("page", str(page))],
            )
            if not isinstance(response, list) or not all(
                isinstance(item, dict) for item in response
            ):
                raise BuildkiteProtocolError(
                    "build list response must be an array of objects"
                )
            builds.extend(response)
            if len(response) < 100:
                return tuple(builds)
        raise BuildkiteProtocolError(
            "build pagination exceeded the configured page bound"
        )

    def get_build(
        self,
        build_number: int,
        *,
        include_retried_jobs: bool = True,
    ) -> dict[str, Any]:
        """Fetch one build with embedded jobs and explicit retry history."""

        number = _positive_number(build_number, label="build number")
        response = self._request(
            f"{self._base_path}/{number}",
            query=[
                ("include_retried_jobs", str(include_retried_jobs).lower()),
            ],
        )
        if not isinstance(response, dict):
            raise BuildkiteProtocolError("build response must be an object")
        return response

    def list_jobs(
        self,
        build_number: int,
        *,
        include_retried_jobs: bool = True,
        states: Sequence[str] = (),
    ) -> tuple[dict[str, Any], ...]:
        """Fetch every job using Buildkite's cursor endpoint.

        The caller can treat a successful return as pagination-complete. A
        malformed, cyclic, cross-origin, or overlong cursor chain fails closed.
        """

        number = _positive_number(build_number, label="build number")
        query: list[tuple[str, str]] = [
            ("include_retried_jobs", str(include_retried_jobs).lower()),
            ("per_page", "100"),
        ]
        for state in states:
            query.append(("state[]", state))
        next_url = self._url(
            f"{self._base_path}/{number}/jobs",
            query=query,
        )
        seen_urls = set()
        jobs = []

        for _ in range(self._max_pages):
            if next_url in seen_urls:
                raise BuildkiteProtocolError("job pagination contains a cycle")
            seen_urls.add(next_url)
            _require_safe_cursor(next_url, self._base_path)
            response = self._transport.request(
                next_url,
                headers=self._headers,
            )
            if not isinstance(response, Mapping):
                raise BuildkiteProtocolError("job list response must be an object")
            items = response.get("items")
            links = response.get("links")
            if not isinstance(items, list) or not all(
                isinstance(item, dict) for item in items
            ):
                raise BuildkiteProtocolError(
                    "job list items must be an array of objects"
                )
            if not isinstance(links, Mapping):
                raise BuildkiteProtocolError("job list links must be an object")
            jobs.extend(items)
            candidate = links.get("next")
            if candidate is None:
                return tuple(jobs)
            if not isinstance(candidate, str):
                raise BuildkiteProtocolError(
                    "job pagination next link must be a URL or null"
                )
            next_url = candidate
        raise BuildkiteProtocolError(
            "job pagination exceeded the configured page bound"
        )

    def create_build(
        self,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Create a build from a server-derived, immutable plan payload."""

        response = self._request(
            self._base_path,
            method="POST",
            body=payload,
        )
        if not isinstance(response, dict):
            raise BuildkiteProtocolError("create build response must be an object")
        return response

    def retry_job(
        self,
        *,
        build_number: int,
        job_id: str,
    ) -> dict[str, Any]:
        """Retry exactly one previously inspected job."""

        number = _positive_number(build_number, label="build number")
        if _JOB_ID.fullmatch(job_id) is None:
            raise ValueError("invalid Buildkite job id")
        job_path = urllib.parse.quote(job_id, safe="")
        response = self._request(
            f"{self._base_path}/{number}/jobs/{job_path}/retry",
            method="PUT",
            body={},
        )
        if not isinstance(response, dict):
            raise BuildkiteProtocolError("retry job response must be an object")
        return response

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: Mapping[str, Any] | None = None,
        query: Sequence[tuple[str, str]] = (),
    ) -> Any:
        return self._transport.request(
            self._url(path, query=query),
            method=method,
            body=body,
            headers=self._headers,
        )

    @staticmethod
    def _url(
        path: str,
        *,
        query: Sequence[tuple[str, str]] = (),
    ) -> str:
        url = f"{_API_ORIGIN}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return url


def _positive_number(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _require_safe_cursor(url: str, base_path: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "api.buildkite.com"
        or not parsed.path.startswith(f"{base_path}/")
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise BuildkiteProtocolError("Buildkite returned an unsafe pagination URL")
