# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""A step whose job recorded nothing tonight keeps last night's row.

Build 92059's basic-models-tests-other job died at checkout, so the table had
no row for it and vllm#57865 and vllm#59200, which changed code its previous
row shows it running, never selected it. These pin the fold that carries the
row forward, the refusals around it, and the reader that lets a carried row
add and never drop.
"""

from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pytest
from ci_selector.codemap.selection import Selection
from ci_selector.coverage.changed_funcs import Attribution, FileQuery, Query
from ci_selector.coverage.rules import RowKeys, newest_commit, read_pr
from ci_selector.coverage.table import Evidence, load
from ci_selector.scripts import build as build_mod
from ci_selector.scripts import publish
from ci_selector.scripts.build import carry_forward, merge_build, write_table

from .helpers import Build, Repo, process_file

CONFIG = 'name: vllm_ci\njob_dirs:\n  - ".buildkite/test_areas"\n'


def steps_yaml(**commands: str) -> str:
    lines = ["group: g", "steps:"]
    for key, command in commands.items():
        lines += [f"  - label: {key}", f"    key: {key}", "    commands:"]
        lines += [f"      - {command}"]
    return "\n".join(lines) + "\n"


TONIGHT = {"runs-it": "pytest -v -s a.py", "fails-tonight": "pytest -v -s b.py"}


@pytest.fixture
def last_night(tmp_path: Path, tmp_repo: Repo) -> Path:
    """Build 1: both steps recorded, published as the previous table."""
    tmp_repo.write(".buildkite/ci_config.yaml", CONFIG)
    tmp_repo.write(".buildkite/test_areas/x.yaml", steps_yaml(**TONIGHT))
    build = Build(tmp_path / "sweep" / "b1", "1", tmp_repo.commit("pipeline"))
    process_file(
        build.job("j0", step_key="runs-it") / "fn.a.txt", [("mod.py", "plain")]
    )
    process_file(
        build.job("j1", step_key="fails-tonight") / "fn.a.txt",
        [("other.py", "elsewhere")],
    )
    out = tmp_path / "previous.json.gz"
    write_table(merge_build(build.finish(), tmp_repo.root), out)
    return out


def tonight(tmp_path: Path, tmp_repo: Repo, **pipeline: str):
    """Build 2, one commit on: fails-tonight's job died before recording."""
    tmp_repo.write(".buildkite/test_areas/x.yaml", steps_yaml(**pipeline))
    tmp_repo.write("README", "moved on\n")
    build = Build(tmp_path / "sweep" / "b2", "2", tmp_repo.commit("tonight"))
    process_file(
        build.job("j2", step_key="runs-it") / "fn.a.txt", [("mod.py", "plain")]
    )
    build.job("j3", step_key="fails-tonight", recorded=False, state="failed")
    return merge_build(build.finish(), tmp_repo.root)


class TestTheFold:
    def test_a_step_with_no_row_tonight_keeps_last_nights(
        self, tmp_path, tmp_repo, last_night
    ):
        rows = tonight(tmp_path, tmp_repo, **TONIGHT)
        assert "fails-tonight" not in rows, "fixture: the step must have no row"
        carried = carry_forward(rows, last_night, tmp_repo.root)
        assert list(carried) == ["fails-tonight"]
        # As recorded: the stamp still names the build and commit it came from.
        assert carried["fails-tonight"].stamp.builds == ["1"]
        assert carried["fails-tonight"].functions == {
            "vllm/other.py": frozenset({"elsewhere"})
        }

    def test_a_step_with_its_own_row_keeps_it(self, tmp_path, tmp_repo, last_night):
        rows = tonight(tmp_path, tmp_repo, **TONIGHT)
        assert "runs-it" not in carry_forward(rows, last_night, tmp_repo.root)

    def test_a_step_whose_commands_changed_is_not_carried(
        self, tmp_path, tmp_repo, last_night
    ):
        """Its row describes a step that no longer exists."""
        changed = {**TONIGHT, "fails-tonight": "pytest -v -s c.py"}
        rows = tonight(tmp_path, tmp_repo, **changed)
        assert carry_forward(rows, last_night, tmp_repo.root) == {}

    def test_a_step_that_is_gone_is_not_carried(self, tmp_path, tmp_repo, last_night):
        rows = tonight(tmp_path, tmp_repo, **{"runs-it": TONIGHT["runs-it"]})
        assert carry_forward(rows, last_night, tmp_repo.root) == {}

    def test_a_table_of_another_version_carries_nothing(
        self, tmp_path, tmp_repo, last_night
    ):
        """`load` refuses it, so a stamp shape change can never smuggle an
        old row into a new table, where its missing fields would read healthy."""
        payload = json.loads(gzip.decompress(last_night.read_bytes()))
        payload["version"] -= 1
        last_night.write_bytes(gzip.compress(json.dumps(payload).encode()))
        rows = tonight(tmp_path, tmp_repo, **TONIGHT)
        assert carry_forward(rows, last_night, tmp_repo.root) == {}

    def test_another_pipelines_table_carries_nothing(
        self, tmp_path, tmp_repo, last_night
    ):
        payload = json.loads(gzip.decompress(last_night.read_bytes()))
        payload["source"]["pipeline"] = "ci-torch-nightly"
        last_night.write_bytes(gzip.compress(json.dumps(payload).encode()))
        rows = tonight(tmp_path, tmp_repo, **TONIGHT)
        assert carry_forward(rows, last_night, tmp_repo.root) == {}

    def test_the_cli_writes_the_carried_row(
        self, tmp_path, tmp_repo, last_night, monkeypatch
    ):
        tonight(tmp_path, tmp_repo, **TONIGHT)
        out = tmp_path / "table.json.gz"
        argv = [
            "ci-build-table",
            str(tmp_repo.root),
            str(tmp_path / "sweep" / "b2"),
            "--previous",
            str(last_night),
            "-o",
            str(out),
        ]
        monkeypatch.setattr(sys, "argv", argv)
        build_mod.main()
        table = load(out)
        assert table.row("fails-tonight") is not None
        assert table.source["carried"] == ["fails-tonight"]


@pytest.fixture
def carried_table(tmp_path, tmp_repo, last_night):
    rows = tonight(tmp_path, tmp_repo, **TONIGHT)
    carried = carry_forward(rows, last_night, tmp_repo.root)
    out = tmp_path / "table.json.gz"
    write_table({**rows, **carried}, out, frozenset(carried))
    return out


def query_for(path: str, *names: str) -> Query:
    return Query(
        base="base",
        head="head",
        files=[
            FileQuery(
                path=path, status=Attribution.ATTRIBUTED, head_names=frozenset(names)
            )
        ],
    )


class TestTheTable:
    def test_it_names_this_builds_commit(self, carried_table, tmp_repo):
        """Not the two commits its rows span: the collect step publishes only a
        table whose commit is the build's, and the publisher refuses one with
        none."""
        table = load(carried_table)
        assert table.commit == tmp_repo.head()
        assert table.build == "2"
        assert table.source["builds"] == ["2"]
        assert table.source["carried"] == ["fails-tonight"]
        assert not table.rejected
        assert publish.main([str(carried_table), "--dry-run"]) == 0

    def test_row_keys_resolve_at_that_commit(self, carried_table, tmp_repo):
        table = load(carried_table)
        # No git: the table names its commit, so this takes the early return.
        assert newest_commit(table, Path("/nonexistent")) == tmp_repo.head()


STEPS = ("vllm_ci:runs-it", "vllm_ci:fails-tonight")


class FakeStep:
    manual_only = False


class TestTheReader:
    def test_a_carried_row_cannot_read_a_silence(self, carried_table):
        table = load(carried_table)
        verdict = table.unusable("fails-tonight")
        assert verdict.evidence is Evidence.ROW_CARRIED and verdict.keep
        assert table.unusable("runs-it") is None

    def test_a_carried_row_never_drops(self, carried_table):
        """fails-tonight's row lacks `plain`. From its own build that silence
        would drop it; carried, it keeps."""
        table = load(carried_table)
        reading = read_pr(
            table,
            Selection(
                selected={s: [] for s in STEPS},
                selected_paths={s: [["vllm/mod.py"]] for s in STEPS},
            ),
            query_for("vllm/mod.py", "plain"),
            {},
            frozenset({"vllm/mod.py", "vllm/other.py"}),
            RowKeys({"vllm_ci"}, {"vllm_ci": 1.0}),
        )
        assert reading.dropped == []
        assert reading.reasons[Evidence.ROW_CARRIED.value] == 1

    def test_a_carried_row_adds(self, carried_table):
        table = load(carried_table)
        reading = read_pr(
            table,
            Selection(
                selected={"vllm_ci:runs-it": []},
                selected_paths={"vllm_ci:runs-it": [["vllm/other.py"]]},
            ),
            query_for("vllm/other.py", "elsewhere"),
            {},
            frozenset({"vllm/mod.py", "vllm/other.py"}),
            RowKeys(
                {"vllm_ci"}, {"vllm_ci": 1.0}, steps={s: FakeStep() for s in STEPS}
            ),
        )
        assert reading.added == ["vllm_ci:fails-tonight"]
