# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""The pass that runs after a rule has answered, not a rule itself.

A rule decides what a path IS. This adds what every path owes regardless: the
steps running on an image it is built into. Running it in one place is what
stops a new rule under-selecting by forgetting it. Idempotent, skips run-all,
and keeps its own exempt set.
"""

from __future__ import annotations

from . import build_map, hardware
from .claim import Claim
from .state import RepoState


def _pin_existing(claim: Claim) -> None:
    """Freeze the reason for the steps already on the claim, before a pass
    appends its own sentence to `detail`. Without this they inherit a summary
    describing steps they are not."""
    for sid in claim.step_ids:
        claim.step_detail.setdefault(sid, claim.detail)


# Every "nothing to run" rule, plus the one rule that already read the build
# graph and scoped its own answer. The union must not revive a path a rule
# established runs nothing (retired yamls, release-only scripts, inert CI
# trees and inert files all get copied into images), and must not undo the
# rust rule's scoping, which deliberately dropped the borrowed images.
_IMAGE_UNION_EXEMPT = frozenset(
    {"no-code", "no-hardware", "legacy-ci", "inert-ci", "inert", "release-ci", "rust"}
)


def _apply_image_input_union(state: RepoState, path: str, claim: Claim) -> Claim:
    """Add the steps that run on an image this file is built into.

    A pass and not a rule, and that distinction is the design. Claiming these
    paths would run before every rule below and would override hardware scoping
    and the `no-code` answer, both of which are right today. Adding leaves
    those decisions alone and puts what the build DAG knows on top.

    This carries the whole build layer now that `run_all_patterns` is unread:
    the C++, cmake and shared requirements files reach the AMD and Intel
    pipelines through here and nowhere else.

    Added non-droppably: a coverage row says which functions a step ran and
    nothing about which image it ran on. For csrc files a later pass can still
    mark these steps droppable, on wrapper names a row can speak to.
    """
    if claim.run_all or claim.rule in _IMAGE_UNION_EXEMPT or claim.image_union_exempt:
        return claim
    steps = state.artifacts.steps_for_input(path) & state.auto_step_ids
    # A family-exclusive file cannot affect another family's jobs even though
    # its tree is copied into that family's image. Scope rather than skip, so a
    # CPU-only source still reaches the CPU suites. Without this the union
    # brings back the very over-selection the exclusive-family rule exists for.
    family = hardware.exclusive_family_of_path(path)
    if family and path not in state.exclusive_disabled:
        steps &= state.family_steps(family)
    # A mapped file only reaches the images whose builds compile it. Keep this
    # before `added` is computed. An unmapped path keeps the full set, and so
    # does everything if the device list has gaps, since family_steps() is then
    # incomplete and cannot be trusted to subtract.
    fams = state.build_map.families.get(path)
    if fams and not state.preflight.unmapped_devices and build_map.mode() == "on":
        steps &= _build_map_allowed(state, fams)
    added = steps - claim.step_ids
    if added:
        _pin_existing(claim)
        claim.step_ids |= added
        claim.detail += f"; +{len(added)} steps run on an image this file is built into"
        # Sorted so a step in two images always names the same one.
        for df in sorted(state.artifacts.images_for_input(path)):
            for sid in state.artifacts.consumers_of_image(df) & added:
                claim.step_detail.setdefault(
                    sid, f"copied into {df}; this step runs on that image"
                )
                claim.step_rule.setdefault(sid, "image-copy")
    return claim


def _build_map_allowed(state: RepoState, fams: frozenset[str]) -> set[str]:
    """The steps these families may keep.

    "cuda" is the remainder that carries no device token, which is where the
    main image's GPU suites live; reading it as a token family would drop them
    all. "other" is every token family except amd and cpu.
    """
    per, union, nonfamily = state.family_partition()
    allowed: set[str] = set()
    if build_map.CUDA in fams:
        allowed |= nonfamily
    if build_map.AMD in fams:
        allowed |= per.get("amd", frozenset())
    if build_map.CPU in fams:
        allowed |= per.get("cpu", frozenset())
    if build_map.OTHER in fams:
        allowed |= union - per.get("amd", frozenset()) - per.get("cpu", frozenset())
    return allowed
