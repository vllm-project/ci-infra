"""Versioned CI catalog validation, selection, and deterministic planning."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from .models import Selector, stable_id_set, validate_stable_id

_DIGEST = re.compile(r"[0-9a-f]{64}")


class CatalogError(ValueError):
    """Base error for an invalid catalog or selection."""


class CatalogValidationError(CatalogError):
    """The trusted catalog is internally inconsistent."""


class UnknownSelectorError(CatalogError):
    """A selector names an unknown group, area, or job."""


class RetiredJobError(CatalogError):
    """A selector names a tombstoned job."""


class RetiredAreaError(CatalogError):
    """A selector names a tombstoned area."""


class EmptySelectionError(CatalogError):
    """Valid selector dimensions have an empty intersection."""


class NonSelectableJobError(CatalogError):
    """A selector directly names an internal dependency job."""


class ExecutionKind(StrEnum):
    """How one logical catalog target maps to provider execution."""

    STEP = "step"
    OPAQUE_PIPELINE = "opaque_pipeline"


@dataclass(frozen=True, slots=True, order=True)
class JobDefinition:
    """One executable, billable catalog job."""

    job_id: str
    groups: frozenset[str]
    areas: frozenset[str]
    definition_digest: str
    dependencies: tuple[str, ...] = ()
    pipeline: str = "ci"
    execution_profile: str = "default"
    execution_kind: ExecutionKind = ExecutionKind.STEP
    shards: int = 1
    cost: int = 1
    selectable: bool = True

    def __post_init__(self) -> None:
        validate_stable_id(self.job_id, label="job id")
        validate_stable_id(self.pipeline, label="pipeline")
        validate_stable_id(
            self.execution_profile,
            label="execution profile",
        )
        try:
            execution_kind = ExecutionKind(self.execution_kind)
        except ValueError as error:
            raise CatalogValidationError("unknown execution kind") from error
        groups = stable_id_set(self.groups, label="group")
        areas = stable_id_set(self.areas, label="area")
        if not groups:
            raise CatalogValidationError(f"job {self.job_id!r} must belong to a group")
        if not areas:
            raise CatalogValidationError(f"job {self.job_id!r} must belong to an area")
        dependencies = tuple(self.dependencies)
        if len(dependencies) != len(set(dependencies)):
            raise CatalogValidationError(
                f"job {self.job_id!r} has duplicate dependencies"
            )
        for dependency in dependencies:
            validate_stable_id(dependency, label="dependency")
        if (
            isinstance(self.shards, bool)
            or not isinstance(self.shards, int)
            or self.shards < 1
        ):
            raise CatalogValidationError("job shards must be a positive integer")
        if (
            isinstance(self.cost, bool)
            or not isinstance(self.cost, int)
            or self.cost < 1
        ):
            raise CatalogValidationError("job cost must be a positive integer")
        if not isinstance(self.selectable, bool):
            raise CatalogValidationError("job selectable must be boolean")
        if _DIGEST.fullmatch(self.definition_digest) is None:
            raise CatalogValidationError(
                "job definition_digest must be a lowercase SHA-256 digest"
            )
        object.__setattr__(self, "groups", groups)
        object.__setattr__(self, "areas", areas)
        object.__setattr__(self, "execution_kind", execution_kind)
        object.__setattr__(
            self,
            "dependencies",
            tuple(sorted(dependencies)),
        )

    @property
    def total_cost(self) -> int:
        """Return the maximum planned cost across every parallel shard."""

        return self.shards * self.cost


@dataclass(frozen=True, slots=True, order=True)
class JobAlias:
    """A historical selector name mapped directly to one active job."""

    alias: str
    target: str

    def __post_init__(self) -> None:
        validate_stable_id(self.alias, label="job alias")
        validate_stable_id(self.target, label="alias target")


@dataclass(frozen=True, slots=True, order=True)
class JobTombstone:
    """A retired job identifier that must never be silently reused."""

    job_id: str
    reason: str
    replacement: str | None = None

    def __post_init__(self) -> None:
        validate_stable_id(self.job_id, label="tombstoned job id")
        if (
            not self.reason
            or self.reason.strip() != self.reason
            or len(self.reason) > 240
        ):
            raise CatalogValidationError(
                "tombstone reason must be normalized non-empty text"
            )
        if self.replacement is not None:
            validate_stable_id(
                self.replacement,
                label="tombstone replacement",
            )


@dataclass(frozen=True, slots=True, order=True)
class AreaAlias:
    """A historical area selector mapped to one current area."""

    alias: str
    target: str

    def __post_init__(self) -> None:
        validate_stable_id(self.alias, label="area alias")
        validate_stable_id(self.target, label="area alias target")


@dataclass(frozen=True, slots=True, order=True)
class AreaTombstone:
    """A retired area identifier that cannot be silently reused."""

    area_id: str
    reason: str
    replacement: str | None = None

    def __post_init__(self) -> None:
        validate_stable_id(self.area_id, label="tombstoned area id")
        if (
            not self.reason
            or self.reason.strip() != self.reason
            or len(self.reason) > 240
        ):
            raise CatalogValidationError(
                "area tombstone reason must be normalized non-empty text"
            )
        if self.replacement is not None:
            validate_stable_id(
                self.replacement,
                label="area tombstone replacement",
            )


@dataclass(frozen=True, slots=True)
class SelectionPlan:
    """A selector result plus its complete prerequisite closure."""

    catalog_version: str
    catalog_digest: str
    requested_jobs: tuple[str, ...]
    jobs: tuple[str, ...]
    dependency_jobs: tuple[str, ...]
    total_cost: int


@dataclass(frozen=True, slots=True)
class Catalog:
    """An immutable, validated catalog revision."""

    version: str
    jobs: tuple[JobDefinition, ...]
    aliases: tuple[JobAlias, ...] = ()
    tombstones: tuple[JobTombstone, ...] = ()
    area_aliases: tuple[AreaAlias, ...] = ()
    area_tombstones: tuple[AreaTombstone, ...] = ()

    def __post_init__(self) -> None:
        validate_stable_id(self.version, label="catalog version")
        object.__setattr__(
            self,
            "jobs",
            tuple(sorted(self.jobs, key=lambda job: job.job_id)),
        )
        object.__setattr__(
            self,
            "aliases",
            tuple(sorted(self.aliases, key=lambda alias: alias.alias)),
        )
        object.__setattr__(
            self,
            "tombstones",
            tuple(
                sorted(
                    self.tombstones,
                    key=lambda tombstone: tombstone.job_id,
                )
            ),
        )
        object.__setattr__(
            self,
            "area_aliases",
            tuple(sorted(self.area_aliases, key=lambda alias: alias.alias)),
        )
        object.__setattr__(
            self,
            "area_tombstones",
            tuple(
                sorted(
                    self.area_tombstones,
                    key=lambda tombstone: tombstone.area_id,
                )
            ),
        )
        self._validate()

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Catalog:
        """Load a catalog from a strict JSON-compatible mapping."""

        _require_exact_keys(
            value,
            required={"version", "jobs"},
            optional={
                "aliases",
                "tombstones",
                "area_aliases",
                "area_tombstones",
            },
            label="catalog",
        )
        raw_jobs = value["jobs"]
        raw_aliases = value.get("aliases", [])
        raw_tombstones = value.get("tombstones", [])
        raw_area_aliases = value.get("area_aliases", [])
        raw_area_tombstones = value.get("area_tombstones", [])
        if not isinstance(raw_jobs, list):
            raise CatalogValidationError("catalog jobs must be a list")
        if not isinstance(raw_aliases, list):
            raise CatalogValidationError("catalog aliases must be a list")
        if not isinstance(raw_tombstones, list):
            raise CatalogValidationError("catalog tombstones must be a list")
        if not isinstance(raw_area_aliases, list):
            raise CatalogValidationError("catalog area_aliases must be a list")
        if not isinstance(raw_area_tombstones, list):
            raise CatalogValidationError("catalog area_tombstones must be a list")

        jobs = tuple(_job_from_mapping(item) for item in raw_jobs)
        aliases = tuple(_alias_from_mapping(item) for item in raw_aliases)
        tombstones = tuple(_tombstone_from_mapping(item) for item in raw_tombstones)
        area_aliases = tuple(
            _area_alias_from_mapping(item) for item in raw_area_aliases
        )
        area_tombstones = tuple(
            _area_tombstone_from_mapping(item) for item in raw_area_tombstones
        )
        return cls(
            version=value["version"],  # type: ignore[arg-type]
            jobs=jobs,
            aliases=aliases,
            tombstones=tombstones,
            area_aliases=area_aliases,
            area_tombstones=area_tombstones,
        )

    @property
    def digest(self) -> str:
        """Return a deterministic digest of the validated catalog."""

        encoded = json.dumps(
            self.to_mapping(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def to_mapping(self) -> dict[str, object]:
        """Return the canonical JSON-compatible catalog representation."""

        return {
            "version": self.version,
            "jobs": [
                {
                    "id": job.job_id,
                    "groups": sorted(job.groups),
                    "areas": sorted(job.areas),
                    "dependencies": list(job.dependencies),
                    "pipeline": job.pipeline,
                    "execution_profile": job.execution_profile,
                    "execution_kind": job.execution_kind.value,
                    "shards": job.shards,
                    "cost": job.cost,
                    "selectable": job.selectable,
                    "definition_digest": job.definition_digest,
                }
                for job in self.jobs
            ],
            "aliases": [
                {"alias": alias.alias, "target": alias.target} for alias in self.aliases
            ],
            "tombstones": [
                {
                    "id": tombstone.job_id,
                    "reason": tombstone.reason,
                    "replacement": tombstone.replacement,
                }
                for tombstone in self.tombstones
            ],
            "area_aliases": [
                {"alias": alias.alias, "target": alias.target}
                for alias in self.area_aliases
            ],
            "area_tombstones": [
                {
                    "id": tombstone.area_id,
                    "reason": tombstone.reason,
                    "replacement": tombstone.replacement,
                }
                for tombstone in self.area_tombstones
            ],
        }

    @property
    def group_ids(self) -> tuple[str, ...]:
        return tuple(sorted({group for job in self.jobs for group in job.groups}))

    @property
    def area_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted({area for job in self.jobs if job.selectable for area in job.areas})
        )

    @property
    def job_ids(self) -> tuple[str, ...]:
        return tuple(job.job_id for job in self.jobs)

    def resolve_job_id(self, value: str) -> str:
        """Resolve one canonical or historical job selector."""

        validate_stable_id(value, label="job selector")
        jobs = self._jobs_by_id()
        if value in jobs:
            return value
        aliases = {item.alias: item.target for item in self.aliases}
        if value in aliases:
            return aliases[value]
        tombstones = {item.job_id: item for item in self.tombstones}
        if value in tombstones:
            record = tombstones[value]
            suggestion = (
                f"; suggested replacement: {record.replacement}"
                if record.replacement
                else ""
            )
            raise RetiredJobError(
                f"job {value!r} is retired: {record.reason}{suggestion}"
            )
        raise UnknownSelectorError(f"unknown job selector: {value}")

    def resolve_area_id(self, value: str) -> str:
        """Resolve one canonical or historical area selector."""

        validate_stable_id(value, label="area selector")
        if value in self.area_ids:
            return value
        aliases = {item.alias: item.target for item in self.area_aliases}
        if value in aliases:
            return aliases[value]
        tombstones = {item.area_id: item for item in self.area_tombstones}
        if value in tombstones:
            record = tombstones[value]
            suggestion = (
                f"; suggested replacement: {record.replacement}"
                if record.replacement
                else ""
            )
            raise RetiredAreaError(
                f"area {value!r} is retired: {record.reason}{suggestion}"
            )
        raise UnknownSelectorError(f"unknown area selector: {value}")

    def plan(self, selector: Selector) -> SelectionPlan:
        """Resolve selectors, dependencies, topological order, and cost."""

        jobs = self._jobs_by_id()
        if selector.all_jobs:
            requested = {job.job_id for job in self.jobs if job.selectable}
        else:
            requested = {job.job_id for job in self.jobs if job.selectable}
            if selector.groups:
                unknown = selector.groups - set(self.group_ids)
                if unknown:
                    raise UnknownSelectorError(
                        "unknown groups: " + ", ".join(sorted(unknown))
                    )
                requested &= {
                    job.job_id for job in self.jobs if job.groups & selector.groups
                }
            if selector.areas:
                selected_areas = {
                    self.resolve_area_id(value) for value in selector.areas
                }
                requested &= {
                    job.job_id for job in self.jobs if job.areas & selected_areas
                }
            if selector.jobs:
                selected_jobs = {self.resolve_job_id(value) for value in selector.jobs}
                non_selectable = {
                    job_id for job_id in selected_jobs if not jobs[job_id].selectable
                }
                if non_selectable:
                    raise NonSelectableJobError(
                        "jobs are internal dependencies: "
                        + ", ".join(sorted(non_selectable))
                    )
                requested &= selected_jobs

        if not requested:
            raise EmptySelectionError("selector dimensions match no common jobs")
        ordered = self._dependency_closure(requested)
        dependency_jobs = tuple(job_id for job_id in ordered if job_id not in requested)
        return SelectionPlan(
            catalog_version=self.version,
            catalog_digest=self.digest,
            requested_jobs=tuple(sorted(requested)),
            jobs=ordered,
            dependency_jobs=dependency_jobs,
            total_cost=sum(jobs[job_id].total_cost for job_id in ordered),
        )

    def _jobs_by_id(self) -> dict[str, JobDefinition]:
        return {job.job_id: job for job in self.jobs}

    def _validate(self) -> None:
        if not self.jobs:
            raise CatalogValidationError("catalog must contain at least one job")
        jobs = self._jobs_by_id()
        if len(jobs) != len(self.jobs):
            raise CatalogValidationError("catalog job ids must be unique")

        aliases = {item.alias: item.target for item in self.aliases}
        if len(aliases) != len(self.aliases):
            raise CatalogValidationError("catalog aliases must be unique")
        tombstones = {item.job_id: item for item in self.tombstones}
        if len(tombstones) != len(self.tombstones):
            raise CatalogValidationError("catalog tombstones must be unique")

        namespaces = [set(jobs), set(aliases), set(tombstones)]
        if any(
            namespaces[left] & namespaces[right]
            for left in range(len(namespaces))
            for right in range(left + 1, len(namespaces))
        ):
            raise CatalogValidationError(
                "job ids, aliases, and tombstones share one namespace"
            )
        for alias, target in aliases.items():
            if target not in jobs:
                raise CatalogValidationError(
                    f"alias {alias!r} must target a canonical active job"
                )
        for tombstone in self.tombstones:
            if tombstone.replacement is not None and tombstone.replacement not in jobs:
                raise CatalogValidationError(
                    f"tombstone {tombstone.job_id!r} replacement is not active"
                )

        active_areas = set(self.area_ids)
        area_aliases = {item.alias: item.target for item in self.area_aliases}
        if len(area_aliases) != len(self.area_aliases):
            raise CatalogValidationError("catalog area aliases must be unique")
        area_tombstones = {item.area_id: item for item in self.area_tombstones}
        if len(area_tombstones) != len(self.area_tombstones):
            raise CatalogValidationError("catalog area tombstones must be unique")
        area_namespaces = [
            active_areas,
            set(area_aliases),
            set(area_tombstones),
        ]
        if any(
            area_namespaces[left] & area_namespaces[right]
            for left in range(len(area_namespaces))
            for right in range(left + 1, len(area_namespaces))
        ):
            raise CatalogValidationError(
                "area ids, aliases, and tombstones share one namespace"
            )
        for alias, target in area_aliases.items():
            if target not in active_areas:
                raise CatalogValidationError(
                    f"area alias {alias!r} must target an active area"
                )
        for tombstone in self.area_tombstones:
            if (
                tombstone.replacement is not None
                and tombstone.replacement not in active_areas
            ):
                raise CatalogValidationError(
                    f"area tombstone {tombstone.area_id!r} replacement is not active"
                )
        for job in self.jobs:
            for dependency in job.dependencies:
                if dependency not in jobs:
                    raise CatalogValidationError(
                        f"job {job.job_id!r} has unknown dependency {dependency!r}"
                    )
                if dependency == job.job_id:
                    raise CatalogValidationError(
                        f"job {job.job_id!r} cannot depend on itself"
                    )
        self._dependency_closure(set(jobs))

    def _dependency_closure(
        self,
        requested: set[str],
    ) -> tuple[str, ...]:
        jobs = self._jobs_by_id()
        ordered: list[str] = []
        permanent: set[str] = set()
        temporary: set[str] = set()

        def visit(job_id: str) -> None:
            if job_id in permanent:
                return
            if job_id in temporary:
                raise CatalogValidationError(
                    f"dependency graph contains a cycle at {job_id!r}"
                )
            temporary.add(job_id)
            for dependency in jobs[job_id].dependencies:
                visit(dependency)
            temporary.remove(job_id)
            permanent.add(job_id)
            ordered.append(job_id)

        for job_id in sorted(requested):
            visit(job_id)
        return tuple(ordered)


def validate_catalog_evolution(previous: Catalog, current: Catalog) -> None:
    """Require deliberate compatibility records for removed public selectors."""

    errors = []
    current_jobs = set(current.job_ids)
    current_job_aliases = {item.alias for item in current.aliases}
    current_job_tombstones = {item.job_id for item in current.tombstones}
    for job_id in sorted(
        (set(previous.job_ids) | {item.alias for item in previous.aliases})
        - current_jobs
        - current_job_aliases
        - current_job_tombstones
    ):
        errors.append(f"removed job selector {job_id!r} requires an alias or tombstone")
    for tombstone in previous.tombstones:
        if tombstone.job_id not in current_job_tombstones:
            errors.append(
                f"retired job selector {tombstone.job_id!r} must remain tombstoned"
            )

    current_areas = set(current.area_ids)
    current_area_aliases = {item.alias for item in current.area_aliases}
    current_area_tombstones = {item.area_id for item in current.area_tombstones}
    for area_id in sorted(
        (set(previous.area_ids) | {item.alias for item in previous.area_aliases})
        - current_areas
        - current_area_aliases
        - current_area_tombstones
    ):
        errors.append(
            f"removed area selector {area_id!r} requires an alias or tombstone"
        )
    for tombstone in previous.area_tombstones:
        if tombstone.area_id not in current_area_tombstones:
            errors.append(
                f"retired area selector {tombstone.area_id!r} must remain tombstoned"
            )

    if errors:
        raise CatalogValidationError("; ".join(errors))


def _require_exact_keys(
    value: object,
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    label: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CatalogValidationError(f"{label} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise CatalogValidationError(
            f"{label} is missing fields: " + ", ".join(sorted(missing))
        )
    if unknown:
        raise CatalogValidationError(
            f"{label} has unknown fields: " + ", ".join(sorted(unknown))
        )
    return value


def _string_list(value: object, *, label: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CatalogValidationError(f"{label} must be a list of strings")
    return value


def _job_from_mapping(value: object) -> JobDefinition:
    item = _require_exact_keys(
        value,
        required={"id", "groups", "areas", "definition_digest"},
        optional={
            "dependencies",
            "pipeline",
            "execution_profile",
            "execution_kind",
            "shards",
            "cost",
            "selectable",
        },
        label="job",
    )
    return JobDefinition(
        job_id=item["id"],  # type: ignore[arg-type]
        groups=frozenset(_string_list(item["groups"], label="job groups")),
        areas=frozenset(_string_list(item["areas"], label="job areas")),
        dependencies=tuple(
            _string_list(
                item.get("dependencies", []),
                label="job dependencies",
            )
        ),
        pipeline=item.get("pipeline", "ci"),  # type: ignore[arg-type]
        execution_profile=item.get(
            "execution_profile",
            "default",
        ),  # type: ignore[arg-type]
        execution_kind=item.get(
            "execution_kind",
            ExecutionKind.STEP,
        ),  # type: ignore[arg-type]
        shards=item.get("shards", 1),  # type: ignore[arg-type]
        cost=item.get("cost", 1),  # type: ignore[arg-type]
        selectable=item.get("selectable", True),  # type: ignore[arg-type]
        definition_digest=item["definition_digest"],  # type: ignore[arg-type]
    )


def _alias_from_mapping(value: object) -> JobAlias:
    item = _require_exact_keys(
        value,
        required={"alias", "target"},
        label="alias",
    )
    return JobAlias(
        alias=item["alias"],  # type: ignore[arg-type]
        target=item["target"],  # type: ignore[arg-type]
    )


def _tombstone_from_mapping(value: object) -> JobTombstone:
    item = _require_exact_keys(
        value,
        required={"id", "reason"},
        optional={"replacement"},
        label="tombstone",
    )
    return JobTombstone(
        job_id=item["id"],  # type: ignore[arg-type]
        reason=item["reason"],  # type: ignore[arg-type]
        replacement=item.get("replacement"),  # type: ignore[arg-type]
    )


def _area_alias_from_mapping(value: object) -> AreaAlias:
    item = _require_exact_keys(
        value,
        required={"alias", "target"},
        label="area alias",
    )
    return AreaAlias(
        alias=item["alias"],  # type: ignore[arg-type]
        target=item["target"],  # type: ignore[arg-type]
    )


def _area_tombstone_from_mapping(value: object) -> AreaTombstone:
    item = _require_exact_keys(
        value,
        required={"id", "reason"},
        optional={"replacement"},
        label="area tombstone",
    )
    return AreaTombstone(
        area_id=item["id"],  # type: ignore[arg-type]
        reason=item["reason"],  # type: ignore[arg-type]
        replacement=item.get("replacement"),  # type: ignore[arg-type]
    )
