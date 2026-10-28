"""Server-side and SDK evaluation agree on thousands of random contexts.

Both paths call the same core function, but they reach it differently: the
server builds a flag's config from database rows, while the SDK parses the
published JSON document. This test checks that the two paths see the same
flag on every kind of rule the API accepts.

Every context goes through the server's evaluation function with the request
and response serialised as JSON, exactly as the endpoint does; a sample also
goes through ``POST /v1/evaluate`` itself, which costs a few milliseconds per
call under the test client.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from contextlib import closing
from typing import Any

import httpx
from fastapi.testclient import TestClient

from flagservice.core.evaluate import EvaluationResult
from flagservice.sdk import FlagClient
from flagservice.server import schemas
from flagservice.server.db import connect
from flagservice.server.routes.runtime import evaluate_in_environment
from flagservice.server.settings import Settings
from flagservice.server.store import Store
from helpers import Api, bearer

SEED = 20261028
FLAGS = 12
CONTEXTS = 3000
HTTP_SAMPLE = 300

COUNTRIES = ["DE", "FR", "US", "BR", "JP"]
PLANS = ["free", "pro", "team"]
VERSIONS = ["1.0.0", "1.2.0", "1.10.0", "2.0.0-rc.1", "2.0.0", "2.1.3+build.7"]
EMAILS = ["ana@shop.test", "bo@example.com", "cy@shop.test", "dee@mail.test"]


def random_condition(rng: random.Random) -> dict[str, Any]:
    family = rng.choice(["country", "plan", "age", "email", "version", "beta", "org"])
    if family == "country":
        operator = rng.choice(["eq", "neq", "in", "not_in"])
        if operator in ("in", "not_in"):
            value: Any = rng.sample(COUNTRIES, rng.randint(1, 3))
        else:
            value = rng.choice(COUNTRIES)
        return {"attribute": "country", "operator": operator, "value": value}
    if family == "plan":
        return {"attribute": "plan", "operator": "in", "value": rng.sample(PLANS, 2)}
    if family == "age":
        operator = rng.choice(["lt", "lte", "gt", "gte"])
        bound = rng.choice([18, 21, 30.5, 65])
        return {"attribute": "age", "operator": operator, "value": bound}
    if family == "email":
        operator = rng.choice(["contains", "starts_with"])
        value = "@shop.test" if operator == "contains" else rng.choice(["ana", "b", "dee@"])
        return {"attribute": "email", "operator": operator, "value": value}
    if family == "version":
        operator = rng.choice(["semver_eq", "semver_lt", "semver_lte", "semver_gt", "semver_gte"])
        return {"attribute": "app_version", "operator": operator, "value": rng.choice(VERSIONS)}
    if family == "beta":
        return {"attribute": "beta", "operator": "eq", "value": True}
    return {"attribute": "org", "operator": rng.choice(["eq", "neq"]), "value": rng.randint(1, 4)}


def random_split(rng: random.Random, variants: list[Any]) -> list[dict[str, Any]]:
    chosen = rng.sample(variants, rng.randint(1, len(variants)))
    cuts = sorted(rng.randint(0, 10000) for _ in range(len(chosen) - 1))
    points = [b - a for a, b in zip([0, *cuts], [*cuts, 10000], strict=True)]
    return [{"variant": v, "weight": p / 100} for v, p in zip(chosen, points, strict=True)]


def random_flag(rng: random.Random, index: int) -> tuple[dict[str, Any], dict[str, Any]]:
    flag_type = rng.choice(["boolean", "string", "number", "json"])
    if flag_type == "boolean":
        variants = [{"key": "off", "value": False}, {"key": "on", "value": True}]
    elif flag_type == "string":
        variants = [{"key": f"s{i}", "value": f"text-{i}"} for i in range(rng.randint(2, 4))]
    elif flag_type == "number":
        variants = [{"key": f"n{i}", "value": rng.choice([i, i + 0.25])} for i in range(3)]
    else:
        variants = [{"key": f"j{i}", "value": {"tier": i, "tags": ["x"] * i}} for i in range(3)]
    keys = [variant["key"] for variant in variants]
    rules = []
    for _ in range(rng.randint(0, 4)):
        conditions = [random_condition(rng) for _ in range(rng.randint(0, 2))]
        if rng.random() < 0.5:
            rule: dict[str, Any] = {"conditions": conditions, "variant": rng.choice(keys)}
        else:
            rule = {"conditions": conditions, "split": random_split(rng, keys)}
            if rng.random() < 0.3:
                rule["bucket_by"] = "org"
        rules.append(rule)
    settings = {
        "enabled": rng.random() < 0.85,
        "default_variant": rng.choice(keys),
        "rules": rules,
    }
    return {"key": f"flag-{index}", "type": flag_type, "variants": variants}, settings


def random_context(rng: random.Random) -> dict[str, Any]:
    candidates: dict[str, Any] = {
        "key": rng.choice([f"user-{rng.randint(0, 10**6)}", rng.randint(0, 10**6), ""]),
        "country": rng.choice([*COUNTRIES, "de", 49]),
        "plan": rng.choice(PLANS),
        "age": rng.choice([rng.randint(10, 80), rng.uniform(10, 80), "30", True]),
        "email": rng.choice([*EMAILS, None]),
        "app_version": rng.choice([*VERSIONS, "2.0", "v1.0.0", 2]),
        "beta": rng.choice([True, False, 1, "true"]),
        "org": rng.choice([1, 2, 3, 4, "1", 2.0]),
    }
    return {name: value for name, value in candidates.items() if rng.random() < 0.8}


def as_tuple(result: EvaluationResult) -> tuple[Any, ...]:
    return (result.variant, result.value, str(result.reason), result.rule_index, result.error)


def response_tuple(body: dict[str, Any]) -> tuple[Any, ...]:
    return (body["variant"], body["value"], body["reason"], body["rule_index"], body["error"])


def test_server_and_sdk_agree(client: TestClient, api: Api, settings: Settings) -> None:
    rng = random.Random(SEED)
    api.project(environments=("production",))
    flag_keys = []
    for index in range(FLAGS):
        definition, flag_settings = random_flag(rng, index)
        api.flag(definition)
        api.set_settings(definition["key"], "production", flag_settings)
        flag_keys.append(definition["key"])
    server_key = api.key("server")
    environment_id = api.environment_id()

    def forward(request: httpx.Request) -> httpx.Response:
        # Hand the SDK's request to the app in-process and convert the response.
        response = client.request(request.method, request.url.path, headers=dict(request.headers))
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )

    with (
        closing(connect(settings.database_path)) as conn,
        httpx.Client(transport=httpx.MockTransport(forward)) as http,
        FlagClient("http://testserver", api.key("sdk"), http_client=http, start=False) as sdk,
    ):
        assert sdk.refresh()
        store = Store(conn)
        row = conn.execute("SELECT id FROM environments WHERE public_id = ?", (environment_id,))
        internal_id = row.fetchone()[0]
        reasons: Counter[str] = Counter()
        for number in range(CONTEXTS):
            flag_key = rng.choice([*flag_keys, "no-such-flag"])
            context = random_context(rng)
            local = as_tuple(sdk.evaluate(flag_key, context))

            wire = json.dumps({"flag": flag_key, "context": context})
            request = schemas.EvaluateRequest.model_validate_json(wire)
            answer = evaluate_in_environment(store, internal_id, request.flag, request.context)
            server = response_tuple(json.loads(answer.model_dump_json()))
            assert local == server, (flag_key, context)

            if number < HTTP_SAMPLE:
                response = client.post(
                    "/v1/evaluate",
                    json={"flag": flag_key, "context": context},
                    headers=bearer(server_key),
                )
                assert local == response_tuple(response.json()), (flag_key, context)
            reasons[local[2]] += 1

    # The sample exercised every way a flag can be served or not served.
    assert set(reasons) == {"TARGETING_MATCH", "SPLIT", "DEFAULT", "DISABLED", "FLAG_NOT_FOUND"}
