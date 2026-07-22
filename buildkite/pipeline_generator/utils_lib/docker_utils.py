from global_config import get_global_config


def get_image(cpu: bool = False, arm64: bool = False) -> str:
    global_config = get_global_config()
    commit = "$BUILDKITE_COMMIT"
    branch = global_config["branch"]
    registries = global_config["registries"]
    repositories = global_config["repositories"]
    image = None
    if branch == "main":
        image = f"{registries}/{repositories['main']}:{commit}"
    else:
        image = f"{registries}/{repositories['premerge']}:{commit}"
    # In the full torch-nightly run, every step must use the nightly image. Use
    # the dedicated -torch-nightly tag so it never collides with the shared
    # postmerge tag (no overwrite / race with the regular postmerge image).
    if global_config.get("torch_nightly") == "1":
        image = f"{image}-torch-nightly"
    if cpu:
        image = f"{image}-cpu"
    if arm64:
        image = f"{image}-arm64"
    return image


def get_torch_nightly_image() -> str:
    global_config = get_global_config()
    commit = "$BUILDKITE_COMMIT"
    registries = global_config["registries"]
    repositories = global_config["repositories"]
    if global_config["branch"] == "main":
        return f"{registries}/{repositories['main']}:{commit}-torch-nightly"
    else:
        return f"{registries}/{repositories['premerge']}:{commit}-torch-nightly"
