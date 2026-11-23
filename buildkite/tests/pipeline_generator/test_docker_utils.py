import utils_lib.docker_utils as docker_utils


def _cfg(**overrides):
    cfg = {
        "registries": "example.com/vllm",
        "repositories": {
            "main": "vllm-ci-postmerge-repo",
            "premerge": "vllm-ci-test-repo",
        },
        "branch": "main",
        "torch_nightly": "0",
    }
    cfg.update(overrides)
    return cfg


def test_get_image_bare_when_not_nightly(monkeypatch):
    monkeypatch.setattr(docker_utils, "get_global_config", lambda: _cfg())
    assert (
        docker_utils.get_image()
        == "example.com/vllm/vllm-ci-postmerge-repo:$BUILDKITE_COMMIT"
    )


def test_get_image_uses_torch_nightly_tag(monkeypatch):
    monkeypatch.setattr(
        docker_utils, "get_global_config", lambda: _cfg(torch_nightly="1")
    )
    # Every step's image switches to the dedicated -torch-nightly tag so the
    # nightly run can't overwrite / race with the shared postmerge image.
    assert (
        docker_utils.get_image()
        == "example.com/vllm/vllm-ci-postmerge-repo:$BUILDKITE_COMMIT-torch-nightly"
    )
    # cpu/arm64 suffixes come after the nightly suffix.
    assert (
        docker_utils.get_image(cpu=True)
        == "example.com/vllm/vllm-ci-postmerge-repo:$BUILDKITE_COMMIT-torch-nightly-cpu"
    )


def test_get_image_arm64_cpu_matches_the_arm64_cpu_build(monkeypatch):
    # image_build_cpu_arm64.sh pushes $REPO:<commit>-arm64-cpu, nightly or not.
    monkeypatch.setattr(
        docker_utils, "get_global_config", lambda: _cfg(torch_nightly="1")
    )
    assert (
        docker_utils.get_image(cpu=True, arm64=True)
        == "example.com/vllm/vllm-ci-postmerge-repo:$BUILDKITE_COMMIT-arm64-cpu"
    )
    monkeypatch.setattr(docker_utils, "get_global_config", lambda: _cfg(branch="my-pr"))
    assert (
        docker_utils.get_image(cpu=True, arm64=True)
        == "example.com/vllm/vllm-ci-test-repo:$BUILDKITE_COMMIT-arm64-cpu"
    )


def test_triggered_main_build_uses_premerge_image_and_cache(monkeypatch):
    monkeypatch.setattr(
        docker_utils, "get_global_config", lambda: _cfg(pull_request="false")
    )
    monkeypatch.setenv("BUILDKITE_SOURCE", "trigger_job")
    assert (
        docker_utils.get_image()
        == "example.com/vllm/vllm-ci-test-repo:$BUILDKITE_COMMIT"
    )
    assert (
        docker_utils.get_torch_nightly_image()
        == "example.com/vllm/vllm-ci-test-repo:$BUILDKITE_COMMIT-torch-nightly"
    )

    monkeypatch.setattr(docker_utils.subprocess, "Popen", lambda *a, **k: type("P", (), {"stdout": type("S", (), {"close": lambda self: None})(), "wait": lambda self: 0})())
    monkeypatch.setattr(docker_utils.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(docker_utils, "_docker_manifest_exists", lambda tag: False)
    cache_from, cache_to = docker_utils.get_ecr_cache_registry()
    assert cache_to == "936637512419.dkr.ecr.us-east-1.amazonaws.com/vllm-ci-test-cache:main"
    assert cache_from == "936637512419.dkr.ecr.us-east-1.amazonaws.com/vllm-ci-postmerge-cache:latest"


def test_non_pr_string_false_uses_postmerge_cache_on_trusted_main(monkeypatch):
    monkeypatch.delenv("BUILDKITE_SOURCE", raising=False)
    monkeypatch.delenv("BUILDKITE_TRIGGERED_FROM_BUILD_ID", raising=False)
    monkeypatch.delenv("BUILDKITE_REPO", raising=False)
    monkeypatch.setattr(
        docker_utils, "get_global_config", lambda: _cfg(pull_request="false")
    )
    monkeypatch.setattr(docker_utils.subprocess, "Popen", lambda *a, **k: type("P", (), {"stdout": type("S", (), {"close": lambda self: None})(), "wait": lambda self: 0})())
    monkeypatch.setattr(docker_utils.subprocess, "run", lambda *a, **k: None)
    cache_from, cache_to = docker_utils.get_ecr_cache_registry()
    assert cache_to == "936637512419.dkr.ecr.us-east-1.amazonaws.com/vllm-ci-postmerge-cache:latest"
    assert cache_from == "936637512419.dkr.ecr.us-east-1.amazonaws.com/vllm-ci-postmerge-cache:latest"
