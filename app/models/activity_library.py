"""Activity Library — Zentrale Aktivitaeten-Verwaltung.

Laedt Aktivitaeten aus:
1. shared/activities/*.json     (allgemein, git-tracked)
2. worlds/{world}/activities/*.json  (welt-spezifisch, nicht git-tracked)

Override-System (3 Ebenen):
- Bibliothek: Basis-Definition
- Location-Override: world.json → activity_overrides
- Character-Override: character_config.json → activity_overrides + extra_activities

Alle Dateien mit *.json in den Verzeichnissen werden geladen.
"""
import json
import copy
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.log import get_logger
from app.core.paths import get_storage_dir

logger = get_logger("activity_library")

# Cached library (reloaded on demand)
_library: Dict[str, Dict[str, Any]] = {}  # id -> activity
_library_loaded = False


def _get_shared_activities_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "shared" / "activities"


def _get_world_activities_dir() -> Path:
    return get_storage_dir() / "activities"


def _load_activities_from_dir(directory: Path) -> Dict[str, Dict[str, Any]]:
    """Laedt alle *.json Dateien aus einem Verzeichnis."""
    result = {}
    if not directory.exists():
        return result
    for json_file in sorted(directory.glob("*.json")):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            group = data.get("group", json_file.stem)
            # Gruppen-Level locations: Alle Aktivitaeten dieser Gruppe sind
            # automatisch an diesen Orten verfuegbar (kann pro Aktivitaet ueberschrieben werden)
            group_locations = data.get("locations", [])
            for act in data.get("activities", []):
                act_id = act.get("id", "")
                if not act_id:
                    act_id = act.get("name", "").lower().replace(" ", "_")
                    act["id"] = act_id
                if not act_id:
                    continue
                act["_group"] = group
                act["_source"] = str(json_file.name)
                # Gruppen-Locations vererben wenn Aktivitaet keine eigenen hat
                if group_locations and "locations" not in act:
                    act["locations"] = group_locations
                if act_id in result:
                    logger.warning("Duplikat-ID '%s' in %s (bereits in %s) — wird ueberschrieben",
                                   act_id, json_file.name, result[act_id].get("_source", "?"))
                result[act_id] = act
        except Exception as e:
            logger.warning("Fehler beim Laden von %s: %s", json_file, e)
    return result


def _load_activities_from_db() -> Dict[str, Dict[str, Any]]:
    """Laedt Aktivitaeten aus der DB (activities-Tabelle, welt-spezifisch)."""
    result = {}
    try:
        from app.core.db import get_connection
        conn = get_connection()
        rows = conn.execute(
            "SELECT id, name, category, meta FROM activities ORDER BY name ASC"
        ).fetchall()
        for r in rows:
            act_id = r[0]
            meta = {}
            try:
                meta = json.loads(r[3] or "{}")
            except Exception:
                pass
            # meta ist der volle Activity-Definition-JSON (ohne id, da die
            # ID in der id-Spalte steht). Immer meta zusammenfuehren —
            # sonst fehlen Felder wie requires_partner, condition, effects etc.
            act = dict(meta) if isinstance(meta, dict) else {}
            act.setdefault("id", act_id)
            act.setdefault("name", r[1] or act_id)
            act.setdefault("category", r[2] or "")
            act.setdefault("_group", r[2] or "")
            act["_origin"] = "world_db"
            result[act_id] = act
    except Exception as e:
        logger.debug("_load_activities_from_db: %s", e)
    return result


def _save_world_activity(act: Dict[str, Any]):
    """Speichert eine einzelne Welt-Aktivitaet in die DB."""
    try:
        from app.core.db import transaction
        with transaction() as conn:
            conn.execute("""
                INSERT INTO activities (id, name, category, meta)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    category=excluded.category,
                    meta=excluded.meta
            """, (
                act.get("id", ""),
                act.get("name", ""),
                act.get("_group", act.get("category", "")),
                json.dumps(act, ensure_ascii=False),
            ))
    except Exception as e:
        logger.debug("_save_world_activity: %s", e)


def load_library(force: bool = False):
    """Laedt die komplette Aktivitaeten-Bibliothek."""
    global _library, _library_loaded
    if _library_loaded and not force:
        return

    _library = {}

    # 1. Shared (allgemein)
    shared_dir = _get_shared_activities_dir()
    shared = _load_activities_from_dir(shared_dir)
    for act in shared.values():
        act["_origin"] = "shared"
    _library.update(shared)
    logger.info("Shared activities geladen: %d aus %s", len(shared), shared_dir)

    # 2. World-spezifisch aus JSON-Verzeichnis
    world_dir = _get_world_activities_dir()
    world_json = _load_activities_from_dir(world_dir)
    for act in world_json.values():
        act["_origin"] = "world"
    _library.update(world_json)
    if world_json:
        logger.info("World activities (JSON) geladen: %d aus %s", len(world_json), world_dir)

    # 3. World-spezifisch aus DB. DB-Felder ueberschreiben gleichnamige
    #    JSON-Felder — aber Felder, die nur in JSON existieren (z.B. outfit_type,
    #    wenn die DB-Variante vor Einfuehrung des Feldes gespeichert wurde),
    #    bleiben erhalten. Kein reines update(), sonst gehen JSON-Keys verloren.
    world_db = _load_activities_from_db()
    for act_id, db_act in world_db.items():
        existing = _library.get(act_id)
        if existing:
            merged = dict(existing)
            merged.update(db_act)
            _library[act_id] = merged
        else:
            _library[act_id] = db_act
    if world_db:
        logger.info("World activities (DB) geladen: %d", len(world_db))

    _library_loaded = True
    logger.info("Activity Library: %d Aktivitaeten total", len(_library))


def reload_library():
    """Erzwingt Neuladen der Bibliothek."""
    load_library(force=True)


def get_all_library_activities() -> List[Dict[str, Any]]:
    """Gibt alle Aktivitaeten der Bibliothek zurueck."""
    load_library()
    return list(_library.values())


def get_library_activity(activity_id: str) -> Optional[Dict[str, Any]]:
    """Gibt eine einzelne Aktivitaet nach ID zurueck."""
    load_library()
    return _library.get(activity_id)


def _get_all_names(act: Dict[str, Any]) -> List[str]:
    """Gibt alle Namensvarianten einer Aktivitaet zurueck (name, name_en, name_XX)."""
    names = []
    for key, val in act.items():
        if (key == "name" or key.startswith("name_")) and isinstance(val, str) and val:
            names.append(val)
    return names


def find_library_activity_by_name(name: str) -> Optional[Dict[str, Any]]:
    """Sucht eine Aktivitaet nach Name in allen Sprachvarianten (case-insensitive).

    Drei Matching-Stufen:
    1. Exakt-Match (name, name_de, ID)
    2. Substring-Match
    3. Wort-Stammform-Match (cocktail → cocktails, drink → drinking)
    """
    load_library()
    name_lower = name.lower()
    # Exakt-Match gegen alle Varianten
    for act in _library.values():
        for n in _get_all_names(act):
            if n.lower() == name_lower:
                return act
    # Substring
    for act in _library.values():
        for n in _get_all_names(act):
            nl = n.lower()
            if name_lower in nl or nl in name_lower:
                return act
    # Wort-Stammform-Match
    _stops = {"a", "an", "the", "at", "in", "on", "and", "or", "to", "with", "some",
              "my", "do", "doing", "have", "having", "get", "getting", "go", "going",
              "make", "making", "take", "taking", "be", "being"}
    def _stems(text):
        words = set(text.lower().replace("_", " ").split())
        result = set()
        for w in words:
            result.add(w)
            if w.endswith("s") and len(w) > 3: result.add(w[:-1])
            if w.endswith("ing") and len(w) > 5: result.add(w[:-3])
        return result - _stops

    input_stems = _stems(name)
    if not input_stems:
        return None
    best_act = None
    best_overlap = 0
    for act in _library.values():
        act_words = set()
        act_words.update(act.get("id", "").lower().replace("_", " ").split())
        for n in _get_all_names(act):
            act_words.update(n.lower().split())
        act_stems = _stems(" ".join(act_words))
        overlap = len(input_stems & act_stems)
        if overlap > best_overlap:
            best_overlap = overlap
            best_act = act
    return best_act


def _merge_override(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Merged Override-Felder in eine Basis-Aktivitaet (shallow merge).

    Nur explizit gesetzte Felder im Override ueberschreiben die Basis.
    Verschachtelte Dicts (effects, cumulative_effect) werden ebenfalls gemerged.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key.startswith("_"):
            continue  # Interne Felder nicht ueberschreiben
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            # Merge nested dicts (effects, cumulative_effect, etc.)
            result[key] = {**result[key], **value}
        else:
            result[key] = value
    return result


def get_available_activities(character_name: str,
    location_id: str = "",
    room_id: str = "",
    filter_conditions: bool = True) -> List[Dict[str, Any]]:
    """Zentrale Funktion: Gibt alle verfuegbaren Aktivitaeten zurueck.

    Merge-Reihenfolge:
    1. Bibliothek laden (shared/ + worlds/)
    2. Room-Aktivitaeten: IDs aus world.json Raum
    3. Location-Override anwenden
    4. Character extra_activities + activity_overrides
    5. Conditions + Cooldowns filtern (optional)

    Returns: Liste von Aktivitaet-Dicts (gemerged, bereit fuer Prompt/UI)
    """
    load_library()

    from app.models.world import get_location_by_id
    from app.models.character import (
        get_character_config,
        get_character_current_location)

    if not location_id:
        location_id = get_character_current_location(character_name) or ""

    # 1. Raum-Aktivitaeten sammeln
    activities_by_id: Dict[str, Dict[str, Any]] = {}
    loc_data = get_location_by_id(location_id) if location_id else None

    if loc_data:
        target_room = None
        if room_id:
            for r in loc_data.get("rooms", []):
                if r.get("id") == room_id:
                    target_room = r
                    break

        # Alle Raeume durchgehen (oder nur den spezifischen)
        rooms_to_check = [target_room] if target_room else loc_data.get("rooms", [])
        for room in rooms_to_check:
            if not room:
                continue
            for act in room.get("activities", []):
                if isinstance(act, str):
                    # Neues Format: ID-Referenz
                    lib_act = get_library_activity(act) or find_library_activity_by_name(act)
                    if lib_act:
                        activities_by_id[lib_act["id"]] = copy.deepcopy(lib_act)
                    else:
                        # Unbekannte ID — als einfache Aktivitaet behandeln
                        activities_by_id[act] = {"id": act, "name": act, "description": ""}
                elif isinstance(act, dict):
                    # Legacy-Format: Inline-Objekt
                    name = act.get("name", "")
                    if not name:
                        continue
                    # Versuche in Bibliothek zu finden
                    lib_act = find_library_activity_by_name(name)
                    if lib_act:
                        # Bibliotheks-Aktivitaet mit lokalen Werten mergen
                        merged = _merge_override(lib_act, act)
                        activities_by_id[lib_act["id"]] = merged
                    else:
                        # Reine lokale Aktivitaet (noch nicht in Bibliothek)
                        act_id = name.lower().replace(" ", "_")
                        act_copy = copy.deepcopy(act)
                        act_copy.setdefault("id", act_id)
                        activities_by_id[act_id] = act_copy

        # 1b. Library-Aktivitaeten mit passender locations-Liste hinzufuegen
        for lib_id, lib_act in _library.items():
            if lib_id in activities_by_id:
                continue  # Bereits ueber Raum zugewiesen
            act_locs = lib_act.get("locations", [])
            if act_locs and location_id in act_locs:
                activities_by_id[lib_id] = copy.deepcopy(lib_act)

        # 2. Location-Override anwenden
        loc_overrides = loc_data.get("activity_overrides", {})
        for act_id, override in loc_overrides.items():
            if act_id in activities_by_id:
                activities_by_id[act_id] = _merge_override(activities_by_id[act_id], override)

    # 3. Character: extra_activities + overrides
    config = get_character_config(character_name)

    # 3a. Template-extra_activities — gemeinsame Basis fuer alle Characters
    #     dieses Templates (z.B. "sleeping" fuer human-*, "sex/masturbating"
    #     fuer human-roleplay-nsfw). Wird mit Character-extras dedupt gemerged.
    template_extras: list = []
    try:
        from app.models.character import get_character_profile as _gcp2
        from app.models.character_template import get_template as _gt2
        _prof = _gcp2(character_name) or {}
        _tmpl = _gt2(_prof.get("template", "human-default"))
        if _tmpl and isinstance(_tmpl.get("extra_activities"), list):
            template_extras = [str(x).strip() for x in _tmpl["extra_activities"]
                               if x and str(x).strip()]
    except Exception as _te:
        logger.debug("Template-extras laden fehlgeschlagen: %s", _te)

    # extra_activities: Template-Basis + Character-Overrides, dedupt
    all_extras: list = []
    seen_extras = set()
    for src in (template_extras, config.get("extra_activities", []) or []):
        for e in src:
            if e and e not in seen_extras:
                seen_extras.add(e)
                all_extras.append(e)
    for extra_id in all_extras:
        if extra_id not in activities_by_id:
            lib_act = get_library_activity(extra_id) or find_library_activity_by_name(extra_id)
            if lib_act:
                activities_by_id[lib_act["id"]] = copy.deepcopy(lib_act)

    # activity_overrides: Character-spezifische Anpassungen
    char_overrides = config.get("activity_overrides", {})
    for act_id, override in char_overrides.items():
        if act_id in activities_by_id:
            activities_by_id[act_id] = _merge_override(activities_by_id[act_id], override)
        else:
            # Override fuer nicht-zugewiesene Aktivitaet → automatisch zuweisen
            lib_act = get_library_activity(act_id) or find_library_activity_by_name(act_id)
            if lib_act:
                activities_by_id[act_id] = _merge_override(copy.deepcopy(lib_act), override)

    # 5. Conditions + Cooldowns + requires_partner + required_roles filtern
    if filter_conditions:
        from app.core.activity_engine import evaluate_condition, check_cooldown, check_partner_available
        # Character-Rollen vorab holen (case-insensitive Vergleich).
        # Akzeptiert Liste oder kommagetrennten String (Editor speichert Text).
        char_roles_raw = config.get("roles", []) or []
        if isinstance(char_roles_raw, str):
            char_roles_raw = [s for s in char_roles_raw.split(",")]
        char_roles = {str(r).strip().lower() for r in char_roles_raw if str(r).strip()}
        filtered = {}
        for act_id, act in activities_by_id.items():
            # Role-Filter: wenn Activity required_roles definiert, muss der
            # Character mindestens eine davon haben. Leer = jeder darf.
            req_roles_raw = act.get("required_roles", []) or []
            if isinstance(req_roles_raw, str):
                req_roles_raw = [s for s in req_roles_raw.split(",")]
            req_roles = {str(r).strip().lower() for r in req_roles_raw if str(r).strip()}
            if req_roles and not (req_roles & char_roles):
                continue
            # Condition-Check
            condition = act.get("condition", "")
            if condition:
                passed, _ = evaluate_condition(condition, character_name, location_id)
                if not passed:
                    continue
            # Cooldown-Check
            cd_ok, _ = check_cooldown(character_name, act.get("name", ""))
            if not cd_ok:
                continue
            # requires_partner: Activity braucht anderen Character am gleichen Ort
            if act.get("requires_partner"):
                partner_ok, _ = check_partner_available(character_name, location_id)
                if not partner_ok:
                    # Fallback-Activity anbieten — aber nur wenn ihre eigene
                    # Condition auch passt. Trifft die nicht zu, wird das ganze
                    # Branch verworfen (Original + Fallback).
                    fallback_id = act.get("fallback_activity", "")
                    if fallback_id:
                        fb_act = get_library_activity(fallback_id) or find_library_activity_by_name(fallback_id)
                        if fb_act and fb_act["id"] not in filtered:
                            fb_cond = fb_act.get("condition", "")
                            if fb_cond:
                                fb_passed, _ = evaluate_condition(
                                    fb_cond, character_name, location_id)
                                if not fb_passed:
                                    # Fallback-Condition greift nicht -> komplett rejecten
                                    continue
                            filtered[fb_act["id"]] = copy.deepcopy(fb_act)
                    continue
            filtered[act_id] = act
        activities_by_id = filtered

    return list(activities_by_id.values())


def get_localized_field(act: Dict[str, Any], field: str, lang: str = "de") -> str:
    """Gibt ein Feld in der gewuenschten Sprache zurueck.

    Sucht {field}_{lang}, Fallback auf {field}.
    Beispiel: get_localized_field(act, "name", "de") → name_de oder name
    """
    localized = act.get(f"{field}_{lang}", "")
    if localized:
        return localized
    return act.get(field, "")


def get_localized_name(act: Dict[str, Any], lang: str = "de") -> str:
    """Gibt den Aktivitaetsnamen in der gewuenschten Sprache zurueck."""
    return get_localized_field(act, "name", lang)


def get_activity_names(character_name: str,
    location_id: str = "",
    room_id: str = "",
    lang: str = "") -> List[str]:
    """Convenience: Gibt die Namen der verfuegbaren Aktivitaeten zurueck.

    Wenn lang angegeben, wird name_{lang} bevorzugt (Fallback: name).
    """
    activities = get_available_activities(character_name, location_id, room_id)
    if lang:
        return [get_localized_name(a, lang) for a in activities if a.get("name")]
    return [a.get("name", "") for a in activities if a.get("name")]


def save_library_activity(activity: Dict[str, Any], target_dir: str = "world"):
    """Speichert eine Aktivitaet in die Bibliothek.

    target_dir: "shared" oder "world"
    """
    act_id = activity.get("id", "")
    group = activity.get("_group", "custom")
    if not act_id:
        return

    if target_dir == "shared":
        base_dir = _get_shared_activities_dir()
    else:
        base_dir = _get_world_activities_dir()

    base_dir.mkdir(parents=True, exist_ok=True)

    # Datei nach Gruppe benennen
    filename = group.lower().replace(" ", "_").replace("&", "und") + ".json"
    filepath = base_dir / filename

    # Aktivitaet aus ALLEN anderen Dateien entfernen (verhindert Duplikate
    # wenn Gruppe/Dateiname sich aendert)
    for other_file in base_dir.glob("*.json"):
        if other_file == filepath:
            continue
        try:
            other_data = json.loads(other_file.read_text(encoding="utf-8"))
            other_acts = other_data.get("activities", [])
            new_acts = [a for a in other_acts if a.get("id") != act_id]
            if len(new_acts) < len(other_acts):
                other_data["activities"] = new_acts
                other_file.write_text(
                    json.dumps(other_data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                logger.debug("Activity '%s' aus %s entfernt (Gruppenwechsel)", act_id, other_file)
        except Exception:
            pass

    # Bestehende Datei laden oder neu
    if filepath.exists():
        data = json.loads(filepath.read_text(encoding="utf-8"))
    else:
        data = {"group": group, "activities": []}

    # Update oder Append
    clean = {k: v for k, v in activity.items() if not k.startswith("_")}
    found = False
    for i, existing in enumerate(data["activities"]):
        if existing.get("id") == act_id:
            data["activities"][i] = clean
            found = True
            break
    if not found:
        data["activities"].append(clean)

    filepath.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8")

    # Cache invalidieren
    global _library_loaded
    _library_loaded = False
    logger.info("Activity '%s' gespeichert in %s", act_id, filepath)


def delete_library_activity(activity_id: str, target_dir: str = "world") -> bool:
    """Loescht eine Aktivitaet aus der Bibliothek und entfernt alle Referenzen."""
    if target_dir == "shared":
        base_dir = _get_shared_activities_dir()
    else:
        base_dir = _get_world_activities_dir()

    if not base_dir.exists():
        return False

    deleted = False
    for json_file in base_dir.glob("*.json"):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
            activities = data.get("activities", [])
            new_acts = [a for a in activities if a.get("id") != activity_id]
            if len(new_acts) < len(activities):
                data["activities"] = new_acts
                json_file.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8")
                global _library_loaded
                _library_loaded = False
                logger.info("Activity '%s' geloescht aus %s", activity_id, json_file)
                deleted = True
                break
        except Exception:
            pass

    if deleted:
        _cleanup_activity_references(activity_id)

    return deleted


def _cleanup_activity_references(activity_id: str):
    """Entfernt alle Referenzen auf eine geloeschte Aktivitaet.

    - Character extra_activities: ID entfernen
    - Character activity_overrides: Override entfernen
    - Room activities: ID-Referenz entfernen
    """
    try:
        from app.models.character import (
            list_available_characters, get_character_config, save_character_config)
        from app.models.world import _load_world_data, _save_world_data

        # 1. Characters: extra_activities + activity_overrides
        # World-per-Dir: keine User-IDs mehr, ein Welt-Dir = ein Storage-Dir.
        try:
            for char_name in list_available_characters():
                config = get_character_config(char_name)
                changed = False

                extras = config.get("extra_activities", [])
                if activity_id in extras:
                    extras.remove(activity_id)
                    config["extra_activities"] = extras
                    changed = True

                overrides = config.get("activity_overrides", {})
                if activity_id in overrides:
                    del overrides[activity_id]
                    config["activity_overrides"] = overrides
                    changed = True

                if changed:
                    save_character_config(char_name, config)
                    logger.info("Activity '%s' Referenzen entfernt bei %s", activity_id, char_name)
        except Exception as e:
            logger.warning("Cleanup Character-Refs fehlgeschlagen: %s", e)

        # 2. Rooms: Activity-ID aus rooms.activities entfernen
        try:
            world = _load_world_data()
            world_changed = False
            for loc in world.get("locations", []):
                for room in loc.get("rooms", []):
                    acts = room.get("activities", [])
                    new_acts = [a for a in acts if a != activity_id]
                    if len(new_acts) < len(acts):
                        room["activities"] = new_acts
                        world_changed = True

                # Location activity_overrides
                loc_ov = loc.get("activity_overrides", {})
                if activity_id in loc_ov:
                    del loc_ov[activity_id]
                    loc["activity_overrides"] = loc_ov
                    world_changed = True

            if world_changed:
                _save_world_data(world)
                logger.info("Activity '%s' Raum-Referenzen entfernt", activity_id)
        except Exception as e:
            logger.warning("Room cleanup fehlgeschlagen: %s", e)

    except Exception as e:
        logger.error("Activity cleanup Fehler fuer '%s': %s", activity_id, e)
