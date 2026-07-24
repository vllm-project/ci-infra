import subprocess
from pathlib import Path
from typing import Tuple

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "buildkite" / "bootstrap-amd.sh"
AMD_TEMPLATE = REPO_ROOT / "buildkite" / "test-template-amd.j2"
ROCM_BASE_DOCKERFILE = "docker/Dockerfile.rocm_base"


def _classify_with_bootstrap(
    file_diff: str, *, run_all: str = "0", nightly: str = "0"
) -> Tuple[int, int, int, str]:
    script = r"""
source "$1"
RUN_ALL="$2"
NIGHTLY="$3"
classify_amd_changes "$4"
LIST_FILE_DIFF=$(build_amd_list_file_diff "$4")
printf 'STATE=%s,%s,%s,%s\n' \
    "$RUN_ALL" \
    "$NIGHTLY" \
    "$ROCM_BASE_DOCKERFILE_CHANGED" \
    "$LIST_FILE_DIFF"
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            script,
            "bootstrap-amd-test",
            str(BOOTSTRAP),
            run_all,
            nightly,
            file_diff,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    state_line = next(
        line for line in result.stdout.splitlines() if line.startswith("STATE=")
    )
    run_all_value, nightly_value, changed_value, list_file_diff = (
        state_line[len("STATE=") :].split(",", maxsplit=3)
    )
    return (
        int(run_all_value),
        int(nightly_value),
        int(changed_value),
        list_file_diff,
    )


@pytest.mark.parametrize(
    ("file_diff", "expected"),
    [
        (
            f"CMakeLists.txt\n{ROCM_BASE_DOCKERFILE}",
            (1, 1, 1, f"run_all|{ROCM_BASE_DOCKERFILE}"),
        ),
        (
            f"{ROCM_BASE_DOCKERFILE}\nCMakeLists.txt",
            (1, 1, 1, f"run_all|{ROCM_BASE_DOCKERFILE}"),
        ),
        (
            ROCM_BASE_DOCKERFILE,
            (0, 1, 1, f"nightly|{ROCM_BASE_DOCKERFILE}"),
        ),
        (
            "csrc/cpu/test.cpp\r\nvllm/config.py",
            (0, 0, 0, "csrc/cpu/test.cpp|vllm/config.py"),
        ),
    ],
)
def test_bootstrap_classifies_full_and_nightly_coverage(file_diff, expected):
    assert _classify_with_bootstrap(file_diff) == expected


def _render_amd_group_steps(
    *,
    file_diff: str = "",
    skip: str = "0",
    force: str = "0",
    diff_unavailable: str = "0",
):
    jinja2 = pytest.importorskip("jinja2")
    environment = jinja2.Environment(
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    environment.filters["split"] = lambda value, separator: value.split(separator)
    template = environment.from_string(AMD_TEMPLATE.read_text())
    rendered = template.render(
        steps=[],
        branch="feature",
        list_file_diff=file_diff,
        run_all="0",
        nightly="0",
        torch_nightly="0",
        mirror_hw="amdproduction",
        fail_fast="true",
        vllm_use_precompiled="0",
        vllm_merge_base_commit="HEAD",
        cov_enabled="0",
        vllm_ci_branch="main",
        rocm_base_refresh_skip=skip,
        rocm_base_refresh_force=force,
        rocm_base_refresh_diff_unavailable=diff_unavailable,
    )
    pipeline = yaml.safe_load(rendered)
    return pipeline[0]["steps"]


@pytest.mark.parametrize(
    ("file_diff", "skip", "force", "diff_unavailable", "expected_timeout"),
    [
        ("", "0", "0", "0", 15),
        (ROCM_BASE_DOCKERFILE, "0", "0", "0", 540),
        ("", "0", "1", "0", 540),
        ("", "0", "0", "1", 540),
        (ROCM_BASE_DOCKERFILE, "1", "1", "1", 15),
    ],
)
def test_template_selects_safe_rocm_base_refresh_timeout(
    file_diff, skip, force, diff_unavailable, expected_timeout
):
    steps = _render_amd_group_steps(
        file_diff=file_diff,
        skip=skip,
        force=force,
        diff_unavailable=diff_unavailable,
    )
    refresh_step = next(
        step for step in steps if step.get("key") == "refresh-rocm-base-amd"
    )

    assert refresh_step["timeout_in_minutes"] == expected_timeout
    assert refresh_step["env"]["ROCM_BASE_REFRESH_SKIP"] == skip
    assert refresh_step["env"]["ROCM_BASE_REFRESH_FORCE"] == force
    assert all(
        step.get("timeout_in_minutes", 0) <= 180
        for step in steps
        if step.get("key") != "refresh-rocm-base-amd"
    )
