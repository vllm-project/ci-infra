"""Main pipeline generator - simplified entry point."""

import os
from typing import Any, Dict, List

import click
import yaml

from .config import VLLM_ECR_REPO, VLLM_ECR_URL, PipelineGeneratorConfig, PipelineMode
from .models import TestStep
from .modes.amd import generate_amd_pipeline
from .modes.ci import generate_ci_pipeline
from .modes.fastcheck import generate_fastcheck_pipeline


class PipelineGenerator:
    """Compatibility wrapper for old PipelineGenerator interface."""
    
    def __init__(self, config: PipelineGeneratorConfig):
        config.validate()
        self.config = config
    
    def generate(self, test_steps: List[TestStep]) -> List[Dict[str, Any]]:
        """Generate pipeline based on mode."""
        if self.config.pipeline_mode == PipelineMode.AMD:
            return generate_amd_pipeline(test_steps, self.config)
        elif self.config.pipeline_mode == PipelineMode.FASTCHECK:
            return generate_fastcheck_pipeline(test_steps, self.config)
        else:
            return generate_ci_pipeline(test_steps, self.config)


def read_test_steps(file_path: str) -> List[TestStep]:
    """Read test steps from test pipeline yaml."""
    with open(file_path, "r") as f:
        content = yaml.safe_load(f)
    return [TestStep(**step) for step in content["steps"]]


def write_pipeline(steps: List[Dict[str, Any]], file_path: str) -> None:
    """Write pipeline steps to yaml file."""
    pipeline = {"steps": steps}
    with open(file_path, "w") as f:
        yaml.dump(pipeline, f, sort_keys=False, default_flow_style=False, allow_unicode=True)


# Alias for backward compatibility
write_buildkite_pipeline = write_pipeline


@click.command()
@click.option("--test_path", type=str, default=".buildkite/test-pipeline.yaml", help="Path to test pipeline yaml")
@click.option("--run_all", type=str, default="0", help="Run all tests")
@click.option("--nightly", type=str, default="0", help="Run nightly tests")
@click.option("--list_file_diff", type=str, default="", help="List of changed files (pipe-separated)")
@click.option("--mirror_hw", type=str, default="amdexperimental", help="Mirror hardware")
@click.option("--fail_fast", type=str, default="false", help="Enable fail fast mode")
@click.option("--vllm_use_precompiled", type=str, default="0", help="Use precompiled wheels")
@click.option("--cov_enabled", type=str, default="0", help="Enable coverage")
@click.option("--vllm_ci_branch", type=str, default="main", help="CI branch")
@click.option("--pipeline_mode", type=str, default="ci", help="Pipeline mode: ci, fastcheck, or amd")
@click.option("--output", type=str, default=".buildkite/pipeline.yaml", help="Output path")
def main(
    test_path: str,
    run_all: str,
    nightly: str,
    list_file_diff: str,
    mirror_hw: str,
    fail_fast: str,
    vllm_use_precompiled: str,
    cov_enabled: str,
    vllm_ci_branch: str,
    pipeline_mode: str,
    output: str,
):
    """Generate Buildkite pipeline from test configuration."""
    test_steps = read_test_steps(test_path)
    
    # Get environment variables
    commit = os.getenv("BUILDKITE_COMMIT", "0" * 40)
    branch = os.getenv("BUILDKITE_BRANCH", "main")
    
    # Parse file diff
    file_diff = list_file_diff.split("|") if list_file_diff else []
    
    # Parse pipeline mode
    if pipeline_mode == "fastcheck":
        mode = PipelineMode.FASTCHECK
    elif pipeline_mode == "amd":
        mode = PipelineMode.AMD
    else:
        mode = PipelineMode.CI
    
    # Create config
    config = PipelineGeneratorConfig(
        run_all=run_all == "1",
        nightly=nightly == "1",
        list_file_diff=file_diff,
        container_registry=VLLM_ECR_URL,
        container_registry_repo=VLLM_ECR_REPO,
        commit=commit,
        branch=branch,
        mirror_hw=mirror_hw,
        fail_fast=fail_fast == "true",
        vllm_use_precompiled=vllm_use_precompiled,
        cov_enabled=cov_enabled == "1",
        vllm_ci_branch=vllm_ci_branch,
        pipeline_mode=mode,
    )
    
    config.validate()
    
    # Generate pipeline based on mode
    if mode == PipelineMode.AMD:
        steps = generate_amd_pipeline(test_steps, config)
    elif mode == PipelineMode.FASTCHECK:
        steps = generate_fastcheck_pipeline(test_steps, config)
    else:
        steps = generate_ci_pipeline(test_steps, config)
    
    # Write pipeline
    write_pipeline(steps, output)
    print(f"Pipeline generated at {output}")


if __name__ == "__main__":
    main()
