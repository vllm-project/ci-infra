"""Runtime sharding for steps enrolled with `automatic_shard: true`.

The pipeline generator plans an enrolled step's shards while it generates the
pipeline, from the vLLM checkout it runs in:

  discover  every test file each of the step's pytest commands names: its
            path arguments walked with pytest's file patterns, less its
            --ignore options.
  timings   main's median time per file for the step (ci.vllm.ai), with
            per-test medians for a file over one shard's budget.
  plan      whole files into the fewest shards that keep each within the
            budget, largest file first (at most 6). A file over the budget is
            split by test function, using its per-test medians.

The step is then emitted as its own job with `parallelism: N`. Every parallel
job runs the step's commands unchanged; runtime_shard_plugin.py, loaded into
their pytest, keeps only the job's tests and opens a log group per file. The
plan and the plugin ride in the job's environment, so a shard fetches nothing.

Sharding never blocks the step: if discovery, timings or planning fail, the
step is emitted as its normal single job, with a warning. Python 3.9: the
generator runs on the small CPU queue's python3.
"""

import base64
import fnmatch
import hashlib
import json
import math
import os
import re
import shlex
import statistics
import subprocess
import sys
import urllib.parse
import urllib.request
import zlib
from collections import Counter
from typing import Dict, List, Optional, Tuple

from constants import AgentQueue

TIMINGS_URL = "https://ci.vllm.ai/api/timings/latest"
# Test time per shard; setup is not counted. Planned below the 20-minute limit
# so run-to-run noise rarely pushes a real shard over it.
MAX_SHARD_SECONDS = 1080
MAX_NUMBER_OF_SHARDS = 6
# A file main has no timing for (new in the PR, say) counts as the step's
# median file, and at least this. Under a command that picks tests by a
# marker or keyword (`-m hybrid_model` over a directory of other models), it
# counts as this alone: main timed every file the filter picks tests in, so
# one it never timed most likely has none. A filter that excludes ("not
# slow_test") still runs most files, so a new file there counts as usual.
MIN_UNKNOWN_FILE_SECONDS = 5
_FILTER_OPTIONS = ("-m", "-k")
# Where the image keeps the checkout; a step's working_dir lives under it.
CONTAINER_WORKSPACE = "/vllm-workspace"
PLAN_ENV = "RUNTIME_SHARD_PLAN"
PLUGIN_ENV = "RUNTIME_SHARD_PLUGIN"
PLUGIN_DIR = "/tmp/runtime-shard"
PLUGIN_MODULE = "runtime_shard_plugin"
# pytest-shard would select a subset a second time on top of the plan.
_UNSHARDABLE_ARGS = ("--num-shards", "--shard-id")
_SHELL_SYNTAX = ("&&", "||", ";", "|", ">", "<", "`", "$", "\n")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# pytest options that take their value as the next argument, so that value is
# not a path.
_VALUE_OPTIONS = {
    "-c",
    "-k",
    "-m",
    "-n",
    "-o",
    "-p",
    "-r",
    "-W",
    "--basetemp",
    "--capture",
    "--color",
    "--confcutdir",
    "--deselect",
    "--dist",
    "--durations",
    "--ignore",
    "--ignore-glob",
    "--import-mode",
    "--junit-xml",
    "--junitxml",
    "--log-level",
    "--maxfail",
    "--override-ini",
    "--rootdir",
    "--tb",
    "--timeout",
    "--timeout-method",
}
# pytest's defaults; vLLM sets neither.
_PYTHON_FILES = ("test_*.py", "*_test.py")
_NORECURSE = ("*.egg", ".*", "_darcs", "build", "CVS", "dist", "node_modules", "venv")


def _legacy_preview(command: str) -> str:
    return command[:80].replace("'", "").replace('"', "").replace("$", "")


def command_preview(command: str) -> str:
    """A readable timing label, with a hash of the full unsanitized command."""
    preview = _legacy_preview(command)
    digest = hashlib.sha256(command.encode("utf-8")).hexdigest()[:12]
    return f"{preview} [{digest}]"


def command_labels(commands: List[str]) -> List[str]:
    """Hashed previews, numbered from the second identical command (` #2`)."""
    labels: List[str] = []
    for command in commands:
        label = preview = command_preview(command)
        number = 1
        while label in labels:
            number += 1
            label = f"{preview} #{number}"
        labels.append(label)
    return labels


def env_and_words(command: str) -> Tuple[Dict[str, str], List[str]]:
    """Split a command into its leading NAME=value assignments and the rest:
    `TP_SIZE=1 pytest -v x.py` is ({"TP_SIZE": "1"}, ["pytest", "-v", "x.py"]).

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

    Returns:
        (setup commands, pytest commands), or None if the step can't be
        sharded: it needs at least one pytest command, only plain pytest
        commands (no shell syntax, no pytest-shard flags) after the first,
        and no torchrun before it.

    """
    first = None
    for position, command in enumerate(commands):
        if _program(command) == "pytest":
            first = position
            break
    if first is None:
        return None
    # Setup runs in full in every shard.
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


def _picks_tests(command: str) -> bool:
    """Whether a pytest command keeps only the tests a -m or -k expression
    names, rather than dropping the ones it excludes."""
    args = shlex.split(command, comments=True)
    for position, arg in enumerate(args):
        if arg in _FILTER_OPTIONS and position + 1 < len(args):
            expression = args[position + 1]
        elif arg[:2] in _FILTER_OPTIONS and len(arg) > 2:
            expression = arg[2:].lstrip("=")
        else:
            continue
        if not re.search(r"\bnot\b", expression):
            return True
    return False


def pack(value) -> str:
    return base64.b64encode(zlib.compress(json.dumps(value).encode())).decode()


def unpack(text: str):
    return json.loads(zlib.decompress(base64.b64decode(text)))


def discover(command: str, cwd: str, root: str) -> Dict:
    """The test files a pytest command names, from the checkout.

    Args:
        command: The pytest command, as the step's YAML writes it.
        cwd: Where it runs, in the checkout.
        root: The checkout, which is pytest's rootdir in the image.

    Returns:
        {"files": its test files, relative to root as pytest's node IDs name
        them; "selected": the files it names by test ID, which run as written
        and are never split}.

    Raises:
        ValueError: A path argument isn't in the checkout, or the command
            names none (pytest would fall back to its testpaths).

    """
    _, words = env_and_words(command)
    args = words[1:]
    paths, ignore, ignore_glob = [], [], []
    position = 0
    while position < len(args):
        arg = args[position]
        name, has_value, value = arg.partition("=")
        if arg.startswith("-"):
            if not has_value and name in _VALUE_OPTIONS:
                position += 1
                value = args[position] if position < len(args) else ""
            if name == "--ignore":
                ignore.append(os.path.normpath(os.path.join(cwd, value)))
            elif name == "--ignore-glob":
                ignore_glob.append(os.path.join(cwd, value))
        else:
            paths.append(arg)
        position += 1
    if not paths:
        raise ValueError(f"no test path in `{command}`")

    def ignored(path: str) -> bool:
        return any(path == i or path.startswith(i + os.sep) for i in ignore) or any(
            fnmatch.fnmatch(path, g) for g in ignore_glob
        )

    files: List[str] = []
    selected: List[str] = []
    for arg in paths:
        target = os.path.normpath(os.path.join(cwd, arg.split("::")[0]))
        if not os.path.exists(target):
            raise ValueError(f"`{arg}` of `{command}` is not in the checkout")
        found = []
        if os.path.isdir(target):
            for directory, dirs, names in os.walk(target):
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if not any(fnmatch.fnmatch(d, p) for p in _NORECURSE)
                    and not ignored(os.path.join(directory, d))
                )
                for name in sorted(names):
                    path = os.path.join(directory, name)
                    if any(
                        fnmatch.fnmatch(name, p) for p in _PYTHON_FILES
                    ) and not ignored(path):
                        found.append(path)
        elif not ignored(target):
            found.append(target)
        for path in found:
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if relative not in files:
                files.append(relative)
            if "::" in arg and relative not in selected:
                selected.append(relative)
    return {"files": files, "selected": selected}


def fetch_timings(step_key: str, queue: AgentQueue) -> Optional[Dict]:
    """Main's per-file medians for the step on `queue`, with per-test medians
    for the files over one shard's budget; None if the endpoint has none or is
    down."""
    query = {
        "stepKey": step_key,
        "queue": queue.value,
        "testsOverMs": MAX_SHARD_SECONDS * 1000,
    }
    url = TIMINGS_URL + "?" + urllib.parse.urlencode(query)
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return json.load(response)
    except Exception as error:  # 404 (no passing main build), outage: no plan
        print(f"runtime-shard: no timings for {step_key} ({error})", file=sys.stderr)
        return None


def _least_loaded(sizes: List[float], count: int) -> Tuple[List[int], List[float]]:
    """Largest first, each to the least-loaded of `count` shards: the shard
    of each size, and each shard's load."""
    loads = [0.0] * count
    shard_of = [0] * len(sizes)
    for index in sorted(range(len(sizes)), key=lambda i: -sizes[i]):
        shard = min(range(count), key=lambda s: loads[s])
        shard_of[index] = shard
        loads[shard] += sizes[index]
    return shard_of, loads


def _mixed_within(
    units: List[Tuple], shard_of: List[int], loads: List[float], budget: float
) -> bool:
    """Whether every shard that runs more than one command keeps to `budget`."""
    commands_of: List[set] = [set() for _ in loads]
    for unit, shard in zip(units, shard_of):
        commands_of[shard].add(unit[0])
    return all(len(c) == 1 or load <= budget for c, load in zip(commands_of, loads))


def plan(
    commands: List[Dict],
    timings: Optional[Dict],
    max_shard_seconds: float = MAX_SHARD_SECONDS,
    max_number_of_shards: int = MAX_NUMBER_OF_SHARDS,
) -> Optional[Dict]:
    """Assign one step's test files to shards.

    Args:
        commands: Per pytest command of the step: {"command", "files",
            "selected"}, as from discover(), and optionally "span_label",
            the command's label on main's spans (command_labels()).
        timings: The timing endpoint's response for the step, or None.
        max_shard_seconds: Test-time budget for one shard.
        max_number_of_shards: Upper bound on the number of shards.

    Returns:
        None when main has no timing for any of the step's files: a split
        would be a guess. Otherwise "shards", the number of shards (1 if the
        step fits in one job), "estimates" per shard in seconds, and per
        command the shard of each of its "files" and, for each file split by
        test, "split": the shard of each part and the part of each test main
        has timed. The rest are counts and flagged files.

    """
    file_seconds = {}  # (label, file) -> seconds on main
    file_tests = {}  # (label, file) -> {nodeid: seconds on main}
    previews: Dict[str, List[str]] = {}  # file -> labels main ran it under
    commands_per_file = Counter(file for entry in commands for file in entry["files"])
    legacy_previews = Counter(_legacy_preview(entry["command"]) for entry in commands)
    for timing in (timings or {}).get("files", []):
        key = (timing["command"], timing["file"])
        file_seconds[key] = timing["observedMs"] / 1000
        previews.setdefault(timing["file"], []).append(timing["command"])
        if timing.get("tests"):
            file_tests[key] = {
                t["nodeid"]: t["observedMs"] / 1000 for t in timing["tests"]
            }

    def timing_key(entry: Dict, file: str) -> Optional[Tuple[str, str]]:
        """Match the exact label, then an unambiguous legacy preview. Fall
        back by file only when current and historical commands are unique."""
        key = (entry.get("span_label") or command_preview(entry["command"]), file)
        if key in file_seconds:
            return key
        historical = previews.get(file, [])
        hashed = [
            label
            for label in historical
            if re.search(r" \[[0-9a-f]{12}\](?: #\d+)?$", label)
        ]
        # Prefer hashed history during rollout. Old labels cannot prove full
        # command identity; reuse them only when current previews are unique.
        legacy = _legacy_preview(entry["command"])
        if hashed:
            historical = hashed
        elif legacy_previews[legacy] == 1 and (legacy, file) in file_seconds:
            return (legacy, file)
        if commands_per_file[file] == 1 and len(historical) == 1:
            return (historical[0], file)
        return None

    keys = {
        (index, file): timing_key(entry, file)
        for index, entry in enumerate(commands)
        for file in entry["files"]
    }
    known = [file_seconds[k] for k in keys.values() if k is not None]
    if not known:
        return None
    unknown_seconds = max(MIN_UNKNOWN_FILE_SECONDS, statistics.median(known))

    # One unit per file, or per part of a file over the budget.
    units = []  # (command index, file, part or None, seconds)
    whole = []  # per unit: its name if main timed it so and it can't be cut
    assigns: Dict[Tuple[int, str], Dict[str, int]] = {}
    unknown_files, filtered_unknown_files, oversized = [], [], []
    filtered = [_picks_tests(entry["command"]) for entry in commands]
    for (index, file), key in keys.items():
        if key is not None:
            seconds = file_seconds[key]
        elif filtered[index]:
            seconds = MIN_UNKNOWN_FILE_SECONDS
            filtered_unknown_files.append(file)
        else:
            seconds = unknown_seconds
            unknown_files.append(file)
        tests = file_tests.get(key) if key is not None else None
        selected = file in commands[index]["selected"]
        # Only this occurrence's full-command hash can justify trimming;
        # legacy and file-only matches remain estimates.
        entry = commands[index]
        exact = key == (
            entry.get("span_label") or command_preview(entry["command"]),
            file,
        )
        if seconds <= max_shard_seconds or not tests or selected:
            units.append((index, file, None, seconds))
            whole.append(file if exact else None)
            continue
        # By test function, its parametrized cases together: they share setup
        # (a model, compiled kernels) that main paid once, in the first case.
        # A function over the budget alone is cut by case.
        oversized.append(file)
        functions: Dict[str, float] = {}
        for nodeid, test_seconds in tests.items():
            function = nodeid.split("[")[0]
            functions[function] = functions.get(function, 0.0) + test_seconds
        groups: Dict[str, float] = {}  # a function, or one case of a big one
        group_of = {}
        for nodeid, test_seconds in tests.items():
            function = nodeid.split("[")[0]
            group = nodeid if functions[function] > max_shard_seconds else function
            group_of[nodeid] = group
            groups[group] = groups.get(group, 0.0) + test_seconds
        names = sorted(groups)
        parts = min(len(names), math.ceil(seconds / max_shard_seconds))
        part_of, loads = _least_loaded([groups[n] for n in names], parts)
        part_of_group = dict(zip(names, part_of))
        assigns[(index, file)] = {
            nodeid: part_of_group[group_of[nodeid]] for nodeid in tests
        }
        for part, load in enumerate(loads):
            units.append((index, file, part, load))
            alone = part_of.count(part) == 1
            whole.append(names[part_of.index(part)] if alone and exact else None)

    sizes = [unit[3] for unit in units]
    most = min(max_number_of_shards, len(units))
    count = min(max(1, math.ceil(sum(sizes) / max_shard_seconds)), most)
    shard_of, loads = _least_loaded(sizes, count)
    while max(loads) > max_shard_seconds and count < most:
        count += 1
        shard_of, loads = _least_loaded(sizes, count)
    # One timed test over the budget sets the step's time whatever the count:
    # the fewest shards that are no slower save the jobs the rest would cost.
    # A shard of several commands keeps to the budget, as packing does: it
    # pays pytest's startup per command, which main's timings leave out.
    longest = max(range(len(sizes)), key=lambda u: sizes[u])
    long_test = None
    if whole[longest] and sizes[longest] > max_shard_seconds:
        long_test = {"name": whole[longest], "seconds": sizes[longest]}
        slowest = max(loads)
        for fewer in range(1, count):
            fewer_shard_of, fewer_loads = _least_loaded(sizes, fewer)
            mixed = _mixed_within(units, fewer_shard_of, fewer_loads, max_shard_seconds)
            if max(fewer_loads) <= slowest and mixed:
                count, shard_of, loads = fewer, fewer_shard_of, fewer_loads
                break

    planned: List[Dict] = [{"files": {}, "split": {}} for _ in commands]
    for (index, file, part, _), shard in zip(units, shard_of):
        if part is None:
            planned[index]["files"][file] = shard
            continue
        split = planned[index]["split"].setdefault(
            file, {"shards": {}, "assign": assigns[(index, file)]}
        )
        split["shards"][str(part)] = shard
    return {
        "shards": count,
        "estimates": [round(load, 1) for load in loads],
        "commands": planned,
        "files": len(keys),
        "unknownFiles": unknown_files,
        "unknownFileSeconds": round(unknown_seconds, 1),
        "filteredUnknownFiles": filtered_unknown_files,
        "oversizedFiles": oversized,
        "overBudget": max(loads) > max_shard_seconds,
        "longTest": long_test,
        # Fewer shards would be slower or mix commands past the budget.
        "fewest": bool(long_test)
        and _mixed_within(units, shard_of, loads, max_shard_seconds),
        "timingSource": {
            k: (timings or {}).get(k) for k in ("buildNumber", "buildNumbers")
        },
    }


def summary(step_key: str, result: Optional[Dict], reason: str = "") -> str:
    """One paragraph on a plan, for the generator's log and annotations."""
    if result is None:
        return (
            f"**Runtime sharding for `{step_key}`:** no plan ({reason}). "
            "The step runs as one job, as it would without sharding."
        )
    if result["shards"] == 1 and not result["overBudget"]:
        return (
            f"**Runtime sharding for `{step_key}`:** its {result['files']} files "
            f"fit in one job ({result['estimates'][0] / 60:.1f} min of tests)."
        )
    estimates = ", ".join(f"{s / 60:.1f}" for s in result["estimates"])
    source = result["timingSource"]
    builds = len(source.get("buildNumbers") or [source.get("buildNumber")])
    text = (
        f"**Runtime sharding for `{step_key}`:** {result['shards']} shards for "
        f"{result['files']} files; estimated test time per shard (min): "
        f"{estimates}. Timings: median of {builds} main build"
        f"{'s' if builds != 1 else ''}, the newest {source.get('buildNumber')}."
    )
    if result["unknownFiles"]:
        text += (
            f" No timing, counted as {result['unknownFileSeconds']:.0f} s each: "
            + ", ".join(f"`{f}`" for f in result["unknownFiles"])
            + "."
        )
    if result.get("filteredUnknownFiles"):
        text += (
            f" No timing under a -m/-k filter, counted as "
            f"{MIN_UNKNOWN_FILE_SECONDS} s each: "
            + ", ".join(f"`{f}`" for f in result["filteredUnknownFiles"])
            + "."
        )
    if result["oversizedFiles"]:
        files = ", ".join(f"`{f}`" for f in result["oversizedFiles"])
        text += f" Split by test: {files}."
    long_test = result.get("longTest")
    if result["overBudget"] and long_test:
        minutes = f"{long_test['seconds'] / 60:.1f} min"
        shards = f"{result['shards']} shard{'s' if result['shards'] != 1 else ''}"
        text += (
            f" :warning: `{long_test['name']}` takes {minutes} on its own, over "
            f"the {MAX_SHARD_SECONDS / 60:.0f}-min target. One test can't be "
            f"split across shards, so its shard needs at least {minutes} "
            f"whatever the shard count."
        )
        if result["fewest"]:
            text += (
                f" {shards} is the fewest that keep every shard within "
                f"{max(result['estimates']) / 60:.1f} min"
            )
            if len(result["commands"]) > 1:
                text += (
                    " and any shard running several commands within "
                    f"{MAX_SHARD_SECONDS / 60:.0f} min"
                )
            text += "."
    elif result["overBudget"]:
        text += (
            f" :warning: At {result['shards']} shards, the most allowed, a shard "
            f"is still over {MAX_SHARD_SECONDS / 60:.0f} min of test time."
        )
    return text


def plan_step(
    step_key: str,
    commands: List[str],
    working_dir: Optional[str],
    root: str,
    queue: AgentQueue,
) -> Tuple[Optional[Dict], str]:
    """Plan an enrolled step from the checkout at `root`.

    Args:
        step_key: The step's key, for its timings.
        commands: The step's commands, as its YAML writes them.
        working_dir: The step's working_dir in the image.
        root: The vLLM checkout the generator runs in.
        queue: The queue the step runs on, to pick its timings.

    Returns:
        (the plan for runtime_shard_plugin.py, or None to run the step as one
        job; a summary of either for the log and annotations).

    """
    reason = ""
    result = None
    try:
        setup, tests = split_commands(commands)
        cwd = root
        if working_dir:
            workspace = CONTAINER_WORKSPACE.rstrip("/")
            if not (working_dir.rstrip("/") + "/").startswith(workspace + "/"):
                raise ValueError(f"working_dir {working_dir} is outside the checkout")
            cwd = os.path.join(root, os.path.relpath(working_dir, workspace))
        labels = command_labels(commands)
        discovered = [
            {
                "command": c,
                "span_label": labels[len(setup) + index],
                **discover(c, cwd, root),
            }
            for index, c in enumerate(tests)
        ]
        if not any(entry["files"] for entry in discovered):
            raise ValueError("no test files found")
        result = plan(discovered, fetch_timings(step_key, queue))
        if result is None:
            reason = "main has no timing for the step's files"
        else:
            prefix = os.path.relpath(cwd, root).replace(os.sep, "/")
            prefix = "" if prefix == "." else prefix + "/"
            for index, (entry, planned) in enumerate(
                zip(discovered, result["commands"])
            ):
                _, words = env_and_words(entry["command"])
                position = len(setup) + index
                planned["args"] = words[1:]
                planned["prefix"] = prefix
                planned["label"] = f"Command ({position + 1}/{len(commands)})"
                planned["position"] = position
                # A test the plan doesn't name runs in the first shard that
                # runs the command at all.
                planned["fallback"] = owners(planned)[0]
    except Exception as error:  # never block the step on its sharding
        result, reason = None, str(error)
    return result, summary(step_key, result, reason)


def owners(planned: Dict) -> List[int]:
    """The shards that run any of a planned command's tests."""
    shards = set(planned["files"].values())
    for split in planned["split"].values():
        shards.update(split["shards"].values())
    return sorted(shards)


def guard(command: str, planned: Dict, shards: int) -> str:
    """A job's command, run only by the shards that have its tests. The others
    say where they run instead of starting pytest to deselect everything."""
    runs = owners(planned)
    if len(runs) == shards:
        return command
    where = ", ".join(str(shard + 1) for shard in runs)
    return (
        f'case "$${{BUILDKITE_PARALLEL_JOB:-0}}" in\n'
        f"{'|'.join(str(shard) for shard in runs)})\n"
        f"{command}\n"
        ";;\n"
        f'*) echo "runtime-shard: this shard has no tests of this command;'
        f' they run in shard {where}" ;;\n'
        "esac"
    )


def plugin_source() -> str:
    """runtime_shard_plugin.py, packed for a shard job's environment."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), PLUGIN_MODULE)
    with open(path + ".py") as f:
        return base64.b64encode(zlib.compress(f.read().encode())).decode()


def shard_job_setup() -> List[str]:
    """Commands that install the plugin in a shard job, before its own. $$ is
    a literal $ in the uploaded pipeline."""
    unpack_plugin = (
        "import base64, os, sys, zlib; sys.stdout.write(zlib.decompress("
        f"base64.b64decode(os.environ['{PLUGIN_ENV}'])).decode())"
    )
    return [
        f'mkdir -p {PLUGIN_DIR} && python3 -c "{unpack_plugin}"'
        f" > {PLUGIN_DIR}/{PLUGIN_MODULE}.py",
        # On the path of whichever python runs pytest (a venv's included).
        f'export PYTHONPATH="{PLUGIN_DIR}$${{PYTHONPATH:+:$$PYTHONPATH}}"',
    ]


def annotate(step_key: str, text: str, style: str) -> None:
    """Best effort: outside Buildkite there is no agent to annotate with."""
    if not os.getenv("BUILDKITE"):
        return
    subprocess.run(
        [
            "buildkite-agent",
            "annotate",
            "--context",
            f"runtime-shard-{step_key}",
            "--style",
            style,
            text,
        ],
        check=False,
    )
