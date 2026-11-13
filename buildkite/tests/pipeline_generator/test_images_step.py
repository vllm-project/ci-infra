"""The images step (`pipeline-generator --images_only`) beside bootstrap.

Buildkite holds the steps a job uploads until that job exits, so the images
bootstrap uploads early still wait for its test selector. A separate step
uploads them and exits; bootstrap, told its key in VLLM_CI_IMAGES_STEP, leaves
them out of its own pipeline once the step records what it uploaded.
"""

import json
import subprocess

import pytest
import yaml
from click.testing import CliRunner

import main as main_module
import pipeline_generator as pg
from step import Step

IMAGES_STEP = "bootstrap-images"
EARLY = {"image-build", "image-build-smoke-test"}


def _steps():
    return [
        Step(label="Image", key="image-build", commands=["build"]),
        Step(
            label="Smoke",
            key="image-build-smoke-test",
            depends_on=["image-build"],
            commands=["smoke"],
        ),
        # A secondary image: it goes up with the pipeline, when selected.
        Step(label="CPU image", key="image-build-cpu", commands=["build-cpu"]),
        Step(
            label="A",
            key="test-a",
            depends_on=["image-build"],
            source_file_dependencies=["vllm/"],
            commands=["a"],
        ),
        Step(
            label="CPU test",
            key="test-cpu",
            depends_on=["image-build-cpu"],
            source_file_dependencies=["vllm/"],
            commands=["cpu"],
        ),
    ]


def _keys(pipeline):
    return {
        step.get("key")
        for group in pipeline["steps"]
        for step in group.get("steps", [group])
        if step.get("key")
    }


class FakeAgent:
    """buildkite-agent: pipeline uploads, build meta-data and step outcomes."""

    def __init__(self):
        self.events = []
        self.meta = {}
        self.outcome = ""
        self.upload_returncode = 0
        # Meta-data the images step "sets" while bootstrap's upload runs.
        self.meta_on_failed_upload = None

    def run(self, cmd, *args, **kwargs):
        if cmd[0] != "buildkite-agent":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[1:3] == ["pipeline", "upload"]:
            self.events.append(("upload", _keys(yaml.safe_load(open(cmd[3])))))
            if self.upload_returncode and self.meta_on_failed_upload is not None:
                self.meta[pg.EARLY_IMAGES_META_KEY] = self.meta_on_failed_upload
            return subprocess.CompletedProcess(cmd, self.upload_returncode, "", "")
        if cmd[1:3] == ["meta-data", "set"]:
            self.events.append(("meta-set", cmd[4]))
            self.meta[cmd[3]] = cmd[4]
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[1:3] == ["meta-data", "exists"]:
            self.events.append(("meta-exists", cmd[3]))
            return subprocess.CompletedProcess(cmd, 0 if cmd[3] in self.meta else 100)
        if cmd[1:3] == ["meta-data", "get"]:
            return subprocess.CompletedProcess(cmd, 0, self.meta[cmd[3]] + "\n", "")
        if cmd[1:4] == ["step", "get", "outcome"]:
            self.events.append(("outcome", cmd[5]))
            return subprocess.CompletedProcess(cmd, 0, self.outcome + "\n", "")
        self.events.append(("agent", cmd[1]))
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def agent(fake_global_config, monkeypatch):
    fake_global_config.update(
        pull_request="123", job_dirs=["jobs"], list_file_diff=["vllm/engine.py"]
    )
    monkeypatch.setattr(pg, "get_global_config", lambda: fake_global_config)
    monkeypatch.setattr(pg, "read_steps_from_job_dir", lambda _: _steps())
    monkeypatch.setenv("BUILDKITE_JOB_ID", "job-1")
    monkeypatch.setattr(pg.time, "sleep", lambda _: None)
    fake = FakeAgent()
    monkeypatch.setattr(pg.subprocess, "run", fake.run)
    real_selector = pg._selector_decision

    def selector(steps):
        fake.events.append(("selector", None))
        return real_selector(steps)

    monkeypatch.setattr(pg, "_selector_decision", selector)
    fake.config = fake_global_config
    return fake


def _generator(tmp_path):
    generator = object.__new__(pg.PipelineGenerator)
    generator.output_file_path = str(tmp_path / "pipeline.yaml")
    return generator


def _bootstrap(tmp_path):
    _generator(tmp_path).generate()
    return _keys(yaml.safe_load(open(tmp_path / "pipeline.yaml")))


def _uploads(agent):
    return [keys for kind, keys in agent.events if kind == "upload"]


# The images step


def test_images_step_uploads_the_early_images_then_records_them(agent, tmp_path):
    _generator(tmp_path).generate_images_only()
    assert _uploads(agent) == [EARLY]
    kinds = [kind for kind, _ in agent.events]
    assert kinds.index("upload") < kinds.index("meta-set")
    assert agent.meta[pg.EARLY_IMAGES_META_KEY] == "image-build,image-build-smoke-test"
    assert "selector" not in kinds


def test_images_step_uploads_nothing_for_a_doc_only_change(agent, tmp_path):
    agent.config.update(docs_only_disable="0", list_file_diff=["docs/usage.md"])
    _generator(tmp_path).generate_images_only()
    assert _uploads(agent) == []
    assert agent.meta[pg.EARLY_IMAGES_META_KEY] == ""


def test_images_step_records_nothing_uploaded_when_its_upload_fails(agent, tmp_path):
    agent.upload_returncode = 1
    _generator(tmp_path).generate_images_only()
    assert agent.meta[pg.EARLY_IMAGES_META_KEY] == ""


def test_images_step_records_nothing_when_it_breaks(agent, tmp_path, monkeypatch):
    def broken(_):
        raise RuntimeError("no checkout")

    monkeypatch.setattr(pg, "read_steps_from_job_dir", broken)
    _generator(tmp_path).generate_images_only()
    assert agent.meta[pg.EARLY_IMAGES_META_KEY] == ""


def test_cli_images_only_runs_the_images_step(monkeypatch, tmp_path):
    calls = []

    class Fake:
        def __init__(self, config, output):
            calls.append(("init", output))

        def generate_images_only(self):
            calls.append("images")

        def generate(self):
            calls.append("generate")

    monkeypatch.setattr(main_module, "PipelineGenerator", Fake)
    config = tmp_path / "ci_config.yaml"
    config.write_text("name: test\n")
    result = CliRunner().invoke(
        main_module.main, ["--pipeline_config_path", str(config), "--images_only"]
    )
    assert result.exit_code == 0, result.output
    assert calls == [("init", ".buildkite/pipeline.yaml"), "images"]


# Only the CUDA and AMD images go up early


def test_secondary_images_go_up_with_the_pipeline(agent, tmp_path):
    final = _bootstrap(tmp_path)
    assert _uploads(agent) == [EARLY]
    assert not final & EARLY
    assert {"image-build-cpu", "test-cpu", "test-a"} <= final


# Bootstrap beside an images step


def test_bootstrap_leaves_out_what_the_images_step_uploaded(
    agent, tmp_path, monkeypatch
):
    monkeypatch.setenv(pg.IMAGES_STEP_ENV_VAR, IMAGES_STEP)
    agent.meta[pg.EARLY_IMAGES_META_KEY] = "image-build,image-build-smoke-test"
    final = _bootstrap(tmp_path)
    assert _uploads(agent) == []
    assert not final & EARLY
    assert {"test-a", "image-build-cpu"} <= final
    # It asks only after the selector, so the selector never waits for it.
    kinds = [kind for kind, _ in agent.events]
    assert kinds.index("selector") < kinds.index("meta-exists")


def test_bootstrap_keeps_the_images_when_the_step_uploaded_none(
    agent, tmp_path, monkeypatch
):
    monkeypatch.setenv(pg.IMAGES_STEP_ENV_VAR, IMAGES_STEP)
    agent.meta[pg.EARLY_IMAGES_META_KEY] = ""
    final = _bootstrap(tmp_path)
    assert _uploads(agent) == []
    assert EARLY <= final


def test_bootstrap_uploads_the_images_when_the_step_finished_without_a_record(
    agent, tmp_path, monkeypatch
):
    monkeypatch.setenv(pg.IMAGES_STEP_ENV_VAR, IMAGES_STEP)
    agent.outcome = "hard_failed"
    final = _bootstrap(tmp_path)
    assert _uploads(agent) == [EARLY]
    assert not final & EARLY
    # It stopped waiting as soon as the step had an outcome.
    assert [k for k, _ in agent.events].count("outcome") == 1


def test_bootstrap_stops_waiting_after_the_timeout(agent, tmp_path, monkeypatch):
    monkeypatch.setenv(pg.IMAGES_STEP_ENV_VAR, IMAGES_STEP)
    monkeypatch.setattr(pg, "IMAGES_STEP_TIMEOUT_SECONDS", 20)
    final = _bootstrap(tmp_path)
    polls = [k for k, _ in agent.events].count("outcome")
    assert polls == 20 // pg.IMAGES_STEP_POLL_SECONDS + 1
    assert _uploads(agent) == [EARLY]
    assert not final & EARLY


def test_bootstrap_defers_to_the_images_step_that_uploaded_first(
    agent, tmp_path, monkeypatch
):
    # Bootstrap timed out and uploaded too, but the images step won: Buildkite
    # rejects bootstrap's duplicate keys, and the step's record names them.
    monkeypatch.setenv(pg.IMAGES_STEP_ENV_VAR, IMAGES_STEP)
    monkeypatch.setattr(pg, "IMAGES_STEP_TIMEOUT_SECONDS", 0)
    agent.upload_returncode = 1
    agent.meta_on_failed_upload = "image-build,image-build-smoke-test"
    final = _bootstrap(tmp_path)
    assert not final & EARLY
    assert "test-a" in final


def test_bootstrap_ignores_the_images_step_for_a_doc_only_change(
    agent, tmp_path, monkeypatch
):
    monkeypatch.setenv(pg.IMAGES_STEP_ENV_VAR, IMAGES_STEP)
    agent.config.update(docs_only_disable="0", list_file_diff=["README.md"])
    _generator(tmp_path).generate()
    assert (tmp_path / ".docs_only").exists()
    assert not [k for k, _ in agent.events if k in ("meta-exists", "outcome")]


def test_outside_a_job_the_images_step_is_ignored(agent, tmp_path, monkeypatch):
    monkeypatch.setenv(pg.IMAGES_STEP_ENV_VAR, IMAGES_STEP)
    monkeypatch.delenv("BUILDKITE_JOB_ID")
    final = _bootstrap(tmp_path)
    assert _uploads(agent) == []
    assert EARLY <= final


def test_an_unselected_secondary_image_is_not_built(agent, tmp_path):
    # The selector picked a CUDA test: the CPU image is left to "Run more
    # jobs" with its test, instead of going up early with the CUDA image.
    agent.config["selector_step_keys"] = frozenset(
        {"test-a", "image-build", "image-build-smoke-test"}
    )
    final = _bootstrap(tmp_path)
    assert _uploads(agent) == [EARLY]
    assert "test-a" in final
    assert not {"image-build-cpu", "test-cpu"} & final
    deferred = json.load(open(tmp_path / "deferred" / "deferred_steps.json"))
    keys = {entry["step"]["key"] for entry in deferred["steps"]}
    assert {"image-build-cpu", "test-cpu"} <= keys
