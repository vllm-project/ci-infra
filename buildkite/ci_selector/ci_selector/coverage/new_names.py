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

A reference we cannot place resolves nothing: one at module level (a registry,
a decorator argument), a string equal to the name, a mention in a changed
non-Python file, or one owned by a function the diff did not change (a
different symbol with the same name). A name that existed at base but no row
ever recorded is out of scope; that is missing data, not new code.

vllm#58684 added four private helpers, all called only from the changed
`plan` and `build`; vllm#58664 added a wrapper only its new test calls. Both
kept nearly every step touching the file on the unknown names alone.
"""

from __future__ import annotations

import ast

from .changed_funcs import Query, _read, attribute, names_of

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


def _leaf(qualname: str) -> str:
    return qualname.rsplit(".", 1)[-1]


class _Refs:
    """One parse of a changed file: where each name is referenced, which
    strings it holds, and who owns a line. Owners are cached per line."""

    def __init__(self, path: str, source: str):
        self.path = path
        self.source = source
        self.lines: dict[str, list[int]] = {}
        self.strings: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name):
                self.lines.setdefault(node.id, []).append(node.lineno)
            elif isinstance(node, ast.Attribute):
                self.lines.setdefault(node.attr, []).append(node.lineno)
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
    # and nothing can name it, so it follows its enclosing function.
    nested: dict[tuple[str, str], str] = {}
    for path, names in candidates.items():
        for name in list(names):
            if "<" in _leaf(name) and name != "<module>":
                nested[(path, name)] = name.rsplit(".<locals>.", 1)[0]
                names.discard(name)
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
                for line in refs.lines.get(leaf, []):
                    owners, residue = refs.owners(line)
                    if residue:
                        found = None
                        break
                    found.append((ref_path, owners))
            uses[(path, name)] = found

    # Fixpoint: a reference is fine when an owner is a known changed name of
    # its file or a candidate already resolved. The definition's own body
    # names itself (recursion), which is fine too.
    resolved: set[tuple[str, str]] = set()
    changed = True
    while changed:
        changed = False
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
                if any((ref_path, o) in resolved for o in owners):
                    continue
                ok = False
                break
            if ok:
                resolved.add(key)
                changed = True

    for (path, name), parent in nested.items():
        if (path, parent) in resolved or parent in known.get(path, set()):
            resolved.add((path, name))

    out: dict[str, set[str]] = {}
    for path, name in resolved:
        out.setdefault(path, set()).add(name)
    return out
