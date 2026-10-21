"""The selector gate: when the test selector may choose a PR build's steps,
what it records, and that it never makes a build fail."""

import json
import os
import subprocess

import pytest

import selector_gate
from constants import Platform

PR = {
    "name": "vllm_ci",
    "pull_request": "12345",
    "branch": "someone:feature",
    "nightly": "0",
    "run_all": False,
    "only_step_keys": None,
    "merge_base_commit": "a" * 40,
    "commit": "b" * 40,
    "list_file_diff": ["vllm/foo.py"],
}


def _emission(keys, omit=False, reason=""):
    return lambda **kw: {
        "emission": {"omit": omit, "reason": reason, "keys": keys},
        "records": {"fnrec": {"build": 1}},
        "log": "coverage: ...",
        "ci_infra_commit": "c" * 40,
        "explain": {"version": 1, "steps": {"vllm_ci:y": {"key": "y"}}},
    }


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.setenv(selector_gate.SELECTOR_ENV_VAR, "on")


def test_on_unless_switched_off(monkeypatch):
    monkeypatch.delenv(selector_gate.SELECTOR_ENV_VAR)
    assert selector_gate.mode() == "on"
    # a typo must not quietly turn selection off
    monkeypatch.setenv(selector_gate.SELECTOR_ENV_VAR, "bogus")
    assert selector_gate.mode() == "on"
    monkeypatch.setenv(selector_gate.SELECTOR_ENV_VAR, "Shadow")
    assert selector_gate.mode() == "shadow"
    monkeypatch.setenv(selector_gate.SELECTOR_ENV_VAR, "OFF")
    assert selector_gate.mode() == ""


def test_a_pr_build_records_the_selectors_keys():
    record = selector_gate.decide(PR, ["x", "y", "z"], _emission(["y", "a"]))
    assert record["selector_keys"] == ["a", "y"]
    assert record["explain"]["steps"]["vllm_ci:y"]["key"] == "y"
    assert record["legacy_keys"] == ["x", "y", "z"]
    assert record["fallback"] is None and record["skipped"] is None
    assert record["base"] == PR["merge_base_commit"] and record["head"] == PR["commit"]
    assert record["records"] == {"fnrec": {"build": 1}}
    assert record["ci_infra_commit"] == "c" * 40


@pytest.mark.parametrize(
    "change, why",
    [
        ({"branch": "releases/v0.20"}, "release branch"),
        ({"nightly": "1"}, "nightly"),
        ({"run_all": True}, "run-all"),
        ({"only_step_keys": frozenset({"x"})}, "VLLM_CI_ONLY_STEP_KEYS"),
        ({"platform": Platform.AMD}, "AMD-only"),
    ],
)
def test_builds_that_keep_todays_rules(change, why):
    def boom(**kw):
        raise AssertionError("the selector must not run")

    record = selector_gate.decide({**PR, **change}, ["x"], boom)
    assert why in record["skipped"] and record["selector_keys"] is None


def test_a_cuda_only_build_is_still_selected():
    assert selector_gate.skip_reason({**PR, "platform": Platform.CUDA}) is None


def test_a_selector_failure_keeps_todays_rules():
    def boom(**kw):
        raise RuntimeError("clone failed")

    record = selector_gate.decide(PR, ["x"], boom)
    assert record["selector_keys"] is None
    assert "clone failed" in record["fallback"]


def test_the_selectors_own_refusal_keeps_todays_rules():
    record = selector_gate.decide(
        PR, ["x"], _emission([], omit=True, reason="run-all: rust")
    )
    assert record["selector_keys"] is None
    assert "run-all: rust" in record["fallback"]


def test_the_record_is_written_as_json(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")  # no buildkite-agent: the upload is skipped
    record = selector_gate.decide(PR, ["x"], _emission(["x"]))
    path = selector_gate.write_and_upload(record, str(tmp_path))
    assert os.path.basename(path) == selector_gate.DECISION_FILE
    assert json.load(open(path))["selector_keys"] == ["x"]


# ---- through the generator -------------------------------------------------

import buildkite_step  # noqa: E402
import pipeline_generator as pg  # noqa: E402
import unblock_jobs  # noqa: E402
from step import Step  # noqa: E402


def _steps():
    return [
        Step(label="Image", key="image-build", commands=["build"]),
        Step(
            label="A",
            key="test-a",
            depends_on=["image-build"],
            commands=["a"],
            source_file_dependencies=["vllm/"],
        ),
        Step(
            label="B",
            key="test-b",
            depends_on=["image-build"],
            commands=["b"],
            source_file_dependencies=["tests/b/"],
        ),
    ]


@pytest.fixture
def pr_config(fake_global_config, monkeypatch):
    fake_global_config.update(
        pull_request="123",
        merge_base_commit="a" * 40,
        commit="b" * 40,
        list_file_diff=["vllm/foo.py"],
    )
    monkeypatch.setattr(pg, "get_global_config", lambda: fake_global_config)
    return fake_global_config


def test_on_mode_hands_the_selectors_keys_to_the_generator(pr_config, monkeypatch):
    monkeypatch.setattr(selector_gate, "run_ci_select", _emission(["test-b"]))
    record = pg._selector_decision(_steps())
    assert record["applied"] is True
    assert pr_config.get("selector_step_keys") == frozenset({"test-b"})
    # today's rules: the image build always runs, and vllm/ matched test-a
    assert record["legacy_keys"] == ["image-build", "test-a"]
    selected, keys = pg.select_steps_and_dependencies(
        _steps(), pr_config.get("selector_step_keys")
    )
    assert keys == frozenset({"image-build", "test-b"}), "prerequisites come along"


def test_shadow_mode_records_but_changes_nothing(pr_config, monkeypatch):
    monkeypatch.setenv(selector_gate.SELECTOR_ENV_VAR, "shadow")
    monkeypatch.setattr(selector_gate, "run_ci_select", _emission(["test-b"]))
    record = pg._selector_decision(_steps())
    assert record["selector_keys"] == ["test-b"] and record["applied"] is False
    assert pr_config.get("selector_step_keys") is None


def test_an_unknown_key_keeps_todays_rules(pr_config, monkeypatch):
    monkeypatch.setattr(selector_gate, "run_ci_select", _emission(["test-b", "gone"]))
    record = pg._selector_decision(_steps())
    assert record["applied"] is False and "gone" in record["fallback"]
    assert pr_config.get("selector_step_keys") is None


def test_the_gate_is_inert_when_off(pr_config, monkeypatch):
    monkeypatch.setenv(selector_gate.SELECTOR_ENV_VAR, "off")
    assert pg._selector_decision(_steps()) is None
    assert pr_config.get("selector_step_keys") is None


def test_optional_steps_stay_optional(pr_config, monkeypatch):
    """The selector named a nightly-only step on a PR (vllm#61804): it is
    dropped, recorded, and the rest still applies."""
    steps = _steps() + [
        Step(
            label="Nightly",
            key="test-nightly",
            optional=True,
            commands=["n"],
            depends_on=["image-build"],
        ),
    ]
    monkeypatch.setattr(
        selector_gate,
        "run_ci_select",
        _emission(["test-b", "test-nightly", "amd-test-nightly"]),
    )
    record = pg._selector_decision(steps)
    assert record["applied"] is True
    assert record["selector_keys"] == ["test-b"]
    assert record["selector_optional_dropped"] == ["amd-test-nightly", "test-nightly"]
    assert pr_config.get("selector_step_keys") == frozenset({"test-b"})


def test_steps_the_selector_skips_are_offered_in_run_more_jobs(
    pr_config, monkeypatch, tmp_path
):
    """The selector's keys pick what runs; every other step stays in the
    pipeline as deferred, so "Run more jobs" can still add it."""
    monkeypatch.setattr(selector_gate, "run_ci_select", _emission(["test-b"]))
    monkeypatch.setattr(pg, "read_steps_from_job_dir", lambda _: _steps())
    monkeypatch.delenv("BUILDKITE_JOB_ID", raising=False)
    monkeypatch.setenv("PATH", "")  # no buildkite-agent: uploads are skipped
    pr_config["job_dirs"] = ["jobs"]
    generator = object.__new__(pg.PipelineGenerator)
    generator.output_file_path = str(tmp_path / "pipeline.yaml")
    generator.generate()

    record = json.load(open(tmp_path / selector_gate.DECISION_FILE))
    assert record["applied"] is True
    assert record["scheduled_keys"] == ["image-build", "test-b"]
    pipeline = open(tmp_path / "pipeline.yaml").read()
    deferred = json.load(
        open(tmp_path / unblock_jobs.DEFERRED_DIR / unblock_jobs.DEFERRED_FILE)
    )
    assert [e["step"]["key"] for e in deferred["steps"]] == ["test-a"]
    assert "key: test-b" in pipeline and "key: test-a" not in pipeline


def test_todays_keys_include_the_amd_mirrors_that_would_run(pr_config):
    """legacy_keys is the baseline the selector is compared with, so it has to
    count the AMD mirrors today's rules run, as the selector's keys do."""
    steps = _steps() + [
        Step(
            label="M",
            key="test-m",
            commands=["m"],
            depends_on=["image-build"],
            source_file_dependencies=["vllm/"],
            mirror={"amd": {"device": "mi300_1", "depends_on": ["image-build-amd"]}},
        ),
    ]
    assert pg._legacy_keys(steps, ["vllm/foo.py"]) == [
        "image-build",
        "test-a",
        "test-m",
        "amd-test-m",
    ]


def _steps_with_mirrors():
    return _steps() + [
        Step(
            label="Mirrored",
            key="test-m",
            commands=["m"],
            depends_on=["image-build"],
            source_file_dependencies=["vllm/"],
            mirror={"amd": {"device": "mi300_1", "depends_on": ["image-build-amd"]}},
        ),
        Step(
            label="Quiet",
            key="test-q",
            commands=["q"],
            depends_on=["image-build"],
            source_file_dependencies=["tests/q/"],
            mirror={"amd": {"device": "mi300_1", "depends_on": ["image-build-amd"]}},
        ),
        Step(
            label="Native AMD",
            key="native-amd",
            device="mi355_1",
            commands=["n"],
            source_file_dependencies=["vllm/"],
        ),
        Step(label="Image AMD", key="image-build-amd", commands=["build"]),
    ]


def test_amd_steps_stay_on_todays_rules(pr_config, monkeypatch):
    """The selector decides NVIDIA steps only. An AMD step today's rules run
    still runs although the selector did not name it; one the selector named
    but today's rules do not run stays deferred."""
    monkeypatch.setattr(
        selector_gate, "run_ci_select", _emission(["test-b", "amd-test-q"])
    )
    steps = _steps_with_mirrors()
    record = pg._selector_decision(steps)
    assert record["applied"] is True
    assert record["selector_keys"] == ["test-b"]
    assert record["selector_amd_left_to_todays_rules"] == ["amd-test-q"]

    def runs(step, amd=None):
        if amd is not None:
            step = buildkite_step._get_amd_mirror_effective_step(step, amd)
        return buildkite_step._step_should_run(step, pr_config["list_file_diff"])

    by_key = {s.key: s for s in steps}
    assert runs(by_key["test-b"]) and not runs(by_key["test-a"]), (
        "NVIDIA: the selector's"
    )
    assert runs(by_key["test-m"], by_key["test-m"].mirror["amd"]), (
        "vllm/ matched its mirror"
    )
    assert not runs(by_key["test-q"], by_key["test-q"].mirror["amd"]), (
        "today's rules say no"
    )
    assert runs(by_key["native-amd"]), "native AMD: today's rules"


def test_a_main_build_is_selected_against_its_parent(monkeypatch):
    """Main builds use the selector too, diffing against the parent commit,
    as today's rules do there; the scheduled full runs (NIGHTLY/RUN_ALL)
    still run everything."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="p" * 40 + "\n", stderr="")

    monkeypatch.setattr(selector_gate.subprocess, "run", fake_run)
    main = dict(PR, branch="main", pull_request="false")
    seen = {}

    def emission(**kw):
        seen.update(kw)
        return _emission(["y"])(**kw)

    record = selector_gate.decide(main, ["x", "y"], emission)
    assert record["skipped"] is None and record["selector_keys"] == ["y"]
    assert record["base"] == "p" * 40 and seen["base"] == "p" * 40
    assert calls == [["git", "rev-parse", f"{main['commit']}^"]]
    full_run = selector_gate.decide(
        dict(main, nightly="1", run_all=True), ["x"], emission
    )
    assert full_run["skipped"].startswith("nightly")


def test_steps_whose_definition_changed_run_even_when_optional(pr_config, monkeypatch):
    """vllm#61832 added automatic_shard to three nightly-only steps; the
    selector names them in changed_keys, and they run. Other optional steps
    stay optional."""
    steps = _steps() + [
        Step(
            label="Nightly changed",
            key="nightly-changed",
            optional=True,
            commands=["n"],
            depends_on=["image-build"],
        ),
        Step(
            label="Nightly other",
            key="nightly-other",
            optional=True,
            commands=["n"],
            depends_on=["image-build"],
        ),
    ]

    def emission(**kw):
        return {
            "emission": {
                "omit": False,
                "reason": "",
                "keys": ["test-b", "nightly-other"],
                "changed_keys": ["nightly-changed", "gone-at-head"],
            },
            "records": {},
            "log": "",
            "ci_infra_commit": "c" * 40,
        }

    monkeypatch.setattr(selector_gate, "run_ci_select", emission)
    record = pg._selector_decision(steps)
    assert record["applied"] is True
    assert record["selector_changed_steps"] == ["nightly-changed"]
    assert record["selector_keys"] == ["nightly-changed", "test-b"]
    assert record["selector_optional_dropped"] == ["nightly-other"]
