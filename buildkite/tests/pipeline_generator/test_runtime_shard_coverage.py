import json
import os
import stat
import subprocess
import sys

import runtime_shard_coverage as coverage


def _record(shard, ran, expected=None, position=0, finished=True, retry=0, at=1.0):
    return {
        "step": "k",
        "position": position,
        "shard": shard,
        "retry": retry,
        "finished": finished,
        "finished_at": at,
        "expected": expected,
        "planned": ran,
        "ran": ran,
    }


STEPS = {"k": [{"position": 0, "label": "Command (1/1)", "fallback": 0, "owners": [0, 1]}]}


def test_all_tests_ran():
    records = [_record(0, ["a"], expected=["a", "b"]), _record(1, ["b"])]
    [result] = coverage.compare(STEPS, records)
    assert result["tests"] == 2 and result["missed"] == []
    assert result["unfinished"] == [] and result["unverified"] is None
    style, text = coverage.report([result])
    assert style == "success"
    assert text == "Every test ran: 2 tests across 1 sharded steps."


def test_a_test_no_finished_shard_ran_is_a_miss():
    records = [_record(0, ["a"], expected=["a", "b", "c"]), _record(1, ["b"])]
    [result] = coverage.compare(STEPS, records)
    assert result["missed"] == ["c"] and result["not_run"] == []
    style, text = coverage.report([result])
    assert style == "error"
    assert text == (
        "### :x: 1 tests ran in no shard, though every shard finished\n"
        "**k**, Command (1/1):\n- `c`"
    )


def test_a_test_with_any_outcome_counts_because_ran_is_any_report():
    records = [_record(0, ["a", "b"], expected=["a", "b"]), _record(1, [])]
    assert coverage.compare(STEPS, records)[0]["missed"] == []


def test_an_unfinished_shard_is_apart_from_misses():
    records = [
        _record(0, ["a"], expected=["a", "b"]),
        _record(1, [], finished=False),
    ]
    [result] = coverage.compare(STEPS, records)
    assert result["missed"] == [] and result["unfinished"] == [1]
    assert result["not_run"] == ["b"]
    style, text = coverage.report([result])
    assert style == "warning"
    assert text == (
        "### Shards that didn't finish, so these tests may not have run\n"
        "**k**, Command (1/1): shard 2\n- `b`"
    )


def test_a_shard_with_no_record_counts_as_unfinished():
    [result] = coverage.compare(STEPS, [_record(0, ["a"], expected=["a", "b"])])
    assert result["unfinished"] == [1] and result["not_run"] == ["b"]


def test_a_dead_first_shard_still_gives_its_list():
    records = [_record(0, [], expected=["a", "b"], finished=False), _record(1, ["b"])]
    [result] = coverage.compare(STEPS, records)
    assert result["unfinished"] == [0] and result["not_run"] == ["a"]


def test_no_expected_list_cannot_be_checked():
    [result] = coverage.compare(STEPS, [_record(0, ["a"]), _record(1, ["b"])])
    assert result["unverified"] == "shard 1 recorded no test list"
    style, text = coverage.report([result])
    assert style == "warning"
    assert text == (
        "### Couldn't check\n**k**, Command (1/1): shard 1 recorded no test list"
    )


def test_nothing_uploaded_is_unverified_not_clean():
    [result] = coverage.compare(STEPS, [])
    assert result["unverified"] and coverage.report([result])[0] == "warning"


def test_latest_finished_attempt_wins_over_an_earlier_and_a_later_unfinished_one():
    records = [
        _record(0, ["a"], expected=["a", "b"]),
        _record(1, [], retry=0),  # finished, ran nothing it should have
        _record(1, ["b"], retry=1),  # the retry that counts
        _record(1, [], retry=2, finished=False),  # died, so doesn't count
    ]
    [result] = coverage.compare(STEPS, records)
    assert result["missed"] == [] and result["unfinished"] == []


def test_an_earlier_attempt_does_not_cover_a_later_finished_one():
    records = [
        _record(0, ["a"], expected=["a", "b"]),
        _record(1, ["b"], retry=0),
        _record(1, [], retry=1),
    ]
    assert coverage.compare(STEPS, records)[0]["missed"] == ["b"]


def test_same_attempt_number_takes_the_later_record():
    latest = coverage.latest_records(
        [_record(1, ["old"], at=1.0), _record(1, ["new"], at=2.0)]
    )
    assert latest[("k", 0, 1)]["ran"] == ["new"]
    latest = coverage.latest_records(
        [_record(1, ["new"], at=2.0), _record(1, ["old"], at=1.0)]
    )
    assert latest[("k", 0, 1)]["ran"] == ["new"]


def test_contract_with_the_generator():
    assert coverage.STEPS_ENV == "RUNTIME_SHARD_COVERAGE_STEPS"
    assert coverage.MAX_IDS_SHOWN == 30


def test_a_list_of_exactly_the_cap_has_no_more_line():
    expected = [f"t{i}" for i in range(coverage.MAX_IDS_SHOWN)]
    [result] = coverage.compare(STEPS, [_record(0, [], expected=expected), _record(1, [])])
    assert "more" not in coverage.report([result])[1]


def test_several_unfinished_shards_are_named_together():
    [result] = coverage.compare(STEPS, [_record(0, [], expected=["a"], finished=False)])
    assert "shard 1, 2\n" in coverage.report([result])[1]


def test_commands_are_compared_apart():
    steps = {
        "k": [
            {"position": 0, "label": "A", "fallback": 0, "owners": [0]},
            {"position": 1, "label": "B", "fallback": 1, "owners": [1]},
        ]
    }
    records = [
        _record(0, ["same"], expected=["same"], position=0),
        _record(1, [], expected=["same"], position=1),
    ]
    first, second = coverage.compare(steps, records)
    assert first["missed"] == [] and second["missed"] == ["same"]


def test_the_annotation_caps_its_lists_and_points_at_the_log():
    expected = [f"t{i}" for i in range(coverage.MAX_IDS_SHOWN + 5)]
    [result] = coverage.compare(STEPS, [_record(0, [], expected=expected), _record(1, [])])
    _, text = coverage.report([result])
    assert text.count("- `t") == coverage.MAX_IDS_SHOWN
    assert text.endswith("- and 5 more, listed in this job's log")


def test_main_reports_exit_and_annotation(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "x" / ".runtime-shard-coverage" / "job"
    folder.mkdir(parents=True)
    (folder / "0.json").write_text(json.dumps(_record(0, ["a"], expected=["a", "gone"])))
    (folder / "1.json").write_text(json.dumps(_record(1, [])))
    calls = []

    def fake_run(command, check):
        assert check is False  # an agent hiccup must not hide the result
        calls.append(command)

    monkeypatch.setenv(coverage.STEPS_ENV, json.dumps(STEPS))
    monkeypatch.setattr(coverage, "_read_records", lambda _: coverage_records(folder))
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert coverage.main() == 1
    assert calls[0][:3] == ["buildkite-agent", "artifact", "download"]
    assert calls[0][3] == ".runtime-shard-coverage/*/*.json"
    assert calls[1][:5] == [
        "buildkite-agent",
        "annotate",
        "--context",
        "runtime-shard-coverage",
        "--style",
    ]
    assert calls[1][5] == "error" and "`gone`" in calls[1][6]
    out = capsys.readouterr().out
    assert "missed: k: gone\n" in out and "### :x:" in out


def coverage_records(folder):
    return [json.loads(path.read_text()) for path in sorted(folder.iterdir())]


def test_main_exits_clean_without_a_miss(monkeypatch):
    monkeypatch.setenv(coverage.STEPS_ENV, json.dumps(STEPS))
    monkeypatch.setattr(subprocess, "run", lambda command, check: None)
    monkeypatch.setattr(coverage, "_read_records", lambda _: [])
    assert coverage.main() == 0  # unverified warns but stays green


def test_read_records_finds_what_download_wrote(tmp_path):
    folder = tmp_path / ".runtime-shard-coverage" / "job"
    folder.mkdir(parents=True)
    (folder / "0.json").write_text(json.dumps({"a": 1}))
    assert coverage._read_records(str(tmp_path)) == [{"a": 1}]


def test_the_script_end_to_end_with_an_agent_on_the_path(tmp_path):
    """As the job runs it: a file of its own, with a real artifact tree."""
    records = tmp_path / "artifacts" / ".runtime-shard-coverage" / "job"
    records.mkdir(parents=True)
    (records / "0.json").write_text(json.dumps(_record(0, ["a"], expected=["a", "gone"])))
    (records / "1.json").write_text(json.dumps(_record(1, [])))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    agent = bin_dir / "buildkite-agent"
    agent.write_text(
        f"""#!/bin/sh
if [ "$1" = artifact ]; then cp -r {tmp_path}/artifacts/. "$4"; else echo "$@" >> {tmp_path}/annotated; fi
"""
    )
    agent.chmod(agent.stat().st_mode | stat.S_IEXEC)
    run = subprocess.run(
        [sys.executable, coverage.__file__],
        env=dict(
            os.environ,
            PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            **{coverage.STEPS_ENV: json.dumps(STEPS)},
        ),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 1, run.stderr
    assert "--style error" in (tmp_path / "annotated").read_text()
    assert "gone" in (tmp_path / "annotated").read_text()
