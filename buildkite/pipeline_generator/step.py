from pydantic import BaseModel, Field, root_validator, model_validator
from typing import List, Dict, Any, Optional
from typing_extensions import Self

from .utils import AgentQueue, GPUType

BUILD_STEP_KEY = "build"
DEFAULT_TEST_WORKING_DIR = "/vllm-workspace/tests"

class TestStep(BaseModel):
    """This class represents a test step defined in the test configuration file."""
    label: str
    working_dir: Optional[str] = DEFAULT_TEST_WORKING_DIR
    optional: Optional[bool] = False
    fast_check: Optional[bool] = None
    mirror_hardwares: Optional[List[str]] = None
    no_gpu: Optional[bool] = None
    gpu: Optional[GPUType] = None
    num_gpus: Optional[int] = None
    num_nodes: Optional[int] = None
    source_file_dependencies: Optional[List[str]] = None
    soft_fail: Optional[bool] = None
    parallelism: Optional[int] = None
    command: Optional[str] = None
    commands: Optional[List[str]] = None

    @model_validator(mode="before")
    @classmethod
    def validate_and_convert_command(cls, values) -> Any:
        """
        Validate that either 'command' or 'commands' is defined.
        If 'command' is defined, convert it to 'commands'.
        """
        if not values.get("command") and not values.get("commands"):
            raise ValueError("Either 'command' or 'commands' must be defined.")
        if values.get("command") and values.get("commands"):
            raise ValueError("Only one of 'command' or 'commands' can be defined.")
        if values.get("command"):
            values["commands"] = [values["command"]]
            del values["command"]
        return values

    @model_validator(mode="after")
    def validate_gpu(self) -> Self:
        if self.gpu and self.no_gpu:
            raise ValueError("Both 'gpu' and 'no_gpu' cannot be defined together.")
        return self
    
    @model_validator(mode="after")
    def validate_multi_node(self) -> Self:
        if self.num_nodes and not self.num_gpus:
            raise ValueError("'num_gpus' must be defined if 'num_nodes' is defined.")
        if self.num_nodes and len(self.commands) != self.num_nodes:
            raise ValueError("Number of commands must match the number of nodes.")
        return self


class BuildkiteStep(BaseModel):
    """This class represents a step in Buildkite format."""
    label: str
    agents: Dict[str, str] = {"queue": AgentQueue.AWS_CPU.value}
    commands: List[str]
    key: Optional[str] = None
    plugins: Optional[List[Dict]] = None
    parallelism: Optional[int] = None
    soft_fail: Optional[bool] = None
    depends_on: Optional[str] = "build"
    env: Optional[Dict[str, str]] = None
    retry: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_agent_queue(self) -> Self:
        queue = self.agents.get("queue")
        if not AgentQueue(queue):
            raise ValueError(f"Invalid agent queue: {queue}")


class BuildkiteBlockStep(BaseModel):
    """This class represents a block step in Buildkite format."""
    block: str
    key: str
    depends_on: Optional[str] = BUILD_STEP_KEY


def get_step_key(step_label: str) -> str:
    step_key = ""
    skip_chars = "()% "
    for char in step_label.lower():
        if char in ", " and step_key[-1] != "-":
            step_key += "-"
        elif char not in skip_chars:
            step_key += char

    return step_key


def get_block_step(step_label: str) -> BuildkiteBlockStep:
    return BuildkiteBlockStep(block=f"Run {step_label}", key=f"block-{get_step_key(step_label)}")
