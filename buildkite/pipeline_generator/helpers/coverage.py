"""Coverage injection for pytest commands."""

from typing import List


def inject_coverage_into_command(cmd: str, coverage_file: str) -> str:
    """Inject coverage flags into pytest commands."""
    if "pytest " in cmd:
        replacement = "pytest --cov=vllm --cov-report= --cov-append --durations=0 "
        return f"COVERAGE_FILE={coverage_file} {cmd.replace('pytest ', replacement)} || true"
    return cmd


def get_coverage_file_id(step_label: str) -> str:
    """Compute coverage file identifier for a step."""
    step_length = len(step_label)
    step_first = step_label[0] if step_label else "x"
    return f".coverage.{step_length}_{step_first}"


def inject_coverage(commands: List[str], step_label: str, vllm_ci_branch: str) -> str:
    """
    Inject coverage into commands and return combined command string.
    """
    coverage_file = get_coverage_file_id(step_label)
    injected_commands = [inject_coverage_into_command(cmd, coverage_file) for cmd in commands]
    
    # Check if any pytest commands were found
    has_pytest = any("pytest " in cmd for cmd in commands)
    result = " && ".join(injected_commands)
    
    if has_pytest:
        upload_script = (
            f" && curl -sSL https://raw.githubusercontent.com/vllm-project/ci-infra/{vllm_ci_branch}"
            f'/buildkite/scripts/upload_codecov.sh | bash -s -- "{step_label}"'
        )
        result += upload_script
    
    return result

