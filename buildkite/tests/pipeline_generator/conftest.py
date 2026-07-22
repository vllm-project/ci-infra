import pytest

import buildkite_step
import step as step_module


@pytest.fixture
def fake_global_config(monkeypatch):
    monkeypatch.delenv(buildkite_step.SKIP_TIMEOUT_ENV_VAR, raising=False)
    config = {
        "name": "vllm_ci",
        "github_repo_name": "vllm-project/vllm",
        "job_dirs": [],
        "registries": "example.com/vllm",
        "repositories": {
            "main": "vllm-ci-postmerge-repo",
            "premerge": "vllm-ci-test-repo",
        },
        "branch": "test-branch",
        "commit": "abc123",
        "pull_request": "false",
        "docs_only_disable": "1",
        "nightly": "0",
        "torch_nightly": "0",
        "run_all": False,
        "list_file_diff": [],
        "fail_fast": False,
    }
    monkeypatch.setattr(step_module, "get_global_config", lambda: config)
    monkeypatch.setattr(buildkite_step, "get_global_config", lambda: config)
    monkeypatch.setattr(
        buildkite_step,
        "get_image",
        lambda cpu=False, arm64=False: "test-image",
    )
    monkeypatch.setattr(
        buildkite_step,
        "get_torch_nightly_image",
        lambda: "torch-nightly-image",
    )
    return config
