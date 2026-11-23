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
        docker_plugin.arm_cpu_plugin_template,
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


def test_arm_cpu_plugin_has_no_gpus_and_mounts_the_agent_hf_cache(
    fake_global_config,
):
    plugin = docker_plugin.get_docker_plugin(
        Step(label="Arm CPU", device="arm_cpu"), "example/image:latest"
    )

    assert "gpus" not in plugin
    assert plugin["volumes"] == [
        "/var/lib/buildkite-agent/.cache/huggingface:/root/.cache/huggingface"
    ]


# Each listed step, the GPU count its quota was staged at, and the quota per GPU.
@pytest.mark.parametrize(
    ("key", "gpus", "gpumem_mib"),
    [
        ("v1-attention-b200", 1, 32768),
        ("kernels-root-misc-test-b200", 1, 73728),
        ("kimi-k3-unit-tests-b200", 1, 6144),
        ("kernels-mhc-test-b200", 1, 4096),
        ("kernels-b200", 1, 90000),
        ("deepseek-v4-kernel-test-b200", 1, 8192),
        ("kernels-minimax-m3-attention-b200", 1, 8192),
        ("fusion-and-compile-unit-tests-2xb200", 1, 8192),
        ("kernels-fusedmoe-layer-test-2-b200s", 2, 24576),
        ("fusion-e2e-tp2-b200", 2, 90000),
        ("v1-attention-h100-mi300", 1, 32768),
        ("deepseek-v4-kernel-test-h100", 1, 16384),
        ("kernels-deepgemm-test-h100", 1, 20480),
        ("kernels-minimax-m3-sparse-prefill-h100", 1, 8192),
        ("kernels-bf16x3-router-gemm-h100", 1, 8192),
        ("kernels-attention-diffkv-test-h100", 1, 8192),
        ("kernels-fusedmoe-layer-test-2-h100s", 2, 20480),
        ("distributed-compile-unit-tests-2xh100", 2, 12288),
        ("kernels-minimax-reduce-rms-test-2-gpus", 2, 12288),
        ("kernels-fp8-moe-test-2xh100", 2, 16384),
        ("fusion-e2e-tp2-quick-h100", 2, 40000),
        ("fault-tolerance-e2e-4xh100", 4, 40000),
        ("dsv41-nvidia-sm80-sm90-decode-8xh100", 8, 12288),
    ],
)
def test_listed_step_gets_a_hami_share_on_the_sharing_node(
    fake_global_config, key, gpus, gpumem_mib
):
    device = (
        "h100"
        if "h100" in key or key == "kernels-minimax-reduce-rms-test-2-gpus"
        else "b200-k8s"
    )
    step = Step(label=key, key=key, device=device, num_devices=gpus)

    plugin = k8s_plugin.get_k8s_plugin(step, "example/image:latest")

    k8s = plugin["kubernetes"]
    assert k8s["metadata"] == {"labels": {"vllm.ci/gpu-sharing": "hami"}}
    pod_spec = k8s["podSpec"]
    assert pod_spec["nodeSelector"] == {"gpu": "on"}
    assert pod_spec["tolerations"] == [
        {
            "key": "nvidia.com/gpu",
            "operator": "Equal",
            "value": "hami",
            "effect": "NoSchedule",
        }
    ]
    container = pod_spec["containers"][0]
    assert container["resources"]["limits"] == {
        "nvidia.com/gpu": gpus,
        "nvidia.com/gpumem": gpumem_mib,
    }
    env = {entry["name"]: entry["value"] for entry in container["env"] if "value" in entry}
    assert env["VLLM_CI_HAMI_GPUMEM_MIB"] == str(gpumem_mib)


def test_every_b200_hami_quota_leaves_room_to_colocate():
    # Physical B200 card memory reported by HAMi is 183,359 MiB.
    min_quota = min(k8s_plugin.HAMI_GPUMEM_MIB.values())
    for key, quota in k8s_plugin.HAMI_GPUMEM_MIB.items():
        assert quota + min_quota <= 183359, (
            f"{key} ({quota} MiB) cannot share a card with any listed step"
        )


def test_every_h100_hami_quota_leaves_room_to_colocate():
    # Physical H100 card memory reported by HAMi is 81,559 MiB.
    min_quota = min(k8s_plugin.HAMI_GPUMEM_MIB_H100.values())
    for key, quota in k8s_plugin.HAMI_GPUMEM_MIB_H100.items():
        assert quota + min_quota <= 81559, (
            f"{key} ({quota} MiB) cannot share a card with any listed step"
        )


def test_hami_merges_existing_pod_metadata_selector_and_tolerations(
    fake_global_config, monkeypatch
):
    import copy

    template = copy.deepcopy(k8s_plugin.b200_plugin_template)
    template["kubernetes"]["metadata"] = {
        "annotations": {"existing": "keep"},
        "labels": {"existing-label": "keep"},
    }
    template["kubernetes"]["podSpec"]["nodeSelector"] = {"existing-node": "keep"}
    template["kubernetes"]["podSpec"]["tolerations"] = [
        {"key": "existing", "operator": "Exists", "effect": "NoSchedule"}
    ]
    monkeypatch.setattr(k8s_plugin, "b200_plugin_template", template)

    plugin = k8s_plugin.get_k8s_plugin(
        Step(label="Kimi", key="kimi-k3-unit-tests-b200", device="b200-k8s"),
        "example/image:latest",
    )

    assert plugin["kubernetes"]["metadata"] == {
        "annotations": {"existing": "keep"},
        "labels": {"existing-label": "keep", "vllm.ci/gpu-sharing": "hami"},
    }
    pod_spec = plugin["kubernetes"]["podSpec"]
    assert pod_spec["nodeSelector"] == {"existing-node": "keep", "gpu": "on"}
    assert pod_spec["tolerations"] == [
        {"key": "existing", "operator": "Exists", "effect": "NoSchedule"},
        {
            "key": "nvidia.com/gpu",
            "operator": "Equal",
            "value": "hami",
            "effect": "NoSchedule",
        },
    ]


def test_every_listed_step_has_a_test_row():
    assert len(k8s_plugin.HAMI_GPUMEM_MIB) == 10
    assert len(k8s_plugin.HAMI_GPUMEM_MIB_H100) == 13


def test_unlisted_b200_step_keeps_a_whole_card_anywhere(fake_global_config):
    step = Step(
        label="Other", key="moe-refactor-dp-ep-test-b200-deepep-v2", device="b200-k8s"
    )

    plugin = k8s_plugin.get_k8s_plugin(step, "example/image:latest")

    assert plugin == {
        "kubernetes": {
            **k8s_plugin.b200_plugin_template["kubernetes"],
            "podSpec": plugin["kubernetes"]["podSpec"],
        }
    }
    pod_spec = plugin["kubernetes"]["podSpec"]
    assert "nodeSelector" not in pod_spec
    assert "tolerations" not in pod_spec
    assert pod_spec["containers"][0]["resources"]["limits"] == {"nvidia.com/gpu": 1}


def test_hami_list_applies_to_b200_and_h100_only(fake_global_config):
    step = Step(label="Same key", key="v1-attention-b200", device="l4")

    plugin = k8s_plugin.get_k8s_plugin(step, "example/image:latest")

    assert "labels" not in plugin["kubernetes"].get("metadata", {})
    limits = plugin["kubernetes"]["podSpec"]["containers"][0]["resources"]["limits"]
    assert "nvidia.com/gpumem" not in limits


def test_listed_b200_step_asking_for_two_gpus_keeps_whole_cards(fake_global_config):
    step = Step(
        label="Kimi", key="kimi-k3-unit-tests-b200", device="b200-k8s", num_devices=2
    )

    plugin = k8s_plugin.get_k8s_plugin(step, "example/image:latest")

    assert "metadata" not in plugin["kubernetes"]
    limits = plugin["kubernetes"]["podSpec"]["containers"][0]["resources"]["limits"]
    assert limits == {"nvidia.com/gpu": 2}


def test_multi_gpu_step_staged_at_two_gpus_keeps_whole_cards_at_four(
    fake_global_config,
):
    step = Step(
        label="Fused MoE",
        key="kernels-fusedmoe-layer-test-2-b200s",
        device="b200-k8s",
        num_devices=4,
    )

    plugin = k8s_plugin.get_k8s_plugin(step, "example/image:latest")

    assert "metadata" not in plugin["kubernetes"]
    limits = plugin["kubernetes"]["podSpec"]["containers"][0]["resources"]["limits"]
    assert limits == {"nvidia.com/gpu": 4}


def test_listed_step_without_a_gpu_count_is_a_one_gpu_share(fake_global_config):
    # Most step definitions omit num_devices; that means one GPU.
    step = Step(label="V1 attention", key="v1-attention-b200", device="b200-k8s")

    plugin = k8s_plugin.get_k8s_plugin(step, "example/image:latest")

    limits = plugin["kubernetes"]["podSpec"]["containers"][0]["resources"]["limits"]
    assert limits == {"nvidia.com/gpu": 1, "nvidia.com/gpumem": 32768}


def test_b200_quota_never_applies_to_the_same_key_on_h100(fake_global_config):
    # A step that moves to H100 keeps its key; its B200 quota would not fit an
    # 80 GB card, so it keeps whole cards until it is staged on H100.
    key = "fusion-e2e-tp2-b200"
    assert key in k8s_plugin.HAMI_GPUMEM_MIB
    step = Step(
        label="Moved",
        key=key,
        device="h100",
        num_devices=2,
    )

    plugin = k8s_plugin.get_k8s_plugin(step, "example/image:latest")

    assert "metadata" not in plugin["kubernetes"]
    limits = plugin["kubernetes"]["podSpec"]["containers"][0]["resources"]["limits"]
    assert limits == {"nvidia.com/gpu": 2}


def test_h100_quota_never_applies_to_the_same_key_on_b200(fake_global_config):
    key = "v1-attention-h100-mi300"
    assert key in k8s_plugin.HAMI_GPUMEM_MIB_H100
    step = Step(label="Moved", key=key, device="b200-k8s")

    plugin = k8s_plugin.get_k8s_plugin(step, "example/image:latest")

    assert "metadata" not in plugin["kubernetes"]
