import click
import os
import re
from typing import List, Optional, Union
import yaml
from pydantic import BaseModel, field_validator

from .step import BuildkiteStep, BuildkiteBlockStep, TestStep
from .utils import VLLM_ECR_URL, VLLM_ECR_REPO, AgentQueue
from .pipeline_generator_helper import get_build_commands

class PipelineGeneratorConfig:
    def __init__(
        self,
        container_registry: str,
        container_registry_repo: str,
        commit: str,
        list_file_diff: List[str],
        run_all: bool = False,
    ):
        self.run_all = run_all
        self.list_file_diff = list_file_diff
        self.container_registry = container_registry
        self.container_registry_repo = container_registry_repo
        self.commit = commit

    @property
    def container_image(self):
        return f"{self.container_registry}/{self.container_registry_repo}:{self.commit}"
    
    def validate(self):
        """Validate the configuration."""
        # Check if commit is a valid Git commit hash
        pattern = r"^[0-9a-f]{40}$"
        if not re.match(pattern, self.commit):
            raise ValueError(f"Commit {self.commit} is not a valid Git commit hash")


class PipelineGenerator:
    def __init__(
            self, 
            config: PipelineGeneratorConfig
        ):
        config.validate()
        self.config = config

    def generate_build_step(self) -> BuildkiteStep:
        """Build the Docker image and push it to container registry."""
        build_commands = get_build_commands(self.config.container_registry, self.config.commit, self.config.container_image)

        return BuildkiteStep(
            label=":docker: build image",
            key="build",
            agents={"queue": AgentQueue.AWS_CPU.value},
            env={"DOCKER_BUILDKIT": "1"},
            retry={
                "automatic": [
                    {"exit_status": -1, "limit": 2},
                    {"exit_status": -10, "limit": 2}
                ]
            },
            commands=build_commands,
            depends_on=None,
        )

def read_test_steps(file_path: str) -> List[TestStep]:
    """Read test steps from test pipeline yaml and parse them into TestStep objects."""
    with open(file_path, "r") as f:
        content = yaml.safe_load(f)
    return [TestStep(**step) for step in content["steps"]]

def write_buildkite_steps(steps: List[Union[BuildkiteStep, BuildkiteBlockStep]], file_path: str) -> None:
    """Write the buildkite steps to the Buildkite pipeline yaml file."""
    buildkite_steps_dict = {"steps": [step.dict(exclude_none=True) for step in steps]}
    with open(file_path, "w") as f:
        yaml.dump(buildkite_steps_dict, f, sort_keys=False)

@click.command()
@click.option("--test_path", type=str, required=True, help="Path to the test pipeline yaml file")
@click.option("--run_all", type=str, help="If set to 1, run all tests")
@click.option("--list_file_diff", type=str, help="List of files in the diff between current branch and main")
def main(test_path: str, external_hardware_test_path: str, run_all: str, list_file_diff: str):
    test_steps = read_test_steps(test_path)

    pipeline_generator_config = PipelineGeneratorConfig(
        run_all=run_all == "1",
        list_file_diff=list_file_diff,
        container_registry=VLLM_ECR_URL,
        container_registry_repo=VLLM_ECR_REPO,
        commit=os.getenv("BUILDKITE_COMMIT"),
    )
