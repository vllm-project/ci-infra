import subprocess

from change_detection import ChangeContext, resolve_changed_files


def _git(repo, *arguments):
    return subprocess.run(
        ["git"] + list(arguments),
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_file(repo, relative_path, contents, message):
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    _git(repo, "add", relative_path)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _new_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Pipeline Generator Tests")
    _git(repo, "config", "user.email", "pipeline-generator@example.com")
    initial_commit = _commit_file(repo, "README.md", "initial\n", "initial")
    _git(repo, "update-ref", "refs/remotes/origin/main", initial_commit)
    return repo, initial_commit


def test_default_branch_build_compares_commit_with_parent(tmp_path):
    repo, _ = _new_repository(tmp_path)
    commit = _commit_file(repo, "src/runtime.py", "new\n", "add runtime")

    changed_files = resolve_changed_files(
        ChangeContext(branch="main", commit=commit), repo
    )

    assert changed_files == ["src/runtime.py"]


def test_pull_request_build_compares_with_target_branch_merge_base(tmp_path):
    repo, _ = _new_repository(tmp_path)
    _git(repo, "switch", "-c", "feature")
    commit = _commit_file(repo, "tests/test_runtime.py", "new\n", "add test")

    changed_files = resolve_changed_files(
        ChangeContext(
            branch="feature",
            commit=commit,
            pull_request="123",
            pull_request_base_branch="main",
        ),
        repo,
    )

    assert changed_files == ["tests/test_runtime.py"]


def test_feature_branch_build_compares_with_default_branch(tmp_path):
    repo, _ = _new_repository(tmp_path)
    _git(repo, "switch", "-c", "feature")
    commit = _commit_file(repo, "src/feature.py", "new\n", "add feature")

    changed_files = resolve_changed_files(
        ChangeContext(branch="feature", commit=commit), repo
    )

    assert changed_files == ["src/feature.py"]


def test_unresolvable_base_returns_none(tmp_path):
    repo, _ = _new_repository(tmp_path)
    _git(repo, "switch", "-c", "feature")
    commit = _commit_file(repo, "src/feature.py", "new\n", "add feature")

    changed_files = resolve_changed_files(
        ChangeContext(branch="feature", commit=commit, default_branch="missing"),
        repo,
    )

    assert changed_files is None


def test_root_commit_on_default_branch_returns_none(tmp_path):
    repo, initial_commit = _new_repository(tmp_path)

    changed_files = resolve_changed_files(
        ChangeContext(branch="main", commit=initial_commit), repo
    )

    assert changed_files is None


def test_change_context_reads_buildkite_environment():
    context = ChangeContext.from_environment(
        {
            "BUILDKITE_BRANCH": "feature",
            "BUILDKITE_COMMIT": "abc123",
            "BUILDKITE_PULL_REQUEST": "42",
            "BUILDKITE_PULL_REQUEST_BASE_BRANCH": "release",
            "BUILDKITE_PIPELINE_DEFAULT_BRANCH": "trunk",
        }
    )

    assert context == ChangeContext(
        branch="feature",
        commit="abc123",
        pull_request="42",
        pull_request_base_branch="release",
        default_branch="trunk",
    )
    assert context.is_pull_request
