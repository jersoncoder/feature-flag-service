"""FastAPI dependencies: database connection, authentication and preconditions.

No ``from __future__ import annotations`` here: FastAPI reads these annotations
at runtime, and the dependency built by ``_require`` refers to a closure variable
that a postponed (string) annotation could not resolve.
"""

import sqlite3
from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from flagservice.server.auth import Principal, PrincipalKind, authenticate
from flagservice.server.db import connect
from flagservice.server.etags import MalformedHeader, parse_tags
from flagservice.server.settings import Settings
from flagservice.server.store import Store

# Dependencies that only touch memory are ``async def`` so FastAPI runs them on
# the event loop; ``def`` dependencies (the database ones) go to a worker thread.


async def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_conn(settings: Annotated[Settings, Depends(get_settings)]) -> Iterator[sqlite3.Connection]:
    conn = connect(settings.database_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


async def get_store(conn: Conn) -> Store:
    return Store(conn)


StoreDep = Annotated[Store, Depends(get_store)]

_SCHEMES: dict[PrincipalKind, HTTPBearer] = {
    "admin": HTTPBearer(
        scheme_name="AdminKey",
        description="The admin key from FLAGS_ADMIN_KEY. Management endpoints only.",
        auto_error=False,
    ),
    "server": HTTPBearer(
        scheme_name="ServerKey",
        description="An environment's server key. POST /v1/evaluate only.",
        auto_error=False,
    ),
    "sdk": HTTPBearer(
        scheme_name="SdkKey",
        description="An environment's SDK key. GET /v1/environments/{env}/config only.",
        auto_error=False,
    ),
}

_ENDPOINT_NEEDS = {
    "admin": "the admin key",
    "server": "a server key",
    "sdk": "an SDK key",
}


def _require(kind: PrincipalKind) -> Callable[..., Principal]:
    def dependency(
        conn: Conn,
        settings: Annotated[Settings, Depends(get_settings)],
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_SCHEMES[kind])],
    ) -> Principal:
        principal = None
        if credentials is not None:
            principal = authenticate(conn, credentials.credentials, settings.admin_key)
        if principal is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                detail="missing or invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if principal.kind != kind:
            needed, given = _ENDPOINT_NEEDS[kind], _ENDPOINT_NEEDS[principal.kind]
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, detail=f"this endpoint needs {needed}, not {given}"
            )
        return principal

    return dependency


Admin = Annotated[Principal, Depends(_require("admin"))]
ServerKey = Annotated[Principal, Depends(_require("server"))]
SdkKey = Annotated[Principal, Depends(_require("sdk"))]


async def if_match(
    header: Annotated[
        str | None,
        Header(
            alias="If-Match",
            description="The flag's current ETag, from a GET or the previous write.",
        ),
    ] = None,
) -> list[tuple[bool, str]]:
    """The entity tags of a required If-Match header."""
    if header is None:
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            detail="this change needs an If-Match header with the flag's current ETag",
        )
    try:
        tags = parse_tags(header)
    except MalformedHeader as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    if tags == [(False, "*")]:
        # "*" would match any version and turn the update into a blind write.
        raise HTTPException(
            status.HTTP_428_PRECONDITION_REQUIRED,
            detail="If-Match: * is not accepted; send the flag's current ETag",
        )
    return tags


IfMatch = Annotated[list[tuple[bool, str]], Depends(if_match)]
