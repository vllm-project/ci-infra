"""Compile the reviewed vLLM Buildkite sources into a selector catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .catalog import (
    AreaAlias,
    AreaTombstone,
    Catalog,
    CatalogValidationError,
    ExecutionKind,
    JobAlias,
    JobDefinition,
    JobTombstone,
    validate_catalog_evolution,
)
from .policy import Policy

try:
    import yaml
except ImportError:  # pragma: no cover - exercised by packaging, not logic
    yaml = None


class RepositoryCatalogError(ValueError):
    """Repository CI sources cannot produce a safe deterministic catalog."""


_PRIMARY_PIPELINE = "ci"
_NATIVE_AMD_PIPELINE = "amd-ci"
_CPU_FILE = Path(".buildkite/hardware_tests/cpu.yaml")
_AMD_PREREQUISITES_FILE = Path(".buildkite/hardware_tests/amd.yaml")
_DEFINITION_SCHEMA = "buildkite-effective-v1"
_DIRECT_EXECUTION_FIELDS = (
    "commands",
    "working_dir",
    "device",
    "agent_tags",
    "num_devices",
    "num_nodes",
    "soft_fail",
    "parallelism",
    "timeout_in_minutes",
    "mount_buildkite_agent",
    "env",
    "retry",
    "no_plugin",
    "no_gpu",
    "dind",
)


def compile_repository_catalog(repository_root: Path) -> Catalog:
    """Compile selectable jobs from one trusted vLLM default-branch checkout."""

    root = repository_root.resolve()
    policy_path = root / ".buildkite/ci_control.toml"
    policy = Policy.from_toml(_read_text(policy_path))
    required_groups = {"upstream", "cpu", "amd"}
    if not required_groups <= policy.catalog_groups:
        raise RepositoryCatalogError(
            "catalog policy must define upstream, cpu, and amd groups"
        )

    source_files = [
        *sorted((root / ".buildkite/image_build").glob("*.yaml")),
        *sorted((root / ".buildkite/test_areas").glob("*.yaml")),
        root / _CPU_FILE,
        root / _AMD_PREREQUISITES_FILE,
    ]
    jobs: list[JobDefinition] = []
    for source_path in source_files:
        jobs.extend(_compile_source(root, source_path))

    native_path = root / ".buildkite/test-amd.yaml"
    native_document = _load_yaml(native_path)
    native_steps = _step_list(native_document, native_path)
    if policy.native_amd_selection == "whole_lane":
        jobs.append(
            JobDefinition(
                job_id="native-amd-lane",
                groups=frozenset({"amd"}),
                areas=frozenset({"native-amd"}),
                pipeline=_NATIVE_AMD_PIPELINE,
                execution_profile="amd-native",
                execution_kind=ExecutionKind.OPAQUE_PIPELINE,
                shards=sum(_parallelism(step, native_path) for step in native_steps),
                definition_digest=_definition_digest(
                    _opaque_pipeline_definition(native_steps)
                ),
            )
        )

    unknown_groups = {
        group
        for job in jobs
        for group in job.groups
        if group not in policy.catalog_groups
    }
    if unknown_groups:
        raise RepositoryCatalogError(
            "compiled jobs use groups outside protected policy: "
            + ", ".join(sorted(unknown_groups))
        )

    aliases = tuple(JobAlias(alias, target) for alias, target in policy.catalog_aliases)
    tombstones = tuple(
        JobTombstone(
            job_id,
            reason="retired by protected repository policy",
        )
        for job_id in policy.catalog_tombstones
    )
    area_aliases = tuple(
        AreaAlias(alias, target) for alias, target in policy.catalog_area_aliases
    )
    area_tombstones = tuple(
        AreaTombstone(
            area_id,
            reason="retired by protected repository policy",
        )
        for area_id in policy.catalog_area_tombstones
    )
    try:
        return Catalog(
            version=f"schema-{policy.api_version}",
            jobs=tuple(jobs),
            aliases=aliases,
            tombstones=tombstones,
            area_aliases=area_aliases,
            area_tombstones=area_tombstones,
        )
    except CatalogValidationError as error:
        raise RepositoryCatalogError(str(error)) from error


def _compile_source(
    root: Path,
    source_path: Path,
) -> list[JobDefinition]:
    document = _load_yaml(source_path)
    steps = _step_list(document, source_path)
    relative = source_path.relative_to(root)
    group_dependencies = _dependencies(
        document.get("depends_on", []),
        source_path,
    )

    jobs = []
    for step in steps:
        key = step.get("key")
        if not isinstance(key, str) or not key:
            raise RepositoryCatalogError(
                f"{relative}: selectable step {step.get('label')!r} "
                "requires an explicit key"
            )
        metadata = _metadata(step, source_path)
        dependencies = _dependencies(
            step.get("depends_on") or group_dependencies,
            source_path,
        )
        groups, areas, selectable = _classify(
            relative=relative,
            key=key,
            metadata=metadata,
        )
        profile = _profile(step.get("device"))
        jobs.append(
            JobDefinition(
                job_id=key,
                groups=groups,
                areas=areas,
                dependencies=dependencies,
                pipeline=_PRIMARY_PIPELINE,
                execution_profile=profile,
                shards=_parallelism(step, source_path),
                selectable=selectable,
                definition_digest=_definition_digest(
                    _direct_execution_definition(step, dependencies)
                ),
            )
        )

        mirror = step.get("mirror")
        if mirror is None:
            continue
        if not isinstance(mirror, Mapping):
            raise RepositoryCatalogError(f"{relative}: mirror must be an object")
        amd = mirror.get("amd")
        if amd is None:
            continue
        if not isinstance(amd, Mapping):
            raise RepositoryCatalogError(f"{relative}: mirror.amd must be an object")
        mirror_dependencies = _normalize_amd_dependencies(
            _dependencies(
                amd.get("depends_on"),
                source_path,
            )
        )
        mirror_id = f"{key}-amd"
        jobs.append(
            JobDefinition(
                job_id=mirror_id,
                groups=frozenset({"amd"}),
                areas=areas,
                dependencies=mirror_dependencies,
                pipeline=_PRIMARY_PIPELINE,
                execution_profile=_profile(amd.get("device"), prefix="amd"),
                shards=_parallelism(step, source_path),
                selectable=selectable,
                definition_digest=_definition_digest(
                    _amd_execution_definition(
                        step,
                        amd,
                        mirror_dependencies,
                    )
                ),
            )
        )
    return jobs


def _classify(
    *,
    relative: Path,
    key: str,
    metadata: Mapping[str, Any],
) -> tuple[frozenset[str], frozenset[str], bool]:
    explicit_groups = metadata.get("groups")
    explicit_areas = metadata.get("areas")
    structurally_internal = relative == _AMD_PREREQUISITES_FILE or relative.parts[
        :2
    ] == (".buildkite", "image_build")
    selectable = bool(metadata.get("selectable", True)) and not structurally_internal

    if explicit_groups is not None:
        groups = frozenset(_string_sequence(explicit_groups, "ci_control.groups"))
    elif relative == _CPU_FILE:
        groups = frozenset({"cpu"})
    elif relative == _AMD_PREREQUISITES_FILE:
        groups = frozenset({"amd"})
    elif relative.parts[:2] == (".buildkite", "image_build"):
        if "amd" in key:
            groups = frozenset({"amd"})
        elif "cpu" in key or "arm" in key:
            groups = frozenset({"cpu"})
        else:
            groups = frozenset({"upstream"})
    else:
        groups = frozenset({"upstream"})

    if explicit_areas is not None:
        areas = frozenset(_string_sequence(explicit_areas, "ci_control.areas"))
    elif relative.parts[:2] == (".buildkite", "test_areas"):
        stem = relative.stem.replace("_", "-")
        values = {stem}
        if stem.startswith("model"):
            values.add("models")
        areas = frozenset(values)
    else:
        areas = frozenset({"infrastructure"})
    return groups, areas, selectable


def _metadata(
    step: Mapping[str, Any],
    source_path: Path,
) -> Mapping[str, Any]:
    value = step.get("ci_control", {})
    if not isinstance(value, Mapping):
        raise RepositoryCatalogError(f"{source_path}: ci_control must be an object")
    unknown = set(value) - {"groups", "areas", "selectable"}
    if unknown:
        raise RepositoryCatalogError(
            f"{source_path}: unknown ci_control fields: " + ", ".join(sorted(unknown))
        )
    if "selectable" in value and not isinstance(value["selectable"], bool):
        raise RepositoryCatalogError(
            f"{source_path}: ci_control.selectable must be boolean"
        )
    return value


def _load_yaml(path: Path) -> Mapping[str, Any]:
    if yaml is None:
        raise RepositoryCatalogError(
            "PyYAML is required; install the repository-validation extra"
        )
    try:
        value = yaml.safe_load(_read_text(path))
    except yaml.YAMLError as error:
        raise RepositoryCatalogError(f"{path}: invalid YAML: {error}") from error
    if not isinstance(value, Mapping):
        raise RepositoryCatalogError(f"{path}: root must be an object")
    return value


def _step_list(
    document: Mapping[str, Any],
    source_path: Path,
) -> list[Mapping[str, Any]]:
    steps = document.get("steps")
    if not isinstance(steps, list) or not all(
        isinstance(step, Mapping) for step in steps
    ):
        raise RepositoryCatalogError(
            f"{source_path}: steps must be an array of objects"
        )
    return list(steps)


def _dependencies(value: Any, source_path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    values = _string_sequence(value, f"{source_path}: depends_on")
    if len(values) != len(set(values)):
        raise RepositoryCatalogError(f"{source_path}: depends_on contains duplicates")
    return tuple(values)


def _normalize_amd_dependencies(dependencies: tuple[str, ...]) -> tuple[str, ...]:
    """Match the pipeline generator's AMD dependency normalization."""

    normalized = []
    for dependency in dependencies:
        value = "image-build-amd" if dependency == "image-build" else dependency
        if value not in normalized:
            normalized.append(value)
    if "image-build-amd" not in normalized:
        normalized.insert(0, "image-build-amd")
    return tuple(normalized)


def _string_sequence(value: Any, label: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise RepositoryCatalogError(f"{label} must be an array of non-empty strings")
    return list(value)


def _parallelism(step: Mapping[str, Any], source_path: Path) -> int:
    value = step.get("parallelism", 1)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RepositoryCatalogError(
            f"{source_path}: parallelism must be a positive integer"
        )
    return value


def _profile(value: Any, *, prefix: str | None = None) -> str:
    if value is None:
        profile = "default"
    elif isinstance(value, str) and value:
        profile = value.replace("_", "-")
    else:
        raise RepositoryCatalogError("device must be non-empty text")
    return f"{prefix}-{profile}" if prefix else profile


def _direct_execution_definition(
    step: Mapping[str, Any],
    dependencies: tuple[str, ...],
) -> dict[str, Any]:
    definition = {key: step[key] for key in _DIRECT_EXECUTION_FIELDS if key in step}
    definition["depends_on"] = sorted(dependencies)
    return {
        "kind": ExecutionKind.STEP.value,
        "schema": _DEFINITION_SCHEMA,
        "step": definition,
    }


def _opaque_pipeline_definition(
    steps: list[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "kind": ExecutionKind.OPAQUE_PIPELINE.value,
        "schema": _DEFINITION_SCHEMA,
        "steps": [
            {
                key: value
                for key, value in step.items()
                if key not in {"ci_control", "key", "label"}
            }
            for step in steps
        ],
    }


def _amd_execution_definition(
    step: Mapping[str, Any],
    amd: Mapping[str, Any],
    dependencies: tuple[str, ...],
) -> dict[str, Any]:
    custom_commands = amd.get("commands")
    source_env = step.get("env")
    amd_env = amd.get("env")
    merged_env = {
        **(source_env if isinstance(source_env, Mapping) else {}),
        **(amd_env if isinstance(amd_env, Mapping) else {}),
    }
    definition = {
        "agent_tags": amd.get("agent_tags"),
        "commands": custom_commands or step.get("commands"),
        "depends_on": sorted(dependencies),
        "device": amd.get("device"),
        "dind": amd.get("dind", True),
        "env": merged_env or None,
        "no_gpu": amd.get("no_gpu", step.get("no_gpu", False)),
        "no_plugin": amd.get("no_plugin", False),
        "num_devices": (
            amd.get("num_devices") or amd.get("num_gpus") or step.get("num_devices")
        ),
        "num_nodes": amd.get("num_nodes", step.get("num_nodes")),
        "parallelism": step.get("parallelism"),
        "soft_fail": amd.get("soft_fail", step.get("soft_fail", False)),
        "timeout_in_minutes": amd.get("timeout_in_minutes"),
        "working_dir": (
            amd.get("working_dir", step.get("working_dir"))
            if custom_commands
            else step.get("working_dir")
        ),
    }
    return {
        "kind": "amd-mirror",
        "schema": _DEFINITION_SCHEMA,
        "step": definition,
    }


def _definition_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        default=str,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise RepositoryCatalogError(f"cannot read {path}: {error}") from error


def main() -> None:
    """Validate a checkout and print its deterministic catalog summary."""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "repository_root",
        nargs="?",
        type=Path,
        default=Path.cwd(),
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--baseline",
        type=Path,
        help="previous repository checkout used for selector compatibility",
    )
    arguments = parser.parse_args()

    catalog = compile_repository_catalog(arguments.repository_root)
    if arguments.baseline is not None:
        baseline = compile_repository_catalog(arguments.baseline)
        validate_catalog_evolution(baseline, catalog)
    if arguments.as_json:
        print(
            json.dumps(
                catalog.to_mapping(),
                indent=2,
                sort_keys=True,
            )
        )
    else:
        selectable = sum(job.selectable for job in catalog.jobs)
        print(
            f"catalog {catalog.version}: {selectable} selectable jobs, "
            f"{len(catalog.jobs)} total, sha256:{catalog.digest}"
        )


if __name__ == "__main__":
    main()
