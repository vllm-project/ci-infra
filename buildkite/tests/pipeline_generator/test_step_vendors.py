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
    # The AMD vendor's defaults, written down.
    assert mirror["depends_on"] == ["image-build-amd"]
    assert mirror["dind"] is False

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


def test_a_device_key_beats_its_vendors_key():
    raw = _spec()
    raw["vendors"]["amd"]["key"] = "from-vendor"
    raw["vendors"]["amd"]["devices"]["mi300_dpx"]["key"] = "from-device"
    keys = [step["key"] for step in compile_vendor_step(raw)]
    assert keys == ["v1-attention", "from-device", "amd-v1-attention-mi355"]

    del raw["vendors"]["amd"]["devices"]["mi300_dpx"]["key"]
    keys = [step["key"] for step in compile_vendor_step(raw)]
    # A vendor's key is its first device's only.
    assert keys == ["v1-attention", "from-vendor", "amd-v1-attention-mi355"]


def test_an_explicit_key_on_the_nvidia_job_unpairs_the_amd_mirror():
    raw = _spec()
    raw["vendors"]["nvidia"]["devices"]["h100"]["key"] = "v1-attention-h100"
    compiled = compile_vendor_step(raw)
    assert [step["key"] for step in compiled] == [
        "v1-attention-h100",
        "amd-v1-attention",
        "amd-v1-attention-mi355",
    ]
    # A mirror is always keyed amd-<its step's key>, so amd-v1-attention is
    # a step of its own, with the same fields.
    assert "mirror" not in compiled[0]
    assert compiled[1]["device"] == "mi300_dpx"
    assert compiled[1]["depends_on"] == ["image-build-amd"]


def test_vendor_defaults_beat_the_test_level():
    raw = {
        "key": "t",
        "label": "T",
        "depends_on": ["some-nvidia-build"],
        "dind": True,
        "vendors": {
            "nvidia": {"devices": {"h100": None}},
            "amd": {"devices": {"mi300_1": None, "mi355_1": {"key": "t-mi355"}}},
        },
    }
    step, second = compile_vendor_step(raw)
    assert step["depends_on"] == ["some-nvidia-build"]
    for amd_job in (step["mirror"]["amd"], second):
        assert amd_job["depends_on"] == ["image-build-amd"]
        assert amd_job["dind"] is False

    raw["vendors"]["amd"]["dind"] = True
    raw["vendors"]["amd"]["devices"]["mi355_1"]["depends_on"] = ["other"]
    step, second = compile_vendor_step(raw)
    assert step["mirror"]["amd"]["dind"] is True
    assert step["mirror"]["amd"]["depends_on"] == ["image-build-amd"]
    assert second["dind"] is True
    assert second["depends_on"] == ["other"]


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
        (lambda r: r.__setitem__("timeout", 5), "unknown field(s) `timeout`"),
        (lambda r: r.__setitem__("gpu", "h100"), "replaces `gpu`"),
        (
            lambda r: r["vendors"]["amd"].__setitem__("retry", {"automatic": True}),
            "vendor amd: unknown field(s) `retry`",
        ),
        (
            lambda r: r["vendors"]["amd"]["devices"]["mi300_dpx"].__setitem__(
                "mount_buildkite_agent", True
            ),
            "device mi300_dpx: unknown field(s) `mount_buildkite_agent`",
        ),
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


HAND_WRITTEN_EXACT = """
- key: attention
  label: Attention
  device: h100
  commands:
    - pytest -v -s v1/attention
  mirror:
    amd:
      device: mi300_dpx
      _exact: true
"""


def test_a_hand_written_exact_mirror_fails_loading(tmp_path, monkeypatch):
    # Only a compiled `vendors:` step may cut the AMD job loose from its step.
    with pytest.raises(VendorStepError, match="write the step with `vendors:`"):
        _load(tmp_path, monkeypatch, HAND_WRITTEN_EXACT)
    with pytest.raises(VendorStepError):
        expand_vendor_steps(yaml.safe_load(HAND_WRITTEN_EXACT))


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


# ---- the compiler and the generator agree on the fields ---------------------


def test_job_fields_are_the_step_models_less_what_the_loader_sets():
    from step import Step
    from step_vendors import JOB_FIELDS

    loader_only = {
        "group",
        "device",
        "mirror",
        "source_file",
        "source_file_declared",
        "mirror_source_file_declared",
    }
    assert set(JOB_FIELDS) == set(Step.model_fields) - loader_only


def test_an_exact_mirror_takes_every_field_it_carries_from_its_block():
    from step import Step
    from step_vendors import MIRROR_FIELDS

    nvidia = {
        "working_dir": "/nvidia",
        "depends_on": ["nvidia-build"],
        "commands": ["nvidia"],
        "agent_tags": {"queue": "nvidia"},
        "num_devices": 8,
        "num_nodes": 2,
        "source_file_dependencies": ["nvidia/"],
        "soft_fail": True,
        "parallelism": 4,
        "concurrency": 3,
        "concurrency_group": "nvidia",
        "timeout_in_minutes": 99,
        "env": {"NVIDIA": "1"},
        "optional": True,
        "no_plugin": True,
        "no_gpu": True,
        "dind": True,
        "automatic_shard": True,
        "retry": {"automatic": True},
        "mount_buildkite_agent": True,
    }
    block = {
        "working_dir": "/amd",
        "depends_on": ["image-build-amd"],
        "commands": ["amd"],
        "agent_tags": {"queue": "amd"},
        "num_devices": 1,
        "num_nodes": 1,
        "source_file_dependencies": ["amd/"],
        "soft_fail": False,
        "parallelism": 2,
        "concurrency": 1,
        "concurrency_group": "amd",
        "timeout_in_minutes": 11,
        "env": {"AMD": "1"},
        "optional": False,
        "no_plugin": False,
        "no_gpu": False,
        "dind": False,
        "automatic_shard": False,
        "label": "A",
        "device": "mi300_1",
        EXACT_MIRROR: True,
    }
    # Every field an exact mirror carries is set above, to differ from the
    # step's, so a field _mirror_parent forgets shows up as the NVIDIA value.
    assert MIRROR_FIELDS - {"label"} <= set(nvidia)
    assert MIRROR_FIELDS <= set(block)
    step = Step(label="N", key="n", device="h100", **nvidia)
    parent = buildkite_step._mirror_parent(step, block)
    for name in MIRROR_FIELDS - {"label"}:
        assert getattr(parent, name) == block[name], name
    # Never read for an AMD job, so never the NVIDIA job's.
    assert parent.retry is None
    assert parent.mount_buildkite_agent is False


# ---- a second device of one vendor --------------------------------------------

TWO_AMD_LEGACY = """- label: ":nvidia: (H100) T"
  key: t
  device: h100
  source_file_dependencies:
    - shared/
  commands:
    - pytest t
  mirror:
    amd:
      label: ":amd: (MI300) T"
      device: mi300_1
      dind: false
      depends_on:
        - image-build-amd
      env:
        AITER: "1"
      source_file_dependencies:
        - rocm/
      commands:
        - pytest t --amd
- label: ":amd: (MI355) T"
  key: amd-t-mi355
  device: mi355_1
  dind: false
  timeout_in_minutes: 70
  depends_on:
    - image-build-amd
  env:
    AITER: "1"
  source_file_dependencies:
    - shared/
    - rocm/
  commands:
    - pytest t --amd
"""
TWO_AMD_VENDORS = """- key: t
  label: T
  source_file_dependencies:
    - shared/
  vendors:
    nvidia:
      commands:
        - pytest t
      devices:
        h100:
          label: ":nvidia: (H100) T"
    amd:
      env:
        AITER: "1"
      source_file_dependencies:
        - rocm/
      commands:
        - pytest t --amd
      devices:
        mi300_1:
        mi355_1:
          key: amd-t-mi355
          timeout_in_minutes: 70
"""


@pytest.mark.parametrize(
    "diff",
    [[], ["shared/x.py"], ["rocm/x.py"], ["other/x.py"], [STEP_YAML]],
)
def test_a_second_amd_device_renders_like_a_native_amd_step(
    tmp_path, monkeypatch, fake_global_config, diff
):
    fake_global_config["list_file_diff"] = diff
    monkeypatch.setattr(amd, "changed_step_keys_in_file", lambda *args: frozenset())
    legacy = _load(tmp_path / "legacy", monkeypatch, TWO_AMD_LEGACY)
    vendors = _load(tmp_path / "vendors", monkeypatch, TWO_AMD_VENDORS)
    assert [step.key for step in vendors] == ["t", "amd-t-mi355"]
    assert _render(vendors) == _render(legacy)


# ---- keys across steps and files ----------------------------------------------


def test_a_vendors_job_key_may_not_repeat_another_jobs(tmp_path, monkeypatch):
    from step import check_vendor_step_keys

    clash = TWO_AMD_VENDORS.replace("key: amd-t-mi355", "key: amd-other")
    other = """- label: ":nvidia: (H100) Other"
  key: other
  device: h100
  mirror:
    amd:
      device: mi300_1
"""
    steps = _load(tmp_path, monkeypatch, clash + other)
    with pytest.raises(VendorStepError, match="Duplicate CI step key amd-other"):
        check_vendor_step_keys(steps)

    # Only legacy steps sharing a key: left alone, as before.
    check_vendor_step_keys(_load(tmp_path / "legacy", monkeypatch, other + other))
    check_vendor_step_keys(_load(tmp_path / "ok", monkeypatch, TWO_AMD_VENDORS))
