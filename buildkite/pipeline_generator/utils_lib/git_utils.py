import subprocess
import os
import time
from typing import List, Optional
import requests


def get_merge_base_commit() -> Optional[str]:
    """Get merge base commit from env var or compute it via git."""
    merge_base = os.getenv("MERGE_BASE_COMMIT")
    if merge_base:
        return merge_base
    # Compute merge base if not provided
    try:
        result = subprocess.run(
            ["git", "merge-base", "origin/main", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def get_list_file_diff(branch: str, merge_base_commit: Optional[str]) -> List[str]:
    """Get list of file paths that get changed between current branch and origin/main."""
    try:
        subprocess.run(["git", "add", "."], check=True)
        if branch == "main":
            output = subprocess.check_output(
                ["git", "diff", "--name-only", "--diff-filter=ACMDR", "HEAD~1"],
                universal_newlines=True,
            )
        else:
            merge_base = merge_base_commit
            if not merge_base:
                pass
            output = subprocess.check_output(
                [
                    "git",
                    "diff",
                    "--name-only",
                    "--diff-filter=ACMDR",
                    merge_base.strip(),
                ],
                universal_newlines=True,
            )
        return [line for line in output.split("\n") if line.strip()]
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to get git diff: {e}")
    except AttributeError:
        # Case where merge_base_commit is None
        raise RuntimeError("Failed to determine merge base commit for git diff.")


def check_precommit_passed(commit: str, repo_name: str) -> None:
    """Poll until the pre-commit GitHub Actions check run completes, then fail if not successful.

    Raises RuntimeError if the check fails, times out, or is not found.
    """
    max_wait = 600
    wait_interval = 30
    elapsed = 0
    api_url = f"https://api.github.com/repos/{repo_name}/commits/{commit}/check-runs"

    while True:
        response = requests.get(api_url)
        response.raise_for_status()
        check_runs = response.json().get("check_runs", [])
        precommit_run = next(
            (run for run in check_runs if run["name"] == "pre-commit"), None
        )

        if precommit_run and precommit_run["status"] == "completed":
            conclusion = precommit_run["conclusion"]
            if conclusion == "success":
                print(f"pre-commit check passed on commit {commit}.")
                return
            subprocess.run(
                [
                    "buildkite-agent",
                    "annotate",
                    f":x: pre-commit check has not passed on this PR (conclusion: {conclusion}). "
                    "Please fix pre-commit issues before running CI.",
                    "--style",
                    "error",
                ],
                check=False,
            )
            raise RuntimeError(
                f"pre-commit check failed on commit {commit} (conclusion: {conclusion})."
            )

        if elapsed >= max_wait:
            subprocess.run(
                [
                    "buildkite-agent",
                    "annotate",
                    ":warning: Timed out waiting for pre-commit check to complete.",
                    "--style",
                    "warning",
                ],
                check=False,
            )
            raise RuntimeError(
                f"Timed out after {max_wait}s waiting for pre-commit check on commit {commit}."
            )

        status = precommit_run["status"] if precommit_run else "not found"
        print(
            f"pre-commit check is not yet complete (status: {status}). "
            f"Waiting {wait_interval}s... ({elapsed}/{max_wait}s)"
        )
        time.sleep(wait_interval)
        elapsed += wait_interval


def get_pr_labels(pull_request: str, repo_name: str) -> List[str]:
    if not pull_request or pull_request == "false":
        return []
    request_url = f"https://api.github.com/repos/{repo_name}/pulls/{pull_request}"
    response = requests.get(request_url)
    response.raise_for_status()
    return [label["name"] for label in response.json()["labels"]]
