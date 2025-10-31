"""Data models for test step input parsing."""

from typing import List, Optional, Union

from pydantic import BaseModel, model_validator
from typing_extensions import Self

from .config import DEFAULT_WORKING_DIR, GPUType


class TestStep(BaseModel):
    """Test step defined in test-pipeline.yaml."""

    label: str
    working_dir: Optional[str] = DEFAULT_WORKING_DIR
    optional: Optional[bool] = False
    fast_check: Optional[bool] = None
    fast_check_only: Optional[bool] = None
    torch_nightly: Optional[bool] = None
    mirror_hardwares: Optional[List[str]] = None
    no_gpu: Optional[bool] = None
    gpu: Optional[GPUType] = None
    num_gpus: Optional[int] = None
    num_nodes: Optional[int] = None
    source_file_dependencies: Optional[List[str]] = None
    soft_fail: Optional[bool] = None
    parallelism: Optional[int] = None
    timeout_in_minutes: Optional[int] = None
    mount_buildkite_agent: Optional[bool] = None
    command: Optional[str] = None
    commands: Optional[Union[List[str], List[List[str]]]] = None

    @model_validator(mode="before")
    @classmethod
    def validate_and_convert_command(cls, values):
        """Validate that either 'command' or 'commands' is defined and convert command to commands."""
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
        if self.num_nodes and self.commands:
            if isinstance(self.commands, list) and len(self.commands) > 0:
                if isinstance(self.commands[0], list):
                    if len(self.commands) != self.num_nodes:
                        raise ValueError("Number of command lists must match the number of nodes.")
        return self

