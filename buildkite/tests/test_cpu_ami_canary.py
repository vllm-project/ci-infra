"""Behavioral tests for the opt-in CANARY_STACK_VERSION canary AMI build path.

Covers the canary/default selection in the build command of
.buildkite/pipelines/rebuild-cpu-ami.yml (by executing it), the cleanup AMI
filter (by matching real names against the filter the pipeline uses), and the
base-agent version guard in packer/agent/install-agent.sh (by running it
against a fake stable agent, with stub curl/uname proving whether the install
path was reached).

The Buildkite step conditions are validated semantically by the testplan's
real canary run, not re-checked here.
"""

import fnmatch
import os
import re
import stat
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parents[2]
PIPELINE = yaml.safe_load(
    (REPO_ROOT / ".buildkite/pipelines/rebuild-cpu-ami.yml").read_text()
)
STEPS = {step["key"]: step for step in PIPELINE["steps"]}
BUILD_COMMAND = STEPS["build-ami"]["command"][0]
INSTALL_AGENT = REPO_ROOT / "packer/agent/install-agent.sh"

V6_AMI_PREFIX = "vllm-buildkite-stack-linux-cpu-build"
CANARY_AMI_PREFIX = "vllm-buildkite-stack-canary-linux-cpu-build"
PINNED_AGENT_VERSION = "3.138.2"


def canary_selection_fragment():
    """The canary/default selection snippet from the build-ami command.

    Buildkite unescapes $$ to $ before the shell runs, so do the same here.
    """
    start = BUILD_COMMAND.index(f'AMI_NAME_PREFIX="{V6_AMI_PREFIX}"')
    end = BUILD_COMMAND.index("cd packer/cpu")
    return BUILD_COMMAND[start:end].replace("$$", "$")


def run_canary_selection(env):
    script = (
        "set -euo pipefail\n"
        + canary_selection_fragment()
        + '\necho "$STACK_VERSION|$AMI_NAME_PREFIX"'
    )
    return subprocess.run(
        ["bash", "-c", script],
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
    )


def assert_selection(env, expected):
    result = run_canary_selection(env)
    assert result.returncode == 0, result.stderr
    # The canary branch prints a banner before the values; take the last line.
    assert result.stdout.strip().splitlines()[-1] == expected


def test_commands_have_no_bare_dollar():
    """Buildkite interpolates commands on upload, and a "$" that starts no
    variable fails the whole upload (canary build 458). Shell syntax such as a
    regex end anchor must be written "$$"; bash alone would accept it."""
    bare_dollar_lines = []
    for step in PIPELINE["steps"]:
        for command in step.get("command", []):
            for line in command.splitlines():
                if re.search(r"(?<!\$)\$(?![${A-Za-z_$])", line):
                    bare_dollar_lines.append(line.strip())
    assert bare_dollar_lines == []


def test_canary_selection_defaults_match_daily_v6():
    assert_selection({"STACK_VERSION": "6.21.0"}, f"6.21.0|{V6_AMI_PREFIX}")


def test_canary_selection_with_empty_canary_version():
    assert_selection(
        {"STACK_VERSION": "6.21.0", "CANARY_STACK_VERSION": ""},
        f"6.21.0|{V6_AMI_PREFIX}",
    )


def test_canary_selection_with_canary_version():
    assert_selection(
        {"STACK_VERSION": "6.21.0", "CANARY_STACK_VERSION": "7.2.0"},
        f"7.2.0|{CANARY_AMI_PREFIX}",
    )


def test_canary_selection_rejects_malformed_version():
    result = run_canary_selection(
        {"STACK_VERSION": "6.21.0", "CANARY_STACK_VERSION": "v7.2.0"}
    )
    assert result.returncode != 0
    assert "CANARY_STACK_VERSION" in result.stdout + result.stderr
    # Fails before any build/download: the canary prefix is never selected.
    assert CANARY_AMI_PREFIX not in result.stdout


def test_canary_ami_name_escapes_cleanup_filter():
    command = STEPS["cleanup-amis"]["command"][0]
    cleanup_filter = re.search(r"Values=([^\"]+)", command).group(1)
    canary_name = f"{CANARY_AMI_PREFIX}-20260101000000"
    v6_name = f"{V6_AMI_PREFIX}-20260101000000"
    assert not fnmatch.fnmatchcase(canary_name, cleanup_filter)
    assert fnmatch.fnmatchcase(v6_name, cleanup_filter)


def run_install_agent(tmp_path, fake_agent, require_exact=False):
    """Run install-agent.sh with a fake stable agent and stub curl/uname.

    fake_agent is the body of the fake agent script, or None for an absent
    stable agent. require_exact passes REQUIRE_EXACT_AGENT_VERSION=true (the
    AGENT_ONLY release-bake mode). The stub curl exits 1 and logs its
    invocation, so reaching the download step fails the script and leaves
    evidence in curl.log. No network, no host /usr/bin.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl_log = tmp_path / "curl.log"
    curl_stub = bin_dir / "curl"
    curl_stub.write_text(f'#!/bin/bash\necho called >> "{curl_log}"\nexit 1\n')
    curl_stub.chmod(curl_stub.stat().st_mode | stat.S_IXUSR)
    # The script only knows Linux arch names; pretend to be a Linux target.
    uname_stub = bin_dir / "uname"
    uname_stub.write_text("#!/bin/bash\necho aarch64\n")
    uname_stub.chmod(uname_stub.stat().st_mode | stat.S_IXUSR)

    if fake_agent is None:
        stable_agent = tmp_path / "no-such-agent"
    else:
        stable_agent = tmp_path / "buildkite-agent-stable"
        stable_agent.write_text(fake_agent)
        stable_agent.chmod(stable_agent.stat().st_mode | stat.S_IXUSR)

    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "BUILDKITE_AGENT_STABLE_PATH": str(stable_agent),
    }
    if require_exact:
        env["REQUIRE_EXACT_AGENT_VERSION"] = "true"
    result = subprocess.run(
        ["bash", str(INSTALL_AGENT)], env=env, capture_output=True, text=True
    )
    return result, curl_log.exists()


def version_agent(version, secrets_ok=True):
    return (
        "#!/bin/bash\n"
        'if [[ "$1" == "--version" ]]; then\n'
        f'  echo "buildkite-agent version {version}, build x"\n'
        "  exit 0\n"
        "fi\n"
        f"exit {0 if secrets_ok else 1}\n"
    )


def test_newer_base_agent_is_kept(tmp_path):
    result, install_reached = run_install_agent(tmp_path, version_agent("4.1.0"))
    assert result.returncode == 0, result.stderr
    assert "4.1.0" in result.stdout
    assert not install_reached


def test_same_base_agent_is_kept(tmp_path):
    result, install_reached = run_install_agent(
        tmp_path, version_agent(PINNED_AGENT_VERSION)
    )
    assert result.returncode == 0, result.stderr
    assert not install_reached


def test_base_agent_without_secrets_cli_is_not_kept(tmp_path):
    # A same-or-newer agent whose Secrets CLI check fails must fail the build,
    # not silently keep an agent that breaks step-level secrets.
    result, install_reached = run_install_agent(
        tmp_path, version_agent("4.1.0", secrets_ok=False)
    )
    assert result.returncode != 0
    assert not install_reached


def test_failing_version_output_falls_back_to_install(tmp_path):
    agent = '#!/bin/bash\nif [[ "$1" == "--version" ]]; then exit 1; fi\nexit 0\n'
    result, install_reached = run_install_agent(tmp_path, agent)
    assert install_reached
    assert result.returncode != 0  # stops at the stubbed download


def test_failing_version_with_output_falls_back_to_install(tmp_path):
    # A version probe that fails is not trusted even if it printed a version.
    agent = (
        "#!/bin/bash\n"
        'if [[ "$1" == "--version" ]]; then echo "buildkite-agent version 4.1.0"; exit 1; fi\n'
        "exit 0\n"
    )
    result, install_reached = run_install_agent(tmp_path, agent)
    assert install_reached
    assert result.returncode != 0  # stops at the stubbed download


def test_unparsable_version_output_falls_back_to_install(tmp_path):
    agent = (
        "#!/bin/bash\n"
        'if [[ "$1" == "--version" ]]; then echo "not a version"; exit 0; fi\n'
        "exit 0\n"
    )
    result, install_reached = run_install_agent(tmp_path, agent)
    assert install_reached
    assert result.returncode != 0  # stops at the stubbed download


def test_older_base_agent_is_replaced(tmp_path):
    result, install_reached = run_install_agent(tmp_path, version_agent("3.73.1"))
    assert install_reached
    assert result.returncode != 0  # stops at the stubbed download


def test_missing_base_agent_installs(tmp_path):
    result, install_reached = run_install_agent(tmp_path, None)
    assert install_reached
    assert result.returncode != 0


def test_exact_mode_replaces_newer_base_agent(tmp_path):
    # AGENT_ONLY release bakes pin exactly: a newer base agent is not kept.
    result, install_reached = run_install_agent(
        tmp_path, version_agent("4.1.0"), require_exact=True
    )
    assert install_reached
    assert result.returncode != 0  # stops at the stubbed download


def test_exact_mode_replaces_prerelease_of_pin(tmp_path):
    # Release workers carry the stable pin, not a prerelease of the same number.
    result, install_reached = run_install_agent(
        tmp_path, version_agent(f"{PINNED_AGENT_VERSION}-beta.1"), require_exact=True
    )
    assert install_reached
    assert result.returncode != 0  # stops at the stubbed download


def test_exact_mode_keeps_same_base_agent(tmp_path):
    result, install_reached = run_install_agent(
        tmp_path, version_agent(PINNED_AGENT_VERSION), require_exact=True
    )
    assert result.returncode == 0, result.stderr
    assert not install_reached
