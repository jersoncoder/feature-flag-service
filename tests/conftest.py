from __future__ import annotations

from hypothesis import settings

# Property tests run from a fixed seed so that every run, locally and in CI,
# checks the same examples.
settings.register_profile("deterministic", derandomize=True, deadline=None, max_examples=200)
settings.load_profile("deterministic")
