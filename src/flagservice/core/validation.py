"""Checks that need a whole flag, run by the server before every write.

Each check returns a list of problems with a location relative to the object it
was given, so the caller can prefix the request path and report every problem
at once.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from flagservice.core.model import EnvironmentSettings, FlagType, Variant

Location = tuple[str | int, ...]


@dataclass(frozen=True)
class Problem:
    loc: Location
    msg: str

    def under(self, *prefix: str | int) -> Problem:
        return Problem(loc=(*prefix, *self.loc), msg=self.msg)


_TYPE_NAMES: dict[FlagType, str] = {
    "boolean": "a boolean",
    "string": "a string",
    "number": "a finite number",
    "json": "a JSON value without NaN or infinity",
}


def value_matches_type(flag_type: FlagType, value: object) -> bool:
    """Whether ``value`` may be served by a flag of ``flag_type``."""
    if flag_type == "boolean":
        return isinstance(value, bool)
    if flag_type == "string":
        return isinstance(value, str)
    if flag_type == "number":
        return (
            isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        )
    return _finite_json(value)


def _finite_json(value: object) -> bool:
    if value is None or isinstance(value, str | bool | int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_finite_json(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _finite_json(item) for key, item in value.items())
    return False


def variant_problems(flag_type: FlagType, variants: Sequence[Variant]) -> list[Problem]:
    """Duplicate variant keys and values that do not match the flag type."""
    problems: list[Problem] = []
    seen: set[str] = set()
    for index, variant in enumerate(variants):
        if variant.key in seen:
            problems.append(Problem((index, "key"), f"duplicate variant key {variant.key!r}"))
        seen.add(variant.key)
        if not value_matches_type(flag_type, variant.value):
            problems.append(
                Problem(
                    (index, "value"),
                    f"variant {variant.key!r} has value {variant.value!r}, "
                    f"but a {flag_type} flag needs {_TYPE_NAMES[flag_type]}",
                )
            )
    return problems


def settings_problems(
    settings: EnvironmentSettings, variant_keys: Collection[str]
) -> list[Problem]:
    """References from an environment's settings to variants the flag does not define."""
    known = ", ".join(sorted(variant_keys))

    def unknown(variant: str) -> str:
        return f"unknown variant {variant!r}; the flag defines: {known}"

    problems: list[Problem] = []
    if settings.default_variant not in variant_keys:
        problems.append(Problem(("default_variant",), unknown(settings.default_variant)))
    for index, rule in enumerate(settings.rules):
        if rule.variant is not None and rule.variant not in variant_keys:
            problems.append(Problem(("rules", index, "variant"), unknown(rule.variant)))
        for position, allocation in enumerate(rule.split or ()):
            if allocation.variant not in variant_keys:
                problems.append(
                    Problem(
                        ("rules", index, "split", position, "variant"),
                        unknown(allocation.variant),
                    )
                )
    return problems
