"""Main pipeline generator orchestration."""

import os
from typing import Any, Dict, List, Union

import click
import yaml

from .ci.amd_tests import generate_amd_group
from .ci.ci_pipeline import generate_ci_pipeline
from .data_models.buildkite_step import BuildkiteBlockStep, BuildkiteStep
from .data_models.test_step import TestStep
from .fastcheck.fastcheck_pipeline import generate_fastcheck_pipeline
from .pipeline_config import PipelineGeneratorConfig
from .utils.constants import VLLM_ECR_REPO, VLLM_ECR_URL, PipelineMode


class PipelineGenerator:
    """Main pipeline generator - orchestrates mode-specific generation."""

    def __init__(self, config: PipelineGeneratorConfig):
        config.validate()
        self.config = config

    def generate(
        self, test_steps: List[TestStep]
    ) -> List[Union[BuildkiteStep, BuildkiteBlockStep, Dict[str, Any]]]:
        """Generate the complete pipeline - delegates to mode-specific generators."""

        # AMD mode: Only AMD group
        if self.config.pipeline_mode == PipelineMode.AMD:
            return [generate_amd_group(test_steps, self.config)]

        # CI mode
        if self.config.pipeline_mode == PipelineMode.CI:
            return generate_ci_pipeline(test_steps, self.config)

        # Fastcheck mode
        return generate_fastcheck_pipeline(test_steps, self.config)


def read_test_steps(file_path: str) -> List[TestStep]:
    """Read test steps from test pipeline yaml and parse them into TestStep objects."""
    with open(file_path, "r") as f:
        content = yaml.safe_load(f)
    return [TestStep(**step) for step in content["steps"]]


def write_buildkite_pipeline(
    steps: List[Union[BuildkiteStep, BuildkiteBlockStep, Dict[str, Any]]], file_path: str
) -> None:
    """Write the buildkite steps to the Buildkite pipeline yaml file."""
    # Convert steps to dicts, handling both objects and plain dicts
    steps_dicts = []
    for step in steps:
        if isinstance(step, (BuildkiteStep, BuildkiteBlockStep)):
            step_dict = step.model_dump(exclude_none=True)
            # Remove empty commands list (matches Jinja behavior)
            if "commands" in step_dict and step_dict["commands"] == []:
                del step_dict["commands"]
            steps_dicts.append(step_dict)
        else:
            steps_dicts.append(step)

    pipeline = {"steps": steps_dicts}
    with open(file_path, "w") as f:
        yaml.dump(pipeline, f, sort_keys=False, default_flow_style=False)


@click.command()
@click.option(
    "--test_path",
    type=str,
    default=".buildkite/test-pipeline.yaml",
    help="Path to the test pipeline yaml file",
)
@click.option("--run_all", type=str, default="0",
              help="If set to 1, run all tests")
@click.option("--nightly", type=str, default="0",
              help="If set to 1, run nightly tests")
@click.option(
    "--list_file_diff",
    type=str,
    default="",
    help="List of files in the diff between current branch and main (pipe-separated)",
)
@click.option("--mirror_hw",
              type=str,
              default="amdexperimental",
              help="Mirror hardware to use")
@click.option("--fail_fast", type=str, default="false",
              help="Enable fail fast mode")
@click.option("--vllm_use_precompiled", type=str,
              default="0", help="Use precompiled wheels")
@click.option("--cov_enabled", type=str, default="0", help="Enable coverage")
@click.option("--vllm_ci_branch", type=str,
              default="main", help="CI branch to use")
@click.option("--pipeline_mode", type=str, default="ci",
              help="Pipeline mode: ci, fastcheck, or amd")
@click.option(
    "--output",
    type=str,
    default=".buildkite/pipeline.yaml",
    help="Output path for generated pipeline",
)
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

    # Parse list_file_diff
    file_diff = list_file_diff.split("|") if list_file_diff else []

    # Parse pipeline mode
    mode = PipelineMode.CI
    if pipeline_mode == "fastcheck":
        mode = PipelineMode.FASTCHECK
    elif pipeline_mode == "amd":
        mode = PipelineMode.AMD

    pipeline_generator_config = PipelineGeneratorConfig(
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

    generator = PipelineGenerator(pipeline_generator_config)
    steps = generator.generate(test_steps)

    write_buildkite_pipeline(steps, output)
    print(f"Pipeline generated at {output}")


if __name__ == "__main__":
    main()
