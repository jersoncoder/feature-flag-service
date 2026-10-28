"""Request and response bodies of the HTTP API."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from flagservice.core.evaluate import Reason
from flagservice.core.model import (
    MAX_VARIANTS,
    EnvironmentSettings,
    FlagType,
    ResourceKey,
    Variant,
)
from flagservice.server.auth import KeyKind

Name = Annotated[str, Field(min_length=1, max_length=200)]
Description = Annotated[str, Field(max_length=2000)]
Variants = Annotated[list[Variant], Field(min_length=1, max_length=MAX_VARIANTS)]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(_Request):
    key: ResourceKey
    name: Name


class ProjectUpdate(_Request):
    name: Name


class Project(BaseModel):
    key: str
    name: str
    created_at: str
    updated_at: str


class EnvironmentCreate(_Request):
    key: ResourceKey
    name: Name


class EnvironmentUpdate(_Request):
    name: Name


class Environment(BaseModel):
    id: str
    """Public id, used in ``/v1/environments/{id}/config`` and embedded in its API keys."""
    project: str
    key: str
    name: str
    created_at: str
    updated_at: str


class ApiKeyCreate(_Request):
    kind: KeyKind
    name: Annotated[str, Field(max_length=200)] = ""


class ApiKey(BaseModel):
    id: str
    kind: KeyKind
    name: str
    environment: str
    created_at: str
    revoked_at: str | None


class ApiKeyCreated(ApiKey):
    token: str
    """The secret. Returned only in this response; the server keeps only its hash."""


class FlagCreate(_Request):
    key: ResourceKey
    type: FlagType
    description: Description = ""
    variants: Variants | None = None
    """Required except for boolean flags, which default to ``off`` (false) and ``on`` (true)."""
    environments: dict[str, EnvironmentSettings] = {}
    """Settings per environment key. Environments left out start disabled, serving
    the first variant, with no rules."""


class FlagUpdate(_Request):
    description: Description = ""
    variants: Variants


class Flag(BaseModel):
    key: str
    type: FlagType
    description: str
    salt: str
    version: int
    variants: list[Variant]
    environments: dict[str, EnvironmentSettings]
    created_at: str
    updated_at: str


class EvaluateRequest(_Request):
    flag: Annotated[str, Field(min_length=1, max_length=64)]
    context: dict[str, JsonValue] = {}


class EvaluateResponse(BaseModel):
    flag: str
    variant: str | None
    value: JsonValue
    reason: Reason
    rule_index: int | None
    error: str | None


class AuditEntry(BaseModel):
    id: int
    at: str
    actor: str
    action: str
    flag: str | None
    environment: str | None
    before: JsonValue
    after: JsonValue


class AuditPage(BaseModel):
    items: list[AuditEntry]
    next_cursor: str | None
    """Pass as ``cursor`` to get the next (older) page; null on the last page."""


class VersionConflict(BaseModel):
    detail: str
    current_version: int
    current_etag: str
