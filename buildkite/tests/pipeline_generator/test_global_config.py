import os
from contextlib import ExitStack
from unittest.mock import mock_open, patch

import pytest

from buildkite.pipeline_generator.global_config import (
    ONLY_STEP_KEYS_ENV_VAR,
    _matches_run_all_patterns,
    _parse_only_step_keys,
    _parse_platform,
    _validate_pipeline_config,
    init_global_config,
)
from constants import Platform


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


@pytest.mark.parametrize(
    "value, torch_nightly, expected",
    [
        (None, False, None),
        ("", False, None),
        ("amd", False, Platform.AMD),
        (" CUDA ", False, Platform.CUDA),
        (None, True, Platform.CUDA),
        ("cuda", True, Platform.CUDA),
    ],
)
def test_parse_platform(value, torch_nightly, expected):
    assert _parse_platform(value, torch_nightly) == expected


def test_parse_platform_rejects_unknown_platform():
    with pytest.raises(ValueError, match="must be one of: amd, cuda"):
        _parse_platform("tpu", False)


def test_parse_platform_rejects_amd_under_torch_nightly():
    with pytest.raises(ValueError, match="TORCH_NIGHTLY runs only CUDA jobs"):
        _parse_platform("amd", True)


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


def test_matches_run_all_patterns_applies_the_excludes():
    patterns, excludes = ["csrc/"], ["csrc/cpu/", "csrc/rocm/"]
    match = _matches_run_all_patterns
    assert match(["csrc/attention/a.cu"], patterns, excludes)
    assert not match(["csrc/cpu/a.cpp"], patterns, excludes)
    assert not match(["csrc/rocm/a.cu"], patterns, excludes)
    assert not match(["vllm/a.py"], patterns, excludes)
    assert not match(["csrc/a.cu"], None, excludes), "no patterns, no match"


# vLLM's ci_config.yaml in miniature: the CUDA excludes name files that do not
# affect CUDA, so AMD keeps a list of its own.
AMD_RUN_ALL_CONFIG = """\
name: test
job_dirs: [/tmp]
registries: reg
repositories: {main: repo}
run_all_patterns: ["docker/Dockerfile"]
run_all_exclude_patterns: ["docker/Dockerfile.", "csrc/cpu/", "csrc/rocm/"]
amd_run_all_patterns: ["csrc/", "docker/Dockerfile.rocm_base"]
"""


def _init_with(pipeline_config, list_file_diff):
    import buildkite.pipeline_generator.global_config as global_config

    with ExitStack() as stack:
        for patcher in (
            patch.object(global_config, "get_merge_base_commit", return_value="sha"),
            patch.object(
                global_config, "get_list_file_diff", return_value=list_file_diff
            ),
            patch.object(global_config, "get_pr_labels", return_value=[]),
            patch("builtins.open", mock_open(read_data=pipeline_config)),
            patch("os.path.exists", return_value=True),
            patch.dict(os.environ, {"BUILDKITE_BRANCH": "test-branch"}),
        ):
            stack.enter_context(patcher)
        init_global_config("dummy_path")
    return global_config.config


@pytest.mark.parametrize(
    "changed, run_all, amd_run_all",
    [
        ("docker/Dockerfile", True, False),
        ("docker/Dockerfile.rocm_base", False, True),
        ("csrc/rocm/attention.cu", False, True),
        ("csrc/attention/a.cu", False, True),
        ("csrc/cpu/a.cpp", False, False),
        ("vllm/a.py", False, False),
    ],
)
def test_amd_run_all_uses_its_own_excludes(changed, run_all, amd_run_all):
    config = _init_with(
        AMD_RUN_ALL_CONFIG + 'amd_run_all_exclude_patterns: ["csrc/cpu/"]\n',
        [changed],
    )
    assert (config["run_all"], config["amd_run_all"]) == (run_all, amd_run_all)


def test_amd_run_all_without_its_own_excludes_excludes_nothing():
    """The CUDA excludes never apply to AMD, set or not."""
    config = _init_with(AMD_RUN_ALL_CONFIG, ["csrc/cpu/a.cpp"])
    assert not config["run_all"] and config["amd_run_all"]
