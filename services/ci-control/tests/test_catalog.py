from __future__ import annotations

import pytest

from vllm_ci_control.catalog import (
    AreaAlias,
    AreaTombstone,
    Catalog,
    CatalogValidationError,
    EmptySelectionError,
    JobAlias,
    JobDefinition,
    JobTombstone,
    NonSelectableJobError,
    RetiredAreaError,
    RetiredJobError,
    UnknownSelectorError,
    validate_catalog_evolution,
)
from vllm_ci_control.models import Selector

DIGEST = "a" * 64


def _catalog() -> Catalog:
    return Catalog(
        version="2026.07",
        jobs=(
            JobDefinition(
                job_id="amd.smoke",
                groups=frozenset({"amd"}),
                areas=frozenset({"tests"}),
                definition_digest=DIGEST,
                dependencies=("prepare",),
                pipeline="amd-ci",
                execution_profile="mi300",
                shards=2,
                cost=3,
            ),
            JobDefinition(
                job_id="prepare",
                groups=frozenset({"upstream"}),
                areas=frozenset({"infra"}),
                definition_digest=DIGEST,
                cost=1,
                selectable=False,
            ),
            JobDefinition(
                job_id="cpu.smoke",
                groups=frozenset({"cpu"}),
                areas=frozenset({"tests"}),
                definition_digest=DIGEST,
                dependencies=("prepare",),
                cost=2,
            ),
        ),
        aliases=(JobAlias("cpu.old", "cpu.smoke"),),
        tombstones=(
            JobTombstone(
                "legacy",
                "lane was retired",
                replacement="cpu.smoke",
            ),
        ),
    )


def test_plan_intersects_dimensions_and_adds_dependencies() -> None:
    plan = _catalog().plan(
        Selector(
            groups=frozenset({"cpu"}),
            areas=frozenset({"tests"}),
        )
    )

    assert plan.requested_jobs == ("cpu.smoke",)
    assert plan.jobs == ("prepare", "cpu.smoke")
    assert plan.dependency_jobs == ("prepare",)
    assert plan.total_cost == 3
    assert len(plan.catalog_digest) == 64


def test_aliases_resolve_but_tombstones_and_unknowns_fail() -> None:
    catalog = _catalog()

    assert catalog.plan(Selector(jobs=frozenset({"cpu.old"}))).requested_jobs == (
        "cpu.smoke",
    )
    with pytest.raises(RetiredJobError, match="suggested replacement"):
        catalog.plan(Selector(jobs=frozenset({"legacy"})))
    with pytest.raises(UnknownSelectorError, match="unknown groups"):
        catalog.plan(Selector(groups=frozenset({"gpu"})))
    with pytest.raises(EmptySelectionError):
        catalog.plan(
            Selector(
                groups=frozenset({"cpu"}),
                jobs=frozenset({"amd.smoke"}),
            )
        )
    with pytest.raises(NonSelectableJobError, match="internal dependencies"):
        catalog.plan(Selector(jobs=frozenset({"prepare"})))


def test_catalog_order_and_digest_are_deterministic() -> None:
    catalog = _catalog()
    reordered = Catalog(
        version=catalog.version,
        jobs=tuple(reversed(catalog.jobs)),
        aliases=tuple(reversed(catalog.aliases)),
        tombstones=tuple(reversed(catalog.tombstones)),
    )

    assert reordered.jobs == catalog.jobs
    assert reordered.digest == catalog.digest
    assert reordered.plan(Selector.all()) == catalog.plan(Selector.all())


def test_plan_cost_includes_parallel_shards_and_route_is_in_digest() -> None:
    catalog = _catalog()
    plan = catalog.plan(Selector(groups=frozenset({"amd"})))

    assert plan.total_cost == 7
    changed_route = Catalog(
        version=catalog.version,
        jobs=tuple(
            JobDefinition(
                job_id=job.job_id,
                groups=job.groups,
                areas=job.areas,
                dependencies=job.dependencies,
                pipeline=("different" if job.job_id == "amd.smoke" else job.pipeline),
                execution_profile=job.execution_profile,
                shards=job.shards,
                cost=job.cost,
                selectable=job.selectable,
                definition_digest=job.definition_digest,
            )
            for job in catalog.jobs
        ),
        aliases=catalog.aliases,
        tombstones=catalog.tombstones,
    )
    assert changed_route.digest != catalog.digest


def test_catalog_validates_duplicates_namespace_and_dependency_cycles() -> None:
    duplicate = JobDefinition(
        "same",
        frozenset({"cpu"}),
        frozenset({"tests"}),
        DIGEST,
    )
    with pytest.raises(CatalogValidationError, match="unique"):
        Catalog(version="v1", jobs=(duplicate, duplicate))

    with pytest.raises(CatalogValidationError, match="share one namespace"):
        Catalog(
            version="v1",
            jobs=(duplicate,),
            aliases=(JobAlias("same", "same"),),
        )

    with pytest.raises(CatalogValidationError, match="cycle"):
        Catalog(
            version="v1",
            jobs=(
                JobDefinition(
                    "a",
                    frozenset({"cpu"}),
                    frozenset({"tests"}),
                    DIGEST,
                    dependencies=("b",),
                ),
                JobDefinition(
                    "b",
                    frozenset({"cpu"}),
                    frozenset({"tests"}),
                    DIGEST,
                    dependencies=("a",),
                ),
            ),
        )


def test_strict_mapping_loader() -> None:
    catalog = Catalog.from_mapping(
        {
            "version": "v1",
            "jobs": [
                {
                    "id": "cpu.smoke",
                    "groups": ["cpu"],
                    "areas": ["tests"],
                    "definition_digest": DIGEST,
                    "dependencies": [],
                    "pipeline": "ci",
                    "execution_profile": "cpu-x86",
                    "shards": 2,
                    "cost": 2,
                }
            ],
            "aliases": [],
            "tombstones": [],
        }
    )

    assert catalog.job_ids == ("cpu.smoke",)
    assert catalog.jobs[0].total_cost == 4
    with pytest.raises(CatalogValidationError, match="unknown fields"):
        Catalog.from_mapping(
            {
                "version": "v1",
                "jobs": [],
                "surprise": True,
            }
        )


def test_area_aliases_and_tombstones_are_first_class_selectors() -> None:
    base = _catalog()
    catalog = Catalog(
        version=base.version,
        jobs=base.jobs,
        aliases=base.aliases,
        tombstones=base.tombstones,
        area_aliases=(AreaAlias("old-tests", "tests"),),
        area_tombstones=(
            AreaTombstone(
                "retired-tests",
                "suite was retired",
                replacement="tests",
            ),
        ),
    )

    assert catalog.plan(Selector(areas=frozenset({"old-tests"}))).requested_jobs == (
        "amd.smoke",
        "cpu.smoke",
    )
    with pytest.raises(RetiredAreaError, match="suggested replacement"):
        catalog.plan(Selector(areas=frozenset({"retired-tests"})))


def test_catalog_evolution_requires_compatibility_records() -> None:
    previous = _catalog()
    remaining = tuple(job for job in previous.jobs if job.job_id != "cpu.smoke")
    incompatible = Catalog(version="next", jobs=remaining)

    with pytest.raises(CatalogValidationError, match="removed job selector"):
        validate_catalog_evolution(previous, incompatible)

    compatible = Catalog(
        version="next",
        jobs=(
            *remaining,
            JobDefinition(
                job_id="cpu.new",
                groups=frozenset({"cpu"}),
                areas=frozenset({"new-tests"}),
                definition_digest=DIGEST,
            ),
        ),
        aliases=(
            JobAlias("cpu.old", "cpu.new"),
            JobAlias("cpu.smoke", "cpu.new"),
        ),
        tombstones=(
            JobTombstone(
                "legacy",
                "lane was retired",
                replacement="cpu.new",
            ),
        ),
    )
    validate_catalog_evolution(previous, compatible)


def test_catalog_evolution_requires_area_compatibility_records() -> None:
    previous = Catalog(
        version="old",
        jobs=(
            JobDefinition(
                job_id="cpu.old",
                groups=frozenset({"cpu"}),
                areas=frozenset({"old-area"}),
                definition_digest=DIGEST,
            ),
        ),
    )
    current_job = JobDefinition(
        job_id="cpu.old",
        groups=frozenset({"cpu"}),
        areas=frozenset({"new-area"}),
        definition_digest=DIGEST,
    )
    with pytest.raises(CatalogValidationError, match="removed area selector"):
        validate_catalog_evolution(
            previous,
            Catalog(version="new", jobs=(current_job,)),
        )

    validate_catalog_evolution(
        previous,
        Catalog(
            version="new",
            jobs=(current_job,),
            area_aliases=(AreaAlias("old-area", "new-area"),),
        ),
    )
