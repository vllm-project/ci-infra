import sys
import pytest
from buildkite.pipeline_generator.steps import read_steps_from_job_dir, sort_steps

TEST_JOB_DIR = "buildkite/tests/pipeline_generator/test_files/test_jobs"


def test_read_steps_from_job_dir():
    steps = read_steps_from_job_dir(TEST_JOB_DIR)
    assert len(steps) == 8
    assert steps[0].label == "Test A"
    assert steps[0].group == "bb"
    assert steps[0].commands == ['echo "Test A"', 'echo "Test A.B"']
    assert steps[3].num_nodes == 2
    assert steps[3].num_devices == 4
    assert steps[4].label == "Test E"
    assert steps[4].group == "a"


def test_sort_steps():
    steps = read_steps_from_job_dir(TEST_JOB_DIR)
    sorted_steps = sort_steps(steps)
    assert [step.label for step in sorted_steps] == [
        "Test E",
        "Test F",
        "Test G",
        "Test H",
        "Test A",
        "Test B",
        "Test C",
        "Test D",
    ]


if __name__ == "__main__":
    sys.exit(pytest.main(["-v", __file__]))
