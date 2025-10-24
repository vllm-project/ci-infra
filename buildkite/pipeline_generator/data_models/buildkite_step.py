from typing import Any, Dict, List, Optional

from pydantic import BaseModel, model_validator
from typing_extensions import Self

from ..utils.constants import AgentQueue

BUILD_STEP_KEY = "build"


class BuildkiteStep(BaseModel):
    """This class represents a step in Buildkite format."""

    label: str
    agents: Dict[str, str] = {"queue": AgentQueue.CPU_QUEUE}
    commands: List[str]
    key: Optional[str] = None
    plugins: Optional[List[Dict]] = None
    parallelism: Optional[int] = None
    soft_fail: Optional[bool] = None
    depends_on: Optional[str] = None
    env: Optional[Dict[str, str]] = None
    retry: Optional[Dict[str, Any]] = None
    timeout_in_minutes: Optional[int] = None
    command: Optional[str] = None
    priority: Optional[int] = None

    @model_validator(mode="after")
    def validate_agent_queue(self) -> Self:
        queue = self.agents.get("queue")
        valid_queues = {getattr(AgentQueue, attr) for attr in dir(AgentQueue) if not attr.startswith("_") and isinstance(getattr(AgentQueue, attr), str)}
        if queue and queue not in valid_queues:
            raise ValueError(f"Invalid agent queue: {queue}")
        return self


class BuildkiteBlockStep(BaseModel):
    """This class represents a block step in Buildkite format."""

    block: str
    key: str
    depends_on: Optional[str] = None  # No default depends_on


def get_step_key(step_label: str) -> str:
    """
    Generate step key from label matching jinja template logic.
    Jinja: step.label | replace(" ", "-") | lower | replace("(", "") | replace(")", "") | replace("%", "") | replace(",", "-") | replace("+", "-")
    """
    step_key = step_label.lower()
    step_key = step_key.replace(" ", "-")
    step_key = step_key.replace("(", "")
    step_key = step_key.replace(")", "")
    step_key = step_key.replace("%", "")
    step_key = step_key.replace(",", "-")
    step_key = step_key.replace("+", "-")
    return step_key


def get_block_step(step_label: str) -> BuildkiteBlockStep:
    return BuildkiteBlockStep(block=f"Run {step_label}", key=f"block-{get_step_key(step_label)}")
