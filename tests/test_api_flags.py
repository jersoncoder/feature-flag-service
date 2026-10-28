"""Flags through the management API: lifecycle, validation and optimistic concurrency."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from helpers import ADMIN, Api

COLOR = {
    "key": "button-color",
    "type": "string",
    "description": "Colour of the checkout button",
    "variants": [
        {"key": "blue", "value": "#1d4ed8"},
        {"key": "green", "value": "#15803d"},
    ],
}


@pytest.fixture
def shop(api: Api) -> Api:
    api.project()
    return api


def test_boolean_flag_defaults(shop: Api) -> None:
    flag, etag = shop.flag({"key": "new-checkout", "type": "boolean"})
    assert flag["variants"] == [{"key": "off", "value": False}, {"key": "on", "value": True}]
    assert flag["version"] == 1
    assert len(flag["salt"]) == 16
    assert flag["environments"] == {
        env: {"enabled": False, "default_variant": "off", "rules": []}
        for env in ("production", "staging")
    }
    assert etag.startswith('"')
    assert etag.endswith('-1"')


def test_flag_lifecycle(client: TestClient, shop: Api) -> None:
    flag, etag = shop.flag(
        {
            **COLOR,
            "environments": {
                "staging": {
                    "enabled": True,
                    "default_variant": "blue",
                    "rules": [
                        {
                            "conditions": [
                                {
                                    "attribute": "email",
                                    "operator": "contains",
                                    "value": "@shop.test",
                                }
                            ],
                            "variant": "green",
                        }
                    ],
                }
            },
        }
    )
    assert flag["environments"]["staging"]["enabled"] is True
    assert flag["environments"]["production"]["enabled"] is False
    assert client.get("/v1/projects/shop/flags", headers=ADMIN).json() == [flag]

    # Toggle production on with a 10% rollout of green.
    settings = {
        "enabled": True,
        "default_variant": "blue",
        "rules": [
            {
                "conditions": [],
                "split": [{"variant": "green", "weight": 10}, {"variant": "blue", "weight": 90}],
                "bucket_by": "user_id",
            }
        ],
    }
    response = shop.put_settings("button-color", "production", settings, etag)
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 2
    assert response.json()["environments"]["production"] == settings
    etag = response.headers["ETag"]

    # Replace the variants: add one, keep the two in use.
    update = {
        "description": "Checkout button",
        "variants": [*COLOR["variants"], {"key": "red", "value": "#b91c1c"}],
    }
    response = client.put(
        "/v1/projects/shop/flags/button-color", json=update, headers={**ADMIN, "If-Match": etag}
    )
    assert response.status_code == 200, response.text
    assert response.json()["version"] == 3
    assert [v["key"] for v in response.json()["variants"]] == ["blue", "green", "red"]
    etag = response.headers["ETag"]

    response = client.delete(
        "/v1/projects/shop/flags/button-color", headers={**ADMIN, "If-Match": etag}
    )
    assert response.status_code == 204
    assert client.get("/v1/projects/shop/flags/button-color", headers=ADMIN).status_code == 404


def test_duplicate_flag_is_a_conflict(shop: Api) -> None:
    shop.flag({"key": "beta", "type": "boolean"})
    assert shop.post("/v1/projects/shop/flags", {"key": "beta", "type": "boolean"}, 409)


def problems(response: Any) -> list[tuple[list[Any], str]]:
    assert response.status_code == 422, response.text
    return [(item["loc"], item["msg"]) for item in response.json()["detail"]]


def test_rule_with_unknown_variant(shop: Api) -> None:
    shop.flag(COLOR)
    response = shop.put_settings(
        "button-color",
        "production",
        {"enabled": True, "default_variant": "blue", "rules": [{"variant": "purple"}]},
        shop.get_flag("button-color")[1],
    )
    assert problems(response) == [
        (
            ["body", "rules", 0, "variant"],
            "unknown variant 'purple'; the flag defines: blue, green",
        )
    ]


def test_split_weights_must_sum_to_100(shop: Api) -> None:
    shop.flag(COLOR)
    settings = {
        "default_variant": "blue",
        "rules": [
            {"split": [{"variant": "blue", "weight": 50}, {"variant": "green", "weight": 40}]}
        ],
    }
    response = shop.put_settings(
        "button-color", "production", settings, shop.get_flag("button-color")[1]
    )
    [(loc, message)] = problems(response)
    assert loc == ["body", "rules", 0]
    assert "split weights must sum to 100, got 90" in message


def test_split_with_unknown_variant(shop: Api) -> None:
    shop.flag(COLOR)
    settings = {
        "default_variant": "nope",
        "rules": [
            {"split": [{"variant": "blue", "weight": 50}, {"variant": "teal", "weight": 50}]}
        ],
    }
    response = shop.put_settings(
        "button-color", "production", settings, shop.get_flag("button-color")[1]
    )
    assert [loc for loc, _ in problems(response)] == [
        ["body", "default_variant"],
        ["body", "rules", 0, "split", 1, "variant"],
    ]


def test_duplicate_variant_keys(client: TestClient, shop: Api) -> None:
    body = {**COLOR, "variants": [*COLOR["variants"], {"key": "blue", "value": "#000"}]}
    response = client.post("/v1/projects/shop/flags", json=body, headers=ADMIN)
    assert problems(response) == [(["body", "variants", 2, "key"], "duplicate variant key 'blue'")]


@pytest.mark.parametrize(
    ("flag_type", "value", "accepted"),
    [
        ("boolean", True, True),
        ("boolean", "true", False),
        ("boolean", 1, False),
        ("string", "x", True),
        ("string", 5, False),
        ("number", 2.5, True),
        ("number", 3, True),
        ("number", True, False),
        ("number", "3", False),
        ("json", {"a": [1, None, "x"]}, True),
        ("json", None, True),
    ],
)
def test_variant_values_must_match_the_flag_type(
    client: TestClient, shop: Api, flag_type: str, value: Any, accepted: bool
) -> None:
    body = {"key": "typed", "type": flag_type, "variants": [{"key": "only", "value": value}]}
    response = client.post("/v1/projects/shop/flags", json=body, headers=ADMIN)
    if accepted:
        assert response.status_code == 201, response.text
        assert response.json()["variants"][0]["value"] == value
    else:
        [(loc, message)] = problems(response)
        assert loc == ["body", "variants", 0, "value"]
        assert f"a {flag_type} flag needs" in message


def test_json_values_must_be_finite(client: TestClient, shop: Api) -> None:
    content = '{"key": "cfg", "type": "json", "variants": [{"key": "v", "value": {"x": NaN}}]}'
    response = client.post(
        "/v1/projects/shop/flags",
        content=content,
        headers={**ADMIN, "Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_non_boolean_flags_need_variants(client: TestClient, shop: Api) -> None:
    response = client.post(
        "/v1/projects/shop/flags", json={"key": "s", "type": "string"}, headers=ADMIN
    )
    assert problems(response) == [(["body", "variants"], "a string flag needs variants")]


def test_settings_for_an_unknown_environment(client: TestClient, shop: Api) -> None:
    body = {**COLOR, "environments": {"prod": {"default_variant": "blue"}}}
    response = client.post("/v1/projects/shop/flags", json=body, headers=ADMIN)
    assert problems(response) == [
        (["body", "environments", "prod"], "project 'shop' has no environment 'prod'")
    ]


def test_variants_in_use_cannot_be_removed(client: TestClient, shop: Api) -> None:
    _, etag = shop.flag(COLOR)
    etag = shop.set_settings(
        "button-color", "staging", {"default_variant": "blue", "rules": [{"variant": "green"}]}
    )
    response = client.put(
        "/v1/projects/shop/flags/button-color",
        json={"variants": [COLOR["variants"][0]]},
        headers={**ADMIN, "If-Match": etag},
    )
    [(loc, message)] = problems(response)
    assert loc == ["body", "variants"]
    assert message.startswith("environment 'staging' still uses a variant this list removes")
    assert shop.get_flag("button-color")[0]["version"] == 2  # nothing changed


# Optimistic concurrency


def test_update_without_if_match_is_428(shop: Api) -> None:
    shop.flag(COLOR)
    response = shop.put_settings("button-color", "production", {"default_variant": "blue"}, None)
    assert response.status_code == 428
    assert "If-Match" in response.json()["detail"]


def test_if_match_star_is_refused(shop: Api) -> None:
    shop.flag(COLOR)
    response = shop.put_settings("button-color", "production", {"default_variant": "blue"}, "*")
    assert response.status_code == 428


def test_malformed_if_match_is_400(shop: Api) -> None:
    shop.flag(COLOR)
    response = shop.put_settings("button-color", "production", {"default_variant": "blue"}, "1-1")
    assert response.status_code == 400


def test_second_writer_with_the_same_etag_gets_412(shop: Api) -> None:
    _, etag = shop.flag(COLOR)
    first = shop.put_settings(
        "button-color", "production", {"enabled": True, "default_variant": "green"}, etag
    )
    assert first.status_code == 200
    second = shop.put_settings(
        "button-color", "production", {"enabled": False, "default_variant": "blue"}, etag
    )
    assert second.status_code == 412
    assert second.json() == {
        "detail": "the flag has changed since it was read",
        "current_version": 2,
        "current_etag": first.headers["ETag"],
    }
    assert second.headers["ETag"] == first.headers["ETag"]
    # The first write stands.
    assert (
        shop.get_flag("button-color")[0]["environments"]["production"]["default_variant"] == "green"
    )


def test_weak_etags_never_satisfy_if_match(shop: Api) -> None:
    _, etag = shop.flag(COLOR)
    response = shop.put_settings(
        "button-color", "production", {"default_variant": "blue"}, f"W/{etag}"
    )
    assert response.status_code == 412


def test_if_match_accepts_a_list(shop: Api) -> None:
    _, etag = shop.flag(COLOR)
    response = shop.put_settings(
        "button-color", "production", {"default_variant": "blue"}, f'"0-0", {etag}'
    )
    assert response.status_code == 200


def test_etag_of_a_deleted_flag_does_not_match_its_replacement(
    client: TestClient, shop: Api
) -> None:
    _, old_etag = shop.flag(COLOR)
    client.delete("/v1/projects/shop/flags/button-color", headers={**ADMIN, "If-Match": old_etag})
    _, new_etag = shop.flag(COLOR)
    assert new_etag != old_etag
    response = shop.put_settings(
        "button-color", "production", {"default_variant": "blue"}, old_etag
    )
    assert response.status_code == 412


def test_delete_needs_the_current_etag(client: TestClient, shop: Api) -> None:
    _, etag = shop.flag(COLOR)
    shop.set_settings("button-color", "production", {"default_variant": "green"})
    assert client.delete("/v1/projects/shop/flags/button-color", headers=ADMIN).status_code == 428
    response = client.delete(
        "/v1/projects/shop/flags/button-color", headers={**ADMIN, "If-Match": etag}
    )
    assert response.status_code == 412


def test_precondition_is_checked_before_validation(shop: Api) -> None:
    _, etag = shop.flag(COLOR)
    shop.set_settings("button-color", "production", {"default_variant": "green"})
    response = shop.put_settings("button-color", "production", {"default_variant": "purple"}, etag)
    assert response.status_code == 412


def test_missing_resources_are_404(shop: Api) -> None:
    _, etag = shop.flag(COLOR)
    assert (
        shop.put_settings("button-color", "qa", {"default_variant": "blue"}, etag).status_code
        == 404
    )
    assert (
        shop.put_settings("nope", "production", {"default_variant": "blue"}, etag).status_code
        == 404
    )
    assert shop.client.get("/v1/projects/nope/flags", headers=ADMIN).status_code == 404
