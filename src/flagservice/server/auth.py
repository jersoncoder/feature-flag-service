"""API keys.

There are three kinds of credential, each accepted only by its own endpoints:

* the admin key, from ``FLAGS_ADMIN_KEY``, for the management API;
* server keys, for ``POST /v1/evaluate``;
* SDK keys, for ``GET /v1/environments/{env}/config``.

Server and SDK keys belong to one environment; their format is in
``flagservice.tokens``. Only the SHA-256 of the whole token is stored, and the
key id doubles as the audit actor. A slow password hash would add nothing here,
because the secret is random rather than chosen by a person.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Literal

from flagservice.tokens import KIND_BY_PREFIX, PREFIX, TOKEN, KeyKind

PrincipalKind = Literal["admin", "server", "sdk"]

ADMIN_ACTOR = "admin"


@dataclass(frozen=True)
class Principal:
    kind: PrincipalKind
    actor: str
    """Recorded in the audit log: ``admin`` or the key id."""
    environment_id: int | None = None
    environment_public_id: str | None = None


@dataclass(frozen=True)
class IssuedKey:
    key_id: str
    token: str
    token_sha256: str


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def new_public_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def issue_key(kind: KeyKind, environment_public_id: str) -> IssuedKey:
    key_id = new_public_id("key")
    token = f"{PREFIX[kind]}.{environment_public_id}.{key_id}.{secrets.token_urlsafe(32)}"
    return IssuedKey(key_id=key_id, token=token, token_sha256=sha256_hex(token))


def authenticate(conn: sqlite3.Connection, token: str, admin_key: str) -> Principal | None:
    """Identify the caller, or return None when the token is not a valid credential."""
    # Comparing digests keeps the comparison constant-time and independent of length.
    if hmac.compare_digest(sha256_hex(token), sha256_hex(admin_key)):
        return Principal(kind="admin", actor=ADMIN_ACTOR)
    match = TOKEN.fullmatch(token)
    if match is None:
        return None
    row = conn.execute(
        "SELECT k.kind, k.token_sha256, e.id AS env_id, e.public_id"
        " FROM api_keys AS k JOIN environments AS e ON e.id = k.environment_id"
        " WHERE k.id = ? AND k.revoked_at IS NULL",
        (match.group("key"),),
    ).fetchone()
    if row is None:
        return None
    if not hmac.compare_digest(row["token_sha256"], sha256_hex(token)):
        return None
    # The hash covers the whole token, so a matching hash means the prefix and the
    # environment in the token are the ones issued; these checks are defensive.
    if row["kind"] != KIND_BY_PREFIX[match.group("prefix")]:
        return None
    if row["public_id"] != match.group("env"):
        return None
    kind: KeyKind = row["kind"]
    return Principal(
        kind=kind,
        actor=match.group("key"),
        environment_id=row["env_id"],
        environment_public_id=row["public_id"],
    )
