import json
import os
import subprocess
import sys
from pathlib import Path

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
    return {
        "command": command,
        "prefix": prefix,
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
    assert calls[-1][5] == "info" and "would run as **4 shards**" in calls[-1][6]
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


def _run_plan_on(tmp_path, monkeypatch, template=None):
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(
        rs.subprocess,
        "run",
        lambda args, check, **kw: calls.append((args, kw.get("input"))),
    )
    monkeypatch.setattr(rs, "fetch_timings", lambda key: None)
    template = template or {
        "label": "ME",
        "key": "model-executor",
        "commands": ["cd /t", "pytest x"],
        "env": {"A": "1"},
    }
    monkeypatch.setenv("RUNTIME_SHARD_TEMPLATE", rs.encode({"steps": [template]}))
    monkeypatch.setenv("RUNTIME_SHARD_SCRIPT_URL", "https://example/runtime_shard.py")
    rs.run_plan("model-executor", rs.encode(TESTS), "on")
    [upload] = [
        json.loads(i)["steps"] for a, i in calls if a[1:3] == ["pipeline", "upload"]
    ]
    return template, upload, calls[-1][0]


def test_run_plan_uploads_the_step_as_parallel_shards(tmp_path, monkeypatch):
    _inventories(tmp_path)
    template, [step], annotate = _run_plan_on(tmp_path, monkeypatch)
    assert step["key"] == "model-executor" and step["parallelism"] == 4
    assert step["label"] == "ME shard %N/%t"
    assert step["commands"][2:] == template["commands"]
    assert "https://example/runtime_shard.py" in step["commands"][0]
    assert (
        step["env"]["A"] == "1" and step["env"]["PYTEST_ADDOPTS"] == "-p runtime_shard"
    )
    shard_plan = rs.decode(step["env"]["RUNTIME_SHARD_PLAN"])
    assert shard_plan["commands"] == TESTS
    # 6 files, no timings: 4 shards of 1, 2, 1, 2 files
    assert shard_plan["shards"][0] == [{"index": 0, "targets": ["tests/f0_0.py"]}]
    assert annotate[5] == "info" and "running as **4 shards**" in annotate[6]


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
    assert template["env"].items() <= step["env"].items()
    assert step["env"]["PYTEST_ADDOPTS"] == "-p runtime_shard"
    assert rs.decode(step["env"]["RUNTIME_SHARD_PLAN"])["commands"] == TESTS


def test_run_plan_falls_back_to_the_single_job(tmp_path, monkeypatch):
    template, [step], annotate = _run_plan_on(
        tmp_path, monkeypatch
    )  # nothing collected
    assert step == template
    assert annotate[5] == "warning" and "runs as one job" in annotate[6]


def _plugin_env(shard_plan, **extra):
    """This interpreter's environment plus the plugin's inputs. A bare env
    drops settings such as LD_LIBRARY_PATH that a CI runner's interpreter
    needs to find its own site-packages, and so pytest."""
    env = {}
    for name, value in os.environ.items():
        if not name.startswith(("BUILDKITE_", "RUNTIME_SHARD_", "PYTEST_")):
            env[name] = value
    env.update(
        PYTHONPATH=str(Path(rs.__file__).parent),
        PYTEST_ADDOPTS="-p runtime_shard",
        RUNTIME_SHARD_PLAN=rs.encode(shard_plan),
        **extra,
    )
    return env


def _plugin_run(tmp_path, shard_plan, index, target="pkg"):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    pkg = tmp_path / "tests" / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "test_a.py").write_text("def test_x(): pass\n")
    (pkg / "test_b.py").write_text("def test_y(): pass\ndef test_z(): pass\n")
    env = _plugin_env(shard_plan, BUILDKITE_PARALLEL_JOB=str(index))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-v", target],
        cwd=tmp_path / "tests",
        env=env,
        capture_output=True,
        text=True,
    )


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


def test_plugin_matches_a_command_with_a_trailing_shell_comment(tmp_path):
    shard_plan = {
        "commands": ["pytest -v pkg # needs a clean process"],
        "shards": [
            [{"index": 0, "targets": ["tests/pkg/test_a.py"]}],
            [{"index": 0, "targets": ["tests/pkg/test_b.py"]}],
        ],
    }
    run = _plugin_run(tmp_path, shard_plan, 0)
    assert run.returncode == 0 and "1 passed, 2 deselected" in run.stdout


def test_plugin_runs_only_this_shards_tests(tmp_path):
    shard_plan = {
        "commands": ["pytest -v pkg"],
        "shards": [
            [
                {
                    "index": 0,
                    "targets": ["tests/pkg/test_a.py", "tests/pkg/test_b.py::test_y"],
                }
            ],
            [{"index": 0, "targets": ["tests/pkg/test_b.py::test_z"]}],
            [],  # no tests of this command in the last shard
        ],
    }
    first = _plugin_run(tmp_path, shard_plan, 0)
    assert first.returncode == 0 and "2 passed, 1 deselected" in first.stdout
    assert "test_z" not in first.stdout.split("deselected")[0].split("collected")[1]
    second = _plugin_run(tmp_path, shard_plan, 1)
    assert second.returncode == 0 and "1 passed, 2 deselected" in second.stdout
    empty = _plugin_run(tmp_path, shard_plan, 2)
    assert empty.returncode == 0 and "3 deselected" in empty.stdout


def test_plugin_runs_tests_no_shard_was_given_in_shard_1(tmp_path):
    shard_plan = {  # test_b.py was collected but planned nowhere
        "commands": ["pytest -v pkg"],
        "shards": [[], [{"index": 0, "targets": ["tests/pkg/test_a.py"]}]],
    }
    first = _plugin_run(tmp_path, shard_plan, 0)
    assert first.returncode == 0 and "2 passed, 1 deselected" in first.stdout
    assert "2 collected tests are in no shard; running them here" in first.stdout
    second = _plugin_run(tmp_path, shard_plan, 1)
    assert second.returncode == 0 and "1 passed, 2 deselected" in second.stdout
    assert "no shard; shard 1 runs them" in second.stdout


def test_plugin_fails_loudly_rather_than_run_the_wrong_tests(tmp_path):
    missing = {
        "commands": ["pytest -v pkg"],
        "shards": [[{"index": 0, "targets": ["tests/pkg/test_gone.py"]}]],
    }
    assert _plugin_run(tmp_path, missing, 0).returncode not in (0, 5)
    other = {"commands": ["pytest -v other"], "shards": [[]]}
    assert _plugin_run(tmp_path, other, 0).returncode not in (0, 5)


def test_plugin_reports_the_count_after_the_commands_own_filters(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = slow_test\n")
    pkg = tmp_path / "tests" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "test_a.py").write_text(
        "import pytest\ndef test_x(): pass\n@pytest.mark.slow_test\ndef test_s(): pass\n"
    )
    shard_plan = {
        "commands": ["pytest -v pkg -m 'not slow_test'"],
        "shards": [[{"index": 0, "targets": ["tests/pkg/test_a.py"]}]],
    }
    env = _plugin_env(shard_plan)
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", "pkg", "-m", "not slow_test"],
        cwd=tmp_path / "tests",
        env=env,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0 and "1 passed, 1 deselected" in run.stdout
    assert "runtime-shard: shard 1/1, command 1: running 1 tests" in run.stdout


def test_plugin_does_not_count_a_test_the_commands_own_filter_drops(tmp_path):
    """The collect step applied -m, so the plan left the slow test out."""
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = slow_test\n")
    pkg = tmp_path / "tests" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "test_a.py").write_text(
        "import pytest\ndef test_x(): pass\n@pytest.mark.slow_test\ndef test_s(): pass\n"
    )
    shard_plan = {
        "commands": ["pytest -v pkg -m 'not slow_test'"],
        "shards": [[{"index": 0, "targets": ["tests/pkg/test_a.py::test_x"]}]],
    }
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", "pkg", "-m", "not slow_test"],
        cwd=tmp_path / "tests",
        env=_plugin_env(shard_plan),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0 and "1 passed, 1 deselected" in run.stdout
    assert "in no shard" not in run.stdout and "test_s PASSED" not in run.stdout
