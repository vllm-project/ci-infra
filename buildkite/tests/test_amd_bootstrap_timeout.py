import os
import stat
import subprocess
from pathlib import Path
from typing import Dict, List

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_AMD = REPO_ROOT / "buildkite" / "bootstrap-amd.sh"


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_bootstrap(tmp_path: Path, file_diff: List[str]) -> Dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture_path = tmp_path / "minijinja-args"

    _write_executable(
        fake_bin / "git",
        """#!/usr/bin/env bash
case "$1" in
    config | fetch)
        exit 0
        ;;
    rev-parse)
        if [[ "$2" == "--is-shallow-repository" ]]; then
            echo false
        elif [[ "$2" == "--verify" ]]; then
            exit 0
        else
            exit 1
        fi
        ;;
    merge-base)
        echo test-merge-base
        ;;
    diff)
        printf '%s\n' "${TEST_FILE_DIFF:-}"
        ;;
    *)
        echo "Unexpected git invocation: $*" >&2
        exit 1
        ;;
esac
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
output_file=""
while (($#)); do
    if [[ "$1" == "-o" ]]; then
        output_file="$2"
        shift 2
        continue
    fi
    shift
done

if [[ -n "$output_file" ]]; then
    : > "$output_file"
else
    printf ':\n'
fi
""",
    )
    _write_executable(
        fake_bin / "minijinja-cli",
        """#!/usr/bin/env bash
printf '%s\n' "$@" > "${TEST_CAPTURE_PATH:?}"
printf 'steps: []\n'
""",
    )
    _write_executable(
        fake_bin / "buildkite-agent",
        """#!/usr/bin/env bash
exit 0
""",
    )

    buildkite_dir = tmp_path / ".buildkite"
    buildkite_dir.mkdir()
    (buildkite_dir / "test-amd.yaml").write_text("steps: []\n")

    cargo_dir = tmp_path / "cargo"
    cargo_dir.mkdir()
    (cargo_dir / "env").write_text(":\n")

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "TEST_CAPTURE_PATH": str(capture_path),
            "TEST_FILE_DIFF": "\n".join(file_diff),
            "CARGO_HOME": str(cargo_dir),
            "BUILDKITE_BRANCH": "feature",
            "BUILDKITE_COMMIT": "test-commit",
            "BUILDKITE_PIPELINE_SLUG": "amd-ci",
            "BUILDKITE_PULL_REQUEST": "false",
            "BUILDKITE_PULL_REQUEST_BASE_BRANCH": "main",
            "RUN_ALL": "0",
            "NIGHTLY": "0",
            "TORCH_NIGHTLY": "0",
            "DOCS_ONLY_DISABLE": "1",
            "VLLM_CI_BRANCH": "main",
            "AMD_MIRROR_HW": "amdproduction",
            "COV_ENABLED": "0",
        }
    )
    env.pop("MERGE_BASE_COMMIT", None)
    env.pop("VLLM_USE_PRECOMPILED", None)

    subprocess.run(
        ["bash", str(BOOTSTRAP_AMD)],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    definitions = {}
    for argument in capture_path.read_text().splitlines():
        if "=" in argument:
            key, value = argument.split("=", 1)
            definitions[key] = value
    return definitions


@pytest.mark.parametrize(
    ("file_diff", "expected_definitions"),
    [
        (
            ["vllm/config.py"],
            {
                "list_file_diff": "vllm/config.py",
                "run_all": "0",
                "nightly": "0",
            },
        ),
        (
            ["docker/Dockerfile.rocm"],
            {
                "list_file_diff": "run_all",
                "run_all": "1",
                "nightly": "0",
            },
        ),
        (
            [
                "docker/Dockerfile.rocm",
                "docker/Dockerfile.rocm_base",
            ],
            {
                "list_file_diff": ("run_all|docker/Dockerfile.rocm_base"),
                "run_all": "1",
                "nightly": "0",
            },
        ),
        (
            ["docker/Dockerfile.rocm_base"],
            {
                "list_file_diff": ("nightly|docker/Dockerfile.rocm_base"),
                "run_all": "0",
                "nightly": "1",
            },
        ),
    ],
)
def test_bootstrap_preserves_rocm_base_marker_for_minijinja(
    tmp_path, file_diff, expected_definitions
):
    definitions = _run_bootstrap(tmp_path, file_diff)

    assert {
        key: definitions[key] for key in expected_definitions
    } == expected_definitions
