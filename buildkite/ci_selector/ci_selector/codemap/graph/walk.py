# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""`ast.walk`, `ast.iter_child_nodes` and `ast.NodeVisitor`, yielding the same
nodes in the same order without the stdlib's nested generators and per-node
method lookup. A graph build visits tens of millions of nodes, so that
overhead was a sixth of it.
"""

from __future__ import annotations

import ast
from collections import deque
from collections.abc import Callable, Iterator

_AST = ast.AST


def iter_child_nodes(node: ast.AST) -> Iterator[ast.AST]:
    for name in node._fields:
        try:
            field = getattr(node, name)
        except AttributeError:
            continue
        if isinstance(field, _AST):
            yield field
        elif isinstance(field, list):
            for item in field:
                if isinstance(item, _AST):
                    yield item


def walk(node: ast.AST) -> Iterator[ast.AST]:
    todo = deque([node])
    pop, push = todo.popleft, todo.append
    while todo:
        node = pop()
        for name in node._fields:
            try:
                field = getattr(node, name)
            except AttributeError:
                continue
            if isinstance(field, _AST):
                push(field)
            elif isinstance(field, list):
                for item in field:
                    if isinstance(item, _AST):
                        push(item)
        yield node


class NodeVisitor(ast.NodeVisitor):
    """Dispatches as `ast.NodeVisitor` does, except that a handler is looked
    up on the class, once per node class: a `visit_*` set on an instance, or
    patched onto the class after its first visit, is not seen."""

    _methods: dict[type, Callable] = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._methods = {}

    def visit(self, node):
        method = self._methods.get(node.__class__)
        if method is None:
            cls = type(self)
            method = getattr(cls, "visit_" + node.__class__.__name__, cls.generic_visit)
            self._methods[node.__class__] = method
        return method(self, node)

    def generic_visit(self, node):
        for name in node._fields:
            try:
                value = getattr(node, name)
            except AttributeError:
                continue
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, _AST):
                        self.visit(item)
            elif isinstance(value, _AST):
                self.visit(value)
