"""The SDK client against a scripted server (httpx.MockTransport)."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from flagservice.core.evaluate import Reason
from flagservice.sdk import FlagClient
from helpers import make_flag

ENV = "env_0123456789ab"
SDK_KEY = f"sdk.{ENV}.key_0123456789ab.{'s' * 43}"
URL = f"http://flags.test/v1/environments/{ENV}/config"


def config(**flags: Any) -> dict[str, Any]:
    return {
        "environment": {"id": ENV, "key": "production", "project": "shop"},
        "flags": {key: flag.model_dump(mode="json") for key, flag in flags.items()},
    }


DEFAULT_CONFIG = config(
    beta=make_flag(key="beta", default_variant="on"),
    color=make_flag(
        key="color",
        flag_type="string",
        variants=[("blue", "#00f")],
        default_variant="blue",
    ),
    limit=make_flag(key="limit", flag_type="number", variants=[("v", 25)], default_variant="v"),
    layout=make_flag(
        key="layout",
        flag_type="json",
        variants=[("v", {"columns": [1, 2]})],
        default_variant="v",
    ),
    dark=make_flag(key="dark", enabled=False),
)


class FakeServer:
    """Serves a config with an ETag and records what the client sent."""

    def __init__(self) -> None:
        self.body: dict[str, Any] = DEFAULT_CONFIG
        self.etag = '"v1"'
        self.requests: list[httpx.Request] = []
        self.failure: Callable[[], httpx.Response] | None = None
        self.seen = threading.Condition()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self.seen:
            self.requests.append(request)
            self.seen.notify_all()
        if self.failure is not None:
            return self.failure()
        assert request.url == URL
        assert request.headers["Authorization"] == f"Bearer {SDK_KEY}"
        if request.headers.get("If-None-Match") == self.etag:
            return httpx.Response(304, headers={"ETag": self.etag})
        return httpx.Response(200, json=self.body, headers={"ETag": self.etag})

    def publish(self, body: dict[str, Any], etag: str) -> None:
        self.body, self.etag = body, etag

    def wait_for_requests(self, count: int, timeout: float = 5.0) -> bool:
        with self.seen:
            return self.seen.wait_for(lambda: len(self.requests) >= count, timeout)


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
def make_client(server: FakeServer) -> Iterator[Callable[..., FlagClient]]:
    clients: list[FlagClient] = []
    transports: list[httpx.Client] = []

    def factory(**kwargs: Any) -> FlagClient:
        http = httpx.Client(transport=httpx.MockTransport(server))
        transports.append(http)
        kwargs.setdefault("start", False)
        client = FlagClient("http://flags.test/", SDK_KEY, http_client=http, **kwargs)
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.close()
    for http in transports:
        http.close()


def test_refresh_uses_the_etag(server: FakeServer, make_client: Callable[..., FlagClient]) -> None:
    client = make_client()
    assert client.refresh()
    assert client.etag == '"v1"'
    assert client.refresh()
    assert "If-None-Match" not in server.requests[0].headers
    assert server.requests[1].headers["If-None-Match"] == '"v1"'
    assert client.bool_value("beta", {}, False) is True

    server.publish(config(beta=make_flag(key="beta", default_variant="off")), '"v2"')
    assert client.refresh()
    assert client.etag == '"v2"'
    assert client.bool_value("beta", {}, True) is False


def failure_modes() -> list[Callable[[], httpx.Response]]:
    def network() -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    def timeout() -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    return [
        network,
        timeout,
        lambda: httpx.Response(500, text="oops"),
        lambda: httpx.Response(401, json={"detail": "missing or invalid API key"}),
        lambda: httpx.Response(200, text="<html>not json</html>"),
        lambda: httpx.Response(200, json={"flags": "wrong"}),
    ]


@pytest.mark.parametrize("failure", failure_modes())
def test_last_good_config_survives_failures(
    server: FakeServer,
    make_client: Callable[..., FlagClient],
    failure: Callable[[], httpx.Response],
) -> None:
    client = make_client()
    assert client.refresh()
    server.failure = failure
    assert client.refresh() is False
    assert client.string_value("color", {}, "default") == "#00f"
    assert client.etag == '"v1"'
    server.failure = None
    assert client.refresh()


def test_failures_are_logged_once_until_recovery(
    server: FakeServer, make_client: Callable[..., FlagClient], caplog: pytest.LogCaptureFixture
) -> None:
    client = make_client()
    server.failure = lambda: httpx.Response(503)
    with caplog.at_level(logging.DEBUG, logger="flagservice.sdk"):
        for _ in range(3):
            client.refresh()
        server.failure = None
        client.refresh()
    levels = [record.levelname for record in caplog.records]
    assert levels == ["WARNING", "DEBUG", "DEBUG", "INFO"]
    assert "HTTP 503" in caplog.records[0].getMessage()


def test_defaults_before_the_first_config(
    server: FakeServer, make_client: Callable[..., FlagClient]
) -> None:
    server.failure = lambda: httpx.Response(500)
    client = make_client()
    assert client.refresh() is False
    assert not client.ready
    assert client.wait_until_ready(timeout=0.01) is False
    assert client.bool_value("beta", {}, False) is False
    result = client.evaluate("beta")
    assert result.reason is Reason.ERROR
    assert result.error == "no flag config has been loaded yet"


def test_typed_getters(make_client: Callable[..., FlagClient]) -> None:
    client = make_client()
    client.refresh()
    assert client.bool_value("beta", {}, False) is True
    assert client.string_value("color", None, "x") == "#00f"
    limit = client.number_value("limit", {}, 0.0)
    assert limit == 25.0
    assert isinstance(limit, float)
    assert client.json_value("layout", {}, None) == {"columns": [1, 2]}
    # Missing, disabled and wrongly typed flags give the caller's default.
    assert client.bool_value("missing", {}, True) is True
    assert client.bool_value("dark", {}, True) is True
    assert client.evaluate("dark").reason is Reason.DISABLED
    assert client.string_value("beta", {}, "fallback") == "fallback"
    assert client.number_value("color", {}, 7.5) == 7.5
    assert client.json_value("beta", {}, {"d": 1}) == {"d": 1}


def test_json_values_are_copies(make_client: Callable[..., FlagClient]) -> None:
    client = make_client()
    client.refresh()
    value = client.json_value("layout", {}, None)
    assert isinstance(value, dict)
    value["columns"] = "mutated"
    assert client.json_value("layout", {}, None) == {"columns": [1, 2]}


def test_getters_never_raise(make_client: Callable[..., FlagClient]) -> None:
    client = make_client()
    client.refresh()
    bad_contexts: list[Any] = ["user-1", 42, ["key"], object()]
    for context in bad_contexts:
        assert client.bool_value("beta", context, False) is False
        assert client.evaluate("beta", context).reason is Reason.ERROR
    assert client.bool_value(None, {}, False) is False  # type: ignore[arg-type]


def test_getters_survive_an_internal_error(
    make_client: Callable[..., FlagClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client()
    client.refresh()

    def explode(*args: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr("flagservice.sdk.client.evaluate", explode)
    assert client.bool_value("beta", {}, False) is False
    assert client.json_value("layout", {}, "fallback") == "fallback"


def test_unreadable_flag_falls_back_but_others_work(
    server: FakeServer, make_client: Callable[..., FlagClient]
) -> None:
    body = json.loads(json.dumps(DEFAULT_CONFIG))
    body["flags"]["beta"]["rules"] = [
        {"conditions": [{"attribute": "a", "operator": "regex", "value": "."}], "variant": "on"}
    ]
    server.publish(body, '"v9"')
    client = make_client()
    assert client.refresh()
    assert client.bool_value("beta", {}, False) is False
    assert client.evaluate("beta").reason is Reason.ERROR
    assert client.string_value("color", {}, "x") == "#00f"


def test_background_polling_picks_up_changes(
    server: FakeServer, make_client: Callable[..., FlagClient]
) -> None:
    client = make_client(start=True, poll_interval=0.01)
    assert client.wait_until_ready(timeout=5)
    assert client.bool_value("beta", {}, False) is True
    server.publish(config(beta=make_flag(key="beta", default_variant="off")), '"v2"')
    seen = len(server.requests)
    assert server.wait_for_requests(seen + 2)
    assert client.bool_value("beta", {}, True) is False
    # Unchanged polls were conditional and answered with 304.
    assert all("If-None-Match" in r.headers for r in server.requests[1:])


def test_close_stops_polling(server: FakeServer, make_client: Callable[..., FlagClient]) -> None:
    client = make_client(start=True, poll_interval=0.01)
    assert server.wait_for_requests(3)
    client.close()
    count = len(server.requests)
    assert not server.wait_for_requests(count + 1, timeout=0.1)
    assert client.bool_value("beta", {}, False) is True  # still serves the last config


def test_close_releases_an_http_client_it_created() -> None:
    # No request is sent: the client is closed before refresh() is called.
    with FlagClient("http://flags.test", SDK_KEY, start=False) as client:
        pass
    assert client.refresh() is False  # a closed transport is reported, not raised


def test_close_leaves_a_supplied_http_client_open(server: FakeServer) -> None:
    with httpx.Client(transport=httpx.MockTransport(server)) as http:
        FlagClient("http://flags.test", SDK_KEY, http_client=http, start=False).close()
        assert not http.is_closed


@pytest.mark.parametrize(
    "key",
    ["", "secret", SDK_KEY.replace("sdk.", "srv.", 1), SDK_KEY + "x"],
)
def test_constructor_rejects_keys_that_are_not_sdk_keys(key: str) -> None:
    with pytest.raises(ValueError, match="not an SDK key"):
        FlagClient("http://flags.test", key, start=False)


def test_constructor_rejects_a_bad_interval() -> None:
    with pytest.raises(ValueError, match="poll_interval"):
        FlagClient("http://flags.test", SDK_KEY, poll_interval=0, start=False)
