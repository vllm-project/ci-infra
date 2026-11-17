# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""The generator's selector gate classifies keys as we do.

The gate (`pipeline_generator/selector_gate.py`) decides which of our keys it
knows, which are AMD's (left to today's rules) and which are optional (left to
a manual unblock or the nightly). We model the same pipeline independently,
mirrors included, in `codemap/pipeline/buildkite.py`. Run both over the real
yaml: a key the gate does not know makes it fall back for the whole build, and
an optional or AMD flag the two disagree on drops or keeps the wrong job.

AMD-ness is the generator's own `is_amd_device` on each side's device: our
hardware families put `amd_cpu` with the CPUs. Optional is the step's raw flag,
not `ci_optional`, which also knows that image builds always run.
"""

import pytest
from ci_selector.codemap.pipeline.buildkite import load_pipeline_configs, load_steps
from ci_selector.codemap.pipeline.step import LoadReport

import selector_gate
import step as step_mod
from amd import is_amd_device
from buildkite_step import _generate_step_key


@pytest.fixture(scope="module")
def pipelines(vllm_repo):
    configs = load_pipeline_configs(vllm_repo)
    ours = {c.name: load_steps(vllm_repo, c, LoadReport()) for c in configs}
    with pytest.MonkeyPatch.context() as mp:
        # As the generator reads them: from the checkout root, for vLLM.
        mp.setattr(
            step_mod,
            "get_global_config",
            lambda: {"github_repo_name": "vllm-project/vllm"},
        )
        mp.chdir(vllm_repo)
        theirs = {}
        for config in configs:
            steps = []
            for job_dir in config.job_dirs:
                steps.extend(step_mod.read_steps_from_job_dir(job_dir))
            for step in steps:  # as pipeline_generator._selector_decision does
                if not step.key:
                    step.key = _generate_step_key(step.label)
            theirs[config.name] = steps
    return ours, theirs


def test_the_gate_classifies_every_key_as_we_do(pipelines):
    ours, theirs = pipelines
    mirrors = optional = 0
    problems = []
    for name, steps in ours.items():
        gate = theirs[name]
        known = {s.buildkite_key for s in steps}
        assert known, f"{name}: no steps loaded"
        mirrors += sum(1 for s in steps if s.mirror_hw)
        optional += sum(1 for s in steps if s.optional)
        for what, expected, actual in (
            ("known", known, selector_gate.known_keys(gate)),
            (
                "optional",
                {s.buildkite_key for s in steps if s.optional},
                selector_gate.optional_keys(gate),
            ),
            (
                "AMD",
                {s.buildkite_key for s in steps if is_amd_device(s.device)},
                selector_gate.amd_keys(gate),
            ),
        ):
            if expected != actual:
                problems.append(
                    f"{name} {what}: only the gate has "
                    f"{sorted(actual - expected)[:10]}, only we have "
                    f"{sorted(expected - actual)[:10]}"
                )
    # A checkout with neither would pass while checking nothing.
    assert mirrors and optional, f"checked {mirrors} mirrors, {optional} optional"
    assert not problems, "\n".join(problems)
