# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Join shell line continuations before anything reads a command.

Scanners split commands on newlines, so a trailing `\\` hides the rest of an
invocation and the test paths on the following lines go missing.

Rules:

- The `\\`+newline is deleted, not replaced with a space, so `a\\`+newline+`b`
  is the single word `ab`.
- Only an odd run of trailing backslashes joins. An even run is an escaped
  backslash.
- A line that has opened a comment never joins, or the command below it is
  swallowed and its tests are lost.

Quotes are not tracked. A `#` inside an argument stops a join it should not,
which costs some precision. Tracking quotes would mean rewriting shlex, which
is what we hand the result to anyway.
"""

from __future__ import annotations

from collections.abc import Iterator

import regex as re

COMMENT_START_RE = re.compile(r"(?:^|\s)#")


def join_continuations(text: str) -> str:
    """Join `\\`-continued lines into single lines."""
    out: list[str] = []
    held: str | None = None
    for line in text.split("\n"):
        # A CRLF checkout leaves the \r after the backslash, hiding it.
        line, eol = (line[:-1], "\r") if line.endswith("\r") else (line, "")
        # Tested joined, not per line, so calling this twice gives the same
        # answer about where a comment starts.
        if _folds((held or "") + line):
            held = (held or "") + line[:-1]
            continue
        out.append((held or "") + line + eol)
        held = None
    if held is not None:
        out.append(held + "\\")  # nothing left to join it to
    return "\n".join(out)


def command_lines(text: str) -> Iterator[str]:
    """The runnable lines of a command block, blanks and comments dropped."""
    for line in join_continuations(text).splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            yield line


def _folds(line: str) -> bool:
    if COMMENT_START_RE.search(line):
        return False
    trailing = len(line) - len(line.rstrip("\\"))
    return trailing % 2 == 1
