"""Flag evaluation: a pure function of a flag config and a context.

The server's ``POST /v1/evaluate`` and the SDK's local evaluation both call
``evaluate``; nothing here performs I/O or keeps state.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from pydantic import JsonValue

from flagservice.core.bucketing import bucket, bucketing_value, choose
from flagservice.core.model import (
    ConditionModel,
    EnvironmentConfig,
    EqualsCondition,
    FlagConfig,
    InvalidFlag,
    ListCondition,
    NumericCondition,
    Rule,
    SemverCondition,
    StringCondition,
)
from flagservice.core.semver import SemVer, parse_semver


class Reason(StrEnum):
    DISABLED = "DISABLED"
    """The flag is switched off in this environment; callers use their own default."""
    TARGETING_MATCH = "TARGETING_MATCH"
    """A rule that serves a single variant matched."""
    SPLIT = "SPLIT"
    """A rule with a percentage split matched and the context was bucketed."""
    DEFAULT = "DEFAULT"
    """No rule matched; the environment's default variant is served."""
    FLAG_NOT_FOUND = "FLAG_NOT_FOUND"
    """No flag with this key exists in the environment."""
    ERROR = "ERROR"
    """The flag or the context could not be evaluated; callers use their own default."""


SERVED_REASONS = frozenset({Reason.TARGETING_MATCH, Reason.SPLIT, Reason.DEFAULT})
"""Reasons that come with a variant and a value."""


@dataclass(frozen=True)
class EvaluationResult:
    variant: str | None
    value: JsonValue
    reason: Reason
    rule_index: int | None = None
    """Position of the matching rule in the environment's rule list."""
    error: str | None = None


Context = Mapping[str, object]


def evaluate(flag: FlagConfig | InvalidFlag | None, context: object) -> EvaluationResult:
    """Evaluate one flag for one context. Never raises."""
    if flag is None:
        return EvaluationResult(variant=None, value=None, reason=Reason.FLAG_NOT_FOUND)
    if isinstance(flag, InvalidFlag):
        return _error(f"flag config could not be read: {flag.error}")
    if not isinstance(context, Mapping):
        return _error("context must be a mapping of attribute names to values")
    try:
        return _evaluate(flag, context)
    except Exception as exc:  # a corrupted config must not break the caller
        return _error(f"{type(exc).__name__}: {exc}")


def evaluate_in(config: EnvironmentConfig, flag_key: str, context: object) -> EvaluationResult:
    """Evaluate ``flag_key`` from a published environment config."""
    return evaluate(config.flags.get(flag_key), context)


def _evaluate(flag: FlagConfig, context: Context) -> EvaluationResult:
    if not flag.enabled:
        return EvaluationResult(variant=None, value=None, reason=Reason.DISABLED)
    values = {variant.key: variant.value for variant in flag.variants}
    for index, rule in enumerate(flag.rules):
        if not all(condition_matches(condition, context) for condition in rule.conditions):
            continue
        if rule.variant is not None:
            return _served(values, rule.variant, Reason.TARGETING_MATCH, index)
        served = _split_variant(flag, rule, context)
        if served is None:
            # The context has no usable bucketing attribute, so it cannot be
            # placed in the split; the rule does not apply to it.
            continue
        return _served(values, served, Reason.SPLIT, index)
    return _served(values, flag.default_variant, Reason.DEFAULT, None)


def _served(
    values: Mapping[str, JsonValue], variant: str, reason: Reason, index: int | None
) -> EvaluationResult:
    if variant not in values:
        return _error(f"variant {variant!r} is not defined on the flag")
    return EvaluationResult(variant=variant, value=values[variant], reason=reason, rule_index=index)


def _split_variant(flag: FlagConfig, rule: Rule, context: Context) -> str | None:
    assert rule.split is not None
    value = bucketing_value(context.get(rule.bucketing_attribute))
    if value is None:
        return None
    allocations = [(allocation.variant, allocation.basis_points) for allocation in rule.split]
    return choose(allocations, bucket(flag.key, flag.salt, value))


def _error(message: str) -> EvaluationResult:
    return EvaluationResult(variant=None, value=None, reason=Reason.ERROR, error=message)


# Conditions


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _is_scalar(value: object) -> bool:
    return isinstance(value, str | bool) or _is_number(value)


def scalar_equals(left: object, right: object) -> bool:
    """Type-strict equality: strings, booleans and numbers only equal their own kind."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if _is_number(left) and _is_number(right):
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    return False


_NUMERIC: dict[str, Callable[[float, float], bool]] = {
    "lt": operator.lt,
    "lte": operator.le,
    "gt": operator.gt,
    "gte": operator.ge,
}

_SEMVER: dict[str, Callable[[SemVer, SemVer], bool]] = {
    "semver_eq": operator.eq,
    "semver_lt": operator.lt,
    "semver_lte": operator.le,
    "semver_gt": operator.gt,
    "semver_gte": operator.ge,
}


def condition_matches(condition: ConditionModel, context: Context) -> bool:
    """Whether one condition holds for the context.

    A condition on an attribute the context does not have, or whose value has the
    wrong kind for the operator, does not match. That holds for ``neq`` and
    ``not_in`` too: a missing attribute is never "not equal" to anything.
    """
    if condition.attribute not in context:
        return False
    actual = context[condition.attribute]
    match condition:
        case EqualsCondition(operator="eq", value=expected):
            return scalar_equals(actual, expected)
        case EqualsCondition(value=expected):
            return _is_scalar(actual) and not scalar_equals(actual, expected)
        case ListCondition(operator="in", value=options):
            return any(scalar_equals(actual, option) for option in options)
        case ListCondition(value=options):
            return _is_scalar(actual) and not any(scalar_equals(actual, o) for o in options)
        case StringCondition(operator="contains", value=fragment):
            return isinstance(actual, str) and fragment in actual
        case StringCondition(value=prefix):
            return isinstance(actual, str) and actual.startswith(prefix)
        case NumericCondition(operator=name, value=bound):
            if not isinstance(actual, int | float) or isinstance(actual, bool):
                return False
            return _NUMERIC[name](actual, bound)
        case SemverCondition(operator=name, value=text):
            if not isinstance(actual, str):
                return False
            version, target = parse_semver(actual), parse_semver(text)
            if version is None or target is None:
                return False
            return _SEMVER[name](version, target)
    raise AssertionError(f"unhandled condition {condition!r}")  # pragma: no cover
