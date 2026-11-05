# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Emission is the one place a bug makes CI run LESS, so every test here pins a
failure resolving to "send nothing", which means everything runs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ci_selector.codemap.step_keys import emit
from ci_selector.handwritten import ONLY_STEP_KEYS_ENV


@dataclass
class FakeStep:
    step_id: str
    key: str | None = None
    label: str = "A Step"
    mirror_hw: str | None = None
    device: str | None = None
    depends_on: list = field(default_factory=list)

    @property
    def buildkite_key(self) -> str:
        from ci_selector.codemap.pipeline.step import derive_step_key

        if not self.key:
            return derive_step_key(self.label)
        if not self.mirror_hw:
            return self.key
        return f"{self.mirror_hw}-{self.key.removesuffix(f'-{self.mirror_hw}')}"


@dataclass
class FakeConfig:
    name: str = "vllm_ci"


@dataclass
class FakePipeline:
    config: FakeConfig
    steps: list


@dataclass
class FakeState:
    pipelines: list


@dataclass
class FakeSelection:
    selected: dict = field(default_factory=dict)
    run_all: dict = field(default_factory=dict)


def state_of(*steps):
    return FakeState([FakePipeline(FakeConfig(), list(steps))])


def test_normal_selection_emits_sorted_keys():
    state = state_of(FakeStep("vllm_ci:b", "b-key"), FakeStep("vllm_ci:a", "a-key"))
    got = emit(state, FakeSelection({"vllm_ci:a": [], "vllm_ci:b": []}))
    assert not got.omit
    assert got.keys == ["a-key", "b-key"]
    assert json.loads(got.as_env()[ONLY_STEP_KEYS_ENV]) == ["a-key", "b-key"]


def test_run_all_omits_the_variable():
    state = state_of(FakeStep("vllm_ci:a", "a-key"))
    got = emit(
        state, FakeSelection({"vllm_ci:a": []}, {"vllm_ci": "a .buildkite change"})
    )
    assert got.omit
    assert "run-all" in got.reason
    assert got.as_env() == {}


def test_empty_selection_omits_rather_than_emitting_an_empty_list():
    # The whole point: an empty list would read as "deliberately run nothing".
    state = state_of(FakeStep("vllm_ci:a", "a-key"))
    got = emit(state, FakeSelection({}))
    assert got.omit
    assert got.as_env() == {}
    assert ONLY_STEP_KEYS_ENV not in got.as_env()


def test_a_kept_step_we_cannot_name_forces_omission():
    state = state_of(FakeStep("vllm_ci:a", "a-key"))
    got = emit(state, FakeSelection({"vllm_ci:a": [], "vllm_ci:ghost": []}))
    assert got.omit
    assert got.unnameable == ["vllm_ci:ghost"]
    assert got.as_env() == {}


def test_a_step_from_another_pipeline_is_not_ours_to_name():
    state = state_of(FakeStep("vllm_ci:a", "a-key"))
    got = emit(state, FakeSelection({"vllm_ci:a": [], "vllm_rocm_ci:x": []}))
    assert not got.omit
    assert got.keys == ["a-key"]


def test_mirror_keeps_the_generators_spelling():
    # We mint `<key>-amd`; the generator publishes `amd-<key>`. Emitting our
    # spelling would name a step that does not exist, and it would not run.
    state = state_of(FakeStep("vllm_ci:lora:amd", "lora-amd", mirror_hw="amd"))
    got = emit(state, FakeSelection({"vllm_ci:lora:amd": []}))
    assert got.keys == ["amd-lora"]


def test_a_keyless_step_uses_the_derived_key():
    state = state_of(
        FakeStep(
            "vllm_ci:Rust Frontend Serve/Admin Coverage",
            None,
            "Rust Frontend Serve/Admin Coverage",
        )
    )
    got = emit(state, FakeSelection({"vllm_ci:Rust Frontend Serve/Admin Coverage": []}))
    assert got.keys == ["rust-frontend-serve-admin-coverage"]


def _image_pipeline():
    return state_of(
        FakeStep("vllm_ci:image", "image-build"),
        FakeStep("vllm_ci:image-cpu", "image-build-cpu"),
        FakeStep("vllm_ci:image-xpu", "image-build-xpu"),
        FakeStep("vllm_ci:smoke", "image-build-smoke-test", depends_on=["image-build"]),
        FakeStep(
            "vllm_ci:kernels", "kernels", device="h100", depends_on=["image-build"]
        ),
        FakeStep(
            "vllm_ci:cpu-test", "cpu-test", device="cpu", depends_on=["image-build-cpu"]
        ),
        FakeStep("vllm_ci:amd-base", "ensure-ci-base-amd"),
    )


def test_images_follow_the_selected_tests():
    """vllm/ci #100085 built the XPU, HPU, CPU and Zen images for two CUDA
    kernel tests. The generator brings a test's own image in through its
    depends_on; only the CUDA image's smoke test is added alongside it."""
    got = emit(_image_pipeline(), FakeSelection({"vllm_ci:kernels": []}))
    assert got.keys == ["image-build-smoke-test", "kernels"]


def test_a_cpu_test_brings_only_its_own_image():
    got = emit(_image_pipeline(), FakeSelection({"vllm_ci:cpu-test": []}))
    assert got.keys == ["cpu-test"], "no CUDA image, so no smoke test"


def test_the_cuda_image_is_the_floor_when_only_amd_is_selected():
    """A comment-only change has always built the CUDA image; AMD steps keep
    today's rules and are dropped by the gate, so they cannot stand in."""
    got = emit(_image_pipeline(), FakeSelection({"vllm_ci:amd-base": []}))
    assert got.keys == ["ensure-ci-base-amd", "image-build", "image-build-smoke-test"]


def test_the_agents_field_does_not_force_a_step():
    """The generator drops `agents`, so it cannot gate execution; it forced
    zen-cpu-image-build onto every PR."""
    from ci_selector.handwritten import KNOWN_STEP_FIELDS

    assert "agents" in KNOWN_STEP_FIELDS
