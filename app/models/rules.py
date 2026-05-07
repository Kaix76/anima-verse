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


def _get_shared_rules_path() -> Path:
    """Shared baseline: globale Regeln die in allen Welten gelten — solange
    nicht per id-overlay in der Welt ueberschrieben."""
    return Path(__file__).resolve().parent.parent.parent / "shared" / "rules" / "rules.json"


def _load_shared_rules() -> List[Dict[str, Any]]:
    """Liest Regeln aus shared/rules/rules.json. Leerer Fallback wenn die
    Datei fehlt oder defekt ist."""
    path = _get_shared_rules_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rules = data.get("rules", []) if isinstance(data, dict) else []
        # Origin-Marker damit das UI shared/world unterscheiden kann
        for r in rules:
            r["_origin"] = "shared"
        return rules
    except Exception as e:
        logger.warning("shared/rules/rules.json laden fehlgeschlagen: %s", e)
        return []


def _save_shared_rules(rules: List[Dict[str, Any]]):
    """Schreibt rules in shared/rules/rules.json zurueck (ohne Origin-Marker)."""
    path = _get_shared_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rules]
    path.write_text(json.dumps({"rules": clean}, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def _load_world_rules() -> List[Dict[str, Any]]:
    """Liest die Welt-spezifischen Regeln aus der DB."""
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
            meta.setdefault("id", r[0])
            if r[1] and not (meta.get("name") or meta.get("message")):
                meta["message"] = r[1]
            if r[2] and not meta.get("type"):
                meta["type"] = r[2]
            meta["_origin"] = "world"
            rules.append(meta)
        return rules
    except Exception as e:
        logger.warning("_load_world_rules DB-Fehler: %s", e)
        return []


def load_rules() -> List[Dict[str, Any]]:
    """Merged shared-baseline + world-overlay. Welt-Eintraege ueberschreiben
    Shared-Eintraege bei id-Match — analog zum Items/Activities-Pattern.

    Origin-Marker im Eintrag:
      "_origin": "shared"           — kommt aus shared/rules/rules.json
      "_origin": "world"            — kommt aus world.db, kein shared-Konflikt
      "_origin": "world override"   — Welt-Eintrag der eine Shared-Rule mit
                                       gleicher id ueberschreibt
    """
    shared = _load_shared_rules()
    world = _load_world_rules()
    if not world:
        return shared
    if not shared:
        return world

    by_id: Dict[str, Dict[str, Any]] = {}
    for r in shared:
        rid = (r.get("id") or "").strip()
        if rid:
            by_id[rid] = r
    for r in world:
        rid = (r.get("id") or "").strip()
        if not rid:
            continue
        if rid in by_id:
            r = dict(r)
            r["_origin"] = "world override"
        by_id[rid] = r
    return list(by_id.values())


def save_rules(rules: List[Dict[str, Any]], target_dir: str = "world"):
    """Speichert Regeln. ``target_dir``:
        "world"  — Welt-DB (Default; backward kompatibel)
        "shared" — shared/rules/rules.json (globale Baseline fuer alle Welten)

    Bei "world": Upsert in DB, Eintraege deren id nicht in der Liste sind
    werden geloescht (full-replace-Semantik fuer Welt-Layer).
    Bei "shared": die Liste wird als komplette shared/rules/rules.json
    geschrieben.
    """
    if target_dir == "shared":
        try:
            _save_shared_rules(rules)
            logger.info("Shared-Rules gespeichert: %d Eintraege", len(rules))
        except Exception as e:
            logger.error("save_rules shared-Fehler: %s", e)
        return

    # World-DB upsert + Cleanup verwaister IDs
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
                # _origin Marker nicht persistieren (Render-Field, nicht Daten)
                clean = {k: v for k, v in rule.items() if not k.startswith("_")}
                # text = Lesbare Zusammenfassung der Regel fuer die Tabelle
                text = clean.get("name", clean.get("text", ""))
                category = clean.get("type", clean.get("category", ""))
                meta_str = json.dumps(clean, ensure_ascii=False)
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


# Default-Rules-Seed: bei Welt-Init oder bei expliziter Migration werden
# Sleep- und Wake-Rules angelegt falls sie noch nicht existieren. Damit
# kommen frische Welten direkt mit funktionierender Erschoepfungs-/
# Aufwach-Logik. Bestehende Welten bleiben unangetastet (Idempotenz via
# id-Match — wer eine eigene Rule mit derselben id angelegt hat behaelt sie).
DEFAULT_RULES = [
    {
        "id": "default_sleep_when_exhausted",
        "name": "Erschoepfung",
        "type": "force",
        "condition": "stamina<10",
        "force_action": {
            "go_to": "home",
            "set_activity": "Sleeping",
        },
        "message": "Bin erschoepft, gehe schlafen.",
    },
    {
        "id": "default_wake_when_rested",
        "name": "Aufwachen",
        "type": "force",
        # Char wacht auf wenn Stamina hoch genug UND aktuell schlafend UND
        # der Tagesablauf nicht (mehr) Schlaf vorschreibt. Letzte Bedingung
        # verhindert dass ein Char waehrend des Schedule-Schlaf-Fensters
        # vorzeitig aufwacht (z.B. zu kurz "ausgeschlafen" um 04:00 weil
        # Stamina knapp ueber Schwelle ist) — der Schedule entscheidet
        # dann mit ob's wirklich Zeit ist.
        "condition": "stamina>60 AND current_activity:sleeping AND NOT schedule:sleeping",
        "force_action": {
            "go_to": "stay",
            "set_activity": "",
        },
        "message": "Ausgeschlafen, wache auf.",
    },
]


def ensure_default_rules() -> int:
    """Stellt sicher dass die Default-Sleep/Wake-Rules in der Shared-Baseline
    (``shared/rules/rules.json``) existieren — von dort sind sie automatisch
    in jeder Welt verfuegbar (es sei denn, eine Welt ueberschreibt sie per
    id-overlay in der DB).

    Idempotent: pruefen ob eine Rule mit der ID schon im shared-File
    eingetragen ist; nur fehlende werden ergaenzt.

    Returns: Anzahl neu angelegter Rules (0 wenn alles schon da war).
    """
    try:
        # Nur shared-baseline pruefen — wenn eine Welt die Rule explizit
        # geloescht hat (DB-Override mit z.B. enabled=False), bleibt die
        # User-Entscheidung erhalten weil load_world_rules() fuer den
        # Override-Pfad sorgt.
        shared = _load_shared_rules()
        shared_ids = {(r.get("id") or "").strip() for r in shared}
        added = 0
        for default in DEFAULT_RULES:
            if default["id"] in shared_ids:
                continue
            shared.append(dict(default))
            added += 1
        if added:
            save_rules(shared, target_dir="shared")
            logger.info("Default-Rules in shared baseline geseedet: %d neu", added)
        return added
    except Exception as e:
        logger.warning("ensure_default_rules fehlgeschlagen: %s", e)
        return 0


def add_rule(rule: Dict[str, Any], target_dir: str = "world") -> Dict[str, Any]:
    """Fuegt eine neue Regel hinzu (Upsert nach id).

    target_dir:
      "world"  — Default. Rule landet in der Welt-DB; nur in dieser Welt.
      "shared" — Rule landet in shared/rules/rules.json; gilt fuer alle Welten.

    Falls die id im Ziel-Layer schon existiert wird der Eintrag ersetzt — das
    erlaubt dem UI, eine Rule per "Speicherort wechseln" zu verschieben (alte
    Seite via DELETE entfernen, neue Seite via POST setzen).
    """
    if not rule.get("id"):
        rule["id"] = f"rule_{uuid.uuid4().hex[:8]}"
    rule_id = rule["id"]
    clean = {k: v for k, v in rule.items() if not k.startswith("_")}
    if target_dir == "shared":
        existing = _load_shared_rules()
        existing = [r for r in existing if r.get("id") != rule_id]
        existing.append(clean)
        save_rules(existing, target_dir="shared")
    else:
        world_only = _load_world_rules()
        world_only = [r for r in world_only if r.get("id") != rule_id]
        world_only.append(clean)
        save_rules(world_only, target_dir="world")
    logger.info("Rule upsert: %s (%s) → %s",
                rule.get("name", "?"), rule_id, target_dir)
    return rule


def update_rule(rule_id: str, updates: Dict[str, Any],
                target_dir: str = "world") -> Optional[Dict[str, Any]]:
    """Aktualisiert eine bestehende Regel.

    target_dir:
      "world"  — die Welt-DB-Version updaten (legt sie als Override an
                 wenn die ID nur in shared existiert).
      "shared" — die shared-baseline-Version updaten.
    """
    if target_dir == "shared":
        rules = _load_shared_rules()
        for r in rules:
            if r.get("id") == rule_id:
                r.update({k: v for k, v in updates.items()
                         if k != "id" and not k.startswith("_")})
                save_rules(rules, target_dir="shared")
                logger.info("Shared-Rule aktualisiert: %s", rule_id)
                return r
        return None

    # World-Update: vorhandenen Welt-Eintrag updaten oder als Override anlegen
    world_rules = _load_world_rules()
    for r in world_rules:
        if r.get("id") == rule_id:
            r.update({k: v for k, v in updates.items()
                     if k != "id" and not k.startswith("_")})
            save_rules(world_rules, target_dir="world")
            logger.info("Rule aktualisiert: %s", rule_id)
            return r
    # Shared-Eintrag wird per Welt-Override veraendert: kopieren + updates
    shared = _load_shared_rules()
    for r in shared:
        if r.get("id") == rule_id:
            override = {k: v for k, v in r.items() if not k.startswith("_")}
            override.update({k: v for k, v in updates.items()
                            if k != "id" and not k.startswith("_")})
            world_rules.append(override)
            save_rules(world_rules, target_dir="world")
            logger.info("Rule aus Shared in Welt ueberschrieben: %s", rule_id)
            return override
    return None


def delete_rule(rule_id: str, target_dir: str = "") -> bool:
    """Loescht eine Regel.

    target_dir:
      ""       — Auto: bevorzugt Welt-Eintrag (Override) loeschen, sonst Shared.
      "world"  — nur den Welt-Eintrag entfernen (Shared bleibt → Rule erscheint wieder).
      "shared" — den Shared-Eintrag entfernen (gilt fuer alle Welten).
    """
    if target_dir == "world":
        world_rules = _load_world_rules()
        new = [r for r in world_rules if r.get("id") != rule_id]
        if len(new) < len(world_rules):
            save_rules(new, target_dir="world")
            logger.info("World-Rule geloescht: %s", rule_id)
            return True
        return False
    if target_dir == "shared":
        shared = _load_shared_rules()
        new = [r for r in shared if r.get("id") != rule_id]
        if len(new) < len(shared):
            save_rules(new, target_dir="shared")
            logger.info("Shared-Rule geloescht: %s", rule_id)
            return True
        return False

    # Auto-Modus: Welt zuerst, sonst Shared
    if delete_rule(rule_id, target_dir="world"):
        return True
    return delete_rule(rule_id, target_dir="shared")


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
        elif scope == "room" and target.get("location_id") == location_id and room_id in (target.get("room_ids") or []):
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
# DISCOVER-REGELN: Entdecken angrenzender Locations
# ============================================================

def check_discover_rules(character_name: str) -> Optional[Dict[str, Any]]:
    """Prueft Discover-Regeln und entdeckt ggf. einen angrenzenden, noch
    unbekannten Ort.

    Vorgang:
    - Iteriert die Discover-Regeln in Reihenfolge.
    - Pro Regel: Character-Filter, Bedingung in der aktuellen Location, dann
      Wahrscheinlichkeits-Wuerfel.
    - Erste Regel mit erfolgreichem Wuerfelwurf gewinnt — dann wird ein
      zufaelliger noch unbekannter Nachbar (Grid-adjacent) zur known_locations-
      Liste hinzugefuegt.

    Wird nicht aktiv fuer Characters ohne known_locations-Feld (Legacy/
    unrestricted) — die sehen ohnehin schon alles.

    Returns: Dict mit location_id/location_name/rule_*/message bei Treffer,
    sonst None (nichts entdeckt diese Runde).
    """
    import random as _random
    from app.core.activity_engine import evaluate_condition
    from app.models.character import (
        get_character_current_location, get_known_locations,
        add_known_location, _record_state_change)
    from app.models.world import get_neighbor_location_ids, get_location_by_id

    location_id = get_character_current_location(character_name) or ""
    if not location_id:
        return None

    known = get_known_locations(character_name)
    if known is None:
        # Legacy/unrestricted — nichts zu entdecken
        return None

    try:
        neighbors = get_neighbor_location_ids(location_id)
    except Exception:
        neighbors = []
    unknown = [n for n in neighbors if n and n not in known]
    if not unknown:
        return None

    for rule in load_rules():
        if rule.get("type") != "discover":
            continue
        rule_char = (rule.get("character") or "").strip()
        if rule_char and rule_char != character_name:
            continue
        condition = (rule.get("condition") or "").strip()
        if condition:
            passed, _ = evaluate_condition(condition, character_name, location_id)
            if not passed:
                continue
        try:
            probability = float(rule.get("probability", 0))
        except (TypeError, ValueError):
            probability = 0.0
        probability = max(0.0, min(1.0, probability))
        if probability <= 0.0 or _random.random() >= probability:
            continue

        discovered_id = _random.choice(unknown)
        add_known_location(character_name, discovered_id)
        loc = get_location_by_id(discovered_id) or {}
        loc_name = loc.get("name", discovered_id)
        message = (rule.get("message") or "").strip() \
            or f"Hat einen neuen Ort entdeckt: {loc_name}"
        try:
            _record_state_change(character_name, "discovery", loc_name,
                metadata={"location_id": discovered_id,
                          "rule_id": rule.get("id", ""),
                          "rule_name": rule.get("name", "")})
        except Exception:
            pass
        logger.info("Discover-Rule '%s' fuer %s -> %s",
                    rule.get("name", "?"), character_name, loc_name)
        return {
            "location_id": discovered_id,
            "location_name": loc_name,
            "rule_id": rule.get("id", ""),
            "rule_name": rule.get("name", ""),
            "message": message,
        }

    return None


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
