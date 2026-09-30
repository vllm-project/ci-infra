# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Turn a diff into the question the record asks: which functions changed, per
file.

Names come from CPython rather than our own AST walk: compile the source and
read the qualnames and line tables off the code objects. That is the mechanism
that produced the recordings, so every name is spelled the way the recorder
wrote it, with no version rules to keep in sync.

A changed line belongs to every code object claiming it, so boundary lines are
owned twice over. That is correct, not merely careful: a default argument
really does run at import time.

Two things this will not do. It never matches on a first line number, which
shifts when anything above a function moves. And it never reads an empty result
as an answer, because "no Python in this file" and "we could not read it" must
not look the same downstream.
"""

from __future__ import annotations

import ast
import io
import subprocess
import tokenize
import types
from dataclasses import dataclass, field
from enum import Enum
from inspect import CO_OPTIMIZED
from pathlib import Path

import regex as re

from ..handwritten import (
    SELECTOR_SCOPE,
)

MODULE = "<module>"

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class Attribution(str, Enum):
    """Why a file's name sets look the way they do.

    ATTRIBUTED   compiled, with real names read off it
    NAMELESS     nothing to extract, so a kernel or a data file
    FAILED       Python we could not compile or could not read
    """

    ATTRIBUTED = "attributed"
    NAMELESS = "nameless-by-nature"
    FAILED = "could-not-attribute"


@dataclass
class FileQuery:
    """One changed file, and the functions it changed on each side of the diff."""

    path: str  # repo-relative; the head-side path, or the old path for a delete
    status: Attribution
    base_names: frozenset[str] = frozenset()
    head_names: frozenset[str] = frozenset()
    old_path: str | None = None
    # The subset of `names` that runs at import: `<module>` and class bodies.
    # Every step importing the file executes these, so they narrow nothing.
    import_time: frozenset[str] = frozenset()
    # A changed line we could not place in any scope. Means the diff and the
    # source disagree, so the file is not answerable.
    residue: bool = False
    # False for anything the recorder never watches (tests/, benchmarks/, csrc/).
    # The names may be perfectly good; no row can ever hold them.
    in_recorder_scope: bool = True
    # Set from the table: the recorder's names for this file are not faithful to
    # its source, so matching on names is meaningless here. See merge.py.
    unfaithful: bool = False
    # A stand-in for a file the recorder cannot see, not part of the diff.
    # The drop side weighs it like any file; the add side skips it.
    proxy: bool = False
    # The one hardware family whose `current_platform.is_<x>()` branch holds
    # every changed line, or None. Such code runs on no other family's jobs,
    # whatever the recording says about the function around it.
    platform: str | None = None
    note: str = ""  # why FAILED, for diagnosis; never load-bearing

    @property
    def names(self) -> frozenset[str]:
        return self.base_names | self.head_names

    @property
    def function_names(self) -> frozenset[str]:
        """Changed names that need a real call, not merely an import."""
        return self.names - self.import_time

    @property
    def fail_open(self) -> bool:
        """True when this file cannot authorize narrowing anything."""
        return (
            self.status is Attribution.FAILED
            or self.residue
            or self.unfaithful
            or not self.in_recorder_scope
        )


@dataclass
class Query:
    base: str
    head: str | None
    files: list[FileQuery] = field(default_factory=list)
    # Python files the diff changed only in line endings: no FileQuery, since
    # no function changed. Kept for diagnosis.
    eol_only: list[str] = field(default_factory=list)
    # Python files whose change preserves behaviour: no code object changed
    # apart from line numbers, or only docstrings of undecorated functions
    # and annotations of the undecorated ones import defines.
    # No FileQuery either, and a step selected only for these may be dropped.
    inert: list[str] = field(default_factory=list)

    @property
    def fail_open(self) -> bool:
        return any(f.fail_open for f in self.files)

    def restrict(self, paths: set[str]) -> Query:
        """The same query, narrowed to the files one step was selected for.

        Without this, one unanswerable `tests/` file keeps every step on the
        PR. Narrowing cuts both ways though: dropping a file can only remove a
        fail-open or a matching name, and both turn a keep into a drop. So it
        is safe only on a path set COMPLETE for the step, which is what
        `Selection.selected_paths` guarantees. A csrc claim carries its
        wrapper files instead of its own path, so completeness there rests on
        a kernel being reachable only through its wrappers.

        Matches either side of a rename. The FileQuery objects are shared, not
        copied, so a flag set on the query afterwards shows through here too.
        """
        return Query(
            base=self.base,
            head=self.head,
            files=[
                f
                for f in self.files
                if f.path in paths or (f.old_path and f.old_path in paths)
            ],
        )

    def flat_names(self) -> frozenset[str]:
        """(path, qualname) pairs, the shape a row is keyed on.

        A projection of the per-file structure, never a replacement for it:
        Phase 5 needs the per-file link to scope abstention by reason.
        """
        return frozenset(f"{f.path}\t{name}" for f in self.files for name in f.names)


def code_objects(code: types.CodeType):
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from code_objects(const)


def names_of(source: str, path: str) -> frozenset[str]:
    """Every qualname CPython produces for this source, as the recorder spells it.

    Raises whatever compile() raises; callers decide that means fail-open.
    """
    return frozenset(c.co_qualname for c in code_objects(compile(source, path, "exec")))


def import_time_names(source: str, path: str) -> frozenset[str]:
    """Names whose code runs on import: the module body and every class body.

    Read off the compiler rather than guessed from the shape of a name. The
    distinction carries weight: every step importing a file runs these, so a
    change to one says almost nothing, while a change inside a real function is
    what lets the record narrow.
    """
    return frozenset(
        c.co_qualname
        for c in code_objects(compile(source, path, "exec"))
        if not c.co_flags & CO_OPTIMIZED
    )


def _spans(code: types.CodeType, total_lines: int) -> dict[str, tuple[int, int]]:
    """qualname -> the inclusive line range it covers, nested scopes included.

    Only used to place a line that no code object claims: a comment, a blank, a
    docstring. `<module>` always spans the whole file so the fallback terminates.
    """
    spans: dict[str, tuple[int, int]] = {}

    def visit(c: types.CodeType) -> tuple[int, int]:
        low = high = c.co_firstlineno
        for _s, _e, lineno in c.co_lines():
            if lineno:
                low = min(low, lineno)
                high = max(high, lineno)
        for const in c.co_consts:
            if isinstance(const, types.CodeType):
                clow, chigh = visit(const)
                low, high = min(low, clow), max(high, chigh)
        prev = spans.get(c.co_qualname)
        spans[c.co_qualname] = (
            (low, high) if prev is None else (min(prev[0], low), max(prev[1], high))
        )
        return low, high

    visit(code)
    spans[MODULE] = (1, max(total_lines, spans.get(MODULE, (1, 1))[1]))
    return spans


def _owners(
    source: str, path: str
) -> tuple[dict[int, set[str]], dict[str, tuple[int, int]]]:
    code = compile(source, path, "exec")
    owners: dict[int, set[str]] = {}
    for c in code_objects(code):
        for _s, _e, lineno in c.co_lines():
            if lineno:
                owners.setdefault(lineno, set()).add(c.co_qualname)
    return owners, _spans(code, source.count("\n") + 1)


def attribute(source: str, path: str, lines: set[int]) -> tuple[frozenset[str], bool]:
    """(names owning those lines, residue). Residue means a changed line sits
    outside every scope in the file, which only happens when the diff and the
    source disagree. Not a gap to paper over, a reason to stop trusting the
    file."""
    if not lines:
        return frozenset(), False
    owners, spans = _owners(source, path)
    found: set[str] = set()
    residue = False
    for line in lines:
        direct = owners.get(line)
        if direct:
            found |= direct
            continue
        # Innermost scope containing the line: smallest span wins, ties kept.
        containing = [
            (hi - lo, name) for name, (lo, hi) in spans.items() if lo <= line <= hi
        ]
        if not containing:
            residue = True
            continue
        best = min(width for width, _ in containing)
        found |= {name for width, name in containing if width == best}
    return frozenset(found), residue


def hunks(
    repo: Path, base: str, head: str | None, ignore_cr_at_eol: bool = True
) -> dict[str, tuple[set[int], set[int]]]:
    """path -> (base-side changed lines, head-side changed lines). Keyed under
    both sides of a rename so either path finds it. A zero count on one side
    means nothing changed there, which is what a pure add or delete looks
    like.

    A line whose only change is a carriage return at its end is not changed:
    Python reads either line ending the same way. Without this, a file an
    editor saved with CRLF names every function in it, and a module's
    import-time code runs in every step that imports it. vllm#58669 changed
    two function bodies of one router and converted it to CRLF, and the
    record added the 43 server-starting steps that import it.
    """
    args = [
        "git",
        "-c",
        "core.quotepath=false",
        "-C",
        str(repo),
        "diff",
        "-U0",
        "-M",
        "--no-prefix",
        *(["--ignore-cr-at-eol"] if ignore_cr_at_eol else []),
        base,
    ]
    if head:
        args.append(head)
    out = subprocess.run(args, capture_output=True, text=True, check=True).stdout

    found: dict[str, tuple[set[int], set[int]]] = {}
    old_path = new_path = None
    for line in out.splitlines():
        if line.startswith("--- "):
            old_path = None if line[4:] == "/dev/null" else line[4:]
        elif line.startswith("+++ "):
            new_path = None if line[4:] == "/dev/null" else line[4:]
        elif line.startswith("@@"):
            m = _HUNK.match(line)
            if not m:
                continue
            ostart, ocount, nstart, ncount = (
                int(m.group(1)),
                1 if m.group(2) is None else int(m.group(2)),
                int(m.group(3)),
                1 if m.group(4) is None else int(m.group(4)),
            )
            for key in (old_path, new_path):
                if key is None:
                    continue
                base_lines, head_lines = found.setdefault(key, (set(), set()))
                base_lines |= set(range(ostart, ostart + ocount))
                head_lines |= set(range(nstart, nstart + ncount))
    return found


def _read(repo: Path, ref: str | None, path: str) -> str | None:
    """File content at a ref, or from the working tree when ref is None."""
    if ref is None:
        try:
            return (repo / path).read_text()
        except OSError:
            return None
    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"], capture_output=True
    )
    if proc.returncode != 0:
        return None
    try:
        return proc.stdout.decode()
    except UnicodeDecodeError:
        return None


def _side(
    repo: Path, ref: str | None, path: str | None, lines: set[int]
) -> tuple[frozenset[str], bool, str, frozenset[str]]:
    """(names, residue, note, import-time subset) for one side of one file."""
    if path is None or not lines:
        return frozenset(), False, "", frozenset()
    source = _read(repo, ref, path)
    if source is None:
        return frozenset(), False, f"unreadable at {ref or 'worktree'}", frozenset()
    try:
        names, residue = attribute(source, path, lines)
        at_import = names & import_time_names(source, path)
    except Exception as exc:  # compile() on anything we cannot handle: fail open
        return frozenset(), False, f"{type(exc).__name__}: {exc}", frozenset()
    return names, residue, "", at_import


def build(repo: Path, base: str, head: str | None = None) -> Query:
    """The query for a diff: per changed file, the functions it touched."""
    from ..gitdiff import diff_files

    by_path = hunks(repo, base, head)
    # Only needed to tell a line-ending-only file from one with no hunks at all.
    strict: dict[str, tuple[set[int], set[int]]] | None = None
    query = Query(base=base, head=head)

    for changed in diff_files(repo, base, head):
        path, old_path = changed.path, changed.old_path
        base_lines, head_lines = set(), set()
        for key in (path, old_path):
            if key and key in by_path:
                base_lines |= by_path[key][0]
                head_lines |= by_path[key][1]

        shown = path if changed.status != "D" else (old_path or path)
        in_scope = shown.startswith(SELECTOR_SCOPE)

        if not shown.endswith(".py"):
            query.files.append(
                FileQuery(
                    path=shown,
                    old_path=old_path,
                    status=Attribution.NAMELESS,
                    in_recorder_scope=in_scope,
                )
            )
            continue

        if not base_lines and not head_lines:
            if strict is None:
                strict = hunks(repo, base, head, ignore_cr_at_eol=False)
            # Hunks only once line endings count: the file changed nothing but
            # them, so it changed no function. The code map still sees the path.
            if any(
                key and strict.get(key, (set(), set())) != (set(), set())
                for key in (path, old_path)
            ):
                query.eol_only.append(shown)
                continue
        # A Python file git reported with no hunks at all: a mode change, a
        # binary blob, or a path our hunk parse failed to match. We cannot tell
        # which, so we do not get to say "nothing changed here".
        if not base_lines and not head_lines:
            query.files.append(
                FileQuery(
                    path=shown,
                    old_path=old_path,
                    status=Attribution.FAILED,
                    in_recorder_scope=in_scope,
                    note="no hunks parsed",
                )
            )
            continue

        base_side = None if changed.status == "A" else (old_path or path)
        head_side = None if changed.status == "D" else path
        base_names, base_residue, base_note, base_import = _side(
            repo, base, base_side, base_lines
        )
        head_names, head_residue, head_note, head_import = _side(
            repo, head, head_side, head_lines
        )

        note = "; ".join(n for n in (base_note, head_note) if n)
        if not note and base_side and head_side and shown.endswith(".py"):
            base_names, head_names, import_time, inert = _drop_unchanged(
                repo,
                base,
                head,
                base_side,
                head_side,
                base_names,
                head_names,
                base_import | head_import,
            )
            if inert:
                query.inert.append(shown)
                continue
            base_import, head_import = import_time, frozenset()
        platform = None
        if shown.endswith(".py") and base_side and head_side and not note:
            sides = [
                _platform_of_lines(_read(repo, base, base_side), base_lines),
                _platform_of_lines(_read(repo, head, head_side), head_lines),
            ]
            placed = {x for x in sides if x is not None}
            if len(placed) == 1 and False not in placed:
                platform = placed.pop()
        query.files.append(
            FileQuery(
                path=shown,
                old_path=old_path,
                status=Attribution.FAILED if note else Attribution.ATTRIBUTED,
                base_names=base_names,
                head_names=head_names,
                residue=base_residue or head_residue,
                import_time=base_import | head_import,
                in_recorder_scope=in_scope,
                note=note,
                platform=platform,
            )
        )
    return query


# current_platform.is_<x>() -> the hardware family whose jobs run that branch.
# Single families only: is_cuda_alike() spans two, and a plain `else` is
# everything the guard is not.
PLATFORM_GUARDS = {"is_xpu": "xpu", "is_rocm": "amd", "is_cpu": "cpu", "is_tpu": "tpu"}


def _guard_family(test: ast.expr) -> str | None:
    if (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Attribute)
        and test.func.attr in PLATFORM_GUARDS
        and isinstance(test.func.value, ast.Name)
        and test.func.value.id == "current_platform"
        and not test.args
    ):
        return PLATFORM_GUARDS[test.func.attr]
    return None


def _platform_of_lines(text: str | None, lines: set[int]) -> str | None | bool:
    """The family whose guarded branch holds every changed line, None when no
    line needs placing, or False when some line sits outside such a branch.
    Blank and comment lines place anywhere."""
    if not lines:
        return None
    if text is None:
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    spans: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            family = _guard_family(node.test)
            if family and node.body:
                spans.append((node.body[0].lineno, node.body[-1].end_lineno, family))
    source = text.splitlines()
    found: set[str] = set()
    for n in lines:
        stripped = source[n - 1].strip() if 0 < n <= len(source) else ""
        if not stripped or stripped.startswith("#"):
            continue
        # Innermost guard wins: the narrowest span holding the line.
        holding = sorted((b - a, fam) for a, b, fam in spans if a <= n <= b)
        if not holding:
            return False
        found.add(holding[0][1])
    if not found:
        return None
    return found.pop() if len(found) == 1 else False


def _equivalent(a: types.CodeType, b: types.CodeType) -> bool:
    """Same code, line numbers aside: bytecode, names, locals, flags and
    constants, nested code compared the same way."""
    if (
        a.co_code != b.co_code
        or a.co_names != b.co_names
        or a.co_varnames != b.co_varnames
        or a.co_freevars != b.co_freevars
        or a.co_cellvars != b.co_cellvars
        or a.co_flags != b.co_flags
        or a.co_argcount != b.co_argcount
        or a.co_posonlyargcount != b.co_posonlyargcount
        or a.co_kwonlyargcount != b.co_kwonlyargcount
        or a.co_qualname != b.co_qualname
        or len(a.co_consts) != len(b.co_consts)
    ):
        return False
    for x, y in zip(a.co_consts, b.co_consts):
        if isinstance(x, types.CodeType) or isinstance(y, types.CodeType):
            if not (
                isinstance(x, types.CodeType)
                and isinstance(y, types.CodeType)
                and _equivalent(x, y)
            ):
                return False
        elif type(x) is not type(y) or x != y:
            return False
    return True


# A module naming one of these may read function annotations at runtime:
# torch custom op registration infers the op schema from them, and
# support_torch_compile the dynamic dims from forward's. vLLM's own CustomOp
# class dispatches by name and reads none, so it is not listed.
_ANNOTATION_READERS = (
    "direct_register_custom_op",
    "library.custom_op",
    "infer_schema",
    "get_type_hints",
    "signature(",
    "support_torch_compile",
)


def _strip_annotations(tree: ast.AST) -> ast.AST:
    """Remove parameter and return annotations of undecorated functions that
    import defines. A decorator may read them (FastAPI, pydantic, typer), so
    decorated ones keep theirs, as do class-level field annotations
    (dataclasses). A def inside a function evaluates its annotations each
    time that function runs, so those stay too."""
    todo = [(tree, False)]
    while todo:
        node, in_function = todo.pop()
        is_def = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if is_def and not in_function and not node.decorator_list:
            node.returns = None
            a = node.args
            for arg in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg):
                if arg is not None:
                    arg.annotation = None
        todo.extend((c, in_function or is_def) for c in ast.iter_child_nodes(node))
    return tree


def _strip_docstrings(tree: ast.AST) -> list[tuple[str, str | None]]:
    """Remove the leading docstring of every undecorated function, and return
    (name, docstring) for each in walk order.

    Functions only: vllm/engine/arg_utils.py turns config classes' docstrings
    into CLI help. A decorator may read one (register_op in vllm/ir/op.py keeps
    it for the op's str), so decorated functions keep theirs.
    """
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not node.decorator_list
        ):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                node.body = node.body[1:]
            found.append((node.name, doc))
    return found


def _logical_lines(text: str) -> list[str]:
    """Each statement line of the source, a call wrapped over lines joined
    back, comments left out. The whole text when it does not tokenize."""
    lines, cur = [], []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
                lines.append(" ".join(cur))
                cur = []
            elif tok.type not in (tokenize.COMMENT, tokenize.NL):
                cur.append(tok.string)
    except (tokenize.TokenError, SyntaxError):
        return [text]
    return lines


def _docstring_read(repo: Path, ref: str | None, names: set[str]) -> bool:
    """Whether a Python statement that reads `__doc__` or `getdoc` names one
    of these: arg_utils.py appends human_readable_int's docstring to CLI help.
    Per statement, not per line, since ruff wraps a long call and puts the
    name on a line of its own. A search that fails counts as a read."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "grep", "-l", "-z", "-I", "-E", "__doc__|getdoc"]
        + ([ref] if ref else [])
        + ["--", "*.py"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if proc.returncode not in (0, 1):
        return True
    word = re.compile(r"\b(?:" + "|".join(map(re.escape, sorted(names))) + r")\b")
    for hit in filter(None, proc.stdout.split("\0")):
        path = hit[len(ref) + 1 :] if ref else hit
        text = _read(repo, ref, path)
        if text is None:
            return True
        for line in _logical_lines(text):
            if ("__doc__" in line or "getdoc" in line) and word.search(line):
                return True
    return False


def _preserving(repo: Path, ref: str | None, before: str, after: str) -> bool:
    """Whether the two sources differ only in annotations _strip_annotations
    removes and docstrings _strip_docstrings removes, with no changed
    docstring read at runtime (searched at `ref`)."""
    annotations = not any(m in before or m in after for m in _ANNOTATION_READERS)
    try:
        trees = [ast.parse(before), ast.parse(after)]
    except SyntaxError:
        return False
    if annotations:
        trees = [_strip_annotations(t) for t in trees]
    old_docs, new_docs = (_strip_docstrings(t) for t in trees)
    if ast.dump(trees[0]) != ast.dump(trees[1]):
        return False
    # Equal trees walk in the same order, so the lists pair up.
    edited = {n for (n, a), (_, b) in zip(old_docs, new_docs) if a != b}
    return not edited or not _docstring_read(repo, ref, edited)


class _Shell(ast.NodeTransformer):
    """A module with what import does not run taken out: function bodies,
    parameter lists and annotations, and defaults that are plain literals.
    Decorators stay, and so does any default that is an expression, since
    both run when the def does."""

    def _fn(self, node):
        args = node.args
        kept = [
            d
            for d in [*args.defaults, *(k for k in args.kw_defaults if k is not None)]
            if not isinstance(d, ast.Constant)
        ]
        for d in kept:
            self.visit(d)
        node.decorator_list = [self.visit(d) for d in node.decorator_list]
        node.body = [ast.Pass()]
        node.returns = None
        node.args = ast.arguments(
            posonlyargs=[], args=[], vararg=None, kwonlyargs=[],
            kw_defaults=[], kwarg=None, defaults=kept,
        )
        if hasattr(node, "type_params"):
            node.type_params = []
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = _fn


def _shell_equal(before: str, after: str) -> bool:
    """Whether import runs the same code on both sides: the modules differ
    only inside function bodies, in signatures, or in literal defaults."""
    try:
        a = _Shell().visit(ast.parse(before))
        b = _Shell().visit(ast.parse(after))
    except SyntaxError:
        return False
    return ast.dump(a) == ast.dump(b)


def _drop_unchanged(
    repo, base, head, base_side, head_side, base_names, head_names, import_time
):
    """Changed names whose code did not change, taken out, and whether nothing
    of the file is left.

    A changed line can belong to a function whose code is identical: a
    signature line split differently, a comment, a blank. vllm#58687 added a
    return annotation to fused_mm_input_norm_triton, which leaves the function
    itself unchanged; its callers were selected anyway. An annotation does
    change the module body, which builds it at import, so a file whose two
    sides differ only there counts as unchanged too.

    A docstring is a constant of its function, so rewording one changes the
    code object though nothing it runs. vllm#59008 reworded packed_qk_rope_'s
    and kept ~30 steps. Compared on the AST, never by skipping the first
    constant: CPython shares it with an equal string the body uses.
    """
    before, after = _read(repo, base, base_side), _read(repo, head, head_side)
    if before is None or after is None:
        return base_names, head_names, import_time, False
    try:
        old = {}
        for c in code_objects(compile(before, base_side, "exec")):
            old.setdefault(c.co_qualname, []).append(c)
        new = {}
        for c in code_objects(compile(after, head_side, "exec")):
            new.setdefault(c.co_qualname, []).append(c)
    except Exception:
        return base_names, head_names, import_time, False

    def same(name: str) -> bool:
        a, b = old.get(name), new.get(name)
        return (
            a is not None
            and b is not None
            and len(a) == len(b)
            and all(_equivalent(x, y) for x, y in zip(a, b))
        )

    unchanged = {n for n in base_names | head_names if same(n)}
    left = (base_names | head_names) - unchanged
    # Judged on the whole file, so all or nothing.
    if left and _preserving(repo, head, before, after):
        unchanged |= left
        left = frozenset()
    # A module or class body differs whenever a def in it does: the def builds
    # its function from the new code, and a new parameter's default is a new
    # constant. If that is all, import runs the same code. vllm#58828 added
    # `expert_map=None` to get_cutlass_moe_mm_data, and `<module>` of
    # _custom_ops.py kept 145 steps that only import it.
    if left & import_time and left - import_time and _shell_equal(before, after):
        unchanged |= left & import_time
        left = left - import_time
    if left and left <= import_time:
        # Only import-time names would be left, and under the default phase
        # mode an import-time-only change counts every importer as a use: far
        # wider than the callers the full name set reaches. All or nothing,
        # then.
        return base_names, head_names, import_time, False
    base_names = frozenset(base_names - unchanged)
    head_names = frozenset(head_names - unchanged)
    return base_names, head_names, frozenset(import_time - unchanged), not left


def mark_unfaithful(query: Query, unfaithful_paths: set[str]) -> Query:
    """Flag files whose recorded names the recorder cannot spell from source.

    The table knows this, the diff cannot. Applied as a separate step so the
    query stays a pure function of the diff.
    """
    for f in query.files:
        if f.path in unfaithful_paths or (
            f.old_path and f.old_path in unfaithful_paths
        ):
            f.unfaithful = True
    return query


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo", type=Path)
    ap.add_argument("ref", help="'A...B' merge-base diff, 'A..B', or 'A' vs worktree")
    ap.add_argument("--names", action="store_true", help="print every name")
    args = ap.parse_args()

    from ..gitdiff import resolve_diff_ref

    base, head = resolve_diff_ref(args.repo, args.ref)
    query = build(args.repo, base, head)
    for f in sorted(query.files, key=lambda f: f.path):
        flags = [f.status.value]
        if f.residue:
            flags.append("residue")
        if not f.in_recorder_scope:
            flags.append("out-of-scope")
        if f.note:
            flags.append(f.note)
        print(f"{f.path}  [{', '.join(flags)}]  {len(f.names)} names")
        if args.names:
            for name in sorted(f.names):
                print(f"    {name}")
    print(f"\n{len(query.files)} files, fail_open={query.fail_open}")


if __name__ == "__main__":
    main()
