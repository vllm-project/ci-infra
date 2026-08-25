"""Bounded Buildkite step polling for the fleet snapshot publisher."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable


STEP_STATES = {
    "ignored",
    "waiting_for_dependencies",
    "ready",
    "running",
    "failing",
    "finished",
    "canceled",
}
STEP_OUTCOMES = {
    "neutral",
    "passed",
    "soft_failed",
    "hard_failed",
    "errored",
}
TERMINAL_STEP_STATES = {"finished", "ignored", "canceled"}

# A terminal state the step lookup rejects is strictly worse than an absent
# one — it appears to handle a case it can never reach.
assert TERMINAL_STEP_STATES <= STEP_STATES


class StepLookupError(RuntimeError):
    """Raised when the Buildkite Agent API cannot return a valid step."""


def query_step(key: str) -> tuple[str, str | None]:
    """Return Buildkite's aggregate state/outcome for one command-step key."""

    result = subprocess.run(
        [
            "buildkite-agent",
            "step",
            "get",
            "--format",
            "json",
            "--step",
            key,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise StepLookupError(f"Buildkite step lookup failed for {key}")
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise StepLookupError(
            f"Buildkite step lookup returned invalid JSON for {key}"
        ) from error
    if not isinstance(document, dict):
        raise StepLookupError(f"Buildkite step lookup returned a non-object for {key}")
    state = document.get("state")
    outcome = document.get("outcome")
    if state not in STEP_STATES:
        raise StepLookupError(
            f"Buildkite step lookup returned an invalid state for {key}: {state!r}"
        )
    if outcome is not None and outcome not in STEP_OUTCOMES:
        raise StepLookupError(
            f"Buildkite step lookup returned an invalid outcome for {key}: "
            f"state={state!r} outcome={outcome!r}"
        )
    if state == "canceled" and outcome not in (None, "neutral"):
        raise StepLookupError(
            f"Buildkite step lookup returned an invalid canceled outcome for {key}: "
            f"{outcome!r}"
        )
    return state, outcome


def _inventory_keys(document: dict[str, Any]) -> list[str]:
    jobs = document.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("trace inventory must contain jobs")
    keys: list[str] = []
    for row in jobs:
        if not isinstance(row, dict) or not isinstance(row.get("key"), str):
            raise ValueError("trace inventory job key is invalid")
        key = row["key"]
        if not key or key in keys:
            raise ValueError("trace inventory job keys must be nonempty and unique")
        keys.append(key)
    return sorted(keys)


def _write_json(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def wait_for_steps(
    inventory_path: Path,
    *,
    timeout_seconds: float,
    poll_seconds: float,
    query: Callable[[str], tuple[str, str | None]] = query_step,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Wait for stable terminal step states, then record a fail-closed cutoff.

    A terminal state must be returned by two consecutive polls. This re-waits
    when Buildkite moves a step back to ready/running for an automatic retry.
    At the hard deadline, every remaining key is explicitly marked
    ``poll_timeout`` so the materializer excludes even late-arriving artifacts.
    Lookup failures are retried and become bounded missing evidence rather than
    silently suppressing snapshot publication.
    """

    if timeout_seconds < 0:
        raise ValueError("step wait timeout must be nonnegative")
    if poll_seconds <= 0:
        raise ValueError("step wait poll interval must be positive")
    document = json.loads(inventory_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("trace inventory must be an object")
    keys = _inventory_keys(document)
    pending = set(keys)
    observations: dict[str, dict[str, Any]] = {
        key: {"outcome": None, "state": None, "lookup_error": False} for key in keys
    }
    terminal_candidates: dict[str, tuple[str, str]] = {}
    wait_results: dict[str, dict[str, Any]] = {}
    deadline = monotonic() + timeout_seconds

    while pending:
        for key in sorted(pending):
            try:
                state, outcome = query(key)
            except StepLookupError as error:
                print(f"Waiting for trace step {key}: {error}")
                observations[key]["lookup_error"] = True
                terminal_candidates.pop(key, None)
                continue
            observations[key] = {
                "outcome": outcome,
                "state": state,
                "lookup_error": False,
            }
            print(f"Trace step {key}: state={state} outcome={outcome}")
            if state in TERMINAL_STEP_STATES and outcome in STEP_OUTCOMES:
                candidate = (state, outcome)
                if terminal_candidates.get(key) == candidate:
                    wait_results[key] = {
                        "outcome": outcome,
                        "state": state,
                        "status": "terminal",
                    }
                    pending.remove(key)
                else:
                    terminal_candidates[key] = candidate
            else:
                terminal_candidates.pop(key, None)
        if not pending:
            break
        remaining = deadline - monotonic()
        if remaining <= 0:
            break
        sleep(min(poll_seconds, remaining))

    for key in sorted(pending):
        observation = observations[key]
        wait_results[key] = {
            "lookup_error": observation["lookup_error"],
            "outcome": observation["outcome"],
            "state": observation["state"],
            "status": "poll_timeout",
        }
        print(
            "Trace step %s reached the bounded poll cutoff: state=%s outcome=%s "
            "lookup_error=%s"
            % (
                key,
                observation["state"],
                observation["outcome"],
                observation["lookup_error"],
            )
        )

    document["wait_results"] = wait_results
    _write_json(inventory_path, document)
    timed_out = sorted(pending)
    return {
        "poll_timeout": timed_out,
        "terminal": sorted(set(keys) - set(timed_out)),
        "wait_results": wait_results,
    }
