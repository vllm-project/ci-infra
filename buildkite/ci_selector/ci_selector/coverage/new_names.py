# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Which new names the record may reason about after all.

A changed name no row holds is unknown, and an unknown name blocks narrowing
for every step touching its file: the record cannot say a step never runs code
it has never seen. For a name that did not exist at the PR's base that is too
cautious. Nothing unchanged can call it, since it did not exist, so the only
code that can reach it is code this PR changed, plus dynamic lookups. When
every reference in the changed files is inside a changed function the record
does know, or in a test, or an import, the new name adds nothing the evidence
for those functions does not already cover. It is then resolved: no longer
unknown.

A reference we cannot place resolves nothing: one that runs on import, at
module level or in a class body, even a new one (a registry, a decorator, a
default argument), a string equal to the name, a mention in a changed
non-Python file, or one owned by a function the diff did not change (a
different symbol with the same name). A name that existed at base but no row
ever recorded is out of scope; that is missing data, not new code.

vllm#58684 added four private helpers, all called only from the changed
`plan` and `build`; vllm#58664 added a wrapper only its new test calls. Both
kept nearly every step touching the file on the unknown names alone.
"""

from __future__ import annotations

import ast
import builtins
import subprocess

import regex as re

from .changed_funcs import Query, _read, attribute, import_time_names, names_of

TEST_ROOTS = ("tests/",)
# Changed files that cannot hold a dynamic reference to a Python name: C and
# CUDA sources, whose op names match their Python wrappers by design, and
# prose. A config file (yaml, json, toml) still blocks.
INERT_TEXT_SUFFIXES = (
    ".cu",
    ".cuh",
    ".cpp",
    ".cc",
    ".c",
    ".h",
    ".hpp",
    ".hip",
    ".md",
    ".rst",
)
INERT_TEXT_ROOTS = ("csrc/", "docs/")
# Class decorators that hand the class back and keep no reference to it.
INERT_CLASS_DECORATORS = ("dataclass",)


def _leaf(qualname: str) -> str:
    return qualname.rsplit(".", 1)[-1]


def _dunder_class(name: str, import_time: frozenset[str]) -> str | None:
    """The class `name` is a dunder of, or None.

    A dunder runs on the class or an instance through syntax (`==`, `with`,
    a call), so its own references say nothing. Matched by leaf, vllm#58975's
    RetryableRequestError.__init__ met every super().__init__ in the diff and
    kept 53 unrelated steps while the class itself resolved. So it follows
    its class instead.
    """
    owner, _, leaf = name.rpartition(".")
    if len(leaf) > 4 and leaf[:2] == leaf[-2:] == "__" and owner in import_time:
        return owner
    return None


def _bindings(tree: ast.Module) -> dict[str, int] | None:
    """How often each name is bound anywhere in the file, or None when a
    star import can bind any of them."""
    count: dict[str, int] = {}
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            names = [node.id]
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ) or (isinstance(node, ast.ExceptHandler) and node.name):
            names = [node.name]
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    return None
                names.append(alias.asname or alias.name.split(".")[0])
        for n in names:
            count[n] = count.get(n, 0) + 1
    return count


def _inert_decorator(node: ast.expr) -> bool:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr in INERT_CLASS_DECORATORS
    return isinstance(node, ast.Name) and node.id in INERT_CLASS_DECORATORS


def _ancestors(tree: ast.Module, cls: str) -> set[str] | None:
    """The classes of this file `cls` inherits from, or None when code can
    get hold of `cls` without naming it, and so run its dunders.

    A decorator is handed the class, and so are a base's __init_subclass__
    and metaclass: vllm/v1/metrics/perf.py registers every ComponentMetrics
    subclass that way, and an Enum builds its members as the class is made.
    So: no decorator but dataclass and no class keywords, on the class or
    any ancestor, and every ancestor a class of this file without
    __init_subclass__, up to builtin types.
    """
    bound = _bindings(tree)
    if bound is None:
        return None
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    seen: set[str] = set()
    todo = [cls]
    while todo:
        name = todo.pop()
        node = classes.get(name)
        if node is None:
            # object, Exception and the like, unless the file rebinds them.
            kind = getattr(builtins, name, None)
            if name == cls or name in bound or type(kind) is not type:
                return None
            continue
        if name in seen:
            continue
        if bound.get(name) != 1 or node.keywords:
            return None
        if name != cls and any(
            isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))
            and s.name == "__init_subclass__"
            for s in node.body
        ):
            return None
        if not all(_inert_decorator(d) for d in node.decorator_list):
            return None
        if not all(isinstance(b, ast.Name) for b in node.bases):
            return None
        seen.add(name)
        todo.extend(b.id for b in node.bases)
    return seen - {cls}


def _scanned(repo, head: str | None, names: set[str]) -> bool:
    """Whether a file calling __subclasses__() names one of `names`: the
    benchmark datasets and CLI subcommands find their classes that way.
    True when git cannot say."""
    if head is None:
        return True

    def grep(files: list[str], *flags: str) -> list[str] | None:
        proc = subprocess.run(
            ["git", "-C", str(repo), "grep", "-l", *flags, head, "--", *files],
            capture_output=True,
            text=True,
        )
        if proc.returncode not in (0, 1):
            return None
        return [ln.removeprefix(f"{head}:") for ln in proc.stdout.splitlines() if ln]

    scans = grep([], "-F", "__subclasses__")
    if not scans:
        return scans is None
    named = grep(scans, "-w", "-E", "|".join(sorted(re.escape(n) for n in names)))
    return named is None or bool(named)


class _Refs:
    """One parse of a changed file: where each name is referenced, which
    strings it holds, and who owns a line. Owners are cached per line.

    A reference also carries the defs it is a decorator or default of: those
    run where the def statement does, though the line is the function's too.
    """

    def __init__(self, path: str, source: str):
        self.path = path
        self.source = source
        self.lines: dict[str, list[tuple[int, frozenset[str]]]] = {}
        self.strings: set[str] = set()
        self.tree = ast.parse(source)
        # Module and class bodies, which every importer runs.
        self.import_time = import_time_names(source, path)
        header: dict[int, frozenset[str]] = {}
        for node in ast.walk(self.tree):
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
            ):
                name = getattr(node, "name", "<lambda>")
                parts = list(getattr(node, "decorator_list", ()))
                if not isinstance(node, ast.ClassDef):
                    parts += [
                        d for d in (*node.args.defaults, *node.args.kw_defaults) if d
                    ]
                for part in parts:
                    for sub in ast.walk(part):
                        header[id(sub)] = header.get(id(sub), frozenset()) | {name}
            if isinstance(node, ast.Name):
                self.lines.setdefault(node.id, []).append(
                    (node.lineno, header.get(id(node), frozenset()))
                )
            elif isinstance(node, ast.Attribute):
                self.lines.setdefault(node.attr, []).append(
                    (node.lineno, header.get(id(node), frozenset()))
                )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # Imports are not Name nodes, so they stay neutral. A string
                # equal to a name is how dynamic lookups spell it.
                self.strings.add(node.value)
        self._owners: dict[int, tuple[frozenset[str], bool]] = {}

    def owners(self, line: int) -> tuple[frozenset[str], bool]:
        if line not in self._owners:
            self._owners[line] = attribute(self.source, self.path, {line})
        return self._owners[line]


def resolve(
    repo, base: str, head: str | None, query: Query, unresolved: dict[str, set[str]]
) -> dict[str, set[str]]:
    """path -> names from `unresolved` that are new and reached only through
    changed code the record knows, tests, or imports. Never raises: a file we
    cannot read or parse resolves nothing."""
    changed_py: dict[str, _Refs] = {}
    other_text: list[str] = []
    for f in query.files:
        if f.proxy:
            continue
        text = _read(repo, head, f.path) if head else None
        if text is None:
            continue
        if f.path.startswith(TEST_ROOTS):
            # Tests route by their own claims; a string there is a monkeypatch
            # target, not a lookup production code does.
            continue
        if f.path.endswith(".py"):
            try:
                changed_py[f.path] = _Refs(f.path, text)
            except SyntaxError:
                return {}
        elif not (
            f.path.endswith(INERT_TEXT_SUFFIXES) or f.path.startswith(INERT_TEXT_ROOTS)
        ):
            other_text.append(text)

    # New: absent from the file at base. A file absent at base is all new.
    candidates: dict[str, set[str]] = {}
    for path, names in unresolved.items():
        if path not in changed_py:
            continue
        before = _read(repo, base, path)
        try:
            existed = names_of(before, path) if before is not None else frozenset()
        except Exception:
            continue
        # A new file's module body is new too; it is reached only by import,
        # which references nothing, so it resolves once nothing else blocks.
        new = {
            n
            for n in names
            if n not in existed
            and (
                "<" not in _leaf(n)
                or (n == "<module>" and before is None)
                or ".<locals>." in n
            )
        }
        if new:
            candidates[path] = new

    # Changed names the record knows, per file: the evidence a new name may
    # borrow. Real functions only: a module or class body runs on import, so
    # a reference there is reached by every importer.
    known = {
        f.path: set(f.names) - unresolved.get(f.path, set()) - set(f.import_time)
        for f in query.files
        if not f.proxy
    }

    # Where each candidate is referenced, as (path, owners). None means a
    # reference we cannot place, which blocks.
    uses: dict[tuple[str, str], list[tuple[str, frozenset[str]]] | None] = {}
    # A lambda or comprehension runs only when the function holding it does,
    # and nothing can name it, so it follows its enclosing function. A dunder
    # follows its class, and only a new class built nowhere but where it is
    # named can vouch for it; any other class's instances may be anywhere.
    nested: dict[tuple[str, str], str] = {}
    sealed: dict[tuple[str, str], bool] = {}
    for path, names in candidates.items():
        refs = changed_py[path]
        new = frozenset(names)
        for name in new:
            if "<" in _leaf(name) and name != "<module>":
                nested[(path, name)] = name.rsplit(".<locals>.", 1)[0]
                names.discard(name)
                continue
            cls = _dunder_class(name, refs.import_time)
            if cls is None:
                continue
            names.discard(name)
            if cls not in new:
                continue
            if (path, cls) not in sealed:
                up = _ancestors(refs.tree, cls)
                sealed[(path, cls)] = up is not None and not (
                    up and _scanned(repo, head, up)
                )
            if sealed[(path, cls)]:
                nested[(path, name)] = cls
    for path, names in candidates.items():
        for name in names:
            leaf = _leaf(name)
            found: list[tuple[str, frozenset[str]]] | None = []
            if any(leaf in t for t in other_text):
                found = None
            for ref_path, refs in changed_py.items():
                if found is None:
                    break
                if leaf in refs.strings:
                    found = None
                    break
                for line, defs in refs.lines.get(leaf, []):
                    owners, residue = refs.owners(line)
                    if residue:
                        found = None
                        break
                    if defs:
                        owners = frozenset(o for o in owners if _leaf(o) not in defs)
                    found.append((ref_path, owners))
            uses[(path, name)] = found

    # Fixpoint: a reference is fine when an owner is a known changed name of
    # its file or a resolved candidate, and not import-time code either way,
    # which every importer runs. The definition's own body names itself
    # (recursion), which is fine too. A following name resolves with its
    # parent in the same loop: a helper only a new __init__ calls needs the
    # __init__ resolved first, and a lambda inside a lambda follows a parent
    # that is itself nested.
    resolved: set[tuple[str, str]] = set()
    changed = True
    while changed:
        changed = False
        for key, parent in nested.items():
            if key not in resolved and (
                (key[0], parent) in resolved or parent in known.get(key[0], set())
            ):
                resolved.add(key)
                changed = True
        for key, refs in uses.items():
            if key in resolved or refs is None:
                continue
            path, name = key
            ok = True
            for ref_path, owners in refs:
                if name in owners and ref_path == path:
                    continue
                if owners & known.get(ref_path, set()):
                    continue
                at_import = changed_py[ref_path].import_time
                if any(
                    (ref_path, o) in resolved and o not in at_import for o in owners
                ):
                    continue
                ok = False
                break
            if ok:
                resolved.add(key)
                changed = True

    out: dict[str, set[str]] = {}
    for path, name in resolved:
        out.setdefault(path, set()).add(name)
    return out
