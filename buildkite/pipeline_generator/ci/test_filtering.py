"""Test selection and filtering logic."""

from typing import List

from ..data_models.test_step import TestStep
from ..pipeline_config import PipelineGeneratorConfig


def should_run_step(test_step: TestStep, config: PipelineGeneratorConfig) -> bool:
    """Determine if a step should run based on configuration and file changes."""
    # Always run if run_all or nightly is enabled
    if config.run_all or config.nightly:
        return True

    # Check source file dependencies
    if test_step.source_file_dependencies:
        for source_file in test_step.source_file_dependencies:
            for changed_file in config.list_file_diff:
                if source_file in changed_file:
                    return True
        return False

    # If no dependencies specified, always run
    return True


def get_changed_tests(file_diff: List[str]) -> List[str]:
    """
    Extract changed test files from file diff.
    Returns list of test paths relative to tests/ directory.
    """
    changed_tests = []
    for file in file_diff:
        if file.startswith("tests/") and "/test_" in file and file.endswith(".py"):
            # Remove tests/ prefix
            changed_tests.append(file[6:])
    return changed_tests


def are_only_tests_changed(file_diff: List[str]) -> bool:
    """Check if only test files have changed."""
    if not file_diff:
        return False

    for file in file_diff:
        if not (file.startswith("tests/") and "/test_" in file and file.endswith(".py")):
            return False

    return True


def get_intelligent_test_targets(test_step: TestStep, changed_tests: List[str]) -> List[str]:
    """
    Get specific test targets when only test files changed.
    Returns list of test paths to run, or empty list if no matches.
    """
    if not test_step.source_file_dependencies:
        return []

    matched_targets = []

    for dep in test_step.source_file_dependencies:
        if not dep.startswith("tests/"):
            continue

        # Remove tests/ prefix
        dep_rel = dep[6:]

        # Handle deps that end with '/' (directories)
        if dep_rel.endswith("/"):
            dep_dir_prefix = dep_rel
            dep_file_name = dep_rel[:-1] + ".py"
        else:
            dep_dir_prefix = dep_rel + "/"
            dep_file_name = dep_rel + ".py"

        # Check changed tests
        for t in changed_tests:
            # Check if t starts with dep_dir_prefix (for directories) or equals
            # dep_file_name (for files)
            if t.startswith(dep_dir_prefix) or t == dep_file_name:
                matched_targets.append(t)

    # Filter matched targets to only include those covered by step commands
    covered_paths = extract_covered_test_paths(test_step.commands or [])
    filtered_targets = []

    for target in matched_targets:
        # Check if target is covered by any of the command paths
        is_covered = any(
            target.startswith(covered_path) and (len(target) == len(covered_path) or target[len(covered_path)] == "/") for covered_path in covered_paths
        )
        if is_covered:
            filtered_targets.append(target)

    return filtered_targets


def extract_covered_test_paths(commands) -> List[str]:
    """Extract test paths that are covered by pytest commands."""
    covered_paths: List[str] = []

    if not commands:
        return covered_paths

    for cmd in commands:
        if "pytest " not in cmd:
            continue

        # Parse pytest arguments after the pytest command
        cmd_parts = cmd.split(" ")
        in_pytest = False

        for part in cmd_parts:
            if part == "pytest":
                in_pytest = True
                continue

            # Skip if not in pytest args, starts with -, or is a specific test
            if not in_pytest or part.startswith("-") or "/" not in part or "::" in part:
                continue

            # Add the test path
            covered_paths.append(part)

            # If it's a file, also add its parent directory
            if part.endswith(".py"):
                path_parts = part.split("/")
                if len(path_parts) > 2:
                    dir_path = "/".join(path_parts[:-1])
                    covered_paths.append(dir_path)

    return covered_paths


def extract_pytest_markers(commands) -> str:
    """Extract pytest markers from commands."""
    if not commands:
        return ""

    for cmd in commands:
        if "pytest " not in cmd or " -m " not in cmd:
            continue

        # Extract the -m marker argument
        parts = cmd.split(" -m ")
        if len(parts) <= 1:
            continue

        after_m = parts[1]

        # Handle different quote styles
        if after_m.startswith("'"):
            marker = after_m[1:].split("'")[0]
            return f" -m '{marker}'"
        elif after_m.startswith('"'):
            marker = after_m[1:].split('"')[0]
            return f' -m "{marker}"'
        else:
            # No quotes, take first word
            marker = after_m.split(" ")[0]
            return f" -m {marker}"

    return ""
