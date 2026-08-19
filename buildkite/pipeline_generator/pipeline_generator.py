import base64
import importlib.metadata
import json
import os
import re
import shlex
import subprocess
from math import ceil
from pathlib import Path
from typing import FrozenSet, List, Optional, Set, Tuple

import yaml
from amd import is_amd_gpu_device
from buildkite_step import (
    BuildkiteCommandStep,
    BuildkiteGroupStep,
    add_precommit_dependency,
    convert_group_step_to_buildkite_step,
    create_precommit_group_step,
)
from constants import AgentQueue, DeviceType
from global_config import get_global_config, init_global_config
from step import Step, group_steps, read_steps_from_job_dir
from test_selection.collector_bundle import (
    bundle_bytes,
    bundle_sha256,
)


class PipelineGenerator:
    def __init__(
        self,
        pipeline_config_path: str,
        output_file_path: str,
        docs_only_disable: bool = False,
    ):
        init_global_config(pipeline_config_path)
        self.output_file_path = output_file_path

    def generate(self):
        global_config = get_global_config()

        # Skip if changes are doc-only (unless RUN_ALL is set)
        if (
            global_config["docs_only_disable"] == "0"
            and not global_config["run_all"]
            and global_config["only_step_keys"] is None
        ):
            if is_docs_only_change(global_config["list_file_diff"]):
                print("List file diff: ", global_config["list_file_diff"])
                print("All changes are doc-only, skipping CI.")
                subprocess.run(
                    [
                        "buildkite-agent",
                        "annotate",
                        ":memo: CI skipped — doc-only changes",
                    ],
                    check=True,
                )
                output_dir_path = os.path.dirname(self.output_file_path)
                with open(os.path.join(output_dir_path, ".docs_only"), "w") as f:
                    f.write("true")
                return

        steps = []
        test_area_keys = set()
        for job_dir in global_config["job_dirs"]:
            job_steps = read_steps_from_job_dir(job_dir)
            steps.extend(job_steps)
            if Path(job_dir).as_posix().rstrip("/").endswith(".buildkite/test_areas"):
                test_area_keys.update(step.key for step in job_steps if step.key)
        publish_trace_snapshot = should_trace_nightly(global_config)
        collector = bundle_bytes() if publish_trace_snapshot else b""
        collector_sha256 = bundle_sha256(collector) if collector else None
        steps, trace_inventory = configure_test_tracing(
            steps,
            test_area_keys if publish_trace_snapshot else set(),
            global_config["commit"],
            _ci_infra_revision() if publish_trace_snapshot else None,
            collector_sha256,
        )
        steps, selected_step_keys = select_steps_and_dependencies(
            steps, global_config["only_step_keys"]
        )
        global_config["only_step_keys"] = selected_step_keys
        grouped_steps = group_steps(steps)

        buildkite_group_steps = convert_group_step_to_buildkite_step(grouped_steps)
        buildkite_group_steps = sorted(buildkite_group_steps, key=lambda x: x.group)
        if publish_trace_snapshot:
            finalize_trace_inventory(trace_inventory, buildkite_group_steps)
            buildkite_group_steps.append(
                create_snapshot_group_step(trace_inventory, global_config)
            )
        if trace_inventory["jobs"]:
            buildkite_group_steps.insert(
                0, create_collector_group_step(collector, collector_sha256 or "")
            )

        # Run pre-commit as a dedicated step in parallel with the image build.
        # Steps that depend on the image build also wait for pre-commit to pass.
        # Place it first so it shows up right after the bootstrap step.
        if global_config["pull_request"] and global_config["pull_request"] != "false":
            add_precommit_dependency(buildkite_group_steps)
            buildkite_group_steps.insert(
                0,
                create_precommit_group_step(
                    global_config["github_repo_name"], global_config["commit"]
                ),
            )

        buildkite_steps_dict = {"steps": []}
        for buildkite_group_step in buildkite_group_steps:
            buildkite_steps_dict["steps"].append(
                buildkite_group_step.dict(exclude_none=True)
            )
        with open(self.output_file_path, "w") as f:
            yaml.dump(
                buildkite_steps_dict, f, sort_keys=False, default_flow_style=False
            )
        return


_PYTEST_COMMAND = re.compile(r"(?:^|[\s;&|])(?:python3?\s+-m\s+)?pytest(?:\s|$)")
_NVIDIA_TRACE_DEVICES = {
    DeviceType.A100,
    DeviceType.B200,
    DeviceType.B200_K8S,
    DeviceType.DGX_SPARK,
    DeviceType.GH200,
    DeviceType.H100,
    DeviceType.H200,
    DeviceType.H200_18GB,
    DeviceType.H200_35GB,
}
_K8S_TRACE_DEVICES = {
    DeviceType.A100,
    DeviceType.B200_K8S,
    DeviceType.H100,
}
TRACE_COLLECTOR_STEP_KEY = "test-selection-collector"

# Full-fleet canary #84324 measured prohibitive Python-coverage overhead for
# these exact jobs. Keep the evidence-based keys visible in the inventory but
# uninstrumented until a lower-overhead collector is proven.
_FLEET_CPU_OVERHEAD_ALWAYS_RUN = frozenset(
    {
        "benchmarks-cli-test",
        "engine",
        "engine-1-gpu",
        "multi-modal-processor-cpu",
        "v1-others-cpu",
    }
)

# Full-fleet canaries #84375 and #84580 proved these exact jobs incompatible
# with per-test Python dynamic contexts. The latter found the exact forked-CUDA
# root in every listed newly enrolled job's trace-wrapped log; subprocess/server
# startup, IPC, or long-running GPU work also failed or timed out in #84375
# while repeated plain-main controls passed. The automatic nightly path must
# preserve these carve-outs; removing the old TRACE_ALL switch does not
# invalidate the underlying fleet evidence.
_FLEET_COLLECTOR_COMPATIBILITY_ALWAYS_RUN = frozenset(
    {
        "async-engine-inputs-utils-worker",
        "basic-models-tests-extra-initialization",
        "basic-models-tests-initialization",
        "basic-models-tests-other",
        "bitsandbytes-plugin",
        "distributed-comm-ops",
        "distributed-tests-2xh100-2xmi300",
        "e2e-core-1-gpu",
        "e2e-scheduling-1-gpu",
        "entrypoints-unit-tests",
        "extract-hidden-states-integration",
        "gguf-plugin",
        "kernels-fp8-moe-test-2xh100",
        "kv-offload-large",
        "language-models-test-extended-pooling",
        "language-models-test-mteb",
        "language-models-test-ppl",
        "lm-eval-humming-act-b200",
        "lm-eval-humming-f16-b200",
        "lm-eval-qwen3-5-models-2xb200",
        "lora",
        "model-runner-v2-core-tests",
        "model-runner-v2-spec-decode",
        "moe-refactor-integration-test-b200-dp-temporary",
        "multi-modal-accuracy-eval-small-models",
        "multi-modal-models-extended-generation-1",
        "multi-modal-models-extended-generation-2",
        "multi-modal-models-extended-generation-3",
        "multi-modal-models-extended-pooling",
        "multi-modal-models-extended-ppl",
        "multi-modal-models-standard-1-qwen2",
        "multi-modal-models-standard-2-qwen3-gemma",
        "multi-modal-models-standard-3-llava-qwen2-vl",
        "multi-modal-models-standard-4-other-whisper",
        "pipeline-context-parallelism-4-gpus",
        "pytorch-compilation-unit-tests-h100",
        "quantized-models-test",
        "regression",
        "spec-decode-dflash-nightly",
        "spec-decode-draft-model",
        "spec-decode-dspark-nightly",
        "spec-decode-eagle-1-deepseek-qwen",
        "spec-decode-eagle-2-llama3-qwen-vl-other",
        "spec-decode-mtp-other-acceptance-nightly",
        "spec-decode-ngram-suffix",
        "spec-decode-speculators-mtp",
    }
)


def should_trace_nightly(global_config: dict) -> bool:
    """Trace vLLM test-area pytest jobs on trusted main nightlies."""

    return bool(
        global_config["github_repo_name"] == "vllm-project/vllm"
        and global_config["nightly"] == "1"
        and global_config["branch"] == "main"
        and global_config["pull_request"] in (None, "false")
        and global_config["trace_s3_bucket"]
    )


def _ci_infra_revision() -> str:
    """Resolve the exact generator commit so the fan-in cannot branch-drift."""

    override = os.getenv("VLLM_CI_REVISION")
    if override:
        if not re.fullmatch(r"[0-9a-f]{40}", override):
            raise ValueError("VLLM_CI_REVISION must be a 40-character Git SHA.")
        return override
    try:
        direct_url_text = importlib.metadata.distribution(
            "pipeline-generator"
        ).read_text("direct_url.json")
        direct_url = json.loads(direct_url_text or "{}")
        commit_id = direct_url.get("vcs_info", {}).get("commit_id")
        if re.fullmatch(r"[0-9a-f]{40}", str(commit_id)):
            return str(commit_id)
    except (importlib.metadata.PackageNotFoundError, json.JSONDecodeError):
        pass
    repository_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=False,
        capture_output=True,
        text=True,
    )
    revision = result.stdout.strip()
    if result.returncode or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("cannot resolve the exact ci-infra generator revision")
    return revision


def _is_traceable_pytest_step(step: Step) -> bool:
    return bool(step.commands) and any(
        _PYTEST_COMMAND.search(command) for command in step.commands or []
    )


def _trace_rejection(step: Step) -> Optional[str]:
    if not step.key:
        return "missing_step_key"
    if not _is_traceable_pytest_step(step):
        return "not_plain_pytest_commands"
    return None


def configure_test_tracing(
    steps: List[Step],
    test_area_step_keys: Set[str],
    repository_sha: Optional[str] = None,
    ci_infra_revision: Optional[str] = None,
    collector_sha256: Optional[str] = None,
) -> Tuple[List[Step], dict]:
    """Instrument selected existing jobs with the exact ci-infra collector."""

    steps_by_key = {step.key: step for step in steps if step.key}
    if test_area_step_keys and not re.fullmatch(
        r"[0-9a-f]{40}", ci_infra_revision or ""
    ):
        raise ValueError("nightly trace inventory requires an exact ci-infra revision")
    if test_area_step_keys and not re.fullmatch(
        r"[0-9a-f]{64}", collector_sha256 or ""
    ):
        raise ValueError("trace collection requires an exact collector checksum")
    rejected = {}
    traced = []
    for step_key in sorted(test_area_step_keys):
        step = steps_by_key[step_key]
        if step_key in _FLEET_COLLECTOR_COMPATIBILITY_ALWAYS_RUN:
            rejected[step_key] = "collector_compatibility_policy"
            continue
        if step_key in _FLEET_CPU_OVERHEAD_ALWAYS_RUN:
            rejected[step_key] = "cpu_overhead_policy"
            continue
        rejection = _trace_rejection(step)
        if rejection:
            rejected[step_key] = rejection
            continue
        timeout = step.timeout_in_minutes
        if timeout is not None:
            timeout = max(timeout + 15, ceil(timeout * 1.5))
        mode = (
            "kernel-set"
            if step.device in _NVIDIA_TRACE_DEVICES
            and not is_amd_gpu_device(step.device)
            else "python-only"
        )
        step.timeout_in_minutes = timeout
        step.mount_buildkite_agent = bool(
            step.mount_buildkite_agent or step.device not in _K8S_TRACE_DEVICES
        )
        step.trace_collector_sha256 = collector_sha256
        step.trace_represented_job_key = step_key
        step.trace_gpu = mode == "kernel-set"
        traced.append(
            {
                "expected_shards": step.parallelism or 1,
                "key": step_key,
                "mode": mode,
            }
        )

    inventory = {
        "always_run": [
            {"key": key, "reason": reason} for key, reason in sorted(rejected.items())
        ],
        "ci_infra_revision": ci_infra_revision,
        "collector_sha256": collector_sha256,
        "jobs": traced,
        "repository_sha": repository_sha,
        "schema_version": 1,
    }
    return steps, inventory


def finalize_trace_inventory(
    inventory: dict, buildkite_group_steps: List[BuildkiteGroupStep]
) -> None:
    """Account for exact rendered command keys, including generated mirrors."""

    rendered_steps = [
        step
        for group in buildkite_group_steps
        for step in group.steps
        if isinstance(step, BuildkiteCommandStep)
    ]
    rendered = {step.key: step for step in rendered_steps}
    if len(rendered) != len(rendered_steps):
        raise ValueError("rendered pipeline contains duplicate command step keys")
    traced = {
        key
        for key, step in rendered.items()
        if any(
            "ci_test_selection.run_job_trace" in command for command in step.commands
        )
    }
    expected = {job["key"]: job for job in inventory["jobs"]}
    if traced != set(expected):
        raise ValueError(
            "rendered trace inventory mismatch: expected=%s observed=%s"
            % (sorted(expected), sorted(traced))
        )
    for key, job in expected.items():
        rendered_shards = rendered[key].parallelism or 1
        if rendered_shards != job["expected_shards"]:
            raise ValueError(f"rendered trace shard count changed for {key}")
    always_run = {row["key"]: row["reason"] for row in inventory["always_run"]}
    overlap = set(always_run) & set(expected)
    if overlap:
        raise ValueError(
            "trace inventory accounts job twice: " + ", ".join(sorted(overlap))
        )
    for key in sorted(set(rendered) - set(expected) - set(always_run)):
        always_run[key] = "rendered_uninstrumented_step"
    accounted = set(expected) | set(always_run)
    if accounted != set(rendered):
        raise ValueError("trace inventory does not match exact rendered command keys")
    inventory["always_run"] = [
        {"key": key, "reason": reason} for key, reason in sorted(always_run.items())
    ]


def create_collector_group_step(
    collector: bytes, collector_sha256: str
) -> BuildkiteGroupStep:
    """Publish the exact ci-infra-owned collector once for all traced jobs."""

    encoded = base64.b64encode(collector).decode()
    command = "\n".join(
        [
            "set -euo pipefail",
            'COLLECTOR_DIR="$$(mktemp -d)"',
            "trap 'rm -rf \"$$COLLECTOR_DIR\"' EXIT",
            (
                f"printf '%s' {shlex.quote(encoded)} | base64 -d "
                '> "$$COLLECTOR_DIR/test-selection-collector.zip"'
            ),
            (
                f'echo "{collector_sha256}  '
                '$$COLLECTOR_DIR/test-selection-collector.zip" | sha256sum -c -'
            ),
            'cd "$$COLLECTOR_DIR"',
            'buildkite-agent artifact upload "test-selection-collector.zip"',
        ]
    )
    return BuildkiteGroupStep(
        group="Test selection collector",
        steps=[
            BuildkiteCommandStep(
                agents={"queue": AgentQueue.SMALL_CPU_PREMERGE.value},
                commands=[command],
                key=TRACE_COLLECTOR_STEP_KEY,
                label=":package: Publish test-selection collector",
                retry={"automatic": [{"exit_status": -1, "limit": 1}]},
                soft_fail=True,
                timeout_in_minutes=10,
            )
        ],
    )


def create_snapshot_group_step(
    inventory: dict, global_config: dict
) -> BuildkiteGroupStep:
    """Create the bounded, self-waiting publisher for a trace snapshot."""

    encoded_inventory = base64.b64encode(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    bucket = shlex.quote(global_config["trace_s3_bucket"])
    prefix = shlex.quote(global_config["trace_s3_prefix"])
    revision = inventory["ci_infra_revision"]
    group = "Test selection snapshot"
    label = ":database: Publish test-selection snapshot"
    command = "\n".join(
        [
            "set -euo pipefail",
            'SNAPSHOT_DIR="$$(mktemp -d)"',
            "trap 'rm -rf \"$$SNAPSHOT_DIR\"' EXIT",
            'mkdir -p "$$SNAPSHOT_DIR/evidence"',
            (
                f"printf '%s' {shlex.quote(encoded_inventory)} | base64 -d "
                '> "$$SNAPSHOT_DIR/inventory.json"'
            ),
            'python3 -m venv "$$SNAPSHOT_DIR/venv"',
            (
                '"$$SNAPSHOT_DIR/venv/bin/pip" install --quiet '
                '"git+https://github.com/vllm-project/ci-infra.git@'
                f'{revision}#subdirectory=buildkite/pipeline_generator"'
            ),
            f"TRACE_S3_BUCKET={bucket}",
            f"TRACE_S3_PREFIX={prefix}",
            'echo "+++ :hourglass_flowing_sand: Wait for traced steps"',
            (
                '"$$SNAPSHOT_DIR/venv/bin/vllm-test-selection" wait-for-steps '
                '--inventory "$$SNAPSHOT_DIR/inventory.json" '
                "--timeout-seconds 28800 --poll-seconds 120"
            ),
            (
                'buildkite-agent artifact download "trace-output/**/*" '
                '"$$SNAPSHOT_DIR/evidence"'
            ),
            (
                '"$$SNAPSHOT_DIR/venv/bin/vllm-test-selection" publish-snapshot '
                '--input "$$SNAPSHOT_DIR/evidence" '
                '--inventory "$$SNAPSHOT_DIR/inventory.json" '
                '--bucket "$$TRACE_S3_BUCKET" --prefix "$$TRACE_S3_PREFIX"'
            ),
        ]
    )
    return BuildkiteGroupStep(
        group=group,
        steps=[
            BuildkiteCommandStep(
                agents={"queue": AgentQueue.CPU_POSTMERGE_US_EAST_1.value},
                commands=[command],
                # Buildkite hard-invalidates dependents when any dependency is
                # canceled, even with both dependency-failure tolerance knobs.
                # Poll aggregate step state instead and let the inventory make
                # bounded nonterminal/missing evidence explicitly always-run.
                depends_on=None,
                key="test-selection-snapshot",
                label=label,
                priority=-100,
                retry={"automatic": [{"exit_status": -1, "limit": 1}]},
                soft_fail=True,
                timeout_in_minutes=510,
            )
        ],
    )


def select_steps_and_dependencies(
    steps: List[Step],
    requested_step_keys: Optional[FrozenSet[str]],
) -> Tuple[List[Step], Optional[FrozenSet[str]]]:
    if requested_step_keys is None:
        return steps, None

    steps_by_key = {}
    for step in steps:
        if not step.key:
            continue
        if step.key in steps_by_key:
            raise ValueError(f"Duplicate CI step key: {step.key}")
        steps_by_key[step.key] = step

    missing = requested_step_keys - steps_by_key.keys()
    if missing:
        raise ValueError("Unknown CI step key(s): " + ", ".join(sorted(missing)))

    selected_step_keys = set(requested_step_keys)
    pending = list(requested_step_keys)
    while pending:
        step_key = pending.pop()
        for dependency in steps_by_key[step_key].depends_on or []:
            if dependency == TRACE_COLLECTOR_STEP_KEY:
                continue
            if dependency not in steps_by_key:
                raise ValueError(
                    f"CI step {step_key} depends on unknown step {dependency}."
                )
            if dependency not in selected_step_keys:
                selected_step_keys.add(dependency)
                pending.append(dependency)

    selected = [step for step in steps if step.key in selected_step_keys]
    return selected, frozenset(selected_step_keys)


def is_docs_only_change(list_file_diff: List[str]) -> bool:
    if len(list_file_diff) == 0:
        return False
    for file_path in list_file_diff:
        if not file_path:
            continue
        if file_path.startswith("docs/"):
            continue
        if file_path.endswith(".md"):
            continue
        if file_path in {"mkdocs.yml", "mkdocs.yaml"}:
            continue
        return False
    return True
