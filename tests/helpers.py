"""Test helpers: flag builders and a chi-square test with no third-party dependency."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from fastapi.testclient import TestClient
from httpx2 import Response

from flagservice.core.model import FlagConfig


def make_flag(
    *,
    key: str = "checkout",
    flag_type: str = "boolean",
    salt: str = "a1b2c3d4",
    variants: Sequence[tuple[str, Any]] = (("off", False), ("on", True)),
    enabled: bool = True,
    default_variant: str = "off",
    rules: Sequence[dict[str, Any]] = (),
) -> FlagConfig:
    return FlagConfig.model_validate(
        {
            "key": key,
            "type": flag_type,
            "salt": salt,
            "version": 1,
            "variants": [{"key": k, "value": v} for k, v in variants],
            "enabled": enabled,
            "default_variant": default_variant,
            "rules": list(rules),
        }
    )


def split_rule(*weights: tuple[str, float], bucket_by: str | None = None) -> dict[str, Any]:
    rule: dict[str, Any] = {"split": [{"variant": v, "weight": w} for v, w in weights]}
    if bucket_by is not None:
        rule["bucket_by"] = bucket_by
    return rule


def chi_square_statistic(observed: Sequence[int], expected: Sequence[float]) -> float:
    return sum((o - e) ** 2 / e for o, e in zip(observed, expected, strict=True))


def chi_square_sf(statistic: float, dof: int) -> float:
    """P(X >= statistic) for X ~ chi-square with an integer number of degrees of freedom.

    Uses the closed forms of the regularized upper incomplete gamma function
    Q(dof / 2, statistic / 2) for integer and half-integer shape parameters.
    """
    if dof < 1:
        raise ValueError("dof must be at least 1")
    if statistic <= 0:
        return 1.0
    half = statistic / 2

    def term(power: float) -> float:
        # half**power * exp(-half) / Gamma(power + 1), computed in log space.
        return math.exp(power * math.log(half) - half - math.lgamma(power + 1))

    if dof % 2 == 0:
        return min(1.0, sum(term(i) for i in range(dof // 2)))
    tail = math.erfc(math.sqrt(half))
    return min(1.0, tail + sum(term(i - 0.5) for i in range(1, (dof - 1) // 2 + 1)))


# HTTP helpers

ADMIN_KEY = "admin-key-for-tests-0123456789abcdef"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


ADMIN = bearer(ADMIN_KEY)


class Api:
    """Thin wrappers over the management API that fail loudly on unexpected statuses."""

    def __init__(self, client: TestClient) -> None:
        self.client = client

    def post(self, path: str, body: Any, expect: int = 201) -> Any:
        response = self.client.post(path, json=body, headers=ADMIN)
        assert response.status_code == expect, response.text
        return response.json()

    def project(
        self, key: str = "shop", environments: Sequence[str] = ("staging", "production")
    ) -> None:
        self.post("/v1/projects", {"key": key, "name": key.title()})
        for environment in environments:
            self.post(f"/v1/projects/{key}/environments", {"key": environment, "name": environment})

    def key(self, kind: str, environment: str = "production", project: str = "shop") -> str:
        body = self.post(f"/v1/projects/{project}/environments/{environment}/keys", {"kind": kind})
        token: str = body["token"]
        return token

    def environment_id(self, environment: str = "production", project: str = "shop") -> str:
        response = self.client.get(
            f"/v1/projects/{project}/environments/{environment}", headers=ADMIN
        )
        assert response.status_code == 200, response.text
        public_id: str = response.json()["id"]
        return public_id

    def flag(self, body: dict[str, Any], project: str = "shop") -> tuple[dict[str, Any], str]:
        response = self.client.post(f"/v1/projects/{project}/flags", json=body, headers=ADMIN)
        assert response.status_code == 201, response.text
        return response.json(), response.headers["ETag"]

    def get_flag(self, key: str, project: str = "shop") -> tuple[dict[str, Any], str]:
        response = self.client.get(f"/v1/projects/{project}/flags/{key}", headers=ADMIN)
        assert response.status_code == 200, response.text
        return response.json(), response.headers["ETag"]

    def put_settings(
        self,
        flag: str,
        environment: str,
        settings: dict[str, Any],
        etag: str | None,
        project: str = "shop",
    ) -> Response:
        headers = dict(ADMIN)
        if etag is not None:
            headers["If-Match"] = etag
        return self.client.put(
            f"/v1/projects/{project}/flags/{flag}/environments/{environment}",
            json=settings,
            headers=headers,
        )

    def set_settings(
        self, flag: str, environment: str, settings: dict[str, Any], project: str = "shop"
    ) -> str:
        """Replace settings using the current ETag; returns the new ETag."""
        _, etag = self.get_flag(flag, project)
        response = self.put_settings(flag, environment, settings, etag, project)
        assert response.status_code == 200, response.text
        return response.headers["ETag"]
