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
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional, Tuple

STEPS_ENV = "RUNTIME_SHARD_COVERAGE_STEPS"
ARTIFACTS = ".runtime-shard-coverage/*/*.json"
# Buildkite refuses an annotation over 1 MiB; the log keeps the whole list.
MAX_IDS_SHOWN = 30


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
        test IDs absent from the expected inventory), `duplicated` (each test ID
        reported by more than one shard, to those shards) and `unverified` (why
        it can't be checked, or None). A shard that left no record makes its
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
                    else:
                        unknown_unfinished_plan = True
                elif isinstance(planned, list):
                    finished_planned.update(planned)
            # The expected list is written before any test runs, so a shard
            # that died still has it.
            first = latest.get((step_key, position, command["fallback"]))
            expected = first.get("expected") if first else None
            unexpected = sorted(ran - set(expected)) if expected is not None else []
            result = {
                "step": step_key,
                "label": command["label"],
                "soft_fail": bool(command.get("soft_fail", False)),
                "tests": len(expected or []),
                "missed": [],
                "unfinished": unfinished,
                "not_run": [],
                "unexpected": unexpected,
                "duplicated": {
                    nodeid: shards
                    for nodeid, shards in sorted(shards_of.items())
                    if len(shards) > 1
                },
                "unverified": None,
            }
            unverified = []
            if expected is None:
                unverified.append(
                    f"shard {command['fallback'] + 1} recorded no test list"
                )
            elif not expected:
                unverified.append("expected inventory is empty")
            if unexpected:
                unverified.append(
                    f"{len(unexpected)} reported tests absent from the expected inventory"
                )
            if missing:
                shards = ", ".join(str(shard + 1) for shard in missing)
                noun = "shard" if len(missing) == 1 else "shards"
                unverified.append(f"{noun} {shards} recorded nothing")
            if unverified:
                result["unverified"] = "; ".join(unverified)
            elif unfinished:
                for nodeid in sorted(set(expected) - ran):
                    if nodeid in finished_planned:
                        result["missed"].append(nodeid)
                    elif nodeid in unfinished_planned or unknown_unfinished_plan:
                        result["not_run"].append(nodeid)
                    else:
                        result["missed"].append(nodeid)
            else:
                result["missed"] = sorted(set(expected) - ran)
            results.append(result)
    return results


def _ids(ids: List[str], shards_of: Optional[Dict[str, List[int]]] = None) -> List[str]:
    lines = []
    for nodeid in ids[:MAX_IDS_SHOWN]:
        line = f"- `{nodeid}`"
        if shards_of:
            line += ": shards " + ", ".join(
                str(shard + 1) for shard in shards_of[nodeid]
            )
        lines.append(line)
    if len(ids) > MAX_IDS_SHOWN:
        lines.append(f"- and {len(ids) - MAX_IDS_SHOWN} more, listed in this job's log")
    return lines


def report(results: List[Dict[str, Any]]) -> Tuple[str, str]:
    """The annotation for a build's results.

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
    unfinished = [result for result in results if result["unfinished"]]
    unverified = [result for result in results if result["unverified"]]
    duplicated = [result for result in results if result["duplicated"]]
    lines = []
    if missed:
        count = sum(len(result["missed"]) for result in missed)
        if unfinished:
            lines.append(f"### :x: {count} tests ran in no finished shard")
        else:
            lines.append(
                f"### :x: {count} tests ran in no shard, though every shard finished"
            )
        for result in missed:
            lines.append(f"**{result['step']}**, {result['label']}:")
            lines.extend(_ids(result["missed"]))
    if unfinished:
        lines.append("### Shards that didn't finish, so these tests may not have run")
        for result in unfinished:
            shards = ", ".join(str(shard + 1) for shard in result["unfinished"])
            soft = " (soft-fail step)" if result["soft_fail"] else ""
            lines.append(
                f"**{result['step']}**, {result['label']}: shard {shards}{soft}"
            )
            lines.extend(_ids(result["not_run"]))
    if duplicated:
        # Costs a shard's time and can hide order-dependent tests, but loses none.
        lines.append("### Tests that ran in more than one shard")
        for result in duplicated:
            lines.append(f"**{result['step']}**, {result['label']}:")
            lines.extend(_ids(list(result["duplicated"]), result["duplicated"]))
    if unverified:
        lines.append("### Couldn't check")
        for result in unverified:
            soft = " (soft-fail step)" if result["soft_fail"] else ""
            lines.append(
                f"**{result['step']}**, {result['label']}: {result['unverified']}{soft}"
            )
            lines.extend(_ids(result.get("unexpected", [])))
    if lines:
        blocking = [
            result
            for result in results
            if (result["unverified"] or result["unfinished"])
            and not result.get("soft_failed", False)
        ]
        style = "error" if missed or blocking else "warning"
        return style, "\n".join(lines)
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
        except ValueError as error:
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


def _relax_soft_failed_gaps(results: List[Dict[str, Any]]) -> None:
    """Do not turn a step's existing soft failure into a hard coverage failure.

    A passed soft-fail step still has to prove its coverage. Only a terminal
    soft_failed, hard_failed, errored or canceled outcome may relax its gaps.
    A passed, neutral or unavailable outcome keeps the hard failure.
    """
    outcomes = {}
    for result in results:
        if not result["soft_fail"] or not (
            result["unverified"] or result["unfinished"]
        ):
            continue
        step = result["step"]
        if step not in outcomes:
            try:
                run = subprocess.run(
                    ["buildkite-agent", "step", "get", "outcome", "--step", step],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                outcomes[step] = run.returncode == 0 and run.stdout.strip() in (
                    "soft_failed",
                    "hard_failed",
                    "errored",
                    "canceled",
                )
            except (OSError, subprocess.TimeoutExpired):
                outcomes[step] = False
        result["soft_failed"] = outcomes[step]


def main() -> int:
    steps = json.loads(os.environ[STEPS_ENV])
    with tempfile.TemporaryDirectory() as folder:
        # Fails when no job uploaded anything; the comparison then says so.
        # Every attempt, not only the latest, so latest_records() can pick the
        # latest that finished. An env var, not the flag: an agent too old for
        # it ignores the variable, where an unknown flag fails the download.
        subprocess.run(
            ["buildkite-agent", "artifact", "download", ARTIFACTS, folder],
            check=False,
            env=dict(os.environ, BUILDKITE_AGENT_INCLUDE_RETRIED_JOBS="true"),
        )
        records = _read_records(folder)
    # Without the variable (a local run) every record counts, as before.
    results = compare(steps, records, build_id=os.environ.get("BUILDKITE_BUILD_ID")) if records else []
    if records:
        _relax_soft_failed_gaps(results)
        style, text = report(results)
    else:
        style = "error"
        text = (
            "No coverage records were uploaded; coverage could not be checked. "
            "The shard jobs may not have run."
        )
    for result in results:
        for name in ("missed", "not_run", "unexpected"):
            for nodeid in result[name]:
                print(f"{name}: {result['step']}: {nodeid}")
        for nodeid, shards in result["duplicated"].items():
            numbers = ", ".join(str(shard + 1) for shard in shards)
            print(f"duplicated: {result['step']}: {nodeid}: shards {numbers}")
    if style == "error":
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
            # Use the job's own key so a retried or separately uploaded check
            # cannot overwrite another check's annotation.
            os.getenv("BUILDKITE_STEP_KEY") or "runtime-shard-coverage",
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
