"""Account-Verwaltung (Login, Settings, Character-Auswahl).

Each world has exactly one account stored in the DB (account table, id=1).
Personal data (appearance, hobbies, etc.) lives in the character profile.
"""
from pathlib import Path
from typing import Dict, Any, List, Optional
import json
import bcrypt
from datetime import datetime

from app.core.paths import get_storage_dir, get_account_path
from app.core.db import get_connection, transaction


def _default_profile() -> Dict[str, Any]:
    return {
        "user_name": "",
        "password_hash": "",
        "system_language": "de",
        "translation_mode": "native",
        "default_character": "",
        "active_character": "",
    }


def get_user_profile() -> Dict[str, Any]:
    """Load the account profile from DB (Fallback: account.json)."""
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT user_name, password_hash, theme, settings FROM account WHERE id=1"
        ).fetchone()
        if row:
            profile = {}
            try:
                profile = json.loads(row[3] or "{}")
            except Exception:
                pass
            profile["user_name"] = row[0] or ""
            profile["password_hash"] = row[1] or ""
            if row[2]:
                profile["theme"] = row[2]
            return profile
    except Exception:
        pass

    # Fallback: JSON-Datei
    path = get_account_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass

    return _default_profile()


def save_user_profile(profile: Dict[str, Any]):
    """Save the account profile to DB and account.json backup."""
    # Settings-Blob: alle Keys ausser Standard-Felder
    settings = {k: v for k, v in profile.items()
                if k not in ("user_name", "password_hash", "theme")}
    now = datetime.now().isoformat()
    try:
        with transaction() as conn:
            conn.execute("""
                INSERT INTO account (id, user_name, password_hash, theme, settings, updated_at)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    user_name=excluded.user_name,
                    password_hash=excluded.password_hash,
                    theme=excluded.theme,
                    settings=excluded.settings,
                    updated_at=excluded.updated_at
            """, (
                profile.get("user_name", ""),
                profile.get("password_hash", ""),
                profile.get("theme", ""),
                json.dumps(settings, ensure_ascii=False),
                now,
            ))
    except Exception as e:
        from app.core.log import get_logger
        get_logger("account").error("save_user_profile DB-Fehler: %s", e)


def get_user_name() -> str:
    return get_user_profile().get("user_name", "")


def save_user_name(name: str):
    profile = get_user_profile()
    profile["user_name"] = name
    save_user_profile(profile)


def _build_language_name_map() -> dict:
    """Reads language code -> English name from shared/config/languages.json."""
    try:
        from app.core.paths import get_config_dir
        path = get_config_dir() / "languages.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return {
                opt["value"]: opt["label"]
                for opt in data.get("languages", [])
                if "value" in opt and "label" in opt
            }
    except Exception:
        pass
    from app.models.character import LANGUAGE_MAP
    return LANGUAGE_MAP


def get_user_language_instruction() -> str:
    """Derives the language instruction from system_language + translation_mode."""
    profile = get_user_profile()
    lang = profile.get("system_language", "de") or "de"
    mode = profile.get("translation_mode", "native") or "native"

    if mode == "translate":
        return ""
    lang_name = _build_language_name_map().get(lang, lang)
    return f"Always respond in {lang_name}."


def _current_user_settings() -> Optional[Dict[str, Any]]:
    """Settings-Dict des aktuell eingeloggten Users (aus Middleware-Context).
    None wenn kein Request-Context (Background-Task)."""
    try:
        from app.core.auth_dependency import get_current_user_from_ctx
        user = get_current_user_from_ctx()
        return user.get("settings", {}) if user else None
    except Exception:
        return None


def _update_current_user_settings(updates: Dict[str, Any]) -> bool:
    """Aktualisiert settings des aktuellen Users. True wenn gespeichert."""
    try:
        from app.core.auth_dependency import get_current_user_from_ctx
        from app.core.users import update_user
        user = get_current_user_from_ctx()
        if not user:
            return False
        settings = dict(user.get("settings") or {})
        settings.update(updates)
        update_user(user["id"], settings=settings)
        # Context-Cache aktualisieren damit nachfolgende Reads die Aenderung sehen
        user["settings"] = settings
        return True
    except Exception:
        return False


def get_active_character() -> str:
    """Return the character currently controlled by this player.

    Fuer eingeloggte User: aus user.settings.active_character.
    Fallback (Background / kein Request): aus account-Profile.
    """
    us = _current_user_settings()
    if us is not None:
        ac = us.get("active_character") or us.get("current_character") or ""
        if ac:
            return ac
        # Fallback fuer User ohne gesetzten active_character: erster allowed
        try:
            from app.core.auth_dependency import get_current_user_from_ctx
            user = get_current_user_from_ctx()
            allowed = (user or {}).get("allowed_characters") or []
            if allowed:
                return allowed[0]
        except Exception:
            pass

    profile = get_user_profile()
    return (
        profile.get("active_character")
        or profile.get("current_character")
        or ""
    )


def set_active_character(character_name: str):
    """Set which character this player controls."""
    if _update_current_user_settings({
        "active_character": character_name,
        "default_character": character_name,
        "current_character": character_name,
    }):
        return

    # Fallback: Account (kein Request-Context)
    profile = get_user_profile()
    profile["active_character"] = character_name
    if character_name:
        profile["default_character"] = character_name
    profile["current_character"] = character_name
    save_user_profile(profile)


def get_default_character() -> str:
    """Return the character pre-selected at login."""
    us = _current_user_settings()
    if us is not None:
        dc = us.get("default_character") or us.get("current_character") or ""
        if dc:
            return dc
        try:
            from app.core.auth_dependency import get_current_user_from_ctx
            user = get_current_user_from_ctx()
            allowed = (user or {}).get("allowed_characters") or []
            if allowed:
                return allowed[0]
        except Exception:
            pass
    profile = get_user_profile()
    return (
        profile.get("default_character")
        or profile.get("current_character")
        or ""
    )


get_current_character = get_active_character
set_current_character = set_active_character
get_current_agent = get_active_character
set_current_agent = set_active_character


def get_chat_partner() -> str:
    """Der Character mit dem der aktuelle User chattet (Chat-Agent).

    Per-User via Middleware-ContextVar. Fallback fuer Background-Tasks:
    Legacy _chat_partner.txt im Welt-Verzeichnis.
    """
    us = _current_user_settings()
    if us is not None:
        return us.get("chat_partner", "") or ""

    # Fallback fuer Background / kein Request-Context
    try:
        cp = get_storage_dir() / "_chat_partner.txt"
        if cp.exists():
            return cp.read_text().strip()
    except Exception:
        pass
    return ""


def set_chat_partner(character_name: str) -> None:
    """Setzt den Chat-Partner fuer den aktuellen User."""
    if _update_current_user_settings({"chat_partner": character_name}):
        return

    # Fallback: Legacy-File
    try:
        cp = get_storage_dir() / "_chat_partner.txt"
        cp.write_text(character_name or "")
    except Exception:
        pass


def is_player_controlled(character_name: str) -> bool:
    """Check whether *character_name* is currently steered by a human player."""
    return get_active_character() == character_name


def get_user_gender() -> str:
    """Return the gender of the player's active character (fallback: account profile)."""
    active = get_active_character()
    if active:
        try:
            from app.models.character import get_character_profile
            g = get_character_profile(active).get("gender", "")
            if g:
                return g
        except Exception:
            pass
    return get_user_profile().get("gender", "")


# --- Passwort-Verwaltung ---

def hash_password(password: str) -> str:
    """Hasht ein Passwort mit bcrypt"""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))
    except Exception:
        return False


def set_user_password(password: str):
    profile = get_user_profile()
    profile["password_hash"] = hash_password(password)
    save_user_profile(profile)


def check_user_password(password: str) -> bool:
    profile = get_user_profile()
    password_hash = profile.get("password_hash", "")
    if not password_hash:
        return False
    return verify_password(password, password_hash)


# --- Theme Preference ---

def get_user_theme() -> str:
    import os
    theme = get_user_profile().get("theme", "")
    if not theme:
        theme = os.getenv("DEFAULT_THEME", "default")
    return theme


def save_user_theme(theme: str):
    profile = get_user_profile()
    profile["theme"] = theme
    save_user_profile(profile)


def get_user_images_dir() -> Path:
    """Return the images directory of the player's active character."""
    active = get_active_character()
    if active:
        try:
            from app.models.character import get_character_images_dir
            return get_character_images_dir(active)
        except Exception:
            pass
    images_dir = get_storage_dir() / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    return images_dir


def get_user_profile_image() -> str:
    active = get_active_character()
    if active:
        try:
            from app.models.character import get_character_profile_image
            img = get_character_profile_image(active)
            if img:
                return img
        except Exception:
            pass
    return get_user_profile().get("profile_image", "")


def get_user_appearance() -> str:
    """Return the appearance of the player's active character."""
    active = get_active_character()
    if active:
        try:
            from app.models.character import get_character_profile
            char_profile = get_character_profile(active)
            appearance = char_profile.get("character_appearance", "")
            if appearance:
                if "{" in appearance:
                    from app.models.character_template import resolve_profile_tokens, get_template
                    template = get_template(char_profile.get("template", "human-default"))
                    return resolve_profile_tokens(
                        appearance, char_profile, template=template,
                        target_key="character_appearance",
                    )
                return appearance
        except Exception:
            pass

    return get_user_profile().get("user_appearance", "")
