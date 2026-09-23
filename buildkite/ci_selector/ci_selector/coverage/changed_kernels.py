# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Turn a CUDA diff into the kernels it touched, per file.

The kernel record keys on symbols and the symbol map keys on files, so the
first reading of a changed `.cu` was every kernel compiled from it. A file
such as `cache_kernels.cu` holds a hundred kernel symbols launched by nearly
every model step, and a one-kernel fix there selected all of them. This is
the CUDA twin of `changed_funcs.py`: read both sides of the file, find the
definition each changed line falls in, and name the kernels it can affect.

    changed line falls in            attributed to
    ---------------------------------------------------------------------
    a __global__ definition          that kernel (template line included)
    a __device__ helper              the kernels in this file whose bodies
                                     reach it, transitively through other
                                     helpers in the file
    a host function                  the kernels it launches (<<<>>>,
                                     cudaLaunchKernel*, or a kernel it names)
    a comment, blank, #include       nothing
    anything else                    THE WHOLE FILE: namespace-scope
                                     constants, macros, #if blocks, using
                                     directives, a helper no kernel reaches,
                                     a host function launching nothing

Every fallback widens. A kernel name is only ever narrowed to when the
changed line provably sits inside code that kernel executes or the code that
launches it. The join back to the record is the kernel's name inside its
mangled symbol (`21fusedQKNormRopeKernel` in `_ZN...21fusedQKNormRopeKernelI...`),
so one name covers every template instantiation.

Text, not a compiler. Comments and string literals are blanked before
parsing so braces inside them do not count; definitions are found by the
`name(...) {` shape and classified by the qualifiers in front. What this
cannot see (operator overloads, implicit template selection, macros that
expand to launches) falls to the whole-file reading, which is where it was
before this module existed. Headers stay whole-file: their kernels live in
the files that include them.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import regex as re

KERNEL = "kernel"
DEVICE = "device"
HOST = "host"
#: `#define NAME(...) body`, body across `\` continuations. vLLM launches most
#: kernels through these (`CALL_RESHAPE_AND_CACHE(...)`), so a macro is a node
#: in the launch graph like a host function.
MACRO = "macro"

#: Names that look like `name(...) {` but are statements, not definitions.
_NOT_A_FUNCTION = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "return",
        "sizeof",
        "alignof",
        "decltype",
        "static_assert",
        "defined",
        "__launch_bounds__",
        "__align__",
        "__attribute__",
        "alignas",
        "noexcept",
        "constexpr",  # `if constexpr (...) {`
        "consteval",
    }
)

_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_STRING = re.compile(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')
_IDENT = re.compile(r"\b[A-Za-z_]\w*\b")
_TEMPLATE_ARGS = r"<(?:[^<>]|<(?:[^<>]|<[^<>]*>)*>)*>"
_TRIPLE_LAUNCH = re.compile(rf"\b([A-Za-z_]\w*)\s*(?:{_TEMPLATE_ARGS})?\s*<<<")
# cudaLaunchKernel((const void*)&k, ...), cudaLaunchKernelEx(&cfg, k<T>, ...),
# cudaLaunchKernelEx(&launcher.config, k, ...): the kernel is the first
# argument, or the second after a config expression.
_API_LAUNCH = re.compile(
    r"\bcudaLaunchKernel(?:Ex|ExC)?\s*\(\s*(?:\(\s*const\s+void\s*\*\s*\))?\s*&?\s*"
    r"(?:[A-Za-z_][\w.\->\[\]]*\s*,\s*&?\s*)?([A-Za-z_]\w*)"
)
# a kernel's own name inside its Itanium-mangled symbol: `<len>name`
_MANGLED_NAME = re.compile(r"(\d+)([A-Za-z_]\w*)")
# a namespace-scope declaration: `constexpr int kFoo = 16;`, `#define FOO 1`,
# `static const char* kEnv =`. The name is what functions will mention.
_DECL_NAME = re.compile(
    r"^\s*(?:#\s*define\s+([A-Za-z_]\w*)|(?:[\w:<>,\*&\s]*?\b)([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*(?:=|;|\{))"
)
# lines that open or close a scope and nothing else
_SCOPE_ONLY = re.compile(
    r"^\s*(?:namespace(?:\s+[\w:]+)?\s*\{|\}\s*;?|extern\s+\"C\"\s*\{|\{)\s*$"
)


def _blank(match: re.Match) -> str:
    """Same length, same newlines, no content: line numbers and brace
    structure survive, comments and strings do not."""
    return "".join("\n" if c == "\n" else " " for c in match.group(0))


def strip_comments_and_strings(text: str) -> str:
    text = _BLOCK_COMMENT.sub(_blank, text)
    text = _LINE_COMMENT.sub(_blank, text)
    return _STRING.sub(_blank, text)


@dataclass(frozen=True)
class Entity:
    kind: str  # KERNEL, DEVICE or HOST
    name: str
    start: int  # 1-based, inclusive; the template<> or qualifier line
    end: int  # 1-based, inclusive; the closing brace
    launches: frozenset[str]  # kernel names a host body launches
    refs: frozenset[str]  # identifiers the body mentions

    def covers(self, line: int) -> bool:
        return self.start <= line <= self.end


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _matching_brace(text: str, open_at: int) -> int | None:
    depth = 0
    for i in range(open_at, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _signature_name(prelude: str) -> str | None:
    """The function name in `... name(params)`, or None when the text before
    a `{` is not a function head."""
    p = prelude.rstrip()
    # trailing specifiers a definition may carry
    while True:
        stripped = re.sub(
            r"(?:\bconst|\bnoexcept|\boverride|\bfinal)\s*$", "", p
        ).rstrip()
        stripped = re.sub(
            r"->\s*[\w:]+(?:" + _TEMPLATE_ARGS + r")?\s*$", "", stripped
        ).rstrip()
        if stripped == p:
            break
        p = stripped
    if not p.endswith(")"):
        return None
    depth = 0
    i = len(p) - 1
    while i >= 0:
        if p[i] == ")":
            depth += 1
        elif p[i] == "(":
            depth -= 1
            if depth == 0:
                break
        i -= 1
    if i <= 0:
        return None
    head = p[:i].rstrip()
    # an explicit specialisation names itself as `name<args>`
    if head.endswith(">"):
        d = 0
        j = len(head) - 1
        while j >= 0:
            if head[j] == ">":
                d += 1
            elif head[j] == "<":
                d -= 1
                if d == 0:
                    break
            j -= 1
        head = head[:j].rstrip()
    m = re.search(r"([A-Za-z_]\w*)$", head)
    if not m:
        return None  # a lambda, a macro argument list, an initializer
    name = m.group(1)
    if name in _NOT_A_FUNCTION:
        return None
    # `Foo::Foo(...) : member(x) {` ends in an initializer, not a function
    before = head[: m.start()].rstrip()
    if before.endswith(":") and not before.endswith("::"):
        return None
    return name


def parse(text: str) -> list[Entity]:
    """Every function definition and function-like macro in a CUDA source,
    with its kind and span."""
    clean = strip_comments_and_strings(text)
    entities: list[Entity] = []
    for m in re.finditer(r"\{", clean):
        open_at = m.start()
        # the head is everything since the previous statement or block boundary
        head_start = (
            max(
                clean.rfind(";", 0, open_at),
                clean.rfind("}", 0, open_at),
                clean.rfind("{", 0, open_at),
            )
            + 1
        )
        prelude = clean[head_start:open_at]
        name = _signature_name(prelude)
        if name is None:
            continue
        close_at = _matching_brace(clean, open_at)
        if close_at is None:
            continue
        if "__global__" in prelude:
            kind = KERNEL
        elif "__device__" in prelude:
            kind = DEVICE
        else:
            kind = HOST
        body = clean[open_at : close_at + 1]
        first_text = head_start + (len(prelude) - len(prelude.lstrip()))
        entities.append(
            Entity(
                kind=kind,
                name=name,
                start=_line_of(clean, first_text),
                end=_line_of(clean, close_at),
                launches=_launches_in(body),
                refs=frozenset(_IDENT.findall(body)),
            )
        )
    entities.extend(_macros(clean))
    return entities


def _launches_in(body: str) -> frozenset[str]:
    return frozenset(_TRIPLE_LAUNCH.findall(body)) | frozenset(
        _API_LAUNCH.findall(body)
    )


_DEFINE = re.compile(r"^\s*#\s*define\s+([A-Za-z_]\w*)")


def _macros(clean: str) -> list[Entity]:
    """`#define` directives as entities spanning their continuation lines."""
    out: list[Entity] = []
    lines = clean.splitlines()
    i = 0
    while i < len(lines):
        m = _DEFINE.match(lines[i])
        if not m:
            i += 1
            continue
        start = i
        while i < len(lines) and lines[i].rstrip().endswith("\\"):
            i += 1
        body = "\n".join(lines[start : i + 1])
        out.append(
            Entity(
                kind=MACRO,
                name=m.group(1),
                start=start + 1,
                end=i + 1,
                launches=_launches_in(body),
                refs=frozenset(_IDENT.findall(body)) - {m.group(1)},
            )
        )
        i += 1
    return out


def kernels_reached_by(entities: list[Entity]) -> dict[str, frozenset[str]]:
    """helper name -> the kernels whose bodies reach it, through other
    device helpers in the same file. Bounded: the closure is over names
    defined here, so a helper from a header never appears as a key."""
    helpers = {e.name for e in entities if e.kind == DEVICE}
    kernels = [e for e in entities if e.kind == KERNEL]
    by_name: dict[str, set[str]] = {}
    for e in entities:
        if e.kind == DEVICE:
            by_name.setdefault(e.name, set()).update(e.refs & helpers)
    reached: dict[str, set[str]] = {h: set() for h in helpers}
    for k in kernels:
        seen: set[str] = set()
        frontier = set(k.refs & helpers)
        while frontier:
            h = frontier.pop()
            if h in seen:
                continue
            seen.add(h)
            reached[h].add(k.name)
            frontier |= by_name.get(h, set()) - seen
    return {h: frozenset(v) for h, v in reached.items()}


@dataclass(frozen=True)
class FileAttribution:
    path: str
    #: kernel names the change is attributed to; meaningful when not file_level
    kernels: frozenset[str]
    #: the whole file is the unit, as before this module
    file_level: bool
    why: str

    @property
    def narrowed(self) -> bool:
        return not self.file_level


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.M)


def changed_line_numbers(diff_u0: str) -> tuple[set[int], set[int]]:
    """(base lines, head lines) a `-U0` diff touches. A pure insertion still
    marks the head lines; a pure deletion still marks the base lines."""
    old: set[int] = set()
    new: set[int] = set()
    for h in _HUNK.finditer(diff_u0):
        a, na, b, nb = int(h[1]), int(h[2] or 1), int(h[3]), int(h[4] or 1)
        if na:
            old.update(range(a, a + na))
        if nb:
            new.update(range(b, b + nb))
    return old, new


def _innermost(entities: list[Entity], line: int) -> Entity | None:
    hits = [e for e in entities if e.covers(line)]
    return min(hits, key=lambda e: e.end - e.start) if hits else None


def _inert_line(clean_lines: list[str], line: int) -> bool:
    """A line that cannot change what runs: blank once comments and strings
    are gone, or an include. `line` is 1-based; a line past the end (a
    deletion hunk anchored at EOF) is inert."""
    if line - 1 >= len(clean_lines):
        return True
    s = clean_lines[line - 1].strip()
    return not s or s.startswith("#include") or bool(_SCOPE_ONLY.match(s))


def kernel_names_in(symbols) -> frozenset[str]:
    """The function name of each kernel symbol, read off the mangling.

    `_ZN<ns>...<len>name[I<args>E]E...` or `_Z<len>name...`: the components
    run from `_Z` until the function's own template arguments (`I`) or the
    end of the nested name (`E`), and the last one is the function. A host
    launcher in a .cu often launches kernels defined in a header it includes,
    so the file's own `__global__` definitions are not the whole set of names
    it may mention; the map's symbols are.
    """
    out: set[str] = set()
    for s in symbols:
        if not s.startswith("_Z"):
            out.add(s)  # extern "C", unmangled
            continue
        i = 2
        if s.startswith("_ZN"):
            i = 3
        last = None
        while i < len(s):
            m = _MANGLED_NAME.match(s, i)
            if m is None:
                break  # `I`, `E`, a substitution: the name is behind us
            n = int(m.group(1))
            name = s[m.end(1) : m.end(1) + n]
            if len(name) != n or not re.fullmatch(r"[A-Za-z_]\w*", name):
                break
            last = name
            i = m.end(1) + n
        if last:
            out.add(last)
    return frozenset(out)


def host_reach(
    entities: list[Entity], kernel_names: set[str]
) -> dict[str, frozenset[str]]:
    """host function or macro name -> the kernels it launches, directly or
    through other host functions and macros in this file (a launcher calling
    a dispatch helper, or expanding `CALL_X(...)`, that holds the `<<<`).
    Bounded to names defined here."""
    nodes = {e.name: e for e in entities if e.kind in (HOST, MACRO)}
    direct = {n: (e.launches | (e.refs & kernel_names)) for n, e in nodes.items()}
    out: dict[str, frozenset[str]] = {}
    for n in nodes:
        seen: set[str] = set()
        reach: set[str] = set()
        frontier = {n}
        while frontier:
            h = frontier.pop()
            if h in seen:
                continue
            seen.add(h)
            reach |= direct.get(h, set())
            frontier |= (nodes[h].refs & set(nodes)) - seen
        out[n] = frozenset(reach)
    return out


def _kernels_of(e: Entity, reached, hosts_reach) -> frozenset[str] | None:
    """The kernels a change inside `e` can affect, or None when that cannot
    be named (which sends the file to the whole-file reading)."""
    if e.kind == KERNEL:
        return frozenset({e.name})
    if e.kind == DEVICE:
        return reached.get(e.name) or None
    return hosts_reach.get(e.name) or None


def attribute_text(
    path: str,
    base_text: str | None,
    head_text: str | None,
    diff_u0: str,
    known_kernels: frozenset[str] = frozenset(),
) -> FileAttribution:
    """The attribution for one file given both sides and its diff.

    `known_kernels` are kernel names the symbol map says this file compiles
    (`kernel_names_in`), so a host function that names one of them counts as
    launching it even when the kernel is defined in a header."""
    old_lines, new_lines = changed_line_numbers(diff_u0)
    if not old_lines and not new_lines:
        return FileAttribution(path, frozenset(), True, "no hunks in the diff")
    kernels: set[str] = set()
    touched: set[str] = set()  # entity names, for the note
    for text, lines in ((base_text, old_lines), (head_text, new_lines)):
        if not lines:
            continue
        if text is None:
            return FileAttribution(
                path, frozenset(), True, "one side of the file could not be read"
            )
        entities = parse(text)
        reached = kernels_reached_by(entities)
        kernel_names = {e.name for e in entities if e.kind == KERNEL} | set(
            known_kernels
        )
        hosts_reach = host_reach(entities, kernel_names)
        clean_lines = strip_comments_and_strings(text).splitlines()
        for line in sorted(lines):
            e = _innermost(entities, line)
            if e is None:
                if _inert_line(clean_lines, line):
                    continue
                # A namespace-scope declaration reaches the kernels through
                # the functions that mention its name. Nothing mentions it, or
                # the line declares nothing we can name: the whole file.
                m = (
                    _DECL_NAME.match(clean_lines[line - 1])
                    if line - 1 < len(clean_lines)
                    else None
                )
                name = (m.group(1) or m.group(2)) if m else None
                users = [u for u in entities if name and name in u.refs] if name else []
                if not users:
                    return FileAttribution(
                        path,
                        frozenset(),
                        True,
                        f"line {line} is outside every function",
                    )
                touched.add(name)
                for u in users:
                    via = _kernels_of(u, reached, hosts_reach)
                    if via is None:
                        return FileAttribution(
                            path,
                            frozenset(),
                            True,
                            f"{name} is used by {u.name}, which reaches no kernel",
                        )
                    kernels |= via
                continue
            touched.add(e.name)
            via = _kernels_of(e, reached, hosts_reach)
            if via is None and e.kind == MACRO:
                # a value macro (`#define TILE 32`): reaches kernels through
                # whatever mentions it, like a namespace-scope constant
                users = [u for u in entities if u is not e and e.name in u.refs]
                vias = [_kernels_of(u, reached, hosts_reach) for u in users]
                if users and all(vias):
                    via = frozenset().union(*vias)
            if via is None:
                what = {DEVICE: "helper", MACRO: "macro"}.get(e.kind, "host function")
                return FileAttribution(
                    path,
                    frozenset(),
                    True,
                    f"{what} {e.name} reaches no kernel in this file",
                )
            kernels |= via
    if not kernels:
        return FileAttribution(
            path, frozenset(), True, "only comments, blanks or includes changed"
        )
    return FileAttribution(
        path, frozenset(kernels), False, "changed: " + ", ".join(sorted(touched))
    )


def _show(repo: Path, ref: str | None, path: str) -> str | None:
    if ref is None:
        try:
            return (repo / path).read_text(errors="replace")
        except OSError:
            return None
    out = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    return out.stdout if out.returncode == 0 else None


def attribute(
    repo: Path, base: str, head: str | None, path: str, symbols=frozenset()
) -> FileAttribution:
    """Read both sides of `path` from git and attribute the diff between them.
    `symbols` are the map's symbols for the file, for `known_kernels`."""
    cmd = [
        "git",
        "-C",
        str(repo),
        "diff",
        "-U0",
        base,
        *([head] if head else []),
        "--",
        path,
    ]
    diff = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if diff.returncode != 0:
        return FileAttribution(path, frozenset(), True, "git diff failed")
    return attribute_text(
        path,
        _show(repo, base, path),
        _show(repo, head, path),
        diff.stdout,
        known_kernels=kernel_names_in(symbols),
    )


def symbols_for(names: frozenset[str], symbols: frozenset[str]) -> frozenset[str]:
    """The symbols carrying any of these kernel names: `<len>name` inside an
    Itanium-mangled symbol, or the bare name for an unmangled one. The digit
    guard keeps `3foo` from matching inside `13foobarbaz`."""
    if not names:
        return frozenset()
    pattern = re.compile(
        "|".join(rf"(?<!\d){len(n)}{re.escape(n)}" for n in sorted(names))
    )
    return frozenset(s for s in symbols if s in names or pattern.search(s))
