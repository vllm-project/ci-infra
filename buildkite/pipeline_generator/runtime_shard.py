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
  plugin   `-p runtime_shard` in each shard job: keeps only the tests the plan
           gave this BUILDKITE_PARALLEL_JOB, so the commands stay unchanged.

Sharding never blocks the step: if collection or planning fails, the plan step
uploads the step's normal single job instead (see run_plan and the generator's
shell fallback).
"""

import base64
import json
import math
import os
import shlex
import subprocess
import sys
import urllib.parse
import urllib.request
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


def split_commands(commands: List[str]) -> Optional[Tuple[List[str], List[str]]]:
    """Separate a step's setup commands from its pytest commands.

    Args:
        commands: The step's commands, in order.

    Returns:
        (setup commands, pytest commands), or None if the step can't be
        sharded: it needs at least one pytest command, and only plain pytest
        commands (no shell syntax, no pytest-shard flags) after the first.

    """
    first = None
    for position, command in enumerate(commands):
        if command.startswith("pytest "):
            first = position
            break
    if first is None:
        return None
    tests = commands[first:]
    for command in tests:
        if not command.startswith("pytest ") or any(
            s in command for s in _SHELL_SYNTAX
        ):
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
                # One unit per test at its own median, so the packing below
                # cuts the file by time, adding a shard if that is what keeps
                # every shard within budget. A test main has no timing for
                # gets the file's average per test.
                average = unit["seconds"] / len(nodeids)
                for nodeid in nodeids:
                    piece = dict(unit, nodeids=[nodeid], split=True)
                    piece["seconds"] = tests.get(nodeid, average)
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

    result = []
    for shard in shards:
        commands: List[Dict] = []
        for unit_index in shard:
            unit = units[unit_index]
            prefix = inventory[unit["command"]]["prefix"]
            prefix = prefix.rstrip("/") + "/" if prefix else ""
            targets = []
            for target in unit["nodeids"] if unit["split"] else [unit["file"]]:
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
            files = _shard_files(command)
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
    selected node IDs and the working dir relative to pytest's rootdir.

    Args:
        index: The command's position among the step's pytest commands.
        command_b64: The command, encoded with encode().
        out_dir: Where to write the inventory file, for artifact upload.

    Raises:
        SystemExit: Collection failed or selected no tests.

    """
    import pytest

    command = decode(command_b64)
    found: Dict = {"nodeids": [], "prefix": ""}

    class Probe:
        def pytest_collection_finish(self, session):
            found["nodeids"] = [item.nodeid for item in session.items]
            # Node IDs are relative to rootdir; run targets to the working dir.
            prefix = os.path.relpath(os.getcwd(), str(session.config.rootpath))
            found["prefix"] = "" if prefix == "." else prefix

    status = int(
        pytest.main(
            [*shlex.split(command, comments=True)[1:], "--collect-only", "-q"],
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

    Each job installs this file, loads it as a pytest plugin and finds its
    tests in RUNTIME_SHARD_PLAN, so the step's commands stay unchanged.

    Args:
        template: The step's normal rendered job.
        result: A checked plan from plan().
        inventory: The inventory the plan was made from.
        script_url: Where the shard jobs fetch this file from.

    Returns:
        The job to upload, with parallelism set to the shard count.

    """
    shards = []
    for shard in result["shards"]:
        commands = []
        for command in shard["commands"]:
            entry = inventory[command["index"]]
            prefix = entry["prefix"].rstrip("/") + "/" if entry["prefix"] else ""
            commands.append(
                {
                    "index": command["index"],
                    "targets": [prefix + t for t in command["targets"]],
                }
            )
        shards.append(commands)
    step = json.loads(json.dumps(template))
    env = dict(step.get("env") or {})
    env["RUNTIME_SHARD_PLAN"] = encode(
        {"commands": [e["command"] for e in inventory], "shards": shards}
    )
    env["PYTEST_ADDOPTS"] = (
        env.get("PYTEST_ADDOPTS", "") + " -p runtime_shard"
    ).strip()
    step["env"] = env
    step["parallelism"] = len(shards)
    # Buildkite fills in %N (from 1) and %t (the shard count): "... shard 2/4".
    step["label"] = f"{step.get('label', '')} shard %N/%t".strip()
    step["commands"] = [
        f'curl -sSfL --retry 3 --max-time 60 -o /tmp/runtime_shard.py "{script_url}"',
        'python3 -c "import shutil, sysconfig; shutil.copy('
        "'/tmp/runtime_shard.py', sysconfig.get_paths()['purelib'])\"",
        *step["commands"],
    ]
    return step


def _upload(step: Dict) -> None:
    subprocess.run(
        ["buildkite-agent", "pipeline", "upload"],
        input=json.dumps({"steps": [step]}),
        text=True,
        check=True,
    )


def run_plan(step_key: str, commands_b64: str, mode: str = "shadow") -> None:
    """Plan the step's shards on a CPU agent, annotate, and upload the jobs.

    If collection, timings or planning fail, the step's normal single job is
    uploaded instead. This raises only if that upload fails too, and then the
    generator's shell fallback uploads the single job.

    Args:
        step_key: The enrolled step's key.
        commands_b64: The step's pytest commands, encoded with encode().
        mode: "on" uploads the jobs; "shadow" only annotates, because the step
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
        style = "info"
    except Exception as error:
        message = (
            f"**Runtime sharding{' (shadow)' if shadow else ''} for `{step_key}`:** "
            f"no plan ({error}). The step runs as one job, as it would without sharding."
        )
        style = "warning"
    if step is not None:
        _upload(step)
    print(message)
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


_EMPTY: Dict[str, bool] = {}

try:  # only the plugin needs pytest; the plan step's python3 has none
    import pytest

    _after_other_filters = pytest.hookimpl(trylast=True)
except ImportError:

    def _after_other_filters(hook):
        return hook


# Last, so the command's own -m and -k and conftest filters have run: the
# collect step recorded the tests left after them, and a test they drop
# is not a stray.
@_after_other_filters
def pytest_collection_modifyitems(session, config, items):
    """Plugin: keep this shard's tests. A no-op without RUNTIME_SHARD_PLAN."""
    encoded = os.environ.get("RUNTIME_SHARD_PLAN")
    if not encoded:
        return

    shard_plan = decode(encoded)
    index = int(os.environ.get("BUILDKITE_PARALLEL_JOB", "0"))
    args = list(config.invocation_params.args)
    matches = []
    for position, command in enumerate(shard_plan["commands"]):
        if shlex.split(command, comments=True)[1:] == args:
            matches.append(position)
    if len(matches) != 1 or index >= len(shard_plan["shards"]):
        raise pytest.UsageError(
            f"runtime-shard: no single planned command for shard {index + 1} and {args}"
        )
    this_command = matches[0]

    # Targets are whole files or single test IDs, for this command only.
    mine = set()  # this shard's targets
    anywhere = set()  # every shard's targets
    for number, shard in enumerate(shard_plan["shards"]):
        for command in shard:
            if command["index"] != this_command:
                continue
            anywhere.update(command["targets"])
            if number == index:
                mine.update(command["targets"])

    def target_of(item, targets):
        """The target that covers this test, or None."""
        if item.nodeid in targets:
            return item.nodeid
        file = item.nodeid.split("::")[0]
        return file if file in targets else None

    kept, deselected, strays = [], [], []
    found = set()  # targets that matched at least one collected test
    for item in items:
        target = target_of(item, mine)
        if target is not None:
            kept.append(item)
            found.add(target)
        elif target_of(item, anywhere) is not None:
            deselected.append(item)
        else:
            # Collected here but given to no shard (collection differed from
            # the collect step's): never drop it, shard 1 runs it.
            strays.append(item)
            if index == 0:
                kept.append(item)
            else:
                deselected.append(item)
    if strays:
        where = "running them here" if index == 0 else "shard 1 runs them"
        print(
            f"\nruntime-shard: {len(strays)} collected tests are in no shard;"
            f" {where}: {[item.nodeid for item in strays[:5]]}",
            flush=True,
        )
    missing = sorted(mine - found)
    if missing:
        raise pytest.UsageError(
            f"runtime-shard: planned tests were not collected: {missing[:5]}"
        )
    config.hook.pytest_deselected(items=deselected)
    items[:] = kept
    _EMPTY["value"] = not kept
    _EMPTY["label"] = (
        f"shard {index + 1}/{len(shard_plan['shards'])}, command {this_command + 1}"
    )


def pytest_collection_finish(session):
    if "label" in _EMPTY:  # after every filter, including the command's own -m
        print(
            f"\nruntime-shard: {_EMPTY['label']}: running {len(session.items)} tests",
            flush=True,
        )


def pytest_sessionfinish(session, exitstatus):
    if _EMPTY.get("value") and exitstatus == 5:  # no tests of this command here
        session.exitstatus = 0


if __name__ == "__main__":
    mode, *args = sys.argv[1:]
    {"collect": run_collect, "plan": run_plan}[mode](*args)
