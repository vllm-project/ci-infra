import subprocess

import pytest
import yaml

import pipeline_generator as pg
from step import Step


def _steps():
    return [
        Step(label="Image", key="image-build", commands=["build"]),
        Step(
            label="Smoke",
            key="image-build-smoke-test",
            depends_on=["image-build"],
            commands=["smoke"],
        ),
        # An image step whose prerequisite is not an image step stays in the
        # final upload: its prerequisite isn't in the build yet.
        Step(
            label="Image after A",
            key="image-build-late",
            depends_on=["test-a"],
            commands=["late"],
        ),
        Step(
            label="A",
            key="test-a",
            depends_on=["image-build"],
            commands=["a"],
        ),
    ]


@pytest.fixture
def build(fake_global_config, monkeypatch, tmp_path):
    """Generate a PR build's pipeline with buildkite-agent faked, recording
    each agent call and when the test selector ran."""
    fake_global_config.update(pull_request="123", job_dirs=["jobs"])
    monkeypatch.setattr(pg, "get_global_config", lambda: fake_global_config)
    monkeypatch.setattr(pg, "read_steps_from_job_dir", lambda _: _steps())
    monkeypatch.setenv("BUILDKITE_JOB_ID", "job-1")
    events = []
    result = {"returncode": 0}

    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["buildkite-agent", "pipeline", "upload"]:
            events.append(("upload", yaml.safe_load(open(cmd[3]))))
            return subprocess.CompletedProcess(cmd, result["returncode"])
        events.append(("agent", cmd[1]))
        return subprocess.CompletedProcess(cmd, 0)

    real_selector = pg._selector_decision

    def selector(steps):
        events.append(("selector", None))
        return real_selector(steps)

    monkeypatch.setattr(pg.subprocess, "run", fake_run)
    monkeypatch.setattr(pg, "_selector_decision", selector)

    def generate():
        generator = object.__new__(pg.PipelineGenerator)
        generator.output_file_path = str(tmp_path / "pipeline.yaml")
        generator.generate()
        final = yaml.safe_load(open(tmp_path / "pipeline.yaml"))
        return events, _keys(final)

    generate.result = result
    generate.config = fake_global_config
    return generate


def _keys(pipeline):
    return {
        step.get("key")
        for group in pipeline["steps"]
        for step in group.get("steps", [group])
        if step.get("key")
    }


def test_image_steps_go_up_before_the_selector_runs(build):
    events, final = build()
    kinds = [kind for kind, _ in events]
    assert kinds.index("upload") < kinds.index("selector")
    early = _keys(events[kinds.index("upload")][1])
    assert early == {"image-build", "image-build-smoke-test"}
    # The final pipeline leaves them out but still depends on them.
    assert not final & early
    assert {"test-a", "image-build-late"} <= final


def test_switched_off_everything_goes_up_together(build, monkeypatch):
    monkeypatch.setenv(pg.EARLY_IMAGE_UPLOAD_ENV_VAR, "0")
    events, final = build()
    assert "upload" not in [kind for kind, _ in events]
    assert {"image-build", "image-build-smoke-test", "test-a"} <= final


def test_a_failed_early_upload_keeps_the_image_steps_in_the_pipeline(build):
    build.result["returncode"] = 1
    _, final = build()
    assert {"image-build", "image-build-smoke-test", "test-a"} <= final


def test_builds_limited_to_some_steps_upload_once(build):
    build.config["only_step_keys"] = frozenset({"test-a"})
    events, final = build()
    assert "upload" not in [kind for kind, _ in events]
    assert "test-a" in final


def test_outside_a_buildkite_job_nothing_is_uploaded_early(build, monkeypatch):
    monkeypatch.delenv("BUILDKITE_JOB_ID")
    monkeypatch.setenv("PATH", "")  # no buildkite-agent for the other uploads
    events, final = build()
    assert "upload" not in [kind for kind, _ in events]
    assert "image-build" in final
