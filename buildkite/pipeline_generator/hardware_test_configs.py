"""Data classes for hardware-specific test configurations."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class HardwareTestConfig:
    """Configuration for a hardware-specific test."""

    label: str
    queue: str
    script_path: str
    depends_on: Optional[str] = None
    soft_fail: bool = True
    timeout_in_minutes: Optional[int] = None
    extra_commands: List[str] = field(default_factory=list)
    env: Optional[Dict[str, str]] = None
    key: Optional[str] = None

    def to_buildkite_step(self) -> Dict[str, Any]:
        """Convert to Buildkite step dictionary."""
        step: Dict[str, Any] = {
            "label": self.label,
            "agents": {"queue": self.queue},
            "soft_fail": self.soft_fail,
        }

        if self.depends_on is not None:
            step["depends_on"] = self.depends_on

        if self.timeout_in_minutes:
            step["timeout_in_minutes"] = self.timeout_in_minutes

        if self.key:
            step["key"] = self.key

        # Build command - special handling for GH200 which combines extra
        # commands
        if self.label == "GH200 Test":
            # GH200 uses: command: nvidia-smi && bash script.sh
            step["command"] = f"nvidia-smi && bash {self.script_path}"
        elif self.extra_commands:
            step["commands"] = self.extra_commands + \
                [f"bash {self.script_path}"]
        else:
            step["command"] = f"bash {self.script_path}"

        if self.env:
            step["env"] = self.env

        return step


@dataclass
class BlockStepConfig:
    """Configuration for a block step."""

    label: str
    key: str
    depends_on: Optional[str] = None

    def to_buildkite_step(self) -> Dict[str, Any]:
        """Convert to Buildkite block step dictionary."""
        step = {"block": self.label, "key": self.key}
        if self.depends_on is not None:
            step["depends_on"] = self.depends_on
        return step


@dataclass
class ConditionalTestConfig:
    """Configuration for a test that may have a block based on conditions."""

    test_config: HardwareTestConfig
    block_config: Optional[BlockStepConfig] = None

    def to_buildkite_steps(self) -> List[Dict[str, Any]]:
        """Convert to list of Buildkite steps (block + test if needed)."""
        steps = []
        if self.block_config:
            steps.append(self.block_config.to_buildkite_step())
        steps.append(self.test_config.to_buildkite_step())
        return steps


@dataclass
class NotificationStepConfig:
    """Configuration for notification/alert steps."""

    label: str
    depends_on: str  # Changed from List[str] to str to match jinja
    queue: str
    check_steps: List[str]
    notification_channel: str
    failure_message: str
    soft_fail: bool = True

    def to_buildkite_step(self) -> Dict[str, Any]:
        """Generate notification step with conditional logic matching jinja template exactly."""
        # Build condition check
        conditions = []
        for step in self.check_steps:
            conditions.append(
                f'$$(buildkite-agent step get "outcome" --step "{step}") != "passed"')
        condition_str = " || ".join(conditions)

        # IBM Power uses single bracket [ ], TPU uses double bracket [[ ]]
        if "IBM Power" in self.label:
            command = f"""if [ {condition_str} ]; then
   cat <<- YAML | buildkite-agent pipeline upload
   steps:
     - label: "Notify owners about failing test"
       soft_fail: true
       agents:
         queue: {self.queue}
       command: echo "{self.failure_message}"
       notify:
         - slack:
             channels:
               - "{self.notification_channel}"
YAML
fi  """
        else:
            # TPU format
            command = f"""if [[ {condition_str} ]]; then
   cat <<- YAML | buildkite-agent pipeline upload
   steps:
     - label: "Notify owners about failing test"
       agents:
         queue: {self.queue}
       command: echo "{self.failure_message}"
       notify:
         - slack:
             channels:
               - "{self.notification_channel}"
YAML
fi"""

        return {
            "label": self.label,
            "depends_on": self.depends_on,
            "soft_fail": self.soft_fail,
            "agents": {"queue": self.queue},
            "commands": command,
        }


# Hardware platform definitions
NEURON_CONFIG = HardwareTestConfig(
    label="Neuron Test",
    queue="neuron",
    script_path=".buildkite/scripts/hardware_ci/run-neuron-test.sh",
)

INTEL_HPU_CONFIG = HardwareTestConfig(
    label="Intel HPU Test",
    queue="intel-hpu",
    script_path=".buildkite/scripts/hardware_ci/run-hpu-test.sh",
)

INTEL_GPU_CONFIG = HardwareTestConfig(
    label="Intel GPU Test",
    queue="intel-gpu",
    script_path=".buildkite/scripts/hardware_ci/run-xpu-test.sh",
)

ASCEND_CONFIG = HardwareTestConfig(
    label="Ascend NPU Test",
    queue="ascend",
    script_path=".buildkite/scripts/hardware_ci/run-npu-test.sh",
    timeout_in_minutes=20,
)

GH200_CONFIG = HardwareTestConfig(
    label="GH200 Test",
    queue="gh200_queue",
    script_path=".buildkite/scripts/hardware_ci/run-gh200-test.sh",
    extra_commands=[],  # nvidia-smi is combined with script in command field
)


def get_intel_cpu_config(branch: str) -> ConditionalTestConfig:
    """Get Intel CPU test configuration."""
    # Always create block (matches jinja behavior)
    block = BlockStepConfig(
        label="Run Intel CPU test",
        key="block-intel-cpu",
        depends_on=None)

    test = HardwareTestConfig(
        label="Intel CPU Test",
        queue="intel-cpu",
        script_path=".buildkite/scripts/hardware_ci/run-cpu-test.sh",
        depends_on=None if branch == "main" else "block-intel-cpu",
    )

    # Always include block (matches jinja template exactly)
    return ConditionalTestConfig(test_config=test, block_config=block)


def get_ibm_power_config(branch: str) -> ConditionalTestConfig:
    """Get IBM Power test configuration."""
    if branch == "main":
        block = BlockStepConfig(
            label="Run IBM Power CPU test",
            key="block-ibm-power",
            depends_on=None)
        test = HardwareTestConfig(
            label="IBM Power(ppc64le) CPU Test",
            queue="ibm-ppc64le",
            script_path=".buildkite/scripts/hardware_ci/run-cpu-test-ppc64le.sh",
            depends_on="block-ibm-power",
            key="ibm-ppc64-test",
        )
    else:
        block = BlockStepConfig(
            label="Run IBM Power(ppc64le) CPU Test",
            key="block-ibm-ppc64-test",
            depends_on=None)
        test = HardwareTestConfig(
            label="IBM Power(ppc64le) CPU Test",
            queue="ibm-ppc64le",
            script_path=".buildkite/scripts/hardware_ci/run-cpu-test-ppc64le.sh",
            depends_on="block-ibm-ppc64-test",
        )

    return ConditionalTestConfig(test_config=test, block_config=block)


def get_ibm_s390x_config(nightly: bool) -> ConditionalTestConfig:
    """Get IBM Z (s390x) test configuration."""
    block = (
        None if nightly else BlockStepConfig(
            label='Run "IBM Z (s390x) CPU Test"',
            key="block-ibm-s390x",
            depends_on=None))

    test = HardwareTestConfig(
        label="IBM Z (s390x) CPU Test",
        queue="ibm_s390x",
        script_path=".buildkite/scripts/hardware_ci/run-cpu-test-s390x.sh",
        depends_on=None if nightly else "block-ibm-s390x",
    )

    return ConditionalTestConfig(test_config=test, block_config=block)


@dataclass
class TPUTestConfig:
    """Configuration for TPU tests."""

    label: str
    key: str
    timeout_in_minutes: int
    script_path: Optional[str] = None
    extra_docker_build: Optional[str] = None
    extra_scripts: List[str] = field(default_factory=list)

    def to_buildkite_step(self) -> Dict[str, Any]:
        """Convert to Buildkite step."""
        commands = ["bash .buildkite/scripts/tpu/cleanup_docker.sh"]

        if self.extra_docker_build:
            commands.append(self.extra_docker_build)

        if self.script_path:
            commands.append(
                f'if [[ -f "{self.script_path}" ]]; then bash {self.script_path}; fi')

        commands.extend(self.extra_scripts)

        return {
            "label": self.label,
            "soft_fail": True,
            "depends_on": None,
            "key": self.key,
            "timeout_in_minutes": self.timeout_in_minutes,
            "agents": {"queue": "tpu_v6e_queue"},
            "commands": commands,
        }


# TPU test definitions
TPU_V0_TEST = TPUTestConfig(
    label="TPU V0 Test",
    key="run-tpu-v0-test",
    timeout_in_minutes=180,
    script_path=".buildkite/scripts/hardware_ci/run-tpu-test.sh",
    extra_scripts=["yes | docker system prune -a"],
)

TPU_V1_TEST = TPUTestConfig(
    label="TPU V1 Test",
    key="run-tpu-v1-test",
    timeout_in_minutes=180,
    script_path=".buildkite/scripts/hardware_ci/run-tpu-v1-test.sh",
)

TPU_V1_TEST_PART2 = TPUTestConfig(
    label="TPU V1 Test Part2",
    key="run-tpu-v1-test-part2",
    timeout_in_minutes=90,
    script_path=".buildkite/scripts/hardware_ci/run-tpu-v1-test-part2.sh",
)

TPU_V1_BENCHMARK = TPUTestConfig(
    label="TPU V1 Benchmark Test",
    key="run-tpu-v1-benchmark-test",
    timeout_in_minutes=60,
    extra_docker_build="DOCKER_BUILDKIT=1 docker build --build-arg max_jobs=16 --build-arg USE_SCCACHE=1 --build-arg GIT_REPO_CHECK=0 --tag vllm/vllm-tpu-bm --progress plain -f docker/Dockerfile.tpu .",
    extra_scripts=[
        "bash .buildkite/scripts/tpu/docker_run_bm.sh .buildkite/scripts/tpu/config_v6e_1.env",
        "bash .buildkite/scripts/tpu/docker_run_bm.sh .buildkite/scripts/tpu/quantized_v6e_1.env",
    ],
)


def get_tpu_v0_notification_config() -> Dict[str, Any]:
    """Get TPU V0 notification configuration - returns dict directly for fastcheck."""
    # Fastcheck uses single bracket, not double bracket
    command = """if [ $$(buildkite-agent step get "outcome" --step "run-tpu-v0-test") != "passed" ]; then
   cat <<- YAML | buildkite-agent pipeline upload
   steps:
     - label: "Notify owners about failing test"
       agents:
         queue: tpu_v5_queue
       command: echo "TPU V0 Test failed"
       notify:
         - slack:
             channels:
               - "#collab-google-ci"
YAML
fi
"""

    return {
        "label": "TPU V0 Test Notification",
        "depends_on": "run-tpu-v0-test",
        "soft_fail": True,
        "agents": {"queue": "tpu_v5_queue"},
        "commands": command,
    }


def get_tpu_notification_config() -> List[str]:
    """Get TPU notification configuration for main branch - returns depends_on list."""
    # TPU uses list for depends_on (unlike IBM Power which uses string)
    return ["run-tpu-v1-test", "run-tpu-v1-test-part2"]


def get_ibm_power_notification_config() -> NotificationStepConfig:
    """Get IBM Power notification configuration for main branch."""
    return NotificationStepConfig(
        label="IBM Power(ppc64le) Build Failure Notification",
        depends_on="ibm-ppc64-test",  # String, not list (matches jinja)
        queue="ibm-ppc64le",
        check_steps=["IBM Power(ppc64le) CPU Test"],
        notification_channel="vllm#vllm-ci-on-power",
        failure_message="IBM Power(ppc64le) Build/Test failed",
    )
