"""fnrec must be invisible when off and executable when on.

Off, the generated YAML has to be byte-identical to what it was before. On, the
injected commands have to survive apostrophes rewritten to double quotes and `$$`
collapsed to `$`, and still be valid shell. Assertions on the command strings
cannot show the second, so they are also run through a real bash here.
"""

import base64
import gzip
import importlib.util
import subprocess
import sys

import pytest
import yaml

import buildkite_step
import fnrec_payload
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


def _run_trap(tmp_path, uploader: str | None, step_exit: int) -> int:
    """Arm the generated trap around a step that exits `step_exit`."""
    target = tmp_path / "fnrec_upload.sh"
    if uploader is not None:
        target.write_text(uploader)
        target.chmod(0o755)
    trap = f"trap '{target} || true' EXIT"
    return subprocess.run(
        ["bash", "-e", "-c", f"{trap}; exit {step_exit}"], capture_output=True
    ).returncode


def _run_uploader(tmp_path, out_dir, agent_dir=None):
    """Execute the real UPLOAD_SH with FNREC_OUT set to `out_dir`."""
    script = tmp_path / "up.sh"
    script.write_bytes(gzip.decompress(base64.b64decode(fnrec_payload.upload_blob())))
    script.chmod(0o755)
    path = f"{agent_dir}:/usr/bin:/bin" if agent_dir else "/usr/bin:/bin"
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        env={"PATH": path, "FNREC_OUT": str(out_dir)},
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
    # Guarded, and shown post-rewrite: apostrophes become double quotes.
    assert 'trap "/tmp/fnrec_upload.sh || true" EXIT' in commands


def test_reaches_kubernetes_routed_steps(monkeypatch):
    """h100/a100/b200-k8s carry a closed podSpec env list, but the commands are
    literal shell in the step body, so the recorder is installed there with no
    k8s_plugin change. If that stops holding, a third of the fleet records
    nothing. FNREC_OUT still reads $BUILDKITE_JOB_ID from the pod env; an empty
    value degrades to a shared container-private dir, which the uploader handles.
    """
    monkeypatch.setenv("FNREC", "1")
    commands = buildkite_step._prepare_commands(
        _step(device="h100", key="fusion-e2e-quick-h100"), variables_to_inject={}
    )
    assert any(c.startswith("export FNREC_OUT=") for c in commands)


@pytest.mark.parametrize(
    "step_kwargs",
    [
        {"label": ":docker: Build image", "key": "image-build"},
        {"no_plugin": True, "key": "rust-frontend-cargo-tests"},
    ],
)
def test_skipped_where_the_profile_is_skipped(monkeypatch, step_kwargs):
    monkeypatch.setenv("FNREC", "1")
    commands = buildkite_step._prepare_commands(
        _step(**step_kwargs), variables_to_inject={}
    )
    assert not any("fnrec" in c.lower() for c in commands)


@pytest.mark.parametrize("profile", ["amd", "none"])
def test_confined_to_the_nvidia_profile(monkeypatch, profile):
    # AMD renders from a separate template and pulls its own image, so the
    # recorder would not be installed there even if the commands were emitted.
    monkeypatch.setenv("FNREC", "1")
    commands = buildkite_step._prepare_commands(
        _step(), variables_to_inject={}, setup_profile=profile
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
    setup = [c for c in emitted if "fnrec" in c.lower()]
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
    assert "OUT=/tmp/fnrec/test-job-id" in result.stdout

    # The .pth runs in every interpreter in the image, including the build smoke
    # test, so importing it with no env set must be silent.
    dormant = subprocess.run(
        [str(venv / "bin" / "python"), "-c", "print(1)"],
        capture_output=True,
        text=True,
    )
    assert dormant.returncode == 0
    assert dormant.stderr == ""


def test_agent_is_mounted_only_when_enabled(monkeypatch):
    from plugin.docker_plugin import get_docker_plugin

    monkeypatch.delenv("FNREC", raising=False)
    assert "mount_buildkite_agent" not in get_docker_plugin(_step(), "img")

    monkeypatch.setenv("FNREC", "1")
    assert get_docker_plugin(_step(), "img")["mount_buildkite_agent"] is True


@pytest.mark.parametrize(
    "uploader",
    [
        "#!/bin/sh\nexit 0\n",
        "#!/bin/sh\nexit 5\n",  # truncated by a full disk, or a failed gzip
        None,  # never written
    ],
)
@pytest.mark.parametrize("step_exit", [0, 3])
def test_trap_never_changes_the_step_status(tmp_path, uploader, step_exit):
    """An EXIT trap's exit status replaces the shell's.

    Without the `|| true` guard a broken uploader turns a passing job red and
    overwrites the real exit code of a failing one, and both are silent.
    """
    assert _run_trap(tmp_path, uploader, step_exit) == step_exit


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
    assert fnrec_payload.upload_blob() == fnrec_payload.upload_blob()


def test_uploader_is_a_noop_without_an_agent(tmp_path):
    out = tmp_path / "job-id"
    out.mkdir()
    (out / "fn.host.abc.1.txt").write_text("x\ty\t1\n")

    result = _run_uploader(tmp_path, out)

    assert result.returncode == 0
    assert "no buildkite-agent" in result.stdout
    assert out.exists(), "recording must survive for manual recovery"


@pytest.mark.parametrize("populated", [True, False])
def test_uploader_ships_one_tarball(tmp_path, populated):
    """One artifact per job, not one per process. A distributed step forks a
    worker per test and each child writes its own file."""
    out = tmp_path / "job-id"
    out.mkdir()
    if populated:
        for pid in (1, 2, 3):
            (out / f"fn.host.abc.{pid}.txt").write_text("x\ty\t1\n")

    agent_dir = tmp_path / "bin"
    agent_dir.mkdir()
    fake = agent_dir / "buildkite-agent"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/uploaded\n')
    fake.chmod(0o755)

    result = _run_uploader(tmp_path, out, agent_dir)

    assert result.returncode == 0
    # An empty directory still uploads: the recording being empty is itself the
    # finding, and a missing artifact is indistinguishable from a lost upload.
    assert (
        tmp_path / "uploaded"
    ).read_text().strip() == "artifact upload job-id.tar.gz"
    assert (tmp_path / "job-id.tar.gz").exists()


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
