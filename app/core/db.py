"""Zentrale SQLite-DB-Zugriffsschicht fuer die Welt-Daten.

Eine DB pro Welt: `{storage_dir}/world.db`
WAL-Mode, JSON1-Extension, Foreign Keys aktiviert.

Alle Welt-Daten (Runtime + Content) liegen hier drin. Ausnahmen:
- Shared-Templates bleiben JSON unter `shared/`
- Image-Dateien + ihre Sidecar-JSONs bleiben auf Disk (Debugging)
- Task-Queue hat eigene `task_queue.db` (Legacy, bleibt getrennt)
"""
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.log import get_logger
from app.core.paths import get_storage_dir

logger = get_logger("db")

_connections: dict = {}
_lock = threading.Lock()


def get_db_path() -> Path:
    return get_storage_dir() / "world.db"


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")


def get_connection() -> sqlite3.Connection:
    """Thread-lokale Connection (SQLite benoetigt pro Thread eine eigene)."""
    tid = threading.get_ident()
    conn = _connections.get(tid)
    if conn is None:
        with _lock:
            conn = _connections.get(tid)
            if conn is None:
                db_path = get_db_path()
                db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(str(db_path), timeout=10, check_same_thread=False)
                _configure(conn)
                _connections[tid] = conn
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Kontextmanager fuer atomare Writes."""
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise




def init_schema() -> None:
    """Fuehrt alle Schema-Create-Statements aus (idempotent)."""
    import sqlite3
    from app.core.world_db_schema import (
        ALTER_MIGRATIONS, POST_MIGRATION_STATEMENTS,
        SCHEMA_STATEMENTS, SCHEMA_VERSION,
    )

    conn = get_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta ("
        "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    current = conn.execute(
        "SELECT value FROM schema_meta WHERE key='version'"
    ).fetchone()
    current_version = int(current["value"]) if current else 0

    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)

    for table, column, typedef in ALTER_MIGRATIONS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")
        except sqlite3.OperationalError:
            pass

    # Indizes auf migrierte Spalten erst nach ALTERs anlegen.
    for stmt in POST_MIGRATION_STATEMENTS:
        conn.execute(stmt)

    conn.execute(
        "INSERT INTO schema_meta (key, value) VALUES ('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),)
    )
    conn.commit()
    if current_version != SCHEMA_VERSION:
        logger.info(
            "World-DB Schema initialisiert: version %d (war %d)",
            SCHEMA_VERSION, current_version
        )
