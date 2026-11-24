"""`pipeline-generator explain` and `pipeline-generator lint`: what the jobs of
a test_areas file (or one key) resolve to, and the lint of `devices:` steps.

    pipeline-generator explain .buildkite/test_areas/attention.yaml
    pipeline-generator explain v1-attention --job-dir .buildkite/test_areas
    pipeline-generator lint .buildkite/test_areas [--fix]

`explain` reads the yaml as the generator does (both step forms, the file's
`depends_on` included) and prints one row per job: key, device, label,
timeout, optional, commands, dependencies and depends_on. `--format json`
prints every resolved field, to compare two versions of a file job by job.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List

import click
import yaml

from amd import (
    get_amd_label,
    get_amd_timeout_in_minutes,
    is_amd_gpu_device,
    normalize_amd_depends_on,
)
from step import Step
from step_lint import lint_paths
from step_vendors import DeviceStepError

DEFAULT_JOB_DIR = ".buildkite/test_areas"
DEFAULT_REPO = "vllm-project/vllm"


def job_rows(step: Step) -> List[Dict[str, Any]]:
    """The jobs a loaded step stands for (itself, and its AMD mirror), each
    with the fields that decide what runs where."""
    # Not at the top: buildkite_step reads the global config when imported
    # by the generator, and these helpers need none.
    from buildkite_step import _get_amd_mirror_effective_step, _mirror_parent

    rows = []
    amd_native = is_amd_gpu_device(step.device)
    rows.append(
        _row(
            key=step.key,
            device=step.device,
            label=get_amd_label(step.label, step.device) if amd_native else step.label,
            timeout=(
                get_amd_timeout_in_minutes(step.timeout_in_minutes)
                if amd_native
                else step.timeout_in_minutes
            ),
            step=step,
            commands=step.commands,
            depends_on=(
                normalize_amd_depends_on(step.depends_on)
                if amd_native
                else step.depends_on
            ),
            dind=step.dind,
            num_devices=step.num_devices,
            env=step.env,
        )
    )
    amd = (step.mirror or {}).get("amd")
    if amd:
        parent = _mirror_parent(step, amd)
        effective = _get_amd_mirror_effective_step(step, amd)
        env = dict(parent.env or {})
        env.update(amd.get("env") or {})
        rows.append(
            _row(
                key=effective.key,
                device=amd.get("device"),
                label=get_amd_label(amd.get("label") or step.label, amd.get("device")),
                timeout=get_amd_timeout_in_minutes(amd.get("timeout_in_minutes")),
                step=effective,
                commands=amd.get("commands") or parent.commands,
                depends_on=normalize_amd_depends_on(amd.get("depends_on")),
                dind=amd.get("dind", True),
                num_devices=amd.get("num_devices", parent.num_devices),
                env=env,
                working_dir=(
                    amd.get("working_dir", parent.working_dir)
                    if amd.get("commands")
                    else parent.working_dir
                ),
                soft_fail=amd.get("soft_fail", parent.soft_fail),
                parallelism=amd.get("parallelism", parent.parallelism),
            )
        )
    return rows


def _row(
    *,
    key,
    device,
    label,
    timeout,
    step: Step,
    commands,
    depends_on,
    dind,
    num_devices,
    env,
    working_dir=None,
    soft_fail=None,
    parallelism=None,
) -> Dict[str, Any]:
    return {
        "key": key,
        "device": device,
        "label": label,
        "timeout_in_minutes": timeout,
        "optional": bool(step.optional),
        "soft_fail": bool(step.soft_fail if soft_fail is None else soft_fail),
        "commands": list(commands or []),
        "source_file_dependencies": list(step.source_file_dependencies or []),
        "depends_on": list(depends_on or []),
        "env": dict(env or {}),
        "dind": bool(dind),
        "num_devices": num_devices,
        "working_dir": working_dir if working_dir is not None else step.working_dir,
        "parallelism": parallelism if parallelism is not None else step.parallelism,
        "automatic_shard": step.automatic_shard,
        "source_file": step.source_file,
    }


def file_rows(path: str, repo: str = DEFAULT_REPO) -> List[Dict[str, Any]]:
    """Every job of a job file, in yaml order."""
    from step import read_steps_from_file

    rows = []
    for step in read_steps_from_file(path, repo):
        for row in job_rows(step):
            row["step"] = step._origin
            rows.append(row)
    return rows


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, list):
        return "<br>".join(f"`{item}`" for item in value) or "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value).replace("|", "\\|")


COLUMNS = (
    ("key", "Key"),
    ("device", "Device"),
    ("label", "Label"),
    ("timeout_in_minutes", "Timeout"),
    ("optional", "Optional"),
    ("commands", "Commands"),
    ("source_file_dependencies", "Dependencies"),
    ("depends_on", "Depends on"),
)


def table(rows: List[Dict[str, Any]]) -> str:
    lines = [
        "| " + " | ".join(title for _, title in COLUMNS) + " |",
        "|" + "---|" * len(COLUMNS),
    ]
    for row in rows:
        lines.append("| " + " | ".join(_cell(row[name]) for name, _ in COLUMNS) + " |")
    return "\n".join(lines)


@click.group()
def cli():
    """Tools for the test_areas yaml."""


@cli.command()
@click.argument("target")
@click.option(
    "--job-dir",
    "job_dirs",
    multiple=True,
    default=[DEFAULT_JOB_DIR],
    show_default=True,
    help="Where to look a key up (repeatable).",
)
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.option(
    "--repo",
    default=DEFAULT_REPO,
    show_default=True,
    help="The pipeline's github_repo_name (it decides the working-dir default).",
)
def explain(target: str, job_dirs, fmt: str, repo: str):
    """Print the jobs of a job file, or of the step a key belongs to."""
    try:
        if os.path.isfile(target):
            rows = file_rows(target, repo)
        else:
            rows = _rows_for_key(target, job_dirs, repo)
    except (DeviceStepError, ValueError, yaml.YAMLError) as error:
        raise click.ClickException(str(error))
    if not rows:
        raise click.ClickException(f"no job file or job key {target!r}")
    if fmt == "json":
        click.echo(json.dumps(rows, indent=2, sort_keys=True))
    else:
        click.echo(table(rows))


def _rows_for_key(key: str, job_dirs, repo: str) -> List[Dict[str, Any]]:
    """Every job of the yaml step that has a job keyed `key`."""
    from step_lint import yaml_files

    for path in yaml_files(job_dirs):
        rows = file_rows(path, repo)
        origin = next((row["step"] for row in rows if row["key"] == key), None)
        if origin is not None:
            return [row for row in rows if row["step"] == origin]
    return []


@cli.command()
@click.argument("paths", nargs=-1)
@click.option("--fix", is_flag=True, help="Fix L1, L2 and L4 in place.")
def lint(paths, fix: bool):
    """Check `devices:` steps (E1-E9, L1-L4); exit 1 on an error or an
    unfixed L1, L2 or L4 finding (L3 only warns)."""
    findings = lint_paths(paths or [DEFAULT_JOB_DIR], fix=fix)
    failed = False
    for finding in findings:
        level = "error" if finding.is_error else "warning"
        click.echo(f"{finding} [{level}]")
        failed = failed or finding.is_error
    if failed:
        if not fix and any(finding.fixable for finding in findings):
            click.echo("Run `pipeline-generator lint --fix` to fix L1, L2 and L4.")
        sys.exit(1)


COMMANDS = ("explain", "lint")
