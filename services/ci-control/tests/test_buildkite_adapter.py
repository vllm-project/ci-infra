from typing import Any

import pytest

from vllm_ci_control.adapters.buildkite import (
    BuildkiteClient,
    BuildkiteProtocolError,
)


class FakeTransport:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def client(transport: FakeTransport, *, max_pages: int = 100):
    return BuildkiteClient(
        token="secret",
        organization="vllm",
        pipeline="ci",
        transport=transport,
        max_pages=max_pages,
    )


def test_get_build_explicitly_requests_retry_history() -> None:
    transport = FakeTransport({"number": 42, "jobs": []})

    result = client(transport).get_build(42)

    assert result["number"] == 42
    assert "include_retried_jobs=true" in transport.calls[0]["url"]


def test_list_jobs_follows_cursor_until_complete() -> None:
    next_url = (
        "https://api.buildkite.com/v2/organizations/vllm/pipelines/ci/"
        "builds/42/jobs?after=cursor"
    )
    transport = FakeTransport(
        {
            "items": [{"id": "first"}],
            "links": {"next": next_url},
        },
        {
            "items": [{"id": "second"}],
            "links": {"next": None},
        },
    )

    jobs = client(transport).list_jobs(42)

    assert [job["id"] for job in jobs] == ["first", "second"]
    assert len(transport.calls) == 2


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "https://evil.example/jobs?after=secret",
        "http://api.buildkite.com/v2/organizations/vllm/pipelines/ci/builds/42/jobs",
        "https://api.buildkite.com/v2/organizations/other/pipelines/ci/builds/42/jobs",
    ],
)
def test_list_jobs_rejects_unsafe_provider_cursor(unsafe_url: str) -> None:
    transport = FakeTransport(
        {"items": [], "links": {"next": unsafe_url}},
    )

    with pytest.raises(BuildkiteProtocolError, match="unsafe"):
        client(transport).list_jobs(42)


def test_list_jobs_rejects_cursor_cycles() -> None:
    first_url = (
        "https://api.buildkite.com/v2/organizations/vllm/pipelines/ci/"
        "builds/42/jobs?after=again"
    )
    transport = FakeTransport(
        {"items": [], "links": {"next": first_url}},
        {"items": [], "links": {"next": first_url}},
    )

    with pytest.raises(BuildkiteProtocolError, match="cycle"):
        client(transport).list_jobs(42)


def test_list_builds_encodes_exact_filters() -> None:
    transport = FakeTransport([])

    client(transport).list_builds(
        branch="main",
        commit="a" * 40,
        states=("passed", "failed"),
        metadata={"ci-control-request": "request:1"},
    )

    url = transport.calls[0]["url"]
    assert "branch=main" in url
    assert f"commit={'a' * 40}" in url
    assert "state%5B%5D=passed" in url
    assert "state%5B%5D=failed" in url
    assert "meta_data%5Bci-control-request%5D=request%3A1" in url


def test_retry_targets_one_validated_job() -> None:
    transport = FakeTransport({"id": "retry-id", "state": "scheduled"})

    client(transport).retry_job(
        build_number=42,
        job_id="b63254c0-3271-4a98-8270-7cfbd6c2f14e",
    )

    call = transport.calls[0]
    assert call["method"] == "PUT"
    assert call["url"].endswith(
        "/builds/42/jobs/b63254c0-3271-4a98-8270-7cfbd6c2f14e/retry"
    )


def test_pagination_bound_fails_closed() -> None:
    full_page = [{"number": index} for index in range(100)]
    transport = FakeTransport(full_page)

    with pytest.raises(BuildkiteProtocolError, match="page bound"):
        client(transport, max_pages=1).list_builds()
