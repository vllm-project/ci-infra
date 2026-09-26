# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""The kernel record: the same three lines as the Python record, for csrc.

    S's row launched a kernel from F              -> select, map gets no vote
    S's USABLE row launched none, F is CLEARABLE   -> drop, if F is all the map had on S
    no row, or F is nothing the map answers for    -> the map decides

Tables and maps are written here in the shapes `kernrec/kernel_table.py` and
vLLM's `tools/ci/kernel_symbol_map.py` produce, so a loader test failing here
means one of those moved.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest
from ci_selector.codemap.claim import OUTPUT_RULES
from ci_selector.codemap.selection import Selection
from ci_selector.coverage.kernels import (
    FILELESS_RULES,
    TABLE_VERSION,
    KernelEvidence,
    KernelRow,
    load_symbol_map,
    load_table,
    read_pr,
)
from ci_selector.coverage.rules import RowKeys
from ci_selector.coverage.table import Table
from ci_selector.decide import KERNEL_ATTRIBUTION_ENV, KERNEL_UNMATCHED_ENV, decide

KERNREC = Path(__file__).resolve().parents[2] / "recorders" / "kernrec"


# --- writers in the producers' shapes ---------------------------------------


def _gz(path: Path, payload: dict) -> Path:
    with gzip.open(path, "wt") as f:
        json.dump(payload, f)
    return path


def _table(tmp_path: Path, rows: dict[str, dict], commit="abc", version=TABLE_VERSION):
    """rows: bare step key -> {kernels, passed, complete, dropped}."""
    names = sorted({k for r in rows.values() for k in r.get("kernels", ())})
    idx = {n: i for i, n in enumerate(names)}
    blobs = {
        key: {
            "jobs": 1,
            "passed": r.get("passed", True),
            "complete": r.get("complete", True),
            "shards": {"expected": None, "seen": 1},
            "processes": 1,
            "dropped": r.get("dropped", 0),
            "kernels": sorted(idx[k] for k in r.get("kernels", ())),
        }
        for key, r in rows.items()
    }
    return _gz(
        tmp_path / "kernel_table.json.gz",
        {
            "version": version,
            "source": {"org": "vllm", "pipeline": "ci", "build": 7, "commit": commit},
            "names": names,
            "rows": blobs,
            "stats": {},
        },
    )


def _obj(source: str, symbols=(), deps=(), error: str | None = None) -> dict:
    d = {
        "source": source,
        "target": "_C",
        "object": f"CMakeFiles/_C.dir/{source}.o",
        "device": bool(symbols),
        "symbols": list(symbols),
        "deps": [source, *deps],
    }
    if error:
        d["error"] = error
    return d


def _map(tmp_path: Path, objects: list[dict], commit="abc", **extra) -> Path:
    return _gz(
        tmp_path / "kernel_symbol_map.json.gz",
        {"version": 1, "commit": commit, "objects": objects, "stats": {}, **extra},
    )


def _evidence(tmp_path, rows, objects, table_commit="abc", map_commit="abc", **extra):
    return KernelEvidence(
        load_table(_table(tmp_path, rows, table_commit)),
        load_symbol_map(_map(tmp_path, objects, map_commit, **extra)),
    )


class FakeStep:
    def __init__(self, manual_only=False):
        self.manual_only = manual_only


def _keys(*bare: str, manual: tuple[str, ...] = ()) -> RowKeys:
    """`RowKeys` without a pipeline: bare keys spell themselves."""
    steps = {f"vllm_ci:{k}": FakeStep(k in manual) for k in bare}
    return RowKeys({"vllm_ci"}, {"vllm_ci": 1.0}, steps=steps)


def _selection(
    by_file: dict[str, list[str]],
    rules: dict[str, str] | None = None,
    fileless: dict[str, str] | None = None,
) -> Selection:
    """A map selection. `by_file`: path -> bare keys it selected, recorded the
    way `selection._record` does. `rules`: bare key -> rule name, default the
    image-copy rule a csrc file selects most steps by. `fileless`: bare key ->
    rule, for steps selected with no file behind them."""
    sel = Selection()
    for path, keys in by_file.items():
        for k in keys:
            sid = f"vllm_ci:{k}"
            sel.selected.setdefault(sid, []).append(f"{path}: copied into the image")
            sel.selected_rules.setdefault(sid, []).append(
                (rules or {}).get(k, "image-copy")
            )
            sel.selected_paths.setdefault(sid, []).append(None)
            seen = sel.selected_by_file.setdefault(path, [])
            if sid not in seen:
                seen.append(sid)
    for k, rule in (fileless or {}).items():
        sid = f"vllm_ci:{k}"
        sel.selected.setdefault(sid, []).append("always-run key shortcut")
        sel.selected_rules.setdefault(sid, []).append(rule)
        sel.selected_paths.setdefault(sid, []).append(None)
    return sel


NO_HELD = frozenset()


def _read(evidence, sel, changed, keys, held=None, **kw):
    return read_pr(evidence, sel, changed, keys, held=held or (lambda p: set()), **kw)


# --- loaders ------------------------------------------------------------------


def test_loaders_fail_safe(tmp_path):
    missing = load_table(tmp_path / "nope.json.gz")
    assert not missing.available and "nope.json.gz" in missing.unavailable

    garbage = tmp_path / "g.json.gz"
    garbage.write_bytes(b"this is not gzip")
    assert "cannot parse" in load_table(garbage).unavailable
    assert "cannot parse" in load_symbol_map(garbage).unavailable

    v1 = _table(tmp_path, {"s": {"kernels": ["k"]}}, version=1)
    assert "version 1" in load_table(v1).unavailable

    empty = _table(tmp_path, {})
    assert "contains no rows" in load_table(empty).unavailable

    # A health field missing is not "healthy", it is unreadable.
    p = _table(tmp_path, {"s": {"kernels": ["k"]}})
    payload = json.load(gzip.open(p, "rt"))
    del payload["rows"]["s"]["complete"]
    _gz(p, payload)
    assert "unreadable row" in load_table(p).unavailable

    # The producer failed closed: no objects, a reason.
    m = _map(tmp_path, [], reason="1 objects unreadable by cuobjdump", incomplete=True)
    assert "cuobjdump" in load_symbol_map(m).unavailable
    assert "no objects" in load_symbol_map(_map(tmp_path, [])).unavailable
    payload = json.load(gzip.open(_map(tmp_path, [_obj("csrc/a.cu", ["kA"])]), "rt"))
    payload["version"] = 2
    assert (
        "version 2"
        in load_symbol_map(_gz(tmp_path / "v2.json.gz", payload)).unavailable
    )


def test_table_reads_rows_and_health(tmp_path):
    t = load_table(
        _table(
            tmp_path,
            {
                "ok": {"kernels": ["kA", "kB"]},
                "failed": {"kernels": ["kA"], "passed": False},
                "partial": {"kernels": [], "complete": False},
                "lossy": {"kernels": ["kB"], "dropped": 3},
            },
            commit="c0ffee",
        )
    )
    assert t.available and len(t) == 4 and t.commit == "c0ffee" and t.build == 7
    assert t.row("ok").kernels == {"kA", "kB"} and t.row("ok").usable
    assert not t.row("failed").usable
    assert not t.row("partial").usable
    assert not t.row("lossy").usable
    assert t.row("missing") is None


def test_usable_mirrors_kernrec():
    def row(**kw):
        health = {"passed": True, "complete": True, "dropped": 0, **kw}
        return KernelRow("s", frozenset(), **health)

    assert row().usable
    assert not row(passed=False).usable
    assert not row(complete=False).usable
    assert not row(dropped=1).usable


def test_symbol_map_reach_host_and_unknown(tmp_path):
    sm = load_symbol_map(
        _map(
            tmp_path,
            [
                _obj("csrc/a.cu", ["kA"], deps=["csrc/shared.h", "csrc/types.hpp"]),
                _obj("csrc/b.cu", ["kB"], deps=["csrc/shared.h"]),
                _obj("csrc/bindings.cpp", [], deps=["csrc/types.hpp"]),
                _obj(
                    "csrc/bad.cu",
                    [],
                    deps=["csrc/bad.h"],
                    error="cuobjdump: cannot open",
                ),
            ],
        )
    )
    assert sm.available and sm.objects == 4
    # a header reaches every kernel compiled with it
    assert sm.symbols("csrc/shared.h") == {"kA", "kB"}
    assert sm.why_not_clearable("csrc/shared.h") == ""
    assert sm.why_not_clearable("csrc/a.cu") == ""
    # a header host code includes may select but not drop
    assert sm.symbols("csrc/types.hpp") == {"kA"}
    assert "without kernels" in sm.why_not_clearable("csrc/types.hpp")
    assert "without kernels" in sm.why_not_clearable("csrc/bindings.cpp")
    # an unreadable object makes a silence about its files worthless
    assert "could not be read" in sm.why_not_clearable("csrc/bad.h")
    assert sm.knows("csrc/bad.cu") and not sm.knows("csrc/cpu/attention.cpp")


def test_drift_kernel_table_version_matches_kernrec():
    """The producer and this reader must agree on the shape or every fetched
    table reads as unavailable and csrc quietly runs on the map alone."""
    spec = importlib.util.spec_from_file_location(
        "kernel_table", KERNREC / "kernel_table.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.TABLE_VERSION == TABLE_VERSION


def test_fileless_rules_are_real_rules():
    assert FILELESS_RULES <= OUTPUT_RULES
    assert "kernels" in OUTPUT_RULES, "the CLI writes it for steps this record added"


# --- the reading ----------------------------------------------------------------


def test_three_lines_and_every_gate(tmp_path):
    ev = _evidence(
        tmp_path,
        {
            "launched": {"kernels": ["kA", "kZ"]},
            "silent": {"kernels": ["kZ"]},
            "failed": {"kernels": ["kZ"], "passed": False},
            "also-python": {"kernels": ["kZ"]},
            "declared": {"kernels": ["kZ"]},
            "always": {"kernels": ["kZ"]},
            "missed": {"kernels": ["kA"]},
        },
        [_obj("csrc/a.cu", ["kA"]), _obj("csrc/other.cu", ["kZ"])],
    )
    sel = _selection(
        {
            "csrc/a.cu": [
                "launched",
                "silent",
                "failed",
                "also-python",
                "declared",
                "no-row",
            ],
            "vllm/x.py": ["also-python"],
        },
        fileless={"always": "always-run"},
    )
    keys = _keys(
        "launched",
        "silent",
        "failed",
        "also-python",
        "declared",
        "no-row",
        "always",
        "missed",
    )
    r = _read(
        ev, sel, ["csrc/a.cu", "vllm/x.py"], keys, held=lambda p: {"vllm_ci:declared"}
    )

    assert r.dropped == ["vllm_ci:silent"]
    assert r.added == ["vllm_ci:missed"], (
        "a row launching kA selects a step the map missed"
    )
    assert r.files == {"csrc/a.cu": "1 kernel symbols; may select and drop"}
    assert r.reasons["row-launched-a-kernel-from-a-changed-file"] == 1
    assert r.reasons["row-not-usable"] == 1
    assert r.reasons["selected-by-a-file-outside-the-map"] == 1
    assert r.reasons["held-by-declaration-or-build"] == 1
    assert r.reasons["selected-without-a-file"] == 1
    assert r.reasons["no-row"] == 1
    assert r.reasons["row-launched-none-of-its-kernels"] == 1
    assert set(r.kept) | set(r.dropped) == set(sel.selected)


def test_header_included_by_host_code_selects_but_never_drops(tmp_path):
    ev = _evidence(
        tmp_path,
        {"silent": {"kernels": ["kZ"]}, "missed": {"kernels": ["kA"]}},
        [
            _obj("csrc/a.cu", ["kA"], deps=["csrc/h.h"]),
            _obj("csrc/bindings.cpp", [], deps=["csrc/h.h"]),
            _obj("csrc/z.cu", ["kZ"]),
        ],
    )
    sel = _selection({"csrc/h.h": ["silent"]})
    r = _read(ev, sel, ["csrc/h.h"], _keys("silent", "missed"))
    assert r.added == ["vllm_ci:missed"] and r.dropped == []
    assert r.reasons["file-may-only-select"] >= 1
    assert "without kernels" in r.files["csrc/h.h"]


def test_unreadable_object_blocks_drops_for_its_files(tmp_path):
    ev = _evidence(
        tmp_path,
        {"silent": {"kernels": []}},
        [_obj("csrc/bad.cu", [], error="cuobjdump failed"), _obj("csrc/a.cu", ["kA"])],
    )
    sel = _selection({"csrc/bad.cu": ["silent"]})
    r = _read(ev, sel, ["csrc/bad.cu"], _keys("silent"))
    assert r.dropped == [] and "could not be read" in r.files["csrc/bad.cu"]


def test_file_outside_the_map_leaves_the_map_alone(tmp_path):
    ev = _evidence(tmp_path, {"silent": {"kernels": []}}, [_obj("csrc/a.cu", ["kA"])])
    sel = _selection({"csrc/cpu/attention.cpp": ["silent"]})
    r = _read(ev, sel, ["csrc/cpu/attention.cpp", "docs/x.md"], _keys("silent"))
    assert r.dropped == [] and r.added == []
    assert (
        r.reasons["file-not-in-map"] == 1
        and "not in the map" in r.files["csrc/cpu/attention.cpp"]
    )
    assert "docs/x.md" not in r.files, "only csrc files are reported as outside the map"
    assert r.reasons["selected-by-a-file-outside-the-map"] == 1


def test_unmatched_pair_and_stale_file_only_select(tmp_path):
    ev = _evidence(
        tmp_path,
        {"silent": {"kernels": []}, "missed": {"kernels": ["kA"]}},
        [_obj("csrc/a.cu", ["kA"])],
        table_commit="abc",
        map_commit="def",
    )
    assert not ev.matched and "DIFFERENT COMMITS" in ev.describe()
    sel = _selection({"csrc/a.cu": ["silent"]})
    keys = _keys("silent", "missed")
    r = _read(ev, sel, ["csrc/a.cu"], keys, allow_drops=False)
    assert r.dropped == [] and r.added == ["vllm_ci:missed"]
    assert r.reasons["table-and-map-from-different-commits"] == 1

    r = _read(
        ev, sel, ["csrc/a.cu"], keys, allow_drops=True, stale=frozenset({"csrc/a.cu"})
    )
    assert r.dropped == [] and "changed between" in r.files["csrc/a.cu"]


def test_python_record_positive_evidence_holds_the_step(tmp_path):
    ev = _evidence(tmp_path, {"silent": {"kernels": []}}, [_obj("csrc/a.cu", ["kA"])])
    sel = _selection({"csrc/a.cu": ["silent"]})
    r = _read(
        ev, sel, ["csrc/a.cu"], _keys("silent"), protected=frozenset({"vllm_ci:silent"})
    )
    assert r.dropped == [] and r.reasons["held-by-the-python-record"] == 1


def test_a_drop_needs_every_selecting_file_cleared(tmp_path):
    ev = _evidence(
        tmp_path,
        {
            "runs-b": {"kernels": ["kB"]},
            "runs-none": {"kernels": []},
            "runs-none-2": {"kernels": []},
        },
        [
            _obj("csrc/a.cu", ["kA"]),
            _obj("csrc/b.cu", ["kB"]),
            _obj("csrc/hosty.cu", ["kH"], deps=["csrc/hosty.h"]),
            _obj("csrc/bindings.cpp", [], deps=["csrc/hosty.h"]),
        ],
    )
    sel = _selection(
        {
            "csrc/a.cu": ["runs-b", "runs-none", "runs-none-2"],
            "csrc/b.cu": ["runs-b", "runs-none"],
            "csrc/hosty.h": ["runs-none-2"],
        }
    )
    r = _read(
        ev,
        sel,
        ["csrc/a.cu", "csrc/b.cu", "csrc/hosty.h"],
        _keys("runs-b", "runs-none", "runs-none-2"),
    )
    assert r.dropped == ["vllm_ci:runs-none"], (
        "runs-b launched kB; runs-none-2 was also selected by a header host code includes"
    )


def test_a_manual_only_step_that_launched_the_kernel_is_added(tmp_path, monkeypatch):
    """Optional steps are addable by default, as in the Python record; set to 0
    the switch keeps them out."""
    from ci_selector.coverage.rules import RECORD_OPTIONAL_ENV

    ev = _evidence(
        tmp_path, {"nightly": {"kernels": ["kA"]}}, [_obj("csrc/a.cu", ["kA"])]
    )
    keys = _keys("nightly", manual=("nightly",))
    monkeypatch.delenv(RECORD_OPTIONAL_ENV, raising=False)
    r = _read(ev, _selection({}), ["csrc/a.cu"], keys)
    assert r.added == ["vllm_ci:nightly"]
    monkeypatch.setenv(RECORD_OPTIONAL_ENV, "0")
    r = _read(ev, _selection({}), ["csrc/a.cu"], keys)
    assert r.added == []


def test_attribution_narrows_a_file_to_its_changed_kernels(tmp_path):
    """Two kernels in one file; only kA changed. The step that launched kB
    alone is held by the whole-file reading and dropped by the narrowed one."""
    ev = _evidence(
        tmp_path,
        {"runs-a": {"kernels": ["kA"]}, "runs-b": {"kernels": ["kB"]}},
        [_obj("csrc/a.cu", ["kA", "kB"])],
    )
    sel = _selection({"csrc/a.cu": ["runs-a", "runs-b"]})
    keys = _keys("runs-a", "runs-b")
    whole = _read(ev, sel, ["csrc/a.cu"], keys)
    assert whole.dropped == []
    narrowed = _read(
        ev,
        sel,
        ["csrc/a.cu"],
        keys,
        attribute=lambda p, syms: (frozenset({"kA"}), "changed: kA"),
    )
    assert narrowed.dropped == ["vllm_ci:runs-b"]
    assert narrowed.reasons["file-narrowed-to-changed-kernels"] == 1
    assert narrowed.files["csrc/a.cu"].startswith("1 of 2 kernel symbols (changed: kA)")
    # names the map has no symbols for (a kernel new in this PR): whole file
    fresh = _read(
        ev,
        sel,
        ["csrc/a.cu"],
        keys,
        attribute=lambda p, syms: (frozenset({"kNew"}), "changed: kNew"),
    )
    assert fresh.dropped == [] and "none of them in the map" in fresh.files["csrc/a.cu"]
    # the attribution declining (whole file) reads like no attribution at all
    declined = _read(
        ev,
        sel,
        ["csrc/a.cu"],
        keys,
        attribute=lambda p, syms: (frozenset(), "line 3 is outside every function"),
    )
    assert declined.dropped == [] and "whole file" in declined.files["csrc/a.cu"]


# --- decide() wiring --------------------------------------------------------------


class _Step:
    def __init__(self, key, deps=(), manual_only=False):
        self.key = key
        self.label = key.title()
        self.step_id = f"vllm_ci:{key}"
        self.buildkite_key = key
        self.manual_only = manual_only
        self.source_file_dependencies = list(deps)


def _state(*steps: _Step):
    return SimpleNamespace(
        pipelines=[
            SimpleNamespace(config=SimpleNamespace(name="vllm_ci"), steps=list(steps))
        ],
        artifacts=SimpleNamespace(producers_of={}, self_builders={}),
    )


@pytest.fixture
def csrc_diff(tmp_repo):
    tmp_repo.write("csrc/a.cu", "__global__ void kA() {}\n")
    base = tmp_repo.commit("add kernel")
    tmp_repo.write("csrc/a.cu", "__global__ void kA() { /* faster */ }\n")
    head = tmp_repo.commit("edit kernel")
    root = Path(tmp_repo.git("rev-parse", "--show-toplevel").strip())
    return root, base, head


def test_decide_applies_the_kernel_record(tmp_path, csrc_diff, monkeypatch):
    monkeypatch.delenv(KERNEL_UNMATCHED_ENV, raising=False)
    root, base, head = csrc_diff
    ev = _evidence(
        tmp_path,
        {
            "launched": {"kernels": ["kA"]},
            "silent": {"kernels": []},
            "declared": {"kernels": []},
        },
        [_obj("csrc/a.cu", ["kA"])],
    )
    sel = _selection({"csrc/a.cu": ["launched", "silent", "declared"]})
    state = _state(
        _Step("launched"), _Step("silent"), _Step("declared", deps=["csrc/a.cu"])
    )
    no_python_table = Table(None, unavailable="no table")

    d = decide(state, sel, root, base, head, table=no_python_table, kernels=ev)
    assert d.used_kernels and not d.used_coverage
    assert d.dropped_by_kernels == {"vllm_ci:silent"}
    assert d.steps == {"vllm_ci:launched", "vllm_ci:declared"}
    assert d.kernel_reasons["held-by-declaration-or-build"] == 1, (
        "a step naming the file in source_file_dependencies is the floor"
    )
    assert "abc" in d.kernel_pair and "DIFFERENT" not in d.kernel_pair
    assert d.kernel_files["csrc/a.cu"].endswith("; may select and drop")
    assert d.kernel_files["csrc/a.cu"].startswith("1 of 1 kernel symbols (changed: kA)")


TWO_KERNELS = """\
__global__ void kA(float* p) {
  p[0] = 1.0f;
}

__global__ void kB(float* p) {
  p[0] = 2.0f;
}
"""


def test_decide_attributes_per_kernel_by_default(tmp_path, tmp_repo, monkeypatch):
    monkeypatch.delenv(KERNEL_UNMATCHED_ENV, raising=False)
    tmp_repo.write("csrc/two.cu", TWO_KERNELS)
    base = tmp_repo.commit("two kernels")
    tmp_repo.write("csrc/two.cu", TWO_KERNELS.replace("p[0] = 1.0f;", "p[0] = 1.5f;"))
    head = tmp_repo.commit("edit kA")
    root = Path(tmp_repo.git("rev-parse", "--show-toplevel").strip())
    ev = _evidence(
        tmp_path,
        {"runs-a": {"kernels": ["_Z2kAPf"]}, "runs-b": {"kernels": ["_Z2kBPf"]}},
        [_obj("csrc/two.cu", ["_Z2kAPf", "_Z2kBPf"])],
    )
    sel = _selection({"csrc/two.cu": ["runs-a", "runs-b"]})
    state = _state(_Step("runs-a"), _Step("runs-b"))
    no_table = Table(None, unavailable="x")

    monkeypatch.delenv(KERNEL_ATTRIBUTION_ENV, raising=False)
    d = decide(state, sel, root, base, head, table=no_table, kernels=ev)
    assert d.dropped_by_kernels == {"vllm_ci:runs-b"}, d.kernel_files
    assert "1 of 2 kernel symbols" in d.kernel_files["csrc/two.cu"]

    monkeypatch.setenv(KERNEL_ATTRIBUTION_ENV, "file")
    d = decide(state, sel, root, base, head, table=no_table, kernels=ev)
    assert d.dropped_by_kernels == set(), "both steps launched a kernel of the file"

    monkeypatch.setenv(KERNEL_ATTRIBUTION_ENV, "per-line")
    with pytest.raises(ValueError):
        decide(state, sel, root, base, head, table=no_table, kernels=ev)


def test_decide_unmatched_pair_drops_only_on_opt_in(tmp_path, csrc_diff, monkeypatch):
    root, base, head = csrc_diff
    ev = _evidence(
        tmp_path,
        {"silent": {"kernels": []}},
        [_obj("csrc/a.cu", ["kA"])],
        map_commit="def",
    )
    sel = _selection({"csrc/a.cu": ["silent"]})
    state = _state(_Step("silent"))
    monkeypatch.delenv(KERNEL_UNMATCHED_ENV, raising=False)
    d = decide(
        state, sel, root, base, head, table=Table(None, unavailable="x"), kernels=ev
    )
    assert d.dropped_by_kernels == set() and "DIFFERENT COMMITS" in d.kernel_pair
    assert d.kernel_reasons["table-and-map-from-different-commits"] == 1
    monkeypatch.setenv(KERNEL_UNMATCHED_ENV, "1")
    d = decide(
        state, sel, root, base, head, table=Table(None, unavailable="x"), kernels=ev
    )
    assert d.dropped_by_kernels == {"vllm_ci:silent"}


def test_decide_without_a_state_or_with_bad_evidence_changes_nothing(
    tmp_path, csrc_diff
):
    root, base, head = csrc_diff
    ev = _evidence(tmp_path, {"silent": {"kernels": []}}, [_obj("csrc/a.cu", ["kA"])])
    sel = _selection({"csrc/a.cu": ["silent"]})
    d = decide(
        None, sel, root, base, head, table=Table(None, unavailable="x"), kernels=ev
    )
    assert d.steps == {"vllm_ci:silent"} and "no pipeline state" in d.kernel_note

    broken = KernelEvidence(load_table(tmp_path / "missing.json.gz"), ev.symbol_map)
    d = decide(
        _state(_Step("silent")),
        sel,
        root,
        base,
        head,
        table=Table(None, unavailable="x"),
        kernels=broken,
    )
    assert d.steps == {"vllm_ci:silent"} and "missing.json.gz" in d.kernel_note


def test_fetch_kernel_evidence_reads_the_configured_paths(tmp_path, monkeypatch):
    from ci_selector.coverage.source import (
        KERNEL_MAP_ENV,
        KERNEL_TABLE_ENV,
        fetch_kernel_evidence,
    )

    monkeypatch.setenv(KERNEL_TABLE_ENV, str(tmp_path / "none.json.gz"))
    monkeypatch.setenv(KERNEL_MAP_ENV, str(tmp_path / "none-map.json.gz"))
    ev = fetch_kernel_evidence()
    assert "ci-fetch-kernel-record" in ev.unavailable

    t = _table(tmp_path, {"s": {"kernels": ["kA"]}})
    m = _map(tmp_path, [_obj("csrc/a.cu", ["kA"])])
    assert fetch_kernel_evidence(t, m).unavailable == ""
    monkeypatch.setenv(KERNEL_TABLE_ENV, str(t))
    monkeypatch.setenv(KERNEL_MAP_ENV, str(m))
    assert fetch_kernel_evidence().matched


# --- the fetch script -----------------------------------------------------------------


def _serve(monkeypatch, files: dict[str, bytes], status_for_missing=403):
    from ci_selector.scripts import fetch_kernels

    def fake_get(url: str) -> bytes:
        if url in files:
            return files[url]
        raise urllib.error.HTTPError(url, status_for_missing, "denied", {}, None)

    monkeypatch.setattr(fetch_kernels, "_get", fake_get)
    return fetch_kernels


def test_fetch_script_follows_latest_and_validates(tmp_path, monkeypatch):
    base = "https://bucket/ci"
    t = _table(tmp_path, {"s": {"kernels": ["kA"]}}, commit="c1")
    m = _map(tmp_path, [_obj("csrc/a.cu", ["kA"])], commit="c1")
    files = {
        f"{base}/latest.json": json.dumps({"commit": "c1", "build": 9}).encode(),
        f"{base}/c1/kernel_table.json.gz": t.read_bytes(),
        f"{base}/c1/kernel_symbol_map.json.gz": m.read_bytes(),
    }
    mod = _serve(monkeypatch, files)
    out = tmp_path / "out"
    assert mod.main(["--url", base, "--out", str(out)]) == 0
    assert load_table(out / "kernel_table.json.gz").commit == "c1"
    assert load_symbol_map(out / "kernel_symbol_map.json.gz").commit == "c1"

    # an unmatched pair is refused and the good pair on disk is untouched
    files[f"{base}/c1/kernel_symbol_map.json.gz"] = _map(
        tmp_path / "other" if (tmp_path / "other").mkdir() is None else tmp_path,
        [_obj("csrc/a.cu", ["kA"])],
        commit="c2",
    ).read_bytes()
    assert mod.main(["--url", base, "--out", str(out)]) == 1
    assert load_symbol_map(out / "kernel_symbol_map.json.gz").commit == "c1"


def test_fetch_script_reports_nothing_published(tmp_path, monkeypatch):
    mod = _serve(monkeypatch, {})
    assert mod.main(["--url", "https://bucket/ci", "--out", str(tmp_path / "o")]) == 1
    assert not (tmp_path / "o" / "kernel_table.json.gz").exists()
