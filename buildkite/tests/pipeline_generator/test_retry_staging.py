"""TEST_RETRY_STAGING=1 appends the test-retry staging group (staging only,
never merge); without the flag the generated pipeline is unchanged."""

import copy
import subprocess
from pathlib import Path

import pytest
import yaml

import pipeline_generator as pg
import retry_staging
from step import Step

TEMPLATE_KEY = "entrypoints-unit-tests"
STAGING_GROUP = "test-retry staging"
STAGING_JOB_KEYS = {"trs-fail", "trs-timeout", "trs-kill"}


def _steps():
    return [
        # A faithful stand-in for the real entrypoints-unit-tests step: sharded,
        # with the retry policy and recorder env the staging jobs strip.
        Step(
            label="Entrypoints unit tests",
            group="Unit tests",
            key=TEMPLATE_KEY,
            commands=["pytest vllm/entrypoints"],
            parallelism=2,
            retry={"automatic": [{"limit": 1}]},
            env={
                "RUNTIME_SHARD_PLAN": "plan",
                "RUNTIME_SHARD_PLUGIN": "plugin",
                "PYTEST_ADDOPTS": "opts",
                "KEEP": "value",
            },
        ),
        Step(label="A", key="test-a", commands=["a"]),
    ]


@pytest.fixture
def generate(fake_global_config, monkeypatch, tmp_path):
    """Generate a PR build's pipeline with buildkite-agent faked, with or
    without TEST_RETRY_STAGING set."""
    fake_global_config.update(pull_request="123", run_all=True, job_dirs=["jobs"])
    monkeypatch.setattr(pg, "get_global_config", lambda: fake_global_config)
    monkeypatch.setattr(pg, "read_steps_from_job_dir", lambda _: _steps())
    monkeypatch.setenv("BUILDKITE_JOB_ID", "job-1")
    monkeypatch.setattr(
        pg.subprocess,
        "run",
        lambda cmd, *args, **kwargs: subprocess.CompletedProcess(cmd, 0),
    )

    def run(staging):
        if staging is False:
            monkeypatch.delenv("TEST_RETRY_STAGING", raising=False)
        else:
            monkeypatch.setenv(
                "TEST_RETRY_STAGING", "1" if staging is True else staging
            )
        generator = object.__new__(pg.PipelineGenerator)
        generator.output_file_path = str(tmp_path / "pipeline.yaml")
        generator.generate()
        return yaml.safe_load(open(tmp_path / "pipeline.yaml"))

    return run


def _all_steps(pipeline):
    for group in pipeline["steps"]:
        for step in group.get("steps", [group]):
            yield step


def test_flag_off_leaves_pipeline_unchanged(generate):
    pipeline = generate(staging=False)
    groups = [g.get("group") for g in pipeline["steps"] if "group" in g]
    assert STAGING_GROUP not in groups
    assert TEMPLATE_KEY in {s.get("key") for s in _all_steps(pipeline)}


def test_flag_zero_behaves_like_unset(generate):
    pipeline = generate(staging="0")
    groups = [g.get("group") for g in pipeline["steps"] if "group" in g]
    assert STAGING_GROUP not in groups
    assert TEMPLATE_KEY in {s.get("key") for s in _all_steps(pipeline)}


def test_flag_on_appends_staging_group(generate):
    pipeline = generate(staging=True)
    staging = next(g for g in pipeline["steps"] if g.get("group") == STAGING_GROUP)
    keys = {s.get("key") for s in staging["steps"]}
    assert STAGING_JOB_KEYS | {"trs-button-1", "trs-trigger-1"} <= keys

    trigger = next(s for s in staging["steps"] if s.get("key") == "trs-trigger-1")
    assert trigger["trigger"] == "ci-shadow"
    assert trigger["depends_on"] == ["trs-button-1"]

    for key in STAGING_JOB_KEYS:
        job = next(s for s in staging["steps"] if s.get("key") == key)
        plugin = next(
            p for p in job["plugins"] if any(k.startswith("docker#") for k in p)
        )
        config = next(iter(plugin.values()))
        assert config["mount-buildkite-agent"] is True

    # The template leaves its original group: the staging jobs are its copies.
    assert TEMPLATE_KEY not in {
        s.get("key")
        for g in pipeline["steps"]
        if g.get("group") != STAGING_GROUP
        for s in g.get("steps", [])
    }
    # The emptied template group is dropped, not left behind empty.
    assert "Unit tests" not in {g.get("group") for g in pipeline["steps"]}


def _public_test_data(value, cache_path):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            result[key] = _public_test_data(item, cache_path)
        return result
    if isinstance(value, list):
        return [_public_test_data(item, cache_path) for item in value]
    if isinstance(value, str):
        return value.replace(cache_path, "/mnt/<shared-cache>")
    return value


def _cache_path(pipeline):
    for step in _all_steps(pipeline):
        for plugin in step.get("plugins", []):
            for config in plugin.values():
                for entry in config.get("environment", []):
                    if entry.startswith("HF_HOME="):
                        return entry.split("=", 1)[1]
    raise AssertionError("template has no HF_HOME")


def test_staging_output_matches_the_original_commit(generate):
    """Characterization: the generated test-retry staging group must equal the
    original staging commit's output for the same input. The fixture holds
    only that group; it was generated offline by applying the staging module
    recovered from the fetched original commit (a52961c, whose branch was
    deleted) to this fixture's flag-off pipeline, so it is not a snapshot of
    the cleaned module and catches any behavior drift the cleanup introduced.
    Cache paths are normalized to the generic /mnt/<shared-cache> placeholder
    in the fixture and the live output before comparison."""
    pipeline = generate(staging=True)
    staging = next(g for g in pipeline["steps"] if g.get("group") == STAGING_GROUP)
    fixture = Path(__file__).parent / "test_files" / "test_retry_staging_expected.yaml"
    assert _public_test_data(staging, _cache_path(pipeline)) == yaml.safe_load(
        fixture.read_text()
    )


def test_add_fails_clearly_without_the_template():
    """A scoped build without entrypoints-unit-tests gets a clear error, not a
    bare StopIteration."""
    pipeline = {"steps": [{"group": "g", "steps": [{"key": "other"}]}]}
    with pytest.raises(ValueError) as exc_info:
        retry_staging.add(pipeline)
    assert (
        str(exc_info.value)
        == "TEST_RETRY_STAGING needs entrypoints-unit-tests in the build"
    )


def test_add_fails_clearly_without_a_docker_plugin():
    """A template step with no docker plugin gets a clear error, not a bare
    StopIteration."""
    template = {"key": TEMPLATE_KEY, "plugins": [{"kubernetes": {}}]}
    pipeline = {"steps": [{"group": "g", "steps": [template]}]}
    with pytest.raises(ValueError) as exc_info:
        retry_staging.add(pipeline)
    assert (
        str(exc_info.value)
        == "TEST_RETRY_STAGING needs a docker# plugin on entrypoints-unit-tests"
    )


def test_add_does_not_mutate_the_template():
    """_test_job works on a deep copy: the template step keeps its env, retry,
    parallelism and plugin config after add() runs."""
    template = {
        "key": TEMPLATE_KEY,
        "plugins": [{"docker#v5.2.0": {"image": "example"}}],
        "parallelism": 2,
        "retry": {"automatic": True},
        "env": {"RUNTIME_SHARD_PLAN": "plan", "KEEP": "value"},
    }
    snapshot = copy.deepcopy(template)
    pipeline = {"steps": [{"group": "g", "steps": [template]}]}
    retry_staging.add(pipeline)
    assert template == snapshot
