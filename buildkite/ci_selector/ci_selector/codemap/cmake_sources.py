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
# Statements that change no build input on their own. An endif() reached from
# its chain is one of them; a changed endif() is not, since moving it moves
# statements in or out of the block (see `walk`).
_NEUTRAL = frozenset({"endif", "message"})
# An arch in an arch list ("8.0", "12.0f", "9.0a", "10.0+PTX").
_ARCH = re.compile(r"(?<![\w.])\d+\.\d+[a-z]?(?:\+PTX)?(?![\w.])")
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
    # The enclosing arms, outermost first: (line of their if(), the arm as
    # text, its if() condition included), comparable across the two sides.
    guards: tuple[tuple[int, str], ...] = ()


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
    # Per open if-chain: (line of its if(), its condition, the current arm).
    arms: list[tuple[int, str, str]] = []

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
        if name in ("endif", "elseif", "else") and arms:
            line, cond, _ = arms.pop()
            if name != "endif":
                arms.append((line, cond, f"{name} {' '.join(args.split())}"))
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
        guards = tuple((line, f"{cond} / {arm}") for line, cond, arm in arms)
        if name in ("elseif", "else"):
            guards = guards[:-1]  # the arm it opens is not around it
        out.append(Statement(name, args, start, number, depth, families(), guards))
        if name == "if":
            arms.append((start, " ".join(args.split()), "if"))
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
            if i < 0:
                raise Declined(f"{st.name}() at line {st.first} closes no if()")
        out = []
        for nxt in self.statements[i + 1 :]:
            if nxt.depth == st.depth and nxt.name == "endif":
                break
            out.append(nxt)
        return out

    def block(self, st: Statement) -> list[Statement] | None:
        """The if() an if/elseif/else/endif belongs to, through its endif()."""
        i = self.statements.index(st)
        while self.statements[i].name != "if" or self.statements[i].depth != st.depth:
            i -= 1
            if i < 0:
                return None
        out = [self.statements[i]]
        for nxt in self.statements[i + 1 :]:
            out.append(nxt)
            if nxt.depth == st.depth and nxt.name == "endif":
                return out
        return None

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
            if any(st.first <= n <= st.last for n in changed) and st.name != "message"
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
            if st.name == "endif" and st in todo_seed:
                # Moved or added: statements moved in or out of the block, on
                # this side or the other, so every arm it closes is reached.
                todo.extend(self._chain(st))
            elif st.name in _BRANCH:
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
                and self._is_source_list(var)
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
    # The edit only removes arches from arch lists, so each source compiles
    # for fewer arches than before: a step that launched none of its kernels
    # cannot have been running them. Any other edit may make a source compile
    # where it did not (an added arch, a guard moved or removed), and the
    # steps that would now launch it are exactly the ones the record never
    # saw launch it, so the record may not clear them.
    only_removes_arches: bool = False


def _arch_definition(st: Statement) -> tuple[str, frozenset[str], str] | None:
    """(variable, arches, everything else) for an arch-list definition."""
    var = _target(st)
    if var is None or st.name not in ("cuda_archs_loose_intersection", "set"):
        return None
    rest = re.sub(r'[\s;"]', "", _ARCH.sub("", st.args))
    if st.name == "set" and rest != var:
        return None
    return var, frozenset(_ARCH.findall(st.args)), rest


def only_removes_arches(
    old: Side | None, old_lines: set[int], new: Side | None, new_lines: set[int]
) -> bool:
    """Whether the edit only drops arches from arch lists.

    Every statement it adds defines an arch list, and every one it removes
    either does or is a guard around removed definitions only (collapsing
    `if(CUDA >= 13) set "8.0;12.0f" else() set "8.0;12.0a" endif()` into one
    list). Each list it redefines keeps its other arguments and names only
    arches that every removed arm of it named, so no compiler or guard
    compiles a source for an arch it did not before.
    """
    removed = old.changed_statements(old_lines) if old else []
    added = new.changed_statements(new_lines) if new else []
    if not removed:
        return False
    was: dict[str, list[tuple[frozenset[str], str, tuple[str, ...]]]] = {}
    gone = {st.first for st in removed}
    for st in removed:
        definition = _arch_definition(st)
        if definition is not None:
            # The guards that stay: a removed block's arms are what the edit
            # collapses, the rest must still be around the new definition.
            outer = tuple(arm for line, arm in st.guards if line not in gone)
            was.setdefault(definition[0], []).append((*definition[1:], outer))
            continue
        if st.name not in ("if", "elseif", "else", "endif") or old is None:
            return False
        # The whole block goes: its if(), every arm, its endif(). Anything
        # it guarded that stays would now compile unguarded.
        block = old.block(st)
        if block is None or any(
            inner.first not in gone and inner.name != "message" for inner in block
        ):
            return False
    now: dict[str, list[tuple[frozenset[str], str, tuple[str, ...]]]] = {}
    for st in added:
        definition = _arch_definition(st)
        if definition is None:
            return False
        guards = tuple(arm for _, arm in st.guards)
        now.setdefault(definition[0], []).append((*definition[1:], guards))
    if set(now) != set(was):
        return False
    for var, defs in now.items():
        common = frozenset.intersection(*(arches for arches, _, _ in was[var]))
        before = {(rest, outer) for _, rest, outer in was[var]}
        for arches, rest, guards in defs:
            if not arches <= common or before != {(rest, guards)}:
                return False
    return True


def reach(repo: Path, base: str, head: str) -> Reach:
    """The csrc sources a CMakeLists.txt edit between base and head reaches."""
    try:
        old, new = _changed_lines(repo, base, head)
        sources: set[str] = set()
        others: set[str] = set()
        touched = False
        sides: dict[str, Side] = {}
        for ref, lines in ((base, old), (head, new)):
            if not lines:
                continue
            side = Side(
                _git(repo, "show", f"{ref}:{CMAKELISTS}"),
                _git(repo, "ls-tree", "-r", "--name-only", ref, "--", "csrc/").split(),
                grep=lambda word, ref=ref: _grep(repo, ref, word),
            )
            touched |= bool(side.changed_statements(lines))
            sides[ref] = side
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
    return Reach(
        sources=frozenset(sources),
        other_families=frozenset(others),
        only_removes_arches=only_removes_arches(
            sides.get(base), old, sides.get(head), new
        ),
    )
