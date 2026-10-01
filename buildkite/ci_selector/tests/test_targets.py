# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Command-shape contract, and what a step really invokes, vs. the real checkout."""

import shlex

import pytest
from ci_selector.codemap.classify import select
from ci_selector.codemap.pipeline.buildkite import load_pipeline_configs, load_steps
from ci_selector.codemap.pipeline.invoked_tests import invoked_files
from ci_selector.codemap.pipeline.scripts import ARG_CUT_RE, PYTEST_LINE_RE, scan_script
from ci_selector.codemap.pipeline.step import LoadReport
from ci_selector.codemap.pipeline.targets import map_step

# Aliased: pytest collects any module-level name starting with `test_`,
# and would report the import itself as a broken test.
from ci_selector.codemap.repo import test_file_catalog as _test_file_catalog
from helpers import drift_message


@pytest.fixture(scope="module")
def mapped(vllm_repo):
    report = LoadReport()
    all_steps = []
    for c in load_pipeline_configs(vllm_repo):
        all_steps.extend(load_steps(vllm_repo, c, report))
    targets = [map_step(vllm_repo, s, script_scanner=scan_script) for s in all_steps]
    invoked = invoked_files(_test_file_catalog(vllm_repo), targets)
    return all_steps, invoked, {t.step_id: t for t in targets}


@pytest.mark.drift
def test_unparsable_empty_at_head(mapped):
    """BENIGN absorbs setup noise; anything left means a command shape
    the parser does not understand."""
    _, _, by_id = mapped
    leftovers = {sid: t.unparsable for sid, t in by_id.items() if t.unparsable}
    assert leftovers == {}, drift_message(
        "These step commands use a shape the target parser does not "
        f"understand: {leftovers}",
        "We cannot tell which tests those commands run, so the step is selected "
        "by weaker signals than it should be.",
        "it is setup noise that runs no test: add the command to BENIGN_CMDS "
        "in ci_selector/handwritten.py",
        "it wraps another command (like `uv run pytest`): add it to UNWRAP_CMDS "
        "in ci_selector/handwritten.py",
        "it is a genuinely new way of invoking tests: teach "
        "ci_selector/codemap/pipeline/targets.py to read it",
    )


def test_dangling_is_bounded_and_explained(mapped):
    _, _, by_id = mapped
    dangling = {d for t in by_id.values() for d in t.dangling}
    # run-npu-test.sh references a path that only exists in the external
    # vllm-ascend CI checkout; anything else is parser skew or YAML rot.
    assert dangling <= {"tests/e2e/vllm_interface/"}, dangling


def test_lora_shard_ignores_recorded_not_applied(mapped):
    """--ignore narrows the shard but never shrinks the target set."""
    _, _, by_id = mapped
    st = by_id["vllm_ci:lora"]
    assert any(t.path == "tests/lora" for t in st.targets)
    assert "tests/lora/test_llama_tp.py" in st.ignored


def test_find_xargs_shape(mapped):
    """pytorch.yaml `find compile/ ... | xargs pytest {}`."""
    _, _, by_id = mapped
    finds = [
        t
        for st in by_id.values()
        for t in st.targets
        if st.step_id.startswith("vllm_ci:")
        and t.path.startswith("tests/compile")
        and any(n.startswith("find-exclude:") for n in t.narrowing)
    ]
    assert finds, "expected find|xargs-derived tests/compile targets"


def test_wrapped_block_extraction(mapped):
    """run-cpu-test.sh receives its pytest block as an argv string."""
    _, _, by_id = mapped
    cpu_kernel = next(
        st
        for sid, st in by_id.items()
        if st.step_id.startswith("vllm_ci:")
        and any(t.path == "tests/kernels/test_onednn.py" for t in st.targets)
    )
    assert any("run-cpu-test.sh" in s for s in cpu_kernel.scripts_seen)


def test_script_recursion_reaches_nixl_tests(mapped):
    """Disaggregated jobs invoke tests/**/*.sh whose pytest lines carry
    ${GIT_ROOT}-prefixed paths."""
    _, _, by_id = mapped
    nixl = "tests/v1/kv_connector/nixl_integration/test_accuracy.py"
    assert any(t.path == nixl for st in by_id.values() for t in st.targets)


def test_config_list_files_become_data_edges(mapped):
    """lm_eval coverage is the .txt list, resolved against the test
    file's directory."""
    _, _, by_id = mapped
    data = {d for st in by_id.values() for d in st.data_files}
    assert any(
        d.startswith("tests/evals/") and d.endswith("models-small.txt") for d in data
    ), sorted(data)


def test_pipe_then_and_chain_not_lost(mapped):
    """`torchrun x | grep ok && pytest y` keeps y (multi-node block shape)."""
    _, invoked, _ = mapped
    assert "tests/distributed/test_multi_node_assignment.py" in invoked
    assert "tests/distributed/test_node_count.py" in invoked


def _wrap_step(commands):
    from ci_selector.codemap.pipeline.step import Step

    return Step(
        pipeline="t",
        source_file="x.yaml",
        label="wrap",
        key="wrap",
        group=None,
        commands=commands,
        source_file_dependencies=None,
    )


def _map(vllm_repo, command):
    from ci_selector.codemap.pipeline.targets import map_step

    return map_step(vllm_repo, _wrap_step([command]))


def test_uv_run_wrapper_unwraps(vllm_repo):
    st = _map(vllm_repo, "uv run pytest -v lora/test_llama_tp.py")
    assert [t.path for t in st.targets] == ["tests/lora/test_llama_tp.py"]
    assert not st.unparsable


def test_uv_non_run_subcommand_not_unwrapped(vllm_repo):
    """uv is also a benign command; only `run` unwraps, so a missing `run`
    must not phantom-target the argv."""
    st = _map(vllm_repo, "uv pytest lora/test_llama_tp.py")
    assert not st.targets and not st.unparsable


def test_sudo_wrapped_pytest(vllm_repo):
    st = _map(vllm_repo, "sudo -E pytest lora/test_llama_tp.py")
    assert [t.path for t in st.targets] == ["tests/lora/test_llama_tp.py"]
    assert not st.unparsable


def test_zero_match_glob_is_dangling(vllm_repo):
    """A glob matching nothing is the same stale hole as a rename; recording
    neither a target nor a dangling hid it from the preflight escalation."""
    st = _map(vllm_repo, "pytest -v lora/test_no_such_thing_*.py")
    assert not st.targets
    assert st.dangling == ["lora/test_no_such_thing_*.py"]


def test_matching_glob_still_targets(vllm_repo):
    st = _map(vllm_repo, "pytest -v lora/test_llama_*.py")
    assert "tests/lora/test_llama_tp.py" in [t.path for t in st.targets]
    assert not st.dangling


def test_foreign_absolute_path_is_dangling(vllm_repo):
    """A container path outside the workspace root used to resolve to '' (the
    vllm_repo root), which matched nothing downstream while silencing both the
    dangling escalation and the zero-target warning."""
    st = _map(vllm_repo, "pytest -v /workspace/tests/e2e/singlecard/test_offline.py")
    assert not st.targets
    assert st.dangling


def test_uv_global_flags_before_run_unwrap(vllm_repo):
    for cmd in (
        "uv -q run pytest lora/test_llama_tp.py",
        "uv --directory /vllm-workspace run pytest lora/test_llama_tp.py",
    ):
        st = _map(vllm_repo, cmd)
        assert [t.path for t in st.targets] == ["tests/lora/test_llama_tp.py"], cmd
        assert not st.unparsable


def test_docker_compose_wrapped_test_unparsable(vllm_repo):
    for cmd in (
        "docker compose run svc pytest tests/lora/test_llama_tp.py",
        "docker-compose exec svc pytest tests/lora/test_llama_tp.py",
        "docker --context prod run img pytest tests/lora/test_llama_tp.py",
    ):
        st = _map(vllm_repo, cmd)
        assert st.unparsable and not st.targets, cmd


def test_continuation_block_keeps_every_shard_path(vllm_repo):
    """A sharded pytest inside a quoted argv block, one path per continued
    line. Written out here so it survives upstream rewriting the yaml."""
    st = _map(
        vllm_repo,
        'bash .buildkite/scripts/hardware_ci/run-cpu-test.sh 30m "\n'
        "pytest -x -v -s --num-shards=$$COUNT --shard-id=$$JOB \\\n"
        "tests/lora/test_llama_tp.py \\\n"
        'tests/kernels/test_onednn.py::TestOneDNN"\n',
    )
    assert [t.path for t in st.targets] == [
        "tests/lora/test_llama_tp.py",
        "tests/kernels/test_onednn.py",
    ]
    assert not st.unparsable and not st.dangling
    assert any("run-cpu-test.sh" in s for s in st.scripts_seen)


def test_top_level_continuation_is_one_command(vllm_repo):
    """An unquoted continuation used to read as a quoted block argument and
    mark the step dangling."""
    st = _map(vllm_repo, "pytest -v \\\n  lora/test_llama_tp.py")
    assert [t.path for t in st.targets] == ["tests/lora/test_llama_tp.py"]
    assert not st.unparsable and not st.dangling


def test_script_pytest_keeps_continued_arguments(tmp_path):
    """A pytest in a shell script with its paths on continuation lines."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    (tmp_path / "tests" / "test_b.py").write_text("def test_b(): pass\n")
    (tmp_path / "run.sh").write_text(
        "pytest -x \\\n  tests/test_a.py \\\n  tests/test_b.py\n"
    )
    st = map_step(tmp_path, _wrap_step(["bash run.sh"]), script_scanner=scan_script)
    assert sorted(t.path for t in st.targets) == [
        "tests/test_a.py",
        "tests/test_b.py",
    ]
    assert not st.dangling and not st.unlexable


def _lex_failure(entry: str) -> str:
    """Why shlex refused the argv of an unlexable entry, re-derived from it."""
    args = ARG_CUT_RE.split(PYTEST_LINE_RE.search(entry).group(1))[0].rstrip()
    try:
        shlex.split(args)
    except ValueError as exc:
        return str(exc)
    return "lexed cleanly"


def test_unlexable_entries_are_quoting_artifacts(mapped):
    """What survives the trailing-quote recovery is still a quote our own line
    slicing opened and did not close; a "No escaped character" would mean
    continuations stopped being joined, which is a parse defect rather than a
    slicing one. Floorless on purpose: empty is the ideal state for this
    bucket, and a floor would keep a defect alive to satisfy it."""
    _, _, by_id = mapped
    for st in by_id.values():
        for entry in st.unlexable:
            assert "quotation" in _lex_failure(entry), (st.step_id, entry)


def test_trailing_payload_quote_is_recovered(tmp_path):
    """The last pytest line of a `bash -c "..."` payload carries the payload's
    own closing quote, so the sliced segment holds one quote too many. Three
    real arm-cpu-test targets were dropped there without a target or an
    escalation to show for it."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    (tmp_path / "run.sh").write_text(
        'docker exec ci bash -c "\n'
        "  set -e\n"
        "  pytest -x -v -s tests/test_a.py\n"
        '  pytest -x -v -s tests/test_a.py::test_a"\n'
    )
    st = map_step(tmp_path, _wrap_step(["bash run.sh"]), script_scanner=scan_script)
    assert [t.path for t in st.targets] == ["tests/test_a.py"] * 2
    assert not st.unlexable and not st.dangling


def test_genuinely_unbalanced_quote_still_recorded(tmp_path):
    """The retry drops one trailing quote, not any quote, so a line that never
    closes its quote stays unlexable."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a(): pass\n")
    (tmp_path / "run.sh").write_text('pytest -k "slow tests/test_a.py\n')
    st = map_step(tmp_path, _wrap_step(["bash run.sh"]), script_scanner=scan_script)
    assert not st.targets
    assert st.unlexable == ['pytest -k "slow tests/test_a.py']


def _console_repo(tmp_path, entry="vllm.entrypoints.cli.main:main"):
    (tmp_path / "pyproject.toml").write_text(f'[project.scripts]\nvllm = "{entry}"\n')
    main = tmp_path / "vllm" / "entrypoints" / "cli" / "main.py"
    main.parent.mkdir(parents=True)
    main.write_text("def main(): pass\n")
    return "vllm/entrypoints/cli/main.py"


def test_a_console_script_targets_its_entry_module(tmp_path):
    """`vllm serve` runs the module pyproject.toml names for `vllm`, as
    `python x.py` runs x.py, from a step command or a script it runs. Prose
    naming the package is not a call: each false hit would run its step on
    nearly every change, since the entry imports most of the tree."""
    entry = _console_repo(tmp_path)
    (tmp_path / "run.sh").write_text(
        "pip install vllm --pre\n"
        'echo "starting vllm serve"\n'
        "docker run -v ~/.cache/vllm:/root/.cache/vllm --rm img\n"
        "Ray cannot accommodate this vllm version.\n"
        'CMD=(vllm serve "$MODEL")\n'
        "out=$(vllm collect-env)\n"
        'run "create" 1200 "${WRAP[@]}" vllm snapshot create m \\\n  --out /tmp/a\n'
    )
    st = map_step(
        tmp_path,
        _wrap_step(["vllm serve m --port 8000", "bash run.sh"]),
        script_scanner=scan_script,
    )
    assert [(t.path, t.via) for t in st.targets] == [(entry, None)] + [
        (entry, "run.sh")
    ] * 3
    assert not st.unparsable


def test_a_background_command_ends_at_its_ampersand(tmp_path):
    """Before `vllm` parsed, `vllm serve m & pytest y` was unparsable and ran
    its step on every PR. Parsed, the `&` has to end the server call, or y is
    read as its argument and a change to y selects nothing. `sleep 5 & pytest
    y` already lost y that way."""
    entry = _console_repo(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("")
    for server, targets in (
        ("vllm serve m --port 8000", [entry, "tests/test_x.py"]),
        ("sleep 5", ["tests/test_x.py"]),
    ):
        st = map_step(tmp_path, _wrap_step([f"{server} & pytest -v tests/test_x.py"]))
        assert [t.path for t in st.targets] == targets
        assert not st.unparsable


def test_a_console_script_with_no_entry_file_stays_unknown(tmp_path):
    """An entry this checkout cannot resolve to a file routes nothing, so the
    command is still unparsable and preflight still runs the step."""
    _console_repo(tmp_path, entry="vllm.gone:main")
    st = map_step(tmp_path, _wrap_step(["vllm serve m"]))
    assert not st.targets
    assert st.unparsable == ["vllm serve m"]


def test_a_vllm_cli_step_routes_from_the_modules_its_entry_runs(state):
    """A step whose script only runs `vllm serve` reaches the subcommand
    modules and what they import, through the entry module. vllm#58978 edited
    vllm/collect_env.py, and select() on it or on vllm/entrypoints/cli/ never
    named deepseek-v2-lite-prefetch-offload-accuracy-h100."""
    step = "vllm_ci:deepseek-v2-lite-prefetch-offload-accuracy-h100"
    entry = "vllm/entrypoints/cli/main.py"
    runners = {
        sid
        for p in state.pipelines
        for sid, st in p.targets.items()
        if any(t.path == entry for t in st.targets)
    }
    assert len(runners) >= 10 and step in runners, sorted(runners)
    for path in (
        entry,
        "vllm/entrypoints/cli/serve.py",
        "vllm/entrypoints/cli/collect_env.py",
        "vllm/collect_env.py",
    ):
        assert step in select(state, [path]).selected, path
