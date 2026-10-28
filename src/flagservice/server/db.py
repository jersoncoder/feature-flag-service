"""SQLite access: connections, explicit transactions and numbered migrations.

Every connection runs in autocommit mode (``isolation_level=None``) and every
write goes through ``transaction()``, which issues ``BEGIN IMMEDIATE``. That
takes SQLite's write lock up front, so a read-check-write sequence inside one
transaction cannot interleave with another writer.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_MIGRATION_NAME = re.compile(r"(\d{4})_([a-z0-9_]+)\.sql")
BUSY_TIMEOUT_MS = 5000


class MigrationError(RuntimeError):
    """The database and the migration files disagree; the server must not start."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def utcnow() -> str:
    """Current UTC time as ISO 8601 with milliseconds, the format stored everywhere."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def connect(path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False: FastAPI may run a request's dependencies and its
    # handler on different worker threads. A connection still serves one request
    # at a time. ``timeout`` is SQLite's busy timeout: how long a writer waits for
    # another writer's lock before failing.
    conn = sqlite3.connect(
        path,
        isolation_level=None,
        check_same_thread=False,
        timeout=BUSY_TIMEOUT_MS / 1000,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, *, write: bool = True) -> Iterator[sqlite3.Connection]:
    """Run a block in one transaction: commit on success, roll back on any exception.

    ``write=False`` opens a deferred transaction, which in WAL mode gives the
    block a consistent snapshot without blocking writers.
    """
    conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Read ``NNNN_name.sql`` files in version order."""
    migrations: dict[int, Migration] = {}
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(f"{path.name}: migration files must be named NNNN_name.sql")
        version = int(match.group(1))
        if version in migrations:
            raise MigrationError(f"{path.name}: version {version} is used twice")
        migrations[version] = Migration(version, match.group(2), path.read_text("utf-8"))
    return [migrations[version] for version in sorted(migrations)]


def split_statements(script: str) -> list[str]:
    """Split a SQL script into statements, respecting strings, comments and triggers.

    ``sqlite3.executescript`` would be simpler, but it commits any open
    transaction first, so a failing migration could leave half its statements
    applied. Executing statement by statement keeps each migration atomic.
    """
    statements: list[str] = []
    buffer = ""
    pieces = script.split(";")
    for piece in pieces[:-1]:
        buffer += piece + ";"
        if sqlite3.complete_statement(buffer):
            if _has_sql(buffer):
                statements.append(buffer.strip())
            buffer = ""
    if _has_sql(buffer + pieces[-1]):
        raise MigrationError("script ends with an incomplete statement")
    return statements


def _has_sql(text: str) -> bool:
    lines = (line.split("--", 1)[0] for line in text.splitlines())
    return any(line.strip(" \t;") for line in lines)


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switch the database to write-ahead logging (a persistent setting).

    The switch needs an exclusive lock and SQLite reports "database is locked"
    at once, without waiting for the busy timeout, when another connection is
    doing the same thing; so this retries for up to the busy timeout.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() > deadline:
                raise
            time.sleep(0.01)


def migrate(conn: sqlite3.Connection, migrations: Sequence[Migration] | None = None) -> list[int]:
    """Apply pending migrations in order, each in its own transaction.

    Returns the versions applied by this call. Safe to run from several
    processes at once: each migration re-checks ``schema_migrations`` after
    taking the write lock. Refuses to run when an applied migration's file has
    changed or when the database has a migration this code does not know.
    """
    if migrations is None:
        migrations = load_migrations()
    _enable_wal(conn)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " checksum TEXT NOT NULL,"
        " applied_at TEXT NOT NULL)"
    )
    known = {migration.version for migration in migrations}
    applied_versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    unknown = sorted(applied_versions - known)
    if unknown:
        raise MigrationError(
            f"the database has migration {unknown[0]:04d}, which this version does not have"
        )
    applied: list[int] = []
    for migration in migrations:
        with transaction(conn):
            row = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version = ?", (migration.version,)
            ).fetchone()
            if row is not None:
                if row[0] != migration.checksum:
                    raise MigrationError(
                        f"migration {migration.version:04d}_{migration.name} was changed "
                        "after it was applied; add a new migration instead"
                    )
                continue
            for statement in split_statements(migration.sql):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at)"
                " VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum, utcnow()),
            )
        applied.append(migration.version)
    return applied
