import json
import shlex
import os
import subprocess
import sys

import pytest

import buildkite_step
import runtime_shard as rs
from pipeline_generator import select_steps_and_dependencies
from step import Step

SETUP = ["apt-get update && apt-get install -y curl", "export PYTHONFAULTHANDLER=1"]
TESTS = [
    "pytest -v -s model_executor -m '(not slow_test)' --timeout=900",
    "pytest -v -s entrypoints/test_tensorizer_entrypoint.py --timeout=900",
]


def _entry(command, files, prefix="tests"):
    """Inventory for one command: {file: number of tests}, in collection order."""
    args = shlex.split(command, comments=True)[1:]
    paths = []
    for i, arg in enumerate(args):
        if not arg.startswith("-") and (i == 0 or args[i - 1] not in ("-m", "-k")):
            paths.append(arg)
    return {
        "command": command,
        "prefix": prefix,
        "paths": paths,
        "nodeids": [
            f"{prefix}/{f}::test_{i}" for f, n in files.items() for i in range(n)
        ],
    }


def _timings(command, seconds):
    return {
        "buildNumber": 7,
        "commit": "abc",
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


def test_contiguous_keeps_order_and_uses_fewest_runs():
    assert len(rs.contiguous([6, 5, 3, 2, 2, 2], 10, 6)) == 3  # 6 | 5,3,2 | 2,2
    assert rs.contiguous([5, 5, 5, 5], 10, 6) == [[0, 1], [2, 3]]
    assert rs.contiguous([4, 4, 4, 4], 12, 6) == [[0, 1], [2, 3]]  # evened, not 12 + 4
    over = rs.contiguous([9] * 10, 10, 6)
    assert len(over) <= 6 and sum(over, []) == list(range(10))


def test_plan_keeps_a_file_main_timed_at_zero_seconds():
    inventory = [_entry(TESTS[0], {"model_executor/a.py": 2, "model_executor/b.py": 1})]
    timings = _timings(TESTS[0], {"model_executor/a.py": 0, "model_executor/b.py": 60})
    result = rs.plan(inventory, timings)
    rs.check(result, inventory)  # used to drop a.py: ceil(0 / budget) = 0 parts
    assert result["shards"][0]["commands"][0]["targets"] == [
        "model_executor/a.py",
        "model_executor/b.py",
    ]


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
    # Only the shell can expand $: collect would see different tests.
    assert rs.split_commands(["PYTHONPATH=$PWD pytest -v a.py"]) is None


def test_plan_packs_whole_files_in_order_and_covers_every_test():
    inventory = [
        _entry(
            TESTS[0],
            {
                "model_executor/a.py": 3,
                "model_executor/b.py": 2,
                "model_executor/c.py": 1,
            },
        ),
        _entry(TESTS[1], {"entrypoints/test_tensorizer_entrypoint.py": 1}),
    ]
    timings = _timings(
        TESTS[0], {"model_executor/a.py": 700, "model_executor/b.py": 600}
    )
    timings["files"] += _timings(
        TESTS[1], {"entrypoints/test_tensorizer_entrypoint.py": 300}
    )["files"]
    result = rs.plan(inventory, timings)
    rs.check(result, inventory)
    # a | b + c (unknown, 150 s) + the second command's file
    assert [s["estimateSeconds"] for s in result["shards"]] == [700, 1050]
    assert result["shards"][1]["commands"] == [
        {
            "index": 0,
            "targets": ["model_executor/b.py", "model_executor/c.py"],
            "tests": 3,
        },
        {
            "index": 1,
            "targets": ["entrypoints/test_tensorizer_entrypoint.py"],
            "tests": 1,
        },
    ]
    assert result["unknownFiles"] == ["tests/model_executor/c.py"]
    assert not result["overBudget"] and result["tests"] == 7 and result["files"] == 4


def test_plan_splits_only_a_file_bigger_than_a_shard():
    inventory = [
        _entry(TESTS[0], {"model_executor/big.py": 4, "model_executor/small.py": 1})
    ]
    result = rs.plan(
        inventory,
        _timings(
            TESTS[0], {"model_executor/big.py": 2000, "model_executor/small.py": 10}
        ),
    )
    rs.check(result, inventory)
    assert result["oversizedFiles"] == ["tests/model_executor/big.py"]
    first, second = (s["commands"][0]["targets"] for s in result["shards"])
    assert first == ["model_executor/big.py::test_0", "model_executor/big.py::test_1"]
    assert second == [
        "model_executor/big.py::test_2",
        "model_executor/big.py::test_3",
        "model_executor/small.py",
    ]


def _with_tests(timings, command, file, test_seconds):
    """Attach per-test medians, in test_i order, to one file of a _timings()
    response. A None entry leaves that test with no timing of its own."""
    preview = rs.command_preview(command)
    for entry in timings["files"]:
        if entry["command"] == preview and entry["file"] == f"tests/{file}":
            entry["tests"] = [
                {"nodeid": f"tests/{file}::test_{i}", "observedMs": s * 1000}
                for i, s in enumerate(test_seconds)
                if s is not None
            ]
    return timings


def test_plan_adds_a_shard_when_the_files_parts_cannot_fit_the_budget():
    """ceil(18 / 10) = 2 parts, but 2 contiguous parts of these tests are 12 s."""
    inventory = [_entry(TESTS[0], {"model_executor/big.py": 3})]
    timings = _with_tests(
        _timings(TESTS[0], {"model_executor/big.py": 18}),
        TESTS[0],
        "model_executor/big.py",
        [6, 6, 6],
    )
    result = rs.plan(inventory, timings, max_shard_seconds=10)
    rs.check(result, inventory)
    assert [s["estimateSeconds"] for s in result["shards"]] == [6, 6, 6]
    assert not result["overBudget"]


def test_plan_splits_an_oversized_files_tests_by_time_not_count():
    inventory = [_entry(TESTS[0], {"model_executor/big.py": 4})]
    timings = _with_tests(
        _timings(TESTS[0], {"model_executor/big.py": 200}),
        TESTS[0],
        "model_executor/big.py",
        [170, 10, 10, 10],
    )
    result = rs.plan(inventory, timings, max_shard_seconds=100)
    rs.check(result, inventory)
    # Equal-count would give test_0,1 | test_2,3; time-based keeps the one
    # slow test alone instead of pairing it with a fast one.
    first, second = (s["commands"][0]["targets"] for s in result["shards"])
    assert first == ["model_executor/big.py::test_0"]
    assert second == [
        "model_executor/big.py::test_1",
        "model_executor/big.py::test_2",
        "model_executor/big.py::test_3",
    ]
    assert [s["estimateSeconds"] for s in result["shards"]] == [170, 30]


def test_plan_keeps_a_test_functions_cases_in_one_shard():
    """Main paid the cases' shared setup once; split apart, each pays it."""
    file = "model_executor/big.py"
    inventory = [
        {
            "command": TESTS[0],
            "prefix": "tests",
            "paths": ["model_executor"],
            "nodeids": [
                f"tests/{file}::test_b",
                f"tests/{file}::test_a[v1]",
                f"tests/{file}::test_a[v2]",
                f"tests/{file}::test_c[x]",
                f"tests/{file}::test_c[y]",
                f"tests/{file}::test_c[z]",
            ],
        }
    ]
    timings = _timings(TESTS[0], {file: 200})
    timings["files"][0]["tests"] = [
        {"nodeid": f"tests/{file}::{name}", "observedMs": s * 1000}
        for name, s in [
            ("test_b", 60),
            ("test_a[v1]", 30),
            ("test_a[v2]", 30),
            ("test_c[x]", 50),
            ("test_c[y]", 50),
            ("test_c[z]", 50),
        ]
    ]
    result = rs.plan(inventory, timings, max_shard_seconds=100)
    rs.check(result, inventory)
    targets = [s["commands"][0]["targets"] for s in result["shards"]]
    # test_a's cases stay together; test_c (150 s) is over budget, so cut.
    assert [f"{file}::test_a[v1]", f"{file}::test_a[v2]"] in targets
    assert sum(len(t) for t in targets) == 6
    assert all(s["estimateSeconds"] <= 100 for s in result["shards"])


def test_plan_gives_a_test_with_no_timing_the_files_average():
    inventory = [_entry(TESTS[0], {"model_executor/big.py": 4})]
    timings = _with_tests(
        _timings(TESTS[0], {"model_executor/big.py": 2000}),
        TESTS[0],
        "model_executor/big.py",
        [1700, None, 100, 100],  # test_1 has no timing of its own
    )
    result = rs.plan(inventory, timings)
    rs.check(result, inventory)
    # test_1 gets the file average (2000 / 4 = 500s) and lands with test_2, 3.
    first, second = (s["commands"][0]["targets"] for s in result["shards"])
    assert first == ["model_executor/big.py::test_0"]
    assert second == [
        "model_executor/big.py::test_1",
        "model_executor/big.py::test_2",
        "model_executor/big.py::test_3",
    ]
    assert [s["estimateSeconds"] for s in result["shards"]] == [1700, 700]


def test_plan_without_per_test_data_still_splits_by_count():
    """No "tests" on the oversized file: unchanged, today's equal split."""
    inventory = [_entry(TESTS[0], {"model_executor/big.py": 4})]
    timings = _timings(TESTS[0], {"model_executor/big.py": 200})
    result = rs.plan(inventory, timings, max_shard_seconds=100)
    rs.check(result, inventory)
    first, second = (s["commands"][0]["targets"] for s in result["shards"])
    assert first == ["model_executor/big.py::test_0", "model_executor/big.py::test_1"]
    assert second == ["model_executor/big.py::test_2", "model_executor/big.py::test_3"]
    assert [s["estimateSeconds"] for s in result["shards"]] == [100, 100]


def test_plan_without_timings_uses_four_equal_shards():
    inventory = [_entry(TESTS[0], {f"model_executor/t{i}.py": 1 for i in range(8)})]
    result = rs.plan(inventory, None)
    rs.check(result, inventory)
    assert [len(s["commands"][0]["targets"]) for s in result["shards"]] == [2, 2, 2, 2]
    assert result["timingSource"] is None and "equal file counts" in rs.annotation(
        "k", result
    )


def test_plan_over_max_number_of_shards_still_assigns_everything():
    files = {f"model_executor/t{i}.py": 1 for i in range(10)}
    inventory = [_entry(TESTS[0], files)]
    result = rs.plan(inventory, _timings(TESTS[0], {f: 1100 for f in files}))
    rs.check(result, inventory)
    # 5 pairs: a 6th shard can't lower the largest one below two files
    assert len(result["shards"]) == 5 and result["overBudget"]
    assert "still over" in rs.annotation("k", result)


def test_plan_counts_a_file_skipped_on_main_at_its_measured_time():
    command = "pytest -v -s tests"
    timings = _timings(command, {"ran.py": 600, "rocm_only.py": 0})
    timings["files"][1]["timingStatus"] = "skip_only"
    result = rs.plan([_entry(command, {"ran.py": 2, "rocm_only.py": 3})], timings)
    # It has a timing, so it is not "unknown", and adds no 2.5 min guess.
    assert result["unknownFiles"] == [] and result["skippedFiles"] == [
        "tests/rocm_only.py"
    ]
    assert result["shards"][0]["estimateSeconds"] == 600
    assert "Skipped on main" in rs.annotation("k", result)
    assert "No timing" not in rs.annotation("k", result)


def test_annotation_lists_each_shards_files_per_command():
    small, big = "pytest -v -s small", "pytest -v -s big"
    inventory = [
        _entry(small, {"a.py": 1, "b.py": 2}),
        _entry(big, {"big.py": 4}),
    ]
    timings = _timings(small, {"a.py": 60, "b.py": 60})
    timings["files"] += _timings(big, {"big.py": 2000})["files"]
    result = rs.plan(inventory, timings)  # [a.py, b.py], [big.py half], [big.py half]
    text = rs.annotation("k", result, shadow=False, commands=[small, big])
    assert '<a href="artifact://.runtime-shard/k/plan.json">plan.json</a>' in text
    assert "| 1 | 3 | 2 files | 2.0 | command 1: 2 files (3 tests) |" in text
    assert (
        "| 2 | 2 | 1 file | 16.7 | command 2: 1 file (2 tests; 1 split by test ID) |"
        in text
    )
    # The folded list names each command and its files; split files say how much.
    files = text[text.index("<details>") :]
    assert (
        "**Shard 1** (3 tests)\n\n- `pytest -v -s small`\n  - `a.py`\n  - `b.py`"
        in files
    )
    assert "- `pytest -v -s big`\n  - `big.py`: 2 of 4 tests" in files
    assert files.endswith("</details>")


def test_annotation_names_the_timings_as_a_median_of_main_builds():
    command = "pytest -v -s tests"
    timings = dict(_timings(command, {"a.py": 60}), buildNumbers=[7, 6, 5])
    result = rs.plan([_entry(command, {"a.py": 2})], timings)
    assert result["timingSource"]["buildNumbers"] == [7, 6, 5]
    assert "median of 3 main builds, the newest 7 (`abc`)" in rs.annotation("k", result)


def test_check_rejects_a_lost_or_repeated_test():
    inventory = [_entry(TESTS[0], {"model_executor/a.py": 1, "model_executor/b.py": 1})]
    result = rs.plan(inventory, None)
    result["shards"][0]["commands"][0]["targets"].pop()
    with pytest.raises(ValueError):
        rs.check(result, inventory)
    result = rs.plan(inventory, None)
    result["shards"].append(result["shards"][0])
    with pytest.raises(ValueError):
        rs.check(result, inventory)


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


def test_generator_adds_shadow_collect_and_plan_steps(fake_global_config, monkeypatch):
    fake_global_config["run_all"] = True
    monkeypatch.setenv("VLLM_CI_BRANCH", "agent/runtime-shard-planner")
    monkeypatch.setenv("VLLM_CI_RUNTIME_SHARD", "shadow")
    plain = _render(_step())
    main, collect, plan = _render(_step(automatic_shard=True))
    assert main.to_yaml() == plain[0].to_yaml()  # shadow: the step is unchanged
    assert collect.key == "model-executor-shard-collect" and collect.soft_fail
    # The build page truncates labels, so the step key comes first.
    assert collect.label == "model-executor: runtime shard collect"
    assert plan.label == "model-executor: runtime shard plan"
    assert collect.depends_on == ["image-build"] and collect.agents == main.agents
    assert collect.plugins == main.plugins
    assert collect.artifact_paths == [".runtime-shard/model-executor/*.json"]
    assert collect.commands[: 1 + len(SETUP)] == ["cd /vllm-workspace/tests", *SETUP]
    assert (
        "ci-infra/agent/runtime-shard-planner/buildkite/pipeline_generator/runtime_shard.py"
        in collect.commands[3]
    )
    assert [rs.decode(c.split()[4]) for c in collect.commands[4:]] == TESTS
    assert plan.depends_on == [collect.key] and plan.allow_dependency_failure
    assert plan.soft_fail and plan.agents == {"queue": "small_cpu_queue_premerge"}
    assert plan.commands[1].endswith(" shadow")
    assert plan.env == {"BUILDKITE_SKIP_CHECKOUT": "true"}  # it never reads the repo
    assert rs.decode(plan.commands[1].split()[-2]) == TESTS
    # The endpoint labels a command by the preview the generator echoes before it.
    assert f"): {rs.command_preview(TESTS[0])}'" in " ".join(main.commands).replace(
        '"', "'"
    )


def test_generator_ignores_the_flag_on_an_ineligible_step(fake_global_config):
    fake_global_config["run_all"] = True
    assert (
        len(_render(_step(automatic_shard=True, commands=[*TESTS, "echo done"]))) == 1
    )
    assert len(_render(_step(automatic_shard=True, num_nodes=2, num_devices=2))) == 1
    fake_global_config["run_all"] = False  # behind a manual block: block + step only
    assert len(_render(_step(automatic_shard=True))) == 2


@pytest.mark.parametrize(
    "device, checkout",
    [
        ("h200_35gb", "/workdir"),  # docker plugin: the checkout's mount point
        # A pod has no /workdir; its checkout is where the agent uploads from.
        ("h100", "$${BUILDKITE_BUILD_CHECKOUT_PATH:-/tmp/fnrec-no-checkout}"),
        ("l4", "$${BUILDKITE_BUILD_CHECKOUT_PATH:-/tmp/fnrec-no-checkout}"),
    ],
)
def test_collect_runs_in_the_steps_own_job_and_writes_to_its_checkout(
    fake_global_config, device, checkout
):
    fake_global_config["run_all"] = True
    [plain] = _render(_step(device=device))
    collect, plan = _render(_step(automatic_shard=True, device=device))
    assert collect.plugins == plain.plugins and collect.agents == plain.agents
    out = f"{checkout}/.runtime-shard/model-executor"
    assert [c.split()[-1] for c in collect.commands[4:]] == [out] * len(TESTS)
    template = rs.decode(plan.env["RUNTIME_SHARD_TEMPLATE"])
    assert template == {"steps": [plain.dict(exclude_none=True)]}


def test_uploaded_template_waits_for_pre_commit_on_a_pull_request(
    fake_global_config,
):
    fake_global_config["run_all"] = True
    fake_global_config["pull_request"] = "123"
    collect, plan = _render(_step(automatic_shard=True))
    group = buildkite_step.BuildkiteGroupStep(group="g", steps=[collect, plan])
    buildkite_step.add_precommit_dependency([group])
    [template] = rs.decode(plan.env["RUNTIME_SHARD_TEMPLATE"])["steps"]
    assert collect.depends_on == ["image-build", "pre-commit"]
    assert template["depends_on"] == ["image-build", "pre-commit"]


def test_collect_writes_node_ids_relative_to_rootdir(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    tests = tmp_path / "tests"
    (tests / "pkg").mkdir(parents=True)
    (tests / "pkg" / "test_a.py").write_text(
        "import pytest\n@pytest.mark.slow_test\ndef test_slow(): pass\ndef test_x(): pass\n"
    )
    command = "pytest -v -s pkg -m 'not slow_test'"
    out = tmp_path / "out"
    subprocess.run(
        [sys.executable, rs.__file__, "collect", "0", rs.encode(command), str(out)],
        cwd=tests,
        check=True,
    )
    entry = json.loads((out / "inventory-0.json").read_text())
    assert entry == {
        "index": 0,
        "command": command,
        "exitstatus": 0,
        "prefix": "tests",
        "nodeids": ["tests/pkg/test_a.py::test_x"],
        "paths": ["pkg"],
    }
    result = rs.plan([entry], None)
    assert result["shards"][0]["commands"][0]["targets"] == ["pkg/test_a.py"]


def test_collect_leaves_inventory_dirs_deletable_by_any_user(tmp_path):
    """Root writes these into the agent's checkout; the agent must delete them."""
    (tmp_path / "test_a.py").write_text("def test_x(): pass\n")
    out = tmp_path / rs.INVENTORY_DIR / "model-executor"
    subprocess.run(
        [sys.executable, rs.__file__, "collect", "0", rs.encode("pytest ."), str(out)],
        cwd=tmp_path,
        check=True,
    )
    for path in (out.parent, out):
        assert path.stat().st_mode & 0o777 == 0o777


def test_run_plan_annotates_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(rs.subprocess, "run", lambda args, check: calls.append(args))
    monkeypatch.setattr(rs, "fetch_timings", lambda key: None)
    rs.run_plan("model-executor", rs.encode(TESTS))  # nothing was downloaded
    assert calls[-1][:5] == [
        "buildkite-agent",
        "annotate",
        "--context",
        "runtime-shard-model-executor",
        "--style",
    ]
    assert calls[-1][5] == "warning" and "no plan" in calls[-1][6]

    out = tmp_path / ".runtime-shard" / "model-executor"
    out.mkdir(parents=True)
    for i, command in enumerate(TESTS):
        entry = _entry(command, {f"f{i}_{j}.py": 1 for j in range(3)})
        (out / f"inventory-{i}.json").write_text(json.dumps({**entry, "exitstatus": 0}))
    rs.run_plan("model-executor", rs.encode(TESTS))
    # No main timings: equal file counts, worth a look.
    assert calls[-1][5] == "warning" and "would run as **4 shards**" in calls[-1][6]
    assert json.loads((out / "plan.json").read_text())["tests"] == 6


def test_generator_on_mode_moves_the_step_into_the_plan(
    fake_global_config, monkeypatch
):
    fake_global_config["run_all"] = True
    [plain] = _render(_step())
    collect, plan = _render(_step(automatic_shard=True))
    assert collect.key == "model-executor-shard-collect"
    # The plan step uploads the step's own job; it fails loudly, never softly.
    assert not plan.soft_fail and plan.allow_dependency_failure
    template = rs.decode(plan.env["RUNTIME_SHARD_TEMPLATE"])
    assert template == {"steps": [plain.dict(exclude_none=True)]}
    [command] = plan.commands
    assert command.startswith("curl ") and " plan model-executor " in command
    assert plan.env["BUILDKITE_SKIP_CHECKOUT"] == "true"
    # Like every job: a lost agent or an infra hook failure is retried once.
    assert (
        plan.retry == collect.retry == buildkite_step.ensure_infra_failure_retry(None)
    )


@pytest.mark.parametrize("uploaded_first", [False, True])
def test_shell_fallback_uploads_the_job_once(
    fake_global_config, tmp_path, uploaded_first
):
    """Run the plan command in a shell whose planner is killed (status 137),
    before or after its own upload. The status is logged either way; the normal
    job is uploaded, with a warning, only if the planner hadn't uploaded it."""
    fake_global_config["run_all"] = True
    _, plan = _render(_step(automatic_shard=True))
    log = tmp_path / "log"
    fakes = {
        "curl": "exit 0",
        "python3": "exit 137",
        "buildkite-agent": f"""echo "$*" >> {log}
case "$1 $2" in
  "step get") [ -n "$STEP_EXISTS" ] && echo running || exit 1 ;;
  "pipeline upload") cat > {tmp_path}/uploaded ;;
esac""",
    }
    for name, body in fakes.items():
        (tmp_path / name).write_text(f"#!/bin/sh\n{body}\n")
        (tmp_path / name).chmod(0o755)
    env = {
        "PATH": f"{tmp_path}:/usr/bin:/bin",
        "RUNTIME_SHARD_TEMPLATE": plan.env["RUNTIME_SHARD_TEMPLATE"],
        **({"STEP_EXISTS": "1"} if uploaded_first else {}),
    }
    # Buildkite turns `$$` into `$` when it uploads the pipeline.
    [command] = [c.replace("$$", "$") for c in plan.commands]
    run = subprocess.run(
        ["sh", "-ec", command], env=env, check=True, capture_output=True, text=True
    )
    failed = "fetching or running the planner failed with status 137"
    assert f"runtime-shard: {failed}" in run.stdout
    calls = log.read_text()
    if uploaded_first:
        assert "annotate" not in calls and "pipeline upload" not in calls
    else:
        assert "annotate --style warning" in calls and f"{failed}." in calls
        uploaded = json.loads((tmp_path / "uploaded").read_text())
        assert uploaded == rs.decode(plan.env["RUNTIME_SHARD_TEMPLATE"])


def test_generator_rollback_switch_and_recording_builds(
    fake_global_config, monkeypatch
):
    fake_global_config["run_all"] = True
    monkeypatch.setenv("VLLM_CI_RUNTIME_SHARD", "off")
    assert [s.key for s in _render(_step(automatic_shard=True))] == ["model-executor"]
    monkeypatch.delenv("VLLM_CI_RUNTIME_SHARD")
    monkeypatch.setattr(buildkite_step, "fnrec_enabled", lambda: True)
    assert [s.key for s in _render(_step(automatic_shard=True))] == ["model-executor"]


def test_retry_keys_of_the_generated_steps_select_the_step(fake_global_config):
    steps = [_step(automatic_shard=True, depends_on=None)]
    for key in ("model-executor-shard-collect", "model-executor-shard-plan"):
        _, selected = select_steps_and_dependencies(steps, frozenset({key}))
        assert selected == frozenset({"model-executor"})


def _inventories(tmp_path):
    out = tmp_path / ".runtime-shard" / "model-executor"
    out.mkdir(parents=True)
    for i, command in enumerate(TESTS):
        entry = _entry(command, {f"f{i}_{j}.py": 1 for j in range(3)})
        (out / f"inventory-{i}.json").write_text(json.dumps({**entry, "exitstatus": 0}))


def _rendered(commands):
    """Commands as the generator renders them: a log header before each, and
    ' turned into "."""
    rendered = []
    for i, command in enumerate(commands):
        preview = rs.command_preview(command)
        rendered.append(
            f'echo "+++ :test_tube: Command ({i + 1}/{len(commands)}): {preview}"'
        )
        rendered.append(command.replace("'", '"'))
    return rendered


def _run_plan_on(tmp_path, monkeypatch, template=None, timings=None):
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(
        rs.subprocess,
        "run",
        lambda args, check, **kw: calls.append((args, kw.get("input"))),
    )
    monkeypatch.setattr(rs, "fetch_timings", lambda key: timings)
    template = template or {
        "label": "ME",
        "key": "model-executor",
        "commands": ["cd /t", *_rendered(TESTS)],
        "env": {"A": "1"},
    }
    monkeypatch.setenv("RUNTIME_SHARD_TEMPLATE", rs.encode({"steps": [template]}))
    monkeypatch.setenv("RUNTIME_SHARD_SCRIPT_URL", "https://x/runtime_shard.py")
    rs.run_plan("model-executor", rs.encode(TESTS), "on")
    [upload] = [
        json.loads(i)["steps"] for a, i in calls if a[1:3] == ["pipeline", "upload"]
    ]
    annotations = [a for a, i in calls if a[1:2] == ["annotate"]]
    return template, upload, annotations[0] if annotations else None


def test_run_plan_uploads_the_step_as_parallel_shards(tmp_path, monkeypatch):
    _inventories(tmp_path)
    template, [step], annotate = _run_plan_on(tmp_path, monkeypatch)
    assert step["key"] == "model-executor" and step["parallelism"] == 4
    assert step["label"] == "ME shard %N/%t"
    assert step["env"] == {"A": "1", "PYTEST_ADDOPTS": "-p runtime_shard"}
    # The plugin's install, then setup as is; each pytest command and its
    # header become one case.
    assert "https://x/runtime_shard.py" in step["commands"][0]
    assert step["commands"][2] == "cd /t" and len(step["commands"]) == 5
    assert all(
        c.startswith('case "$$BUILDKITE_PARALLEL_JOB" in') for c in step["commands"][3:]
    )
    # 6 files, no timings: 4 shards of 1, 2, 1, 2 files
    first = step["commands"][3].split(";;")[0]
    assert "\npytest -v -s -m '(not slow_test)' --timeout=900 f0_0.py\n" in first
    # No main timings, so equal file counts: worth a look, so a warning.
    assert annotate[5] == "warning" and "running as **4 shards**" in annotate[6]


def test_run_plan_leaves_the_build_page_alone_for_a_good_plan(tmp_path, monkeypatch):
    _inventories(tmp_path)
    timings = _timings(TESTS[0], {f"f0_{j}.py": 600 for j in range(3)})
    timings["files"] += _timings(TESTS[1], {f"f1_{j}.py": 600 for j in range(3)})[
        "files"
    ]
    _, [step], annotate = _run_plan_on(tmp_path, monkeypatch, timings=timings)
    assert step["parallelism"] > 1 and annotate is None


def test_run_plan_shards_a_kubernetes_job_with_its_pod_spec_and_env(
    fake_global_config, tmp_path, monkeypatch
):
    fake_global_config["run_all"] = True
    _, plan = _render(_step(automatic_shard=True, device="l4", env={"A": "1"}))
    [template] = rs.decode(plan.env["RUNTIME_SHARD_TEMPLATE"])["steps"]
    assert "kubernetes" in template["plugins"][0]
    _inventories(tmp_path)
    _, [step], _ = _run_plan_on(tmp_path, monkeypatch, template)
    # One Buildkite job per shard, each its own pod from the same pod spec.
    assert step["parallelism"] == 4 and step["plugins"] == template["plugins"]
    assert step["retry"] == template["retry"] == buildkite_step.K8S_RETRY
    assert step["env"] == {**template["env"], "PYTEST_ADDOPTS": "-p runtime_shard"}
    # After the plugin's install, the setup commands' headers stay; each
    # pytest command and its header become one case.
    cases = [c for c in step["commands"] if c.startswith('case "$$BUILDKITE_')]
    assert (
        len(cases) == 2
        and step["commands"][2 : -len(cases)] == template["commands"][:-4]
    )


def test_run_plan_falls_back_to_the_single_job(tmp_path, monkeypatch):
    template, [step], annotate = _run_plan_on(
        tmp_path, monkeypatch
    )  # nothing collected
    assert step == template
    assert annotate[5] == "warning" and "runs as one job" in annotate[6]


def test_run_plan_falls_back_if_a_command_is_not_in_the_job(tmp_path, monkeypatch):
    """A pytest command the shards can't replace would run in full in each."""
    _inventories(tmp_path)
    template = {"label": "ME", "key": "model-executor", "commands": ["cd /t"]}
    template, [step], annotate = _run_plan_on(tmp_path, monkeypatch, template)
    assert step == template
    assert "command 1 is missing" in annotate[6]


def test_collect_ignores_a_trailing_shell_comment(tmp_path):
    """vLLM writes `pytest x.py # needs a clean process`; the shell drops it."""
    (tmp_path / "test_a.py").write_text("def test_x(): pass\n")
    out = tmp_path / "out"
    subprocess.run(
        [
            sys.executable,
            rs.__file__,
            "collect",
            "0",
            rs.encode("pytest -v test_a.py # it needs a clean process"),
            str(out),
        ],
        cwd=tmp_path,
        check=True,
    )
    entry = json.loads((out / "inventory-0.json").read_text())
    assert entry["exitstatus"] == 0 and entry["nodeids"] == ["test_a.py::test_x"]


def _collected(tmp_path, command, files):
    """Collect `command` over these test files, in tmp_path/tests."""
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    tests = tmp_path / "tests"
    for name, text in files.items():
        (tests / name).parent.mkdir(parents=True, exist_ok=True)
        (tests / name).write_text(text)
    out = tmp_path / "out"
    subprocess.run(
        [sys.executable, rs.__file__, "collect", "0", rs.encode(command), str(out)],
        cwd=tests,
        check=True,
    )
    return json.loads((out / "inventory-0.json").read_text())


def _run_shard(step, shard, cwd):
    """Run a shard job's commands as its agent would: $$ is a literal $. The
    plugin loads from this checkout, not its install from GitHub."""
    script = "\n".join(step["commands"][2:]).replace("$$", "$")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update(step["env"], PYTHONPATH=os.path.dirname(rs.__file__))
    env["BUILDKITE_PARALLEL_JOB"] = str(shard)
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env["PATH"]
    return subprocess.run(
        ["bash", "-ec", script], cwd=cwd, env=env, capture_output=True, text=True
    )


def _result(*shards):
    """A plan of one command, from each shard's targets."""
    result = {"shards": []}
    for targets in shards:
        commands = []
        if targets:
            commands.append({"index": 0, "targets": targets, "tests": len(targets)})
        result["shards"].append({"commands": commands})
    return result


def _groups(stdout):
    """The Buildkite log group lines of a job's output."""
    return [l for l in stdout.splitlines() if l[:4] in ("+++ ", "--- ", "^^^ ")]


def test_a_shard_runs_its_files_in_one_pytest_with_a_log_group_each(tmp_path):
    command = "pytest -v pkg -m 'not slow'"
    entry = _collected(
        tmp_path,
        command,
        {
            "pkg/test_a.py": "def test_x(): pass\n",
            "pkg/test_b.py": "def test_y(): pass\ndef test_z(): pass\n",
            "pkg/test_c.py": "def test_v(): pass\ndef test_w(): pass\n",
        },
    )
    assert entry["paths"] == ["pkg"]
    result = _result(
        ["pkg/test_a.py", "pkg/test_b.py::test_y"],
        ["pkg/test_b.py::test_z", "pkg/test_c.py"],
        [],
    )
    step = rs.shard_step({"commands": _rendered([command])}, result, [entry], "u")

    first = _run_shard(step, 0, tmp_path / "tests")
    assert first.returncode == 0, first.stdout + first.stderr
    # One pytest process for the command, not one per file.
    assert first.stdout.count("test session starts") == 1
    assert _groups(first.stdout) == [
        "+++ :test_tube: Command (1/1): 2 files, 2 tests",
        "--- :test_tube: Command (1/1), file 1/2: pkg/test_a.py",
        "--- :test_tube: Command (1/1), file 2/2: pkg/test_b.py   (1 of 2 tests)",
        "+++ :test_tube: Command (1/1): results",
    ]
    # The section starts with the exact command, ready to paste.
    lines = first.stdout.splitlines()
    assert lines[1] == "pytest -v -m 'not slow' pkg/test_a.py pkg/test_b.py::test_y"
    assert "2 passed" in first.stdout and "test_z" not in first.stdout

    second = _run_shard(step, 1, tmp_path / "tests")
    assert second.returncode == 0 and "test_b.py::test_z PASSED" in second.stdout
    # A whole file needs no count, however many tests it has.
    assert _groups(second.stdout)[1:3] == [
        "--- :test_tube: Command (1/1), file 1/2: pkg/test_b.py   (1 of 2 tests)",
        "--- :test_tube: Command (1/1), file 2/2: pkg/test_c.py",
    ]
    assert "3 passed" in second.stdout

    empty = _run_shard(step, 2, tmp_path / "tests")
    # A shard with none of the command's tests skips it without a header.
    assert empty.returncode == 0 and empty.stdout == ""


def test_a_failing_file_fails_the_job_after_the_shards_other_files(tmp_path):
    command = "pytest -v pkg"
    entry = _collected(
        tmp_path,
        command,
        {
            "pkg/test_a.py": "def test_x(): assert 0\n",
            "pkg/test_b.py": "def test_y(): pass\n",
        },
    )
    result = _result(["pkg/test_a.py", "pkg/test_b.py"])
    step = rs.shard_step(
        {"commands": [*_rendered([command]), "echo after"]}, result, [entry], "u"
    )
    run = _run_shard(step, 0, tmp_path / "tests")
    assert run.returncode == 1 and "test_b.py::test_y PASSED" in run.stdout
    # The failing file's group opens; the passing file's stays folded.
    assert _groups(run.stdout) == [
        "+++ :test_tube: Command (1/1): 2 files, 2 tests",
        "--- :test_tube: Command (1/1), file 1/2: pkg/test_a.py",
        "^^^ +++",
        "--- :test_tube: Command (1/1), file 2/2: pkg/test_b.py",
        "+++ :test_tube: Command (1/1): results",
    ]
    # As the step's own failing command would, it stops the job there.
    assert "after" not in run.stdout


def test_a_shard_keeps_the_generators_wrapping_and_a_commands_variables(tmp_path):
    """Tracing labels each file's command by the step command's preview, for
    main's timings; the command's own NAME=value assignments reach collect and
    every shard."""
    command = "N=3 pytest -v test_n.py"
    entry = _collected(
        tmp_path,
        command,
        {
            "test_n.py": "import os, pytest\n"
            "@pytest.mark.parametrize('i', range(int(os.environ.get('N', '1'))))\n"
            "def test_i(i): pass\n"
        },
    )
    assert len(entry["nodeids"]) == 3 and entry["paths"] == ["test_n.py"]
    header, _ = _rendered([command])
    preview = shlex.quote(rs.command_preview(command)).replace("'", '"')
    traced = f"ci_otel_start 1 {preview} || :\n{command}\nstatus=$$?\n(exit $$status)"
    result = _result(
        ["test_n.py::test_i[0]"], ["test_n.py::test_i[1]", "test_n.py::test_i[2]"]
    )
    step = rs.shard_step({"commands": [header, traced]}, result, [entry], "u")
    case = step["commands"][2]
    assert (
        f"\nci_otel_start 1 {preview} || :\n"
        "N=3 pytest -v 'test_n.py::test_i[1]' 'test_n.py::test_i[2]'\n"
        "status=$$?\n(exit $$status)\n"
    ) in case
    step["commands"][2] = case.replace(f"ci_otel_start 1 {preview} || :", ":")
    run = _run_shard(step, 1, tmp_path / "tests")
    assert run.returncode == 0 and "2 passed" in run.stdout
    assert run.stdout.splitlines()[:2] == [
        "+++ :test_tube: Command (1/1): 1 file, 2 tests",
        "N=3 pytest -v 'test_n.py::test_i[1]' 'test_n.py::test_i[2]'",
    ]
    assert (
        "--- :test_tube: Command (1/1), file 1/1: test_n.py   (2 of 3 tests)"
        in _groups(run.stdout)
    )


def test_a_command_that_names_test_ids_runs_only_those(tmp_path):
    command = "pytest -v test_a.py::test_selected test_b.py"
    entry = _collected(
        tmp_path,
        command,
        {
            "test_a.py": "def test_selected(): pass\ndef test_excluded(): assert 0\n",
            "test_b.py": "def test_y(): pass\n",
        },
    )
    result = rs.plan([entry], None)
    rs.check(result, [entry])
    assert result["shards"][0]["commands"][0]["targets"] == ["test_a.py::test_selected"]
    step = rs.shard_step({"commands": _rendered([command])}, result, [entry], "u")
    run = _run_shard(step, 0, tmp_path / "tests")
    assert run.returncode == 0 and "test_excluded" not in run.stdout
    # All of the file's selected tests: no "(1 of 1 tests)".
    assert "--- :test_tube: Command (1/1), file 1/1: test_a.py" in _groups(run.stdout)


def test_a_shard_fails_if_a_planned_test_does_not_run(tmp_path):
    """A conftest hook that drops a test only in the shard jobs: without the
    plugin, the test would run nowhere and every shard would pass."""
    command = "pytest -v pkg"
    files = {
        "pkg/test_a.py": "def test_x(): pass\ndef test_y(): pass\ndef test_z(): pass\n",
        "conftest.py": "import os\n"
        "def pytest_collection_modifyitems(items):\n"
        "    if 'BUILDKITE_PARALLEL_JOB' in os.environ:\n"
        "        items[:] = [i for i in items if 'test_z' not in i.nodeid]\n",
    }
    entry = _collected(tmp_path, command, files)
    assert len(entry["nodeids"]) == 3
    result = _result(["pkg/test_a.py"])
    step = rs.shard_step({"commands": _rendered([command])}, result, [entry], "u")
    run = _run_shard(step, 0, tmp_path / "tests")
    assert run.returncode == 4  # pytest's usage error, as the job's exit status
    assert "1 planned test would not run: ['tests/pkg/test_a.py::test_z']" in (
        run.stderr
    )
