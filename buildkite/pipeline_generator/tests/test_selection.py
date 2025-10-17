"""Tests for test selection logic."""
import pytest

from ..utils.constants import PipelineMode
from ..selection.filtering import (
    should_run_step,
    get_changed_tests,
    are_only_tests_changed,
    get_intelligent_test_targets,
    extract_covered_test_paths,
    extract_pytest_markers
)
from ..selection.blocking import should_block_step
from ..models.test_step import TestStep


class TestShouldRunStep:
    """Tests for should_run_step logic."""
    
    def test_should_run_step_run_all_enabled(self):
        """Test that all steps run when run_all is enabled."""
        test_step = TestStep(label="Test", commands=["pytest test.py"])
        
        class Config:
            pipeline_mode = PipelineMode.CI
            run_all = True
            nightly = False
            list_file_diff = []
        
        assert should_run_step(test_step, Config()) is True
    
    def test_should_run_step_nightly_enabled(self):
        """Test that all steps run when nightly is enabled."""
        test_step = TestStep(label="Test", commands=["pytest test.py"])
        
        class Config:
            pipeline_mode = PipelineMode.CI
            run_all = False
            nightly = True
            list_file_diff = []
        
        assert should_run_step(test_step, Config()) is True
    
    def test_should_run_step_with_matching_dependency(self):
        """Test step runs when dependencies match file diff."""
        test_step = TestStep(
            label="Test",
            commands=["pytest test.py"],
            source_file_dependencies=["vllm/engine/"]
        )
        
        class Config:
            pipeline_mode = PipelineMode.CI
            run_all = False
            nightly = False
            list_file_diff = ["vllm/engine/engine.py", "vllm/model.py"]
        
        assert should_run_step(test_step, Config()) is True
    
    def test_should_run_step_no_matching_dependency(self):
        """Test step doesn't run when dependencies don't match."""
        test_step = TestStep(
            label="Test",
            commands=["pytest test.py"],
            source_file_dependencies=["vllm/engine/"]
        )
        
        class Config:
            pipeline_mode = PipelineMode.CI
            run_all = False
            nightly = False
            list_file_diff = ["vllm/model.py"]
        
        assert should_run_step(test_step, Config()) is False
    
    def test_should_run_step_no_dependencies(self):
        """Test step runs when no dependencies specified."""
        test_step = TestStep(label="Test", commands=["pytest test.py"])
        
        class Config:
            pipeline_mode = PipelineMode.CI
            run_all = False
            nightly = False
            list_file_diff = ["vllm/model.py"]
        
        assert should_run_step(test_step, Config()) is True


class TestChangedTests:
    """Tests for detecting changed test files."""
    
    def test_get_changed_tests(self):
        """Test extracting changed test files."""
        file_diff = [
            "tests/v1/test_engine.py",
            "tests/v1/test_model.py",
            "vllm/engine.py",
            "tests/v2/test_executor.py"
        ]
        changed = get_changed_tests(file_diff)
        assert changed == [
            "v1/test_engine.py",
            "v1/test_model.py",
            "v2/test_executor.py"
        ]
    
    def test_get_changed_tests_no_tests(self):
        """Test extracting changed tests when none changed."""
        file_diff = ["vllm/engine.py", "vllm/model.py"]
        changed = get_changed_tests(file_diff)
        assert changed == []
    
    def test_are_only_tests_changed_true(self):
        """Test detection when only tests changed."""
        file_diff = [
            "tests/v1/test_engine.py",
            "tests/v2/test_model.py"
        ]
        assert are_only_tests_changed(file_diff) is True
    
    def test_are_only_tests_changed_false(self):
        """Test detection when non-test files also changed."""
        file_diff = [
            "tests/v1/test_engine.py",
            "vllm/engine.py"
        ]
        assert are_only_tests_changed(file_diff) is False
    
    def test_are_only_tests_changed_empty(self):
        """Test detection with empty file diff."""
        assert are_only_tests_changed([]) is False


class TestIntelligentTestTargeting:
    """Tests for intelligent test targeting."""
    
    def test_get_intelligent_test_targets_match(self):
        """Test getting targets when tests match dependencies."""
        test_step = TestStep(
            label="Test",
            commands=["pytest v1/core/test_engine.py v1/core/test_model.py"],
            source_file_dependencies=["tests/v1/core/"]
        )
        changed_tests = ["v1/core/test_engine.py", "v1/core/test_model.py", "v2/test_other.py"]
        
        targets = get_intelligent_test_targets(test_step, changed_tests)
        # Tests in v1/core/ should match dependencies
        assert len(targets) >= 2  # At least the two tests that match
        # If filtering works, these should be in targets
        for target in targets:
            assert target.startswith("v1/core/") or target.startswith("v2/")
    
    def test_get_intelligent_test_targets_no_dependencies(self):
        """Test getting targets when no dependencies specified."""
        test_step = TestStep(
            label="Test",
            commands=["pytest v1/"]
        )
        changed_tests = ["v1/test_engine.py"]
        
        targets = get_intelligent_test_targets(test_step, changed_tests)
        assert targets == []
    
    def test_get_intelligent_test_targets_file_dependency(self):
        """Test targeting with specific file dependency."""
        test_step = TestStep(
            label="Test",
            commands=["pytest v1/test_engine.py"],
            source_file_dependencies=["tests/v1/test_engine"]
        )
        changed_tests = ["v1/test_engine.py", "v1/test_model.py"]
        
        targets = get_intelligent_test_targets(test_step, changed_tests)
        assert "v1/test_engine.py" in targets
        assert "v1/test_model.py" not in targets


class TestExtractCoveredTestPaths:
    """Tests for extracting covered test paths."""
    
    def test_extract_covered_test_paths(self):
        """Test extracting test paths from pytest commands."""
        commands = ["pytest v1/core/", "pytest v1/engine/test_specific.py"]
        paths = extract_covered_test_paths(commands)
        
        assert "v1/core/" in paths
        assert "v1/engine/test_specific.py" in paths
    
    def test_extract_covered_test_paths_no_pytest(self):
        """Test extracting from non-pytest commands."""
        commands = ["echo hello", "ls"]
        paths = extract_covered_test_paths(commands)
        assert paths == []
    
    def test_extract_covered_test_paths_with_flags(self):
        """Test extracting paths with pytest flags."""
        commands = ["pytest -v -s v1/core/"]
        paths = extract_covered_test_paths(commands)
        assert "v1/core/" in paths
    
    def test_extract_pytest_markers(self):
        """Test extracting pytest markers."""
        commands = ["pytest -m slow v1/"]
        markers = extract_pytest_markers(commands)
        assert "-m slow" in markers or "-m 'slow'" in markers
    
    def test_extract_pytest_markers_quoted(self):
        """Test extracting quoted markers."""
        commands = ["pytest -m 'not slow' v1/"]
        markers = extract_pytest_markers(commands)
        assert "-m 'not slow'" in markers
    
    def test_extract_pytest_markers_none(self):
        """Test extracting markers when none present."""
        commands = ["pytest v1/"]
        markers = extract_pytest_markers(commands)
        assert markers == ""


class TestShouldBlockStep:
    """Tests for block step decisions."""
    
    def test_should_block_optional_step(self):
        """Test that optional steps are blocked when not in nightly mode."""
        test_step = TestStep(
            label="Test",
            commands=["pytest test.py"],
            optional=True
        )
        
        class Config:
            pipeline_mode = PipelineMode.CI
            run_all = False
            nightly = False
            list_file_diff = []
        
        assert should_block_step(test_step, Config()) is True
    
    def test_should_not_block_optional_step_in_nightly(self):
        """Test that optional steps are not blocked in nightly mode."""
        test_step = TestStep(
            label="Test",
            commands=["pytest test.py"],
            optional=True
        )
        
        class Config:
            pipeline_mode = PipelineMode.CI
            run_all = False
            nightly = True
            list_file_diff = []
        
        assert should_block_step(test_step, Config()) is False
    
    def test_should_block_step_no_matching_dependencies(self):
        """Test blocking when dependencies don't match."""
        test_step = TestStep(
            label="Test",
            commands=["pytest test.py"],
            source_file_dependencies=["vllm/engine/"]
        )
        
        class Config:
            pipeline_mode = PipelineMode.CI
            run_all = False
            nightly = False
            list_file_diff = ["vllm/model.py"]
        
        assert should_block_step(test_step, Config()) is True
    
    def test_should_not_block_step_matching_dependencies(self):
        """Test not blocking when dependencies match."""
        test_step = TestStep(
            label="Test",
            commands=["pytest test.py"],
            source_file_dependencies=["vllm/engine/"]
        )
        
        class Config:
            pipeline_mode = PipelineMode.CI
            run_all = False
            nightly = False
            list_file_diff = ["vllm/engine/engine.py"]
        
        assert should_block_step(test_step, Config()) is False
    
    def test_torch_nightly_group_blocking(self):
        """Test blocking logic for torch nightly group."""
        test_step = TestStep(
            label="Test",
            commands=["pytest test.py"],
            torch_nightly=True,
            source_file_dependencies=["vllm/engine/"]
        )
        
        class Config:
            pipeline_mode = PipelineMode.CI
            run_all = False
            nightly = False
            list_file_diff = []  # No matching files
        
        # In torch nightly group with dependencies but no matches, should be blocked
        assert should_block_step(test_step, Config(), is_torch_nightly_group=True) is True
    
    def test_torch_nightly_group_not_blocking_in_nightly_mode(self):
        """Test not blocking torch nightly steps in nightly mode."""
        test_step = TestStep(
            label="Test",
            commands=["pytest test.py"],
            torch_nightly=True
        )
        
        class Config:
            pipeline_mode = PipelineMode.CI
            run_all = False
            nightly = True
            list_file_diff = []
        
        assert should_block_step(test_step, Config(), is_torch_nightly_group=True) is False

