import re
from pathlib import Path

import minijinja
import yaml

BUILDKITE_DIR = Path(__file__).parents[1]
TEMPLATE = (BUILDKITE_DIR / "test-template-amd.j2").read_text()
BOOTSTRAP = (BUILDKITE_DIR / "bootstrap-amd.sh").read_text()
PRODUCTION = ["amdexperimental", "amdproduction"]
NATIVE_TAG = "rocm/vllm-dev:ci_base-build-$BUILDKITE_BUILD_ID"


def _step(label, gpu="mi300", tag="amdgfx942nightly", **extra):
    return {
        "label": label,
        "agent_pool": f"{gpu}_1",
        "mirror_hardwares": PRODUCTION + [tag],
        "dind": False,
        "source_file_dependencies": ["vllm/model_executor"],
        "commands": ["pytest -v -s kernels"],
        **extra,
    }


STEPS = [
    _step("MoE"),
    _step("Optional", gpu="mi355", tag="amdgfx950nightly", optional=True),
    _step("MI250", gpu="mi250", tag="amdgfx90anightly"),  # AITER skips gfx90a
    # Not mirrored to amdproduction: never runs there, nightly or not.
    _step("Experimental", mirror_hardwares=["amdexperimental", "amdgfx942nightly"]),
]


def _context(**overrides):
    return {
        "branch": "main",
        "list_file_diff": "",
        "run_all": "0",
        "nightly": "0",
        "torch_nightly": "0",
        "mirror_hw": "amdproduction",
        "fail_fast": "false",
        "vllm_use_precompiled": "1",
        "vllm_merge_base_commit": "abc123",
        "cov_enabled": "0",
        "vllm_ci_branch": "my-branch",
        "rocm_base_refresh_skip": "0",
        "rocm_base_refresh_force": "0",
        "steps": STEPS,
        **overrides,
    }


def _render(**overrides):
    rendered = minijinja.Environment().render_str(TEMPLATE, **_context(**overrides))
    steps = yaml.safe_load(rendered)[0]["steps"]
    return {step.get("key", step.get("label")): step for step in steps}


def _tests(steps):
    """The GPU test steps, by label."""
    return {
        step["label"].split(": ", 1)[1]: step
        for step in steps.values()
        if step.get("label", "").startswith("mi")
    }


def test_bootstrap_passes_the_flag_off_by_default():
    passed = set(re.findall(r"^\s*-D (\w+)=", BOOTSTRAP, re.M))
    assert passed == set(_context(aiter_nightly="0")) - {"steps"}
    assert re.search(r'AITER_NIGHTLY:-\}" ]]; then\s+AITER_NIGHTLY=0', BOOTSTRAP)
    # The nightly tests main as it is, docs-only changes included.
    assert re.search(r'"1" \]\]; then\s+DOCS_ONLY_DISABLE=1', BOOTSTRAP)


def test_flag_off_renders_the_stock_pipeline():
    steps = _render(aiter_nightly="0")
    assert steps == _render()
    assert "aiter-nightly-amd" not in steps
    assert steps["image-build-amd"]["depends_on"] == "ensure-ci-base-amd"
    # Every production step runs, whatever its GPU tag.
    assert set(_tests(steps)) == {"MoE", "Optional", "MI250"}


def test_nightly_installs_aiter_between_ci_base_and_test_image():
    steps = _render(aiter_nightly="1")
    overlay = steps["aiter-nightly-amd"]
    assert overlay["depends_on"] == "ensure-ci-base-amd"
    # The overlay script lives in the vLLM checkout the job runs in.
    assert overlay["commands"] == [
        "bash .buildkite/scripts/rocm/aiter-nightly-overlay.sh"
    ]

    build = steps["image-build-amd"]
    assert build["depends_on"] == "aiter-nightly-amd"
    # Point the ci_base handoff at the nightly image, then build as usual. $$
    # defers the substitution from pipeline upload to the job.
    assert build["commands"][:2] == [
        "buildkite-agent meta-data set rocm-ci-base-image "
        '"$$(buildkite-agent meta-data get aiter-nightly-ci-base)"',
        "bash .buildkite/scripts/rocm/build-test-image.sh",
    ]
    assert build["env"]["ROCM_CACHE_BRANCH_NAME"] == "aiter-nightly"
    assert build["env"]["ROCM_CONTENT_CACHE_EXPORT_MODE"] == "never"

    # The overlay replaces this build's ci_base-build tag, so native jobs use
    # the usual tag; they just wait for the overlay.
    verify = steps["verify-native-ci-base-amd"]
    assert verify["depends_on"] == "aiter-nightly-amd"
    assert verify["commands"] == [f'docker manifest inspect "{NATIVE_TAG}"']


def test_nightly_runs_every_production_step_on_an_aiter_gpu_unblocked():
    steps = _render(aiter_nightly="1")
    tests = _tests(steps)
    assert set(tests) == {"MoE", "Optional"}
    assert tests["MoE"]["env"]["VLLM_CI_BASE_IMAGE"] == NATIVE_TAG
    # Optional steps run straight away, not behind a block step.
    assert not any("block" in step for step in steps.values())
    assert tests["Optional"]["depends_on"][0] == "image-build-amd"
