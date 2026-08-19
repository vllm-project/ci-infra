# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

from io import BytesIO
from zipfile import ZipFile

from pipeline_generator import create_collector_group_step
from test_selection.collector_bundle import bundle_bytes, bundle_sha256


def test_collector_bundle_is_deterministic_and_minimal():
    first = bundle_bytes()
    second = bundle_bytes()

    assert first == second
    assert len(bundle_sha256(first)) == 64
    with ZipFile(BytesIO(first)) as archive:
        names = set(archive.namelist())
        assert "ci_test_selection/run_job_trace.py" in names
        assert "ci_test_selection/run_trace.py" in names
        assert "ci_test_selection/parse_nsys_sqlite.py" in names
        assert "ci_test_selection/run_traced.sh" in names
        assert not any("deep" in name for name in names)


def test_collector_step_publishes_the_exact_checksum_once():
    collector = bundle_bytes()
    checksum = bundle_sha256(collector)

    group = create_collector_group_step(collector, checksum)
    step = group.steps[0]

    assert step.key == "test-selection-collector"
    assert step.soft_fail is True
    assert checksum in step.commands[0]
    assert step.commands[0].count("artifact upload") == 1
