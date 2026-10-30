#!/usr/bin/env python3
"""The build's check that no test dropped out of its shards.

Each shard of a runtime-sharded step records what it ran
(runtime_shard_plugin.py), and the first shard that runs a command also records
every test the command should run. This job compares them and posts one
annotation: red for a miss, a test that no shard ran although every shard
finished. Tests of a shard that didn't finish are listed apart, because the
build is already red for that. Standard library only: it runs on a small CPU
agent, fetched as a single file.
"""

import glob
import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Tuple

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
        run), `missed` (test IDs), `unfinished` (shards that didn't finish),
        `not_run` (test IDs lost to them) and `unverified` (why it can't be
        checked, or None).
    """
    latest = latest_records(records)
    results = []
    for step_key, commands in steps.items():
        for command in commands:
            position = command["position"]
            ran = set()
            unfinished = []
            for shard in command["owners"]:
                record = latest.get((step_key, position, shard))
                if record is None or not record["finished"]:
                    unfinished.append(shard)
                else:
                    ran.update(record["ran"])
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
                "unverified": None,
            }
            if expected is None:
                result["unverified"] = (
                    f"shard {command['fallback'] + 1} recorded no test list"
                )
            else:
                not_ran = sorted(set(expected) - ran)
                result["not_run" if unfinished else "missed"] = not_ran
            results.append(result)
    return results


def _ids(ids: List[str]) -> List[str]:
    lines = [f"- `{nodeid}`" for nodeid in ids[:MAX_IDS_SHOWN]]
    if len(ids) > MAX_IDS_SHOWN:
        lines.append(f"- and {len(ids) - MAX_IDS_SHOWN} more, listed in this job's log")
    return lines


def report(results: List[Dict[str, Any]]) -> Tuple[str, str]:
    """The annotation for a build's results.

    Returns:
        (its style: error, warning or success; its markdown).
    """
    tests = sum(result["tests"] for result in results)
    missed = [result for result in results if result["missed"]]
    unfinished = [result for result in results if result["unfinished"]]
    unverified = [result for result in results if result["unverified"]]
    lines = []
    if missed:
        count = sum(len(result["missed"]) for result in missed)
        lines.append(f"### :x: {count} tests ran in no shard, though every shard finished")
        for result in missed:
            lines.append(f"**{result['step']}**, {result['label']}:")
            lines.extend(_ids(result["missed"]))
    if unfinished:
        lines.append("### Shards that didn't finish, so these tests may not have run")
        for result in unfinished:
            shards = ", ".join(str(shard + 1) for shard in result["unfinished"])
            lines.append(f"**{result['step']}**, {result['label']}: shard {shards}")
            lines.extend(_ids(result["not_run"]))
    if unverified:
        lines.append("### Couldn't check")
        for result in unverified:
            lines.append(f"**{result['step']}**, {result['label']}: {result['unverified']}")
    if lines:
        style = "error" if missed else "warning"
        return style, "\n".join(lines)
    steps = len({result["step"] for result in results})
    return "success", f"Every test ran: {tests} tests across {steps} sharded steps."


def _read_records(folder: str) -> List[Dict[str, Any]]:
    records = []
    pattern = os.path.join(folder, ARTIFACTS)
    for path in glob.glob(pattern, recursive=True):
        with open(path) as f:
            records.append(json.load(f))
    return records


def main() -> int:
    steps = json.loads(os.environ[STEPS_ENV])
    with tempfile.TemporaryDirectory() as folder:
        # Fails when no job uploaded anything; the comparison then says so.
        subprocess.run(
            ["buildkite-agent", "artifact", "download", ARTIFACTS, folder], check=False
        )
        records = _read_records(folder)
    results = compare(steps, records)
    for result in results:
        for name in ("missed", "not_run"):
            for nodeid in result[name]:
                print(f"{name}: {result['step']}: {nodeid}")
    style, text = report(results)
    print(text)
    subprocess.run(
        [
            "buildkite-agent",
            "annotate",
            "--context",
            "runtime-shard-coverage",
            "--style",
            style,
            text,
        ],
        check=False,
    )
    return 1 if style == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
