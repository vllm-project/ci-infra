# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Scan the shell scripts a live step runs for the shapes the YAML parser
already knows: pytest lines, python drivers, nested bash, and cd. The rest of a
script is docker and setup logic and is skipped.

Only scripts a step actually invokes are read, so a script nothing runs cannot
invent targets. Giving up is always recorded and shown by preflight, never a
quiet return, or the tests those lines run would look like nothing runs them.
"""

from __future__ import annotations

import shlex

import regex as re

from ..shell import join_continuations

# Exactly the deepest `bash x.sh` chain in the tree, no headroom. A new
# nesting level records a dangling target, which preflight force-selects.
SCRIPT_MAX_DEPTH = 3
PYTEST_LINE_RE = re.compile(r"(?:^|[;&(\s])(?:python3?\s+-m\s+)?pytest\s+(.*)")
# Any .sh path, not just `bash x.sh`: a script is often put in a var first.
SH_PATH_RE = re.compile(r"[\w./-]+\.sh\b")
# Tolerates quotes and a "${VAR}/" prefix, which resolve_path strips.
PYTHON_LINE_RE = re.compile(r'(?:^|[;&(\s])python3?\s+"?(\S+?\.py)\b')
CD_RE = re.compile(r"(?:^|[;&(\s])cd\s+(\S+)")
SEP_RE = re.compile(r"&&|\|\||;")
# A `-c '...'` payload handed to a container runtime, matched over the whole
# script because both the command and the payload span lines. The required
# closing quote keeps it safe: an unterminated payload matches nothing.
CONTAINER_PAYLOAD_RE = re.compile(
    r"\b(?:docker|podman|nerdctl)\s+(?:run|exec|create)\b[\s\S]*?-c\s*([\"'])([\s\S]*?)\1"
)
# Cut at separators, pipes and redirects, or a trailing `&& bash next.sh`
# reads as a pytest positional.
ARG_CUT_RE = re.compile(r"&&|;|\||2>|>")


def scan_script(script: str, parser, depth: int = 0) -> None:
    """Feed a script body's test shapes into the invoking step's CommandParser.
    Targets land in that step's StepTargets tagged via=<script>. A script's
    runtime cwd is not knowable here, so resolve_path falls back to the repo
    root."""
    if depth >= SCRIPT_MAX_DEPTH:
        parser.out.dangling.append(script)
        return
    path = parser.repo / script
    try:
        text = path.read_text()
    except (UnicodeDecodeError, OSError):
        parser.out.dangling.append(script)
        return
    # Raw text, not joined: key matching reads words, not commands.
    parser.out.haystack += "\n" + text
    saved_cwd = parser.cwd
    joined = join_continuations(text)
    payloads = [m.span() for m in CONTAINER_PAYLOAD_RE.finditer(joined)]
    offset = 0
    for raw in joined.splitlines(keepends=True):
        start, offset = offset, offset + len(raw)
        in_container = any(lo <= start < hi for lo, hi in payloads)
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = CD_RE.search(line)
        if m and "$" not in m.group(1):
            parser.chdir(m.group(1))
        # Split so every chained `pytest` is seen. The .sh loop below still
        # reads the whole line.
        for segment in SEP_RE.split(line):
            m = PYTEST_LINE_RE.search(segment)
            if m:
                args = _tokenize(m.group(1))
                if args is None:
                    parser.out.unlexable.append(segment)
                    continue
                parser.parse_pytest(args, via=script, container=in_container)
        for token in SH_PATH_RE.findall(line):
            nested = parser.resolve_path(token)
            if nested is None:
                # `"$(dirname "$0")/x.sh"`: fall back to a sibling of this script.
                sibling = f"{script.rsplit('/', 1)[0]}/{token.rsplit('/', 1)[-1]}"
                if (parser.repo / sibling).is_file():
                    nested = sibling
            if nested and nested not in parser.out.scripts_seen:
                parser.out.scripts_seen.append(nested)
                scan_script(nested, parser, depth + 1)
        # Runs even when the line also named a script, which still leaves a
        # real driver target.
        m = PYTHON_LINE_RE.search(line)
        if m:
            resolved = parser.resolve_path(m.group(1))
            if resolved:
                parser.out.add_target(resolved, "script", via=script)
    parser.cwd = saved_cwd


def _tokenize(argstr: str) -> list[str] | None:
    argstr = ARG_CUT_RE.split(argstr)[0].rstrip()
    try:
        return shlex.split(argstr)
    except ValueError as exc:
        if "quotation" not in str(exc) or not argstr.endswith(("'", '"')):
            return None
    # The last pytest line of a `bash -c "..."` payload carries the payload's
    # own closing quote, so our slice holds one quote too many. Drop it and
    # retry; anything still unreadable stays recorded.
    try:
        return shlex.split(argstr[:-1])
    except ValueError:
        return None
