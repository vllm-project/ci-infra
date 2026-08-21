import base64
import binascii
import hashlib
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

        if _published_graph_overlay_verification_requested():
            verification_step = create_published_graph_overlay_verification_group_step(
                global_config
            )
            with open(self.output_file_path, "w") as output:
                yaml.dump(
                    {"steps": [verification_step.dict(exclude_none=True)]},
                    output,
                    sort_keys=False,
                    default_flow_style=False,
                )
            return

        if _published_graph_overlay_requested():
            overlay_step = create_published_graph_overlay_group_step(global_config)
            with open(self.output_file_path, "w") as output:
                yaml.dump(
                    {"steps": [overlay_step.dict(exclude_none=True)]},
                    output,
                    sort_keys=False,
                    default_flow_style=False,
                )
            return

        if _recovery_image_copy_requested():
            copy_step = create_recovery_image_copy_group_step(global_config)
            with open(self.output_file_path, "w") as output:
                yaml.dump(
                    {"steps": [copy_step.dict(exclude_none=True)]},
                    output,
                    sort_keys=False,
                    default_flow_style=False,
                )
            return

        if _snapshot_republish_requested():
            republish_step = create_snapshot_republish_group_step(global_config)
            with open(self.output_file_path, "w") as output:
                yaml.dump(
                    {"steps": [republish_step.dict(exclude_none=True)]},
                    output,
                    sort_keys=False,
                    default_flow_style=False,
                )
            return

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
        steps, selected_step_keys = select_steps_and_dependencies(
            steps, global_config["only_step_keys"]
        )
        global_config["only_step_keys"] = selected_step_keys
        selected_test_area_keys = set(test_area_keys)
        if selected_step_keys is not None:
            selected_test_area_keys &= set(selected_step_keys)
        steps, trace_inventory = configure_test_tracing(
            steps,
            selected_test_area_keys if publish_trace_snapshot else set(),
            global_config["commit"],
            _ci_infra_revision() if publish_trace_snapshot else None,
            collector_sha256,
            _recovery_trace_timeout_overrides(
                global_config,
                selected_test_area_keys if publish_trace_snapshot else set(),
            ),
        )
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
REPUBLISH_INVENTORY_ENV = "VLLM_CI_REPUBLISH_INVENTORY_B64"
REPUBLISH_SOURCE_BUILD_ENV = "VLLM_CI_REPUBLISH_SOURCE_BUILD"
REPUBLISH_SOURCE_BUILD_ID_ENV = "VLLM_CI_REPUBLISH_SOURCE_BUILD_ID"
REPUBLISH_TRIALS_ENV = "VLLM_CI_REPUBLISH_TRIALS_JSON"
PUBLISHED_GRAPH_OVERLAY_ENV = "VLLM_CI_PUBLISHED_GRAPH_OVERLAY"
PUBLISHED_GRAPH_OVERLAY_VERIFY_ENV = "VLLM_CI_PUBLISHED_GRAPH_OVERLAY_VERIFY_ONLY"
PUBLISHED_GRAPH_OVERLAY_BASE_INVENTORY_ENV = (
    "VLLM_CI_PUBLISHED_GRAPH_OVERLAY_BASE_INVENTORY_B64"
)
PUBLISHED_GRAPH_OVERLAY_RETRY_INVENTORY_ENV = (
    "VLLM_CI_PUBLISHED_GRAPH_OVERLAY_RETRY_INVENTORY_B64"
)
RECOVERY_IMAGE_COPY_ENV = "VLLM_CI_RECOVERY_IMAGE_COPY"
RECOVERY_IMAGE_BRANCH = "ci-tsel-main-mirror-eac636a7"
RECOVERY_IMAGE_COMMIT = "eac636a7fa476983cdae34b45a984e9852aad375"
RECOVERY_IMAGE_REGISTRY = "public.ecr.aws/q9t5s3a7"
RECOVERY_IMAGE_SOURCE_REPO = "vllm-ci-postmerge-repo"
RECOVERY_IMAGE_DESTINATION_REPO = "vllm-ci-test-repo"
RECOVERY_IMAGE_AMD64_DIGEST = (
    "sha256:530a18dfb04c66cdb4ebb939b111d84c47b902abf21b3e7d3fded2deac8b556a"
)
RECOVERY_IMAGE_ARM64_DIGEST = (
    "sha256:41490e868bf2ceee1a6d4c5b3bd1434c4d4959cf694dd5ffec8ebf6916cefe83"
)
RECOVERY_BUILDX_VERSION = "v0.15.1"
RECOVERY_BUILDX_SHA256 = (
    "8d486f0088b7407a90ad675525ba4a17d0a537741b9b33fe3391a88cafa2dd0b"
)
PUBLISHED_GRAPH_OVERLAY_BUCKET = "vllm-ci-test-selection-traces-936637512419-us-east-1"
PUBLISHED_GRAPH_OVERLAY_BASE_PREFIX = "test-selection/vllm/canary/node-export-3c43f17"
PUBLISHED_GRAPH_OVERLAY_OUTPUT_PREFIX = (
    "test-selection/vllm/canary/node-export-merged-84881"
)
PUBLISHED_GRAPH_OVERLAY_BASE_MANIFEST_KEY = (
    "test-selection/vllm/canary/node-export-3c43f17/snapshots/"
    f"{RECOVERY_IMAGE_COMMIT}/manifest.json"
)
PUBLISHED_GRAPH_OVERLAY_BASE_MANIFEST_SHA256 = (
    "ca59aa071c4f31df0a3e01056c2d04753e3768ab57c0246390ad11a401d752f7"
)
PUBLISHED_GRAPH_OVERLAY_BASE_INVENTORY_SHA256 = (
    "ec9d0204b1c088cf12107603df3113cbac8e2f99ed6ada3c1a4f6e05fa047a7d"
)
PUBLISHED_GRAPH_OVERLAY_RETRY_INVENTORY_SHA256 = (
    "863a64b84649424338a6545fa5640f7258d70c48b95eb0acd6a2c440ce2c9364"
)
PUBLISHED_GRAPH_OVERLAY_BASE_BUILD_ID = "01a01ca3-7564-451f-9291-e08217fdcdd5"
PUBLISHED_GRAPH_OVERLAY_RETRY_BUILD_ID = "01a020d0-48cb-45b3-adbd-b61fdfb02781"
PUBLISHED_GRAPH_OVERLAY_REPLACEMENTS = (
    "distributed-compile-unit-tests-2xh100",
    "lm-eval-humming-act-a100",
    "rayexecutorv2-4-gpus",
)
PUBLISHED_GRAPH_OVERLAY_RETRY_MISSING = ("lm-eval-large-models-8xh200",)
PUBLISHED_GRAPH_OVERLAY_OUTPUT_MANIFEST_KEY = (
    f"{PUBLISHED_GRAPH_OVERLAY_OUTPUT_PREFIX}/snapshots/"
    f"{RECOVERY_IMAGE_COMMIT}/manifest.json"
)
PUBLISHED_GRAPH_OVERLAY_OUTPUT_MANIFEST_SHA256 = (
    "3851159f1437e33aef228d845156f7cf79db58b61104f8f611d1fcf5c2059e2b"
)
PUBLISHED_GRAPH_OVERLAY_OUTPUT_GRAPH_SHA256 = (
    "7a48d66419b246e2847cb95c8e226070c675cb19e8787cd47be2ef9729018a15"
)
RECOVERY_TRACE_TIMEOUTS = {
    "batch-invariance-b200": 120,
    "distributed-compile-unit-tests-2xh100": 120,
    "lm-eval-humming-act-a100": 90,
    "lm-eval-large-models-8xh200": 90,
    "model-executor": 150,
    "rayexecutorv2-4-gpus": 75,
}

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

# Full-fleet canary #84585 found these jobs fail only when the NVIDIA
# kernel-set collector initializes CUDA/CUPTI before their forked workers.
# Build #84375 completed every exact key in python-only mode. Preserve each
# current job's command/shard shape and Python line evidence while deliberately
# foregoing kernel evidence until subprocess-safe kernel collection is proven.
# The two explicit budgets cover jobs without a source timeout; all other rows
# keep the normal evidence-based timeout expansion below.
_FLEET_PYTHON_ONLY_TRACE_POLICY: dict[str, Optional[int]] = {
    "basic-correctness": None,
    "distributed-tests-2xb200": 42,
    "distributed-tests-4xa100": 24,
    "kernels-b200": None,
    "kernels-deepgemm-test-h100": None,
    "kernels-fusedmoe-layer-test-2-b200s": None,
    "language-models-tests-hybrid": None,
    "multi-modal-processor": None,
    "pytorch-compilation-unit-tests": None,
    "pytorch-fullgraph-test": None,
    "spec-decode-draft-model-nightly-b200": None,
    "spec-decode-eagle-nightly-b200": None,
    "spec-decode-speculators-mtp-nightly-b200": None,
    "speculators-correctness": None,
    "v1-sample-logits": None,
}

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
    """Trace vLLM jobs on a trusted nightly or exact mirror-branch canary."""

    return bool(
        global_config["github_repo_name"] == "vllm-project/vllm"
        and global_config["nightly"] == "1"
        and (
            global_config["branch"] == "main"
            or (
                global_config.get("trace_canary_branch") is not None
                and global_config["trace_canary_branch"] == global_config["branch"]
            )
        )
        and global_config["pull_request"] in (None, "false")
        and global_config["trace_s3_bucket"]
    )


def _recovery_trace_timeout_overrides(
    global_config: dict, selected_test_area_keys: Set[str]
) -> dict[str, int]:
    """Return frozen budgets for a nonempty subset of the eac recovery wave."""

    selected_keys = frozenset(selected_test_area_keys)
    recovery_keys = frozenset(RECOVERY_TRACE_TIMEOUTS)

    trusted = bool(
        global_config["github_repo_name"] == "vllm-project/vllm"
        and global_config["branch"] == RECOVERY_IMAGE_BRANCH
        and global_config["commit"] == RECOVERY_IMAGE_COMMIT
        and global_config.get("trace_canary_branch") == RECOVERY_IMAGE_BRANCH
        and global_config.get("trace_canary_commit") == RECOVERY_IMAGE_COMMIT
        and global_config["nightly"] == "1"
        and global_config["pull_request"] in (None, "false")
        and selected_keys
        and selected_keys <= recovery_keys
    )
    return (
        {key: RECOVERY_TRACE_TIMEOUTS[key] for key in selected_keys} if trusted else {}
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
    timeout_overrides: Optional[dict[str, int]] = None,
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
        force_python_only = step_key in _FLEET_PYTHON_ONLY_TRACE_POLICY
        timeout = step.timeout_in_minutes
        if timeout_overrides and step_key in timeout_overrides:
            timeout = timeout_overrides[step_key]
        elif timeout is not None:
            timeout = max(timeout + 15, ceil(timeout * 1.5))
        elif force_python_only:
            timeout = _FLEET_PYTHON_ONLY_TRACE_POLICY[step_key]
        mode = (
            "kernel-set"
            if not force_python_only
            and step.device in _NVIDIA_TRACE_DEVICES
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


def _snapshot_republish_requested() -> bool:
    return any(
        os.getenv(name) is not None
        for name in (
            REPUBLISH_INVENTORY_ENV,
            REPUBLISH_SOURCE_BUILD_ENV,
            REPUBLISH_SOURCE_BUILD_ID_ENV,
            REPUBLISH_TRIALS_ENV,
        )
    )


def _published_graph_overlay_requested() -> bool:
    return any(
        os.getenv(name) is not None
        for name in (
            PUBLISHED_GRAPH_OVERLAY_ENV,
            PUBLISHED_GRAPH_OVERLAY_VERIFY_ENV,
            PUBLISHED_GRAPH_OVERLAY_BASE_INVENTORY_ENV,
            PUBLISHED_GRAPH_OVERLAY_RETRY_INVENTORY_ENV,
        )
    )


def _published_graph_overlay_verification_requested() -> bool:
    return os.getenv(PUBLISHED_GRAPH_OVERLAY_VERIFY_ENV) is not None


def _recovery_image_copy_requested() -> bool:
    return os.getenv(RECOVERY_IMAGE_COPY_ENV) is not None


def _decode_published_graph_overlay_inventory(
    environment_variable: str, expected_sha256: str
) -> tuple[str, dict]:
    encoded = os.getenv(environment_variable)
    if not encoded:
        raise ValueError(f"{environment_variable} must be set")
    try:
        raw = base64.b64decode(encoded, validate=True)
        document = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{environment_variable} is invalid") from error
    if not isinstance(document, dict):
        raise ValueError(f"{environment_variable} must contain a JSON object")
    canonical = (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    if raw != canonical:
        raise ValueError(f"{environment_variable} must be canonical JSON")
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError(f"{environment_variable} checksum mismatch")
    return encoded, document


def _published_graph_overlay_inputs(global_config: dict) -> dict:
    if os.getenv(PUBLISHED_GRAPH_OVERLAY_ENV) != "1":
        raise ValueError(f"{PUBLISHED_GRAPH_OVERLAY_ENV} must equal 1")
    if (
        _recovery_image_copy_requested()
        or _snapshot_republish_requested()
        or global_config.get("only_step_keys") is not None
    ):
        raise ValueError(
            "published graph overlay cannot be combined with another recovery mode"
        )
    required = {
        "name": "vllm_ci",
        "github_repo_name": "vllm-project/vllm",
        "branch": RECOVERY_IMAGE_BRANCH,
        "commit": RECOVERY_IMAGE_COMMIT,
        "pull_request": "false",
        "nightly": "1",
        "trace_canary_branch": RECOVERY_IMAGE_BRANCH,
        "trace_canary_commit": RECOVERY_IMAGE_COMMIT,
        "trace_s3_bucket": PUBLISHED_GRAPH_OVERLAY_BUCKET,
        "trace_s3_prefix": PUBLISHED_GRAPH_OVERLAY_OUTPUT_PREFIX,
        "registries": RECOVERY_IMAGE_REGISTRY,
    }
    for field, expected in required.items():
        if global_config.get(field) != expected:
            raise ValueError(f"published graph overlay requires {field}={expected!r}")
    repositories = global_config.get("repositories") or {}
    if (
        repositories.get("main") != RECOVERY_IMAGE_SOURCE_REPO
        or repositories.get("premerge") != RECOVERY_IMAGE_DESTINATION_REPO
    ):
        raise ValueError("published graph overlay repository mapping is not trusted")
    requested_revision = os.getenv("VLLM_CI_BRANCH", "")
    revision = _ci_infra_revision()
    if not re.fullmatch(r"[0-9a-f]{40}", requested_revision):
        raise ValueError(
            "published graph overlay requires VLLM_CI_BRANCH as an exact SHA"
        )
    if requested_revision != revision:
        raise ValueError("published graph overlay generator revision does not match")

    base_encoded, base_inventory = _decode_published_graph_overlay_inventory(
        PUBLISHED_GRAPH_OVERLAY_BASE_INVENTORY_ENV,
        PUBLISHED_GRAPH_OVERLAY_BASE_INVENTORY_SHA256,
    )
    retry_encoded, retry_inventory = _decode_published_graph_overlay_inventory(
        PUBLISHED_GRAPH_OVERLAY_RETRY_INVENTORY_ENV,
        PUBLISHED_GRAPH_OVERLAY_RETRY_INVENTORY_SHA256,
    )
    if base_inventory.get("repository_sha") != RECOVERY_IMAGE_COMMIT:
        raise ValueError("published graph overlay base repository SHA mismatch")
    if retry_inventory.get("repository_sha") != RECOVERY_IMAGE_COMMIT:
        raise ValueError("published graph overlay retry repository SHA mismatch")
    if base_inventory.get("ci_infra_revision") != (
        "3c43f17714e9f59748992a7d76d64430f2c93779"
    ):
        raise ValueError("published graph overlay base ci-infra revision mismatch")
    if retry_inventory.get("ci_infra_revision") != (
        "ec4a54df07f82f0d1e62aaf199d80c7d90f97d10"
    ):
        raise ValueError("published graph overlay retry ci-infra revision mismatch")
    if base_inventory.get("collector_sha256") != (
        "ffe147610119438dcd36d624c8137f16014470f1ff590a70f23990c6e93f45e4"
    ):
        raise ValueError("published graph overlay base collector mismatch")
    if retry_inventory.get("collector_sha256") != (
        "00323b97a2fee832cf72c71b7ab4a84df4ca366eed44e7b47e7a1cb86eb29abe"
    ):
        raise ValueError("published graph overlay retry collector mismatch")
    base_jobs = {str(row.get("key")): row for row in base_inventory.get("jobs", [])}
    retry_jobs = {str(row.get("key")): row for row in retry_inventory.get("jobs", [])}
    expected_retry_jobs = set(PUBLISHED_GRAPH_OVERLAY_REPLACEMENTS) | set(
        PUBLISHED_GRAPH_OVERLAY_RETRY_MISSING
    )
    if len(base_jobs) != 121 or set(retry_jobs) != expected_retry_jobs:
        raise ValueError("published graph overlay inventory job set mismatch")
    if any(base_jobs.get(key) != row for key, row in retry_jobs.items()):
        raise ValueError("published graph overlay retry changes a base job policy")
    retry_wait = retry_inventory.get("wait_results", {})
    if any(
        retry_wait.get(key, {}).get("status") != "terminal"
        for key in PUBLISHED_GRAPH_OVERLAY_REPLACEMENTS
    ) or any(
        retry_wait.get(key, {}).get("status") != "poll_timeout"
        for key in PUBLISHED_GRAPH_OVERLAY_RETRY_MISSING
    ):
        raise ValueError("published graph overlay retry wait result mismatch")
    return {
        "base_inventory_base64": base_encoded,
        "retry_inventory_base64": retry_encoded,
        "revision": revision,
    }


def create_published_graph_overlay_group_step(
    global_config: dict,
) -> BuildkiteGroupStep:
    """Render the exact one-time published-base recovery and verifier."""

    inputs = _published_graph_overlay_inputs(global_config)
    revision = inputs["revision"]
    base_fetch_validation = shlex.quote(
        "import json,sys; d=json.load(open(sys.argv[1])); "
        f"assert d['manifest_key']=={PUBLISHED_GRAPH_OVERLAY_BASE_MANIFEST_KEY!r}; "
        f"assert d['manifest_sha256']=={PUBLISHED_GRAPH_OVERLAY_BASE_MANIFEST_SHA256!r}; "
        f"assert d['repository_sha']=={RECOVERY_IMAGE_COMMIT!r}"
    )
    final_validation = shlex.quote(
        "import json,sys; "
        "from pathlib import Path; "
        "from test_selection.graph import graph_metadata,sha256_file; "
        "overlay=json.load(open(sys.argv[1])); "
        "provenance=json.load(open(sys.argv[2])); "
        "publish=json.load(open(sys.argv[3])); "
        "readback=json.load(open(sys.argv[4])); "
        "merged=Path(sys.argv[5]); fetched=Path(sys.argv[6]); "
        "metadata=graph_metadata(fetched); "
        "assert sha256_file(merged)==sha256_file(fetched); "
        "assert metadata==overlay['metadata']; "
        "assert len(metadata['healthy_jobs'])==85; "
        "assert len(metadata['missing_jobs'])==4; "
        "assert len(metadata['unhealthy_jobs'])==32; "
        f"assert set({PUBLISHED_GRAPH_OVERLAY_REPLACEMENTS!r})<=set(metadata['healthy_jobs']); "
        f"assert set({PUBLISHED_GRAPH_OVERLAY_RETRY_MISSING!r})<=set(metadata['missing_jobs']); "
        f"assert provenance['replacement_jobs']==sorted({PUBLISHED_GRAPH_OVERLAY_REPLACEMENTS!r}); "
        f"assert provenance['base_manifest_key']=={PUBLISHED_GRAPH_OVERLAY_BASE_MANIFEST_KEY!r}; "
        f"assert provenance['base_manifest_sha256']=={PUBLISHED_GRAPH_OVERLAY_BASE_MANIFEST_SHA256!r}; "
        f"assert provenance['merge_revision']=={revision!r}; "
        "assert provenance['merged_graph_sha256']==sha256_file(merged); "
        f"assert publish['snapshot']['manifest_key']=={(PUBLISHED_GRAPH_OVERLAY_OUTPUT_PREFIX + '/snapshots/' + RECOVERY_IMAGE_COMMIT + '/manifest.json')!r}; "
        "assert readback['manifest_key']==publish['snapshot']['manifest_key']; "
        "assert readback['manifest_sha256']==publish['snapshot']['manifest_sha256']; "
        f"assert readback['repository_sha']=={RECOVERY_IMAGE_COMMIT!r}"
    )
    replacement_arguments = " ".join(
        f"--expected-replacement-job {shlex.quote(key)}"
        for key in PUBLISHED_GRAPH_OVERLAY_REPLACEMENTS
    )
    missing_arguments = " ".join(
        f"--expected-retry-missing-job {shlex.quote(key)}"
        for key in PUBLISHED_GRAPH_OVERLAY_RETRY_MISSING
    )
    command = [
        "set -euo pipefail",
        (
            "echo "
            + shlex.quote(
                "+++ :warning: PUBLISHED GRAPH OVERLAY recovery authorized for "
                f"{RECOVERY_IMAGE_BRANCH}@{RECOVERY_IMAGE_COMMIT}"
            )
        ),
        f'test "$$BUILDKITE_BRANCH" = {shlex.quote(RECOVERY_IMAGE_BRANCH)}',
        f'test "$$BUILDKITE_COMMIT" = {shlex.quote(RECOVERY_IMAGE_COMMIT)}',
        'D="$$(mktemp -d)"',
        "trap 'rm -rf \"$$D\"' EXIT",
        'mkdir -p "$$D/retry" "$$D/results"',
        (
            f"printf '%s' {shlex.quote(inputs['base_inventory_base64'])} | "
            'base64 -d > "$$D/base-inventory.json"'
        ),
        (
            f"printf '%s' {shlex.quote(inputs['retry_inventory_base64'])} | "
            'base64 -d > "$$D/retry-inventory.json"'
        ),
        (
            'test "$$(sha256sum "$$D/base-inventory.json" | awk '
            "'{print $$1}')\" = "
            f"{PUBLISHED_GRAPH_OVERLAY_BASE_INVENTORY_SHA256}"
        ),
        (
            'test "$$(sha256sum "$$D/retry-inventory.json" | awk '
            "'{print $$1}')\" = "
            f"{PUBLISHED_GRAPH_OVERLAY_RETRY_INVENTORY_SHA256}"
        ),
        'python3 -m venv "$$D/venv"',
        (
            '"$$D/venv/bin/pip" install --quiet '
            '"git+https://github.com/vllm-project/ci-infra.git@'
            f'{revision}#subdirectory=buildkite/pipeline_generator"'
        ),
        (
            '"$$D/venv/bin/vllm-test-selection" fetch-snapshot '
            f"--bucket {PUBLISHED_GRAPH_OVERLAY_BUCKET} "
            f"--prefix {PUBLISHED_GRAPH_OVERLAY_BASE_PREFIX} "
            f'--repo "$$PWD" --base {RECOVERY_IMAGE_COMMIT} '
            '--output "$$D/base.sqlite" --max-snapshot-age-days 7 '
            '> "$$D/results/base-fetch.json"'
        ),
        '"$$D/venv/bin/python" -m json.tool "$$D/results/base-fetch.json" >/dev/null',
        'cat "$$D/results/base-fetch.json"',
        (
            f'"$$D/venv/bin/python" -c {base_fetch_validation} '
            '"$$D/results/base-fetch.json"'
        ),
        (
            'buildkite-agent artifact download "trace-output/**/*" '
            f'"$$D/retry" --build {PUBLISHED_GRAPH_OVERLAY_RETRY_BUILD_ID}'
        ),
        (
            '"$$D/venv/bin/python" -m test_selection.published_overlay '
            '--base-graph "$$D/base.sqlite" '
            '--base-inventory "$$D/base-inventory.json" '
            '--retry-input "$$D/retry" '
            '--retry-inventory "$$D/retry-inventory.json" '
            '--output "$$D/merged.sqlite" '
            '--provenance-output "$$D/results/provenance.json" '
            f"--base-source-build-id {PUBLISHED_GRAPH_OVERLAY_BASE_BUILD_ID} "
            f"--retry-source-build-id {PUBLISHED_GRAPH_OVERLAY_RETRY_BUILD_ID} "
            f"--merge-revision {revision} "
            f"--base-manifest-key {PUBLISHED_GRAPH_OVERLAY_BASE_MANIFEST_KEY} "
            f"--base-manifest-sha256 {PUBLISHED_GRAPH_OVERLAY_BASE_MANIFEST_SHA256} "
            "--expected-base-healthy-count 82 "
            "--expected-base-missing-count 7 "
            "--expected-base-unhealthy-count 32 "
            f"{replacement_arguments} {missing_arguments} "
            '> "$$D/results/overlay.json"'
        ),
        '"$$D/venv/bin/python" -m json.tool "$$D/results/overlay.json" >/dev/null',
        'cat "$$D/results/overlay.json"',
        (
            '"$$D/venv/bin/vllm-test-selection" publish-graph '
            f"--bucket {PUBLISHED_GRAPH_OVERLAY_BUCKET} "
            f"--prefix {PUBLISHED_GRAPH_OVERLAY_OUTPUT_PREFIX} "
            '--graph "$$D/merged.sqlite" > "$$D/results/publish.json"'
        ),
        '"$$D/venv/bin/python" -m json.tool "$$D/results/publish.json" >/dev/null',
        'cat "$$D/results/publish.json"',
        (
            '"$$D/venv/bin/vllm-test-selection" fetch-snapshot '
            f"--bucket {PUBLISHED_GRAPH_OVERLAY_BUCKET} "
            f"--prefix {PUBLISHED_GRAPH_OVERLAY_OUTPUT_PREFIX} "
            f'--repo "$$PWD" --base {RECOVERY_IMAGE_COMMIT} '
            '--output "$$D/readback.sqlite" --max-snapshot-age-days 7 '
            '> "$$D/results/readback.json"'
        ),
        '"$$D/venv/bin/python" -m json.tool "$$D/results/readback.json" >/dev/null',
        'cat "$$D/results/readback.json"',
        (
            f'"$$D/venv/bin/python" -c {final_validation} '
            '"$$D/results/overlay.json" "$$D/results/provenance.json" '
            '"$$D/results/publish.json" "$$D/results/readback.json" '
            '"$$D/merged.sqlite" "$$D/readback.sqlite"'
        ),
        'sha256sum "$$D/merged.sqlite" > "$$D/results/merged.sqlite.sha256"',
        'sha256sum "$$D/readback.sqlite" > "$$D/results/readback.sqlite.sha256"',
        'cp "$$D/base-inventory.json" "$$D/results/"',
        'cp "$$D/retry-inventory.json" "$$D/results/"',
        f"printf '%s\n' {revision} > \"$$D/results/runner-revision.txt\"",
        'buildkite-agent artifact upload "$$D/results/*"',
    ]
    return BuildkiteGroupStep(
        group=":warning: Published graph overlay recovery",
        steps=[
            BuildkiteCommandStep(
                agents={"queue": AgentQueue.CPU_POSTMERGE_US_EAST_1.value},
                commands=["\n".join(command)],
                key="test-selection-published-graph-overlay",
                label=":warning: Merge #84714 graph + #84881 raw",
                priority=0,
                timeout_in_minutes=180,
            )
        ],
    )


def create_published_graph_overlay_verification_group_step(
    global_config: dict,
) -> BuildkiteGroupStep:
    """Render the one-time read-only verifier for the published overlay."""

    if os.getenv(PUBLISHED_GRAPH_OVERLAY_VERIFY_ENV) != "1":
        raise ValueError(f"{PUBLISHED_GRAPH_OVERLAY_VERIFY_ENV} must equal 1")
    inputs = _published_graph_overlay_inputs(global_config)
    revision = inputs["revision"]
    verification = shlex.quote(
        "import json,sys; "
        "metadata=json.load(open(sys.argv[1])); "
        "readback=json.load(open(sys.argv[2])); "
        f"assert readback['manifest_key']=={PUBLISHED_GRAPH_OVERLAY_OUTPUT_MANIFEST_KEY!r}; "
        f"assert readback['manifest_sha256']=={PUBLISHED_GRAPH_OVERLAY_OUTPUT_MANIFEST_SHA256!r}; "
        f"assert readback['repository_sha']=={RECOVERY_IMAGE_COMMIT!r}; "
        "assert len(metadata['healthy_jobs'])==85; "
        "assert len(metadata['missing_jobs'])==4; "
        "assert len(metadata['unhealthy_jobs'])==32; "
        f"assert set({PUBLISHED_GRAPH_OVERLAY_REPLACEMENTS!r})<=set(metadata['healthy_jobs']); "
        f"assert set({PUBLISHED_GRAPH_OVERLAY_RETRY_MISSING!r})<=set(metadata['missing_jobs'])"
    )
    command = [
        "set -euo pipefail",
        (
            "echo "
            + shlex.quote(
                "+++ :mag: READ-ONLY published graph overlay verification for "
                f"{RECOVERY_IMAGE_BRANCH}@{RECOVERY_IMAGE_COMMIT}"
            )
        ),
        f'test "$$BUILDKITE_BRANCH" = {shlex.quote(RECOVERY_IMAGE_BRANCH)}',
        f'test "$$BUILDKITE_COMMIT" = {shlex.quote(RECOVERY_IMAGE_COMMIT)}',
        'D="$$(mktemp -d)"',
        "trap 'rm -rf \"$$D\"' EXIT",
        'mkdir -p "$$D/results"',
        'python3 -m venv "$$D/venv"',
        (
            '"$$D/venv/bin/pip" install --quiet '
            '"git+https://github.com/vllm-project/ci-infra.git@'
            f'{revision}#subdirectory=buildkite/pipeline_generator"'
        ),
        (
            '"$$D/venv/bin/vllm-test-selection" fetch-snapshot '
            f"--bucket {PUBLISHED_GRAPH_OVERLAY_BUCKET} "
            f"--prefix {PUBLISHED_GRAPH_OVERLAY_OUTPUT_PREFIX} "
            f'--repo "$$PWD" --base {RECOVERY_IMAGE_COMMIT} '
            '--output "$$D/readback.sqlite" --max-snapshot-age-days 7 '
            '> "$$D/results/readback.json"'
        ),
        '"$$D/venv/bin/python" -m json.tool "$$D/results/readback.json" >/dev/null',
        'cat "$$D/results/readback.json"',
        (
            '"$$D/venv/bin/vllm-test-selection" inspect-graph '
            '--graph "$$D/readback.sqlite" --metadata '
            '> "$$D/results/metadata.json"'
        ),
        '"$$D/venv/bin/python" -m json.tool "$$D/results/metadata.json" >/dev/null',
        'cat "$$D/results/metadata.json"',
        ('sha256sum "$$D/readback.sqlite" > "$$D/results/readback.sqlite.sha256"'),
        (
            'test "$$(awk \'{print $$1}\' "$$D/results/readback.sqlite.sha256")" = '
            f"{PUBLISHED_GRAPH_OVERLAY_OUTPUT_GRAPH_SHA256}"
        ),
        (
            f'"$$D/venv/bin/python" -c {verification} '
            '"$$D/results/metadata.json" "$$D/results/readback.json"'
        ),
        f"printf '%s\n' {revision} > \"$$D/results/runner-revision.txt\"",
        'buildkite-agent artifact upload "$$D/results/*"',
    ]
    return BuildkiteGroupStep(
        group=":mag: Published graph overlay read-only verification",
        steps=[
            BuildkiteCommandStep(
                agents={"queue": AgentQueue.CPU_POSTMERGE_US_EAST_1.value},
                commands=["\n".join(command)],
                key="test-selection-published-overlay-readback",
                label=":mag: Verify published graph overlay",
                timeout_in_minutes=60,
            )
        ],
    )


def _validate_recovery_image_copy(global_config: dict) -> str:
    if os.getenv(RECOVERY_IMAGE_COPY_ENV) != "1":
        raise ValueError(f"{RECOVERY_IMAGE_COPY_ENV} must equal 1")
    if (
        _snapshot_republish_requested()
        or _published_graph_overlay_requested()
        or global_config.get("only_step_keys") is not None
    ):
        raise ValueError(
            "recovery image copy cannot be combined with another recovery mode"
        )
    required = {
        "name": "vllm_ci",
        "github_repo_name": "vllm-project/vllm",
        "branch": RECOVERY_IMAGE_BRANCH,
        "commit": RECOVERY_IMAGE_COMMIT,
        "pull_request": "false",
        "nightly": "1",
        "trace_canary_branch": RECOVERY_IMAGE_BRANCH,
        "trace_canary_commit": RECOVERY_IMAGE_COMMIT,
        "registries": RECOVERY_IMAGE_REGISTRY,
    }
    for field, expected in required.items():
        if global_config.get(field) != expected:
            raise ValueError(f"recovery image copy requires {field}={expected!r}")
    repositories = global_config.get("repositories") or {}
    if (
        repositories.get("main") != RECOVERY_IMAGE_SOURCE_REPO
        or repositories.get("premerge") != RECOVERY_IMAGE_DESTINATION_REPO
    ):
        raise ValueError("recovery image copy repository mapping is not trusted")
    requested_revision = os.getenv("VLLM_CI_BRANCH", "")
    revision = _ci_infra_revision()
    if not re.fullmatch(r"[0-9a-f]{40}", requested_revision):
        raise ValueError("recovery image copy requires VLLM_CI_BRANCH as an exact SHA")
    if requested_revision != revision:
        raise ValueError("recovery image copy generator revision does not match")
    return revision


def create_recovery_image_copy_group_step(global_config: dict) -> BuildkiteGroupStep:
    """Carbon-copy #84714's pinned images for the exact mirror recovery."""

    revision = _validate_recovery_image_copy(global_config)
    source = f"{RECOVERY_IMAGE_REGISTRY}/{RECOVERY_IMAGE_SOURCE_REPO}"
    destination = f"{RECOVERY_IMAGE_REGISTRY}/{RECOVERY_IMAGE_DESTINATION_REPO}"
    amd64_destination = f"{destination}:{RECOVERY_IMAGE_COMMIT}"
    arm64_destination = f"{destination}:{RECOVERY_IMAGE_COMMIT}-arm64"
    command = [
        "set -euo pipefail",
        (
            "echo "
            + shlex.quote(
                "+++ :warning: EXACT IMAGE COPY authorized for "
                f"{RECOVERY_IMAGE_BRANCH}@{RECOVERY_IMAGE_COMMIT}"
            )
        ),
        f'test "$$BUILDKITE_BRANCH" = {shlex.quote(RECOVERY_IMAGE_BRANCH)}',
        f'test "$$BUILDKITE_COMMIT" = {shlex.quote(RECOVERY_IMAGE_COMMIT)}',
        'D="$$(mktemp -d)"',
        "trap 'rm -rf \"$$D\"' EXIT",
        (
            "curl --fail --location --proto '=https' --tlsv1.2 "
            f'--output "$$D/docker-buildx" https://github.com/docker/buildx/'
            f"releases/download/{RECOVERY_BUILDX_VERSION}/"
            f"buildx-{RECOVERY_BUILDX_VERSION}.linux-amd64"
        ),
        (
            f"printf '%s  %s\\n' {RECOVERY_BUILDX_SHA256} "
            '"$$D/docker-buildx" | sha256sum -c -'
        ),
        'chmod 0755 "$$D/docker-buildx"',
        '"$$D/docker-buildx" version',
        (
            "aws ecr-public get-login-password --region us-east-1 | "
            f"docker login --username AWS --password-stdin {RECOVERY_IMAGE_REGISTRY}"
        ),
        (
            '"$$D/docker-buildx" imagetools create --prefer-index=false '
            f"--tag {amd64_destination} "
            f"{source}@{RECOVERY_IMAGE_AMD64_DIGEST}"
        ),
        (
            '"$$D/docker-buildx" imagetools create --prefer-index=false '
            f"--tag {arm64_destination} "
            f"{source}@{RECOVERY_IMAGE_ARM64_DIGEST}"
        ),
        "wait_for_digest() {",
        '  local image="$$1" expected="$$2" observed=""',
        "  for attempt in $$(seq 1 12); do",
        '    if "$$D/docker-buildx" imagetools inspect --raw "$$image" '
        '> "$$D/manifest.json" 2>/dev/null; then',
        (
            '      observed="sha256:$$(sha256sum "$$D/manifest.json" '
            "| awk '{print $$1}')\""
        ),
        "    fi",
        '    if [[ "$$observed" = "$$expected" ]]; then',
        '      printf \'%s\\t%s\\n\' "$$image" "$$observed"',
        "      return 0",
        "    fi",
        "    sleep 5",
        "  done",
        (
            '  echo "digest mismatch for $$image: expected $$expected, '
            'observed $$observed" >&2'
        ),
        "  return 1",
        "}",
        (
            f"wait_for_digest {amd64_destination} {RECOVERY_IMAGE_AMD64_DIGEST} "
            '| tee "$$D/image-copy-provenance.txt"'
        ),
        (
            f"wait_for_digest {arm64_destination} {RECOVERY_IMAGE_ARM64_DIGEST} "
            '| tee -a "$$D/image-copy-provenance.txt"'
        ),
        (
            f"printf 'ci_infra_revision\\t%s\\nsource_repository\\t%s\\n' "
            f'{revision} {source} >> "$$D/image-copy-provenance.txt"'
        ),
        'buildkite-agent artifact upload "$$D/image-copy-provenance.txt"',
    ]
    return BuildkiteGroupStep(
        group=":warning: Exact recovery image copy",
        steps=[
            BuildkiteCommandStep(
                agents={"queue": AgentQueue.CPU_PREMERGE_US_EAST_1.value},
                commands=["\n".join(command)],
                key="test-selection-recovery-image-copy",
                label=":warning: Copy pinned #84714 images",
                retry={"automatic": [{"exit_status": -1, "limit": 1}]},
                timeout_in_minutes=20,
            )
        ],
    )


def _snapshot_republish_inputs(global_config: dict) -> dict:
    if not should_trace_nightly(global_config):
        raise ValueError(
            "test-selection republish requires a trusted vLLM nightly or "
            "exact mirror-branch canary"
        )
    encoded_inventory = os.getenv(REPUBLISH_INVENTORY_ENV)
    source_build = os.getenv(REPUBLISH_SOURCE_BUILD_ENV)
    source_build_id = os.getenv(REPUBLISH_SOURCE_BUILD_ID_ENV)
    if not encoded_inventory or not source_build or not source_build_id:
        raise ValueError(
            f"{REPUBLISH_INVENTORY_ENV}, {REPUBLISH_SOURCE_BUILD_ENV}, and "
            f"{REPUBLISH_SOURCE_BUILD_ID_ENV} must all be set"
        )
    if not re.fullmatch(r"[1-9][0-9]*", source_build):
        raise ValueError(f"{REPUBLISH_SOURCE_BUILD_ENV} must be a build number")
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        source_build_id,
    ):
        raise ValueError(f"{REPUBLISH_SOURCE_BUILD_ID_ENV} must be a build UUID")
    try:
        inventory_bytes = base64.b64decode(encoded_inventory, validate=True)
        inventory = json.loads(inventory_bytes)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{REPUBLISH_INVENTORY_ENV} is invalid") from error
    if not isinstance(inventory, dict):
        raise ValueError("republish inventory must be an object")
    canonical_inventory = (
        json.dumps(inventory, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    if inventory_bytes != canonical_inventory:
        raise ValueError("republish inventory must be canonical JSON with a newline")
    repository_sha = str(inventory.get("repository_sha", ""))
    if repository_sha != global_config["commit"] or not re.fullmatch(
        r"[0-9a-f]{40}", repository_sha
    ):
        raise ValueError("republish inventory repository SHA must match the build")
    if inventory.get("schema_version") != 1:
        raise ValueError("republish inventory schema is unsupported")
    if not re.fullmatch(
        r"[0-9a-f]{40}", str(inventory.get("ci_infra_revision", ""))
    ) or not re.fullmatch(r"[0-9a-f]{64}", str(inventory.get("collector_sha256", ""))):
        raise ValueError("republish inventory identity is invalid")
    jobs = inventory.get("jobs")
    always_run = inventory.get("always_run")
    wait_results = inventory.get("wait_results")
    if (
        not isinstance(jobs, list)
        or not jobs
        or not isinstance(always_run, list)
        or not isinstance(wait_results, dict)
    ):
        raise ValueError("republish inventory accounting is invalid")
    if not all(
        isinstance(row, dict)
        and re.fullmatch(r"[a-zA-Z0-9_-]+", str(row.get("key", "")))
        and isinstance(row.get("expected_shards"), int)
        and not isinstance(row.get("expected_shards"), bool)
        and row["expected_shards"] > 0
        and row.get("mode") in ("python-only", "kernel-set")
        for row in jobs
    ) or not all(
        isinstance(row, dict)
        and re.fullmatch(r"[a-zA-Z0-9_-]+", str(row.get("key", "")))
        and isinstance(row.get("reason"), str)
        and row["reason"]
        for row in always_run
    ):
        raise ValueError("republish inventory job rows are invalid")
    job_keys = [str(row["key"]) for row in jobs]
    current_jobs = sorted(job_keys + [str(row["key"]) for row in always_run])
    if len(current_jobs) != len(set(current_jobs)):
        raise ValueError("republish inventory contains duplicate job keys")
    if set(wait_results) != set(job_keys) or not all(
        isinstance(result, dict)
        and result.get("status") in ("terminal", "poll_timeout")
        for result in wait_results.values()
    ):
        raise ValueError("republish inventory wait results are incomplete")

    trials_text = os.getenv(REPUBLISH_TRIALS_ENV, "[]")
    try:
        trials = json.loads(trials_text)
    except json.JSONDecodeError as error:
        raise ValueError(f"{REPUBLISH_TRIALS_ENV} is invalid") from error
    if not isinstance(trials, list) or len(trials) > 10:
        raise ValueError("republish trials must be a list of at most 10 entries")
    normalized_trials = []
    for row in trials:
        if not isinstance(row, dict):
            raise ValueError("republish trial entry must be an object")
        pull_request = row.get("pull_request")
        head = str(row.get("head", ""))
        if (
            not isinstance(pull_request, int)
            or isinstance(pull_request, bool)
            or pull_request < 1
            or not re.fullmatch(r"[0-9a-f]{40}", head)
        ):
            raise ValueError("republish trial entry is invalid")
        normalized_trials.append({"head": head, "pull_request": pull_request})
    if len({row["pull_request"] for row in normalized_trials}) != len(
        normalized_trials
    ):
        raise ValueError("republish trials contain duplicate pull requests")

    current_jobs_bytes = (
        json.dumps(current_jobs, separators=(",", ":")) + "\n"
    ).encode()
    return {
        "current_jobs_base64": base64.b64encode(current_jobs_bytes).decode(),
        "current_jobs_sha256": hashlib.sha256(current_jobs_bytes).hexdigest(),
        "inventory_base64": base64.b64encode(inventory_bytes).decode(),
        "inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
        "repository_sha": repository_sha,
        "source_build": source_build,
        "source_build_id": source_build_id,
        "trials": normalized_trials,
    }


def create_snapshot_republish_group_step(global_config: dict) -> BuildkiteGroupStep:
    """Render one fail-closed cross-build snapshot republisher and verifier."""

    inputs = _snapshot_republish_inputs(global_config)
    revision = _ci_infra_revision()
    bucket = shlex.quote(str(global_config["trace_s3_bucket"]))
    prefix = shlex.quote(str(global_config["trace_s3_prefix"]))
    source_build = inputs["source_build"]
    source_build_id = inputs["source_build_id"]
    repository_sha = inputs["repository_sha"]
    canary_branch = global_config.get("trace_canary_branch")
    canary_commit = global_config.get("trace_canary_commit")
    if canary_branch:
        canary_identity = f"{canary_branch}@{canary_commit}"
        group = f":warning: SNAPSHOT REPUBLISH CANARY {canary_identity}"
        label = f":warning: Republish CANARY snapshot ({canary_identity})"
        canary_banner = "echo " + shlex.quote(
            "+++ :warning: SNAPSHOT REPUBLISH CANARY authorized for " + canary_identity
        )
    else:
        group = "Test selection snapshot republish"
        label = ":database: Republish test-selection snapshot"
        canary_banner = None
    command = [
        "set -euo pipefail",
        *([canary_banner] if canary_banner else []),
        f'test "$$BUILDKITE_COMMIT" = {shlex.quote(repository_sha)}',
        f'test "$$BUILDKITE_BUILD_NUMBER" != {shlex.quote(source_build)}',
        'D="$$(mktemp -d)"',
        "trap 'rm -rf \"$$D\"' EXIT",
        'mkdir -p "$$D/evidence" "$$D/results"',
        (
            f"printf '%s' {shlex.quote(inputs['inventory_base64'])} | base64 -d "
            '> "$$D/inventory.json"'
        ),
        (
            'test "$$(sha256sum "$$D/inventory.json" | awk \'{print $$1}\')" = '
            f"{inputs['inventory_sha256']}"
        ),
        (
            f"printf '%s' {shlex.quote(inputs['current_jobs_base64'])} | base64 -d "
            '> "$$D/results/current-jobs.json"'
        ),
        (
            'test "$$(sha256sum "$$D/results/current-jobs.json" | '
            "awk '{print $$1}')\" = "
            f"{inputs['current_jobs_sha256']}"
        ),
        'python3 -m venv "$$D/venv"',
        (
            '"$$D/venv/bin/pip" install --quiet '
            '"git+https://github.com/vllm-project/ci-infra.git@'
            f'{revision}#subdirectory=buildkite/pipeline_generator"'
        ),
        (
            'buildkite-agent artifact download "trace-output/**/*" '
            f'"$$D/evidence" --build {shlex.quote(source_build_id)}'
        ),
        (
            '"$$D/venv/bin/vllm-test-selection" publish-snapshot '
            '--input "$$D/evidence" --inventory "$$D/inventory.json" '
            f'--bucket {bucket} --prefix {prefix} | tee "$$D/results/publish.json"'
        ),
        (
            '"$$D/venv/bin/vllm-test-selection" fetch-snapshot '
            f'--bucket {bucket} --prefix {prefix} --repo "$$PWD" '
            f'--base {repository_sha} --output "$$D/readback.sqlite" '
            '--max-snapshot-age-days 1 | tee "$$D/results/fetch.json"'
        ),
        (
            '"$$D/venv/bin/vllm-test-selection" inspect-graph '
            '--graph "$$D/readback.sqlite" --metadata | '
            'tee "$$D/results/metadata.json"'
        ),
        'sha256sum "$$D/readback.sqlite" | tee "$$D/results/readback.sha256"',
        'cp "$$D/inventory.json" "$$D/results/source-inventory.json"',
        'cp "$$D/readback.sqlite.sha256" "$$D/results/"',
        f"printf '%s\\n' {revision} > \"$$D/results/publisher-revision.txt\"",
        f"printf '%s\\n' {source_build} > \"$$D/results/source-build.txt\"",
        (f"printf '%s\\n' {source_build_id} > \"$$D/results/source-build-id.txt\""),
        "TRIAL_STATUS=0",
    ]
    for trial in inputs["trials"]:
        pull_request = trial["pull_request"]
        head = trial["head"]
        remote = f"origin/pr-{pull_request}"
        command.extend(
            [
                (
                    "git fetch --force origin "
                    f"refs/pull/{pull_request}/head:refs/remotes/{remote}"
                ),
                f'test "$$(git rev-parse {remote})" = {head}',
                (
                    '"$$D/venv/bin/vllm-test-selection" select '
                    '--graph "$$D/readback.sqlite" --repo "$$PWD" '
                    f"--base {repository_sha} --head {remote} "
                    '--current-jobs "$$D/results/current-jobs.json" '
                    f'> "$$D/results/pr-{pull_request}.json" || TRIAL_STATUS=$$?'
                ),
            ]
        )
    command.extend(
        [
            'buildkite-agent artifact upload "$$D/results/*"',
            'test "$$TRIAL_STATUS" -eq 0',
        ]
    )
    return BuildkiteGroupStep(
        group=group,
        steps=[
            BuildkiteCommandStep(
                agents={"queue": AgentQueue.CPU_POSTMERGE_US_EAST_1.value},
                commands=["\n".join(command)],
                key="test-selection-snapshot-republish",
                label=label,
                priority=-100,
                retry={"automatic": [{"exit_status": -1, "limit": 1}]},
                timeout_in_minutes=180,
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
    canary_branch = global_config.get("trace_canary_branch")
    canary_commit = global_config.get("trace_canary_commit")
    if canary_branch:
        canary_identity = f"{canary_branch}@{canary_commit}"
        group = f":warning: TEST-SELECTION CANARY {canary_identity}"
        label = f":warning: Publish CANARY snapshot ({canary_identity})"
        canary_banner = "echo " + shlex.quote(
            "+++ :warning: TEST-SELECTION CANARY authorized for " + canary_identity
        )
    else:
        group = "Test selection snapshot"
        label = ":database: Publish test-selection snapshot"
        canary_banner = None
    command = "\n".join(
        [
            "set -euo pipefail",
            *([canary_banner] if canary_banner else []),
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
