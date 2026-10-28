"""Python SDK: evaluate flags locally from a config polled in the background."""

from flagservice.core.evaluate import EvaluationResult, Reason
from flagservice.sdk.client import FlagClient

__all__ = ["EvaluationResult", "FlagClient", "Reason"]
