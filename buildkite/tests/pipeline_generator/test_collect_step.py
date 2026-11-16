"""The collect steps' wait and the delivery share they are passed.

The wait runs as the agent runs it: the rendered command with `$$` unescaped,
in `bash -e`, with the step's env. Stub `date` and `sleep` drive a fake clock,
and a stub `buildkite-agent` reports each waited-on step finished from a given
time on.
"""

import os
import subprocess
import time
from datetime import datetime, timezone

import buildkite_step
import pytest
import recorder_switches
from step import Step

pytestmark = pytest.mark.usefixtures("fake_global_config")


def _epoch(stamp):
    return int(datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc).timestamp())


# A collect that starts over three hours after the pipeline was generated,
# while recording steps still run until 00:29:02.
GENERATED = _epoch("2026-11-12T19:03:23")
COLLECT_STARTED = _epoch("2026-11-12T22:19:24")
FINISHED = {
    "kernels": _epoch("2026-11-12T21:10:00"),
    "distributed-tests-8xh100": _epoch("2026-11-12T23:55:40"),
    "amd-lm-eval-large-models-ep-2xb200": _epoch("2026-11-13T00:29:02"),
}
POLL_SECONDS = 300


def _step(**overrides):
    fields = {
        "label": ":nvidia: (H100) Kernels",
        "key": "kernels",
        "group": "kernels",
        "commands": ["pytest -v -s kernels/core"],
        "device": "h100",
        "num_devices": 1,
        "working_dir": "tests",
    }
    fields.update(overrides)
    return Step(**fields)


def _collect(monkeypatch, fake_global_config, recorder):
    monkeypatch.setenv(recorder_switches.KERNREC_ENV_VAR, "1")
    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    fake_global_config["nightly"] = "1"
    groups = buildkite_step.convert_group_step_to_buildkite_step({"kernels": [_step()]})
    return getattr(buildkite_step, f"{recorder}_collect_group")(groups).steps[0]


def _run_wait(tmp_path, step, wait_for, finished, now):
    """Run the step's wait command from `now`; return its output and the
    clock when it ended, which is when the fold would start."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    clock = tmp_path / "clock"
    clock.write_text(str(now))
    done = tmp_path / "finished"
    done.mkdir()
    for key, at in finished.items():
        (done / key).write_text(str(at))
    # Agents run GNU date; this answers the two forms the loop uses.
    (bin_dir / "date").write_text(
        "#!/usr/bin/env bash\n"
        f'now=$(cat "{clock}")\n'
        'if [ "$1" = "+%s" ]; then echo "$now"; exit 0; fi\n'
        'python3 -c \'import sys, time; print(time.strftime(sys.argv[2], time.gmtime(int(sys.argv[1]))))\' '
        '"${3#@}" "${4#+}"\n'
    )
    (bin_dir / "sleep").write_text(
        f'#!/usr/bin/env bash\necho $(( $(cat "{clock}") + $1 )) > "{clock}"\n'
    )
    # `step get state --step KEY`: an error for a step it knows nothing of, as
    # an older agent, a missing step or a failed API call answers.
    (bin_dir / "buildkite-agent").write_text(
        "#!/usr/bin/env bash\n"
        f'f="{done}/$5"; [ -f "$f" ] || {{ echo "fatal: no such step" >&2; exit 1; }}\n'
        f'if [ "$(cat "{clock}")" -ge "$(cat "$f")" ]; then echo finished; else echo running; fi\n'
    )
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    env = {
        **os.environ,
        **{k: str(v) for k, v in step.env.items()},
        "CI_SELECTOR_COLLECT_WAIT_FOR": " ".join(wait_for),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    command = step.commands[0].replace("$$", "$")
    result = subprocess.run(
        ["bash", "-e", "-c", command], env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return result.stdout, int(clock.read_text())


@pytest.mark.parametrize("recorder", ["kernrec", "fnrec"])
def test_a_collect_that_starts_late_still_waits_for_the_recordings(
    tmp_path, monkeypatch, fake_global_config, recorder
):
    """The cap counts from the job's start, not from generation."""
    # Fails if a generation-time timestamp ever bounds the wait again.
    monkeypatch.setattr(time, "time", lambda: GENERATED)
    step = _collect(monkeypatch, fake_global_config, recorder)
    out, folded_at = _run_wait(tmp_path, step, list(FINISHED), FINISHED, COLLECT_STARTED)

    last = max(FINISHED.values())
    assert "every recording step has finished" in out
    assert last <= folded_at < last + POLL_SECONDS, "folds at the first poll after the last job"
    assert out.count("waiting on ") == (folded_at - COLLECT_STARTED) // POLL_SECONDS == 26


@pytest.mark.parametrize("recorder", ["kernrec", "fnrec"])
def test_a_step_that_never_finishes_ends_the_wait_at_start_plus_the_cap(
    tmp_path, monkeypatch, fake_global_config, recorder
):
    """An unreadable or unfinished step is waited for until start plus the
    cap, leaving the fold about the timeout's last 90 minutes."""
    monkeypatch.setattr(time, "time", lambda: GENERATED)
    step = _collect(monkeypatch, fake_global_config, recorder)
    out, folded_at = _run_wait(
        tmp_path, step, ["kernels", "amd-hung"], {"kernels": FINISHED["kernels"]}, COLLECT_STARTED
    )

    cap = COLLECT_STARTED + buildkite_step.COLLECT_AFTER_MINUTES * 60
    assert "every recording step has finished" not in out
    assert "waiting on amd-hung (state unknown); collecting at the latest at 01:19Z" in out
    assert cap <= folded_at < cap + POLL_SECONDS
    assert step.timeout_in_minutes * 60 - (folded_at - COLLECT_STARTED) >= 85 * 60


@pytest.mark.parametrize("recorder", ["kernrec", "fnrec"])
def test_both_collects_get_the_one_delivery_share(
    monkeypatch, fake_global_config, recorder
):
    """Both collects get the same share in their env."""
    step = _collect(monkeypatch, fake_global_config, recorder)
    share = step.env["CI_SELECTOR_MIN_DELIVERED_SHARE"]
    assert share == str(buildkite_step.COLLECT_MIN_DELIVERED_SHARE) == "0.8"
