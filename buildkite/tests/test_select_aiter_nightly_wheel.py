import datetime
import importlib.metadata
import types

import pytest

from buildkite.scripts import select_aiter_nightly_wheel as script

INDEX_URL = script.INDEX_URL


def _name(rocm, commit, date, torch="2.12", version="0.1.25", python="cp312"):
    return (
        f"amd_aiter-{version}+rocm{rocm}.torch{torch}.{commit}.d{date}"
        f"-{python}-{python}-linux_x86_64.whl"
    )


NEWEST = _name("7.2.3", "bcb56d9", "20260928", version="0.1.24")
LISTED = [
    _name("7.2.3", "11d3852", "20260927"),  # a newer version label, an older build
    NEWEST,
    _name("7.2.3", "ddddddd", "20260928", torch="2.13"),  # same night, other torch
    _name("7.2.3", "ccccccc", "20260929", python="cp313"),
    _name("10.1.0a20260910", "bbbbbbb", "20260929"),  # pre-release stamp
]
NEWEST_URL = INDEX_URL + NEWEST


def _index(hrefs):
    links = "".join(f'<a href="{href}">x</a>' for href in hrefs)
    return f"<html><body>{links}</body></html>"


def test_parses_wheel_names():
    wheels = script.parse_index(_index(LISTED), INDEX_URL)
    assert [(w.rocm, w.torch, w.commit, w.python) for w in wheels] == [
        ("7.2.3", "2.12", "11d3852", "cp312"),  # a commit of digits is its own part
        ("7.2.3", "2.12", "bcb56d9", "cp312"),
        ("7.2.3", "2.13", "ddddddd", "cp312"),
        ("7.2.3", "2.12", "ccccccc", "cp313"),
        ("10.1.0", "2.12", "bbbbbbb", "cp312"),
    ]
    assert wheels[1].version == "0.1.24+rocm7.2.3.torch2.12.bcb56d9.d20260928"
    assert wheels[1].build_date == datetime.date(2026, 9, 28)


def test_fields_added_to_the_name_later_are_ignored():
    local = "triton3.5.libtorch9.9.rocm7.2.3.torch2.12.ae984f5.d20261008.extra1"
    name = f"amd_aiter-0.1.25+{local}-cp312-cp312-linux_x86_64.whl"
    wheel = script.wheel_from_url(INDEX_URL + name)
    assert (wheel.rocm, wheel.torch, wheel.commit) == ("7.2.3", "2.12", "ae984f5")
    assert wheel.build_date == datetime.date(2026, 10, 8)


def test_keeps_links_as_linked_and_skips_other_files():
    href = f"../../packages/{NEWEST.replace('+', '%2B')}#sha256=abc"
    other = [
        "amd_aiter-0.1.25.tar.gz",
        "other-1.0-cp312-cp312-linux_x86_64.whl",
        # Before 2026-10-08, with no torch: what it was built against is unknown.
        "amd_aiter-0.1.25+rocm7.2.3.e4ceb9d.d20261007-cp312-cp312-linux_x86_64.whl",
    ]
    (wheel,) = script.parse_index(_index([href, *other, "../"]), INDEX_URL)
    assert wheel.filename == NEWEST
    assert wheel.url == "https://rocm.frameworks-nightlies.amd.com/" + href[6:]


def test_selects_the_newest_build_for_this_torch_not_the_highest_version():
    wheels = script.parse_index(_index(LISTED), INDEX_URL)
    select = script.select_wheel
    assert select(wheels, "7.2.3", "2.12", "cp312").filename == NEWEST
    assert select(wheels, "7.2.3", "2.13", "cp312").commit == "ddddddd"


def test_versions_as_wheel_names_spell_them():
    assert script.rocm_release("7.2.3-49\n") == "7.2.3"
    assert script.torch_release("2.12.0a0+git6bbd260") == "2.12"
    for parse in (script.rocm_release, script.torch_release):
        with pytest.raises(ValueError):
            parse("unknown")


@pytest.fixture
def image(monkeypatch, tmp_path):
    """An image on ROCm 7.2.3, torch 2.12 and Python 3.12, seeing LISTED on
    2026-09-29. Tests vary it through `rocm_file` and `torch`."""
    image = types.SimpleNamespace(
        rocm_file=tmp_path / "version", torch="2.12.0a0+git6bbd260"
    )
    image.rocm_file.write_text("7.2.3-49\n")

    def version(name):
        if image.torch is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return image.torch

    monkeypatch.setattr(script.importlib.metadata, "version", version)
    monkeypatch.setattr(script, "ROCM_VERSION_FILE", image.rocm_file)
    monkeypatch.setattr(script, "python_tag", lambda _: "cp312")
    monkeypatch.setattr(script, "fetch", lambda url: _index(LISTED))
    monkeypatch.setattr(script, "_today", lambda: datetime.date(2026, 9, 29))
    return image


def _main(capsys, *args):
    status = script.main(list(args))
    return status, capsys.readouterr().out


def test_prints_the_newest_wheel_then_its_annotation(image, capsys):
    status, out = _main(capsys)
    url, note = out.split("\n", 1)
    assert (status, url) == (0, NEWEST_URL)
    # Built yesterday: not stale.
    assert note.startswith(":crescent_moon: **AITER nightly**: main at [`bcb56d9`]")
    assert "built 2026-09-28 for torch 2.12" in note


def test_warns_on_a_stale_wheel(image, monkeypatch, capsys):
    monkeypatch.setattr(script, "_today", lambda: datetime.date(2026, 9, 30))
    note = _main(capsys)[1].split("\n", 1)[1]
    assert note.startswith(":warning: **AITER nightly is 2 days old**")
    assert "rocm7.2.3/torch2.12/cp312" in note


def test_retry_reselects_its_wheel_without_reading_the_index(
    image, monkeypatch, capsys
):
    pinned = INDEX_URL + LISTED[0]  # not the newest
    monkeypatch.setattr(script, "fetch", lambda url: pytest.fail("read index"))
    status, out = _main(capsys, "--wheel-url", pinned)
    assert (status, out.split("\n", 1)[0]) == (0, pinned)


TORCH = "2.12.0"
PIN = "--wheel-url"


@pytest.mark.parametrize(
    ("rocm", "torch", "args", "message"),
    [
        ("7.3.0", TORCH, [], "No AITER nightly for rocm7.3.0/torch2.12/cp312. Avail"),
        ("unknown", TORCH, [], "Cannot read this image's ROCm version"),
        (None, TORCH, [], "Cannot read this image's ROCm version"),
        ("7.2.3", None, [], "Cannot read this image's torch version"),
        # A retry's pin must fit the image too.
        ("7.2.3", TORCH, [PIN, INDEX_URL + LISTED[3]], "No AITER nightly for"),
        ("7.2.3", TORCH, [PIN, INDEX_URL + "x.whl"], "Not an AITER nightly wheel"),
    ],
)
def test_exits_10_when_no_wheel_fits(image, capsys, rocm, torch, args, message):
    if rocm is None:
        image.rocm_file.unlink()
    else:
        image.rocm_file.write_text(rocm)
    image.torch = torch
    status, out = _main(capsys, *args)
    assert status == 10
    assert message in out
