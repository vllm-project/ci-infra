# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Parse .buildkite ci_config*.yaml pipelines into Step records.

Only what the v2 generator reads: the job_dirs a ci_config names. The legacy
test-amd.yaml is in none of them, and test-pipeline.yaml is a dead stub.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import yaml

from ...handwritten import (
    KNOWN_STEP_FIELDS,
    MIRROR_DEFAULT_DEPENDS_ON,
    MIRROR_OVERRIDABLE,
)
from .step import (
    DEFAULT_WORKING_DIR,
    LoadReport,
    PipelineConfig,
    Step,
    derive_step_key,
    repo_rel,
)

CI_DIR = ".buildkite"
CI_CONFIG_GLOB = "ci_config*.yaml"
JOB_FILE_GLOB = "*.yaml"


def load_pipeline_configs(repo: Path) -> list[PipelineConfig]:
    configs = []
    for path in sorted((repo / CI_DIR).glob(CI_CONFIG_GLOB)):
        data = yaml.safe_load(path.read_text())
        configs.append(
            PipelineConfig(
                name=data["name"],
                config_file=repo_rel(path, repo),
                job_dirs=data.get("job_dirs", []),
                run_all_patterns=data.get("run_all_patterns", []),
                run_all_exclude_patterns=data.get("run_all_exclude_patterns", []),
            )
        )
    if not configs:
        raise FileNotFoundError(f"no {CI_DIR}/{CI_CONFIG_GLOB} under {repo}")
    return configs


def load_steps(
    repo: Path, config: PipelineConfig, report: LoadReport | None = None
) -> list[Step]:
    report = report if report is not None else LoadReport()
    steps: list[Step] = []
    seen_keys: set[str] = set()
    for job_dir in config.job_dirs:
        base = repo / job_dir
        if not base.is_dir():
            raise FileNotFoundError(f"{config.name}: job_dir {job_dir} missing")
        for path in sorted(base.rglob(JOB_FILE_GLOB)):
            steps.extend(_parse_job_file(path, repo, config.name, report, seen_keys))
    return steps


def edited_steps(
    before: str | None, after: str | None, source_file: str, pipeline: str
) -> frozenset[str] | None:
    """The steps an edit to one job file adds or changes, by step id, or None
    when the edit has to count as touching every step the file defines.

    A step is paired with its namesake on the other side and compared as the
    loader resolves it, and on its yaml entry too, for the keys the Step does
    not model, such as `retry` and `agent_tags`. A mirror is compared on its
    own block and on the parent keys that block does not set. Two keys are
    left out: `source_file_dependencies`, which only today's rules read, and
    the label, which names the job and is the id itself when there is no key.

    Without this, editing a job file selected every step it defines and
    nothing it added. vllm#52928 added replayssm-e2e to engine.yaml, the only
    job running its new test, and the base pipeline had no such step to pick;
    vllm#54025 added one dependency line to seven files and held 35 steps.

    The file-level keys apply to every step, so a change there, a side that
    is not a job file, or two steps sharing an id returns None. A side that is
    None is a file the diff added or deleted: every step on the other side is
    added, or gone.
    """
    try:
        old = _definitions(before, source_file, pipeline)
        new = _definitions(after, source_file, pipeline)
    except Exception:  # noqa: BLE001 - what the loader cannot read, CI cannot either
        return None
    if old is None or new is None:
        return None
    if before is not None and after is not None and old[0] != new[0]:
        return None
    return frozenset(sid for sid, d in new[1].items() if old[1].get(sid) != d)


def _definitions(
    text: str | None, source_file: str, pipeline: str
) -> tuple[dict, dict[str, tuple]] | None:
    """(the file-level keys, step id -> the yaml defining that step), for
    `edited_steps`. A missing file is an empty one."""
    if text is None:
        return {}, {}
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or "steps" not in data:
        return None
    steps = _job_steps(data, source_file, pipeline, LoadReport(), set())
    # Same order `_job_steps` emits: each step, then its mirrors.
    entries = [
        (raw, hw) for raw in data["steps"] for hw in (None, *(raw.get("mirror") or {}))
    ]
    defs = {}
    for step, (raw, hw) in zip(steps, entries, strict=True):
        own = _outcome(raw, "mirror")
        if hw is not None:
            # A key the mirror sets replaces the parent's, so a parent edit to
            # it changes nothing here. The resolved step carries the rest.
            block = _outcome(raw["mirror"][hw] or {})
            own = (block, {k: v for k, v in own.items() if k not in block})
        resolved = dataclasses.replace(
            step, source_file_dependencies=None, label="", mirror_label=None
        )
        defs[step.step_id] = (resolved, own)
    if len(defs) != len(steps):
        return None
    return {k: v for k, v in data.items() if k != "steps"}, defs


def _outcome(raw: dict, *also: str) -> dict:
    return {
        k: v
        for k, v in raw.items()
        if k not in ("source_file_dependencies", "label", *also)
    }


def _parse_job_file(
    path: Path,
    repo: Path,
    pipeline: str,
    report: LoadReport,
    seen_keys: set[str],
) -> list[Step]:
    data = yaml.safe_load(path.read_text())
    return _job_steps(data, repo_rel(path, repo), pipeline, report, seen_keys)


def _job_steps(
    data,
    source_file: str,
    pipeline: str,
    report: LoadReport,
    seen_keys: set[str],
) -> list[Step]:
    if not isinstance(data, dict) or "steps" not in data:
        return []
    group = data.get("group")
    # Generator quirk: depends_on inheritance, the working-dir default and the
    # self-dep append only fire when file-level depends_on is truthy. An empty
    # list gets none of them.
    group_depends_raw = data.get("depends_on")
    group_active = bool(group_depends_raw)
    group_depends = _as_list(group_depends_raw)
    steps: list[Step] = []
    for raw in data["steps"]:
        step = _parse_step(
            raw, source_file, pipeline, group, group_depends, group_active, report
        )
        if step.key:
            if step.key in seen_keys:
                report.duplicate_keys.append(f"{step.pipeline}:{step.key}")
            seen_keys.add(step.key)
        steps.append(step)
        mirror = raw.get("mirror") or {}
        for hw, overrides in mirror.items():
            steps.append(_expand_mirror(step, hw, overrides or {}, report))
    return steps


def _parse_step(
    raw: dict,
    source_file: str,
    pipeline: str,
    group: str | None,
    group_depends: list[str],
    group_active: bool,
    report: LoadReport,
) -> Step:
    commands = raw.get("commands")
    if commands is None and "command" in raw:  # legacy singular
        commands = raw["command"]
    if isinstance(commands, str):
        commands = [commands]
    depends = raw.get("depends_on", ...)
    deps = (
        list(raw["source_file_dependencies"])
        if raw.get("source_file_dependencies") is not None
        else None
    )
    working_dir = raw.get("working_dir")
    if group_active:
        deps = (deps or []) + [source_file]  # generator's self-dep append
        working_dir = working_dir or DEFAULT_WORKING_DIR
    step = Step(
        pipeline=pipeline,
        source_file=source_file,
        label=str(raw.get("label", "")),
        key=raw.get("key"),
        group=group,
        commands=list(commands or []),
        source_file_dependencies=deps,
        device=raw.get("device") or raw.get("gpu"),  # gpu = legacy alias
        num_devices=raw.get("num_devices") or raw.get("num_gpus"),
        num_nodes=raw.get("num_nodes"),
        working_dir=working_dir or "",
        timeout_in_minutes=raw.get("timeout_in_minutes"),
        optional=bool(raw.get("optional", False)),
        soft_fail=bool(raw.get("soft_fail", False)),
        autorun_on_main=bool(raw.get("autorun_on_main", False)),
        no_plugin=bool(raw.get("no_plugin", False)),
        dind=bool(raw.get("dind", True)),
        parallelism=raw.get("parallelism"),
        # In a group-active file an empty or absent own depends_on is replaced
        # by the group's.
        depends_on=(
            group_depends
            if (depends is ... or (group_active and not depends))
            else _as_list(depends)
        ),
        env=dict(raw.get("env") or {}),
        extra={k: v for k, v in raw.items() if k not in KNOWN_STEP_FIELDS},
    )
    if step.extra:
        report.record_unknown(step.extra, step.step_id)
    return step


def _expand_mirror(parent: Step, hw: str, overrides: dict, report: LoadReport) -> Step:
    """Copy of ci-infra's mirror derivation: deps are a union with the mirror's
    first, commands and working_dir override only when the mirror brings its own
    commands, and the rest falls back to the parent."""
    deps = list(overrides.get("source_file_dependencies") or [])
    for dep in parent.source_file_dependencies or []:
        if dep not in deps:
            deps.append(dep)
    custom_commands = overrides.get("commands")
    env = dict(parent.env)
    env.update(overrides.get("env") or {})
    variant = Step(
        pipeline=parent.pipeline,
        source_file=parent.source_file,
        label=f"{parent.label} ({hw})",
        # Never None, even when the parent yaml omits a key. The generator
        # publishes `amd-<parent key or derived-from-PARENT-label>`; leaving
        # this None made `buildkite_key` derive from the mirror's own renamed
        # label instead, dropping the `amd-` prefix entirely.
        key=f"{parent.key or derive_step_key(parent.label)}-{hw}",
        group=parent.group,
        commands=list(custom_commands or parent.commands),
        source_file_dependencies=deps or None,
        device=overrides.get("device", parent.device),
        num_devices=(
            overrides.get("num_devices")
            or overrides.get("num_gpus")
            or parent.num_devices
        ),
        num_nodes=overrides.get("num_nodes", parent.num_nodes),
        working_dir=(
            overrides.get("working_dir", parent.working_dir)
            if custom_commands
            else parent.working_dir
        ),
        timeout_in_minutes=overrides.get(
            "timeout_in_minutes", parent.timeout_in_minutes
        ),
        optional=bool(overrides.get("optional", parent.optional)),
        soft_fail=bool(overrides.get("soft_fail", parent.soft_fail)),
        autorun_on_main=parent.autorun_on_main,
        no_plugin=bool(overrides.get("no_plugin", parent.no_plugin)),
        dind=bool(overrides.get("dind", True)),
        parallelism=parent.parallelism,
        depends_on=(
            _as_list(overrides.get("depends_on")) or list(MIRROR_DEFAULT_DEPENDS_ON)
        ),
        env=env,
        mirror_hw=hw,
        mirror_of=parent.step_id,
        mirror_label=overrides.get("label"),
    )
    unknown = {k: v for k, v in overrides.items() if k not in MIRROR_OVERRIDABLE}
    if unknown:
        report.record_unknown(unknown, variant.step_id)
    return variant


def _as_list(value) -> list[str]:
    if value is None or value is ...:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)
