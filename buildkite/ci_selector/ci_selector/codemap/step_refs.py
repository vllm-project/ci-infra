# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Steps a path reaches by a route the import graph cannot see.

Two of them: a step naming the file outright, and the hardware naming
convention. Shared, because both the graph rule and the co-location rule need
both and neither owns them. The hand-written `source_file_dependencies` lists
are never read here: they are what the selector is measured against.
"""

from __future__ import annotations

from . import hardware
from .state import RepoState


def _direct_step_refs(state: RepoState, path: str) -> set[str]:
    """Steps naming the file itself: as a target, a scanned script, or a data
    file."""
    return {
        sid
        for p in state.pipelines
        for sid, st in p.targets.items()
        if path in st.data_files
        or path in st.scripts_seen
        or any(t.path == path for t in st.targets)
    }


PLATFORM_DIR = "vllm/platforms/"


def hardware_steps_held(path: str, hw_steps: set[str]) -> set[str]:
    """The part of a hardware-convention tagging the record may not drop.

    All of it for compiled code, whose kernels reach a family's jobs where no
    recording sees, and for the platform modules, which vLLM loads by a
    qualname string the graph cannot follow and whose import-time code runs in
    every job of the family. None of it for any other Python file: the import
    graph found it, and the Python record, which has rows for AMD steps too,
    sees every call into it. vllm#58689 renamed three classes in one ROCm-only
    model file, and all 106 AMD mirror steps were held though none calls the
    renamed code.
    """
    if path.endswith(".py") and not path.startswith(PLATFORM_DIR):
        return set()
    return set(hw_steps)


def _hardware_family_steps(state: RepoState, path: str) -> tuple[str | None, set[str]]:
    """Steps a source file reaches by hardware naming convention.

    For a source file whose compiled kernels reach a family's jobs invisibly.
    A test, benchmark or example has no such reach, since nothing under vllm/
    imports them, so their steps are exactly their own coverage.
    """
    family = hardware.family_of_path(path)
    if not family or path.startswith(("tests/", "benchmarks/", "examples/")):
        return None, set()
    return family, state.family_steps(family)
