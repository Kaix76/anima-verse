"""Schema-Definitionen fuer die Welt-DB.

Alle CREATE TABLE / CREATE INDEX Statements zentral verwaltet. Migrations-
Philosophie: idempotent, IF NOT EXISTS ueberall — Schema-Aenderungen laufen
bei jedem init_schema() durch.

Konventionen:
- Primary Keys als explizites PK, keine ROWID-Abhaengigkeiten
- Timestamps als ISO-Strings (TEXT) fuer Sort-Ordering
- Komplexe/variable Strukturen als JSON-Blobs (TEXT + json_valid Check)
- IDs fuer Welt-Items mit Praefix `w_`, Shared mit `s_` (Shared bleibt JSON)
- Foreign Keys: AN, ON DELETE CASCADE wo sinnvoll
"""

SCHEMA_VERSION = 4


SCHEMA_STATEMENTS = [
    # ── Kern / Welt ────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS world_kv (
        key   TEXT PRIMARY KEY,
        value TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS locations (
        id               TEXT PRIMARY KEY,
        name             TEXT NOT NULL,
        description      TEXT DEFAULT '',
        grid_x           INTEGER,
        grid_y           INTEGER,
        outfit_type      TEXT DEFAULT '',
        image_prompt_day TEXT DEFAULT '',
        image_prompt_night TEXT DEFAULT '',
        image_prompt_map TEXT DEFAULT '',
        visible_when     TEXT DEFAULT '[]',
        accessible_when  TEXT DEFAULT '[]',
        background_images TEXT DEFAULT '[]',
        meta             TEXT DEFAULT '{}',
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS rooms (
        id           TEXT PRIMARY KEY,
        location_id  TEXT NOT NULL,
        name         TEXT NOT NULL,
        outfit_type  TEXT DEFAULT '',
        meta         TEXT DEFAULT '{}',
        FOREIGN KEY(location_id) REFERENCES locations(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS rules (
        id       TEXT PRIMARY KEY,
        text     TEXT NOT NULL,
        category TEXT DEFAULT '',
        meta     TEXT DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS status_modifiers (
        id     TEXT PRIMARY KEY,
        name   TEXT NOT NULL,
        effect TEXT DEFAULT '{}'
    )""",
    # Prompt-Filter: pro Zustand (drunk, exhausted, ...) wird definiert
    # welche Bloecke aus dem Thought-Prompt rausfliegen + welcher Modifier-
    # Text dem LLM stattdessen gezeigt wird. Ueberschreibt by id den
    # gleichnamigen Eintrag aus shared/prompt_filters/filters.json.
    """CREATE TABLE IF NOT EXISTS prompt_filters (
        id              TEXT PRIMARY KEY,
        condition       TEXT NOT NULL,
        label           TEXT DEFAULT '',
        drop_blocks     TEXT DEFAULT '[]',
        prompt_modifier TEXT DEFAULT '',
        enabled         INTEGER NOT NULL DEFAULT 1,
        meta            TEXT DEFAULT '{}',
        icon            TEXT DEFAULT '',
        image_modifier  TEXT DEFAULT ''
    )""",

    # ── Characters ─────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS characters (
        name         TEXT PRIMARY KEY,
        template     TEXT DEFAULT '',
        profile_json TEXT DEFAULT '{}',   -- Stamm+Config als Blob (bewusst)
        config_json  TEXT DEFAULT '{}',   -- Stamm+Config als Blob (bewusst)
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS character_state (
        character_name    TEXT PRIMARY KEY,
        current_location  TEXT DEFAULT '',
        current_room      TEXT DEFAULT '',
        current_activity  TEXT DEFAULT '',
        current_feeling   TEXT DEFAULT '',
        location_changed_at TEXT DEFAULT '',
        activity_changed_at TEXT DEFAULT '',
        last_thought_at   TEXT DEFAULT '',
        meta              TEXT DEFAULT '{}',
        FOREIGN KEY(character_name) REFERENCES characters(name) ON DELETE CASCADE
    )""",

    # ── Chat-Historie ──────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS chat_messages (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        character_name TEXT NOT NULL,
        partner        TEXT NOT NULL,
        ts             TEXT NOT NULL,
        role           TEXT NOT NULL,          -- user | assistant | system
        content        TEXT NOT NULL,
        channel        TEXT DEFAULT 'web',
        channel_message_id TEXT DEFAULT NULL,
        metadata       TEXT DEFAULT '{}',
        FOREIGN KEY(character_name) REFERENCES characters(name) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chat_char_partner_ts ON chat_messages (character_name, partner, ts)",
    "CREATE INDEX IF NOT EXISTS idx_chat_ts ON chat_messages (ts)",

    # ── Memories / Summaries ───────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS memories (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        character_name TEXT NOT NULL,
        tier           TEXT NOT NULL,          -- episodic | daily | weekly | monthly | relationship | ...
        ts             TEXT NOT NULL,
        content        TEXT NOT NULL,
        source_ids     TEXT DEFAULT '[]',
        tags           TEXT DEFAULT '[]',
        meta           TEXT DEFAULT '{}',
        FOREIGN KEY(character_name) REFERENCES characters(name) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_memories_char_tier_ts ON memories (character_name, tier, ts)",
    """CREATE TABLE IF NOT EXISTS summaries (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        character_name TEXT NOT NULL,
        kind           TEXT NOT NULL,          -- daily | weekly | history
        date_key       TEXT NOT NULL,          -- YYYY-MM-DD oder ISO-Week
        partner        TEXT NOT NULL DEFAULT '', -- Konversationspartner (Charaktername) — leer fuer kind='history' (sliding window)
        content        TEXT NOT NULL,
        meta           TEXT DEFAULT '{}',
        UNIQUE(character_name, kind, date_key, partner),
        FOREIGN KEY(character_name) REFERENCES characters(name) ON DELETE CASCADE
    )""",

    # ── Histories (Zeit-Serien) ────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS mood_history (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        character_name TEXT NOT NULL,
        ts             TEXT NOT NULL,
        mood           TEXT NOT NULL,
        source         TEXT DEFAULT '',
        FOREIGN KEY(character_name) REFERENCES characters(name) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_mood_char_ts ON mood_history (character_name, ts)",
    """CREATE TABLE IF NOT EXISTS state_history (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        character_name TEXT NOT NULL,
        ts             TEXT NOT NULL,
        state_json     TEXT NOT NULL,
        FOREIGN KEY(character_name) REFERENCES characters(name) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_state_char_ts ON state_history (character_name, ts)",
    """CREATE TABLE IF NOT EXISTS evolution_history (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        character_name TEXT NOT NULL,
        ts             TEXT NOT NULL,
        field          TEXT NOT NULL,
        old_value      TEXT,
        new_value      TEXT,
        reason         TEXT DEFAULT '',
        FOREIGN KEY(character_name) REFERENCES characters(name) ON DELETE CASCADE
    )""",
    # social_dialog_history ist entfernt — Social-Dialog laeuft als forced_thought
    # ueber die normale Chat-History (plan-thoughts-and-conversation).
    """CREATE TABLE IF NOT EXISTS diary_entries (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        character_name TEXT NOT NULL,
        ts             TEXT NOT NULL,
        content        TEXT NOT NULL,
        tags           TEXT DEFAULT '[]',
        meta           TEXT DEFAULT '{}',
        FOREIGN KEY(character_name) REFERENCES characters(name) ON DELETE CASCADE
    )""",

    # ── Content (Welt-Level) ───────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS items (
        id            TEXT PRIMARY KEY,         -- w_... (Welt) / s_... (nur referenziert, liegt in shared)
        name          TEXT NOT NULL,
        category      TEXT DEFAULT '',
        prompt_fragment TEXT DEFAULT '',
        pieces        TEXT DEFAULT '{}',
        slots         TEXT DEFAULT '[]',
        meta          TEXT DEFAULT '{}',
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS activities (
        id           TEXT PRIMARY KEY,
        name         TEXT NOT NULL,
        category     TEXT DEFAULT '',
        meta         TEXT DEFAULT '{}'
    )""",
    """CREATE TABLE IF NOT EXISTS knowledge (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        character_name TEXT NOT NULL,
        topic          TEXT DEFAULT '',
        content        TEXT NOT NULL,
        tier           TEXT DEFAULT '',
        ts             TEXT NOT NULL,
        meta           TEXT DEFAULT '{}',
        FOREIGN KEY(character_name) REFERENCES characters(name) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_knowledge_char ON knowledge (character_name)",
    """CREATE TABLE IF NOT EXISTS secrets (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        character_name TEXT NOT NULL,
        content        TEXT NOT NULL,
        visibility     TEXT DEFAULT '{}',
        meta           TEXT DEFAULT '{}',
        FOREIGN KEY(character_name) REFERENCES characters(name) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS assignments (
        id             TEXT PRIMARY KEY,
        character_name TEXT,
        task           TEXT NOT NULL,
        status         TEXT DEFAULT 'open',
        due            TEXT DEFAULT '',
        meta           TEXT DEFAULT '{}',
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS stories (
        id             TEXT PRIMARY KEY,
        title          TEXT NOT NULL,
        content        TEXT DEFAULT '',
        character_name TEXT,
        meta           TEXT DEFAULT '{}',
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS story_arcs (
        id           TEXT PRIMARY KEY,
        title        TEXT NOT NULL,
        state        TEXT DEFAULT 'active',
        beats        TEXT DEFAULT '[]',
        participants TEXT DEFAULT '[]',
        meta         TEXT DEFAULT '{}',
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS outfits_sets (
        id             TEXT PRIMARY KEY,
        character_name TEXT NOT NULL,
        name           TEXT NOT NULL,
        pieces         TEXT DEFAULT '{}',
        image          TEXT DEFAULT '',
        meta           TEXT DEFAULT '{}',
        created_at     TEXT NOT NULL,
        FOREIGN KEY(character_name) REFERENCES characters(name) ON DELETE CASCADE
    )""",

    # ── Events / Notifications / Relations ─────────────────────────────
    """CREATE TABLE IF NOT EXISTS events (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        ts             TEXT NOT NULL,
        kind           TEXT NOT NULL,
        character_name TEXT,
        payload        TEXT DEFAULT '{}'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind, ts)",
    """CREATE TABLE IF NOT EXISTS notifications (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        ts     TEXT NOT NULL,
        kind   TEXT NOT NULL,
        title  TEXT DEFAULT '',
        body   TEXT DEFAULT '',
        read   INTEGER NOT NULL DEFAULT 0,
        meta   TEXT DEFAULT '{}'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_notifications_ts ON notifications (ts)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_read ON notifications (read, ts)",
    """CREATE TABLE IF NOT EXISTS relationships (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        from_char    TEXT NOT NULL,
        to_char      TEXT NOT NULL,
        content      TEXT NOT NULL,
        ts           TEXT NOT NULL,
        meta         TEXT DEFAULT '{}'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_relationships_pair ON relationships (from_char, to_char, ts)",
    """CREATE TABLE IF NOT EXISTS group_chats (
        id           TEXT PRIMARY KEY,
        participants TEXT DEFAULT '[]',
        messages     TEXT DEFAULT '[]',
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    )""",

    # ── Inventar ──────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS inventory_items (
        character_name TEXT NOT NULL,
        item_id        TEXT NOT NULL,
        quantity       INTEGER NOT NULL DEFAULT 1,
        acquired_at    TEXT NOT NULL,
        meta           TEXT DEFAULT '{}',
        PRIMARY KEY (character_name, item_id),
        FOREIGN KEY(character_name) REFERENCES characters(name) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS equipped_pieces (
        character_name TEXT NOT NULL,
        slot           TEXT NOT NULL,
        item_id        TEXT NOT NULL,
        color_meta     TEXT DEFAULT '{}',
        PRIMARY KEY (character_name, slot),
        FOREIGN KEY(character_name) REFERENCES characters(name) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS equipped_items (
        character_name TEXT NOT NULL,
        item_id        TEXT NOT NULL,
        PRIMARY KEY (character_name, item_id),
        FOREIGN KEY(character_name) REFERENCES characters(name) ON DELETE CASCADE
    )""",

    # ── Scheduler ──────────────────────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS scheduler_jobs (
        id             TEXT PRIMARY KEY,
        character_name TEXT,
        action         TEXT NOT NULL,
        trigger        TEXT NOT NULL,
        source         TEXT DEFAULT '',
        meta           TEXT DEFAULT '{}',
        created_at     TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_scheduler_char ON scheduler_jobs (character_name)",
    """CREATE TABLE IF NOT EXISTS scheduler_logs (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id  TEXT NOT NULL,
        ts      TEXT NOT NULL,
        status  TEXT NOT NULL,
        result  TEXT DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_scheduler_logs_job_ts ON scheduler_logs (job_id, ts)",
    """CREATE TABLE IF NOT EXISTS daily_schedules (
        character_name TEXT PRIMARY KEY,
        enabled        INTEGER NOT NULL DEFAULT 0,
        slots          TEXT DEFAULT '[]',
        meta           TEXT DEFAULT '{}'
    )""",

    # ── Telegram / Session / Account ───────────────────────────────────
    """CREATE TABLE IF NOT EXISTS telegram_mapping (
        chat_id        TEXT PRIMARY KEY,
        character_name TEXT NOT NULL,
        created_at     TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS account (
        id            INTEGER PRIMARY KEY CHECK (id = 1),    -- single-row
        user_name     TEXT NOT NULL DEFAULT 'admin',
        password_hash TEXT DEFAULT '',
        theme         TEXT DEFAULT '',
        settings      TEXT DEFAULT '{}',
        updated_at    TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS session_kv (
        key   TEXT PRIMARY KEY,
        value TEXT
    )""",

    # ── Image-Metadaten (redundant zum Sidecar-JSON auf Disk) ──────────
    """CREATE TABLE IF NOT EXISTS image_metadata (
        filename       TEXT PRIMARY KEY,
        character_name TEXT,
        directory      TEXT NOT NULL,          -- images | outfits | instagram | world_gallery | ...
        prompt         TEXT DEFAULT '',
        negative_prompt TEXT DEFAULT '',
        seed           INTEGER,
        backend        TEXT DEFAULT '',
        model          TEXT DEFAULT '',
        workflow       TEXT DEFAULT '',
        created_at     TEXT NOT NULL,
        sidecar_json   TEXT DEFAULT '{}'       -- voller Sidecar-Inhalt als Blob fuer Queries
    )""",
    "CREATE INDEX IF NOT EXISTS idx_image_char ON image_metadata (character_name, directory)",
    "CREATE INDEX IF NOT EXISTS idx_image_created ON image_metadata (created_at)",

    # ── Model-Capabilities (Cache) ─────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS model_capabilities (
        model        TEXT PRIMARY KEY,
        capabilities TEXT DEFAULT '{}'
    )""",

    # ── Multiuser / Auth (Phase 1) ─────────────────────────────────────
    """CREATE TABLE IF NOT EXISTS users (
        id                 TEXT PRIMARY KEY,          -- UUID
        username           TEXT NOT NULL UNIQUE,
        password_hash      TEXT NOT NULL,
        role               TEXT NOT NULL,             -- admin | user
        allowed_characters TEXT NOT NULL DEFAULT '[]',
        theme              TEXT DEFAULT '',
        settings           TEXT DEFAULT '{}',
        created_at         TEXT NOT NULL,
        last_login         TEXT DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_users_username ON users (username)",

    """CREATE TABLE IF NOT EXISTS user_sessions (
        token         TEXT PRIMARY KEY,               -- opaque session ID
        user_id       TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        expires_at    TEXT NOT NULL,
        last_activity TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON user_sessions (user_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_expires ON user_sessions (expires_at)",

    """CREATE TABLE IF NOT EXISTS character_locks (
        character_name TEXT PRIMARY KEY,
        user_id        TEXT NOT NULL,
        acquired_at    TEXT NOT NULL,
        last_activity  TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
    )""",

    # ── LLM Call Statistik (fuer Dauer-Schaetzung + Admin-Auswertung) ──
    """CREATE TABLE IF NOT EXISTS llm_call_stats (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          TEXT NOT NULL,
        model       TEXT NOT NULL,
        task        TEXT NOT NULL,
        provider    TEXT DEFAULT '',
        agent_name  TEXT DEFAULT '',
        in_tokens   INTEGER NOT NULL,
        out_tokens  INTEGER NOT NULL,
        max_tokens  INTEGER DEFAULT 0,
        duration_s  REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_llm_call_stats_lookup ON llm_call_stats (model, task, provider, ts DESC)",
]


# ALTER TABLE migrations — laufen nach allen CREATEs idempotent durch.
# Pattern: pro Tabelle die fehlenden Spalten hinzufuegen, OperationalError = existiert schon.
ALTER_MIGRATIONS = [
    # llm_call_stats: agent_name + max_tokens fuer Admin-Stats-Tab nachgezogen
    ("llm_call_stats", "agent_name", "TEXT DEFAULT ''"),
    ("llm_call_stats", "max_tokens", "INTEGER DEFAULT 0"),
    # character_state: last_thought_at fuer Agent-Loop / Inbox-Tracking
    # Zeitpunkt der letzten verarbeiteten Gedanken-Runde — alle chat_messages
    # mit role='user' und ts > last_thought_at gelten als "ungelesen".
    ("character_state", "last_thought_at", "TEXT DEFAULT ''"),
    # prompt_filters: icon + image_modifier fuer den verschmolzenen
    # "Zustaende"-Tab (frueher in status_modifiers.json). Icon wird im
    # Character-Header-Badge gerendert, image_modifier landet im
    # Bildgenerierungs-Prompt aktiver Conditions.
    ("prompt_filters", "icon", "TEXT DEFAULT ''"),
    ("prompt_filters", "image_modifier", "TEXT DEFAULT ''"),
    # summaries: partner-Spalte fuer Character-vs-Character Daily-Summaries
    # (eine Summary pro (character, partner) pro Tag statt eine pro Tag).
    # UNIQUE-Constraint-Wechsel laeuft separat in db.py (Table-Rebuild).
    ("summaries", "partner", "TEXT NOT NULL DEFAULT ''"),
]


# Indizes die auf migrierte Spalten zugreifen — laufen NACH ALTER_MIGRATIONS,
# sonst schlaegt CREATE INDEX auf alten DBs fehl ("no such column: agent_name").
POST_MIGRATION_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_llm_call_stats_agent_ts ON llm_call_stats (agent_name, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_llm_call_stats_ts ON llm_call_stats (ts DESC)",
]
