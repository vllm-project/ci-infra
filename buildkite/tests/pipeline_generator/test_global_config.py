import os
from unittest.mock import mock_open, patch

import pytest

from buildkite.pipeline_generator.global_config import (
    ONLY_STEP_KEYS_ENV_VAR,
    _parse_only_step_keys,
    _validate_pipeline_config,
    init_global_config,
)


@pytest.fixture(autouse=True)
def reset_config():
    import buildkite.pipeline_generator.global_config

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
    read_data="name: test\njob_dirs: [/tmp]\nregistries: reg\nrepositories: {main: repo}",
)
@patch("os.path.exists", return_value=True)
def test_init_global_config_valid_branch(
    mock_exists, mock_open, mock_pr_labels, mock_diff, mock_mb
):
    with patch.dict(os.environ, {"BUILDKITE_BRANCH": "valid-branch-name_123/pkg"}):
        init_global_config("dummy_path")
        # Should succeed


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
        # Should succeed


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
