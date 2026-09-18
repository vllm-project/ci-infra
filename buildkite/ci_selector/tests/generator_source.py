# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Reading facts out of the pipeline generator that it does not expose as names.

Most of what we need from the generator is importable, and importable facts are
not checked here: they are used directly, so they cannot drift. What is left is
the handful of values the generator writes as bare literals inside a function
body. Those have no name to import, so we read them out of its live source.

`inspect.getsource` on the imported object, never a stored copy. There is no
snapshot, no download and no network: the generator is a sibling package in
this repo, installed alongside us.

Every query here raises `LookupError` when the shape it looks for is gone, so
an upstream restructure fails loudly rather than returning an empty answer that
reads like agreement.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Callable


def _tree(func: Callable) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(func)))


def literals_in(func: Callable) -> set[str]:
    """Every non-empty string literal in a function."""
    return {
        node.value
        for node in ast.walk(_tree(func))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value
    }


def method_arg(func: Callable, method: str) -> str:
    """The literal argument of `<anything>.method("...")` inside a function."""
    for node in ast.walk(_tree(func)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == method
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            return node.args[0].value
    raise LookupError(f"{func.__name__}: no .{method}(<literal>) call")


def attr_assignment(func: Callable, attr: str) -> str:
    """The literal assigned to `<anything>.attr` inside a function."""
    for node in ast.walk(_tree(func)):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and any(
                isinstance(t, ast.Attribute) and t.attr == attr for t in node.targets
            )
        ):
            return node.value.value
    raise LookupError(f"{func.__name__}: nothing assigned to .{attr}")


# The name the mirror override dict is bound to in both reading functions.
# `step.mirror["amd"]` is an Attribute, not a Name, so the hardware key is
# excluded and only override keys are collected.
MIRROR_VAR = "amd"


def mirror_override_keys(*funcs: Callable) -> set[str]:
    """Every key the generator reads out of a `mirror.<hw>` block.

    No model to compare against: `Step.mirror` is typed `Dict[str, Any]`, so an
    unknown key is accepted and dropped in silence. These call sites are the
    whole schema.
    """
    keys: set[str] = set()
    for func in funcs:
        for node in ast.walk(_tree(func)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == MIRROR_VAR
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                keys.add(node.args[0].value)
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == MIRROR_VAR
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                keys.add(node.slice.value)
    return keys
