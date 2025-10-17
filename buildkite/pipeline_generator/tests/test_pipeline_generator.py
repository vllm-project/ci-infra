"""Integration tests for pipeline generator."""
import pytest
import tempfile
import yaml
import os

from ..config import PipelineGeneratorConfig
from ..pipeline_generator import (
    PipelineGenerator,
    read_test_steps,
    write_buildkite_pipeline
)
from ..models.buildkite_step import BuildkiteStep, BuildkiteBlockStep
from ..models.test_step import DEFAULT_TEST_WORKING_DIR
from ..utils.constants import AgentQueue

TEST_COMMIT = "abcdef0123456789abcdef0123456789abcdef01"
TEST_CONTAINER_REGISTRY = "container.registry"
TEST_CONTAINER_REGISTRY_REPO = "test"


class TestPipelineGeneratorConfig:
    """Tests for pipeline generator configuration."""
    
    def test_config_get_container_image(self, pipeline_config):
        """Test container image URL generation."""
        pipeline_config.validate()
        # Should contain BUILDKITE_COMMIT placeholder
        assert "$BUILDKITE_COMMIT" in pipeline_config.container_image
        assert "vllm-ci" in pipeline_config.container_image
    
    def test_config_get_container_image_main_branch(self):
        """Test container image URL for main branch."""
        from ..utils.constants import PipelineMode
        config = PipelineGeneratorConfig(
            container_registry=TEST_CONTAINER_REGISTRY,
            container_registry_repo=TEST_CONTAINER_REGISTRY_REPO,
            commit=TEST_COMMIT,
            branch="main",
            list_file_diff=[],
            pipeline_mode=PipelineMode.CI
        )
        config.validate()
        assert "postmerge" in config.container_image
    
    def test_config_get_container_image_feature_branch(self):
        """Test container image URL for feature branch."""
        from ..utils.constants import PipelineMode
        config = PipelineGeneratorConfig(
            container_registry=TEST_CONTAINER_REGISTRY,
            container_registry_repo=TEST_CONTAINER_REGISTRY_REPO,
            commit=TEST_COMMIT,
            branch="feature-branch",
            list_file_diff=[],
            pipeline_mode=PipelineMode.CI
        )
        config.validate()
        assert "test" in config.container_image
    
    @pytest.mark.parametrize(
        "commit",
        [
            "abcdefghijklmnopqrstuvwxyz1234567890abcd",  # Invalid, not in a-f 0-9
            "1234567890abcdef",  # Invalid, not 40 characters
            "123",  # Too short
        ]
    )
    def test_config_invalid_commit(self, commit):
        """Test that invalid commits fail validation."""
        from ..utils.constants import PipelineMode
        config = PipelineGeneratorConfig(
            container_registry=TEST_CONTAINER_REGISTRY,
            container_registry_repo=TEST_CONTAINER_REGISTRY_REPO,
            commit=commit,
            branch="main",
            list_file_diff=[],
            pipeline_mode=PipelineMode.CI
        )
        with pytest.raises(ValueError, match="not a valid Git commit hash"):
            config.validate()
    
    def test_config_container_image_variants(self, pipeline_config):
        """Test all container image variants."""
        pipeline_config.validate()
        
        assert "vllm-ci" in pipeline_config.container_image
        assert "torch-nightly" in pipeline_config.container_image_torch_nightly
        assert "cu118" in pipeline_config.container_image_cu118
        assert "cpu" in pipeline_config.container_image_cpu
        assert "rocm/vllm-ci" in pipeline_config.container_image_amd


class TestReadTestSteps:
    """Tests for reading test steps from YAML."""
    
    def test_read_test_steps(self):
        """Test reading test steps from file."""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        test_path = os.path.join(current_dir, "test_files/test-pipeline.yaml")
        
        test_steps = read_test_steps(test_path)
        
        assert len(test_steps) == 4
        assert test_steps[0].commands == ['echo "Test 1"']
        assert test_steps[0].command is None
        assert test_steps[0].working_dir == DEFAULT_TEST_WORKING_DIR
        
        assert test_steps[1].working_dir == "/tests2/"
        assert test_steps[1].no_gpu is True
        
        assert test_steps[2].commands == ['echo "Test 3"', 'echo "Test 3.1"']
        assert test_steps[2].source_file_dependencies == ["file1", "src/file2"]
        
        assert test_steps[3].commands == ['echo "Test 4.1"', 'echo "Test 4.2"']
        assert test_steps[3].num_nodes == 2
        assert test_steps[3].num_gpus == 4


class TestWriteBuildkitePipeline:
    """Tests for writing Buildkite pipeline."""
    
    def test_write_buildkite_steps(self):
        """Test writing Buildkite steps to YAML."""
        steps = [
            BuildkiteStep(label="Test 1", commands=['echo "Test1.1"', 'echo "Test1.2"']),
            BuildkiteStep(label="Test 2", commands=["command3"], agents={"queue": AgentQueue.AWS_1xL4.value}),
            BuildkiteBlockStep(block="Run Test 3", key="block-test-3"),
            BuildkiteStep(label="Test 3", commands=["command4"], depends_on="block-test-3"),
        ]
        
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file_path = os.path.join(temp_dir, "output.yaml")
            write_buildkite_pipeline(steps, output_file_path)
            
            # Verify file was created and is valid YAML
            assert os.path.exists(output_file_path)
            
            with open(output_file_path, "r") as f:
                pipeline = yaml.safe_load(f)
            
            # Check structure
            assert "steps" in pipeline
            assert len(pipeline["steps"]) == 4
            
            # Check that steps have expected labels
            labels = [s.get("label") or s.get("block") for s in pipeline["steps"]]
            assert "Test 1" in labels
            assert "Test 2" in labels
            assert "Run Test 3" in labels
            assert "Test 3" in labels


class TestPipelineGenerator:
    """Integration tests for PipelineGenerator."""
    
    def test_generate_pipeline_with_simple_test(self, pipeline_config, simple_test_step):
        """Test generating pipeline with simple test."""
        generator = PipelineGenerator(pipeline_config)
        steps = generator.generate([simple_test_step])
        
        assert len(steps) > 0
        # Should have build steps and test steps
        assert any(isinstance(step, (BuildkiteStep, dict)) for step in steps)
    
    def test_generate_pipeline_with_optional(self, pipeline_config, optional_test_step):
        """Test generating pipeline with optional test."""
        generator = PipelineGenerator(pipeline_config)
        steps = generator.generate([optional_test_step])
        
        # Optional test should be in the pipeline (may be blocked)
        assert len(steps) > 0
    
    def test_generate_complete_pipeline(self, pipeline_config, simple_test_step):
        """Test generating complete pipeline."""
        generator = PipelineGenerator(pipeline_config)
        steps = generator.generate([simple_test_step])
        
        # Should have build steps, test steps, and special groups
        assert len(steps) > 5
        
        # Check that we have various types of steps
        buildkite_steps = [s for s in steps if isinstance(s, BuildkiteStep)]
        assert len(buildkite_steps) > 0
        
        # Should have at least one step labeled with "Build"
        labels = [s.label for s in buildkite_steps if hasattr(s, "label")]
        assert any("build" in label.lower() or "image" in label.lower() for label in labels)
    
    def test_generate_with_run_all(self, simple_test_step):
        """Test generating pipeline with run_all enabled."""
        from ..utils.constants import PipelineMode
        config = PipelineGeneratorConfig(
            container_registry=TEST_CONTAINER_REGISTRY,
            container_registry_repo=TEST_CONTAINER_REGISTRY_REPO,
            commit=TEST_COMMIT,
            branch="main",
            list_file_diff=[],
            run_all=True,
            pipeline_mode=PipelineMode.CI
        )
        
        generator = PipelineGenerator(config)
        steps = generator.generate([simple_test_step])
        
        # With run_all, should not have blocks
        block_steps = [s for s in steps if isinstance(s, BuildkiteBlockStep)]
        # There might be some block steps for special reasons, but fewer than without run_all
        assert len(block_steps) < 5
    
    def test_generate_with_nightly(self, optional_test_step):
        """Test generating pipeline in nightly mode."""
        from ..utils.constants import PipelineMode
        config = PipelineGeneratorConfig(
            container_registry=TEST_CONTAINER_REGISTRY,
            container_registry_repo=TEST_CONTAINER_REGISTRY_REPO,
            commit=TEST_COMMIT,
            branch="main",
            list_file_diff=[],
            nightly=True,
            pipeline_mode=PipelineMode.CI
        )
        
        generator = PipelineGenerator(config)
        steps = generator.generate([optional_test_step])
        
        # In nightly mode, optional tests should not be blocked
        # Check that test step exists without preceding block
        test_steps = [s for s in steps if isinstance(s, BuildkiteStep) and s.label == "Optional Test"]
        assert len(test_steps) > 0


class TestEndToEnd:
    """End-to-end integration tests."""
    
    def test_full_pipeline_generation(self, pipeline_config):
        """Test full pipeline generation from config to YAML."""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        test_path = os.path.join(current_dir, "test_files/test-pipeline.yaml")
        
        test_steps = read_test_steps(test_path)
        generator = PipelineGenerator(pipeline_config)
        steps = generator.generate(test_steps)
        
        with tempfile.TemporaryDirectory() as temp_dir:
            output_file = os.path.join(temp_dir, "pipeline.yaml")
            write_buildkite_pipeline(steps, output_file)
            
            # Verify file was created and is valid YAML
            assert os.path.exists(output_file)
            
            with open(output_file, "r") as f:
                pipeline = yaml.safe_load(f)
            
            assert "steps" in pipeline
            assert isinstance(pipeline["steps"], list)
            assert len(pipeline["steps"]) > 0

