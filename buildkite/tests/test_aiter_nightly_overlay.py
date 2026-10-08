import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "aiter-nightly-overlay.sh"
BASH = shutil.which("bash")

STOCK = "rocm/vllm-dev:ci_base-build-42@sha256:" + "a" * 64
TAG = "rocm/vllm-dev:ci_base-build-42"
DIGEST = "sha256:" + "b" * 64
WHEEL = "https://example.com/amd_aiter-0.1.25+rocm7.2.3.torch2.12.bcb56d9.d20260928-cp312-cp312-linux_x86_64.whl"
NOTE = ":crescent_moon: **AITER nightly**: main at bcb56d9"

# Meta-data is a file per key; docker calls are logged. `docker run` replays
# what select_aiter_nightly_wheel.py printed and exited with: $STATE/select.*
STUBS = {
    "buildkite-agent": r"""#!/bin/bash
set -euo pipefail
case "$1 ${2:-}" in
  "meta-data get")
    if [[ -f "$STATE/meta/$3" ]]; then cat "$STATE/meta/$3"
    elif [[ "${4:-}" == "--default" ]]; then printf '%s' "$5"
    else exit 1; fi ;;
  "meta-data set") printf '%s' "$4" > "$STATE/meta/$3" ;;
  annotate*) cat > "$STATE/annotation.$3" ;;
esac
""",
    "docker": r"""#!/bin/bash
echo "$*" >> "$STATE/docker.log"
case "$1 ${2:-}" in
  "buildx build") cp "${@: -1}/Dockerfile" "$STATE/"; exit "$(cat "$STATE/build.status")" ;;
  "buildx imagetools") echo "Digest: $DIGEST" ;;
  "run "*)
    cat "$STATE/select.out"; cat "$STATE/select.err" >&2
    exit "$(cat "$STATE/select.status")" ;;
esac
""",
    "curl": r"""#!/bin/bash
while [[ $# -gt 0 ]]; do [[ "$1" == "-o" ]] && touch "$2"; shift; done
""",
}


@pytest.fixture
def agent(tmp_path):
    if not BASH:
        pytest.skip("bash is not available")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        (bin_dir / name).write_text(body, newline="\n")
        (bin_dir / name).chmod(0o755)
    (tmp_path / "meta").mkdir()

    class Agent:
        def set(self, name, value):
            (tmp_path / name).write_text(str(value))

        def get(self, name):
            path = tmp_path / name
            return path.read_text() if path.exists() else None

        def select(self, status=0, out="", err=""):
            """What select_aiter_nightly_wheel.py prints and exits with."""
            for suffix, value in (("status", status), ("out", out), ("err", err)):
                self.set(f"select.{suffix}", value)

        def run(self):
            self.set("docker.log", "")
            env = dict(
                os.environ,
                PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                STATE=str(tmp_path),
                DIGEST=DIGEST,
                BUILDKITE_BUILD_ID="42",
                VLLM_CI_BRANCH="my-branch",
            )
            result = subprocess.run(
                [BASH, str(SCRIPT)], env=env, capture_output=True, text=True
            )
            result.docker = self.get("docker.log").splitlines()
            return result

    agent = Agent()
    agent.set("meta/rocm-ci-base-image", STOCK)
    agent.set("build.status", 0)
    agent.select(out=f"{WHEEL}\n{NOTE}\n")
    return agent


def test_passing_run(agent):
    result = agent.run()

    assert result.returncode == 0, result.stderr
    # Select in the stock ci_base, install the wheel over it, push the result.
    assert [line.split()[0] for line in result.docker] == [
        "pull", "run", "buildx", "push", "buildx",
    ]  # fmt: skip
    assert result.docker[1].endswith(f"{STOCK} -P -")
    assert f"--build-arg CI_BASE_IMAGE={STOCK}" in result.docker[2]
    assert f"--build-arg AITER_WHEEL_URL={WHEEL}" in result.docker[2]
    pip = 'RUN python3 -m pip install --no-cache-dir "$AITER_WHEEL_URL"'
    assert pip in agent.get("Dockerfile")
    assert result.docker[3] == f"push {TAG}"
    assert agent.get("meta/aiter-nightly-wheel-url") == WHEEL
    assert agent.get("meta/aiter-nightly-ci-base") == f"{TAG}@{DIGEST}"
    assert agent.get("annotation.info") == NOTE + "\n"


def test_a_retry_reselects_the_first_attempts_wheel(agent):
    agent.set("meta/aiter-nightly-wheel-url", WHEEL)
    assert agent.run().docker[1].endswith(f" -P - --wheel-url {WHEEL}")


def test_a_stale_wheel_passes_with_a_warning_annotation(agent):
    note = f":warning: **AITER nightly is 9 days old**\n\n{NOTE}"
    agent.select(out=f"{WHEEL}\n{note}\n")
    assert agent.run().returncode == 0
    assert agent.get("annotation.warning") == note + "\n"
    assert agent.get("annotation.info") is None


@pytest.mark.parametrize(
    ("status", "title", "detail"),
    [
        (10, "no wheel for this image", "No AITER nightly for rocm7.3.0"),
        (11, "the wheel does not install", WHEEL.rsplit("/", 1)[-1]),
        # A crash: the annotation shows the end of stderr.
        (1, "could not select a wheel", "Traceback"),
    ],
)
def test_each_failure_has_its_own_exit_status_and_annotation(
    agent, status, title, detail
):
    if status == 11:
        agent.set("build.status", 1)
    else:
        agent.select(status, out="No AITER nightly for rocm7.3.0\n", err="Traceback\n")

    result = agent.run()

    assert result.returncode == status
    annotation = agent.get("annotation.error")
    assert f"**AITER nightly: {title}**" in annotation
    assert detail in annotation
    assert not any(line.startswith("push") for line in result.docker)
    assert agent.get("meta/aiter-nightly-ci-base") is None
