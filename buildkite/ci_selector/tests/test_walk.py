# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""The graph's AST walkers must see exactly what the stdlib's see, in the
same order: edges, literal sets and the `nodes[0]` module read all depend on
it. Checked over this package's own source plus the syntax it lacks."""

import ast
import warnings
from pathlib import Path

import ci_selector
import pytest
from ci_selector.codemap.graph import walk

SOURCES = sorted(Path(ci_selector.__file__).parent.rglob("*.py"))

# Syntax the package itself does not use.
SNIPPET = """
match point:
    case [x, *rest] if x:
        pass
    case {"k": v, **kw}:
        pass
    case Point(x=0) | None:
        pass
try:
    pass
except* (ValueError, TypeError) as group:
    pass
type Alias[T: int, *Ts, **P] = list[T]
def generic[T](x: T) -> T:
    global g
    return (y := x)
async def coroutine():
    return [z async for z in aiter() if await z]
lambda *a, b=1, **k: f"{a!r:>{b}}"
"""


@pytest.fixture(scope="module")
def trees():
    out = [ast.parse(p.read_text(), filename=str(p)) for p in SOURCES]
    out.append(ast.parse(SNIPPET))
    # Nodes built by hand, with fields left unset.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        out.append(ast.Module(body=[ast.Expr(ast.BinOp())]))
    return out


def test_walk_yields_the_stdlib_nodes_in_order(trees):
    for tree in trees:
        assert list(map(id, walk.walk(tree))) == list(map(id, ast.walk(tree)))


def test_child_nodes_match_the_stdlib(trees):
    for tree in trees:
        for node in ast.walk(tree):
            assert list(map(id, walk.iter_child_nodes(node))) == list(
                map(id, ast.iter_child_nodes(node))
            )


def _recorder(base):
    """A visitor in the shape the graph uses: some typed handlers, one that
    recurses by hand, an alias, and a `visit` override, with every call
    recorded."""

    class Recorder(base):
        def __init__(self):
            self.seen = []

        def visit(self, node):
            self.seen.append(("visit", id(node)))
            return super().visit(node)

        def visit_FunctionDef(self, node):
            self.seen.append(("def", id(node)))
            self.generic_visit(node)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            self.seen.append(("call", id(node)))
            for arg in node.args:
                self.visit(arg)

        def visit_Name(self, node):
            self.seen.append(("name", id(node)))

    class Narrower(Recorder):
        def visit_Name(self, node):
            self.seen.append(("narrow", id(node)))

    return Recorder, Narrower


@pytest.mark.parametrize("which", [0, 1], ids=["visitor", "its-subclass"])
def test_the_visitor_dispatches_as_the_stdlib_does(trees, which):
    ours, theirs = _recorder(walk.NodeVisitor)[which], _recorder(ast.NodeVisitor)[which]
    for tree in trees:
        a, b = ours(), theirs()
        a.visit(tree)
        b.visit(tree)
        assert a.seen == b.seen


def test_the_bare_visitor_needs_no_subclass(trees):
    for tree in trees:
        walk.NodeVisitor().visit(tree)
