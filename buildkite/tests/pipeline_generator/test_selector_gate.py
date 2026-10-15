"""The selector gate: when the test selector may choose a PR build's steps,
what it records, and that it never makes a build fail."""

import json
import os

import pytest

import selector_gate

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
    }


@pytest.fixture(autouse=True)
def _gate_on(monkeypatch):
    monkeypatch.setenv(selector_gate.SELECTOR_ENV_VAR, "on")


def test_off_unless_the_switch_is_set(monkeypatch):
    monkeypatch.delenv(selector_gate.SELECTOR_ENV_VAR)
    assert selector_gate.mode() == ""
    monkeypatch.setenv(selector_gate.SELECTOR_ENV_VAR, "bogus")
    assert selector_gate.mode() == ""
    monkeypatch.setenv(selector_gate.SELECTOR_ENV_VAR, "Shadow")
    assert selector_gate.mode() == "shadow"


def test_a_pr_build_records_the_selectors_keys():
    record = selector_gate.decide(PR, ["x", "y", "z"], _emission(["y", "a"]))
    assert record["selector_keys"] == ["a", "y"]
    assert record["legacy_keys"] == ["x", "y", "z"]
    assert record["fallback"] is None and record["skipped"] is None
    assert record["base"] == PR["merge_base_commit"] and record["head"] == PR["commit"]
    assert record["records"] == {"fnrec": {"build": 1}}
    assert record["ci_infra_commit"] == "c" * 40


@pytest.mark.parametrize(
    "change, why",
    [
        ({"pull_request": "false"}, "not a pull request"),
        ({"branch": "main"}, "main branch"),
        ({"nightly": "1"}, "nightly"),
        ({"run_all": True}, "run-all"),
        ({"only_step_keys": frozenset({"x"})}, "VLLM_CI_ONLY_STEP_KEYS"),
    ],
)
def test_builds_that_keep_todays_rules(change, why):
    def boom(**kw):
        raise AssertionError("the selector must not run")

    record = selector_gate.decide({**PR, **change}, ["x"], boom)
    assert why in record["skipped"] and record["selector_keys"] is None


def test_a_selector_failure_keeps_todays_rules():
    def boom(**kw):
        raise RuntimeError("clone failed")

    record = selector_gate.decide(PR, ["x"], boom)
    assert record["selector_keys"] is None
    assert "clone failed" in record["fallback"]


def test_the_selectors_own_refusal_keeps_todays_rules():
    record = selector_gate.decide(PR, ["x"], _emission([], omit=True, reason="run-all: rust"))
    assert record["selector_keys"] is None
    assert "run-all: rust" in record["fallback"]


def test_the_record_is_written_as_json(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")  # no buildkite-agent: the upload is skipped
    record = selector_gate.decide(PR, ["x"], _emission(["x"]))
    path = selector_gate.write_and_upload(record, str(tmp_path))
    assert os.path.basename(path) == selector_gate.DECISION_FILE
    assert json.load(open(path))["selector_keys"] == ["x"]


# ---- through the generator -------------------------------------------------

import pipeline_generator as pg  # noqa: E402
from step import Step  # noqa: E402


def _steps():
    return [
        Step(label="Image", key="image-build", commands=["build"]),
        Step(label="A", key="test-a", depends_on=["image-build"], commands=["a"],
             source_file_dependencies=["vllm/"]),
        Step(label="B", key="test-b", depends_on=["image-build"], commands=["b"],
             source_file_dependencies=["tests/b/"]),
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
    assert pr_config["only_step_keys"] == frozenset({"test-b"})
    # today's rules: the image build always runs, and vllm/ matched test-a
    assert record["legacy_keys"] == ["image-build", "test-a"]
    selected, keys = pg.select_steps_and_dependencies(_steps(), pr_config["only_step_keys"])
    assert keys == frozenset({"image-build", "test-b"}), "prerequisites come along"


def test_shadow_mode_records_but_changes_nothing(pr_config, monkeypatch):
    monkeypatch.setenv(selector_gate.SELECTOR_ENV_VAR, "shadow")
    monkeypatch.setattr(selector_gate, "run_ci_select", _emission(["test-b"]))
    record = pg._selector_decision(_steps())
    assert record["selector_keys"] == ["test-b"] and record["applied"] is False
    assert pr_config["only_step_keys"] is None


def test_an_unknown_key_keeps_todays_rules(pr_config, monkeypatch):
    monkeypatch.setattr(selector_gate, "run_ci_select", _emission(["test-b", "gone"]))
    record = pg._selector_decision(_steps())
    assert record["applied"] is False and "gone" in record["fallback"]
    assert pr_config["only_step_keys"] is None


def test_the_gate_is_inert_when_off(pr_config, monkeypatch):
    monkeypatch.delenv(selector_gate.SELECTOR_ENV_VAR)
    assert pg._selector_decision(_steps()) is None
    assert pr_config["only_step_keys"] is None
