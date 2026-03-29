import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock
from buildkite_step import (
    _create_block_step,
    _generate_step_key,
    BuildkiteBlockStep,
)
from step import Step


def _make_step(label: str, **kwargs) -> Step:
    """Helper to create a minimal Step for testing."""
    defaults = {
        "label": label,
    }
    defaults.update(kwargs)
    return Step(**defaults)


# ---------- _create_block_step tests ----------

class TestCreateBlockStep:
    def test_returns_buildkite_block_step(self):
        step = _make_step("My Test Step")
        result = _create_block_step(step)
        assert isinstance(result, BuildkiteBlockStep)

    def test_block_field_contains_label(self):
        step = _make_step("Lint Check")
        result = _create_block_step(step)
        assert result.block == "Run Lint Check"

    def test_depends_on_is_empty_list(self):
        step = _make_step("Unit Tests")
        result = _create_block_step(step)
        assert result.depends_on == []

    def test_key_uses_generated_step_key(self):
        step = _make_step("Build Image")
        result = _create_block_step(step)
        assert result.key == f"block-{_generate_step_key('Build Image')}"
        assert result.key == "block-build-image"

    def test_docker_label(self):
        step = _make_step(":docker: Build")
        result = _create_block_step(step)
        assert result.block == "Run :docker: Build"
        assert result.depends_on == []
        assert result.key == "block--docker--build"


# ---------- _generate_step_key tests ----------

class TestGenerateStepKey:
    def test_spaces_become_dashes(self):
        assert _generate_step_key("hello world") == "hello-world"

    def test_lowercased(self):
        assert _generate_step_key("Hello World") == "hello-world"

    def test_parentheses_removed(self):
        assert _generate_step_key("test (gpu)") == "test-gpu"

    def test_percent_removed(self):
        assert _generate_step_key("coverage 80%") == "coverage-80"

    def test_comma_becomes_dash(self):
        assert _generate_step_key("a,b,c") == "a-b-c"

    def test_plus_becomes_dash(self):
        assert _generate_step_key("c++") == "c--"

    def test_colon_becomes_dash(self):
        assert _generate_step_key(":docker: build") == "-docker--build"

    def test_dot_becomes_dash(self):
        assert _generate_step_key("v1.2.3") == "v1-2-3"

    def test_slash_becomes_dash(self):
        assert _generate_step_key("path/to/thing") == "path-to-thing"

    def test_combined_special_chars(self):
        result = _generate_step_key("Test (GPU, 2x): v1.0+build")
        assert result == "test-gpu--2x--v1-0-build"

    def test_empty_string(self):
        assert _generate_step_key("") == ""

    def test_no_special_chars(self):
        assert _generate_step_key("simple") == "simple"
