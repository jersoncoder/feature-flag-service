"""Evaluation semantics: reasons, rule order, operators and the config schema."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from flagservice.core.evaluate import EvaluationResult, Reason, evaluate, evaluate_in
from flagservice.core.model import (
    InvalidFlag,
    Rule,
    parse_environment_config,
)
from helpers import make_flag, split_rule


def rule(*conditions: tuple[str, str, Any], variant: str = "on") -> dict[str, Any]:
    return {
        "conditions": [{"attribute": a, "operator": op, "value": val} for a, op, val in conditions],
        "variant": variant,
    }


# Reasons


def test_flag_not_found() -> None:
    assert evaluate(None, {}) == EvaluationResult(None, None, Reason.FLAG_NOT_FOUND)


def test_disabled_flag_serves_nothing() -> None:
    flag = make_flag(enabled=False, rules=[rule()])
    assert evaluate(flag, {"key": "u"}) == EvaluationResult(None, None, Reason.DISABLED)


def test_default_when_no_rule_matches() -> None:
    flag = make_flag(rules=[rule(("country", "eq", "DE"))])
    assert evaluate(flag, {"country": "FR"}) == EvaluationResult("off", False, Reason.DEFAULT)


def test_targeting_match_reports_the_rule() -> None:
    flag = make_flag(
        rules=[rule(("country", "eq", "FR"), variant="off"), rule(("country", "eq", "DE"))]
    )
    assert evaluate(flag, {"country": "DE"}) == EvaluationResult(
        "on", True, Reason.TARGETING_MATCH, rule_index=1
    )


def test_split_reason() -> None:
    flag = make_flag(rules=[split_rule(("on", 100), ("off", 0))])
    assert evaluate(flag, {"key": "u"}) == EvaluationResult("on", True, Reason.SPLIT, rule_index=0)


def test_first_matching_rule_wins() -> None:
    flag = make_flag(
        variants=[("a", "a"), ("b", "b"), ("c", "c")],
        flag_type="string",
        default_variant="c",
        rules=[rule(("plan", "eq", "pro"), variant="a"), rule(("plan", "eq", "pro"), variant="b")],
    )
    assert evaluate(flag, {"plan": "pro"}).variant == "a"


def test_all_conditions_of_a_rule_must_match() -> None:
    flag = make_flag(rules=[rule(("country", "eq", "DE"), ("plan", "eq", "pro"))])
    assert evaluate(flag, {"country": "DE", "plan": "pro"}).variant == "on"
    assert evaluate(flag, {"country": "DE", "plan": "free"}).variant == "off"
    assert evaluate(flag, {"country": "DE"}).variant == "off"


def test_rule_without_conditions_matches_everyone() -> None:
    flag = make_flag(rules=[rule()])
    assert evaluate(flag, {}).reason is Reason.TARGETING_MATCH


def test_split_without_bucketing_attribute_falls_through() -> None:
    flag = make_flag(rules=[split_rule(("on", 50), ("off", 50)), rule(variant="on")])
    for context in ({}, {"key": ""}, {"key": True}, {"key": 3.5}, {"key": None}):
        result = evaluate(flag, context)
        assert (result.reason, result.rule_index) == (Reason.TARGETING_MATCH, 1), context


def test_split_buckets_by_the_named_attribute() -> None:
    flag = make_flag(rules=[split_rule(("on", 50), ("off", 50), bucket_by="org")])
    # Everyone in one organisation gets the same variant, whatever their own key.
    for org in ("acme", "globex", "initech"):
        served = {evaluate(flag, {"org": org, "key": f"user-{i}"}).variant for i in range(50)}
        assert len(served) == 1


def test_unknown_variant_in_config_is_an_error() -> None:
    # The server's validation prevents this; the evaluator must still not crash.
    flag = make_flag(rules=[rule(variant="ghost")])
    result = evaluate(flag, {})
    assert result.reason is Reason.ERROR
    assert result.error is not None
    assert "ghost" in result.error
    assert evaluate(make_flag(default_variant="ghost"), {}).reason is Reason.ERROR


def test_invalid_flag_and_bad_context_are_errors() -> None:
    assert evaluate(InvalidFlag("x", "bad"), {}).reason is Reason.ERROR
    for context in (None, "user-1", ["key"], 42):
        result = evaluate(make_flag(), context)
        assert result.reason is Reason.ERROR
        assert result.error is not None
        assert "mapping" in result.error


# Operators

OPERATOR_CASES: list[tuple[str, Any, Any, bool]] = [
    # operator, condition value, context value, matches
    ("eq", "DE", "DE", True),
    ("eq", "DE", "de", False),
    ("eq", 1, 1.0, True),
    ("eq", 1, "1", False),
    ("eq", 1, True, False),
    ("eq", True, 1, False),
    ("eq", True, True, True),
    ("eq", "a", ["a"], False),
    ("neq", "DE", "FR", True),
    ("neq", "DE", "DE", False),
    ("neq", 1, "1", True),
    ("neq", "a", ["a"], False),
    ("neq", "a", None, False),
    ("in", ["DE", "FR"], "FR", True),
    ("in", ["DE", "FR"], "IT", False),
    ("in", [1, 2], 2.0, True),
    ("in", [1, 2], True, False),
    ("in", [], "DE", False),
    ("not_in", ["DE", "FR"], "IT", True),
    ("not_in", ["DE", "FR"], "DE", False),
    ("not_in", ["DE"], {"country": "IT"}, False),
    ("contains", "@example.com", "ana@example.com", True),
    ("contains", "@example.com", "ana@EXAMPLE.com", False),
    ("contains", "1", 1234, False),
    ("starts_with", "beta-", "beta-7", True),
    ("starts_with", "beta-", "7-beta-", False),
    ("lt", 18, 17, True),
    ("lt", 18, 18, False),
    ("lte", 18, 18, True),
    ("gt", 18, 18.5, True),
    ("gte", 18, 18, True),
    ("gte", 18, 17.999, False),
    ("gt", 0, True, False),
    ("gt", 0, "5", False),
    ("lt", 0, float("nan"), False),
    ("gte", 0, float("nan"), False),
    ("semver_gte", "2.1.0", "2.1.0", True),
    ("semver_gte", "2.1.0", "2.10.0", True),
    ("semver_gte", "2.1.0", "2.1.0-rc.1", False),
    ("semver_lt", "2.0.0", "1.99.99", True),
    ("semver_lte", "2.0.0", "2.0.0+build", True),
    ("semver_gt", "1.0.0-alpha", "1.0.0-alpha.1", True),
    ("semver_eq", "1.2.3", "1.2.3+x", True),
    ("semver_eq", "1.2.3", "1.2", False),
    ("semver_gt", "1.0.0", 2, False),
]


@pytest.mark.parametrize(("operator", "value", "actual", "expected"), OPERATOR_CASES)
def test_operator(operator: str, value: Any, actual: Any, expected: bool) -> None:
    flag = make_flag(rules=[rule(("attr", operator, value))])
    assert (evaluate(flag, {"attr": actual}).variant == "on") is expected


@pytest.mark.parametrize("operator", ["eq", "neq", "in", "not_in", "contains", "lt", "semver_eq"])
def test_missing_attribute_never_matches(operator: str) -> None:
    value = {"in": ["x"], "not_in": ["x"], "lt": 1, "semver_eq": "1.0.0"}.get(operator, "x")
    flag = make_flag(rules=[rule(("attr", operator, value))])
    assert evaluate(flag, {"other": "x"}).reason is Reason.DEFAULT


# Schema


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"conditions": []}, "exactly one of"),
        ({"variant": "on", "split": [{"variant": "on", "weight": 100}]}, "exactly one of"),
        (
            {"split": [{"variant": "a", "weight": 60}, {"variant": "b", "weight": 30}]},
            "sum to 100, got 90",
        ),
        (
            {"split": [{"variant": "a", "weight": 50}, {"variant": "a", "weight": 50}]},
            "appears twice",
        ),
        (
            {"split": [{"variant": "a", "weight": 99.995}, {"variant": "b", "weight": 0.005}]},
            "0.01",
        ),
        ({"split": [{"variant": "a", "weight": 101}]}, "less than or equal to 100"),
        ({"variant": "on", "bucket_by": "org"}, "only applies"),
        (
            {
                "variant": "on",
                "conditions": [{"attribute": "v", "operator": "semver_gt", "value": "1.0"}],
            },
            "semantic version",
        ),
        (
            {
                "variant": "on",
                "conditions": [{"attribute": "v", "operator": "matches", "value": "x"}],
            },
            "does not match any of the expected tags",
        ),
        (
            {"variant": "on", "conditions": [{"attribute": "n", "operator": "lt", "value": "5"}]},
            "valid number",
        ),
        (
            {"variant": "on", "conditions": [{"attribute": "", "operator": "eq", "value": "x"}]},
            "at least 1 character",
        ),
        ({"variant": "on", "unknown": 1}, "Extra inputs"),
    ],
)
def test_rule_validation(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Rule.model_validate(data)


def test_weights_with_two_decimals_are_accepted() -> None:
    parsed = Rule.model_validate(split_rule(("a", 33.33), ("b", 33.33), ("c", 33.34)))
    assert parsed.split is not None
    assert [a.basis_points for a in parsed.split] == [3333, 3333, 3334]


def test_rule_serialisation_omits_the_unused_serve_form() -> None:
    single = Rule.model_validate({"variant": "on"})
    assert single.model_dump(mode="json") == {"conditions": [], "variant": "on"}
    split = Rule.model_validate(split_rule(("on", 10), ("off", 90)))
    dumped = split.model_dump(mode="json")
    assert set(dumped) == {"conditions", "split"}
    assert Rule.model_validate(dumped) == split


# Published configs


def _config(flags: dict[str, Any]) -> dict[str, Any]:
    return {
        "environment": {"id": "env_000000000001", "key": "prod", "project": "p"},
        "flags": flags,
    }


def test_parse_config_isolates_unreadable_flags() -> None:
    good = make_flag(key="good").model_dump(mode="json")
    newer = {**make_flag(key="newer").model_dump(mode="json")}
    newer["rules"] = [
        {"conditions": [{"attribute": "x", "operator": "regex", "value": "."}], "variant": "on"}
    ]
    renamed = make_flag(key="other").model_dump(mode="json")
    config = parse_environment_config(_config({"good": good, "newer": newer, "renamed": renamed}))
    assert config.environment == "env_000000000001"
    assert evaluate_in(config, "good", {}).reason is Reason.DEFAULT
    for key in ("newer", "renamed"):
        result = evaluate_in(config, key, {})
        assert result.reason is Reason.ERROR, key
    assert evaluate_in(config, "missing", {}).reason is Reason.FLAG_NOT_FOUND


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"flags": {}},
        {"environment": {"id": 1}, "flags": {}},
        {"environment": {"id": "e"}, "flags": []},
    ],
)
def test_parse_config_rejects_a_bad_envelope(payload: Any) -> None:
    with pytest.raises(ValueError, match="config"):
        parse_environment_config(payload)
