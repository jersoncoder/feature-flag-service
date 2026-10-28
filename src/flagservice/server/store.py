"""Persistence and the rules of the management API.

Every method that changes data runs in one ``BEGIN IMMEDIATE`` transaction that
also appends the audit entry, so a change and its audit record are committed
together or not at all, and a read-check-write sequence (such as comparing a
flag's ETag before updating it) cannot interleave with another writer.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from flagservice.core.model import EnvironmentSettings, FlagConfig, InvalidFlag, Variant
from flagservice.core.validation import Problem, settings_problems, variant_problems
from flagservice.server import schemas
from flagservice.server.auth import issue_key, new_public_id
from flagservice.server.db import transaction, utcnow
from flagservice.server.errors import (
    BadRequest,
    Conflict,
    InvalidRequest,
    NotFound,
    PreconditionFailed,
)
from flagservice.server.etags import content_etag, flag_etag, strong_match
from flagservice.tokens import KeyKind

BOOLEAN_VARIANTS = (Variant(key="off", value=False), Variant(key="on", value=True))


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), allow_nan=False)


def _dump_list(models: Iterable[BaseModel]) -> str:
    return _dumps([model.model_dump(mode="json") for model in models])


@dataclass(frozen=True)
class FlagRecord:
    id: int
    project_id: int
    flag: schemas.Flag

    @property
    def etag(self) -> str:
        return flag_etag(self.id, self.flag.version)


class Store:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # Projects

    def create_project(self, data: schemas.ProjectCreate, actor: str) -> schemas.Project:
        with transaction(self.conn):
            if self._maybe_project(data.key) is not None:
                raise Conflict(f"project {data.key!r} already exists")
            now = utcnow()
            cursor = self.conn.execute(
                "INSERT INTO projects (key, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (data.key, data.name, now, now),
            )
            project = self._project_out(self._project(data.key))
            self._audit(_lastrowid(cursor), actor, "project.created", after=project)
        return project

    def list_projects(self) -> list[schemas.Project]:
        rows = self.conn.execute("SELECT * FROM projects ORDER BY key").fetchall()
        return [self._project_out(row) for row in rows]

    def get_project(self, key: str) -> schemas.Project:
        return self._project_out(self._project(key))

    def update_project(self, key: str, data: schemas.ProjectUpdate, actor: str) -> schemas.Project:
        with transaction(self.conn):
            row = self._project(key)
            before = self._project_out(row)
            self.conn.execute(
                "UPDATE projects SET name = ?, updated_at = ? WHERE id = ?",
                (data.name, utcnow(), row["id"]),
            )
            after = self._project_out(self._project(key))
            self._audit(row["id"], actor, "project.updated", before=before, after=after)
        return after

    def delete_project(self, key: str, actor: str) -> None:
        with transaction(self.conn):
            row = self._project(key)
            in_use = self.conn.execute(
                "SELECT EXISTS (SELECT 1 FROM environments WHERE project_id = ?)"
                " OR EXISTS (SELECT 1 FROM flags WHERE project_id = ?)",
                (row["id"], row["id"]),
            ).fetchone()[0]
            if in_use:
                raise Conflict(
                    f"project {key!r} still has environments or flags; delete those first"
                )
            self.conn.execute("DELETE FROM projects WHERE id = ?", (row["id"],))
            self._audit(row["id"], actor, "project.deleted", before=self._project_out(row))

    # Environments

    def create_environment(
        self, project_key: str, data: schemas.EnvironmentCreate, actor: str
    ) -> schemas.Environment:
        with transaction(self.conn):
            project = self._project(project_key)
            if self._maybe_environment(project["id"], data.key) is not None:
                raise Conflict(f"environment {data.key!r} already exists in {project_key!r}")
            now = utcnow()
            self.conn.execute(
                "INSERT INTO environments"
                " (public_id, project_id, key, name, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (new_public_id("env"), project["id"], data.key, data.name, now, now),
            )
            row = self._environment(project["id"], data.key)
            environment = self._environment_out(row, project_key)
            self._audit(
                project["id"],
                actor,
                "environment.created",
                environment_key=data.key,
                after=environment,
            )
            # Every flag gets settings for the new environment: off, serving its
            # first variant. That changes each flag's representation, so each
            # flag's version moves on and the change is audited.
            for record in self._flag_records(project["id"]):
                self._write_settings(
                    record.id,
                    row["id"],
                    EnvironmentSettings(default_variant=record.flag.variants[0].key),
                )
                self._bump_and_audit(
                    record, actor, "flag.environment_added", environment_key=data.key
                )
        return environment

    def list_environments(self, project_key: str) -> list[schemas.Environment]:
        project = self._project(project_key)
        rows = self.conn.execute(
            "SELECT * FROM environments WHERE project_id = ? ORDER BY key", (project["id"],)
        ).fetchall()
        return [self._environment_out(row, project_key) for row in rows]

    def get_environment(self, project_key: str, key: str) -> schemas.Environment:
        project = self._project(project_key)
        return self._environment_out(self._environment(project["id"], key), project_key)

    def update_environment(
        self, project_key: str, key: str, data: schemas.EnvironmentUpdate, actor: str
    ) -> schemas.Environment:
        with transaction(self.conn):
            project = self._project(project_key)
            row = self._environment(project["id"], key)
            before = self._environment_out(row, project_key)
            self.conn.execute(
                "UPDATE environments SET name = ?, updated_at = ? WHERE id = ?",
                (data.name, utcnow(), row["id"]),
            )
            after = self._environment_out(self._environment(project["id"], key), project_key)
            self._audit(
                project["id"],
                actor,
                "environment.updated",
                environment_key=key,
                before=before,
                after=after,
            )
        return after

    def delete_environment(self, project_key: str, key: str, actor: str) -> None:
        """Delete an environment with its API keys and every flag's settings for it."""
        with transaction(self.conn):
            project = self._project(project_key)
            row = self._environment(project["id"], key)
            records = self._flag_records(project["id"])
            self.conn.execute("DELETE FROM environments WHERE id = ?", (row["id"],))
            for record in records:
                self._bump_and_audit(record, actor, "flag.environment_removed", environment_key=key)
            self._audit(
                project["id"],
                actor,
                "environment.deleted",
                environment_key=key,
                before=self._environment_out(row, project_key),
            )

    # API keys

    def create_api_key(
        self, project_key: str, environment_key: str, data: schemas.ApiKeyCreate, actor: str
    ) -> schemas.ApiKeyCreated:
        with transaction(self.conn):
            project = self._project(project_key)
            environment = self._environment(project["id"], environment_key)
            issued = issue_key(data.kind, environment["public_id"])
            self.conn.execute(
                "INSERT INTO api_keys (id, environment_id, kind, name, token_sha256, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    issued.key_id,
                    environment["id"],
                    data.kind,
                    data.name,
                    issued.token_sha256,
                    utcnow(),
                ),
            )
            key = self._api_key(environment["id"], issued.key_id, environment_key)
            self._audit(
                project["id"],
                actor,
                "api_key.created",
                environment_key=environment_key,
                after=key,
            )
        return schemas.ApiKeyCreated(**key.model_dump(), token=issued.token)

    def list_api_keys(self, project_key: str, environment_key: str) -> list[schemas.ApiKey]:
        project = self._project(project_key)
        environment = self._environment(project["id"], environment_key)
        rows = self.conn.execute(
            "SELECT * FROM api_keys WHERE environment_id = ? ORDER BY created_at, id",
            (environment["id"],),
        ).fetchall()
        return [self._api_key_out(row, environment_key) for row in rows]

    def revoke_api_key(
        self, project_key: str, environment_key: str, key_id: str, actor: str
    ) -> None:
        """Revoke a key. Revoking a revoked key changes nothing."""
        with transaction(self.conn):
            project = self._project(project_key)
            environment = self._environment(project["id"], environment_key)
            before = self._api_key(environment["id"], key_id, environment_key)
            if before.revoked_at is not None:
                return
            self.conn.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ?", (utcnow(), key_id))
            after = self._api_key(environment["id"], key_id, environment_key)
            self._audit(
                project["id"],
                actor,
                "api_key.revoked",
                environment_key=environment_key,
                before=before,
                after=after,
            )

    # Flags

    def create_flag(self, project_key: str, data: schemas.FlagCreate, actor: str) -> FlagRecord:
        with transaction(self.conn):
            project = self._project(project_key)
            if self._maybe_flag_row(project["id"], data.key) is not None:
                raise Conflict(f"flag {data.key!r} already exists in {project_key!r}")
            if data.variants is not None:
                variants = list(data.variants)
            elif data.type == "boolean":
                variants = list(BOOLEAN_VARIANTS)
            else:
                raise InvalidRequest([Problem(("variants",), f"a {data.type} flag needs variants")])
            environments = {
                row["key"]: row
                for row in self.conn.execute(
                    "SELECT * FROM environments WHERE project_id = ?", (project["id"],)
                )
            }
            problems = [p.under("variants") for p in variant_problems(data.type, variants)]
            keys = {variant.key for variant in variants}
            for environment_key, settings in data.environments.items():
                if environment_key not in environments:
                    problems.append(
                        Problem(
                            ("environments", environment_key),
                            f"project {project_key!r} has no environment {environment_key!r}",
                        )
                    )
                problems += [
                    p.under("environments", environment_key)
                    for p in settings_problems(settings, keys)
                ]
            if problems:
                raise InvalidRequest(problems)
            now = utcnow()
            cursor = self.conn.execute(
                "INSERT INTO flags (project_id, key, type, description, salt, variants, version,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    project["id"],
                    data.key,
                    data.type,
                    data.description,
                    secrets.token_hex(8),
                    _dump_list(variants),
                    now,
                    now,
                ),
            )
            flag_id = _lastrowid(cursor)
            default = EnvironmentSettings(default_variant=variants[0].key)
            for environment_key, row in environments.items():
                settings = data.environments.get(environment_key, default)
                self._write_settings(flag_id, row["id"], settings)
            record = self._flag_record(project["id"], data.key)
            self._audit(project["id"], actor, "flag.created", flag_key=data.key, after=record.flag)
        return record

    def list_flags(self, project_key: str) -> list[schemas.Flag]:
        # A read transaction gives the flag and settings queries one snapshot.
        with transaction(self.conn, write=False):
            project = self._project(project_key)
            return [record.flag for record in self._flag_records(project["id"])]

    def get_flag(self, project_key: str, flag_key: str) -> FlagRecord:
        with transaction(self.conn, write=False):
            project = self._project(project_key)
            return self._flag_record(project["id"], flag_key)

    def update_flag(
        self,
        project_key: str,
        flag_key: str,
        data: schemas.FlagUpdate,
        if_match: Sequence[tuple[bool, str]],
        actor: str,
    ) -> FlagRecord:
        """Replace a flag's description and variants (its key and type never change)."""
        with transaction(self.conn):
            project = self._project(project_key)
            record = self._flag_record(project["id"], flag_key)
            _check_precondition(record, if_match)
            problems = [
                p.under("variants") for p in variant_problems(record.flag.type, data.variants)
            ]
            keys = {variant.key for variant in data.variants}
            for environment_key, settings in record.flag.environments.items():
                for problem in settings_problems(settings, keys):
                    where = ".".join(str(part) for part in problem.loc)
                    problems.append(
                        Problem(
                            ("variants",),
                            f"environment {environment_key!r} still uses a variant this list "
                            f"removes ({where}: {problem.msg})",
                        )
                    )
            if problems:
                raise InvalidRequest(problems)
            self.conn.execute(
                "UPDATE flags SET description = ?, variants = ? WHERE id = ?",
                (data.description, _dump_list(data.variants), record.id),
            )
            return self._bump_and_audit(record, actor, "flag.updated")

    def update_flag_environment(
        self,
        project_key: str,
        flag_key: str,
        environment_key: str,
        settings: EnvironmentSettings,
        if_match: Sequence[tuple[bool, str]],
        actor: str,
    ) -> FlagRecord:
        """Replace a flag's switch, default variant and rules in one environment."""
        with transaction(self.conn):
            project = self._project(project_key)
            record = self._flag_record(project["id"], flag_key)
            environment = self._environment(project["id"], environment_key)
            _check_precondition(record, if_match)
            keys = {variant.key for variant in record.flag.variants}
            problems = settings_problems(settings, keys)
            if problems:
                raise InvalidRequest(problems)
            self._write_settings(record.id, environment["id"], settings)
            return self._bump_and_audit(
                record, actor, "flag.environment_updated", environment_key=environment_key
            )

    def delete_flag(
        self,
        project_key: str,
        flag_key: str,
        if_match: Sequence[tuple[bool, str]],
        actor: str,
    ) -> None:
        with transaction(self.conn):
            project = self._project(project_key)
            record = self._flag_record(project["id"], flag_key)
            _check_precondition(record, if_match)
            self.conn.execute("DELETE FROM flags WHERE id = ?", (record.id,))
            self._audit(project["id"], actor, "flag.deleted", flag_key=flag_key, before=record.flag)

    # Runtime

    def environment_config(self, environment_id: int) -> tuple[bytes, str]:
        """The environment's full ruleset as published to SDKs, and its ETag.

        The body is serialised deterministically (sorted keys, no whitespace), so
        the ETag, a hash of the body, changes exactly when the content changes.
        """
        with transaction(self.conn, write=False):
            environment = self.conn.execute(
                "SELECT e.public_id, e.key, p.key AS project FROM environments AS e"
                " JOIN projects AS p ON p.id = e.project_id WHERE e.id = ?",
                (environment_id,),
            ).fetchone()
            if environment is None:
                raise NotFound("environment not found")
            rows = self.conn.execute(
                _CONFIG_QUERY + " WHERE s.environment_id = ? ORDER BY f.key", (environment_id,)
            ).fetchall()
        payload = {
            "environment": {
                "id": environment["public_id"],
                "key": environment["key"],
                "project": environment["project"],
            },
            "flags": {row["key"]: _config_dict(row) for row in rows},
        }
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        encoded = body.encode()
        return encoded, content_etag(encoded)

    def flag_config(self, environment_id: int, flag_key: str) -> FlagConfig | InvalidFlag | None:
        """One flag's config, built exactly as ``environment_config`` publishes it."""
        row = self.conn.execute(
            _CONFIG_QUERY + " WHERE s.environment_id = ? AND f.key = ?",
            (environment_id, flag_key),
        ).fetchone()
        if row is None:
            return None
        try:
            return FlagConfig.model_validate(_config_dict(row))
        except ValidationError as exc:
            return InvalidFlag(key=flag_key, error=str(exc))

    # Audit log

    def audit_page(
        self, project_key: str, flag_key: str | None, limit: int, cursor: str | None
    ) -> schemas.AuditPage:
        """Entries newest first. The cursor encodes the last id seen.

        Paging by id rather than by offset keeps pages stable while new entries
        are appended: a page never repeats or skips an entry.
        """
        project = self._project(project_key)
        before_id = _decode_cursor(cursor) if cursor is not None else _NO_CURSOR
        if flag_key is None:
            rows = self.conn.execute(
                "SELECT * FROM audit_log WHERE project_id = ? AND id < ? ORDER BY id DESC LIMIT ?",
                (project["id"], before_id, limit + 1),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM audit_log WHERE project_id = ? AND flag_key = ? AND id < ?"
                " ORDER BY id DESC LIMIT ?",
                (project["id"], flag_key, before_id, limit + 1),
            ).fetchall()
        entries = [
            schemas.AuditEntry(
                id=row["id"],
                at=row["at"],
                actor=row["actor"],
                action=row["action"],
                flag=row["flag_key"],
                environment=row["environment_key"],
                before=json.loads(row["before"]) if row["before"] is not None else None,
                after=json.loads(row["after"]) if row["after"] is not None else None,
            )
            for row in rows[:limit]
        ]
        more = len(rows) > limit
        return schemas.AuditPage(
            items=entries, next_cursor=_encode_cursor(entries[-1].id) if more else None
        )

    # Helpers

    def _maybe_project(self, key: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM projects WHERE key = ?", (key,)
        ).fetchone()
        return row

    def _project(self, key: str) -> sqlite3.Row:
        row = self._maybe_project(key)
        if row is None:
            raise NotFound(f"project {key!r} not found")
        return row

    def _maybe_environment(self, project_id: int, key: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM environments WHERE project_id = ? AND key = ?", (project_id, key)
        ).fetchone()
        return row

    def _environment(self, project_id: int, key: str) -> sqlite3.Row:
        row = self._maybe_environment(project_id, key)
        if row is None:
            raise NotFound(f"environment {key!r} not found")
        return row

    def _api_key(self, environment_id: int, key_id: str, environment_key: str) -> schemas.ApiKey:
        row = self.conn.execute(
            "SELECT * FROM api_keys WHERE id = ? AND environment_id = ?", (key_id, environment_id)
        ).fetchone()
        if row is None:
            raise NotFound(f"API key {key_id!r} not found")
        return self._api_key_out(row, environment_key)

    def _maybe_flag_row(self, project_id: int, key: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM flags WHERE project_id = ? AND key = ?", (project_id, key)
        ).fetchone()
        return row

    def _flag_record(self, project_id: int, key: str) -> FlagRecord:
        row = self._maybe_flag_row(project_id, key)
        if row is None:
            raise NotFound(f"flag {key!r} not found")
        settings = self.conn.execute(
            _SETTINGS_QUERY + " WHERE s.flag_id = ? ORDER BY e.key", (row["id"],)
        ).fetchall()
        return _records([row], settings)[0]

    def _flag_records(self, project_id: int) -> list[FlagRecord]:
        rows = self.conn.execute(
            "SELECT * FROM flags WHERE project_id = ? ORDER BY key", (project_id,)
        ).fetchall()
        settings = self.conn.execute(
            _SETTINGS_QUERY + " WHERE e.project_id = ? ORDER BY e.key", (project_id,)
        ).fetchall()
        return _records(rows, settings)

    def _write_settings(
        self, flag_id: int, environment_id: int, settings: EnvironmentSettings
    ) -> None:
        self.conn.execute(
            "INSERT INTO flag_settings (flag_id, environment_id, enabled, default_variant, rules)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (flag_id, environment_id) DO UPDATE SET"
            " enabled = excluded.enabled,"
            " default_variant = excluded.default_variant,"
            " rules = excluded.rules",
            (
                flag_id,
                environment_id,
                int(settings.enabled),
                settings.default_variant,
                _dump_list(settings.rules),
            ),
        )

    def _bump_and_audit(
        self,
        record: FlagRecord,
        actor: str,
        action: str,
        *,
        environment_key: str | None = None,
    ) -> FlagRecord:
        """Move the flag to its next version and record the before and after states."""
        cursor = self.conn.execute(
            "UPDATE flags SET version = version + 1, updated_at = ? WHERE id = ? AND version = ?",
            (utcnow(), record.id, record.flag.version),
        )
        if cursor.rowcount != 1:  # pragma: no cover - the write lock rules this out
            raise RuntimeError(f"flag {record.flag.key!r} changed inside a write transaction")
        after = self._flag_record(record.project_id, record.flag.key)
        self._audit(
            record.project_id,
            actor,
            action,
            flag_key=record.flag.key,
            environment_key=environment_key,
            before=record.flag,
            after=after.flag,
        )
        return after

    def _audit(
        self,
        project_id: int,
        actor: str,
        action: str,
        *,
        flag_key: str | None = None,
        environment_key: str | None = None,
        before: BaseModel | None = None,
        after: BaseModel | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO audit_log"
            " (project_id, at, actor, action, flag_key, environment_key, before, after)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                utcnow(),
                actor,
                action,
                flag_key,
                environment_key,
                _dumps(before.model_dump(mode="json")) if before is not None else None,
                _dumps(after.model_dump(mode="json")) if after is not None else None,
            ),
        )

    @staticmethod
    def _project_out(row: sqlite3.Row) -> schemas.Project:
        return schemas.Project(
            key=row["key"],
            name=row["name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _environment_out(row: sqlite3.Row, project_key: str) -> schemas.Environment:
        return schemas.Environment(
            id=row["public_id"],
            project=project_key,
            key=row["key"],
            name=row["name"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _api_key_out(row: sqlite3.Row, environment_key: str) -> schemas.ApiKey:
        kind: KeyKind = row["kind"]
        return schemas.ApiKey(
            id=row["id"],
            kind=kind,
            name=row["name"],
            environment=environment_key,
            created_at=row["created_at"],
            revoked_at=row["revoked_at"],
        )


_SETTINGS_QUERY = (
    "SELECT s.*, e.key AS environment_key FROM flag_settings AS s"
    " JOIN environments AS e ON e.id = s.environment_id"
)

_NO_CURSOR = 2**63 - 1
"""Larger than any audit id, so the first page needs no special query."""


def _records(rows: Sequence[sqlite3.Row], settings_rows: Sequence[sqlite3.Row]) -> list[FlagRecord]:
    """Assemble flags from their rows and the settings rows of their environments."""
    settings: dict[int, dict[str, EnvironmentSettings]] = {row["id"]: {} for row in rows}
    for row in settings_rows:
        if row["flag_id"] in settings:
            settings[row["flag_id"]][row["environment_key"]] = EnvironmentSettings.model_validate(
                {
                    "enabled": bool(row["enabled"]),
                    "default_variant": row["default_variant"],
                    "rules": json.loads(row["rules"]),
                }
            )
    return [
        FlagRecord(
            id=row["id"],
            project_id=row["project_id"],
            flag=schemas.Flag.model_validate(
                {
                    "key": row["key"],
                    "type": row["type"],
                    "description": row["description"],
                    "salt": row["salt"],
                    "version": row["version"],
                    "variants": json.loads(row["variants"]),
                    "environments": settings[row["id"]],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
            ),
        )
        for row in rows
    ]


_CONFIG_QUERY = (
    "SELECT f.key, f.type, f.salt, f.version, f.variants,"
    " s.enabled, s.default_variant, s.rules"
    " FROM flag_settings AS s JOIN flags AS f ON f.id = s.flag_id"
)


def _config_dict(row: sqlite3.Row) -> dict[str, Any]:
    """The published form of one flag in one environment (input to ``FlagConfig``)."""
    return {
        "key": row["key"],
        "type": row["type"],
        "salt": row["salt"],
        "version": row["version"],
        "variants": json.loads(row["variants"]),
        "enabled": bool(row["enabled"]),
        "default_variant": row["default_variant"],
        "rules": json.loads(row["rules"]),
    }


def _check_precondition(record: FlagRecord, if_match: Sequence[tuple[bool, str]]) -> None:
    if not strong_match(list(if_match), record.etag):
        raise PreconditionFailed(record.flag.version, record.etag)


def _lastrowid(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:  # pragma: no cover - INSERT always sets it
        raise RuntimeError("INSERT did not report a row id")
    return cursor.lastrowid


_CURSOR_PREFIX = "audit:"


def _encode_cursor(entry_id: int) -> str:
    raw = f"{_CURSOR_PREFIX}{entry_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> int:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise BadRequest("invalid cursor") from None
    number = raw.removeprefix(_CURSOR_PREFIX)
    if number == raw or not (number.isascii() and number.isdigit()):
        raise BadRequest("invalid cursor")
    return int(number)
