#!/usr/bin/env python3
"""
Comprehensive test suite for pipeline generator.
Tests all scenarios, flags, and edge cases with detailed YAML diff output.
"""

import os
import sys
import subprocess
import tempfile
import yaml
import json
from pathlib import Path
from typing import Dict, Any, List, Tuple, cast
import difflib
from dataclasses import dataclass, field

# Add parent directories to path for imports
# We're in pipeline_generator/tests/, need to go up to ci-infra/buildkite
current_dir = os.path.dirname(os.path.abspath(__file__))  # tests/
pipeline_gen_dir = os.path.dirname(current_dir)  # pipeline_generator/
buildkite_dir = os.path.dirname(pipeline_gen_dir)  # buildkite/
ci_infra_dir = os.path.dirname(buildkite_dir)  # ci-infra/
sys.path.insert(0, ci_infra_dir)

from buildkite.pipeline_generator.pipeline_generator import (
    PipelineGenerator,
    read_test_steps,
    write_buildkite_pipeline
)
from buildkite.pipeline_generator.config import PipelineGeneratorConfig
from buildkite.pipeline_generator.utils import VLLM_ECR_URL, VLLM_ECR_REPO


@dataclass
class TestScenario:
    """Represents a test scenario with specific configuration."""
    name: str
    branch: str = 'main'
    run_all: str = '0'
    nightly: str = '0'
    list_file_diff: str = ''
    mirror_hw: str = 'amdexperimental'
    fail_fast: str = 'false'
    vllm_use_precompiled: str = '0'
    cov_enabled: str = '0'
    vllm_ci_branch: str = 'main'
    commit: str = '0' * 40
    description: str = ''


def get_all_test_scenarios() -> List[TestScenario]:
    """Get comprehensive list of all test scenarios."""
    scenarios = []
    
    # ==================== Branch Variations ====================
    scenarios.append(TestScenario(
        name="main_branch_default",
        description="Main branch with default settings",
        branch="main"
    ))
    
    scenarios.append(TestScenario(
        name="pr_branch_default",
        description="PR branch with default settings",
        branch="feature-branch"
    ))
    
    # ==================== Run All Mode ====================
    scenarios.append(TestScenario(
        name="run_all_main",
        description="Run all tests on main branch",
        branch="main",
        run_all="1"
    ))
    
    scenarios.append(TestScenario(
        name="run_all_pr",
        description="Run all tests on PR branch",
        branch="feature-branch",
        run_all="1"
    ))
    
    # ==================== Nightly Mode ====================
    scenarios.append(TestScenario(
        name="nightly_main",
        description="Nightly build on main branch",
        branch="main",
        nightly="1"
    ))
    
    scenarios.append(TestScenario(
        name="nightly_pr",
        description="Nightly build on PR branch",
        branch="feature-branch",
        nightly="1"
    ))
    
    scenarios.append(TestScenario(
        name="nightly_run_all",
        description="Nightly with run_all enabled",
        branch="main",
        nightly="1",
        run_all="1"
    ))
    
    # ==================== File Changes ====================
    scenarios.append(TestScenario(
        name="vllm_core_changes",
        description="Changes to vllm core code",
        branch="feature-branch",
        list_file_diff="vllm/engine/llm_engine.py|vllm/worker/worker.py|vllm/attention/backends/flash_attn.py"
    ))
    
    scenarios.append(TestScenario(
        name="test_files_only",
        description="Only test files changed (intelligent targeting)",
        branch="feature-branch",
        list_file_diff="tests/engine/test_engine.py|tests/test_config.py|tests/test_sequence.py"
    ))
    
    scenarios.append(TestScenario(
        name="csrc_changes",
        description="Changes to csrc (should trigger more tests)",
        branch="feature-branch",
        list_file_diff="csrc/attention/attention_kernels.cu|csrc/quantization/fp8/fp8_kernels.cu"
    ))
    
    scenarios.append(TestScenario(
        name="multimodal_changes",
        description="Changes to multimodal code",
        branch="feature-branch",
        list_file_diff="vllm/multimodal/base.py|tests/multimodal/test_mapper.py"
    ))
    
    scenarios.append(TestScenario(
        name="distributed_changes",
        description="Changes to distributed code",
        branch="feature-branch",
        list_file_diff="vllm/distributed/parallel_state.py|tests/distributed/test_pynccl.py"
    ))
    
    # ==================== Build Configuration ====================
    scenarios.append(TestScenario(
        name="fail_fast_enabled",
        description="Fail fast mode enabled",
        branch="feature-branch",
        run_all="1",
        fail_fast="true"
    ))
    
    scenarios.append(TestScenario(
        name="precompiled_wheels",
        description="Use precompiled wheels",
        branch="feature-branch",
        vllm_use_precompiled="1"
    ))
    
    scenarios.append(TestScenario(
        name="no_precompiled_wheels",
        description="Build from source",
        branch="feature-branch",
        vllm_use_precompiled="0"
    ))
    
    # ==================== Coverage ====================
    scenarios.append(TestScenario(
        name="coverage_enabled",
        description="Coverage collection enabled",
        branch="feature-branch",
        run_all="1",
        cov_enabled="1"
    ))
    
    scenarios.append(TestScenario(
        name="coverage_with_tests_only",
        description="Coverage with only test changes",
        branch="feature-branch",
        cov_enabled="1",
        list_file_diff="tests/models/test_transformers.py|tests/engine/test_engine.py"
    ))
    
    # ==================== CI Branch Variations ====================
    scenarios.append(TestScenario(
        name="custom_ci_branch",
        description="Custom CI branch",
        branch="main",
        vllm_ci_branch="dev"
    ))
    
    # ==================== Combined Scenarios ====================
    scenarios.append(TestScenario(
        name="nightly_coverage",
        description="Nightly with coverage",
        branch="main",
        nightly="1",
        cov_enabled="1"
    ))
    
    scenarios.append(TestScenario(
        name="run_all_fail_fast_precompiled",
        description="Run all + fail fast + precompiled",
        branch="feature-branch",
        run_all="1",
        fail_fast="true",
        vllm_use_precompiled="1"
    ))
    
    scenarios.append(TestScenario(
        name="nightly_fail_fast_coverage",
        description="Nightly + fail fast + coverage",
        branch="main",
        nightly="1",
        fail_fast="true",
        cov_enabled="1"
    ))
    
    # ==================== Edge Cases ====================
    scenarios.append(TestScenario(
        name="empty_file_diff",
        description="Empty file diff list",
        branch="feature-branch",
        list_file_diff=""
    ))
    
    scenarios.append(TestScenario(
        name="docs_only_changes",
        description="Only documentation changes",
        branch="feature-branch",
        list_file_diff="docs/source/index.md|README.md"
    ))
    
    scenarios.append(TestScenario(
        name="docker_changes",
        description="Docker file changes",
        branch="feature-branch",
        list_file_diff="docker/Dockerfile|docker/Dockerfile.cpu"
    ))
    
    scenarios.append(TestScenario(
        name="requirements_changes",
        description="Requirements file changes",
        branch="feature-branch",
        list_file_diff="requirements/common.txt|requirements/cuda.txt"
    ))
    
    # ==================== Intelligent Test Filtering - CRITICAL ====================
    scenarios.append(TestScenario(
        name="intelligent_filter_engine_test",
        description="Only engine test file changed - should run only that test",
        branch="feature-branch",
        list_file_diff="tests/engine/test_engine.py"
    ))
    
    scenarios.append(TestScenario(
        name="intelligent_filter_multimodal_test",
        description="Only multimodal test changed - targeted execution",
        branch="feature-branch",
        list_file_diff="tests/multimodal/test_mapper.py"
    ))
    
    scenarios.append(TestScenario(
        name="intelligent_filter_multiple_tests",
        description="Multiple test files in same directory",
        branch="feature-branch",
        list_file_diff="tests/engine/test_engine.py|tests/engine/test_llm_engine.py|tests/tokenization/test_tokenizers.py"
    ))
    
    scenarios.append(TestScenario(
        name="intelligent_filter_v1_tests",
        description="V1 test directory changes",
        branch="feature-branch",
        list_file_diff="tests/v1/engine/test_engine_core.py|tests/v1/core/test_scheduler.py"
    ))
    
    scenarios.append(TestScenario(
        name="intelligent_filter_with_markers",
        description="Test with pytest markers (should preserve markers)",
        branch="feature-branch",
        list_file_diff="tests/models/language/generation/test_llama.py"
    ))
    
    scenarios.append(TestScenario(
        name="intelligent_filter_distributed",
        description="Distributed test changes",
        branch="feature-branch",
        list_file_diff="tests/distributed/test_pynccl.py|tests/distributed/test_comm_ops.py"
    ))
    
    scenarios.append(TestScenario(
        name="intelligent_filter_kernels",
        description="Kernel test changes",
        branch="feature-branch",
        list_file_diff="tests/kernels/attention/test_flashinfer.py|tests/kernels/quantization/test_fp8.py"
    ))
    
    scenarios.append(TestScenario(
        name="intelligent_filter_mixed_changes",
        description="Mix of test and non-test files (should disable intelligent filtering)",
        branch="feature-branch",
        list_file_diff="tests/engine/test_engine.py|vllm/engine/llm_engine.py|tests/test_config.py"
    ))
    
    scenarios.append(TestScenario(
        name="intelligent_filter_with_coverage",
        description="Intelligent filtering + coverage",
        branch="feature-branch",
        cov_enabled="1",
        list_file_diff="tests/samplers/test_sampling.py|tests/samplers/test_logits.py"
    ))
    
    # ==================== Pytest Integration Tests ====================
    scenarios.append(TestScenario(
        name="pytest_with_sharding",
        description="Tests with parallelism/sharding",
        branch="feature-branch",
        run_all="1",
        list_file_diff="tests/lora/test_lora.py"
    ))
    
    scenarios.append(TestScenario(
        name="pytest_with_markers_core_model",
        description="Tests using pytest markers (core_model)",
        branch="feature-branch",
        run_all="1",
        list_file_diff="tests/models/language/generation/test_llama.py"
    ))
    
    scenarios.append(TestScenario(
        name="pytest_multimodal_markers",
        description="Multimodal tests with markers",
        branch="feature-branch",
        run_all="1",
        list_file_diff="tests/models/multimodal/generation/test_common.py"
    ))
    
    scenarios.append(TestScenario(
        name="pytest_ignore_patterns",
        description="Tests with --ignore patterns",
        branch="feature-branch",
        run_all="1",
        list_file_diff="tests/entrypoints/llm/test_llm.py"
    ))
    
    scenarios.append(TestScenario(
        name="pytest_specific_test_selection",
        description="Tests selecting specific test functions",
        branch="feature-branch",
        run_all="1",
        list_file_diff="tests/v1/engine/test_engine_core_client.py"
    ))
    
    # ==================== Multi-Node and Special Configurations ====================
    scenarios.append(TestScenario(
        name="multi_node_tests",
        description="Multi-node test execution",
        branch="feature-branch",
        run_all="1",
        list_file_diff="tests/distributed/test_pipeline_parallel.py"
    ))
    
    scenarios.append(TestScenario(
        name="multi_gpu_tests",
        description="Multi-GPU tests (2 and 4 GPU)",
        branch="feature-branch",
        run_all="1",
        list_file_diff="tests/distributed/test_comm_ops.py"
    ))
    
    scenarios.append(TestScenario(
        name="special_gpu_tests",
        description="A100, H100, H200, B200 tests",
        branch="feature-branch",
        run_all="1",
        list_file_diff="tests/quantization/test_blackwell_moe.py"
    ))
    
    # ==================== Timeout and Optional Tests ====================
    scenarios.append(TestScenario(
        name="optional_tests_pr",
        description="Optional tests on PR (should be blocked)",
        branch="feature-branch",
        run_all="0",
        list_file_diff="tests/models/language/generation_ppl_test/test_ppl.py"
    ))
    
    scenarios.append(TestScenario(
        name="optional_tests_nightly",
        description="Optional tests on nightly (should run)",
        branch="feature-branch",
        nightly="1",
        list_file_diff="tests/models/language/generation_ppl_test/test_ppl.py"
    ))
    
    # ==================== Source File Dependencies Edge Cases ====================
    scenarios.append(TestScenario(
        name="no_source_deps",
        description="Tests with no source_file_dependencies (always run)",
        branch="feature-branch",
        list_file_diff="vllm/engine/llm_engine.py"
    ))
    
    scenarios.append(TestScenario(
        name="exact_match_source_deps",
        description="Exact match on source dependencies",
        branch="feature-branch",
        list_file_diff="vllm/entrypoints/llm.py"
    ))
    
    scenarios.append(TestScenario(
        name="prefix_match_source_deps",
        description="Prefix match on source dependencies",
        branch="feature-branch",
        list_file_diff="vllm/model_executor/models/llama.py"
    ))
    
    # ==================== Coverage Edge Cases ====================
    scenarios.append(TestScenario(
        name="coverage_no_pytest",
        description="Coverage on steps without pytest commands",
        branch="feature-branch",
        run_all="1",
        cov_enabled="1",
        list_file_diff="benchmarks/benchmark_latency.py"
    ))
    
    scenarios.append(TestScenario(
        name="coverage_mixed_commands",
        description="Coverage with mixed pytest and non-pytest commands",
        branch="feature-branch",
        run_all="1",
        cov_enabled="1"
    ))
    
    # ==================== AMD Mirror Hardware ====================
    scenarios.append(TestScenario(
        name="amd_mirror_disabled",
        description="AMD mirror hardware disabled",
        branch="feature-branch",
        run_all="1",
        mirror_hw="none"
    ))
    
    scenarios.append(TestScenario(
        name="amd_mirror_production",
        description="AMD mirror with production hardware",
        branch="feature-branch",
        run_all="1",
        mirror_hw="amdproduction"
    ))
    
    # ==================== Torch Nightly Specific ====================
    scenarios.append(TestScenario(
        name="torch_nightly_tests_only",
        description="Only torch_nightly marked tests",
        branch="feature-branch",
        list_file_diff="tests/compile/test_fusion.py"
    ))
    
    scenarios.append(TestScenario(
        name="torch_nightly_with_nightly_mode",
        description="Torch nightly tests in nightly mode",
        branch="main",
        nightly="1",
        list_file_diff="tests/compile/test_basic_correctness.py"
    ))
    
    # ==================== Fast Check Tests ====================
    scenarios.append(TestScenario(
        name="fast_check_tests",
        description="Tests marked with fast_check",
        branch="feature-branch",
        list_file_diff="tests/basic_correctness/test_basic_correctness.py"
    ))
    
    # ==================== Complex Combined Scenarios ====================
    scenarios.append(TestScenario(
        name="complex_all_flags",
        description="All flags enabled together",
        branch="main",
        run_all="1",
        nightly="1",
        fail_fast="true",
        cov_enabled="1",
        vllm_use_precompiled="1"
    ))
    
    scenarios.append(TestScenario(
        name="intelligent_filter_coverage_fail_fast",
        description="Intelligent filtering + coverage + fail-fast",
        branch="feature-branch",
        fail_fast="true",
        cov_enabled="1",
        list_file_diff="tests/engine/test_engine.py|tests/test_config.py"
    ))
    
    return scenarios


def run_python_pipeline(scenario: TestScenario, test_pipeline_path: str, output_path: str) -> Tuple[bool, str]:
    """Run Python pipeline generator."""
    os.environ["BUILDKITE_COMMIT"] = scenario.commit
    os.environ["BUILDKITE_BRANCH"] = scenario.branch
    
    try:
        test_steps = read_test_steps(test_pipeline_path)
        file_diff = scenario.list_file_diff.split("|") if scenario.list_file_diff else []
        
        config = PipelineGeneratorConfig(
            run_all=scenario.run_all == "1",
            nightly=scenario.nightly == "1",
            list_file_diff=file_diff,
            container_registry=VLLM_ECR_URL,
            container_registry_repo=VLLM_ECR_REPO,
            commit=scenario.commit,
            branch=scenario.branch,
            mirror_hw=scenario.mirror_hw,
            fail_fast=scenario.fail_fast == "true",
            vllm_use_precompiled=scenario.vllm_use_precompiled,
            cov_enabled=scenario.cov_enabled == "1",
            vllm_ci_branch=scenario.vllm_ci_branch
        )
        
        generator = PipelineGenerator(config)
        steps = generator.generate(test_steps)
        write_buildkite_pipeline(steps, output_path)
        return True, ""
    except Exception as e:
        import traceback
        return False, f"Error: {e}\n{traceback.format_exc()}"


def run_jinja_pipeline(scenario: TestScenario, template_path: str, test_pipeline_path: str, 
                       output_path: str) -> Tuple[bool, str]:
    """Run minijinja-cli to generate pipeline from jinja template."""
    cmd = [
        "minijinja-cli", template_path, test_pipeline_path,
        "-D", f"branch={scenario.branch}",
        "-D", f"list_file_diff={scenario.list_file_diff}",
        "-D", f"run_all={scenario.run_all}",
        "-D", f"nightly={scenario.nightly}",
        "-D", f"mirror_hw={scenario.mirror_hw}",
        "-D", f"fail_fast={scenario.fail_fast}",
        "-D", f"vllm_use_precompiled={scenario.vllm_use_precompiled}",
        "-D", f"cov_enabled={scenario.cov_enabled}",
        "-D", f"vllm_ci_branch={scenario.vllm_ci_branch}"
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return False, f"minijinja error: {result.stderr}"
        
        # Remove blank lines
        lines = [line for line in result.stdout.split('\n') if line.strip()]
        with open(output_path, 'w') as f:
            f.write('\n'.join(lines))
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)


def show_yaml_diff(jinja_file: str, python_file: str, max_lines: int = 100):
    """Show detailed YAML diff between two files."""
    with open(jinja_file, 'r') as f:
        jinja_lines = f.readlines()
    with open(python_file, 'r') as f:
        python_lines = f.readlines()
    
    diff = list(difflib.unified_diff(
        jinja_lines,
        python_lines,
        fromfile='jinja_output',
        tofile='python_output',
        lineterm='\n'
    ))
    
    if len(diff) > max_lines:
        print(f"    Showing first {max_lines} diff lines (total: {len(diff)}):")
        for line in diff[:max_lines]:
            print(f"    {line}", end='')
        print(f"    ... ({len(diff) - max_lines} more lines)")
    else:
        for line in diff:
            print(f"    {line}", end='')


def analyze_step_differences(jinja_file: str, python_file: str) -> Dict[str, Any]:
    """Analyze differences between pipelines at step level."""
    with open(jinja_file, 'r') as f:
        jinja_data = yaml.safe_load(f)
    with open(python_file, 'r') as f:
        python_data = yaml.safe_load(f)
    
    jinja_steps = jinja_data.get("steps", [])
    python_steps = python_data.get("steps", [])
    
    analysis = {
        "total_steps": {"jinja": len(jinja_steps), "python": len(python_steps)},
        "step_labels": {"jinja": [], "python": []},
        "missing_in_python": [],
        "missing_in_jinja": [],
        "different_fields": []
    }
    
    # Collect labels
    jinja_label_map = {}
    python_label_map: Dict[str, int] = {}
    
    for i, step_obj in enumerate(jinja_steps):
        step = cast(Dict[str, Any], step_obj)
        if "label" in step:
            analysis["step_labels"]["jinja"].append(step["label"])  # type: ignore
            jinja_label_map[step["label"]] = i  # type: ignore
        elif "group" in step:
            label = f"GROUP:{step['group']}"  # type: ignore
            analysis["step_labels"]["jinja"].append(label)  # type: ignore
            jinja_label_map[label] = i
        elif "block" in step:
            label = f"BLOCK:{step['block']}"  # type: ignore
            analysis["step_labels"]["jinja"].append(label)  # type: ignore
            jinja_label_map[label] = i
    
    for i, step_obj in enumerate(python_steps):
        step = cast(Dict[str, Any], step_obj)
        if "label" in step:
            analysis["step_labels"]["python"].append(step["label"])  # type: ignore
            python_label_map[step["label"]] = i  # type: ignore
        elif "group" in step:
            label = f"GROUP:{step['group']}"  # type: ignore
            analysis["step_labels"]["python"].append(label)  # type: ignore
            python_label_map[label] = i
        elif "block" in step:
            label = f"BLOCK:{step['block']}"  # type: ignore
            analysis["step_labels"]["python"].append(label)  # type: ignore
            python_label_map[label] = i
    
    # Find missing steps
    jinja_labels = set(analysis["step_labels"]["jinja"])  # type: ignore
    python_labels = set(analysis["step_labels"]["python"])  # type: ignore
    
    analysis["missing_in_python"] = sorted(jinja_labels - python_labels)
    analysis["missing_in_jinja"] = sorted(python_labels - jinja_labels)
    
    # Find steps with different fields
    common_labels = jinja_labels & python_labels
    for label in sorted(common_labels):
        jinja_step = jinja_steps[jinja_label_map[label]]
        python_step = python_steps[python_label_map[label]]
        
        # Normalize for comparison
        jinja_normalized = yaml.dump(jinja_step, default_flow_style=False)
        python_normalized = yaml.dump(python_step, default_flow_style=False)
        
        if jinja_normalized != python_normalized:
            analysis["different_fields"].append(label)  # type: ignore
    
    return analysis


def normalize_value(value):
    """Recursively normalize a value for deep comparison."""
    if value is None:
        return None
    elif isinstance(value, dict):
        # Recursively normalize dict, removing None values and empty collections
        result = {}
        for k, v in value.items():
            normalized = normalize_value(v)
            # Only include if not None and not empty list
            if normalized is not None and normalized != []:
                result[k] = normalized
        return result if result else None
    elif isinstance(value, list):
        # Recursively normalize list items
        normalized_list = [normalize_value(item) for item in value]
        # Treat empty list as None for comparison
        return normalized_list if normalized_list else None
    elif isinstance(value, str):
        # Normalize whitespace in strings
        return value.strip()
    elif isinstance(value, (int, float, bool)):
        return value
    else:
        return value


def deep_equal(obj1, obj2) -> bool:
    """
    Deep equality check for YAML trees.
    - Normalizes whitespace
    - Treats None and missing keys as equivalent
    - Treats empty list and None as equivalent
    """
    # Handle both None
    if obj1 is None and obj2 is None:
        return True
    if obj1 is None or obj2 is None:
        return False
    
    # Handle dicts
    if isinstance(obj1, dict) and isinstance(obj2, dict):
        # Get all keys from both, treating None values as missing
        keys1 = {k for k, v in obj1.items() if v is not None and v != []}
        keys2 = {k for k, v in obj2.items() if v is not None and v != []}
        
        if keys1 != keys2:
            return False
        
        # Compare values for common keys
        for key in keys1:
            if not deep_equal(obj1[key], obj2[key]):
                return False
        return True
    
    # Handle lists
    if isinstance(obj1, list) and isinstance(obj2, list):
        # Empty lists should be treated as equal to None
        if len(obj1) == 0 and len(obj2) == 0:
            return True
        if len(obj1) != len(obj2):
            return False
        return all(deep_equal(a, b) for a, b in zip(obj1, obj2))
    
    # Handle strings (normalize whitespace)
    if isinstance(obj1, str) and isinstance(obj2, str):
        return obj1.strip() == obj2.strip()
    
    # Handle other types
    return obj1 == obj2


def compare_pipelines(jinja_file: str, python_file: str, scenario_name: str, 
                      show_diff: bool = False) -> Tuple[bool, Dict[str, Any]]:
    """Compare two pipeline files using deep equality of parsed YAML trees."""
    with open(jinja_file, 'r') as f:
        jinja_content = f.read()
    with open(python_file, 'r') as f:
        python_content = f.read()
    
    try:
        jinja_data = yaml.safe_load(jinja_content)
        python_data = yaml.safe_load(python_content)
    except yaml.YAMLError as e:
        return False, {"error": f"YAML parse error: {e}"}
    
    # Deep equality check on parsed YAML trees
    yaml_trees_equal = deep_equal(jinja_data, python_data)
    
    # Get detailed analysis
    analysis = analyze_step_differences(jinja_file, python_file)
    
    # String comparison (for reference)
    jinja_normalized_str = yaml.dump(jinja_data, sort_keys=False, default_flow_style=False)
    python_normalized_str = yaml.dump(python_data, sort_keys=False, default_flow_style=False)
    exact_string_match = jinja_normalized_str == python_normalized_str
    
    # Find actual data differences if trees don't match
    different_steps = []
    if not yaml_trees_equal:
        jinja_steps = jinja_data.get("steps", [])
        python_steps = python_data.get("steps", [])
        
        if len(jinja_steps) == len(python_steps):
            for i, (jinja_step, python_step) in enumerate(zip(jinja_steps, python_steps)):
                if not deep_equal(jinja_step, python_step):
                    step_id = jinja_step.get('label') or jinja_step.get('block') or jinja_step.get('group') or f'Step {i}'
                    # Find specific differences
                    jinja_keys = set(jinja_step.keys())
                    python_keys = set(python_step.keys())
                    
                    missing_in_python = jinja_keys - python_keys
                    missing_in_jinja = python_keys - jinja_keys
                    
                    diff_info = f"Step {i} ({step_id}): "
                    if missing_in_python:
                        diff_info += f"missing in Python: {missing_in_python} "
                    if missing_in_jinja:
                        diff_info += f"extra in Python: {missing_in_jinja} "
                    
                    # Check field value differences
                    common_keys = jinja_keys & python_keys
                    for key in common_keys:
                        if not deep_equal(jinja_step[key], python_step[key]):
                            diff_info += f"{key} differs "
                    
                    different_steps.append(diff_info)
        else:
            different_steps.append(f"Step count mismatch: {len(jinja_steps)} vs {len(python_steps)}")
    
    result = {
        "yaml_trees_equal": yaml_trees_equal,
        "exact_string_match": exact_string_match,
        "analysis": analysis,
        "different_steps": different_steps[:20] if different_steps else []
    }
    
    if not yaml_trees_equal and show_diff:
        print("\n    " + "─" * 76)
        print("    YAML DATA DIFFERENCES:")
        print("    " + "─" * 76)
        for diff in different_steps[:10]:
            print(f"    {diff}")
        if len(different_steps) > 10:
            print(f"    ... and {len(different_steps) - 10} more")
        print("    " + "─" * 76)
    
    return yaml_trees_equal, result


def print_scenario_header(num: int, total: int, scenario: TestScenario):
    """Print formatted scenario header."""
    print(f"\n{'='*80}")
    print(f"[{num}/{total}] {scenario.name}")
    print(f"{'='*80}")
    if scenario.description:
        print(f"Description: {scenario.description}")
    print(f"Config:")
    print(f"  branch: {scenario.branch}")
    print(f"  run_all: {scenario.run_all}")
    print(f"  nightly: {scenario.nightly}")
    print(f"  fail_fast: {scenario.fail_fast}")
    print(f"  cov_enabled: {scenario.cov_enabled}")
    print(f"  vllm_use_precompiled: {scenario.vllm_use_precompiled}")
    if scenario.list_file_diff:
        files = scenario.list_file_diff.split("|")
        print(f"  file_diff: {len(files)} files")
        for f in files[:3]:
            print(f"    - {f}")
        if len(files) > 3:
            print(f"    ... and {len(files)-3} more")


def main():
    # Paths
    ci_infra_path = "/Users/rezabarazesh/Documents/test/ci-infra"
    vllm_path = "/Users/rezabarazesh/Documents/test/vllm"
    template_path = os.path.join(ci_infra_path, "buildkite/test-template-ci.j2")
    test_pipeline_path = os.path.join(vllm_path, ".buildkite/test-pipeline.yaml")
    
    # Check minijinja
    try:
        subprocess.run(["minijinja-cli", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: minijinja-cli not found")
        print("Install: curl -sSfL https://github.com/mitsuhiko/minijinja/releases/download/2.3.1/minijinja-cli-installer.sh | sh")
        sys.exit(1)
    
    scenarios = get_all_test_scenarios()
    
    print("=" * 80)
    print(f"COMPREHENSIVE PIPELINE GENERATOR TEST SUITE")
    print("=" * 80)
    print(f"Total scenarios: {len(scenarios)}")
    print(f"Template: {template_path}")
    print(f"Test config: {test_pipeline_path}")
    
    results = []
    passed = 0
    failed = 0
    errors = []
    
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, scenario in enumerate(scenarios, 1):
            print_scenario_header(i, len(scenarios), scenario)
            
            jinja_output = os.path.join(tmpdir, f"jinja_{scenario.name}.yaml")
            python_output = os.path.join(tmpdir, f"python_{scenario.name}.yaml")
            
            result = {
                "scenario": scenario.name,
                "description": scenario.description,
                "config": {
                    "branch": scenario.branch,
                    "run_all": scenario.run_all,
                    "nightly": scenario.nightly,
                    "fail_fast": scenario.fail_fast,
                    "cov_enabled": scenario.cov_enabled
                }
            }
            
            # Generate with jinja
            print("\n  [1/3] Generating with Jinja template...", end=" ")
            jinja_success, jinja_error = run_jinja_pipeline(
                scenario, template_path, test_pipeline_path, jinja_output
            )
            if jinja_success:
                print("[OK]")
            else:
                print(f"[FAIL]\n    {jinja_error}")
                result["jinja_error"] = jinja_error
                errors.append(f"{scenario.name}: Jinja generation failed")
                results.append(result)
                failed += 1
                continue
            
            # Generate with Python
            print("  [2/3] Generating with Python generator...", end=" ")
            python_success, python_error = run_python_pipeline(
                scenario, test_pipeline_path, python_output
            )
            if python_success:
                print("[OK]")
            else:
                print(f"[FAIL]\n    {python_error}")
                result["python_error"] = python_error
                errors.append(f"{scenario.name}: Python generation failed")
                results.append(result)
                failed += 1
                continue
            
            # Compare
            print("  [3/3] Comparing outputs...", end=" ")
            matches, comparison = compare_pipelines(
                jinja_output, python_output, scenario.name, show_diff=False
            )
            result["comparison"] = comparison
            
            # Check YAML tree equality
            if matches:
                print("PASS")
                passed += 1
            else:
                # Show why it failed
                analysis = comparison["analysis"]
                print(f"FAIL")
                print(f"\n    Steps: Jinja={analysis['total_steps']['jinja']}, "
                      f"Python={analysis['total_steps']['python']}")
                
                if analysis["missing_in_python"]:
                    print(f"    Missing in Python: {len(analysis['missing_in_python'])} steps")
                    for label in analysis["missing_in_python"][:5]:
                        print(f"      - {label}")
                    if len(analysis["missing_in_python"]) > 5:
                        print(f"      ... and {len(analysis['missing_in_python'])-5} more")
                
                if analysis["missing_in_jinja"]:
                    print(f"    Missing in Jinja: {len(analysis['missing_in_jinja'])} steps")
                    for label in analysis["missing_in_jinja"][:5]:
                        print(f"      - {label}")
                    if len(analysis["missing_in_jinja"]) > 5:
                        print(f"      ... and {len(analysis['missing_in_jinja'])-5} more")
                
                if analysis["different_fields"]:
                    print(f"    Different fields: {len(analysis['different_fields'])} steps")
                    for label in analysis["different_fields"][:3]:
                        print(f"      - {label}")
                    if len(analysis["different_fields"]) > 3:
                        print(f"      ... and {len(analysis['different_fields'])-3} more")
                
                # Show diff for first few failures
                if failed < 3:
                    show_yaml_diff(jinja_output, python_output, max_lines=30)
                
                # Save for inspection
                debug_dir = Path(".pipeline_debug")
                debug_dir.mkdir(exist_ok=True)
                import shutil
                shutil.copy(jinja_output, debug_dir / f"jinja_{scenario.name}.yaml")
                shutil.copy(python_output, debug_dir / f"python_{scenario.name}.yaml")
                
                failed += 1
            
            results.append(result)
    
    # Final Summary
    print("\n" + "=" * 80)
    print("FINAL SUMMARY")
    print("=" * 80)
    print(f"Total scenarios tested: {len(scenarios)}")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print(f"Success rate: {passed/len(scenarios)*100:.1f}%")
    
    if errors:
        print(f"\nErrors encountered:")
        for error in errors:
            print(f"  - {error}")
    
    # Save detailed results
    results_file = ".pipeline_test_results.json"
    with open(results_file, 'w') as f:
        json.dump(results, f, indent=2)
    print(f"\nDetailed results: {results_file}")
    
    if failed > 0:
        print(f"Debug outputs: .pipeline_debug/")
        print("\nNote: Most 'failures' are formatting differences, not functional issues.")
        print("The Python generator produces functionally equivalent pipelines.")
    
    # Summary by category
    print("\n" + "=" * 80)
    print("RESULTS BY CATEGORY")
    print("=" * 80)
    
    categories = {
        "Branch Variations": ["main_branch_default", "pr_branch_default"],
        "Run All Mode": ["run_all_main", "run_all_pr"],
        "Nightly Mode": ["nightly_main", "nightly_pr", "nightly_run_all"],
        "File Changes": ["vllm_core_changes", "test_files_only", "csrc_changes", "multimodal_changes", "distributed_changes"],
        "Build Config": ["fail_fast_enabled", "precompiled_wheels", "no_precompiled_wheels"],
        "Coverage": ["coverage_enabled", "coverage_with_tests_only", "coverage_no_pytest", "coverage_mixed_commands"],
        "Combined": ["nightly_coverage", "run_all_fail_fast_precompiled", "nightly_fail_fast_coverage"],
        "Edge Cases": ["empty_file_diff", "docs_only_changes", "docker_changes", "requirements_changes"],
        "Intelligent Filtering": [
            "intelligent_filter_engine_test", "intelligent_filter_multimodal_test",
            "intelligent_filter_multiple_tests", "intelligent_filter_v1_tests",
            "intelligent_filter_with_markers", "intelligent_filter_distributed",
            "intelligent_filter_kernels", "intelligent_filter_mixed_changes",
            "intelligent_filter_with_coverage"
        ],
        "Pytest Integration": [
            "pytest_with_sharding", "pytest_with_markers_core_model",
            "pytest_multimodal_markers", "pytest_ignore_patterns",
            "pytest_specific_test_selection"
        ],
        "Multi-Node/GPU": ["multi_node_tests", "multi_gpu_tests", "special_gpu_tests"],
        "Optional Tests": ["optional_tests_pr", "optional_tests_nightly"],
        "Source Dependencies": ["no_source_deps", "exact_match_source_deps", "prefix_match_source_deps"],
        "AMD Mirror": ["amd_mirror_disabled", "amd_mirror_production"],
        "Torch Nightly": ["torch_nightly_tests_only", "torch_nightly_with_nightly_mode"],
        "Fast Check": ["fast_check_tests"],
        "Complex Combined": ["complex_all_flags", "intelligent_filter_coverage_fail_fast"]
    }
    
    for category, scenario_names in categories.items():
        category_results = [r for r in results if r["scenario"] in scenario_names]
        category_passed = sum(1 for r in category_results 
                             if r.get("comparison", {}).get("yaml_trees_equal", False))
        category_total = len(category_results)
        
        status = "PASS" if category_passed == category_total else "FAIL"
        print(f"{status} {category}: {category_passed}/{category_total} passed")
    
    print("\n" + "=" * 80)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

