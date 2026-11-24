"""Tests for where the daily rebuild-cpu-ami pipeline rolls its AMI out to.

The launch-template step must touch only the CI build stack that still runs
the v6 AMI, and the cleanup step must never delete the AMI promoted to the
release queues. Cleanup is checked by running its real shell with stub `aws`
and `date` commands that log every call.
"""

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

RELEASE_AMI = "ami-0000000000000000c"
# Newest first, as the describe-images query in the cleanup step returns them.
OLD_AMIS = [
    "ami-0000000000000000a",
    "ami-0000000000000000b",
    RELEASE_AMI,
    "ami-0000000000000000d",
    "ami-0000000000000000e",
]

STUB_AWS = """#!/bin/bash
echo "$*" >> "$CALL_LOG"
case "$1 $2" in
  "ec2 describe-images")
    if [[ "$*" == *"--image-ids"* ]]; then echo "snap-0"; else echo "$OLD_AMIS"; fi ;;
  "ssm get-parameter")
    if [[ "$*" != *"--name /buildkite/release-cpu-build-ami/us-east-1 "* || -n "${RELEASE_PARAM_FAILS:-}" ]]; then
      echo "ParameterNotFound" >&2; exit 254
    fi
    echo "$RELEASE_AMI" ;;
  "autoscaling describe-auto-scaling-groups")
    if [[ -n "${ASG_READ_FAILS:-}" ]]; then echo "AccessDenied" >&2; exit 254; fi
    i=0
    for ami in $IN_USE_AMIS; do i=$((i + 1)); printf 'lt-%s\t%s\n' "$ami" "$i"; done ;;
  "ec2 describe-launch-template-versions")
    # The template ID carries the AMI it launches, so the stub needs no state.
    for arg in "$@"; do
      if [[ "$arg" == lt-* ]]; then
        if [[ " ${LT_LOOKUP_FAILS:-} " == *" ${arg#lt-} "* ]]; then echo "AccessDenied" >&2; exit 254; fi
        if [[ " ${LT_LOOKUP_NONE:-} " == *" ${arg#lt-} "* ]]; then echo "None"; else echo "${arg#lt-}"; fi
      fi
    done ;;
esac
exit 0
"""
# BSD date on a laptop has no -d; the cutoff value itself is never checked.
STUB_DATE = '#!/bin/bash\necho "2026-01-01T00:00:00"\n'
# Cleanup reads the AMI line's name prefix that build-ami recorded; the v6 line.
STUB_BUILDKITE_AGENT = (
    '#!/bin/bash\n'
    'if [[ "$*" == "meta-data get ami-name-prefix" ]]; then echo "vllm-buildkite-stack-linux-cpu-build"; exit 0; fi\n'
    'exit 1\n'
)


def command_of(step_key):
    """A step's shell command, with Buildkite's $$ escape turned back into $."""
    return STEPS[step_key]["command"][0].replace("$$", "$")


def write_stub(bin_dir, name, body):
    path = bin_dir / name
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def run_cleanup(tmp_path, extra_env=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_stub(bin_dir, "aws", STUB_AWS)
    write_stub(bin_dir, "date", STUB_DATE)
    write_stub(bin_dir, "buildkite-agent", STUB_BUILDKITE_AGENT)
    call_log = tmp_path / "calls.log"
    call_log.touch()
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CALL_LOG": str(call_log),
        "OLD_AMIS": " ".join(OLD_AMIS),
        "RELEASE_AMI": RELEASE_AMI,
        "IN_USE_AMIS": "ami-000000000000000aa ami-000000000000000bb",
        **PIPELINE["env"],
        **(extra_env or {}),
    }
    result = subprocess.run(
        ["bash", "-c", command_of("cleanup-amis")],
        env=env,
        capture_output=True,
        text=True,
    )
    deregistered = []
    for line in call_log.read_text().splitlines():
        match = re.search(r"deregister-image --image-id (\S+)", line)
        if match:
            deregistered.append(match.group(1))
    return result, deregistered


def test_launch_templates_update_only_the_v6_ci_build_stack():
    """Premerge us-east-1 runs Elastic CI Stack v7 and the release stacks boot
    a hand-promoted AMI, so neither may get the daily v6 AMI."""
    loops = re.findall(r"for STACK_NAME in ([^;]+);", command_of("update-launch-templates"))
    assert loops == ["$TARGET_STACKS"]
    # The first assignment in build-ami is the default (v6) line's target list.
    v6_targets = re.search(r'TARGET_STACKS="([^"]*)"', command_of("build-ami")).group(1)
    assert v6_targets.split() == ["bk-cpu-queue-postmerge-us-east-1"]


def test_cleanup_keeps_the_release_ami(tmp_path):
    result, deregistered = run_cleanup(tmp_path)
    assert result.returncode == 0, result.stderr
    # The newest 3 old AMIs (the release one among them) are kept for rollback.
    assert deregistered == ["ami-0000000000000000d", "ami-0000000000000000e"]
    assert f"Keeping release AMI: {RELEASE_AMI}" in result.stdout


def test_cleanup_skips_the_release_ami_when_it_is_old(tmp_path):
    old_amis = OLD_AMIS[:2] + ["ami-0000000000000000f"] + [RELEASE_AMI, "ami-0000000000000000e"]
    result, deregistered = run_cleanup(tmp_path, {"OLD_AMIS": " ".join(old_amis)})
    assert result.returncode == 0, result.stderr
    assert deregistered == ["ami-0000000000000000e"]
    assert f"Skipping {RELEASE_AMI}" in result.stdout


def test_cleanup_deletes_nothing_without_the_release_parameter(tmp_path):
    result, deregistered = run_cleanup(tmp_path, {"RELEASE_PARAM_FAILS": "1"})
    assert result.returncode != 0
    assert deregistered == []


def test_cleanup_deletes_nothing_when_the_parameter_holds_no_ami(tmp_path):
    """Terraform seeds parameters with "placeholder" until a real value lands."""
    result, deregistered = run_cleanup(tmp_path, {"RELEASE_AMI": "placeholder"})
    assert result.returncode != 0
    assert deregistered == []


def test_cleanup_keeps_amis_an_auto_scaling_group_still_launches(tmp_path):
    """After a promotion the release stacks boot the old AMI until the apply."""
    result, deregistered = run_cleanup(
        tmp_path, {"IN_USE_AMIS": "ami-0000000000000000d ami-0000000000000000f"}
    )
    assert result.returncode == 0, result.stderr
    assert deregistered == ["ami-0000000000000000e"]
    assert "Skipping ami-0000000000000000d: an Auto Scaling group still launches it" in result.stdout


def test_cleanup_deletes_nothing_when_auto_scaling_groups_cannot_be_read(tmp_path):
    result, deregistered = run_cleanup(tmp_path, {"ASG_READ_FAILS": "1"})
    assert result.returncode != 0
    assert deregistered == []


def test_cleanup_deletes_nothing_when_no_ami_is_in_use(tmp_path):
    result, deregistered = run_cleanup(tmp_path, {"IN_USE_AMIS": ""})
    assert result.returncode != 0
    assert deregistered == []


def test_cleanup_deletes_nothing_when_one_template_lookup_fails(tmp_path):
    """A failed lookup must not be masked by a later one that succeeds
    (bash drops errexit inside command substitutions)."""
    result, deregistered = run_cleanup(
        tmp_path,
        {
            "IN_USE_AMIS": "ami-0000000000000000d ami-0000000000000000f",
            "LT_LOOKUP_FAILS": "ami-0000000000000000d",
        },
    )
    assert result.returncode != 0
    assert deregistered == []


def test_cleanup_deletes_nothing_when_a_template_has_no_image(tmp_path):
    result, deregistered = run_cleanup(
        tmp_path,
        {
            "IN_USE_AMIS": "ami-0000000000000000d ami-0000000000000000f",
            "LT_LOOKUP_NONE": "ami-0000000000000000d",
        },
    )
    assert result.returncode != 0
    assert deregistered == []
