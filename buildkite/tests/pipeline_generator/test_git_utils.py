from unittest.mock import MagicMock, patch

import pytest
import requests

import utils_lib.git_utils as git_utils


def _response(status, labels=()):
    response = MagicMock(status_code=status)
    response.json.return_value = {"labels": [{"name": n} for n in labels]}
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
    return response


@pytest.fixture(autouse=True)
def buildkite_job(monkeypatch):
    monkeypatch.setenv("BUILDKITE_JOB_ID", "job-1")
    with patch.object(git_utils.subprocess, "run") as run:
        yield run


def test_get_pr_labels_retries_5xx_then_succeeds(buildkite_job, capsys):
    responses = [_response(500), _response(502), _response(200, ["ready"])]
    with patch.object(git_utils.requests, "get", side_effect=responses), patch.object(
        git_utils.time, "sleep"
    ) as sleep:
        assert git_utils.get_pr_labels("1", "o/r") == ["ready"]

    assert [c.args[0] for c in sleep.call_args_list] == [2, 4]
    assert "failed (attempt 1/5); retrying in 2s" in capsys.readouterr().out
    buildkite_job.assert_called_once()
    assert buildkite_job.call_args.args[0][-2:] == [
        "infra-retry:job-1",
        "bootstrap-labels,3,success",
    ]


def test_get_pr_labels_gives_up_after_five_attempts(buildkite_job):
    with patch.object(
        git_utils.requests, "get", side_effect=requests.ConnectionError()
    ) as get, patch.object(git_utils.time, "sleep") as sleep:
        with pytest.raises(requests.ConnectionError):
            git_utils.get_pr_labels("1", "o/r")

    assert get.call_count == 5
    assert [c.args[0] for c in sleep.call_args_list] == [2, 4, 8, 16]
    assert buildkite_job.call_args.args[0][-1] == "bootstrap-labels,5,failed"


def test_get_pr_labels_does_not_retry_4xx(buildkite_job):
    with patch.object(
        git_utils.requests, "get", return_value=_response(404)
    ) as get, patch.object(git_utils.time, "sleep") as sleep:
        with pytest.raises(requests.HTTPError):
            git_utils.get_pr_labels("1", "o/r")

    assert get.call_count == 1
    sleep.assert_not_called()
    buildkite_job.assert_not_called()


def test_get_pr_labels_first_try_writes_no_metadata(buildkite_job):
    with patch.object(git_utils.requests, "get", return_value=_response(200, ["a"])):
        assert git_utils.get_pr_labels("1", "o/r") == ["a"]
    buildkite_job.assert_not_called()


def test_get_pr_labels_ignores_missing_buildkite_agent(buildkite_job):
    buildkite_job.side_effect = FileNotFoundError
    responses = [_response(503), _response(200, ["a"])]
    with patch.object(git_utils.requests, "get", side_effect=responses), patch.object(
        git_utils.time, "sleep"
    ):
        assert git_utils.get_pr_labels("1", "o/r") == ["a"]
