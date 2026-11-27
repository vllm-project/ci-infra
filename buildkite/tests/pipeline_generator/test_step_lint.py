"""The lint of `devices:` steps (step_lint.py) with `--fix`, and the
`pipeline-generator explain|lint` commands (steps_cli.py)."""

import json

import pytest
import yaml
from click.testing import CliRunner

import main as main_module
from step_lint import fix_text, lint_paths, lint_text
from step_vendors import compile_device_step

HEADER = "group: Spec Decode\ndepends_on:\n  - image-build\nsteps:\n"


def _codes(text):
    return sorted({finding.code for finding in lint_text(HEADER + text, "t.yaml")})


def _fix(text):
    fixed, refused = fix_text(HEADER + text, "t.yaml")
    assert not refused, [str(r) for r in refused]
    assert fixed.startswith(HEADER)
    return fixed[len(HEADER) :]


def _compiled(text):
    doc = yaml.safe_load(HEADER + text)
    out = []
    for raw in doc["steps"]:
        for step in compile_device_step(raw, doc["depends_on"]):
            for block in (step, step.get("mirror", {}).get("amd", {})):
                if "source_file_dependencies" in block:
                    block["source_file_dependencies"] = sorted(
                        block["source_file_dependencies"]
                    )
            out.append(step)
    return sorted(out, key=lambda step: step["key"])


CLEAN = """\
- label: V1 Spec Decode
  key: v1-spec-decode
  devices:
    h200_35gb:
      timeout_in_minutes: 40
    mi300_dpx:
      timeout_in_minutes: 50
  automatic_shard: true
  source_file_dependencies:
    - vllm/v1/
  commands:
    - pytest -v -s v1/spec_decode
"""


def test_a_clean_step_has_no_findings():
    assert _codes(CLEAN) == []
    assert _fix(CLEAN) == CLEAN


# ---- L1 hoist ---------------------------------------------------------------------

HOIST = """\
- label: V1 Spec Decode
  key: v1-spec-decode
  devices:
    h200_35gb:
      timeout_in_minutes: 40
      env:
        SHARED: "1"
        NV: "x"
      source_file_dependencies:
        # the core
        - vllm/v1/
        - vllm/nvidia/
      # the same everywhere
      commands:
        - export VLLM_WORKER_MULTIPROC_METHOD=spawn
        - >-
          pytest -v -s
          v1/spec_decode
    mi300_dpx:
      timeout_in_minutes: 50
      env:
        SHARED: "1"
      source_file_dependencies:
        - vllm/v1/
        - vllm/platforms/rocm.py
      commands:
        - export VLLM_WORKER_MULTIPROC_METHOD=spawn
        - pytest -v -s v1/spec_decode
    mi355_dpx:
      same_as: mi300_dpx
      timeout_in_minutes: 70
  automatic_shard: true
  source_file_dependencies:
    - tests/v1/spec_decode
"""
HOISTED = """\
- label: V1 Spec Decode
  key: v1-spec-decode
  devices:
    h200_35gb:
      timeout_in_minutes: 40
      env:
        NV: "x"
      source_file_dependencies:
        - vllm/nvidia/
    mi300_dpx:
      timeout_in_minutes: 50
      source_file_dependencies:
        - vllm/platforms/rocm.py
    mi355_dpx:
      same_as: mi300_dpx
      timeout_in_minutes: 70
  automatic_shard: true
  env:
    SHARED: "1"
  source_file_dependencies:
    - tests/v1/spec_decode
    # the core
    - vllm/v1/
  # the same everywhere
  commands:
    - export VLLM_WORKER_MULTIPROC_METHOD=spawn
    - >-
      pytest -v -s
      v1/spec_decode
"""


def test_l1_finds_what_every_device_has():
    messages = [f.message for f in lint_text(HEADER + HOIST, "t.yaml")]
    assert any("`source_file_dependencies` 'vllm/v1/'" in m for m in messages)
    assert any("`env` ('SHARED', '1')" in m for m in messages)
    assert any("every device has `commands`" in m for m in messages)
    assert _codes(HOIST) == ["L1"]


def test_l1_fix_moves_the_text_with_its_comments():
    assert _fix(HOIST) == HOISTED
    assert _compiled(HOISTED) == _compiled(HOIST)
    assert _codes(HOISTED) == []


def test_l1_needs_two_devices():
    single = """\
- label: T
  key: t
  devices:
    h100:
      commands:
        - pytest
"""
    assert _codes(single) == []


def _comment_texts(text):
    out = []
    for line in text.splitlines():
        if "#" in line:
            out.append(line[line.index("#") :].strip())
    return sorted(out)


def test_l1_fix_keeps_the_comments_of_every_device():
    text = """\
- label: T1
  key: t1
  devices:
    h100:
      timeout_in_minutes: 10
      # shared deps (nvidia)
      source_file_dependencies:
        # the x dir
        - x/
      # run it
      commands:
        - pytest a
    mi355_1:
      timeout_in_minutes: 20
      # shared deps (amd)
      source_file_dependencies:
        # amd x dir
        - x/  # inline amd
      # amd run note
      commands:
        - pytest a  # amd inline cmd
"""
    fixed = _fix(text)
    assert (
        fixed
        == """\
- label: T1
  key: t1
  devices:
    h100:
      timeout_in_minutes: 10
    mi355_1:
      timeout_in_minutes: 20
  # shared deps (nvidia)
  # shared deps (amd)
  source_file_dependencies:
    # the x dir
    # amd x dir
    # inline amd
    - x/
  # run it
  # amd run note
  # amd inline cmd
  commands:
    - pytest a
"""
    )
    assert _comment_texts(fixed) == _comment_texts(text)
    assert _compiled(fixed) == _compiled(text)
    assert _codes(fixed) == []


def test_l1_fix_drops_no_comment():
    """Every comment of a step with comments everywhere a hoist touches (on
    items, above fields it empties or keeps, after the last item, inside a
    command) is still there after the fix."""
    text = """\
- label: T
  key: t
  devices:
    h100:
      # h100 env
      env:
        # h100 shared
        SHARED: "1"
        NV: "1"  # nv only
      # h100 deps
      source_file_dependencies:
        - a/  # h100 a
        # h100 b
        - b/
        # h100 after the list
      commands:
        - pytest  # h100 cmd
    b200:
      # b200 env
      env:
        SHARED: "1"  # b200 shared
      source_file_dependencies:
        # b200 a
        - a/
        - b/
      # b200 commands
      commands:
        # inside b200 commands
        - pytest
    mi355_1:
      env:
        # amd shared
        SHARED: "1"
        AMD: "1"
      # amd deps
      source_file_dependencies:
        - a/
        - rocm/  # amd only
      commands:
        - pytest
  # the step label
"""
    fixed = _fix(text)
    assert _comment_texts(fixed) == _comment_texts(text)
    assert _compiled(fixed) == _compiled(text)
    assert _codes(fixed) == []


def test_l1_fix_keeps_a_comment_written_twice_once():
    text = """\
- label: T
  key: t
  devices:
    h100:
      # the tests
      commands:
        - pytest
    mi355_1:
      # the tests
      commands:
        - pytest
"""
    assert (
        _fix(text)
        == """\
- label: T
  key: t
  devices:
    h100:
    mi355_1:
  # the tests
  commands:
    - pytest
"""
    )


def test_a_fix_that_would_drop_a_comment_is_refused(monkeypatch):
    import step_lint

    monkeypatch.setattr(step_lint, "_comment_lines", lambda *args: [])
    text = """\
- label: T
  key: t
  devices:
    h100:
      commands:
        - pytest
    mi355_1:
      # amd only
      commands:
        - pytest
"""
    fixed, refused = fix_text(HEADER + text, "t.yaml")
    assert fixed == HEADER + text
    assert ["drop the comment '# amd only'" in r.message for r in refused] == [True]


# ---- L2 same_as ---------------------------------------------------------------------

SAME = """\
- label: T
  key: t
  devices:
    h100:
      timeout_in_minutes: 30
      commands:
        - pytest nvidia
    mi355_1:
      timeout_in_minutes: 60
      commands:
        - pytest amd
    mi300_1:
      # copied from mi355_1
      timeout_in_minutes: 60
      commands:
        - pytest amd
      variants:
        aiter:
          env:
            AITER: "1"
"""


def test_l2_and_its_fix():
    assert _codes(SAME) == ["L2"]
    fixed = _fix(SAME)
    assert fixed == SAME.replace(
        """      # copied from mi355_1
      timeout_in_minutes: 60
      commands:
        - pytest amd
""",
        """      # copied from mi355_1
      same_as: mi355_1
""",
    )
    assert _compiled(fixed) == _compiled(SAME)


def test_l2_fix_keeps_the_comments_inside_what_same_as_replaces():
    text = SAME.replace(
        "        - pytest amd\n      variants:",
        "        # the amd suite\n        - pytest amd  # all of it\n      variants:",
    )
    fixed = _fix(text)
    assert (
        """      # copied from mi355_1
      # the amd suite
      # all of it
      same_as: mi355_1
"""
        in fixed
    )
    assert _comment_texts(fixed) == _comment_texts(text)
    assert _compiled(fixed) == _compiled(text)


def test_l2_ignores_another_vendor_and_empty_entries():
    text = """\
- label: T
  key: t
  devices:
    h100:
      timeout_in_minutes: 30
    mi355_1:
      timeout_in_minutes: 30
  commands:
    - pytest
"""
    assert _codes(text) == []
    empty = text.replace("      timeout_in_minutes: 30\n", "")
    assert _codes(empty) == []


# ---- L3 ----------------------------------------------------------------------------


def test_l3_warns_about_covered_dependencies_and_is_not_fixed():
    text = """\
- label: T
  key: t
  devices:
    h100:
      source_file_dependencies:
        - vllm/models/x/
        - "!vllm/models/x/skip.py"
    mi355_1:
      source_file_dependencies:
        - csrc/
        - csrc/rocm/
  source_file_dependencies:
    - vllm/
    - vllm/config.py
  commands:
    - pytest
"""
    findings = lint_text(HEADER + text, "t.yaml")
    messages = sorted(f.message for f in findings if f.code == "L3")
    assert messages == [
        "step t: `vllm/config.py` is already covered by `vllm/`",
        "step t: device h100: `vllm/models/x/` is already covered by `vllm/`",
        "step t: device mi355_1: `csrc/rocm/` is already covered by `csrc/`",
    ]
    assert not any(f.is_error for f in findings)
    assert _fix(text) == text


# ---- L4 order ---------------------------------------------------------------------

UNORDERED = """\
- label: T
  # the commands
  commands:
    - pytest
  devices:
    # CPU first
    cpu-small:
      env:
        CPU: "1"
    mi355_1:
      env:
        AMD: "1"
      timeout_in_minutes: 60
    h100:
      variants:
        eager:
          env:
            EAGER: "1"
          optional: true
      timeout_in_minutes: 30
  key: t
"""
ORDERED = """\
- label: T
  key: t
  devices:
    h100:
      key: t-h100
      timeout_in_minutes: 30
      variants:
        eager:
          optional: true
          env:
            EAGER: "1"
    mi355_1:
      timeout_in_minutes: 60
      env:
        AMD: "1"
    # CPU first
    cpu-small:
      key: t
      env:
        CPU: "1"
  # the commands
  commands:
    - pytest
"""


def test_l4_and_its_fix_keep_every_key():
    assert _codes(UNORDERED) == ["L4"]
    fixed = _fix(UNORDERED)
    assert fixed == ORDERED
    # The CPU device keeps `t` (and the mirror), the NVIDIA one `t-h100`.
    assert _compiled(fixed) == _compiled(UNORDERED)
    assert _codes(fixed) == []


def test_a_fix_that_would_change_a_job_is_refused(monkeypatch):
    import step_lint

    # A reorder that forgets to pin the keys it changes: the CPU job would
    # lose `t` to the H100 one.
    keys_of = step_lint._keys_of
    monkeypatch.setattr(
        step_lint,
        "_keys_of",
        lambda raw, order: keys_of(raw, sorted(order, key=step_lint._device_rank)),
    )
    fixed, refused = fix_text(HEADER + UNORDERED, "t.yaml")
    assert fixed == HEADER + UNORDERED
    assert [r.message for r in refused] == [
        "the fix would change a job; fix it by hand"
    ]


def test_a_fix_that_would_not_compile_is_refused(monkeypatch):
    import step_lint

    order_fix = step_lint._order_fix

    def broken_order_fix(step, raw):
        order_fix(step, raw)
        step.block.entries.append(step_lint.Entry("bogus", [], ["  bogus: 1\n"]))

    monkeypatch.setattr(step_lint, "_order_fix", broken_order_fix)
    fixed, refused = fix_text(HEADER + UNORDERED, "t.yaml")
    assert fixed == HEADER + UNORDERED
    assert len(refused) == 1
    assert refused[0].message.startswith("the fix would not compile")


ALIASED = """\
- label: First
  key: first
  devices:
    h100:
  source_file_dependencies: &shared-deps
    - vllm/
  commands:
    - pytest -v -s a
- label: Second
  key: second
  devices:
    h100:
      commands:
        - pytest -v -s b
    mi300_1:
      commands:
        - pytest -v -s b
  source_file_dependencies: *shared-deps
"""


def test_a_step_with_an_anchor_or_alias_is_refused_not_crashed():
    # The fixer reads one step's text, where an alias has no anchor.
    text = ALIASED + HOIST
    fixed, refused = fix_text(HEADER + text, "t.yaml")
    assert fixed == HEADER + ALIASED + HOISTED
    assert [(r.code, r.line) for r in refused] == [("L0", 13)]
    assert "anchor or alias" in refused[0].message
    anchored = ALIASED.replace("*shared-deps", "&other-deps\n    - vllm/")
    fixed, refused = fix_text(HEADER + anchored, "t.yaml")
    assert fixed == HEADER + anchored
    assert [r.code for r in refused] == ["L0"]


def test_errors_are_reported_and_never_fixed():
    text = CLEAN.replace("mi300_dpx", "mi999_dpx")
    findings = lint_text(HEADER + text, "t.yaml")
    assert [(f.code, f.line) for f in findings] == [("E1", 5)]
    assert "mi999_dpx" in findings[0].message
    assert _fix(text) == text


def test_flow_style_is_e9():
    text = CLEAN.replace(
        "  source_file_dependencies:\n    - vllm/v1/\n",
        "  source_file_dependencies: [vllm/v1/]\n",
    )
    assert _codes(text) == ["E9"]
    assert fix_text(HEADER + text)[0] == HEADER + text


def test_lint_checks_every_job_against_the_step_model_as_loading_does():
    # The AMD job that becomes the exact mirror block, too.
    text = CLEAN.replace(
        "      timeout_in_minutes: 50\n",
        "      timeout_in_minutes: 50\n      concurrency: 2\n",
    )
    findings = lint_text(HEADER + text, "t.yaml")
    assert [f.code for f in findings] == ["E2"]
    assert "job amd-v1-spec-decode" in findings[0].message
    assert "must be defined together" in findings[0].message


def test_legacy_steps_are_only_checked_for_what_cannot_load():
    legacy = '- {label: ":nvidia: (H100) T", key: t, device: h100}\n'
    assert _codes(legacy) == []
    assert _codes("- label: T\n  key: t\n  vendors:\n    nvidia:\n") == ["E2"]


# ---- files and the CLI --------------------------------------------------------------


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(HEADER + text)
    return path


def test_duplicate_keys_across_files_are_e8(tmp_path):
    _write(tmp_path, "a.yaml", CLEAN)
    _write(
        tmp_path,
        "b.yaml",
        '- label: ":amd: (MI300) Other"\n  key: amd-v1-spec-decode\n  device: mi300_1\n',
    )
    findings = lint_paths([str(tmp_path)])
    assert [f.code for f in findings] == ["E8"]
    assert "amd-v1-spec-decode" in findings[0].message


def test_duplicate_github_statuses_across_files_are_e8(tmp_path):
    _write(tmp_path, "a.yaml", CLEAN)
    _write(
        tmp_path,
        "b.yaml",
        '- label: ":nvidia: (H200 MIG 35GB) V1 Spec Decode"\n  key: other\n'
        "  device: h200_35gb\n",
    )
    findings = lint_paths([str(tmp_path)])
    assert [f.code for f in findings] == ["E8"]
    assert "'ci/step: (H200 MIG 35GB) V1 Spec Decode'" in findings[0].message
    assert "a.yaml" in findings[0].message and "b.yaml" in findings[0].message


def test_lint_command_exits_1_until_fixed(tmp_path):
    path = _write(tmp_path, "a.yaml", HOIST)
    runner = CliRunner()
    result = runner.invoke(main_module.main, ["lint", str(tmp_path)])
    assert result.exit_code == 1
    assert "L1" in result.output and "lint --fix" in result.output
    result = runner.invoke(main_module.main, ["lint", "--fix", str(path)])
    assert result.exit_code == 0, result.output
    assert path.read_text() == HEADER + HOISTED
    result = runner.invoke(main_module.main, ["lint", str(path)])
    assert result.exit_code == 0 and result.output == ""


def test_explain_prints_every_job_of_a_file(tmp_path, monkeypatch):
    path = _write(tmp_path, "spec.yaml", HOISTED)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main_module.main, ["explain", "spec.yaml"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[0] == (
        "| Key | Device | Label | Timeout | Optional | Commands | Dependencies "
        "| Depends on |"
    )
    keys = [line.split(" | ")[0][2:] for line in lines[2:]]
    assert keys == [
        "v1-spec-decode",
        "amd-v1-spec-decode",
        "amd-v1-spec-decode-mi355-dpx",
    ]
    assert (
        "| h200_35gb | :nvidia: (H200 MIG 35GB) V1 Spec Decode | 40 | no |" in lines[2]
    )
    assert "`image-build-amd`" in lines[3]
    assert "`vllm/platforms/rocm.py`" in lines[4]
    del path


def test_explain_by_key_prints_its_steps_jobs_as_json(tmp_path, monkeypatch):
    (tmp_path / "areas").mkdir()
    _write(
        tmp_path / "areas",
        "spec.yaml",
        HOISTED
        + CLEAN.replace("v1-spec-decode", "other").replace("V1 Spec Decode", "Other"),
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        main_module.main,
        ["explain", "amd-v1-spec-decode", "--job-dir", "areas", "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert [row["key"] for row in rows] == [
        "v1-spec-decode",
        "amd-v1-spec-decode",
        "amd-v1-spec-decode-mi355-dpx",
    ]
    mi355 = rows[2]
    assert mi355["device"] == "mi355_dpx"
    assert mi355["timeout_in_minutes"] == 70
    assert mi355["env"] == {"SHARED": "1"}
    assert mi355["dind"] is False
    assert mi355["working_dir"] == "/vllm-workspace/tests"
    assert sorted(mi355["source_file_dependencies"]) == sorted(
        [
            "tests/v1/spec_decode",
            "vllm/v1/",
            "vllm/platforms/rocm.py",
            "areas/spec.yaml",
        ]
    )
    result = CliRunner().invoke(
        main_module.main, ["explain", "nope", "--job-dir", "areas"]
    )
    assert result.exit_code == 1
    assert "no job file or job key 'nope'" in result.output


def test_explain_shows_a_legacy_mirror_as_the_generator_resolves_it(
    tmp_path, monkeypatch
):
    legacy = """\
- label: ":nvidia: (H100) T"
  key: t
  device: h100
  optional: true
  env:
    A: "1"
  source_file_dependencies:
    - shared/
  commands:
    - pytest
  mirror:
    amd:
      device: mi300_1
      env:
        B: "1"
      source_file_dependencies:
        - rocm/
"""
    _write(tmp_path, "t.yaml", legacy)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        main_module.main, ["explain", "t.yaml", "--format", "json"]
    )
    nvidia, amd_job = json.loads(result.output)
    assert amd_job["key"] == "amd-t"
    assert amd_job["label"] == ":amd: (MI300) T"
    assert amd_job["optional"] is True
    assert amd_job["commands"] == ["pytest"]
    assert amd_job["env"] == {"A": "1", "B": "1"}
    assert amd_job["timeout_in_minutes"] == 180
    assert amd_job["dind"] is True
    assert sorted(amd_job["source_file_dependencies"]) == ["rocm/", "shared/", "t.yaml"]
    assert nvidia["source_file_dependencies"] == ["shared/", "t.yaml"]


def test_the_generator_command_still_takes_its_options():
    result = CliRunner().invoke(main_module.main, ["--help"])
    assert result.exit_code == 0
    assert "--pipeline_config_path" in result.output


BARE_LIST = """\
- label: T
  key: t
  devices:
    h100:
      bogus: 5
  commands:
    - x
"""


@pytest.mark.parametrize(
    "text, message",
    [
        (BARE_LIST, "put the list under `steps:`"),
        ("", "a job file is a map"),
        ("group: G\nsteps:\n  label: T\n", "`steps` must be a list"),
        ("group: G\nsteps:\n  - T\n", "step 1 of `steps` is not a map"),
    ],
)
def test_a_file_that_is_not_a_job_file_fails_lint_and_explain(
    tmp_path, monkeypatch, text, message
):
    path = tmp_path / "t.yaml"
    path.write_text(text)
    findings = lint_paths([str(path)])
    assert [(f.code, f.line) for f in findings] == [("E2", 1)]
    assert message in findings[0].message
    runner = CliRunner()
    result = runner.invoke(main_module.main, ["lint", str(path)])
    assert result.exit_code == 1 and message in result.output
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(main_module.main, ["explain", "t.yaml"])
    assert result.exit_code == 1, result.output
    assert message in result.output
    assert not isinstance(result.exception, (AttributeError, TypeError))


def test_explain_reports_yaml_that_does_not_parse(tmp_path, monkeypatch):
    (tmp_path / "t.yaml").write_text("steps:\n  - label: [\n")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(main_module.main, ["explain", "t.yaml"])
    assert result.exit_code == 1
    assert "Error:" in result.output


DUPLICATE_KEY = """\
group: G
depends_on:
  - image-build
steps:
  - label: ":nvidia: (H100) Legacy"
    key: x
    device: h100
    commands:
      - pytest l
    mirror:
      amd:
        device: mi300_1
  - label: Other
    key: x2
    devices:
      mi355_1:
        key: amd-x
    commands:
      - pytest x
"""

DUPLICATE_STATUS = """\
group: G
depends_on:
  - image-build
steps:
  - label: T
    key: t
    devices:
      h100:
        timeout_in_minutes: 10
      mi355_1:
        timeout_in_minutes: 10
      mi355_4:
        key: amd-t-4
        timeout_in_minutes: 10
    commands:
      - pytest x
"""


@pytest.mark.parametrize(
    "text, message",
    [
        (DUPLICATE_KEY, "E8 t.yaml: duplicate CI step key amd-x"),
        (DUPLICATE_STATUS, "GitHub status 'ci/step: (MI355) T'"),
    ],
)
def test_explain_fails_on_a_duplicate_key_or_status_as_lint_does(
    tmp_path, monkeypatch, text, message
):
    (tmp_path / "areas").mkdir()
    _write(tmp_path / "areas", "t.yaml", text)
    monkeypatch.chdir(tmp_path / "areas")
    runner = CliRunner()
    result = runner.invoke(main_module.main, ["lint", "t.yaml"])
    assert result.exit_code == 1 and "E8" in result.output
    result = runner.invoke(main_module.main, ["explain", "t.yaml"])
    assert result.exit_code == 1
    assert "| amd-" in result.output  # the rows still print, before the error
    assert message in result.output
    monkeypatch.chdir(tmp_path)
    key = "amd-x" if text is DUPLICATE_KEY else "t"
    result = runner.invoke(main_module.main, ["explain", key, "--job-dir", "areas"])
    assert result.exit_code == 1
    assert "E8" in result.output
