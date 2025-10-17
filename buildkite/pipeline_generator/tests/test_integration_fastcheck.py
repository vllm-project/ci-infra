#!/usr/bin/env python3
"""
Integration test suite for fastcheck pipeline mode.
Compares Python generator output against test-template-fastcheck.j2
"""

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Tuple

import pytest
import yaml

from buildkite.pipeline_generator.config import VLLM_ECR_REPO, VLLM_ECR_URL, PipelineGeneratorConfig, PipelineMode
from buildkite.pipeline_generator.pipeline_generator import (
    PipelineGenerator,
    read_test_steps,
    write_buildkite_pipeline,
)

# Add parent directories to path for imports
current_dir = os.path.dirname(os.path.abspath(__file__))
pipeline_gen_dir = os.path.dirname(current_dir)
buildkite_dir = os.path.dirname(pipeline_gen_dir)
ci_infra_dir = os.path.dirname(buildkite_dir)
sys.path.insert(0, ci_infra_dir)


@dataclass
class Scenario:
    """Test scenario for fastcheck mode."""

    name: str
    branch: str = "main"
    vllm_use_precompiled: str = "0"
    fail_fast: str = "false"
    mirror_hw: str = "amdexperimental"
    commit: str = "0" * 40
    description: str = ""


def get_fastcheck_scenarios() -> List[Scenario]:
    """Get test scenarios for fastcheck mode."""
    return [
        Scenario(name="fastcheck_default", description="Default fastcheck configuration"),
        Scenario(name="fastcheck_main_branch", branch="main", description="Fastcheck on main branch"),
        Scenario(
            name="fastcheck_pr_branch",
            branch="feature-branch",
            description="Fastcheck on PR branch",
        ),
        Scenario(
            name="fastcheck_precompiled",
            vllm_use_precompiled="1",
            description="Fastcheck with precompiled wheels",
        ),
        Scenario(
            name="fastcheck_no_precompiled",
            vllm_use_precompiled="0",
            description="Fastcheck building from source",
        ),
        Scenario(
            name="fastcheck_fail_fast",
            fail_fast="true",
            description="Fastcheck with fail-fast enabled",
        ),
        Scenario(
            name="fastcheck_amd_production",
            mirror_hw="amdproduction",
            description="Fastcheck with AMD production hardware",
        ),
        Scenario(
            name="fastcheck_amd_experimental",
            mirror_hw="amdexperimental",
            description="Fastcheck with AMD experimental hardware",
        ),
    ]


def run_jinja_fastcheck(scenario: Scenario, template_path: str, test_pipeline_path: str, output_path: str) -> Tuple[bool, str]:
    """Run Jinja template to generate pipeline."""
    try:
        cmd = [
            "minijinja-cli",
            template_path,
            test_pipeline_path,
            "-D",
            f"vllm_use_precompiled={scenario.vllm_use_precompiled}",
            "-D",
            f"fail_fast={scenario.fail_fast}",
            "-D",
            f"mirror_hw={scenario.mirror_hw}",
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, check=True)

        # Remove blank lines like bootstrap.sh does
        lines = [line for line in result.stdout.split("\n") if line.strip()]
        output = "\n".join(lines) + "\n"

        with open(output_path, "w") as f:
            f.write(output)

        return True, ""
    except Exception as e:
        return False, str(e)


def run_python_fastcheck(scenario: Scenario, test_pipeline_path: str, output_path: str) -> Tuple[bool, str]:
    """Run Python generator to generate pipeline."""
    try:
        test_steps = read_test_steps(test_pipeline_path)

        config = PipelineGeneratorConfig(
            run_all=False,
            nightly=False,
            list_file_diff=[],
            container_registry=VLLM_ECR_URL,
            container_registry_repo=VLLM_ECR_REPO,
            commit=scenario.commit,
            branch=scenario.branch,
            mirror_hw=scenario.mirror_hw,
            fail_fast=scenario.fail_fast == "true",
            vllm_use_precompiled=scenario.vllm_use_precompiled,
            cov_enabled=False,
            vllm_ci_branch="main",
            pipeline_mode=PipelineMode.FASTCHECK,
        )

        generator = PipelineGenerator(config)
        steps = generator.generate(test_steps)
        write_buildkite_pipeline(steps, output_path)

        return True, ""
    except Exception as e:
        return False, str(e)


def normalize_for_comparison(obj):
    """Recursively normalize data structure for comparison (order-independent)."""
    if isinstance(obj, dict):
        # Convert dict to sorted tuple of items for comparison
        return tuple(sorted((k, normalize_for_comparison(v)) for k, v in obj.items()))
    elif isinstance(obj, list):
        # Lists maintain order, normalize each element
        return tuple(normalize_for_comparison(item) for item in obj)
    elif isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    else:
        return str(obj)


def compare_yaml_trees(jinja_path: str, python_path: str) -> Tuple[bool, str]:
    """Compare two YAML files for structural equality (field order independent)."""
    with open(jinja_path, "r") as f:
        jinja_data = yaml.safe_load(f)
    with open(python_path, "r") as f:
        python_data = yaml.safe_load(f)

    # Normalize both structures for order-independent comparison
    jinja_normalized = normalize_for_comparison(jinja_data)
    python_normalized = normalize_for_comparison(python_data)
    
    matches = jinja_normalized == python_normalized
    
    if not matches:
        # Show diff using sorted YAML for readability
        import difflib
        jinja_str = yaml.dump(jinja_data, default_flow_style=False, sort_keys=True)
        python_str = yaml.dump(python_data, default_flow_style=False, sort_keys=True)
        
        diff = list(difflib.unified_diff(
            jinja_str.splitlines(keepends=True),
            python_str.splitlines(keepends=True),
            fromfile='jinja',
            tofile='python',
            lineterm=''
        ))
        
        diff_msg = "YAML structures don't match (semantically different data)\n" + "".join(diff[:50])
    else:
        diff_msg = ""

    return matches, diff_msg


# ============================================================================
# PYTEST TEST FUNCTIONS
# ============================================================================


@pytest.fixture(scope="module")
def template_path():
    """Path to Fastcheck Jinja template."""
    ci_infra_path = "/Users/rezabarazesh/Documents/test/ci-infra"
    return os.path.join(ci_infra_path, "buildkite/test-template-fastcheck.j2")


@pytest.fixture(scope="module")
def test_pipeline_path():
    """Path to test pipeline YAML."""
    vllm_path = "/Users/rezabarazesh/Documents/test/vllm"
    return os.path.join(vllm_path, ".buildkite/test-pipeline.yaml")


@pytest.fixture(scope="module")
def check_minijinja():
    """Check that minijinja-cli is available."""
    try:
        subprocess.run(["minijinja-cli", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip(
            "minijinja-cli not found. Install: curl -sSfL https://github.com/mitsuhiko/minijinja/releases/download/2.3.1/minijinja-cli-installer.sh | sh"
        )


@pytest.mark.parametrize("scenario", get_fastcheck_scenarios(), ids=lambda s: s.name)
def test_fastcheck_pipeline_scenario(scenario, template_path, test_pipeline_path, check_minijinja, tmp_path):
    """Test that Python generator produces identical output to Jinja template for fastcheck scenarios."""
    jinja_output = tmp_path / f"jinja_{scenario.name}.yaml"
    python_output = tmp_path / f"python_{scenario.name}.yaml"

    # Generate with Jinja
    jinja_success, jinja_error = run_jinja_fastcheck(scenario, template_path, test_pipeline_path, str(jinja_output))
    assert jinja_success, f"Jinja generation failed: {jinja_error}"

    # Generate with Python
    python_success, python_error = run_python_fastcheck(scenario, test_pipeline_path, str(python_output))
    assert python_success, f"Python generation failed: {python_error}"

    # Also save to /tmp for debugging
    import shutil
    shutil.copy(str(jinja_output), f'/tmp/jinja_{scenario.name}.yaml')
    shutil.copy(str(python_output), f'/tmp/python_{scenario.name}.yaml')

    # Compare outputs
    matches, diff = compare_yaml_trees(str(jinja_output), str(python_output))
    assert matches, f"Pipeline outputs don't match:\n{diff}"


# Run with: pytest tests/test_integration_fastcheck.py -v
# To run specific scenario: pytest tests/test_integration_fastcheck.py -k
# "precompiled"
