"""Resolve repository changes without mutating the working tree."""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional


@dataclass(frozen=True)
class ChangeContext:
    """Build context used to choose the appropriate Git comparison base."""

    branch: str
    commit: str
    pull_request: Optional[str] = None
    pull_request_base_branch: Optional[str] = None
    default_branch: str = "main"

    @property
    def is_pull_request(self) -> bool:
        return self.pull_request not in (None, "", "false")

    @classmethod
    def from_environment(
        cls, environment: Optional[Mapping[str, str]] = None
    ) -> "ChangeContext":
        """Build a context from standard Buildkite environment variables."""
        environment = os.environ if environment is None else environment
        return cls(
            branch=environment.get("BUILDKITE_BRANCH", ""),
            commit=environment.get("BUILDKITE_COMMIT", "HEAD"),
            pull_request=environment.get("BUILDKITE_PULL_REQUEST"),
            pull_request_base_branch=environment.get(
                "BUILDKITE_PULL_REQUEST_BASE_BRANCH"
            ),
            default_branch=environment.get("BUILDKITE_PIPELINE_DEFAULT_BRANCH", "main"),
        )


def resolve_changed_files(
    context: ChangeContext, repo_root: Path = Path(".")
) -> Optional[List[str]]:
    """Return changed paths, or ``None`` when a safe diff cannot be resolved.

    Pull requests are compared with the merge base of their target branch.
    Other feature branches use the pipeline's default branch. Builds on the
    default branch compare the current commit with its first parent.

    Returning ``None`` lets callers safely fall back to running every step.
    This function intentionally performs no fetches and never changes the Git
    index or working tree.
    """
    commit = context.commit or "HEAD"

    if context.is_pull_request:
        base_branch = context.pull_request_base_branch or context.default_branch
        base = _merge_base(repo_root, "origin/{}".format(base_branch), commit)
    elif context.branch == context.default_branch:
        base = _git_output(repo_root, ["rev-parse", "{}^".format(commit)])
    else:
        base = _merge_base(
            repo_root, "origin/{}".format(context.default_branch), commit
        )

    if base is None:
        return None

    output = _git_output(
        repo_root,
        [
            "diff",
            "--name-only",
            "--diff-filter=ACMDR",
            base,
            commit,
            "--",
        ],
    )
    if output is None:
        return None
    return [path for path in output.splitlines() if path]


def _merge_base(repo_root: Path, base_ref: str, commit: str) -> Optional[str]:
    return _git_output(repo_root, ["merge-base", base_ref, commit])


def _git_output(repo_root: Path, arguments: List[str]) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git"] + arguments,
            cwd=str(repo_root),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()
