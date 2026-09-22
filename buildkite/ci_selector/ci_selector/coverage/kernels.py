# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""The kernel record: which GPU kernels each step launched, and which csrc
file each of those kernels was compiled from.

The Python record cannot see a kernel. A `.cu` change reaches the coverage
table only through the Python wrappers that dispatch to it, and everything
else about csrc falls to the map, which today routes a kernel file to every
step that runs the CUDA image. This is the second record, and it answers for
csrc directly.

Two files, produced together by a recording build (`kernrec/README.md`):

  kernel_table.json.gz       one row per step: the set of kernel names the
                             step's processes launched (CUPTI), plus whether
                             every job passed, every shard reported and no
                             records were dropped
  kernel_symbol_map.json.gz  per compiled object: its source file, the
                             headers it included, and the kernel entry
                             symbols it defines, read off the objects the
                             image build produced. One symbol per compiled
                             instantiation: a kernel template over dtype,
                             head size and layout is dozens of symbols, and
                             the recorder reports the one that launched.

Joined, they say for a changed file F which steps launched a kernel compiled
from F or from something that includes F. Per changed file F and step S:

    S's row launched a kernel from F              -> select. The map gets no vote.
    S's USABLE row launched none, F is CLEARABLE   -> drop, if F is all the map
                                                     had on S
    no row, or F is nothing the map can answer for -> the map decides

Usable is the row's health: every job passed, every shard reported, no
dropped records (`kernrec/kernel_table.py::usable`). Clearable is the file's:
the map compiled it into at least one object with kernels and into no object
without, so no host-only code depends on it. A header that `torch_bindings.cpp`
includes can change what every step does at import, and no kernel silence
can speak to that. Such a file still ADDS steps whose rows launched its
kernels; it never drops one.

Selecting needs one observation and no gate. Dropping needs the row healthy,
the file clearable, the step selected for nothing but files this record can
clear, and, when the table and the map were recorded at different commits,
an explicit opt-in: a kernel renamed between the two reads as never launched.

Any problem loading either file yields evidence that authorizes nothing, and
the map's selection stands. Same failure direction as `table.py`.
"""

from __future__ import annotations

import gzip
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .rules import RowKeys

#: `kernrec/kernel_table.py::TABLE_VERSION`. A drift test compares the two.
TABLE_VERSION = 2
#: What `tools/ci/kernel_symbol_map.py` in vLLM writes.
MAP_VERSION = 1

#: Selection rules that carry no changed file: image builds that always run,
#: preflight force-selects and a pipeline-wide run-all. A step holding one is
#: selected regardless of the diff, so nothing about a file can drop it.
FILELESS_RULES = frozenset({"preflight", "run-all", "always-run"})


@dataclass(frozen=True)
class KernelRow:
    key: str
    kernels: frozenset[str]
    passed: bool
    complete: bool
    dropped: int
    jobs: int = 0
    processes: int = 0

    @property
    def usable(self) -> bool:
        """The same three conditions as `kernrec/kernel_table.py::usable`."""
        return self.passed and self.complete and not self.dropped


class KernelTable:
    """Rows keyed by the Buildkite step key the recording job ran under."""

    def __init__(
        self,
        rows: dict[str, KernelRow] | None,
        unavailable: str = "",
        *,
        commit: str = "",
        build=None,
        pipeline: str = "",
    ):
        self._rows = rows or {}
        self.unavailable = unavailable
        self.commit = commit
        self.build = build
        self.pipeline = pipeline

    @property
    def available(self) -> bool:
        return not self.unavailable

    def __len__(self) -> int:
        return len(self._rows)

    def row(self, key: str) -> KernelRow | None:
        return self._rows.get(key)


class SymbolMap:
    """path -> the kernel symbols compiled from it or from anything including it."""

    def __init__(
        self,
        reach: dict[str, frozenset[str]] | None,
        host: frozenset[str] = frozenset(),
        unknown: frozenset[str] = frozenset(),
        unavailable: str = "",
        *,
        commit: str = "",
        incomplete: bool = False,
        objects: int = 0,
    ):
        self._reach = reach or {}
        # Files some object WITHOUT kernels was compiled from or included.
        self._host = host
        # Files an object the producer could not read was compiled from or
        # included. Its kernels are unaccounted for, so a silence about these
        # files is not evidence.
        self._unknown = unknown
        self.unavailable = unavailable
        self.commit = commit
        self.incomplete = incomplete
        self.objects = objects

    @property
    def available(self) -> bool:
        return not self.unavailable

    def symbols(self, path: str) -> frozenset[str]:
        return self._reach.get(path, frozenset())

    def knows(self, path: str) -> bool:
        """Whether the image build compiled anything from or including `path`.
        A file it did not is the map's business, not this record's."""
        return path in self._reach or path in self._host or path in self._unknown

    def why_not_clearable(self, path: str) -> str:
        """Empty when a silence about `path` may drop a step."""
        if path in self._unknown:
            return "an object compiled from or including it could not be read"
        if path in self._host:
            return "code without kernels includes it"
        if path not in self._reach:
            return "no kernel was compiled from it"
        return ""


@dataclass
class KernelEvidence:
    table: KernelTable
    symbol_map: SymbolMap

    @property
    def unavailable(self) -> str:
        if not self.table.available:
            return self.table.unavailable
        if not self.symbol_map.available:
            return self.symbol_map.unavailable
        return ""

    @property
    def matched(self) -> bool:
        """Both halves from the same vLLM commit, so the names line up."""
        return bool(self.table.commit) and self.table.commit == self.symbol_map.commit

    def describe(self) -> str:
        t, m = self.table, self.symbol_map
        pair = f"table {t.commit[:10] or '?'} (build {t.build}), map {m.commit[:10] or '?'}"
        return pair + ("" if self.matched else ", DIFFERENT COMMITS")


def _read_json_gz(path: Path) -> tuple[dict | None, str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"
    try:
        text = gzip.decompress(raw) if path.suffix == ".gz" else raw
        payload = json.loads(text)
    except Exception as exc:  # noqa: BLE001 - any parse problem reads the same
        return None, f"cannot parse {path}: {exc}"
    if not isinstance(payload, dict):
        return None, f"{path} is not a JSON object"
    return payload, ""


def load_table(path: Path) -> KernelTable:
    """Read a kernel table. Any problem yields a table that authorizes nothing."""
    payload, why = _read_json_gz(path)
    if payload is None:
        return KernelTable(None, why)
    version = payload.get("version")
    if version != TABLE_VERSION:
        return KernelTable(
            None,
            f"{path} is kernel table version {version!r}, expected {TABLE_VERSION}; "
            "rebuild it from the recordings with kernrec/kernel_table.py",
        )
    names = payload.get("names")
    blobs = payload.get("rows")
    if not isinstance(names, list) or not isinstance(blobs, dict):
        return KernelTable(None, f"{path} has no names list or rows object")
    if not blobs:
        # An empty rows object is a broken table, never "no step launched anything".
        return KernelTable(None, f"{path} contains no rows")
    rows: dict[str, KernelRow] = {}
    try:
        for key, b in blobs.items():
            # Indexed, not .get(): a missing health field would default to
            # the healthy value and read a weaker table as stronger.
            rows[key] = KernelRow(
                key=key,
                kernels=frozenset(names[i] for i in b["kernels"]),
                passed=bool(b["passed"]),
                complete=bool(b["complete"]),
                dropped=int(b["dropped"]),
                jobs=int(b.get("jobs", 0)),
                processes=int(b.get("processes", 0)),
            )
    except (KeyError, TypeError, IndexError, ValueError) as exc:
        return KernelTable(None, f"{path}: unreadable row: {type(exc).__name__}: {exc}")
    src = payload.get("source") or {}
    return KernelTable(
        rows,
        commit=str(src.get("commit") or ""),
        build=src.get("build"),
        pipeline=str(src.get("pipeline") or ""),
    )


def load_symbol_map(path: Path) -> SymbolMap:
    """Read a symbol map. Any problem yields a map that knows no file."""
    payload, why = _read_json_gz(path)
    if payload is None:
        return SymbolMap(None, unavailable=why)
    version = payload.get("version")
    if version != MAP_VERSION:
        return SymbolMap(
            None,
            unavailable=f"{path} is symbol map version {version!r}, expected {MAP_VERSION}",
        )
    # The producer fails closed: any object it could not read after a retry
    # empties the whole map and says why. Nothing to join then.
    if payload.get("reason"):
        return SymbolMap(
            None,
            unavailable=f"{path}: the producer wrote no objects ({payload['reason']})",
        )
    objects = payload.get("objects")
    if not isinstance(objects, list) or not objects:
        return SymbolMap(None, unavailable=f"{path} has no objects")
    reach: dict[str, set[str]] = defaultdict(set)
    host: set[str] = set()
    unknown: set[str] = set()
    try:
        for o in objects:
            paths = {o["source"], *o.get("deps", ())}
            if o.get("error"):
                unknown |= paths
                continue
            symbols = o.get("symbols") or ()
            # Symbols, not the `device` flag: a .cu holding only host code
            # compiles as a device object and still launches nothing.
            if symbols:
                for p in paths:
                    reach[p].update(symbols)
            else:
                host |= paths
    except (KeyError, TypeError) as exc:
        return SymbolMap(None, unavailable=f"{path}: unreadable object: {exc}")
    return SymbolMap(
        {p: frozenset(s) for p, s in reach.items()},
        frozenset(host),
        frozenset(unknown),
        commit=str(payload.get("commit") or ""),
        incomplete=bool(payload.get("incomplete")),
        objects=len(objects),
    )


@dataclass
class KernelReading:
    """One reading of the kernel record over one PR."""

    added: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    reasons: Counter = field(default_factory=Counter)
    #: changed file -> what this record could do with it, for the report
    files: dict[str, str] = field(default_factory=dict)


def read_pr(
    evidence: KernelEvidence,
    selection,
    changed_paths: Iterable[str],
    keys: RowKeys,
    *,
    held: Callable[[str], set[str]],
    stale: frozenset[str] = frozenset(),
    allow_drops: bool = True,
    protected: frozenset[str] = frozenset(),
) -> KernelReading:
    """The kernel record over one PR's map selection.

    `held(path)` names the steps a csrc file keeps whatever the record says
    (`classify.csrc_held_steps`). `stale` is the changed files whose source
    moved between the map's commit and the PR's base, which the caller
    computes when the freshness gate is on. `protected` is the steps the
    Python record kept on positive evidence about some other changed file;
    a silence about kernels cannot overrule an observed call.
    """
    table, sm = evidence.table, evidence.symbol_map
    reading = KernelReading()

    # Which changed files this record may speak about, and how loudly.
    voting: dict[str, frozenset[str]] = {}
    clearable: set[str] = set()
    for path in changed_paths:
        if not sm.knows(path):
            if path.startswith("csrc/"):
                reading.files[path] = (
                    "not in the map: the image build compiled nothing from it"
                )
                reading.reasons["file-not-in-map"] += 1
            continue
        voting[path] = sm.symbols(path)
        why = sm.why_not_clearable(path)
        if not why and path in stale:
            why = "its source changed between the map's commit and this base"
        if why:
            reading.files[path] = (
                f"{len(voting[path])} kernel symbols; may only select: {why}"
            )
            reading.reasons["file-may-only-select"] += 1
        else:
            clearable.add(path)
            reading.files[path] = (
                f"{len(voting[path])} kernel symbols; may select and drop"
            )
            reading.reasons["file-may-drop"] += 1

    def launched(row: KernelRow) -> bool:
        return any(syms and row.kernels & syms for syms in voting.values())

    # ADD. Ungated: one launch proves the step runs code from the file, and a
    # failed or incomplete row is still a record of what did run.
    already = set(selection.selected)
    for step_id in keys.candidates():
        if step_id in already:
            continue
        key = keys.key_for(step_id)
        row = table.row(key) if key else None
        if row is not None and launched(row):
            reading.added.append(step_id)
    reading.reasons["row-adds-a-step-the-map-missed"] += len(reading.added)

    # DROP. Every gate resolves toward keeping.
    by_step: dict[str, set[str]] = defaultdict(set)
    for path, steps in selection.selected_by_file.items():
        for s in steps:
            by_step[s].add(path)

    for step_id in selection.selected:
        why_keep = _why_keep(
            step_id,
            table,
            keys,
            launched,
            selection.selected_rules.get(step_id, ()),
            by_step.get(step_id, set()),
            voting,
            clearable,
            allow_drops,
            protected,
            held,
        )
        if why_keep:
            reading.kept.append(step_id)
            reading.reasons[why_keep] += 1
        else:
            reading.dropped.append(step_id)
            reading.reasons["row-launched-none-of-its-kernels"] += 1
    return reading


def _why_keep(
    step_id,
    table,
    keys,
    launched,
    rules,
    selectors,
    voting,
    clearable,
    allow_drops,
    protected,
    held,
) -> str:
    """The first gate that holds the step, or "" when it may drop."""
    key = keys.key_for(step_id)
    if key is None:
        return "unmappable-step-id"
    row = table.row(key)
    if row is None:
        return "no-row"
    if launched(row):
        return "row-launched-a-kernel-from-a-changed-file"
    if step_id in protected:
        return "held-by-the-python-record"
    if any(r in FILELESS_RULES for r in rules):
        return "selected-without-a-file"
    if not selectors:
        return "no-attributed-file"
    if any(p not in voting for p in selectors):
        # A Python file, a cmake file, a csrc file outside the CUDA build:
        # some reason the map had is one this record cannot argue with.
        return "selected-by-a-file-outside-the-map"
    if any(p not in clearable for p in selectors):
        return "file-may-only-select"
    if not row.usable:
        return "row-not-usable"
    if not allow_drops:
        return "table-and-map-from-different-commits"
    if any(step_id in held(p) for p in selectors):
        return "held-by-declaration-or-build"
    return ""
