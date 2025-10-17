"""Tests for Docker plugin construction."""
import pytest

from ..utils.constants import PipelineMode
from ..docker.plugin_builder import (
    build_docker_command,
    build_full_docker_command,
    build_plugin_for_test_step,
)
from ..models.test_step import TestStep
from ..models.docker_config import (
    DockerEnvironment,
    DockerVolumes,
    StandardDockerConfig,
    SpecialGPUDockerConfig,
    HF_HOME_FSX
)
from ..utils.constants import GPUType


class TestDockerEnvironment:
    """Tests for Docker environment configuration."""
    
    def test_get_environment_list_basic(self):
        """Test basic environment list."""
        env = DockerEnvironment(hf_home=HF_HOME_FSX)
        env_list = env.get_environment_list()
        
        assert f"HF_HOME={HF_HOME_FSX}" in env_list
        assert "VLLM_USAGE_SOURCE=ci-test" in env_list
        assert "HF_TOKEN" in env_list
        assert "CODECOV_TOKEN" in env_list
    
    def test_get_environment_list_with_fail_fast(self):
        """Test environment list with fail fast."""
        env = DockerEnvironment(fail_fast=True)
        env_list = env.get_environment_list()
        
        assert "PYTEST_ADDOPTS=-x" in env_list
    
    def test_get_environment_list_main_branch(self):
        """Test environment list for main branch."""
        env = DockerEnvironment(is_main_branch=True)
        env_list = env.get_environment_list()
        
        assert "BUILDKITE_ANALYTICS_TOKEN" in env_list
    
    def test_get_environment_list_special_attention_backend(self):
        """Test environment list with special attention backend."""
        env = DockerEnvironment(special_attention_backend=True)
        env_list = env.get_environment_list()
        
        assert "VLLM_ATTENTION_BACKEND=XFORMERS" in env_list
    
    def test_get_environment_list_additional_vars(self):
        """Test environment list with additional variables."""
        env = DockerEnvironment(additional_vars=["CUSTOM_VAR=value"])
        env_list = env.get_environment_list()
        
        assert "CUSTOM_VAR=value" in env_list


class TestDockerVolumes:
    """Tests for Docker volumes configuration."""
    
    def test_get_volume_list_basic(self):
        """Test basic volume list."""
        volumes = DockerVolumes(hf_home=HF_HOME_FSX)
        volume_list = volumes.get_volume_list()
        
        assert "/dev/shm:/dev/shm" in volume_list
        assert f"{HF_HOME_FSX}:{HF_HOME_FSX}" in volume_list
    
    def test_get_volume_list_additional(self):
        """Test volume list with additional volumes."""
        volumes = DockerVolumes(additional_volumes=["/data:/data"])
        volume_list = volumes.get_volume_list()
        
        assert "/data:/data" in volume_list


class TestStandardDockerConfig:
    """Tests for standard Docker configuration."""
    
    def test_to_plugin_dict_basic(self):
        """Test basic Docker plugin dict."""
        config = StandardDockerConfig(
            image="test:latest",
            command="echo hello"
        )
        plugin = config.to_plugin_dict()
        
        assert "docker#v5.2.0" in plugin
        assert plugin["docker#v5.2.0"]["image"] == "test:latest"
        assert plugin["docker#v5.2.0"]["command"] == ["bash", "-xc", "echo hello"]
        assert plugin["docker#v5.2.0"]["always-pull"] is True
        assert plugin["docker#v5.2.0"]["gpus"] == "all"
    
    def test_to_plugin_dict_no_gpu(self):
        """Test Docker plugin dict without GPU."""
        config = StandardDockerConfig(
            image="test:latest",
            command="echo hello",
            has_gpu=False
        )
        plugin = config.to_plugin_dict()
        
        assert "gpus" not in plugin["docker#v5.2.0"]
    
    def test_to_plugin_dict_mount_agent(self):
        """Test Docker plugin dict with mounted agent."""
        config = StandardDockerConfig(
            image="test:latest",
            command="echo hello",
            mount_buildkite_agent=True
        )
        plugin = config.to_plugin_dict()
        
        assert plugin["docker#v5.2.0"]["mount-buildkite-agent"] is True


class TestSpecialGPUDockerConfig:
    """Tests for special GPU Docker configuration."""
    
    def test_to_plugin_dict_h200(self):
        """Test H200 Docker plugin dict."""
        config = SpecialGPUDockerConfig(
            image="test:latest",
            command="echo hello",
            gpu_type="h200"
        )
        plugin = config.to_plugin_dict()
        
        assert "docker#v5.2.0" in plugin
        assert plugin["docker#v5.2.0"]["gpus"] == "all"
        assert "HF_HOME=/benchmark-hf-cache" in plugin["docker#v5.2.0"]["environment"]
    
    def test_to_plugin_dict_b200(self):
        """Test B200 Docker plugin dict (no gpus key)."""
        config = SpecialGPUDockerConfig(
            image="test:latest",
            command="echo hello",
            gpu_type="b200"
        )
        plugin = config.to_plugin_dict()
        
        assert "gpus" not in plugin["docker#v5.2.0"]


class TestDockerCommandBuilder:
    """Tests for Docker command builder."""
    
    def test_build_docker_command_simple(self):
        """Test building simple Docker command."""
        test_step = TestStep(label="Test", commands=["pytest test.py"])
        
        class Config:
            pipeline_mode = PipelineMode.CI
            list_file_diff = []
            cov_enabled = False
            vllm_ci_branch = "main"
        
        result = build_docker_command(test_step, Config())
        assert result == "pytest test.py"
    
    def test_build_docker_command_with_coverage(self):
        """Test building Docker command with coverage."""
        test_step = TestStep(label="Test", commands=["pytest test.py"])
        
        class Config:
            pipeline_mode = PipelineMode.CI
            list_file_diff = []
            cov_enabled = True
            vllm_ci_branch = "main"
        
        result = build_docker_command(test_step, Config())
        assert "COVERAGE_FILE=" in result
        assert "--cov=vllm" in result
    
    def test_build_docker_command_intelligent_targeting(self):
        """Test building Docker command with intelligent targeting."""
        test_step = TestStep(
            label="Test",
            commands=["pytest v1/test_engine.py"],
            source_file_dependencies=["tests/v1/"]
        )
        
        class Config:
            pipeline_mode = PipelineMode.CI
            list_file_diff = ["tests/v1/test_engine.py"]
            cov_enabled = False
            vllm_ci_branch = "main"
        
        result = build_docker_command(test_step, Config())
        # Should apply intelligent targeting
        assert "pytest -v -s" in result
        assert "v1/test_engine.py" in result
    
    def test_build_full_docker_command(self):
        """Test building full Docker command."""
        test_step = TestStep(
            label="Test",
            commands=["pytest test.py"],
            working_dir="/tests"
        )
        
        class Config:
            pipeline_mode = PipelineMode.CI
            list_file_diff = []
            cov_enabled = False
            vllm_ci_branch = "main"
        
        result = build_full_docker_command(test_step, Config())
        assert "(command nvidia-smi || true)" in result
        assert "VLLM_ALLOW_DEPRECATED_BEAM_SEARCH" in result
        assert "cd /tests" in result
        assert "pytest test.py" in result


class TestPluginBuilder:
    """Tests for plugin builder."""
    
    def test_build_plugin_for_standard_gpu(self):
        """Test building plugin for standard GPU."""
        test_step = TestStep(label="Test", commands=["pytest test.py"])
        
        class Config:
            pipeline_mode = PipelineMode.CI
            list_file_diff = []
            cov_enabled = False
            vllm_ci_branch = "main"
            fail_fast = False
            branch = "feature"
        
        plugin = build_plugin_for_test_step(test_step, "test:latest", Config())
        
        assert "docker#v5.2.0" in plugin
        assert plugin["docker#v5.2.0"]["image"] == "test:latest"
    
    def test_build_plugin_for_h200(self):
        """Test building plugin for H200."""
        test_step = TestStep(
            label="Test",
            commands=["pytest test.py"],
            gpu=GPUType.H200
        )
        
        class Config:
            pipeline_mode = PipelineMode.CI
            list_file_diff = []
            cov_enabled = False
            vllm_ci_branch = "main"
            fail_fast = False
            branch = "main"
        
        plugin = build_plugin_for_test_step(test_step, "test:latest", Config())
        
        assert "docker#v5.2.0" in plugin
        assert "HF_HOME=/benchmark-hf-cache" in plugin["docker#v5.2.0"]["environment"]
    
    def test_build_plugin_for_h100(self):
        """Test building plugin for H100 (Kubernetes)."""
        test_step = TestStep(
            label="Test",
            commands=["pytest test.py"],
            gpu=GPUType.H100,
            num_gpus=2
        )
        
        class Config:
            pipeline_mode = PipelineMode.CI
            list_file_diff = []
            cov_enabled = False
            vllm_ci_branch = "main"
            fail_fast = False
            branch = "main"
        
        plugin = build_plugin_for_test_step(test_step, "test:latest", Config())
        
        # Should return Kubernetes plugin
        assert "kubernetes" in plugin
    
    def test_build_plugin_with_coverage_enabled(self):
        """Test building plugin with coverage enabled."""
        test_step = TestStep(label="Test", commands=["pytest test.py"])
        
        class Config:
            pipeline_mode = PipelineMode.CI
            list_file_diff = []
            cov_enabled = True
            vllm_ci_branch = "main"
            fail_fast = False
            branch = "main"
        
        plugin = build_plugin_for_test_step(test_step, "test:latest", Config())
        
        # Should mount buildkite agent when coverage enabled
        assert plugin["docker#v5.2.0"]["mount-buildkite-agent"] is True

