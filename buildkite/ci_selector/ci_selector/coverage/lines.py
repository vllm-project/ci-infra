# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Which recorded lines a changed function's change would have run.

The record says which functions a step entered. A function nearly every step
enters, like `Scheduler.schedule`, then selects nearly every step, though the
change sits in a branch most of them never take: vllm#59029 changed one
condition there and added 112 steps, 2 of them needed. A row that also lists
the lines the step ran can answer for the change itself.

Per changed function, the probe lines are lines at the PR's base that any run
reaching the change must execute first:

- the lines of the change that carry instructions;
- the statements ending just before it and starting just after it;
- the header of the innermost compound statement holding it.

An execution that differs between base and head differs first at the change,
and to get there it ran one of those: it fell through the statement before,
or entered the block from its header, or the changed line itself. So a row
that ran none of them never reached the change. Every probe set
over-approximates, which only keeps more.

Probes are read at the PR's base and the row's lines at the record's commit,
so each probe maps through `git diff` between the two. A probe on a line that
changed in between has no counterpart, and the function falls back to the
function-level answer. So does a function new at base, one that runs at
import, a file renamed since the record, or a table spanning commits.
"""

from __future__ import annotations

import ast
import re
import subprocess
import types
from pathlib import Path

from .changed_funcs import MODULE, Attribution, _read, code_objects

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _diff_hunks(repo: Path, a: str, b: str | None, path: str):
    """[(old start, old count, new start, new count)] for one file."""
    args = ["git", "-C", str(repo), "diff", "-U0", "--no-color", "--no-ext-diff", a]
    if b:
        args.append(b)
    out = subprocess.run([*args, "--", path], capture_output=True, text=True)
    if out.returncode != 0:
        return None
    found = []
    for line in out.stdout.splitlines():
        m = _HUNK.match(line)
        if m:
            found.append(
                (
                    int(m.group(1)),
                    1 if m.group(2) is None else int(m.group(2)),
                    int(m.group(3)),
                    1 if m.group(4) is None else int(m.group(4)),
                )
            )
    return found


def line_map(repo: Path, record: str, base: str, path: str):
    """A function from a line at `base` to the same line at `record`, None
    for a line that changed in between. None when the file cannot be mapped."""
    if record == base:
        return lambda n: n
    hunks = _diff_hunks(repo, record, base, path)
    if hunks is None or _read(repo, record, path) is None:
        return None

    def to_record(n: int) -> int | None:
        shift = 0
        for ostart, ocount, nstart, ncount in hunks:
            first = nstart if ncount else nstart + 1
            if ncount and nstart <= n < nstart + ncount:
                return None
            if n < first:
                break
            shift += ocount - ncount
        return n + shift

    return to_record


def _instruction_lines(code: types.CodeType, qualname: str) -> set[int]:
    """Lines carrying instructions in `qualname`'s own code objects, nested
    scopes left out: the lines a LINE event can fire on for it."""
    out: set[int] = set()
    for c in code_objects(code):
        if c.co_qualname == qualname:
            out |= {ln for _s, _e, ln in c.co_lines() if ln}
    return out


def _own_lines(st: ast.stmt) -> set[int]:
    """A statement's own lines: all of a simple one, the header of a compound
    one (decorators to the line before its first body statement)."""
    first = min([st.lineno, *(d.lineno for d in getattr(st, "decorator_list", ()))])
    body = getattr(st, "body", None)
    if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
        return set(range(first, max(first, body[0].lineno - 1) + 1))
    return set(range(first, (st.end_lineno or st.lineno) + 1))


def _blocks(fn: ast.AST) -> list[tuple[list[ast.stmt], ast.AST]]:
    """Every statement list the function runs itself, with the statement
    owning it (the function, for its body). Nested scopes' bodies are not
    the function's."""
    out: list[tuple[list[ast.stmt], ast.AST]] = [(fn.body, fn)]

    def walk(stmts: list[ast.stmt]) -> None:
        for st in stmts:
            if isinstance(st, _SCOPES):
                continue
            for attr in ("body", "orelse", "finalbody"):
                block = getattr(st, attr, None)
                if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                    out.append((block, st))
                    walk(block)
            for handler in getattr(st, "handlers", ()):
                out.append((handler.body, handler))
                walk(handler.body)
            for case in getattr(st, "cases", ()):
                out.append((case.body, case))
                walk(case.body)

    walk(fn.body)
    return out


def _start(st: ast.stmt) -> int:
    return min([st.lineno, *(d.lineno for d in getattr(st, "decorator_list", ()))])


def _probes(
    fn: ast.AST, instr: set[int], first: int, last: int, indent: int | None = None
) -> set[int]:
    """Probe lines for a changed region [first, last] of one function; an
    insertion has last == first - 1, and `indent` is the column its first
    statement sits at. Empty when no line can witness it."""
    probes = {n for n in instr if first <= n <= last}
    if probes:
        return probes  # a run reaching the change runs one of these
    # Nothing in the region runs (an insertion, a comment, a closing
    # bracket): it is reached by falling through the statement before it in
    # its block, or from the block's header when it starts the block. Which
    # block an insertion joins is its indentation; without one, a boundary
    # between blocks is ambiguous and every block touching it counts.
    touching = [
        (block, owner)
        for block, owner in _blocks(fn)
        if _start(block[0]) <= last + 1
        and first - 1 <= max(x.end_lineno or x.lineno for x in block)
    ]
    same = [(b, o) for b, o in touching if b[0].col_offset == indent]
    for block, owner in same or touching:
        before = [x for x in block if (x.end_lineno or x.lineno) < first]
        if before:
            probes |= _own_lines(before[-1]) & instr
            continue
        header = set()
        if owner is not fn and isinstance(owner, ast.stmt):
            header = _own_lines(owner) & instr
        elif isinstance(owner, ast.excepthandler):
            header = set(range(owner.lineno, block[0].lineno)) & instr
        if not header:
            # The block starts with the change and its header runs nothing
            # (a function entry, `try:`, `else:`): the next statement does.
            after = [x for x in block if _start(x) > last and _own_lines(x) & instr]
            header = _own_lines(after[0]) & instr if after else set()
        probes |= header
    return probes


def _indent(head: str | None, start: int, count: int) -> int | None:
    """The column of the first statement line an insertion adds."""
    if head is None or not count:
        return None
    for text in head.splitlines()[start - 1 : start - 1 + count]:
        stripped = text.lstrip()
        if stripped and not stripped.startswith("#"):
            return len(text.expandtabs()) - len(stripped)
    return None


def probes_at_base(
    source: str, path: str, hunks, names, head: str | None = None
) -> dict[str, frozenset[int]]:
    """Changed function -> its probe lines, at base numbering, for the names
    this can answer for. `hunks` is the PR's diff of the file, `head` the
    file after it."""
    try:
        code = compile(source, path, "exec")
        tree = ast.parse(source)
    except Exception:
        return {}
    scopes: dict[str, list[ast.AST]] = {}

    def index(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}"
                scopes.setdefault(qual, []).append(child)
                index(child, f"{qual}.<locals>.")
            elif isinstance(child, ast.ClassDef):
                index(child, f"{prefix}{child.name}.")
            elif not isinstance(child, ast.Lambda):
                index(child, prefix)

    index(tree, "")
    out: dict[str, frozenset[int]] = {}
    for name in names:
        defs = scopes.get(name)
        if name == MODULE or not defs or len(defs) != 1:
            continue  # new at base, an import-time body, or ambiguous
        fn = defs[0]
        lo = min([fn.lineno, *(d.lineno for d in fn.decorator_list)])
        hi = fn.end_lineno or fn.lineno
        body_lo = fn.body[0].lineno
        instr = _instruction_lines(code, name)
        if not instr:
            continue
        probes: set[int] = set()
        touched = False
        for ostart, ocount, nstart, ncount in hunks:
            first, last = (
                (ostart, ostart + ocount - 1) if ocount else (ostart + 1, ostart)
            )
            if last < lo or first > hi:
                continue
            if first < body_lo:
                probes = set()  # the signature or decorators: a call-site change
                touched = False
                break
            touched = True
            indent = _indent(head, nstart, ncount) if not ocount else None
            found = _probes(fn, instr, max(first, body_lo), min(last, hi), indent)
            if not found:
                probes = set()
                touched = False
                break
            probes |= found
        if touched and probes:
            out[name] = frozenset(probes)
    return out


def attach_line_probes(query, repo: Path, base: str, head: str | None, table) -> int:
    """Set `line_probes` on each changed file the record's lines can answer
    for, at the record's numbering. Returns how many functions got probes."""
    commits = table.source.get("commits") or ([table.commit] if table.commit else [])
    if len(commits) != 1 or not any(
        r.stamp.lines_recorded for r in table._rows.values()
    ):
        return 0
    record = commits[0]
    attached = 0
    for f in query.files:
        if (
            f.status is not Attribution.ATTRIBUTED
            or f.old_path
            or not f.path.endswith(".py")
        ):
            continue
        names = (f.base_names | f.head_names) - f.import_time
        if not names:
            continue
        source = _read(repo, base, f.path)
        hunks = _diff_hunks(repo, base, head, f.path)
        to_record = line_map(repo, record, base, f.path)
        if source is None or not hunks or to_record is None:
            continue
        probes = {}
        head_text = _read(repo, head, f.path)
        for name, lines in probes_at_base(
            source, f.path, hunks, names, head_text
        ).items():
            mapped = {to_record(n) for n in lines}
            if mapped and None not in mapped:
                probes[name] = frozenset(mapped)
        if probes:
            f.line_probes = probes
            attached += len(probes)
    return attached
