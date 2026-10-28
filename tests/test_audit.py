"""The audit log: what is recorded, pagination, and atomicity with the change."""

from __future__ import annotations

from contextlib import closing
from typing import Any

import pytest
from fastapi.testclient import TestClient

from flagservice.core.model import EnvironmentSettings
from flagservice.server.db import connect
from flagservice.server.settings import Settings
from flagservice.server.store import Store
from helpers import ADMIN, Api


def audit(client: TestClient, query: str = "") -> dict[str, Any]:
    response = client.get(f"/v1/projects/shop/audit{query}", headers=ADMIN)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_changes_are_recorded_newest_first(client: TestClient, api: Api) -> None:
    api.project()
    api.flag({"key": "beta", "type": "boolean"})
    api.set_settings("beta", "production", {"enabled": True, "default_variant": "on"})
    api.key("sdk")
    actions = [(e["action"], e["flag"], e["environment"]) for e in audit(client)["items"]]
    assert actions == [
        ("api_key.created", None, "production"),
        ("flag.environment_updated", "beta", "production"),
        ("flag.created", "beta", None),
        ("environment.created", None, "production"),
        ("environment.created", None, "staging"),
        ("project.created", None, None),
    ]


def test_entries_hold_before_and_after(client: TestClient, api: Api) -> None:
    api.project()
    created, _ = api.flag({"key": "beta", "type": "boolean"})
    api.set_settings("beta", "production", {"enabled": True, "default_variant": "on"})
    latest, _ = api.get_flag("beta")
    entry = audit(client, "?flag=beta")["items"][0]
    assert entry["actor"] == "admin"
    assert entry["at"].endswith("Z")
    assert entry["before"] == created
    assert entry["after"] == latest
    assert entry["before"]["environments"]["production"]["enabled"] is False
    assert entry["after"]["environments"]["production"]["enabled"] is True


def test_key_creation_is_recorded_without_the_secret(client: TestClient, api: Api) -> None:
    api.project()
    token = api.key("server")
    entry = audit(client)["items"][0]
    assert entry["action"] == "api_key.created"
    assert entry["after"]["id"] == token.split(".")[2]
    assert "token" not in entry["after"]
    assert token not in str(entry)


def test_deletion_keeps_the_last_state(client: TestClient, api: Api) -> None:
    api.project()
    flag, etag = api.flag({"key": "beta", "type": "boolean"})
    client.delete("/v1/projects/shop/flags/beta", headers={**ADMIN, "If-Match": etag})
    entry = audit(client, "?flag=beta")["items"][0]
    assert (entry["action"], entry["before"], entry["after"]) == ("flag.deleted", flag, None)


def test_pagination_walks_every_entry_once(client: TestClient, api: Api) -> None:
    api.project(environments=("production",))
    api.flag({"key": "beta", "type": "boolean"})
    for i in range(23):
        api.set_settings("beta", "production", {"enabled": i % 2 == 0, "default_variant": "off"})
    everything = audit(client, "?limit=200")
    assert everything["next_cursor"] is None
    total = len(everything["items"])
    assert total == 1 + 1 + 1 + 23  # project, environment, flag, updates

    seen: list[int] = []
    cursor = None
    pages = 0
    while True:
        query = "?limit=10" + (f"&cursor={cursor}" if cursor else "")
        page = audit(client, query)
        seen += [entry["id"] for entry in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert seen == [entry["id"] for entry in everything["items"]]
    assert seen == sorted(seen, reverse=True)


def test_flag_filter(client: TestClient, api: Api) -> None:
    api.project()
    api.flag({"key": "a", "type": "boolean"})
    api.flag({"key": "b", "type": "boolean"})
    api.set_settings("b", "production", {"default_variant": "on"})
    assert [e["action"] for e in audit(client, "?flag=b")["items"]] == [
        "flag.environment_updated",
        "flag.created",
    ]
    page = audit(client, "?flag=b&limit=1")
    assert len(page["items"]) == 1
    rest = audit(client, f"?flag=b&cursor={page['next_cursor']}")
    assert [e["action"] for e in rest["items"]] == ["flag.created"]


@pytest.mark.parametrize("cursor", ["garbage!", "YWJj", "YXVkaXQ6eA", "YXVkaXQ6wrI"])
def test_invalid_cursor_is_400(client: TestClient, api: Api, cursor: str) -> None:
    api.project()
    response = client.get(f"/v1/projects/shop/audit?cursor={cursor}", headers=ADMIN)
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid cursor"}


@pytest.mark.parametrize("limit", [0, 201, -1])
def test_limit_is_bounded(client: TestClient, api: Api, limit: int) -> None:
    api.project()
    response = client.get(f"/v1/projects/shop/audit?limit={limit}", headers=ADMIN)
    assert response.status_code == 422


def test_refused_changes_leave_no_entry(client: TestClient, api: Api) -> None:
    api.project()
    _, etag = api.flag({"key": "beta", "type": "boolean"})
    api.set_settings("beta", "production", {"default_variant": "on"})
    before = audit(client)["items"]
    api.put_settings("beta", "production", {"default_variant": "on"}, etag)  # 412
    api.put_settings("beta", "production", {"default_variant": "x"}, api.get_flag("beta")[1])
    api.post("/v1/projects/shop/flags", {"key": "beta", "type": "boolean"}, expect=409)
    assert audit(client)["items"] == before


def test_change_and_audit_entry_commit_together(
    settings: Settings, api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.project()
    api.flag({"key": "beta", "type": "boolean"})
    _, etag = api.get_flag("beta")

    def failing_audit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("disk full")

    with closing(connect(settings.database_path)) as conn:
        store = Store(conn)
        monkeypatch.setattr(store, "_audit", failing_audit)
        with pytest.raises(RuntimeError, match="disk full"):
            store.update_flag_environment(
                "shop",
                "beta",
                "production",
                EnvironmentSettings(enabled=True, default_variant="on"),
                [(False, etag)],
                "admin",
            )
    flag, same_etag = api.get_flag("beta")
    assert same_etag == etag
    assert flag["environments"]["production"]["enabled"] is False
