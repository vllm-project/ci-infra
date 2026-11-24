"""The `devices:` step form (step_vendors.py): how a step resolves to jobs (merge
rules, same_as, variants, keys, labels, registry defaults), what it rejects
(E1-E9), and that it renders exactly like the `device` + `mirror` form it
replaces."""

import copy

import pytest
import yaml

import amd
import buildkite_step
from constants import NON_AMD_DEVICES, Vendor
from pipeline_generator import select_steps_and_dependencies
from step import Step, check_device_step_keys, read_steps_from_job_dir
from step_vendors import (
    DEVICES,
    EXACT_MIRROR,
    DeviceStepError,
    compile_device_step,
    expand_document,
    expand_steps,
    resolve_step,
)

pytestmark = pytest.mark.usefixtures("fake_global_config")

STEP_YAML = ".buildkite/test_areas/attention.yaml"
HEADER = "group: Attention\ndepends_on:\n  - image-build\nsteps:\n"

# The design's case A (two vendors) and D (variants on the NVIDIA device).
SPEC = """\
- label: V1 Attention
  key: v1-attention
  devices:
    h100:
      timeout_in_minutes: 85
      source_file_dependencies:
        - vllm/v1/hisparse/
      commands:
        - pytest -v -s v1/attention
      variants:
        eager:
          env:
            VLLM_ENFORCE_EAGER: "1"
        extended:
          timeout_in_minutes: 90
          commands:
            - pytest -v -s --runslow v1/attention
    mi300_1:
      timeout_in_minutes: 125
      env:
        VLLM_ROCM_USE_AITER: "1"
      source_file_dependencies:
        - vllm/platforms/rocm.py
      commands:
        - pytest -v -s v1/attention --ignore=v1/attention/test_mla_backends.py
  automatic_shard: true
  source_file_dependencies:
    - vllm/v1/attention/
    - tests/v1/attention
"""


def _spec():
    return yaml.safe_load(SPEC)[0]


def _jobs(raw, group_depends_on=None):
    return {job.key: job for job in resolve_step(raw, group_depends_on)}


def _step(text):
    return yaml.safe_load(text)[0]


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


def _raises(code, match, raw, group_depends_on=None):
    with pytest.raises(DeviceStepError, match=match) as error:
        compile_device_step(raw, group_depends_on)
    assert error.value.code == code, str(error.value)
    return str(error.value)


# ---- compiling --------------------------------------------------------------


def test_the_first_nvidia_device_is_the_step_and_the_amd_one_its_exact_mirror():
    step, eager, extended = compile_device_step(_spec())
    mirror = step.pop("mirror")["amd"]
    assert step == {
        "label": ":nvidia: (H100) V1 Attention",
        "key": "v1-attention",
        "device": "h100",
        "automatic_shard": True,
        "source_file_dependencies": [
            "vllm/v1/attention/",
            "tests/v1/attention",
            "vllm/v1/hisparse/",
        ],
        "timeout_in_minutes": 85,
        "commands": ["pytest -v -s v1/attention"],
    }
    assert mirror == {
        "label": ":amd: (MI300) V1 Attention",
        "device": "mi300_1",
        "automatic_shard": True,
        "source_file_dependencies": [
            "vllm/v1/attention/",
            "tests/v1/attention",
            "vllm/platforms/rocm.py",
        ],
        "timeout_in_minutes": 125,
        "env": {"VLLM_ROCM_USE_AITER": "1"},
        "commands": [
            "pytest -v -s v1/attention --ignore=v1/attention/test_mla_backends.py"
        ],
        "depends_on": ["image-build-amd"],
        "dind": False,
        "num_devices": 1,
        EXACT_MIRROR: True,
    }
    # The variants are steps of their own.
    assert eager["key"] == "v1-attention-eager"
    assert extended["key"] == "v1-attention-extended"


def test_variants_start_from_their_devices_job():
    jobs = _jobs(_spec())
    eager = jobs["v1-attention-eager"]
    base = jobs["v1-attention"]
    assert eager.label == ":nvidia: (H100) V1 Attention [eager]"
    assert eager.fields == {**base.fields, "env": {"VLLM_ENFORCE_EAGER": "1"}}
    extended = jobs["v1-attention-extended"]
    assert extended.label == ":nvidia: (H100) V1 Attention [extended]"
    assert extended.fields["timeout_in_minutes"] == 90
    assert extended.fields["commands"] == ["pytest -v -s --runslow v1/attention"]
    assert (
        extended.fields["source_file_dependencies"]
        == base.fields["source_file_dependencies"]
    )
    # Never on another device.
    assert {job.device for job in jobs.values() if job.variant} == {"h100"}


def test_a_variant_adds_dependencies_and_may_replace_step_level_commands():
    raw = _step(
        """\
- label: T
  key: t
  devices:
    h100:
      timeout_in_minutes: 10
      variants:
        slow:
          optional: true
          env:
            SLOW: "1"
          source_file_dependencies:
            - slow/
          commands:
            - pytest --slow
  env:
    SHARED: "1"
  source_file_dependencies:
    - t/
  commands:
    - pytest
"""
    )
    slow = _jobs(raw)["t-slow"]
    assert slow.fields["commands"] == ["pytest --slow"]
    assert slow.fields["source_file_dependencies"] == ["t/", "slow/"]
    assert slow.fields["env"] == {"SHARED": "1", "SLOW": "1"}
    assert slow.label == ":nvidia: (H100) [Nightly] T [slow]"


def test_a_step_level_field_is_every_jobs():
    jobs = _jobs(_spec())
    for job in jobs.values():
        assert job.fields["automatic_shard"] is True
        assert job.fields["source_file_dependencies"][:2] == [
            "vllm/v1/attention/",
            "tests/v1/attention",
        ]


def test_steps_without_devices_pass_through_untouched():
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
    assert expand_steps(legacy) == before
    doc = {"group": "G", "steps": legacy}
    assert expand_document(doc) is doc
    raw = _spec()
    before = copy.deepcopy(raw)
    expand_steps([raw])
    assert raw == before


def test_a_file_mixes_both_forms(tmp_path, monkeypatch):
    legacy = """\
- label: ":nvidia: (H100) Legacy"
  key: legacy
  device: h100
  commands:
    - pytest legacy
  mirror:
    amd:
      device: mi300_1
"""
    steps = _load(tmp_path, monkeypatch, legacy + SPEC)
    assert [step.key for step in steps] == [
        "legacy",
        "v1-attention",
        "v1-attention-eager",
        "v1-attention-extended",
    ]
    assert "_exact" not in steps[0].mirror["amd"]
    assert steps[1].mirror["amd"]["_exact"] is True


# ---- merge rules ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("device_field", "message"),
    [
        ({"source_file_dependencies": ["tests/v1/attention"]}, "already in the step"),
        ({"automatic_shard": False}, "`automatic_shard` is set at the step level"),
    ],
)
def test_a_device_may_not_shadow_the_step(device_field, message):
    raw = _spec()
    raw["devices"]["mi300_1"].update(device_field)
    text = _raises("E3", message, raw)
    assert "device mi300_1" in text


def test_a_device_may_not_redefine_an_env_var_or_set_commands_twice():
    raw = _step(
        """\
- label: T
  key: t
  devices:
    h100:
      env:
        A: "2"
  env:
    A: "1"
  commands:
    - pytest
"""
    )
    _raises("E3", "set it on every device or only at step level", raw)
    raw["devices"]["h100"] = {"commands": ["pytest -x"]}
    _raises("E3", "`commands` is set at the step level and on the device", raw)
    raw["devices"]["h100"] = {"working_dir": "/x"}
    raw["working_dir"] = "/y"
    _raises("E3", "`working_dir`", raw)


def test_machine_fields_live_only_on_devices():
    for name, value, hint in [
        ("timeout_in_minutes", 5, "timeout_in_minutes` belongs on each device"),
        ("optional", True, "write `optional: true` on the nightly device"),
        ("num_devices", 2, "num_devices"),
        ("soft_fail", True, "soft_fail"),
    ]:
        raw = _spec()
        raw[name] = value
        _raises("E4", hint, raw)


# ---- same_as -----------------------------------------------------------------------

SAME_AS = """\
- label: Fused MoE Kernels
  key: fused-moe-kernels
  devices:
    h100:
      timeout_in_minutes: 40
      env:
        A: "1"
        B: "1"
      source_file_dependencies:
        - csrc/moe/
      commands:
        - pytest -v -s kernels/moe
      variants:
        eager:
          env:
            EAGER: "1"
    b200-k8s:
      same_as: h100
      timeout_in_minutes: 30
      env:
        B: "2"
      source_file_dependencies:
        - csrc/b200/
    mi355_1:
      timeout_in_minutes: 60
      commands:
        - pytest -v -s kernels/moe --ignore=kernels/moe/test_cutlass_moe.py
    mi300_1:
      same_as: mi355_1
  automatic_shard: true
  source_file_dependencies:
    - vllm/model_executor/layers/fused_moe/
"""


def test_same_as_copies_a_sibling_and_replaces_adds_or_merges_what_it_writes():
    jobs = _jobs(_step(SAME_AS))
    assert set(jobs) == {
        "fused-moe-kernels",
        "fused-moe-kernels-eager",
        "fused-moe-kernels-b200",
        "amd-fused-moe-kernels",
        "amd-fused-moe-kernels-mi300",
    }
    b200 = jobs["fused-moe-kernels-b200"]
    assert b200.label == ":nvidia: (B200) Fused MoE Kernels"
    assert b200.fields == {
        "automatic_shard": True,
        "timeout_in_minutes": 30,
        "env": {"A": "1", "B": "2"},
        "source_file_dependencies": [
            "vllm/model_executor/layers/fused_moe/",
            "csrc/moe/",
            "csrc/b200/",
        ],
        "commands": ["pytest -v -s kernels/moe"],
    }
    mi300 = jobs["amd-fused-moe-kernels-mi300"]
    assert mi300.label == ":amd: (MI300) Fused MoE Kernels"
    assert mi300.fields == jobs["amd-fused-moe-kernels"].fields


@pytest.mark.parametrize(
    ("device", "same_as", "message"),
    [
        (
            "mi300_1",
            "h100",
            "is a nvidia device; copy only a device of the same vendor",
        ),
        ("h100", "b200-k8s", "must name an earlier device"),
        ("mi300_1", "eager", "must name an earlier device of this step"),
        ("mi300_1", "mi325_1", "must name an earlier device of this step"),
    ],
)
def test_a_bad_same_as_is_rejected(device, same_as, message):
    raw = _step(SAME_AS)
    raw["devices"][device]["same_as"] = same_as
    _raises("E5", message, raw)


def test_same_as_does_not_chain():
    raw = _step(SAME_AS)
    raw["devices"]["b200"] = {"same_as": "b200-k8s", "key": "x"}
    _raises("E5", "has `same_as` itself; point at h100 instead", raw)


def test_same_as_rejects_a_no_op_override():
    raw = _step(SAME_AS)
    raw["devices"]["b200-k8s"]["commands"] = ["pytest -v -s kernels/moe"]
    _raises("E6", "`commands` is the same as h100's", raw)
    raw = _step(SAME_AS)
    raw["devices"]["b200-k8s"]["source_file_dependencies"] = ["csrc/moe/"]
    _raises("E6", "already come from h100", raw)
    raw = _step(SAME_AS)
    raw["devices"]["b200-k8s"]["env"] = {"A": "1"}
    _raises("E6", "already come from h100", raw)


# ---- variants ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "code", "message"),
    [
        (None, "E6", "must change at least one field"),
        ({"label": "Other"}, "E6", "besides key and label"),
        ({"timeout_in_minutes": 125}, "E6", "is the device job's already"),
        ({"env": {"VLLM_ROCM_USE_AITER": "1"}}, "E6", "already have that value"),
        (
            {"source_file_dependencies": ["tests/v1/attention"]},
            "E6",
            "already in the device job",
        ),
        ({"same_as": "h100"}, "E5", "a variant cannot use `same_as`"),
        (
            {"variants": {"x": {"optional": True}}},
            "E2",
            "variants cannot have variants",
        ),
        ({"b200-k8s": {}}, "E2", "a variant runs on its device"),
        ({"depends_on": ["x"]}, "E2", "set once, at the step level"),
        ({"timeout": 5}, "E2", "did you mean `timeout_in_minutes`"),
        ({"num_devices": 2}, "E7", "mi300_1 already means 1 GPU"),
    ],
)
def test_a_bad_variant_is_rejected(body, code, message):
    raw = _spec()
    raw["devices"]["mi300_1"]["variants"] = {"v": body}
    _raises(code, message, raw)


def test_a_variant_name_is_lowercase_words():
    raw = _spec()
    raw["devices"]["h100"]["variants"]["Eager_Mode"] = {"optional": True}
    _raises("E1", "variant name must match", raw)


def test_variant_keys_and_labels_can_be_pinned():
    raw = _spec()
    raw["devices"]["h100"]["variants"]["eager"].update(
        {"key": "attention-eager-legacy", "label": "Attention (eager)"}
    )
    raw["devices"]["mi300_1"]["key"] = "amd-attention-pinned"
    raw["devices"]["mi300_1"]["variants"] = {"aiter": {"env": {"AITER": "1"}}}
    jobs = _jobs(raw)
    eager = jobs["attention-eager-legacy"]
    assert eager.label == ":nvidia: (H100) Attention (eager)"
    # A pinned device's variants derive from its pinned key.
    assert jobs["amd-attention-pinned-aiter"].label == (
        ":amd: (MI300) V1 Attention [aiter]"
    )


# ---- keys --------------------------------------------------------------------------


def test_default_keys_follow_the_map_order_per_vendor():
    raw = _step(
        """\
- label: T
  key: t
  devices:
    l4:
    mi355_dpx:
    b200-k8s:
    mi300_1:
    cpu-small:
  commands:
    - pytest
"""
    )
    assert [job.key for job in resolve_step(raw)] == [
        "t",
        "amd-t",
        "t-b200",
        "amd-t-mi300",
        "t-cpu",
    ]


def test_an_explicit_key_is_verbatim():
    raw = _step(
        """\
- label: Fusion E2E
  key: fusion-e2e
  devices:
    mi355_2:
      key: fusion-e2e-amd
  commands:
    - pytest
"""
    )
    (step,) = compile_device_step(raw)
    assert step["key"] == "fusion-e2e-amd"
    assert step["label"] == ":amd: (MI355) Fusion E2E"


def test_two_devices_of_one_key_tag_need_a_pin():
    raw = _step(
        """\
- label: T
  key: t
  devices:
    mi355_1:
    mi355_4:
  commands:
    - pytest
"""
    )
    _raises("E8", "share the key tag 'mi355'", raw)
    raw["devices"]["mi355_4"] = {"key": "amd-t-4"}
    assert [job.key for job in resolve_step(raw)] == ["amd-t", "amd-t-4"]


def test_two_jobs_of_one_step_may_not_share_a_key():
    raw = _spec()
    raw["devices"]["mi300_1"]["key"] = "v1-attention-eager"
    _raises("E8", "two jobs are keyed v1-attention-eager", raw)


def test_the_mirror_is_the_amd_job_keyed_amd_plus_the_steps_key():
    # A pin that breaks the pair makes the AMD job a step of its own.
    raw = _spec()
    raw["devices"]["mi300_1"]["key"] = "amd-v1-attention-mi300"
    compiled = compile_device_step(raw)
    assert "mirror" not in compiled[0]
    amd_step = next(s for s in compiled if s["key"] == "amd-v1-attention-mi300")
    assert amd_step["device"] == "mi300_1"
    assert amd_step["dind"] is False
    # A pin on both sides keeps it.
    raw["devices"]["h100"]["key"] = "attention"
    raw["devices"]["mi300_1"]["key"] = "amd-attention"
    step = compile_device_step(raw)[0]
    assert step["key"] == "attention"
    assert step["mirror"]["amd"]["device"] == "mi300_1"


def test_the_step_is_the_non_amd_device_keyed_k_even_when_it_is_not_first():
    raw = _step(
        """\
- label: T
  key: t
  devices:
    h200_35gb:
      key: t-h200
    mi300_dpx:
    cpu-small:
      key: t
  commands:
    - pytest
"""
    )
    compiled = compile_device_step(raw)
    step = next(s for s in compiled if s["key"] == "t")
    assert step["device"] == "cpu-small"
    assert step["mirror"]["amd"]["device"] == "mi300_dpx"


# ---- labels ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("device", "label"),
    [
        ("h100", ":nvidia: (H100) T"),
        ("h200", ":nvidia: (H200) T"),
        ("h200_18gb", ":nvidia: (H200 MIG 18GB) T"),
        ("h200_35gb", ":nvidia: (H200 MIG 35GB) T"),
        ("b200-k8s", ":nvidia: (B200) T"),
        ("l4", ":nvidia: (L4) T"),
        ("a100", ":nvidia: (A100) T"),
        ("dgx-spark", ":nvidia: (DGX Spark) T"),
        ("mi250_1", ":amd: (MI250) T"),
        ("mi300_4", ":amd: (MI300) T"),
        ("mi300_dpx", ":amd: (MI300 DPX) T"),
        ("mi325_8", ":amd: (MI325) T"),
        ("mi355_8", ":amd: (MI355) T"),
        ("mi355_dpx", ":amd: (MI355 DPX) T"),
        ("cpu-small", ":computer: (CPU) T"),
        ("cpu-medium", ":computer: (CPU) T"),
    ],
)
def test_labels_come_from_the_registry(device, label):
    raw = {"label": "T", "key": "t", "devices": {device: None}, "commands": ["x"]}
    (job,) = resolve_step(raw)
    assert job.label == label
    raw["devices"][device] = {"optional": True}
    (job,) = resolve_step(raw)
    assert job.label == label.replace(") T", ") [Nightly] T")


def test_a_device_label_replaces_only_the_title():
    raw = _spec()
    raw["devices"]["mi300_1"]["label"] = "V1 Attention (ROCm)"
    raw["devices"]["mi300_1"]["optional"] = True
    jobs = _jobs(raw)
    assert (
        jobs["amd-v1-attention"].label == ":amd: (MI300) [Nightly] V1 Attention (ROCm)"
    )
    assert jobs["v1-attention"].label == ":nvidia: (H100) V1 Attention"


@pytest.mark.parametrize(
    "label",
    [":nvidia: (H100) V1 Attention", "(H100) V1 Attention", "[Nightly] V1 Attention"],
)
def test_a_label_with_what_the_generator_adds_is_rejected(label):
    raw = _spec()
    raw["label"] = label
    _raises("E2", "write the bare title", raw)
    raw = _spec()
    raw["devices"]["h100"]["label"] = label
    _raises("E2", "write the bare title", raw)


# ---- the registry --------------------------------------------------------------------


def _group_steps(devices, header_depends_on, step_depends_on=None):
    raw = {"label": "T", "key": "t", "devices": devices, "commands": ["x"]}
    if step_depends_on:
        raw["depends_on"] = step_depends_on
    return {s["key"]: s for s in compile_device_step(raw, header_depends_on)}


def test_the_registry_sets_each_vendors_image_dependency():
    steps = _group_steps(
        {"h100": None, "mi355_1": None, "cpu-small": None, "dgx-spark": None},
        ["image-build"],
    )
    # NVIDIA: the file's (the loader adds it).
    assert "depends_on" not in steps["t"]
    assert steps["t"]["mirror"]["amd"]["depends_on"] == ["image-build-amd"]
    assert steps["t-cpu"]["depends_on"] == ["image-build-cpu"]
    assert steps["t-dgx-spark"]["depends_on"] == ["arm64-image-build"]


def test_other_step_dependencies_follow_the_image():
    steps = _group_steps(
        {"h100": None, "mi355_1": None, "cpu-small": None},
        ["image-build"],
        ["prepare-data"],
    )
    assert steps["t"]["depends_on"] == ["image-build", "prepare-data"]
    assert steps["t"]["mirror"]["amd"]["depends_on"] == [
        "image-build-amd",
        "prepare-data",
    ]
    assert steps["t-cpu"]["depends_on"] == ["image-build-cpu", "prepare-data"]


def test_a_file_that_builds_no_image_gives_cpu_jobs_none():
    steps = _group_steps({"cpu-medium": None}, [])
    assert steps["t"]["depends_on"] == []


def test_amd_jobs_get_no_dind_and_the_pools_gpus():
    steps = _group_steps(
        {"mi355_4": None, "mi300_dpx": {"key": "amd-t-dpx", "dind": True}}, None
    )
    assert steps["amd-t"]["dind"] is False
    assert steps["amd-t"]["num_devices"] == 4
    assert steps["amd-t-dpx"]["dind"] is True
    assert steps["amd-t-dpx"]["num_devices"] == 1


@pytest.mark.parametrize("device", ["mi355_4", "cpu-small"])
def test_num_devices_is_only_for_nvidia(device):
    raw = {
        "label": "T",
        "key": "t",
        "devices": {device: {"num_devices": 4}},
        "commands": ["x"],
    }
    _raises("E7", "num_devices", raw)


def test_every_amd_pool_is_registered_with_its_gpu_count():
    for name, config in amd.AMD_DEVICE_CONFIGS.items():
        info = DEVICES[name]
        assert info.vendor == Vendor.AMD
        assert info.gpus == config.default_gpu_count
        assert info.queue == config.queue
        assert info.tag == amd.get_amd_label_tag(name)


@pytest.mark.parametrize("device", sorted(NON_AMD_DEVICES))
def test_the_registry_queue_is_the_generators(device):
    step = Step(label="T", key="t", device=device)
    assert buildkite_step.get_agent_queue(step) == NON_AMD_DEVICES[device].queue


def test_amd_label_tags_are_unchanged():
    # What get_amd_label_tag derived from the device name before the registry.
    def derived(value):
        if not value.startswith("mi"):
            return ""
        if value.endswith("_dpx"):
            return f"{value.removesuffix('_dpx').upper()} DPX"
        return value.split("_", 1)[0].upper()

    for name in list(amd.AMD_DEVICE_CONFIGS) + ["amd_cpu", "h100", "zen5"]:
        assert amd.get_amd_label_tag(name) == derived(name), name


# ---- validation --------------------------------------------------------------------


def test_an_unknown_device_lists_the_valid_ones():
    raw = _spec()
    raw["devices"]["r9700_1"] = None
    message = _raises("E1", "'r9700_1' is not in the device registry", raw)
    assert "mi355_1" in message and "h100" in message


@pytest.mark.parametrize(
    ("field", "value", "hint"),
    [
        ("device", "h100", "only as keys under `devices:`"),
        ("gpu", "h100", "only as keys under `devices:`"),
        ("mirror", {"amd": {}}, "not a `mirror.amd` block"),
        ("vendors", {}, "the `vendors:` form is gone"),
        ("target", "h100", "variants.<name>"),
        ("run_on", "h100", "variants.<name>"),
        ("num_gpus", 2, "write `num_devices` on the NVIDIA device"),
        ("commandz", ["x"], "did you mean `commands`"),
    ],
)
def test_removed_and_unknown_fields_say_where_the_concept_lives(field, value, hint):
    raw = _spec()
    raw[field] = value
    _raises("E2", hint, raw)
    raw = _spec()
    raw["devices"]["h100"][field] = value
    _raises("E2", hint, raw)


@pytest.mark.parametrize(
    ("change", "code", "message"),
    [
        (lambda r: r.pop("key"), "E2", "needs a `key`"),
        (lambda r: r.pop("label"), "E2", "`label` must be a title"),
        (lambda r: r.__setitem__("devices", {}), "E2", "must be a map of device names"),
        (lambda r: r.__setitem__("devices", ["h100"]), "E2", "must be a map"),
        (
            lambda r: r.__setitem__("depends_on", ["image-build-amd"]),
            "E2",
            "comes from the device registry",
        ),
        (
            lambda r: r.__setitem__("depends_on", ["arm64-image-build"]),
            "E2",
            "comes from the device registry",
        ),
        (
            lambda r: r["devices"]["mi300_1"].__setitem__("dind", False),
            "E2",
            "only `dind: true` may be written",
        ),
        (
            lambda r: r["devices"]["mi300_1"].__setitem__("retry", {"automatic": True}),
            "E2",
            "AMD jobs do not read `retry`",
        ),
        (
            lambda r: r["devices"]["mi300_1"].__setitem__("depends_on", ["x"]),
            "E2",
            "set once, at the step level",
        ),
        (
            lambda r: r["devices"]["mi300_1"].__setitem__("commands", "pytest"),
            "E2",
            "must be a list of strings",
        ),
        (
            lambda r: r["devices"]["mi300_1"].__setitem__("env", {"A": 1}),
            "E2",
            "quote numbers",
        ),
        (
            lambda r: r["devices"]["mi300_1"].__setitem__("timeout_in_minutes", None),
            "E6",
            "is empty",
        ),
        (
            lambda r: r["devices"]["mi300_1"].__setitem__("timeout_in_minutes", "5"),
            "E2",
            "must be a number",
        ),
        (
            lambda r: r["devices"]["mi300_1"].__setitem__("key", "amd v1"),
            "E2",
            "must match",
        ),
        (
            lambda r: r["devices"].__setitem__("mi300_1", "pytest"),
            "E2",
            "a device entry must be a map",
        ),
    ],
)
def test_invalid_device_steps_are_rejected(change, code, message):
    raw = _spec()
    change(raw)
    _raises(code, message, raw)


def test_an_invalid_device_step_fails_loading(tmp_path, monkeypatch):
    with pytest.raises(DeviceStepError, match="E1"):
        _load(tmp_path, monkeypatch, SPEC.replace("mi300_1", "mi999_1"))


def test_flow_style_fails_loading_a_devices_step_only(tmp_path, monkeypatch):
    flow = SPEC.replace("        - vllm/platforms/rocm.py\n", "").replace(
        "      source_file_dependencies:\n      commands:\n"
        "        - pytest -v -s v1/attention --ignore",
        "      source_file_dependencies: [vllm/platforms/rocm.py]\n      commands:\n"
        "        - pytest -v -s v1/attention --ignore",
    )
    assert "[vllm/platforms/rocm.py]" in flow
    with pytest.raises(
        DeviceStepError, match=r"E9 .*attention.yaml:\d+ \(step v1-attention\)"
    ):
        _load(tmp_path, monkeypatch, flow)
    # A legacy step may still use it.
    legacy = '- {label: "Legacy", key: legacy, device: h100, commands: [pytest]}\n'
    (step,) = _load(tmp_path / "legacy", monkeypatch, legacy)
    assert step.commands == ["pytest"]
    with pytest.raises(DeviceStepError, match="E9"):
        _load(
            tmp_path / "env",
            monkeypatch,
            SPEC.replace(
                '      env:\n        VLLM_ROCM_USE_AITER: "1"\n',
                '      env: {VLLM_ROCM_USE_AITER: "1"}\n',
            ),
        )


def test_a_leftover_vendors_step_is_rejected():
    raw = {"label": "T", "key": "t", "vendors": {"nvidia": {}}}
    with pytest.raises(DeviceStepError, match="the `vendors:` form is gone"):
        expand_steps([raw])


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
    # Only a compiled `devices:` step may cut the AMD job loose from its step.
    with pytest.raises(DeviceStepError, match="write the step with `devices:`"):
        _load(tmp_path, monkeypatch, HAND_WRITTEN_EXACT)
    with pytest.raises(DeviceStepError):
        expand_steps(yaml.safe_load(HAND_WRITTEN_EXACT))


def test_a_devices_job_key_may_not_repeat_another_jobs(tmp_path, monkeypatch):
    other = """\
- label: ":nvidia: (H100) Other"
  key: v1-attention-eager
  device: h100
"""
    steps = _load(tmp_path, monkeypatch, SPEC + other)
    with pytest.raises(
        DeviceStepError, match="E8 .*duplicate CI step key v1-attention-eager"
    ):
        check_device_step_keys(steps)
    mirror_clash = """\
- label: ":nvidia: (H100) Other"
  key: other
  device: h100
  mirror:
    amd:
      device: mi300_1
- label: T
  key: t
  devices:
    mi355_1:
      key: amd-other
  commands:
    - x
"""
    steps = _load(tmp_path / "mirror", monkeypatch, mirror_clash)
    with pytest.raises(DeviceStepError, match="amd-other"):
        check_device_step_keys(steps)
    # Only legacy steps sharing a key: left alone, as before.
    check_device_step_keys(_load(tmp_path / "legacy", monkeypatch, other + other))
    check_device_step_keys(_load(tmp_path / "ok", monkeypatch, SPEC))


# ---- rendering: the same jobs as the legacy form ------------------------------------
#
# Real steps of vLLM's test_areas (Nov 2026), in both forms.

LEGACY_PAIRS = {
    # Case A: a mirror with its own commands and dependencies, a union today.
    "pair": (
        """\
- label: ":nvidia: (H100) V1 Attention"
  key: v1-attention-h100-mi300
  automatic_shard: true
  timeout_in_minutes: 85
  device: h100
  source_file_dependencies:
  - vllm/v1/attention
  - vllm/v1/hisparse/
  - tests/v1/attention
  commands:
  - pytest -v -s v1/attention
  mirror:
    amd:
      label: ":amd: (MI300) V1 Attention"
      dind: false
      automatic_shard: true
      device: mi300_1
      timeout_in_minutes: 125
      depends_on:
      - image-build-amd
      source_file_dependencies:
      - vllm/v1/attention
      - tests/v1/attention
      - vllm/_aiter_ops.py
      - vllm/platforms/rocm.py
      commands:
      - pytest -v -s v1/attention --ignore=v1/attention/test_mla_backends.py
      - VLLM_ROCM_USE_AITER=1 pytest -v -s v1/attention/test_mla_backends.py
""",
        """\
- label: V1 Attention
  key: v1-attention-h100-mi300
  devices:
    h100:
      timeout_in_minutes: 85
      commands:
        - pytest -v -s v1/attention
    mi300_1:
      timeout_in_minutes: 125
      source_file_dependencies:
        - vllm/_aiter_ops.py
        - vllm/platforms/rocm.py
      commands:
        - pytest -v -s v1/attention --ignore=v1/attention/test_mla_backends.py
        - VLLM_ROCM_USE_AITER=1 pytest -v -s v1/attention/test_mla_backends.py
  automatic_shard: true
  # Today the AMD job takes the NVIDIA job's dependencies too (a union), so
  # in phase 1 they are all shared.
  source_file_dependencies:
    - vllm/v1/attention
    - vllm/v1/hisparse/
    - tests/v1/attention
""",
    ),
    # Case F: nightly on L4 (4 GPUs), every PR on MI355.
    "nightly": (
        """\
- label: ":nvidia: (L4) [Nightly] Distributed NixlConnector PD accuracy"
  key: distributed-nixlconnector-pd-accuracy-4-gpus
  automatic_shard: true
  optional: true
  timeout_in_minutes: 55
  working_dir: /vllm-workspace/tests
  device: l4
  num_devices: 4
  source_file_dependencies:
  - vllm/distributed/kv_transfer/kv_connector/v1/nixl/
  - tests/v1/kv_connector/nixl_integration/
  commands:
  - bash /vllm-workspace/.buildkite/scripts/check-kv-connectors.sh
  - pytest -v -s v1/kv_connector/nixl_integration/test_accuracy_sweep.py
  mirror:
    amd:
      optional: false
      label: ":amd: (MI355) Distributed NixlConnector PD accuracy"
      automatic_shard: true
      dind: false
      device: mi355_4
      timeout_in_minutes: 60
      depends_on:
      - image-build-amd
      source_file_dependencies:
      - vllm/distributed/kv_transfer/kv_connector/v1/nixl/
      - tests/v1/kv_connector/nixl_integration/
      - vllm/platforms/rocm.py
      commands:
      - ATTENTION_BACKEND=TRITON_ATTN pytest -v -s v1/kv_connector/nixl_integration/test_accuracy_sweep.py
""",
        """\
- label: Distributed NixlConnector PD accuracy
  key: distributed-nixlconnector-pd-accuracy-4-gpus
  devices:
    l4:
      optional: true
      num_devices: 4
      timeout_in_minutes: 55
      commands:
        - bash /vllm-workspace/.buildkite/scripts/check-kv-connectors.sh
        - pytest -v -s v1/kv_connector/nixl_integration/test_accuracy_sweep.py
    mi355_4:
      timeout_in_minutes: 60
      source_file_dependencies:
        - vllm/platforms/rocm.py
      commands:
        - ATTENTION_BACKEND=TRITON_ATTN pytest -v -s v1/kv_connector/nixl_integration/test_accuracy_sweep.py
  automatic_shard: true
  working_dir: /vllm-workspace/tests
  source_file_dependencies:
    - vllm/distributed/kv_transfer/kv_connector/v1/nixl/
    - tests/v1/kv_connector/nixl_integration/
""",
    ),
    # Bonus: both vendors run the same thing.
    "inherited": (
        """\
- label: ":nvidia: (H200 MIG 35GB) V1 Spec Decode"
  device: h200_35gb
  key: v1-spec-decode
  automatic_shard: true
  timeout_in_minutes: 40
  source_file_dependencies:
  - vllm/v1/
  - '!vllm/distributed/kv_transfer/'
  - tests/v1/spec_decode
  commands:
  - export VLLM_WORKER_MULTIPROC_METHOD=spawn
  - pytest -v -s -m 'not slow_test' v1/spec_decode
  mirror:
    amd:
      label: ":amd: (MI300 DPX) V1 Spec Decode"
      dind: false
      automatic_shard: true
      device: mi300_dpx
      timeout_in_minutes: 50
      depends_on:
      - image-build-amd
""",
        """\
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
    - "!vllm/distributed/kv_transfer/"
    - tests/v1/spec_decode
  commands:
    - export VLLM_WORKER_MULTIPROC_METHOD=spawn
    - pytest -v -s -m 'not slow_test' v1/spec_decode
""",
    ),
    # Case E1: AMD only, its legacy key pinned.
    "amd-only": (
        """\
- label: ":amd: (MI355) [Nightly] Fusion E2E TP2 AR-RMS (Inductor Partition)"
  key: fusion-e2e-tp2-ar-rms-inductor-partition-amd
  optional: true
  automatic_shard: true
  dind: false
  device: mi355_2
  num_devices: 2
  timeout_in_minutes: 65
  working_dir: /vllm-workspace/
  depends_on:
  - image-build-amd
  source_file_dependencies:
  - vllm/compilation/
  - tests/compile/fusions_e2e/
  commands:
  - export VLLM_TEST_CLEAN_GPU_MEMORY=1
  - pytest -v -s tests/compile/fusions_e2e/test_tp2_ar_rms.py -k "inductor_partition"
""",
        """\
- label: Fusion E2E TP2 AR-RMS (Inductor Partition)
  key: fusion-e2e-tp2-ar-rms-inductor-partition
  devices:
    mi355_2:
      key: fusion-e2e-tp2-ar-rms-inductor-partition-amd
      optional: true
      timeout_in_minutes: 65
  automatic_shard: true
  working_dir: /vllm-workspace/
  source_file_dependencies:
    - vllm/compilation/
    - tests/compile/fusions_e2e/
  commands:
    - export VLLM_TEST_CLEAN_GPU_MEMORY=1
    - pytest -v -s tests/compile/fusions_e2e/test_tp2_ar_rms.py -k "inductor_partition"
""",
    ),
    # Case E3: CPU only, in a file whose depends_on is the CUDA image.
    "cpu-only": (
        """\
- label: ":computer: (CPU) V1 Others"
  key: v1-others-cpu
  automatic_shard: true
  depends_on:
  - image-build-cpu
  source_file_dependencies:
  - vllm/v1/
  - tests/v1
  device: cpu-small
  commands:
  - pytest -v -s -m 'cpu_test' v1/core
""",
        """\
- label: V1 Others
  key: v1-others-cpu
  devices:
    cpu-small:
  automatic_shard: true
  source_file_dependencies:
    - vllm/v1/
    - tests/v1
  commands:
    - pytest -v -s -m 'cpu_test' v1/core
""",
    ),
    # DGX Spark runs in the arm64 image.
    "dgx-spark": (
        """\
- label: ":nvidia: (DGX Spark) [Nightly] FlashInfer NVFP4 XQA"
  key: flashinfer-nvfp4-xqa-dgx-spark
  automatic_shard: true
  timeout_in_minutes: 30
  device: dgx-spark
  optional: true
  num_devices: 1
  depends_on:
  - arm64-image-build
  source_file_dependencies:
  - vllm/v1/attention/backends/flashinfer.py
  commands:
  - pytest -v -s v1/attention/test_attention_backends.py -k nvfp4_decode_correctness
""",
        """\
- label: FlashInfer NVFP4 XQA
  key: flashinfer-nvfp4-xqa-dgx-spark
  devices:
    dgx-spark:
      optional: true
      num_devices: 1
      timeout_in_minutes: 30
  automatic_shard: true
  source_file_dependencies:
    - vllm/v1/attention/backends/flashinfer.py
  commands:
    - pytest -v -s v1/attention/test_attention_backends.py -k nvfp4_decode_correctness
""",
    ),
    # A second AMD device: a native AMD step beside the pair.
    "second-amd": (
        """\
- label: ":nvidia: (H100) T"
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
""",
        """\
- label: T
  key: t
  devices:
    h100:
      commands:
        - pytest t
    mi300_1:
      env:
        AITER: "1"
      source_file_dependencies:
        - rocm/
      commands:
        - pytest t --amd
    mi355_1:
      same_as: mi300_1
      timeout_in_minutes: 70
  source_file_dependencies:
    - shared/
""",
    ),
}


@pytest.mark.parametrize(
    "diff",
    [
        [],
        ["vllm/v1/attention/backends/x.py"],
        ["vllm/v1/hisparse/x.py"],
        ["vllm/platforms/rocm.py"],
        ["vllm/distributed/kv_transfer/x.py"],
        ["rocm/x.py"],
        [STEP_YAML],
        ["docker/Dockerfile.rocm_base"],
    ],
)
@pytest.mark.parametrize("header", [HEADER, "group: Attention\nsteps:\n"])
@pytest.mark.parametrize("case", sorted(LEGACY_PAIRS))
def test_a_devices_step_renders_like_the_legacy_step_it_replaces(
    tmp_path, monkeypatch, fake_global_config, diff, header, case
):
    legacy_text, devices_text = LEGACY_PAIRS[case]
    fake_global_config["list_file_diff"] = diff
    # The edit to the yaml redefines no step, in either form.
    monkeypatch.setattr(amd, "changed_step_keys_in_file", lambda *args: frozenset())
    legacy = _load(tmp_path / "legacy", monkeypatch, legacy_text, header)
    devices = _load(tmp_path / "devices", monkeypatch, devices_text, header)

    assert _render(devices) == _render(legacy)
    assert sorted(s.key for s in devices) == sorted(s.key for s in legacy)
    for old, new in zip(
        sorted(legacy, key=lambda s: s.key), sorted(devices, key=lambda s: s.key)
    ):
        assert set(new.source_file_dependencies or []) == set(
            old.source_file_dependencies or []
        )
        assert new.depends_on == old.depends_on
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


def test_a_file_without_an_image_renders_like_its_legacy_cpu_step(
    tmp_path, monkeypatch, fake_global_config
):
    header = "group: Rust Frontend Cargo\ndepends_on: []\nsteps:\n"
    legacy_text = """\
- label: ":computer: (CPU) Rust Frontend Cargo"
  key: rust-frontend-cargo-tests
  depends_on: []
  timeout_in_minutes: 20
  device: cpu-medium
  no_plugin: true
  source_file_dependencies:
  - rust/
  commands:
  - .buildkite/scripts/run-rust-frontend-cargo-ci.sh test
"""
    devices_text = """\
- label: Rust Frontend Cargo
  key: rust-frontend-cargo-tests
  devices:
    cpu-medium:
      timeout_in_minutes: 20
  no_plugin: true
  source_file_dependencies:
    - rust/
  commands:
    - .buildkite/scripts/run-rust-frontend-cargo-ci.sh test
"""
    fake_global_config["list_file_diff"] = ["rust/x.rs"]
    legacy = _load(tmp_path / "legacy", monkeypatch, legacy_text, header)
    devices = _load(tmp_path / "devices", monkeypatch, devices_text, header)
    assert _render(devices) == _render(legacy)


def test_a_variant_renders_like_the_separate_step_it_replaces(
    tmp_path, monkeypatch, fake_global_config
):
    legacy_text = """\
- label: ":nvidia: (H100) T"
  key: t
  device: h100
  source_file_dependencies:
  - t/
  commands:
  - pytest t
- label: ":nvidia: (H100) T [eager]"
  key: t-eager
  device: h100
  env:
    VLLM_ENFORCE_EAGER: "1"
  source_file_dependencies:
  - t/
  commands:
  - pytest t
"""
    devices_text = """\
- label: T
  key: t
  devices:
    h100:
      variants:
        eager:
          env:
            VLLM_ENFORCE_EAGER: "1"
  source_file_dependencies:
    - t/
  commands:
    - pytest t
"""
    fake_global_config["list_file_diff"] = ["t/x.py"]
    legacy = _load(tmp_path / "legacy", monkeypatch, legacy_text)
    devices = _load(tmp_path / "devices", monkeypatch, devices_text)
    assert _render(devices) == _render(legacy)


def test_an_nvidia_job_takes_the_files_depends_on_whatever_it_is(
    tmp_path, monkeypatch, fake_global_config
):
    # vLLM's monokernels.yaml builds on the AMD image for every step.
    header = "group: Monokernels\ndepends_on:\n  - image-build-amd\nsteps:\n"
    steps = _load(
        tmp_path,
        monkeypatch,
        "- label: T\n  key: t\n  devices:\n    b200-k8s:\n  commands:\n    - x\n",
        header,
    )
    assert steps[0].depends_on == ["image-build-amd"]


def test_retries_pick_every_job_of_a_devices_step(tmp_path, monkeypatch):
    steps = _load(tmp_path, monkeypatch, SPEC)
    keys = frozenset({"amd-v1-attention", "v1-attention-eager"})
    selected, selected_keys = select_steps_and_dependencies(
        steps
        + _load(
            tmp_path / "images",
            monkeypatch,
            "- {label: AMD image, key: image-build-amd, device: amd_cpu}\n"
            "- {label: Image, key: image-build}\n",
            "steps:\n",
        ),
        keys,
    )
    assert {step.key for step in selected} == {
        "v1-attention",
        "v1-attention-eager",
        "image-build-amd",
        "image-build",
    }
    assert selected_keys == keys | {"image-build-amd", "image-build"}


def test_an_exact_mirror_counts_its_yaml_only_when_it_lists_it(
    tmp_path, monkeypatch, fake_global_config
):
    fake_global_config["list_file_diff"] = [STEP_YAML]
    monkeypatch.setattr(amd, "changed_step_keys_in_file", lambda *args: frozenset())
    step = _load(tmp_path, monkeypatch, SPEC)[0]
    mirror = buildkite_step.amd_mirror_step(step)
    assert STEP_YAML in mirror.source_file_dependencies
    assert mirror.source_file_declared is False
    assert not buildkite_step._step_should_run(mirror, [STEP_YAML])

    declared = SPEC.replace(
        "        - vllm/platforms/rocm.py\n",
        f"        - vllm/platforms/rocm.py\n        - {STEP_YAML}\n",
    )
    step = _load(tmp_path / "declared", monkeypatch, declared)[0]
    mirror = buildkite_step.amd_mirror_step(step)
    assert mirror.source_file_declared is True
    assert buildkite_step._step_should_run(mirror, [STEP_YAML])


# ---- what an edit to the yaml changes -------------------------------------------


def _edit(old, new, text=SPEC):
    assert old in text
    return text.replace(old, new, 1)


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        pytest.param(SPEC, set(), id="unchanged"),
        pytest.param(
            _edit("    - tests/v1/attention\n", "    - tests/v1/attention/\n"),
            {
                "v1-attention",
                "amd-v1-attention",
                "v1-attention-eager",
                "v1-attention-extended",
            },
            id="shared",
        ),
        pytest.param(
            _edit("timeout_in_minutes: 85", "timeout_in_minutes: 80"),
            # `extended` sets its own.
            {"v1-attention", "v1-attention-eager"},
            id="device-but-overridden",
        ),
        pytest.param(
            _edit('VLLM_ENFORCE_EAGER: "1"', 'VLLM_ENFORCE_EAGER: "0"'),
            {"v1-attention-eager"},
            id="variant",
        ),
        pytest.param(
            _edit("timeout_in_minutes: 125", "timeout_in_minutes: 120"),
            {"amd-v1-attention"},
            id="amd-device",
        ),
    ],
)
def test_changed_step_keys_follow_the_jobs_an_edit_changes(head, expected):
    keys = amd.changed_step_keys(HEADER + SPEC, HEADER + head)
    assert keys == frozenset(expected)


def test_changed_step_keys_give_up_on_a_base_that_does_not_compile():
    broken = _edit("mi300_1", "mi999_1")
    assert amd.changed_step_keys(HEADER + broken, HEADER + SPEC) is None


# ---- the compiler and the generator agree on the fields ---------------------------


def test_job_fields_are_the_step_models_less_what_the_loader_sets():
    from step_vendors import JOB_FIELDS, MACHINE_FIELDS, SOFTWARE_FIELDS

    loader_only = {
        "group",
        "device",
        "mirror",
        "source_file",
        "source_file_declared",
        "mirror_source_file_declared",
    }
    assert set(JOB_FIELDS) == set(Step.model_fields) - loader_only
    # Every job field has exactly one place in a `devices:` step.
    assert set(JOB_FIELDS) - {"key", "label", "depends_on"} == set(
        SOFTWARE_FIELDS
    ) | set(MACHINE_FIELDS)
    assert not set(SOFTWARE_FIELDS) & set(MACHINE_FIELDS)


def test_an_exact_mirror_takes_every_field_it_carries_from_its_block():
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
