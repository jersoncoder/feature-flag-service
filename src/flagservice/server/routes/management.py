"""Management API: projects, environments, API keys, flags and the audit log.

Every endpoint here needs the admin key.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Response, status

from flagservice.core.model import EnvironmentSettings
from flagservice.server import schemas
from flagservice.server.deps import Admin, IfMatch, StoreDep

router = APIRouter(prefix="/v1/projects", tags=["management"])

_FLAG_ETAG: dict[str, Any] = {
    "ETag": {"description": "The flag's version; send it back in If-Match to change the flag."}
}
_CONDITIONAL: dict[int | str, dict[str, Any]] = {
    status.HTTP_412_PRECONDITION_FAILED: {
        "model": schemas.VersionConflict,
        "description": "If-Match does not name the flag's current version.",
    },
    status.HTTP_428_PRECONDITION_REQUIRED: {"description": "If-Match is missing or is *."},
}


# Projects


@router.post("", status_code=status.HTTP_201_CREATED)
def create_project(
    body: schemas.ProjectCreate, admin: Admin, store: StoreDep, response: Response
) -> schemas.Project:
    project = store.create_project(body, admin.actor)
    response.headers["Location"] = f"/v1/projects/{project.key}"
    return project


@router.get("")
def list_projects(admin: Admin, store: StoreDep) -> list[schemas.Project]:
    return store.list_projects()


@router.get("/{project}")
def get_project(project: str, admin: Admin, store: StoreDep) -> schemas.Project:
    return store.get_project(project)


@router.patch("/{project}")
def update_project(
    project: str, body: schemas.ProjectUpdate, admin: Admin, store: StoreDep
) -> schemas.Project:
    return store.update_project(project, body, admin.actor)


@router.delete("/{project}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project: str, admin: Admin, store: StoreDep) -> None:
    """Delete an empty project (409 while it still has environments or flags)."""
    store.delete_project(project, admin.actor)


# Environments


@router.post("/{project}/environments", status_code=status.HTTP_201_CREATED)
def create_environment(
    project: str,
    body: schemas.EnvironmentCreate,
    admin: Admin,
    store: StoreDep,
    response: Response,
) -> schemas.Environment:
    """Create an environment. Every existing flag gets settings for it (off, first variant)."""
    environment = store.create_environment(project, body, admin.actor)
    response.headers["Location"] = f"/v1/projects/{project}/environments/{environment.key}"
    return environment


@router.get("/{project}/environments")
def list_environments(project: str, admin: Admin, store: StoreDep) -> list[schemas.Environment]:
    return store.list_environments(project)


@router.get("/{project}/environments/{environment}")
def get_environment(
    project: str, environment: str, admin: Admin, store: StoreDep
) -> schemas.Environment:
    return store.get_environment(project, environment)


@router.patch("/{project}/environments/{environment}")
def update_environment(
    project: str,
    environment: str,
    body: schemas.EnvironmentUpdate,
    admin: Admin,
    store: StoreDep,
) -> schemas.Environment:
    return store.update_environment(project, environment, body, admin.actor)


@router.delete("/{project}/environments/{environment}", status_code=status.HTTP_204_NO_CONTENT)
def delete_environment(project: str, environment: str, admin: Admin, store: StoreDep) -> None:
    """Delete an environment, its API keys and every flag's settings for it."""
    store.delete_environment(project, environment, admin.actor)


# API keys


@router.post("/{project}/environments/{environment}/keys", status_code=status.HTTP_201_CREATED)
def create_api_key(
    project: str,
    environment: str,
    body: schemas.ApiKeyCreate,
    admin: Admin,
    store: StoreDep,
) -> schemas.ApiKeyCreated:
    """Create a server or SDK key. The token is in this response only."""
    return store.create_api_key(project, environment, body, admin.actor)


@router.get("/{project}/environments/{environment}/keys")
def list_api_keys(
    project: str, environment: str, admin: Admin, store: StoreDep
) -> list[schemas.ApiKey]:
    return store.list_api_keys(project, environment)


@router.delete(
    "/{project}/environments/{environment}/keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def revoke_api_key(
    project: str, environment: str, key_id: str, admin: Admin, store: StoreDep
) -> None:
    store.revoke_api_key(project, environment, key_id, admin.actor)


# Flags


@router.post(
    "/{project}/flags",
    status_code=status.HTTP_201_CREATED,
    responses={status.HTTP_201_CREATED: {"headers": _FLAG_ETAG}},
)
def create_flag(
    project: str, body: schemas.FlagCreate, admin: Admin, store: StoreDep, response: Response
) -> schemas.Flag:
    record = store.create_flag(project, body, admin.actor)
    response.headers["ETag"] = record.etag
    response.headers["Location"] = f"/v1/projects/{project}/flags/{record.flag.key}"
    return record.flag


@router.get("/{project}/flags")
def list_flags(project: str, admin: Admin, store: StoreDep) -> list[schemas.Flag]:
    return store.list_flags(project)


@router.get("/{project}/flags/{flag}", responses={200: {"headers": _FLAG_ETAG}})
def get_flag(
    project: str, flag: str, admin: Admin, store: StoreDep, response: Response
) -> schemas.Flag:
    record = store.get_flag(project, flag)
    response.headers["ETag"] = record.etag
    return record.flag


@router.put("/{project}/flags/{flag}", responses={200: {"headers": _FLAG_ETAG}, **_CONDITIONAL})
def update_flag(
    project: str,
    flag: str,
    body: schemas.FlagUpdate,
    tags: IfMatch,
    admin: Admin,
    store: StoreDep,
    response: Response,
) -> schemas.Flag:
    """Replace the description and variants. Needs If-Match."""
    record = store.update_flag(project, flag, body, tags, admin.actor)
    response.headers["ETag"] = record.etag
    return record.flag


@router.put(
    "/{project}/flags/{flag}/environments/{environment}",
    responses={200: {"headers": _FLAG_ETAG}, **_CONDITIONAL},
)
def update_flag_environment(
    project: str,
    flag: str,
    environment: str,
    body: EnvironmentSettings,
    tags: IfMatch,
    admin: Admin,
    store: StoreDep,
    response: Response,
) -> schemas.Flag:
    """Replace the switch, default variant and rules in one environment. Needs If-Match."""
    record = store.update_flag_environment(project, flag, environment, body, tags, admin.actor)
    response.headers["ETag"] = record.etag
    return record.flag


@router.delete(
    "/{project}/flags/{flag}", status_code=status.HTTP_204_NO_CONTENT, responses=_CONDITIONAL
)
def delete_flag(project: str, flag: str, tags: IfMatch, admin: Admin, store: StoreDep) -> None:
    """Delete a flag in every environment. Needs If-Match."""
    store.delete_flag(project, flag, tags, admin.actor)


# Audit log


@router.get("/{project}/audit")
def audit_log(
    project: str,
    admin: Admin,
    store: StoreDep,
    flag: Annotated[str | None, Query(description="Only entries about this flag.")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(description="next_cursor of the previous page.")] = None,
) -> schemas.AuditPage:
    """Changes in the project, newest first."""
    return store.audit_page(project, flag, limit, cursor)
