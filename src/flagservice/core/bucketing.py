"""Deterministic percentage bucketing.

A context is placed in one of ``BUCKETS`` buckets numbered 0 to 9999:

    digest = SHA-256(UTF-8(f"{flag_key}:{salt}:{value}"))
    bucket = int.from_bytes(digest[0:8], "big") % 10000

``value`` is the context attribute named by the rule's ``bucket_by`` (``key`` by
default). Flag keys and salts cannot contain ``:``, so the hashed string is
unambiguous. Splits take consecutive bucket ranges in the order they are listed,
starting at bucket 0, with weights in hundredths of a percent.

The modulo has a bias: 2**64 is not a multiple of 10000, so buckets 0 to 1615
have one more preimage in 2**64 than the others, a relative difference of about
5e-16. That is far below anything a rollout can observe.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

BUCKETS = 10_000
"""Number of buckets; a weight of 1% covers 100 of them."""


def bucket(flag_key: str, salt: str, value: str) -> int:
    """Return the bucket (0 to 9999) for ``value`` under one flag."""
    digest = hashlib.sha256(f"{flag_key}:{salt}:{value}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % BUCKETS


def bucketing_value(raw: object) -> str | None:
    """Turn a context attribute into the string that is hashed.

    Strings are used as they are and integers by their decimal form, so ``42`` and
    ``"42"`` land in the same bucket. Anything else (booleans, floats, objects,
    missing or empty values) cannot be bucketed and yields None.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        return raw or None
    if isinstance(raw, int):
        return str(raw)
    return None


def choose(allocations: Sequence[tuple[str, int]], bucket_number: int) -> str:
    """Pick the variant whose bucket range contains ``bucket_number``.

    ``allocations`` holds ``(variant, basis_points)`` pairs that must add up to
    ``BUCKETS``; the first pair owns buckets ``[0, w0)``, the next ``[w0, w0 + w1)``
    and so on.
    """
    if not 0 <= bucket_number < BUCKETS:
        raise ValueError(f"bucket {bucket_number} is outside 0..{BUCKETS - 1}")
    total = sum(points for _, points in allocations)
    if total != BUCKETS:
        raise ValueError(f"split weights cover {total} buckets, expected {BUCKETS}")
    upper = 0
    for variant, points in allocations:
        upper += points
        if bucket_number < upper:
            return variant
    raise AssertionError("unreachable: weights cover every bucket")  # pragma: no cover
