"""API keys.

There are three kinds of credential, each accepted only by its own endpoints:

* the admin key, from ``FLAGS_ADMIN_KEY``, for the management API;
* server keys, for ``POST /v1/evaluate``;
* SDK keys, for ``GET /v1/environments/{env}/config``.

Server and SDK keys belong to one environment. A token looks like
``sdk.env_0123456789ab.key_0123456789ab.<secret>``: the kind, the public id of
the environment (so the SDK can build its URL from the key alone), the key id
(public, used to find the record and as the audit actor) and 256 random bits.
Only the SHA-256 of the whole token is stored. A slow password hash would add
nothing here, because the secret is random rather than chosen by a person.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Literal

KeyKind = Literal["server", "sdk"]
PrincipalKind = Literal["admin", "server", "sdk"]

_PREFIX: dict[KeyKind, str] = {"server": "srv", "sdk": "sdk"}
_KIND_BY_PREFIX: dict[str, KeyKind] = {prefix: kind for kind, prefix in _PREFIX.items()}
_TOKEN = re.compile(
    r"(?P<prefix>srv|sdk)\.(?P<env>env_[0-9a-f]{12})\.(?P<key>key_[0-9a-f]{12})\.[A-Za-z0-9_-]{43}"
)

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
    token = f"{_PREFIX[kind]}.{environment_public_id}.{key_id}.{secrets.token_urlsafe(32)}"
    return IssuedKey(key_id=key_id, token=token, token_sha256=sha256_hex(token))


def environment_of_token(token: str) -> str | None:
    """The environment public id a server or SDK token belongs to, if it is well formed."""
    match = _TOKEN.fullmatch(token)
    return match.group("env") if match else None


def authenticate(conn: sqlite3.Connection, token: str, admin_key: str) -> Principal | None:
    """Identify the caller, or return None when the token is not a valid credential."""
    # Comparing digests keeps the comparison constant-time and independent of length.
    if hmac.compare_digest(sha256_hex(token), sha256_hex(admin_key)):
        return Principal(kind="admin", actor=ADMIN_ACTOR)
    match = _TOKEN.fullmatch(token)
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
    if row["kind"] != _KIND_BY_PREFIX[match.group("prefix")]:
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
