import os
import re
import subprocess
from pathlib import Path

import minijinja
import pytest
import yaml


BUILDKITE_DIR = Path(__file__).parents[1]
TEMPLATE = (BUILDKITE_DIR / "test-template-amd.j2").read_text()
# Shaped like vLLM's hardware_tests/amd.yaml, as bootstrap passes it when the checkout has
# build.sh. The last step is synthetic: no such step exists in vLLM, it exercises the
# template's source_file_dependencies gating for build steps.
BUILD_STEPS = [
    {
        "label": "AMD: :docker: ROCm stack and runtime",
        "key": "refresh-rocm-base-amd",
        "depends_on": [],
        "commands": ["bash .buildkite/scripts/rocm/build.sh rocm base"],
        "timeout_in_minutes": 540,
        "concurrency": 1,
        "concurrency_group": "vllm/rocm/base/rocm/$ROCM_CI_REPO/$BUILDKITE_BRANCH",
        "env": {"TERM": "xterm-256color"},
        # build.sh exits 75 on a registry error.
        "retry": {"automatic": [{"exit_status": 75, "limit": 1}]},
    },
    {
        "label": "AMD: :docker: build image",
        "key": "image-build-amd",
        "depends_on": ["refresh-rocm-base-amd"],
        "commands": ["bash .buildkite/scripts/rocm/build.sh rocm ci"],
        "timeout_in_minutes": 180,
        "retry": {"automatic": [{"exit_status": 1, "limit": 1}]},
    },
    {
        "label": "AMD: gated build step",
        "key": "gated-build-amd",
        "depends_on": [],
        "commands": ["true"],
        "timeout_in_minutes": 180,
        "soft_fail": True,
        "source_file_dependencies": ["csrc/"],
    },
]


@pytest.mark.parametrize("build_steps", [[], BUILD_STEPS])
@pytest.mark.parametrize("dind", [False, True])
@pytest.mark.parametrize(
    "label,slug",
    [
        (
            ":amd: (MI355) LM Eval DeepSeek V3.2 TP",
            ":amd:-mi355-lm-eval-deepseek-v3-2-tp",
        ),
        (
            ":amd: (MI355) LM Eval DeepSeek V3.2 DP",
            ":amd:-mi355-lm-eval-deepseek-v3-2-dp",
        ),
        (":amd: (MI355) Prefill/Decode", ":amd:-mi355-prefill-decode"),
        ("Tokens@128", "tokens-128"),
        ("Model café", "model-caf-"),
        (
            ":amd: (MI355) Quant, FP8+FP4 Shard %N",
            ":amd:-mi355-quant--fp8-fp4-shard-n",
        ),
        (":amd: (MI355) test_name: FP8-FP4", ":amd:-mi355-test_name:-fp8-fp4"),
    ],
)
def test_amd_template_block_keys_and_dependencies(label, slug, dind, build_steps):
    rendered = minijinja.render_str(
        TEMPLATE,
        amd_build_steps=build_steps,
        ci_repo="rocm/vllm-dev",
        branch="test-branch",
        list_file_diff="",
        mirror_hw="amdproduction",
        run_all="1",
        nightly="0",
        steps=[
            {
                "label": label,
                "agent_pool": "mi355_4",
                "mirror_hardwares": ["amdproduction"],
                "optional": True,
                "dind": dind,
                "commands": ["true"],
            }
        ],
    )
    block, command = yaml.safe_load(rendered)[0]["steps"][-2:]
    expected_key = f"block-mi355_4-{slug}"
    assert re.fullmatch(r"[A-Za-z0-9_:-]+", block["key"])
    assert block["key"] == expected_key
    assert block["block"] == f"Run {label}"
    assert command["label"] == label
    assert command["depends_on"] == (
        expected_key
        if dind or build_steps
        else [expected_key, "verify-native-ci-base-amd"]
    )


def _render(list_file_diff="", aiter_nightly="0", ci_repo="rocm/vllm-ci", **extra):
    gpu_steps = [("MoE", "amdgfx942nightly"), ("MI250", "amdgfx90anightly")]
    rendered = minijinja.render_str(
        TEMPLATE,
        branch="main",
        list_file_diff=list_file_diff,
        mirror_hw="amdproduction",
        run_all="0",
        nightly="0",
        aiter_nightly=aiter_nightly,
        amd_build_steps=BUILD_STEPS,
        ci_repo=ci_repo,
        **extra,
        steps=[
            {
                "label": label,
                "agent_pool": "mi300_1",
                "mirror_hardwares": ["amdproduction", tag],
                "dind": False,
                "commands": ["true"],
            }
            for label, tag in gpu_steps
        ],
    )
    steps = yaml.safe_load(rendered)[0]["steps"]
    return {step.get("key", step.get("label")): step for step in steps}


@pytest.mark.parametrize("ci_repo", ["rocm/vllm-ci", "rocm/vllm-dev"])
def test_build_sh_layout_renders_vllm_build_steps(ci_repo):
    steps = _render(ci_repo=ci_repo)
    queue = "amd-cpu-trusted" if ci_repo == "rocm/vllm-ci" else "amd-cpu"
    build = [s for s in steps.values() if s.get("agents") == {"queue": queue}]
    assert [s["key"] for s in build] == ["refresh-rocm-base-amd", "image-build-amd"]
    assert all(s["env"]["ROCM_CI_REPO"] == ci_repo for s in build)
    base, image = build
    assert base["concurrency"] == 1 and base["timeout_in_minutes"] == 540
    # Exported by bootstrap, so pipeline upload interpolates it.
    assert "$ROCM_CI_REPO" in base["concurrency_group"]
    assert base["env"]["TERM"] == "xterm-256color"
    assert image["depends_on"] == ["refresh-rocm-base-amd"]
    # Infra failures (pod provisioning, lost agents) retry first, then vLLM's own rules.
    infra = [
        {"signal_reason": reason, "limit": 1}
        for reason in ("stack_error", "agent_stop", "agent_refused")
    ]
    assert image["retry"]["automatic"] == infra + BUILD_STEPS[1]["retry"]["automatic"]
    assert base["retry"]["automatic"] == infra + BUILD_STEPS[0]["retry"]["automatic"]

    gpu = steps["MoE"]
    assert gpu["depends_on"] == "image-build-amd"
    pod = gpu["plugins"][0]["kubernetes"]["podSpecPatch"]
    assert pod["containers"][0]["image"] == f"{ci_repo}:build-$BUILDKITE_BUILD_ID"
    assert [v["name"] for v in pod["volumes"]] == ["devshm"]
    assert [m["name"] for m in pod["containers"][0]["volumeMounts"]] == ["devshm"]


@pytest.mark.parametrize("build_steps", [[], BUILD_STEPS])
def test_gpu_job_env_matches_the_checkouts_runner(build_steps):
    """Checkouts without build.sh keep the old run-amd-test.sh, which needs the
    artifact and workspace handoff; build.sh images already contain both."""
    rendered = minijinja.render_str(
        TEMPLATE,
        amd_build_steps=build_steps,
        ci_repo="rocm/vllm-dev",
        branch="main",
        list_file_diff="",
        mirror_hw="amdproduction",
        run_all="1",
        nightly="0",
        steps=[
            {
                "label": "MoE",
                "agent_pool": "mi300_1",
                "mirror_hardwares": ["amdproduction"],
                "dind": False,
                "commands": ["true"],
            }
        ],
    )
    env = yaml.safe_load(rendered)[0]["steps"][-1]["env"]
    handoff = {
        "VLLM_CI_USE_ARTIFACTS": "1",
        "VLLM_CI_ARTIFACT_STEP": "image-build-amd",
        "VLLM_CI_WORKSPACE": "/vllm-workspace",
        "VLLM_CI_REQUIRE_WORKSPACE_MOUNT": "1",
    }
    if build_steps:
        assert not handoff.keys() & env.keys()
    else:
        assert {k: env.get(k) for k in handoff} == handoff


def test_build_sh_layout_gates_build_steps_by_source_files():
    assert "gated-build-amd" not in _render(list_file_diff="vllm/envs.py")
    check = _render(list_file_diff="vllm/envs.py|csrc/ops.cu")["gated-build-amd"]
    assert check["soft_fail"] is True and check["depends_on"] == []


def test_aiter_nightly_build_sh_layout_has_no_overlay_step():
    steps = _render(aiter_nightly="1", ci_repo="rocm/vllm-dev")
    assert "aiter-nightly-amd" not in steps
    assert steps["image-build-amd"]["depends_on"] == ["refresh-rocm-base-amd"]
    gpu_jobs = {
        k
        for k, s in steps.items()
        if s.get("agents", {}).get("queue", "").startswith("amd_mi")
    }
    assert gpu_jobs == {"MoE"}


PR_SHA, MAIN_SHA = "a" * 40, "b" * 40
PREBUILD_OFFER = dict(
    rocm_prebuild="offer",
    pr="123",
    commit=PR_SHA,
    main_commit=MAIN_SHA,
    pipeline_slug="amd-ci",
)


def _queues(steps):
    return {k: s.get("agents", {}).get("queue") for k, s in steps.items()}


@pytest.mark.parametrize(
    "ci_repo,extra",
    [("rocm/vllm-dev", PREBUILD_OFFER), ("rocm/vllm-ci", {}), ("rocm/vllm-dev", {})],
)
def test_only_main_build_steps_target_the_trusted_queue(ci_repo, extra):
    """PR, nightly and branch builds never reach the agents that can push rocm/vllm-ci."""
    queues = _queues(_render(ci_repo=ci_repo, **extra))
    trusted = {k for k, q in queues.items() if q == "amd-cpu-trusted"}
    if ci_repo == "rocm/vllm-ci":
        assert trusted == {"refresh-rocm-base-amd", "image-build-amd"}
    else:
        assert not trusted


def test_pr_offers_a_gated_prebuild_as_a_main_build():
    steps = _render(ci_repo="rocm/vllm-dev", **PREBUILD_OFFER)
    block = steps["block-rocm-prebuild"]
    assert PR_SHA in block["prompt"]
    trigger = next(s for s in steps.values() if "trigger" in s)
    assert trigger["depends_on"] == "block-rocm-prebuild" and trigger["async"] is True
    assert trigger["trigger"] == "amd-ci"
    assert trigger["build"]["branch"] == "main"
    assert trigger["build"]["commit"] == MAIN_SHA
    assert trigger["build"]["env"] == {
        "ROCM_PREBUILD_PR": "123",
        "ROCM_PREBUILD_SHA": PR_SHA,
    }
    assert "block-rocm-prebuild" not in _render(ci_repo="rocm/vllm-dev")


def test_prebuild_build_runs_only_mains_build_sh_on_the_trusted_queue():
    steps = _render(rocm_prebuild="run", prebuild_pr="123")
    assert list(steps) == ["rocm-prebuild"]
    step = steps["rocm-prebuild"]
    assert step["agents"] == {"queue": "amd-cpu-trusted"}
    # Not the PR's amd.yaml: a fixed command, run in the main checkout.
    assert step["commands"] == [f"{BUILD_SH} prebuild"]
    assert step["env"] == {"ROCM_CI_REPO": "rocm/vllm-ci"}
    assert {"exit_status": 75, "limit": 1} in step["retry"]["automatic"]


TRUSTED_HOOK = BUILDKITE_DIR / "hooks" / "amd-cpu-trusted" / "pre-checkout"
BUILD_SH = "bash .buildkite/scripts/rocm/build.sh rocm"
MAIN_JOB = {
    "BUILDKITE_PULL_REQUEST": "false",
    "BUILDKITE_BRANCH": "main",
    "BUILDKITE_REPO": "https://github.com/vllm-project/vllm.git",
    "BUILDKITE_COMMIT": MAIN_SHA,
    "BUILDKITE_COMMAND": f"{BUILD_SH} base",
}


@pytest.mark.parametrize(
    "override,reason",
    [
        ({"BUILDKITE_PULL_REQUEST": "123"}, "PR builds never run here"),
        ({"BUILDKITE_BRANCH": "feature"}, "only main builds run here"),
        ({"BUILDKITE_REPO": "https://github.com/someone/vllm.git"}, "not vllm-project"),
        ({"BUILDKITE_COMMIT": "HEAD"}, "not a full commit SHA"),
        ({"BUILDKITE_PLUGINS": '[{"docker#v5": {}}]'}, "plugins are not allowed"),
        ({"BUILDKITE_COMMAND": f"{BUILD_SH} base; id"}, "command not allowed"),
        ({"BUILDKITE_COMMAND": f"{BUILD_SH} prebuild"}, "needs ROCM_PREBUILD_PR"),
    ],
)
def test_trusted_queue_hook_refuses_untrusted_jobs(override, reason):
    env = {"PATH": os.environ["PATH"], **MAIN_JOB, **override}
    out = subprocess.run(
        ["bash", str(TRUSTED_HOOK)], env=env, capture_output=True, text=True
    )
    assert out.returncode != 0 and reason in out.stderr


VLLM = "https://github.com/vllm-project/vllm.git"


@pytest.mark.parametrize(
    "pr,repo,aiter,expected,branch",
    [
        ("false", VLLM, "0", "rocm/vllm-ci", "main"),
        ("false", "git@github.com:vllm-project/vllm.git", "0", "rocm/vllm-ci", "main"),
        ("123", VLLM, "0", "rocm/vllm-dev", "main"),
        ("", VLLM, "0", "rocm/vllm-dev", "main"),
        ("false", "https://github.com/someone/vllm.git", "0", "rocm/vllm-dev", "main"),
        (
            "false",
            "https://github.com/vllm-project/vllm-x.git",
            "0",
            "rocm/vllm-dev",
            "main",
        ),
        ("false", VLLM, "1", "rocm/vllm-dev", "main"),
        # A pushed or manually built branch other than main never writes the trusted repo.
        ("false", VLLM, "0", "rocm/vllm-dev", "releases/v0.99"),
    ],
)
def test_bootstrap_trust_rule(pr, repo, aiter, expected, branch):
    bootstrap = (BUILDKITE_DIR / "bootstrap-amd.sh").read_text()
    rule = re.search(
        r"ROCM_CI_REPO=rocm/vllm-dev\n.*?export ROCM_CI_REPO\n", bootstrap, re.S
    )
    env = {
        "BUILDKITE_PULL_REQUEST": pr,
        "BUILDKITE_REPO": repo,
        "AITER_NIGHTLY": aiter,
        "BUILDKITE_BRANCH": branch,
    }
    out = subprocess.run(
        ["bash", "-c", rule.group(0) + 'echo "$ROCM_CI_REPO"'],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == expected
