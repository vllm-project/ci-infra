from pathlib import Path
import sys


PIPELINE_GENERATOR_DIR = Path(__file__).resolve().parents[2] / "pipeline_generator"

if str(PIPELINE_GENERATOR_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_GENERATOR_DIR))

import step as step_module


TEST_JOB_DIR = Path(__file__).resolve().parent / "test_files" / "test_jobs"


def _stub_global_config():
    return {"github_repo_name": "example/project"}


def test_read_steps_from_job_dir(monkeypatch):
    monkeypatch.setattr(step_module, "get_global_config", _stub_global_config)
    steps = step_module.read_steps_from_job_dir(str(TEST_JOB_DIR))
    assert len(steps) == 8

    by_label = {step.label: step for step in steps}
    assert by_label["Test A"].group == "bb"
    assert by_label["Test A"].commands == ['echo "Test A"', 'echo "Test A.B"']
    assert by_label["Test D"].num_nodes == 2
    assert by_label["Test D"].num_devices == 4
    assert by_label["Test E"].group == "a"
    assert by_label["Test G"].source_file_dependencies == ["file3", "src/file4"]


def test_group_steps_sorts_each_group(monkeypatch):
    monkeypatch.setattr(step_module, "get_global_config", _stub_global_config)
    grouped_steps = step_module.group_steps(
        step_module.read_steps_from_job_dir(str(TEST_JOB_DIR))
    )
    assert [step.label for step in grouped_steps["a"]] == [
        "Test E",
        "Test F",
        "Test G",
        "Test H",
    ]
    assert [step.label for step in grouped_steps["bb"]] == [
        "Test A",
        "Test B",
        "Test C",
        "Test D",
    ]
