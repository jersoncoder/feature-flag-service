"""Credentials: each key kind reaches its own endpoints and nothing else."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flagservice.server.auth import sha256_hex
from flagservice.server.settings import Settings
from flagservice.tokens import environment_of_token
from helpers import ADMIN_KEY, Api, bearer


@pytest.fixture
def tokens(api: Api) -> dict[str, str]:
    api.project()
    api.flag({"key": "beta", "type": "boolean"})
    return {"admin": ADMIN_KEY, "server": api.key("server"), "sdk": api.key("sdk")}


def call(client: TestClient, api: Api, endpoint: str, token: str | None) -> int:
    headers = bearer(token) if token is not None else {}
    if endpoint == "management-read":
        return client.get("/v1/projects/shop/flags", headers=headers).status_code
    if endpoint == "management-write":
        flag = {"key": "made-by-key", "type": "boolean"}
        return client.post("/v1/projects/shop/flags", json=flag, headers=headers).status_code
    if endpoint == "evaluate":
        request = {"flag": "beta", "context": {}}
        return client.post("/v1/evaluate", json=request, headers=headers).status_code
    if endpoint == "config":
        path = f"/v1/environments/{api.environment_id()}/config"
        return client.get(path, headers=headers).status_code
    raise AssertionError(endpoint)


# Which credential may call which endpoint: 2xx where allowed, 403 elsewhere.
ALLOWED = {
    "admin": {"management-read", "management-write"},
    "server": {"evaluate"},
    "sdk": {"config"},
}
ENDPOINTS = ["management-read", "management-write", "evaluate", "config"]


@pytest.mark.parametrize("kind", ["admin", "server", "sdk"])
@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_key_scope(
    client: TestClient, api: Api, tokens: dict[str, str], kind: str, endpoint: str
) -> None:
    status = call(client, api, endpoint, tokens[kind])
    if endpoint in ALLOWED[kind]:
        assert 200 <= status < 300
    else:
        assert status == 403


def test_sdk_key_cannot_write(client: TestClient, api: Api, tokens: dict[str, str]) -> None:
    response = client.post(
        "/v1/projects/shop/flags",
        json={"key": "x", "type": "boolean"},
        headers=bearer(tokens["sdk"]),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "this endpoint needs the admin key, not an SDK key"
    assert client.get("/v1/projects/shop/flags/x", headers=bearer(ADMIN_KEY)).status_code == 404


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_missing_or_invalid_credentials_are_401(
    client: TestClient, api: Api, tokens: dict[str, str], endpoint: str
) -> None:
    sdk = tokens["sdk"]
    tampered = sdk[:-1] + ("A" if sdk[-1] != "A" else "B")
    wrong_prefix = "srv" + sdk[3:]
    for token in (None, "", "nonsense", ADMIN_KEY + "x", tampered, wrong_prefix):
        assert call(client, api, endpoint, token) == 401, token


def test_401_names_the_scheme(client: TestClient) -> None:
    response = client.get("/v1/projects")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_revoked_key_stops_working(client: TestClient, api: Api, tokens: dict[str, str]) -> None:
    key_id = tokens["server"].split(".")[2]
    assert call(client, api, "evaluate", tokens["server"]) == 200
    client.delete(
        f"/v1/projects/shop/environments/production/keys/{key_id}", headers=bearer(ADMIN_KEY)
    )
    assert call(client, api, "evaluate", tokens["server"]) == 401


def test_sdk_key_only_reads_its_own_environment(
    client: TestClient, api: Api, tokens: dict[str, str]
) -> None:
    staging = api.environment_id("staging")
    response = client.get(f"/v1/environments/{staging}/config", headers=bearer(tokens["sdk"]))
    assert response.status_code == 403
    assert response.json()["detail"] == "this key is for another environment"


def test_only_hashes_are_stored(settings: Settings, tokens: dict[str, str]) -> None:
    with closing(sqlite3.connect(settings.database_path)) as conn:
        stored = conn.execute("SELECT id, token_sha256 FROM api_keys").fetchall()
        dump = "\n".join(conn.iterdump())
    assert len(stored) == 2
    for kind in ("server", "sdk"):
        secret = tokens[kind].rsplit(".", 1)[1]
        assert secret not in dump
        assert (tokens[kind].split(".")[2], sha256_hex(tokens[kind])) in stored


def test_token_names_its_environment(api: Api, tokens: dict[str, str]) -> None:
    assert environment_of_token(tokens["sdk"]) == api.environment_id()
    assert environment_of_token(tokens["sdk"], "sdk") == api.environment_id()
    assert environment_of_token(tokens["sdk"], "server") is None
    assert environment_of_token("sdk.nope") is None


def test_short_admin_key_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        Settings(database_path=Path("flags.db"), admin_key="short")


def test_settings_from_environment() -> None:
    with pytest.raises(ValueError, match="FLAGS_ADMIN_KEY"):
        Settings.from_env({})
    settings = Settings.from_env({"FLAGS_ADMIN_KEY": ADMIN_KEY, "FLAGS_DATABASE": "/data/f.db"})
    assert str(settings.database_path) == "/data/f.db"
    assert str(Settings.from_env({"FLAGS_ADMIN_KEY": ADMIN_KEY}).database_path) == "flags.db"
