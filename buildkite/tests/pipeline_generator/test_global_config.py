import os
from unittest.mock import mock_open, patch

import pytest

from buildkite.pipeline_generator.global_config import (
    HF_OFFLINE_RETRY_KILL_SWITCH_ENV,
    ONLY_STEP_KEYS_ENV_VAR,
    _parse_only_step_keys,
    _read_strict_bool_env,
    _validate_pipeline_config,
    get_global_config,
    init_global_config,
)


@pytest.fixture(autouse=True)
def reset_config(monkeypatch):
    import buildkite.pipeline_generator.global_config

    monkeypatch.delenv(HF_OFFLINE_RETRY_KILL_SWITCH_ENV, raising=False)
    monkeypatch.delenv(ONLY_STEP_KEYS_ENV_VAR, raising=False)
    buildkite.pipeline_generator.global_config.config = None


@patch(
    "buildkite.pipeline_generator.global_config.get_merge_base_commit",
    return_value="sha",
)
@patch(
    "buildkite.pipeline_generator.global_config.get_list_file_diff",
    return_value=[],
)
@patch("buildkite.pipeline_generator.global_config.get_pr_labels", return_value=[])
@patch(
    "builtins.open",
    new_callable=mock_open,
    read_data=(
        "name: test\njob_dirs: [/tmp]\nregistries: reg\n"
        "repositories: {main: repo}\namd_hf_offline_retry: true"
    ),
)
@patch("os.path.exists", return_value=True)
def test_init_global_config_valid_branch(
    mock_exists, mock_open, mock_pr_labels, mock_diff, mock_mb
):
    with patch.dict(
        os.environ,
        {
            "BUILDKITE_BRANCH": "valid-branch-name_123/pkg",
            HF_OFFLINE_RETRY_KILL_SWITCH_ENV: "1",
        },
    ):
        init_global_config("dummy_path")
        assert get_global_config()["amd_hf_offline_retry"] is True
        assert get_global_config()["disable_hf_offline_retry"] is True


@patch(
    "buildkite.pipeline_generator.global_config.get_merge_base_commit",
    return_value="sha",
)
@patch(
    "buildkite.pipeline_generator.global_config.get_list_file_diff",
    return_value=[],
)
@patch("buildkite.pipeline_generator.global_config.get_pr_labels", return_value=[])
@patch(
    "builtins.open",
    new_callable=mock_open,
    read_data="name: test\njob_dirs: [/tmp]\nregistries: reg\nrepositories: {main: repo}",
)
@patch("os.path.exists", return_value=True)
def test_init_global_config_invalid_branch(
    mock_exists, mock_open, mock_pr_labels, mock_diff, mock_mb
):
    with patch.dict(os.environ, {"BUILDKITE_BRANCH": "invalid;branch"}):
        with pytest.raises(ValueError, match="Invalid branch name"):
            init_global_config("dummy_path")


@patch(
    "buildkite.pipeline_generator.global_config.get_merge_base_commit",
    return_value="sha",
)
@patch(
    "buildkite.pipeline_generator.global_config.get_list_file_diff",
    return_value=[],
)
@patch("buildkite.pipeline_generator.global_config.get_pr_labels", return_value=[])
@patch(
    "builtins.open",
    new_callable=mock_open,
    read_data="name: test\njob_dirs: [/tmp]\nregistries: reg\nrepositories: {main: repo}",
)
@patch("os.path.exists", return_value=True)
def test_init_global_config_fork_branch(
    mock_exists, mock_open, mock_pr_labels, mock_diff, mock_mb
):
    # Fork PRs arrive as "owner:branch"; the colon must be accepted.
    with patch.dict(os.environ, {"BUILDKITE_BRANCH": "octocat:update-pytorch-2.13.0"}):
        init_global_config("dummy_path")
        assert get_global_config()["amd_hf_offline_retry"] is False
        assert get_global_config()["disable_hf_offline_retry"] is False


def test_parse_only_step_keys():
    assert _parse_only_step_keys(
        '["basic-models-test-other-cpu", "image-build-cpu"]'
    ) == frozenset(
        {
            "basic-models-test-other-cpu",
            "image-build-cpu",
        }
    )


@pytest.mark.parametrize(
    "value, message",
    [
        ("not-json", "must be a JSON array"),
        ("[]", "must be a non-empty JSON array"),
        ('["bad key"]', "contains an invalid step key"),
        ('["same", "same"]', "contains duplicate step keys"),
    ],
)
def test_parse_only_step_keys_rejects_invalid_values(value, message):
    with pytest.raises(ValueError, match=message):
        _parse_only_step_keys(value)


@patch(
    "buildkite.pipeline_generator.global_config.get_merge_base_commit",
    return_value="sha",
)
@patch(
    "buildkite.pipeline_generator.global_config.get_list_file_diff",
    return_value=[],
)
@patch("buildkite.pipeline_generator.global_config.get_pr_labels", return_value=[])
@patch(
    "builtins.open",
    new_callable=mock_open,
    read_data="name: test\njob_dirs: [/tmp]\nregistries: reg\nrepositories: {main: repo}",
)
@patch("os.path.exists", return_value=True)
def test_init_global_config_reads_only_step_keys(
    mock_exists, mock_open, mock_pr_labels, mock_diff, mock_mb
):
    import buildkite.pipeline_generator.global_config as global_config

    with patch.dict(
        os.environ,
        {
            "BUILDKITE_BRANCH": "test-branch",
            ONLY_STEP_KEYS_ENV_VAR: '["selected-step"]',
        },
    ):
        init_global_config("dummy_path")

    assert global_config.config["only_step_keys"] == frozenset({"selected-step"})


def test_validate_pipeline_config_valid_repo():
    config = {
        "name": "test",
        "job_dirs": ["/tmp"],
        "registries": "registry",
        "repositories": {"main": "repo"},
        "github_repo_name": "vllm-project/vllm",
    }
    with patch("os.path.exists", return_value=True):
        # Should not raise ValueError
        _validate_pipeline_config(config)


def test_validate_pipeline_config_invalid_repo_org():
    config = {
        "name": "test",
        "job_dirs": ["/tmp"],
        "registries": "registry",
        "repositories": {"main": "repo"},
        "github_repo_name": "attacker/vllm",
    }
    with patch("os.path.exists", return_value=True):
        with pytest.raises(ValueError, match="Invalid github_repo_name"):
            _validate_pipeline_config(config)


def test_validate_pipeline_config_invalid_repo_traversal():
    config = {
        "name": "test",
        "job_dirs": ["/tmp"],
        "registries": "registry",
        "repositories": {"main": "repo"},
        "github_repo_name": "vllm-project/../../attacker/repo",
    }
    with patch("os.path.exists", return_value=True):
        with pytest.raises(ValueError, match="Invalid github_repo_name"):
            _validate_pipeline_config(config)


@pytest.mark.parametrize(
    ("value", "is_valid"),
    [
        (True, True),
        (False, True),
        ("true", False),
        ("false", False),
        (0, False),
        (1, False),
        (None, False),
    ],
)
def test_validate_pipeline_config_requires_boolean_amd_capability(value, is_valid):
    config = {
        "name": "test",
        "job_dirs": ["/tmp"],
        "registries": "registry",
        "repositories": {"main": "repo"},
        "amd_hf_offline_retry": value,
    }
    with patch("os.path.exists", return_value=True):
        if is_valid:
            _validate_pipeline_config(config)
        else:
            with pytest.raises(
                ValueError, match="amd_hf_offline_retry must be a boolean"
            ):
                _validate_pipeline_config(config)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, False),
        ("0", False),
        ("1", True),
        ("", ValueError),
        ("true", ValueError),
        ("false", ValueError),
        ("yes", ValueError),
        ("2", ValueError),
        ("-1", ValueError),
    ],
)
def test_hf_offline_retry_kill_switch_is_strict(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(HF_OFFLINE_RETRY_KILL_SWITCH_ENV, raising=False)
    else:
        monkeypatch.setenv(HF_OFFLINE_RETRY_KILL_SWITCH_ENV, value)

    if expected is ValueError:
        with pytest.raises(ValueError, match="must be exactly '0' or '1'"):
            _read_strict_bool_env(HF_OFFLINE_RETRY_KILL_SWITCH_ENV)
    else:
        assert _read_strict_bool_env(HF_OFFLINE_RETRY_KILL_SWITCH_ENV) is expected
