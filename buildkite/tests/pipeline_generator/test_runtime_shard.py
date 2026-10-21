import os
import re
import shlex
import subprocess
import sys

import pytest

import buildkite_step
import runtime_shard as rs
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
    result = rs.plan(_commands(TESTS[0], seconds), _timings(TESTS[0], seconds))
    # 2100 s doesn't fit two shards of 1080 s; three, evenly.
    assert result["shards"] == 3 and result["estimates"] == [700, 700, 700]
    files = result["commands"][0]["files"]
    assert {f for f, s in files.items() if s == files["tests/b.py"]} == {
        "tests/a.py",
        "tests/b.py",
    }
    assert not result["overBudget"] and result["unknownFiles"] == []


def test_plan_of_a_step_that_fits_in_one_job():
    seconds = {"a.py": 100, "b.py": 200}
    result = rs.plan(_commands(TESTS[0], seconds), _timings(TESTS[0], seconds))
    assert result["shards"] == 1 and result["estimates"] == [300]
    assert "fit in one job" in rs.summary("k", result)


def test_one_job_over_the_budget_says_so():
    seconds = {"a.py": 1200}
    result = rs.plan(_commands(TESTS[0], seconds), _timings(TESTS[0], seconds))
    assert result["shards"] == 1 and result["overBudget"]
    text = rs.summary("k", result)
    assert "fit in one job" not in text
    assert "`tests/a.py` takes 20.0 min on its own" in text
    assert "It runs on 1 shard, the fewest" in text


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
    """An edited command has a different hash but keeps its file timings
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


def test_command_labels_hash_the_full_command_and_number_identical_commands():
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


def test_command_preview_hashes_before_truncation_or_sanitizing():
    command = "pytest 'a.py' $FLAGS"
    label = rs.command_preview(command)
    assert label.startswith("pytest a.py FLAGS [") and label.endswith("]")
    assert re.fullmatch(r"pytest a.py FLAGS \[[0-9a-f]{12}\]", label)
    assert label != rs.command_preview("pytest a.py FLAGS")
    assert label == rs.command_preview(command)
    assert rs.command_preview("pytest tests/你好.py").startswith(
        "pytest tests/你好.py ["
    )


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


@pytest.mark.parametrize("suffix", ["", " #2"])
def test_plan_prefers_hashed_history_over_legacy_history_during_rollout(suffix):
    timings = _timings("pytest a.py", {"a.py": 30})
    timings["files"][0]["command"] += suffix
    timings["files"] += [
        dict(timings["files"][0], command="pytest a.py", observedMs=900000)
    ]
    # Editing a unique command still reuses its file timing, even while the
    # endpoint returns both the old preview and the new hash for that file.
    commands = _commands("pytest -v a.py", ["a.py"])
    result = rs.plan(commands, timings)
    assert result["unknownFiles"] == [] and result["estimates"] == [30]


def test_plan_does_not_prefer_a_legacy_match_over_another_commands_hash():
    one, two = "pytest a.py", "pytest a.py -v"
    commands = _commands(one, ["a.py"]) + _commands(two, ["a.py"])
    timings = _timings(two, {"a.py": 30})
    timings["files"] += [dict(timings["files"][0], command=one, observedMs=900000)]
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
    """The generator's hash suffix selects each command's own timings."""
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
    cases = {f"test_x[{i}]": 700 for i in range(3)}
    timings = _with_tests(_timings(TESTS[0], {"big.py": 2100}), "big.py", cases)
    result = rs.plan(_commands(TESTS[0], ["big.py"]), timings)
    assign = result["commands"][0]["split"]["tests/big.py"]["assign"]
    assert len(set(assign.values())) == 2 and result["shards"] == 2


def test_plan_never_splits_a_file_the_command_names_by_test_id():
    timings = _with_tests(
        _timings(TESTS[0], {"big.py": 2000}), "big.py", {"test_a": 1000, "test_b": 1000}
    )
    commands = _commands(TESTS[0], ["big.py", "small.py"], selected=["big.py"])
    result = rs.plan(commands, timings)
    assert result["commands"][0]["split"] == {} and result["oversizedFiles"] == []


def test_plan_drops_a_shard_that_cannot_beat_one_long_test():
    """One test alone sets how long the step takes: a third shard would only
    pay for another job, so the rest share the second."""
    timings = _with_tests(
        _timings(TESTS[0], {"big.py": 2184}),
        "big.py",
        {"test_slow": 1116, "test_a": 564, "test_b": 504},
    )
    result = rs.plan(_commands(TESTS[0], ["big.py"]), timings)
    assert result["shards"] == 2 and result["estimates"] == [1116, 1068]
    assert result["overBudget"]
    assert rs.summary("k", result) == (
        "**Runtime sharding for `k`:** 2 shards for 1 files; estimated test "
        "time per shard (min): 18.6, 17.8. Timings: median of 2 main builds, "
        "the newest 7. Split by test: `tests/big.py`. :warning: "
        "`tests/big.py::test_slow` takes 18.6 min on its own, over the 18-min "
        "target, and can't be split across shards, so the step can't finish "
        "in under 18.6 min. It runs on 2 shards, the fewest that finish in "
        "18.6 min."
    )


def test_plan_takes_the_fewest_shards_no_slower():
    """Two shards and three both finish with the 20-min file: two it is."""
    seconds = {"long.py": 1200, "a.py": 10, "b.py": 10, "c.py": 10}
    result = rs.plan(_commands(TESTS[0], seconds), _timings(TESTS[0], seconds))
    assert result["shards"] == 2 and result["estimates"] == [1200, 30]


def test_plan_of_one_long_test_and_skipped_ones_is_one_job():
    """Skipped tests take no time: a second shard would only run those."""
    timings = _with_tests(
        _timings(TESTS[0], {"big.py": 1116}),
        "big.py",
        {"test_slow": 1116, "test_skipped": 0},
    )
    result = rs.plan(_commands(TESTS[0], ["big.py"]), timings)
    assert result["shards"] == 1 and result["estimates"] == [1116]


def test_a_test_of_exactly_the_budget_is_not_long():
    seconds = {"a.py": 1080, "b.py": 60}
    result = rs.plan(_commands(TESTS[0], seconds), _timings(TESTS[0], seconds))
    assert result["shards"] == 2 and not result["overBudget"]
    assert result["longTest"] is None


@pytest.mark.parametrize("rest", [450, 480])  # 35.5 and 36.5 min in all
def test_plan_of_one_long_test_holds_steady_across_twice_the_budget(rest):
    """Main's medians drift: a step just over twice the budget used to take a
    third shard that the long test kept from finishing any sooner."""
    tests = {"test_slow": 1230, "test_a": rest, "test_b": rest}
    timings = _with_tests(
        _timings(TESTS[0], {"big.py": sum(tests.values())}), "big.py", tests
    )
    result = rs.plan(_commands(TESTS[0], ["big.py"]), timings)
    assert result["shards"] == 2 and result["estimates"] == [1230, 2 * rest]


def test_plan_at_the_most_shards_drops_one_that_does_not_help():
    """At the cap the slowest shard is 40 min with six shards or five."""
    files = {"t0.py": 1800, **{f"t{i}.py": 1200 for i in range(1, 9)}}
    result = rs.plan(_commands(TESTS[0], files), _timings(TESTS[0], files))
    assert result["shards"] == 5 and max(result["estimates"]) == 2400
    text = rs.summary("k", result)
    assert "can't finish in under 30.0 min" in text
    assert "It runs on 5 shards, the fewest that finish in 40.0 min." in text
    assert sorted(result["commands"][0]["files"]) == sorted(f"tests/{f}" for f in files)


def test_plan_keeps_its_shards_when_the_long_unit_is_a_guess():
    """A file main never timed counts as the median file: an hour here. Were it
    taken at its word the timed tests would share two shards, and a new file
    that takes a minute would leave the step an hour long."""
    tests = {f"test_{i}": 600 for i in range(6)}
    timings = _with_tests(_timings(TESTS[0], {"big.py": 3600}), "big.py", tests)
    result = rs.plan(_commands(TESTS[0], ["big.py", "new.py"]), timings)
    assert result["unknownFiles"] == ["tests/new.py"]
    assert result["shards"] == 5
    assert sorted(result["estimates"]) == [600, 600, 1200, 1200, 3600]


def test_plan_keeps_its_shards_when_the_long_unit_holds_several_tests():
    """Cutting a file of four 10-minute tests in three parts puts two tests
    in one: a part over the budget that no single test is."""
    tests = {f"test_{i}": 600 for i in range(4)}
    seconds = {"big.py": 2400, "small.py": 300}
    timings = _with_tests(_timings(TESTS[0], seconds), "big.py", tests)
    result = rs.plan(_commands(TESTS[0], seconds), timings)
    assert result["shards"] == 4
    assert sorted(result["estimates"]) == [300, 600, 600, 1200]


def test_plan_keeps_its_shards_for_a_step_of_several_commands():
    """Each command a shard runs starts pytest again, which main's timings
    leave out: fewer shards would add that to the slowest shard."""
    timings = _with_tests(
        _timings(TESTS[0], {"big.py": 2184}),
        "big.py",
        {"test_slow": 1116, "test_a": 564, "test_b": 504},
    )
    timings["files"] += _timings(TESTS[1], {"small.py": 300})["files"]
    commands = _commands(TESTS[0], ["big.py"]) + _commands(TESTS[1], ["small.py"])
    result = rs.plan(commands, timings)
    assert result["shards"] == 4
    assert sorted(result["estimates"]) == [300, 504, 564, 1116]


def test_plan_over_the_most_shards_still_assigns_every_file():
    files = {f"t{i}.py": 1000 for i in range(8)}
    result = rs.plan(_commands(TESTS[0], files), _timings(TESTS[0], files))
    assert result["shards"] == 6 and result["overBudget"]
    assert sorted(result["commands"][0]["files"]) == [f"tests/{f}" for f in files]
    assert ":warning:" in rs.summary("k", result)


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
    # log headers and tracing labels, so main's timings keep matching. A test
    # command runs only in the shards that have its tests.
    assert step.commands[:2] == rs.shard_job_setup()
    jobs = step.commands[2:]
    assert len(jobs) == len(plain.commands)
    guarded = [job for job, own in zip(jobs, plain.commands) if job != own]
    assert len(guarded) == 1 and TESTS[1] in guarded[0]  # the one-file command
    assert guarded[0].startswith('case "$${BUILDKITE_PARALLEL_JOB:-0}" in')
    assert step.env["PYTEST_ADDOPTS"] == "-p no:pytest-shard -p runtime_shard_plugin"
    plan = rs.unpack(step.env["RUNTIME_SHARD_PLAN"])
    assert plan["shards"] == 3
    me, entrypoint = plan["commands"]
    assert me["args"] == shlex.split(TESTS[0])[1:] and me["prefix"] == "tests/"
    assert me["label"] == "Command (3/4)" and entrypoint["label"] == "Command (4/4)"
    assert sorted(me["files"]) == [f"tests/{f}" for f in ME_FILES]  # no helpers.py
    assert list(entrypoint["files"]) == [f"tests/{ENTRYPOINT}"]
    assert checkout == []  # a good plan leaves the build page alone


def test_generator_runs_a_step_of_one_long_test_in_two_jobs(
    fake_global_config, tmp_path, monkeypatch
):
    """One test over the budget and two that share the other shard."""
    command = "pytest -v -s evals/test_accuracy.py"
    _tree(tmp_path / "tests", ["evals/test_accuracy.py"])
    monkeypatch.chdir(tmp_path)
    timings = _with_tests(
        _timings(command, {"evals/test_accuracy.py": 2184}),
        "evals/test_accuracy.py",
        {"test_slow": 1116, "test_a": 564, "test_b": 504},
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


def test_a_command_runs_only_in_the_shards_with_its_tests():
    planned = {
        "files": {"tests/a.py": 2},
        "split": {"tests/b.py": {"shards": {"0": 2, "1": 0}}},
    }
    script = rs.guard("echo RAN", planned, 3).replace("$$", "$")
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
        "files": {"tests/a.py": 0, "tests/c.py": 1, "tests/d.py": 2},
        "split": {},
    }
    assert rs.guard("echo RAN", everywhere, 3) == "echo RAN"


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
    assert step.commands[:2] == rs.shard_job_setup()


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
    guarded = rs.guard(TESTS[1], plan["commands"][1], 3)
    assert guarded.startswith('case "$${BUILDKITE_PARALLEL_JOB:-0}" in')
    mirror = plain.env["VLLM_TEST_COMMANDS"]
    assert mirror.count(f"&& {TESTS[1]}") == 1
    assert job.env["VLLM_TEST_COMMANDS"] == " && ".join(
        rs.shard_job_setup() + [mirror.replace(f"&& {TESTS[1]}", f"&& {guarded}")]
    )
    # The CUDA step is planned separately, on its own queue.
    assert checkout.calls == [
        ("model-executor", AgentQueue.H200_35GB),
        ("amd-model-executor", AgentQueue.AMD_MI325_1),
    ]


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

    monkeypatch.setattr(rs.urllib.request, "urlopen", urlopen)
    assert rs.fetch_timings("amd-k", AgentQueue.AMD_MI325_1) is None
    assert "stepKey=amd-k" in urls[0] and "queue=amd_mi325_1" in urls[0]


def test_generator_shards_a_kubernetes_step_with_its_pod_spec(
    fake_global_config, checkout
):
    fake_global_config["run_all"] = True
    [plain] = _render(_step(device="l4"))
    [step] = _render(_step(device="l4", automatic_shard=True))
    assert "kubernetes" in step.plugins[0] and step.plugins == plain.plugins
    assert step.retry == plain.retry == buildkite_step.K8S_RETRY
    assert step.parallelism == 3 and len(step.commands[2:]) == len(plain.commands)
    # The same commands, the one-file command run only by the shard with it.
    changed = [new for new, old in zip(step.commands[2:], plain.commands) if new != old]
    assert len(changed) == 1 and changed[0].startswith("case ")


def test_shard_job_setup_installs_the_plugin_for_any_python():
    script = "\n".join(rs.shard_job_setup()).replace("$$", "$")
    script += '\npython3 -c "import runtime_shard_plugin as p; print(p.__file__)"'
    env = dict(os.environ, RUNTIME_SHARD_PLUGIN=rs.plugin_source(), PYTHONPATH="/x")
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env["PATH"]
    run = subprocess.run(
        ["bash", "-ec", script + '\necho "$PYTHONPATH"'],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    path, pythonpath = run.stdout.split()
    assert path == f"{rs.PLUGIN_DIR}/runtime_shard_plugin.py"
    assert pythonpath == f"{rs.PLUGIN_DIR}:/x"
    source = os.path.join(os.path.dirname(rs.__file__), "runtime_shard_plugin.py")
    with open(path) as installed, open(source) as original:
        assert installed.read() == original.read()


# The plugin, in real pytest runs: each shard runs the step's command as is.


def _project(tmp_path, files):
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers =\n    slow\n")
    for name, text in files.items():
        path = tmp_path / "tests" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _run(tmp_path, command, plan, shard, wrapper_args=()):
    """One shard's run of a command, as its job runs it."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update(
        PYTEST_ADDOPTS="-p runtime_shard_plugin",
        PYTHONPATH=os.path.dirname(rs.__file__),
        BUILDKITE_PARALLEL_JOB=str(shard),
    )
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
    }


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
        if shard == 0:  # what the plan doesn't name runs in the first shard
            assert "not in the plan, so run in shard 1: tests/pkg/test_new.py" in (
                run.stdout
            )
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
