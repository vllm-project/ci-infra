"""Data classes for Docker and Kubernetes plugin configurations."""
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


# Environment configuration
HF_HOME_FSX = "/fsx/hf_cache"
HF_HOME = "/root/.cache/huggingface"


@dataclass
class DockerEnvironment:
    """Docker environment configuration."""
    hf_home: str = HF_HOME_FSX
    additional_vars: List[str] = field(default_factory=list)
    fail_fast: bool = False
    is_main_branch: bool = False
    special_attention_backend: bool = False
    skip_codecov: bool = False  # For fastcheck mode
    
    def get_environment_list(self) -> List[str]:
        """Get list of environment variables."""
        env_vars = [
            "VLLM_USAGE_SOURCE=ci-test",
            "NCCL_CUMEM_HOST_ENABLE=0",
            f"HF_HOME={self.hf_home}",
            "HF_TOKEN"
        ]
        
        # CODECOV_TOKEN not in fastcheck
        if not self.skip_codecov:
            env_vars.append("CODECOV_TOKEN")
        
        if self.fail_fast:
            env_vars.append("PYTEST_ADDOPTS=-x")
        
        if self.is_main_branch:
            env_vars.append("BUILDKITE_ANALYTICS_TOKEN")
        
        if self.special_attention_backend:
            env_vars.append("VLLM_ATTENTION_BACKEND=XFORMERS")
        
        env_vars.extend(self.additional_vars)
        return env_vars


@dataclass
class DockerVolumes:
    """Docker volume configuration."""
    hf_home: str = HF_HOME_FSX
    additional_volumes: List[str] = field(default_factory=list)
    
    def get_volume_list(self) -> List[str]:
        """Get list of volume mounts."""
        volumes = [
            "/dev/shm:/dev/shm",
            f"{self.hf_home}:{self.hf_home}"
        ]
        volumes.extend(self.additional_volumes)
        return volumes


@dataclass
class StandardDockerConfig:
    """Configuration for standard Docker plugin."""
    image: str
    command: str
    bash_flags: str = "-xc"
    has_gpu: bool = True
    environment: Optional[DockerEnvironment] = None
    volumes: Optional[DockerVolumes] = None
    mount_buildkite_agent: bool = False
    
    def to_plugin_dict(self) -> Dict[str, Any]:
        """Convert to Docker plugin dictionary."""
        env = self.environment or DockerEnvironment()
        vols = self.volumes or DockerVolumes()
        
        plugin = {
            "docker#v5.2.0": {
                "image": self.image,
                "always-pull": True,
                "propagate-environment": True,
                "command": ["bash", self.bash_flags, self.command],
                "environment": env.get_environment_list(),
                "volumes": vols.get_volume_list()
            }
        }
        
        if self.has_gpu:
            plugin["docker#v5.2.0"]["gpus"] = "all"
        
        if self.mount_buildkite_agent:
            plugin["docker#v5.2.0"]["mount-buildkite-agent"] = True
        
        return plugin


@dataclass
class SpecialGPUDockerConfig:
    """Configuration for special GPUs (H200, B200) that need custom paths."""
    image: str
    command: str
    bash_flags: str = "-xc"
    gpu_type: str = "h200"  # h200 or b200
    fail_fast: bool = False
    is_main_branch: bool = False
    
    def to_plugin_dict(self) -> Dict[str, Any]:
        """Convert to Docker plugin dictionary for special GPUs."""
        env_vars = [
            "VLLM_USAGE_SOURCE=ci-test",
            "NCCL_CUMEM_HOST_ENABLE=0",
            "HF_HOME=/benchmark-hf-cache",
            "HF_TOKEN",
            "CODECOV_TOKEN"
        ]
        
        if self.fail_fast:
            env_vars.append("PYTEST_ADDOPTS=-x")
        
        if self.is_main_branch:
            env_vars.append("BUILDKITE_ANALYTICS_TOKEN")
        
        volumes = [
            "/dev/shm:/dev/shm",
            "/data/benchmark-hf-cache:/benchmark-hf-cache",
            "/data/benchmark-vllm-cache:/root/.cache/vllm"
        ]
        
        plugin = {
            "docker#v5.2.0": {
                "image": self.image,
                "always-pull": True,
                "propagate-environment": True,
                "command": ["bash", self.bash_flags, self.command],
                "environment": env_vars,
                "volumes": volumes
            }
        }
        
        # B200 doesn't specify gpus (configured by environment variable)
        if self.gpu_type != "b200":
            plugin["docker#v5.2.0"]["gpus"] = "all"
        
        return plugin


@dataclass
class KubernetesResourceConfig:
    """Kubernetes resource configuration."""
    num_gpus: int = 1
    gpu_product: str = "NVIDIA-H100-80GB-HBM3"
    
    def get_resource_limits(self) -> Dict[str, Any]:
        """Get resource limits."""
        return {"nvidia.com/gpu": self.num_gpus}
    
    def get_node_selector(self) -> Dict[str, str]:
        """Get node selector."""
        return {"nvidia.com/gpu.product": self.gpu_product}


@dataclass
class KubernetesConfig:
    """Configuration for Kubernetes plugin."""
    image: str
    command: str
    resources: KubernetesResourceConfig
    hf_cache_path: str = "/mnt/hf-cache"
    priority_class: Optional[str] = None
    
    def to_plugin_dict(self) -> Dict[str, Any]:
        """Convert to Kubernetes plugin dictionary."""
        pod_spec: Dict[str, Any] = {
            "containers": [{
                "image": self.image,
                "command": [f'bash -c "{self.command}"'],
                "resources": {
                    "limits": self.resources.get_resource_limits()
                },
                "volumeMounts": [
                    {"name": "devshm", "mountPath": "/dev/shm"},
                    {"name": "hf-cache", "mountPath": HF_HOME}
                ],
                "env": [
                    {"name": "VLLM_USAGE_SOURCE", "value": "ci-test"},
                    {"name": "NCCL_CUMEM_HOST_ENABLE", "value": "0"},
                    {"name": "HF_HOME", "value": HF_HOME}
                ]
            }],
            "nodeSelector": self.resources.get_node_selector(),
            "volumes": [
                {"name": "devshm", "emptyDir": {"medium": "Memory"}},
                {"name": "hf-cache", "hostPath": {"path": self.hf_cache_path, "type": "Directory"}}
            ]
        }
        
        if self.priority_class:
            pod_spec["priorityClassName"] = self.priority_class
            # Add HF_TOKEN secret for A100
            pod_spec["containers"][0]["env"].append({
                "name": "HF_TOKEN",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "hf-token-secret",
                        "key": "token"
                    }
                }
            })
        
        return {"kubernetes": {"podSpec": pod_spec}}


# GPU-specific Kubernetes configurations
def get_h100_kubernetes_config(image: str, command: str, num_gpus: int = 1) -> Dict[str, Any]:
    """Get H100 Kubernetes configuration."""
    config = KubernetesConfig(
        image=image,
        command=command,
        resources=KubernetesResourceConfig(
            num_gpus=num_gpus,
            gpu_product="NVIDIA-H100-80GB-HBM3"
        ),
        hf_cache_path="/mnt/hf-cache"
    )
    return config.to_plugin_dict()


def get_a100_kubernetes_config(image: str, command: str, num_gpus: int = 1) -> Dict[str, Any]:
    """Get A100 Kubernetes configuration."""
    config = KubernetesConfig(
        image=image,
        command=command,
        resources=KubernetesResourceConfig(
            num_gpus=num_gpus,
            gpu_product="NVIDIA-A100-SXM4-80GB"
        ),
        hf_cache_path=HF_HOME,
        priority_class="ci"
    )
    return config.to_plugin_dict()

