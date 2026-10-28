"""Projects, environments and API keys through the management API."""

from __future__ import annotations

from fastapi.testclient import TestClient

from helpers import ADMIN, Api


def test_project_crud(client: TestClient, api: Api) -> None:
    created = client.post("/v1/projects", json={"key": "shop", "name": "Shop"}, headers=ADMIN)
    assert created.status_code == 201
    assert created.headers["Location"] == "/v1/projects/shop"
    assert created.json()["name"] == "Shop"

    renamed = client.patch("/v1/projects/shop", json={"name": "Web shop"}, headers=ADMIN)
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Web shop"
    assert client.get("/v1/projects/shop", headers=ADMIN).json()["name"] == "Web shop"
    assert [p["key"] for p in client.get("/v1/projects", headers=ADMIN).json()] == ["shop"]

    assert client.delete("/v1/projects/shop", headers=ADMIN).status_code == 204
    assert client.get("/v1/projects/shop", headers=ADMIN).status_code == 404


def test_duplicate_project_is_a_conflict(api: Api) -> None:
    api.project(environments=())
    body = api.post("/v1/projects", {"key": "shop", "name": "Again"}, expect=409)
    assert body["detail"] == "project 'shop' already exists"


def test_project_with_content_cannot_be_deleted(client: TestClient, api: Api) -> None:
    api.project(environments=("production",))
    response = client.delete("/v1/projects/shop", headers=ADMIN)
    assert response.status_code == 409
    assert "delete those first" in response.json()["detail"]
    assert (
        client.delete("/v1/projects/shop/environments/production", headers=ADMIN).status_code == 204
    )
    assert client.delete("/v1/projects/shop", headers=ADMIN).status_code == 204


def test_keys_must_be_well_formed(client: TestClient) -> None:
    for key in ("Shop", "has space", "-leading", "a:b", "x" * 65, ""):
        response = client.post("/v1/projects", json={"key": key, "name": "n"}, headers=ADMIN)
        assert response.status_code == 422, key
        assert response.json()["detail"][0]["loc"] == ["body", "key"]


def test_unknown_fields_are_rejected(client: TestClient) -> None:
    response = client.post(
        "/v1/projects", json={"key": "shop", "name": "Shop", "owner": "me"}, headers=ADMIN
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "owner"]


def test_environment_crud(client: TestClient, api: Api) -> None:
    api.project(environments=())
    created = client.post(
        "/v1/projects/shop/environments", json={"key": "production", "name": "Prod"}, headers=ADMIN
    )
    assert created.status_code == 201
    environment = created.json()
    assert environment["id"].startswith("env_")
    assert environment["project"] == "shop"

    renamed = client.patch(
        "/v1/projects/shop/environments/production", json={"name": "Production"}, headers=ADMIN
    )
    assert renamed.json()["name"] == "Production"
    assert renamed.json()["id"] == environment["id"]
    listed = client.get("/v1/projects/shop/environments", headers=ADMIN).json()
    assert [e["key"] for e in listed] == ["production"]

    duplicate = client.post(
        "/v1/projects/shop/environments", json={"key": "production", "name": "x"}, headers=ADMIN
    )
    assert duplicate.status_code == 409
    assert client.get("/v1/projects/nope/environments", headers=ADMIN).status_code == 404


def test_new_environment_gets_settings_for_every_flag(client: TestClient, api: Api) -> None:
    api.project(environments=("production",))
    flag, etag = api.flag({"key": "beta", "type": "boolean"})
    assert flag["version"] == 1

    api.post("/v1/projects/shop/environments", {"key": "qa", "name": "QA"})
    updated, new_etag = api.get_flag("beta")
    assert updated["environments"]["qa"] == {
        "enabled": False,
        "default_variant": "off",
        "rules": [],
    }
    # The flag's representation changed, so its version did too.
    assert updated["version"] == 2
    assert new_etag != etag


def test_deleting_an_environment_removes_its_settings_and_keys(
    client: TestClient, api: Api
) -> None:
    api.project()
    api.flag({"key": "beta", "type": "boolean"})
    sdk_key = api.key("sdk", "staging")
    staging_id = api.environment_id("staging")

    assert client.delete("/v1/projects/shop/environments/staging", headers=ADMIN).status_code == 204
    flag, _ = api.get_flag("beta")
    assert set(flag["environments"]) == {"production"}
    assert flag["version"] == 2
    response = client.get(
        f"/v1/environments/{staging_id}/config", headers={"Authorization": f"Bearer {sdk_key}"}
    )
    assert response.status_code == 401


def test_api_keys_are_listed_without_secrets_and_can_be_revoked(
    client: TestClient, api: Api
) -> None:
    api.project()
    created = api.post(
        "/v1/projects/shop/environments/production/keys", {"kind": "server", "name": "backend"}
    )
    environment_id = api.environment_id()
    assert created["token"].startswith(f"srv.{environment_id}.{created['id']}.")
    assert created["kind"] == "server"
    assert created["revoked_at"] is None

    listed = client.get("/v1/projects/shop/environments/production/keys", headers=ADMIN).json()
    assert listed == [{k: v for k, v in created.items() if k != "token"}]

    path = f"/v1/projects/shop/environments/production/keys/{created['id']}"
    assert client.delete(path, headers=ADMIN).status_code == 204
    assert client.delete(path, headers=ADMIN).status_code == 204  # idempotent
    listed = client.get("/v1/projects/shop/environments/production/keys", headers=ADMIN).json()
    assert listed[0]["revoked_at"] is not None
    missing = "/v1/projects/shop/environments/production/keys/key_000000000000"
    assert client.delete(missing, headers=ADMIN).status_code == 404


def test_key_kind_is_validated(api: Api) -> None:
    api.project()
    body = api.post("/v1/projects/shop/environments/production/keys", {"kind": "admin"}, 422)
    assert body["detail"][0]["loc"] == ["body", "kind"]


def test_health(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_document_builds(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    assert "/v1/evaluate" in schema["paths"]
    assert {"AdminKey", "ServerKey", "SdkKey"} <= set(schema["components"]["securitySchemes"])
