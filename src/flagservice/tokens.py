"""The format of server and SDK API keys, shared by the server and the SDK.

A token reads ``<kind>.<environment id>.<key id>.<secret>``, for example
``sdk.env_0123456789ab.key_0123456789ab.<43 characters>``. The environment id
lets an SDK build its config URL from the key alone; the key id lets the
server find the key's record without scanning; the secret is 32 random bytes
in URL-safe base64.
"""

from __future__ import annotations

import re
from typing import Literal

KeyKind = Literal["server", "sdk"]

PREFIX: dict[KeyKind, str] = {"server": "srv", "sdk": "sdk"}
KIND_BY_PREFIX: dict[str, KeyKind] = {prefix: kind for kind, prefix in PREFIX.items()}

TOKEN = re.compile(
    r"(?P<prefix>srv|sdk)\.(?P<env>env_[0-9a-f]{12})\.(?P<key>key_[0-9a-f]{12})\.[A-Za-z0-9_-]{43}"
)


def environment_of_token(token: str, kind: KeyKind | None = None) -> str | None:
    """The environment id in a well-formed token (of ``kind``, if given), else None."""
    match = TOKEN.fullmatch(token)
    if match is None:
        return None
    if kind is not None and match.group("prefix") != PREFIX[kind]:
        return None
    return match.group("env")
