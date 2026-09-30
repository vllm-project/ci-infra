"""Runtime sharding for steps enrolled with `automatic_shard: true`.

One stdlib-only file (Python 3.9: the small CPU queue's python3), because the
collect and plan steps fetch it with curl from the ci-infra branch in use,
like the recorders' scripts:

  collect  runs in the step's own test image after its setup commands. It
           imports PR code, so it only writes the collected node IDs of one
           pytest command to the checkout, for the agent to upload.
  plan     runs on a CPU agent. It reads those lists and main's timings for
           the step, packs whole files into contiguous shards, checks every
           test lands exactly once and annotates the build. Shadow mode: the
           step itself still runs as one job.

Planning never fails a build: anything unexpected becomes an annotation.
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
ROOM_SECONDS = 1200  # test time per shard; setup is not counted
UNKNOWN_SECONDS = 150  # a file main has no timing for
CEILING = 6
NO_TIMING_SHARDS = 4
INVENTORY_DIR = ".runtime-shard"
# pytest-shard would select a subset a second time on top of the plan.
_UNSHARDABLE_ARGS = ("--num-shards", "--shard-id")
_SHELL_SYNTAX = ("&&", "||", ";", "|", ">", "<", "`", "$", "\n")


def command_preview(command: str) -> str:
    """The label main's timing spans carry for a command: the same preview the
    generator echoes before running it (see _prepare_commands)."""
    return command[:80].replace("'", "").replace('"', "").replace("$", "")


def split_commands(commands: List[str]) -> Optional[Tuple[List[str], List[str]]]:
    """(setup, pytest commands), or None if the step can't be split by file:
    it needs at least one pytest command and nothing but plain pytest
    commands after the first one."""
    first = next((i for i, c in enumerate(commands) if c.startswith("pytest ")), None)
    if first is None:
        return None
    tests = commands[first:]
    for command in tests:
        if not command.startswith("pytest ") or any(
            s in command for s in _SHELL_SYNTAX
        ):
            return None
        if any(a.split("=")[0] in _UNSHARDABLE_ARGS for a in shlex.split(command)):
            return None
    return commands[:first], tests


def encode(value) -> str:
    return base64.b64encode(json.dumps(value).encode()).decode()


def decode(text: str):
    return json.loads(base64.b64decode(text))


def contiguous(sizes: List[float], room: float, ceiling: int) -> List[List[int]]:
    """Fewest runs of consecutive units that each fit room (at most ceiling),
    then the smallest largest-load for that count. Returns index lists."""

    def cut(limit: float) -> List[List[int]]:  # greedy fill: fewest runs at this limit
        shards: List[List[int]] = [[]]
        load = 0.0
        for i, size in enumerate(sizes):
            if shards[-1] and load + size > limit:
                shards.append([])
                load = 0.0
            shards[-1].append(i)
            load += size
        return shards

    n = min(len(cut(room)), ceiling)
    lo, hi = max(sizes), sum(sizes)
    for _ in range(60):  # bisect the largest load that still gives <= n runs
        mid = (lo + hi) / 2
        if len(cut(mid)) <= n:
            hi = mid
        else:
            lo = mid
    return cut(hi)


def plan(
    inventory: List[Dict],
    timings: Optional[Dict],
    room: float = ROOM_SECONDS,
    unknown: float = UNKNOWN_SECONDS,
    ceiling: int = CEILING,
) -> Dict:
    """Shards for one step.

    inventory: one entry per pytest command, in order: {"command": the
      step's command, "prefix": its working dir relative to pytest's rootdir,
      "nodeids": the collected IDs, in collection order}.
    timings: the timing endpoint's response for the step, or None.

    Whole files are the unit, kept inside their own command and in collection
    order. Only a file bigger than room is split, into consecutive runs of its
    tests. Every shard's targets are relative to the command's working dir.
    """
    seconds = {}
    for f in (timings or {}).get("files", []):
        if f["timingStatus"] != "skip_only":
            seconds[(f["command"], f["file"])] = f["observedMs"] / 1000

    units = []  # [command index, file, node IDs, seconds]
    unknown_files, oversized = [], []
    for c, entry in enumerate(inventory):
        for nodeid in entry["nodeids"]:
            file = nodeid.split("::")[0]
            if units and units[-1][0] == c and units[-1][1] == file:
                units[-1][2].append(nodeid)
            else:
                units.append([c, file, [nodeid], None])
    for unit in units:
        unit[3] = seconds.get((command_preview(inventory[unit[0]]["command"]), unit[1]))
        if unit[3] is None:
            unit[3] = unknown
            unknown_files.append(unit[1])

    if timings is None:  # equal file counts, as a safe default
        k = min(NO_TIMING_SHARDS, len(units))
        shards = [
            list(range(i * len(units) // k, (i + 1) * len(units) // k))
            for i in range(k)
        ]
        sizes = [0.0] * len(units)
        unknown_files = []
    else:
        split = []
        for c, file, nodeids, size in units:
            parts = min(math.ceil(size / room), len(nodeids))
            if parts > 1:
                oversized.append(file)
            for p in range(parts):  # ponytail: tests share the file's time evenly
                chunk = nodeids[
                    p * len(nodeids) // parts : (p + 1) * len(nodeids) // parts
                ]
                split.append([c, file, chunk, size / parts, parts > 1])
        units = split
        sizes = [u[3] for u in units]
        shards = contiguous(sizes, room, ceiling)

    result = []
    for shard in shards:
        commands: List[Dict] = []
        for i in shard:
            c, file, nodeids = units[i][:3]
            whole = timings is None or not units[i][4]
            prefix = (
                inventory[c]["prefix"].rstrip("/") + "/"
                if inventory[c]["prefix"]
                else ""
            )
            targets = [file] if whole else nodeids
            targets = [t[len(prefix) :] if t.startswith(prefix) else t for t in targets]
            if not commands or commands[-1]["index"] != c:
                commands.append({"index": c, "targets": [], "tests": 0})
            commands[-1]["targets"] += targets
            commands[-1]["tests"] += len(nodeids)
        result.append(
            {
                "estimateSeconds": round(sum(sizes[i] for i in shard), 1)
                if timings
                else None,
                "commands": commands,
            }
        )
    return {
        "shards": result,
        "tests": sum(len(e["nodeids"]) for e in inventory),
        "files": len({(u[0], u[1]) for u in units}),
        "timingSource": None
        if timings is None
        else {k: timings.get(k) for k in ("buildNumber", "commit", "finishedAt")},
        "unknownFiles": unknown_files,
        "oversizedFiles": oversized,
        "overBudget": bool(timings)
        and any(s["estimateSeconds"] > room for s in result),
        "rules": {
            "packing": "contiguous",
            "roomSeconds": room,
            "unknownSeconds": unknown,
            "ceiling": ceiling,
        },
    }


def check(result: Dict, inventory: List[Dict]) -> None:
    """Every collected test is assigned exactly once, and nothing else is."""
    assigned: Dict[Tuple[int, str], int] = {}
    for shard in result["shards"]:
        for command in shard["commands"]:
            entry = inventory[command["index"]]
            prefix = entry["prefix"].rstrip("/") + "/" if entry["prefix"] else ""
            for target in command["targets"]:
                full = prefix + target
                matched = [
                    n
                    for n in entry["nodeids"]
                    if n == full or ("::" not in full and n.split("::")[0] == full)
                ]
                if not matched:
                    raise ValueError("target matches no collected test: " + target)
                for n in matched:
                    key = (command["index"], n)
                    assigned[key] = assigned.get(key, 0) + 1
    expected = {(c, n) for c, e in enumerate(inventory) for n in e["nodeids"]}
    if set(assigned) != expected or any(v != 1 for v in assigned.values()):
        raise ValueError("plan does not assign every collected test exactly once")


def annotation(step_key: str, result: Dict) -> str:
    shards = result["shards"]
    lines = [
        f"**Runtime sharding (shadow) for `{step_key}`:** would run as "
        f"**{len(shards)} shard{'s' if len(shards) != 1 else ''}** "
        f"({result['tests']} tests in {result['files']} files, ceiling "
        f"{result['rules']['ceiling']}). This build still ran the step as one job."
    ]
    source = result["timingSource"]
    if source:
        estimates = ", ".join(f"{s['estimateSeconds'] / 60:.1f}" for s in shards)
        lines.append(
            f"Estimated test time per shard (min): {estimates}. Timings from "
            f"main build {source['buildNumber']} (`{(source['commit'] or '')[:12]}`)."
        )
    else:
        lines.append(
            f"No main timings for this step: {len(shards)} shards with equal file counts."
        )
    if result["unknownFiles"]:
        lines.append(
            f"No timing, counted as {result['rules']['unknownSeconds'] / 60:.1f} min each: "
            + ", ".join(f"`{f}`" for f in result["unknownFiles"])
        )
    if result["oversizedFiles"]:
        lines.append(
            "Split by tests (file alone exceeds one shard): "
            + ", ".join(f"`{f}`" for f in result["oversizedFiles"])
        )
    if result["overBudget"]:
        lines.append(
            ":warning: At the ceiling, a shard is still over "
            f"{result['rules']['roomSeconds'] / 60:.0f} min of test time."
        )
    rows = ["| Shard | Tests | Estimate (min) | Targets |", "|---|---|---|---|"]
    for n, s in enumerate(shards, 1):
        targets = sum(len(c["targets"]) for c in s["commands"])
        est = "" if s["estimateSeconds"] is None else f"{s['estimateSeconds'] / 60:.1f}"
        rows.append(
            f"| {n} | {sum(c['tests'] for c in s['commands'])} | {est} | {targets} |"
        )
    return "\n\n".join(lines) + "\n\n" + "\n".join(rows)


def fetch_timings(step_key: str) -> Optional[Dict]:
    url = TIMINGS_URL + "?" + urllib.parse.urlencode({"stepKey": step_key})
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return json.load(response)
    except Exception as error:  # 404 (no passing main build), outage: fall back
        print(f"runtime-shard: no timings ({error})", file=sys.stderr)
        return None


def run_collect(index: str, command_b64: str, out_dir: str) -> None:
    """In the test image: collect one pytest command's selected node IDs."""
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
            [*shlex.split(command)[1:], "--collect-only", "-q"], plugins=[Probe()]
        )
    )
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, f"inventory-{index}.json"), "w") as f:
        json.dump(
            {"index": int(index), "command": command, "exitstatus": status, **found}, f
        )
    if status != 0 or not found["nodeids"]:
        raise SystemExit(f"runtime-shard: collection failed (pytest exit {status})")


def run_plan(step_key: str, commands_b64: str) -> None:
    """On a CPU agent: plan from the collect step's artifacts and annotate."""
    commands = decode(commands_b64)
    context = f"runtime-shard-{step_key}"
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
        message, style = annotation(step_key, result), "info"
    except Exception as error:
        message = (
            f"**Runtime sharding (shadow) for `{step_key}`:** no plan ({error}). "
            "The step ran as one job, as it would without sharding."
        )
        style = "warning"
    print(message)
    subprocess.run(
        [
            "buildkite-agent",
            "annotate",
            "--context",
            context,
            "--style",
            style,
            message,
        ],
        check=False,
    )


if __name__ == "__main__":
    mode, *args = sys.argv[1:]
    {"collect": run_collect, "plan": run_plan}[mode](*args)
