import pytest

from plugin import docker_plugin, k8s_plugin
from plugin.analytics import (
    BUILDKITE_ANALYTICS_SECRET,
    BUILDKITE_ANALYTICS_TOKEN,
)
from step import Step


@pytest.mark.parametrize(
    "template",
    [
        docker_plugin.docker_plugin_template,
        docker_plugin.h200_18gb_plugin_template,
        docker_plugin.h200_35gb_plugin_template,
        docker_plugin.h200_plugin_template,
        docker_plugin.b200_plugin_template,
    ],
)
def test_standard_docker_templates_forward_buildkite_analytics_token(template):
    assert BUILDKITE_ANALYTICS_TOKEN in template["environment"]


def test_every_trusted_main_step_mounts_buildkite_agent(fake_global_config):
    fake_global_config["branch"] = "main"
    step = Step(label="Traced", device="h200_35gb")

    plugin = docker_plugin.get_docker_plugin(step, "example/image:latest")

    assert plugin["mount_buildkite_agent"] is True


def test_pull_request_step_does_not_mount_agent_for_tracing(fake_global_config):
    fake_global_config["branch"] = "main"
    fake_global_config["pull_request"] = "123"
    step = Step(label="Untrusted", device="h200_35gb")

    plugin = docker_plugin.get_docker_plugin(step, "example/image:latest")

    assert not plugin.get("mount_buildkite_agent", False)


@pytest.mark.parametrize(
    "template",
    [
        k8s_plugin.nebius_h200_plugin_template,
        k8s_plugin.h100_plugin_template,
        k8s_plugin.a100_plugin_template,
        k8s_plugin.b200_plugin_template,
        k8s_plugin.h100_rh_plugin_template,
    ],
)
def test_standard_k8s_templates_inject_buildkite_analytics_token(template):
    container = template["kubernetes"]["podSpec"]["containers"][0]
    env = {entry["name"]: entry for entry in container["env"]}

    assert env[BUILDKITE_ANALYTICS_TOKEN] == {
        "name": BUILDKITE_ANALYTICS_TOKEN,
        "valueFrom": {
            "secretKeyRef": {
                "name": BUILDKITE_ANALYTICS_SECRET,
                "key": "token",
                "optional": True,
            }
        },
    }
