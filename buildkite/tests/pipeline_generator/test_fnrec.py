"""fnrec must be invisible when off and deliverable when on.

Off, the generated YAML has to be byte-identical to what it was before. On, the
emitted commands have to survive apostrophes rewritten to double quotes and `$$`
collapsed to `$`, and still be valid shell. Assertions on the command strings
cannot show the second, so several tests run them through a real bash.

Two shapes of failure this file exists to catch, both of which passed every
other test:

  A recording the step's own globs do not match is a recording that does not
  exist, so `test_the_recording_lands_where_the_step_uploads_from` walks the
  whole path rather than asserting a path string.

  The AMD path joins commands with `&&`, so an element ending in a bare
  `|| true` binds to the whole chain and reports success for a step whose tests
  failed. `test_amd_chain_reports_a_failing_step` runs it.
"""

import subprocess
from pathlib import Path

import buildkite_step
import constants
import pytest
import recorder_switches
from amd import is_amd_gpu_device
from plugin.docker_plugin import DOCKER_CHECKOUT_MOUNT_PATH, get_docker_plugin
from step import Step

pytestmark = pytest.mark.usefixtures("fake_global_config")

REPO_ROOT = Path(__file__).resolve().parents[3]
PAYLOAD = REPO_ROOT / "buildkite" / "ci_selector" / "recorders" / "fnrec"
_FAKE_JOB_ID = "0193f0c2-dead-beef-cafe-000000000001"


def _step(**kwargs):
    defaults = dict(
        label="Extract Hidden States Integration",
        group="Misc",
        key="extract-hidden-states-integration",
        depends_on=["image-build"],
        device="h200_18gb",
        num_devices=1,
        working_dir="/vllm-workspace/tests",
        commands=["pytest -v -s v1/kv_connector/extract_hidden_states_integration"],
    )
    defaults.update(kwargs)
    return Step(**defaults)


def _commands(step=None, profile="nvidia"):
    return buildkite_step._prepare_commands(
        step or _step(), variables_to_inject={}, setup_profile=profile
    )


def _setup_element(commands):
    """The single element that fetches and sources the recorder."""
    return next((c for c in commands if "fnrec" in c.lower() and "curl" in c), None)


def _fake_curl(bin_dir):
    """A curl that serves the real payload files, so no test needs the network."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        'out=""; url=""\n'
        'while [ $# -gt 0 ]; do case "$1" in\n'
        '  -o) out="$2"; shift 2 ;;\n'
        "  -*) shift ;;\n"
        '  *) url="$1"; shift ;;\n'
        "esac; done\n"
        f'cp "{PAYLOAD}/$(basename "$url")" "$out"\n'
    )
    curl.chmod(0o755)
    return bin_dir


def _run_setup(tmp_path, commands, checkout, job_id="job-a", installer="false"):
    """Run the emitted setup through bash against a fake checkout.

    `installer` stands in for `python3 <dir>/install.py`, which would otherwise
    write into whatever site-packages the test runner resolves to.
    """
    element = _setup_element(commands)
    assert element is not None, commands
    script = element.replace("$$", "$")
    script = script.replace(DOCKER_CHECKOUT_MOUNT_PATH, str(checkout))
    script = script.replace("python3 $FNREC_TMP/install.py", installer)
    for name in ("install.py", "host_install.py"):
        script = script.replace(f"python3 /tmp/fnrec.{job_id}/{name}", installer)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{_fake_curl(tmp_path / 'bin')}:/usr/bin:/bin",
            "BUILDKITE_JOB_ID": job_id,
            "BUILDKITE_BUILD_CHECKOUT_PATH": str(checkout),
        },
    )


# --- arming -----------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "0", "true"])
def test_absent_unless_enabled(monkeypatch, value):
    # Only an exact "1" arms it, so a stray truthy value cannot instrument a
    # build by accident.
    monkeypatch.delenv(recorder_switches.FNREC_ENV_VAR, raising=False)
    if value is not None:
        monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, value)
    assert not any("fnrec" in c.lower() for c in _commands())


def test_present_when_enabled(monkeypatch):
    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    commands = _commands()
    element = _setup_element(commands)
    assert element is not None, commands

    # Setup precedes the step's own commands, so a crash cannot pre-empt it.
    assert commands.index(element) < commands.index("(command nvidia-smi || true)")
    # Packing is a plain command, not an EXIT trap: a shell has one EXIT slot
    # and ci_otel.sh takes it.
    assert buildkite_step._fnrec_pack_command() in commands
    assert not any(c.startswith("trap ") for c in commands), (
        "an EXIT trap is exactly what stopped being delivered"
    )
    assert f'export FNREC_CHECKOUT="{DOCKER_CHECKOUT_MOUNT_PATH}"' in element


@pytest.mark.parametrize(
    "step_kwargs,profile",
    [
        # An image build runs no pytest, so there is nothing to record.
        ({"label": ":docker: Build image", "key": "image-build"}, "nvidia"),
        ({}, "none"),
        # Multi-node gets no plugin and spans hosts, so no install can be
        # scoped to one job.
        ({"num_nodes": 2, "no_plugin": True}, "nvidia"),
    ],
)
def test_steps_that_must_not_be_armed(monkeypatch, step_kwargs, profile):
    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    commands = _commands(_step(**step_kwargs), profile)
    assert not any("fnrec" in c.lower() for c in commands)


def test_reaches_kubernetes_routed_steps(monkeypatch):
    """A k8s step runs checkout, command and artifact phases against one shared
    volume, so the agent's own BUILDKITE_BUILD_CHECKOUT_PATH is right inside the
    container, unlike the docker case."""
    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    element = _setup_element(_commands(_step(device="h100", key="fusion-e2e-quick")))
    assert element is not None
    assert 'FNREC_CHECKOUT="$${BUILDKITE_BUILD_CHECKOUT_PATH:-' in element


def test_reaches_a_plugin_less_step(monkeypatch):
    """A plugin-less step runs pytest like any other; it just runs on the agent
    host instead of in a container, so it installs in host mode."""
    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    step = _step(no_plugin=True, key="cpu-kernel-tests")
    element = _setup_element(_commands(step))
    assert element is not None
    assert 'export FNREC_MODE="host"' in element
    # The agent's own checkout, never the docker mount: there is no container.
    assert 'FNREC_CHECKOUT="$${BUILDKITE_BUILD_CHECKOUT_PATH:-' in element
    assert buildkite_step._fnrec_artifact_paths(step, "nvidia")


def test_the_host_installer_does_not_touch_the_shared_interpreter():
    """The property the whole host path rests on.

    A .pth in site-packages executes at every interpreter start on that agent
    for every later job, with no uninstall. The host installer must write only
    inside the job's own directory and reach subprocesses through PYTHONPATH.
    Asserted on what it WRITES, not what it mentions: its comments discuss the
    .pth precisely because it must not create one.
    """
    host = (PAYLOAD / "host_install.py").read_text()
    assert '"fnrec.pth"' not in host
    assert "site.getsitepackages()" not in host
    assert 'os.environ["FNREC_LIB"]' in host

    # The container step must keep the .pth: it is the only thing that reaches
    # engine and worker subprocesses, and the container is thrown away.
    container = (PAYLOAD / "install.py").read_text()
    assert '"fnrec.pth"' in container
    assert "site.getsitepackages()" in container


# --- the checkout root ------------------------------------------------------


@pytest.mark.parametrize("device", [d.value for d in constants.DeviceType])
def test_the_checkout_choice_tracks_the_plugin_router(monkeypatch, device):
    """Two roots exist and picking the wrong one fails silently, so the choice
    is pinned against the router rather than restated as a list."""
    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    step = _step(device=device, num_devices=1)
    path = buildkite_step._fnrec_checkout_path(step, "nvidia")
    if is_amd_gpu_device(device):
        assert path.startswith("$${BUILDKITE_BUILD_CHECKOUT_PATH:-")
        return
    routed_to_k8s = "kubernetes" in buildkite_step._get_step_plugin(step)
    assert path.startswith("$${BUILDKITE_BUILD_CHECKOUT_PATH:-") is routed_to_k8s


@pytest.mark.parametrize("device", ["h200_18gb", "h200_35gb", "h100", "b200"])
def test_an_amd_mirror_never_uses_the_docker_checkout_path(monkeypatch, device):
    """An AMD mirror is copied from its NVIDIA parent and keeps that device, so
    the device cannot pick the root: a mirror of an h200 step would be sent to
    /workdir, which a ROCm pod does not have. Parametrised across both parent
    families so the accident that once hid this cannot hide it again."""
    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    element = _setup_element(_commands(_step(device=device), profile="amd"))
    assert element is not None
    assert DOCKER_CHECKOUT_MOUNT_PATH not in element, element
    assert "BUILDKITE_BUILD_CHECKOUT_PATH" in element


# --- the emitted shell ------------------------------------------------------


def test_setup_survives_the_apostrophe_rewrite(monkeypatch):
    """_prepare_commands rewrites every apostrophe to a double quote, so an
    emitted single quote changes the command's meaning."""
    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    element = _setup_element(_commands())
    assert "'" not in element
    # A lone `$` would be expanded against the agent's environment at upload.
    assert "$" not in element.replace("$$", "")


def test_the_setup_url_names_a_file_that_exists(monkeypatch):
    """The payload is fetched by URL, so a moved or renamed file is invisible to
    every other test here: the emitted string and the assertion agree while
    naming a path that is not in the tree."""
    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    element = _setup_element(_commands())
    marker = "buildkite/ci_selector/recorders/fnrec/"
    assert marker in element
    name = element.split(marker)[1].split('"')[0]
    assert (PAYLOAD / name).is_file(), f"the step fetches {name}, which is not here"


@pytest.mark.parametrize("profile", ["nvidia", "amd"])
def test_amd_chain_reports_a_failing_step(monkeypatch, profile):
    """The AMD path joins commands with `&&`, so an element ending in a bare
    `|| true` would bind to the whole chain and turn a failing suite green."""
    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    commands = _commands(profile=profile)
    joined = " && ".join(commands)
    assert not joined.rstrip().endswith("|| true"), (
        "a trailing `|| true` swallows the status of everything before it"
    )
    # Run it: the shape matters more than the string.
    script = joined.replace("$$", "$")
    script = script.replace(commands[0], "cd /tmp")
    for command in commands:
        if command.startswith("pytest"):
            script = script.replace(command, "false")
    script = script.replace(_setup_element(commands) or "@@none@@", "true")
    script = script.replace(buildkite_step._fnrec_pack_command(), "{ true || true; }")
    result = subprocess.run(["bash", "-c", script], capture_output=True)
    assert result.returncode != 0, "a failing step reported success"


def test_setup_does_not_end_the_amd_command_chain(monkeypatch):
    """The setup element contains `;` separators. Unbraced, they end the `&&`
    chain, so a failing `cd <working_dir>` would stop aborting the step and
    pytest would run in the wrong directory."""
    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    element = _setup_element(_commands())
    script = f"cd /nope/nope/nope && {element.replace('$$', '$')} && echo REACHED"
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert "REACHED" not in result.stdout, "a failing cd no longer aborts the step"


# --- delivery ---------------------------------------------------------------


@pytest.mark.parametrize("packed", [True, False])
@pytest.mark.parametrize(
    "step_kwargs,profile",
    [
        ({"device": "h200_18gb"}, "nvidia"),  # docker plugin
        ({"device": "h100"}, "nvidia"),  # agent-stack-k8s
        ({"device": "mi300_4", "dind": False}, "amd"),  # native ROCm pod
    ],
)
def test_the_recording_lands_where_the_step_uploads_from(
    monkeypatch, tmp_path, step_kwargs, profile, packed
):
    """The one invariant the whole design rests on.

    A recording the step's own globs do not match is a recording that does not
    exist, and asserting the path string would not catch it. `packed=False` is
    the case that actually happened: packing never ran, and delivery has to
    survive that.
    """
    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    step = _step(**step_kwargs)
    commands = _commands(step, profile)
    globs = buildkite_step._fnrec_artifact_paths(step, profile)
    assert globs, "fnrec armed nothing, so nothing can be delivered"

    result = _run_setup(tmp_path, commands, checkout, job_id=_FAKE_JOB_ID)
    assert result.returncode == 0, result.stderr
    # Stand in for the recorder: one file per process, as a container writes it.
    out = checkout / ".fnrec" / _FAKE_JOB_ID
    assert out.is_dir(), result.stdout
    (out / "fn.host.abc.1.txt").write_text("x\ty\t1\n")
    if packed:
        pack = subprocess.run(
            ["bash", str(PAYLOAD / "pack.sh")],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "FNREC_OUT": str(out)},
        )
        assert pack.returncode == 0, pack.stderr

    # As the agent does it: globs relative to the checkout, after the container.
    matched = {path for glob in globs for path in checkout.glob(glob)}
    assert matched, f"nothing under {checkout} matches {globs}"
    assert all(_FAKE_JOB_ID in str(path) for path in matched)
    assert packed == any(str(path).endswith(".tar.gz") for path in matched)


def test_setup_clears_a_previous_jobs_recording(monkeypatch, tmp_path):
    """The checkout outlives the job. Buildkite's own git clean runs first and
    would already remove this, but that is its default, not ours."""
    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    checkout = tmp_path / "checkout"
    stale = checkout / ".fnrec" / "job-from-yesterday"
    stale.mkdir(parents=True)
    (stale / "fn.host.abc.9.txt").write_text("stale\n")

    result = _run_setup(tmp_path, _commands(), checkout)
    assert result.returncode == 0, result.stderr
    assert not stale.exists()
    assert (checkout / ".fnrec" / "job-a").is_dir()


def test_recording_directories_stay_removable_by_the_agent(monkeypatch, tmp_path):
    """The container runs as root and the agent does not. Deleting a file needs
    write on its directory, so without this the next job's git clean fails and
    the agent wedges on checkout for every job after it."""
    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    assert _run_setup(tmp_path, _commands(), checkout).returncode == 0

    for path in (checkout / ".fnrec", checkout / ".fnrec" / "job-a"):
        assert path.stat().st_mode & 0o777 == 0o777, path


# --- artifact paths and the plugin ------------------------------------------


def test_amd_keeps_its_diagnostics_glob_when_fnrec_is_on(monkeypatch):
    """The diagnostics artifact is where a ROCm hang investigation starts."""
    from amd import AMD_DIAGNOSTICS_ARTIFACT_GLOB

    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    merged = buildkite_step._merge_artifact_paths(
        [AMD_DIAGNOSTICS_ARTIFACT_GLOB], list(buildkite_step.FNREC_ARTIFACT_PATHS)
    )
    assert merged[0] == AMD_DIAGNOSTICS_ARTIFACT_GLOB
    assert set(buildkite_step.FNREC_ARTIFACT_PATHS) <= set(merged)


def test_the_two_recorders_do_not_share_a_directory():
    """Each owns its tree outright, so neither can remove or carry off the
    other's files and the order they run in does not matter."""
    fnrec = {g.split("/")[0] for g in buildkite_step.FNREC_ARTIFACT_PATHS}
    kernrec = {buildkite_step.KERNREC_ARTIFACT_PATH.split("/")[0]}
    assert fnrec.isdisjoint(kernrec), f"{fnrec} overlaps {kernrec}"


def test_artifact_paths_stay_absent_when_fnrec_is_off(monkeypatch):
    """With the switch unset the generated YAML must be byte-identical to
    before, and `exclude_none` only drops the field while it is None."""
    monkeypatch.delenv(recorder_switches.FNREC_ENV_VAR, raising=False)
    assert buildkite_step._fnrec_artifact_paths(_step(), "nvidia") == []
    assert buildkite_step._merge_artifact_paths(None, []) is None


def test_docker_plugin_pins_the_checkout_mount(monkeypatch):
    """/workdir is a plugin default. Delivery depends on it, so it is stated in
    our own YAML and cannot be moved by an upstream bump. Gated, so a build with
    the recorder off generates exactly the YAML it generated before."""
    monkeypatch.delenv(recorder_switches.FNREC_ENV_VAR, raising=False)
    off = get_docker_plugin(_step(), "img")
    assert "workdir" not in off and "mount-checkout" not in off

    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    plugin = get_docker_plugin(_step(), "img")
    assert plugin["mount-checkout"] is True
    assert plugin["workdir"] == DOCKER_CHECKOUT_MOUNT_PATH == "/workdir"


def test_fnrec_does_not_mount_the_buildkite_agent(monkeypatch):
    """The agent binary was only there so an uploader could run in-container.
    Delivery is artifact_paths now, so fnrec needs nothing mounted."""
    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    assert "mount_buildkite_agent" not in get_docker_plugin(_step(), "img")


def _timeout(step):
    """The timeout the generator renders for this step."""
    group = buildkite_step.convert_group_step_to_buildkite_step({step.group: [step]})[0]
    steps = group["steps"] if isinstance(group, dict) else group.steps
    rendered = next(
        s
        for s in steps
        if getattr(s, "key", None) == step.key
        or (isinstance(s, dict) and s.get("key") == step.key)
    )
    return (
        rendered.get("timeout_in_minutes")
        if isinstance(rendered, dict)
        else rendered.timeout_in_minutes
    )


def test_recording_gives_steps_a_timeout_margin(monkeypatch):
    """Recording costs the step time, and kernrec's margin is keyed on kernrec
    alone, so a build with only fnrec on would keep the unrecorded limit."""
    monkeypatch.delenv(recorder_switches.KERNREC_ENV_VAR, raising=False)
    step = _step(timeout_in_minutes=40)
    monkeypatch.delenv(recorder_switches.FNREC_ENV_VAR, raising=False)
    assert _timeout(step) == 40
    monkeypatch.setenv(recorder_switches.FNREC_ENV_VAR, "1")
    assert _timeout(step) == 50
