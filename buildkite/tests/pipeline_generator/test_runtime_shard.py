import io
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
import types
import urllib.error

import pytest
import yaml

import buildkite_step
import runtime_shard as rs
import runtime_shard_plugin
from constants import AgentQueue
from step import Step

SETUP = ["apt-get update && apt-get install -y curl", "export PYTHONFAULTHANDLER=1"]
TESTS = [
    "pytest -v -s model_executor -m '(not slow_test)' --timeout=900",
    "pytest -v -s entrypoints/test_tensorizer_entrypoint.py --timeout=900",
]


def _timings(command, seconds):
    """The timing endpoint's response: main's median per file (repo-relative)."""
    return {
        "buildNumber": 7,
        "buildNumbers": [7, 6],
        "files": [
            {
                "command": rs.command_preview(command),
                "file": f"tests/{f}",
                "observedMs": s * 1000,
                "timingStatus": "passed",
            }
            for f, s in seconds.items()
        ],
    }


def _with_tests(timings, file, test_seconds):
    """Attach per-test medians to one file of a _timings() response."""
    for entry in timings["files"]:
        if entry["file"] == f"tests/{file}":
            entry["tests"] = [
                {"nodeid": f"tests/{file}::{name}", "observedMs": s * 1000}
                for name, s in test_seconds.items()
            ]
    return timings


def _commands(command, files, selected=()):
    return [
        {
            "command": command,
            "files": [f"tests/{f}" for f in files],
            "selected": [f"tests/{f}" for f in selected],
        }
    ]


# Planning


def test_split_commands():
    assert rs.split_commands(SETUP + TESTS) == (SETUP, TESTS)
    assert rs.split_commands(SETUP) is None
    assert rs.split_commands([TESTS[0], "echo done"]) is None
    assert rs.split_commands([TESTS[0] + " && echo done"]) is None
    assert rs.split_commands(["pytest -v x --shard-id=1 --num-shards=2"]) is None
    # A command's program decides, not its first word or a substring.
    with_env = "TP_SIZE=1 DP_SIZE=2 pytest -v -s a.py"
    assert rs.split_commands(["export A=1", with_env, *TESTS]) == (
        ["export A=1"],
        [with_env, *TESTS],
    )
    pip = "pip install pytest-timeout pytest-forked"
    assert rs.split_commands([pip, *TESTS]) == ([pip], TESTS)
    # torchrun in setup would run in full in every shard.
    torchrun = "VLLM_TEST_SAME_HOST=1 torchrun --nproc-per-node=2 a.py"
    assert rs.split_commands([torchrun, *TESTS]) is None
    # Only the shell can expand $.
    assert rs.split_commands(["PYTHONPATH=$PWD pytest -v a.py"]) is None


def _tree(root, names):
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_x(): pass\n")


def test_discover_finds_the_files_pytest_would(tmp_path):
    _tree(
        tmp_path,
        [
            "tests/pkg/test_a.py",
            "tests/pkg/b_test.py",
            "tests/pkg/sub/test_c.py",
            "tests/pkg/conftest.py",
            "tests/pkg/helpers.py",
            "tests/pkg/.hidden/test_h.py",
            "tests/pkg/skip/test_s.py",
            "tests/pkg/test_glob_skip.py",
            "tests/other/test_o.py",
            "tests/one/test_one.py",
        ],
    )
    cwd = str(tmp_path / "tests")
    found = rs.discover(
        "N=1 pytest -v -s pkg -m 'not slow' -k fast --ignore=pkg/skip "
        "--ignore-glob '*glob*' --timeout 900 one/test_one.py::test_x",
        cwd,
        str(tmp_path),
    )
    assert found["files"] == [
        "tests/pkg/b_test.py",
        "tests/pkg/test_a.py",
        "tests/pkg/sub/test_c.py",
        "tests/one/test_one.py",
    ]
    assert found["selected"] == ["tests/one/test_one.py"]
    with pytest.raises(ValueError, match="not in the checkout"):
        rs.discover("pytest -v missing", cwd, str(tmp_path))
    with pytest.raises(ValueError, match="no test path"):
        rs.discover("pytest -v -m slow", cwd, str(tmp_path))


def test_plan_packs_the_largest_files_first_into_the_fewest_shards():
    seconds = {"a.py": 100, "b.py": 600, "c.py": 300, "d.py": 500, "e.py": 200}
    seconds["f.py"] = 400
    timings = dict(_timings(TESTS[0], seconds), setupMs=120_000)
    result = rs.plan(_commands(TESTS[0], seconds), timings)
    # After 120 s of setup, 2100 s doesn't fit two shards of 1020 s of tests;
    # three, evenly.
    assert result["shards"] == 3 and result["estimates"] == [700, 700, 700]
    files = result["commands"][0]["files"]
    assert {f for f, s in files.items() if s == files["tests/b.py"]} == {
        "tests/a.py",
        "tests/b.py",
    }
    assert not result["overBudget"] and result["unknownFiles"] == []


def test_plan_charges_setup_up_to_the_limit_and_flags_more():
    seconds = {f"t{i}.py": 475 for i in range(4)}  # 1900 s of tests

    def planned(setup_seconds):
        timings = dict(_timings(TESTS[0], seconds), setupMs=setup_seconds * 1000)
        return rs.plan(_commands(TESTS[0], seconds), timings)

    # Two shards of 950 s fit 1140 s with up to 190 s of setup each.
    assert planned(0)["shards"] == 2 and planned(180)["shards"] == 2
    assert not rs.setup_too_long(planned(180))
    assert ":warning:" not in rs.summary("k", planned(180))
    # Charged in full, 600 s would leave 540 s per shard: four shards. It is
    # charged as 180 s instead, and flagged.
    slow = planned(600)
    assert slow["shards"] == 2 and slow["setupSeconds"] == 600
    assert rs.setup_too_long(slow)
    assert "Setup takes 10.0 min per job" in rs.summary("k", slow)
    # A step without setupMs (an older endpoint) charges none.
    assert rs.plan(_commands(TESTS[0], seconds), _timings(TESTS[0], seconds))[
        "setupSeconds"
    ] == 0


def test_a_one_job_plan_flags_a_long_setup_too():
    seconds = {"a.py": 300}
    timings = dict(_timings(TESTS[0], seconds), setupMs=240_000)
    result = rs.plan(_commands(TESTS[0], seconds), timings)
    assert result["shards"] == 1
    assert "Setup takes 4.0 min per job" in rs.summary("k", result)


def test_plan_of_a_step_that_fits_in_one_job():
    seconds = {"a.py": 100, "b.py": 200}
    result = rs.plan(_commands(TESTS[0], seconds), _timings(TESTS[0], seconds))
    assert result["shards"] == 1 and result["estimates"] == [300]
    assert "fit in one job" in rs.summary("k", result)


def test_plan_without_timings_runs_the_step_as_one_job():
    """With nothing to split by, a split would be a guess that costs GPU jobs:
    the endpoint is down, or main has no timing for any of the step's files."""
    commands = _commands(TESTS[0], [f"t{i}.py" for i in range(8)])
    other_files = _timings(TESTS[0], {"gone.py": 600})
    for timings in (None, other_files, dict(other_files, files=[])):
        assert rs.plan(commands, timings) is None
    assert "runs as one job" in rs.summary("k", None, "main has no timing")


def test_plan_counts_an_unknown_file_as_the_steps_median_file():
    commands = _commands(TESTS[0], ["a.py", "b.py", "c.py", "new.py"])
    timings = _timings(TESTS[0], {"a.py": 10, "b.py": 20, "c.py": 90})
    result = rs.plan(commands, timings)
    assert result["unknownFiles"] == ["tests/new.py"]
    assert result["unknownFileSeconds"] == 20 and result["estimates"] == [140]
    small = _timings(TESTS[0], {"a.py": 1, "b.py": 2})
    assert rs.plan(commands, small)["unknownFileSeconds"] == 5  # the floor


def test_plan_counts_an_untimed_file_under_a_filter_at_the_floor():
    """`-m hybrid_model` over a directory: main timed the files the marker
    selects tests in, and the others most likely have none to run."""
    command = "pytest -v -s models -m hybrid_model -k 'not granite'"
    files = ["hybrid.py", "mamba.py", "gemma.py", "phimoe.py"]
    timed = {"hybrid.py": 900, "mamba.py": 900}
    result = rs.plan(_commands(command, files), _timings(command, timed))
    assert result["unknownFiles"] == []
    assert result["filteredUnknownFiles"] == ["tests/gemma.py", "tests/phimoe.py"]
    assert result["shards"] == 2 and sorted(result["estimates"]) == [905, 905]
    assert "under a -m/-k filter, counted as 5 s each" in rs.summary("k", result)
    # Only excluding tests ("not slow_test", above) keeps most files running.
    assert not rs._picks_tests("pytest x -m '(not slow_test)' -k 'not granite'")
    assert rs._picks_tests("pytest x -mcore_model") and rs._picks_tests("pytest -k=a x")


def test_plan_finds_main_timings_after_a_pr_edits_the_command():
    """An edited command has a different label but keeps its file timings
    when each file has only one current and historical command."""
    files = {f"t{i}.py": 500 for i in range(4)}
    edited = TESTS[0].replace("pytest -v -s", "pytest -v -s -x")
    result = rs.plan(_commands(edited, files), _timings(TESTS[0], files))
    assert result["unknownFiles"] == [] and result["estimates"] == [1000, 1000]


def test_plan_tells_apart_commands_that_run_the_same_file():
    one, two = TESTS[0], TESTS[0] + " -k fast"
    timings = _timings(one, {"a.py": 900})
    timings["files"] += _timings(two, {"a.py": 30})["files"]
    commands = _commands(one, ["a.py"]) + _commands(two, ["a.py"])
    assert rs.plan(commands, timings)["estimates"] == [930]
    # Neither preview matches: no telling which time is this command's.
    edited = one.replace("pytest -v -s", "pytest -v -s -x")
    commands = _commands(edited, ["a.py"]) + _commands(two, ["a.py"])
    assert rs.plan(commands, timings)["unknownFiles"] == ["tests/a.py"]


def test_command_labels_are_full_commands_numbered_when_identical():
    long = "pytest -s -v evals/gsm8k/test_gsm8k_correctness.py --config-list-file=evals/gsm8k/"
    fp8, int8, other = long + "fp8.txt", long + "int8.txt", "pytest -v -s a.py"
    assert fp8[:80] == int8[:80]
    first, unrelated, second, repeated = rs.command_labels([fp8, other, int8, fp8])
    assert first != second
    assert repeated == first + " #2"
    assert unrelated == rs.command_preview(other)
    # Distinct commands keep their labels when reordered or setup is inserted.
    assert rs.command_labels(["export A=1", int8, fp8])[1:] == [second, first]
    assert rs.command_labels([fp8] * 3) == [first, first + " #2", first + " #3"]


def test_command_preview_is_the_whole_command_as_echoed():
    assert rs.command_preview("pytest 'a.py' $FLAGS") == "pytest a.py FLAGS"
    assert rs.command_preview("pytest a.py \\\n  -k fast") == "pytest a.py \\   -k fast"
    long = "pytest -v -s " + "x" * 200
    assert rs.command_preview(long) == long  # not cut at 80 characters
    # A command of 80 characters or fewer keeps the label main already has.
    short = "pytest -v -s 'tests/a.py'"
    assert rs.command_preview(short) == rs._legacy_preview(short)
    assert rs.command_preview("pytest tests/你好.py") == "pytest tests/你好.py"


def test_plan_does_not_reuse_one_legacy_timing_for_two_current_commands():
    """A shared old label may pool two commands' cases; it cannot seed both."""
    one, two = TESTS[0], TESTS[0] + " -k fast"
    commands = _commands(one, ["a.py", "b.py"]) + _commands(two, ["a.py"])
    timings = _timings(one, {"a.py": 900, "b.py": 30})
    for timing in timings["files"]:
        timing["command"] = "old preview"
    result = rs.plan(commands, timings)
    assert result["unknownFiles"] == ["tests/a.py"]
    assert result["filteredUnknownFiles"] == ["tests/a.py"]
    assert result["estimates"] == [65]


def test_plan_reads_distinct_legacy_labels_for_a_shared_file():
    commands = _commands("pytest a.py", ["a.py"]) + _commands(
        "pytest a.py -k fast", ["a.py"]
    )
    timings = _timings("pytest a.py", {"a.py": 900})
    timings["files"][0]["command"] = "pytest a.py"
    timings["files"] += [
        dict(timings["files"][0], command="pytest a.py -k fast", observedMs=30000)
    ]
    result = rs.plan(commands, timings)
    assert result["unknownFiles"] == [] and result["estimates"] == [930]


def test_plan_rejects_a_legacy_preview_shared_by_current_commands():
    base = "pytest " + "-v " * 30 + "a.py"
    one, two = base + " -k first", base + " -k second"
    timings = _timings(one, {"a.py": 900})
    timings["files"][0]["command"] = one[:80]
    commands = _commands(one, ["a.py"]) + _commands(two, ["a.py"])
    assert rs.plan(commands, timings) is None


def test_plan_prefers_the_full_label_over_its_old_prefix_during_rollout():
    """Main's runs carry a long command's old 80-character label and its full
    one; a PR that edits the command reads the full one's timing."""
    full = "pytest " + "-v " * 30 + "a.py"
    timings = _timings(full, {"a.py": 30})
    timings["files"] += [
        dict(timings["files"][0], command=full[:80], observedMs=900000)
    ]
    commands = _commands(full + " -x", ["a.py"])
    result = rs.plan(commands, timings)
    assert result["unknownFiles"] == [] and result["estimates"] == [30]


def test_plan_matches_a_multiline_command_s_old_label_to_its_full_one():
    """A multi-line command's old label kept its line breaks, which the full
    label turns into spaces: they are still one command. A PR that replaces
    it, or edits it past 80 characters, reads the full label's timing."""
    case = "set -eu\ncase $BUILDKITE_PARALLEL_JOB in\n  0)\n    " + " ".join(
        f"pytest -v -s tests/t{i}.py" for i in range(4)
    )
    old = rs._legacy_preview(case)
    assert "\n" in old and old != rs.command_preview(old)
    timings = _timings(case, {"a.py": 30})
    timings["files"] += [dict(timings["files"][0], command=old, observedMs=900000)]
    result = rs.plan(_commands("pytest -v -s a.py", ["a.py"]), timings)
    assert result["unknownFiles"] == [] and result["estimates"] == [30]
    result = rs.plan(_commands(case + " -x", ["a.py"]), timings)
    assert result["unknownFiles"] == [] and result["estimates"] == [30]


def test_plan_does_not_read_a_shared_old_prefix_for_either_command():
    """Two long commands that main ran under one 80-character label: that
    timing pools both, so neither reads it."""
    base = "pytest " + "-v " * 30 + "a.py"
    one, two = base + " --tb=short", base + " --tb=long"
    commands = _commands(one, ["a.py"]) + _commands(two, ["a.py"])
    timings = _timings(two, {"a.py": 30})
    timings["files"] += [dict(timings["files"][0], command=one[:80], observedMs=900000)]
    result = rs.plan(commands, timings)
    assert result["unknownFiles"] == ["tests/a.py"]
    assert result["estimates"] == [60]


def test_plan_step_tells_apart_two_identical_commands(tmp_path, monkeypatch):
    _project(tmp_path, {"pkg/test_a.py": "def test_1(): pass\n"})
    command = "pytest -v pkg/test_a.py"
    first, second = rs.command_labels(["export A=1", command, command])[1:]
    timings = _timings(command, {"pkg/test_a.py": 900})
    timings["files"] += _timings(command, {"pkg/test_a.py": 30})["files"]
    timings["files"][1]["command"] = second
    monkeypatch.setattr(rs, "fetch_timings", lambda key, queue: timings)
    result, _ = rs.plan_step(
        "k",
        ["export A=1", command, command],
        "/vllm-workspace/tests",
        str(tmp_path),
        AgentQueue.H200,
    )
    assert result["estimates"] == [930]
    assert [c["position"] for c in result["commands"]] == [1, 2]
    assert [c["label"] for c in result["commands"]] == [
        "Command (2/3)",
        "Command (3/3)",
    ]


def test_plan_step_reads_each_commands_timing_when_their_previews_match(
    tmp_path, monkeypatch
):
    """Full labels select each command's own timings past 80 characters."""
    _project(tmp_path, {"pkg/test_a.py": "def test_1(): pass\ndef test_2(): pass\n"})
    base = "pytest " + "-v " * 30 + "pkg/test_a.py"
    one, two = base + " -k test_1", base + " -k test_2"
    first, second = rs.command_labels([one, two])
    timings = _timings(one, {"pkg/test_a.py": 900})
    timings["files"] += _timings(two, {"pkg/test_a.py": 30})["files"]
    timings["files"][1]["command"] = second
    monkeypatch.setattr(rs, "fetch_timings", lambda key, queue: timings)
    result, _ = rs.plan_step(
        "k", [one, two], "/vllm-workspace/tests", str(tmp_path), AgentQueue.H200
    )
    assert result["estimates"] == [930]


def test_plan_step_sends_unplanned_tests_to_the_least_loaded_owner(
    tmp_path, monkeypatch
):
    """The fallback shard runs the tests no plan names and collects the
    command's full inventory for coverage; the least-loaded owner pays for
    it, not always shard 0."""
    files = {f"pkg/test_{name}.py": "def test_x(): pass\n" for name in "abc"}
    _project(tmp_path, files)
    command = "pytest -v pkg"
    timings = _timings(
        command, {"pkg/test_a.py": 800, "pkg/test_b.py": 800, "pkg/test_c.py": 100}
    )
    monkeypatch.setattr(rs, "fetch_timings", lambda key, queue: timings)
    result, _ = rs.plan_step(
        "k", [command], "/vllm-workspace/tests", str(tmp_path), AgentQueue.H200
    )
    assert result["estimates"] == [900, 800]
    planned = result["commands"][0]
    assert rs.owners(planned) == [0, 1]
    # The coverage job reads this same field to find the command's inventory.
    assert planned["fallback"] == 1


def test_plan_splits_a_file_over_the_budget_by_test_function():
    timings = _with_tests(
        _timings(TESTS[0], {"big.py": 2000}),
        "big.py",
        {"test_a[1]": 500, "test_a[2]": 500, "test_b": 700, "test_c": 300},
    )
    result = rs.plan(_commands(TESTS[0], ["big.py"]), timings)
    assert result["shards"] == 2 and result["estimates"] == [1000, 1000]
    assert result["oversizedFiles"] == ["tests/big.py"]
    split = result["commands"][0]["split"]["tests/big.py"]
    assign = {n.split("::")[1]: p for n, p in split["assign"].items()}
    # A function's cases share setup that main paid once: they stay together.
    assert assign["test_a[1]"] == assign["test_a[2]"] != assign["test_b"]
    assert assign["test_b"] == assign["test_c"]
    assert sorted(split["shards"].values()) == [0, 1]
    assert result["commands"][0]["files"] == {}


def test_plan_cuts_a_function_over_the_budget_by_case():
    # Two of these cases together are over the budget, so each is its own part.
    cases = {f"test_x[{i}]": 700 for i in range(3)}
    timings = _with_tests(_timings(TESTS[0], {"big.py": 2100}), "big.py", cases)
    result = rs.plan(_commands(TESTS[0], ["big.py"]), timings)
    assign = result["commands"][0]["split"]["tests/big.py"]["assign"]
    assert len(set(assign.values())) == 3 and result["shards"] == 3
    assert not result["overBudget"]


def test_plan_cuts_a_file_into_parts_that_each_fit():
    # PyTorch Fullgraph's test_basic_correctness.py: four long cases that
    # ceil(total / budget) = 3 parts would force two of into one part over
    # the budget, which no shard count can fit; with the step's small files,
    # the plan then climbed to the most shards.
    cases = {"test_c[a]": 720, "test_c[b]": 666, "test_c[c]": 654, "test_c[d]": 498}
    small = {f"test_{n}.py": 90 for n in ("simple", "toy", "graphs")}
    timings = _with_tests(
        _timings(TESTS[0], {"big.py": 2538, "mm.py": 708, **small}), "big.py", cases
    )
    result = rs.plan(_commands(TESTS[0], ["big.py", "mm.py", *small]), timings)
    split = result["commands"][0]["split"]["tests/big.py"]
    assert len(set(split["assign"].values())) == len(split["shards"]) == 4  # a part per case
    assert not result["overBudget"] and result["shards"] == 5


def test_plan_keeps_a_node_selection_whole_without_matching_command_timings():
    timings = _with_tests(
        _timings(TESTS[0], {"big.py": 2000}), "big.py", {"test_a": 1000, "test_b": 1000}
    )
    commands = _commands(
        TESTS[0] + "::test_a", ["big.py", "small.py"], selected=["big.py"]
    )
    result = rs.plan(commands, timings)
    assert result["commands"][0]["split"] == {} and result["oversizedFiles"] == []


def test_plan_gives_a_long_step_up_to_ten_shards():
    files = {f"t{i}.py": 1000 for i in range(10)}
    result = rs.plan(_commands(TESTS[0], files), _timings(TESTS[0], files))
    assert result["shards"] == 10 and not result["overBudget"]


def test_plan_of_a_step_that_needs_sixteen_shards_stops_at_ten():
    # 16 files of nearly a full shard each: 16 shards to fit the budget.
    files = {f"t{i:02}.py": 1000 for i in range(16)}
    result = rs.plan(_commands(TESTS[0], files), _timings(TESTS[0], files))
    assert result["shards"] == 10 and result["overBudget"]
    assert sorted(result["estimates"]) == [1000.0] * 4 + [2000.0] * 6
    assert sorted(result["commands"][0]["files"]) == [f"tests/{f}" for f in files]
    assert ":warning:" in rs.summary("k", result)


def test_plan_cuts_a_file_too_long_for_the_most_shards_into_one_part_each():
    # 160 tests of 100 s: 15 parts at the budget would put two parts in five
    # of the 10 shards. Cut to the most shards instead, each gets one.
    timings = _with_tests(
        _timings(TESTS[0], {"big.py": 16000}),
        "big.py",
        {f"test_{i:03}": 100 for i in range(160)},
    )
    result = rs.plan(_commands(TESTS[0], ["big.py"]), timings)
    assert result["shards"] == 10 and result["overBudget"]
    assert result["estimates"] == [1600.0] * 10


def test_a_case_over_the_budget_alone_does_not_cut_the_rest_finer():
    # No number of parts fits the long case; the short ones still share one.
    cases = {"test_x[long]": 1200, **{f"test_x[{i}]": 1 for i in range(600)}}
    timings = _with_tests(_timings(TESTS[0], {"big.py": 1800}), "big.py", cases)
    result = rs.plan(_commands(TESTS[0], ["big.py"]), timings)
    assert len(result["commands"][0]["split"]["tests/big.py"]["shards"]) == 2
    assert result["estimates"] == [1200, 600]


def test_plan_at_the_most_shards_keeps_coarser_parts_when_they_are_faster():
    # Cutting a.py finer gives eleven units for ten shards, and two of them
    # share one: slower than its one part over the budget.
    a = {"test_a[0]": 600, "test_a[1]": 600, "test_a[2]": 750, "test_a[3]": 300}
    b = {f"test_b[{i}]": 1050 for i in range(8)}
    timings = _timings(TESTS[0], {"a.py": 2250, "b.py": 8400})
    timings = _with_tests(_with_tests(timings, "a.py", a), "b.py", b)
    result = rs.plan(_commands(TESTS[0], ["a.py", "b.py"]), timings)
    assert result["shards"] == 10 and max(result["estimates"]) == 1200


def test_plan_cuts_many_long_cases_in_one_pass():
    # Cases over half the budget can't share a part: start from their count
    # rather than adding parts one at a time.
    cases = {f"test_x[{i}]": 600 for i in range(800)}
    timings = _with_tests(_timings(TESTS[0], {"big.py": 480000}), "big.py", cases)
    start = time.monotonic()
    result = rs.plan(_commands(TESTS[0], ["big.py"]), timings)
    assert time.monotonic() - start < 1
    assert result["shards"] == 10 and max(result["estimates"]) == 48000


def test_a_part_of_exactly_the_budget_is_not_cut_again():
    cases = {"test_x[0]": 1140, "test_x[1]": 570, "test_x[2]": 570}
    timings = _with_tests(_timings(TESTS[0], {"big.py": 2280}), "big.py", cases)
    result = rs.plan(_commands(TESTS[0], ["big.py"]), timings)
    assert len(result["commands"][0]["split"]["tests/big.py"]["shards"]) == 2
    assert result["shards"] == 2 and not result["overBudget"]


def test_plan_at_the_most_shards_keeps_an_even_share_of_the_file_per_part():
    """Past the most shards' budget, parts of an even share are cut once:
    some run a case over the share, and more parts than shards would double
    up (Basic Models Extra Initialization, one file of 410 cases)."""
    cases = {f"test_x[{i}]": 132 for i in range(101)}
    timings = _with_tests(_timings(TESTS[0], {"big.py": 13332}), "big.py", cases)
    result = rs.plan(_commands(TESTS[0], ["big.py"]), timings)
    assert result["shards"] == 10 and max(result["estimates"]) == 11 * 132


def test_plan_keeps_budget_sized_parts_when_they_balance_better():
    # Cut to an even share of 10 shards, big.py's parts would stack on the
    # 1,000 s files; at the budget, its 15 small parts fill around them.
    seconds = {"big.py": 16000, **{f"f{i}.py": 1000 for i in range(5)}}
    timings = _with_tests(
        _timings(TESTS[0], seconds),
        "big.py",
        {f"test_{i:03}": 100 for i in range(160)},
    )
    result = rs.plan(_commands(TESTS[0], seconds), timings)
    assert result["shards"] == 10 and result["overBudget"]
    assert result["estimates"] == [2100.0] * 10



# Trimming shards one long test makes useless. Each step here has 1 min of
# setup: 18 min of tests per shard.


def _timed(timings):
    timings["setupMs"] = 60_000
    return timings


def test_one_job_over_the_budget_says_so():
    seconds = {"a.py": 1200}
    result = rs.plan(_commands(TESTS[0], seconds), _timed(_timings(TESTS[0], seconds)))
    assert result["shards"] == 1 and result["overBudget"]
    text = rs.summary("k", result)
    assert "fit in one job" not in text and "one job for 1 file;" in text
    assert "`tests/a.py` takes 20.0 min on its own, over the 18.0 min" in text
    assert "The planner cannot split it further" in text
    assert "1 shard is the fewest the planner finds that keep every shard within 20.0 min." in text


def test_a_one_job_plan_without_a_long_test_says_it_is_over():
    # Main timed a.py under another command: an estimate, which can't name it.
    timings = _timed(_timings("pytest a.py", {"a.py": 1200}))
    result = rs.plan(_commands(TESTS[0], ["a.py"]), timings)
    assert result["shards"] == 1 and result["longTest"] is None
    assert rs.summary("k", result).endswith(
        ":warning: At least one job is still over 19 min with its setup."
    )


def test_plan_drops_a_shard_that_cannot_beat_one_long_test():
    """One test alone sets how long the step takes: a third shard would only
    pay for another job, so the rest share the second."""
    timings = _timed(
        _with_tests(
            _timings(TESTS[0], {"big.py": 2184}),
            "big.py",
            {"test_slow": 1116, "test_a": 564, "test_b": 504},
        )
    )
    result = rs.plan(_commands(TESTS[0], ["big.py"]), timings)
    assert result["shards"] == 2 and result["estimates"] == [1116, 1068]
    assert result["overBudget"]
    assert rs.summary("k", result).endswith(
        "Split by test: `tests/big.py`. :warning: `tests/big.py::test_slow` "
        "takes 18.6 min on its own, over the 18.0 min a shard has for tests. "
        "The planner cannot split it further, so its shard needs at least "
        "18.6 min whatever the shard count. 2 shards is the fewest the planner finds that keep "
        "every shard within 18.6 min."
    )


def test_plan_does_not_trim_on_a_command_edited_past_80_characters():
    # Labels carry the whole command, so an edit past 80 characters is a new
    # command: it reads main's timings by file, untrimmed.
    command = "pytest -v -s model_executor " + "--timeout=900 " * 6
    edited = command + " -k 'not test_slow'"
    assert len(command) > 80 and edited[:80] == command[:80]
    tests = {"test_slow": 1116, "test_a": 564, "test_b": 504}
    timings = _timed(_with_tests(_timings(command, {"big.py": 2184}), "big.py", tests))
    result = rs.plan(_commands(edited, ["big.py"]), timings)
    assert result["longTest"] is None and result["shards"] == 3


def test_plan_trims_a_step_whose_commands_share_their_first_80_characters():
    # Full labels tell the two commands apart, so the long test is known to be
    # the first one's.
    command = "pytest -v -s model_executor " + "--timeout=900 " * 6
    other = command + " -x"
    tests = {"test_slow": 1116, "test_a": 564, "test_b": 504}
    timings = _timed(_with_tests(_timings(command, {"big.py": 2184}), "big.py", tests))
    timings["files"] += _timings(other, {"small.py": 60})["files"]
    commands = _commands(command, ["big.py"]) + _commands(other, ["small.py"])
    result = rs.plan(commands, timings)
    assert result["longTest"]["seconds"] == 1116 and result["shards"] == 3


@pytest.mark.parametrize("split", [False, True])
def test_plan_uses_timings_found_by_file_without_trimming(split):
    seconds = {"long.py": 1200, "a.py": 600, "b.py": 600}
    timings = _timings(TESTS[0], seconds)
    if split:
        seconds = {"big.py": 2400}
        timings = _with_tests(
            _timings(TESTS[0], seconds),
            "big.py",
            {"test_slow": 1200, "test_a": 600, "test_b": 600},
        )
    for timing in timings["files"]:
        timing["command"] = "pytest -v -s model_executor -m (not slow_test)"
    result = rs.plan(_commands(TESTS[0], seconds), _timed(timings))
    assert result["unknownFiles"] == []
    assert result["shards"] == 3 and result["estimates"] == [1200, 600, 600]
    assert result["longTest"] is None


def test_plan_takes_the_fewest_shards_no_slower():
    """Two shards and three both finish with the 20-min file: two it is."""
    seconds = {"long.py": 1200, "a.py": 10, "b.py": 10, "c.py": 10}
    result = rs.plan(_commands(TESTS[0], seconds), _timed(_timings(TESTS[0], seconds)))
    assert result["shards"] == 2 and result["estimates"] == [1200, 30]


def test_plan_of_one_long_file_and_many_short_ones_takes_three_not_ten():
    seconds = {"long.py": 1500, **{f"f{i:02}.py": 60 for i in range(30)}}
    result = rs.plan(_commands(TESTS[0], seconds), _timed(_timings(TESTS[0], seconds)))
    assert result["shards"] == 3 and result["estimates"] == [1500, 900, 900]


def test_plan_of_one_long_test_and_skipped_ones_is_one_job():
    """Skipped tests take no time: a second shard would only run those."""
    timings = _timed(
        _with_tests(
            _timings(TESTS[0], {"big.py": 1116}),
            "big.py",
            {"test_slow": 1116, "test_skipped": 0},
        )
    )
    result = rs.plan(_commands(TESTS[0], ["big.py"]), timings)
    assert result["shards"] == 1 and result["estimates"] == [1116]


def test_a_test_of_exactly_the_budget_is_not_long():
    seconds = {"a.py": 1080, "b.py": 60}
    result = rs.plan(_commands(TESTS[0], seconds), _timed(_timings(TESTS[0], seconds)))
    assert result["shards"] == 2 and not result["overBudget"]
    assert result["longTest"] is None


@pytest.mark.parametrize("rest", [450, 480])  # 35.5 and 36.5 min in all
def test_plan_of_one_long_test_holds_steady_across_twice_the_budget(rest):
    """Main's medians drift: a step just over twice the budget used to take a
    third shard that the long test kept from finishing any sooner."""
    tests = {"test_slow": 1230, "test_a": rest, "test_b": rest}
    timings = _timed(
        _with_tests(_timings(TESTS[0], {"big.py": sum(tests.values())}), "big.py", tests)
    )
    result = rs.plan(_commands(TESTS[0], ["big.py"]), timings)
    assert result["shards"] == 2 and result["estimates"] == [1230, 2 * rest]


def test_plan_at_the_most_shards_drops_one_that_does_not_help():
    """At the cap the slowest shard is 50 min with ten shards or nine."""
    files = {"t0.py": 3000, **{f"t{i}.py": 1500 for i in range(1, 17)}}
    result = rs.plan(_commands(TESTS[0], files), _timed(_timings(TESTS[0], files)))
    assert result["shards"] == 9 and max(result["estimates"]) == 3000
    text = rs.summary("k", result)
    assert "its shard needs at least 50.0 min" in text
    assert "9 shards is the fewest the planner finds that keep every shard within 50.0 min." in text
    assert sorted(result["commands"][0]["files"]) == sorted(f"tests/{f}" for f in files)


def test_plan_keeps_its_shards_when_the_long_unit_is_a_guess():
    """A file main never timed counts as the median file: an hour here. Were it
    taken at its word the timed tests would share one shard, and a new file
    that takes a minute would leave the step an hour long."""
    tests = {f"test_{i}": 600 for i in range(6)}
    timings = _timed(_with_tests(_timings(TESTS[0], {"big.py": 3600}), "big.py", tests))
    result = rs.plan(_commands(TESTS[0], ["big.py", "new.py"]), timings)
    assert result["unknownFiles"] == ["tests/new.py"]
    # Two of these tests are over the budget together: a shard each.
    assert result["shards"] == 7
    assert sorted(result["estimates"]) == [600] * 6 + [3600]


@pytest.mark.parametrize("filtered", [False, True])
def test_plan_keeps_its_shards_when_a_smaller_unit_has_no_timing(filtered):
    command = TESTS[0] + (" -k fast" if filtered else "")
    seconds = {"long.py": 1200, "a.py": 600, "b.py": 600}
    result = rs.plan(
        _commands(command, [*seconds, "new.py"]), _timed(_timings(command, seconds))
    )
    field = "filteredUnknownFiles" if filtered else "unknownFiles"
    assert result[field] == ["tests/new.py"]
    assert result["shards"] == 4
    assert result["longTest"] is None


def test_plan_never_cuts_a_part_over_the_budget_from_shorter_tests():
    """Cutting a file of four 10-minute tests in ceil(40 / 18) = 3 parts would
    put two tests in one: a part over the budget that no single test is."""
    tests = {f"test_{i}": 600 for i in range(4)}
    seconds = {"big.py": 2400, "small.py": 300}
    timings = _timed(_with_tests(_timings(TESTS[0], seconds), "big.py", tests))
    result = rs.plan(_commands(TESTS[0], seconds), timings)
    assert result["shards"] == 4 and not result["overBudget"]
    assert sorted(result["estimates"]) == [600, 600, 600, 900]


def test_plan_combines_commands_in_a_shard_within_the_budget():
    """The second command's file joins a part of the first in a shard that
    stays within the budget, as packing does in any step."""
    timings = _with_tests(
        _timings(TESTS[0], {"big.py": 2184}),
        "big.py",
        {"test_slow": 1116, "test_a": 564, "test_b": 504},
    )
    timings["files"] += _timings(TESTS[1], {"small.py": 300})["files"]
    commands = _commands(TESTS[0], ["big.py"]) + _commands(TESTS[1], ["small.py"])
    result = rs.plan(commands, _timed(timings))
    assert result["shards"] == 3 and result["estimates"] == [1116, 564, 804]


def test_plan_of_several_commands_drops_a_shard_that_keeps_one_command_each():
    """Fewer shards are fine when no shard has to start a second command:
    the other command's two files share a shard."""
    timings = _timings(TESTS[0], {"big.py": 1200})
    timings["files"] += _timings(TESTS[1], {"b1.py": 540, "b2.py": 480})["files"]
    commands = _commands(TESTS[0], ["big.py"]) + _commands(TESTS[1], ["b1.py", "b2.py"])
    result = rs.plan(commands, _timed(timings))
    assert result["shards"] == 2 and result["estimates"] == [1200, 1020]
    assert result["commands"][0]["files"] == {"tests/big.py": 0}
    assert result["commands"][1]["files"] == {"tests/b1.py": 1, "tests/b2.py": 1}
    assert rs.summary("k", result).endswith(
        "2 shards is the fewest the planner finds that keep every shard within 20.0 min and any "
        "shard running several commands within 18.0 min."
    )


def _three_commands(seconds):
    commands, files = [], []
    for command, (file, s) in zip(
        [TESTS[0], TESTS[1], TESTS[0] + " -x"],
        [("a.py", seconds[0]), ("b.py", seconds[1]), ("c.py", seconds[2])],
    ):
        commands += _commands(command, [file])
        files += _timings(command, {file: s})["files"]
    return commands, _timed({"buildNumber": 7, "buildNumbers": [7], "files": files})


def test_plan_puts_two_short_commands_in_one_shard_within_the_budget():
    result = rs.plan(*_three_commands([1116, 564, 504]))
    assert result["shards"] == 2 and result["estimates"] == [1116, 1068]


def test_plan_combines_commands_that_reach_the_budget_exactly():
    result = rs.plan(*_three_commands([1116, 540, 540]))
    assert result["shards"] == 2 and result["estimates"] == [1116, 1080]


def test_plan_keeps_commands_apart_past_the_budget():
    """Together the 10 and 9 min commands would take 19 min: within the
    20-min test, but over the budget a shard of two commands keeps to."""
    result = rs.plan(*_three_commands([1200, 600, 540]))
    assert result["shards"] == 3 and result["estimates"] == [1200, 600, 540]
    assert rs.summary("k", result).endswith(
        "3 shards is the fewest the planner finds that keep every shard within 20.0 min and any "
        "shard running several commands within 18.0 min."
    )


def test_plan_keeps_its_shards_when_a_pr_narrows_the_long_file():
    """Main timed all of a.py; this command runs one of its tests, so a.py
    takes main's whole-file time only for want of a better guess."""
    files = {"a.py": 1200, "b.py": 600, "c.py": 600}
    edited = TESTS[0].replace("model_executor", "model_executor/a.py::test_fast")
    commands = _commands(edited, files, selected=["a.py"])
    result = rs.plan(commands, _timed(_timings(TESTS[0], files)))
    assert result["unknownFiles"] == [] and result["shards"] == 3
    assert result["longTest"] is None


def test_a_plan_that_mixes_commands_claims_no_fewest():
    """Ten shards for eleven commands: one shard runs two, and fewer shards
    would keep within 20 min too, were the commands allowed to share."""
    commands, files = [], []
    for i, seconds in enumerate([1200] + [600] * 10):
        command = f"pytest -v -s t{i}.py"
        commands += _commands(command, [f"t{i}.py"])
        files += _timings(command, {f"t{i}.py": seconds})["files"]
    timings = _timed({"buildNumber": 7, "buildNumbers": [7], "files": files})
    result = rs.plan(commands, timings)
    assert result["shards"] == 10
    text = rs.summary("k", result)
    assert "`tests/t0.py` takes 20.0 min on its own" in text
    assert "fewest" not in text


def test_generator_runs_a_step_of_one_long_test_in_two_jobs(
    fake_global_config, tmp_path, monkeypatch
):
    """One test over the budget and two that share the other shard."""
    command = "pytest -v -s evals/test_accuracy.py"
    _tree(tmp_path / "tests", ["evals/test_accuracy.py"])
    monkeypatch.chdir(tmp_path)
    timings = _timed(
        _with_tests(
            _timings(command, {"evals/test_accuracy.py": 2184}),
            "evals/test_accuracy.py",
            {"test_slow": 1116, "test_a": 564, "test_b": 504},
        )
    )
    monkeypatch.setattr(rs, "fetch_timings", lambda key, queue: timings)
    annotations = []
    monkeypatch.setattr(rs, "annotate", lambda *args: annotations.append(args))
    fake_global_config["run_all"] = True
    [step] = _render(_step(commands=SETUP + [command], automatic_shard=True))
    assert step.parallelism == 2
    [(key, text, style)] = annotations
    assert (
        style == "warning"
        and "`tests/evals/test_accuracy.py::test_slow` takes 18.6 min" in text
    )


# The generator


ME_FILES = {f"model_executor/test_{i}.py": 400 for i in range(6)}
ENTRYPOINT = "entrypoints/test_tensorizer_entrypoint.py"


def _step(**kwargs):
    fields = dict(
        label="Model Executor",
        key="model-executor",
        device="h200_35gb",
        working_dir="/vllm-workspace/tests",
        depends_on=["image-build"],
        commands=SETUP + TESTS,
    )
    fields.update(kwargs)
    return Step(**fields)


def _render(step):
    [group] = buildkite_step.convert_group_step_to_buildkite_step({"g": [step]})
    return group.steps


class _Annotations(list):
    """The generator's annotations; `calls` holds its (key, queue) timing requests."""

    calls = None

    def __init__(self):
        super().__init__()
        self.calls = []


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A vLLM checkout with the step's tests, as the generator runs in, and
    main's timings for them: 6 files at 400 s and one at 100 s. Returns the
    annotations the generator makes."""
    _tree(tmp_path / "tests", [*ME_FILES, ENTRYPOINT, "model_executor/helpers.py"])
    monkeypatch.chdir(tmp_path)
    timings = _timings(TESTS[0], ME_FILES)
    timings["files"] += _timings(TESTS[1], {ENTRYPOINT: 100})["files"]
    annotations = _Annotations()
    monkeypatch.setattr(
        rs,
        "fetch_timings",
        lambda key, queue: annotations.calls.append((key, queue)) or timings,
    )
    monkeypatch.setattr(rs, "annotate", lambda *args: annotations.append(args))
    return annotations


def test_generator_runs_an_enrolled_step_as_parallel_shards(
    fake_global_config, checkout
):
    fake_global_config["run_all"] = True
    [plain] = _render(_step())
    [step] = _render(_step(automatic_shard=True))
    # The step's own job, in its own group, with its key, depends_on, agents,
    # plugins and retries: only parallel.
    assert step.key == "model-executor" and step.parallelism == 3
    assert step.label == "Model Executor Shard %N/%t"
    for field in ("depends_on", "agents", "plugins", "retry", "timeout_in_minutes"):
        assert getattr(step, field) == getattr(plain, field)
    # Its commands are the step's own, after installing the plugin: the same
    # log headers and tracing labels, so main's timings keep matching. Each
    # test command exports its position, and runs only in the shards that
    # have its tests.
    setup = rs.shard_job_setup("/workdir")
    assert step.commands[: len(setup)] == setup
    jobs = step.commands[len(setup) :]
    assert len(jobs) == len(plain.commands)
    tagged = [job for job, own in zip(jobs, plain.commands) if job != own]
    assert len(tagged) == 2
    assert tagged[0].startswith(f"{{ export {rs.COMMAND_ENV}=2 {rs.APPLIED_ENV}=")
    assert "{\npytest -v -s model_executor" in tagged[0]
    assert TESTS[1] in tagged[1]  # the one-file command
    assert tagged[1].startswith('case "$${BUILDKITE_PARALLEL_JOB:-0}" in')
    assert f"{{ export {rs.COMMAND_ENV}=3 {rs.APPLIED_ENV}=" in tagged[1]
    assert "{\npytest -v -s entrypoints" in tagged[1]
    assert step.env["PYTEST_ADDOPTS"] == "-p no:pytest-shard -p runtime_shard_plugin"
    plan = rs.unpack(step.env["RUNTIME_SHARD_PLAN"])
    assert plan["shards"] == 3
    me, entrypoint = plan["commands"]
    assert me["args"] == shlex.split(TESTS[0])[1:] and me["prefix"] == "tests/"
    assert me["label"] == "Command (3/4)" and entrypoint["label"] == "Command (4/4)"
    assert sorted(me["files"]) == [f"tests/{f}" for f in ME_FILES]  # no helpers.py
    assert list(entrypoint["files"]) == [f"tests/{ENTRYPOINT}"]
    assert checkout == []  # a good plan leaves the build page alone


def test_a_command_runs_only_in_the_shards_with_its_tests():
    planned = {
        "position": 0,
        "files": {"tests/a.py": 2},
        "split": {"tests/b.py": {"shards": {"0": 2, "1": 0}}},
    }
    # Stands in for the plugin, which creates the marker's directory itself.
    applied = (
        f'echo RAN && mkdir -p "$$(dirname "$${rs.APPLIED_ENV}")"'
        f' && touch "$${rs.APPLIED_ENV}"'
    )
    script = rs.guard(applied, planned, 3).replace("$$", "$")
    ran = {}
    for job in range(3):
        env = dict(os.environ, BUILDKITE_PARALLEL_JOB=str(job))
        out = subprocess.run(
            ["sh", "-ec", script], env=env, capture_output=True, text=True
        )
        assert out.returncode == 0, out.stderr
        ran[job] = out.stdout.strip()
    assert ran[0] == ran[2] == "RAN"
    assert ran[1] == (
        "runtime-shard: this shard has no tests of this command; they run in shard 1, 3"
    )
    # A command every shard has a part of runs as it is.
    everywhere = {
        "position": 1,
        "files": {"tests/a.py": 0, "tests/c.py": 1, "tests/d.py": 2},
        "split": {},
    }
    assert rs.guard("echo RAN", everywhere, 3).startswith(
        f"{{ export {rs.COMMAND_ENV}=1 {rs.APPLIED_ENV}={rs.PLUGIN_DIR}/applied-1;"
    )


def _guarded(command):
    planned = {"position": 0, "files": {"tests/a.py": 0}, "split": {}}
    script = rs.guard(command, planned, 1).replace("$$", "$")
    return subprocess.run(["sh", "-ec", script], capture_output=True, text=True)


def test_a_command_that_passes_without_the_plan_applied_fails():
    # A pytest that didn't load the plugin, or didn't match the planned
    # command, ran all of its tests or none: it must not pass as a shard.
    run = _guarded("echo RAN")
    assert run.returncode == 1
    assert "RAN" in run.stdout
    assert "pytest ran without applying the shard plan" in run.stderr


@pytest.mark.parametrize("shards", [1, 2])
def test_a_failing_command_stops_the_job_with_its_own_status(shards):
    # A job runs its commands as lines under `set -e`, and the generator adds
    # lines after each command. A failing pytest must still stop the job,
    # whether or not the command is wrapped to run in only some shards.
    planned = {"position": 0, "files": {"tests/a.py": 0}, "split": {}}
    script = rs.guard("(exit 3)", planned, shards).replace("$$", "$")
    run = subprocess.run(
        ["sh", "-ec", script + "\necho NEXT"], capture_output=True, text=True
    )
    assert run.returncode == 3 and run.stdout == "" and run.stderr == ""


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores the mode"
)
def test_a_marker_that_cannot_be_cleared_stops_the_job(tmp_path, monkeypatch):
    # Otherwise the command and the check are skipped, and the job passes.
    monkeypatch.setattr(rs, "PLUGIN_DIR", str(tmp_path))
    (tmp_path / "applied-0").touch()
    tmp_path.chmod(0o555)
    planned = {"position": 0, "files": {"tests/a.py": 0}, "split": {}}
    script = rs.guard("echo RAN", planned, 1).replace("$$", "$")
    try:
        run = subprocess.run(
            ["sh", "-ec", script + "\necho NEXT"], capture_output=True, text=True
        )
    finally:
        tmp_path.chmod(0o755)
    assert run.returncode != 0 and run.stdout == ""


def test_a_command_ending_in_a_comment_still_runs_guarded():
    planned = {"position": 0, "files": {"tests/a.py": 0}, "split": {}}
    command = (
        f'mkdir -p "$$(dirname "$${rs.APPLIED_ENV}")" && touch "$${rs.APPLIED_ENV}"'
        " # a note"
    )
    script = rs.guard(command, planned, 1).replace("$$", "$")
    run = subprocess.run(
        ["sh", "-ec", script + "\necho NEXT"], capture_output=True, text=True
    )
    assert run.returncode == 0 and run.stdout == "NEXT\n", run.stderr


def test_generator_keeps_the_single_job_without_a_plan(
    fake_global_config, checkout, monkeypatch
):
    fake_global_config["run_all"] = True
    [plain] = _render(_step())
    monkeypatch.setattr(rs, "fetch_timings", lambda key, queue: None)
    [step] = _render(_step(automatic_shard=True))
    assert step.to_yaml() == plain.to_yaml()
    [(_, text, style)] = checkout
    assert style == "warning" and "runs as one job" in text
    # A step whose files are not in the checkout: the same.
    checkout.clear()
    commands = [*SETUP, "pytest -v -s missing_dir"]
    [plain] = _render(_step(commands=commands))
    [step] = _render(_step(automatic_shard=True, commands=commands))
    assert step.to_yaml() == plain.to_yaml() and "not in the checkout" in checkout[0][1]


def test_generator_warns_of_a_long_setup_on_a_good_plan(
    fake_global_config, checkout, monkeypatch
):
    fake_global_config["run_all"] = True
    timings = _timings(TESTS[0], ME_FILES)
    timings["files"] += _timings(TESTS[1], {ENTRYPOINT: 100})["files"]
    timings["setupMs"] = 200_000
    monkeypatch.setattr(rs, "fetch_timings", lambda key, queue: timings)
    [step] = _render(_step(automatic_shard=True))
    assert step.parallelism == 3
    [(_, text, style)] = checkout
    assert style == "warning" and "Setup takes 3.3 min per job" in text


def test_generator_shadow_mode_only_annotates(
    fake_global_config, checkout, monkeypatch
):
    fake_global_config["run_all"] = True
    monkeypatch.setenv("VLLM_CI_RUNTIME_SHARD", "shadow")
    [plain] = _render(_step())
    [step] = _render(_step(automatic_shard=True))
    assert step.to_yaml() == plain.to_yaml()
    [(_, text, style)] = checkout
    assert style == "info" and "(shadow)" in text and "3 shards" in text


def test_generator_ignores_the_flag_where_it_cannot_shard(
    fake_global_config, checkout, monkeypatch
):
    fake_global_config["run_all"] = True
    for step in (
        _step(automatic_shard=True, commands=[*TESTS, "echo done"]),
        _step(automatic_shard=True, num_nodes=2, num_devices=2),
    ):
        assert _render(step)[0].parallelism is None
    monkeypatch.setenv("VLLM_CI_RUNTIME_SHARD", "off")  # the rollback switch
    assert _render(_step(automatic_shard=True))[0].parallelism is None
    assert checkout == []


def test_generator_shards_on_a_recording_build_too(
    fake_global_config, checkout, monkeypatch
):
    """Nightly recording builds shard like PRs. The table builder unions a
    step's shards into one row and checks every shard reported, so an
    unsharded step only ran long (or timed out) for nothing."""
    fake_global_config["run_all"] = True
    monkeypatch.setattr(buildkite_step, "fnrec_enabled", lambda: True)
    monkeypatch.setattr(buildkite_step, "kernrec_enabled", lambda: True)
    [step] = _render(_step(automatic_shard=True))
    assert step.parallelism == 3
    setup = rs.shard_job_setup("/workdir")
    assert step.commands[: len(setup)] == setup


def _mirror_step(**amd):
    amd = {"device": "mi325_1", "dind": False, "automatic_shard": True, **amd}
    return _step(automatic_shard=True, mirror={"amd": amd})


def _render_mirror(step):
    """The mirror's job, from the generator's Hardware-AMD Tests group."""
    groups = buildkite_step.convert_group_step_to_buildkite_step({"g": [step]})
    [amd] = [g for g in groups if g.group == "Hardware-AMD Tests"]
    return amd.steps[-1]


def test_generator_shards_a_flagged_native_mirror(fake_global_config, checkout):
    fake_global_config["run_all"] = True
    plain = _render_mirror(_step(mirror={"amd": {"device": "mi325_1", "dind": False}}))
    job = _render_mirror(_mirror_step())
    assert job.key == "amd-model-executor" and job.parallelism == 3
    assert job.label == ":amd: (MI325) Model Executor Shard %N/%t"
    assert job.env["PYTEST_ADDOPTS"] == "-p no:pytest-shard -p runtime_shard_plugin"
    plan = rs.unpack(job.env["RUNTIME_SHARD_PLAN"])
    assert plan["shards"] == 3
    assert "RUNTIME_SHARD_PLUGIN" in job.env
    # The shard setup goes in front of the mirror's own commands, and, as on
    # CUDA, the one-file command runs only in the shard that has its tests.
    # AMD _prepare_commands wraps each command in { } to preserve exports/cd
    # across &&-joined commands, so the actual prepared form is "{\nTESTS[1]\n}".
    mirror = plain.env["VLLM_TEST_COMMANDS"]
    wrapped = re.findall(r"\{\n[^\n]*\n\}", mirror)[-2:]  # the two test commands
    assert TESTS[1] in wrapped[1]
    for one, planned in zip(wrapped, plan["commands"]):
        assert mirror.count(f"&& {one}") == 1
        mirror = mirror.replace(f"&& {one}", f"&& {rs.guard(one, planned, 3)}")
    assert rs.guard(wrapped[1], plan["commands"][1], 3).startswith(
        'case "$${BUILDKITE_PARALLEL_JOB:-0}" in'
    )
    assert job.env["VLLM_TEST_COMMANDS"] == " && ".join(
        rs.shard_job_setup("$${BUILDKITE_BUILD_CHECKOUT_PATH:-/tmp/fnrec-no-checkout}")
        + [mirror]
    )
    # The CUDA step is planned separately, on its own queue.
    assert checkout.calls == [
        ("model-executor", AgentQueue.H200_35GB),
        ("amd-model-executor", AgentQueue.AMD_MI325_1),
    ]


def test_generator_shards_a_standalone_native_amd_step(fake_global_config, checkout):
    fake_global_config["run_all"] = True
    job = _render_mirror(_step(device="mi325_1", dind=False, automatic_shard=True))
    assert job.key == "model-executor"
    assert job.parallelism == 3
    assert job.label.endswith("Shard %N/%t")
    plan = rs.unpack(job.env[rs.PLAN_ENV])
    assert plan["shards"] == 3
    assert checkout.calls == [("model-executor", AgentQueue.AMD_MI325_1)]
    assert "runtime_shard_plugin" in job.env["PYTEST_ADDOPTS"]
    assert rs.shard_job_setup("x")[0] in job.env["VLLM_TEST_COMMANDS"]
    assert rs.COVERAGE_ARTIFACT_PATH in job.artifact_paths
    assert (
        'RUNTIME_SHARD_COVERAGE_DIR="$${BUILDKITE_BUILD_CHECKOUT_PATH'
        in job.env["VLLM_TEST_COMMANDS"]
    )


@pytest.mark.parametrize("overrides", [{"dind": True}, {"parallelism": 2}])
def test_standalone_amd_keeps_dind_and_explicit_shards(
    fake_global_config, checkout, overrides
):
    fake_global_config["run_all"] = True
    job = _render_mirror(
        _step(
            **{"device": "mi325_1", "dind": False, "automatic_shard": True, **overrides}
        )
    )
    assert rs.PLAN_ENV not in (job.env or {})
    assert job.parallelism == overrides.get("parallelism")
    assert checkout.calls == []


def test_generator_leaves_an_unflagged_or_dind_mirror_alone(
    fake_global_config, checkout
):
    fake_global_config["run_all"] = True
    for step in (
        _mirror_step(automatic_shard=False),
        _mirror_step(dind=True),
        _step(
            mirror={
                "amd": {"device": "mi325_1", "dind": False, "automatic_shard": True}
            }
        ),
    ):
        job = _render_mirror(step)
        assert job.parallelism is None and "RUNTIME_SHARD_PLAN" not in job.env
    assert "amd-model-executor" not in [key for key, _ in checkout.calls]


def test_an_explicit_mirror_parallelism_skips_planning(fake_global_config, checkout):
    fake_global_config["run_all"] = True
    job = _render_mirror(_mirror_step(parallelism=2))
    assert job.parallelism == 2 and "RUNTIME_SHARD_PLAN" not in job.env
    assert "amd-model-executor" not in [key for key, _ in checkout.calls]


def test_a_mirror_without_a_plan_keeps_the_inherited_parallelism(
    fake_global_config, checkout, monkeypatch
):
    fake_global_config["run_all"] = True
    monkeypatch.setattr(rs, "fetch_timings", lambda key, queue: None)
    step = _mirror_step()
    step.parallelism = 2
    assert _render_mirror(step).parallelism == 2


def test_a_mirror_without_timings_runs_as_one_job(
    fake_global_config, checkout, monkeypatch
):
    fake_global_config["run_all"] = True
    plain = _render_mirror(_mirror_step(automatic_shard=False))
    monkeypatch.setattr(rs, "fetch_timings", lambda key, queue: None)
    checkout.clear()
    job = _render_mirror(_mirror_step())
    assert job.to_yaml() == plain.to_yaml()
    assert any(
        key == "amd-model-executor" and style == "warning" and "runs as one job" in text
        for key, text, style in checkout
    )


def test_a_mirror_is_planned_when_its_own_step_runs(fake_global_config, checkout):
    # The mirror's effective step decides, not the CUDA step's source deps.
    fake_global_config["list_file_diff"] = ["vllm/model_executor/a.py"]
    step = _mirror_step(optional=True)
    step.source_file_dependencies = ["vllm/model_executor/"]
    assert _render_mirror(step).parallelism is None  # behind a block step
    assert "amd-model-executor" not in [key for key, _ in checkout.calls]
    # The native runner script is a dependency of the mirror only.
    fake_global_config["list_file_diff"] = [
        ".buildkite/scripts/hardware_ci/run-amd-test.sh"
    ]
    step = _mirror_step()
    step.source_file_dependencies = ["vllm/model_executor/"]
    assert _render_mirror(step).parallelism == 3


def test_shadow_and_off_apply_to_mirrors(fake_global_config, checkout, monkeypatch):
    fake_global_config["run_all"] = True
    monkeypatch.setenv("VLLM_CI_RUNTIME_SHARD", "shadow")
    assert _render_mirror(_mirror_step()).parallelism is None
    assert any(key == "amd-model-executor" for key, *_ in checkout.calls)
    monkeypatch.setenv("VLLM_CI_RUNTIME_SHARD", "off")
    checkout.clear()
    assert _render_mirror(_mirror_step()).parallelism is None and checkout == []


def test_a_mirror_shards_on_a_recording_build_too(
    fake_global_config, checkout, monkeypatch
):
    fake_global_config["run_all"] = True
    monkeypatch.setattr(buildkite_step, "fnrec_enabled", lambda: True)
    monkeypatch.setattr(buildkite_step, "kernrec_enabled", lambda: True)
    assert _render_mirror(_mirror_step()).parallelism == 3


def test_fetch_timings_sends_the_queue(monkeypatch):
    urls = []

    def urlopen(url, timeout):
        urls.append(url)
        raise OSError("down")

    monkeypatch.setattr(rs, "_timings_failures", 0)
    monkeypatch.setattr(rs.urllib.request, "urlopen", urlopen)
    assert rs.fetch_timings("amd-k", AgentQueue.AMD_MI325_1) is None
    assert "stepKey=amd-k" in urls[0] and "queue=amd_mi325_1" in urls[0]
    # Per-test times for any file a shard's tests could fail to hold: the
    # budget less the most setup charged, 16 min.
    assert "testsOverMs=960000" in urls[0]


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_answering(*answers):
    """A fake urlopen that raises or returns each answer in turn, recording
    the timeout of every call."""
    timeouts = []

    def urlopen(url, timeout):
        timeouts.append(timeout)
        answer = answers[len(timeouts) - 1]
        if isinstance(answer, Exception):
            raise answer
        return _Response(json.dumps(answer).encode())

    return urlopen, timeouts


def test_fetch_timings_starts_with_no_failed_steps():
    assert rs._timings_failures == 0


def test_fetch_timings_retries_a_timeout_with_a_longer_limit(monkeypatch):
    urlopen, timeouts = _urlopen_answering(TimeoutError("read timed out"), {"files": []})
    monkeypatch.setattr(rs, "_timings_failures", 0)
    monkeypatch.setattr(rs.urllib.request, "urlopen", urlopen)
    assert rs.fetch_timings("k", AgentQueue.GPU_1) == {"files": []}
    assert timeouts == [15, 30]


def test_fetch_timings_retries_a_server_error(monkeypatch):
    error = urllib.error.HTTPError("u", 500, "internal error", {}, None)
    urlopen, timeouts = _urlopen_answering(error, {"files": []})
    monkeypatch.setattr(rs, "_timings_failures", 0)
    monkeypatch.setattr(rs.urllib.request, "urlopen", urlopen)
    assert rs.fetch_timings("k", AgentQueue.GPU_1) == {"files": []}
    assert len(timeouts) == 2


def test_fetch_timings_counts_a_step_whose_reads_both_fail(monkeypatch, capsys):
    error = urllib.error.HTTPError("u", 502, "bad gateway", {}, None)
    urlopen, timeouts = _urlopen_answering(error, error)
    monkeypatch.setattr(rs, "_timings_failures", 0)
    monkeypatch.setattr(rs.urllib.request, "urlopen", urlopen)
    assert rs.fetch_timings("k", AgentQueue.GPU_1) is None
    assert len(timeouts) == 2
    assert rs._timings_failures == 1
    assert capsys.readouterr().err == (
        "runtime-shard: no timings for k (HTTP Error 502: bad gateway)\n")


def test_fetch_timings_does_not_retry_a_404(monkeypatch, capsys):
    # 404: main has no passing build for the step; a retry can't change that,
    # and it isn't the endpoint failing.
    error = urllib.error.HTTPError("u", 404, "not found", {}, None)
    urlopen, timeouts = _urlopen_answering(error, {"files": []})
    monkeypatch.setattr(rs, "_timings_failures", 2)
    monkeypatch.setattr(rs.urllib.request, "urlopen", urlopen)
    assert rs.fetch_timings("k", AgentQueue.GPU_1) is None
    assert len(timeouts) == 1
    # The endpoint answered, so it is up: earlier failures stop counting.
    assert rs._timings_failures == 0
    assert capsys.readouterr().err == (
        "runtime-shard: no timings for k (HTTP Error 404: not found)\n")


def test_fetch_timings_skips_the_endpoint_after_three_failed_steps(monkeypatch, capsys):
    calls = []

    def urlopen(url, timeout):
        calls.append(url)
        raise TimeoutError("read timed out")

    monkeypatch.setattr(rs, "_timings_failures", 0)
    monkeypatch.setattr(rs.urllib.request, "urlopen", urlopen)
    for key in ("a", "b", "c", "d"):
        assert rs.fetch_timings(key, AgentQueue.GPU_1) is None
    # Two reads each for a, b and c; none for d.
    assert len(calls) == 6
    assert capsys.readouterr().err.splitlines() == [
        "runtime-shard: no timings for a (read timed out)",
        "runtime-shard: no timings for b (read timed out)",
        "runtime-shard: no timings for c (read timed out)",
        "runtime-shard: no timings for d (endpoint skipped after 3 failed steps)",
    ]


def test_fetch_timings_404s_between_failures_keep_the_endpoint_in_use(monkeypatch):
    timeout = TimeoutError("read timed out")
    missing = urllib.error.HTTPError("u", 404, "not found", {}, None)
    # Steps a, c, e time out on both reads; b and d have no passing main build.
    urlopen, timeouts = _urlopen_answering(
        timeout, timeout, missing, timeout, timeout, missing, timeout, timeout,
        {"files": []})
    monkeypatch.setattr(rs, "_timings_failures", 0)
    monkeypatch.setattr(rs.urllib.request, "urlopen", urlopen)
    for key in ("a", "b", "c", "d", "e"):
        assert rs.fetch_timings(key, AgentQueue.GPU_1) is None
    assert rs.fetch_timings("f", AgentQueue.GPU_1) == {"files": []}


def test_fetch_timings_success_resets_the_failure_count(monkeypatch):
    urlopen, _ = _urlopen_answering({"files": []})
    monkeypatch.setattr(rs, "_timings_failures", 2)
    monkeypatch.setattr(rs.urllib.request, "urlopen", urlopen)
    assert rs.fetch_timings("k", AgentQueue.GPU_1) == {"files": []}
    assert rs._timings_failures == 0


def test_generator_shards_a_kubernetes_step_with_its_pod_spec(
    fake_global_config, checkout
):
    fake_global_config["run_all"] = True
    [plain] = _render(_step(device="l4"))
    [step] = _render(_step(device="l4", automatic_shard=True))
    assert "kubernetes" in step.plugins[0] and step.plugins == plain.plugins
    assert step.retry == plain.retry == buildkite_step.ensure_infra_failure_retry(
        buildkite_step.K8S_RETRY
    )
    jobs = step.commands[len(rs.shard_job_setup("/workdir")) :]
    assert step.parallelism == 3 and len(jobs) == len(plain.commands)
    # The same commands, each tagged with its position, the one-file command
    # run only by the shard with it.
    changed = [new for new, old in zip(jobs, plain.commands) if new != old]
    assert len(changed) == 2 and changed[0].startswith(f"{{ export {rs.COMMAND_ENV}=")
    assert changed[1].startswith("case ")


def _venv(path, pytest_python=None):
    """A venv whose `pytest` runs pytest_python (its own python by default)."""
    if not path.exists():
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", path], check=True
        )
    pytest = path / "bin" / "pytest"
    pytest.write_text(f"#!{pytest_python or path / 'bin' / 'python3'}\n")
    pytest.chmod(0o755)
    return path


def _run_after_setup(tmp_path, command, pythonpath="/x", venv=None):
    """A shard job's setup, then the command, with python3 and pytest a fresh
    venv's: the setup's copies go there and not into the python running these
    tests."""
    venv = venv or _venv(tmp_path / "venv")
    script = "\n".join(rs.shard_job_setup("/checkout") + [command]).replace("$$", "$")
    env = dict(
        os.environ, RUNTIME_SHARD_PLUGIN=rs.plugin_source(), PYTHONPATH=pythonpath
    )
    env["PATH"] = str(venv / "bin") + os.pathsep + env["PATH"]
    return subprocess.run(
        ["bash", "-ec", script], env=env, capture_output=True, text=True, check=True
    )


FIND_PLUGIN = (
    'python3 -c "import importlib.util as u;'
    " print(u.find_spec('runtime_shard_plugin').origin)\""
)


def test_shard_job_setup_installs_the_plugin_for_any_python(tmp_path):
    run = _run_after_setup(
        tmp_path, FIND_PLUGIN + '\necho "$PYTHONPATH" "$RUNTIME_SHARD_COVERAGE_DIR"'
    )
    path, pythonpath, coverage_dir = run.stdout.split()
    assert coverage_dir == "/checkout/.runtime-shard-coverage"
    assert path == f"{rs.PLUGIN_DIR}/runtime_shard_plugin.py"
    assert pythonpath == f"{rs.PLUGIN_DIR}:/x"
    source = os.path.join(os.path.dirname(rs.__file__), "runtime_shard_plugin.py")
    with open(path) as installed, open(source) as original:
        assert installed.read() == original.read()


def test_a_command_setting_its_own_pythonpath_still_finds_the_plugin(tmp_path):
    # vLLM's entrypoints-integration-api-server runs
    # `PYTHONPATH=/vllm-workspace pytest ...`, which replaces the exported path.
    for command in (
        f"PYTHONPATH=/vllm-workspace {FIND_PLUGIN}",
        f"export PYTHONPATH=/x; {FIND_PLUGIN}",
        f"env -u PYTHONPATH {FIND_PLUGIN}",
    ):
        run = _run_after_setup(tmp_path, command)
        [found] = run.stdout.split()
        assert found.startswith(str(tmp_path / "venv" / "lib")), (command, found)
        assert found.endswith("/site-packages/runtime_shard_plugin.py")


# What pip writes after `#!/bin/sh` when the python's path is too long for a
# shebang line.
PIP_EXEC_LINES = "'''exec' /no/python3 \"$0\" \"$@\"\n' '''\n"


@pytest.mark.parametrize(
    "script, expected",
    [
        ("#!{bin}/python3\n", "{bin}/python3"),
        ("#!/usr/bin/env python3\n", "{bin}/python3"),
        # pip's form for a long path; the python needn't be on PATH.
        ("#!/bin/sh\n" + PIP_EXEC_LINES, "/no/python3"),
        ("#!/usr/bin/sh\n" + PIP_EXEC_LINES, "/no/python3"),
        ("#!/bin/bash\n", None),
        ("", None),
    ],
)
def test_the_python_pytest_runs_comes_from_its_shebang(
    tmp_path, monkeypatch, script, expected
):
    (tmp_path / "python3").symlink_to(sys.executable)
    pytest_script = tmp_path / "pytest"
    pytest_script.write_text(script.format(bin=tmp_path))
    pytest_script.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    found = runtime_shard_plugin._pytest_python()
    assert found == (expected and expected.format(bin=tmp_path))


def test_no_pytest_on_path_means_no_second_python(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert runtime_shard_plugin._pytest_python() is None


def test_shard_job_setup_installs_the_plugin_for_pytests_own_python(tmp_path):
    # python3 and the python that runs `pytest` can differ: both get a copy.
    other = _venv(tmp_path / "other")
    venv = _venv(tmp_path / "venv", pytest_python=other / "bin" / "python3")
    _run_after_setup(tmp_path, "true", venv=venv)
    for each in (venv, other):
        [site] = (each / "lib").glob("python*/site-packages")
        assert (site / "runtime_shard_plugin.py").exists(), each


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores the mode"
)
def test_shard_job_setup_runs_where_site_packages_is_read_only(tmp_path):
    # The copy is best effort, and says so: the exported PYTHONPATH still
    # covers the job.
    venv = _venv(tmp_path / "venv")
    [site] = (venv / "lib").glob("python*/site-packages")
    site.chmod(0o555)
    try:
        run = _run_after_setup(tmp_path, FIND_PLUGIN)
    finally:
        site.chmod(0o755)
    assert run.stdout.split() == [f"{rs.PLUGIN_DIR}/runtime_shard_plugin.py"]
    assert "runtime-shard: couldn't install the plugin for" in run.stderr


# The plugin, in real pytest runs: each shard runs the step's command as is.


def _project(tmp_path, files):
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    slow\n")
    for name, text in files.items():
        path = tmp_path / "tests" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _run(tmp_path, command, plan, shard, wrapper_args=(), extra_env=None):
    """One shard's run of a command, as its job runs it."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update(
        PYTEST_ADDOPTS="-p runtime_shard_plugin",
        PYTHONPATH=os.path.dirname(rs.__file__),
        **(extra_env or {}),
    )
    if shard is not None:
        env["BUILDKITE_PARALLEL_JOB"] = str(shard)
    if plan is not None:
        env["RUNTIME_SHARD_PLAN"] = rs.pack(plan)
    return subprocess.run(
        [sys.executable, "-m", "pytest", *wrapper_args, *shlex.split(command)[1:]],
        cwd=tmp_path / "tests",
        env=env,
        capture_output=True,
        text=True,
    )


def _ran(stdout):
    return re.findall(r"^(\S+::\S+) .*?(?:PASSED|FAILED)", stdout, re.M)


def _groups(stdout):
    """The Buildkite log group lines of a job's output."""
    return [s for s in stdout.splitlines() if s[:4] in ("+++ ", "--- ", "^^^ ")]


def _command_plan(command, label, files):
    return {
        "args": shlex.split(command)[1:],
        "prefix": "tests/",
        "label": label,
        "files": files,
        "split": {},
        "position": 0,
    }


def test_selected_function_cases_run_once_across_shards(tmp_path, monkeypatch):
    _project(
        tmp_path,
        {
            "test_selected.py": (
                "import pytest\n"
                "@pytest.mark.parametrize('i', range(5))\n"
                "def test_selected(i): pass\n"
                "def test_other(): raise AssertionError('outside the selected function')\n"
            )
        },
    )
    command = "pytest -v test_selected.py::test_selected -k 'not [4]'"
    timings = _with_tests(
        _timings(command, {"test_selected.py": 1600}),
        "test_selected.py",
        {f"test_selected[{i}]": 400 for i in range(4)},
    )
    monkeypatch.setattr(rs, "fetch_timings", lambda key, queue: timings)
    result, _ = rs.plan_step(
        "k", [command], "/vllm-workspace/tests", str(tmp_path), AgentQueue.H200
    )
    assert result["shards"] == 2
    plan = {"shards": result["shards"], "commands": result["commands"]}
    expected = _ran(_run(tmp_path, command, None, 0).stdout)
    assert len(expected) == 4
    actual = []
    for shard in range(plan["shards"]):
        run = _run(tmp_path, command, plan, shard)
        assert run.returncode == 0, run.stdout + run.stderr
        actual.extend(_ran(run.stdout))
    assert sorted(actual) == sorted(expected)


def test_every_test_runs_in_exactly_one_shard(tmp_path, monkeypatch):
    def tests(*names):
        return "".join(f"def {n}(): pass\n" for n in names)

    big = (
        "import pytest\n"
        "@pytest.mark.parametrize('i', range(4))\n"
        "def test_f(i): pass\n" + tests("test_g")
    )
    _project(
        tmp_path,
        {
            "pkg/test_a.py": tests("test_1", "test_2"),
            "pkg/test_b.py": tests("test_1", "test_2", "test_3"),
            "pkg/test_c.py": tests("test_1")
            + "import pytest\n@pytest.mark.slow\ndef test_slow(): pass\n",
            "pkg/sub/test_d.py": tests("test_1"),
            "big/test_big.py": big,
        },
    )
    command = "pytest -v pkg big -m 'not slow'"
    seconds = {"pkg/test_a.py": 300, "pkg/test_b.py": 400, "pkg/test_c.py": 200}
    seconds.update({"pkg/sub/test_d.py": 100, "big/test_big.py": 1500})
    per_test = {f"test_f[{i}]": 300 for i in range(4)}
    timings = _with_tests(
        _timings(command, seconds), "big/test_big.py", {**per_test, "test_g": 300}
    )
    monkeypatch.setattr(rs, "fetch_timings", lambda key, queue: timings)
    result, _ = rs.plan_step(
        "k", [command], "/vllm-workspace/tests", str(tmp_path), AgentQueue.H200
    )
    assert result["commands"][0]["split"]  # big/test_big.py is cut by test
    plan = {"shards": result["shards"], "commands": result["commands"]}
    # The PR adds what main never timed: a function and a file.
    with open(tmp_path / "tests" / "big" / "test_big.py", "a") as f:
        f.write(tests("test_h"))
    _project(tmp_path, {"pkg/test_new.py": tests("test_1")})

    everything = _ran(_run(tmp_path, command, None, 0).stdout)
    assert len(everything) == 14 and not any("slow" in t for t in everything)
    ran = []
    for shard in range(plan["shards"]):
        run = _run(tmp_path, command, plan, shard)
        assert run.returncode == 0, run.stdout + run.stderr
        ran += _ran(run.stdout)
        # What the plan doesn't name runs in the least-loaded shard.
        fallback = plan["commands"][0]["fallback"]
        if shard == fallback:
            assert (
                f"not in the plan, so run in shard {fallback + 1}: "
                "tests/pkg/test_new.py"
            ) in run.stdout
        else:
            assert "test_new" not in run.stdout
        # A file's group says when it holds only part of the file.
        for line in _groups(run.stdout):
            if "big/test_big.py" in line:
                assert re.search(
                    r"\(\d of 6 tests, split by test; the rest run in other shards\)$",
                    line,
                ), line
        # And says where the command's other tests run.
        summary = re.search(r"^runtime-shard: shard .*$", run.stdout, re.M).group(0)
        assert "; the rest run in shard " in summary, summary
    assert sorted(ran) == sorted(everything)  # each test exactly once


def test_commands_that_differ_only_in_env_each_run_their_own_tests(tmp_path):
    # Batch Invariance (Distributed) runs one pytest command per model, chosen
    # by env vars alone, so the commands' pytest args are identical.
    _project(
        tmp_path,
        {"test_model.py": "import os\ndef test_model(): print('MODEL=' + os.environ['MODEL'])\n"},
    )
    command = "pytest -v -s test_model.py"
    plan = {"shards": 2, "commands": []}
    for position in range(2):
        planned = _command_plan(
            command, f"Command ({position + 1}/2)", {"tests/test_model.py": position}
        )
        plan["commands"].append({**planned, "position": position})
    pytest = f"{shlex.quote(sys.executable)} -m {command}"
    job = " && ".join(
        rs.guard(f"MODEL={model} {pytest}", planned, 2)
        for model, planned in zip(("olmoe", "granite"), plan["commands"])
    ).replace("$$", "$")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update(
        PYTEST_ADDOPTS="-p runtime_shard_plugin",
        PYTHONPATH=os.path.dirname(rs.__file__),
        RUNTIME_SHARD_PLAN=rs.pack(plan),
    )
    models = []
    for shard in range(2):
        run = subprocess.run(
            ["sh", "-ec", job],
            cwd=tmp_path / "tests",
            env=dict(env, BUILDKITE_PARALLEL_JOB=str(shard)),
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        models += re.findall(r"MODEL=(\w+)", run.stdout)
    assert sorted(models) == ["granite", "olmoe"]


def test_a_job_without_a_parallel_index_runs_the_first_shards_tests(tmp_path):
    _project(tmp_path, {"test_a.py": "def test_a(): pass\n", "test_b.py": "def test_b(): pass\n"})
    command = "pytest -v test_a.py test_b.py"
    files = {"tests/test_a.py": 0, "tests/test_b.py": 1}
    plan = {"shards": 2, "commands": [_command_plan(command, "Command (1/1)", files)]}
    run = _run(tmp_path, command, plan, None)
    assert run.returncode == 0, run.stdout + run.stderr
    assert _ran(run.stdout) == ["test_a.py::test_a"]


def test_a_shard_opens_a_log_group_per_file(tmp_path):
    _project(
        tmp_path,
        {
            "pkg/test_a.py": "def test_1(): pass\ndef test_2(): assert 0\n",
            "pkg/test_b.py": "def test_1(): pass\n",
            "other/test_z.py": "def test_1(): pass\n",
        },
    )
    one, two = "pytest -v pkg", "pytest -v other/test_z.py"
    files = {"tests/pkg/test_a.py": 0, "tests/pkg/test_b.py": 0}
    plan = {
        "shards": 2,
        "commands": [
            _command_plan(one, "Command (1/2)", files),
            _command_plan(two, "Command (2/2)", {"tests/other/test_z.py": 1}),
        ],
    }
    run = _run(tmp_path, one, plan, 0)
    # A failing test fails the command, after the shard's other files.
    assert run.returncode == 1 and "test_b.py::test_1 PASSED" in run.stdout
    assert _groups(run.stdout) == [
        "--- :test_tube: Command (1/2), file 1/2: pkg/test_a.py",
        "^^^ +++",  # the failing file's group opens
        "--- :test_tube: Command (1/2), file 2/2: pkg/test_b.py",
        "+++ :test_tube: Command (1/2): results",
    ]
    # All of a command's tests in other shards: nothing to run is no failure.
    run = _run(tmp_path, two, plan, 0)
    assert run.returncode == 0, run.stdout
    assert (
        "runtime-shard: shard 1/2 runs none of this command's tests; "
        "they run in shard 2 (1 test)"
    ) in run.stdout
    assert _run(tmp_path, two, plan, 1).returncode == 0
    # But a command that selects nothing at all still fails, as it would alone.
    plan["commands"].append(_command_plan("pytest -v pkg -k nothing", "C", files))
    assert _run(tmp_path, "pytest -v pkg -k nothing", plan, 0).returncode == 5


def test_a_wrapper_loading_its_own_plugin_still_shards(tmp_path):
    # vLLM's CI OTel shim runs `pytest -p ci_otel <the step's args>`.
    _project(
        tmp_path,
        {
            "pkg/test_a.py": "def test_1(): pass\n",
            "pkg/test_b.py": "def test_1(): pass\n",
        },
    )
    command = "pytest -v pkg"
    files = {"tests/pkg/test_a.py": 0, "tests/pkg/test_b.py": 1}
    plan = {"shards": 2, "commands": [_command_plan(command, "Command (1/1)", files)]}
    for wrapper in (("-p", "no:cacheprovider"), ("-pno:cacheprovider",)):
        ran = [
            _ran(_run(tmp_path, command, plan, shard, wrapper).stdout)
            for shard in (0, 1)
        ]
        assert ran == [["pkg/test_a.py::test_1"], ["pkg/test_b.py::test_1"]]


def test_the_plugin_leaves_other_pytest_runs_alone(tmp_path):
    """A setup command's pytest, and a pytest a test starts, run as written."""
    nested = (
        "import subprocess, sys\n"
        "def test_nested():\n"
        "    run = subprocess.run([sys.executable, '-m', 'pytest', '-q', 'inner'],\n"
        "                         capture_output=True, text=True)\n"
        "    assert run.returncode == 0 and '2 passed' in run.stdout, run.stdout\n"
    )
    _project(
        tmp_path,
        {
            "pkg/test_nested.py": nested,
            "inner/test_inner.py": "def test_1(): pass\ndef test_2(): pass\n",
        },
    )
    command = "pytest -v pkg"
    files = {"tests/pkg/test_nested.py": 1, "tests/inner/test_inner.py": 0}
    plan = {"shards": 2, "commands": [_command_plan(command, "Command (1/1)", files)]}
    run = _run(tmp_path, command, plan, 1)
    assert run.returncode == 0 and "test_nested PASSED" in run.stdout, run.stdout
    # Arguments the plan doesn't cover: the whole run, in any shard.
    run = _run(tmp_path, "pytest -v inner", plan, 1)
    assert run.returncode == 0 and "2 passed" in run.stdout


# automatic_shard_default: true, on a branch whose pipeline config opted in.


@pytest.fixture
def by_default(fake_global_config, checkout):
    fake_global_config["run_all"] = True
    fake_global_config["automatic_shard_default"] = True
    buildkite_step.ONE_JOB_REASONS.clear()
    yield checkout
    buildkite_step.ONE_JOB_REASONS.clear()


AMD = {"device": "mi325_1", "dind": False}


@pytest.mark.parametrize(
    "step_flag, mirror_flag, step_shards, mirror_shards",
    [
        (None, None, True, True),  # no flags: both shard
        (True, None, True, True),
        (None, True, True, True),
        (True, True, True, True),
        (False, None, False, False),  # a mirror follows its step's false
        (False, False, False, False),
        (None, False, True, False),  # a mirror opts out alone
        (True, False, True, False),
        (False, True, False, True),  # a mirror opts in alone
    ],
)
def test_default_on_flag_combinations(
    by_default, step_flag, mirror_flag, step_shards, mirror_shards
):
    amd = dict(AMD)
    if mirror_flag is not None:
        amd["automatic_shard"] = mirror_flag
    fields = {} if step_flag is None else {"automatic_shard": step_flag}
    step = _step(mirror={"amd": amd}, **fields)
    [group, amd_group] = buildkite_step.convert_group_step_to_buildkite_step({"g": [step]})
    [job], [mirror] = group.steps, amd_group.steps
    assert (job.parallelism == 3) is step_shards
    assert ("Shard %N/%t" in job.label) is step_shards
    assert (mirror.parallelism == 3) is mirror_shards
    assert ("Shard %N/%t" in mirror.label) is mirror_shards
    if step_shards:
        assert "RUNTIME_SHARD_PLAN" in job.env
    else:
        assert "RUNTIME_SHARD_PLAN" not in (job.env or {})
    reasons = buildkite_step.ONE_JOB_REASONS
    assert ("model-executor" in reasons) is not step_shards
    assert ("amd-model-executor" in reasons) is not mirror_shards


@pytest.mark.parametrize(
    "step_flag, mirror_flag, step_shards, mirror_shards",
    [
        (None, None, False, False),  # no flags: nothing shards
        (True, None, True, False),  # a mirror needs its own flag
        (None, True, False, False),  # and its step's
        (True, True, True, True),
        (False, True, False, False),
        (True, False, True, False),
    ],
)
def test_without_the_default_flags_work_as_before(
    fake_global_config, checkout, step_flag, mirror_flag, step_shards, mirror_shards
):
    fake_global_config["run_all"] = True
    buildkite_step.ONE_JOB_REASONS.clear()
    amd = dict(AMD)
    if mirror_flag is not None:
        amd["automatic_shard"] = mirror_flag
    fields = {} if step_flag is None else {"automatic_shard": step_flag}
    step = _step(mirror={"amd": amd}, **fields)
    [group, amd_group] = buildkite_step.convert_group_step_to_buildkite_step({"g": [step]})
    assert (group.steps[0].parallelism == 3) is step_shards
    assert (amd_group.steps[0].parallelism == 3) is mirror_shards


@pytest.mark.parametrize("value", ["true", "on", 1, None])
def test_a_mirror_flag_that_is_not_a_boolean_is_an_error(by_default, value):
    step = _step(mirror={"amd": {**AMD, "automatic_shard": value}})
    with pytest.raises(
        ValueError,
        match=f"^amd-model-executor: automatic_shard must be true or false, not {value!r}$",
    ):
        buildkite_step.convert_group_step_to_buildkite_step({"g": [step]})


def test_yaml_on_and_off_are_booleans():
    # What a YAML `automatic_shard: on` parses to, on a step and in a mirror.
    parsed = yaml.safe_load("a: on\nb: off\n")
    assert parsed == {"a": True, "b": False}
    assert Step(label="x", commands=["true"], automatic_shard="on").automatic_shard is True


def test_a_standalone_amd_step_shards_with_no_flag(by_default):
    step = _step(device="mi325_1", dind=False)
    [job] = _render(step)
    assert job.parallelism == 3 and "Shard %N/%t" in job.label


def test_a_standalone_amd_step_that_forgets_dind_false_is_listed(by_default):
    [job] = _render(_step(device="mi325_1"))
    assert job.parallelism is None
    assert buildkite_step.ONE_JOB_REASONS["model-executor"] == "dind"


@pytest.mark.parametrize(
    "fields, reason",
    [
        (dict(commands=["python3 run.py"]), "commands aren't all plain pytest"),
        (dict(no_plugin=True), "no_plugin"),
        (dict(num_nodes=2, num_devices=2), "multi-node"),
        (dict(automatic_shard=False), "opted out (`automatic_shard: false`)"),
    ],
)
def test_a_job_that_cannot_shard_is_listed_quietly(by_default, capsys, fields, reason):
    [job] = _render(_step(**fields))
    assert job.parallelism is None and "Shard" not in job.label
    assert buildkite_step.ONE_JOB_REASONS["model-executor"] == reason
    assert "automatic_shard ignored" not in capsys.readouterr().out
    assert by_default == []  # no per-step annotation


def test_without_the_default_an_enrolled_job_that_cannot_shard_still_warns(
    fake_global_config, checkout, capsys
):
    fake_global_config["run_all"] = True
    buildkite_step.ONE_JOB_REASONS.clear()
    _render(_step(commands=["python3 run.py"], automatic_shard=True))
    assert (
        "automatic_shard ignored on model-executor: only single-node, non-dind "
        "steps whose test commands are all plain pytest can be sharded\n"
    ) in capsys.readouterr().out
    assert buildkite_step.ONE_JOB_REASONS == {}


def test_no_timings_lists_the_job_instead_of_annotating_it(by_default, monkeypatch):
    monkeypatch.setattr(rs, "fetch_timings", lambda key, queue: None)
    [job] = _render(_step())
    assert job.parallelism is None
    assert buildkite_step.ONE_JOB_REASONS["model-executor"] == (
        "**Runtime sharding for `model-executor`:** no plan (main has no timing "
        "for the step's files). The step runs as one job, as it would without sharding."
    )
    assert by_default == []


def test_a_planner_error_is_listed_with_its_message(by_default, monkeypatch):
    def broken(command, cwd, root):
        raise RuntimeError("bad | glob\nin the command")

    monkeypatch.setattr(rs, "discover", broken)
    [job] = _render(_step())
    assert job.parallelism is None
    group = buildkite_step.BuildkiteGroupStep(group="g", steps=[job])
    assert (
        "| Model Executor | **Runtime sharding for `model-executor`:** no plan "
        "(bad \\| glob in the command). "
        "The step runs as one job, as it would without sharding. |"
    ) in buildkite_step.one_job_annotation([group])


def test_without_the_default_no_timings_still_annotates(
    fake_global_config, checkout, monkeypatch
):
    fake_global_config["run_all"] = True
    monkeypatch.setattr(rs, "fetch_timings", lambda key, queue: None)
    _render(_step(automatic_shard=True))
    assert [style for _, _, style in checkout] == ["warning"]


def test_a_job_that_fits_in_one_job_is_not_listed(by_default, monkeypatch):
    timings = _timings(TESTS[0], {f: 10 for f in ME_FILES})
    timings["files"] += _timings(TESTS[1], {ENTRYPOINT: 10})["files"]
    monkeypatch.setattr(rs, "fetch_timings", lambda key, queue: timings)
    [job] = _render(_step())
    assert job.parallelism is None
    assert buildkite_step.ONE_JOB_REASONS == {}


def test_rollback_switch_runs_everything_as_one_job(by_default, monkeypatch):
    monkeypatch.setenv("VLLM_CI_RUNTIME_SHARD", "off")
    [job] = _render(_step())
    assert job.parallelism is None
    assert buildkite_step.ONE_JOB_REASONS["model-executor"] == "VLLM_CI_RUNTIME_SHARD=off"


def test_a_step_not_in_this_build_is_not_listed(by_default, fake_global_config):
    fake_global_config["only_step_keys"] = frozenset({"other"})
    buildkite_step.convert_group_step_to_buildkite_step(
        {"g": [_step(automatic_shard=False)]}
    )
    assert buildkite_step.ONE_JOB_REASONS == {}


def test_one_job_annotation_lists_this_builds_jobs_and_skips_the_rest(by_default):
    reasons = buildkite_step.ONE_JOB_REASONS
    [one] = _render(_step(commands=["python3 run.py"]))
    [image] = _render(_step(label=":docker: Build", key="image"))
    reasons["image"] = "commands aren't all plain pytest"
    image.parallelism = None  # fits in one job, and still isn't a test job
    [deferred] = _render(_step(label="Later", key="later", commands=["python3 x.py"]))
    deferred.deferred = True
    [sharded] = _render(_step(label="Big", key="big"))
    group = buildkite_step.BuildkiteGroupStep(
        group="g", steps=[one, image, deferred, sharded]
    )
    text = buildkite_step.one_job_annotation([group])
    assert text == (
        "<details><summary>1 job ran as one job without a shard plan</summary>\n\n"
        "| Job | Why |\n| --- | --- |\n"
        "| Model Executor | commands aren't all plain pytest |\n\n</details>"
    )


def test_one_job_annotation_counts_and_stays_away_when_empty(by_default):
    [sharded] = _render(_step())
    group = buildkite_step.BuildkiteGroupStep(group="g", steps=[sharded])
    assert buildkite_step.one_job_annotation([group]) is None
    [a] = _render(_step(label="A", key="a", automatic_shard=False))
    [b] = _render(_step(label="B", key="b", automatic_shard=False))
    group = buildkite_step.BuildkiteGroupStep(group="g", steps=[a, b])
    text = buildkite_step.one_job_annotation([group])
    assert "2 jobs ran as one job" in text
    opted_out = "opted out (`automatic_shard: false`)"
    assert f"| A | {opted_out} |\n| B | {opted_out} |\n" in text


def test_without_the_default_there_is_no_one_job_annotation(
    by_default, fake_global_config, monkeypatch
):
    monkeypatch.setenv("VLLM_CI_RUNTIME_SHARD", "off")
    [job] = _render(_step())
    group = buildkite_step.BuildkiteGroupStep(group="g", steps=[job])
    assert "1 job ran as one job" in buildkite_step.one_job_annotation([group])
    fake_global_config["automatic_shard_default"] = False
    assert buildkite_step.one_job_annotation([group]) is None


def test_generate_posts_the_one_job_list_as_one_warning(
    by_default, fake_global_config, tmp_path, monkeypatch
):
    import pipeline_generator as generator

    steps = [
        _step(depends_on=None),
        _step(label="Script", key="script", depends_on=None, commands=["python3 run.py"]),
    ]
    monkeypatch.setattr(generator, "get_global_config", lambda: fake_global_config)
    monkeypatch.setattr(generator, "read_steps_from_job_dir", lambda d: steps)
    monkeypatch.delenv("BUILDKITE_JOB_ID", raising=False)
    monkeypatch.setenv("PATH", "")  # no buildkite-agent: uploads are skipped
    fake_global_config["job_dirs"] = ["x"]
    pipeline = generator.PipelineGenerator.__new__(generator.PipelineGenerator)
    pipeline.output_file_path = str(tmp_path / "out.yaml")
    pipeline.generate()
    [(key, text, style)] = by_default
    assert (key, style) == ("one-job", "warning")
    assert "1 job ran as one job" in text and "| Script | commands aren't all plain pytest |" in text

    del by_default[:]
    fake_global_config["automatic_shard_default"] = False
    pipeline.generate()
    assert by_default == []


@pytest.mark.parametrize("flag", [None, True, False])
def test_a_fixed_split_and_the_mirror_that_inherits_it_run_as_they_do_today(by_default, flag):
    fields = {} if flag is None else {"automatic_shard": flag}
    step = _step(parallelism=2, mirror={"amd": dict(AMD)}, **fields)
    [group, amd_group] = buildkite_step.convert_group_step_to_buildkite_step({"g": [step]})
    for job in (group.steps[0], amd_group.steps[0]):
        assert job.parallelism == 2 and "Shard %N/%t" not in job.label
        assert "RUNTIME_SHARD_PLAN" not in (job.env or {})
    assert by_default.calls == []  # neither is planned
    assert buildkite_step.one_job_annotation([group, amd_group]) is None


def test_a_fixed_split_on_a_mirror_or_a_standalone_amd_step_stays(by_default):
    mirrored = _step(mirror={"amd": {**AMD, "parallelism": 4}})
    [group, amd_group] = buildkite_step.convert_group_step_to_buildkite_step({"g": [mirrored]})
    assert group.steps[0].parallelism == 3  # the step itself still shards
    assert amd_group.steps[0].parallelism == 4
    assert "Shard %N/%t" not in amd_group.steps[0].label
    job = _render_mirror(_step(device="mi325_1", dind=False, parallelism=2))
    assert job.parallelism == 2 and "Shard %N/%t" not in job.label


def test_pytest_xdist_can_not_be_sharded():
    for args in ("-n 4", "-n=4", "-n4", "-nauto", "-nlogical", "--numprocesses=4"):
        assert rs.split_commands([f"pytest tests {args}"]) is None
    assert rs.split_commands(["pytest tests -name-like-arg"]) is not None
    for args in ("--num-shards=2", "--shard-id=0"):
        assert rs.split_commands([f"pytest tests {args}"]) is None
    assert rs.split_commands(["pytest tests  # not with -n 4"]) is not None
    # An inline PYTEST_ADDOPTS replaces the job's, which loads the shard plugin.
    assert rs.split_commands(["PYTEST_ADDOPTS=-q pytest tests"]) is None
    assert rs.split_commands(["OTHER=-q pytest tests"]) is not None
    # A command that doesn't parse can't be planned; it must not stop the build.
    assert rs.split_commands(["pytest tests -k 'unterminated"]) is None


def test_xdist_in_a_jobs_environment_keeps_it_one_job(by_default):
    reason = "`PYTEST_ADDOPTS` sets its own shards or pytest-xdist"
    # pytest reads the variable as a shell would: quotes, and a value it can't parse.
    for addopts in ("-v -n 4", "'-n4'", '"-n" "4"', "--num-shards=2", "-k 'unterminated"):
        buildkite_step.ONE_JOB_REASONS.clear()
        [job] = _render(_step(env={"PYTEST_ADDOPTS": addopts}))
        assert job.parallelism is None, addopts
        assert buildkite_step.ONE_JOB_REASONS == {"model-executor": reason}
    # A mirror's own environment counts for the mirror only.
    amd = {**AMD, "env": {"PYTEST_ADDOPTS": "-nauto"}}
    step = _step(env={"PYTEST_ADDOPTS": "-p plug"}, mirror={"amd": amd})
    [group, amd_group] = buildkite_step.convert_group_step_to_buildkite_step({"g": [step]})
    assert group.steps[0].parallelism == 3 and amd_group.steps[0].parallelism is None
    assert buildkite_step.ONE_JOB_REASONS["amd-model-executor"] == reason


def test_excluded_hand_splits_keep_their_split_and_are_not_called_one_job(by_default):
    job = _render_mirror(
        _step(automatic_shard=False, device="mi325_1", dind=False, parallelism=2)
    )
    assert job.parallelism == 2 and "Shard %N/%t" not in job.label
    [nvidia] = _render(_step(automatic_shard=False, parallelism=2))
    group = buildkite_step.BuildkiteGroupStep(group="g", steps=[job, nvidia])
    assert buildkite_step.one_job_annotation([group]) is None
    mirrored = _step(mirror={"amd": {**AMD, "parallelism": 4, "automatic_shard": False}})
    [_, amd_group] = buildkite_step.convert_group_step_to_buildkite_step({"g": [mirrored]})
    assert amd_group.steps[0].parallelism == 4

# The coverage records, from real pytest runs.


COVERED_FILES = {
    "pkg/test_a.py": "def test_1(): pass\ndef test_2(): pass\n",
    "pkg/test_b.py": "open('imported_b', 'w').close()\ndef test_1(): pass\n",
}


def _covered_run(
    tmp_path,
    shard,
    job,
    command="pytest -v pkg",
    retry=None,
    fallback=0,
    extra_env=None,
):
    """One job's run with records on. Returns (the run, its record)."""
    files = {"tests/pkg/test_a.py": 0, "tests/pkg/test_b.py": 1}
    planned = _command_plan(command, "Command (1/1)", files)
    planned["fallback"] = fallback
    plan = {"shards": 2, "key": "k", "commands": [planned]}
    env = {
        "RUNTIME_SHARD_COVERAGE_DIR": str(tmp_path / "records"),
        "BUILDKITE_JOB_ID": job,
        **(extra_env or {}),
    }
    if retry is not None:
        env["BUILDKITE_RETRY_COUNT"] = str(retry)
    run = _run(tmp_path, command, plan, shard, extra_env=env)
    path = tmp_path / "records" / job / "0.json"
    if not (tmp_path / "records").is_dir() or not path.exists():
        return run, None
    return run, json.loads(path.read_text())


def test_the_first_shard_records_the_whole_command_and_each_what_it_ran(tmp_path):
    _project(tmp_path, COVERED_FILES)
    every = [
        "tests/pkg/test_a.py::test_1",
        "tests/pkg/test_a.py::test_2",
        "tests/pkg/test_b.py::test_1",
    ]
    shown = [nodeid[len("tests/") :] for nodeid in every]

    run, first = _covered_run(tmp_path, 0, "job-0", retry=2)
    assert run.returncode == 0, run.stdout
    # The inventory's pytest runs quietly: none of its output leaks into the
    # shard's own log.
    assert run.stdout.count("test session starts") == 1
    assert first["expected"] == every
    assert first["ran"] == every[:2] and first["planned"] == every[:2]
    assert (first["step"], first["position"], first["shard"]) == ("k", 0, 0)
    assert first["retry"] == 2 and first["finished"] is True
    assert first["finished_at"] > 1_000_000_000
    # Its inventory subprocess imported the other shard's file; the shard's own
    # run did not, and did not run its test.
    assert (tmp_path / "tests" / "imported_b").exists()
    assert _ran(run.stdout) == shown[:2]

    (tmp_path / "tests" / "imported_b").unlink()
    run, second = _covered_run(tmp_path, 1, "job-1")
    assert run.returncode == 0, run.stdout
    assert second["expected"] is None and second["ran"] == every[2:]
    assert second["retry"] == 0
    assert _ran(run.stdout) == shown[2:]


def test_the_recording_shards_run_does_not_import_other_shards_files(tmp_path):
    """The recording shard lists the command's tests from a separate pytest it
    starts, so an import side effect of another shard's file cannot reach this
    shard's own tests."""
    _project(
        tmp_path,
        {
            "pkg/test_a.py": (
                "import os\n"
                "def test_1():\n"
                "    assert 'OTHER_SHARD_IMPORTED' not in os.environ\n"
            ),
            "pkg/test_b.py": (
                "import os\n"
                "os.environ['OTHER_SHARD_IMPORTED'] = '1'\n"
                "def test_1(): pass\n"
            ),
        },
    )
    run, record = _covered_run(tmp_path, 0, "job-0")
    assert run.returncode == 0, run.stdout
    assert _ran(run.stdout) == ["pkg/test_a.py::test_1"]
    # The expected list still covers the whole command, and the inventory's
    # pytest wrote no record of its own.
    assert record["expected"] == [
        "tests/pkg/test_a.py::test_1",
        "tests/pkg/test_b.py::test_1",
    ]
    assert [p.name for p in (tmp_path / "records").rglob("*.json")] == ["0.json"]


def test_only_the_recording_shards_inventory_covers_other_shards_files(tmp_path):
    _project(tmp_path, COVERED_FILES)
    _, first = _covered_run(tmp_path, 1, "job-1", fallback=1)  # shard 2 records
    assert first["expected"] == [
        "tests/pkg/test_a.py::test_1",
        "tests/pkg/test_a.py::test_2",
        "tests/pkg/test_b.py::test_1",
    ]
    assert (tmp_path / "tests" / "imported_b").exists()
    (tmp_path / "tests" / "imported_b").unlink()
    # And shard 1 is then an ordinary shard: test_b.py is another shard's file,
    # and no inventory runs here.
    run, record = _covered_run(tmp_path, 0, "job-0", fallback=1)
    assert record["expected"] is None and not (tmp_path / "tests" / "imported_b").exists()
    assert run.returncode == 0


def test_no_coverage_directory_means_no_records_and_no_extra_collection(tmp_path):
    _project(tmp_path, COVERED_FILES)
    files = {"tests/pkg/test_a.py": 0, "tests/pkg/test_b.py": 1}
    plan = {"shards": 2, "commands": [_command_plan("pytest -v pkg", "C", files)]}
    assert _run(tmp_path, "pytest -v pkg", plan, 0).returncode == 0
    assert not (tmp_path / "tests" / "imported_b").exists()
    assert not (tmp_path / "records").exists()


def test_an_inventory_that_cannot_collect_leaves_the_command_unverified(tmp_path):
    """A file of another shard that cannot be imported fails the inventory's
    collection, so the expected list stays unavailable: the coverage check
    reports the command as unverified rather than compare against a partial
    list. The recording shard's own tests are unaffected, and pass."""
    _project(
        tmp_path,
        {"pkg/test_a.py": "def test_1(): pass\n", "pkg/test_b.py": "raise ValueError\n"},
    )
    run, record = _covered_run(tmp_path, 0, "job-0")
    assert run.returncode == 0, run.stdout  # test_b.py is shard 2's
    assert record["expected"] is None
    assert record["ran"] == ["tests/pkg/test_a.py::test_1"] and record["finished"] is True
    assert (
        "runtime-shard: couldn't collect the command's full test list (exit 1); "
        "the coverage check will report the command unverified\n"
    ) in run.stderr


def test_the_inventory_is_the_tests_the_command_selects(tmp_path):
    """The command's -m/-k deselection applies in the inventory's collection
    too: the expected list is what the command selects, not every test in its
    files."""
    _project(tmp_path, COVERED_FILES)  # test_a: test_1, test_2; test_b: test_1
    run, record = _covered_run(tmp_path, 0, "job-0", command="pytest -v pkg -k test_1")
    assert run.returncode == 0, run.stdout
    assert record["expected"] == [
        "tests/pkg/test_a.py::test_1",
        "tests/pkg/test_b.py::test_1",
    ]
    assert record["ran"] == ["tests/pkg/test_a.py::test_1"]


def test_an_inventory_subprocess_that_cannot_start_leaves_the_shard_running(tmp_path):
    """If the inventory's pytest cannot even start (spawning it raises), the
    shard's own tests still run and pass, and the expected list stays
    unavailable so the coverage check reports the command unverified."""
    _project(tmp_path, COVERED_FILES)
    (tmp_path / "tests" / "break_inventory.py").write_text(
        "import runtime_shard_plugin\n"
        "def pytest_configure(config):\n"
        "    def dies(*args, **kwargs):\n"
        "        raise OSError('out of processes')\n"
        "    runtime_shard_plugin.subprocess.run = dies\n"
    )
    command = "pytest -v pkg"
    files = {"tests/pkg/test_a.py": 0, "tests/pkg/test_b.py": 1}
    plan = {"shards": 2, "key": "k", "commands": [_command_plan(command, "C", files)]}
    env = {
        "RUNTIME_SHARD_COVERAGE_DIR": str(tmp_path / "records"),
        "BUILDKITE_JOB_ID": "job-0",
    }
    run = _run(
        tmp_path, command, plan, 0, wrapper_args=("-p", "break_inventory"), extra_env=env
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert _ran(run.stdout) == ["pkg/test_a.py::test_1", "pkg/test_a.py::test_2"]
    record = json.loads((tmp_path / "records" / "job-0" / "0.json").read_text())
    assert record["expected"] is None and record["finished"] is True
    assert "couldn't collect the command's full test list" in run.stderr


def test_inventory_timeout_is_positive_and_finite():
    timeout = runtime_shard_plugin.INVENTORY_TIMEOUT_SECONDS
    assert isinstance(timeout, (int, float)) and timeout > 0 and timeout != float("inf")


def test_an_inventory_spawn_failure_returns_no_list(tmp_path, monkeypatch, capsys):
    import runtime_shard_plugin as plugin

    monkeypatch.setenv(plugin.COVERAGE_DIR_ENV, str(tmp_path / "records"))
    command = {"files": {}, "split": {}, "prefix": "", "label": "L", "position": 0}
    shard = plugin.ShardPlugin(command, 0, 2, "k")

    def dies(*args, **kwargs):
        raise OSError("out of processes")

    monkeypatch.setattr(plugin.subprocess, "run", dies)
    config = types.SimpleNamespace(
        invocation_params=types.SimpleNamespace(args=["-v", "pkg"], dir=str(tmp_path))
    )
    assert shard._expected_inventory(config) is None
    assert capsys.readouterr().err == (
        "runtime-shard: couldn't collect the command's full test list "
        "(out of processes); the coverage check will report the command "
        "unverified\n"
    )


def test_an_inventory_that_times_out_returns_no_list(tmp_path, monkeypatch, capsys):
    """A hung inventory collection is bounded: after the timeout the shard's
    own run goes on without the list, so the coverage check reports the
    command unverified."""
    import runtime_shard_plugin as plugin

    monkeypatch.setenv(plugin.COVERAGE_DIR_ENV, str(tmp_path / "records"))
    command = {"files": {}, "split": {}, "prefix": "", "label": "L", "position": 0}
    shard = plugin.ShardPlugin(command, 0, 2, "k")

    def hangs(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd=args[0], timeout=plugin.INVENTORY_TIMEOUT_SECONDS
        )

    monkeypatch.setattr(plugin.subprocess, "run", hangs)
    config = types.SimpleNamespace(
        invocation_params=types.SimpleNamespace(args=["-v", "pkg"], dir=str(tmp_path))
    )
    assert shard._expected_inventory(config) is None
    err = capsys.readouterr().err
    assert (
        "runtime-shard: couldn't collect the command's full test list "
        f"(timed out after {plugin.INVENTORY_TIMEOUT_SECONDS} seconds); the "
        "coverage check will report the command unverified\n"
    ) == err


def test_the_inventory_collects_where_the_command_ran(tmp_path, monkeypatch):
    """The inventory's pytest runs in the command's own directory — its
    conftest.py and rootdir resolve as they did for the command — and cannot
    run longer than the inventory timeout."""
    import runtime_shard_plugin as plugin

    monkeypatch.setenv(plugin.COVERAGE_DIR_ENV, str(tmp_path / "records"))
    command = {"files": {}, "split": {}, "prefix": "", "label": "L", "position": 0}
    shard = plugin.ShardPlugin(command, 0, 2, "k")
    captured = {}

    def fake_run(args, **kwargs):
        captured.update(kwargs)
        with open(kwargs["env"][plugin.INVENTORY_ENV], "w") as f:
            json.dump(["tests/pkg/test_a.py::test_1"], f)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    config = types.SimpleNamespace(
        invocation_params=types.SimpleNamespace(args=["-v", "pkg"], dir=str(tmp_path))
    )
    assert shard._expected_inventory(config) == ["tests/pkg/test_a.py::test_1"]
    assert captured["cwd"] == str(tmp_path)
    assert captured["timeout"] == plugin.INVENTORY_TIMEOUT_SECONDS
    assert captured["env"]["PYTHONSAFEPATH"] == "1"
    monkeypatch.setattr(plugin, "_INITIAL_SYS_PATH_ENTRY", str(tmp_path))
    assert shard._expected_inventory(config) == ["tests/pkg/test_a.py::test_1"]
    assert "PYTHONSAFEPATH" not in captured["env"]


def test_a_failed_inventory_prints_a_bounded_tail_of_its_output(
    tmp_path, monkeypatch, capsys
):
    """When the inventory's collection fails, the shard's log gets the end of
    its output — where pytest names the file that broke — bounded so a huge
    collection log cannot bury the job's own."""
    import runtime_shard_plugin as plugin

    monkeypatch.setenv(plugin.COVERAGE_DIR_ENV, str(tmp_path / "records"))
    command = {"files": {}, "split": {}, "prefix": "", "label": "L", "position": 0}
    shard = plugin.ShardPlugin(command, 0, 2, "k")
    noise = "".join(f"noise line {i}\n" for i in range(200))
    error = "ERROR tests/pkg/test_b.py - ValueError: broken\n"

    def failed(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, noise, error)

    monkeypatch.setattr(plugin.subprocess, "run", failed)
    config = types.SimpleNamespace(
        invocation_params=types.SimpleNamespace(args=["-v", "pkg"], dir=str(tmp_path))
    )
    assert shard._expected_inventory(config) is None
    err = capsys.readouterr().err
    assert err == (
        "runtime-shard: couldn't collect the command's full test list (exit 1); "
        "the coverage check will report the command unverified"
        "\n--- last lines of its output ---\n...\n"
        + "\n".join(f"noise line {i}" for i in range(160, 200))
        + "\n--- last lines of its error output ---\n"
        "ERROR tests/pkg/test_b.py - ValueError: broken\n"
    )


def test_stderr_inventory_tail_is_kept_and_exact_limit_is_not_truncated(
    tmp_path, monkeypatch, capsys
):
    import runtime_shard_plugin as plugin

    monkeypatch.setenv(plugin.COVERAGE_DIR_ENV, str(tmp_path / "records"))
    command = {"files": {}, "split": {}, "prefix": "", "label": "L", "position": 0}
    shard = plugin.ShardPlugin(command, 0, 2, "k")
    noise = "".join(
        f"line {i}\n" for i in range(plugin.INVENTORY_TAIL_LINES - 1)
    )

    def failed(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "first error\n" + noise)

    monkeypatch.setattr(plugin.subprocess, "run", failed)
    config = types.SimpleNamespace(
        invocation_params=types.SimpleNamespace(args=["-v", "pkg"], dir=str(tmp_path))
    )
    assert shard._expected_inventory(config) is None
    err = capsys.readouterr().err
    assert "first error" in err and "line 0" in err
    assert "error output" in err and "...\n" not in err


def test_the_coverage_record_names_its_build(tmp_path, monkeypatch):
    """Each record carries its build's id, so the coverage check can filter
    out records a stale job wrote; off Buildkite there is no build and the id
    is null."""
    monkeypatch.delenv("BUILDKITE_BUILD_ID", raising=False)
    _project(tmp_path, COVERED_FILES)
    _, record = _covered_run(
        tmp_path, 0, "job-0", extra_env={"BUILDKITE_BUILD_ID": "build-123"}
    )
    assert record["build_id"] == "build-123"
    _, record = _covered_run(tmp_path, 1, "job-1")
    assert record["build_id"] is None


def test_a_collection_error_in_a_later_shard_still_stops_it(tmp_path):
    # The file's own shard fails as before; the recording shard's run never
    # imports it (covered by the inventory tests above).
    _project(
        tmp_path,
        {"pkg/test_a.py": "def test_1(): pass\n", "pkg/test_b.py": "raise ValueError\n"},
    )
    run, _ = _covered_run(tmp_path, 1, "job-1")
    assert run.returncode == 2  # pytest's "interrupted"


def test_a_collection_error_before_assignment_records_an_unknown_plan(tmp_path):
    _project(tmp_path, {"pkg/test_b.py": "raise ValueError\n"})
    _, record = _covered_run(tmp_path, 1, "job-1")
    assert record["expected"] is None and record["planned"] is None


def test_a_stopped_run_is_not_finished(tmp_path):
    _project(
        tmp_path,
        {
            "pkg/test_a.py": "def test_1(): assert False\ndef test_2(): pass\n",
            "pkg/test_b.py": "def test_1(): pass\n",
        },
    )
    run, record = _covered_run(tmp_path, 0, "job-0", command="pytest -v -x pkg")
    assert run.returncode == 1
    assert record["finished"] is False and record["ran"] == ["tests/pkg/test_a.py::test_1"]


def test_a_run_that_dies_leaves_its_list_marked_unfinished(tmp_path):
    _project(
        tmp_path,
        {
            "pkg/test_a.py": "import os\ndef test_1(): os._exit(3)\n",
            "pkg/test_b.py": "def test_1(): pass\n",
        },
    )
    run, record = _covered_run(tmp_path, 0, "job-0")
    assert run.returncode == 3
    assert record["finished"] is False
    assert record["expected"] == ["tests/pkg/test_a.py::test_1", "tests/pkg/test_b.py::test_1"]
    assert record["ran"] == []


def test_a_failed_test_counts_as_ran_and_keeps_the_job_red(tmp_path):
    _project(
        tmp_path,
        {
            "pkg/test_a.py": "import pytest\ndef test_1(): assert False\n"
            "@pytest.mark.skip\ndef test_2(): pass\n",
            "pkg/test_b.py": "def test_1(): pass\n",
        },
    )
    run, record = _covered_run(tmp_path, 0, "job-0")
    assert run.returncode == 1
    assert record["ran"] == ["tests/pkg/test_a.py::test_1", "tests/pkg/test_a.py::test_2"]


def test_collect_only_writes_no_record_and_a_job_id_is_optional(tmp_path):
    _project(tmp_path, COVERED_FILES)
    _, record = _covered_run(tmp_path, 0, "job-0", command="pytest --collect-only pkg")
    assert record is None
    files = {"tests/pkg/test_a.py": 0, "tests/pkg/test_b.py": 1}
    plan = {"shards": 2, "key": "k", "commands": [_command_plan("pytest -v pkg", "C", files)]}
    env = {"RUNTIME_SHARD_COVERAGE_DIR": str(tmp_path / "records")}
    without_id = {k: v for k, v in os.environ.items() if k != "BUILDKITE_JOB_ID"}
    os.environ.pop("BUILDKITE_JOB_ID", None)
    try:
        _run(tmp_path, "pytest -v pkg", plan, 0, extra_env=env)
    finally:
        os.environ.clear()
        os.environ.update(without_id)
    assert (tmp_path / "records" / "no-job-id" / "0.json").exists()


def test_record_directories_can_be_deleted_by_the_agent(tmp_path):
    _project(tmp_path, COVERED_FILES)
    _covered_run(tmp_path, 0, "job-0")
    for path in (tmp_path / "records", tmp_path / "records" / "job-0"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o777


def test_a_record_that_cannot_be_written_leaves_the_job_alone(tmp_path):
    _project(tmp_path, COVERED_FILES)
    (tmp_path / "records").write_text("a file where the directory goes")
    run, record = _covered_run(tmp_path, 0, "job-0")
    assert run.returncode == 0 and record is None
    assert re.search(r"^runtime-shard: no coverage record: ", run.stdout, re.M)


def test_a_write_that_dies_halfway_leaves_the_earlier_record_whole(
    tmp_path, monkeypatch
):
    import runtime_shard_plugin as plugin

    monkeypatch.setenv(plugin.COVERAGE_DIR_ENV, str(tmp_path / "records"))
    monkeypatch.setenv("BUILDKITE_JOB_ID", "job-0")
    command = {"files": {}, "split": {}, "prefix": "", "label": "L", "position": 0}
    shard = plugin.ShardPlugin(command, 0, 2, "k")
    shard._write_record(finished=False)
    path = tmp_path / "records" / "job-0" / "0.json"
    assert json.loads(path.read_text())["finished"] is False

    def dies_halfway(record, f):
        f.write('{"step": "k", "fin')
        raise OSError("killed")

    monkeypatch.setattr(plugin.json, "dump", dies_halfway)
    shard._write_record(finished=True)
    assert json.loads(path.read_text())["finished"] is False
    records = [p.name for p in path.parent.iterdir() if p.suffix == ".json"]
    assert records == ["0.json"]


# The job that compares them.


def test_generator_adds_one_coverage_job_for_the_sharded_steps(
    fake_global_config, checkout
):
    fake_global_config["run_all"] = True
    [step] = _render(_step(automatic_shard=True))
    mirror = _render_mirror(_mirror_step())
    group = buildkite_step.BuildkiteGroupStep(group="g", steps=[step, mirror])
    job = buildkite_step.shard_coverage_step([group])
    assert job.key == "runtime-shard-coverage"
    # Poll the shards rather than depend on them: a cancelled soft-fail shard
    # must not make the whole coverage check disappear.
    assert job.depends_on == []
    assert job.env["CI_SELECTOR_COLLECT_WAIT_FOR"] == "model-executor amd-model-executor"
    assert "CI_SELECTOR_COLLECT_DEADLINE" not in job.env
    assert job.allow_dependency_failure is True and job.soft_fail is False
    steps = json.loads(job.env["RUNTIME_SHARD_COVERAGE_STEPS"])
    assert sorted(steps) == ["amd-model-executor", "model-executor"]
    plan = rs.unpack(step.env["RUNTIME_SHARD_PLAN"])
    assert plan["key"] == "model-executor"
    assert [c["position"] for c in steps["model-executor"]] == [2, 3]
    me = steps["model-executor"][0]
    assert me["fallback"] == plan["commands"][0]["fallback"]
    assert me["label"] == plan["commands"][0]["label"]
    assert me["owners"] == rs.owners(plan["commands"][0])
    assert me["soft_fail"] is False
    # Sharded steps upload their records; the AMD mirror too.
    assert rs.COVERAGE_ARTIFACT_PATH == ".runtime-shard-coverage/*/*.json"
    assert rs.COVERAGE_ARTIFACT_PATH in step.artifact_paths
    assert rs.COVERAGE_ARTIFACT_PATH in mirror.artifact_paths
    assert job.label == ":mag: Check every test ran" and job.timeout_in_minutes == 360
    assert job.retry == buildkite_step.ensure_infra_failure_retry(None)
    assert job.commands[0] == (
        "while :; do "
        'pending=""; '
        "for k in $$CI_SELECTOR_COLLECT_WAIT_FOR; do "
        's="$$(buildkite-agent step get state --step "$$k" 2>/dev/null)"; '
        'case "$$s" in finished|ignored|*_failed|timed_out|canceled|cancelled|expired|broken|skipped) ;; '
        '*) pending="$$k"; break;; esac; '
        "done; "
        '[ -z "$$pending" ] && { echo "every sharded step has stopped"; break; }; '
        'echo "waiting on $$pending ($${s:-state unknown})"; '
        "sleep 30; "
        "done"
    )
    script = "/tmp/runtime_shard_coverage.$${BUILDKITE_JOB_ID:-local}.py"
    assert job.commands[1] == (
        'python3 -c \'import base64,json,os,sys,zlib; '
        'sys.stdout.write(json.loads(zlib.decompress(base64.b64decode(os.environ["RUNTIME_SHARD_COVERAGE_SCRIPT"]))))\''
        f" > {script} && python3 {script}"
    )
    # And their records land where each job can reach the checkout.
    coverage_setup = rs.shard_job_setup("/workdir")[-1]
    assert 'RUNTIME_SHARD_COVERAGE_DIR="/workdir/' in coverage_setup
    assert step.commands[len(rs.shard_job_setup("/workdir")) - 1] == coverage_setup
    assert (
        'RUNTIME_SHARD_COVERAGE_DIR="$${BUILDKITE_BUILD_CHECKOUT_PATH'
        in mirror.env["VLLM_TEST_COMMANDS"]
    )


def test_coverage_job_propagates_a_steps_soft_fail(fake_global_config, checkout):
    """A soft-failing sharded step keeps that in the spec, so the comparison
    can keep its failures a warning."""
    fake_global_config["run_all"] = True
    [step] = _render(_step(automatic_shard=True))
    step.soft_fail = True
    group = buildkite_step.BuildkiteGroupStep(group="g", steps=[step])

    job = buildkite_step.shard_coverage_step([group])

    steps = json.loads(job.env["RUNTIME_SHARD_COVERAGE_STEPS"])
    assert [c["soft_fail"] for c in steps["model-executor"]] == [True, True]


def test_coverage_job_leaves_out_deferred_steps_and_builds_without_shards(
    fake_global_config, checkout
):
    fake_global_config["run_all"] = True
    [sharded] = _render(_step(automatic_shard=True))
    [plain] = _render(_step())
    plain.key = "plain"
    group = buildkite_step.BuildkiteGroupStep(group="g", steps=[plain])
    assert buildkite_step.shard_coverage_step([group]) is None
    sharded.deferred = True
    group = buildkite_step.BuildkiteGroupStep(group="g", steps=[sharded, plain])
    assert buildkite_step.shard_coverage_step([group]) is None
    [running] = _render(_step(automatic_shard=True, key="running"))
    group = buildkite_step.BuildkiteGroupStep(group="g", steps=[sharded, running])
    job = buildkite_step.shard_coverage_step([group])
    assert job.depends_on == []
    assert json.loads(job.env["RUNTIME_SHARD_COVERAGE_STEPS"]).keys() == {"running"}
    image = buildkite_step.BuildkiteGroupStep(
        group="i",
        steps=[buildkite_step.BuildkiteCommandStep(label="i", key="image-build", commands=["x"])],
    )
    job = buildkite_step.shard_coverage_step([image, group])
    assert job.depends_on == ["image-build"]  # not the shard jobs themselves


def test_coverage_job_embeds_and_decodes_the_generator_script(fake_global_config, checkout):
    fake_global_config["run_all"] = True
    [step] = _render(_step(automatic_shard=True))
    group = buildkite_step.BuildkiteGroupStep(group="g", steps=[step])
    job = buildkite_step.shard_coverage_step([group])
    with open(
        os.path.join(
            os.path.dirname(buildkite_step.__file__), "runtime_shard_coverage.py"
        )
    ) as f:
        source = f.read()
    assert rs.unpack(job.env["RUNTIME_SHARD_COVERAGE_SCRIPT"]) == source
    assert all("raw.githubusercontent.com" not in command for command in job.commands)
    assert job.agents["queue"] == AgentQueue.SMALL_CPU_PREMERGE.value
    fake_global_config["branch"] = "main"
    job = buildkite_step.shard_coverage_step([group])
    assert job.agents["queue"] == AgentQueue.SMALL_CPU_POSTMERGE.value

    script = "/tmp/runtime_shard_coverage.test-job.py"
    decode = job.commands[-1].replace("$$", "$").split(" && python3 ", 1)[0]
    try:
        subprocess.run(
            ["bash", "-c", decode],
            env=dict(os.environ, **job.env, BUILDKITE_JOB_ID="test-job"),
            capture_output=True,
            text=True,
            check=True,
        )
        with open(script) as f:
            assert f.read() == source
    finally:
        if os.path.exists(script):
            os.unlink(script)


def test_coverage_wait_stops_when_shards_cannot_run(
    fake_global_config, checkout, tmp_path
):
    fake_global_config["run_all"] = True
    [step] = _render(_step(automatic_shard=True))
    group = buildkite_step.BuildkiteGroupStep(group="g", steps=[step])
    wait = buildkite_step.shard_coverage_step([group]).commands[0].replace("$$", "$")
    agent = tmp_path / "buildkite-agent"
    agent.write_text('#!/bin/sh\nprintf "%s\\n" "$STEP_STATE"\n')
    agent.chmod(0o755)
    for state in ("waiting_failed", "timed_out", "canceled"):
        run = subprocess.run(
            ["bash", "-c", wait],
            env=dict(
                os.environ,
                PATH=f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
                CI_SELECTOR_COLLECT_WAIT_FOR="model-executor",
                STEP_STATE=state,
            ),
            capture_output=True,
            text=True,
            timeout=2,
        )
        assert run.returncode == 0
        assert run.stdout == "every sharded step has stopped\n"
