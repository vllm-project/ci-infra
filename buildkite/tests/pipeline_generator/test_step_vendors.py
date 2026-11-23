"""The `vendors:` step form (step_vendors.py): how it compiles, what it
rejects, and that it renders exactly like the `mirror:` form it replaces."""

import copy

import pytest
import yaml

import amd
import buildkite_step
from pipeline_generator import select_steps_and_dependencies
from step import read_steps_from_job_dir
from step_vendors import (
    EXACT_MIRROR,
    VendorStepError,
    compile_vendor_step,
    expand_vendor_steps,
)

pytestmark = pytest.mark.usefixtures("fake_global_config")

STEP_YAML = ".buildkite/test_areas/attention.yaml"
HEADER = "group: Attention\ndepends_on:\n  - image-build\nsteps:\n"

SPEC = """\
- key: v1-attention
  label: V1 Attention
  timeout_in_minutes: 40
  source_file_dependencies:
    - vllm/v1/attention/
    - tests/v1/attention
  vendors:
    nvidia:
      devices:
        h100:
          label: ":nvidia: (H100) V1 Attention"
          commands:
            - pytest -v -s v1/attention
    amd:
      env:
        VLLM_ROCM_USE_AITER: "1"
      source_file_dependencies:
        - vllm/v1/attention/ops/rocm_aiter_mla_sparse.py
      commands:
        - pytest -v -s v1/attention --ignore=v1/attention/test_mla_backends.py
      devices:
        mi300_dpx:
          timeout_in_minutes: 50
        mi355_dpx:
          key: amd-v1-attention-mi355
"""


def _spec():
    return yaml.safe_load(SPEC)[0]


def _load(tmp_path, monkeypatch, steps_text, header=HEADER):
    path = tmp_path / STEP_YAML
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + steps_text)
    monkeypatch.chdir(tmp_path)
    return read_steps_from_job_dir(".buildkite/test_areas")


def _render(steps):
    groups = buildkite_step.convert_group_step_to_buildkite_step(
        {"Attention": sorted(steps, key=lambda step: step.label)}
    )
    return [
        (group.group, [step.dict(exclude_none=True) for step in group.steps])
        for group in groups
    ]


# ---- compiling ----------------------------------------------------------


def test_the_first_device_of_each_vendor_becomes_the_step_and_its_mirror():
    step, extra = compile_vendor_step(_spec())

    assert step["key"] == "v1-attention"
    assert step["device"] == "h100"
    assert step["label"] == ":nvidia: (H100) V1 Attention"
    assert step["timeout_in_minutes"] == 40
    assert step["commands"] == ["pytest -v -s v1/attention"]
    # The AMD vendor's dependencies and env stay on the AMD jobs.
    assert step["source_file_dependencies"] == [
        "vllm/v1/attention/",
        "tests/v1/attention",
    ]
    assert "env" not in step

    mirror = step["mirror"]["amd"]
    assert mirror[EXACT_MIRROR] is True
    assert mirror["device"] == "mi300_dpx"
    assert mirror["label"] == ":amd: (MI300 DPX) V1 Attention"
    assert mirror["timeout_in_minutes"] == 50
    assert mirror["env"] == {"VLLM_ROCM_USE_AITER": "1"}
    assert mirror["source_file_dependencies"] == [
        "vllm/v1/attention/",
        "tests/v1/attention",
        "vllm/v1/attention/ops/rocm_aiter_mla_sparse.py",
    ]
    assert mirror["commands"] == [
        "pytest -v -s v1/attention --ignore=v1/attention/test_mla_backends.py"
    ]
    # What a mirror gets when it leaves them out, written down.
    assert mirror["depends_on"] == ["image-build-amd"]
    assert mirror["dind"] is True

    # A second device is a step of its own, under its own key.
    assert extra["key"] == "amd-v1-attention-mi355"
    assert extra["device"] == "mi355_dpx"
    assert extra["label"] == ":amd: (MI355 DPX) V1 Attention"
    assert extra["timeout_in_minutes"] == 40
    assert extra["env"] == mirror["env"]
    assert extra["commands"] == mirror["commands"]
    assert "mirror" not in extra


def test_lists_concatenate_without_duplicate_dependencies_and_env_merges():
    raw = {
        "key": "t",
        "label": "T",
        "commands": ["setup"],
        "source_file_dependencies": ["a/", "b/"],
        "env": {"A": "test", "B": "test"},
        "vendors": {
            "amd": {
                "commands": ["vendor"],
                "source_file_dependencies": ["b/", "c/"],
                "env": {"B": "vendor", "C": "vendor"},
                "devices": {
                    "mi355_1": {
                        "commands": ["device"],
                        "source_file_dependencies": ["!c/x", "a/"],
                        "env": {"C": "device"},
                    }
                },
            }
        },
    }
    (step,) = compile_vendor_step(raw)

    assert step["commands"] == ["setup", "vendor", "device"]
    assert step["source_file_dependencies"] == ["a/", "b/", "c/", "!c/x"]
    assert step["env"] == {"A": "test", "B": "vendor", "C": "device"}


def test_an_amd_only_test_is_a_native_amd_step_keyed_amd_key():
    (step,) = compile_vendor_step(
        {
            "key": "kernels-mi355",
            "label": "Kernels",
            "dind": False,
            "commands": ["pytest kernels"],
            "vendors": {"amd": {"devices": {"mi355_1": None}}},
        }
    )
    assert step == {
        "key": "amd-kernels-mi355",
        "label": ":amd: (MI355) Kernels",
        "device": "mi355_1",
        "dind": False,
        "depends_on": ["image-build-amd"],
        "commands": ["pytest kernels"],
    }


def test_explicit_keys_win_and_a_mirror_needs_the_amd_prefixed_key():
    raw = _spec()
    raw["vendors"]["amd"]["devices"]["mi300_dpx"]["key"] = "my-amd-job"
    compiled = compile_vendor_step(raw)

    assert "mirror" not in compiled[0]
    assert [step["key"] for step in compiled] == [
        "v1-attention",
        "my-amd-job",
        "amd-v1-attention-mi355",
    ]


def test_null_values_are_unset_and_fields_the_generator_ignores_stay_on_steps():
    (step,) = compile_vendor_step(
        {
            "key": "t",
            "label": "T",
            "depends_on": None,
            "num_gpus": 2,
            "autorun_on_main": True,
            "vendors": {"nvidia": {"devices": {"h100": None}}},
        }
    )
    assert step == {
        "key": "t",
        "label": "T",
        "device": "h100",
        "num_gpus": 2,
        "autorun_on_main": True,
    }


def test_nvidia_without_devices_is_one_job_on_the_default_queue():
    (step,) = compile_vendor_step(
        {
            "key": "t",
            "label": "T",
            "vendors": {"nvidia": None, "amd": {"devices": {"mi300_dpx": None}}},
        }
    )
    assert "device" not in step
    assert step["mirror"]["amd"]["device"] == "mi300_dpx"


def test_a_cpu_job_can_have_an_amd_mirror():
    (step,) = compile_vendor_step(
        {
            "key": "t",
            "label": "T",
            "vendors": {
                "cpu": {"devices": {"cpu-small": None}},
                "amd": {"no_gpu": True, "devices": {"mi250_1": None}},
            },
        }
    )
    assert step["device"] == "cpu-small"
    assert step["mirror"]["amd"]["no_gpu"] is True
    assert "no_gpu" not in step


def test_steps_without_vendors_pass_through_untouched():
    legacy = [
        {
            "label": "A",
            "key": "a",
            "device": "h100",
            "mirror": {"amd": {"device": "mi300_1"}},
        },
        {"label": "B"},
    ]
    before = copy.deepcopy(legacy)
    assert expand_vendor_steps(legacy) == before
    raw = _spec()
    before = copy.deepcopy(raw)
    expand_vendor_steps([raw])
    assert raw == before


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda r: r.pop("key"), "needs a `key`"),
        (lambda r: r.__setitem__("device", "h100"), "replaces `device`"),
        (lambda r: r.__setitem__("mirror", {"amd": {}}), "replaces `mirror`"),
        (lambda r: r.__setitem__("vendors", {}), "non-empty map"),
        (lambda r: r["vendors"].__setitem__("tpu", None), "unknown vendor 'tpu'"),
        (
            lambda r: r["vendors"]["nvidia"]["devices"].__setitem__("mi355_1", None),
            "device mi355_1: not a nvidia device",
        ),
        (
            lambda r: r["vendors"]["amd"]["devices"].__setitem__("h100", None),
            "device h100: not a amd device",
        ),
        (
            lambda r: r["vendors"]["amd"]["devices"]["mi355_dpx"].pop("key"),
            "needs a `key` of its own",
        ),
        (
            lambda r: r["vendors"]["amd"]["devices"]["mi355_dpx"].__setitem__(
                "key", "v1-attention"
            ),
            "two jobs are keyed v1-attention",
        ),
        (lambda r: r["vendors"]["amd"].pop("devices"), "needs `devices`"),
        (
            lambda r: r["vendors"]["amd"].__setitem__("devices", {}),
            "`devices` must be a non-empty map",
        ),
        (
            lambda r: r["vendors"]["amd"].__setitem__("mirror", {}),
            "unknown field(s) `mirror`",
        ),
        (
            lambda r: r["vendors"]["amd"]["devices"]["mi300_dpx"].__setitem__(
                "gpu", "x"
            ),
            "unknown field(s) `gpu`",
        ),
        (
            lambda r: r["vendors"]["amd"].__setitem__("commands", "pytest"),
            "must be a list",
        ),
        (lambda r: r["vendors"]["amd"].__setitem__("env", ["A=1"]), "must be a map"),
        (lambda r: r.pop("label"), "no `label` for amd-v1-attention"),
    ],
)
def test_invalid_vendor_steps_are_rejected(change, message):
    raw = _spec()
    change(raw)
    with pytest.raises(
        VendorStepError, match=message.replace("(", r"\(").replace(")", r"\)")
    ):
        compile_vendor_step(raw)


def test_an_invalid_vendor_step_fails_loading(tmp_path, monkeypatch):
    with pytest.raises(VendorStepError):
        _load(tmp_path, monkeypatch, SPEC.replace("mi300_dpx", "h200"))


# ---- rendering: the same jobs as the mirror form ----------------------------

LEGACY = """\
- label: ":nvidia: (H100) V1 Attention"
  key: v1-attention
  device: h100
  timeout_in_minutes: 40
  automatic_shard: true
  optional: true
  env:
    SHARED: "1"
  source_file_dependencies:
    - vllm/v1/attention/
    - tests/v1/attention
  commands:
    - pytest -v -s v1/attention
  mirror:
    amd:
      label: ":amd: (MI300) V1 Attention"
      device: mi300_2
      dind: false
      automatic_shard: true
      optional: false
      timeout_in_minutes: 50
      num_devices: 2
      depends_on:
        - image-build-amd
      env:
        VLLM_ROCM_USE_AITER: "1"
      source_file_dependencies:
        - vllm/v1/attention/ops/rocm_aiter_mla_sparse.py
      commands:
        - pytest -v -s v1/attention --ignore=v1/attention/test_mla_backends.py
"""
VENDORS = """\
- key: v1-attention
  label: V1 Attention
  automatic_shard: true
  env:
    SHARED: "1"
  source_file_dependencies:
    - vllm/v1/attention/
    - tests/v1/attention
  vendors:
    nvidia:
      optional: true
      commands:
        - pytest -v -s v1/attention
      devices:
        h100:
          label: ":nvidia: (H100) V1 Attention"
          timeout_in_minutes: 40
    amd:
      dind: false
      env:
        VLLM_ROCM_USE_AITER: "1"
      source_file_dependencies:
        - vllm/v1/attention/ops/rocm_aiter_mla_sparse.py
      commands:
        - pytest -v -s v1/attention --ignore=v1/attention/test_mla_backends.py
      devices:
        mi300_2:
          timeout_in_minutes: 50
          num_devices: 2
"""
NATIVE_LEGACY = """\
- label: ":amd: (MI355) Kernels"
  key: amd-kernels-mi355
  device: mi355_1
  dind: false
  depends_on:
    - image-build-amd
  source_file_dependencies:
    - csrc/
  commands:
    - pytest kernels
"""
NATIVE_VENDORS = """\
- key: kernels-mi355
  label: Kernels
  source_file_dependencies:
    - csrc/
  commands:
    - pytest kernels
  vendors:
    amd:
      dind: false
      devices:
        mi355_1:
"""


@pytest.mark.parametrize(
    "diff",
    [
        [],
        ["vllm/v1/attention/backends/x.py"],
        ["vllm/v1/attention/ops/rocm_aiter_mla_sparse.py"],
        ["csrc/foo.cu"],
        [STEP_YAML],
        ["docker/Dockerfile.rocm_base"],
    ],
)
@pytest.mark.parametrize("header", [HEADER, "group: Attention\nsteps:\n"])
def test_a_vendors_step_renders_like_the_mirror_it_replaces(
    tmp_path, monkeypatch, fake_global_config, diff, header
):
    fake_global_config["list_file_diff"] = diff
    # The edit to the yaml redefines no step, in either form.
    monkeypatch.setattr(amd, "changed_step_keys_in_file", lambda *args: frozenset())
    legacy = _load(tmp_path / "legacy", monkeypatch, LEGACY + NATIVE_LEGACY, header)
    vendors = _load(tmp_path / "vendors", monkeypatch, VENDORS + NATIVE_VENDORS, header)

    assert _render(vendors) == _render(legacy)
    for old, new in zip(
        sorted(legacy, key=lambda s: s.key), sorted(vendors, key=lambda s: s.key)
    ):
        if old.mirror:
            old_amd = buildkite_step.amd_mirror_step(old)
            new_amd = buildkite_step.amd_mirror_step(new)
            assert set(new_amd.source_file_dependencies) == set(
                old_amd.source_file_dependencies
            )
            assert new_amd.source_file_declared == old_amd.source_file_declared
            for name in ("key", "device", "optional", "dind"):
                assert getattr(new_amd, name) == getattr(old_amd, name)
            assert buildkite_step._step_should_run(
                new_amd, diff
            ) == buildkite_step._step_should_run(old_amd, diff)


def test_one_vendors_fields_never_reach_another_vendors_job(
    tmp_path, monkeypatch, fake_global_config
):
    text = """\
- key: t
  label: T
  source_file_dependencies:
    - shared/
  vendors:
    nvidia:
      num_devices: 2
      optional: true
      timeout_in_minutes: 30
      env:
        NVIDIA_ONLY: "1"
      source_file_dependencies:
        - nvidia-only/
      commands:
        - pytest nvidia
      devices:
        h100:
    amd:
      commands:
        - pytest amd
      devices:
        mi355_1:
"""
    (step,) = _load(tmp_path, monkeypatch, text)
    mirror = buildkite_step.amd_mirror_step(step)
    assert mirror.source_file_dependencies == ["shared/", STEP_YAML]
    assert mirror.optional is False

    fake_global_config["list_file_diff"] = ["nvidia-only/x.py"]
    groups = {
        group.group: group.steps
        for group in buildkite_step.convert_group_step_to_buildkite_step(
            {"Attention": [step]}
        )
    }
    (amd_job,) = groups[buildkite_step.AMD_GROUP]
    assert amd_job.deferred is True
    assert "NVIDIA_ONLY" not in amd_job.env
    assert "pytest amd" in amd_job.env["VLLM_TEST_COMMANDS"]
    assert "pytest nvidia" not in amd_job.env["VLLM_TEST_COMMANDS"]
    # Its own timeout is unset, so AMD's standard one, not the NVIDIA job's 30.
    assert amd_job.timeout_in_minutes == amd.get_amd_timeout_in_minutes(None)
    (nvidia_job,) = groups["Attention"]
    assert nvidia_job.env["NVIDIA_ONLY"] == "1"


def test_an_exact_mirror_counts_its_yaml_only_when_it_lists_it(
    tmp_path, monkeypatch, fake_global_config
):
    fake_global_config["list_file_diff"] = [STEP_YAML]
    monkeypatch.setattr(amd, "changed_step_keys_in_file", lambda *args: frozenset())
    (step,) = _load(tmp_path, monkeypatch, VENDORS)
    mirror = buildkite_step.amd_mirror_step(step)
    assert STEP_YAML in mirror.source_file_dependencies
    assert mirror.source_file_declared is False
    assert not buildkite_step._step_should_run(mirror, [STEP_YAML])

    declared = VENDORS.replace(
        "        - vllm/v1/attention/ops/rocm_aiter_mla_sparse.py\n",
        f"        - vllm/v1/attention/ops/rocm_aiter_mla_sparse.py\n        - {STEP_YAML}\n",
    )
    (step,) = _load(tmp_path / "declared", monkeypatch, declared)
    mirror = buildkite_step.amd_mirror_step(step)
    assert mirror.source_file_declared is True
    assert buildkite_step._step_should_run(mirror, [STEP_YAML])


def test_retries_pick_every_job_of_a_vendors_step(tmp_path, monkeypatch):
    steps = _load(tmp_path, monkeypatch, SPEC)
    keys = frozenset({"amd-v1-attention", "amd-v1-attention-mi355"})
    selected, selected_keys = select_steps_and_dependencies(
        steps
        + _load(
            tmp_path / "images",
            monkeypatch,
            "- {label: AMD image, key: image-build-amd, device: amd_cpu}\n",
            "steps:\n",
        ),
        keys,
    )
    assert {step.key for step in selected} == {
        "v1-attention",
        "amd-v1-attention-mi355",
        "image-build-amd",
    }
    assert selected_keys == keys | {"image-build-amd"}


# ---- what an edit to the yaml changes -----------------------------------------


def _edit(old, new, text=SPEC):
    assert old in text
    return text.replace(old, new, 1)


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        pytest.param(SPEC, set(), id="unchanged"),
        pytest.param(
            _edit("    - tests/v1/attention\n", "    - tests/v1/attention/\n"),
            {"v1-attention", "amd-v1-attention", "amd-v1-attention-mi355"},
            id="shared",
        ),
        pytest.param(
            _edit("timeout_in_minutes: 40", "timeout_in_minutes: 45"),
            # mi300_dpx sets its own.
            {"v1-attention", "amd-v1-attention-mi355"},
            id="shared-but-overridden",
        ),
        pytest.param(
            _edit("pytest -v -s v1/attention\n", "pytest -v -s v1/attention -x\n"),
            {"v1-attention"},
            id="nvidia-only",
        ),
        pytest.param(
            _edit("timeout_in_minutes: 50", "timeout_in_minutes: 55"),
            {"amd-v1-attention"},
            id="one-amd-device",
        ),
        pytest.param(
            _edit('VLLM_ROCM_USE_AITER: "1"', 'VLLM_ROCM_USE_AITER: "0"'),
            {"amd-v1-attention", "amd-v1-attention-mi355"},
            id="amd-vendor",
        ),
        pytest.param(
            _edit("        mi300_dpx:\n          timeout_in_minutes: 50\n", ""),
            # The removed job has nothing left to run; the others are as they were.
            set(),
            id="amd-device-removed",
        ),
    ],
)
def test_changed_step_keys_follow_the_jobs_an_edit_changes(head, expected):
    keys = amd.changed_step_keys(HEADER + SPEC, HEADER + head)
    assert keys == frozenset(expected)


def test_changed_step_keys_give_up_on_a_base_that_does_not_compile():
    broken = _edit("mi300_dpx", "h200")
    assert amd.changed_step_keys(HEADER + broken, HEADER + SPEC) is None
