import json
import subprocess

import pytest

import buildkite_step
import unblock_jobs
from buildkite_step import BuildkiteCommandStep, BuildkiteGroupStep
from pipeline_generator import write_deferred_steps


def _command(key, depends_on=None, deferred=False):
    return BuildkiteCommandStep(
        label=key.title(),
        key=key,
        commands=[f"echo {key}"],
        depends_on=depends_on,
        deferred=deferred,
    )


def _entry(key, group="g", depends_on=None):
    step = {"label": key.title(), "key": key, "commands": [f"echo {key}"]}
    if depends_on:
        step["depends_on"] = depends_on
    return {"group": group, "step": step}


def test_split_keeps_deferred_steps_out_of_the_pipeline():
    groups = [
        BuildkiteGroupStep(
            group="g1",
            steps=[_command("runs"), _command("later", deferred=True)],
        ),
        BuildkiteGroupStep(group="g2", steps=[_command("also-later", deferred=True)]),
    ]

    runnable, deferred = buildkite_step.split_deferred_steps(groups)

    assert [(g.group, [s.key for s in g.steps]) for g in runnable] == [("g1", ["runs"])]
    assert [(g.group, [s.key for s in g.steps]) for g in deferred] == [
        ("g1", ["later"]),
        ("g2", ["also-later"]),
    ]


def test_split_runs_a_deferred_step_a_running_step_needs():
    """A running step waiting on a step that is not in the build never starts."""
    groups = [
        BuildkiteGroupStep(
            group="g",
            steps=[
                _command("image", deferred=True),
                _command("helper", depends_on=["image"], deferred=True),
                _command("test", depends_on=["helper"]),
                _command("other", depends_on=["image"], deferred=True),
            ],
        )
    ]

    runnable, deferred = buildkite_step.split_deferred_steps(groups)

    assert [s.key for s in runnable[0].steps] == ["image", "helper", "test"]
    assert [s.key for s in deferred[0].steps] == ["other"]


def test_deferred_flag_is_not_serialized():
    step = _command("later", deferred=True)
    assert "deferred" not in step.dict(exclude_none=True)
    group = BuildkiteGroupStep(group="g", steps=[step])
    assert "deferred" not in group.dict(exclude_none=True)["steps"][0]


def test_pick_brings_deferred_dependencies_only():
    entries = [
        _entry("image"),
        _entry("a", depends_on=["image", "image-build"]),
        _entry("b"),
    ]

    selected, remaining = unblock_jobs.pick(entries, {"a"})

    # image-build is not deferred: it is already in the build.
    assert [e["step"]["key"] for e in selected] == ["image", "a"]
    assert [e["step"]["key"] for e in remaining] == ["b"]


def test_pick_rejects_unknown_keys():
    with pytest.raises(SystemExit, match="nope"):
        unblock_jobs.pick([_entry("a")], {"nope"})


def test_more_jobs_group_lists_every_entry():
    group = unblock_jobs.more_jobs_group(
        [_entry("a", group="Kernels"), _entry("b", group="Models")],
        1,
        ".buildkite/deferred",
        "job-123",
        "small_cpu_queue_premerge",
    )

    input_step, upload_step = group["steps"]
    assert input_step["key"] == "run-more-jobs"
    assert input_step["depends_on"] == []
    text, a, b = input_step["fields"]
    assert text["key"] == "more-jobs"
    assert text["required"] is False
    # One checkbox per job, headed by its group's name.
    assert [(f["select"], f["key"]) for f in (a, b)] == [
        ("Kernels", "more-jobs-job-a"),
        ("Models", "more-jobs-job-b"),
    ]
    assert all(f["multiple"] and not f["required"] for f in (a, b))
    assert a["options"] == [{"label": "A", "value": "a"}]
    assert b["options"] == [{"label": "B", "value": "b"}]
    assert upload_step["depends_on"] == ["run-more-jobs"]
    assert upload_step["agents"] == {"queue": "small_cpu_queue_premerge"}
    (command,) = upload_step["commands"]
    assert "--step job-123" in command
    assert "unblock_jobs.py 1 .buildkite/deferred" in command


def test_later_rounds_get_their_own_keys():
    group = unblock_jobs.more_jobs_group([_entry("a")], 3, "d", "job", "q")
    input_step, upload_step = group["steps"]
    assert input_step["key"] == "run-more-jobs-3"
    assert [f["key"] for f in input_step["fields"]] == [
        "more-jobs-3",
        "more-jobs-3-job-a",
    ]
    assert upload_step["key"] == "upload-more-jobs-3"
    assert upload_step["depends_on"] == ["run-more-jobs-3"]


class FakeAgent:
    """Stands in for `buildkite-agent` meta-data and pipeline upload."""

    def __init__(self, meta_data):
        self.meta_data = dict(meta_data)
        self.uploads = []

    def run(self, command, check=False, **kwargs):
        assert command[0] == "buildkite-agent"
        stdout = ""
        returncode = 0
        if command[1:3] == ["meta-data", "keys"]:
            stdout = "\n".join(self.meta_data)
        elif command[1:3] == ["meta-data", "exists"]:
            returncode = 0 if command[3] in self.meta_data else 100
        elif command[1:3] == ["meta-data", "get"]:
            stdout = self.meta_data[command[3]]
        elif command[1:3] == ["meta-data", "set"]:
            self.meta_data[command[3]] = command[4]
        elif command[1:3] == ["pipeline", "upload"]:
            with open(command[3]) as f:
                self.uploads.append(json.load(f))
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(command, returncode, stdout=stdout)


def test_only_the_first_job_of_a_group_carries_its_heading():
    fields = unblock_jobs._job_fields(
        [
            _entry("a", group="CPU"),
            _entry("b", group="CPU"),
            _entry("c.d", group="GPU"),
        ],
        1,
    )
    assert [(key, label) for key, label, _ in fields] == [
        ("more-jobs-job-a", "CPU"),
        ("more-jobs-job-b", unblock_jobs.NO_LABEL),
        ("more-jobs-job-c_d", "GPU"),
    ]


def test_main_uploads_picks_then_offers_the_rest(tmp_path, monkeypatch):
    entries = [
        _entry("image", group="Build"),
        _entry("a", depends_on=["image"]),
        _entry("b", depends_on=["image"]),
        _entry("c"),
    ]
    (tmp_path / unblock_jobs.DEFERRED_FILE).write_text(
        json.dumps({"source_job": "job-1", "queue": "q", "steps": entries})
    )
    agent = FakeAgent({"more-jobs-job-a": "a"})
    monkeypatch.setattr(unblock_jobs.subprocess, "run", agent.run)

    unblock_jobs.main(["unblock_jobs.py", "1", str(tmp_path)])

    (upload,) = agent.uploads
    assert [(g["group"], [s["key"] for s in g["steps"]]) for g in upload["steps"]] == [
        ("Build", ["image"]),
        ("g", ["a"]),
        (unblock_jobs.GROUP, ["run-more-jobs-2", "upload-more-jobs-2"]),
    ]
    fields = upload["steps"][-1]["steps"][0]["fields"]
    assert [o["value"] for f in fields[1:] for o in f["options"]] == ["b", "c"]
    assert agent.meta_data[unblock_jobs.UPLOADED_META_KEY] == "a\nimage"

    # Round two: "image" is in the build already, so it is not uploaded again,
    # and with nothing left there is no third input step. The text field
    # and the checkboxes add up.
    agent.meta_data["more-jobs-2"] = "b"
    agent.meta_data["more-jobs-2-job-c"] = "c"
    unblock_jobs.main(["unblock_jobs.py", "2", str(tmp_path)])

    upload = agent.uploads[-1]
    assert [(g["group"], [s["key"] for s in g["steps"]]) for g in upload["steps"]] == [
        ("g", ["b", "c"]),
    ]


def test_submitting_nothing_offers_the_same_jobs_again(tmp_path, monkeypatch):
    (tmp_path / unblock_jobs.DEFERRED_FILE).write_text(
        json.dumps({"source_job": "job-1", "queue": "q", "steps": [_entry("a")]})
    )
    agent = FakeAgent({})
    monkeypatch.setattr(unblock_jobs.subprocess, "run", agent.run)

    unblock_jobs.main(["unblock_jobs.py", "1", str(tmp_path)])

    (upload,) = agent.uploads
    (group,) = upload["steps"]
    assert [s["key"] for s in group["steps"]] == [
        "run-more-jobs-2",
        "upload-more-jobs-2",
    ]
    assert unblock_jobs.UPLOADED_META_KEY not in agent.meta_data


def test_write_deferred_steps_saves_what_the_input_step_offers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BUILDKITE_JOB_ID", raising=False)
    groups = [
        BuildkiteGroupStep(group="g", steps=[_command("later", deferred=True)]),
    ]

    group = write_deferred_steps(groups, ".buildkite")

    artifact_dir = tmp_path / ".buildkite" / unblock_jobs.DEFERRED_DIR
    saved = json.loads((artifact_dir / unblock_jobs.DEFERRED_FILE).read_text())
    assert saved["steps"] == [
        {"group": "g", "step": groups[0].steps[0].dict(exclude_none=True)}
    ]
    assert (artifact_dir / unblock_jobs.SCRIPT_FILE).read_text() == open(
        unblock_jobs.__file__
    ).read()
    options = group["steps"][0]["fields"][1]["options"]
    assert options == [{"label": "Later", "value": "later"}]


def test_amd_steps_are_listed_under_their_nvidia_group(tmp_path, monkeypatch):
    """One AMD list would hold a few hundred checkboxes."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BUILDKITE_JOB_ID", raising=False)
    groups = [
        BuildkiteGroupStep(
            group=buildkite_step.AMD_GROUP,
            steps=[
                _command("amd-kern", deferred=True),
                _command("amd-direct", deferred=True),
            ],
        ),
        BuildkiteGroupStep(group="Kernels", steps=[_command("lora", deferred=True)]),
    ]

    group = write_deferred_steps(
        groups, ".buildkite", {"kern": "Kernels", "lora": "LoRA"}
    )

    fields = group["steps"][0]["fields"][1:]
    assert [(f["select"], [o["value"] for o in f["options"]]) for f in fields] == [
        ("Kernels", ["lora"]),
        ("AMD - Kernels", ["amd-kern"]),
        ("AMD - Other", ["amd-direct"]),
    ]
    saved = json.loads(
        (tmp_path / ".buildkite" / "deferred" / "deferred_steps.json").read_text()
    )
    # Uploaded into the AMD group all the same.
    assert {
        e["group"] for e in saved["steps"] if e["step"]["key"].startswith("amd-")
    } == {buildkite_step.AMD_GROUP}


def test_option_labels_drop_emoji_codes_and_job_placeholders():
    label = unblock_jobs._option_label
    assert label(":nvidia: (H200) Basic Correctness Models %N") == (
        "(H200) Basic Correctness Models"
    )
    assert label(":amd: (MI300) V1 Attention Shard %N") == "(MI300) V1 Attention"
    assert label(":docker: Build arm64 image") == "Build arm64 image"
