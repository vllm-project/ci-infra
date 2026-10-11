import subprocess
import os
import time
from typing import List, Optional
import requests

LABEL_FETCH_ATTEMPTS = 5


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


def _record_infra_retry(attempts: int, outcome: str) -> None:
    """Best-effort Buildkite meta-data mark that a bootstrap call was retried."""
    job_id = os.getenv("BUILDKITE_JOB_ID")
    if not job_id:
        return
    try:
        subprocess.run(
            [
                "buildkite-agent",
                "meta-data",
                "set",
                f"infra-retry:{job_id}",
                f"bootstrap-labels,{attempts},{outcome}",
            ],
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _is_transient(error: requests.RequestException) -> bool:
    return isinstance(error, requests.ConnectionError) or (
        isinstance(error, requests.HTTPError) and error.response.status_code >= 500
    )


def get_pr_labels(pull_request: str, repo_name: str) -> List[str]:
    if not pull_request or pull_request == "false":
        return []
    request_url = f"https://api.github.com/repos/{repo_name}/pulls/{pull_request}"
    attempt = 0
    outcome = "failed"
    try:
        while True:
            attempt += 1
            try:
                response = requests.get(request_url)
                response.raise_for_status()
                outcome = "success"
                break
            except requests.RequestException as error:
                if not _is_transient(error) or attempt == LABEL_FETCH_ATTEMPTS:
                    raise
                delay = 2**attempt
                print(
                    f"get_pr_labels failed (attempt {attempt}/{LABEL_FETCH_ATTEMPTS}); "
                    f"retrying in {delay}s: {error}"
                )
                time.sleep(delay)
    finally:
        if attempt > 1:
            _record_infra_retry(attempt, outcome)
    return [label["name"] for label in response.json()["labels"]]
