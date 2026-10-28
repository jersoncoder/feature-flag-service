from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import settings as hypothesis_settings

from flagservice.server.app import create_app
from flagservice.server.settings import Settings
from helpers import ADMIN_KEY, Api

# Property tests run from a fixed seed so that every run, locally and in CI,
# checks the same examples.
hypothesis_settings.register_profile(
    "deterministic", derandomize=True, deadline=None, max_examples=200
)
hypothesis_settings.load_profile("deterministic")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(database_path=tmp_path / "flags.db", admin_key=ADMIN_KEY)


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def api(client: TestClient) -> Api:
    return Api(client)
