"""The flag configuration schema shared by the server and the SDK.

These models describe structure only: field types, key formats, operator values
and split weights. Checks that need the whole flag (does a rule serve a variant
that exists, does a value match the flag type) are in ``validation``; the
server runs them on every write, and the evaluator reports ERROR if a config
that skipped them reaches it anyway.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializerFunctionWrapHandler,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from flagservice.core.bucketing import BUCKETS
from flagservice.core.semver import parse_semver

FlagType = Literal["boolean", "string", "number", "json"]
FLAG_TYPES: tuple[FlagType, ...] = ("boolean", "string", "number", "json")

KEY_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
"""Project, environment and flag keys: lowercase, digits, ``_`` and ``-``; never ``:``."""

VARIANT_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"

DEFAULT_BUCKET_BY = "key"

MAX_VARIANTS = 100
MAX_RULES = 200
MAX_CONDITIONS = 50
MAX_LIST_VALUES = 1000

ResourceKey = Annotated[str, Field(pattern=KEY_PATTERN)]
VariantKey = Annotated[str, Field(pattern=VARIANT_KEY_PATTERN)]
AttributeName = Annotated[str, Field(min_length=1, max_length=256)]
FiniteFloat = Annotated[float, Field(strict=True, allow_inf_nan=False)]
Number = StrictInt | FiniteFloat
Scalar = StrictStr | StrictBool | StrictInt | FiniteFloat
"""A value a condition compares against. Strict: ``"1"``, ``1`` and ``True`` are all different."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Variant(_Frozen):
    key: VariantKey
    value: JsonValue


# Conditions: one model per operator family, discriminated by ``operator``.


class _ConditionBase(_Frozen):
    attribute: AttributeName


class EqualsCondition(_ConditionBase):
    operator: Literal["eq", "neq"]
    value: Scalar


class ListCondition(_ConditionBase):
    operator: Literal["in", "not_in"]
    value: Annotated[list[Scalar], Field(max_length=MAX_LIST_VALUES)]


class StringCondition(_ConditionBase):
    operator: Literal["contains", "starts_with"]
    value: StrictStr


class NumericCondition(_ConditionBase):
    operator: Literal["lt", "lte", "gt", "gte"]
    value: Number


class SemverCondition(_ConditionBase):
    operator: Literal["semver_eq", "semver_lt", "semver_lte", "semver_gt", "semver_gte"]
    value: StrictStr

    @field_validator("value")
    @classmethod
    def _valid_semver(cls, value: str) -> str:
        if parse_semver(value) is None:
            raise ValueError(f"{value!r} is not a valid semantic version (for example 1.4.0)")
        return value


ConditionModel = (
    EqualsCondition | ListCondition | StringCondition | NumericCondition | SemverCondition
)
Condition = Annotated[ConditionModel, Field(discriminator="operator")]

OPERATORS: tuple[str, ...] = (
    "eq",
    "neq",
    "in",
    "not_in",
    "contains",
    "starts_with",
    "lt",
    "lte",
    "gt",
    "gte",
    "semver_eq",
    "semver_lt",
    "semver_lte",
    "semver_gt",
    "semver_gte",
)


class SplitAllocation(_Frozen):
    variant: VariantKey
    weight: Annotated[float, Field(ge=0, le=100)]
    """Percentage of contexts, with a resolution of 0.01 (one bucket)."""

    @field_validator("weight")
    @classmethod
    def _hundredths(cls, weight: float) -> float:
        scaled = weight * 100
        if not math.isclose(scaled, round(scaled), abs_tol=1e-6):
            raise ValueError("weight must be a multiple of 0.01")
        return weight

    @property
    def basis_points(self) -> int:
        """The weight in buckets (hundredths of a percent)."""
        return round(self.weight * 100)


Split = Annotated[list[SplitAllocation], Field(min_length=1, max_length=MAX_VARIANTS)]


class Rule(_Frozen):
    """Serves ``variant``, or picks one from ``split``, when every condition matches."""

    conditions: Annotated[list[Condition], Field(max_length=MAX_CONDITIONS)] = []
    variant: VariantKey | None = None
    split: Split | None = None
    bucket_by: AttributeName | None = None

    @model_validator(mode="after")
    def _one_serve(self) -> Rule:
        if (self.variant is None) == (self.split is None):
            raise ValueError("a rule needs exactly one of 'variant' or 'split'")
        if self.split is None:
            if self.bucket_by is not None:
                raise ValueError("'bucket_by' only applies to a rule with a 'split'")
            return self
        seen: set[str] = set()
        for allocation in self.split:
            if allocation.variant in seen:
                raise ValueError(f"variant {allocation.variant!r} appears twice in the split")
            seen.add(allocation.variant)
        total = sum(allocation.basis_points for allocation in self.split)
        if total != BUCKETS:
            raise ValueError(f"split weights must sum to 100, got {total / 100:g}")
        return self

    @model_serializer(mode="wrap")
    def _omit_unused(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # Leave out the serve form the rule does not use, so stored and published
        # rules read the way they were written.
        data: dict[str, Any] = handler(self)
        return {name: value for name, value in data.items() if value is not None}

    @property
    def bucketing_attribute(self) -> str:
        return self.bucket_by or DEFAULT_BUCKET_BY


class EnvironmentSettings(_Frozen):
    """A flag's settings in one environment."""

    enabled: StrictBool = False
    default_variant: VariantKey
    rules: Annotated[list[Rule], Field(max_length=MAX_RULES)] = []


class FlagConfig(_Frozen):
    """Everything needed to evaluate one flag in one environment."""

    key: ResourceKey
    type: FlagType
    salt: Annotated[str, Field(pattern=r"^[0-9a-f]{1,64}$")]
    version: Annotated[int, Field(ge=1)]
    variants: Annotated[list[Variant], Field(min_length=1, max_length=MAX_VARIANTS)]
    enabled: StrictBool
    default_variant: VariantKey
    rules: list[Rule]


@dataclass(frozen=True)
class InvalidFlag:
    """A flag present in a published config that this version could not read."""

    key: str
    error: str


@dataclass(frozen=True)
class EnvironmentConfig:
    """An environment's ruleset as published by ``GET /v1/environments/{env}/config``."""

    environment: str
    flags: Mapping[str, FlagConfig | InvalidFlag]


def parse_environment_config(payload: object) -> EnvironmentConfig:
    """Parse a published config.

    Each flag is parsed on its own: a flag this version cannot read (say, one
    using an operator added in a newer server) becomes an ``InvalidFlag`` that
    evaluates to ERROR, and the other flags keep working. A payload whose
    envelope is wrong raises ``ValueError``.
    """
    if not isinstance(payload, dict):
        raise ValueError("config must be a JSON object")
    environment = payload.get("environment")
    flags = payload.get("flags")
    if not isinstance(environment, dict) or not isinstance(environment.get("id"), str):
        raise ValueError("config.environment.id must be a string")
    if not isinstance(flags, dict):
        raise ValueError("config.flags must be an object")
    parsed: dict[str, FlagConfig | InvalidFlag] = {}
    for key, raw in flags.items():
        try:
            flag = FlagConfig.model_validate(raw)
        except ValidationError as exc:
            parsed[key] = InvalidFlag(key=key, error=_first_error(exc))
            continue
        if flag.key != key:
            message = f"flag published under {key!r} has key {flag.key!r}"
            parsed[key] = InvalidFlag(key=key, error=message)
            continue
        parsed[key] = flag
    return EnvironmentConfig(environment=environment["id"], flags=parsed)


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error["loc"])
    return f"{location}: {error['msg']}" if location else str(error["msg"])
