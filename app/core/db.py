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
from typing import Iterator, List

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

    # One-shot Daten-Cleanup: Items mit rarity="generic" auf "common"
    # migrieren (das alte "Generic = einfaerbbar"-Feature wurde entfernt).
    # Idempotent: laeuft nur solange noch generic-Items existieren.
    try:
        import json as _json
        rows = conn.execute("SELECT id, meta FROM items").fetchall()
        migrated = 0
        for iid, meta_raw in rows:
            try:
                meta = _json.loads(meta_raw or "{}")
            except Exception:
                continue
            if (meta.get("rarity") or "").strip().lower() == "generic":
                meta["rarity"] = "common"
                conn.execute("UPDATE items SET meta=? WHERE id=?",
                             (_json.dumps(meta, ensure_ascii=False), iid))
                migrated += 1
        if migrated:
            logger.info("items rarity-Migration: %d 'generic' -> 'common'",
                        migrated)
    except Exception as e:
        logger.warning("items rarity-Migration fehlgeschlagen: %s", e)

    # One-shot Daten-Cleanup: Lebensdauer-Feature (auto_expire_minutes/TTL)
    # wurde entfernt. Ueberbleibsel ``expires_at`` aus inventory_items.meta
    # rauspulen, sonst bleibt es ewig als toter String drin. Idempotent —
    # via schema_meta-Flag gegated, laeuft genau einmal pro Welt.
    flag = conn.execute(
        "SELECT value FROM schema_meta WHERE key='inventory_expires_at_purged'"
    ).fetchone()
    if not flag:
        try:
            import json as _json
            rows = conn.execute(
                "SELECT character_name, item_id, meta FROM inventory_items"
            ).fetchall()
            purged = 0
            for char_name, item_id, meta_raw in rows:
                try:
                    meta = _json.loads(meta_raw or "{}")
                except Exception:
                    continue
                if "expires_at" in meta:
                    meta.pop("expires_at", None)
                    conn.execute(
                        "UPDATE inventory_items SET meta=? "
                        "WHERE character_name=? AND item_id=?",
                        (_json.dumps(meta, ensure_ascii=False), char_name, item_id),
                    )
                    purged += 1
            if purged:
                logger.info("inventory_items expires_at-Cleanup: %d Eintraege bereinigt",
                            purged)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES "
                "('inventory_expires_at_purged', '1')"
            )
        except Exception as e:
            logger.warning("inventory_items expires_at-Cleanup fehlgeschlagen: %s", e)

    # One-shot Migration: summaries.UNIQUE-Constraint von 3 auf 4 Spalten
    # umstellen (zusaetzlich `partner`). SQLite kann Constraints nicht inline
    # aendern — Tabelle muss rebuilt werden. Idempotent via schema_meta-Flag.
    flag = conn.execute(
        "SELECT value FROM schema_meta WHERE key='summaries_partner_unique_v2'"
    ).fetchone()
    if not flag:
        try:
            # Pruefen ob die alte UNIQUE noch aktiv ist: SQLite legt sie als
            # sqlite_autoindex_summaries_* an. Wenn der hoechste Index nur 3
            # Spalten hat → migrieren.
            old_idx = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='summaries' "
                "AND name LIKE 'sqlite_autoindex_summaries_%'"
            ).fetchall()
            needs_rebuild = False
            for (idx_name,) in old_idx:
                cols = conn.execute(
                    f"PRAGMA index_info({idx_name})"
                ).fetchall()
                if len(cols) == 3:
                    needs_rebuild = True
                    break
            if needs_rebuild:
                logger.info("summaries: UNIQUE-Constraint wird auf 4 Spalten "
                            "umgebaut (partner ergaenzt)")
                conn.execute("ALTER TABLE summaries RENAME TO summaries_old_v1")
                conn.execute("""
                    CREATE TABLE summaries (
                        id             INTEGER PRIMARY KEY AUTOINCREMENT,
                        character_name TEXT NOT NULL,
                        kind           TEXT NOT NULL,
                        date_key       TEXT NOT NULL,
                        partner        TEXT NOT NULL DEFAULT '',
                        content        TEXT NOT NULL,
                        meta           TEXT DEFAULT '{}',
                        UNIQUE(character_name, kind, date_key, partner),
                        FOREIGN KEY(character_name) REFERENCES characters(name)
                            ON DELETE CASCADE
                    )
                """)
                conn.execute("""
                    INSERT INTO summaries
                    (id, character_name, kind, date_key, partner, content, meta)
                    SELECT id, character_name, kind, date_key,
                           COALESCE(partner, ''), content, COALESCE(meta, '{}')
                    FROM summaries_old_v1
                """)
                conn.execute("DROP TABLE summaries_old_v1")
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES "
                "('summaries_partner_unique_v2', '1')"
            )
        except Exception as e:
            logger.warning("summaries partner-Migration fehlgeschlagen: %s", e)

    # One-shot Backfill: known_locations-Feld auf jedem Character verpflichtend
    # machen. Frueher bedeutete "Feld fehlt" → unrestricted (Legacy-Bypass);
    # der Bypass ist raus und Read-Pfade liefern jetzt immer eine Liste.
    # Damit Bestands-Charaktere nach dem Bypass-Removal nicht ganz festsitzen,
    # seeden wir die Liste mit der aktuellen Location (falls real) — von dort
    # erweitert sich die Liste durch Auto-Discovery beim Betreten und durch
    # Discover-Regeln. Idempotent via schema_meta-Flag.
    flag = conn.execute(
        "SELECT value FROM schema_meta WHERE key='known_locations_backfill_v1'"
    ).fetchone()
    if not flag:
        try:
            import json as _json
            char_rows = conn.execute(
                "SELECT name, config_json FROM characters"
            ).fetchall()
            real_loc_ids = {
                r[0] for r in conn.execute("SELECT id FROM locations").fetchall()
            }
            state_locs = {
                r[0]: r[1] for r in conn.execute(
                    "SELECT character_name, current_location FROM character_state"
                ).fetchall()
            }
            backfilled = 0
            for char_name, conf_raw in char_rows:
                try:
                    conf = _json.loads(conf_raw or "{}")
                except Exception:
                    continue
                if "known_locations" in conf and isinstance(conf["known_locations"], list):
                    continue
                seed: List[str] = []
                cur_loc = (state_locs.get(char_name) or "").strip()
                if cur_loc and cur_loc in real_loc_ids:
                    seed.append(cur_loc)
                conf["known_locations"] = seed
                conn.execute(
                    "UPDATE characters SET config_json=?, updated_at=datetime('now') "
                    "WHERE name=?",
                    (_json.dumps(conf, ensure_ascii=False), char_name),
                )
                backfilled += 1
            if backfilled:
                logger.info("known_locations Backfill: %d Characters geseedet "
                            "(je current_location)", backfilled)
            conn.execute(
                "INSERT INTO schema_meta (key, value) VALUES "
                "('known_locations_backfill_v1', '1')"
            )
        except Exception as e:
            logger.warning("known_locations Backfill fehlgeschlagen: %s", e)

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

    # Default-Rules seeden — Sleep + Wake. Idempotent: nur fehlende werden
    # angelegt (Match by id), User-Aenderungen bleiben. Frueher waren
    # diese Pfade hardcoded im AgentLoop / hourly_tick — durch das Seed
    # sind frische Welten direkt funktional, bestehende Welten bekommen
    # die Defaults beim naechsten Restart nachgereicht.
    try:
        from app.models.rules import ensure_default_rules
        ensure_default_rules()
    except Exception as e:
        logger.warning("Default-Rules seeding fehlgeschlagen: %s", e)
