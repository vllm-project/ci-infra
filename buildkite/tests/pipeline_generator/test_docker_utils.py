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
