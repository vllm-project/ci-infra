import importlib.util
import os
import pathlib
import site


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


target = pathlib.Path(site_dir())
(target / "fnrec.py").write_text(FNREC_SOURCE)
# Loads fnrec at every interpreter start, including subprocesses.
(target / "fnrec.pth").write_text("import fnrec\n")

# FNREC_ROOT is where vllm's code is, not where we installed to.
spec = importlib.util.find_spec("vllm")
print(pathlib.Path(spec.origin).parent if spec is not None and spec.origin else target / "vllm")
