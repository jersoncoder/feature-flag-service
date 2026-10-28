"""Server-side evaluation and the published config."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from helpers import Api, bearer

GERMANY_ONLY = {
    "enabled": True,
    "default_variant": "off",
    "rules": [
        {
            "conditions": [{"attribute": "country", "operator": "eq", "value": "DE"}],
            "variant": "on",
        },
        {"split": [{"variant": "on", "weight": 50}, {"variant": "off", "weight": 50}]},
    ],
}


@pytest.fixture
def setup(api: Api) -> dict[str, str]:
    api.project()
    api.flag({"key": "beta", "type": "boolean"})
    api.set_settings("beta", "production", GERMANY_ONLY)
    return {
        "server": api.key("server"),
        "sdk": api.key("sdk"),
        "staging_server": api.key("server", "staging"),
        "env": api.environment_id(),
    }


def evaluate(client: TestClient, token: str, flag: str, context: Any) -> dict[str, Any]:
    response = client.post(
        "/v1/evaluate", json={"flag": flag, "context": context}, headers=bearer(token)
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_evaluate_reports_the_reason(client: TestClient, setup: dict[str, str]) -> None:
    server = setup["server"]
    assert evaluate(client, server, "beta", {"country": "DE"}) == {
        "flag": "beta",
        "variant": "on",
        "value": True,
        "reason": "TARGETING_MATCH",
        "rule_index": 0,
        "error": None,
    }
    split = evaluate(client, server, "beta", {"country": "FR", "key": "user-1"})
    assert (split["reason"], split["rule_index"]) == ("SPLIT", 1)
    assert evaluate(client, server, "beta", {"country": "FR"})["reason"] == "DEFAULT"
    missing = evaluate(client, server, "nope", {})
    assert (missing["reason"], missing["value"]) == ("FLAG_NOT_FOUND", None)


def test_evaluate_uses_the_keys_environment(client: TestClient, setup: dict[str, str]) -> None:
    # The flag is on in production and off in staging.
    assert evaluate(client, setup["server"], "beta", {"country": "DE"})["value"] is True
    staging = evaluate(client, setup["staging_server"], "beta", {"country": "DE"})
    assert (staging["reason"], staging["value"]) == ("DISABLED", None)


def test_evaluate_validates_the_request(client: TestClient, setup: dict[str, str]) -> None:
    bodies: list[dict[str, Any]] = [
        {"flag": "beta", "context": ["DE"]},
        {"context": {}},
        {"flag": ""},
    ]
    for body in bodies:
        response = client.post("/v1/evaluate", json=body, headers=bearer(setup["server"]))
        assert response.status_code == 422, body


def get_config(client: TestClient, setup: dict[str, str], etag: str | None = None) -> Any:
    headers = bearer(setup["sdk"])
    if etag is not None:
        headers["If-None-Match"] = etag
    return client.get(f"/v1/environments/{setup['env']}/config", headers=headers)


def test_config_contains_the_environment_ruleset(
    client: TestClient, api: Api, setup: dict[str, str]
) -> None:
    api.project("other", environments=("production",))
    api.flag({"key": "elsewhere", "type": "boolean"}, project="other")
    response = get_config(client, setup)
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Cache-Control"] == "no-cache"
    config = response.json()
    assert config["environment"] == {"id": setup["env"], "key": "production", "project": "shop"}
    assert list(config["flags"]) == ["beta"]
    flag, _ = api.get_flag("beta")
    published = config["flags"]["beta"]
    assert published["version"] == flag["version"]
    assert published["salt"] == flag["salt"]
    assert published["variants"] == flag["variants"]
    assert {k: published[k] for k in ("enabled", "default_variant", "rules")} == (
        flag["environments"]["production"]
    )


def test_config_honours_if_none_match(client: TestClient, api: Api, setup: dict[str, str]) -> None:
    first = get_config(client, setup)
    etag = first.headers["ETag"]
    assert get_config(client, setup).headers["ETag"] == etag  # deterministic body

    for header in (etag, f"W/{etag}", f'"other", {etag}', "*"):
        unchanged = get_config(client, setup, header)
        assert unchanged.status_code == 304, header
        assert unchanged.content == b""
        assert unchanged.headers["ETag"] == etag

    assert get_config(client, setup, '"stale"').status_code == 200
    assert get_config(client, setup, "not-a-tag").status_code == 200

    api.set_settings("beta", "production", {**GERMANY_ONLY, "enabled": False})
    changed = get_config(client, setup, etag)
    assert changed.status_code == 200
    assert changed.headers["ETag"] != etag
    assert changed.json()["flags"]["beta"]["enabled"] is False


def test_config_etag_follows_content(client: TestClient, api: Api, setup: dict[str, str]) -> None:
    etag = get_config(client, setup).headers["ETag"]
    # Another project's changes are not part of this environment's config.
    api.project("other", environments=("production",))
    api.flag({"key": "elsewhere", "type": "boolean"}, project="other")
    assert get_config(client, setup, etag).status_code == 304
    # A change to the flag in staging moves the flag's version, and the version is
    # published with the flag, so production SDKs fetch the config once more.
    api.set_settings("beta", "staging", {"enabled": True, "default_variant": "on"})
    refreshed = get_config(client, setup, etag)
    assert refreshed.status_code == 200
    assert refreshed.json()["flags"]["beta"]["enabled"] is True


def test_config_body_is_compact_sorted_json(client: TestClient, setup: dict[str, str]) -> None:
    body = get_config(client, setup).content
    assert body == json.dumps(json.loads(body), sort_keys=True, separators=(",", ":")).encode()
