"""Runtime sharding for steps enrolled with `automatic_shard: true`.

One stdlib-only file (Python 3.9: the small CPU queue's python3), because the
collect and plan steps fetch it with curl from the ci-infra branch in use,
like the recorders' scripts:

  collect  runs in the step's own test image after its setup commands. It
           imports PR code, so it only writes the collected node IDs of one
           pytest command to the checkout, for the agent to upload.
  plan     runs on a CPU agent. It reads those lists and main's timings for
           the step, packs whole files into contiguous shards, checks every
           test lands exactly once and annotates the build. Then it uploads
           the step's own job with `parallelism: N`; in shadow mode it uploads
           nothing, because the step already runs as one job.
  shards   each parallel job runs one pytest command per file of its shard,
           picked by BUILDKITE_PARALLEL_JOB, each under its own log header.
  plugin   `-p runtime_shard` in each shard job: fails a file's command if a
           test the plan gave it does not run there.

Sharding never blocks the step: if collection or planning fails, the plan step
uploads the step's normal single job instead (see run_plan and the generator's
shell fallback).
"""

import base64
import json
import math
import os
import re
import shlex
import subprocess
import sys
import urllib.parse
import urllib.request
import zlib
from typing import Dict, List, Optional, Tuple

TIMINGS_URL = "https://ci.vllm.ai/api/timings/latest"
# Test time per shard; setup is not counted. Planned below the 20-minute limit
# so run-to-run noise rarely pushes a real shard over it.
MAX_SHARD_SECONDS = 1080
UNKNOWN_FILE_SECONDS = 150  # a file main has no timing for
MAX_NUMBER_OF_SHARDS = 6
NO_TIMING_SHARDS = 4
INVENTORY_DIR = ".runtime-shard"
# pytest-shard would select a subset a second time on top of the plan.
_UNSHARDABLE_ARGS = ("--num-shards", "--shard-id")
_SHELL_SYNTAX = ("&&", "||", ";", "|", ">", "<", "`", "$", "\n")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# The section header the generator echoes before each command. The rendered
# step has it in double quotes; the preview in it has no quotes of its own.
_COMMAND_HEADER = re.compile(
    r"""echo (['"])\+\+\+ :test_tube: Command \((\d+/\d+)\): (.*)\1"""
)


def command_preview(command: str) -> str:
    """The label main's timing spans carry for a command.

    It is the same preview the generator echoes before running the command
    (see _prepare_commands), so it matches timings back to commands.

    Args:
        command: A pytest command of the step, as written in its YAML.

    Returns:
        The first 80 characters, with quotes and `$` removed.

    """
    return command[:80].replace("'", "").replace('"', "").replace("$", "")


def env_and_words(command: str) -> Tuple[Dict[str, str], List[str]]:
    """Split a command into its leading NAME=value assignments and the rest.

    Args:
        command: A shell command, like `TP_SIZE=1 pytest -v x.py`.

    Returns:
        ({"TP_SIZE": "1"}, ["pytest", "-v", "x.py"]).

    Raises:
        ValueError: The command doesn't parse as shell words.

    """
    words = shlex.split(command, comments=True)
    env = {}
    while words and _ASSIGNMENT.match(words[0]):
        name, value = words.pop(0).split("=", 1)
        env[name] = value
    return env, words


def _program(command: str) -> Optional[str]:
    """The program a command runs, after its NAME=value assignments."""
    try:
        _, words = env_and_words(command)
    except ValueError:  # e.g. a multi-line script
        return None
    return words[0] if words else None


def split_commands(commands: List[str]) -> Optional[Tuple[List[str], List[str]]]:
    """Separate a step's setup commands from its pytest commands.

    A pytest command is one whose program is pytest, after any NAME=value
    assignments: `TP_SIZE=1 pytest x.py` is one, `pip install pytest-timeout`
    is not.

    Args:
        commands: The step's commands, in order.

    Returns:
        (setup commands, pytest commands), or None if the step can't be
        sharded: it needs at least one pytest command, only plain pytest
        commands (no shell syntax, no pytest-shard flags) after the first,
        and no torchrun before it, since setup runs in full in every shard.

    """
    first = None
    for position, command in enumerate(commands):
        if _program(command) == "pytest":
            first = position
            break
    if first is None:
        return None
    if any(_program(c) == "torchrun" for c in commands[:first]):
        return None
    tests = commands[first:]
    for command in tests:
        if _program(command) != "pytest" or any(s in command for s in _SHELL_SYNTAX):
            return None
        if any(
            a.split("=")[0] in _UNSHARDABLE_ARGS
            for a in shlex.split(command, comments=True)
        ):
            return None
    return commands[:first], tests


def encode(value) -> str:
    return base64.b64encode(json.dumps(value).encode()).decode()


def decode(text: str):
    return json.loads(base64.b64decode(text))


def pack(nodeids: List[str]) -> str:
    """encode(), compressed: every file's tests go into a shard job's script."""
    return base64.b64encode(zlib.compress(json.dumps(nodeids).encode())).decode()


def unpack(text: str) -> List[str]:
    return json.loads(zlib.decompress(base64.b64decode(text)))


def contiguous(
    unit_seconds: List[float], max_shard_seconds: float, max_number_of_shards: int
) -> List[List[int]]:
    """Group consecutive units into shards.

    Uses the fewest shards that keep each one within max_shard_seconds (but no
    more than max_number_of_shards), then evens out the load across that many shards.

    Args:
        unit_seconds: Estimated test time of each unit, in collection order.
        max_shard_seconds: Test-time budget for one shard.
        max_number_of_shards: Upper bound on the number of shards.

    Returns:
        One list of unit indexes per shard, in order.

    """

    def fill(max_seconds: float) -> List[List[int]]:
        """Fill shards in order, starting a new one when the next unit would
        take the current one past max_seconds."""
        shards: List[List[int]] = [[]]
        load = 0.0
        for unit, seconds in enumerate(unit_seconds):
            if shards[-1] and load + seconds > max_seconds:
                shards.append([])
                load = 0.0
            shards[-1].append(unit)
            load += seconds
        return shards

    shard_count = min(len(fill(max_shard_seconds)), max_number_of_shards)
    # Bisect for the smallest largest-shard load that still fits in
    # shard_count shards, so the shards come out even, not full, full, rest.
    low, high = max(unit_seconds), sum(unit_seconds)
    for _ in range(60):
        middle = (low + high) / 2
        if len(fill(middle)) <= shard_count:
            high = middle
        else:
            low = middle
    return fill(high)


def plan(
    inventory: List[Dict],
    timings: Optional[Dict],
    max_shard_seconds: float = MAX_SHARD_SECONDS,
    unknown_file_seconds: float = UNKNOWN_FILE_SECONDS,
    max_number_of_shards: int = MAX_NUMBER_OF_SHARDS,
) -> Dict:
    """Assign one step's collected tests to shards.

    Whole files are the unit, kept inside their own pytest command and in
    collection order. Only a file whose time alone exceeds max_shard_seconds
    is split, into consecutive runs of its tests.

    Args:
        inventory: One entry per pytest command of the step, in order:
            {"command": the command, "prefix": its working dir relative to
            pytest's rootdir, "nodeids": the collected test IDs, in collection
            order}.
        timings: The timing endpoint's response for the step, or None when
            main has no usable timings. A files[] entry whose median exceeds
            the requested testsOverMs may carry a "tests" list of
            {"nodeid", "observedMs"}; a split file with that data is cut by
            its tests' times instead of their count.
        max_shard_seconds: Test-time budget for one shard.
        unknown_file_seconds: Time assumed for a file main has no timing for.
        max_number_of_shards: Upper bound on the number of shards.

    Returns:
        The plan. "shards" holds, per shard, its "estimateSeconds" and its
        "commands", each with the command "index" and the "targets" (files or
        test IDs relative to the command's working dir). The rest are counts,
        the timing source, flagged files and the rules used.

    """
    file_seconds = {}  # (command preview, file) -> seconds on main
    file_tests = {}  # (command preview, file) -> {nodeid: seconds on main}
    # Files whose every test was skipped on main: they run in about 0 s on
    # this step's hardware too, so their measured time is used, not the
    # unknown-file default.
    skip_only = set()
    for timing in (timings or {}).get("files", []):
        key = (timing["command"], timing["file"])
        file_seconds[key] = timing["observedMs"] / 1000
        if timing["timingStatus"] == "skip_only":
            skip_only.add(key)
        if timing.get("tests"):
            file_tests[key] = {
                t["nodeid"]: t["observedMs"] / 1000 for t in timing["tests"]
            }

    # One unit per file of each command, in collection order.
    units: List[Dict] = []
    for command_index, entry in enumerate(inventory):
        for nodeid in entry["nodeids"]:
            file = nodeid.split("::")[0]
            last = units[-1] if units else None
            if last and last["command"] == command_index and last["file"] == file:
                last["nodeids"].append(nodeid)
            else:
                units.append(
                    {
                        "command": command_index,
                        "file": file,
                        "nodeids": [nodeid],
                        "seconds": None,
                        "split": False,
                    }
                )
    files = set()
    unknown_files = []
    skipped_files = []
    for unit in units:
        files.add((unit["command"], unit["file"]))
        preview = command_preview(inventory[unit["command"]]["command"])
        unit["seconds"] = file_seconds.get((preview, unit["file"]))
        if unit["seconds"] is None:
            unit["seconds"] = unknown_file_seconds
            unknown_files.append(unit["file"])
        elif (preview, unit["file"]) in skip_only:
            skipped_files.append(unit["file"])

    oversized = []
    if timings is None:  # equal file counts, as a safe default
        unknown_files = []
        count = min(NO_TIMING_SHARDS, len(units))
        shards = []
        for number in range(count):
            start = number * len(units) // count
            end = (number + 1) * len(units) // count
            shards.append(list(range(start, end)))
    else:
        split_units = []
        for unit in units:
            nodeids = unit["nodeids"]
            parts = math.ceil(unit["seconds"] / max_shard_seconds)
            parts = max(1, min(parts, len(nodeids)))  # a 0 s file is still 1 part
            if parts > 1:
                oversized.append(unit["file"])
            preview = command_preview(inventory[unit["command"]]["command"])
            tests = file_tests.get((preview, unit["file"])) if parts > 1 else None
            if tests:
                # One unit per test function, its parametrized cases at the
                # sum of their own medians, so the packing below cuts the file
                # by time, adding a shard if that is what keeps every shard
                # within budget. The cases stay together because they share
                # setup (a model, compiled kernels) that main paid only once,
                # in the first case. A function over budget is cut per case.
                # A test main has no timing for gets the file's average.
                average = unit["seconds"] / len(nodeids)
                functions: List[List[str]] = []
                for nodeid in nodeids:
                    function = nodeid.split("[")[0]
                    if functions and functions[-1][0].split("[")[0] == function:
                        functions[-1].append(nodeid)
                    else:
                        functions.append([nodeid])
                for cases in functions:
                    seconds = [tests.get(nodeid, average) for nodeid in cases]
                    if sum(seconds) > max_shard_seconds:
                        pieces = [
                            ([nodeid], time) for nodeid, time in zip(cases, seconds)
                        ]
                    else:
                        pieces = [(cases, sum(seconds))]
                    for piece_ids, piece_seconds in pieces:
                        piece = dict(unit, nodeids=piece_ids, split=True)
                        piece["seconds"] = piece_seconds
                        split_units.append(piece)
            else:  # no per-test data: tests share the file's time evenly
                for part in range(parts):
                    start = part * len(nodeids) // parts
                    end = (part + 1) * len(nodeids) // parts
                    piece = dict(unit, nodeids=nodeids[start:end], split=parts > 1)
                    piece["seconds"] = unit["seconds"] / parts
                    split_units.append(piece)
        units = split_units
        shards = contiguous(
            [unit["seconds"] for unit in units], max_shard_seconds, max_number_of_shards
        )

    # A command that names test IDs, like `pytest a.py::test_x b.py`, runs
    # only those of a.py: a whole-file target would run all of them.
    by_id = set()
    for command_index, entry in enumerate(inventory):
        if any("::" in path for path in entry["paths"]):
            by_id.add(command_index)
    result = []
    for shard in shards:
        commands: List[Dict] = []
        for unit_index in shard:
            unit = units[unit_index]
            prefix = inventory[unit["command"]]["prefix"]
            prefix = prefix.rstrip("/") + "/" if prefix else ""
            whole = not unit["split"] and unit["command"] not in by_id
            targets = []
            for target in [unit["file"]] if whole else unit["nodeids"]:
                if target.startswith(prefix):
                    target = target[len(prefix) :]
                targets.append(target)
            if not commands or commands[-1]["index"] != unit["command"]:
                commands.append({"index": unit["command"], "targets": [], "tests": 0})
            commands[-1]["targets"] += targets
            commands[-1]["tests"] += len(unit["nodeids"])
        estimate = None
        if timings:
            estimate = round(sum(units[i]["seconds"] for i in shard), 1)
        result.append({"estimateSeconds": estimate, "commands": commands})

    timing_source = None
    if timings is not None:
        timing_source = {}
        for key in ("buildNumber", "buildNumbers", "commit", "finishedAt"):
            timing_source[key] = timings.get(key)
    over_budget = False
    if timings:
        for shard in result:
            if shard["estimateSeconds"] > max_shard_seconds:
                over_budget = True
    return {
        "shards": result,
        "tests": sum(len(entry["nodeids"]) for entry in inventory),
        "files": len(files),
        "timingSource": timing_source,
        "unknownFiles": unknown_files,
        "skippedFiles": skipped_files,
        "oversizedFiles": oversized,
        "overBudget": over_budget,
        "rules": {
            "packing": "contiguous",
            "maxShardSeconds": max_shard_seconds,
            "unknownFileSeconds": unknown_file_seconds,
            "maxNumberOfShards": max_number_of_shards,
        },
    }


def check(result: Dict, inventory: List[Dict]) -> None:
    """Verify every collected test is assigned exactly once, and nothing else.

    Args:
        result: A plan from plan().
        inventory: The inventory the plan was made from.

    Raises:
        ValueError: A target matches no collected test, or a collected test is
            assigned zero or several times.

    """
    assigned: Dict[Tuple[int, str], int] = {}
    for shard in result["shards"]:
        for command in shard["commands"]:
            entry = inventory[command["index"]]
            prefix = entry["prefix"].rstrip("/") + "/" if entry["prefix"] else ""
            for target in command["targets"]:
                full = prefix + target
                matched = []
                for nodeid in entry["nodeids"]:
                    if nodeid == full or nodeid.split("::")[0] == full:
                        matched.append(nodeid)
                if not matched:
                    raise ValueError("target matches no collected test: " + target)
                for nodeid in matched:
                    key = (command["index"], nodeid)
                    assigned[key] = assigned.get(key, 0) + 1
    expected = set()
    for index, entry in enumerate(inventory):
        for nodeid in entry["nodeids"]:
            expected.add((index, nodeid))
    if set(assigned) != expected or any(v != 1 for v in assigned.values()):
        raise ValueError("plan does not assign every collected test exactly once")


def _plural(number: int, word: str) -> str:
    return f"{number} {word}{'' if number == 1 else 's'}"


def _shard_files(command: Dict) -> List[Tuple[str, int, bool]]:
    """The files one shard runs for one command, in order.

    Args:
        command: One command of a shard in a plan from plan().

    Returns:
        (file, number of its tests in this shard, whether the file is split by
        test ID). A whole-file target counts as 0 tests here; the caller has
        the command's total.

    """
    files: List[Tuple[str, int, bool]] = []
    for target in command["targets"]:
        file = target.split("::")[0]
        split = "::" in target
        if split and files and files[-1][0] == file:
            files[-1] = (file, files[-1][1] + 1, True)
        else:
            files.append((file, 1 if split else 0, split))
    return files


def annotation(
    step_key: str,
    result: Dict,
    shadow: bool = True,
    commands: Optional[List[str]] = None,
) -> str:
    """The build annotation describing a plan.

    Args:
        step_key: The enrolled step's key.
        result: A plan from plan().
        shadow: Whether the step still runs as one job (shadow mode).
        commands: The step's pytest commands, to name them in the file list;
            without them the list says "command 1", "command 2", ...

    Returns:
        Markdown: the shard count, estimates, flagged files, a link to
        plan.json, a table with each shard's files per command, and a folded
        list of every shard's files.

    """
    shards = result["shards"]
    count = f"**{len(shards)} shard{'s' if len(shards) != 1 else ''}**"
    lines = [
        f"**Runtime sharding{' (shadow)' if shadow else ''} for `{step_key}`:** "
        + (f"would run as {count}" if shadow else f"running as {count}")
        + f" ({result['tests']} tests in {result['files']} files, at most "
        f"{result['rules']['maxNumberOfShards']} shards)."
        + (" This build still ran the step as one job." if shadow else "")
    ]
    source = result["timingSource"]
    if source:
        estimates = ", ".join(f"{s['estimateSeconds'] / 60:.1f}" for s in shards)
        builds = len(source.get("buildNumbers") or [source["buildNumber"]])
        lines.append(
            f"Estimated test time per shard (min): {estimates}. Timings: median of "
            f"{builds} main build{'s' if builds != 1 else ''}, the newest "
            f"{source['buildNumber']} (`{(source['commit'] or '')[:12]}`)."
        )
    else:
        lines.append(
            f"No main timings for this step: {len(shards)} shards with equal file counts."
        )
    if result["unknownFiles"]:
        lines.append(
            f"No timing, counted as {result['rules']['unknownFileSeconds'] / 60:.1f} min each: "
            + ", ".join(f"`{f}`" for f in result["unknownFiles"])
        )
    if result.get("skippedFiles"):
        lines.append(
            "Skipped on main (every test skipped in recent main runs), counted at "
            "their measured time: "
            + ", ".join(f"`{f}`" for f in result["skippedFiles"])
        )
    if result["oversizedFiles"]:
        lines.append(
            "Split by tests (file alone exceeds one shard): "
            + ", ".join(f"`{f}`" for f in result["oversizedFiles"])
        )
    if result["overBudget"]:
        lines.append(
            ":warning: At the most shards allowed, a shard is still over "
            f"{result['rules']['maxShardSeconds'] / 60:.0f} min of test time."
        )
    lines.append(
        f'Full plan: <a href="artifact://{INVENTORY_DIR}/{step_key}/plan.json">'
        "plan.json</a>"
    )

    # How many tests each split file has across all shards, for "N of M tests".
    split_totals: Dict[Tuple[int, str], int] = {}
    for s in shards:
        for command in s["commands"]:
            for file, tests, split in _shard_files(command):
                if split:
                    key = (command["index"], file)
                    split_totals[key] = split_totals.get(key, 0) + tests

    rows = [
        "| Shard | Tests | Files | Estimate (min) | Breakdown |",
        "|---|---|---|---|---|",
    ]
    details = ["<details>", "<summary>Files per shard</summary>", ""]
    for n, s in enumerate(shards, 1):
        breakdown = []
        shard_files = 0
        details.append(
            f"**Shard {n}** ({_plural(sum(c['tests'] for c in s['commands']), 'test')})"
        )
        details.append("")
        for command in s["commands"]:
            files = []
            for file, tests, split in _shard_files(command):
                # Test IDs that are all of the file's tests: not split.
                if split and tests == split_totals[(command["index"], file)]:
                    split = False
                files.append((file, tests, split))
            shard_files += len(files)
            split_count = sum(1 for _, _, split in files if split)
            part = f"command {command['index'] + 1}: {_plural(len(files), 'file')} ({_plural(command['tests'], 'test')}"
            if split_count:
                part += f"; {split_count} split by test ID"
            breakdown.append(part + ")")
            name = (
                commands[command["index"]]
                if commands
                else f"command {command['index'] + 1}"
            )
            details.append(f"- `{name}`")
            for file, tests, split in files:
                if split:
                    total = split_totals[(command["index"], file)]
                    details.append(f"  - `{file}`: {tests} of {total} tests")
                else:
                    details.append(f"  - `{file}`")
        details.append("")
        est = "" if s["estimateSeconds"] is None else f"{s['estimateSeconds'] / 60:.1f}"
        rows.append(
            f"| {n} | {sum(c['tests'] for c in s['commands'])} | "
            f"{_plural(shard_files, 'file')} | {est} | {'; '.join(breakdown)} |"
        )
    details.append("</details>")
    return "\n\n".join(lines) + "\n\n" + "\n".join(rows) + "\n\n" + "\n".join(details)


def fetch_timings(step_key: str) -> Optional[Dict]:
    """Per-file timings from main's latest passing build of the step, with
    per-test medians for the files over one shard's budget.

    Args:
        step_key: The enrolled step's key.

    Returns:
        The timing endpoint's response, or None if it has none or is down.

    """
    query = {"stepKey": step_key, "testsOverMs": MAX_SHARD_SECONDS * 1000}
    url = TIMINGS_URL + "?" + urllib.parse.urlencode(query)
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.load(response)
    except Exception as error:  # 404 (no passing main build), outage: fall back
        print(f"runtime-shard: no timings ({error})", file=sys.stderr)
        return None


def run_collect(index: str, command_b64: str, out_dir: str) -> None:
    """Collect one pytest command's tests, in the step's test image.

    Writes inventory-<index>.json with the command, pytest's exit status, the
    selected node IDs, the working dir relative to pytest's rootdir and the
    command's arguments that pytest took as paths.

    Args:
        index: The command's position among the step's pytest commands.
        command_b64: The command, encoded with encode().
        out_dir: Where to write the inventory file, for artifact upload.

    Raises:
        SystemExit: Collection failed or selected no tests.

    """
    import pytest

    command = decode(command_b64)
    # The command's own NAME=value assignments can change what it collects.
    env, words = env_and_words(command)
    os.environ.update(env)
    found: Dict = {"nodeids": [], "prefix": "", "paths": []}

    class Probe:
        def pytest_collection_finish(self, session):
            found["nodeids"] = [item.nodeid for item in session.items]
            # Node IDs are relative to rootdir; run targets to the working dir.
            prefix = os.path.relpath(os.getcwd(), str(session.config.rootpath))
            found["prefix"] = "" if prefix == "." else prefix
            found["paths"] = list(session.config.args)

    status = int(
        pytest.main(
            [*words[1:], "--collect-only", "-q"],
            plugins=[Probe()],
        )
    )
    os.makedirs(out_dir, exist_ok=True)
    # In a container root writes into an agent-owned checkout, so these land
    # root-owned. Deleting a file needs write on its directory, not the file, so
    # 0777 is what lets the next job's checkout clean up. Without it every later
    # job on that agent fails to clone.
    for path in (os.path.dirname(os.path.abspath(out_dir)), out_dir):
        try:
            os.chmod(path, 0o777)
        except OSError:
            pass
    with open(os.path.join(out_dir, f"inventory-{index}.json"), "w") as f:
        json.dump(
            {"index": int(index), "command": command, "exitstatus": status, **found}, f
        )
    if status != 0 or not found["nodeids"]:
        raise SystemExit(f"runtime-shard: collection failed (pytest exit {status})")


def shard_step(
    template: Dict, result: Dict, inventory: List[Dict], script_url: str
) -> Dict:
    """The step's own job, run as one parallel job per shard.

    Parallel jobs share their commands, so each pytest command becomes a
    `case` on BUILDKITE_PARALLEL_JOB: a job runs one pytest command per file
    of its shard, each under a log header that shows it. Each job installs
    this file as a pytest plugin, which checks that every test the plan gave
    a file's command runs.

    Args:
        template: The step's normal rendered job.
        result: A checked plan from plan().
        inventory: The inventory the plan was made from.
        script_url: Where the shard jobs fetch this file from.

    Returns:
        The job to upload, with parallelism set to the shard count.

    Raises:
        ValueError: A pytest command is not where the generator puts it.

    """
    step = json.loads(json.dumps(template))
    step["parallelism"] = len(result["shards"])
    # Buildkite fills in %N (from 1) and %t (the shard count): "... shard 2/4".
    step["label"] = f"{step.get('label', '')} shard %N/%t".strip()
    env = dict(step.get("env") or {})
    env["PYTEST_ADDOPTS"] = (
        env.get("PYTEST_ADDOPTS", "") + " -p runtime_shard"
    ).strip()
    step["env"] = env
    step["commands"] = [
        f'curl -sSfL --retry 3 --max-time 60 -o /tmp/runtime_shard.py "{script_url}"',
        'python3 -c "import shutil, sysconfig; shutil.copy('
        "'/tmp/runtime_shard.py', sysconfig.get_paths()['purelib'])\"",
        *_shard_commands(step["commands"], result, inventory),
    ]
    return step


def _shard_command(entry: Dict, targets: List[str]) -> List[str]:
    """The pytest command that runs these targets of one planned command: its
    own NAME=value assignments and options, with its paths replaced by the
    targets.

    Args:
        entry: The command's inventory entry.
        targets: The targets, relative to the command's working dir.

    Returns:
        The command's parts, shell-quoted: assignments, pytest and options
        first, then one part per target.

    """
    env, words = env_and_words(entry["command"])
    options = words[1:]
    for path in entry["paths"]:
        if path in options:
            options.remove(path)
    # NAME='a b', not 'NAME=a b', which the shell would run as a program.
    assignments = [f"{name}={shlex.quote(value)}" for name, value in env.items()]
    parts = [" ".join([*assignments, shlex.join(["pytest", *options])])]
    for target in targets:
        parts.append(shlex.quote(target))
    return parts


def _files_by_shard(result: Dict) -> List[List[Tuple[int, str, List[str]]]]:
    """Each shard's files, in plan order: (command index, file, its targets).

    Args:
        result: A checked plan from plan().

    Returns:
        One list per shard.

    """
    shards = []
    for shard in result["shards"]:
        files = []
        for planned in shard["commands"]:
            by_file: Dict[str, List[str]] = {}
            for target in planned["targets"]:
                by_file.setdefault(target.split("::")[0], []).append(target)
            for file, targets in by_file.items():
                files.append((planned["index"], file, targets))
        shards.append(files)
    return shards


def _shard_commands(
    commands: List[str], result: Dict, inventory: List[Dict]
) -> List[str]:
    """The job's commands, with each pytest command and its log header
    replaced by a `case` on the shard: one pytest command per file of the
    shard, each under its own log header.

    A failing file doesn't stop the shard's other files of the command; the
    job then fails after them, where the step's own command would have.

    Args:
        commands: The step's rendered commands.
        result: A checked plan from plan().
        inventory: The inventory the plan was made from.

    Returns:
        The commands for the shard jobs.

    Raises:
        ValueError: A pytest command is not where the generator puts it.

    """
    previews = [command_preview(entry["command"]) for entry in inventory]
    shard_files = _files_by_shard(result)
    replaced = []
    done = set()  # the planned commands replaced so far
    position = 0
    while position < len(commands):
        line = commands[position]
        match = _COMMAND_HEADER.fullmatch(line)
        if not match or match.group(3) not in previews:
            replaced.append(line)
            position += 1
            continue
        index = previews.index(match.group(3))
        entry = inventory[index]
        # The generator wraps the command (tracing, continue-on-failure) and
        # renders its ' as ". Each file gets the wrapped line with its own
        # command in place of the step's.
        if position + 1 >= len(commands):
            raise ValueError(f"command {index + 1} is missing from the step's job")
        wrapped = commands[position + 1]
        before, found, after = wrapped.rpartition(entry["command"].replace("'", '"'))
        if not found:
            raise ValueError(f"command {index + 1} is missing from the step's job")
        prefix = entry["prefix"].rstrip("/") + "/" if entry["prefix"] else ""
        file_tests: Dict[str, int] = {}
        for nodeid in entry["nodeids"]:
            file = nodeid.split("::")[0][len(prefix) :]
            file_tests[file] = file_tests.get(file, 0) + 1

        # The headers keep the step's own numbering, "Command (4/5)", so a
        # number means the same YAML command in every shard, and name only
        # the file; the exact command is the first line of its section. A
        # shard shows only the commands it runs: check() made sure every test
        # runs in some shard.
        command = f"+++ :test_tube: Command ({match.group(2)})"
        branches = []
        for number, files in enumerate(shard_files):
            mine = []  # this command's files in this shard
            for planned, file, targets in files:
                if planned == index:
                    mine.append((file, targets))
            lines = []
            for file_number, (file, targets) in enumerate(mine):
                parts = _shard_command(entry, targets)
                # The plugin fails the file's command if one of these is not
                # among the tests it runs.
                full = {prefix + target for target in targets}
                expected = []
                for nodeid in entry["nodeids"]:
                    if nodeid in full or nodeid.split("::")[0] in full:
                        expected.append(nodeid)
                lines.append(f"export RUNTIME_SHARD_TESTS={pack(expected)}")
                title = f"{command}, file {file_number + 1}/{len(mine)}: {file}"
                # A split file's targets are its test IDs, one per test.
                if "::" in targets[0] and len(targets) < file_tests[file]:
                    title += f"   ({len(targets)} of {file_tests[file]} tests)"
                shown = " ".join(shlex.quote(text) for text in (title, " ".join(parts)))
                # Buildkite interpolates the uploaded step: $$ is a literal $.
                # The generator's own lines are already escaped.
                lines.append(f"printf '%s\\n' {shown}".replace("$", "$$"))
                run = before + " ".join(parts).replace("$", "$$") + after
                lines.append(f"{{ {run}\n}} || runtime_shard_status=1")
            if lines:
                lines.insert(0, "runtime_shard_status=0")
                lines.append("unset RUNTIME_SHARD_TESTS")
                lines.append("(exit $$runtime_shard_status)")
            else:
                lines.append(":")
            branches.append(f"{number})\n" + "\n".join(lines) + "\n;;")
        replaced.append(
            'case "$$BUILDKITE_PARALLEL_JOB" in\n'
            + "\n".join(branches)
            + '\n*) echo "runtime-shard: no shard $$BUILDKITE_PARALLEL_JOB"; exit 1;;'
            + "\nesac"
        )
        done.add(index)
        position += 2
    # A command left as is would run in full in every shard.
    for index in range(len(inventory)):
        if index not in done:
            raise ValueError(f"command {index + 1} is missing from the step's job")
    return replaced


def _upload(step: Dict) -> None:
    subprocess.run(
        ["buildkite-agent", "pipeline", "upload"],
        input=json.dumps({"steps": [step]}),
        text=True,
        check=True,
    )


def run_plan(step_key: str, commands_b64: str, mode: str = "shadow") -> None:
    """Plan the step's shards on a CPU agent and upload the jobs.

    If collection, timings or planning fail, the step's normal single job is
    uploaded instead. This raises only if that upload fails too, and then the
    generator's shell fallback uploads the single job.

    Args:
        step_key: The enrolled step's key.
        commands_b64: The step's pytest commands, encoded with encode().
        mode: "on" uploads the jobs; "shadow" only plans, because the step
            already runs as one job.

    """
    commands = decode(commands_b64)
    shadow = mode == "shadow"
    template = (
        None if shadow else decode(os.environ["RUNTIME_SHARD_TEMPLATE"])["steps"][0]
    )
    step = template
    try:
        pattern = f"{INVENTORY_DIR}/{step_key}/inventory-*.json"
        subprocess.run(
            ["buildkite-agent", "artifact", "download", pattern, "."], check=True
        )
        inventory = []
        for i, command in enumerate(commands):
            with open(f"{INVENTORY_DIR}/{step_key}/inventory-{i}.json") as f:
                entry = json.load(f)
            if (
                entry["command"] != command
                or entry["exitstatus"] != 0
                or not entry["nodeids"]
            ):
                raise ValueError(f"collection of command {i + 1} is missing or failed")
            inventory.append(entry)
        result = plan(inventory, fetch_timings(step_key))
        check(result, inventory)
        result.update(stepKey=step_key, inventory=inventory)
        path = f"{INVENTORY_DIR}/{step_key}/plan.json"
        with open(path, "w") as f:
            json.dump(result, f, indent=1)
        subprocess.run(["buildkite-agent", "artifact", "upload", path], check=False)
        if not shadow and len(result["shards"]) > 1:
            step = shard_step(
                template, result, inventory, os.environ["RUNTIME_SHARD_SCRIPT_URL"]
            )
        message = annotation(step_key, result, shadow, commands)
        # Only a plan someone should look at gets a build annotation, so the
        # build page stays readable with many enrolled steps. Every plan is in
        # this job's log and plan.json, and each shard's log shows its command.
        degraded = result["overBudget"] or result["timingSource"] is None
        style = "warning" if degraded else None
    except Exception as error:
        message = (
            f"**Runtime sharding{' (shadow)' if shadow else ''} for `{step_key}`:** "
            f"no plan ({error}). The step runs as one job, as it would without sharding."
        )
        style = "warning"
    if step is not None:
        _upload(step)
    print(message)
    if style is None:
        return
    subprocess.run(
        [
            "buildkite-agent",
            "annotate",
            "--context",
            f"runtime-shard-{step_key}",
            "--style",
            style,
            message,
        ],
        check=False,
    )


def pytest_collection_finish(session):
    """Plugin: fail a shard's file command if a test the plan gave it is not
    among the tests it runs, after every filter. Otherwise a test that the
    shard's collection lost would run in no shard while every shard passed.
    A no-op without RUNTIME_SHARD_TESTS.
    """
    # Popped, so a pytest that a test itself starts doesn't check against it.
    packed = os.environ.pop("RUNTIME_SHARD_TESTS", None)
    if not packed:
        return
    import pytest

    running = {item.nodeid for item in session.items}
    missing = []
    for nodeid in unpack(packed):
        if nodeid not in running:
            missing.append(nodeid)
    if missing:
        raise pytest.UsageError(
            f"runtime-shard: {_plural(len(missing), 'planned test')} would not run:"
            f" {missing[:5]}"
        )


if __name__ == "__main__":
    mode, *args = sys.argv[1:]
    {"collect": run_collect, "plan": run_plan}[mode](*args)
