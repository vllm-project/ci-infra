#!/usr/bin/env python3
"""
Integration test suite for AMD pipeline mode.
Compares Python generator output against test-template-amd.j2
"""

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List

import yaml

from buildkite.pipeline_generator.pipeline_config import PipelineGeneratorConfig
from buildkite.pipeline_generator.pipeline_generator import (
    PipelineGenerator,
    read_test_steps,
    write_buildkite_pipeline,
)
from buildkite.pipeline_generator.utils import VLLM_ECR_REPO, VLLM_ECR_URL, PipelineMode

# Add parent directories to path for imports
current_dir = os.path.dirname(os.path.abspath(__file__))
pipeline_gen_dir = os.path.dirname(current_dir)
buildkite_dir = os.path.dirname(pipeline_gen_dir)
ci_infra_dir = os.path.dirname(buildkite_dir)
sys.path.insert(0, ci_infra_dir)


@dataclass
class Scenario:
    """Test scenario for AMD mode."""

    name: str
    mirror_hw: str = "amdproduction"
    commit: str = "0" * 40
    description: str = ""


def get_amd_scenarios() -> List[Scenario]:
    """Get test scenarios for AMD mode."""
    return [
        Scenario(name="amd_default", description="Default AMD configuration"),
        Scenario(
            name="amd_production",
            mirror_hw="amdproduction",
            description="AMD with production hardware",
        ),
        Scenario(
            name="amd_experimental",
            mirror_hw="amdexperimental",
            description="AMD with experimental hardware",
        ),
    ]


def run_jinja_amd(scenario: Scenario, template_path: str, test_pipeline_path: str, output_path: str) -> tuple:
    """Run Jinja template to generate AMD pipeline."""
    try:
        cmd = [
            "minijinja-cli",
            template_path,
            test_pipeline_path,
            "-D",
            f"mirror_hw={scenario.mirror_hw}",
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, check=True)

        # Remove blank lines like bootstrap.sh does
        lines = [line for line in result.stdout.split("\n") if line.strip()]
        output = "\n".join(lines) + "\n"

        with open(output_path, "w") as f:
            f.write(output)

        return True, None
    except Exception as e:
        return False, str(e)


def run_python_amd(scenario: Scenario, test_pipeline_path: str, output_path: str) -> tuple:
    """Run Python generator to generate AMD pipeline."""
    try:
        test_steps = read_test_steps(test_pipeline_path)

        config = PipelineGeneratorConfig(
            run_all=False,
            nightly=False,
            list_file_diff=[],
            container_registry=VLLM_ECR_URL,
            container_registry_repo=VLLM_ECR_REPO,
            commit=scenario.commit,
            branch="main",  # AMD pipeline doesn't vary by branch
            mirror_hw=scenario.mirror_hw,
            fail_fast=False,
            vllm_use_precompiled="0",
            cov_enabled=False,
            vllm_ci_branch="main",
            pipeline_mode=PipelineMode.AMD,
        )

        generator = PipelineGenerator(config)
        steps = generator.generate(test_steps)
        write_buildkite_pipeline(steps, output_path)

        return True, None
    except Exception as e:
        return False, str(e)


def compare_yaml_trees(jinja_path: str, python_path: str) -> Dict[str, Any]:
    """Compare two YAML files for structural equality."""
    with open(jinja_path, "r") as f:
        jinja_data = yaml.safe_load(f)
    with open(python_path, "r") as f:
        python_data = yaml.safe_load(f)

    matches = jinja_data == python_data

    return {"yaml_trees_equal": matches, "jinja_data": jinja_data, "python_data": python_data}


def main():
    # Paths
    ci_infra_path = "/Users/rezabarazesh/Documents/test/ci-infra"
    vllm_path = "/Users/rezabarazesh/Documents/test/vllm"
    template_path = os.path.join(ci_infra_path, "buildkite/test-template-amd.j2")
    test_pipeline_path = os.path.join(vllm_path, ".buildkite/test-pipeline.yaml")

    # Check minijinja
    try:
        subprocess.run(["minijinja-cli", "--version"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: minijinja-cli not found")
        print("Install: curl -sSfL https://github.com/mitsuhiko/minijinja/releases/download/2.3.1/minijinja-cli-installer.sh | sh")
        sys.exit(1)

    scenarios = get_amd_scenarios()

    print("=" * 80)
    print("AMD PIPELINE INTEGRATION TEST")
    print("=" * 80)
    print(f"Total scenarios: {len(scenarios)}")
    print(f"Template: {template_path}")
    print(f"Test config: {test_pipeline_path}")
    print()

    passed = 0
    failed = 0
    results = []

    for i, scenario in enumerate(scenarios, 1):
        print("=" * 80)
        print(f"[{i}/{len(scenarios)}] {scenario.name}")
        print("=" * 80)
        print(f"Description: {scenario.description}")
        print("Config:")
        print(f"  mirror_hw: {scenario.mirror_hw}")
        print()

        with tempfile.TemporaryDirectory() as temp_dir:
            jinja_output = os.path.join(temp_dir, "jinja.yaml")
            python_output = os.path.join(temp_dir, "python.yaml")

            # Generate with jinja
            print("  [1/3] Generating with Jinja template...", end=" ")
            jinja_success, jinja_error = run_jinja_amd(scenario, template_path, test_pipeline_path, jinja_output)
            if jinja_success:
                print("[OK]")
            else:
                print(f"[FAIL]\n    {jinja_error}")
                failed += 1
                continue

            # Generate with Python
            print("  [2/3] Generating with Python generator...", end=" ")
            python_success, python_error = run_python_amd(scenario, test_pipeline_path, python_output)
            if python_success:
                print("[OK]")
            else:
                print(f"[FAIL]\n    {python_error}")
                failed += 1
                continue

            # Compare
            print("  [3/3] Comparing outputs...", end=" ")
            comparison = compare_yaml_trees(jinja_output, python_output)

            if comparison["yaml_trees_equal"]:
                print("PASS")
                passed += 1
                results.append({"scenario": scenario.name, "status": "PASS"})
            else:
                print("FAIL")
                print("\n    YAML trees are not equal!")
                failed += 1
                results.append({"scenario": scenario.name, "status": "FAIL"})

    # Final Summary
    print("\n" + "=" * 80)
    print("FINAL SUMMARY")
    print("=" * 80)
    print(f"Total scenarios tested: {len(scenarios)}")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")
    print(f"Success rate: {passed / len(scenarios) * 100:.1f}%")
    print("=" * 80)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
