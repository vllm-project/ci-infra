#!/usr/bin/env python3
"""The build's check that no test dropped out of its shards.

Each shard of a runtime-sharded step records what it ran
(runtime_shard_plugin.py), and the first shard that runs a command also records
every test the command should run. This job compares them and posts one
annotation, failing closed: red whenever coverage is not proven. That is a
miss (a test no shard ran although every shard finished), a shard that
didn't finish (its tests may not have run, so they are listed apart from
misses), and a command it cannot check (a shard left no record, or none
recorded the test list). Yellow only for a test that two finished shards both
ran: it costs a shard's time but loses no test. Standard library only: it runs
on a small CPU agent, fetched as a single file.
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
    steps: Dict[str, List[Dict[str, Any]]], records: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Compare what each sharded command should run with what its shards ran.

    Args:
        steps: The step key to its planned commands, from the generator.
        records: Every record the build's shards uploaded.

    Returns:
        One entry per command: the step, label, `tests` (how many it should
        run), `missed` (test IDs), `unfinished` (shards that recorded they
        didn't finish), `not_run` (test IDs lost to them), `duplicated` (each
        test ID that more than one finished shard ran, to those shards) and
        `unverified` (why it can't be checked, or None). A shard that left no
        record makes its command unverified: which tests it owned can't be
        known, so they are neither missed nor not_run.
    """
    latest = latest_records(records)
    results = []
    for step_key, commands in steps.items():
        for command in commands:
            position = command["position"]
            ran = set()
            unfinished = []
            missing = []  # shards that left no record at all
            shards_of = {}  # test ID -> the finished shards that ran it
            for shard in command["owners"]:
                record = latest.get((step_key, position, shard))
                if record is None:
                    missing.append(shard)
                elif not record["finished"]:
                    unfinished.append(shard)
                else:
                    ran.update(record["ran"])
                    for nodeid in set(record["ran"]):
                        shards_of.setdefault(nodeid, []).append(shard)
            # The expected list is written before any test runs, so a shard
            # that died still has it.
            first = latest.get((step_key, position, command["fallback"]))
            expected = first.get("expected") if first else None
            result = {
                "step": step_key,
                "label": command["label"],
                "tests": len(expected or []),
                "missed": [],
                "unfinished": unfinished,
                "not_run": [],
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
            if missing:
                shards = ", ".join(str(shard + 1) for shard in missing)
                noun = "shard" if len(missing) == 1 else "shards"
                unverified.append(f"{noun} {shards} recorded nothing")
            if unverified:
                result["unverified"] = "; ".join(unverified)
            else:
                not_ran = sorted(set(expected) - ran)
                result["not_run" if unfinished else "missed"] = not_ran
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
        warning.
    """
    tests = sum(result["tests"] for result in results)
    missed = [result for result in results if result["missed"]]
    unfinished = [result for result in results if result["unfinished"]]
    unverified = [result for result in results if result["unverified"]]
    duplicated = [result for result in results if result["duplicated"]]
    lines = []
    if missed:
        count = sum(len(result["missed"]) for result in missed)
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
            lines.append(f"**{result['step']}**, {result['label']}: shard {shards}")
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
            lines.append(
                f"**{result['step']}**, {result['label']}: {result['unverified']}"
            )
    if lines:
        style = "error" if missed or unfinished or unverified else "warning"
        return style, "\n".join(lines)
    steps = len({result["step"] for result in results})
    return "success", f"Every test ran: {tests} tests across {steps} sharded steps."


def _read_records(folder: str) -> List[Dict[str, Any]]:
    records = []
    pattern = os.path.join(folder, ARTIFACTS)
    for path in glob.glob(pattern, recursive=True):
        # A shard killed mid-write leaves one bad file; skipping it makes that
        # shard read as unfinished rather than stopping every step's check.
        try:
            with open(path) as f:
                records.append(json.load(f))
        except ValueError as error:
            print(f"runtime-shard coverage: skipped unreadable {path}: {error}")
    return records


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
    results = compare(steps, records)
    for result in results:
        for name in ("missed", "not_run"):
            for nodeid in result[name]:
                print(f"{name}: {result['step']}: {nodeid}")
        for nodeid, shards in result["duplicated"].items():
            numbers = ", ".join(str(shard + 1) for shard in shards)
            print(f"duplicated: {result['step']}: {nodeid}: shards {numbers}")
    style, text = report(results)
    print(text)
    subprocess.run(
        [
            "buildkite-agent",
            "annotate",
            "--context",
            # The job's own step key: the pipeline's job annotates under
            # "runtime-shard-coverage" as before, and a "Run more jobs"
            # round's job under its own key, next to it instead of over it.
            os.getenv("BUILDKITE_STEP_KEY") or "runtime-shard-coverage",
            "--style",
            style,
            text,
        ],
        check=False,
    )
    return 1 if style == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
