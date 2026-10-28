"""Properties of percentage bucketing: determinism, monotonic rollouts, distribution."""

from __future__ import annotations

import hashlib
import random
from collections import Counter

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flagservice.core.bucketing import BUCKETS, bucket, bucketing_value, choose
from flagservice.core.evaluate import Reason, evaluate
from helpers import chi_square_sf, chi_square_statistic, make_flag, split_rule

flag_keys = st.from_regex(r"[a-z0-9][a-z0-9_-]{0,20}", fullmatch=True)
salts = st.from_regex(r"[0-9a-f]{1,16}", fullmatch=True)
user_keys = st.text(min_size=1, max_size=40)


# Determinism


@given(flag_keys, salts, user_keys)
def test_bucket_follows_the_documented_formula(flag_key: str, salt: str, value: str) -> None:
    digest = hashlib.sha256(f"{flag_key}:{salt}:{value}".encode()).digest()
    assert bucket(flag_key, salt, value) == int.from_bytes(digest[:8], "big") % 10000


@pytest.mark.parametrize(
    ("flag_key", "salt", "value", "expected"),
    [
        ("new-checkout", "5f2c8a91d3e4b607", "user-1", 4900),
        ("new-checkout", "5f2c8a91d3e4b607", "user-2", 9013),
        ("dark-mode", "00", "42", 4975),
    ],
)
def test_bucket_reference_vectors(flag_key: str, salt: str, value: str, expected: int) -> None:
    # Pinned values: changing them reassigns every user of every rollout, and
    # any other SDK that implements the same formula must produce them too.
    assert bucket(flag_key, salt, value) == expected


@given(
    st.dictionaries(st.sampled_from(["key", "country", "plan", "age"]), st.integers(), min_size=1),
    st.text(min_size=1),
)
def test_same_context_always_gets_the_same_variant(extra: dict[str, int], key: str) -> None:
    flag = make_flag(
        variants=[("a", False), ("b", True), ("c", False)],
        rules=[split_rule(("a", 20), ("b", 30), ("c", 50))],
    )
    context = {**extra, "key": key}
    first = evaluate(flag, context)
    assert first.reason is Reason.SPLIT
    # Repeated calls, a copy in another key order, and changes to attributes the
    # split does not use all give the same answer.
    reordered = dict(reversed(list(context.items())))
    changed = {**context, "country": "elsewhere", "plan": "other"}
    for candidate in (context, reordered, changed, dict(context)):
        assert evaluate(flag, candidate) == first


def test_integer_and_string_keys_share_a_bucket() -> None:
    assert bucketing_value(42) == bucketing_value("42") == "42"


@pytest.mark.parametrize("raw", [None, "", True, False, 1.5, ["a"], {"id": 1}])
def test_values_that_cannot_be_bucketed(raw: object) -> None:
    assert bucketing_value(raw) is None


# Monotonic rollouts

percents = st.integers(min_value=0, max_value=10000)


@given(user_keys, percents, percents)
def test_raising_a_rollout_keeps_everyone_already_in(key: str, a: int, b: int) -> None:
    low, high = sorted((a, b))

    def served(points: int, on_first: bool) -> str | None:
        on, off = ("on", points / 100), ("off", (10000 - points) / 100)
        split = (on, off) if on_first else (off, on)
        flag = make_flag(rules=[split_rule(*split)])
        return evaluate(flag, {"key": key}).variant

    # Either listing order works for two variants: the growing range keeps one end fixed.
    for on_first in (True, False):
        if served(low, on_first) == "on":
            assert served(high, on_first) == "on"


@given(
    user_keys, st.integers(min_value=0, max_value=5000), st.integers(min_value=0, max_value=5000)
)
def test_growing_a_variant_from_its_neighbour_keeps_its_users(
    key: str, start: int, extra: int
) -> None:
    # Three variants: 'b' grows by taking weight only from 'c', the entry after it,
    # so the start of b's range stays where it is and its range only widens.
    extra = min(extra, 5000 - start)

    def served(b_points: int) -> str | None:
        weights = (("a", 50), ("b", b_points / 100), ("c", (5000 - b_points) / 100))
        flag = make_flag(
            flag_type="number",
            variants=[("a", 1), ("b", 2), ("c", 3)],
            default_variant="a",
            rules=[split_rule(*weights)],
        )
        return evaluate(flag, {"key": key}).variant

    if served(start) == "b":
        assert served(start + extra) == "b"
    if served(start) == "a":
        assert served(start + extra) == "a"


def test_rollout_reassigns_only_the_new_share() -> None:
    keys = [f"user-{i}" for i in range(20_000)]

    def on_set(percent: float) -> set[str]:
        flag = make_flag(rules=[split_rule(("on", percent), ("off", 100 - percent))])
        return {k for k in keys if evaluate(flag, {"key": k}).variant == "on"}

    at_10, at_20 = on_set(10), on_set(20)
    assert at_10 <= at_20
    # About a tenth of the users joined; nobody left.
    assert 0.09 < len(at_20 - at_10) / len(keys) < 0.11


# Distribution


def _keys(seed: int, count: int) -> list[str]:
    rng = random.Random(seed)
    return [f"{rng.getrandbits(64):016x}" for _ in range(count)]


@pytest.mark.parametrize(
    "weights",
    [
        (10, 30, 60),
        (33.33, 33.33, 33.34),
        (1, 99),
        (25, 25, 25, 25),
        (0.5, 4.5, 95),
    ],
)
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_split_distribution_matches_weights(weights: tuple[float, ...], seed: int) -> None:
    names = [f"v{i}" for i in range(len(weights))]
    flag = make_flag(
        flag_type="string",
        salt=f"{seed:08x}",
        variants=[(name, name) for name in names],
        default_variant=names[0],
        rules=[split_rule(*zip(names, weights, strict=True))],
    )
    keys = _keys(seed, 50_000)
    counts = Counter(evaluate(flag, {"key": k}).variant for k in keys)
    observed = [counts[name] for name in names]
    expected = [len(keys) * w / 100 for w in weights]
    p_value = chi_square_sf(chi_square_statistic(observed, expected), len(weights) - 1)
    assert p_value > 0.001, (observed, expected, p_value)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_buckets_are_uniform(seed: int) -> None:
    bins = Counter(bucket("uniformity", f"{seed:x}", k) // 100 for k in _keys(seed, 100_000))
    observed = [bins[i] for i in range(100)]
    p_value = chi_square_sf(chi_square_statistic(observed, [1000.0] * 100), 99)
    assert p_value > 0.001, p_value


def test_flags_with_different_salts_bucket_independently() -> None:
    # Two 50% rollouts: being in one must not predict being in the other.
    keys = _keys(7, 40_000)
    table = Counter(
        (bucket("feature-a", "aaaa", k) < 5000, bucket("feature-b", "bbbb", k) < 5000) for k in keys
    )
    observed = [table[(x, y)] for x in (True, False) for y in (True, False)]
    rows = [observed[0] + observed[1], observed[2] + observed[3]]
    cols = [observed[0] + observed[2], observed[1] + observed[3]]
    expected = [r * c / len(keys) for r in rows for c in cols]
    p_value = chi_square_sf(chi_square_statistic(observed, expected), 1)
    assert p_value > 0.001, (observed, p_value)


# choose()


def test_choose_boundaries() -> None:
    allocations = [("on", 1000), ("off", 9000)]
    assert choose(allocations, 0) == "on"
    assert choose(allocations, 999) == "on"
    assert choose(allocations, 1000) == "off"
    assert choose(allocations, BUCKETS - 1) == "off"


def test_choose_skips_zero_weights() -> None:
    allocations = [("a", 0), ("b", 10000), ("c", 0)]
    assert {choose(allocations, b) for b in range(0, BUCKETS, 7)} == {"b"}


def test_choose_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="expected 10000"):
        choose([("a", 5000)], 10)
    with pytest.raises(ValueError, match="outside"):
        choose([("a", 10000)], BUCKETS)
