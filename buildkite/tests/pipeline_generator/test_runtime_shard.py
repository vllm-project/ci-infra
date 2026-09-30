import json
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


def test_plan_without_timings_uses_four_equal_shards():
    inventory = [_entry(TESTS[0], {f"model_executor/t{i}.py": 1 for i in range(8)})]
    result = rs.plan(inventory, None)
    rs.check(result, inventory)
    assert [len(s["commands"][0]["targets"]) for s in result["shards"]] == [2, 2, 2, 2]
    assert result["timingSource"] is None and "equal file counts" in rs.annotation(
        "k", result
    )


def test_plan_over_the_ceiling_still_assigns_everything():
    files = {f"model_executor/t{i}.py": 1 for i in range(10)}
    inventory = [_entry(TESTS[0], files)]
    result = rs.plan(inventory, _timings(TESTS[0], {f: 1100 for f in files}))
    rs.check(result, inventory)
    # 5 pairs: a 6th shard can't lower the largest one below two files
    assert len(result["shards"]) == 5 and result["overBudget"]
    assert "still over" in rs.annotation("k", result)


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
    assert plan.commands[1].endswith(" shadow") and plan.env is None
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
    assert len(_render(_step(automatic_shard=True, device="h100"))) == 1  # k8s
    fake_global_config["run_all"] = False  # behind a manual block: block + step only
    assert len(_render(_step(automatic_shard=True))) == 2


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
    # Even if the planner can't start, the normal job is uploaded.
    assert command.endswith(
        '|| (echo "$$RUNTIME_SHARD_TEMPLATE" | base64 -d | buildkite-agent pipeline upload)'
    )


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


def _run_plan_on(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(
        rs.subprocess,
        "run",
        lambda args, check, **kw: calls.append((args, kw.get("input"))),
    )
    monkeypatch.setattr(rs, "fetch_timings", lambda key: None)
    template = {
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


def test_run_plan_falls_back_to_the_single_job(tmp_path, monkeypatch):
    template, [step], annotate = _run_plan_on(
        tmp_path, monkeypatch
    )  # nothing collected
    assert step == template
    assert annotate[5] == "warning" and "runs as one job" in annotate[6]


def _plugin_run(tmp_path, shard_plan, index, target="pkg"):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    pkg = tmp_path / "tests" / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "test_a.py").write_text("def test_x(): pass\n")
    (pkg / "test_b.py").write_text("def test_y(): pass\ndef test_z(): pass\n")
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(Path(rs.__file__).parent),
        "PYTEST_ADDOPTS": "-p runtime_shard",
        "BUILDKITE_PARALLEL_JOB": str(index),
        "RUNTIME_SHARD_PLAN": rs.encode(shard_plan),
    }
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-v", target],
        cwd=tmp_path / "tests",
        env=env,
        capture_output=True,
        text=True,
    )


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
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(Path(rs.__file__).parent),
        "PYTEST_ADDOPTS": "-p runtime_shard",
        "RUNTIME_SHARD_PLAN": rs.encode(shard_plan),
    }
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", "pkg", "-m", "not slow_test"],
        cwd=tmp_path / "tests",
        env=env,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0 and "1 passed, 1 deselected" in run.stdout
    assert "runtime-shard: shard 1/1, command 1: running 1 tests" in run.stdout
