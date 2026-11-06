# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Turn a selection into the step-key list CI consumes.

The last thing that happens, and the one place where a bug makes CI run *less*
rather than more. Everywhere else a failure adds steps; here a missing name
silently removes one.

Two ways that bites: handing over an empty list looks exactly like deciding
nothing needs to run, and keeping a step we cannot spell the way the generator
spells it drops that step without a trace.

Both resolve the same way, using a distinction the transport already makes. An
absent variable and an empty one are different things, and absent means "apply
your own rules". So every failure here omits the variable, and omission runs
everything. `.github/workflows/scripts/run_ci_command.py` already works this
way; this mirrors it.

The generator adds prerequisites itself, so we name tests and never plumbing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..handwritten import (
    AMD_ALWAYS_RUN_STEP_KEYS,
    IMAGE_BUILD_KEY_PREFIX,
    ONLY_STEP_KEYS_ENV,
    PR_PIPELINE,
)
from .hardware import family_of_device
from .pipeline.step import Step
from .selection import Selection
from .state import RepoState


@dataclass
class Emission:
    """What to hand CI, and why, kept together on purpose.

    `keys` means anything only when `omit` is False. When `omit` is True the
    variable must not be set at all, and an empty list is not the same thing.
    """

    omit: bool
    reason: str = ""
    keys: list[str] = field(default_factory=list)
    # Kept steps we could not name. Non-empty always forces omission, and it is
    # reported so drift in the key derivation shows up as a logged reason
    # instead of as steps quietly not running.
    unnameable: list[str] = field(default_factory=list)
    kept: int = 0
    # Steps whose own definition the diff changed or added, by their head
    # keys (Selection.changed_step_keys). CI runs these even when optional.
    changed_keys: list[str] = field(default_factory=list)

    def as_env(self) -> dict[str, str]:
        return (
            {}
            if self.omit
            else {ONLY_STEP_KEYS_ENV: json.dumps(self.keys, separators=(",", ":"))}
        )


def emit(state: RepoState, sel: Selection, pipeline: str = PR_PIPELINE) -> Emission:
    """The step-key list for one pipeline, or a reasoned refusal to send one."""
    if sel.run_all.get(pipeline):
        return Emission(omit=True, reason=f"run-all: {sel.run_all[pipeline]}")

    steps: dict[str, Step] = {
        s.step_id: s
        for p in state.pipelines
        if p.config.name == pipeline
        for s in p.steps
    }

    keys: set[str] = set()
    unnameable: list[str] = []
    kept = 0
    for step_id in sel.selected:
        step = steps.get(step_id)
        if step is None:
            # A selected id this checkout cannot explain: unnameable, and we
            # cannot rule out that it matters.
            if step_id.startswith(f"{pipeline}:"):
                unnameable.append(step_id)
            continue
        kept += 1
        key = step.buildkite_key
        if key:
            keys.add(key)
        else:
            unnameable.append(step_id)

    if unnameable:
        return Emission(
            omit=True,
            reason=f"{len(unnameable)} kept step(s) could not be named",
            unnameable=sorted(unnameable),
            kept=kept,
        )
    keys |= _image_floor_and_smoke_test(keys, steps.values())
    if not keys:
        # An empty answer is a bug in us, never an instruction to run nothing.
        return Emission(
            omit=True,
            reason="nothing selected; refusing to emit an empty list",
            kept=kept,
        )
    return Emission(
        omit=False,
        keys=sorted(keys),
        kept=kept,
        changed_keys=sorted(getattr(sel, "changed_step_keys", ())),
    )


# The CUDA image and the step that smoke-tests it.
IMAGE_SMOKE_TEST_KEY = f"{IMAGE_BUILD_KEY_PREFIX}-smoke-test"


def _image_floor_and_smoke_test(keys: set[str], steps) -> set[str]:
    """Images build on demand: the generator expands each emitted key's
    depends_on, so a selected test brings its own image and no other (vllm/ci
    #100085 built the XPU, HPU, CPU and Zen images, ~40 CPU-minutes, for two
    CUDA kernel tests). What that leaves out is added here:

    - the CUDA image's smoke test, whenever the CUDA image builds, since no
      test depends on it;
    - the CUDA image as the floor when nothing outside AMD (which keeps today's
      rules) is selected, as a comment-only change has always built it.
    """
    by_key = {s.buildkite_key: s for s in steps if s.buildkite_key}
    if IMAGE_BUILD_KEY_PREFIX not in by_key:
        return set()

    def is_amd(key: str) -> bool:
        step = by_key.get(key)
        return key in AMD_ALWAYS_RUN_STEP_KEYS or (
            step is not None
            and (
                step.mirror_hw == "amd"
                or family_of_device(getattr(step, "device", None)) == "amd"
            )
        )

    added: set[str] = set()
    if all(is_amd(k) for k in keys):
        added.add(IMAGE_BUILD_KEY_PREFIX)
    needed, stack = set(), [*keys, *added]
    while stack:
        key = stack.pop()
        if key in needed:
            continue
        needed.add(key)
        step = by_key.get(key)
        stack.extend(getattr(step, "depends_on", None) or ())
    if IMAGE_BUILD_KEY_PREFIX in needed and IMAGE_SMOKE_TEST_KEY in by_key:
        added.add(IMAGE_SMOKE_TEST_KEY)
    return added


def render(emission: Emission) -> str:
    """Both sets side by side, so "was that skip meant to happen?" stays
    answerable later instead of being re-derived from a red build."""
    return json.dumps(
        {
            "omit": emission.omit,
            "reason": emission.reason,
            "kept": emission.kept,
            "emitted": len(emission.keys),
            "keys": emission.keys,
            "unnameable": emission.unnameable,
            "changed_keys": emission.changed_keys,
            "env": emission.as_env(),
        },
        indent=1,
        sort_keys=True,
    )
