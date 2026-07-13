"""Render native Buildkite pipeline YAML with change-based step filtering."""

import copy
from typing import Any, Dict, List, Mapping, Optional, Sequence

import yaml

SOURCE_DEPENDENCIES_KEY = "source_file_dependencies"


def render_pipeline_document(
    document: Mapping[str, Any], changed_files: Optional[Sequence[str]]
) -> Dict[str, Any]:
    """Filter a native Buildkite document while preserving all native fields.

    ``source_file_dependencies`` is generator metadata, so it is removed from
    every retained step. When ``changed_files`` is ``None``, filtering is
    disabled and all steps are retained as a safe fallback.
    """
    if not isinstance(document, Mapping):
        raise ValueError("pipeline document must be a mapping")

    rendered = copy.deepcopy(dict(document))
    dependencies = rendered.pop(SOURCE_DEPENDENCIES_KEY, None)
    if dependencies is not None:
        _validate_dependencies(dependencies)

    steps = rendered.get("steps")
    if isinstance(steps, list):
        rendered["steps"] = _filter_steps(steps, changed_files)

    return rendered


def render_pipeline_yaml(
    pipeline_yaml: str, changed_files: Optional[Sequence[str]]
) -> str:
    """Load, filter, and serialize a native Buildkite YAML document."""
    document = yaml.safe_load(pipeline_yaml)
    rendered = render_pipeline_document(document, changed_files)
    return yaml.safe_dump(rendered, sort_keys=False)


def _filter_steps(
    steps: List[Any], changed_files: Optional[Sequence[str]]
) -> List[Any]:
    filtered = []
    for step in steps:
        if not isinstance(step, Mapping):
            filtered.append(copy.deepcopy(step))
            continue

        rendered_step = _filter_step(step, changed_files)
        if rendered_step is not None:
            filtered.append(rendered_step)
    return filtered


def _filter_step(
    step: Mapping[str, Any], changed_files: Optional[Sequence[str]]
) -> Optional[Dict[str, Any]]:
    rendered = copy.deepcopy(dict(step))
    dependencies = rendered.pop(SOURCE_DEPENDENCIES_KEY, None)

    if dependencies is not None:
        _validate_dependencies(dependencies)
        if changed_files is not None and dependencies:
            if not _dependencies_match(dependencies, changed_files):
                return None

    child_steps = rendered.get("steps")
    if isinstance(child_steps, list):
        rendered["steps"] = _filter_steps(child_steps, changed_files)
        if "group" in rendered and child_steps and not rendered["steps"]:
            return None

    return rendered


def _validate_dependencies(dependencies: Any) -> None:
    if not isinstance(dependencies, list) or not all(
        isinstance(dependency, str) for dependency in dependencies
    ):
        raise ValueError(
            "source_file_dependencies must be a list of repository-relative paths"
        )


def _dependencies_match(
    dependencies: Sequence[str], changed_files: Sequence[str]
) -> bool:
    for dependency in dependencies:
        normalized_dependency = dependency.rstrip("/")
        if not normalized_dependency:
            continue
        for changed_file in changed_files:
            if changed_file == normalized_dependency or changed_file.startswith(
                normalized_dependency + "/"
            ):
                return True
    return False
