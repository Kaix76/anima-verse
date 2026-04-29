"""Rules Engine — Konfigurierbare Zugangs- und Zwangsregeln pro Welt.

Zwei Regeltypen:
- block: Verhindert Zugang zu Ort/Raum wenn Bedingung erfuellt
- force: Erzwingt Aktion (Ortswechsel + Aktivitaet) wenn Bedingung erfuellt

Regeln nutzen die gleiche Condition-Syntax wie Aktivitaeten
(stamina>20, courage<30, NOT alone AND night, etc.)

Storage: worlds/{world}/rules.json
"""
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.core.log import get_logger
from app.core.paths import get_storage_dir
from app.core.db import get_connection, transaction

logger = get_logger("rules")


def _get_rules_path() -> Path:
    return get_storage_dir() / "rules.json"


def load_rules() -> List[Dict[str, Any]]:
    """Laedt alle Regeln der aktuellen Welt aus der DB.

    Schema: (id TEXT PK, text, category, meta)
    Das komplette Rule-Dict wird im meta-Blob gespeichert.
    """
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT id, text, category, meta FROM rules ORDER BY rowid ASC"
        ).fetchall()
        rules = []
        for r in rows:
            meta = {}
            try:
                meta = json.loads(r[3] or "{}")
            except Exception:
                pass
            # id/name/type aus den Spalten in meta ergaenzen — meta kann
            # durch Teil-Saves verstuemmelt sein. Kritische Identifier immer
            # aus Spalten-Werten spiegeln.
            meta.setdefault("id", r[0])
            if r[1] and not (meta.get("name") or meta.get("message")):
                meta["message"] = r[1]
            if r[2] and not meta.get("type"):
                meta["type"] = r[2]
            rules.append(meta)
        return rules
    except Exception as e:
        logger.warning("load_rules DB-Fehler: %s", e)
        # Fallback: JSON-Datei
        path = _get_rules_path()
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("rules", [])
        except Exception as exc:
            logger.error("Rules laden fehlgeschlagen: %s", exc)
            return []


def save_rules(rules: List[Dict[str, Any]]):
    """Speichert alle Regeln in die DB (Upsert).

    Schema: (id TEXT PK, text, category, meta) — meta haelt das komplette Rule-Dict.
    """
    try:
        with transaction() as conn:
            existing_ids = {r[0] for r in conn.execute(
                "SELECT id FROM rules"
            ).fetchall()}
            new_ids = {r.get("id") for r in rules if r.get("id")}

            for rid in existing_ids - new_ids:
                conn.execute("DELETE FROM rules WHERE id=?", (rid,))

            for rule in rules:
                rid = rule.get("id")
                if not rid:
                    continue
                # text = Lesbare Zusammenfassung der Regel fuer die Tabelle
                text = rule.get("name", rule.get("text", ""))
                category = rule.get("type", rule.get("category", ""))
                meta_str = json.dumps(rule, ensure_ascii=False)
                conn.execute("""
                    INSERT INTO rules (id, text, category, meta)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        text=excluded.text,
                        category=excluded.category,
                        meta=excluded.meta
                """, (rid, text, category, meta_str))
    except Exception as e:
        logger.error("save_rules DB-Fehler: %s", e)

    # JSON-Backup
    try:
        path = _get_rules_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"rules": rules}, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    except Exception:
        pass


def add_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    """Fuegt eine neue Regel hinzu."""
    rules = load_rules()
    if not rule.get("id"):
        rule["id"] = f"rule_{uuid.uuid4().hex[:8]}"
    rules.append(rule)
    save_rules(rules)
    logger.info("Rule erstellt: %s (%s)", rule.get("name", "?"), rule["id"])
    return rule


def update_rule(rule_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Aktualisiert eine bestehende Regel."""
    rules = load_rules()
    for r in rules:
        if r.get("id") == rule_id:
            r.update({k: v for k, v in updates.items() if k != "id"})
            save_rules(rules)
            logger.info("Rule aktualisiert: %s", rule_id)
            return r
    return None


def delete_rule(rule_id: str) -> bool:
    """Loescht eine Regel."""
    rules = load_rules()
    new_rules = [r for r in rules if r.get("id") != rule_id]
    if len(new_rules) < len(rules):
        save_rules(new_rules)
        logger.info("Rule geloescht: %s", rule_id)
        return True
    return False


def get_rule(rule_id: str) -> Optional[Dict[str, Any]]:
    """Gibt eine einzelne Regel zurueck."""
    for r in load_rules():
        if r.get("id") == rule_id:
            return r
    return None


# ============================================================
# BLOCKADE-REGELN: Zugang pruefen
# ============================================================

def check_access(character_name: str,
    location_id: str,
    room_id: str = "",
    action: str = "enter") -> Tuple[bool, str]:
    """Prueft alle Blockade-Regeln fuer einen Ort/Raum-Zugang.

    Returns: (allowed, reason) — False + Meldung wenn blockiert.
    """
    from app.core.activity_engine import evaluate_condition

    # Danger-Level des Ziels ermitteln
    target_danger = _get_target_danger_level(location_id, room_id)

    for rule in load_rules():
        if rule.get("type") != "block":
            continue
        if rule.get("action", "enter") != action:
            continue

        # Character-Filter: Regel kann auf einen Character beschraenkt sein
        rule_char = (rule.get("character") or "").strip()
        if rule_char and rule_char != character_name:
            continue

        # Ziel-Match pruefen
        target = rule.get("target", {})
        scope = target.get("scope", "")

        matched = False
        if scope == "location" and target.get("location_id") == location_id:
            matched = True
        elif scope == "room" and target.get("location_id") == location_id and target.get("room_id") == room_id:
            matched = True
        elif scope == "any_room":
            # Gilt fuer jeden Raum. Zwei Faelle:
            #  a) Mit konkretem room_id → pruefe direkt gegen den Raum.
            #  b) Location-level ohne room_id → pruefe ob JEDER Raum der
            #     Location blocken wuerde. Wenn mind. ein Raum die Condition
            #     nicht erfuellt, kann der Character dorthin routen und die
            #     Location-Entry ist erlaubt (Scheduler/SetLocation-Skill
            #     waehlen dann den passenden Raum).
            if room_id:
                matched = True
            elif location_id:
                condition_for_check = rule.get("condition", "")
                if condition_for_check:
                    from app.models.world import get_location_by_id
                    _loc = get_location_by_id(location_id)
                    _rooms = (_loc or {}).get("rooms", []) if _loc else []
                    if _rooms:
                        _all_block = True
                        for _r in _rooms:
                            _rid = _r.get("id", "")
                            _p, _ = evaluate_condition(condition_for_check,
                                                        character_name, location_id, _rid)
                            if not _p:
                                _all_block = False
                                break
                        if _all_block:
                            message = rule.get("message", "") or "Zugang verweigert."
                            logger.info("Rule blockiert %s: %s (alle Raeume)",
                                         character_name, rule.get("name", "?"))
                            return False, message
                        # mindestens ein Raum OK → Regel skippen
                        continue
                else:
                    # Keine Condition → any_room blockt grundsaetzlich
                    matched = True
        elif scope == "danger_level":
            min_danger = int(target.get("min_danger", 0))
            if target_danger >= min_danger:
                matched = True

        if not matched:
            continue

        # Bedingung pruefen — wenn Bedingung WAHR, wird blockiert
        # room_id an evaluate_condition durchreichen fuer room_has_item:X
        condition = rule.get("condition", "")
        if condition:
            passed, _ = evaluate_condition(condition, character_name, location_id, room_id)
            if passed:
                message = rule.get("message", "") or "Zugang verweigert."
                logger.info("Rule blockiert %s: %s (%s)", character_name,
                           rule.get("name", "?"), message[:60])
                return False, message

    return True, ""


# ============================================================
# ZWANGS-REGELN: Erzwungene Aktionen pruefen
# ============================================================

def check_force_rules(character_name: str) -> Optional[Dict[str, Any]]:
    """Prueft alle Zwangs-Regeln fuer einen Character.

    Returns: force_action Dict wenn eine Regel greift, sonst None.
    Nur die erste matchende Regel wird zurueckgegeben.
    """
    from app.core.activity_engine import evaluate_condition
    from app.models.character import get_character_current_location, get_character_current_activity

    location_id = get_character_current_location(character_name) or ""
    current_activity = get_character_current_activity(character_name) or ""

    for rule in load_rules():
        if rule.get("type") != "force":
            continue

        condition = rule.get("condition", "")
        if not condition:
            continue

        passed, _ = evaluate_condition(condition, character_name, location_id)
        if passed:
            force = rule.get("force_action", {})
            if force:
                # Bereits in der erzwungenen Aktivitaet? → nicht nochmal feuern
                forced_activity = force.get("set_activity", "")
                if forced_activity and current_activity.lower() == forced_activity.lower():
                    continue

                logger.info("Zwangs-Regel greift fuer %s: %s", character_name,
                           rule.get("name", "?"))
                return {
                    "rule_id": rule.get("id", ""),
                    "rule_name": rule.get("name", ""),
                    "go_to": force.get("go_to", "stay"),
                    "set_activity": force.get("set_activity", ""),
                    "message": rule.get("message", ""),
                }

    return None


def resolve_force_destination(character_name: str,
    go_to: str) -> Tuple[str, str]:
    """Loest das Ziel einer Zwangs-Regel auf.

    go_to Optionen:
    - "stay": Bleiben wo man ist
    - "sleep_location": Character-spezifischer Schlafplatz
    - "home": Home-Location des Characters
    - Location-ID: Direkt zu dieser Location

    Returns: (location_id, room_id)
    """
    from app.models.character import (
        get_character_config, get_character_current_location,
        get_character_current_room)

    if go_to == "stay":
        loc = get_character_current_location(character_name) or ""
        room = get_character_current_room(character_name) or ""
        return loc, room

    config = get_character_config(character_name)

    if go_to in ("sleep_location", "home"):
        # Home-Location = Schlafplatz
        home_loc = config.get("home_location", "")
        home_room = config.get("home_room", "")
        if home_loc:
            return home_loc, home_room
        return _find_nearest_sleep(character_name)

    # "stay" wurde oben behandelt, alles andere ist eine direkte Location-ID

    # Direkte Location-ID
    return go_to, ""


# ============================================================
# HELPER
# ============================================================

def _get_target_danger_level(location_id: str, room_id: str = "") -> int:
    """Ermittelt den danger_level eines Ziels (Raum ueberschreibt Location)."""
    try:
        from app.models.world import get_location_by_id
        location = get_location_by_id(location_id)
        if not location:
            return 0

        # Raum-spezifischer danger_level
        if room_id:
            for room in location.get("rooms", []):
                if room.get("id") == room_id:
                    room_danger = room.get("danger_level")
                    if room_danger is not None:
                        return max(0, min(5, int(room_danger)))
                    break

        # Location danger_level
        return max(0, min(5, int(location.get("danger_level", 0))))
    except Exception:
        return 0


def _find_nearest_sleep(character_name: str) -> Tuple[str, str]:
    """Sucht den naechsten Raum mit Sleeping-Aktivitaet.

    Suche: Aktuelle Location → Nachbar-Locations (via Grid).
    Returns: (location_id, room_id) oder ("", "")
    """
    try:
        from app.models.character import get_character_current_location
        from app.models.world import get_location_by_id, get_neighbor_location_ids

        current_loc = get_character_current_location(character_name) or ""

        # Locations durchsuchen: erst aktuelle, dann Nachbarn
        search_order = [current_loc] if current_loc else []
        if current_loc:
            try:
                search_order.extend(get_neighbor_location_ids(current_loc))
            except Exception:
                pass

        for loc_id in search_order:
            location = get_location_by_id(loc_id)
            if not location:
                continue
            for room in location.get("rooms", []):
                acts = room.get("activities", [])
                for act in acts:
                    act_id = act if isinstance(act, str) else act.get("id", act.get("name", ""))
                    if act_id.lower() in ("sleeping", "schlafen", "sleep"):
                        return loc_id, room.get("id", "")

        return "", ""
    except Exception:
        return "", ""
