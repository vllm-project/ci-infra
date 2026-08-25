# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project

import json
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

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


_PILOT_DIGEST = (
    "sha256:530a18dfb04c66cdb4ebb939b111d84c47b902abf21b3e7d3fded2deac8b556a"
)
_BASELINE_MEMBER = "ci_test_selection/worktree-baseline-530a18dfb04c.json"


def test_bundle_includes_exactly_the_pinned_baseline():
    names = ZipFile(BytesIO(bundle_bytes(_PILOT_DIGEST))).namelist()
    assert _BASELINE_MEMBER in names
    assert len([n for n in names if "worktree-baseline" in n]) == 1


def test_bundle_without_digest_excludes_baselines():
    names = ZipFile(BytesIO(bundle_bytes())).namelist()
    assert not [n for n in names if "worktree-baseline" in n]


def test_bundle_missing_pinned_baseline_raises():
    with pytest.raises(RuntimeError, match="lacks the pinned baseline"):
        bundle_bytes("sha256:" + "f" * 64)


def test_bundle_rejects_malformed_digest():
    with pytest.raises(ValueError):
        bundle_bytes("not-a-digest")


def test_bundle_baseline_selection_is_noninterfering(tmp_path):
    # An unrelated extra baseline must not change the emitted bytes.
    import shutil

    source = tmp_path / "collector"
    shutil.copytree(
        Path(bundle_bytes.__code__.co_filename).with_name("collector"), source
    )
    baseline = source / "worktree-baseline-530a18dfb04c.json"
    baseline.write_bytes(
        Path(_baseline_source()).read_bytes()
    )
    target_only = bundle_bytes(_PILOT_DIGEST, source=source)
    # Unrelated valid baseline alongside the target.
    other = json.loads(baseline.read_text())
    other["image_digest"] = "sha256:" + "f" * 64
    (source / "worktree-baseline-ffffffffffff.json").write_text(json.dumps(other))
    with_extra = bundle_bytes(_PILOT_DIGEST, source=source)
    assert with_extra == target_only


def _baseline_source() -> str:
    return str(
        Path(bundle_bytes.__code__.co_filename).with_name("collector")
        / "worktree-baseline-530a18dfb04c.json"
    )


def test_verify_step_blocks_until_uploaded_bundle_proves_exact():
    from pipeline_generator import create_bundle_verify_group_step

    collector = bundle_bytes(_PILOT_DIGEST)
    group = create_bundle_verify_group_step(
        collector,
        bundle_sha256(collector),
        _PILOT_DIGEST,
        "eac636a7fa476983cdae34b45a984e9852aad375",
    )
    step = group.steps[0]
    assert step.key == "verify-collector-bundle"
    assert step.depends_on == ["test-selection-collector"]
    assert not step.soft_fail
    command = step.commands[0]
    assert 'artifact download "test-selection-collector.zip"' in command
    assert bundle_sha256(collector) in command
    # The expected member set is embedded at render time.
    import base64

    tokens = command.split()
    members = json.loads(base64.b64decode(tokens[-2]))
    expect = json.loads(base64.b64decode(tokens[-1]))
    assert _BASELINE_MEMBER in members
    assert expect["entry_count"] == 2868
    assert expect["raw_sha256"] == (
        "4baa54f37a7498939362267b1d88b89212b0b9d9f2830dd9d01516cb9fcd87b1"
    )
    assert expect["image_digest"] == _PILOT_DIGEST
    assert expect["repository_sha"] == "eac636a7fa476983cdae34b45a984e9852aad375"


def test_verify_step_refuses_a_bundle_missing_the_baseline():
    import pytest as _pytest

    from pipeline_generator import create_bundle_verify_group_step

    collector = bundle_bytes()  # no digest -> no baseline shipped
    with _pytest.raises(ValueError, match="lacks the pinned baseline"):
        create_bundle_verify_group_step(
            collector, bundle_sha256(collector), _PILOT_DIGEST, "e" * 40
        )
