# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""Derived facts about CI infra we do not model.

Both sets are recomputed every build from repo text, so a moved script or a new
COPY line is picked up on its own. Neither is a routing map: each gates one
narrow rule. A script only the release pipeline runs selects nothing, and a
docker build input keeps its run-all but names itself as the reason.
"""

from __future__ import annotations

import posixpath
from pathlib import Path

import regex as re

from ..handwritten import RELEASE_PIPELINE_FILES
from .shell import join_continuations

DOCKER_DIR = "docker"
DOCKERFILE_GLOB = "Dockerfile*"

# Repo paths inside a yaml or script, anchored on a top-level dir so a bare word
# or an image tag is not read as a file. docker/ and vllm/ are left out on
# purpose: neither should ever be silenced as release-only.
_REPO_PATH_RE = re.compile(
    r"(?<![\w/])((?:\.buildkite|tools|csrc|cmake|examples|benchmarks|"
    r"tests|requirements)/[\w./=,-]+)"
)


def release_pipeline_refs(repo: Path) -> frozenset[str]:
    """Repo files the release pipeline references: the scripts it runs and the
    artifacts it reads, restricted to paths that exist. Follows one script into
    the next, so a lib a release script sources is release-only too. A file a
    live step also uses is still claimed by that step first."""
    refs: set[str] = set()
    seen: set[str] = set()
    frontier = list(RELEASE_PIPELINE_FILES)
    while frontier:
        rel = frontier.pop()
        if rel in seen:
            continue
        seen.add(rel)
        try:
            text = (repo / rel).read_text()
        except OSError:
            continue
        for match in _REPO_PATH_RE.findall(text):
            if (repo / match).is_file() and match not in refs:
                refs.add(match)
                frontier.append(match)
    return frozenset(refs)


def _copy_sources(text: str) -> list[str]:
    """Sources of every COPY/ADD, minus `--from=` stage copies, whose sources
    are image paths and not repo files."""
    joined = join_continuations(text)
    out: list[str] = []
    for raw in joined.splitlines():
        line = raw.strip()
        upper = line.upper()
        if not (upper.startswith("COPY ") or upper.startswith("ADD ")):
            continue
        if "--from=" in line:
            continue
        tokens = [t for t in line.split()[1:] if not t.startswith("--")]
        if len(tokens) < 2:
            continue
        for src in tokens[:-1]:  # last token is the destination
            out.append(src[2:] if src.startswith("./") else src)
    return out


_FROM_RE = re.compile(r"^FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?", re.I)
# `COPY --from=<stage>` and `RUN --mount=...,from=<stage>`: one stage built
# out of another.
_STAGE_READ_RE = re.compile(r"(?<![\w-])(?:--)?from=([^\s,]+)", re.I)


def _stage_refs(token: str, names: list[str]) -> set[int]:
    """The stages a FROM or from= token can mean. A name built from an ARG
    could be any of them; anything else is an image, not a stage."""
    token = token.lower()
    if "$" in token:
        return set(range(len(names)))
    hits = {i for i, name in enumerate(names) if name == token}
    if not hits and token.isdigit() and int(token) < len(names):
        hits.add(int(token))
    return hits


def payload_sources(text: str) -> set[str]:
    """Named COPY/ADD sources the build carries without running: no line of
    their stage or of a stage built FROM it names the file, other than a copy,
    and no from= reads one of those stages into another. Any line, not only a
    RUN, so a heredoc body counts.

    vllm#58978 edited vllm/collect_env.py, which docker/Dockerfile copies into
    vllm-base for users and Dockerfile.cpu leaves in its test stage. Neither
    build runs it, and a test imports the module from the wheel like any
    other. vllm/envs.py lands in csrc-build, which --from reads, so setup.py
    loading it by path keeps it a build input.
    """
    stages: list[tuple[str, str, list[str]]] = []  # name, FROM token, lines
    copies: list[tuple[int, str, set[str]]] = []  # stage, source, names
    reads: list[str] = []
    for raw in join_continuations(text).splitlines():
        line = raw.strip()
        begins = _FROM_RE.match(line)
        if begins:
            name = begins.group(2) or str(len(stages))
            stages.append((name.lower(), begins.group(1), []))
            continue
        if not stages or line.startswith("#"):
            continue
        reads += _STAGE_READ_RE.findall(line)
        if not line.upper().startswith(("COPY ", "ADD ")):
            stages[-1][2].append(line)
        elif "--from=" not in line:
            tokens = [t for t in line.split()[1:] if not t.startswith("--")]
            srcs, dest = tokens[:-1], tokens[-1] if tokens else ""
            for src in srcs:
                src = (src[2:] if src.startswith("./") else src).rstrip("/")
                # The module name too: `python -m vllm.collect_env` runs it.
                wanted = {posixpath.splitext(posixpath.basename(src))[0]}
                if len(srcs) == 1 and not dest.endswith("/") and dest != ".":
                    wanted.add(posixpath.splitext(posixpath.basename(dest))[0])
                copies.append((len(stages) - 1, src, wanted))
    names = [name for name, _, _ in stages]
    read = {i for token in reads for i in _stage_refs(token, names)}
    children: dict[int, set[int]] = {i: set() for i in range(len(stages))}
    for i, (_, parent, _) in enumerate(stages):
        for p in _stage_refs(parent, names[:i]):
            children[p].add(i)
    ran: set[str] = set()
    carried: set[str] = set()
    for stage, src, wanted in copies:
        built_on, frontier = {stage}, [stage]
        while frontier:
            for child in children[frontier.pop()] - built_on:
                built_on.add(child)
                frontier.append(child)
        runs = any(
            j in read or any(n in line for line in stages[j][2] for n in wanted)
            for j in built_on
        )
        (ran if runs else carried).add(src)
    return carried - ran


def docker_image_inputs(repo: Path) -> dict[str, str]:
    """Repo file -> the Dockerfile that copies it in. File sources only: a
    directory blanket would relabel every fail-open and say nothing."""
    out: dict[str, str] = {}
    for dockerfile in sorted((repo / DOCKER_DIR).glob(DOCKERFILE_GLOB)):
        rel_df = dockerfile.relative_to(repo).as_posix()
        try:
            text = dockerfile.read_text()
        except OSError:
            continue
        for src in _copy_sources(text):
            if (repo / src).is_file():
                out.setdefault(src, rel_df)
    return out


def copy_inputs(
    repo: Path, dockerfiles
) -> tuple[dict[str, set[str]], dict[str, set[str]], set[str], dict[str, set[str]]]:
    """What each Dockerfile copies IN: (file sources, dir sources, blankets,
    and per file source the Dockerfiles that only carry it as payload).

    Separate from `docker_image_inputs` on purpose. That one answers "which
    Dockerfile do I name in a fail-open message" and keeps one winner per file;
    this one answers "which images does a change here rebuild", where a file
    three Dockerfiles copy must reach all three.

    Directory sources are returned rather than dropped, since the build-layer
    callers arrive that way. Which of them are safe to use needs the import
    graph, which this module does not have, so that call is the caller's.

    A whole-context `COPY . .` is reported as the third element rather than
    expanded. Expanding it would make every tree the graph cannot route a build
    input and send a docs edit to the full image closure.
    """
    files: dict[str, set[str]] = {}
    dirs: dict[str, set[str]] = {}
    blanket: set[str] = set()
    payload: dict[str, set[str]] = {}
    for rel in dockerfiles:
        try:
            text = (repo / rel).read_text()
        except OSError:
            continue
        for src in payload_sources(text):
            payload.setdefault(src, set()).add(rel)
        for src in _copy_sources(text):
            src = src.rstrip("/")
            if src in ("", ".", "./"):
                blanket.add(rel)
                continue
            if not (repo / src).exists():
                continue
            target = files if (repo / src).is_file() else dirs
            target.setdefault(src, set()).add(rel)
    return files, dirs, blanket, payload
