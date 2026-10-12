"""Synthetic legacy, interruption, WAL, and cross-process bootstrap regressions."""

import hashlib
import json
import sqlite3
import subprocess
import sys
import types
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from local_ai_router import db_migrations, receipts
from local_ai_router.app import create_app
from local_ai_router.cli import main
from local_ai_router.mcp_server import create_mcp
from local_ai_router.skills import SkillRegistry
from local_ai_router.store import Store
from local_ai_router.task_api import run_status

LEGACY_FIXTURE = Path(__file__).parent / "fixtures" / "workbench" / "legacy_schema.sql"
LEGACY_V0_TABLES = {"cache", "traces", "calls", "profiles", "health", "runs", "metadata"}


def table_digests(db):
    result = {}
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    for (table,) in tables.fetchall():
        rows = [
            json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":"), default=str)
            for row in db.execute(f'SELECT * FROM "{table}"')
        ]
        result[table] = (len(rows), hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest())
    return result


def snapshot(db):
    schema = list(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"))
    return db.execute("PRAGMA user_version").fetchone()[0], [tuple(row) for row in schema], table_digests(db)


def legacy_database(settings, *, version=0, complete=False):
    """DDL and rows come from the genuine pre-edit V1 Store/SkillRegistry dump."""
    path = settings.state / "router.sqlite3"
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.executescript(LEGACY_FIXTURE.read_text(encoding="utf-8"))
        if not complete:
            tables = set(table_digests(db))
            for table in tables - LEGACY_V0_TABLES:
                db.execute(f'DROP TABLE "{table}"')
            # The initial v0 source (48ed15e) predates the calls.latency column.
            db.execute("ALTER TABLE calls DROP COLUMN latency")
        db.execute(f"PRAGMA user_version={version}")
    return path


@pytest.mark.parametrize("version", [0, 1, 2, 3])
def test_legacy_table_digests_preserved(settings, version):
    path = legacy_database(settings, version=version, complete=True)
    with closing(sqlite3.connect(path)) as db:
        before = table_digests(db)
    recorded = {}
    for line in LEGACY_FIXTURE.read_text(encoding="utf-8").splitlines():
        if line.startswith("-- ") and ": " in line:
            table, digest = line[3:].split(": ", 1)
            if table in before:
                recorded[table] = digest
    assert recorded == {table: digest for table, (_, digest) in before.items()}

    store = Store(settings)
    try:
        skills = SkillRegistry(store)
        after = table_digests(store.db)
        assert {table: after[table] for table in before} == before
        assert all(count == 0 for table, (count, _) in after.items() if table.startswith("wb_"))
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == db_migrations.SUPPORTED_VERSION
        assert skills.get("fixture-skill").name == "Fixture Skill"
        assert skills.presets()["items"][0]["ordered_skills"] == ["fixture-skill"]
        assert store.costs("fixture-request")["calls"] == 1
        assert store.task_cost("fixture-task") == 0.01
        assert store.registry()[0].id == "fixture-model"
        assert store.models_page()["items"][0]["provider"] == "fixture-provider"
        engine = SimpleNamespace(store=store)
        assert run_status(engine, "fixture-request")["completed_tasks"] == ["fixture-task"]
        assert receipts.get(engine, "fixture-run")["verified"]
        assert store.db.execute("SELECT status FROM runs WHERE id='fixture-run'").fetchone()[0] == "complete"
        assert json.loads(store.db.execute("SELECT data FROM receipts WHERE run='fixture-run'").fetchone()[0])[
            "verified"
        ]
    finally:
        store.close()
    backups = list((settings.state / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    reopened = Store(settings)
    reopened.close()
    assert list((settings.state / "backups").glob("*.sqlite3")) == backups


def test_legacy_v0_initial_schema_is_supported(settings):
    path = legacy_database(settings)
    with closing(sqlite3.connect(path)) as db:
        before_calls = tuple(db.execute("SELECT * FROM calls").fetchone())
        before = table_digests(db)
    store = Store(settings)
    try:
        assert tuple(store.db.execute("SELECT * FROM calls").fetchone()) == (*before_calls, 0.0)
        after = table_digests(store.db)
        assert all(after[table] == digest for table, digest in before.items() if table != "calls")
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == db_migrations.SUPPORTED_VERSION
    finally:
        store.close()


def test_migrate_v3_to_v4_preserves_all_v1_rows(settings):
    path = legacy_database(settings, complete=True, version=3)
    with closing(sqlite3.connect(path)) as db:
        before = table_digests(db)
    store = Store(settings)
    try:
        after = table_digests(store.db)
        assert {table: after[table] for table in before} == before
        assert db_migrations.WORKBENCH_COLUMNS.keys() <= after.keys()
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == db_migrations.SUPPORTED_VERSION
        assert db_migrations.DEVICE_COLUMNS.keys() <= after.keys()
    finally:
        store.close()
    with closing(sqlite3.connect(next((settings.state / "backups").glob("*.sqlite3")))) as backup:
        assert table_digests(backup) == before
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 3


@pytest.mark.parametrize("version", [4, 5])
def test_v4_to_v5_preserves_local_state(settings, version):
    path = legacy_database(settings, complete=True, version=3)
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        for statement in db_migrations.WORKBENCH_STATEMENTS:
            db.execute(statement)
        if version == 5:
            db.execute("ALTER TABLE wb_projects ADD COLUMN review_policy TEXT NOT NULL DEFAULT 'AUTO_VERIFY'")
        db.execute(f"PRAGMA user_version={version}")
        before = table_digests(db)
    store = Store(settings)
    try:
        after = table_digests(store.db)
        assert {table: after[table] for table in before} == before
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == db_migrations.SUPPORTED_VERSION
        assert db_migrations.DEVICE_COLUMNS.keys() <= after.keys()
        assert store.db.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        store.close()
    with closing(sqlite3.connect(next((settings.state / "backups").glob("*.sqlite3")))) as backup:
        assert table_digests(backup) == before
        assert backup.execute("PRAGMA user_version").fetchone()[0] == version


def test_v6_journal_upgrade_preserves_rows_and_verified_backup(settings):
    from test_workbench_agents import HUMAN

    from local_ai_router.workbench.service import WorkbenchService
    from local_ai_router.workbench.store import identity

    store = Store(settings)
    service = WorkbenchService(store)
    workspace = service.seed_defaults()
    service.create(
        "project",
        {"command_id": identity(), "workspace_id": workspace.id, "key": "OLD", "name": "Legacy journal"},
        HUMAN,
    )
    store.db.execute("ALTER TABLE wb_entity_versions RENAME TO old_versions")
    statement = next(s for s in db_migrations.WORKBENCH_STATEMENTS if s.startswith("CREATE TABLE wb_entity_versions("))
    store.db.execute(statement)
    store.db.execute("INSERT INTO wb_entity_versions SELECT * FROM old_versions")
    store.db.execute("DROP TABLE old_versions")
    store.db.execute("PRAGMA user_version=6")
    before = table_digests(store.db)
    store.close()
    upgraded = Store(settings)
    try:
        assert table_digests(upgraded.db) == before
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert upgraded.db.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            upgraded.db.execute("INSERT INTO wb_entity_versions SELECT * FROM wb_entity_versions LIMIT 1")
    finally:
        upgraded.close()
    backup_path = next((settings.state / "backups").glob("state-v6-*.sqlite3"))
    with closing(sqlite3.connect(backup_path)) as backup:
        assert table_digests(backup) == before


@pytest.mark.parametrize("boundary", [0, 13, 25, 30, 40, "version"])
def test_migration_v4_interruption_atomic(settings, boundary):
    path = legacy_database(settings, complete=True, version=3)
    with closing(sqlite3.connect(path)) as db:
        before = snapshot(db)
    target = (
        f"PRAGMA user_version={db_migrations.SUPPORTED_VERSION}"
        if boundary == "version"
        else db_migrations.WORKBENCH_STATEMENTS[boundary]
    )

    class Fault(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == target:
                raise sqlite3.OperationalError("injected schema-4 interruption")
            return super().execute(sql, *args, **kwargs)

    with closing(sqlite3.connect(path, isolation_level=None, factory=Fault)) as db:
        with pytest.raises(db_migrations.DatabaseMigrationError, match="no partial"):
            db_migrations.initialize_database(db, path)
    with closing(sqlite3.connect(path)) as db:
        assert snapshot(db) == before
    store = Store(settings)
    store.close()


def test_empty_bootstrap_and_read_only_reopen(settings):
    path = settings.state / "router.sqlite3"
    store = Store(settings)
    try:
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == db_migrations.SUPPORTED_VERSION
        assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert store.db.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        assert store.db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert table_digests(store.db)["savings_revision"][0] == 1
    finally:
        store.close()
    statements = []
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.set_trace_callback(statements.append)
        db_migrations.initialize_database(db, path)
    assert not any(
        sql.upper().startswith(("CREATE", "ALTER", "INSERT", "UPDATE", "DELETE", "PRAGMA USER_VERSION="))
        for sql in statements
    )
    assert not (settings.state / "backups").exists()


def test_future_version_refused_without_writes(settings, monkeypatch):
    path = settings.state / "router.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE future_data(value TEXT)")
        db.execute("INSERT INTO future_data VALUES('keep me')")
        db.execute("PRAGMA user_version=99")
        db.commit()
    before_bytes = path.read_bytes()
    statements, connections = [], []
    original_connect = sqlite3.connect

    def connect(*args, **kwargs):
        db = original_connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        connections.append(db)
        return db

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(db_migrations.UnsupportedDatabaseVersion, match="UNSUPPORTED_DATABASE_VERSION.*99"):
        Store(settings)
    assert path.read_bytes() == before_bytes
    assert not any(
        sql.upper().startswith(("CREATE", "ALTER", "INSERT", "PRAGMA JOURNAL_MODE=", "PRAGMA USER_VERSION="))
        for sql in statements
    )
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    assert not (settings.state / "backups").exists()
    assert not path.with_name(path.name + ".migration.lock").exists()


@pytest.mark.parametrize("failure", [sqlite3.OperationalError("injected DDL failure"), KeyboardInterrupt()])
def test_interrupted_migration_rolls_back(settings, monkeypatch, failure):
    path = legacy_database(settings)
    with closing(sqlite3.connect(path)) as db:
        before = snapshot(db)
    connections = []
    original_connect = sqlite3.connect

    class InterruptedConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith("CREATE TABLE IF NOT EXISTS outcomes"):
                raise failure
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        if Path(args[0]) == path:
            kwargs["factory"] = InterruptedConnection
        db = original_connect(*args, **kwargs)
        connections.append(db)
        return db

    monkeypatch.setattr(sqlite3, "connect", connect)
    expected = KeyboardInterrupt if isinstance(failure, KeyboardInterrupt) else db_migrations.DatabaseMigrationError
    with pytest.raises(expected):
        Store(settings)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    with closing(original_connect(path)) as db:
        assert snapshot(db) == before
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    with closing(original_connect(next((settings.state / "backups").glob("*.sqlite3")))) as backup:
        assert snapshot(backup) == before
    monkeypatch.setattr(sqlite3, "connect", original_connect)
    store = Store(settings)
    store.close()


@pytest.mark.parametrize("failure_kind", ["backup", "verification", "directory"])
def test_failed_backup_keeps_original_database(settings, monkeypatch, failure_kind):
    path = legacy_database(settings)
    with closing(sqlite3.connect(path)) as db:
        before = snapshot(db)
    original_connect = sqlite3.connect
    connections = []

    class BackupFailureConnection(sqlite3.Connection):
        def backup(self, *args, **kwargs):
            raise sqlite3.OperationalError("injected backup failure")

    class BadBackupConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA quick_check":
                return super().execute("SELECT 'injected corrupt backup'")
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        if Path(args[0]) == path and failure_kind == "backup":
            kwargs["factory"] = BackupFailureConnection
        elif Path(args[0]) != path and failure_kind == "verification":
            kwargs["factory"] = BadBackupConnection
        db = original_connect(*args, **kwargs)
        connections.append(db)
        return db

    if failure_kind == "directory":
        (settings.state / "backups").write_text("unwritable directory fixture")
    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(db_migrations.DatabaseMigrationError, match="verified pre-migration backup"):
        Store(settings)
    for db in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            db.execute("SELECT 1")
    with closing(original_connect(path)) as db:
        assert snapshot(db) == before
    assert not list((settings.state / "backups").glob("*.sqlite3"))


def test_wal_backup_contains_committed_rows(settings):
    path = legacy_database(settings)
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute("PRAGMA foreign_keys=ON")
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO metadata VALUES('wal-only','committed')")
        assert Path(str(path) + "-wal").stat().st_size > 0
        writer.execute("BEGIN")
        writer.execute("INSERT INTO metadata VALUES('uncommitted','must not appear')")
        with closing(sqlite3.connect(path, isolation_level=None)) as reader:
            reader.execute("PRAGMA foreign_keys=ON")
            backup_path = db_migrations._create_verified_backup(reader, settings.state / "backups", 0)
        writer.execute("ROLLBACK")
    with closing(sqlite3.connect(backup_path)) as backup:
        assert backup.execute("SELECT value FROM metadata WHERE key='wal-only'").fetchone() == ("committed",)
        assert backup.execute("SELECT value FROM metadata WHERE key='uncommitted'").fetchone() is None
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("existing", [False, True])
def test_concurrent_bootstrap_and_foreign_keys(settings, tmp_path, existing):
    if existing:
        legacy_database(settings)
    script = """
import json, sys, time
from pathlib import Path
from local_ai_router.config import load
from local_ai_router.store import Store
from local_ai_router.skills import SkillRegistry
settings = load(sys.argv[1], mock=True)
Path(sys.argv[2]).write_text('ready')
while not Path(sys.argv[3]).exists():
    time.sleep(0.01)
store = Store(settings)
try:
    SkillRegistry(store)
    print(json.dumps([store.db.execute('PRAGMA user_version').fetchone()[0],
                      store.db.execute('PRAGMA foreign_keys').fetchone()[0],
                      store.db.execute('SELECT value FROM savings_revision').fetchone()[0]]))
finally:
    store.close()
"""
    processes = []
    go = tmp_path / "go"
    try:
        for number in range(2):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", script, str(settings.home), str(tmp_path / f"ready-{number}"), str(go)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        import time

        deadline = time.monotonic() + 20
        while not all((tmp_path / f"ready-{number}").exists() for number in range(2)):
            assert time.monotonic() < deadline, "bootstrap processes failed to become ready"
            time.sleep(0.01)
        go.write_text("start")
        for process in processes:
            out, err = process.communicate(timeout=25)
            assert process.returncode == 0, err
            assert json.loads(out) == [db_migrations.SUPPORTED_VERSION, 1, 0]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()
    assert len(list((settings.state / "backups").glob("*.sqlite3"))) == int(existing)
    stores = [Store(settings), Store(settings)]
    try:
        stores[0].db.execute("CREATE TABLE fk_probe(cache_key TEXT REFERENCES cache(key))")
        for store in stores:
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                store.db.execute("INSERT INTO fk_probe VALUES('missing-cache-key')")
    finally:
        for store in stores:
            store.close()


@pytest.mark.parametrize("layout", ["unknown", "malformed", "incomplete", "corrupt"])
def test_invalid_database_is_refused_without_replacement(settings, layout):
    path = settings.state / "router.sqlite3"
    if layout == "corrupt":
        path.write_bytes(b"not a SQLite database")
    else:
        with closing(sqlite3.connect(path)) as db:
            if layout == "unknown":
                db.execute("CREATE TABLE unrelated(important TEXT)")
            else:
                db.execute("CREATE TABLE calls(id TEXT)")
            if layout == "incomplete":
                db.execute("PRAGMA user_version=3")
            db.commit()
    before = path.read_bytes()
    with pytest.raises(db_migrations.DatabaseMigrationError):
        Store(settings)
    assert path.read_bytes() == before
    assert not (settings.state / "backups").exists()


def test_migration_lock_timeout_is_actionable(settings, monkeypatch):
    path = settings.state / "router.sqlite3"
    with db_migrations._migration_lock(path):
        monkeypatch.setattr(db_migrations, "MIGRATION_LOCK_TIMEOUT", 0.05)
        with pytest.raises(db_migrations.DatabaseMigrationError, match="Timed out.*migration lock"):
            Store(settings)
    store = Store(settings)
    store.close()


def test_database_busy_does_not_commit_upgrade(settings):
    path = legacy_database(settings)
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        before = snapshot(db)
        db.execute("BEGIN IMMEDIATE")
        with closing(sqlite3.connect(path, timeout=0.05, isolation_level=None)) as contender:

            def shorten_timeout(sql):
                if sql == "BEGIN IMMEDIATE":
                    contender.execute("PRAGMA busy_timeout=50")

            contender.set_trace_callback(shorten_timeout)
            with pytest.raises(db_migrations.DatabaseMigrationError, match="database is locked"):
                db_migrations.initialize_database(contender, path)
        db.execute("ROLLBACK")
        assert snapshot(db) == before


def test_future_schema_refused_through_cli_and_daemon(settings, monkeypatch, capsys):
    path = settings.state / "router.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA user_version=99")
    monkeypatch.setattr("local_ai_router.cli.load", lambda *_: settings)
    with pytest.raises(SystemExit) as error:
        main(["--mock", "costs", "--json"])
    assert error.value.code == 5
    assert "UNSUPPORTED_DATABASE_VERSION" in capsys.readouterr().err
    with pytest.raises(db_migrations.UnsupportedDatabaseVersion):
        create_app(settings)


async def test_future_schema_refused_through_mcp(settings):
    path = settings.state / "router.sqlite3"
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA user_version=99")
    mcp = create_mcp(settings)
    with pytest.raises(db_migrations.UnsupportedDatabaseVersion):
        async with mcp._mcp_server.lifespan(mcp._mcp_server):
            pytest.fail("unsupported database must not enter MCP lifespan")


@pytest.mark.parametrize("fault", ["foreign_keys", "quick_check", "foreign_key_check", "wal", "negative_version"])
def test_integrity_preconditions_refuse_migration(settings, fault):
    path = legacy_database(settings, complete=True, version=3)
    before_bytes = path.read_bytes()

    class FaultConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            replacements = {
                "foreign_keys": ("PRAGMA foreign_keys", "SELECT 0"),
                "quick_check": ("PRAGMA quick_check", "SELECT 'corrupt page'"),
                "foreign_key_check": ("PRAGMA foreign_key_check", "SELECT 'calls', 1, 'cache', 0"),
                "wal": ("PRAGMA journal_mode=WAL", "SELECT 'delete'"),
                "negative_version": ("PRAGMA user_version", "SELECT -1"),
            }
            target, replacement = replacements[fault]
            return super().execute(replacement if sql == target else sql, *args, **kwargs)

    with closing(sqlite3.connect(path, isolation_level=None, factory=FaultConnection)) as db:
        with pytest.raises(db_migrations.DatabaseMigrationError):
            db_migrations.initialize_database(db, path)
    assert path.read_bytes() == before_bytes


def test_posix_migration_lock_contention_and_release(tmp_path, monkeypatch):
    """Exercise the POSIX contract on Windows; actual OS matrix remains separate."""
    operations = []
    module = types.ModuleType("fcntl")
    module.LOCK_EX, module.LOCK_NB, module.LOCK_UN = 1, 2, 4
    contended = True

    def flock(fd, operation):
        nonlocal contended
        operations.append(operation)
        if operation == 3 and contended:
            contended = False
            raise BlockingIOError("lock held")

    module.flock = flock
    monkeypatch.setitem(sys.modules, "fcntl", module)
    monkeypatch.setattr(db_migrations, "os", SimpleNamespace(name="posix"))
    path = tmp_path / "lock-probe.sqlite3"
    with db_migrations._migration_lock(path):
        pass
    assert operations == [3, 3, 4]

    module.flock = lambda *_: (_ for _ in ()).throw(BlockingIOError("lock held"))
    monkeypatch.setattr(db_migrations, "MIGRATION_LOCK_TIMEOUT", 0)
    with pytest.raises(db_migrations.DatabaseMigrationError, match="Timed out"):
        with db_migrations._migration_lock(path):
            pytest.fail("contended lock must not enter")
