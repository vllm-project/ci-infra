# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Rust workspace parse: crate closures from the two shipped artifacts.

The two-root discriminator rests on which artifact a crate feeds; these pin
the parse floor and the fail-open direction so a moved workspace reads as
loud failure or as the widest RUST answer, not as the image union.
"""

from pathlib import Path

import pytest
from ci_selector.codemap.rust_workspace import RustWorkspace


@pytest.fixture(scope="module")
def ws(vllm_repo):
    return RustWorkspace.build(vllm_repo)


def test_workspace_parse_floor(ws):
    """Derivation collapsing to nothing must fail loudly: ten members and
    both artifact closures are far below today's fourteen, so ordinary
    churn passes while a moved workspace or renamed root does not."""
    assert len(ws.members) >= 10, sorted(ws.members)
    assert len(ws.binary_crates) >= 8, sorted(ws.binary_crates)
    assert ws.cdylib_crates == {
        "rust/src/parser/python",
        "rust/src/parser",
        "rust/src/tokenizer",
    }


def test_buckets_follow_artifact_reach(ws):
    assert ws.bucket_of("rust/src/server/src/lib.rs") == "binary"
    assert ws.bucket_of("rust/src/tokenizer/src/lib.rs") == "cdylib"
    assert ws.bucket_of("rust/Cargo.lock") == "root"
    assert ws.bucket_of("rust-toolchain.toml") == "root"
    assert ws.bucket_of("build_rust.sh") == "root"
    assert ws.bucket_of("tools/build_rust.py") == "root"


def test_mock_engine_feeds_no_artifact_but_stays_binary(ws):
    """A dev fixture only cargo compiles; calling it nothing-to-run would
    silence a cargo-visible crate for a saving of zero steps, since the
    cargo steps ride in every bucket anyway."""
    assert ws.bucket_of("rust/src/mock-engine/src/lib.rs") == "binary"


def _outside_every_member(ws: RustWorkspace, candidate: str) -> str:
    """A crate directory the workspace does not know, derived from the ones it
    does, so a crate joining the workspace moves the probe instead of breaking
    the test. A candidate already inside a member cannot be lengthened out of
    one, so that is a caller mistake."""
    inside = sorted(m for m in ws.members if candidate.startswith(m + "/"))
    assert not inside, f"{candidate} already lives inside {inside}"
    while any(m == candidate or m.startswith(candidate + "/") for m in ws.members):
        candidate += "x"
    return candidate


def _loose_rust_files(repo: Path, ws: RustWorkspace) -> list[str]:
    """Real files under rust/ that no member contains. target/ is a build
    tree rather than source, so it is pruned instead of walked."""
    loose: list[str] = []
    stack = [repo / "rust"]
    while stack:
        for p in sorted(stack.pop().iterdir()):
            rel = p.relative_to(repo).as_posix()
            if p.name.startswith(".") or p.name == "target":
                continue
            if any(rel == m or rel.startswith(m + "/") for m in ws.members):
                continue
            if p.is_dir():
                stack.append(p)
            else:
                loose.append(rel)
    return sorted(loose)


def test_unknown_rust_path_fails_open_to_root_bucket(ws, vllm_repo):
    """A rust path the parser cannot place takes the WIDEST rust answer, not
    the image union, which would balloon the answer for the files most likely
    to hit it. Three ways to be unplaceable, all derived: a crate directory no
    member occupies, a sibling whose name extends the longest member's, and
    the real files sitting above every crate."""
    new_crate = _outside_every_member(ws, "rust/src/brand_new_crate")
    assert ws.bucket_of(f"{new_crate}/src/lib.rs") == "root"

    # The longest-prefix boundary: bucket_of matching on the bare member name
    # instead of member + "/" would silently bucket this as that member, and
    # the longest one is the one such a match would win with.
    longest = max(ws.members, key=len)
    sibling = _outside_every_member(ws, f"{longest}-sibling")
    assert ws.bucket_of(f"{sibling}/src/lib.rs") == "root"

    loose = _loose_rust_files(vllm_repo, ws)
    assert len(loose) >= 2, f"only {loose} sit above the crates; the walk moved"
    for rel in loose:
        assert ws.bucket_of(rel) == "root", rel


def test_nested_member_longest_prefix(ws):
    """rust/src/parser/python/ must beat rust/src/parser/ or the cdylib's
    own sources bucket as the parent crate."""
    assert ws.bucket_of("rust/src/parser/python/src/lib.rs") == "cdylib"
    assert ws.bucket_of("rust/src/parser/src/lib.rs") == "cdylib"


def test_owns_covers_the_toolchain_trio_and_nothing_python(ws):
    for p in ("rust/src/cmd/src/main.rs", "rust-toolchain.toml", "build_rust.sh"):
        assert ws.owns(p), p
    for p in ("vllm/envs.py", "pyproject.toml", "rustfmt.toml"):
        assert not ws.owns(p), p
