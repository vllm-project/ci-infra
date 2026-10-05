# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Steps that test vLLM from outside, routed by the API a diff breaks.

A step whose pytest target exists only inside its own image runs another
project's tests against this checkout. Today that is the Ascend NPU job, which
runs vllm-ascend's interface-compatibility check over the vLLM names
vllm-ascend uses. Nothing here imports those tests, so no edge reaches the
step, and it has no row. It was skipped on 43 of 64 shadowed PRs, two of which
broke it: vllm#53558 removed kv_cache_utils functions vllm-ascend calls (17
breaks), and vllm#58997 removed postprocess_mamba_all.

What reaches such a step is the surface vLLM lets other code import, so a
vllm/ module whose diff breaks that surface selects it. A break is:

- a module- or class-level def, class or assignment gone at head: removed,
  renamed, or its module deleted or moved. A name an import binds at head is
  still there, which is how a moved class stays importable from its old home;
- a function that no longer takes its old calls: its old positional
  parameters are not a prefix of the new ones, a parameter is required that
  was not, a keyword-only parameter is gone, or it lost *args or **kwargs;
- a class that gains a required field, an annotation with no default, which
  every dataclass or NamedTuple construction now has to pass.

Private names count, since a plugin patches those too: vllm#54442 added a
required parameter to RejectionSampler._verify. Device kernels do not
(@triton.jit and the like), as only vLLM's own wrappers launch them. A name
bound twice in one scope, an overload or a property setter, is checked for
presence only. A side that cannot be read or parsed counts as a break, since
its surface is unknown.
"""

from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass
from pathlib import Path

# A decorator's last dotted part that marks a device kernel: triton.jit,
# triton.autotune, triton.heuristics, gluon.jit, cute.jit, cute.kernel. A name
# ending in `_jit` (tilelang_jit) is a local wrapper of one.
KERNEL_DECORATORS = frozenset({"jit", "autotune", "heuristics", "kernel"})


@dataclass(frozen=True)
class Signature:
    positional: tuple[str, ...]
    # How many leading positional parameters have no default.
    required: int
    keyword: frozenset[str]
    required_keyword: frozenset[str]
    varargs: bool
    varkw: bool

    @classmethod
    def of(cls, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> Signature:
        a = fn.args
        positional = tuple(p.arg for p in a.posonlyargs + a.args)
        return cls(
            positional=positional,
            required=len(positional) - len(a.defaults),
            keyword=frozenset(p.arg for p in a.kwonlyargs),
            required_keyword=frozenset(
                p.arg for p, d in zip(a.kwonlyargs, a.kw_defaults) if d is None
            ),
            varargs=a.vararg is not None,
            varkw=a.kwarg is not None,
        )

    def rejects(self, old: Signature) -> str | None:
        """Why a call `old` accepted may fail against this one, or None."""
        if old.varargs and not self.varargs:
            return "lost *args"
        if old.varkw and not self.varkw:
            return "lost **kwargs"
        if self.positional[: len(old.positional)] != old.positional:
            return "positional parameters changed"
        if self.required > old.required:
            now = self.positional[old.required : self.required]
            return f"now requires {', '.join(now)}"
        added = self.required_keyword - old.required_keyword
        if added:
            return f"now requires {', '.join(sorted(added))}"
        gone = old.keyword - self.keyword - set(self.positional)
        if gone and not self.varkw:
            return f"dropped {', '.join(sorted(gone))}"
        return None


@dataclass(frozen=True)
class Binding:
    kind: str  # "def" | "class" | "name"
    # A def's signature, or a class's annotated fields with no default. None
    # for a name bound twice, which is checked for presence only.
    shape: Signature | frozenset[str] | None = None


def _is_kernel(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for d in fn.decorator_list:
        last = ast.unparse(d.func if isinstance(d, ast.Call) else d).rsplit(".", 1)[-1]
        if last in KERNEL_DECORATORS or last.endswith("_jit"):
            return True
    return False


def _required_fields(cls: ast.ClassDef) -> frozenset[str]:
    return frozenset(
        n.target.id
        for n in cls.body
        if isinstance(n, ast.AnnAssign)
        and isinstance(n.target, ast.Name)
        and n.value is None
        and "ClassVar" not in ast.unparse(n.annotation)
    )


def _target_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for t in target.elts for n in _target_names(t)]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


def surface(tree: ast.Module) -> tuple[dict[str, Binding], set[str]]:
    """(qualified name -> binding, the qualified names imports bind).

    Module and class bodies only, including the branches of an `if` or `try`
    there, which bind names in that scope as surely as straight-line code.
    """
    out: dict[str, Binding] = {}
    imported: set[str] = set()

    def bind(name: str, binding: Binding) -> None:
        out[name] = Binding(binding.kind) if name in out else binding

    def walk(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not _is_kernel(node):
                    bind(prefix + node.name, Binding("def", Signature.of(node)))
            elif isinstance(node, ast.ClassDef):
                bind(prefix + node.name, Binding("class", _required_fields(node)))
                walk(node.body, f"{prefix}{node.name}.")
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    for name in _target_names(t):
                        bind(prefix + name, Binding("name"))
            elif isinstance(node, ast.AnnAssign):
                for name in _target_names(node.target):
                    bind(prefix + name, Binding("name"))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".", 1)[0]
                    imported.add(prefix + name)
            elif isinstance(node, ast.If):
                walk(node.body, prefix)
                walk(node.orelse, prefix)
            elif isinstance(node, ast.Try):
                walk(node.body, prefix)
                for handler in node.handlers:
                    walk(handler.body, prefix)
                walk(node.orelse, prefix)
                walk(node.finalbody, prefix)

    walk(tree.body, "")
    return out, imported


def _owners(name: str) -> list[str]:
    """The classes enclosing a qualified name, outermost first."""
    parts = name.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts))]


def breaks(before: str, after: str | None) -> list[str]:
    """What `after` breaks of `before`'s surface, each as `name: why`. `after`
    is None when the module is gone. Raises SyntaxError on either side."""
    old, _ = surface(ast.parse(before))
    new, imported = surface(ast.parse(after)) if after is not None else ({}, set())
    # An imported class is defined elsewhere now, so its members are not ours
    # to see.
    gone = {
        n
        for n in old
        if n not in new
        and n not in imported
        and not any(o in imported for o in _owners(n))
    }
    out: list[str] = []
    for name, was in old.items():
        if name in gone:
            # A member of a removed class is reported as that class.
            if not any(o in gone for o in _owners(name)):
                out.append(f"{name}: removed")
            continue
        now = new.get(name)
        if now is None or now.kind != was.kind or None in (was.shape, now.shape):
            continue
        if isinstance(now.shape, Signature):
            why = now.shape.rejects(was.shape)
            if why:
                out.append(f"{name}: {why}")
        elif now.shape - was.shape:
            added = ", ".join(sorted(now.shape - was.shape))
            out.append(f"{name}: new required field {added}")
    return out


def _show(repo: Path, ref: str, path: str) -> str | None:
    r = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
    )
    return r.stdout if r.returncode == 0 else None


def diff_breaks(repo: Path, path: str, ctx) -> list[str]:
    """The API breaks one changed file makes, empty for none or for a file
    outside vllm/. `ctx` is the DiffContext; without one there is no diff."""
    if ctx is None or not path.startswith("vllm/") or not path.endswith(".py"):
        return []
    status = ctx.status.get(path)
    # Nothing was importable from this path at base. A rename answers through
    # its old path, which the diff carries as deleted.
    if status in ("A", "R", "C"):
        return []
    before = _show(repo, ctx.base, path)
    after = None if status == "D" else _show(repo, ctx.head, path)
    if before is None or (after is None and status != "D"):
        return [f"{path}: unreadable at one end, so its API is unknown"]
    try:
        return breaks(before, after)
    except SyntaxError:
        return [f"{path}: does not parse at one end, so its API is unknown"]


def outside_steps(pdata) -> set[str]:
    """A pipeline's steps running a test that exists only inside their image.
    Preflight warns on the same steps and never forces them (guards.py)."""
    return {sid for sid, st in pdata.targets.items() if st.container_tests}
