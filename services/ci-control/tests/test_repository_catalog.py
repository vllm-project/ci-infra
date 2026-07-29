from pathlib import Path

import pytest

from vllm_ci_control.models import Selector
from vllm_ci_control.repository_catalog import (
    RepositoryCatalogError,
    compile_repository_catalog,
)

POLICY = """
api_version = 1

[catalog]
groups = ["upstream", "cpu", "amd"]
aliases = {}
tombstones = []
area_aliases = {}
area_tombstones = []
native_amd_selection = "whole_lane"

[authorization]
minimum_compute_permission = "write"
minimum_refresh_permission = "write"
minimum_credit_grant_permission = "maintain"
committer_teams = ["vllm-committers"]
credit_admin_teams = ["vllm-ci-admins"]

[credits]
initial_grant = 300
reset = "none"

[retry]
failures_limit = "inf"
include_states = ["failed", "timed_out", "expired"]

[main_status]
confirmation_distinct_shas = 2
resolution_clean_distinct_shas = 3
evidence_max_age_hours = 72
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def repository(tmp_path: Path) -> Path:
    write(tmp_path / ".buildkite/ci_control.toml", POLICY)
    write(
        tmp_path / ".buildkite/image_build/images.yaml",
        """
group: images
steps:
- label: primary image
  key: image-build
  depends_on: []
  ci_control:
    groups: [upstream]
- label: AMD image
  key: image-build-amd
  depends_on: []
- label: CPU image
  key: image-build-cpu
  depends_on: []
""",
    )
    write(
        tmp_path / ".buildkite/test_areas/models_language.yaml",
        """
group: models
depends_on: [image-build]
steps:
- label: language models
  key: language-models
  device: h100
  depends_on:
  parallelism: 2
  commands: [pytest models]
  mirror:
    amd:
      device: mi300_1
""",
    )
    write(
        tmp_path / ".buildkite/hardware_tests/cpu.yaml",
        """
group: CPU
steps:
- label: CPU models
  key: cpu-models
  device: intel_cpu
  depends_on: [image-build-cpu]
  ci_control:
    areas: [models]
""",
    )
    write(
        tmp_path / ".buildkite/hardware_tests/amd.yaml",
        """
group: AMD prerequisites
steps:
- label: AMD image alias
  key: amd-prerequisite
  depends_on: []
""",
    )
    write(
        tmp_path / ".buildkite/test-amd.yaml",
        """
steps:
- label: native one
  parallelism: 2
- label: native two
""",
    )
    return tmp_path


def test_compiles_routes_variants_areas_and_dependency_cost(
    tmp_path: Path,
) -> None:
    catalog = compile_repository_catalog(repository(tmp_path))
    by_id = {job.job_id: job for job in catalog.jobs}

    assert by_id["language-models"].pipeline == "ci"
    assert by_id["language-models"].execution_profile == "h100"
    assert by_id["language-models"].areas == frozenset({"models", "models-language"})
    assert by_id["language-models"].dependencies == ("image-build",)
    assert by_id["language-models-amd"].groups == frozenset({"amd"})
    assert by_id["language-models-amd"].dependencies == ("image-build-amd",)
    assert by_id["native-amd-lane"].pipeline == "amd-ci"
    assert by_id["native-amd-lane"].shards == 3
    assert not by_id["image-build"].selectable

    cpu = catalog.plan(Selector(groups=frozenset({"cpu"})))
    assert cpu.requested_jobs == ("cpu-models",)
    assert cpu.jobs == ("image-build-cpu", "cpu-models")
    assert cpu.total_cost == 2

    amd = catalog.plan(Selector(groups=frozenset({"amd"})))
    assert amd.requested_jobs == ("language-models-amd", "native-amd-lane")
    assert amd.jobs == (
        "image-build-amd",
        "language-models-amd",
        "native-amd-lane",
    )

    amd_models = catalog.plan(
        Selector(
            groups=frozenset({"amd"}),
            areas=frozenset({"models"}),
        )
    )
    assert amd_models.requested_jobs == ("language-models-amd",)

    upstream_and_cpu = catalog.plan(Selector(groups=frozenset({"upstream", "cpu"})))
    assert upstream_and_cpu.requested_jobs == (
        "cpu-models",
        "language-models",
    )


def test_catalog_digest_changes_with_the_executable_definition(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    before = compile_repository_catalog(root)
    path = root / ".buildkite/test_areas/models_language.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "pytest models",
            "pytest changed-models",
        ),
        encoding="utf-8",
    )

    after = compile_repository_catalog(root)

    assert before.digest != after.digest


def test_variant_definition_digests_exclude_unrelated_metadata(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    before = compile_repository_catalog(root)
    path = root / ".buildkite/test_areas/models_language.yaml"
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace(
            "      device: mi300_1",
            "      device: mi300_1\n      timeout_in_minutes: 90",
        )
        .replace(
            "  commands: [pytest models]",
            "  commands: [pytest models]\n  ci_control:\n    areas: [language]",
        ),
        encoding="utf-8",
    )

    after = compile_repository_catalog(root)
    before_jobs = {job.job_id: job for job in before.jobs}
    after_jobs = {job.job_id: job for job in after.jobs}

    assert (
        before_jobs["language-models"].definition_digest
        == after_jobs["language-models"].definition_digest
    )
    assert (
        before_jobs["language-models-amd"].definition_digest
        != after_jobs["language-models-amd"].definition_digest
    )


def test_mirror_dependencies_do_not_inherit_source_only_dependencies(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    path = root / ".buildkite/test_areas/models_language.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "depends_on: [image-build]",
            "depends_on: [image-build, image-build-cpu]",
        ),
        encoding="utf-8",
    )

    catalog = compile_repository_catalog(root)
    by_id = {job.job_id: job for job in catalog.jobs}

    assert by_id["language-models"].dependencies == (
        "image-build",
        "image-build-cpu",
    )
    assert by_id["language-models-amd"].dependencies == ("image-build-amd",)


def test_groups_outside_protected_policy_fail_closed(tmp_path: Path) -> None:
    root = repository(tmp_path)
    path = root / ".buildkite/hardware_tests/cpu.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "    areas: [models]",
            "    areas: [models]\n    groups: [typo-group]",
        ),
        encoding="utf-8",
    )

    with pytest.raises(RepositoryCatalogError, match="outside protected policy"):
        compile_repository_catalog(root)


def test_explicitly_nonselectable_job_remains_available_to_the_dag(
    tmp_path: Path,
) -> None:
    root = repository(tmp_path)
    path = root / ".buildkite/hardware_tests/cpu.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "    areas: [models]",
            "    areas: [models]\n    selectable: false",
        ),
        encoding="utf-8",
    )

    catalog = compile_repository_catalog(root)
    cpu = {job.job_id: job for job in catalog.jobs}["cpu-models"]
    assert not cpu.selectable


def test_missing_explicit_key_fails_closed(tmp_path: Path) -> None:
    root = repository(tmp_path)
    path = root / ".buildkite/test_areas/models_language.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "  key: language-models\n",
            "",
        ),
        encoding="utf-8",
    )

    with pytest.raises(RepositoryCatalogError, match="explicit key"):
        compile_repository_catalog(root)
