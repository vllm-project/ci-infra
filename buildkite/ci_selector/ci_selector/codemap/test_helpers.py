# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""A changed test helper routes by the symbols that changed, not the file.

Nothing under tests/ is recorded, so a helper there has no function evidence
and its file-level reach decides alone. For tests/utils.py that reach is
nearly every test: tests/conftest.py imports three unrelated helpers from it,
and every test depends on conftest. vllm#56740 changed one method of
RemoteVLLMServer and 297 steps were selected, 238 of them for that file alone.

Code that can run a changed helper names it. So the claim follows names:

  1. the top-level definitions the diff touched, on either side;
  2. everything else in the helper naming one of those, to a fixpoint (a
     subclass inherits the change, a wrapper calls it, an alias rebinds it);
  3. the files importing the helper that name any of them, which seed
  4. the ordinary reverse closure from there.

An importer naming none of them is not followed, which is what drops
conftest for vllm#56740: 1556 test files become 132.

Declines, leaving the file-level graph claim, on what it cannot place: a
changed module-level statement that binds no name (it runs on import, so
every importer runs it), an affected name used by a module-level statement
that binds none, a star import of the helper, a parse or git failure, or
no seed at all.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

from .repo import is_test_file

TEST_ROOT = "tests/"
# A conftest routes by the fixtures it changed (see route); an __init__ runs
# on every import of its package.
NOT_HELPERS = ("conftest.py", "__init__.py")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def is_conftest(path: str) -> bool:
    return path.startswith(TEST_ROOT) and path.rsplit("/", 1)[-1] == "conftest.py"


def _fixture_kinds(tree: ast.Module) -> tuple[set[str], set[str], set[str]]:
    """(fixtures, names bound by import, names that reach every test) in a
    conftest. A test reaches a conftest only through fixtures it names, or
    through ones the conftest imports from elsewhere; a hook, pytest_plugins
    and an autouse fixture reach every test beneath it."""
    fixtures: set[str] = set()
    imported: set[str] = set()
    everywhere: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decorators = [ast.unparse(d) for d in stmt.decorator_list]
            if stmt.name.startswith("pytest_"):
                everywhere.add(stmt.name)
            elif any("fixture" in d for d in decorators):
                fixtures.add(stmt.name)
                if any("autouse" in d for d in decorators):
                    everywhere.add(stmt.name)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            imported |= _binds(stmt)
        elif "pytest_plugins" in _binds(stmt):
            everywhere.add("pytest_plugins")
    return fixtures, imported, everywhere


def is_helper(path: str) -> bool:
    return (
        path.startswith(TEST_ROOT)
        and path.endswith(".py")
        and not is_test_file(path)
        and path.rsplit("/", 1)[-1] not in NOT_HELPERS
    )


def _git(repo: Path, *args: str) -> str | None:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )
    return proc.stdout if proc.returncode == 0 else None


def _changed_lines(diff: str) -> tuple[set[int], set[int]]:
    """(base lines, head lines) the -U0 diff touches. A pure insertion has no
    base line, a pure deletion no head line."""
    base: set[int] = set()
    head: set[int] = set()
    for line in diff.splitlines():
        m = _HUNK.match(line)
        if not m:
            continue
        b0, bn = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
        h0, hn = int(m.group(3)), int(m.group(4) if m.group(4) is not None else 1)
        base.update(range(b0, b0 + bn))
        head.update(range(h0, h0 + hn))
    return base, head


def _binds(stmt: ast.stmt) -> set[str]:
    """Module-level names a top-level statement binds."""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {stmt.name}
    out: set[str] = set()
    for node in ast.walk(stmt):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            out.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                out.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
    return out


def _span(stmt: ast.stmt) -> range:
    start = min(
        [stmt.lineno] + [d.lineno for d in getattr(stmt, "decorator_list", [])]
    )
    return range(start, (stmt.end_lineno or stmt.lineno) + 1)


def _names_used(stmt: ast.stmt) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(stmt):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
    return out


# Decorators that only wrap the function they decorate: applying one at
# import touches nothing else, so it reaches only what names the function.
# Anything else (a registry, a plugin hook) may act on every importer.
_WRAP_ONLY_DECORATORS = frozenset(
    {
        "pytest.fixture",
        "pytest.mark.parametrize",
        "pytest.mark.skipif",
        "pytest.mark.skip",
        "pytest.mark.slow",
        "staticmethod",
        "classmethod",
        "property",
        "abstractmethod",
        "abc.abstractmethod",
        "functools.cache",
        "functools.lru_cache",
        "functools.cached_property",
        "functools.wraps",
        "cache",
        "lru_cache",
        "cached_property",
        "wraps",
        "dataclass",
        "dataclasses.dataclass",
        "overload",
        "typing.overload",
        "contextmanager",
        "contextlib.contextmanager",
    }
)


def _applied(decorator: ast.expr) -> ast.expr:
    """A decorator as the code it runs at import: nothing beyond its own
    arguments for a wrap-only one, otherwise a call, so `@register` counts as
    running code as `@register()` does."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    if ast.unparse(target) in _WRAP_ONLY_DECORATORS:
        return decorator
    return ast.copy_location(ast.Call(func=decorator, args=[], keywords=[]), decorator)


def _import_time(stmt: ast.stmt) -> list[ast.AST]:
    """The parts of a top-level statement that run when the module is
    imported: all of a plain statement; a def's decorators and defaults, not
    its body; a class's bases, keywords and decorators, and its body by the
    same rule."""
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        args = stmt.args
        return [
            *(_applied(d) for d in stmt.decorator_list),
            *args.defaults,
            *(d for d in args.kw_defaults if d is not None),
        ]
    if isinstance(stmt, ast.ClassDef):
        out: list[ast.AST] = [
            *(_applied(d) for d in stmt.decorator_list),
            *stmt.bases,
            *stmt.keywords,
        ]
        for item in stmt.body:
            out += _import_time(item)
        return out
    return [stmt]


def _runs_code(node: ast.AST) -> bool:
    return any(
        isinstance(n, (ast.Call, ast.Import, ast.ImportFrom)) for n in ast.walk(node)
    )


def _lines_of(node: ast.AST) -> range:
    return range(node.lineno, (node.end_lineno or node.lineno) + 1)


def changed_names(tree: ast.Module, lines: set[int]) -> set[str] | None:
    """Top-level names whose statements hold a changed line. None when a
    changed line is in code that runs on import and runs anything (an
    import, a call, a decorator): every importer runs it, whatever names it
    binds. A changed `m = importlib.import_module(...)` broke all 45
    importers of a helper in a probe, and routing by `m` selected none. None
    too when a changed statement binds nothing. A line no statement holds is
    a comment or blank line between them, and changes nothing."""
    out: set[str] = set()
    for stmt in tree.body:
        if not lines.intersection(_span(stmt)):
            continue
        if any(
            lines.intersection(_lines_of(n)) and _runs_code(n)
            for n in _import_time(stmt)
            if hasattr(n, "lineno")
        ):
            return None
        bound = _binds(stmt)
        if not bound:
            return None
        out |= bound
    return out


def affected_names(trees: list[ast.Module], changed: set[str]) -> set[str] | None:
    """`changed` plus every top-level name whose statement names one of them,
    to a fixpoint, on every side given. None when a statement binding nothing
    names one: that is a use on import."""
    affected = set(changed)
    grew = True
    while grew:
        grew = False
        for tree in trees:
            for stmt in tree.body:
                if not (_names_used(stmt) & affected):
                    continue
                # Import-time code calling an affected name (a decorator, a
                # default, `SERVER = make_server()`) runs it for every importer.
                if any(
                    _names_used(n) & affected and _runs_code(n)
                    for n in _import_time(stmt)
                ):
                    return None
                bound = _binds(stmt)
                if not bound:
                    return None
                if not bound <= affected:
                    affected |= bound
                    grew = True
    return affected


def _grep(repo: Path, base: str, files: list[str], *flags: str) -> set[str] | None:
    """Files at `base` matching, or None when git fails. Exit 1 is no match."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "grep", "-l", *flags, base, "--", *files],
        capture_output=True,
        text=True,
    )
    if proc.returncode not in (0, 1):
        return None
    prefix = f"{base}:"
    return {line.removeprefix(prefix) for line in proc.stdout.splitlines() if line}


def seeds(repo: Path, base: str, importers: set[str], names: set[str]) -> set[str] | None:
    """The importers naming any of `names` as a whole word, strings included:
    a getattr spells the name too. A star import need not spell where a name
    came from, so an importer holding one seeds as well."""
    if not importers or not names:
        return set()
    files = sorted(importers)
    pattern = "|".join(sorted(re.escape(n) for n in names))
    named = _grep(repo, base, files, "-w", "-E", pattern)
    star = _grep(repo, base, files, "-E", r"import[[:space:]]+\*")
    if named is None or star is None:
        return None
    return named | star


def route(state, path: str, ctx) -> tuple[set[str], set[str], str] | None:
    """(test files, script files, detail) for a modified helper, or None to
    leave it to the file-level graph claim."""
    conftest = is_conftest(path)
    if ctx is None or ctx.status.get(path) != "M" or not (is_helper(path) or conftest):
        return None
    repo = Path(state.repo)
    diff = _git(repo, "diff", "-U0", "--no-color", ctx.base, ctx.head, "--", path)
    before = _git(repo, "show", f"{ctx.base}:{path}")
    after = _git(repo, "show", f"{ctx.head}:{path}")
    if diff is None or before is None or after is None:
        return None
    try:
        base_tree, head_tree = ast.parse(before), ast.parse(after)
    except SyntaxError:
        return None
    base_lines, head_lines = _changed_lines(diff)
    old = changed_names(base_tree, base_lines)
    new = changed_names(head_tree, head_lines)
    if old is None or new is None:
        return None
    changed = old | new
    if not changed:
        # Comments and blank lines only.
        return set(), set(), f"{path}: only comments or blank lines changed"
    affected = affected_names([base_tree, head_tree], changed)
    if affected is None:
        return None
    if conftest:
        # Only fixtures carry a conftest's change to a test: its other names
        # reach a test through a fixture that uses them, which the fixpoint
        # has already followed. vllm#58916 deleted an unused fixture from
        # tests/conftest.py and 182 steps came along.
        fixtures, imported, everywhere = set(), set(), set()
        for tree in (base_tree, head_tree):
            f, i, e = _fixture_kinds(tree)
            fixtures |= f
            imported |= i
            everywhere |= e
        if affected & everywhere:
            return None
        affected = affected & (fixtures | imported)
        if not affected:
            return set(), set(), f"{path}: changed no fixture {sorted(changed)[:3]}"
    graph = state.full.graph
    importers = set(graph.reverse.get(path, ()))
    seeded = seeds(repo, ctx.base, importers, affected)
    if seeded is None:
        return None
    if not seeded:
        # Strings are searched too, so even a getattr would have matched.
        return set(), set(), f"{path}: no importer names {sorted(changed)[:3]}"
    closure = graph.reverse_closure(seeded)
    tests = {f for f in closure if is_test_file(f)}
    scripts = {f for f in closure if f.startswith(("examples/", "benchmarks/"))}
    detail = (
        f"{path} changed {sorted(changed)[:3]}; {len(seeded)} of its "
        f"{len(importers)} importers name them or their users in the file, "
        f"reaching {len(tests)} test files"
    )
    return tests, scripts, detail
