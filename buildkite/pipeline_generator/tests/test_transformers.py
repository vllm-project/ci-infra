"""Tests for command transformers."""
from ..transformers.normalizer import normalize_command, normalize_commands, flatten_commands
from ..transformers.test_targeting import TestTargetingTransformer
from ..transformers.coverage import (
    CoverageTransformer,
    inject_coverage_into_command,
    get_coverage_file_id,
    inject_coverage_into_commands
)
from ..models.test_step import TestStep


class TestNormalizer:
    """Tests for command normalization."""
    
    def test_normalize_command_removes_backslashes(self):
        """Test that normalize_command removes backslashes."""
        cmd = "pytest \\  test.py"
        assert normalize_command(cmd) == "pytest   test.py"
    
    def test_normalize_command_no_backslashes(self):
        """Test normalize_command with no backslashes."""
        cmd = "pytest test.py"
        assert normalize_command(cmd) == "pytest test.py"
    
    def test_normalize_commands_list(self):
        """Test normalizing a list of commands."""
        commands = ["pytest \\ test1.py", "pytest test2.py", "echo \\hello"]
        expected = ["pytest  test1.py", "pytest test2.py", "echo hello"]
        assert normalize_commands(commands) == expected
    
    def test_flatten_commands_simple_list(self):
        """Test flattening a simple command list."""
        commands = ["cmd1", "cmd2", "cmd3"]
        assert flatten_commands(commands) == ["cmd1", "cmd2", "cmd3"]
    
    def test_flatten_commands_multi_node(self):
        """Test flattening multi-node commands (list of lists)."""
        commands = [["cmd1", "cmd2"], ["cmd3", "cmd4"]]
        # Should return first node's commands
        assert flatten_commands(commands) == ["cmd1", "cmd2"]
    
    def test_flatten_commands_empty(self):
        """Test flattening empty commands."""
        assert flatten_commands([]) == []
        assert flatten_commands(None) == []


class TestTestTargetingTransformer:
    """Tests for intelligent test targeting transformer."""
    
    def test_transform_no_test_changes(self):
        """Test transform when non-test files changed."""
        transformer = TestTargetingTransformer()
        test_step = TestStep(
            label="Test",
            commands=["pytest v1/"],
            source_file_dependencies=["tests/v1/"]
        )
        
        class Config:
            list_file_diff = ["vllm/engine.py", "vllm/model.py"]
        
        result = transformer.transform(["pytest v1/"], test_step, Config())
        # Should return None when only test changes don't apply
        assert result is None
    
    def test_transform_only_tests_changed_with_match(self):
        """Test transform when only matching tests changed."""
        transformer = TestTargetingTransformer()
        test_step = TestStep(
            label="Test",
            # Commands should match the test paths for the filtering to work
            commands=["pytest v1/test_engine.py v1/test_model.py"],
            source_file_dependencies=["tests/v1/"]
        )
        
        class Config:
            list_file_diff = ["tests/v1/test_engine.py", "tests/v1/test_model.py"]
        
        result = transformer.transform(["pytest v1/test_engine.py v1/test_model.py"], test_step, Config())
        # Should return targeted command when tests match
        assert result is not None
        assert "pytest -v -s" in result
        assert "v1/test_engine.py" in result
        assert "v1/test_model.py" in result
    
    def test_transform_only_tests_changed_no_match(self):
        """Test transform when tests changed but no match."""
        transformer = TestTargetingTransformer()
        test_step = TestStep(
            label="Test",
            commands=["pytest v1/"],
            source_file_dependencies=["tests/v1/"]
        )
        
        class Config:
            list_file_diff = ["tests/v2/test_other.py"]
        
        result = transformer.transform(["pytest v1/"], test_step, Config())
        # Should return None when no targets match
        assert result is None
    
    def test_transform_with_pytest_markers(self):
        """Test transform preserves pytest markers."""
        transformer = TestTargetingTransformer()
        test_step = TestStep(
            label="Test",
            commands=["pytest -m slow v1/test_engine.py"],
            source_file_dependencies=["tests/v1/"]
        )
        
        class Config:
            list_file_diff = ["tests/v1/test_engine.py"]
        
        result = transformer.transform(["pytest -m slow v1/test_engine.py"], test_step, Config())
        assert result is not None
        assert "-m 'slow'" in result or "-m slow" in result
        assert "v1/test_engine.py" in result


class TestCoverageTransformer:
    """Tests for coverage injection transformer."""
    
    def test_inject_coverage_into_command_pytest(self):
        """Test injecting coverage into pytest command."""
        cmd = "pytest test.py"
        coverage_file = ".coverage.test"
        result = inject_coverage_into_command(cmd, coverage_file)
        assert "COVERAGE_FILE=" in result
        assert "--cov=vllm" in result
        assert "--cov-report=" in result
        assert "--cov-append" in result
        assert "|| true" in result
    
    def test_inject_coverage_into_command_non_pytest(self):
        """Test injecting coverage into non-pytest command."""
        cmd = "echo hello"
        coverage_file = ".coverage.test"
        result = inject_coverage_into_command(cmd, coverage_file)
        assert result == "echo hello"
    
    def test_get_coverage_file_id(self):
        """Test generating coverage file ID."""
        assert get_coverage_file_id("Test Step") == ".coverage.9_T"
        assert get_coverage_file_id("A") == ".coverage.1_A"
        assert get_coverage_file_id("") == ".coverage.0_x"
    
    def test_inject_coverage_into_commands(self):
        """Test injecting coverage into multiple commands."""
        commands = ["pytest test1.py", "pytest test2.py"]
        label = "Test"
        branch = "main"
        result = inject_coverage_into_commands(commands, label, branch)
        
        assert "COVERAGE_FILE=" in result
        assert "--cov=vllm" in result
        assert " && " in result  # Commands joined
        assert "upload_codecov.sh" in result  # Upload script added
        assert f'"{label}"' in result  # Label passed to script
    
    def test_inject_coverage_into_commands_no_pytest(self):
        """Test injecting coverage with no pytest commands."""
        commands = ["echo hello", "echo world"]
        label = "Test"
        branch = "main"
        result = inject_coverage_into_commands(commands, label, branch)
        
        assert result == "echo hello && echo world"
        assert "upload_codecov.sh" not in result
    
    def test_coverage_transformer_enabled(self):
        """Test CoverageTransformer when coverage enabled."""
        transformer = CoverageTransformer()
        test_step = TestStep(label="Test", commands=["pytest test.py"])
        
        class Config:
            cov_enabled = True
            vllm_ci_branch = "main"
        
        result = transformer.transform(["pytest test.py"], test_step, Config())
        assert "COVERAGE_FILE=" in result
        assert "--cov=vllm" in result
    
    def test_coverage_transformer_disabled(self):
        """Test CoverageTransformer when coverage disabled."""
        transformer = CoverageTransformer()
        test_step = TestStep(label="Test", commands=["pytest test.py"])
        
        class Config:
            cov_enabled = False
            vllm_ci_branch = "main"
        
        result = transformer.transform(["pytest test.py"], test_step, Config())
        assert result == "pytest test.py"
        assert "COVERAGE_FILE=" not in result

