# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""The three commands, and the two argparse traps that make them dangerous.

`ci-select` is bare-or-subcommand, which argparse supports badly. Both traps
below fail silently and with exit code 0, which is why they are pinned rather
than left to a manual check.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from ci_selector.cli import _DEFAULTS, build_parser


def _parse(argv):
    """Parse exactly what the binary parses, without running it."""
    args = build_parser().parse_args(argv)
    for k, v in _DEFAULTS.items():
        if not hasattr(args, k):
            setattr(args, k, v)
    return args


def test_a_flag_before_the_subcommand_is_not_reset_by_it():
    """The trap: with `parents=`, a subparser re-applies its parents' defaults
    over what was already parsed. A plain default would silently put --repo
    back to "." here -- the selector then analyzes the wrong checkout, finds no
    changed files, and exits 0."""
    before = _parse(["--repo", "/somewhere", "codemap"])
    after = _parse(["codemap", "--repo", "/somewhere"])
    assert before.repo == Path("/somewhere"), "the subcommand reset --repo"
    assert before.repo == after.repo
    assert before._mode == after._mode == "codemap"


def test_a_two_ended_range_is_required():
    """Both commands take diffs only.

    The coverage half needs changed FUNCTION names, which need a diff; bare
    paths cannot give it one. And a one-ended range is worse than it looks --
    `--diff BASE` is silently a working-tree comparison that skips the base
    worktree, so added-file routing and table scoping never fire, and
    `git diff <base>` cannot see untracked files at all.
    """
    from ci_selector.cli import main

    repo = os.environ["VLLM_REPO"]
    assert main(["--repo", repo]) == 2, "a missing --diff was accepted"
    assert main(["--repo", repo, "--diff", "HEAD"]) == 2, "one-ended was accepted"
    assert main(["--repo", repo, "--diff", "..HEAD"]) == 2, "malformed was accepted"
    assert main(["codemap", "--repo", repo]) == 2, "codemap skipped the check"


def test_bare_command_means_both():
    assert _parse([])._mode == "both"
    assert _parse(["--repo", "."])._mode == "both"


def test_there_is_no_way_to_ask_for_half_the_rule():
    """Dropping is not a flag. A record that may only add is not the rule, it
    is half of it, and the half that cannot lose a test is the wrong half to
    make optional."""

    with pytest.raises(SystemExit):
        _parse(["--drop"])
    with pytest.raises(SystemExit):
        _parse(["coverage"])


def test_the_codemap_subcommand_is_reachable():
    assert _parse(["codemap"])._mode == "codemap"


def test_missing_table_falls_back_to_the_map(tmp_path, monkeypatch):
    """No coverage available must degrade to the code map, never to silence."""
    monkeypatch.setenv("CI_SELECTOR_TABLE", str(tmp_path / "absent.json.gz"))
    from ci_selector.coverage.source import fetch_table

    table = fetch_table()
    assert not table.available
    assert "Running on the code map alone" in table.unavailable


def test_the_table_override_reads_the_path_it_is_given(tmp_path):
    """`--table` is documented in the README and never once ran end to end: the
    CLI handed `decide` a Path, which read `.available` off it above its own
    try block. A crash means run-everything, so it failed safe by luck."""
    from ci_selector.coverage.source import fetch_table

    override = tmp_path / "elsewhere.json"
    override.write_text('{"version": 999, "rows": {}}')
    table = fetch_table(override)

    # Reached the file rather than raising, and judged it on its contents.
    assert not table.available
    assert "re-merge" in table.unavailable


def _no_download(monkeypatch):
    import ci_selector.scripts.fetch_functions as fetch_functions

    def refuse():
        raise AssertionError("downloaded a table although one was named")

    monkeypatch.setattr(fetch_functions, "fetch_latest", refuse)
    return fetch_functions


def test_a_named_table_is_read_as_it_is(tmp_path, monkeypatch):
    """Replays and tests pin a table with --table; it must never be swapped
    for the latest one."""
    from ci_selector.coverage.source import coverage_table

    _no_download(monkeypatch)
    monkeypatch.delenv("CI_SELECTOR_TABLE", raising=False)
    pinned = tmp_path / "pinned.json"
    pinned.write_text('{"version": 999, "rows": {}}')
    assert "re-merge" in coverage_table(pinned).unavailable


def test_a_table_named_by_the_environment_is_read_as_it_is(tmp_path, monkeypatch):
    from ci_selector.coverage.source import coverage_table

    _no_download(monkeypatch)
    pinned = tmp_path / "pinned.json"
    pinned.write_text('{"version": 999, "rows": {}}')
    monkeypatch.setenv("CI_SELECTOR_TABLE", str(pinned))
    assert "re-merge" in coverage_table(None).unavailable


def test_with_no_table_named_the_latest_is_fetched_and_parsed_once(monkeypatch):
    """What CI does: no separate fetch step, one parse, here."""
    import ci_selector.scripts.fetch_functions as fetch_functions
    from ci_selector.coverage.source import coverage_table

    monkeypatch.delenv("CI_SELECTOR_TABLE", raising=False)
    sentinel = object()
    calls = []
    monkeypatch.setattr(
        fetch_functions, "fetch_latest", lambda: calls.append(1) or sentinel
    )
    assert coverage_table(None) is sentinel
    assert calls == [1]


def test_a_refused_download_reaches_the_caller(monkeypatch):
    """main() turns it into exit 1, which sends CI back to today's rules."""
    import ci_selector.scripts.fetch_functions as fetch_functions
    from ci_selector.coverage.source import coverage_table

    monkeypatch.delenv("CI_SELECTOR_TABLE", raising=False)

    def refuse():
        raise fetch_functions.FetchError("nothing published yet: HTTP 404")

    monkeypatch.setattr(fetch_functions, "fetch_latest", refuse)
    with pytest.raises(fetch_functions.FetchError):
        coverage_table(None)
