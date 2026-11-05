# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Which csrc sources an edit to CMakeLists.txt reaches.

CMakeLists.txt is copied into the image, so any edit to it runs everything.
Most edits touch one kernel's corner of the file: its arch list, its source
list, the guard around them. The file's own dataflow says which sources that
corner compiles. An arch variable reaches sources through
``set_gencode_flags_for_srcs(SRCS ... CUDA_ARCHS ...)`` and the ``if(<var>)``
blocks it guards; a source list reaches the build through
``list(APPEND <extension sources> ...)``.

The walk is over statements, on both sides of the diff. It starts at the
changed statements and follows each variable they define to every statement
that uses it. Anything outside a small grammar declines: an unknown command,
a variable reaching an extension target's other arguments or a compile flag,
a source that is not a csrc path. Declining keeps today's answer, which runs
everything.

Lines added to or removed from a multi-line source list reach only the
sources on those lines. A glob that matches no tracked file names sources the
build generates (the Marlin kernels); it stands for the tracked files of its
directory, which are the generator and the headers the generated sources
include.
"""

from __future__ import annotations

import fnmatch
import subprocess
from dataclasses import dataclass
from pathlib import Path

import regex as re

from .build_map import ALL_FAMILIES, CUDA, _classify_cond

CMAKELISTS = "CMakeLists.txt"

_REF = re.compile(r"\$\{(\w+)\}")
_CSRC = re.compile(r"(csrc/[\w./*+-]+)")
_SOURCE_LIKE = re.compile(r"[\w./*${}-]+\.(?:cu|cuh|cpp|cc|c|h|hpp|hip)\b")
_SOURCE_EXTS = (".cu", ".cuh", ".cpp", ".cc", ".c", ".h", ".hpp", ".hip", ".inl")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_KEYWORD = re.compile(r"^[A-Z][A-Z_]+$")

_BRANCH = frozenset({"if", "elseif", "else"})
# Statements that change no build input on their own.
_NEUTRAL = frozenset({"endif", "message"})
# Statements that place flags on the sources SRCS (or the leading list) names.
_ON_SOURCES = frozenset({"set_gencode_flags_for_srcs", "set_source_files_properties"})


class Declined(Exception):
    pass


@dataclass(frozen=True)
class Statement:
    name: str
    args: str
    first: int  # 1-based, inclusive
    last: int
    depth: int  # if-nesting of the statement itself
    # Device families whose build can run it, from the enclosing guards.
    families: frozenset[str] = ALL_FAMILIES


def _strip_comment(line: str) -> str:
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == "#" and not quoted:
            return line[:i]
    return line


def _guard(cond: str) -> frozenset[str] | None:
    """Families a guard admits, or None when it cannot be read safely.

    build_map's reading looks for a `VLLM_GPU_LANG STREQUAL` term; under NOT,
    or OR'd with anything else, that term no longer bounds the arm, and an
    else-arm computed from it would skip CUDA code.
    """
    upper = cond.upper()
    if re.search(r"\bNOT\b", upper):
        return None
    if re.search(r"\bOR\b", upper):
        rest = re.sub(r'VLLM_GPU_LANG\s+STREQUAL\s+"\w+"', "", upper)
        if re.sub(r"\bOR\b|[()\s]", "", rest):
            return None
    return _classify_cond(cond)


def parse(text: str) -> list[Statement]:
    """Command invocations with their line spans, comments stripped."""
    out: list[Statement] = []
    balance = start = 0
    buf: list[str] = []
    # Per open if-chain: [this arm's families or None, families taken, known].
    frames: list[list] = []

    def families() -> frozenset[str]:
        eff = set(ALL_FAMILIES)
        for arm, _taken, _known in frames:
            if arm is not None:
                eff &= arm
        return frozenset(eff)

    for number, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not buf and not line.strip():
            continue
        if not buf:
            start = number
        buf.append(line)
        balance += line.count("(") - line.count(")")
        if balance > 0:
            continue
        joined = "\n".join(buf).strip()
        buf, balance = [], 0
        m = re.match(r"([A-Za-z_]\w*)\s*\((.*)\)\s*$", joined, re.S)
        name, args = (m.group(1).lower(), m.group(2)) if m else ("?", joined)
        if name in ("endif", "elseif", "else") and frames:
            arm, taken, known = frames[-1]
            if name == "endif":
                frames.pop()
            else:
                taken = taken | (arm or set())
                if name == "elseif":
                    fams = _guard(args)
                    frames[-1] = [
                        set(fams) if fams is not None else None,
                        taken,
                        known and fams is not None,
                    ]
                else:
                    frames[-1] = [
                        set(ALL_FAMILIES) - taken if known else None,
                        taken,
                        known,
                    ]
        depth = len(frames) - (1 if name in ("elseif", "else") else 0)
        out.append(Statement(name, args, start, number, depth, families()))
        if name == "if":
            fams = _guard(args)
            frames.append(
                [set(fams) if fams is not None else None, set(), fams is not None]
            )
    return out


def _target(st: Statement) -> str | None:
    """The variable a statement writes, if it is one of the forms walked."""
    words = st.args.split()
    if st.name in ("set", "cuda_archs_loose_intersection") and words:
        return words[0]
    if st.name == "file" and len(words) > 1 and words[0] in ("GLOB", "GLOB_RECURSE"):
        return words[1]
    if st.name == "list" and len(words) > 1 and words[0] in ("APPEND", "PREPEND"):
        return words[1]
    return None


def _items(st: Statement) -> str:
    """The values a set/file/list statement writes, after the variable."""
    skip = 1 if st.name == "set" else 2
    parts = st.args.split(None, skip)
    return parts[skip] if len(parts) > skip else ""


def _srcs(st: Statement) -> str:
    if st.name == "set_gencode_flags_for_srcs":
        m = re.search(r"\bSRCS\b(.*?)(?:\bCUDA_ARCHS\b|$)", st.args, re.S)
        return m.group(1) if m else ""
    return re.split(r"\bPROPERTIES\b", st.args, maxsplit=1)[0]


def extension_sources(statements: list[Statement]) -> frozenset[str]:
    """Variables passed as SOURCES to an extension target."""
    sinks: set[str] = set()
    for st in statements:
        if st.name not in ("define_extension_target", "define_gpu_extension_target"):
            continue
        taking = False
        for token in st.args.split():
            if _KEYWORD.match(token):
                taking = token == "SOURCES"
            elif taking:
                sinks.update(_REF.findall(token))
    return frozenset(sinks)


class Side:
    """One side of the diff: its statements and the csrc files tracked there."""

    def __init__(self, text: str, files: list[str], grep=None):
        # grep(word) -> csrc files mentioning `word` at this side's commit.
        self.grep = grep
        self.lines = text.splitlines()
        self.statements = parse(text)
        self.files = files
        self.sinks = extension_sources(self.statements)
        self.other_families: set[str] = set()

    def _chain(self, st: Statement) -> list[Statement]:
        """Everything an if-chain governs, every arm: a changed condition, or
        a variable it reads, can move control from one arm to another."""
        i = self.statements.index(st)
        while self.statements[i].name != "if" or self.statements[i].depth != st.depth:
            i -= 1
        out = []
        for nxt in self.statements[i + 1 :]:
            if nxt.depth == st.depth and nxt.name == "endif":
                break
            out.append(nxt)
        return out

    def _uses(self, var: str) -> list[Statement]:
        bare = re.compile(rf"(?<![\w${{]){re.escape(var)}(?!\w)")
        return [
            st
            for st in self.statements
            if f"${{{var}}}" in st.args
            or (st.name in ("if", "elseif") and bare.search(st.args))
        ]

    def expand(self, text: str, seen: frozenset[str] = frozenset()) -> set[str]:
        """Repo paths a list of source arguments names."""
        text = text.replace("${CMAKE_CURRENT_SOURCE_DIR}/", "")
        for token in _SOURCE_LIKE.findall(text):
            if not token.startswith("csrc/") and "${" not in token:
                raise Declined(f"source {token} is outside csrc/")
        paths: set[str] = set()
        for pat in _CSRC.findall(text):
            if "*" not in pat:
                paths.add(pat)
                continue
            hits = [f for f in self.files if fnmatch.fnmatch(f, pat)]
            if not hits:
                folder = pat.rsplit("/", 1)[0] + "/"
                hits = [
                    f
                    for f in self.files
                    if f.startswith(folder)
                    and "/" not in f[len(folder) :]
                    and f.endswith(_SOURCE_EXTS)
                ]
            if not hits:
                raise Declined(f"glob {pat} matches nothing tracked")
            paths.update(hits)
        for var in _REF.findall(text):
            if var in seen:
                continue
            defs = [st for st in self.statements if _target(st) == var]
            if not defs or any(st.name not in ("set", "file", "list") for st in defs):
                raise Declined(f"${{{var}}} is not a source list")
            for st in defs:
                paths |= self.expand(_items(st), seen | {var})
        return paths

    def _is_source_list(self, var: str) -> bool:
        defs = [st for st in self.statements if _target(st) == var]
        if not defs or any(st.name not in ("set", "file", "list") for st in defs):
            return False
        try:
            return bool(self.expand(f"${{{var}}}"))
        except Declined:
            return False

    def _macro_files(self, st: Statement) -> set[str]:
        """csrc files that read the macros a target_compile_definitions sets."""
        names: set[str] = set()
        for word in st.args.split()[1:]:
            if word in ("PRIVATE", "PUBLIC", "INTERFACE"):
                continue
            for var in _REF.findall(word):
                defs = [d for d in self.statements if _target(d) == var]
                if not defs or any(d.name != "set" for d in defs):
                    raise Declined(f"${{{var}}} in compile definitions is unknown")
                for d in defs:
                    names.update(_items(d).split())
            literal = _REF.sub("", word).strip('"').removeprefix("-D")
            if literal:
                names.add(literal)
        names = {n.split("=", 1)[0] for n in names if n}
        if not names or self.grep is None:
            raise Declined(f"compile definitions at line {st.first}")
        files: set[str] = set()
        for name in names:
            hits = self.grep(name)
            if not hits:
                raise Declined(f"macro {name} is read by no csrc file")
            files |= hits
        return files

    def changed_statements(self, changed: set[int]) -> list[Statement]:
        return [
            st
            for st in self.statements
            if any(st.first <= n <= st.last for n in changed)
            and st.name not in _NEUTRAL
        ]

    def _lines_text(self, st: Statement, lines) -> str:
        return "\n".join(
            _strip_comment(self.lines[n - 1]) for n in lines if st.first <= n <= st.last
        )

    def walk(self, changed: set[int]) -> set[str]:
        """The sources the statements on `changed` lines reach."""
        todo_seed = self.changed_statements(changed)
        todo = list(todo_seed)
        sources: set[str] = set()
        done: set[int] = set()
        done_vars: set[str] = set()
        while todo:
            st = todo.pop()
            if st.first in done:
                continue
            done.add(st.first)
            if CUDA not in st.families:
                # Only another device's build runs it: that family's steps,
                # not its sources, answer for it.
                self.other_families |= st.families
                continue
            var = _target(st)
            header_changed = st.first in changed
            if st.name in _BRANCH:
                todo.extend(self._chain(st))
            elif st.name in _NEUTRAL:
                continue
            elif st.name in _ON_SOURCES:
                sources |= self.expand(_srcs(st))
            elif st.name == "target_compile_definitions":
                sources |= self._macro_files(st)
            elif var is None:
                raise Declined(f"{st.name}() at line {st.first}")
            elif var in self.sinks:
                # Items handed to an extension: the ones this statement adds,
                # or just the edited lines of it.
                inner = st.first < st.last and not header_changed and st in todo_seed
                sources |= self.expand(
                    self._lines_text(st, changed) if inner else _items(st)
                )
            elif (
                st in todo_seed
                and not header_changed
                and st.name in ("set", "list")
                and st.first < st.last
            ):
                # Lines edited inside a source list: those items, not its users.
                sources |= self.expand(self._lines_text(st, changed))
            elif var in done_vars:
                continue
            elif self._is_source_list(var):
                # A source list reaches its own items, wherever it is listed;
                # only a block it guards reaches further.
                done_vars.add(var)
                sources |= self.expand(f"${{{var}}}")
                todo.extend(u for u in self._uses(var) if u.name in ("if", "elseif"))
            else:
                done_vars.add(var)
                uses = self._uses(var)
                if not uses:
                    raise Declined(
                        f"{var} is read by nothing in the file (CMake may read it)"
                    )
                todo.extend(uses)
        return sources


def _changed_lines(repo: Path, base: str, head: str) -> tuple[set[int], set[int]]:
    out = _git(repo, "diff", "-U0", base, head, "--", CMAKELISTS)
    old: set[int] = set()
    new: set[int] = set()
    for line in out.splitlines():
        m = _HUNK.match(line)
        if m:
            a, b = int(m.group(1)), int(m.group(2) or 1)
            c, d = int(m.group(3)), int(m.group(4) or 1)
            old.update(range(a, a + b))
            new.update(range(c, c + d))
    return old, new


def _grep(repo: Path, ref: str, word: str) -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "grep", "-l", "-w", "-F", word, ref, "--", "csrc/"],
        capture_output=True,
        text=True,
    ).stdout
    return {line.split(":", 1)[1] for line in out.splitlines() if ":" in line}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


@dataclass(frozen=True)
class Reach:
    sources: frozenset[str] = frozenset()
    why_not: str = ""  # set when the edit reaches beyond named sources
    inert: bool = False  # only comments, blank lines or messages changed
    # Families whose build alone runs part of the edit (an `if(VLLM_GPU_LANG
    # STREQUAL "HIP")` block): their steps run, whatever the sources say.
    other_families: frozenset[str] = frozenset()


def reach(repo: Path, base: str, head: str) -> Reach:
    """The csrc sources a CMakeLists.txt edit between base and head reaches."""
    try:
        old, new = _changed_lines(repo, base, head)
        sources: set[str] = set()
        others: set[str] = set()
        touched = False
        for ref, lines in ((base, old), (head, new)):
            if not lines:
                continue
            side = Side(
                _git(repo, "show", f"{ref}:{CMAKELISTS}"),
                _git(repo, "ls-tree", "-r", "--name-only", ref, "--", "csrc/").split(),
                grep=lambda word, ref=ref: _grep(repo, ref, word),
            )
            touched |= bool(side.changed_statements(lines))
            sources |= side.walk(lines)
            others |= side.other_families
    except Declined as why:
        return Reach(why_not=str(why))
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        return Reach(why_not=f"could not read the edit ({exc})")
    if not touched:
        # Comments, blank lines and message() only.
        return Reach(inert=True)
    if not sources and not others:
        return Reach(why_not="the edit reaches no source")
    return Reach(sources=frozenset(sources), other_families=frozenset(others))
