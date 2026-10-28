"""Check percentage bucketing on a fixed population, deterministically.

    python examples/rollout_distribution.py

Prints how 100,000 users ``user-0`` to ``user-99999`` split over a 10/30/60
split with a fixed salt, a chi-square goodness-of-fit test of those counts, and
what happens to them when a 10% rollout is raised to 20%. The README quotes
this script's output.
"""

from __future__ import annotations

import math
from collections import Counter

from flagservice.core import FlagConfig, evaluate

USERS = [f"user-{i}" for i in range(100_000)]
SALT = "0123456789abcdef"


def flag(*weights: tuple[str, float]) -> FlagConfig:
    return FlagConfig.model_validate(
        {
            "key": "rollout-demo",
            "type": "string",
            "salt": SALT,
            "version": 1,
            "variants": [{"key": name, "value": name} for name, _ in weights],
            "enabled": True,
            "default_variant": weights[0][0],
            "rules": [{"split": [{"variant": n, "weight": w} for n, w in weights]}],
        }
    )


def served(config: FlagConfig) -> dict[str, str | None]:
    return {user: evaluate(config, {"key": user}).variant for user in USERS}


def main() -> None:
    weights = (("a", 10.0), ("b", 30.0), ("c", 60.0))
    counts = Counter(served(flag(*weights)).values())
    statistic = 0.0
    print(f"Split 10/30/60 over {len(USERS):,} users (salt {SALT}):")
    for name, weight in weights:
        expected = len(USERS) * weight / 100
        statistic += (counts[name] - expected) ** 2 / expected
        print(f"  {name}: {counts[name]:6,} observed, {expected:8,.0f} expected")
    # With 2 degrees of freedom the chi-square survival function is exp(-x / 2).
    print(
        f"  chi-square = {statistic:.3f} (2 degrees of freedom), p = {math.exp(-statistic / 2):.3f}"
    )

    at_10 = served(flag(("on", 10), ("off", 90)))
    at_20 = served(flag(("on", 20), ("off", 80)))
    on_10 = {user for user, variant in at_10.items() if variant == "on"}
    on_20 = {user for user, variant in at_20.items() if variant == "on"}
    print("Raising a rollout from 10% to 20%:")
    print(f"  on at 10%: {len(on_10):,}; on at 20%: {len(on_20):,}")
    print(f"  on at 10% but not at 20%: {len(on_10 - on_20)}")
    print(f"  newly on at 20%: {len(on_20 - on_10):,}")


if __name__ == "__main__":
    main()
