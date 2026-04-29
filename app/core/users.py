"""User-Verwaltung (Multiuser Phase 1).

Eine User-Tabelle pro Welt. Rollen: admin | user.
Admin hat vollen Zugriff, user ist beschraenkt auf zugeordnete Characters.
"""
import json
import uuid
from datetime import datetime
from typing import Dict, Any, List, Optional

import bcrypt

from app.core.db import get_connection, transaction
from app.core.log import get_logger

logger = get_logger("users")


ROLE_ADMIN = "admin"
ROLE_USER = "user"
VALID_ROLES = {ROLE_ADMIN, ROLE_USER}

DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin1234"
MIN_PASSWORD_LENGTH = 8


def _now_iso() -> str:
    return datetime.now().isoformat()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def _row_to_user(row) -> Dict[str, Any]:
    d = dict(row)
    try:
        d["allowed_characters"] = json.loads(d.get("allowed_characters") or "[]")
    except Exception:
        d["allowed_characters"] = []
    try:
        d["settings"] = json.loads(d.get("settings") or "{}")
    except Exception:
        d["settings"] = {}
    d.pop("password_hash", None)
    return d


def list_users() -> List[Dict[str, Any]]:
    """Listet alle User (ohne password_hash)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, username, role, allowed_characters, theme, settings, "
        "created_at, last_login FROM users ORDER BY created_at ASC"
    ).fetchall()
    return [_row_to_user(r) for r in rows]


def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    row = conn.execute(
        "SELECT id, username, role, allowed_characters, theme, settings, "
        "created_at, last_login FROM users WHERE id=?",
        (user_id,),
    ).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    row = conn.execute(
        "SELECT id, username, role, allowed_characters, theme, settings, "
        "created_at, last_login FROM users WHERE username=?",
        (username,),
    ).fetchone()
    return _row_to_user(row) if row else None


def _get_password_hash(user_id: str) -> str:
    conn = get_connection()
    row = conn.execute(
        "SELECT password_hash FROM users WHERE id=?", (user_id,)
    ).fetchone()
    return row["password_hash"] if row else ""


def check_user_password(username: str, password: str) -> Optional[Dict[str, Any]]:
    """Prueft username+password. Returns User-Dict bei Erfolg, None bei Fehler."""
    user = get_user_by_username(username)
    if not user:
        return None
    pwd_hash = _get_password_hash(user["id"])
    if not verify_password(password, pwd_hash):
        return None
    return user


def create_user(
    username: str,
    password: str,
    role: str = ROLE_USER,
    allowed_characters: Optional[List[str]] = None,
) -> str:
    """Legt einen neuen User an. Returns user_id."""
    if role not in VALID_ROLES:
        raise ValueError(f"Invalid role: {role}")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if get_user_by_username(username):
        raise ValueError(f"Username already exists: {username}")

    user_id = f"u_{uuid.uuid4().hex[:12]}"
    now = _now_iso()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO users (id, username, password_hash, role, "
            "allowed_characters, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                user_id, username, hash_password(password), role,
                json.dumps(allowed_characters or [], ensure_ascii=False),
                now,
            ),
        )
    logger.info("User angelegt: %s (role=%s)", username, role)
    return user_id


def update_user(user_id: str, **fields) -> bool:
    """Updates user fields. Supports: username, role, allowed_characters, theme, settings."""
    allowed = {"username", "role", "allowed_characters", "theme", "settings"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    if "role" in updates and updates["role"] not in VALID_ROLES:
        raise ValueError(f"Invalid role: {updates['role']}")
    if "allowed_characters" in updates:
        updates["allowed_characters"] = json.dumps(
            updates["allowed_characters"], ensure_ascii=False
        )
    if "settings" in updates:
        updates["settings"] = json.dumps(updates["settings"], ensure_ascii=False)

    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [user_id]
    with transaction() as conn:
        cur = conn.execute(f"UPDATE users SET {set_clause} WHERE id=?", values)
        return cur.rowcount > 0


def set_user_password(user_id: str, password: str) -> bool:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash=? WHERE id=?",
            (hash_password(password), user_id),
        )
        return cur.rowcount > 0


def delete_user(user_id: str) -> bool:
    with transaction() as conn:
        cur = conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        return cur.rowcount > 0


def touch_last_login(user_id: str) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE users SET last_login=? WHERE id=?", (_now_iso(), user_id)
        )


def ensure_default_admin() -> None:
    """Legt den Default-Admin an falls noch kein User existiert."""
    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
    if count > 0:
        return

    # Migration: alten account.user_name + password_hash uebernehmen falls vorhanden
    acc = conn.execute(
        "SELECT user_name, password_hash, theme, settings FROM account WHERE id=1"
    ).fetchone()
    if acc and acc["user_name"] and acc["password_hash"]:
        user_id = f"u_{uuid.uuid4().hex[:12]}"
        with transaction() as c:
            c.execute(
                "INSERT INTO users (id, username, password_hash, role, "
                "allowed_characters, theme, settings, created_at) "
                "VALUES (?, ?, ?, 'admin', '[]', ?, ?, ?)",
                (
                    user_id, acc["user_name"], acc["password_hash"],
                    acc["theme"] or "", acc["settings"] or "{}", _now_iso(),
                ),
            )
        logger.info("Default-Admin aus account.json migriert: %s", acc["user_name"])
        return

    # Frische Welt: Default-Admin mit Default-Passwort
    create_user(DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD, role=ROLE_ADMIN)
    logger.warning(
        "Default-Admin angelegt: username='%s' password='%s' — "
        "bitte im Admin-UI aendern!",
        DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD,
    )
