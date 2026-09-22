# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Combine the two inputs into one job list.

Per changed file F and step S:

    a USABLE row shows S ran F     -> select. The map gets no vote.
    a USABLE row shows S ran none  -> drop, if every gate agrees.
    no usable row                  -> the map decides.

Usable, not merely present. A row that is stale, thin, digest-failed, from a job
that did not pass, or recorded by another Python minor is not evidence of
absence, so it takes the third line. Expect that line to be the common case.

Those three describe authority, not order. The map proposes the candidate set
first and the record adjusts it both ways. Evidence cannot originate a set,
being silent about new code and untraced trees, and silence is not "not needed".

Per FILE, not per diff. The recorder watches some trees and is blind to others,
so a diff touching both gets the record's answer for one and the map's for the
other.

Selecting takes one observation and carries no gate. Dropping needs the
recording to still describe the step it came from, so it carries the freshness
gate and every health check in `table.look_up`.

A second record answers for csrc, where no Python frame exists: the kernel
record (`coverage/kernels.py`), a table of the GPU kernels each step launched
joined to the file each kernel was compiled from. Same three lines, same
authority: it selects on one observation and drops only behind every gate,
and it runs after the Python record, never overruling a step that record kept
on an observed call.

WHEN ANYTHING GOES WRONG, THE MAP'S SELECTION STANDS UNCHANGED -- and note the
shape of that. It is NOT "carry on with an empty stale set": an empty stale set
means nothing is disqualified, so every step stays droppable, and the failure
would land on the one side that can lose a test. The only safe degradation is
to skip dropping entirely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .coverage import freshness
from .coverage.phase import DEFAULT_MODE, PhaseMode, mode_from_env
from .coverage.kernels import KernelEvidence
from .coverage.rules import RowKeys, newest_commit, read_pr, unknown_names
from .coverage.source import fetch_kernel_evidence, fetch_table
from .coverage.table import Table

#: Set to re-enable the freshness gate, which is off by default. Governs both
#: records: for the kernel record it means a csrc file whose source moved
#: between the map's commit and the PR's base may select but not drop.
FRESHNESS_ENV = "CI_SELECTOR_FRESHNESS"
#: Set to let the kernel record DROP when its table and map were recorded at
#: different commits. Off, such a pair only selects: a kernel renamed between
#: the two reads as never launched. The collect step publishes matched pairs,
#: so this is for measuring against older data.
KERNEL_UNMATCHED_ENV = "CI_SELECTOR_KERNEL_UNMATCHED_DROPS"


@dataclass
class Decision:
    """What each input contributed, kept apart so the answer can be explained."""

    steps: set[str] = field(default_factory=set)
    from_map: set[str] = field(default_factory=set)
    added_by_coverage: set[str] = field(default_factory=set)
    dropped_by_coverage: set[str] = field(default_factory=set)
    # Why the record could not help, when it could not.
    coverage_note: str = ""
    # Steps whose row no longer describes them, so it cannot authorise a drop.
    stale_steps: int = 0
    # Rows nothing in this checkout can address, and the size of the table they
    # sit in. They already behave as `no row`.
    unreadable_rows: int = 0
    rows: int = 0
    reasons: dict = field(default_factory=dict)
    # Steps the Python record kept on positive evidence. The kernel record
    # may not drop these.
    executes_by_coverage: set[str] = field(default_factory=set)
    # The kernel record's contribution, kept apart the same way.
    added_by_kernels: set[str] = field(default_factory=set)
    dropped_by_kernels: set[str] = field(default_factory=set)
    kernel_note: str = ""
    # Which table and map answered, and whether they match.
    kernel_pair: str = ""
    kernel_reasons: dict = field(default_factory=dict)
    # changed csrc file -> what the kernel record could do with it
    kernel_files: dict = field(default_factory=dict)

    @property
    def used_coverage(self) -> bool:
        return not self.coverage_note

    @property
    def used_kernels(self) -> bool:
        return not self.kernel_note


def decide(
    state,
    selection,
    repo: Path,
    base: str | None,
    head: str | None,
    *,
    table: Table | None = None,
    mode: PhaseMode | None = None,
    kernels: KernelEvidence | None = None,
) -> Decision:
    """Apply both records to the map's selection.

    When a record cannot be used, for any reason at all, it changes nothing
    and the reason lands in `coverage_note` or `kernel_note`.
    """
    # Resolved above the try on purpose. A bad env value has to kill the run:
    # below, the broad handler would swallow it, every PR would come back
    # map-only, and that reads as "the mode does nothing".
    mode = mode if mode is not None else mode_from_env()
    out = Decision(steps=set(selection.selected), from_map=set(selection.selected))

    table = table if table is not None else fetch_table()
    if not table.available:
        out.coverage_note = table.unavailable
    else:
        out.rows = len(table)
        # Only askable with a built state. Every live caller has one; the tests
        # that pass None are exercising the rules rather than the tally.
        if getattr(state, "pipelines", None):
            out.unreadable_rows = _unreadable_rows(state, table)
        try:
            _apply_record(out, table, selection, repo, base, head, mode)
        except Exception as exc:  # noqa: BLE001 - see the module docstring
            # Broad on purpose. A narrower handler would have to decide what
            # to do with a half-built reading, and the only safe answer is
            # nothing.
            out.steps = set(out.from_map)
            out.added_by_coverage.clear()
            out.dropped_by_coverage.clear()
            out.executes_by_coverage.clear()
            out.coverage_note = f"coverage unusable ({type(exc).__name__}: {exc})"

    kernels = kernels if kernels is not None else fetch_kernel_evidence()
    if kernels.unavailable:
        out.kernel_note = kernels.unavailable
    else:
        out.kernel_pair = kernels.describe()
        try:
            _apply_kernel_record(out, kernels, selection, state, repo, base, head)
        except Exception as exc:  # noqa: BLE001 - same reasoning as above
            out.steps = (
                set(out.from_map) | out.added_by_coverage
            ) - out.dropped_by_coverage
            out.added_by_kernels.clear()
            out.dropped_by_kernels.clear()
            out.kernel_note = f"kernel record unusable ({type(exc).__name__}: {exc})"
    return out


def _unreadable_rows(state, table: Table) -> int:
    """Rows filed under a key no step in this checkout reconstructs.

    A row is filed under the identity Buildkite published, and a step reaches it
    by rebuilding that identity from the yaml. Move a label and the row is never
    read again: no error, no reason code, no change in any count, because an
    unreachable row and a step that was never recorded are the same `NO_ROW`.
    Selection is unaffected either way, which is exactly why this needs saying
    out loud -- the record shrinks and the answer still looks healthy.

    `test_every_recorded_row_is_readable_by_some_step` asks this at the table's
    OWN commit, deliberately, since spelling a row recorded elsewhere is a
    different question. Nothing asked it of the tree being selected against.
    """
    readable = {
        step.buildkite_key or step.label
        for pipeline in state.pipelines
        for step in pipeline.steps
    }
    return len(set(table._rows) - readable)


def _steps_at(repo: Path, ref: str) -> tuple[frozenset[str], frozenset[tuple]]:
    """Every step the pipeline yaml declares at `ref`, spelled both ways.

    Both, because neither alone answers "is this step here". `step_id` falls
    back to the label, so a reword makes one step look like two; `identity` is
    rename-tolerant but says nothing about a step that was genuinely renamed
    AND moved. `restrict_to` keeps a candidate matching either.

    Deliberately not `state_for`: that builds an import graph and costs ~17s,
    and the state cache holds two entries, both already spoken for here. This
    parses yaml and nothing else, and the identities are free once it has.
    """
    from .codemap.pipeline.buildkite import load_pipeline_configs, load_steps
    from .codemap.worktree import worktree_at

    tree = worktree_at(repo, ref)
    try:
        configs = load_pipeline_configs(tree)
    except FileNotFoundError:
        # No pipeline at that ref is not an answer about which steps exist,
        # only that we could not look. Empty means "do not restrict", so the
        # add side keeps working instead of silently switching itself off.
        return frozenset(), frozenset()
    steps = [step for config in configs for step in load_steps(tree, config)]
    return frozenset(s.step_id for s in steps), frozenset(s.identity for s in steps)


def _apply_record(
    out: Decision,
    table: Table,
    selection,
    repo: Path,
    base: str,
    head: str | None,
    mode: PhaseMode = DEFAULT_MODE,
) -> None:
    from .codemap.worktree import state_for
    from .coverage.changed_funcs import build as build_query
    from .coverage.changed_funcs import mark_unfaithful

    query = mark_unfaithful(build_query(repo, base, head), table.unfaithful_paths)

    recorded_at = newest_commit(table, repo)
    keys = RowKeys.resolve(table, repo, recorded_at)
    keys.restrict_to(*_steps_at(repo, base))

    stale: frozenset[str] = frozenset()
    if os.environ.get(FRESHNESS_ENV):
        # `RowKeys.resolve` already built and cached this state, so this is
        # cheap.
        surfaces = freshness.build(state_for(repo, recorded_at), recorded_at)
        moved = freshness.changed_between(repo, recorded_at, base)
        stale = frozenset(surfaces.stale_steps(selection.selected, moved))

    union: dict[str, set[str]] = {}
    for row in table._rows.values():
        for path, names in row.functions.items():
            union.setdefault(path, set()).update(names)
    union_names = {p: frozenset(n) for p, n in union.items()}

    _append_op_proxies(query, repo, base, union_names, table)

    reading = read_pr(
        table,
        selection,
        query,
        unknown_names(query, union_names, {}),
        frozenset(union_names),
        keys,
        stale,
        mode=mode,
    )
    out.stale_steps = len(stale)
    out.reasons = dict(reading.reasons)
    out.added_by_coverage = set(reading.added)
    out.dropped_by_coverage = set(reading.dropped)
    out.executes_by_coverage = set(reading.executes)
    out.steps |= out.added_by_coverage
    out.steps -= out.dropped_by_coverage


def _append_op_proxies(query, repo: Path, base: str, union_names, table) -> None:
    """Stand-in queries for changed csrc files: the wrapper names the drop
    side weighs instead of the path, which is never recorded.

    A derived name the record has never seen marks the whole stand-in FAILED,
    which keeps the step. Reading it as "never ran" would be a wrong drop, and
    the per-file name gate cannot catch it when the file itself is recorded.
    """
    from .codemap import native_ops as native_ops_mod
    from .codemap.worktree import state_for
    from .coverage.changed_funcs import Attribution, FileQuery

    if native_ops_mod.mode() != "on":
        return
    if not any(f.path.startswith("csrc/") for f in query.files):
        return
    no = getattr(state_for(repo, base), "native_ops", None)
    if no is None or no.error:
        return
    proxies: dict[str, set[str]] = {}
    for f in query.files:
        if f.proxy or not no.owns(f.path):
            continue
        for wf, quals in (no.proxies_for(f.path) or {}).items():
            proxies.setdefault(wf, set()).update(quals)
    for wf in sorted(proxies):
        quals = frozenset(proxies[wf])
        missing = quals - union_names.get(wf, frozenset())
        query.files.append(
            FileQuery(
                path=wf,
                status=Attribution.FAILED if missing else Attribution.ATTRIBUTED,
                head_names=quals,
                unfaithful=wf in table.unfaithful_paths,
                proxy=True,
                note=(
                    f"op-wrapper proxy; unrecorded names: {sorted(missing)[:3]}"
                    if missing
                    else "op-wrapper proxy"
                ),
            )
        )


def _apply_kernel_record(
    out: Decision,
    kernels: KernelEvidence,
    selection,
    state,
    repo: Path,
    base: str,
    head: str | None,
) -> None:
    """The kernel record over the map's selection, after the Python record.

    Keys resolve at the PR's base from the state `decide` was handed, since
    the table's rows carry the generator's step keys and the emitter names
    steps from the base. Raises rather than guessing when there is no state
    to spell them with; `decide` turns that into a note.
    """
    from .codemap.classify import csrc_held_steps
    from .coverage import kernels as kernel_rules
    from .gitdiff import changed_paths, diff_files

    if not getattr(state, "pipelines", None):
        raise RuntimeError("no pipeline state to resolve step keys against")
    keys = RowKeys.resolve_from_state(set(kernels.table._rows), state)
    changed = changed_paths(diff_files(repo, base, head))

    stale: frozenset[str] = frozenset()
    if os.environ.get(FRESHNESS_ENV):
        stale = _csrc_moved_since(repo, kernels.symbol_map.commit, base, changed)

    allow_drops = kernels.matched or bool(os.environ.get(KERNEL_UNMATCHED_ENV))
    reading = kernel_rules.read_pr(
        kernels,
        selection,
        changed,
        keys,
        held=lambda path: csrc_held_steps(state, path),
        stale=stale,
        allow_drops=allow_drops,
        protected=frozenset(out.executes_by_coverage),
    )
    out.kernel_reasons = dict(reading.reasons)
    out.kernel_files = dict(reading.files)
    out.added_by_kernels = set(reading.added)
    out.dropped_by_kernels = set(reading.dropped)
    out.steps |= out.added_by_kernels
    out.steps -= out.dropped_by_kernels


def _csrc_moved_since(
    repo: Path, map_commit: str, base: str, changed
) -> frozenset[str]:
    """Changed files whose source differs between the map's commit and the
    base, so the map may describe kernels that are no longer there, or miss
    ones that are. A commit the repo does not have makes every changed file
    stale: unknown freshness is not freshness."""
    import subprocess

    if not map_commit:
        return frozenset(changed)
    out = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-only", map_commit, base, "--", "csrc"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        return frozenset(changed)
    moved = set(out.stdout.split())
    return frozenset(p for p in changed if p in moved)
