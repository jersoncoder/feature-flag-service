"""A client that evaluates flags locally from a config it polls in the background.

The client downloads its environment's full ruleset from
``GET /v1/environments/{env}/config`` and evaluates flags in-process with the
same function the server uses, so a flag check costs no network round trip.
A background thread re-fetches the config every ``poll_interval`` seconds with
``If-None-Match``; when the server answers 304 nothing is downloaded. When a
fetch fails, the client keeps serving the last config it loaded.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import TypeVar

import httpx
from pydantic import JsonValue

from flagservice.core.evaluate import SERVED_REASONS, EvaluationResult, Reason, evaluate
from flagservice.core.model import (
    EnvironmentConfig,
    FlagConfig,
    FlagType,
    parse_environment_config,
)
from flagservice.tokens import environment_of_token

logger = logging.getLogger("flagservice.sdk")

T = TypeVar("T")

_NOT_LOADED = EvaluationResult(
    variant=None, value=None, reason=Reason.ERROR, error="no flag config has been loaded yet"
)


@dataclass(frozen=True)
class _Snapshot:
    config: EnvironmentConfig
    etag: str | None
    fetched_at: float


class FlagClient:
    """Local flag evaluation with background polling.

    ``base_url`` is the server's address, ``sdk_key`` an SDK key of the
    environment to follow (the key names its environment) and
    ``poll_interval`` the seconds between fetches. Pass ``start=False`` to poll
    only when ``refresh()`` is called, and ``http_client`` to supply a
    configured ``httpx.Client`` (the client does not close one it did not create).
    """

    def __init__(
        self,
        base_url: str,
        sdk_key: str,
        poll_interval: float = 30.0,
        *,
        timeout: float = 5.0,
        http_client: httpx.Client | None = None,
        start: bool = True,
    ) -> None:
        environment = environment_of_token(sdk_key, "sdk")
        if environment is None:
            raise ValueError("sdk_key is not an SDK key (it should start with 'sdk.env_')")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._url = f"{base_url.rstrip('/')}/v1/environments/{environment}/config"
        self._headers = {"Authorization": f"Bearer {sdk_key}", "Accept": "application/json"}
        self._poll_interval = poll_interval
        self._owns_http = http_client is None
        self._http = http_client if http_client is not None else httpx.Client(timeout=timeout)
        self._snapshot: _Snapshot | None = None
        self._refresh_lock = threading.Lock()
        self._loaded = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failing = False
        if start:
            self.start()

    # Lifecycle

    def start(self) -> None:
        """Start background polling; the first fetch happens immediately."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll, name="flagservice-poller", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Stop polling. Flags keep evaluating against the last config."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join()
        self._thread = None
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> FlagClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def wait_until_ready(self, timeout: float | None = None) -> bool:
        """Block until a config has been loaded; False if ``timeout`` passes first."""
        return self._loaded.wait(timeout)

    @property
    def ready(self) -> bool:
        return self._loaded.is_set()

    @property
    def etag(self) -> str | None:
        snapshot = self._snapshot
        return snapshot.etag if snapshot is not None else None

    # Fetching

    def refresh(self) -> bool:
        """Fetch the config once. True when the client holds a current config afterwards.

        Never raises: on any failure the previous config stays in place.
        """
        with self._refresh_lock:
            ok, problem = self._fetch()
        if ok and self._failing:
            logger.info("flag config fetch recovered")
        elif not ok and not self._failing:
            logger.warning("flag config fetch failed, keeping the last config: %s", problem)
        elif not ok:
            logger.debug("flag config fetch failed again: %s", problem)
        self._failing = not ok
        return ok

    def _fetch(self) -> tuple[bool, str]:
        snapshot = self._snapshot
        headers = dict(self._headers)
        if snapshot is not None and snapshot.etag is not None:
            headers["If-None-Match"] = snapshot.etag
        try:
            response = self._http.get(self._url, headers=headers)
        except Exception as exc:  # network errors, timeouts, a closed client
            return False, f"{type(exc).__name__}: {exc}"
        if response.status_code == 304 and snapshot is not None:
            return True, ""
        if response.status_code != 200:
            return False, f"HTTP {response.status_code}"
        try:
            config = parse_environment_config(response.json())
        except ValueError as exc:  # also covers invalid JSON
            return False, f"unreadable config: {exc}"
        self._snapshot = _Snapshot(config, response.headers.get("ETag"), time.monotonic())
        self._loaded.set()
        return True, ""

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # pragma: no cover - refresh() handles expected failures
                logger.exception("unexpected error while refreshing flag config")
            self._stop.wait(self._poll_interval)

    # Evaluation

    def evaluate(
        self, flag_key: str, context: Mapping[str, object] | None = None
    ) -> EvaluationResult:
        """Evaluate a flag with the reason; never raises."""
        snapshot = self._snapshot
        if snapshot is None:
            return _NOT_LOADED
        return evaluate(snapshot.config.flags.get(flag_key), {} if context is None else context)

    def bool_value(
        self, flag_key: str, context: Mapping[str, object] | None, default: bool
    ) -> bool:
        return self._typed(flag_key, context, default, "boolean", _as_bool)

    def string_value(
        self, flag_key: str, context: Mapping[str, object] | None, default: str
    ) -> str:
        return self._typed(flag_key, context, default, "string", _as_str)

    def number_value(
        self, flag_key: str, context: Mapping[str, object] | None, default: float
    ) -> float:
        return self._typed(flag_key, context, default, "number", _as_number)

    def json_value(
        self, flag_key: str, context: Mapping[str, object] | None, default: JsonValue
    ) -> JsonValue:
        # A deep copy, so a caller that mutates the result cannot change the cached config.
        return self._typed(flag_key, context, default, "json", copy.deepcopy)

    def _typed(
        self,
        flag_key: str,
        context: Mapping[str, object] | None,
        default: T,
        flag_type: FlagType,
        convert: Callable[[JsonValue], T],
    ) -> T:
        """The flag's value when it is served and has the expected type, else ``default``."""
        try:
            snapshot = self._snapshot
            flag = snapshot.config.flags.get(flag_key) if snapshot is not None else None
            if not isinstance(flag, FlagConfig):
                return default
            if flag.type != flag_type:
                logger.debug("flag %r is a %s flag, not %s", flag_key, flag.type, flag_type)
                return default
            result = evaluate(flag, {} if context is None else context)
            if result.reason not in SERVED_REASONS:
                return default
            return convert(result.value)
        except Exception:
            logger.exception("evaluating flag %r failed; returning the default", flag_key)
            return default


def _as_bool(value: JsonValue) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"expected a boolean, got {value!r}")
    return value


def _as_str(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected a string, got {value!r}")
    return value


def _as_number(value: JsonValue) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"expected a number, got {value!r}")
    return float(value)
