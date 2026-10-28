from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

import pytest

from flagservice.server.db import (
    Migration,
    MigrationError,
    connect,
    load_migrations,
    migrate,
    split_statements,
)

EXPECTED_TABLES = {
    "schema_migrations",
    "projects",
    "environments",
    "api_keys",
    "flags",
    "flag_settings",
    "audit_log",
}


Opener = Callable[[Path], sqlite3.Connection]


@pytest.fixture
def open_db() -> Iterator[Opener]:
    opened: list[sqlite3.Connection] = []

    def factory(path: Path) -> sqlite3.Connection:
        conn = connect(path)
        opened.append(conn)
        return conn

    yield factory
    for conn in opened:
        conn.close()


def tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row[0] for row in rows if not row[0].startswith("sqlite_")}


def test_migrations_apply_to_an_empty_database(tmp_path: Path, open_db: Opener) -> None:
    conn = open_db(tmp_path / "flags.db")
    assert tables(conn) == set()
    applied = migrate(conn)
    assert applied == [m.version for m in load_migrations()]
    assert applied[0] == 1
    assert tables(conn) == EXPECTED_TABLES
    rows = conn.execute("SELECT version, name, checksum FROM schema_migrations").fetchall()
    assert [(r[0], r[1]) for r in rows] == [(m.version, m.name) for m in load_migrations()]
    assert all(len(r[2]) == 64 for r in rows)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_migrate_is_idempotent(tmp_path: Path, open_db: Opener) -> None:
    conn = open_db(tmp_path / "flags.db")
    migrate(conn)
    assert migrate(conn) == []
    assert migrate(open_db(tmp_path / "flags.db")) == []


def write(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql, "utf-8")


def test_pending_migrations_apply_in_version_order(tmp_path: Path, open_db: Opener) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    write(directory, "0002_add_b.sql", "CREATE TABLE b (id INTEGER REFERENCES a (id));")
    write(directory, "0001_add_a.sql", "CREATE TABLE a (id INTEGER PRIMARY KEY);")
    conn = open_db(tmp_path / "x.db")
    assert migrate(conn, load_migrations(directory)) == [1, 2]
    write(directory, "0010_add_c.sql", "CREATE TABLE c (id INTEGER);")
    assert migrate(conn, load_migrations(directory)) == [10]
    assert {"a", "b", "c"} <= tables(conn)


def test_a_failing_migration_leaves_nothing_behind(tmp_path: Path, open_db: Opener) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    write(directory, "0001_ok.sql", "CREATE TABLE a (id INTEGER);")
    write(
        directory,
        "0002_broken.sql",
        "CREATE TABLE half_done (id INTEGER);\nINSERT INTO missing_table VALUES (1);",
    )
    conn = open_db(tmp_path / "x.db")
    with pytest.raises(sqlite3.OperationalError, match="missing_table"):
        migrate(conn, load_migrations(directory))
    assert "half_done" not in tables(conn)
    assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations")] == [1]
    # Fixing the file lets the next start-up finish the job.
    write(directory, "0002_broken.sql", "CREATE TABLE half_done (id INTEGER);")
    assert migrate(conn, load_migrations(directory)) == [2]


def test_an_edited_migration_is_refused(tmp_path: Path, open_db: Opener) -> None:
    conn = open_db(tmp_path / "x.db")
    migrate(conn, [Migration(1, "first", "CREATE TABLE a (id INTEGER);")])
    with pytest.raises(MigrationError, match="was changed after it was applied"):
        migrate(conn, [Migration(1, "first", "CREATE TABLE a (id INTEGER, x TEXT);")])


def test_a_database_newer_than_the_code_is_refused(tmp_path: Path, open_db: Opener) -> None:
    conn = open_db(tmp_path / "x.db")
    first = Migration(1, "first", "CREATE TABLE a (id INTEGER);")
    migrate(conn, [first, Migration(2, "second", "CREATE TABLE b (id INTEGER);")])
    with pytest.raises(MigrationError, match="0002"):
        migrate(conn, [first])


@pytest.mark.parametrize(
    ("names", "message"),
    [
        (["0001_ok.sql", "1_short.sql"], "must be named"),
        (["0001_ok.sql", "0001_again.sql"], "used twice"),
        (["0001_Upper.sql"], "must be named"),
    ],
)
def test_bad_migration_files(tmp_path: Path, names: list[str], message: str) -> None:
    for name in names:
        write(tmp_path, name, "SELECT 1;")
    with pytest.raises(MigrationError, match=message):
        load_migrations(tmp_path)


def test_concurrent_start_ups_apply_each_migration_once(tmp_path: Path) -> None:
    path = tmp_path / "flags.db"
    results: list[list[int]] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def start() -> None:
        with closing(connect(path)) as conn:
            barrier.wait()
            try:
                results.append(migrate(conn))
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    applied = sorted(v for result in results for v in result)
    assert applied == [m.version for m in load_migrations()]


def test_split_statements_handles_strings_comments_and_triggers() -> None:
    script = """
    -- a comment; with a semicolon
    CREATE TABLE t (v TEXT); INSERT INTO t VALUES ('a;b');
    CREATE TRIGGER t_upper AFTER INSERT ON t BEGIN
        UPDATE t SET v = upper(v) WHERE rowid = new.rowid;
    END;
    -- trailing comment
    """
    statements = split_statements(script)
    assert len(statements) == 3
    assert statements[1] == "INSERT INTO t VALUES ('a;b');"
    assert statements[2].startswith("CREATE TRIGGER")
    assert statements[2].endswith("END;")
    with closing(sqlite3.connect(":memory:")) as conn:
        for statement in statements:
            conn.execute(statement)
        conn.execute("INSERT INTO t VALUES ('c')")
        assert [r[0] for r in conn.execute("SELECT v FROM t ORDER BY rowid")] == ["a;b", "C"]


def test_split_statements_rejects_a_truncated_script() -> None:
    with pytest.raises(MigrationError, match="incomplete"):
        split_statements("CREATE TABLE a (id INTEGER); CREATE TABLE b (")
