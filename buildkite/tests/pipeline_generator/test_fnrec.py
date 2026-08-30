"""fnrec must be invisible when off and deliverable when on.

Off, the generated YAML has to be byte-identical to what it was before. On, the
injected commands have to survive apostrophes rewritten to double quotes and `$$`
collapsed to `$`, and still be valid shell. Assertions on the command strings
cannot show the second, so they are also run through a real bash here.

Delivery is the step's `artifact_paths`, evaluated by the agent on the host after
the container exits. Build 85489 recorded 87 of 369 jobs because a third-party
EXIT trap replaced ours and every non-AMD recording died inside its container, so
`test_the_recording_lands_where_the_step_uploads_from` walks the whole path -- from
the emitted shell to the step's own globs -- rather than asserting a path string,
which would not have caught it.
"""

import base64
import gzip
import importlib.util
import subprocess
import sys

import pytest
import yaml

import buildkite_step
import constants
import fnrec_payload
from amd import is_amd_gpu_device
from step import Step

pytestmark = pytest.mark.usefixtures("fake_global_config")


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


def _run_pack_command(monkeypatch, tmp_path, packer: str | None, step_exit: int) -> int:
    """Run the generator's own pack command after a step that exits `step_exit`.

    The command is lifted from the emitted list rather than rewritten here, so
    dropping its `|| true` guard fails the test that uses this.
    """
    target = tmp_path / "fnrec_pack.sh"
    if packer is not None:
        target.write_text(packer)
        target.chmod(0o755)
    monkeypatch.setenv("FNREC", "1")
    commands = buildkite_step._prepare_commands(_step(), variables_to_inject={})
    pack = next((c for c in commands if c.endswith("fnrec_pack.sh || true")), None)
    assert pack is not None, commands
    pack = pack.replace("/tmp/fnrec_pack.sh", str(target))
    return subprocess.run(
        ["bash", "-e", "-c", f"(exit {step_exit}); {pack}; exit {step_exit}"],
        capture_output=True,
    ).returncode


def _run_pack(tmp_path, out_dir):
    """Execute the real PACK_SH with FNREC_OUT set to `out_dir`."""
    script = tmp_path / "pack.sh"
    script.write_bytes(gzip.decompress(base64.b64decode(fnrec_payload.pack_blob())))
    script.chmod(0o755)
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "FNREC_OUT": str(out_dir)},
    )


def _load_recorder(tmp_path):
    """Import the embedded recorder as a module, dormant (no env set)."""
    src = tmp_path / "fnrec.py"
    src.write_text(fnrec_payload.FNREC_PY)
    spec = importlib.util.spec_from_file_location("fnrec_under_test", src)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("value", [None, "0", "true"])
def test_absent_unless_enabled(monkeypatch, value):
    # Only an exact "1" enables it, so a stray truthy value cannot instrument a
    # build by accident.
    monkeypatch.delenv("FNREC", raising=False)
    if value is not None:
        monkeypatch.setenv("FNREC", value)
    commands = buildkite_step._prepare_commands(_step(), variables_to_inject={})
    assert not any("fnrec" in c.lower() for c in commands)


def test_present_when_enabled(monkeypatch):
    monkeypatch.setenv("FNREC", "1")
    commands = buildkite_step._prepare_commands(_step(), variables_to_inject={})

    # Setup precedes the step's own commands, so a crash cannot pre-empt it.
    assert commands.index('echo "--- :dna: fnrec setup"') < commands.index(
        "(command nvidia-smi || true)"
    )
    # Packing is a plain command, not an EXIT trap: a POSIX shell has one EXIT
    # slot and ci_otel.sh takes it. Guarded, because a step's status is not ours.
    assert "/tmp/fnrec_pack.sh || true" in commands
    assert not any(c.startswith("trap ") for c in commands), (
        "an EXIT trap is exactly what stopped being delivered"
    )
    # In the checkout, which the agent can still read once the container is gone.
    out = next(c for c in commands if c.startswith("export FNREC_OUT="))
    assert '"/workdir/.fnrec"' in next(
        c for c in commands if c.startswith("export FNREC_BASE=")
    )
    assert "BUILDKITE_JOB_ID" in out


def test_reaches_kubernetes_routed_steps(monkeypatch):
    """h100/a100/b200-k8s carry a closed podSpec env list, but the commands are
    literal shell in the step body, so the recorder is installed there with no
    k8s_plugin change. If that stops holding, a third of the fleet records
    nothing. The checkout root differs from the docker case: agent-stack-k8s runs
    checkout, command and artifact phases against one shared volume, so the
    agent's own BUILDKITE_BUILD_CHECKOUT_PATH is correct inside the container.
    """
    monkeypatch.setenv("FNREC", "1")
    commands = buildkite_step._prepare_commands(
        _step(device="h100", key="fusion-e2e-quick-h100"), variables_to_inject={}
    )
    assert any(c.startswith("export FNREC_OUT=") for c in commands)
    base = next(c for c in commands if c.startswith("export FNREC_BASE="))
    assert "${BUILDKITE_BUILD_CHECKOUT_PATH:-/tmp/fnrec-no-checkout}/.fnrec" in base


@pytest.mark.parametrize(
    "step_kwargs,setup_profile",
    [
        # An image build runs no pytest, so there is nothing to record.
        ({"label": ":docker: Build image", "key": "image-build"}, "nvidia"),
        # "none" is never passed in production, only here. Kept so the branch
        # cannot rot into something that silently instruments.
        ({"key": "extract-hidden-states-integration"}, "none"),
    ],
)
def test_skipped_where_the_profile_is_skipped(monkeypatch, step_kwargs, setup_profile):
    monkeypatch.setenv("FNREC", "1")
    commands = buildkite_step._prepare_commands(
        _step(**step_kwargs), variables_to_inject={}, setup_profile=setup_profile
    )
    assert not any("fnrec" in c.lower() for c in commands)


def test_reaches_a_plugin_less_step(monkeypatch):
    """`no_plugin` used to skip recording along with image builds, which is what
    cost every CPU, Arm and XPU suite its coverage. It runs pytest like any other
    step; it just runs on the agent host instead of in a container."""
    monkeypatch.setenv("FNREC", "1")
    commands = buildkite_step._prepare_commands(
        _step(no_plugin=True, key="cpu-kernel-tests"), variables_to_inject={}
    )
    assert any("--- :dna: fnrec setup" in c for c in commands)
    base = next(c for c in commands if c.startswith("export FNREC_BASE="))
    # The agent's own checkout, never the docker mount: there is no container.
    assert "${BUILDKITE_BUILD_CHECKOUT_PATH:-/tmp/fnrec-no-checkout}/.fnrec" in base
    # Delivery follows from the commands, so it needs no separate predicate.
    assert fnrec_payload.fnrec_artifact_paths(commands)


def test_a_plugin_less_step_does_not_touch_the_shared_interpreter(monkeypatch):
    """The property the whole host path rests on.

    The container installer writes a .pth into site-packages, which then executes
    at every interpreter start on that agent, for every later job, with no
    uninstall. That is why multi-node steps are excluded from recording, and
    plugin-less steps run on the same shared hosts. So they get an installer that
    writes only inside the job's own directory and reaches subprocesses through
    PYTHONPATH instead.
    """
    monkeypatch.setenv("FNREC", "1")
    commands = buildkite_step._prepare_commands(
        _step(no_plugin=True, key="cpu-kernel-tests"), variables_to_inject={}
    )
    installer = next(c for c in commands if "fnrec_install.py" in c)
    source = gzip.decompress(
        base64.b64decode(installer.split()[1])
    ).decode()
    # Assert on what it WRITES, not on what it mentions: the host installer's
    # comments discuss the .pth precisely because it must not create one.
    assert '"fnrec.pth"' not in source
    assert "site.getsitepackages()" not in source
    assert 'os.environ["FNREC_LIB"]' in source
    assert any(c.startswith('export FNREC_LIB="$$FNREC_OUT/lib"') for c in commands)
    assert any("PYTHONPATH" in c for c in commands)

    # A container step must keep the .pth: it is the only thing that reaches the
    # engine and worker subprocesses, and the container is thrown away.
    contained = buildkite_step._prepare_commands(_step(), variables_to_inject={})
    src = gzip.decompress(
        base64.b64decode(
            next(c for c in contained if "fnrec_install.py" in c).split()[1]
        )
    ).decode()
    assert '"fnrec.pth"' in src
    assert "site.getsitepackages()" in src


def test_reaches_the_amd_profile(monkeypatch):
    # FNREC_OUT must exist before the ROCm setup runs, so a crash there still
    # leaves a directory the agent can collect. Packing is last, after the tests.
    monkeypatch.setenv("FNREC", "1")
    commands = buildkite_step._prepare_commands(
        _step(), variables_to_inject={}, setup_profile="amd"
    )
    armed = [i for i, c in enumerate(commands) if c.startswith("export FNREC_OUT=")]
    rocm = [i for i, c in enumerate(commands) if "amd-smi" in c]
    packed = [i for i, c in enumerate(commands) if c.endswith("fnrec_pack.sh || true")]
    assert armed and rocm and packed, commands
    assert armed[0] < rocm[0] < packed[0]


def test_absent_from_the_none_profile(monkeypatch):
    monkeypatch.setenv("FNREC", "1")
    commands = buildkite_step._prepare_commands(
        _step(), variables_to_inject={}, setup_profile="none"
    )
    assert not any("fnrec" in c.lower() for c in commands)


def test_payload_survives_the_apostrophe_rewrite(monkeypatch):
    """base64 is chosen so the blobs contain no apostrophe and no dollar sign. If
    an edit reintroduces either, the decoded installer differs from the source.
    """
    monkeypatch.setenv("FNREC", "1")
    commands = buildkite_step._prepare_commands(_step(), variables_to_inject={})

    blob = next(c for c in commands if "fnrec_install.py" in c and "base64 -d" in c)
    encoded = blob.split()[1]
    assert "'" not in encoded and "$" not in encoded

    decoded = gzip.decompress(base64.b64decode(encoded)).decode()
    assert (
        decoded
        == gzip.decompress(base64.b64decode(fnrec_payload.install_blob())).decode()
    )
    # It has to be importable Python once it lands in the container.
    compile(decoded, "fnrec_install.py", "exec")


def test_injected_shell_actually_runs(monkeypatch, tmp_path):
    """Execute the generated setup through real bash, as the job would.

    A throwaway venv stands in for the CI image: the installer only accepts a
    directory `site` reports, so a plain directory on PYTHONPATH would be
    rejected and the recorder would land in the interpreter running the tests.
    """
    monkeypatch.setenv("FNREC", "1")
    venv = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True
    )
    site_dir = next((venv / "lib").glob("python*/site-packages"))
    (site_dir / "vllm").mkdir()
    (site_dir / "vllm" / "__init__.py").write_text("")

    # Pulled from _prepare_commands, not the raw list, so bash sees the
    # commands after the apostrophe rewrite -- which is the point.
    emitted = buildkite_step._prepare_commands(_step(), variables_to_inject={})
    # The docker mount point is an absolute literal a test cannot create, so the
    # checkout stands in for it. The literal itself is pinned separately by
    # test_docker_plugin_pins_the_checkout_mount.
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    setup = [
        c.replace("/workdir", str(checkout))
        for c in emitted
        if "fnrec" in c.lower() and not c.endswith("fnrec_pack.sh || true")
    ]
    script = " && ".join(setup).replace("$$", "$")
    script += ' && echo "RESOLVED=$FNREC_ROOT" && echo "OUT=$FNREC_OUT"'
    result = subprocess.run(
        ["bash", "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env={
            "PATH": f"{venv / 'bin'}:/usr/bin:/bin",
            "BUILDKITE_JOB_ID": "test-job-id",
            "TMPDIR": str(tmp_path),
        },
    )

    assert result.returncode == 0, result.stderr
    assert (site_dir / "fnrec.py").exists()
    assert (site_dir / "fnrec.pth").read_text() == "import fnrec\n"

    # FNREC_ROOT must be where vllm's code is, not the install target. Getting
    # this wrong records zero functions while every other signal looks healthy,
    # which is the one failure that costs a whole run to notice.
    assert f"RESOLVED={site_dir / 'vllm'}" in result.stdout
    assert f"OUT={checkout}/.fnrec/test-job-id" in result.stdout

    # The .pth runs in every interpreter in the image, including the build smoke
    # test, so importing it with no env set must be silent.
    dormant = subprocess.run(
        [str(venv / "bin" / "python"), "-c", "print(1)"],
        capture_output=True,
        text=True,
    )
    assert dormant.returncode == 0
    assert dormant.stderr == ""


def test_fnrec_no_longer_mounts_the_buildkite_agent(monkeypatch):
    """The agent binary was only ever there so the uploader could run in-container.

    Delivery is `artifact_paths` now, so fnrec needs nothing mounted. OTel still
    uploads from inside and still asks for it.
    """
    from plugin.docker_plugin import get_docker_plugin

    monkeypatch.setenv("FNREC", "1")
    assert "mount_buildkite_agent" not in get_docker_plugin(_step(), "img")


_FAKE_JOB_ID = "0193f0c2-dead-beef-cafe-000000000001"


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
    exist. That is build 85489: 282 of 369 jobs wrote perfect records into a
    container /tmp that `docker run --rm` threw away, and every one looked green.
    Asserting the path string would not have caught it; only walking from the
    emitted shell to the agent's globs does.

    `packed=False` is the case that actually happened -- the packing step never
    ran, and delivery has to survive that.
    """
    monkeypatch.setenv("FNREC", "1")
    checkout = tmp_path / "checkout"
    checkout.mkdir()

    step = _step(**step_kwargs)
    commands = buildkite_step._prepare_commands(
        step, variables_to_inject={}, setup_profile=profile
    )
    globs = fnrec_payload.fnrec_artifact_paths(commands)
    assert globs, "fnrec injected nothing, so nothing can be delivered"

    fnrec = [c for c in commands if "fnrec" in c.lower()]
    setup = [c for c in fnrec if not c.endswith("fnrec_pack.sh || true")]
    # Stand in for the recorder, between setup and packing exactly as a job runs:
    # one file per process, as the container writes them.
    parts = setup + ['echo x > "$$FNREC_OUT/fn.host.abc.1.txt"']
    if packed:
        parts.append("/tmp/fnrec_pack.sh || true")
    script = " && ".join(parts).replace("$$", "$")
    # The docker mount is an absolute literal a test cannot create; the literal
    # itself is pinned by test_docker_plugin_pins_the_checkout_mount.
    script = script.replace(constants.DOCKER_CHECKOUT_MOUNT_PATH, str(checkout))
    # Never run the real installer: it writes into whatever site-packages
    # python3 resolves to. Where its stderr lands is what this test cares about.
    script = script.replace("python3 /tmp/fnrec_install.py", "false")

    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "BUILDKITE_JOB_ID": _FAKE_JOB_ID,
            "BUILDKITE_BUILD_CHECKOUT_PATH": str(checkout),
        },
    )
    assert result.returncode == 0, result.stderr

    # As the agent does it: globs relative to the checkout, after the container.
    matched = {path for glob in globs for path in checkout.glob(glob)}
    assert matched, f"nothing under {checkout} matches {globs}\n{result.stdout}"
    assert all(_FAKE_JOB_ID in str(path) for path in matched)
    assert packed == any(str(path).endswith(".tar.gz") for path in matched)


def _run_setup(tmp_path, commands, checkout, job_id="job-a"):
    """Run just the setup half of the emitted commands against a fake checkout."""
    setup = [c for c in commands if "fnrec" in c.lower()]
    setup = [c for c in setup if not c.endswith("fnrec_pack.sh || true")]
    script = " && ".join(setup).replace("$$", "$")
    script = script.replace(constants.DOCKER_CHECKOUT_MOUNT_PATH, str(checkout))
    script = script.replace("python3 /tmp/fnrec_install.py", "false")
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "BUILDKITE_JOB_ID": job_id,
            "BUILDKITE_BUILD_CHECKOUT_PATH": str(checkout),
        },
    )


def test_setup_clears_a_previous_jobs_recording(monkeypatch, tmp_path):
    """The checkout outlives the job. Buildkite's own `git clean` runs first and
    would already remove this, but that is Buildkite's default, not ours."""
    monkeypatch.setenv("FNREC", "1")
    checkout = tmp_path / "checkout"
    stale = checkout / ".fnrec" / "job-from-yesterday"
    stale.mkdir(parents=True)
    (stale / "fn.host.abc.9.txt").write_text("stale\n")

    commands = buildkite_step._prepare_commands(_step(), variables_to_inject={})
    result = _run_setup(tmp_path, commands, checkout)

    assert result.returncode == 0, result.stderr
    assert not stale.exists()
    assert (checkout / ".fnrec" / "job-a").is_dir()


def test_recording_directories_stay_removable_by_the_agent(monkeypatch, tmp_path):
    """The container runs as root and the agent does not.

    Unlinking a file needs write and execute on its directory, so without this
    the next job's `git clean` fails, and the agent then wedges on checkout for
    every job after it -- worse than losing a recording.
    """
    monkeypatch.setenv("FNREC", "1")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    commands = buildkite_step._prepare_commands(_step(), variables_to_inject={})
    assert _run_setup(tmp_path, commands, checkout).returncode == 0

    for path in (checkout / ".fnrec", checkout / ".fnrec" / "job-a"):
        assert path.stat().st_mode & 0o777 == 0o777, path


@pytest.mark.parametrize("device", [d.value for d in constants.DeviceType])
def test_the_checkout_choice_tracks_the_plugin_router(monkeypatch, device):
    """Two roots exist and picking the wrong one fails silently, so the choice is
    pinned against the router rather than restated as a list."""
    monkeypatch.setenv("FNREC", "1")
    step = _step(device=device, num_devices=1)
    path = buildkite_step._fnrec_checkout_path(step, "nvidia")
    if is_amd_gpu_device(device):
        assert path.startswith("$${BUILDKITE_BUILD_CHECKOUT_PATH:-")
        return
    routed_to_k8s = "kubernetes" in buildkite_step._get_step_plugin(step)
    assert path.startswith("$${BUILDKITE_BUILD_CHECKOUT_PATH:-") is routed_to_k8s


@pytest.mark.parametrize("device", ["h200_18gb", "h200_35gb", "h100", "b200"])
def test_an_amd_mirror_never_uses_the_docker_checkout_path(monkeypatch, device):
    """An AMD mirror keeps its NVIDIA parent's device, so the device cannot pick
    the root.

    `step.model_copy(...)` in the mirror branch changes `no_plugin` and nothing
    else, so a mirror of an h200 step still reports device=h200 while running in
    a ROCm pod that has no /workdir. Build 85562 lost 85 of 91 AMD recordings to
    exactly this, and the six that survived were the ones whose parent happened
    to be k8s-routed. Parametrised across both parent families so the accident
    that hid it cannot hide it again.
    """
    monkeypatch.setenv("FNREC", "1")
    commands = buildkite_step._prepare_commands(
        _step(device=device, num_devices=1), variables_to_inject={}, setup_profile="amd"
    )
    base = next(c for c in commands if c.startswith("export FNREC_BASE="))
    assert constants.DOCKER_CHECKOUT_MOUNT_PATH not in base, base
    assert "BUILDKITE_BUILD_CHECKOUT_PATH" in base


def test_amd_keeps_its_diagnostics_glob_when_fnrec_is_on(monkeypatch):
    """The diagnostics artifact is where a ROCm hang investigation starts.
    Assigning over it would trade one silent data loss for another."""
    from amd import AMD_DIAGNOSTICS_ARTIFACT_GLOB

    monkeypatch.setenv("FNREC", "1")
    merged = buildkite_step._merge_artifact_paths(
        [AMD_DIAGNOSTICS_ARTIFACT_GLOB], list(fnrec_payload.FNREC_ARTIFACT_PATHS)
    )
    assert merged[0] == AMD_DIAGNOSTICS_ARTIFACT_GLOB
    assert set(fnrec_payload.FNREC_ARTIFACT_PATHS) <= set(merged)


def test_artifact_paths_stay_absent_when_fnrec_is_off(monkeypatch):
    """With FNREC unset the generated YAML must be byte-identical to before, and
    `exclude_none` only drops the field while it is None."""
    monkeypatch.delenv("FNREC", raising=False)
    commands = buildkite_step._prepare_commands(_step(), variables_to_inject={})
    assert fnrec_payload.fnrec_artifact_paths(commands) == []
    assert buildkite_step._merge_artifact_paths(None, []) is None


def test_docker_plugin_pins_the_checkout_mount(monkeypatch):
    """`/workdir` is a plugin default. Delivery depends on it now, so it has to
    be stated in our own YAML and not moved by an upstream bump.

    Gated on FNREC, so a build with the recorder off generates exactly the YAML
    it generated before any of this existed.
    """
    from plugin.docker_plugin import get_docker_plugin

    monkeypatch.delenv("FNREC", raising=False)
    off = get_docker_plugin(_step(), "img")
    assert "workdir" not in off and "mount-checkout" not in off

    monkeypatch.setenv("FNREC", "1")
    plugin = get_docker_plugin(_step(), "img")
    assert plugin["mount-checkout"] is True
    assert plugin["workdir"] == constants.DOCKER_CHECKOUT_MOUNT_PATH == "/workdir"


@pytest.mark.parametrize(
    "packer",
    [
        "#!/bin/sh\nexit 0\n",
        "#!/bin/sh\nexit 5\n",  # truncated by a full disk, or a failed gzip
        None,  # never written
    ],
)
@pytest.mark.parametrize("step_exit", [0, 3])
def test_pack_never_changes_the_step_status(monkeypatch, tmp_path, packer, step_exit):
    """Under `set -e`, a failing command aborts the shell and sets its status.

    Without the `|| true` guard a broken or missing packer would turn a passing
    job red, and packing is the one part of fnrec that is allowed to fail: the
    raw files are delivered either way.
    """
    assert _run_pack_command(monkeypatch, tmp_path, packer, step_exit) == step_exit


def test_blobs_round_trip_through_yaml(monkeypatch):
    """The blob is one unbroken 6 KB token, and the pipeline is dumped as YAML
    before an agent ever reads it."""
    monkeypatch.setenv("FNREC", "1")
    commands = buildkite_step._prepare_commands(_step(), variables_to_inject={})
    assert yaml.safe_load(yaml.dump({"commands": commands}))["commands"] == commands


def test_blobs_are_reproducible():
    """gzip stamps the current time into its header by default, which would make
    the blob differ per call and the generated pipeline unreproducible."""
    assert fnrec_payload.install_blob() == fnrec_payload.install_blob()
    assert fnrec_payload.pack_blob() == fnrec_payload.pack_blob()


@pytest.mark.parametrize("populated", [True, False])
def test_pack_makes_one_tarball_and_removes_the_raw_files(tmp_path, populated):
    """One artifact per job, not one per process. A distributed step forks a
    worker per test and each child writes its own file.

    An empty directory still packs: the recording being empty is itself the
    finding, and a missing artifact is indistinguishable from a lost upload.
    """
    out = tmp_path / "job-id"
    out.mkdir()
    if populated:
        for pid in (1, 2, 3):
            (out / f"fn.host.abc.{pid}.txt").write_text("x\ty\t1\n")

    result = _run_pack(tmp_path, out)

    assert result.returncode == 0
    assert (tmp_path / "job-id.tar.gz").exists()
    assert not out.exists(), "the raw files would upload a second time"
    assert not list(tmp_path.glob("*.part"))
    assert "packed" in result.stdout


def test_pack_never_publishes_a_partial_tarball(tmp_path):
    """A step killed mid-tar must not leave something the glob will ship.

    A truncated .tar.gz reads downstream as a complete, nearly empty recording,
    which is a wrong answer rather than a missing one.
    """
    assert ".part" in fnrec_payload.PACK_SH
    staged = fnrec_payload.PACK_SH.index("${JOB}.tar.gz.part")
    renamed = fnrec_payload.PACK_SH.index("mv ")
    assert staged < renamed
    import fnmatch

    assert not any(
        fnmatch.fnmatch(".fnrec/job-id.tar.gz.part", glob)
        for glob in fnrec_payload.FNREC_ARTIFACT_PATHS
    )


@pytest.mark.parametrize("case", ["unset", "missing", "untarrable"])
def test_pack_is_loud_on_every_bail(tmp_path, case):
    """Silence is the one outcome that cannot be interpreted.

    Four silent `exit 0`s are why a total delivery failure looked exactly like a
    build whose steps ran no vLLM code, for a whole rollout.
    """
    script = tmp_path / "pack.sh"
    script.write_bytes(gzip.decompress(base64.b64decode(fnrec_payload.pack_blob())))
    script.chmod(0o755)

    env = {"PATH": "/usr/bin:/bin"}
    if case == "missing":
        env["FNREC_OUT"] = str(tmp_path / "absent")
    elif case == "untarrable":
        locked = tmp_path / "locked"
        locked.mkdir()
        out = locked / "job-id"
        out.mkdir()
        (out / "fn.host.abc.1.txt").write_text("x\ty\t1\n")
        locked.chmod(0o500)  # tar can read, but cannot write the .part beside it
        env["FNREC_OUT"] = str(out)

    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True, env=env
    )
    if case == "untarrable":
        (tmp_path / "locked").chmod(0o700)

    assert result.returncode == 0
    assert result.stdout.strip(), f"{case} bailed without saying anything"
    assert result.stdout.startswith("fnrec:")


@pytest.mark.parametrize(
    "argv,expected",
    [
        (
            ["/venv/bin/pytest", "-v", "-s", "v1/kv_connector"],
            "pytest -v -s v1/kv_connector",
        ),
        (["/venv/bin/vllm", "serve", "Qwen/Qwen3-0.6B"], "vllm serve Qwen/Qwen3-0.6B"),
        (
            ["/x/vllm", "serve", "--api-key", "sk-real-secret"],
            "vllm serve --api-key <redacted>",
        ),
        (["/x/vllm", "--hf-token=hf_real_secret"], "vllm --hf-token=<redacted>"),
    ],
)
def test_recorded_argv_identifies_the_process_without_leaking(
    tmp_path, monkeypatch, argv, expected
):
    """The header goes into a build artifact, and argv is the only field whose
    content is unbounded: an env allowlist cannot catch a key passed on a
    command line. It still has to distinguish `vllm serve` from `pytest`, which
    is what the process census is for."""
    fnrec = _load_recorder(tmp_path)
    monkeypatch.setattr(fnrec.sys, "argv", argv)
    assert fnrec._argv() == expected
