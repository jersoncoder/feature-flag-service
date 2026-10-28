"""Test helpers: flag builders and a chi-square test with no third-party dependency."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from flagservice.core.model import FlagConfig


def make_flag(
    *,
    key: str = "checkout",
    flag_type: str = "boolean",
    salt: str = "a1b2c3d4",
    variants: Sequence[tuple[str, Any]] = (("off", False), ("on", True)),
    enabled: bool = True,
    default_variant: str = "off",
    rules: Sequence[dict[str, Any]] = (),
) -> FlagConfig:
    return FlagConfig.model_validate(
        {
            "key": key,
            "type": flag_type,
            "salt": salt,
            "version": 1,
            "variants": [{"key": k, "value": v} for k, v in variants],
            "enabled": enabled,
            "default_variant": default_variant,
            "rules": list(rules),
        }
    )


def split_rule(*weights: tuple[str, float], bucket_by: str | None = None) -> dict[str, Any]:
    rule: dict[str, Any] = {"split": [{"variant": v, "weight": w} for v, w in weights]}
    if bucket_by is not None:
        rule["bucket_by"] = bucket_by
    return rule


def chi_square_statistic(observed: Sequence[int], expected: Sequence[float]) -> float:
    return sum((o - e) ** 2 / e for o, e in zip(observed, expected, strict=True))


def chi_square_sf(statistic: float, dof: int) -> float:
    """P(X >= statistic) for X ~ chi-square with an integer number of degrees of freedom.

    Uses the closed forms of the regularized upper incomplete gamma function
    Q(dof / 2, statistic / 2) for integer and half-integer shape parameters.
    """
    if dof < 1:
        raise ValueError("dof must be at least 1")
    if statistic <= 0:
        return 1.0
    half = statistic / 2

    def term(power: float) -> float:
        # half**power * exp(-half) / Gamma(power + 1), computed in log space.
        return math.exp(power * math.log(half) - half - math.lgamma(power + 1))

    if dof % 2 == 0:
        return min(1.0, sum(term(i) for i in range(dof // 2)))
    tail = math.erfc(math.sqrt(half))
    return min(1.0, tail + sum(term(i - 0.5) for i in range(1, (dof - 1) // 2 + 1)))
