import importlib.util
import os
import pathlib
import site
import sys


# ci_setup.sh fetches fnrec.py next to this script.
FNREC_SOURCE = (pathlib.Path(__file__).parent / "fnrec.py").read_text()


def site_dir():
    """Where a .pth will actually be executed.

    Under an editable install vllm's parent is the repo root, and a .pth there
    is never read, so only accept it if it is a site directory.
    """
    dirs = [p for p in site.getsitepackages() if os.path.isdir(p)]
    spec = importlib.util.find_spec("vllm")
    if spec is not None and spec.origin:
        parent = str(pathlib.Path(spec.origin).parent.parent)
        if parent in dirs:
            return parent
    return dirs[0]


def install_pytest_plugin(target: pathlib.Path) -> None:
    """Test outcomes without the job log: fnrec_pytest.py as a pytest11 entry
    point beside the recorder, so every pytest this interpreter starts writes
    its collected count and summary line into FNREC_OUT. The marker tells the
    collect step the plugin was in place, so a job with no pytest.*.txt ran no
    pytest rather than lost its counts. Optional: the recorder works without it.
    """
    source = pathlib.Path(__file__).parent / "fnrec_pytest.py"
    out = os.environ.get("FNREC_OUT")
    if not source.is_file() or not out:
        return
    try:
        (target / "fnrec_pytest.py").write_text(source.read_text())
        dist = target / "fnrec_pytest-0.dist-info"
        dist.mkdir(exist_ok=True)
        metadata = "Metadata-Version: 2.1\nName: fnrec-pytest\nVersion: 0\n"
        (dist / "METADATA").write_text(metadata)
        (dist / "entry_points.txt").write_text("[pytest11]\nfnrec = fnrec_pytest\n")
        marker = pathlib.Path(out) / "pytest.installed"
        marker.write_text("")
        marker.chmod(0o666)
    except Exception as exc:  # noqa: BLE001 - never fail the install over the plugin
        print(f"fnrec: no pytest plugin: {exc}", file=sys.stderr)


target = pathlib.Path(site_dir())
(target / "fnrec.py").write_text(FNREC_SOURCE)
# Loads fnrec at every interpreter start, including subprocesses.
(target / "fnrec.pth").write_text("import fnrec\n")
install_pytest_plugin(target)

# FNREC_ROOT is where vllm's code is, not where we installed to.
spec = importlib.util.find_spec("vllm")
print(pathlib.Path(spec.origin).parent if spec is not None and spec.origin else target / "vllm")
