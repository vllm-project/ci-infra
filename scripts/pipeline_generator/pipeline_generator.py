import click
import os
import re
import yaml
from typing import List, Optional

from pydantic import BaseModel, field_validator
from .step import TestStep

class PipelineGeneratorConfig:
    def __init__(
        self,
        container_registry: str,
        container_registry_repo: str,
        commit: str,
        list_file_diff: List[str],
        test_path: str,  # List of tests
        external_hardware_test_path: str,  # List of external hardware tests
        pipeline_file_path: str,  # Path to the output pipeline file
        run_all: bool = False,
    ):
        self.run_all = run_all
        self.list_file_diff = list_file_diff
        self.container_registry = container_registry
        self.container_registry_repo = container_registry_repo
        self.commit = commit
        self.test_path = test_path
        self.external_hardware_test_path = external_hardware_test_path
        self.pipeline_file_path = pipeline_file_path
    
    @property
    def container_image(self):
        return f"{self.container_registry}/{self.container_registry_repo}:{self.commit}"
    
    def validate(self):
        """Validate the configuration."""
        # Check if commit is a valid Git commit hash
        pattern = r"^[0-9a-f]{40}$"
        if not re.match(pattern, self.commit):
            raise ValueError(f"Commit {self.commit} is not a valid Git commit hash")

        # Check if test_path exists
        if not os.path.isfile(self.test_path):
            raise FileNotFoundError(f"Test file {self.test_path} not found")
        
        # Check if external_hardware_test_path exists
        if not os.path.isfile(self.external_hardware_test_path):
            raise FileNotFoundError(f"External hardware test file {self.external_hardware_test_path} not found")


class PipelineGenerator:
    def __init__(
            self, 
            config: PipelineGeneratorConfig
        ):
        config.validate()
        self.config = config

def read_test_steps(file_path: str) -> List[TestStep]:
    """Read test steps from test pipeline yaml and parse them into TestStep objects."""
    print(os.getcwd(), os.path.abspath(file_path))
    with open(file_path, "r") as f:
        content = yaml.safe_load(f)
    return [TestStep(**step) for step in content["steps"]]

@click.command()
@click.option("--test_path", type=str, required=True, help="Path to the test pipeline yaml file")
@click.option("--run_all", type=str)
@click.option("--list_file_diff", type=str)
def main(test_path: str, external_hardware_test_path: str, run_all: str, list_file_diff: str):
    test_steps = read_test_steps(test_path)

    pipeline_generator_config = PipelineGeneratorConfig(
        run_all=run_all == "1",
        list_file_diff=list_file_diff,
        container_registry=VLLM_ECR_URL,
        container_registry_repo=VLLM_ECR_REPO,
        commit=os.getenv("BUILDKITE_COMMIT"),
        pipeline_file_path=PIPELINE_FILE_PATH
    )
