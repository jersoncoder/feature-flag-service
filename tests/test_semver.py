from __future__ import annotations

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from flagservice.core.semver import SemVer, parse_semver


def v(text: str) -> SemVer:
    parsed = parse_semver(text)
    assert parsed is not None, text
    return parsed


def test_precedence_example_from_the_specification() -> None:
    # Section 11.4 of semver.org lists these in ascending precedence.
    ordered = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.1",
        "1.1.0",
        "2.0.0",
    ]
    versions = [v(text) for text in ordered]
    for lower, higher in itertools.pairwise(versions):
        assert lower < higher
        assert higher > lower
        assert lower <= higher
        assert not lower >= higher
    assert sorted(reversed(versions)) == versions


def test_numeric_parts_compare_as_numbers() -> None:
    assert v("1.10.0") > v("1.9.0")
    assert v("1.0.0-rc.10") > v("1.0.0-rc.9")
    assert v("1.0.0-10") < v("1.0.0-9a")  # numeric identifiers sort before alphanumeric ones


def test_build_metadata_is_ignored_for_precedence() -> None:
    assert v("1.2.3+build.1") == v("1.2.3+build.2")
    assert v("1.2.3+anything") == v("1.2.3")
    assert hash(v("1.2.3+a")) == hash(v("1.2.3"))
    assert str(v("1.2.3-rc.1+build.5")) == "1.2.3-rc.1+build.5"


@pytest.mark.parametrize(
    "text",
    [
        "1.2",
        "1.2.3.4",
        "v1.2.3",
        "01.2.3",
        "1.02.3",
        "1.2.3-01",
        "1.2.3-",
        "1.2.3+",
        "1.2.3-a..b",
        "1.2.3\n",
        " 1.2.3",
        "\u0661.2.3",  # ARABIC-INDIC DIGIT ONE is a Unicode digit, not an ASCII one
        "",
    ],
)
def test_invalid_versions_are_rejected(text: str) -> None:
    assert parse_semver(text) is None


def test_comparison_with_other_types() -> None:
    assert v("1.0.0") != "1.0.0"


releases = st.tuples(*(st.integers(min_value=0, max_value=50) for _ in range(3)))


@given(releases, releases)
def test_release_order_is_tuple_order(a: tuple[int, int, int], b: tuple[int, int, int]) -> None:
    left, right = v(".".join(map(str, a))), v(".".join(map(str, b)))
    assert (left < right) == (a < b)
    assert (left == right) == (a == b)


identifiers = st.one_of(
    st.integers(min_value=0, max_value=30).map(str),
    st.text(alphabet="abcxyz-", min_size=1, max_size=4),
)
prereleases = st.lists(identifiers, min_size=1, max_size=3).map(".".join)


@given(prereleases, prereleases, prereleases)
def test_prerelease_order_is_a_total_order(a: str, b: str, c: str) -> None:
    x, y, z = v(f"1.0.0-{a}"), v(f"1.0.0-{b}"), v(f"1.0.0-{c}")
    assert sum([x < y, x == y, x > y]) == 1
    if x <= y and y <= z:
        assert x <= z
    assert x < v("1.0.0")
