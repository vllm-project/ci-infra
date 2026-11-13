import json
import os
import stat
import subprocess
import sys

import runtime_shard_coverage as coverage


def _record(
    shard,
    ran,
    expected=None,
    position=0,
    finished=True,
    retry=0,
    at=1.0,
    build_id=None,
    planned=None,
):
    record = {
        "step": "k",
        "position": position,
        "shard": shard,
        "retry": retry,
        "finished": finished,
        "finished_at": at,
        "expected": expected,
        "planned": ran if planned is None else planned,
        "ran": ran,
    }
    if build_id is not None:
        record["build_id"] = build_id
    return record


STEPS = {
    "k": [{"position": 0, "label": "Command (1/1)", "fallback": 0, "owners": [0, 1]}]
}
SOFT_STEPS = {
    "k": [
        {
            "position": 0,
            "label": "Command (1/1)",
            "fallback": 0,
            "owners": [0, 1],
            "soft_fail": True,
        }
    ]
}


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


def test_an_unfinished_shard_is_apart_from_misses_but_fails_closed():
    records = [
        _record(0, ["a"], expected=["a", "b"]),
        _record(1, [], finished=False, planned=["b"]),
    ]
    [result] = coverage.compare(STEPS, records)
    assert result["missed"] == [] and result["unfinished"] == [1]
    assert result["not_run"] == ["b"]
    style, text = coverage.report([result])
    # Not a miss (the shard may have run them), but unproven: it fails.
    assert style == "error"
    assert text == (
        "### Shards that didn't finish, so these tests may not have run\n"
        "**k**, Command (1/1): shard 2\n- `b`"
    )


def test_a_finished_shards_miss_is_not_hidden_by_an_unfinished_shard():
    records = [
        _record(0, [], expected=["a", "b"], planned=["a"]),
        _record(1, [], finished=False, planned=["b"]),
    ]
    [result] = coverage.compare(SOFT_STEPS, records)
    assert result["missed"] == ["a"] and result["not_run"] == ["b"]
    assert result["unfinished"] == [1]
    style, text = coverage.report([result])
    assert style == "error"
    assert text.startswith("### :x: 1 tests ran in no finished shard\n")


def test_an_unknown_unfinished_plan_does_not_invent_a_definite_miss():
    unknown = _record(1, [], finished=False)
    unknown["planned"] = None
    records = [
        _record(0, [], expected=["a", "b"], planned=[]),
        unknown,
    ]
    [result] = coverage.compare(SOFT_STEPS, records)
    assert result["missed"] == [] and result["not_run"] == ["a", "b"]


def test_a_shard_with_no_record_fails_closed():
    # The shard left nothing: its tests may have run, so not a miss, but
    # nothing proves coverage, so the check fails rather than passes blind.
    [result] = coverage.compare(STEPS, [_record(0, ["a"], expected=["a", "b"])])
    assert result["unfinished"] == [] and result["missed"] == []
    assert result["not_run"] == []  # which tests it owned can't be known
    assert result["unverified"] == "shard 2 recorded nothing"
    style, text = coverage.report([result])
    assert style == "error"
    assert text == (
        "### Couldn't check\n**k**, Command (1/1): shard 2 recorded nothing"
    )


def test_a_missing_record_next_to_an_unfinished_shard_hides_its_not_run():
    # Shard 1 recorded it didn't finish; shard 2 left nothing. Shard 2's gap
    # makes attribution unknowable, so the command reports unverified, not
    # not_run tests that shard 2 may in fact have run.
    records = [_record(0, ["a"], expected=["a", "b"], finished=False)]
    [result] = coverage.compare(STEPS, records)
    assert result["unfinished"] == [0] and result["not_run"] == []
    assert result["unverified"] == "shard 2 recorded nothing"
    assert coverage.report([result])[0] == "error"


def test_a_dead_first_shard_still_gives_its_list():
    records = [
        _record(0, [], expected=["a", "b"], finished=False, planned=["a", "b"]),
        _record(1, ["b"]),
    ]
    [result] = coverage.compare(STEPS, records)
    assert result["unfinished"] == [0] and result["not_run"] == ["a"]


def test_no_expected_list_cannot_be_checked_and_fails_closed():
    [result] = coverage.compare(STEPS, [_record(0, ["a"]), _record(1, ["b"])])
    assert result["unverified"] == "shard 1 recorded no test list"
    style, text = coverage.report([result])
    assert style == "error"
    assert text == (
        "### Couldn't check\n**k**, Command (1/1): shard 1 recorded no test list"
    )


def test_nothing_uploaded_is_unverified_not_clean():
    [result] = coverage.compare(STEPS, [])
    assert result["unverified"] == (
        "shard 1 recorded no test list; shards 1, 2 recorded nothing"
    )
    assert coverage.report([result])[0] == "error"


def test_a_test_two_shards_ran_is_a_duplicate_naming_both():
    records = [_record(0, ["a", "b"], expected=["a", "b"]), _record(1, ["b"])]
    [result] = coverage.compare(STEPS, records)
    assert result["duplicated"] == {"b": [0, 1]} and result["missed"] == []


def test_a_retried_shard_or_a_rerun_inside_one_shard_is_not_a_duplicate():
    records = [
        _record(0, ["a", "a"], expected=["a", "b"]),  # rerun of a flaky test
        _record(1, ["b"], retry=0),
        _record(1, ["b"], retry=1),  # the same shard retried
    ]
    assert coverage.compare(STEPS, records)[0]["duplicated"] == {}


def test_a_duplicate_alone_is_a_warning_naming_the_test_and_its_shards():
    records = [_record(0, ["a", "b"], expected=["a", "b"]), _record(1, ["b"])]
    style, text = coverage.report(coverage.compare(STEPS, records))
    assert style == "warning"
    assert text == (
        "### Tests that ran in more than one shard\n"
        "**k**, Command (1/1):\n- `b`: shards 1, 2"
    )


def test_a_miss_stays_an_error_when_a_duplicate_is_also_listed():
    records = [_record(0, ["a", "b"], expected=["a", "b", "c"]), _record(1, ["b"])]
    style, text = coverage.report(coverage.compare(STEPS, records))
    assert style == "error"
    assert "- `c`" in text and "- `b`: shards 1, 2" in text


def test_the_duplicate_list_is_capped_like_the_others():
    many = [f"t{i}" for i in range(coverage.MAX_IDS_SHOWN + 2)]
    records = [_record(0, many, expected=many), _record(1, many)]
    _, text = coverage.report(coverage.compare(STEPS, records))
    assert text.count(": shards 1, 2") == coverage.MAX_IDS_SHOWN
    assert text.endswith("- and 2 more, listed in this job's log")


def test_only_finished_shards_count_towards_a_duplicate():
    records = [
        _record(0, ["a", "b"], expected=["a", "b"]),
        _record(1, ["b"], finished=False),
    ]
    assert coverage.compare(STEPS, records)[0]["duplicated"] == {}


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


def test_a_soft_fail_step_missed_test_is_still_an_error():
    # Soft fail lets the step's tests fail, not the sharding: a test no
    # finished shard ran is a definite miss, so it stays red.
    records = [_record(0, ["a"], expected=["a", "b"]), _record(1, [])]
    [result] = coverage.compare(SOFT_STEPS, records)
    assert result["missed"] == ["b"] and result["soft_fail"] is True
    style, text = coverage.report([result])
    assert style == "error"
    assert "### :x: 1 tests ran in no shard" in text


def test_a_soft_fail_step_unfinished_shard_is_warning_only():
    # The step may fail, so a shard that didn't finish proves nothing new:
    # it reads as a warning, matching the build's soft-fail semantics.
    records = [
        _record(0, ["a"], expected=["a", "b"]),
        _record(1, [], finished=False, planned=["b"]),
    ]
    [result] = coverage.compare(SOFT_STEPS, records)
    assert result["unfinished"] == [1] and result["not_run"] == ["b"]
    style, text = coverage.report([result])
    assert style == "warning"
    assert "**k**, Command (1/1): shard 2 (soft-fail step)" in text


def test_a_soft_fail_step_unverified_fails_closed():
    [result] = coverage.compare(SOFT_STEPS, [_record(0, ["a"], expected=["a", "b"])])
    assert result["unverified"] == "shard 2 recorded nothing"
    style, text = coverage.report([result])
    assert style == "error"
    assert "shard 2 recorded nothing (soft-fail step)" in text


def test_main_only_relaxes_unverified_evidence_for_a_soft_failed_step(monkeypatch):
    monkeypatch.setenv(coverage.STEPS_ENV, json.dumps(SOFT_STEPS))
    monkeypatch.setattr(
        coverage,
        "_read_records",
        lambda _: [_record(0, ["a"], expected=["a", "b"])],
    )
    outcomes = iter(["soft_failed\n", "passed\n", None, OSError("agent unavailable")])
    styles = []

    def fake_run(command, check, **kwargs):
        if command[:3] == ["buildkite-agent", "step", "get"]:
            assert command == [
                "buildkite-agent",
                "step",
                "get",
                "outcome",
                "--step",
                "k",
            ]
            assert check is False
            assert kwargs == {"capture_output": True, "text": True, "timeout": 60}
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return subprocess.CompletedProcess(
                command, int(outcome is None), stdout=outcome or ""
            )
        if command[:2] == ["buildkite-agent", "annotate"]:
            styles.append(command[command.index("--style") + 1])
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert coverage.main() == 0
    assert coverage.main() == 1
    assert coverage.main() == 1
    assert coverage.main() == 1
    assert styles == ["warning", "error", "error", "error"]


def test_soft_failed_outcomes_are_independent_for_each_step(monkeypatch):
    steps = {
        "hard": [{"position": 0, "label": "Hard", "fallback": 0, "owners": [0]}],
        "soft-a": [dict(SOFT_STEPS["k"][0])],
        "soft-b": [dict(SOFT_STEPS["k"][0])],
    }
    results = coverage.compare(steps, [])
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command[-1])
        outcome = "soft_failed\n" if command[-1] == "soft-a" else "passed\n"
        return subprocess.CompletedProcess(command, 0, stdout=outcome)

    monkeypatch.setattr(subprocess, "run", fake_run)
    coverage._relax_soft_failed_unverified(results)
    assert calls == ["soft-a", "soft-b"]
    assert [result.get("soft_failed") for result in results] == [None, True, False]


def test_soft_fail_defaults_to_false_so_todays_behavior_is_kept():
    [result] = coverage.compare(STEPS, [_record(0, ["a"], expected=["a", "b"])])
    assert result["soft_fail"] is False
    assert coverage.report([result])[0] == "error"


def test_a_hard_steps_gap_stays_an_error_next_to_a_soft_fail_steps_gap():
    steps = {
        "soft": [
            {
                "position": 0,
                "label": "Soft",
                "fallback": 0,
                "owners": [0],
                "soft_fail": True,
            }
        ],
        "hard": [{"position": 0, "label": "Hard", "fallback": 0, "owners": [0]}],
    }
    records = [
        dict(
            _record(0, [], expected=["a"], finished=False, planned=["a"]),
            step="soft",
        ),
        dict(
            _record(0, [], expected=["a"], finished=False, planned=["a"]),
            step="hard",
        ),
    ]
    soft, hard = coverage.compare(steps, records)
    assert soft["unfinished"] == [0] and hard["unfinished"] == [0]
    assert coverage.report([soft])[0] == "warning"  # the soft-fail step alone
    assert coverage.report([soft, hard])[0] == "error"


def test_a_list_of_exactly_the_cap_has_no_more_line():
    expected = [f"t{i}" for i in range(coverage.MAX_IDS_SHOWN)]
    [result] = coverage.compare(
        STEPS, [_record(0, [], expected=expected), _record(1, [])]
    )
    assert "more" not in coverage.report([result])[1]


def test_several_unfinished_shards_are_named_together():
    records = [
        _record(0, [], expected=["a"], finished=False, planned=["a"]),
        _record(1, [], finished=False),
    ]
    [result] = coverage.compare(STEPS, records)
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
    [result] = coverage.compare(
        STEPS, [_record(0, [], expected=expected), _record(1, [])]
    )
    _, text = coverage.report([result])
    assert text.count("- `t") == coverage.MAX_IDS_SHOWN
    assert text.endswith("- and 5 more, listed in this job's log")


def test_records_from_another_build_are_ignored_when_the_build_is_known():
    # A stale artifact from an earlier build must not pass this build's
    # check: with every record foreign, nothing is proven, so unverified.
    records = [
        _record(0, ["a", "b"], expected=["a", "b"], build_id="old-build"),
        _record(1, [], build_id="old-build"),
    ]
    [result] = coverage.compare(STEPS, records, build_id="this-build")
    assert result["unverified"] == (
        "shard 1 recorded no test list; shards 1, 2 recorded nothing"
    )
    assert coverage.report([result])[0] == "error"


def test_a_record_without_a_build_id_is_stale_when_the_build_is_known():
    # Records from before the field existed can't be told from stale ones,
    # so they don't count either.
    records = [_record(0, ["a", "b"], expected=["a", "b"]), _record(1, [])]
    [result] = coverage.compare(STEPS, records, build_id="this-build")
    assert result["unverified"] is not None and result["missed"] == []


def test_only_the_current_builds_records_count():
    records = [
        _record(0, ["a"], expected=["a", "b"], build_id="this-build"),
        _record(1, ["b"], build_id="this-build"),
        # A newer-looking stale record that ran nothing must not displace
        # this build's shard 1 record and read as a miss.
        _record(1, [], build_id="old-build", at=2.0),
    ]
    [result] = coverage.compare(STEPS, records, build_id="this-build")
    assert result["missed"] == [] and result["duplicated"] == {}
    assert result["unverified"] is None
    assert coverage.report([result])[0] == "success"


def test_without_a_build_id_every_record_counts():
    # compare() stays backward compatible: no build ID, no filtering.
    records = [
        _record(0, ["a", "b"], expected=["a", "b"], build_id="old-build"),
        _record(1, []),
    ]
    [result] = coverage.compare(STEPS, records)
    assert result["missed"] == [] and result["unverified"] is None


def test_main_reports_exit_and_annotation(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "x" / ".runtime-shard-coverage" / "job"
    folder.mkdir(parents=True)
    (folder / "0.json").write_text(
        json.dumps(_record(0, ["a"], expected=["a", "gone"]))
    )
    (folder / "1.json").write_text(json.dumps(_record(1, [])))
    calls = []
    annotated = []

    def fake_run(command, check, **kwargs):
        assert check is False  # an agent hiccup must not hide the result
        calls.append(command)
        if command[:2] == ["buildkite-agent", "annotate"]:
            annotated.append(kwargs["input"])

    monkeypatch.setenv(coverage.STEPS_ENV, json.dumps(STEPS))
    monkeypatch.delenv("BUILDKITE_STEP_KEY", raising=False)
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
    assert calls[1][5] == "error" and "`gone`" in annotated[0]
    out = capsys.readouterr().out
    assert "missed: k: gone\n" in out and "### :x:" in out


def test_main_logs_every_duplicate_and_downloads_every_attempt(monkeypatch, capsys):
    many = [f"t{i}" for i in range(coverage.MAX_IDS_SHOWN + 1)]
    records = [_record(0, many, expected=many), _record(1, many)]
    calls = []

    def fake_run(command, check, **kwargs):
        calls.append((command, kwargs.get("env") or {}))

    monkeypatch.setenv(coverage.STEPS_ENV, json.dumps(STEPS))
    monkeypatch.setattr(coverage, "_read_records", lambda _: records)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert coverage.main() == 0  # a duplicate alone doesn't fail the job
    download, env = calls[0]
    assert download[:3] == ["buildkite-agent", "artifact", "download"]
    # An earlier finished attempt only reaches latest_records() this way.
    assert env["BUILDKITE_AGENT_INCLUDE_RETRIED_JOBS"] == "true"
    out = capsys.readouterr().out
    last = many[-1]
    assert f"duplicated: k: {last}: shards 1, 2\n" in out  # past the cap


def coverage_records(folder):
    return [json.loads(path.read_text()) for path in sorted(folder.iterdir())]


def test_main_fails_closed_with_no_records(monkeypatch):
    monkeypatch.setenv(coverage.STEPS_ENV, json.dumps(STEPS))
    monkeypatch.delenv("BUILDKITE_STEP_KEY", raising=False)
    calls = []

    def fake_run(command, check, **kwargs):
        calls.append(command)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(coverage, "_read_records", lambda _: [])
    assert coverage.main() == 1  # no evidence: unverified fails, not passes
    assert calls[1][:5] == [
        "buildkite-agent",
        "annotate",
        "--context",
        "runtime-shard-coverage",
        "--style",
    ]
    assert calls[1][5] == "error"


def test_main_checks_only_the_current_builds_records(monkeypatch):
    # main passes BUILDKITE_BUILD_ID, so a stale record can't cover for or
    # accuse this build's shards: only the current build's records count.
    records = [
        _record(0, ["a"], expected=["a", "b"], build_id="this-build"),
        _record(1, ["b"], build_id="this-build"),
        # A newer-looking stale record that ran nothing: unfiltered it would
        # displace shard 1's record and read as a miss.
        _record(1, [], build_id="old-build", at=2.0),
    ]
    calls = []

    def fake_run(command, check, **kwargs):
        calls.append(command)

    monkeypatch.setenv(coverage.STEPS_ENV, json.dumps(STEPS))
    monkeypatch.setenv("BUILDKITE_BUILD_ID", "this-build")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(coverage, "_read_records", lambda _: records)
    assert coverage.main() == 0
    assert calls[1][5] == "success"


def test_main_fails_closed_when_every_record_is_from_another_build(monkeypatch):
    records = [
        _record(0, ["a", "b"], expected=["a", "b"], build_id="old-build"),
        _record(1, [], build_id="old-build"),
    ]
    calls = []

    def fake_run(command, check, **kwargs):
        calls.append(command)

    monkeypatch.setenv(coverage.STEPS_ENV, json.dumps(STEPS))
    monkeypatch.setenv("BUILDKITE_BUILD_ID", "this-build")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(coverage, "_read_records", lambda _: records)
    assert coverage.main() == 1  # nothing proven for this build
    assert calls[1][5] == "error"


def test_main_without_a_build_id_uses_every_record(monkeypatch):
    # A local run has no BUILDKITE_BUILD_ID: compare() sees every record.
    records = [
        _record(0, ["a", "b"], expected=["a", "b"], build_id="old-build"),
        _record(1, []),
    ]
    calls = []

    def fake_run(command, check, **kwargs):
        calls.append(command)

    monkeypatch.setenv(coverage.STEPS_ENV, json.dumps(STEPS))
    monkeypatch.delenv("BUILDKITE_BUILD_ID", raising=False)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(coverage, "_read_records", lambda _: records)
    assert coverage.main() == 0
    assert calls[1][5] == "success"


def test_main_annotates_under_its_step_key(monkeypatch):
    """A second coverage job (a "Run more jobs" round) keeps its report apart
    from the pipeline's: same script, and the agent sets BUILDKITE_STEP_KEY
    to the job's own key."""
    monkeypatch.setenv(coverage.STEPS_ENV, json.dumps(STEPS))
    monkeypatch.setenv("BUILDKITE_STEP_KEY", "runtime-shard-coverage-2")
    calls = []

    def fake_run(command, check, **kwargs):
        calls.append(command)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(coverage, "_read_records", lambda _: [])
    assert coverage.main() == 1
    assert calls[1][:4] == [
        "buildkite-agent",
        "annotate",
        "--context",
        "runtime-shard-coverage-2",
    ]


def test_read_records_finds_what_download_wrote(tmp_path):
    folder = tmp_path / ".runtime-shard-coverage" / "job"
    folder.mkdir(parents=True)
    (folder / "0.json").write_text(json.dumps({"a": 1}))
    assert coverage._read_records(str(tmp_path)) == [{"a": 1}]


def test_a_cut_off_record_is_skipped_and_the_rest_still_read(tmp_path, capsys):
    folder = tmp_path / ".runtime-shard-coverage" / "job"
    folder.mkdir(parents=True)
    (folder / "0.json").write_text('{"step": "k", "ran": ["a"')  # killed mid-write
    (folder / "1.json").write_text(json.dumps({"a": 1}))
    assert coverage._read_records(str(tmp_path)) == [{"a": 1}]
    [line] = capsys.readouterr().out.splitlines()  # named in the log
    assert line.startswith("runtime-shard coverage: skipped unreadable ")
    assert "0.json: " in line and line.endswith("(char 25)")


def test_the_script_end_to_end_with_an_agent_on_the_path(tmp_path):
    """As the job runs it: a file of its own, with a real artifact tree."""
    records = tmp_path / "artifacts" / ".runtime-shard-coverage" / "job"
    records.mkdir(parents=True)
    (records / "0.json").write_text(
        json.dumps(_record(0, ["a"], expected=["a", "gone"]))
    )
    (records / "1.json").write_text(json.dumps(_record(1, [])))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    agent = bin_dir / "buildkite-agent"
    agent.write_text(
        f"""#!/bin/sh
if [ "$1" = artifact ]; then cp -r {tmp_path}/artifacts/. "$4"; else echo "$@" >> {tmp_path}/annotated; cat >> {tmp_path}/annotated; fi
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


def test_a_large_annotation_is_passed_on_stdin_not_as_an_argument(monkeypatch):
    text = "x" * 140_000
    monkeypatch.setenv(coverage.STEPS_ENV, json.dumps(STEPS))
    monkeypatch.setattr(coverage, "_read_records", lambda _: [])
    monkeypatch.setattr(coverage, "report", lambda _: ("warning", text))
    calls = []

    def fake_run(command, check, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert coverage.main() == 0
    command, kwargs = calls[-1]
    assert text not in command
    assert kwargs["input"] == text
    assert kwargs["text"] is True
