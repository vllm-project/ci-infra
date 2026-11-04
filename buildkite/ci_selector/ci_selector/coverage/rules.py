# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

"""The rule the coverage record applies to one PR. Two directions, gated
differently.

ADD: a row showing a step ran a changed file selects it, whatever the code map
concluded. Ungated, because one observation proves presence.

DROP: a row showing a step ran none of them removes it, but only once every
gate below agrees. Proving ABSENCE needs the recorder to have been watching the
right file, during a run that finished, on code that still exists. Every check
resolves toward keeping.
"""

from __future__ import annotations

import os
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..handwritten import TESTS_SCOPE
from .changed_funcs import Query
from .phase import DEFAULT_MODE, PhaseMode, ran_changed_lines, row_shows_use
from .table import Table

# Lets a row add an optional step it shows executing the change; see
# RowKeys.candidates.
RECORD_OPTIONAL_ENV = "CI_SELECTOR_RECORD_OPTIONAL"


def row_key_for(step_id: str) -> str | None:
    """The bare row key inside a step id, or None. Strips the pipeline prefix
    and nothing else, so a mirror keeps its hardware suffix and matches no row.
    A bare key is unique only WITHIN a pipeline, so this alone will hand a step
    another pipeline's row. Use `RowKeys`."""
    pipeline, sep, rest = step_id.partition(":")
    return rest if sep and rest else None


def newest_commit(table: Table, repo: Path) -> str:
    """The checkout to resolve row keys against: the latest commit any row saw.

    Picked by history rather than sort order, which agreed only by luck.
    Production reads one sweep, so one commit, and takes the early return with
    no subprocess; the ordering is for multi-build tables we measure with.
    """
    commits = {c for row in table._rows.values() for c in row.stamp.commits}
    if not commits:
        return "HEAD"
    if len(commits) == 1:
        return next(iter(commits))
    ordered = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--date-order", "--no-walk", *commits],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return ordered[0] if ordered else sorted(commits)[0]


class RowKeys:
    """Which steps a table is entitled to answer for.

    Recordings carry no pipeline and a bare key is not unique across configs,
    so the owner is derived: whichever config's keys explain the most rows
    wins, and steps from any other get no row. Nothing is curated, so a sweep
    of a different pipeline resolves itself.

    Identity comes from the resolved step, not its printed id, since the
    generator spells a mirror the other way round from us.

    TODO: only `vllm_ci` is emitted today, so shared keys never collide. A
    second config on a PR would need a real tie-break.
    """

    def __init__(
        self,
        owners: set[str],
        match_rate: dict[str, float],
        spellings: dict[str, str] | None = None,
        steps: dict[str, object] | None = None,
    ):
        self.owners = owners
        self.match_rate = match_rate
        self.spellings = spellings
        # step_id -> Step, for the owning pipelines only. The additive half
        # needs the step objects to ask `match_jobs` whether an added step
        # covers a failure, and a raw key lookup would miss every sharded job.
        self.steps = steps or {}
        # Filled by `restrict_to`; read into the reason histogram so a lost
        # add candidate is visible rather than merely absent.
        self.restricted_away: list[str] = []

    @classmethod
    def resolve(cls, table: Table, repo: Path, ref: str) -> RowKeys:
        from ci_selector.codemap.worktree import state_for

        return cls.resolve_from_state(set(table._rows), state_for(repo, ref))

    @classmethod
    def resolve_from_state(cls, rows: set[str], state) -> RowKeys:
        """`resolve` against a state the caller already holds.

        The kernel record resolves at the PR's base, whose state `decide`
        was handed, so this costs nothing where `resolve` builds a worktree.
        `rows` is the table's row keys, whichever table.
        """
        spellings: dict[str, str] = {}
        match_rate: dict[str, float] = {}
        for pipeline in state.pipelines:
            idents = set()
            for step in pipeline.steps:
                ident = step.buildkite_key or step.label
                spellings[step.step_id] = ident
                idents.add(ident)
            match_rate[pipeline.config.name] = (
                len(rows & idents) / len(rows) if rows else 0.0
            )
        resolved = cls.from_match_rates(match_rate, spellings)
        resolved.steps = {
            step.step_id: step
            for pipeline in state.pipelines
            if pipeline.config.name in resolved.owners
            for step in pipeline.steps
        }
        return resolved

    @classmethod
    def from_match_rates(
        cls, match_rate: dict[str, float], spellings: dict[str, str] | None = None
    ) -> RowKeys:
        """Pick the owner, separated from the state building so it can be
        tested. No match rates at all means no owner, so no step resolves to a
        row and the table changes nothing."""
        ranked = sorted(match_rate.items(), key=lambda kv: -kv[1])
        owners = {ranked[0][0]} if ranked else set()
        return cls(owners, match_rate, spellings)

    def candidates(self) -> list[str]:
        """Every step the table may ADD, which is not every step it can answer
        for.

        The manual-only filter is doing real work here, not tidying: the sweeps
        unblocked the optional steps so the table would cover them, so it holds
        rows for nightlies and mirrors CI never runs on a PR. Picking one is
        not over-selection, it names a step the generator will not emit.

        The subtractive direction needs no such filter, its population being
        the map's selection, which holds no manual-only step.
        """
        # CI_SELECTOR_RECORD_OPTIONAL=1 lifts the filter, an experiment and not
        # a default: most confirmed selection leaks are optional evals that ran
        # the changed code on main and never on the PR, and only a row can say
        # so. The emitter would have to name them for them to run.
        optional_ok = os.environ.get(RECORD_OPTIONAL_ENV) == "1"
        return [
            sid
            for sid, step in self.steps.items()
            # Defaults to manual_only=True: this filter is a safety gate, so an
            # object that cannot answer the question is not addable.
            if self.key_for(sid) is not None
            and (optional_ok or not getattr(step, "manual_only", True))
        ]

    def restrict_to(
        self,
        step_ids: frozenset[str],
        identities: frozenset[tuple] = frozenset(),
    ) -> None:
        """Drop addable steps that do not exist at the PR's base.

        Two clocks: these ids come from the TABLE's commit while the emitter
        names steps from the base. Naming a step that is not there makes the
        emitter omit the variable, and that runs everything.

        A step survives if its `step_id` OR its rename-tolerant `Step.identity`
        is at base. Id alone was the defect, since it falls back to the label
        and a reword gives one step two ids.

        An empty id set means we could not read the base pipeline, which is not
        the same as "no step is there", so it changes nothing.
        """
        if not step_ids:
            return
        kept = {
            k: v
            for k, v in self.steps.items()
            if k in step_ids or getattr(v, "identity", None) in identities
        }
        # Counted, not just dropped. This stage used to lose candidates with no
        # trace in the reason histogram, which is how 118 went unnoticed.
        self.restricted_away = sorted(set(self.steps) - set(kept))
        self.steps = kept

    def key_for(self, step_id: str) -> str | None:
        """The row this step may consult, or None when it may consult none."""
        pipeline, sep, rest = step_id.partition(":")
        if not sep or not rest or pipeline not in self.owners:
            return None
        if self.spellings is None:
            return rest
        return self.spellings.get(step_id)


@dataclass
class Reading:
    """One reading of the record over one PR."""

    dropped: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    reasons: Counter = field(default_factory=Counter)
    # Kept steps this record clears once the csrc files it defers to the
    # kernel record are set aside (`deferred`). Read by that record: it may
    # drop one of these when it clears those files itself.
    cleared: list[str] = field(default_factory=list)
    # Steps the record removed whose CI job actually failed. The number that matters
    # more than any ratio in this file.
    dropped_and_failed: list[str] = field(default_factory=list)
    # Steps the RECORD selected that the map did not: a row shows the step
    # executing a changed file. Addition needs one observation where removal
    # needs the recording to be complete, so this half carries no gate.
    added: list[str] = field(default_factory=list)
    # Additions whose CI job actually failed: the recall this half buys, and
    # the mirror of dropped_and_failed.
    added_and_failed: list[str] = field(default_factory=list)
    # Steps kept because a row shows them running a changed function. Read by
    # the kernel record: a silence about kernels cannot overrule an observed
    # call on another changed file.
    executes: list[str] = field(default_factory=list)
    # The subset of `executes` whose only evidence is an op-wrapper stand-in
    # for a changed csrc file. That is the same file the kernel record answers
    # for, not another one, and a coarser answer: the stand-in covers every op
    # the file binds, and one of them, a torch.compile pass that names dozens
    # of ops, runs in every compiling step (61 steps held on vllm#55755).
    executes_by_proxy: list[str] = field(default_factory=list)


def unknown_names(
    query: Query,
    union_names: dict[str, frozenset[str]],
    fresh: dict[str, frozenset[str]],
) -> dict[str, set[str]]:
    """Changed names the record has no business reasoning about, by file. Two
    sources, which stack rather than compete.

    A name no row has ever recorded is unknown outright: missing from one
    step's row is evidence about that step, missing from every row is no data.
    That is a rule of the design, not an artifact of our table.

    A name that did not exist at the PR's base is unknown too, but only our
    table hides that, having been recorded after those PRs merged. That half is
    measurement bias, so pass an empty dict to leave it out.
    """
    out: dict[str, set[str]] = defaultdict(set)
    for changed in query.files:
        missing = changed.names - union_names.get(changed.path, frozenset())
        if missing:
            out[changed.path] |= missing
    for path, names in fresh.items():
        out[path] |= set(names)
    return dict(out)


def _foreign(
    query: Query, step, row=None, mockers: dict[str, frozenset[str]] | None = None
) -> set[str]:
    """Changed files whose every changed line only other hardware families'
    jobs can reach, through `current_platform.is_<x>()` guards: this step
    cannot run them, whatever its row says about the functions around them.
    vllm#54874 changed only the XPU branch of get_accelerator_view_from_cpu_tensor,
    which 315 CUDA rows call, and all of them came along.

    A test can patch the platform and run another family's branch on this
    one: vllm#51274's test sets `is_rocm` true on a CUDA job. So a step whose
    row ran the changed file and one of the tests `mockers` lists for it
    keeps it. A step on no known family keeps everything."""
    if step is None:
        return set()
    from ..codemap.hardware import family_of_device, step_in_family

    if family_of_device(getattr(step, "device", None)) is None and not getattr(
        step, "mirror_hw", None
    ):
        return set()

    def patched_here(f) -> bool:
        return (
            row is not None
            and f.path in row.functions
            and any(t in row.functions for t in (mockers or {}).get(f.path, ()))
        )

    return {
        f.path
        for f in query.files
        if getattr(f, "platform", None)
        and not any(step_in_family(step, fam) for fam in f.platform)
        and not patched_here(f)
    }


def read_pr(
    table: Table,
    selection,
    query: Query,
    unresolved: dict[str, set[str]],
    known_files: frozenset[str],
    keys: RowKeys,
    stale: frozenset[str] = frozenset(),
    *,
    mode: PhaseMode = DEFAULT_MODE,
    failed_ran: dict[str, str] | None = None,
    matched_slugs: dict[str, list[str]] | None = None,
    failed_missed: dict[str, str] | None = None,
    reached_via: dict[str, frozenset[str]] | None = None,
    platform_mockers: dict[str, frozenset[str]] | None = None,
    deferred: frozenset[str] = frozenset(),
) -> Reading:
    """The record over one PR's map selection.

    Takes a `Selection` so it can run at decide time. It used to take a
    crosscheck record, which confined it to the harness, because half of that
    is CI outcomes that do not exist yet while a PR is being selected for.
    Those survive as keyword arguments the harness passes and decide time does
    not.

    The query and the unknown-name set come from the caller, since both cost a
    git read per file and both want the same answer. So does `reached_via`: a
    new file the record has never seen, mapped to the known files a step must
    import to run it.

    `deferred` are changed csrc files the kernel record answers for. Steps
    this reading keeps get a second look with those set aside
    (`_clear_deferred`); the ones it clears land in `cleared`, still kept.
    """
    reading = Reading()
    via = reached_via or {}
    failed = set(failed_ran or {})
    matched = matched_slugs or {}
    # Absent means the step is not droppable, not droppable against
    # everything. The old fallback claimed to be the more careful reading,
    # which was true of the scope and false of the authority it granted.
    attribution = selection.selected_paths or {}
    proxy_paths = frozenset(f.path for f in query.files if f.proxy)

    for step_id in selection.selected:
        key = keys.key_for(step_id)
        if key is None:
            reading.kept.append(step_id)
            reading.reasons["unmappable-step-id"] += 1
            continue

        # THE INVARIANT: scoping may narrow what you examine, never who may be
        # doubted. So positive evidence is read against the WHOLE diff, before
        # any narrowing. A row that plainly runs a changed function makes its
        # step relevant whatever path the map happened to cite.
        #
        # What counts as "runs" is `mode`. Under OFF an import counts as use: a
        # step that imports a file it never otherwise touches can still break
        # when that file changes. The stricter modes require a call frame.
        # `look_up` re-makes this exact match further down, so both read the
        # same predicate or neither moves.
        row = table.row(key)
        foreign = _foreign(query, keys.steps.get(step_id), row, platform_mockers)
        if row is not None:
            direct = any(
                row_shows_use(row, f, name, mode)
                for f in query.files
                if not f.proxy and f.path not in foreign
                for name in f.names
            )
            via_proxy = not direct and any(
                row_shows_use(row, f, name, mode)
                for f in query.files
                if f.proxy
                for name in f.names
            )
        else:
            direct = via_proxy = False
        if direct or via_proxy:
            reading.kept.append(step_id)
            reading.executes.append(step_id)
            if via_proxy:
                reading.executes_by_proxy.append(step_id)
            reading.reasons["row-executes-a-changed-function"] += 1
            continue

        # The row describes the step as it was at the table's commit. If the
        # step's tests or its definition moved since, it describes a step that
        # no longer exists. Below the positive check because both outcomes keep,
        # so the order only decides which counter gets the credit.
        if step_id in stale:
            reading.kept.append(step_id)
            reading.reasons["row-is-stale"] += 1
            continue

        # The other half of the same invariant. A step narrows only when EVERY
        # reason the map had for it is one that function evidence can overturn.
        # A single reason that cannot be holds the step on its own.
        reasons = attribution.get(step_id)
        if reasons is None:
            # No attribution is not "argue with the whole diff". That confuses
            # a scope decision with an authority one, and on an input missing
            # the key it made every step droppable, forced ones included.
            reading.kept.append(step_id)
            reading.reasons["no-attribution"] += 1
            continue
        if any(r is None for r in reasons):
            reading.kept.append(step_id)
            reading.reasons["not droppable-reason"] += 1
            continue
        scope = {p for r in reasons for p in r}
        if not scope:
            reading.kept.append(step_id)
            reading.reasons["no-attributed-file"] += 1
            continue
        verdict, why = _weigh(
            table, query, key, scope, foreign, unresolved, known_files, via, mode
        )
        # Counted so a stand-in that never fires cannot pass for one that
        # fired and found nothing. The two early drops never consult one.
        consulted = bool(proxy_paths & (scope - foreign)) and why not in _EARLY_DROPS
        if consulted:
            reading.reasons["proxy-evidence-consulted"] += 1
        reading.reasons[why] += 1
        if verdict:
            reading.dropped.append(step_id)
            if consulted:
                reading.reasons["proxy-evidence-drop"] += 1
            if set(matched.get(step_id, ())) & failed:
                reading.dropped_and_failed.append(step_id)
        else:
            reading.kept.append(step_id)

    if deferred:
        _clear_deferred(
            table,
            selection,
            query,
            keys,
            reading,
            unresolved,
            known_files,
            via,
            mode,
            stale,
            deferred,
            platform_mockers,
        )

    _add_from_rows(
        table,
        selection,
        query,
        keys,
        reading,
        unresolved,
        failed_missed,
        platform_mockers,
    )
    return reading


#: `_weigh`'s verdicts that need no evidence from a row.
_EARLY_DROPS = frozenset(
    {"only-behaviour-preserving-changes", "changed-code-runs-on-another-platform"}
)


def _weigh(
    table: Table,
    query: Query,
    key: str,
    scope: set[str],
    foreign: frozenset[str] | set[str],
    unresolved: dict[str, set[str]],
    known_files: frozenset[str],
    via: dict[str, frozenset[str]],
    mode: PhaseMode,
) -> tuple[bool, str]:
    """Whether the record may drop a step selected for exactly `scope`, and
    the reason counter for it. The gates after a step's reasons are known."""
    # Every file the map picked this step for changed without changing
    # behaviour: line endings, or annotations and layout the code does not
    # see. There is nothing for the step to test.
    preserving = set(query.inert) | set(query.eol_only)
    if preserving and scope <= preserving:
        return True, "only-behaviour-preserving-changes"
    if foreign and scope <= preserving | foreign:
        # What is left runs only on another family's hardware.
        return True, "changed-code-runs-on-another-platform"
    scope = scope - foreign

    scoped = query.restrict(scope)
    # The unknown-name gates, per step and not per PR. Whole-diff was the
    # same mistake as the fail-open: an unknown name in a file unrelated to
    # this step's selection kept every step on the PR.
    local = {p: n for p, n in unresolved.items() if p in scope}
    unseen = [p for p in local if p not in known_files and p not in via]
    if unseen:
        return False, "unknown-code-blocks-narrowing"

    # A row recorded before the recorder wrote tests/ holds no tests/ name
    # at all, and its silence about one is no evidence. Tables merge rows
    # across builds, so old and new rows can sit side by side. The same
    # recorder change started writing `#pkg` lines, so a row with no
    # packages is an old row. Having no tests/ path is not the sign: a
    # step that runs only scripts (examples, lm-eval) enters none.
    row = table.row(key)
    if (
        row is not None
        and any(
            f.path in scope and f.path.startswith(TESTS_SCOPE) and f.in_recorder_scope
            for f in query.files
        )
        and not row.stamp.packages
    ):
        return False, "row-predates-tests-recording"

    evidence = table.look_up(key, scoped, mode).evidence
    if evidence.authorizes_drop and local:
        # The file projection the design specifies under an unknown
        # function: a step whose row touches a file carrying unknown code
        # keeps, because that code could be reached from what it does run.
        # A new file's code runs wherever the files reaching it are
        # imported, so those stand in for it.
        touched = set(local).union(*(via.get(p, ()) for p in local))
        if row is not None and any(p in row.functions for p in touched):
            return False, "file-projection-keeps"
    return evidence.authorizes_drop, evidence.value


def _clear_deferred(
    table: Table,
    selection,
    query: Query,
    keys: RowKeys,
    reading: Reading,
    unresolved: dict[str, set[str]],
    known_files: frozenset[str],
    via: dict[str, frozenset[str]],
    mode: PhaseMode,
    stale: frozenset[str],
    deferred: frozenset[str],
    platform_mockers: dict[str, frozenset[str]] | None,
) -> None:
    """A second reading of the steps the first kept, with the changed csrc
    files the kernel record answers for set aside: their reasons, and the
    op-wrapper stand-ins that answer for them here. A step cleared on what is
    left goes to `reading.cleared` and stays kept; the kernel record drops it
    if it also clears those files.

    Without this, a PR changing a kernel and a Python file both held every
    step: this record kept it for the csrc reason it cannot weigh (or for a
    stand-in covering every op the file binds), the kernel record for the
    Python file it cannot weigh. Neither answer changes here; the step only
    becomes the kernel record's to decide.

    Stand-ins are set aside only when every csrc file that can produce one is
    deferred, since they are not told apart by the file they stand in for.
    """
    direct_query = query
    if any(f.proxy for f in query.files):
        stand_in_sources = {
            f.path
            for f in query.files
            if not f.proxy
            and f.path.startswith("csrc/")
            and not f.path.startswith(("csrc/cpu/", "csrc/rocm/"))
        }
        if not stand_in_sources <= deferred:
            return
        direct_query = Query(
            base=query.base,
            head=query.head,
            files=[f for f in query.files if not f.proxy],
        )
    direct_executes = set(reading.executes) - set(reading.executes_by_proxy)
    attribution = selection.selected_paths or {}
    for step_id in reading.kept:
        if step_id in direct_executes or step_id in stale:
            continue
        key = keys.key_for(step_id)
        reasons = attribution.get(step_id)
        files = selection.selected_files.get(step_id) or []
        if key is None or reasons is None or len(files) != len(reasons):
            continue
        rest = [r for r, f in zip(reasons, files) if f not in deferred]
        if len(rest) == len(reasons) and not any(r is None for r in reasons):
            continue  # nothing deferred: the first reading stands
        if any(r is None for r in rest):
            continue
        scope = {p for r in rest for p in r}
        if scope:
            row = table.row(key)
            foreign = _foreign(query, keys.steps.get(step_id), row, platform_mockers)
            if row is not None and any(
                row_shows_use(row, f, name, mode)
                for f in direct_query.files
                if f.path not in foreign
                for name in f.names
            ):
                continue
            verdict, _ = _weigh(
                table,
                direct_query,
                key,
                scope,
                foreign,
                unresolved,
                known_files,
                via,
                mode,
            )
            if not verdict:
                continue
        reading.cleared.append(step_id)
        reading.reasons["cleared-pending-kernel-record"] += 1


def _add_from_rows(
    table: Table,
    selection,
    query: Query,
    keys: RowKeys,
    reading: Reading,
    unresolved: dict[str, set[str]],
    failed_missed: dict[str, str] | None = None,
    platform_mockers: dict[str, frozenset[str]] | None = None,
) -> None:
    """The direction the record is usually forgotten to have: it SELECTS, not
    only removes.

    A row showing a step ran a changed file proves relevance whatever the map
    thought, so this walks every step the table can answer for. No completeness,
    freshness or row gate at all: presence needs one observation where absence
    needs the whole recording, so even a thin or stale row adds. Safe, because
    adding only over-selects.

    The one gate judges the evidence rather than the row. `Table.discriminates`
    refuses a name nearly every row holds, which says only "the step imported
    the file" and would hand back most of a narrower map's saving.

    `unresolved` brackets the result: our table postdates the measured PRs, so
    it holds functions production could never have recorded.

    Failures match through the crosscheck's own matcher, since job slugs carry
    shard suffixes a raw key comparison misses.
    """
    already = set(selection.selected)
    for step_id in keys.candidates():
        if step_id in already:
            continue
        key = keys.key_for(step_id)
        row = table.row(key) if key else None
        if row is None:
            continue  # no row: the map decides, and the map did not pick it
        foreign = _foreign(query, keys.steps.get(step_id), row, platform_mockers)
        if any(
            f.path not in foreign
            and row.contains_call(f.path, name)
            and ran_changed_lines(row, f, name) is not False
            and table.discriminates(f.path, name)
            # Stand-ins are drop evidence only. A file outside the recorder
            # scope may still be in a row (tests/ is recorded before the
            # selector reads it), and adds nothing until the scope says so.
            for f in query.files
            if not f.proxy and f.in_recorder_scope
            for name in f.names - set(unresolved.get(f.path, ()))
        ):
            reading.added.append(step_id)

    # Counted here, above the failure scoring, so this is how often the half
    # fired at all. Below the early return it only counted adds on PRs that
    # also had an uncovered failure, which is almost none, so the histogram
    # read as though the half never ran.
    reading.reasons["row-adds-a-step-the-map-missed"] += len(reading.added)
    # Candidates `restrict_to` removed before this loop could see them. They
    # never enter `candidates()`, so without this they are invisible: not a
    # zero in the histogram, an absence from it.
    reading.reasons["candidate-absent-at-base"] += len(keys.restricted_away)

    missed = failed_missed or {}
    if not (missed and reading.added):
        return
    from ..codemap.pipeline.match import match_jobs

    steps = [keys.steps[s] for s in reading.added if s in keys.steps]
    _, _, by_step = match_jobs(dict(missed), steps)
    reading.added_and_failed = sorted(by_step)
