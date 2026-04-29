"""Character Secrets - Geheimnisse pro Character.

Speichert Geheimnisse die ein Character hat oder ueber andere kennt.
Werden in den System-Prompt injiziert (eigene Geheimnisse + bekanntes Wissen).

Storage: world.db Tabelle `secrets` (pro Welt).
"""
import json
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.log import get_logger
from app.core.db import get_connection, transaction

logger = get_logger("secrets")

SEVERITY_LABELS = {
    1: "harmless",
    2: "embarrassing",
    3: "serious",
    4: "dangerous",
    5: "devastating",
}

VALID_CATEGORIES = ("personal", "relationship", "location", "criminal")
VALID_SOURCES = ("manual", "generated", "discovered", "event", "conversation")


def _row_to_secret(row) -> Dict[str, Any]:
    """Konvertiert eine DB-Zeile in ein Secret-Dict.
    Schema: (id INTEGER, character_name, content, visibility, meta)
    """
    meta = {}
    try:
        meta = json.loads(row[4] or "{}")
    except Exception:
        pass
    # meta haelt das komplette originale Secret-Dict
    if not meta:
        # Minimaler Fallback
        meta = {
            "id": f"sec_{row[0]}",
            "content": row[2] or "",
            "category": "personal",
            "severity": 2,
        }
    return meta


def _load_secrets(character_name: str) -> List[Dict[str, Any]]:
    """Laedt alle Geheimnisse eines Characters aus der DB."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT id, character_name, content, visibility, meta "
            "FROM secrets WHERE character_name=? ORDER BY id ASC",
            (character_name,),
        ).fetchall()
        return [_row_to_secret(r) for r in rows]
    except Exception as e:
        logger.error("_load_secrets DB-Fehler fuer %s: %s", character_name, e)
        return []


def _save_secrets(character_name: str, secrets: List[Dict[str, Any]]):
    """Speichert alle Geheimnisse eines Characters in die DB."""
    try:
        with transaction() as conn:
            # Vorhandene Zeilen laden: db_id -> str_id
            existing_rows = conn.execute(
                "SELECT id, meta FROM secrets WHERE character_name=?",
                (character_name,),
            ).fetchall()
            db_to_str: Dict[int, str] = {}
            str_to_db: Dict[str, int] = {}
            for db_id, meta_str in existing_rows:
                try:
                    m = json.loads(meta_str or "{}")
                    str_id = m.get("id", f"sec_{db_id}")
                except Exception:
                    str_id = f"sec_{db_id}"
                db_to_str[db_id] = str_id
                str_to_db[str_id] = db_id

            new_str_ids = {s.get("id") for s in secrets if s.get("id")}

            # Geloeschte entfernen
            for db_id, str_id in db_to_str.items():
                if str_id not in new_str_ids:
                    conn.execute("DELETE FROM secrets WHERE id=?", (db_id,))

            # Upsert
            for secret in secrets:
                str_id = secret.get("id")
                if not str_id:
                    continue
                visibility = json.dumps({
                    "known_by": secret.get("known_by", []),
                    "discovered_by": secret.get("discovered_by", []),
                }, ensure_ascii=False)
                meta_str = json.dumps(secret, ensure_ascii=False)

                if str_id in str_to_db:
                    conn.execute(
                        "UPDATE secrets SET content=?, visibility=?, meta=? WHERE id=?",
                        (secret.get("content", ""), visibility, meta_str,
                         str_to_db[str_id]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO secrets (character_name, content, visibility, meta) "
                        "VALUES (?, ?, ?, ?)",
                        (character_name, secret.get("content", ""), visibility, meta_str),
                    )
    except Exception as e:
        logger.error("_save_secrets DB-Fehler fuer %s: %s", character_name, e)


def list_secrets(character_name: str) -> List[Dict[str, Any]]:
    """Listet alle Geheimnisse eines Characters."""
    return _load_secrets(character_name)


def get_secret(character_name: str, secret_id: str) -> Optional[Dict[str, Any]]:
    """Gibt ein einzelnes Geheimnis zurueck."""
    secrets = _load_secrets(character_name)
    for s in secrets:
        if s.get("id") == secret_id:
            return s
    return None


def add_secret(character_name: str,
    content: str,
    category: str = "personal",
    severity: int = 2,
    related_characters: Optional[List[str]] = None,
    related_location: Optional[str] = None,
    consequences_if_revealed: str = "",
    source: str = "manual",
    known_by: Optional[List[str]] = None) -> Dict[str, Any]:
    """Erstellt ein neues Geheimnis fuer einen Character."""
    if category not in VALID_CATEGORIES:
        category = "personal"
    severity = max(1, min(5, severity))
    if source not in VALID_SOURCES:
        source = "manual"

    secrets = _load_secrets(character_name)
    now = datetime.now().isoformat()

    secret = {
        "id": f"sec_{uuid.uuid4().hex[:8]}",
        "content": content.strip(),
        "category": category,
        "severity": severity,
        "known_by": known_by or [],
        "discovered_by": [],
        "created_at": now,
        "discovered_at": None,
        "source": source,
        "related_characters": related_characters or [],
        "related_location": related_location or None,
        "consequences_if_revealed": consequences_if_revealed.strip(),
    }

    secrets.append(secret)
    _save_secrets(character_name, secrets)
    logger.info("Secret erstellt: %s fuer %s (severity=%d, category=%s)",
                secret["id"], character_name, severity, category)
    return secret


def update_secret(character_name: str,
    secret_id: str,
    updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Aktualisiert ein bestehendes Geheimnis."""
    secrets = _load_secrets(character_name)
    for s in secrets:
        if s.get("id") == secret_id:
            # Erlaubte Felder aktualisieren
            allowed = {
                "content", "category", "severity", "known_by", "discovered_by",
                "related_characters", "related_location", "consequences_if_revealed",
                "discovered_at", "source",
            }
            for key, value in updates.items():
                if key in allowed:
                    s[key] = value
            # Validierung
            if s.get("category") not in VALID_CATEGORIES:
                s["category"] = "personal"
            s["severity"] = max(1, min(5, s.get("severity", 2)))
            _save_secrets(character_name, secrets)
            logger.info("Secret aktualisiert: %s", secret_id)
            return s
    return None


def delete_secret(character_name: str, secret_id: str) -> bool:
    """Loescht ein Geheimnis."""
    secrets = _load_secrets(character_name)
    new_secrets = [s for s in secrets if s.get("id") != secret_id]
    if len(new_secrets) < len(secrets):
        _save_secrets(character_name, new_secrets)
        logger.info("Secret geloescht: %s", secret_id)
        return True
    return False


def add_known_by(character_name: str,
    secret_id: str,
    knower_name: str,
    discovered: bool = False) -> bool:
    """Fuegt einen Character zur known_by-Liste eines Geheimnisses hinzu.

    discovered=True: Character hat es selbst herausgefunden (nicht erzaehlt bekommen).
    """
    secrets = _load_secrets(character_name)
    for s in secrets:
        if s.get("id") == secret_id:
            known_by = s.get("known_by", [])
            if knower_name not in known_by:
                known_by.append(knower_name)
                s["known_by"] = known_by
            if discovered:
                discovered_by = s.get("discovered_by", [])
                if knower_name not in discovered_by:
                    discovered_by.append(knower_name)
                    s["discovered_by"] = discovered_by
                if not s.get("discovered_at"):
                    s["discovered_at"] = datetime.now().isoformat()
            _save_secrets(character_name, secrets)
            logger.info("Secret %s: %s weiss jetzt davon (discovered=%s)",
                        secret_id, knower_name, discovered)
            return True
    return False


# --- Secrets fuer andere Characters (was dieser Character ueber andere weiss) ---

def get_known_secrets_about(observer_name: str) -> List[Dict[str, Any]]:
    """Gibt alle Geheimnisse zurueck die observer_name ueber ANDERE Characters kennt.

    Sucht in den Secrets aller Characters nach Eintraegen wo observer_name
    in known_by steht.
    """
    from app.models.character import list_available_characters

    known = []
    all_chars = list_available_characters()
    for char_name in all_chars:
        if char_name == observer_name:
            continue
        secrets = _load_secrets(char_name)
        for s in secrets:
            if observer_name in s.get("known_by", []):
                known.append({
                    "secret_owner": char_name,
                    "secret": s,
                })
    return known


# --- Prompt-Injection ---

def build_secrets_prompt_section(character_name: str) -> str:
    """Baut den Secrets-Abschnitt fuer den System-Prompt.

    Zwei Teile:
    1. Eigene Geheimnisse des Characters
    2. Was der Character ueber andere weiss
    """
    own_secrets = list_secrets(character_name)
    known_about_others = get_known_secrets_about(character_name)

    if not own_secrets and not known_about_others:
        return ""

    parts = []

    # Eigene Geheimnisse
    if own_secrets:
        lines = []
        for s in own_secrets:
            content = (s.get("content") or "").strip()
            if not content:
                continue
            severity_label = SEVERITY_LABELS.get(s.get("severity", 2), "unknown")
            line = f"- {content} (severity: {severity_label})"
            if s.get("consequences_if_revealed"):
                line += f" — if revealed: {s['consequences_if_revealed']}"
            known_count = len(s.get("known_by", []))
            if known_count > 0:
                knowers = ", ".join(s["known_by"])
                line += f" [known by: {knowers}]"
            lines.append(line)

        parts.append(
            "\nYour secrets (things you keep to yourself — never reveal these unless you choose to):\n"
            + "\n".join(lines)
        )

    # Was dieser Character ueber andere weiss
    if known_about_others:
        lines = []
        for entry in known_about_others:
            owner = entry["secret_owner"]
            s = entry["secret"]
            content = (s.get("content") or "").strip()
            if not content:
                continue
            discovered = character_name in s.get("discovered_by", [])
            method = "you discovered this yourself" if discovered else "someone told you"
            sev = s.get("severity", 2)
            lines.append(f"- About {owner}: {content} ({method}, severity {sev}/5)")

        parts.append(
            "\nSecrets you know about others:\n"
            + "\n".join(lines)
            + "\n\nWhen someone asks directly about one of these people, consider sharing what you know — "
            + "BUT only if: (a) you trust them more than the secret's owner, OR (b) the severity is low, "
            + "OR (c) you have reason to betray the owner (bad relationship, recent conflict). "
            + "Otherwise deflect, lie, or partially reveal. Never volunteer high-severity secrets unprompted."
        )

    return "\n".join(parts)
