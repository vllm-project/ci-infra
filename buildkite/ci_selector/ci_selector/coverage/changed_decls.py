# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Turn a declaration or op-registration diff into the kernels behind it.

`changed_kernels.py` places a changed line inside the definition it belongs
to. Two kinds of file hold no definitions to place it in, and both sat at the
whole-file reading, where they hold every step on the image:

  csrc/ops.h               `void reshape_and_cache(Tensor& key, ...);`
  csrc/torch_bindings.cpp  `ops.def("reshape_and_cache(Tensor key, ...)");`
                           `ops.impl("reshape_and_cache", TORCH_BOX(&reshape_and_cache));`

Every piece of that chain shares one name, and the name leads to the host
function that launches the kernel. So a change made only of such lines
narrows to the kernels those host functions launch:

    changed statement             name it contributes
    ---------------------------------------------------------------------
    a function prototype          the function it declares
    `<lib>.def("op(...)" ...)`    the functions `<lib>.impl("op", &fn)` or
                                  `<lib>.def("op", &fn)` bind to the op
    `<lib>.impl("op", &fn)`       fn
    a comment, blank, #include    nothing
    anything else                 THE WHOLE FILE

and each name resolves to the kernels its host definition launches in a file
the image build compiled (`changed_kernels.host_reach`).

What such a line can break, and why the narrowing still holds:

  - a prototype that no longer matches its definition: the build fails, and
    the image build runs on every PR
  - a schema string that does not parse: `vllm._C` fails to load in every
    process, so any one step that runs the op fails. The caller keeps the
    whole-file reading unless at least one row launched these kernels
  - a schema that disagrees with the C++ signature: every call to the op
    fails, which is exactly the steps that launch its kernels

Every fallback widens. A name with no host definition in a compiled file, a
host function that launches nothing this can name, an op whose binding is
not found: the whole file, as before this module existed.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import regex as re

from .changed_kernels import (
    HOST,
    KERNEL,
    MACRO,
    _inert_line,
    _innermost,
    _signature_name,
    changed_line_numbers,
    host_reach,
    parse,
    strip_comments_and_strings,
)

# `ops.def("name(...)"`, `m.impl("name.overload", ...`: the op a registration
# statement is about. Read off the raw text, since stripping blanks strings.
_REGISTRATION = re.compile(r'^\s*\w+\s*\.\s*(def|impl)\s*\(\s*"(\w+)(?:\.\w+)?["(]')
# The function an op is bound to: `.impl("op", TORCH_BOX(&fn))`,
# `.impl("op", torch::kCUDA, &ns::fn)`, `.def("op", &fn)`.
_BINDING = re.compile(
    r'\.\s*(?:impl|def)\s*\(\s*"(\w+)(?:\.\w+)?"\s*,[^;]*?&\s*((?:\w+::)*\w+)'
)


@dataclass(frozen=True)
class Declared:
    """What one file's diff declares or registers, or why it cannot say."""

    #: C++ functions whose prototypes or bindings changed
    functions: frozenset[str]
    #: ops whose `def` changed, to resolve through their bindings
    ops: frozenset[str]
    #: empty when every changed line was read; otherwise the whole file
    why: str = ""
    touched: frozenset[str] = frozenset()

    @property
    def readable(self) -> bool:
        return not self.why and bool(self.functions or self.ops)


def _whole(why: str) -> Declared:
    return Declared(frozenset(), frozenset(), why)


def _statement(clean: str, line: int) -> tuple[int, int] | None:
    """[start, end) of the statement holding `line`, ending at its `;`, or
    None when the line is not inside a plain statement."""
    lines = clean.split("\n")
    if line - 1 >= len(lines):
        return None
    line_start = sum(len(x) + 1 for x in lines[: line - 1])
    text = lines[line - 1]
    offset = line_start + (len(text) - len(text.lstrip()))
    start = max(clean.rfind(c, 0, offset) for c in ";{}") + 1
    end = clean.find(";", offset)
    if end < 0 or "{" in clean[start:end] or "}" in clean[start:end]:
        return None
    return start, end


def _prototype_name(stmt: str) -> str | None:
    """The function a namespace-scope prototype declares, or None."""
    s = " ".join(stmt.split())
    if not s or "=" in s.replace("==", "") or s.startswith(("using ", "typedef ")):
        return None
    name = _signature_name(s)
    if name is None:
        return None
    head = s[: s.find(name)].strip()
    # A prototype has a return type in front; `foo(x)` alone is a call or a
    # macro invocation.
    return name if head else None


def declared_text(
    base_text: str | None, head_text: str | None, diff_u0: str
) -> Declared:
    """Read one file's diff as declarations and registrations, or decline."""
    old_lines, new_lines = changed_line_numbers(diff_u0)
    if not old_lines and not new_lines:
        return _whole("no hunks in the diff")
    functions: set[str] = set()
    ops: set[str] = set()
    for text, lines in ((base_text, old_lines), (head_text, new_lines)):
        if not lines:
            continue
        if text is None:
            return _whole("one side of the file could not be read")
        clean = strip_comments_and_strings(text)
        clean_lines = clean.splitlines()
        entities = parse(text)
        seen: set[tuple[int, int]] = set()
        for line in sorted(lines):
            if _inert_line(clean_lines, line):
                continue
            span = _statement(clean, line)
            if span is None:
                return _whole(f"line {line} is not a declaration or registration")
            if span in seen:
                continue
            seen.add(span)
            # Comments before the statement fall inside its span; strings
            # stay, since a registration names its op in one.
            raw = strip_line_comments(text[span[0] : span[1]])
            reg = _REGISTRATION.match(raw)
            if reg:
                verb, op = reg.groups()
                bound = _BINDING.search(raw + ";")
                if bound:
                    functions.add(bound.group(2).split("::")[-1])
                elif verb == "def":
                    ops.add(op)
                else:
                    return _whole(f"line {line} registers {op} to nothing nameable")
                continue
            # A prototype sits at namespace scope. Inside a definition the same
            # shape is a call, which is `changed_kernels`' business.
            inside = _innermost(entities, line)
            if inside is not None and inside.kind in (HOST, KERNEL, MACRO):
                return _whole(f"line {line} is inside {inside.name}")
            name = _prototype_name(clean[span[0] : span[1]])
            if name is None:
                return _whole(f"line {line} is not a declaration or registration")
            functions.add(name)
    if not functions and not ops:
        return _whole("only comments, blanks or includes changed")
    return Declared(
        frozenset(functions), frozenset(ops), touched=frozenset(functions | ops)
    )


def bindings_of(ops: Iterable[str], texts: Iterable[str]) -> dict[str, set[str]]:
    """op -> the C++ functions bound to it anywhere in `texts`."""
    wanted = set(ops)
    out: dict[str, set[str]] = {}
    for text in texts:
        for op, fn in _BINDING.findall(strip_line_comments(text)):
            if op in wanted:
                out.setdefault(op, set()).add(fn.split("::")[-1])
    return out


def strip_line_comments(text: str) -> str:
    """Comments gone, strings kept: a binding names its op in a string."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def kernels_behind(
    functions: Iterable[str],
    definitions: Callable[[str], dict[str, str]],
    known_kernels: Callable[[str], frozenset[str]],
) -> tuple[frozenset[str], str]:
    """The kernel names these functions launch, or (empty, why) when one of
    them cannot be followed to a kernel.

    `definitions(name)` is {path: text} for the compiled files that may define
    `name`; `known_kernels(path)` the kernel names the map compiled from it."""
    kernels: set[str] = set()
    for name in sorted(set(functions)):
        reached: set[str] = set()
        for path, text in sorted(definitions(name).items()):
            entities = parse(text)
            own = {e.name for e in entities if e.kind == KERNEL}
            if name in own:
                reached.add(name)
                continue
            if not any(e.name == name and e.kind == HOST for e in entities):
                continue
            reach = host_reach(entities, own | set(known_kernels(path)))
            reached |= reach.get(name, frozenset())
        if not reached:
            return frozenset(), f"{name} launches no kernel this can name"
        kernels |= reached
    return frozenset(kernels), ""


# ---- reading from git ------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        errors="replace",
    )


def _show(repo: Path, ref: str | None, path: str) -> str | None:
    if ref is None:
        try:
            return (repo / path).read_text(errors="replace")
        except OSError:
            return None
    out = _git(repo, "show", f"{ref}:{path}")
    return out.stdout if out.returncode == 0 else None


def _grep(repo: Path, ref: str | None, pattern: str, paths: Iterable[str]) -> list[str]:
    """Files among `paths` whose text at `ref` matches `pattern` (PCRE)."""
    paths = sorted(set(paths))
    if not paths:
        return []
    out = _git(repo, "grep", "-l", "-P", pattern, *([ref] if ref else []), "--", *paths)
    prefix = f"{ref}:" if ref else ""
    return [p[len(prefix) :] if p.startswith(prefix) else p for p in out.stdout.split()]


@dataclass(frozen=True)
class DeclAttribution:
    path: str
    #: kernel names the change is attributed to; empty means the whole file
    kernels: frozenset[str]
    why: str

    @property
    def narrowed(self) -> bool:
        return bool(self.kernels)


def attribute(
    repo: Path,
    base: str,
    head: str | None,
    path: str,
    sources: frozenset[str],
    known_kernels: Callable[[str], frozenset[str]],
) -> DeclAttribution:
    """Read `path`'s diff as declarations and registrations and follow the
    names into `sources`, the files the image build compiled kernels from."""
    diff = _git(repo, "diff", "-U0", base, *([head] if head else []), "--", path)
    if diff.returncode != 0:
        return DeclAttribution(path, frozenset(), "git diff failed")
    declared = declared_text(
        _show(repo, base, path), _show(repo, head, path), diff.stdout
    )
    if not declared.readable:
        return DeclAttribution(path, frozenset(), declared.why)
    functions = set(declared.functions)
    for ref in (base, head):
        if declared.ops:
            # A def names an op; its binding is usually beside it, else in
            # another registration file.
            files = _grep(
                repo,
                ref,
                "|".join(rf'"{re.escape(op)}[".]' for op in sorted(declared.ops)),
                ["csrc"],
            )
            bound = bindings_of(
                declared.ops, (_show(repo, ref, f) or "" for f in files)
            )
            missing = declared.ops - set(bound)
            if missing and ref == head:
                return DeclAttribution(
                    path,
                    frozenset(),
                    f"op {sorted(missing)[0]} is bound to nothing found",
                )
            for fns in bound.values():
                functions |= fns

    def definitions(name: str) -> dict[str, str]:
        found: dict[str, str] = {}
        for ref in (base, head):
            for f in _grep(repo, ref, rf"\b{re.escape(name)}\s*\(", sources):
                text = _show(repo, ref, f)
                if text is not None:
                    found.setdefault(f, text)
        return found

    kernels, why = kernels_behind(functions, definitions, known_kernels)
    if not kernels:
        return DeclAttribution(path, frozenset(), why)
    return DeclAttribution(
        path, kernels, "declares " + ", ".join(sorted(declared.touched))
    )
