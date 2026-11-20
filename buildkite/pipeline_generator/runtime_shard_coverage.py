#!/usr/bin/env python3
"""The build's check that no test dropped out of its shards.

Each shard of a runtime-sharded step records what it ran
(runtime_shard_plugin.py), and the command's least-loaded owner also records
every test the command should run. This job compares them and posts one
annotation, failing closed: red whenever coverage is not proven. That is a
miss (a test no shard ran although every shard finished), a shard that
didn't finish (its tests may not have run, so they are listed apart from
misses), and a command it cannot check (a shard left no record, or none
recorded the test list). Yellow for a test that two finished shards both ran:
it costs a shard's time but loses no test. Unfinished shards and missing
evidence are yellow only when Buildkite reports that the step did not pass;
a passed soft-fail step with either gap stays red. A definite miss
stays red, though, since that means the sharding itself is broken. And only
the current build's records count: a record from another build (or one too
old to name its build) is stale evidence, ignored when the build's ID is
known. Standard library only: it runs on a small CPU agent, with its source
packed into the job environment at generation and decoded there.
"""

import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

STEPS_ENV = "RUNTIME_SHARD_COVERAGE_STEPS"
ARTIFACTS = ".runtime-shard-coverage/*/*.json"
# Buildkite refuses an annotation over 1 MiB; the log keeps the whole list.
MAX_IDS_INLINE = 5
MAX_FILES_SHOWN = 30
MAX_COMMAND_CHARS = 100


def latest_records(records: List[Dict[str, Any]]) -> Dict[Tuple, Dict[str, Any]]:
    """Each shard's record per command: its latest attempt that finished, or
    its latest attempt when none finished."""
    latest: Dict[Tuple, Dict[str, Any]] = {}
    for record in records:
        where = (record["step"], record["position"], record["shard"])
        rank = (record["finished"], record["retry"], record["finished_at"])
        best = latest.get(where)
        if best is None or rank > (
            best["finished"],
            best["retry"],
            best["finished_at"],
        ):
            latest[where] = record
    return latest


def _expected_from_attempts(records, step, position, shard):
    """The latest valid expected list from any owner attempt, or None."""
    candidates = [
        record
        for record in records
        if record["step"] == step
        and record["position"] == position
        and record["shard"] == shard
        and isinstance(record.get("expected"), list)
    ]
    if not candidates:
        return None
    valid = [record for record in candidates if record["expected"]]
    return max(valid or candidates, key=lambda record: (record["retry"], record["finished_at"]))[
        "expected"
    ]


def compare(
    steps: Dict[str, List[Dict[str, Any]]],
    records: List[Dict[str, Any]],
    build_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Compare what each sharded command should run with what its shards ran.

    Args:
        steps: The step key to its planned commands, from the generator.
        records: Every record the build's shards uploaded. Their planned IDs
            attribute missed tests to finished or unfinished shards when known.
        build_id: The current build's ID. When given, only records naming it
            count: a record from another build, or one old enough to carry no
            build ID, is stale evidence and ignored. When None, every record
            counts (how local runs and older callers behave).

    Returns:
        One entry per command: the step, label, `soft_fail` (whether the step
        may fail without failing the build), `tests` (how many it should
        run), `missed` (test IDs from a finished shard or with no possible
        unfinished owner), `unfinished` (shards that recorded they didn't
        finish), `not_run` (test IDs assigned to unfinished shards with no
        reported outcome, or whose ownership is unknown), `unexpected` (reported
        test IDs absent from the expected inventory), `retryable` (whether an
        unfinished shard or a missing shard alongside reported outcomes may
        produce new evidence on retry), `duplicated` (each test ID reported by
        more than one shard, to those shards) and `unverified` (why it can't be
        checked, or None). A shard that left no record makes its
        command unverified: which tests it owned can't be known, so they are
        neither missed nor not_run. An empty or inconsistent expected inventory
        is unverified too.
    """
    if build_id is not None:
        records = [record for record in records if record.get("build_id") == build_id]
    latest = latest_records(records)
    results = []
    for step_key, commands in steps.items():
        for command in commands:
            position = command["position"]
            ran = set()
            unfinished = []
            retryable_unfinished = []
            missing = []  # shards that left no record at all
            finished_planned = set()
            unfinished_planned = set()
            unknown_unfinished_plan = False
            shards_of = {}  # test ID -> the shards that reported it
            for shard in command["owners"]:
                record = latest.get((step_key, position, shard))
                if record is None:
                    missing.append(shard)
                    continue
                planned = record.get("planned")
                ran.update(record["ran"])
                for nodeid in set(record["ran"]):
                    shards_of.setdefault(nodeid, []).append(shard)
                if not record["finished"]:
                    unfinished.append(shard)
                    if isinstance(planned, list):
                        unfinished_planned.update(planned)
                        retryable_unfinished.append(shard)
                    else:
                        unknown_unfinished_plan = True
                elif isinstance(planned, list):
                    finished_planned.update(planned)
            expected = _expected_from_attempts(
                records, step_key, position, command["fallback"]
            )
            unexpected = sorted(ran - set(expected)) if expected is not None else []
            result = {
                "step": step_key,
                "label": command["label"],
                "command": command.get("command"),
                "soft_fail": bool(command.get("soft_fail", False)),
                "tests": len(expected or []),
                "missed": [],
                "unfinished": unfinished,
                "not_run": [],
                "unexpected": unexpected,
                "missing_shards": missing,
                "retryable": bool(retryable_unfinished or (missing and ran)),
                "duplicated": {
                    nodeid: shards
                    for nodeid, shards in sorted(shards_of.items())
                    if len(shards) > 1
                },
                "unverified": None,
            }
            # Reasons name the problem only, so commands with the same problem
            # group together; which shards are involved goes in the detail.
            unverified = []
            details = []
            if expected is None:
                unverified.append("no full test list to compare against")
                if command["fallback"] not in missing:
                    details.append(f"shard {command['fallback'] + 1} saved no test list")
            elif not expected:
                unverified.append("the full test list is empty")
            if unexpected:
                unverified.append(
                    f"{len(unexpected)} tests ran that aren't in the full test list"
                )
            # A shard collected these, so they exist; the inventory missed them.
            # Reported ones are already counted as unexpected above.
            planned_absent = (
                (finished_planned | unfinished_planned) - set(expected) - ran
                if expected is not None
                else set()
            )
            if planned_absent:
                unverified.append(
                    f"{len(planned_absent)} tests were planned but aren't in the full test list"
                )
            if missing:
                shards = ", ".join(str(shard + 1) for shard in missing)
                noun = "shard" if len(missing) == 1 else "shards"
                unverified.append("no record of which tests ran")
                details.append(f"no record from {noun} {shards}")
            if unverified:
                result["unverified"] = "; ".join(unverified)
                result["unverified_detail"] = " · ".join(details) or None
            if expected and not missing:
                for nodeid in sorted(set(expected) - ran):
                    if nodeid in finished_planned:
                        result["missed"].append(nodeid)
                    elif unfinished and (
                        nodeid in unfinished_planned or unknown_unfinished_plan
                    ):
                        result["not_run"].append(nodeid)
                    else:
                        result["missed"].append(nodeid)
                # Planned in a finished shard but absent from a short inventory.
                result["missed"] = sorted(
                    set(result["missed"]) | (finished_planned - ran - set(expected))
                )
            else:
                result["missed"] = sorted(finished_planned - ran)
            results.append(result)
    return results


def _ids(ids: List[str], shards_of: Optional[Dict[str, List[int]]] = None) -> List[str]:
    """A short list of test IDs as bullets; a long one folded into a
    <details> block with a count per test file. The job log has every ID."""
    if len(ids) <= MAX_IDS_INLINE:
        lines = []
        for nodeid in ids:
            line = f"- `{nodeid}`"
            if shards_of:
                line += ": shards " + ", ".join(
                    str(shard + 1) for shard in shards_of[nodeid]
                )
            lines.append(line)
        return lines
    per_file: Dict[str, int] = {}
    for nodeid in ids:
        file = nodeid.split("::")[0]
        per_file[file] = per_file.get(file, 0) + 1
    # IDs arrive sorted, so equal counts keep file-name order (a stable sort).
    files = sorted(per_file.items(), key=lambda item: -item[1])
    lines = [
        f"<details><summary>{len(ids)} tests in {len(files)} "
        f"file{'s' if len(files) != 1 else ''}</summary>",
        "",
    ]
    for file, count in files[:MAX_FILES_SHOWN]:
        lines.append(f"- `{file}`: {count}")
    if len(files) > MAX_FILES_SHOWN:
        lines.append(f"- and {len(files) - MAX_FILES_SHOWN} more files")
    lines += ["", "Every test ID is in this job's log.", "</details>"]
    return lines


def _command_name(result: Dict[str, Any]) -> str:
    """The command as the job log shows it: "`pytest -v -s v1/core`
    (command 1 of 9)". Without its text (an older generator), the label."""
    match = re.fullmatch(r"Command \((\d+)/(\d+)\)", result["label"])
    where = f"command {match.group(1)} of {match.group(2)}" if match else result["label"]
    text = result.get("command")
    if not text:
        return where
    if len(text) > MAX_COMMAND_CHARS:
        text = text[: MAX_COMMAND_CHARS - 1] + "…"
    return f"`{text}` ({where})"


def _step_heading(result: Dict[str, Any]) -> str:
    soft = " (soft-fail step)" if result["soft_fail"] else ""
    return f"**{result['step']}**{soft}"


def report(results: List[Dict[str, Any]]) -> Tuple[str, str]:
    """The annotation for a build's results.

    Each step's findings are their own Markdown block, separated by blank
    lines, so they stay apart on a narrow screen.

    Returns:
        (its style: error, warning or success; its markdown). Error unless
        every test is proven run: a miss, an unfinished shard and a command
        that can't be checked all fail the job; only duplicates stay a
        warning. A soft-fail step is the exception for an unfinished shard or
        unverified evidence after an allowed terminal failure outcome: its
        build already allows that failure. Either gap after a passed, neutral
        or unavailable outcome still fails, as does a definite miss.
    """
    tests = sum(result["tests"] for result in results)
    missed = [result for result in results if result["missed"]]
    # An unfinished command with no test to name is left to "Couldn't check".
    unfinished = [
        result
        for result in results
        if result["unfinished"] and (result["not_run"] or not result["unverified"])
    ]
    unverified = [result for result in results if result["unverified"]]
    duplicated = [result for result in results if result["duplicated"]]
    blocks = []
    for result in missed:
        count = len(result["missed"])
        if result["unfinished"] or result["unverified"]:
            title = f"### :x: {count} tests ran in no finished shard"
        else:
            title = f"### :x: {count} tests ran in no shard, though every shard finished"
        blocks.append(title)
        blocks.append(
            "\n".join(
                [f"{_step_heading(result)} · {_command_name(result)}"] + _ids(result["missed"])
            )
        )
    if unfinished:
        blocks.append("### Shards that didn't finish, so these tests may not have run")
        for result in unfinished:
            shards = ", ".join(str(shard + 1) for shard in result["unfinished"])
            blocks.append(
                "\n".join(
                    [f"{_step_heading(result)} · {_command_name(result)} · shard {shards}"]
                    + _ids(result["not_run"])
                )
            )
    if duplicated:
        # Costs a shard's time and can hide order-dependent tests, but loses none.
        blocks.append("### Tests that ran in more than one shard")
        for result in duplicated:
            blocks.append(
                "\n".join(
                    [f"{_step_heading(result)} · {_command_name(result)}"]
                    + _ids(list(result["duplicated"]), result["duplicated"])
                )
            )
    if unverified:
        blocks.append("### Couldn't check")
        # One block per step; commands with the same reason share a line.
        by_step: Dict[str, List[Dict[str, Any]]] = {}
        for result in unverified:
            by_step.setdefault(result["step"], []).append(result)
        for step_results in by_step.values():
            reasons: Dict[str, List[str]] = {}
            for result in step_results:
                name = _command_name(result)
                if result.get("unverified_detail"):
                    name += f" · {result['unverified_detail']}"
                reasons.setdefault(result["unverified"], []).append(name)
            lines = [_step_heading(step_results[0])]
            for reason, names in reasons.items():
                lines.append(f"- {reason[0].upper()}{reason[1:]}:")
                lines.extend(f"  - {name}" for name in names)
            blocks.append("\n".join(lines))
            for result in step_results:
                if result.get("unexpected"):
                    blocks.append(
                        "\n".join(
                            [f"Ran but not in the full test list, {_command_name(result)}:"]
                            + _ids(result["unexpected"])
                        )
                    )
    if blocks:
        blocking = [
            result
            for result in results
            if (result["unverified"] or result["unfinished"])
            and not result.get("soft_failed", False)
        ]
        style = "error" if missed or blocking else "warning"
        return style, "\n\n".join(blocks)
    steps = len({result["step"] for result in results})
    return "success", f"Every test ran: {tests} tests across {steps} sharded steps."


def _read_records(folder: str) -> List[Dict[str, Any]]:
    records = []
    required = {
        "step": str,
        "position": int,
        "shard": int,
        "retry": int,
        "finished": bool,
        "finished_at": (int, float),
        "ran": list,
    }
    pattern = os.path.join(folder, ARTIFACTS)
    for path in glob.glob(pattern, recursive=True):
        # A shard killed mid-write leaves one bad file; skipping it makes that
        # shard's coverage unverified rather than stopping every step's check.
        try:
            with open(path) as f:
                record = json.load(f)
        except (OSError, ValueError) as error:
            print(f"runtime-shard coverage: skipped unreadable {path}: {error}")
            continue
        valid = isinstance(record, dict) and all(
            isinstance(record.get(key), kind) for key, kind in required.items()
        )
        if valid:
            for key in ("expected", "planned", "ran"):
                value = record.get(key)
                if value is not None and (
                    not isinstance(value, list)
                    or any(not isinstance(nodeid, str) for nodeid in value)
                ):
                    valid = False
                    break
        if not valid:
            print(f"runtime-shard coverage: skipped malformed {path}")
            continue
        records.append(record)
    return records


def _download_records(folder: str) -> Optional[str]:
    """Download shard records, retrying failures but not an empty artifact set.

    Args:
        folder: The directory in which Buildkite should place the records.

    Returns:
        A download error to annotate, or None on success or no matching records.
    """
    details = []
    for attempt in range(3):
        time.sleep(10 * attempt)
        try:
            run = subprocess.run(
                ["buildkite-agent", "artifact", "download", ARTIFACTS, folder],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
                env=dict(os.environ, BUILDKITE_AGENT_INCLUDE_RETRIED_JOBS="true"),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            details.append(type(error).__name__)
            continue
        returncode = run.returncode
        if not returncode:
            return None
        output = run.stdout + run.stderr
        if "no artifacts found for downloading" in output:
            return None
        details.append(f"exit {returncode}")
    return (
        f"Coverage artifact download failed after 3 attempts ({details[-1]}); "
        "retry this coverage job."
    )


def _step_property(name: str, step: str) -> Optional[str]:
    """Read a Buildkite step property.

    Args:
        name: The property to read, such as state or outcome.
        step: The step's key.

    Returns:
        The property value, or None when the agent cannot supply it.
    """
    try:
        run = subprocess.run(
            ["buildkite-agent", "step", "get", name, "--step", step],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if not run or run.returncode:
        return None
    return run.stdout.strip()


def _relax_soft_failed_gaps(results: List[Dict[str, Any]]) -> None:
    """Do not turn a step's existing soft failure into a hard coverage failure.

    A passed soft-fail step still has to prove its coverage. Only a canceled
    step, or a terminal state with a soft_failed, hard_failed, errored or
    canceled outcome, may relax its gaps. A pending step, or a finished one
    with a passed, neutral or unavailable outcome, stays hard.
    """
    outcomes = {}
    for result in results:
        if not result["soft_fail"] or not (
            result["unverified"] or result["unfinished"]
        ):
            continue
        step = result["step"]
        if step not in outcomes:
            state = _step_property("state", step)
            outcome = _step_property("outcome", step)
            print(f"soft-fail step {step}: state={state} outcome={outcome}")
            terminal = state in (
                "finished",
                "ignored",
                "canceled",
                "waiting_failed",
            )
            # Buildkite reports a soft-fail step with a canceled shard as
            # state canceled, outcome neutral: the state is the failure.
            outcomes[step] = state == "canceled" or (
                terminal
                and outcome in ("soft_failed", "hard_failed", "errored", "canceled")
            )
        result["soft_failed"] = outcomes[step]


def main() -> int:
    steps = json.loads(os.environ[STEPS_ENV])
    with tempfile.TemporaryDirectory() as folder:
        # Every attempt, not only the latest, so latest_records() can pick the
        # latest that finished. An env var, not the flag: an agent too old for
        # it ignores the variable, where an unknown flag fails the download.
        download_error = _download_records(folder)
        records = _read_records(folder)
    if download_error:
        results = []
        style, text = "error", download_error
    else:
        # Without the variable (a local run) every record counts, as before.
        results = compare(
            steps, records, build_id=os.environ.get("BUILDKITE_BUILD_ID")
        )
        _relax_soft_failed_gaps(results)
        style, text = report(results)
    for result in results:
        for name in ("missed", "not_run", "unexpected"):
            for nodeid in result[name]:
                print(f"{name}: {result['step']}: {nodeid}")
        for nodeid, shards in result["duplicated"].items():
            numbers = ", ".join(str(shard + 1) for shard in shards)
            print(f"duplicated: {result['step']}: {nodeid}: shards {numbers}")
    if style == "error" and any(
        result["retryable"] and not result.get("soft_failed", False)
        for result in results
    ):
        text += (
            "\n\nAfter the retried shard finishes, retry this coverage job too "
            "so it compares the new records."
        )
    print(text)
    subprocess.run(
        [
            "buildkite-agent",
            "annotate",
            "--context",
            # A separately uploaded coverage job gets its own context; retrying
            # the same job replaces its stale report.
            os.getenv("BUILDKITE_STEP_KEY") or "runtime-shard-coverage-check",
            "--style",
            style,
        ],
        input=text,
        text=True,
        check=False,
    )
    return 1 if style == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
