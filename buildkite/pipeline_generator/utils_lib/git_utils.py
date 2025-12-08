import subprocess
import os
from typing import List, Optional

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
            check=True
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
                universal_newlines=True
            )
        else:
            merge_base = merge_base_commit
            if not merge_base:
                 pass
            output = subprocess.check_output(
                ["git", "diff", "--name-only", "--diff-filter=ACMDR", merge_base.strip()],
                universal_newlines=True
            )
        return [line for line in output.split('\n') if line.strip()]
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Failed to get git diff: {e}")
    except AttributeError:
        # Case where merge_base_commit is None
        raise RuntimeError("Failed to determine merge base commit for git diff.")

def get_pr_labels(pull_request: str) -> List[str]:
    if not pull_request or pull_request == "false":
        return []
    request_url = f"https://api.github.com/repos/vllm-project/vllm/pulls/{pull_request}"
    response = requests.get(request_url)
    response.raise_for_status()
    return [label["name"] for label in response.json()["labels"]]
