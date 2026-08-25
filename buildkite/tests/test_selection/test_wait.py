import json
import subprocess
from pathlib import Path

import pytest
from test_selection.wait import StepLookupError, query_step, wait_for_steps


def _inventory(path: Path, *keys: str) -> None:
    path.write_text(
        json.dumps(
            {
                "jobs": [
                    {"expected_shards": 1, "key": key, "mode": "python-only"}
                    for key in keys
                ],
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.value += seconds


def test_wait_rechecks_retry_transition_and_lookup_error(tmp_path: Path):
    inventory = tmp_path / "inventory.json"
    _inventory(inventory, "retry", "canceled", "expired")
    clock = Clock()
    responses = {
        "retry": iter(
            [
                ("running", None),
                ("finished", "hard_failed"),
                ("ready", None),
                ("finished", "passed"),
                ("finished", "passed"),
            ]
        ),
        "canceled": iter(
            [
                StepLookupError("temporary Agent API failure"),
                ("canceled", "neutral"),
                ("canceled", "neutral"),
            ]
        ),
        "expired": iter(
            [
                ("finished", "errored"),
                ("finished", "errored"),
            ]
        ),
    }

    def query(key: str):
        response = next(responses[key])
        if isinstance(response, Exception):
            raise response
        return response

    result = wait_for_steps(
        inventory,
        timeout_seconds=10,
        poll_seconds=1,
        query=query,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result["terminal"] == ["canceled", "expired", "retry"]
    assert result["poll_timeout"] == []
    assert result["wait_results"] == {
        "canceled": {
            "outcome": "neutral",
            "state": "canceled",
            "status": "terminal",
        },
        "expired": {
            "outcome": "errored",
            "state": "finished",
            "status": "terminal",
        },
        "retry": {
            "outcome": "passed",
            "state": "finished",
            "status": "terminal",
        },
    }
    assert json.loads(inventory.read_text())["wait_results"] == result["wait_results"]


def test_wait_timeout_accounts_nonterminal_and_lookup_failure(tmp_path: Path):
    inventory = tmp_path / "inventory.json"
    _inventory(inventory, "healthy", "slow", "unknown")
    clock = Clock()

    def query(key: str):
        if key == "healthy":
            return "finished", "passed"
        if key == "slow":
            return "running", None
        raise StepLookupError("unknown key or Agent API unavailable")

    result = wait_for_steps(
        inventory,
        timeout_seconds=2,
        poll_seconds=1,
        query=query,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result["terminal"] == ["healthy"]
    assert result["poll_timeout"] == ["slow", "unknown"]
    assert result["wait_results"]["slow"] == {
        "lookup_error": False,
        "outcome": None,
        "state": "running",
        "status": "poll_timeout",
    }
    assert result["wait_results"]["unknown"] == {
        "lookup_error": True,
        "outcome": None,
        "state": None,
        "status": "poll_timeout",
    }


def test_query_step_rejects_invalid_agent_response(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"state":"mystery"}', stderr=""
        ),
    )

    with pytest.raises(StepLookupError, match="invalid state.*'mystery'"):
        query_step("unit")


def test_query_step_reports_invalid_outcome_with_state(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"state":"finished","outcome":"mystery"}',
            stderr="",
        ),
    )

    with pytest.raises(
        StepLookupError,
        match="invalid outcome.*state='finished' outcome='mystery'",
    ):
        query_step("unit")


def test_query_step_rejects_non_neutral_canceled_outcome(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"state":"canceled","outcome":"errored"}',
            stderr="",
        ),
    )

    with pytest.raises(StepLookupError, match="invalid canceled outcome.*'errored'"):
        query_step("unit")


def test_terminal_states_are_reachable_through_the_lookup():
    # A terminal state that query_step rejects is dead coverage that reads
    # as handling. (An import-time assert would silently strip under -O.)
    from test_selection.wait import STEP_STATES, TERMINAL_STEP_STATES

    assert TERMINAL_STEP_STATES <= STEP_STATES
