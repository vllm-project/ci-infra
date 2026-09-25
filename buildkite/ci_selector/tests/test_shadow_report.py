# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""ci-validate shadow against recorded Buildkite JSON, no network.

Build 91500 carries one failure of each kind in steps the selector would
skip, plus failures it must not score (a kept step, plumbing, the shadow step
itself). The main builds around its base put kernels-b200 and helion-tests
inside the window and spec-decode and lora-tests outside it.
"""

import argparse
import json
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ci_selector.scripts.fetch import API, Transport
from ci_selector.validate import shadow

FIXTURES = Path(__file__).parent / "fixtures" / "shadow"
PIPELINE = f"{API}/organizations/vllm/pipelines/ci"
BASE_TIME = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)


def _load(name):
    return json.loads((FIXTURES / name).read_text())


class FixtureTransport(Transport):
    """Buildkite's REST API over the fixture files. Records every URL."""

    def __init__(self):
        self.requested: list[str] = []
        self._main = _load("main-builds.json")

    def get_bytes(self, url, accept="application/json"):
        self.requested.append(url)
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        if query.get("page", "1") != "1":
            return b"[]"
        path = parts.path.removeprefix(urllib.parse.urlsplit(PIPELINE).path)
        if path == "/builds" and query.get("branch") == "main":
            lo = shadow._ts(query["created_from"])
            hi = shadow._ts(query["created_to"])
            rows = [b for b in self._main if lo <= shadow._ts(b["created_at"]) < hi]
            return json.dumps(rows).encode()
        if path.startswith("/builds/"):
            number, _, rest = path.removeprefix("/builds/").partition("/")
            if rest == "artifacts":
                sel = FIXTURES / f"selection-{number}.json"
                arts = [{"path": "shadow/selector.log", "download_url": "x://log"}]
                if sel.is_file():
                    arts.append(
                        {
                            "path": "shadow/selection.json",
                            "download_url": f"x://{number}",
                        }
                    )
                return json.dumps(arts).encode()
            build = FIXTURES / f"build-{number}.json"
            return build.read_bytes() if build.is_file() else None
        if url.startswith("x://"):
            return (FIXTURES / f"selection-{url[4:]}.json").read_bytes()
        raise AssertionError(f"unexpected request {url}")


def _score(number, transport=None, cache=None):
    transport = transport or FixtureTransport()
    history = shadow.MainHistory(transport, PIPELINE, cache)
    build_url = f"{PIPELINE}/builds/{number}"
    return shadow.score_build(
        transport.get_json(build_url),
        shadow.shadow_selection(transport, build_url),
        history,
        BASE_TIME,
    )


def _verdicts(row):
    return {(f["step_key"], f["shard"]): f["verdict"] for f in row["skipped_failures"]}


def test_each_failure_in_a_skipped_step_is_classified():
    assert _verdicts(_score(91500)) == {
        ("entrypoints-llm", None): "flake",
        ("helion-tests", None): "pre-existing",
        ("kernels-b200", 2): "pre-existing",
        ("lora-tests", None): "MISS",
        ("spec-decode", None): "MISS",
    }


def test_why_names_the_evidence():
    why = {f["step_key"]: f["why"] for f in _score(91500)["skipped_failures"]}
    assert why["kernels-b200"] == "failed on main build(s) 90800"
    assert why["entrypoints-llm"] == "passed on attempt 2"


def test_counts_ran_against_kept():
    row = _score(91500)
    # Started script jobs, retries folded, the shadow step left out.
    assert row["ran_jobs"] == 10
    assert row["ran_steps"] == 9
    # kernels-core and models-basic, plus the two image builds.
    assert row["kept_jobs"] == 4
    assert row["miss"] == 2
    assert row["base"] == "5eed" * 10


def test_kept_plumbing_and_shadow_failures_are_not_scored():
    keys = {k for k, _ in _verdicts(_score(91500))}
    assert not keys & {"kernels-core", "image-build-amd", "ci-selector-shadow"}


def test_a_main_failure_fixed_by_retry_is_not_pre_existing():
    # lora-tests failed then passed on main build 90820 inside the window.
    assert _verdicts(_score(91500))[("lora-tests", None)] == "MISS"


def test_window_is_configurable():
    transport = FixtureTransport()
    history = shadow.MainHistory(transport, PIPELINE, None)
    url = f"{PIPELINE}/builds/91500"
    row = shadow.score_build(
        transport.get_json(url),
        shadow.shadow_selection(transport, url),
        history,
        BASE_TIME,
        window_hours=120,
    )
    assert _verdicts(row)[("spec-decode", None)] == "pre-existing"


def test_run_all_skips_nothing():
    row = _score(91501)
    assert row["run_all"] is True
    assert row["skipped_failures"] == []
    assert row["kept_jobs"] == row["ran_jobs"]


@pytest.mark.parametrize(
    "number, why",
    [
        (91502, "no shadow/selection.json"),
        (91503, "build is running"),
        (91504, "shadow no-base"),
    ],
)
def test_builds_without_an_answer_are_skipped_with_a_reason(number, why):
    assert why in _score(number)["skip"]


def test_main_history_is_fetched_once_per_day_and_cached(tmp_path):
    transport = FixtureTransport()
    _score(91500, transport, cache=tmp_path)
    days = [u for u in transport.requested if "branch=main" in u]
    # ±48h around 09-05T10:00 touches 09-03..09-07.
    assert len(days) == 5
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        f"main-2026-09-0{d}.json" for d in range(3, 8)
    ]
    again = FixtureTransport()
    _score(91500, again, cache=tmp_path)
    assert not [u for u in again.requested if "branch=main" in u]


def test_cli_writes_table_and_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(shadow, "commit_time", lambda sha, repo: BASE_TIME)
    out = tmp_path / "out.json"
    args = argparse.Namespace(
        builds=[91500, 91501, 91502],
        prs=None,
        org="vllm",
        pipeline="ci",
        json_out=out,
        repo=None,
        window_hours=48,
        cache=None,
        rate=100.0,
    )
    assert shadow.run(args, transport=FixtureTransport()) == 0
    text = capsys.readouterr().out
    assert "build 91500  PR #55755" in text
    assert "MISS 2, pre-existing 2, flake 1" in text
    assert "    MISS         spec-decode" in text
    assert "build 91502  PR #1: SKIP" in text
    assert "TOTAL over 2 builds" in text
    rows = json.loads(out.read_text())
    assert [r["build"] for r in rows] == [91500, 91501, 91502]
    assert rows[0]["base_time_source"] == "commit"


def test_cli_without_a_token_says_so(monkeypatch, capsys):
    for var in ("BUILDKITE_TOKEN", "BK_TOKEN", "BUILDKITE_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    args = argparse.Namespace(rate=1.0)
    assert shadow.run(args) == 2
    assert "BUILDKITE_TOKEN" in capsys.readouterr().err


def test_parser_wires_the_subcommand():
    from ci_selector.validate.__main__ import main

    with pytest.raises(SystemExit) as exc:
        main(["shadow", "--help"])
    assert exc.value.code == 0
