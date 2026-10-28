"""Concurrent writers holding the same ETag: exactly one wins."""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Callable
from contextlib import closing
from typing import TypeVar

from fastapi.testclient import TestClient

from flagservice.core.model import EnvironmentSettings
from flagservice.server.db import connect
from flagservice.server.errors import PreconditionFailed
from flagservice.server.settings import Settings
from flagservice.server.store import Store
from helpers import Api

T = TypeVar("T")


def run_together(count: int, work: Callable[[int], T]) -> list[T]:
    """Run ``work(i)`` on ``count`` threads released at the same moment."""
    barrier = threading.Barrier(count)
    results: list[T | None] = [None] * count
    errors: list[BaseException] = []

    def target(index: int) -> None:
        barrier.wait()
        try:
            results[index] = work(index)
        except BaseException as exc:  # pragma: no cover - re-raised below
            errors.append(exc)

    threads = [threading.Thread(target=target, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]
    return [result for result in results if result is not None]


def test_store_level_writers_race(settings: Settings, api: Api) -> None:
    api.project(environments=("production",))
    api.flag({"key": "beta", "type": "boolean"})
    for round_number in range(10):
        _, etag = api.get_flag("beta")

        def write(index: int, etag: str = etag) -> str:
            with closing(connect(settings.database_path)) as conn:
                try:
                    Store(conn).update_flag_environment(
                        "shop",
                        "beta",
                        "production",
                        EnvironmentSettings(enabled=index % 2 == 0, default_variant="on"),
                        [(False, etag)],
                        f"writer-{index}",
                    )
                except PreconditionFailed:
                    return "412"
                return "200"

        outcomes = Counter(run_together(4, write))
        assert outcomes == {"200": 1, "412": 3}, round_number
    flag, _ = api.get_flag("beta")
    assert flag["version"] == 1 + 10


def test_http_writers_with_the_same_etag(client: TestClient, api: Api) -> None:
    api.project(environments=("production",))
    _, etag = api.flag({"key": "beta", "type": "boolean"})

    def write(index: int) -> int:
        settings = {"enabled": True, "default_variant": "on" if index % 2 else "off"}
        return api.put_settings("beta", "production", settings, etag).status_code

    assert sorted(run_together(2, write)) == [200, 412]
    flag, _ = api.get_flag("beta")
    assert flag["version"] == 2
