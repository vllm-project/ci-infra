# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Line-continuation folding: what joins, what must not, and what survives."""

import pytest
from ci_selector.codemap.shell import command_lines, join_continuations

# (name, input, joined output)
FOLD_CASES = [
    (
        "space before the backslash is kept, so the words stay apart",
        "pytest -v \\\n  tests/a.py",
        "pytest -v   tests/a.py",
    ),
    (
        "no space means one word: the newline is deleted, not replaced",
        "tests/lo\\\nng.py",
        "tests/long.py",
    ),
    (
        "an even run is an escaped backslash before a real newline",
        "echo a\\\\\nrun b",
        "echo a\\\\\nrun b",
    ),
    (
        "an odd run folds on its last backslash only",
        "echo a\\\\\\\nrun b",
        "echo a\\\\run b",
    ),
    (
        "a trailing backslash at EOF has nothing to join",
        "pytest tests/a.py \\",
        "pytest tests/a.py \\",
    ),
    (
        "a comment never folds: # runs to the newline whatever ends it",
        "# NOTE: requires a driver \\\npytest tests/a.py",
        "# NOTE: requires a driver \\\npytest tests/a.py",
    ),
    (
        "an indented comment is still a comment",
        "    # keep \\\n    pytest tests/a.py",
        "    # keep \\\n    pytest tests/a.py",
    ),
    (
        "chained continuations collapse to one line",
        "pytest \\\n  -v \\\n  tests/a.py \\\n  tests/b.py",
        "pytest   -v   tests/a.py   tests/b.py",
    ),
    (
        "a continued line running into a comment stops there",
        "RUN foo \\\n  # why \\\n  bar",
        "RUN foo   # why \\\n  bar",
    ),
    (
        # Glued to `foo`, so shell opens no comment here either.
        "a `#` with no boundary in front of it is a word, not a comment",
        "RUN foo\\\n# why \\\nbar",
        "RUN foo# why bar",
    ),
    (
        # The price of not tracking quotes, pinned so it stays a decision.
        "a quoted `#` stops the fold like any other",
        'pytest -k "not slow # fast" \\\n  tests/a.py',
        'pytest -k "not slow # fast" \\\n  tests/a.py',
    ),
    (
        "CRLF folds too, and the `\\r` it did not consume stays put",
        "pytest -v \\\r\n  tests/a.py\r\n",
        "pytest -v   tests/a.py\r\n",
    ),
]


@pytest.mark.parametrize("case", FOLD_CASES, ids=[c[0] for c in FOLD_CASES])
def test_join_continuations(case):
    _, text, expected = case
    assert join_continuations(text) == expected


@pytest.mark.parametrize("case", FOLD_CASES, ids=[c[0] for c in FOLD_CASES])
def test_join_is_idempotent(case):
    """Callers join a block twice, once inside its command and once on its own,
    so a second pass must not fold what the first refused."""
    _, text, _ = case
    once = join_continuations(text)
    assert join_continuations(once) == once


def test_comment_fold_would_swallow_a_live_command():
    """Why the comment rule exists: Dockerfiles put backslash-terminated
    comments directly above real commands, and folding one hides them."""
    text = "  # Give DeepEP a stable prefix \\\n  pytest tests/a.py\n"
    assert list(command_lines(text)) == ["pytest tests/a.py"]


def test_command_lines_joins_before_it_splits():
    """Splitting first is the bug: every path after the first one is lost."""
    text = "pytest -x \\\n  tests/a.py \\\n  tests/b.py\n"
    assert list(command_lines(text)) == ["pytest -x   tests/a.py   tests/b.py"]


def test_command_lines_strips_blanks_and_comments():
    text = "\n  set -e\n\n# a comment\n  pytest tests/a.py  \n"
    assert list(command_lines(text)) == ["set -e", "pytest tests/a.py"]


def test_command_lines_keeps_a_comment_that_was_folded_into():
    """`#` inside a joined line is shell's own comment, not ours to drop: the
    line still begins with a command, so it is still a command."""
    text = "pytest tests/a.py \\\n# trailing note\n"
    assert list(command_lines(text)) == ["pytest tests/a.py # trailing note"]
