"""Every script and workflow installs the same pinned uv.

The checks look at what each file does with the values, not only that it
declares them, because a constant can outlive the code that read it.
"""

import re
from pathlib import Path

import pytest

BUILDKITE = Path(__file__).resolve().parents[1]
REPO = BUILDKITE.parent

# Both are piped to bash with nothing on disk, so neither can source a shared
# copy. bootstrap.sh is what the rest are checked against.
SOURCE = BUILDKITE / "bootstrap.sh"
COPIES = [BUILDKITE / "ci_selector" / "recorders" / "fnrec" / "collect.sh"]
PIN_HOLDERS = [SOURCE] + COPIES

WORKFLOWS = [
    REPO / ".github" / "workflows" / "pipeline-generator-tests.yml",
    REPO / ".github" / "workflows" / "ci-selector-tests.yml",
]

FIELDS = ("UV_VERSION", "UV_TARBALL", "UV_SHA256")

UV_COMMAND = re.compile(
    r'(?:\$\{UV_BIN\}"|^\s*(?:run:\s*)?uv)\s+(?:sync|run)\b[^\n]*', re.MULTILINE
)


def _shell_pin(path):
    text = path.read_text()
    found = {}
    for field in FIELDS:
        # bash takes the last assignment, so count rather than take the first.
        matches = re.findall(rf'^\s*{field}="?([^"\n]+)"?$', text, re.MULTILINE)
        assert len(matches) == 1, (
            f"{path.name} declares {field} {len(matches)} times, expected once"
        )
        found[field] = matches[0]
    return found


@pytest.fixture(scope="module")
def pinned():
    return _shell_pin(SOURCE)


@pytest.mark.parametrize("path", COPIES, ids=lambda p: p.name)
def test_every_copy_pins_the_same_uv(path, pinned):
    assert _shell_pin(path) == pinned, (
        f"{path.relative_to(REPO)} pins a different uv from "
        f"{SOURCE.relative_to(REPO)}. Change both, or neither."
    )


@pytest.mark.parametrize("path", PIN_HOLDERS, ids=lambda p: p.name)
def test_a_uv_already_present_is_used_only_if_it_is_the_pinned_one(path):
    """Skipping the download is also how an unpinned uv would get in."""
    text = path.read_text()
    assert "uv_is_pinned" in text, (
        f"{path.relative_to(REPO)} does not check the version of a uv it finds"
    )
    assert "command -v uv" in text, (
        f"{path.relative_to(REPO)} ignores a uv already on PATH, which means "
        "downloading one on every build"
    )


@pytest.mark.parametrize("path", PIN_HOLDERS, ids=lambda p: p.name)
def test_the_pinned_version_is_the_one_downloaded(path):
    text = path.read_text()
    assert re.search(r"releases/download/\$\{UV_VERSION\}", text), (
        f"{path.relative_to(REPO)} does not build its download URL out of "
        "UV_VERSION, so the two can disagree"
    )
    hardcoded = re.findall(r"releases/download/([\d][^/$\s\"]*)", text)
    assert not hardcoded, (
        f"{path.relative_to(REPO)} downloads {hardcoded} by name, not UV_VERSION"
    )


@pytest.mark.parametrize("path", PIN_HOLDERS, ids=lambda p: p.name)
def test_the_pinned_checksum_is_the_one_verified(path):
    """UV_SHA256 can outlive the check that reads it."""
    text = path.read_text()
    assert re.search(r"\$\{UV_SHA256\}.*\|\s*sha256sum", text), (
        f"{path.relative_to(REPO)} declares UV_SHA256 but never checks a "
        "download against it"
    )


@pytest.mark.parametrize("path", PIN_HOLDERS, ids=lambda p: p.name)
def test_no_script_installs_whatever_uv_is_current(path):
    assert "astral.sh/uv/install.sh" not in path.read_text(), (
        f"{path.relative_to(REPO)} installs the current uv, not the pinned one"
    )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_workflow_sets_up_the_pinned_uv(path, pinned):
    text = path.read_text()
    assert "astral-sh/setup-uv" in text, f"{path.name} does not set up uv"
    versions = re.findall(r'^\s+version:\s*"([^"]+)"', text, re.MULTILINE)
    assert versions == [pinned["UV_VERSION"]], (
        f"{path.relative_to(REPO)} sets up uv {versions}, but "
        f"{SOURCE.relative_to(REPO)} pins {pinned['UV_VERSION']}."
    )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_pins_a_python_beside_it(path):
    """`python-version:` and `UV_PYTHON` both override .python-version."""
    # Strip comments: the workflows mention UV_PYTHON without setting it.
    text = re.sub(r"^\s*#.*$", "", path.read_text(), flags=re.MULTILINE)
    assert not re.search(r"^\s+python-version:", text, re.MULTILINE), (
        f"{path.relative_to(REPO)} pins a Python beside buildkite/.python-version"
    )
    assert "UV_PYTHON" not in text, (
        f"{path.relative_to(REPO)} sets UV_PYTHON, which overrides "
        "buildkite/.python-version"
    )


def test_the_pinned_python_is_the_one_the_packages_require():
    declared = (BUILDKITE / ".python-version").read_text().strip()
    for name in ("pyproject.toml", "ci_selector/pyproject.toml"):
        text = (BUILDKITE / name).read_text()
        match = re.search(r'requires-python = ">=([\d.]+)"', text)
        assert match, f"buildkite/{name} does not declare requires-python"
        assert match.group(1) == declared, (
            f"buildkite/{name} requires Python {match.group(1)}, but "
            f"buildkite/.python-version installs {declared}"
        )


@pytest.mark.parametrize("path", PIN_HOLDERS + WORKFLOWS, ids=lambda p: p.name)
def test_every_uv_command_takes_the_lockfile_as_given(path):
    """Without --locked uv resolves at build time and the lockfile decides
    nothing, which is the whole point of committing one."""
    # Join backslash continuations so a flag on the next line still counts.
    text = re.sub(r"\\\n\s*", " ", path.read_text())
    bare = [
        m.group(0) for m in UV_COMMAND.finditer(text) if "--locked" not in m.group(0)
    ]
    assert not bare, f"{path.relative_to(REPO)} runs uv without --locked: {bare}"


@pytest.mark.parametrize("path", PIN_HOLDERS, ids=lambda p: p.name)
def test_no_script_names_a_python_of_its_own(path):
    """`uv --python X` is a second Python pin, out of reach of the above."""
    declared = (BUILDKITE / ".python-version").read_text().strip()
    named = set(re.findall(r"--python\s+([\d.]+)", path.read_text()))
    assert named <= {declared}, (
        f"{path.relative_to(REPO)} asks uv for Python {sorted(named)}, but "
        f"buildkite/.python-version installs {declared}"
    )
