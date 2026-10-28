"""Evaluation core shared by the server and the SDK. Pure: no I/O, no state."""

from flagservice.core.bucketing import BUCKETS, bucket
from flagservice.core.evaluate import (
    SERVED_REASONS,
    EvaluationResult,
    Reason,
    evaluate,
    evaluate_in,
)
from flagservice.core.model import (
    EnvironmentConfig,
    EnvironmentSettings,
    FlagConfig,
    FlagType,
    InvalidFlag,
    Rule,
    Variant,
    parse_environment_config,
)

__all__ = [
    "BUCKETS",
    "SERVED_REASONS",
    "EnvironmentConfig",
    "EnvironmentSettings",
    "EvaluationResult",
    "FlagConfig",
    "FlagType",
    "InvalidFlag",
    "Reason",
    "Rule",
    "Variant",
    "bucket",
    "evaluate",
    "evaluate_in",
    "parse_environment_config",
]
