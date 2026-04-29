"""Session-Verwaltung (Multiuser Phase 1).

Server-side Sessions mit opaken Tokens. Tokens liegen in der DB, der Browser
bekommt nur das Token als HttpOnly-Cookie.

TTL: Sliding Expiration — jede Aktivitaet verlaengert um SESSION_TTL_HOURS.
"""
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any

from app.core.db import get_connection, transaction
from app.core.log import get_logger

logger = get_logger("sessions")


SESSION_COOKIE_NAME = "av_session"
SESSION_TTL_HOURS = 24


def _now() -> datetime:
    return datetime.now()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def create_session(user_id: str) -> str:
    """Legt eine neue Session an und gibt das Token zurueck."""
    token = secrets.token_urlsafe(32)
    now = _now()
    expires = now + timedelta(hours=SESSION_TTL_HOURS)
    with transaction() as conn:
        conn.execute(
            "INSERT INTO user_sessions (token, user_id, created_at, expires_at, "
            "last_activity) VALUES (?, ?, ?, ?, ?)",
            (token, user_id, _iso(now), _iso(expires), _iso(now)),
        )
    return token


def get_session(token: str) -> Optional[Dict[str, Any]]:
    """Laedt die Session wenn Token gueltig. Verlaengert TTL (sliding)."""
    if not token:
        return None
    conn = get_connection()
    row = conn.execute(
        "SELECT token, user_id, created_at, expires_at, last_activity "
        "FROM user_sessions WHERE token=?",
        (token,),
    ).fetchone()
    if not row:
        return None

    now = _now()
    expires = datetime.fromisoformat(row["expires_at"])
    if now >= expires:
        # abgelaufen — aufraeumen
        delete_session(token)
        return None

    # Sliding: bei jedem Zugriff TTL verlaengern
    new_expires = now + timedelta(hours=SESSION_TTL_HOURS)
    with transaction() as conn:
        conn.execute(
            "UPDATE user_sessions SET last_activity=?, expires_at=? WHERE token=?",
            (_iso(now), _iso(new_expires), token),
        )
    return dict(row)


def delete_session(token: str) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM user_sessions WHERE token=?", (token,))


def delete_sessions_for_user(user_id: str) -> None:
    """Kickt alle Sessions eines Users (z.B. nach Passwort-Change)."""
    with transaction() as conn:
        conn.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))


