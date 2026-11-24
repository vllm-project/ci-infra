# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""`devices:` steps, which the generator compiles with step_vendors.py.

We compile them with the generator's own code, so what is checked here is
that the rest of our loader reads the result as the generator does: an AMD job
from a `devices:` step (`_exact`) is the whole job, with nothing of the NVIDIA
step's, and a pure syntax change from `mirror:` leaves every step as it was.
No vLLM checkout needed: the yaml is written here.
"""

from pathlib import Path

import pytest
import yaml
from ci_selector.codemap.classify import changed_step_entries
from ci_selector.codemap.pipeline.buildkite import (
    _expand_mirror,
    load_pipeline_configs,
    load_steps,
)
from ci_selector.codemap.pipeline.step import DEFAULT_WORKING_DIR, LoadReport, Step

import buildkite_step
import selector_gate
import step as step_mod
from amd import is_amd_device
from step_vendors import expand_document

CONFIG = """\
name: vllm_ci
job_dirs:
  - .buildkite/test_areas
"""
HEADER = "group: Attention\ndepends_on:\n  - image-build\nsteps:\n"
NO_GROUP_HEADER = "group: Plain\nsteps:\n"

LEGACY = """\
- label: ":nvidia: (H100) [Nightly] V1 Attention"
  key: v1-attention
  device: h100
  timeout_in_minutes: 40
  optional: true
  num_devices: 2
  env:
    SHARED: "1"
  source_file_dependencies:
    - vllm/v1/attention/
  commands:
    - pytest -v -s v1/attention
  mirror:
    amd:
      label: ":amd: (MI300) V1 Attention"
      device: mi300_2
      dind: false
      optional: false
      timeout_in_minutes: 50
      num_devices: 2
      depends_on:
        - image-build-amd
      working_dir: /vllm-workspace/tests
      env:
        AITER: "1"
      source_file_dependencies:
        - vllm/platforms/rocm.py
      commands:
        - pytest -v -s v1/attention -k amd
- label: ":amd: (MI355) Kernels"
  key: amd-kernels-mi355
  device: mi355_1
  num_devices: 1
  dind: false
  depends_on:
    - image-build-amd
  source_file_dependencies:
    - csrc/
  commands:
    - pytest kernels
"""
DEVICES = """\
- label: V1 Attention
  key: v1-attention
  devices:
    h100:
      optional: true
      num_devices: 2
      timeout_in_minutes: 40
      commands:
        - pytest -v -s v1/attention
    mi300_2:
      timeout_in_minutes: 50
      working_dir: /vllm-workspace/tests
      env:
        AITER: "1"
      source_file_dependencies:
        - vllm/platforms/rocm.py
      commands:
        - pytest -v -s v1/attention -k amd
  env:
    SHARED: "1"
  source_file_dependencies:
    - vllm/v1/attention/
- label: Kernels
  key: kernels-mi355
  devices:
    mi355_1:
  source_file_dependencies:
    - csrc/
  commands:
    - pytest kernels
"""
TWO_DEVICES = """\
- label: LoRA
  key: lora
  devices:
    h100:
      env:
        NVIDIA_ONLY: "1"
      source_file_dependencies:
        - vllm/nvidia-only/
      variants:
        eager:
          env:
            EAGER: "1"
    b200-k8s:
      same_as: h100
    mi355_1:
      optional: true
    mi300_1:
      same_as: mi355_1
      timeout_in_minutes: 20
  source_file_dependencies:
    - vllm/lora/
  commands:
    - pytest lora
"""


def _repo(root: Path, files: dict) -> Path:
    (root / ".buildkite/test_areas").mkdir(parents=True)
    (root / ".buildkite/ci_config.yaml").write_text(CONFIG)
    for name, text in files.items():
        (root / ".buildkite/test_areas" / name).write_text(text)
    return root


def _ours(repo: Path) -> list[Step]:
    (config,) = load_pipeline_configs(repo)
    report = LoadReport()
    steps = load_steps(repo, config, report)
    assert report.unknown_fields == {}
    return steps


def _theirs(repo: Path, monkeypatch) -> list:
    monkeypatch.setattr(
        step_mod, "get_global_config", lambda: {"github_repo_name": "vllm-project/vllm"}
    )
    monkeypatch.chdir(repo)
    steps = step_mod.read_steps_from_job_dir(".buildkite/test_areas")
    for step in steps:
        step.key = step.key or buildkite_step._generate_step_key(step.label)
    return steps


@pytest.mark.parametrize("header", [HEADER, NO_GROUP_HEADER])
def test_a_pure_syntax_change_from_mirror_loads_the_same_steps(tmp_path, header):
    legacy = _ours(_repo(tmp_path / "legacy", {"a.yaml": header + LEGACY}))
    devices = _ours(_repo(tmp_path / "devices", {"a.yaml": header + DEVICES}))

    def view(step):
        # Dependencies are matched as a set; the order is the merge's.
        deps = sorted(step.source_file_dependencies or [])
        return {**vars(step), "source_file_dependencies": deps}

    assert [view(s) for s in devices] == [view(s) for s in legacy]
    assert [s.identity for s in devices] == [s.identity for s in legacy]
    assert [s.step_id for s in devices] == [s.step_id for s in legacy]
    assert [s.buildkite_key for s in devices] == [s.buildkite_key for s in legacy]


def test_an_amd_job_from_devices_takes_nothing_from_the_nvidia_job(tmp_path):
    steps = {
        s.buildkite_key: s
        for s in _ours(_repo(tmp_path, {"lora.yaml": HEADER + TWO_DEVICES}))
    }
    assert set(steps) == {
        "lora",
        "lora-eager",
        "lora-b200",
        "amd-lora",
        "amd-lora-mi300",
    }
    mirror = steps["amd-lora"]
    assert mirror.mirror_hw == "amd" and mirror.device == "mi355_1"
    assert mirror.source_file_dependencies == [
        "vllm/lora/",
        ".buildkite/test_areas/lora.yaml",
    ]
    assert mirror.env == {}
    assert mirror.optional is True
    assert mirror.mirror_label == ":amd: (MI355) [Nightly] LoRA"
    native = steps["amd-lora-mi300"]
    assert native.mirror_hw is None and native.device == "mi300_1"
    assert native.depends_on == ["image-build-amd"]
    assert native.optional is True and native.timeout_in_minutes == 20
    assert steps["lora-b200"].env == {"NVIDIA_ONLY": "1"}
    assert "vllm/nvidia-only/" in steps["lora-b200"].source_file_dependencies
    assert steps["lora-eager"].env == {"NVIDIA_ONLY": "1", "EAGER": "1"}
    assert steps["lora-eager"].device == "h100"
    assert steps["lora-eager"].label == ":nvidia: (H100) LoRA [eager]"


@pytest.mark.parametrize(
    "files", [{"a.yaml": HEADER + DEVICES}, {"l.yaml": HEADER + TWO_DEVICES}]
)
def test_the_gate_and_the_generator_read_devices_steps_as_we_do(
    tmp_path, monkeypatch, files
):
    """test_gate_contract's check, and the AMD jobs' run inputs, over a
    `devices:` tree (the pinned vLLM has none yet)."""
    repo = _repo(tmp_path, files)
    ours = _ours(repo)
    theirs = _theirs(repo, monkeypatch)

    assert {s.buildkite_key for s in ours} == selector_gate.known_keys(theirs)
    assert {s.buildkite_key for s in ours if s.optional} == selector_gate.optional_keys(
        theirs
    )
    assert {
        s.buildkite_key for s in ours if is_amd_device(s.device)
    } == selector_gate.amd_keys(theirs)

    by_key = {s.key: s for s in theirs}
    for step in ours:
        if step.mirror_hw:
            parent = by_key[step.buildkite_key.removeprefix("amd-")]
            amd = parent.mirror["amd"]
            generator = buildkite_step.amd_mirror_step(parent)
            job = buildkite_step._mirror_parent(parent, amd)
            commands = amd.get("commands") or job.commands
            env = {**(job.env or {}), **(amd.get("env") or {})}
            working_dir = amd.get("working_dir", job.working_dir)
        else:
            generator = by_key[step.buildkite_key]
            commands, env = generator.commands, generator.env or {}
            working_dir = generator.working_dir
        assert sorted(step.source_file_dependencies or []) == sorted(
            generator.source_file_dependencies or []
        )
        assert step.device == generator.device
        assert step.optional == generator.optional
        assert step.commands == list(commands or [])
        assert step.env == env
        assert step.working_dir == (working_dir or "")


def test_an_exact_block_falls_back_to_nothing():
    parent = Step(
        pipeline="vllm_ci",
        source_file=".buildkite/test_areas/x.yaml",
        label="X",
        key="x",
        group=None,
        commands=["pytest nvidia"],
        source_file_dependencies=["nvidia/", ".buildkite/test_areas/x.yaml"],
        num_devices=4,
        timeout_in_minutes=30,
        optional=True,
        no_plugin=True,
        parallelism=2,
        env={"NVIDIA": "1"},
    )
    job = {"_exact": True, "device": "mi355_1", "label": ":amd: (MI355) X"}
    for group_active in (False, True):
        report = LoadReport()
        variant = _expand_mirror(parent, "amd", job, report, group_active=group_active)
        assert report.unknown_fields == {}
        assert variant.commands == []
        assert variant.env == {}
        assert variant.num_devices is None
        assert variant.timeout_in_minutes is None
        assert variant.optional is False
        assert variant.no_plugin is False
        assert variant.parallelism is None
        assert variant.depends_on == ["image-build-amd"]
        assert variant.source_file_dependencies == (
            [parent.source_file] if group_active else None
        )
        assert variant.working_dir == (DEFAULT_WORKING_DIR if group_active else "")
        assert variant.step_id == "vllm_ci:x-amd:amd"


def _edit(old, new, text=TWO_DEVICES):
    assert old in text
    return text.replace(old, new, 1)


@pytest.mark.parametrize(
    ("head", "changed", "mirror_only"),
    [
        (TWO_DEVICES, set(), set()),
        (
            _edit("    - pytest lora\n", "    - pytest lora -x\n"),
            {"lora", "lora-eager", "lora-b200", "amd-lora-mi300"},
            set(),
        ),
        (
            _edit("      optional: true\n", "      optional: false\n"),
            {"amd-lora-mi300"},
            {"lora"},
        ),
        (
            _edit('NVIDIA_ONLY: "1"', 'NVIDIA_ONLY: "2"'),
            {"lora", "lora-eager", "lora-b200"},
            set(),
        ),
        (_edit('EAGER: "1"', 'EAGER: "2"'), {"lora-eager"}, set()),
        (
            _edit("timeout_in_minutes: 20", "timeout_in_minutes: 25"),
            {"amd-lora-mi300"},
            set(),
        ),
    ],
)
def test_edits_to_a_devices_step_land_on_the_jobs_they_change(
    head, changed, mirror_only
):
    before = expand_document(yaml.safe_load(HEADER + TWO_DEVICES))
    after = expand_document(yaml.safe_load(HEADER + head))
    got_changed, added, got_mirror_only = changed_step_entries(before, after)
    assert added == {}
    assert set(got_changed) == changed
    assert set(got_mirror_only) == mirror_only
