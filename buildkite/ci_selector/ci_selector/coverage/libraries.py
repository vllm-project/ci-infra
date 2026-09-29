# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""A dependency bump, routed by the library calls each step made.

A change that only moves the pinned version of a watched library needs the
steps that call that library, not every step of the image it is installed in.
The recorder says which: per step, the functions of each watched library that
code outside it called at runtime (`Stamp.libcalls`).

One direction only. It drops, never adds, and weighs only a step selected for
nothing but files whose whole change is such a pin. That step goes when its
usable row watched each bumped library and saw no call into any of them.
Anything else keeps its step: another change in the same file, a step with no
row, a row too weak to read a silence off, or one recorded before the recorder
watched the library.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

import regex as re

from .kernels import FILELESS_RULES
from .table import Table

# Watched library -> the names its version is pinned under in vLLM: pip
# distributions, Dockerfile ARGs and versions.json keys, cmake variables.
# Compared case-insensitively, with `-`, `_` and `.` alike, as pip does.
# The keys are the recorder's `_LIBS`, a list it cannot import from here.
# Torch is absent on purpose: a torch bump runs everything.
# Update when: vLLM pins one of these under a new name, or a library joins.
# Guard: tests/coverage/test_libraries.py pins the keys to the recorder's.
LIBRARY_PINS: dict[str, tuple[str, ...]] = {
    "deep_gemm": ("deep-gemm", "_DEEPGEMM_UPSTREAM_TAG"),
    "flash_attn": ("flash-attn",),
    "flashinfer": (
        "flashinfer-python",
        "flashinfer-cubin",
        "flashinfer-jit-cache",
        "FLASHINFER_VERSION",
    ),
    "triton": ("triton",),
}

# A version or a commit, the only token a bump may change.
_VERSION = re.compile(r"(?<![\w.+-])\d[\w.+!-]*|\b[0-9a-f]{7,40}\b")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")


def _norm(text: str) -> str:
    return re.sub(r"[_.]", "-", text.lower())


def _named(text: str) -> set[str]:
    """Libraries whose pin `text` names, as a whole token."""
    low = _norm(text)
    return {
        lib
        for lib, spellings in LIBRARY_PINS.items()
        for s in spellings
        if re.search(rf"(?<![a-z0-9-]){re.escape(_norm(s))}(?![a-z0-9-])", low)
    }


def _template(line: str) -> str:
    return _VERSION.sub("<v>", line)


def _pair(old: str, new: str) -> set[str] | None:
    """The libraries this one-line change bumps, or None if it is anything
    more than a version moving on a line that names a pin."""
    named = _named(old)
    if not named or _named(new) != named or _template(old) != _template(new):
        return None
    return named


def _diff_bumps(diff: str) -> set[str] | None:
    """Every hunk trades lines one for one, and each trade is a pin bump."""
    libs: set[str] = set()
    old: list[str] = []
    new: list[str] = []

    def flush() -> bool:
        if len(old) != len(new):
            return False
        for a, b in zip(old, new):
            bumped = _pair(a, b)
            if bumped is None:
                return False
            libs.update(bumped)
        old.clear()
        new.clear()
        return True

    in_hunk = False
    for line in diff.splitlines():
        if _HUNK.match(line):
            if not flush():
                return None
            in_hunk = True
        elif not in_hunk:
            continue  # the file header, whose `---` is not a removed line
        elif line.startswith("-"):
            old.append(line[1:])
        elif line.startswith("+"):
            new.append(line[1:])
    return libs if flush() and libs else None


def _json_bumps(old, new, path: tuple[str, ...] = ()) -> set[str] | None:
    """versions.json keeps the name one line above the value, so compare the
    parsed trees: every differing leaf is a version under a key naming a pin."""
    if isinstance(old, dict) and isinstance(new, dict):
        if old.keys() != new.keys():
            return None
        libs: set[str] = set()
        for key in old:
            got = _json_bumps(old[key], new[key], (*path, key))
            if got is None:
                return None
            libs |= got
        return libs
    if old == new:
        return set()
    if not isinstance(old, str) or not isinstance(new, str):
        return None
    named = _named(" ".join(path))
    return named if named and _template(old) == _template(new) else None


def _show(repo: Path, ref: str | None, path: str) -> str | None:
    """The file at `ref`, or in the worktree when there is no ref."""
    if ref is None:
        try:
            return (repo / path).read_text()
        except OSError:
            return None
    out = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
    )
    return out.stdout if out.returncode == 0 else None


def bumps(repo: Path, base: str, head: str | None, files) -> dict[str, frozenset[str]]:
    """Changed file -> the watched libraries it bumps, for each file whose
    whole change is moving their pinned versions. Any other file is absent,
    and so is one added, deleted or renamed."""
    out: dict[str, frozenset[str]] = {}
    for f in files:
        if f.status not in ("M", "T"):
            continue
        libs: set[str] | None = None
        if f.path.endswith(".json"):
            old, new = _show(repo, base, f.path), _show(repo, head, f.path)
            try:
                if old is not None and new is not None:
                    libs = _json_bumps(json.loads(old), json.loads(new)) or None
            except ValueError:
                libs = None
        else:
            args = ["git", "-C", str(repo), "diff", "-U0", "--no-color"]
            args += ["--no-ext-diff", "--no-textconv", base]
            proc = subprocess.run(
                [*args, *([head] if head else []), "--", f.path],
                capture_output=True,
                text=True,
            )
            if proc.returncode == 0:
                libs = _diff_bumps(proc.stdout)
        if libs:
            out[f.path] = frozenset(libs)
    return out


def read_pr(
    table: Table,
    selection,
    bumped: dict[str, frozenset[str]],
    keys,
    protected: frozenset[str] = frozenset(),
) -> tuple[list[str], Counter]:
    """(steps to drop, why each weighed step went the way it did).

    Weighs only the steps every changed file behind which is a bump, read off
    `selected_by_file` as the kernel record does: the image and requirements
    rules pick them non-droppably, since a function row says nothing about an
    image, and a row of library calls is the evidence that can.
    """
    dropped: list[str] = []
    reasons: Counter = Counter()
    by_step: dict[str, set[str]] = defaultdict(set)
    for path, steps in selection.selected_by_file.items():
        for s in steps:
            by_step[s].add(path)
    for step_id in selection.selected:
        files = by_step.get(step_id, set())
        if not files or not files <= bumped.keys():
            continue  # another file selected it; that is another rule's answer
        if any(r in FILELESS_RULES for r in selection.selected_rules.get(step_id, ())):
            reasons["selected-without-a-file"] += 1
            continue
        libs = frozenset().union(*(bumped[p] for p in files))
        key = keys.key_for(step_id)
        if key is None:
            reasons["unmappable-step-id"] += 1
            continue
        if step_id in protected:
            reasons["held-by-the-python-record"] += 1
            continue
        unusable = table.unusable(key)
        if unusable is not None:
            reasons[unusable.evidence.value] += 1
            continue
        calls = table.row(key).stamp.libcalls
        if not libs <= calls.keys():
            # Recorded before the recorder watched the library.
            reasons["row-predates-library-recording"] += 1
        elif any(calls[lib] for lib in libs):
            reasons["row-calls-the-bumped-library"] += 1
        else:
            reasons["row-never-calls-the-bumped-library"] += 1
            dropped.append(step_id)
    return dropped, reasons
