"""Character-Profil Verwaltung - User-spezifisch

Renamed from agent.py. All functions use character_name instead of agent_name,
and directories are stored under characters/ instead of agents/.

Storage: world.db — Tabellen characters, character_state
"""
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
import json
import random
import uuid

from app.core.log import get_logger
from app.core.db import get_connection, transaction

logger = get_logger("character_model")

from app.core.paths import get_storage_dir

# ISO 639-1 language code → full language name mapping
LANGUAGE_MAP = {
    "de": "German",
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "nl": "Dutch",
    "pl": "Polish",
    "ru": "Russian",
    "ja": "Japanese",
    "zh": "Chinese",
    "ko": "Korean",
    "ar": "Arabic",
    "tr": "Turkish",
    "sv": "Swedish",
    "da": "Danish",
    "no": "Norwegian",
    "fi": "Finnish",
    "cs": "Czech",
    "uk": "Ukrainian",
    "hi": "Hindi",
}


def get_user_characters_dir() -> Path:
    """Gibt das Characters-Verzeichnis fuer einen User zurueck.

    Supports both characters/ (new) and agents/ (legacy) directories.
    Single-world model: characters live directly under storage root.
    """
    characters_dir = get_storage_dir() / "characters"
    if characters_dir.exists():
        return characters_dir
    # Fallback to old agents/ dir if it still exists
    agents_dir = get_storage_dir() / "agents"
    if agents_dir.exists():
        return agents_dir
    # New user - create characters/
    characters_dir.mkdir(parents=True, exist_ok=True)
    return characters_dir


def get_character_dir(character_name: str, *, create: bool = False) -> Path:
    """Gibt das Verzeichnis fuer einen spezifischen Character zurueck.

    create: wenn True, wird das Verzeichnis bei Bedarf angelegt. Default
    False — Lese-Pfade (FE-Polls fuer current-location, Bilder, Soul-MDs)
    sollen kein Verzeichnis erzeugen wenn der Character nicht existiert.
    Sonst rutscht z.B. ein verwaister localStorage-Char (alte Welt) als
    leeres Verzeichnis in jede neue Welt rein.

    Aufrufer die wirklich anlegen wollen (Character-Erstellung,
    save_character_*-Pfade) muessen create=True explizit setzen.
    """
    if not character_name or character_name == "KI":
        raise ValueError(f"Ungueltiger Character-Name: '{character_name}'")
    # JS-stringified Null-Werte abfangen — entstehen wenn ein FE-Pfad
    # ``${value}`` interpoliert obwohl value undefined/null/NaN ist.
    # Sonst legt ein get_character_skills_dir("undefined") still ein
    # Verzeichnis an, das danach in der Roster/Sidebar als Geister-Character
    # auftaucht.
    if character_name.lower() in ("undefined", "null", "none", "nan"):
        raise ValueError(f"Ungueltiger Character-Name (JS-Null): '{character_name}'")
    character_dir = get_user_characters_dir() / character_name
    if create:
        character_dir.mkdir(parents=True, exist_ok=True)
    return character_dir


def _record_state_change(character_name: str, change_type: str, value: str, metadata: dict = None):
    """Append a state change (location/activity) to state_history DB table.

    Lightweight log used by the Diary to show location/activity changes.
    Max 200 entries per character (oldest trimmed).
    Optional metadata dict is stored alongside (e.g. effect changes).
    """
    ts = datetime.now().isoformat()
    state_entry = {
        "timestamp": ts,
        "type": change_type,
        "value": value,
    }
    if metadata:
        state_entry["metadata"] = metadata
    try:
        with transaction() as conn:
            conn.execute("""
                INSERT INTO state_history (character_name, ts, state_json)
                VALUES (?, ?, ?)
            """, (character_name, ts, json.dumps(state_entry, ensure_ascii=False)))
        # Trim: max 200 entries per character
        conn = get_connection()
        total = conn.execute(
            "SELECT COUNT(*) FROM state_history WHERE character_name=?",
            (character_name,),
        ).fetchone()[0]
        if total > 200:
            excess = total - 200
            conn.execute("""
                DELETE FROM state_history WHERE id IN (
                    SELECT id FROM state_history
                    WHERE character_name=?
                    ORDER BY ts ASC LIMIT ?
                )
            """, (character_name, excess))
            conn.commit()
    except Exception as e:
        logger.debug("_record_state_change DB-Fehler fuer %s: %s", character_name, e)


def record_access_denied(character_name: str,
    location_id: str,
    location_name: str,
    reason: str,
    rule_name: str = "",
    action: str = "enter") -> None:
    """Protokolliert einen verweigerten Ortswechsel fuers Tagebuch.

    ``action`` unterscheidet Eintritts- ("enter") und Verlassens-Blockaden
    ("leave"). Wird ins Metadata-Dict geschrieben, damit Diary-Renderer und
    Recent-Activity die Richtung anzeigen koennen.
    """
    metadata = {
        "location_id": location_id,
        "location_name": location_name,
        "reason": reason,
        "action": action,
    }
    if rule_name:
        metadata["rule_name"] = rule_name
    _record_state_change(character_name, "access_denied", location_name or location_id, metadata
    )


def _replace_last_state_entry(character_name: str, change_type: str, value: str, metadata: dict = None):
    """Replace the last state_history entry of the given type (used for reclassification).

    Existing metadata keys are preserved unless overridden by the new metadata dict.
    Reclassification only renames the activity — partner/detail/etc. that were
    already attached to the prior entry must survive the rename.
    """
    try:
        conn = get_connection()
        # Find the most recent row of the given type
        rows = conn.execute(
            "SELECT id, state_json FROM state_history "
            "WHERE character_name=? ORDER BY ts DESC LIMIT 50",
            (character_name,),
        ).fetchall()
        for db_id, state_json_str in rows:
            state = {}
            try:
                state = json.loads(state_json_str or "{}")
            except Exception:
                pass
            if state.get("type") == change_type:
                state["value"] = value
                existing_meta = state.get("metadata") or {}
                if not isinstance(existing_meta, dict):
                    existing_meta = {}
                if metadata:
                    existing_meta.update(metadata)
                if existing_meta:
                    state["metadata"] = existing_meta
                elif "metadata" in state:
                    del state["metadata"]
                with transaction() as wconn:
                    wconn.execute(
                        "UPDATE state_history SET state_json=? WHERE id=?",
                        (json.dumps(state, ensure_ascii=False), db_id),
                    )
                return
    except Exception as e:
        logger.debug("_replace_last_state_entry DB-Fehler fuer %s: %s", character_name, e)
    # No existing entry found — add new
    _record_state_change(character_name, change_type, value, metadata)


# Runtime-Keys: werden in character_state gespeichert, in profile_json NICHT.
# get_character_profile injiziert sie in die Rueckgabe, save_character_profile
# extrahiert sie vor dem Persistieren von profile_json.
_STATE_COLS = ("current_location", "current_room", "current_activity",
               "current_feeling", "location_changed_at", "activity_changed_at")

# Weitere Runtime-Keys — landen in character_state.meta (JSON-Blob) statt
# in profile_json. Inject/Extract analog zu _STATE_COLS.
_STATE_META_KEYS = ("equipped_pieces", "equipped_items", "equipped_pieces_meta",
                    "active_conditions", "status_effects", "activity_cooldowns",
                    "runtime_outfit_skip",
                    "current_activity_detail",
                    "movement_target")

# Per-Character User-Config (nicht Stamm) — wandern in config_json, nicht profile_json.
# Beim Laden aus config_json in Profile injiziert (fuer Abwaerts-Kompatibilitaet der
# Consumer-APIs), beim Save extrahiert.
_CONFIG_KEYS_IN_PROFILE = ("outfit_exceptions", "outfit_imagegen", "slot_overrides",
                           "no_outfit_prompt_top", "no_outfit_prompt_bottom",
                           "no_outfit_prompt")


def _load_character_state(character_name: str) -> Dict[str, Any]:
    """Laedt character_state: Spalten (current_*, *_changed_at) + meta-Keys."""
    result: Dict[str, Any] = {k: "" for k in _STATE_COLS}
    for k in _STATE_META_KEYS:
        result[k] = None
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT current_location, current_room, current_activity, "
            "current_feeling, location_changed_at, activity_changed_at, meta "
            "FROM character_state WHERE character_name=?",
            (character_name,),
        ).fetchone()
        if row:
            result.update({
                "current_location": row[0] or "",
                "current_room": row[1] or "",
                "current_activity": row[2] or "",
                "current_feeling": row[3] or "",
                "location_changed_at": row[4] or "",
                "activity_changed_at": row[5] or "",
            })
            try:
                meta = json.loads(row[6] or "{}")
            except Exception:
                meta = {}
            for k in _STATE_META_KEYS:
                if k in meta:
                    result[k] = meta[k]
    except Exception:
        pass
    # None-Platzhalter entfernen, damit profile.get(k) fehlende Keys meldet
    return {k: v for k, v in result.items() if v is not None}


def get_character_profile(character_name: str) -> Dict[str, Any]:
    """Laedt das Profil eines Characters aus der DB.

    Runtime-State (current_location/room/activity/feeling + *_changed_at)
    wird aus character_state injiziert, nicht aus profile_json.
    Soul-MD-Files (tasks.md, personality.md etc.) werden ueber
    _inject_soul_md_values eingelesen — beim Speichern werden diese Felder
    aus profile_json entfernt, also muessen sie beim Lesen re-injiziert
    werden, sonst fehlt z.B. character_task im ThoughtLoop.
    Fallback auf JSON-Datei falls kein DB-Eintrag vorhanden.
    """
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT profile_json, config_json, template FROM characters WHERE name=?",
            (character_name,),
        ).fetchone()
        if row:
            profile = json.loads(row[0] or "{}")
            profile.setdefault("character_name", character_name)
            profile.update(_load_character_state(character_name))
            # Per-Character User-Config aus config_json in profile injizieren
            # (Abwaertskompatibilitaet fuer Consumer-Code).
            try:
                cfg = json.loads(row[1] or "{}")
                for k in _CONFIG_KEYS_IN_PROFILE:
                    if k in cfg and k not in profile:
                        profile[k] = cfg[k]
            except Exception:
                pass
            # Template-Name aus eigener Spalte ins Profile injizieren —
            # is_feature_enabled() liest profile["template"], um die
            # Feature-Defaults zu finden. Ohne diese Zeile fielen alle
            # Characters auf "human-default" zurueck und sahen damit
            # einen Default-Set mit allen Features = False.
            tmpl_name = (row[2] or "").strip()
            if tmpl_name and "template" not in profile:
                profile["template"] = tmpl_name
            _inject_soul_md_values(character_name, profile)
            return profile
    except Exception as e:
        logger.debug("get_character_profile DB-Fehler fuer %s: %s", character_name, e)

    # Fallback: JSON-Datei (vor Migration oder bei DB-Fehler)
    character_dir = get_character_dir(character_name)
    profile_path = character_dir / "character_profile.json"
    old_path = character_dir / "profile.json"
    if not profile_path.exists() and old_path.exists():
        old_path.rename(profile_path)
        logger.info("Migration: %s -> %s", old_path, profile_path)

    if profile_path.exists():
        try:
            _p = json.loads(profile_path.read_text())
            _inject_soul_md_values(character_name, _p)
            return _p
        except Exception:
            pass

    return {
        "character_name": character_name,
        "character_personality": "",
        "character_appearance": "",
        "created_by": ""
    }


def _inject_soul_md_values(character_name: str, profile: Dict[str, Any]) -> None:
    """Injiziert Werte aus soul/*.md Files in profile-Dict.

    Beim Save werden source_file-Felder aus profile_json entfernt (MD = Source
    of Truth). Beim Load muss der Wert aus der MD wieder injiziert werden,
    sonst bekommen Consumer wie ThoughtLoop (character_task) oder
    system_prompt_builder (personality, beliefs etc.) leere Strings.

    Nicht-destruktiv: bestehende Werte im profile werden NICHT ueberschrieben
    (falls Caller etwas gesetzt hat). Fehlt das Feld → von MD lesen.
    """
    try:
        template_id = profile.get("template", "")
        if not template_id:
            return
        from app.models.character_template import get_template
        tmpl = get_template(template_id)
        if not tmpl:
            return
        char_dir = get_character_dir(character_name)
        for section in tmpl.get("sections", []):
            for field in section.get("fields", []):
                source_file = field.get("source_file")
                if not source_file:
                    continue
                key = field.get("key", "")
                if not key or profile.get(key):
                    continue
                md_path = char_dir / source_file
                if not md_path.exists():
                    continue
                try:
                    raw = md_path.read_text(encoding="utf-8")
                except Exception:
                    continue
                body = _extract_md_body(raw)
                if body:
                    profile[key] = body
    except Exception as e:
        logger.debug("_inject_soul_md_values fuer %s: %s", character_name, e)


def _extract_md_body(md_text: str) -> str:
    """Liefert den Body einer Soul-MD ohne den ersten Titel-Header.

    Erste Zeile beginnt typischerweise mit '# Titel'. Wir entfernen genau diese,
    der Rest (## Sections + Body) bleibt als Markdown erhalten, weil Consumer
    den Inhalt im Prompt-Kontext als formatiertes Markdown weiterverarbeiten.
    """
    if not md_text:
        return ""
    lines = md_text.splitlines()
    out = []
    header_consumed = False
    for line in lines:
        if not header_consumed and line.strip().startswith("# ") and not line.strip().startswith("## "):
            header_consumed = True
            continue
        out.append(line)
    # Fuehrende Leerzeilen abschneiden
    while out and not out[0].strip():
        out.pop(0)
    return "\n".join(out).rstrip()


def get_character_language(character_name: str) -> str:
    """Gibt den Sprachcode des Characters zurueck (z.B. 'de', 'en'). Fallback: 'de'."""
    profile = get_character_profile(character_name)
    return profile.get("language", "") or "de"


def get_character_language_instruction(character_name: str) -> str:
    """Erzeugt die Sprach-Anweisung fuer den System-Prompt aus dem Character-Profil.

    Liest das 'language'-Feld aus dem Character-Profil und erzeugt daraus
    eine Anweisung wie 'Always respond in German.' Falls kein Sprachfeld
    gesetzt ist, Fallback auf User-Level language_instruction.
    """
    profile = get_character_profile(character_name)
    lang_code = profile.get("language", "")

    if lang_code:
        lang_name = LANGUAGE_MAP.get(lang_code, lang_code)
        return f"Always respond in {lang_name}."

    # Fallback: User-Level language_instruction
    from app.models.account import get_user_language_instruction
    return get_user_language_instruction()


_RESERVED_NAMES = {"user", "admin", "system", "default", "player", "",
                   "undefined", "null", "none", "nan"}


def save_character_profile(character_name: str, profile: Dict[str, Any],
                           create_new: bool = False):
    """Speichert das Profil eines Characters in der DB.

    Stellt sicher dass die Soul-MD-Dateien gemaess Template existieren
    (legt fehlende aus shared/templates/soul/ an).

    ``create_new``: nur die explizite Charakter-Erstellung (POST /characters/create)
    darf neue Charaktere anlegen. Alle anderen Callsites sind Updates fuer
    BESTEHENDE Charaktere — wenn der Name unbekannt ist, wird das als Bug
    behandelt (z.B. ein LLM hat "Lirien" statt "Lirien Edwinsdottir"
    durchgereicht) und das Schreiben verworfen.
    """
    if character_name.lower() in _RESERVED_NAMES:
        logger.warning("save_character_profile: reservierter Name '%s' uebersprungen",
                       character_name)
        return

    # Existenz-Check — wenn nicht create_new, muss Character bereits existieren
    if not create_new:
        _exists = False
        try:
            conn = get_connection()
            _row = conn.execute(
                "SELECT 1 FROM characters WHERE name=? LIMIT 1",
                (character_name,)).fetchone()
            _exists = bool(_row)
        except Exception:
            pass
        if not _exists:
            try:
                _exists = (get_user_characters_dir() / character_name).is_dir()
            except Exception:
                pass
        if not _exists:
            logger.warning(
                "save_character_profile: Character '%s' existiert nicht — "
                "kein Save (Geister-Character verhindert). Wenn das ein "
                "neuer Charakter sein soll, ueber POST /characters/create "
                "anlegen.", character_name)
            return

    character_dir = get_character_dir(character_name, create=True)
    profile_path = character_dir / "character_profile.json"

    profile["character_name"] = character_name
    profile["created_by"] = ""
    # Legacy-Felder aus dem alten Outfit-System entfernen — nicht mehr genutzt.
    for _legacy in ("current_outfit", "outfit_last_changed", "outfit_last_location"):
        profile.pop(_legacy, None)

    # Runtime-State aus profile extrahieren — wird in character_state persistiert,
    # nicht in profile_json. Nur Keys die tatsaechlich im Dict sind werden uebertragen
    # (fehlende Keys lassen den bestehenden State unveraendert).
    state_values: Dict[str, str] = {}
    for k in _STATE_COLS:
        if k in profile:
            state_values[k] = profile.pop(k) or ""
    state_meta: Dict[str, Any] = {}
    for k in _STATE_META_KEYS:
        if k in profile:
            state_meta[k] = profile.pop(k)
    # Per-Character User-Config aus profile rausziehen → wandert in config_json
    config_patch: Dict[str, Any] = {}
    for k in _CONFIG_KEYS_IN_PROFILE:
        if k in profile:
            config_patch[k] = profile.pop(k)
    # JSON-Sidecar und DB-Blob bekommen das gestrippte Profil
    profile_to_store = profile

    # In DB schreiben
    now = datetime.now().isoformat()
    try:
        with transaction() as conn:
            # Vorhandenes config_json lesen um Config-Patch zu mergen
            existing_config: Dict[str, Any] = {}
            _crow = conn.execute(
                "SELECT config_json FROM characters WHERE name=?",
                (character_name,),
            ).fetchone()
            if _crow:
                try:
                    existing_config = json.loads(_crow[0] or "{}")
                except Exception:
                    existing_config = {}
            if config_patch:
                existing_config.update(config_patch)
            conn.execute("""
                INSERT INTO characters (name, template, profile_json, config_json,
                    created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    template=excluded.template,
                    profile_json=excluded.profile_json,
                    config_json=excluded.config_json,
                    updated_at=excluded.updated_at
            """, (
                character_name,
                profile_to_store.get("template", ""),
                json.dumps(profile_to_store, ensure_ascii=False),
                json.dumps(existing_config, ensure_ascii=False),
                now,
                now,
            ))
            # Bestehenden State laden — wir updaten nur die Spalten die der Caller
            # mitgeliefert hat, damit ein reiner Profil-Save (ohne Runtime-Keys)
            # den State nicht versehentlich leert.
            existing_state = {c: "" for c in _STATE_COLS}
            existing_meta: Dict[str, Any] = {}
            _srow = conn.execute(
                "SELECT current_location, current_room, current_activity, "
                "current_feeling, location_changed_at, activity_changed_at, meta "
                "FROM character_state WHERE character_name=?",
                (character_name,),
            ).fetchone()
            if _srow:
                existing_state = {
                    "current_location": _srow[0] or "",
                    "current_room": _srow[1] or "",
                    "current_activity": _srow[2] or "",
                    "current_feeling": _srow[3] or "",
                    "location_changed_at": _srow[4] or "",
                    "activity_changed_at": _srow[5] or "",
                }
                try:
                    existing_meta = json.loads(_srow[6] or "{}")
                except Exception:
                    existing_meta = {}
            merged_state = dict(existing_state)
            merged_state.update(state_values)
            existing_meta.update(state_meta)
            meta_json = json.dumps(existing_meta, ensure_ascii=False)
            conn.execute("""
                INSERT INTO character_state
                (character_name, current_location, current_room,
                 current_activity, current_feeling,
                 location_changed_at, activity_changed_at, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(character_name) DO UPDATE SET
                    current_location=excluded.current_location,
                    current_room=excluded.current_room,
                    current_activity=excluded.current_activity,
                    current_feeling=excluded.current_feeling,
                    location_changed_at=excluded.location_changed_at,
                    activity_changed_at=excluded.activity_changed_at,
                    meta=excluded.meta
            """, (
                character_name,
                merged_state["current_location"],
                merged_state["current_room"],
                merged_state["current_activity"],
                merged_state["current_feeling"],
                merged_state["location_changed_at"],
                merged_state["activity_changed_at"],
                meta_json,
            ))
    except Exception as e:
        logger.error("save_character_profile DB-Fehler fuer %s: %s", character_name, e)

    # Caller erwartet dass der uebergebene Dict weiterhin die Runtime-Keys hat
    profile.update(state_values)
    for k, v in state_meta.items():
        profile[k] = v

    # Soul-Files anlegen falls noetig (Template-getrieben).
    # Falls World Dev / Neu-Anlage Werte fuer source_file-Felder mitliefert,
    # werden diese in MD geschrieben (populate_soul_files_from_profile),
    # danach aus dem Profil geloescht.
    try:
        ensure_soul_files(character_name)
        populate_soul_files_from_profile(character_name)
        # source_file Werte aus dem Profil-JSON entfernen — MD ist Source
        from app.models.character_template import get_template as _gt
        _tmpl = _gt(profile.get("template", ""))
        if _tmpl:
            for _section in _tmpl.get("sections", []):
                for _field in _section.get("fields", []):
                    _key = _field.get("key", "")
                    if _key and _field.get("source_file") and _key in profile:
                        del profile[_key]
    except Exception as _se:
        logger.debug("ensure/populate soul files failed for %s: %s", character_name, _se)


def delete_character(character_name: str) -> bool:
    """Entfernt einen Character vollstaendig: DB-Zeilen + Storage-Verzeichnis.

    Sweept alle Tabellen mit einer character_name-Spalte sowie die
    Sonderfaelle (relationships.from_char/to_char, llm_call_stats.agent_name,
    chat_messages.partner). Loescht anschliessend characters/<name>/.
    """
    if not character_name or character_name.lower() in _RESERVED_NAMES:
        return False

    # 1) DB sweep
    try:
        with transaction() as conn:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            for tbl in tables:
                cols = {c[1] for c in conn.execute(f"PRAGMA table_info({tbl})").fetchall()}
                if "character_name" in cols:
                    conn.execute(f"DELETE FROM {tbl} WHERE character_name=?", (character_name,))
            # Sonderspalten — Tabellen, deren Schluessel anders heisst:
            # characters.name, relationships.from_char/to_char,
            # llm_call_stats.agent_name, chat_messages.partner.
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='characters'").fetchone():
                conn.execute("DELETE FROM characters WHERE name=?", (character_name,))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='relationships'").fetchone():
                conn.execute("DELETE FROM relationships WHERE from_char=? OR to_char=?",
                             (character_name, character_name))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='llm_call_stats'").fetchone():
                conn.execute("DELETE FROM llm_call_stats WHERE agent_name=?", (character_name,))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='chat_messages'").fetchone():
                conn.execute("DELETE FROM chat_messages WHERE partner=?", (character_name,))
    except Exception as e:
        logger.error("delete_character DB-Fehler fuer %s: %s", character_name, e)
        return False

    # 2) Storage-Verzeichnis entfernen
    try:
        char_dir = get_user_characters_dir() / character_name
        if char_dir.exists():
            import shutil
            shutil.rmtree(char_dir)
    except Exception as e:
        logger.warning("delete_character: Verzeichnis fuer %s nicht entfernbar: %s",
                       character_name, e)

    logger.info("Character '%s' geloescht (DB + Storage)", character_name)
    return True


def get_character_personality(character_name: str) -> str:
    """Gibt die Persoenlichkeit eines Characters zurueck"""
    if not character_name:
        return ""
    profile = get_character_profile(character_name)
    return profile.get("character_personality", "")


def save_character_personality(character_name: str, personality: str):
    """Speichert die Persoenlichkeit eines Characters"""
    profile = get_character_profile(character_name)
    profile["character_personality"] = personality
    save_character_profile(character_name, profile)


def _get_character_config_path(character_name: str, *, create: bool = False) -> Path:
    """Pfad zur Character-Config JSON-Datei.

    Wenn ``create=False`` (Default), wird das Character-Verzeichnis NICHT
    angelegt — das ist wichtig für reine Read-Pfade (``get_character_config``),
    weil sonst eine bloße Status-Abfrage über einen Geister-Character
    (z. B. ein längst gelöschter, der nur noch im Browser-State des Users
    steckt) das Verzeichnis neu erschafft und damit die "Existence"-Heuristik
    in ``get_character_config`` triggert, die wiederum eine leere DB-Reihe
    schreibt. ``create=True`` nutzt nur ``save_character_config``.

    Migration: Benennt alte agent_config.json / llm_config.json automatisch um.
    """
    character_dir = get_character_dir(character_name, create=create)
    new_path = character_dir / "character_config.json"
    # Migration läuft nur wenn das Verzeichnis schon existiert (read- und
    # write-Pfade); für nicht-existierende Characters ist nichts zu migrieren.
    if not character_dir.exists():
        return new_path
    # Migration from agent_config.json
    old_agent_path = character_dir / "agent_config.json"
    if not new_path.exists() and old_agent_path.exists():
        old_agent_path.rename(new_path)
        logger.info("Migration: %s -> %s", old_agent_path, new_path)
    # Migration from llm_config.json
    old_llm_path = character_dir / "llm_config.json"
    if not new_path.exists() and old_llm_path.exists():
        old_llm_path.rename(new_path)
        logger.info("Migration: %s -> %s", old_llm_path, new_path)
    return new_path


def _get_character_defaults() -> Dict[str, Any]:
    """Character-Default-Config. LLM-Wahl erfolgt zentral ueber den Router."""
    return {
        "telegram_bot_token": "",
        # Telegram has no avatar selector — every incoming message must
        # belong to SOME in-world character. Set this to the avatar that
        # the human on the other end controls; otherwise messages get
        # tagged with an empty partner and disappear into limbo.
        "telegram_partner_character": "",
        "tts_enabled": True,
        "tts_auto": False,
        "tts_voice": "",
        "tts_speaker_wav": "",
        "tool_format": "auto",
        # Agent-Loop scheduling weight: 1=Low, 2=Medium, 3=High. High agents
        # get picked 3x as often as Low. See app/core/agent_loop.py.
        "importance": 1,
    }


def get_character_config(character_name: str) -> Dict[str, Any]:
    """Gibt die per-Character Konfiguration zurueck (LLM, Vision, Telegram, etc.).

    Das Profil-Feld 'language' wird automatisch als 'tts_language' injiziert,
    damit die TTS-Kette die richtige Sprache verwendet.
    """
    if not character_name:
        return {}

    # Reserved names (system, admin, user, ...) are not real characters;
    # return defaults without writing anything to disk. Without this guard
    # the auto-create branch below would call save_character_config which
    # rightly skips and emits a warning per call.
    if character_name.lower() in _RESERVED_NAMES:
        return dict(_get_character_defaults(), name=character_name)

    config = None

    # Versuche aus DB zu lesen
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT config_json FROM characters WHERE name=?", (character_name,)
        ).fetchone()
        if row and row[0] and row[0] != "{}":
            config = json.loads(row[0])
    except Exception as e:
        logger.debug("get_character_config DB-Fehler fuer %s: %s", character_name, e)

    # Fallback: JSON-Datei
    if not config:
        config_path = _get_character_config_path(character_name)
        if config_path.exists():
            try:
                with open(config_path) as f:
                    config = json.load(f)
            except (json.JSONDecodeError, IOError):
                config = None

    if config is None:
        # Auto-Create darf nur greifen wenn der Character TATSAECHLICH existiert
        # (Profil-Row in DB ODER Character-Verzeichnis). Sonst legt jeder
        # get_character_config("Lirien")-Aufruf — z.B. weil ein LLM den
        # Vornamen statt des vollen Namens nennt — eine neue Geister-Row an.
        config = _get_character_defaults()
        _exists = False
        try:
            conn = get_connection()
            _row = conn.execute(
                "SELECT 1 FROM characters WHERE name=? LIMIT 1",
                (character_name,)
            ).fetchone()
            _exists = bool(_row)
        except Exception:
            pass
        if not _exists:
            try:
                _exists = (get_user_characters_dir() / character_name).is_dir()
            except Exception:
                pass
        if _exists:
            save_character_config(character_name, config)
            logger.info("Auto-Config fuer %s erstellt", character_name)
        else:
            logger.debug("get_character_config: '%s' existiert nicht — "
                         "Defaults nur in-memory zurueck (kein DB-Write)",
                         character_name)
    else:
        # Fill in fields that have a default but are missing in the stored
        # config — keeps existing characters in sync when new fields ship.
        for _key, _default in _get_character_defaults().items():
            config.setdefault(_key, _default)

    # Name injizieren (fuer TTS etc.)
    config["name"] = character_name

    # Sprache aus Profil als tts_language injizieren (Profil hat Vorrang)
    profile_lang = get_character_language(character_name)
    if profile_lang:
        config["tts_language"] = profile_lang

    return config


def save_character_config(character_name: str, config: Dict[str, Any]):
    """Speichert die per-Character Konfiguration in DB.

    Akzeptiert nur Updates fuer Characters die bereits per save_character_profile
    angelegt wurden (oder ein Verzeichnis haben). So legt ein versehentlich aus
    einem LLM-Output durchgereichter Vorname (z.B. "Lirien" statt
    "Lirien Edwinsdottir") keinen neuen Geister-Character an.
    """
    if character_name.lower() in _RESERVED_NAMES:
        logger.warning("save_character_config: reservierter Name '%s' uebersprungen",
                       character_name)
        return

    # Existenz-Check: Row in characters-Tabelle ODER Verzeichnis vorhanden
    try:
        conn = get_connection()
        _row = conn.execute(
            "SELECT 1 FROM characters WHERE name=? LIMIT 1",
            (character_name,)).fetchone()
        _exists = bool(_row)
    except Exception:
        _exists = False
    if not _exists:
        try:
            _exists = (get_user_characters_dir() / character_name).is_dir()
        except Exception:
            pass
    if not _exists:
        logger.warning("save_character_config: Character '%s' existiert nicht — "
                       "kein DB-Insert (Geister-Character verhindert)",
                       character_name)
        return

    now = datetime.now().isoformat()
    try:
        with transaction() as conn:
            conn.execute("""
                INSERT INTO characters (name, template, profile_json, config_json,
                    created_at, updated_at)
                VALUES (?, '', '{}', ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    config_json=excluded.config_json,
                    updated_at=excluded.updated_at
            """, (
                character_name,
                json.dumps(config, ensure_ascii=False),
                now,
                now,
            ))
    except Exception as e:
        logger.error("save_character_config DB-Fehler fuer %s: %s", character_name, e)


def get_character_appearance(character_name: str) -> str:
    """Gibt das Aussehen eines Characters zurueck (Platzhalter aufgeloest)"""
    if not character_name:
        return ""
    profile = get_character_profile(character_name)
    appearance = profile.get("character_appearance", "")
    if appearance and '{' in appearance:
        appearance = resolve_outfit_placeholders(appearance, character_name)
    return appearance


def save_character_appearance(character_name: str, appearance: str):
    """Speichert das Aussehen eines Characters"""
    profile = get_character_profile(character_name)
    profile["character_appearance"] = appearance
    save_character_profile(character_name, profile)


def get_character_current_location(character_name: str = "") -> str:
    """Gibt den aktuellen Aufenthaltsort des Characters zurueck (Character-Level)."""
    if not character_name:
        return ""
    profile = get_character_profile(character_name)
    return profile.get("current_location", "")


def get_known_locations(character_name: str) -> List[str]:
    """Liefert die known_locations-Liste eines Characters (immer eine Liste).

    Strict membership in ``location_visible_to_character`` — leere Liste
    bedeutet "kennt nichts", Auto-Discovery beim Betreten und der Discover-
    Regel-Mechanismus erweitern die Liste schrittweise.
    """
    if not character_name:
        return []
    cfg = get_character_config(character_name) or {}
    val = cfg.get("known_locations")
    if isinstance(val, list):
        return [str(v) for v in val if v]
    return []


def add_known_location(character_name: str, location_id: str) -> List[str]:
    """Fuegt eine Location-ID zur known_locations-Liste hinzu (idempotent).

    Erstellt das Feld falls noch nicht vorhanden (aktiviert damit strict mode
    fuer diesen Character). Gibt die aktualisierte Liste zurueck.
    """
    if not character_name or not location_id:
        return []
    cfg = get_character_config(character_name) or {}
    existing = cfg.get("known_locations")
    if not isinstance(existing, list):
        existing = []
    known = [str(v) for v in existing if v]
    if location_id not in known:
        known.append(location_id)
    cfg["known_locations"] = known
    save_character_config(character_name, cfg)
    return known


def _schedule_background_variant(character_name: str) -> None:
    """Triggert Expression-Variant fuer aktuelle Mood/Activity/Equipped im
    Hintergrund — damit beim naechsten Character-Wechsel bereits ein
    frisches Bild im Cache liegt. Kein Fehler wenn nicht moeglich.
    """
    if not character_name:
        return
    try:
        from app.core.expression_regen import trigger_expression_generation
        from app.models.inventory import get_equipped_pieces, get_equipped_items
        profile = get_character_profile(character_name) or {}
        mood = profile.get("current_feeling", "") or ""
        activity = profile.get("current_activity", "") or ""
        eq_p = get_equipped_pieces(character_name)
        eq_i = get_equipped_items(character_name)
        eq_meta = profile.get("equipped_pieces_meta") or {}
        trigger_expression_generation(
            character_name, mood, activity,
            equipped_pieces=eq_p, equipped_items=eq_i,
            equipped_pieces_meta=eq_meta,
            ignore_cooldown=False)  # Cooldown respektieren
    except Exception as _e:
        logger.debug("Background-Variant-Trigger fuer %s fehlgeschlagen: %s",
                     character_name, _e)


def get_movement_target(character_name: str) -> str:
    """Liefert die aktuell anvisierte Reise-Ziel-Location-ID (oder '')."""
    if not character_name:
        return ""
    profile = get_character_profile(character_name) or {}
    return (profile.get("movement_target") or "").strip()


def set_movement_target(character_name: str, location_id: str) -> None:
    """Setzt das Reise-Ziel. Wird vom Agent-Loop pro Tick um einen Schritt
    abgearbeitet. Lege es nur, wenn ein Pfad ueber bekannte Locations
    existiert — Validierung im Aufrufer.
    """
    if not character_name:
        return
    profile = get_character_profile(character_name)
    profile["movement_target"] = (location_id or "").strip()
    save_character_profile(character_name, profile)


def clear_movement_target(character_name: str) -> None:
    """Loescht das Reise-Ziel — z.B. nach Ankunft, Teleport-Override oder
    wenn der Pfad ploetzlich nicht mehr begehbar ist."""
    set_movement_target(character_name, "")


def save_character_current_location(character_name: str = "", location: str = "",
                                    _skip_compliance: bool = False,
                                    _preserve_movement_target: bool = False):
    """Speichert den aktuellen Aufenthaltsort.

    _skip_compliance: wenn True, ueberspringt Outfit-Type-Compliance
    (z.B. fuer den Avatar, der manuelle Outfit-Wahl behaelt).
    _preserve_movement_target: True nur bei programmiertem Walk-Step im
    Agent-Loop. Default False = Manueller Teleport (Drag&Drop, Admin,
    Scheduler-Force, Rule-Force) loescht das Ziel automatisch — der
    User/das System hat die Reise gerade ueberschrieben. Bei True und
    ``location == movement_target`` wird das Ziel ebenfalls geloescht
    (Ankunft).
    """
    from datetime import datetime
    profile = get_character_profile(character_name)
    old_location = profile.get("current_location", "")
    target = (profile.get("movement_target") or "").strip()
    location_changed = bool(location) and location != old_location
    if location_changed and target:
        # Bei manuellem Teleport (kein _preserve_movement_target) bricht
        # die Reise ab — Aufrufer hat das Ziel ueberschrieben.
        # Bei programmiertem Walk-Step wird das Ziel nur bei Ankunft
        # geloescht.
        if not _preserve_movement_target:
            profile["movement_target"] = ""
        elif location == target:
            profile["movement_target"] = ""
    profile["current_location"] = location
    profile["location_changed_at"] = datetime.now().isoformat()
    # current_room beim Location-Wechsel leeren — sonst zeigt ein Char an
    # Location A weiter auf einen Raum von Location B (stale Reference).
    # Caller (SetLocation-Skill, Scheduler etc.) kann nach diesem Aufruf
    # einen passenden Raum am neuen Ort explizit setzen.
    if location and location != old_location and profile.get("current_room"):
        profile["current_room"] = ""
    # runtime_outfit_skip zuruecksetzen bei echtem Location-Wechsel: die
    # absichtlich-leeren Slots aus dem Chat ("zieht sich aus") galten fuer
    # die alte Location. Am neuen Ort greift wieder die normale outfit-Regel.
    if location and location != old_location and profile.get("runtime_outfit_skip"):
        profile["runtime_outfit_skip"] = []
    save_character_profile(character_name, profile)
    # Location-History aufzeichnen (nur bei echtem Wechsel)
    if location and location != old_location:
        _record_state_change(character_name, "location", location)
        # Auto-Discovery: Wer einen Ort tatsaechlich betritt kennt ihn ab
        # jetzt — sonst kann er nicht zurueck (visibility-restricted Travel
        # wuerde den eigenen Standort als unbekannt sehen). Sicher idempotent;
        # add_known_location legt das Feld an falls noch nicht vorhanden.
        try:
            add_known_location(character_name, location)
        except Exception:
            pass
    # Outfit-Type-Compliance: tausche nicht-passende equipped Pieces gegen
    # Inventar-Pieces mit passendem Typ. Nur wenn nicht _skip_compliance
    # und tatsaechlich Location gewechselt wurde.
    # WICHTIG: hier Location/Raum-Type direkt verwenden (NICHT resolve_helper),
    # weil die Activity beim Location-Wechsel noch die alte ist — sonst zieht
    # z.B. der Sunbathing-Type der Pool-Activity beim Umzug ins Buero durch
    # und Kira kommt halbnackt am Schreibtisch an. Activity-Type wird spaeter
    # in save_character_current_activity nachgezogen wenn Scheduler activity updated.
    if not _skip_compliance and location and location != old_location:
        try:
            from app.models.world import get_location_by_id
            from app.models.inventory import apply_outfit_type_compliance
            from app.core.outfit_rules import default_outfit_type
            loc_data = get_location_by_id(location)
            target_type = ""
            if loc_data:
                room_id = profile.get("current_room", "")
                if room_id:
                    for r in (loc_data.get("rooms") or []):
                        if r.get("id") == room_id:
                            target_type = (r.get("outfit_type") or "").strip()
                            break
                if not target_type:
                    target_type = (loc_data.get("outfit_type") or "").strip()
            # Fallback auf den als default markierten outfit_type aus den Regeln
            if not target_type:
                target_type = default_outfit_type()
            if target_type:
                apply_outfit_type_compliance(character_name, target_type)
        except Exception as _e:
            from app.core.log import get_logger
            get_logger("character_model").debug(
                "Outfit-Compliance bei Location-Wechsel fehlgeschlagen: %s", _e)

    # Hintergrund-Variant fuer den neuen Ort vorgenerieren
    if location and location != old_location:
        _schedule_background_variant(character_name)


def get_location_changed_at(character_name: str = "") -> str:
    """Gibt den Timestamp des letzten Location-Wechsels zurueck (Character-Level)."""
    if not character_name:
        return ""
    profile = get_character_profile(character_name)
    return profile.get("location_changed_at", "")


def get_character_current_activity(character_name: str) -> str:
    """Gibt die aktuelle Aktivitaet zurueck"""
    if not character_name:
        return ""
    profile = get_character_profile(character_name)
    return profile.get("current_activity", "")


def _normalize_activity_name(character_name: str, activity: str) -> tuple:
    """Normalisiert einen Aktivitaetsnamen gegen die Bibliothek.

    Wenn die Aktivitaet in der Bibliothek gefunden wird, wird der kanonische
    Name zurueckgegeben. Der Original-Freitext wird als Detail behalten.

    Returns: (normalized_name, detail_or_empty)
    """
    if not activity:
        return activity, ""
    try:
        from app.models.activity_library import get_library_activity, find_library_activity_by_name
        # Erst nach ID, dann nach Name (inkl. Stemming)
        lib_act = get_library_activity(activity)
        if not lib_act:
            lib_act = find_library_activity_by_name(activity)
        if lib_act:
            canonical = lib_act.get("name", activity)
            if canonical.lower() != activity.lower():
                return canonical, activity  # Freitext als Detail behalten
            return canonical, ""
    except Exception:
        pass
    return activity, ""


def _decide_partner_on_change(profile: dict, new_activity: str, location_id: str):
    """Entscheidet was beim Activity-Wechsel mit activity_partner passiert.

    Returns:
      str(partner_name) — vererben (neue Activity ist requires_partner und
                          alter Partner ist am gleichen Ort)
      ""               — verwerfen (klar Solo: Library-Activity ohne
                          requires_partner ODER Partner abwesend)
      None             — unentschieden (Freitext, nicht in Library) — der
                          Aufrufer soll activity_partner unangetastet lassen.
                          Der nachgelagerte Background-Classifier setzt den
                          State-History-Eintrag dann mit Partner-Tag, sobald
                          die Reklassifizierung greift.

    Damit bleibt der Partner ueber Activity-Ketten und Chat-Marker-Freitext
    hinweg erhalten (Passionate kissing → Foreplay → Sex), ohne dass jeder
    Pfad den partner-Parameter explizit durchreichen muss.
    """
    if not new_activity or not location_id:
        return ""
    old_partner = (profile.get("activity_partner") or "").strip()
    if not old_partner:
        return ""
    try:
        from app.models.activity_library import get_library_activity, find_library_activity_by_name
        lib_act = get_library_activity(new_activity) or find_library_activity_by_name(new_activity)
        if not lib_act:
            # Unbekannter Freitext — Entscheidung verschieben.
            return None
        if not lib_act.get("requires_partner"):
            return ""
        partner_loc = get_character_current_location(old_partner) or ""
        if partner_loc != location_id:
            return ""
        return old_partner
    except Exception:
        return ""


def save_character_current_activity(character_name: str, activity: str, detail: str = "",
                                    _skip_classify: bool = False, _is_reclassify: bool = False,
                                    partner: str = "",
                                    _skip_partner_transfer: bool = False):
    """Speichert die aktuelle Aktivitaet.

    activity: Kurzer Kategorie-Name fuer Mechanik (Outfits, Effects, Conditions)
    detail: Optionale ausfuehrliche Beschreibung (fuer Prompt-Flavour, Bild-Generierung)
    partner: Optional — der Partner dieser Aktivitaet (fuer requires_partner Activities).
             Wird im Profil als activity_partner gespeichert.
    _skip_classify: Internal flag to prevent recursive classify calls
    _is_reclassify: True wenn Background-Classify den kurzen Namen nachliefert (ersetzt letzten Eintrag)
    _skip_partner_transfer: Internal flag to prevent recursion during auto partner transfer
    """
    # Sanitize: LLM-Tokenizer-Artefakte entfernen (<|END_OF_TURN_TOKEN|>, <SPECIAL_X>)
    if activity:
        import re as _re
        activity = _re.sub(r'<SPECIAL_\d+>|<\|[A-Z_][A-Z_0-9]*\|>', '', activity).strip()
    if detail:
        import re as _re
        detail = _re.sub(r'<SPECIAL_\d+>|<\|[A-Z_][A-Z_0-9]*\|>', '', detail).strip()

    # Normalisierung: Freitext gegen Bibliothek matchen
    if activity and not _is_reclassify:
        normalized, auto_detail = _normalize_activity_name(character_name, activity)
        if normalized != activity:
            if not detail:
                detail = auto_detail
            activity = normalized

    profile = get_character_profile(character_name)
    old_activity = profile.get("current_activity", "")
    old_detail = profile.get("current_activity_detail", "")

    # Duplicate-Skip: gleiche Activity (case-insensitive) UND gleiche detail
    # innerhalb der letzten 30s → no-op. Verhindert dass Tool-LLM-Doppelaufrufe
    # oder AgentLoop-Auto-Sleep wiederholt dieselbe Activity setzen + on_start
    # nochmal feuern. Reclassify ist bewusst ausgenommen (das ist genau dafuer
    # da, den Namen zu normalisieren).
    if activity and not _is_reclassify and old_activity:
        if (activity.strip().lower() == old_activity.strip().lower()
                and (detail or "").strip() == (old_detail or "").strip()):
            try:
                started_iso = (profile.get("activity_started_at") or "").strip()
                if started_iso:
                    started = datetime.fromisoformat(started_iso)
                    if (datetime.now() - started).total_seconds() < 30:
                        return  # idempotent — nichts zu tun
            except (ValueError, TypeError):
                pass

    # Zeitproportionale Effekte fuer die alte Aktivitaet abrechnen
    if activity and old_activity and activity != old_activity and not _is_reclassify:
        try:
            from app.core.activity_engine import finalize_activity_effects
            finalize_activity_effects(character_name, old_activity)
            # Profil neu laden, da finalize status_effects geaendert haben kann
            profile = get_character_profile(character_name)
        except Exception:
            pass

        # on_interrupted Trigger: alte Aktivitaet wurde vor Ablauf abgebrochen.
        # Unterscheidet sich von on_complete: on_complete feuert wenn die Aktivitaet
        # ihre duration_minutes regulaer abschliesst (Scheduler setzt activity='').
        # Hier: activity wird zu einem anderen nicht-leeren Wert geaendert.
        try:
            from app.core.activity_engine import _find_activity_definition, execute_trigger
            _old_def = _find_activity_definition(character_name, old_activity)
            if _old_def and _old_def.get("interruptible", True):
                _triggers = _old_def.get("triggers", {}) or {}
                _on_int = _triggers.get("on_interrupted")
                if _on_int:
                    execute_trigger(character_name, _on_int,
                                    context={"interrupted_activity": old_activity,
                                             "new_activity": activity})
        except Exception:
            pass

    profile["current_activity"] = activity
    if detail:
        profile["current_activity_detail"] = detail
    elif activity != old_activity:
        profile.pop("current_activity_detail", None)

    # Activity-Lifetime: bei einem ECHTEN Activity-Wechsel (nicht
    # Reclassify) den Start-Timestamp + duration_minutes aus der Library
    # ins Profil schreiben. Der World-Admin-Tick (periodic_jobs) prueft
    # zyklisch ob die Activity ihre Dauer ueberschritten hat und setzt
    # sie dann auf "" zurueck (loest on_complete-Trigger aus). Ersetzt
    # die alten APScheduler-One-Time-``activity_done_*``-Jobs.
    if activity and activity != old_activity and not _is_reclassify:
        profile["activity_started_at"] = datetime.now().isoformat()
        try:
            from app.core.activity_engine import _find_activity_definition
            _act_def = _find_activity_definition(character_name, activity) or {}
            _dur = int(_act_def.get("duration_minutes") or 0)
            if _dur > 0:
                profile["activity_duration_minutes"] = _dur
            else:
                profile.pop("activity_duration_minutes", None)
        except Exception:
            profile.pop("activity_duration_minutes", None)
    elif not activity and old_activity:
        # Activity geleert (Aufwach-/Reset-Pfad) — Lifetime-Felder weg.
        profile.pop("activity_started_at", None)
        profile.pop("activity_duration_minutes", None)

    current_location = profile.get("current_location", "")
    # Partner-Feld pflegen:
    # - explizit gesetzt -> speichern.
    # - Reklassifizierung -> activity_partner unangetastet lassen (Rename, kein
    #   Wechsel; der Vorgaenger-Save hat den Partner bereits korrekt gesetzt).
    # - sonst bei Activity-Wechsel pruefen ob die neue Activity requires_partner
    #   ist und der bisherige Partner noch am gleichen Ort — dann erben, sonst
    #   verwerfen (Solo-Wechsel oder Partner abwesend).
    if partner:
        profile["activity_partner"] = partner
    elif _is_reclassify:
        # Existierenden Partner mitfuehren, damit detail_meta unten ihn
        # mit in die Reclassify-Metadata schreibt.
        partner = (profile.get("activity_partner") or "").strip()
    elif activity != old_activity:
        decision = _decide_partner_on_change(profile, activity, current_location)
        if decision is None:
            # Freitext (Chat-Marker) — Partner unangetastet lassen, Reclassify entscheidet.
            partner = (profile.get("activity_partner") or "").strip()
        elif decision:
            profile["activity_partner"] = decision
            partner = decision
        else:
            profile.pop("activity_partner", None)
    # Raum aus Activity ableiten: wenn ein Raum am aktuellen Ort diese Activity hat,
    # Character automatisch dorthin bewegen. Damit bleiben Activity und Raum immer
    # konsistent, egal ob Chat-Marker, Tool-LLM, Skill oder Scheduler die Activity setzt.
    if activity and activity != old_activity and current_location:
        try:
            from app.models.world import get_location_by_id, find_room_by_activity
            _loc_data = get_location_by_id(current_location)
            if _loc_data:
                _matched_room = find_room_by_activity(_loc_data, activity)
                if _matched_room:
                    _new_room_id = _matched_room.get("id", "")
                    if _new_room_id and profile.get("current_room", "") != _new_room_id:
                        profile["current_room"] = _new_room_id
        except Exception:
            pass
    save_character_profile(character_name, profile)

    # on_start Trigger fuer die NEUE Activity feuern — egal welcher Pfad
    # save_character_current_activity aufruft (set_activity_skill,
    # avatar_activity_detect, thoughts, scheduler-extras). Frueher hat
    # nur set_activity_skill den Trigger gerufen, was reine
    # Keyword-Detection-Aenderungen (z.B. Avatar tippt "ich gehe schlafen")
    # ohne Wirkung liess. Reclassify nicht doppelt feuern.
    if activity and activity != old_activity and not _is_reclassify:
        try:
            from app.core.activity_engine import _find_activity_definition, execute_trigger
            _new_def = _find_activity_definition(character_name, activity)
            if _new_def:
                _triggers = _new_def.get("triggers", {}) or {}
                _on_start = _triggers.get("on_start")
                if _on_start:
                    execute_trigger(character_name, _on_start,
                                    context={"activity": activity,
                                             "previous_activity": old_activity})
        except Exception as _trig_err:
            from app.core.log import get_logger
            get_logger("character_model").debug(
                "on_start-Trigger fuer %s/%s fehlgeschlagen: %s",
                character_name, activity, _trig_err)

    # Effects-Tracking aktualisieren
    if activity:
        try:
            from app.core.activity_engine import reset_effects_tracking, update_effects_tracking_name
            if not _is_reclassify:
                reset_effects_tracking(character_name, activity)
            else:
                update_effects_tracking_name(character_name, activity)
        except Exception:
            pass

    # Activity-History aufzeichnen (nur bei echtem Wechsel)
    detail_meta = {"detail": detail} if detail and detail.lower() != activity.lower() else None
    if partner:
        detail_meta = detail_meta or {}
        detail_meta["partner"] = partner
    if activity and activity != old_activity:
        if _is_reclassify:
            # Reklassifizierung: letzten Aktivitaets-Eintrag ersetzen statt neuen anlegen
            _replace_last_state_entry(character_name, "activity", activity, metadata=detail_meta)
        else:
            _record_state_change(character_name, "activity", activity, metadata=detail_meta)

        # Outfit-Compliance bei Activity-Wechsel.
        # Nutzt resolve_target_outfit_type (Activity > Raum > Location):
        # - Activity mit explizitem outfit_type (z.B. sunbathing → pool)
        #   uebersteuert Raum-Type — korrekt, die Taetigkeit diktiert Kleidung.
        # - Activity ohne Typ → faellt auf Raum/Location zurueck — greift
        #   den Sekundaer-Fix fuer Scheduler-Reihenfolge ein: Location wurde
        #   zuerst gesetzt mit Location-Type, dann die Activity — hier wird
        #   ein evtl. fehlendes Piece (z.B. Kira kam aus Pool ins Buero mit
        #   nur Bikini) nochmal via Auto-Fill auf den richtigen Type gebracht.
        # Bei Reklassifizierung (_is_reclassify) nicht, da nur Namens-Normalisierung.
        if not _is_reclassify and not _skip_partner_transfer:
            try:
                from app.models.account import is_player_controlled
                if not is_player_controlled(character_name) and not is_outfit_locked(character_name):
                    from app.core.outfit_rules import resolve_target_outfit_type
                    from app.models.inventory import apply_outfit_type_compliance
                    _tt = resolve_target_outfit_type(character_name)
                    if _tt:
                        apply_outfit_type_compliance(character_name, _tt)
                    # Variant-Trigger neu anstossen — coalesce merged mit
                    # dem evtl. schon pending Mood-Trigger, so dass der
                    # finale Fire den NEUEN Activity-Namen/Equipped-State
                    # verwendet.
                    _schedule_background_variant(character_name)
            except Exception as _e:
                from app.core.log import get_logger
                get_logger("character_model").debug(
                    "Outfit-Compliance bei Activity-Wechsel fehlgeschlagen: %s", _e)
    # Auto-classify long activity text in background (if no detail provided = raw text as activity)
    if activity and not detail and not _skip_classify and len(activity) > 25:
        try:
            from app.core.activity_engine import classify_activity_background
            classify_activity_background(character_name, activity)
        except Exception:
            pass
    # Kurze Aktivitaeten direkt im Raum tracken (Zaehler + Auto-Add)
    elif activity and len(activity) <= 30 and not _is_reclassify:
        try:
            room_id = profile.get("current_room", "")
            if current_location and room_id:
                from app.models.world import track_room_activity
                track_room_activity(current_location, room_id, activity)
        except Exception:
            pass

    # Partner-Auto-Transfer: requires_partner Activities setzen den Partner mit.
    # Greift fuer alle Aufrufer (SetActivity-Skill, Follow-up, Scheduler, Chat-Marker).
    if (activity and activity != old_activity and not _is_reclassify
            and not _skip_partner_transfer):
        try:
            _auto_transfer_partner_activity(character_name, activity,
                current_location, explicit_partner=partner)
        except Exception as _pt_err:
            from app.core.log import get_logger
            get_logger("character").debug("Partner auto-transfer failed: %s", _pt_err)


def _auto_transfer_partner_activity(initiator: str, activity_name: str,
    location_id: str, explicit_partner: str = "") -> str:
    """Setzt die Gegenaktivitaet beim Partner, wenn die Activity requires_partner ist.

    Partner-Ermittlung (in Reihenfolge):
      1. explicit_partner (Argument)
      2. activity_partner im Initiator-Profil (aus vorherigem Transfer-Zyklus)
      3. Aktiver Avatar, wenn am gleichen Ort

    Kein Fallback auf "irgendein Character am Ort" — lieber kein Transfer als
    falscher Partner. Der Initiator behaelt seine Activity solo, sein
    State-History-Eintrag ohne Partner-Tag ist ein ehrliches Signal.

    Returns: Name des transferierten Partners oder "" wenn kein Transfer passierte.
    """
    from app.models.activity_library import (
        get_library_activity, find_library_activity_by_name)

    if not activity_name or not location_id:
        return ""
    lib_act = get_library_activity(activity_name) or find_library_activity_by_name(activity_name)
    if not lib_act or not lib_act.get("requires_partner"):
        return ""

    partner = explicit_partner or ""
    if not partner:
        prof = get_character_profile(initiator) or {}
        partner = prof.get("activity_partner", "") or ""
    if not partner:
        try:
            from app.models.account import get_active_character
            avatar = get_active_character()
            if avatar and avatar != initiator:
                if get_character_current_location(avatar) == location_id:
                    partner = avatar
        except Exception:
            pass

    if not partner or partner == initiator:
        return ""

    try:
        partner_loc = get_character_current_location(partner) or ""
    except Exception:
        partner_loc = ""
    if partner_loc != location_id:
        return ""

    partner_activity_id = lib_act.get("partner_activity", "") or ""
    partner_activity_name = activity_name
    if partner_activity_id:
        p_def = (get_library_activity(partner_activity_id)
                 or find_library_activity_by_name(partner_activity_id))
        if p_def:
            partner_activity_name = p_def.get("name", partner_activity_id)

    partner_profile = get_character_profile(partner) or {}
    if partner_profile.get("current_activity", "") == partner_activity_name:
        # Schon gesetzt — nur Partner-Feld aktualisieren, falls noetig
        if partner_profile.get("activity_partner", "") != initiator:
            partner_profile["activity_partner"] = initiator
            save_character_profile(partner, partner_profile)
        return ""

    save_character_current_activity(partner, partner_activity_name,
        partner=initiator,
        _skip_partner_transfer=True)
    try:
        from app.core.log import get_logger
        get_logger("activity_engine").info(
            "Partner-Auto-Transfer: %s -> Activity '%s' (mit %s)",
            partner, partner_activity_name, initiator)
    except Exception:
        pass
    return partner


def get_character_current_room(character_name: str) -> str:
    """Gibt die aktuelle Raum-ID zurueck"""
    if not character_name:
        return ""
    profile = get_character_profile(character_name)
    return profile.get("current_room", "")


def save_character_current_room(character_name: str, room_id: str):
    """Speichert den aktuellen Raum (Room-ID) und prueft Outfit-Type-
    Compliance fuer den Raum (ueberschreibt Location-Vorgabe)."""
    profile = get_character_profile(character_name)
    old_room = profile.get("current_room", "")
    profile["current_room"] = room_id
    save_character_profile(character_name, profile)

    # state_history-Event bei echtem Raumwechsel — sonst sind Raum-Aenderungen
    # in der Diary/Activity-Auswertung unsichtbar (frueher nur location getrackt).
    if room_id and room_id != old_room:
        room_name = room_id
        try:
            row = get_connection().execute(
                "SELECT name FROM rooms WHERE id=?", (room_id,)).fetchone()
            if row and row[0]:
                room_name = row[0]
        except Exception:
            pass
        _record_state_change(character_name, "room", room_id,
                              metadata={"name": room_name, "old": old_room})

    # Outfit-Type-Compliance nur bei echtem Raumwechsel + Avatar ausnehmen
    if not room_id or room_id == old_room:
        return
    try:
        from app.models.account import is_player_controlled
        if is_player_controlled(character_name):
            return
        from app.core.outfit_rules import resolve_target_outfit_type
        from app.models.inventory import apply_outfit_type_compliance
        target_type = resolve_target_outfit_type(character_name)
        if target_type:
            apply_outfit_type_compliance(character_name, target_type)
    except Exception as _e:
        from app.core.log import get_logger
        get_logger("character_model").debug(
            "Outfit-Compliance bei Raumwechsel fehlgeschlagen: %s", _e)


def get_character_current_feeling(character_name: str) -> str:
    """Gibt das aktuelle Gefuehl zurueck"""
    if not character_name:
        return ""
    profile = get_character_profile(character_name)
    return profile.get("current_feeling", "")


def save_character_current_feeling(character_name: str, feeling: str):
    """Speichert das aktuelle Gefuehl"""
    profile = get_character_profile(character_name)
    old_feeling = profile.get("current_feeling", "")
    profile["current_feeling"] = feeling
    save_character_profile(character_name, profile)
    # Hintergrund-Variant fuer die neue Mood generieren, damit beim
    # Character-Wechsel schon ein aktuelles Bild bereitsteht. Low-Prio GPU-Task.
    if feeling and feeling != old_feeling:
        _schedule_background_variant(character_name)


def force_set_status(character_name: str,
                     location: Optional[str] = None,
                     room: Optional[str] = None,
                     activity: Optional[str] = None,
                     feeling: Optional[str] = None) -> Dict[str, Any]:
    """Direct write of character state — no LLM, no AgentLoop, no guards.

    For plot/admin overrides where the story or world needs to put a
    character somewhere/in some state regardless of in-progress activities,
    chat sessions, or partner locks. Use sparingly — bypasses every safety
    that the scheduler-driven ``_action_set_status`` and the AgentLoop
    bump+hint pattern provide.

    Returns a dict with the keys that were actually written.
    """
    if not character_name:
        return {}
    written: Dict[str, Any] = {}
    if location is not None:
        save_character_current_location(character_name, location=location)
        written["location"] = location
    if room is not None:
        save_character_current_room(character_name, room)
        written["room"] = room
    if activity is not None:
        save_character_current_activity(
            character_name, activity, detail="", _skip_classify=True)
        written["activity"] = activity
    if feeling is not None:
        save_character_current_feeling(character_name, feeling)
        written["feeling"] = feeling
    return written


def is_outfit_locked(character_name: str) -> bool:
    """True wenn das Outfit des Characters gegen Auto-Aenderung gesperrt ist.

    Gesperrte Characters ueberspringen Chat-/RP-Scene-Outfit-Extraktion +
    zugehoerige Expression-Regeneration → spart LLM-Calls und GPU-Zeit.
    Funktioniert fuer Agent- und Avatar-Character gleich.
    """
    if not character_name:
        return False
    profile = get_character_profile(character_name)
    return bool(profile.get("outfit_locked", False))


def set_outfit_locked(character_name: str, locked: bool):
    """Setzt/entfernt die Outfit-Sperre."""
    profile = get_character_profile(character_name)
    if locked:
        profile["outfit_locked"] = True
    else:
        profile.pop("outfit_locked", None)
    save_character_profile(character_name, profile)


# === Spezial-Aktivitaeten (pro Character, in character_config.json) ===

# === Erlaubte Orte: DEPRECATED ============================================
# Das allowed_locations-Feld wurde abgeschafft. Orte-Zugang wird ueber das
# Rules-System (Block-Rules) gesteuert; Wissen/Sichtbarkeit eines Ortes laeuft
# jetzt ueber knowledge_item_id an Location/Room. Der Getter bleibt als
# No-Op bestehen fuer etwaige externe Aufrufer.

def _migrate_outfits(outfits: list) -> list:
    """Konvertiert altes Format (location/activity) zu neuem (locations/activities).

    Idempotent — bereits migrierte Eintraege werden unveraendert uebernommen.
    """
    migrated = []
    for o in outfits:
        if "locations" in o:
            # Bereits neues Format — sicherstellen dass id + image + excluded_locations vorhanden
            if not o.get("id"):
                o["id"] = uuid.uuid4().hex[:8]
            if "image" not in o:
                o["image"] = ""
            if "excluded_locations" not in o:
                o["excluded_locations"] = []
            migrated.append(o)
            continue
        # Altes Format: {location, activity, outfit}
        migrated.append({
            "id": uuid.uuid4().hex[:8],
            "name": "",
            "outfit": o.get("outfit", ""),
            "locations": [o["location"]] if o.get("location") else [],
            "activities": [o["activity"]] if o.get("activity") else [],
            "excluded_locations": [],
            "image": "",
        })
    return migrated


# ---------------------------------------------------------------------------
# Soul-Files: Template-getriebene Erstellung
# ---------------------------------------------------------------------------

# Map: Template-Feature → Soul-Datei. None = immer aktiv (kein Feature-Gate).
_SOUL_FILE_FEATURES = {
    "personality.md":    None,                  # immer
    "tasks.md":          None,                  # immer
    "presence.md":       None,                  # immer — Aussenwirkung
    "roleplay_rules.md": "roleplay_rules_enabled",
    "beliefs.md":        "beliefs_enabled",
    "lessons.md":        "lessons_enabled",
    "goals.md":          "goals_enabled",
    "soul.md":           "soul_enabled",
}


def _soul_template_dir() -> Path:
    """shared/templates/soul/ Verzeichnis (Repo-Root)."""
    return Path(__file__).resolve().parents[2] / "shared" / "templates" / "soul"


def populate_soul_files_from_profile(character_name: str) -> int:
    """Schreibt JSON-Profil-Werte in die MD-Files unter erste ## Section.

    Aufruf nach save_character_profile + ensure_soul_files. Wirkt nur wenn:
      - Template hat ein Feld mit source_file
      - JSON-Profil hat einen Wert fuer den Field-Key
      - MD-File existiert UND erste Section ist leer
    Ueberschreibt nichts wenn MD bereits Content hat.

    Returns: Anzahl befuellter Files.
    """
    if not character_name:
        return 0
    profile = get_character_profile(character_name)
    template_id = profile.get("template", "")
    if not template_id:
        return 0
    try:
        from app.models.character_template import get_template
        tmpl = get_template(template_id)
    except Exception:
        return 0
    if not tmpl:
        return 0

    char_dir = get_character_dir(character_name)
    populated = 0
    for section in tmpl.get("sections", []):
        for field in section.get("fields", []):
            source_file = field.get("source_file")
            if not source_file:
                continue
            key = field.get("key", "")
            if not key:
                continue
            value = profile.get(key)
            if not value or not isinstance(value, str) or not value.strip():
                continue
            md_path = char_dir / source_file
            if not md_path.exists():
                continue
            # Round-trip guard: _inject_soul_md_values puts the full MD body
            # (incl. "## " sub-headings) back into profile[key] on read. If
            # the user saves without editing, that value comes back here. We
            # must not inject structured markdown as a section body —
            # otherwise the "## " lines duplicate as new headings on each save.
            if any(line.strip().startswith("## ")
                   for line in value.splitlines()):
                continue
            existing = md_path.read_text(encoding="utf-8")
            new_text = _inject_into_first_empty_section(existing, value.strip())
            if new_text and new_text != existing:
                md_path.write_text(new_text, encoding="utf-8")
                populated += 1
    if populated:
        logger.info("Soul-Files befuellt aus Profil fuer %s: %d", character_name, populated)
    return populated


def _inject_into_first_empty_section(md_text: str, content: str) -> str:
    """Schreibt content unter die erste ## Section die noch leer ist.

    Gibt unveraenderten Text zurueck wenn keine leere Section gefunden ODER
    bereits Content vorhanden ist.
    """
    if not md_text:
        return md_text
    lines = md_text.splitlines()
    out_lines = []
    injected = False
    i = 0
    while i < len(lines):
        line = lines[i]
        out_lines.append(line)
        if not injected and line.startswith("## "):
            # Schauen ob diese Section leer ist (kein Body bis naechste ## oder EOF)
            j = i + 1
            section_body = []
            while j < len(lines) and not lines[j].startswith("## ") and not lines[j].startswith("# "):
                section_body.append(lines[j])
                j += 1
            body_text = "\n".join(section_body).strip()
            if not body_text:
                # Leere Section → content einsetzen
                out_lines.append(content)
                if section_body and section_body[-1] == "":
                    out_lines.append("")  # behalte trenner
                injected = True
                i = j
                continue
        i += 1
    if injected:
        return "\n".join(out_lines).rstrip() + "\n"
    return md_text


def ensure_soul_files(character_name: str) -> int:
    """Legt fehlende Soul-MD-Dateien aus shared/templates/soul/ an.

    Beruecksichtigt die Template-Features des Characters: Felder die
    durch ein Feature gegated sind, werden nur erstellt wenn das Feature
    im aktiven Template aktiviert ist (oder ungated → immer).

    Returns: Anzahl neu angelegter Dateien.
    """
    if not character_name:
        return 0

    char_dir = get_character_dir(character_name)
    soul_dir = char_dir / "soul"

    # Aktives Template + Features bestimmen
    profile = get_character_profile(character_name)
    template_id = profile.get("template", "")
    features = {}
    if template_id:
        try:
            from app.models.character_template import get_template
            tmpl = get_template(template_id)
            features = (tmpl or {}).get("features", {}) or {}
        except Exception:
            features = {}

    tpl_dir = _soul_template_dir()
    created = 0
    for fname, feature in _SOUL_FILE_FEATURES.items():
        # Feature-Check (ungated → immer anlegen)
        if feature and not features.get(feature, False):
            continue
        target = soul_dir / fname
        if target.exists():
            continue
        src = tpl_dir / fname
        if not src.exists():
            continue
        soul_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        created += 1
    if created:
        logger.info("Soul-Files angelegt fuer %s: %d", character_name, created)
    return created


_OUTFIT_COLUMNS = ("id", "name", "pieces", "image", "created_at")


def _load_outfits_file(character_name: str) -> List[Dict]:
    """Laedt Outfit-Definitionen aus outfits_sets-Tabelle."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT id, name, pieces, image, meta, created_at FROM outfits_sets "
            "WHERE character_name=? ORDER BY created_at ASC",
            (character_name,),
        ).fetchall()
    except Exception as e:
        logger.warning("_load_outfits_file DB-Fehler fuer %s: %s", character_name, e)
        return []
    outfits = []
    for r in rows:
        try:
            pieces = json.loads(r[2] or "[]")
        except Exception:
            pieces = []
        try:
            meta = json.loads(r[4] or "{}")
        except Exception:
            meta = {}
        outfit = {
            "id": r[0],
            "name": r[1] or "",
            "image": r[3] or "",
            "created_at": r[5] or "",
            "pieces": pieces,
        }
        outfit.update(meta)
        outfits.append(outfit)
    return outfits


def _save_outfits_file(character_name: str, outfits: List[Dict]):
    """Schreibt Outfit-Definitionen in outfits_sets-Tabelle (KEIN image_meta)."""
    now = datetime.now().isoformat()
    new_ids = {o.get("id") for o in outfits if o.get("id")}
    try:
        with transaction() as conn:
            existing_ids = {r[0] for r in conn.execute(
                "SELECT id FROM outfits_sets WHERE character_name=?",
                (character_name,),
            ).fetchall()}
            for oid in existing_ids - new_ids:
                conn.execute("DELETE FROM outfits_sets WHERE id=?", (oid,))
            for outfit in outfits:
                oid = outfit.get("id")
                if not oid:
                    continue
                pieces = outfit.get("pieces", [])
                meta = {k: v for k, v in outfit.items()
                        if k not in _OUTFIT_COLUMNS and k != "image_meta"}
                conn.execute("""
                    INSERT INTO outfits_sets (id, character_name, name, pieces,
                        image, meta, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name,
                        pieces=excluded.pieces,
                        image=excluded.image,
                        meta=excluded.meta
                """, (
                    oid,
                    character_name,
                    outfit.get("name", ""),
                    json.dumps(pieces, ensure_ascii=False),
                    outfit.get("image", ""),
                    json.dumps(meta, ensure_ascii=False),
                    outfit.get("created_at", now),
                ))
    except Exception as e:
        logger.error("_save_outfits_file DB-Fehler fuer %s: %s", character_name, e)


def _get_outfit_sidecar_path(character_name: str, image_filename: str) -> Path:
    """Pfad zur Sidecar-JSON eines Outfit-Bildes (gleicher Basename, .json)."""
    stem = Path(image_filename).stem
    return get_character_outfits_dir(character_name) / f"{stem}.json"


def save_outfit_image_meta(character_name: str, image_filename: str, meta: Dict[str, Any]):
    """Schreibt die Sidecar-Metadaten eines Outfit-Bildes."""
    p = _get_outfit_sidecar_path(character_name, image_filename)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")


def get_character_outfits(character_name: str) -> List[Dict]:
    """Gibt alle definierten Outfits zurueck (aus outfits_sets-Tabelle).

    Migriert automatisch alte Felder (locations: str→list).
    Embedded image_meta aus alten Profilen wird ignoriert — Sidecar ist Quelle.
    """
    if not character_name:
        return []
    raw = _load_outfits_file(character_name)
    return _migrate_outfits(raw)


def find_outfit_by_image(character_name: str, image_filename: str) -> Optional[Dict]:
    """Sucht ein Outfit anhand seines Bild-Dateinamens.

    Wird vom Re-Creation Dialog genutzt um aus reference_images[input_reference_image_1]
    das Outfit beim Erstellungszeitpunkt zu rekonstruieren — auch fuer alte Bilder
    ohne canonical.outfits.
    """
    if not character_name or not image_filename:
        return None
    fn = image_filename.strip()
    for o in get_character_outfits(character_name):
        if (o.get("image") or "").strip() == fn:
            return o
    return None


def save_character_outfits(character_name: str, outfits: List[Dict]):
    """Speichert die Outfit-Konfigurationen in der DB (KEIN image_meta).

    image_meta gehoert in die Sidecar-JSON neben dem PNG, nicht in der DB.
    """
    for outfit in outfits:
        if not outfit.get("name", "").strip():
            outfit["name"] = _next_outfit_name(outfits)
    _save_outfits_file(character_name, outfits)


def _next_outfit_name(outfits: List[Dict]) -> str:
    """Generate next auto-name like Outfit1, Outfit2, ... that is not yet taken."""
    existing_names = {o.get("name", "") for o in outfits}
    idx = 1
    while f"Outfit{idx}" in existing_names:
        idx += 1
    return f"Outfit{idx}"


def add_character_outfit(character_name: str, outfit_data: Dict) -> str:
    """Fuegt ein neues Outfit hinzu oder aktualisiert ein bestehendes.

    outfit_data: {id?, name, outfit, locations[], activities[]}
    Returns: outfit id
    """
    outfits = get_character_outfits(character_name)
    outfit_id = outfit_data.get("id", "")

    if outfit_id:
        # Update bestehend
        for existing in outfits:
            if existing.get("id") == outfit_id:
                name = outfit_data.get("name", existing.get("name", ""))
                if not name or not name.strip():
                    name = _next_outfit_name(outfits)
                existing["name"] = name
                existing["outfit"] = outfit_data.get("outfit", existing.get("outfit", ""))
                existing["locations"] = outfit_data.get("locations", existing.get("locations", []))
                existing["activities"] = outfit_data.get("activities", existing.get("activities", []))
                existing["excluded_locations"] = outfit_data.get("excluded_locations", existing.get("excluded_locations", []))
                if "image" in outfit_data:
                    existing["image"] = outfit_data["image"]
                if "pieces" in outfit_data:
                    existing["pieces"] = list(outfit_data["pieces"] or [])
                if "remove_slots" in outfit_data:
                    existing["remove_slots"] = list(outfit_data["remove_slots"] or [])
                if "pieces_colors" in outfit_data:
                    _pc = outfit_data.get("pieces_colors") or {}
                    if isinstance(_pc, dict):
                        existing["pieces_colors"] = {
                            str(k): str(v).strip()
                            for k, v in _pc.items() if v and str(v).strip()
                        }
                save_character_outfits(character_name, outfits)
                return outfit_id

    # Neues Outfit
    from datetime import datetime
    outfit_id = uuid.uuid4().hex[:8]
    name = outfit_data.get("name", "")
    if not name or not name.strip():
        name = _next_outfit_name(outfits)
    _raw_pc = outfit_data.get("pieces_colors") or {}
    _pieces_colors = {
        str(k): str(v).strip()
        for k, v in _raw_pc.items() if v and str(v).strip()
    } if isinstance(_raw_pc, dict) else {}
    outfits.append({
        "id": outfit_id,
        "name": name,
        "outfit": outfit_data.get("outfit", ""),
        "pieces": list(outfit_data.get("pieces", []) or []),
        "remove_slots": list(outfit_data.get("remove_slots", []) or []),
        "pieces_colors": _pieces_colors,
        "locations": outfit_data.get("locations", []),
        "activities": outfit_data.get("activities", []),
        "excluded_locations": outfit_data.get("excluded_locations", []),
        "image": outfit_data.get("image", ""),
        "created_at": outfit_data.get("created_at", datetime.now().isoformat()),
    })
    save_character_outfits(character_name, outfits)
    return outfit_id


_REMBG_SESSION = None
_REMBG_SESSION_LOCK = None


def preload_rembg_session() -> None:
    """Laedt die rembg/u2net-Session im Hintergrund.

    Ohne Preload triggert der erste outfit-postprocessing-Request einen
    ~5s Block des Event-Loops (170 MB ONNX-Modell lod, GIL haelt zwischendurch).
    Beim Server-Startup als Background-Thread aufgerufen.
    """
    import threading as _t
    def _worker():
        try:
            _get_rembg_session()
            logger.info("rembg-Preload abgeschlossen")
        except Exception as e:
            logger.warning("rembg-Preload fehlgeschlagen: %s", e)
    t = _t.Thread(target=_worker, daemon=True, name="rembg-preload")
    t.start()


def _get_rembg_session():
    """Lazy-Init und Cache der rembg/U2Net-Session.

    Eine wiederverwendete Session vermeidet (a) wiederholtes
    Modell-Laden bei jedem Aufruf und (b) den onnxruntime
    pthread_setaffinity_np-Fehler, der bei jeder neuen
    InferenceSession ausgeloest wird wenn keine SessionOptions
    mit explizitem intra_op_num_threads gesetzt sind.
    """
    global _REMBG_SESSION, _REMBG_SESSION_LOCK
    if _REMBG_SESSION is not None:
        return _REMBG_SESSION
    if _REMBG_SESSION_LOCK is None:
        import threading as _t
        _REMBG_SESSION_LOCK = _t.Lock()
    with _REMBG_SESSION_LOCK:
        if _REMBG_SESSION is not None:
            return _REMBG_SESSION
        # OMP_NUM_THREADS VOR rembg-Import setzen, damit onnxruntime
        # die CPU-Affinity nicht zu setzen versucht (im Container fehlerhaft).
        import os
        os.environ.setdefault("OMP_NUM_THREADS",
            os.environ.get("REMBG_OMP_NUM_THREADS", "4"))
        from rembg import new_session
        _REMBG_SESSION = new_session("u2net")
        logger.info("rembg-Session initialisiert (u2net, OMP_NUM_THREADS=%s)",
                    os.environ.get("OMP_NUM_THREADS"))
    return _REMBG_SESSION


def postprocess_outfit_image(image_path: Path) -> Path:
    """Entfernt Hintergrund (rembg) und schneidet transparente Raender links/rechts ab.

    Args:
        image_path: Pfad zum Outfit-Bild.

    Returns:
        Pfad zum verarbeiteten Bild (ggf. neuer Name als .png).
    """
    try:
        from rembg import remove
        from PIL import Image

        session = _get_rembg_session()
        img_data = image_path.read_bytes()
        result_data = remove(img_data, session=session)
        # Als PNG speichern
        png_path = image_path.with_suffix(".png")
        png_path.write_bytes(result_data)

        # Transparenten Rand links/rechts abschneiden
        try:
            _img = Image.open(png_path)
            if _img.mode == "RGBA":
                _alpha = _img.getchannel("A")
                _bbox = _alpha.getbbox()  # (left, top, right, bottom)
                if _bbox:
                    _orig_w, _orig_h = _img.size
                    # Nur links/rechts croppen, Hoehe beibehalten
                    _crop_box = (_bbox[0], 0, _bbox[2], _orig_h)
                    _cropped = _img.crop(_crop_box)
                    _cropped.save(png_path)
                    logger.info("Outfit-Bild gecroppt: %dx%d -> %dx%d",
                                _orig_w, _orig_h, _cropped.size[0], _cropped.size[1])
        except Exception as _crop_err:
            logger.warning("Outfit-Crop fehlgeschlagen: %s", _crop_err)

        # Original loeschen wenn anderer Name
        if image_path != png_path and image_path.exists():
            image_path.unlink()

        return png_path

    except Exception as e:
        logger.warning("Hintergrundentfernung fehlgeschlagen: %s", e)
        return image_path


def update_outfit_image(character_name: str, outfit_id: str, image: str,
                        image_meta: dict | None = None) -> bool:
    """Aktualisiert das image-Feld eines Outfits.

    image_meta wird als Sidecar-JSON (`outfits/<image>.json`) geschrieben — NICHT
    in outfits.json embedded.

    Loescht automatisch alle Expression-Variants fuer dieses Outfit,
    da sie auf dem alten Bild basieren.
    """
    outfits = get_character_outfits(character_name)
    for o in outfits:
        if o.get("id") == outfit_id:
            old_image = o.get("image", "")
            o["image"] = image
            save_character_outfits(character_name, outfits)

            # Sidecar fuer das neue Bild schreiben (falls Meta da)
            if image and image_meta:
                try:
                    save_outfit_image_meta(character_name, image, image_meta)
                except Exception as _se:
                    logger.warning("Sidecar-Write fehlgeschlagen fuer %s: %s", image, _se)

            # Altes PNG + Sidecar loeschen wenn ersetzt
            if old_image and old_image != image:
                _delete_outfit_files(character_name, old_image)

            # Expression-Variants invalidieren — Variant-Cache haengt jetzt
            # nur noch an Character + Equipped + Mood + Activity, d.h. ein
            # Outfit-Image-Wechsel betrifft den Cache nicht mehr. Bleibt als
            # No-Op fuer Lesbarkeit.

            return True
    return False


def _delete_outfit_files(character_name: str, image_filename: str) -> None:
    """Loescht PNG + Sidecar-JSON eines Outfits aus dem outfits/-Verzeichnis."""
    if not image_filename:
        return
    outfits_dir = get_character_outfits_dir(character_name)
    png = outfits_dir / image_filename
    sidecar = _get_outfit_sidecar_path(character_name, image_filename)
    for p in (png, sidecar):
        try:
            if p.exists():
                p.unlink()
        except Exception as _de:
            logger.warning("Outfit-Datei loeschen fehlgeschlagen %s: %s", p.name, _de)


def delete_character_outfit(character_name: str,
                            outfit_id: str = "", location: str = "",
                            activity: str = "") -> bool:
    """Loescht ein Outfit per ID oder legacy location+activity.

    Loescht auch das PNG und die Sidecar-JSON aus dem outfits/-Verzeichnis.
    """
    outfits = get_character_outfits(character_name)

    if outfit_id:
        to_delete = [o for o in outfits if o.get("id") == outfit_id]
        new_outfits = [o for o in outfits if o.get("id") != outfit_id]
    else:
        # Legacy: location+activity Match (fuer Rueckwaertskompatibilitaet)
        to_delete = [
            o for o in outfits
            if (o.get("locations") == [location] and
                o.get("activities") == ([activity] if activity else []))
        ]
        new_outfits = [
            o for o in outfits
            if not (o.get("locations") == [location] and
                    o.get("activities") == ([activity] if activity else []))
        ]

    if len(new_outfits) < len(outfits):
        save_character_outfits(character_name, new_outfits)
        # PNG + Sidecar mit-loeschen
        for o in to_delete:
            _delete_outfit_files(character_name, o.get("image", ""))
        return True
    return False


def resolve_outfit_placeholders(outfit_text: str, character_name: str) -> str:
    """Ersetzt {placeholder} in Outfit-Texten durch Profil-Felder des Characters.

    Unterstuetzte Platzhalter: alle direkten Profil-Felder wie
    {size}, {body_type}, {breast_size}, {butt_size}, {hair_color},
    {hair_length}, {eye_color}, {skin_color}, {gender}, etc.
    Unbekannte Platzhalter werden unveraendert belassen.
    """
    if not outfit_text or '{' not in outfit_text:
        return outfit_text
    profile = get_character_profile(character_name)
    import re
    def _replace(match):
        key = match.group(1)
        val = profile.get(key)
        if val is not None and str(val) != "__custom__":
            return str(val)
        return match.group(0)
    return re.sub(r'\{([\w-]+)\}', _replace, outfit_text)


def _collect_covered_slots(equipped_pieces: Dict[str, str]) -> set:
    """Sammelt alle Slots, die durch ein anderes angezogenes Piece verdeckt
    werden (covers-Feld). Verdeckte Slots fallen aus dem Prompt raus."""
    from app.models.inventory import get_item as _gi
    covered = set()
    for _slot, _iid in (equipped_pieces or {}).items():
        if not _iid:
            continue
        it = _gi(_iid)
        if not it:
            continue
        op = it.get("outfit_piece") or {}
        for c in (op.get("covers") or []):
            cs = str(c).strip().lower()
            if cs:
                covered.add(cs)
    return covered


def build_unworn_slot_fragments(character_name: str,
                                 profile: Optional[Dict[str, Any]] = None) -> str:
    """Sammelt prompt-Fragmente fuer alle leeren UND nicht-verdeckten Slots.

    Iteriert ``VALID_PIECE_SLOTS`` und nimmt fuer jeden Slot, der
      a) NICHT in ``equipped_pieces`` belegt ist, UND
      b) NICHT durch ein anderes Piece via ``covers`` verdeckt ist,
    den Eintrag aus ``profile.slot_overrides[slot].prompt`` (Game Admin →
    Character → Bild-Darstellung). Fallback fuer Underwear-Slots:
    ``no_outfit_prompt_top`` / ``no_outfit_prompt_bottom``.

    Token-Platzhalter werden via ``resolve_profile_tokens`` ausgewertet
    (gleiche Logik wie der Variant-Bild-Aufbau in expression_regen).
    Returns: kommaseparierte Fragment-Liste oder leerer String.
    """
    from app.models.inventory import VALID_PIECE_SLOTS
    from app.models.character_template import get_template, resolve_profile_tokens
    if profile is None:
        profile = get_character_profile(character_name)
    pieces = profile.get("equipped_pieces") or {}
    covered = _collect_covered_slots(pieces)
    slot_overrides = profile.get("slot_overrides") or {}
    if not isinstance(slot_overrides, dict):
        slot_overrides = {}

    # Template fuer Token-Resolution laden — identisch zur expression_regen.
    try:
        tmpl = get_template(profile.get("template", "human-default"))
    except Exception:
        tmpl = None

    def _resolve(raw: str) -> str:
        s = (raw or "").strip()
        if not s or "{" not in s:
            return s
        try:
            return resolve_profile_tokens(s, profile, template=tmpl,
                                           target_key="character_appearance")
        except Exception:
            return s

    parts: List[str] = []
    for slot in VALID_PIECE_SLOTS:
        if pieces.get(slot):
            continue
        if slot in covered:
            continue
        ov = slot_overrides.get(slot) or {}
        ov_prompt = ""
        if isinstance(ov, dict):
            ov_prompt = (ov.get("prompt") or "").strip()
        if ov_prompt:
            resolved = _resolve(ov_prompt)
            if resolved:
                parts.append(resolved)
            continue
        # Legacy-Fallback fuer Underwear-Slots
        if slot == "underwear_top":
            fb = _resolve((profile.get("no_outfit_prompt_top") or "").strip())
            if fb:
                parts.append(fb)
        elif slot == "underwear_bottom":
            fb = _resolve((profile.get("no_outfit_prompt_bottom") or "").strip())
            if fb:
                parts.append(fb)
    return ", ".join(parts)


def build_equipped_outfit_prompt(character_name: str,
                                 profile: Optional[Dict[str, Any]] = None) -> str:
    """Baut den Outfit-Prompt aus equipped_pieces + equipped_items zusammen.

    Liefert leeren String wenn weder Pieces noch Items angelegt sind —
    Aufrufer sollen dann auf den Freitext-Fallback zurueckgreifen.
    """
    if profile is None:
        profile = get_character_profile(character_name)
    pieces = profile.get("equipped_pieces") or {}
    pieces_meta = profile.get("equipped_pieces_meta") or {}
    items = profile.get("equipped_items") or []
    if not pieces and not items:
        return ""

    from app.models.inventory import VALID_PIECE_SLOTS, get_item

    def _slot_color(slot_name: str) -> str:
        # Direkt gesetzte Color am gegebenen Slot
        entry = pieces_meta.get(slot_name) or {}
        if isinstance(entry, dict):
            c = (entry.get("color") or "").strip()
            if c:
                return c
        # Multi-Slot-Pieces: dasselbe Item ist in mehreren Slots eingehaengt
        # (z.B. Bikini-Bra in top + underwear_top). Color wird typischerweise
        # nur am Primary-Slot vom User gesetzt — beim Mirror-Slot fehlt sie.
        # Daher Fallback: alle anderen Slots mit derselben item_id pruefen.
        iid = pieces.get(slot_name)
        if iid:
            for other_slot, other_iid in pieces.items():
                if other_iid == iid and other_slot != slot_name:
                    _other = pieces_meta.get(other_slot) or {}
                    if isinstance(_other, dict):
                        c = (_other.get("color") or "").strip()
                        if c:
                            return c
        return ""

    covered_slots = _collect_covered_slots(pieces)

    # Teilverdeckte Slots: {slot → (covering_fragment, covering_slot)}
    # Multi-Slot-Pieces nur einmal verarbeiten — der Render-Slot (erster Slot
    # in VALID_PIECE_SLOTS-Reihenfolge) traegt die partially_covers-Beziehung.
    partially_covered: Dict[str, tuple] = {}
    seen_items: set = set()
    for _slot in VALID_PIECE_SLOTS:
        _iid = pieces.get(_slot)
        if not _iid or _iid in seen_items:
            continue
        seen_items.add(_iid)
        _it = get_item(_iid)
        if not _it:
            continue
        _op = _it.get("outfit_piece") or {}
        _covering_frag = (_it.get("prompt_fragment") or "").strip()
        _covering_color = _slot_color(_slot)
        if _covering_color and _covering_frag:
            _covering_frag = f"{_covering_color} {_covering_frag}"
        for _c in (_op.get("partially_covers") or []):
            _cs = str(_c).strip().lower()
            if _cs and _covering_frag:
                partially_covered[_cs] = (_covering_frag, _slot)

    # Slots deren Fragment bereits im "underneath"-Teil steckt → nicht doppelt.
    # NUR unterdruecken wenn der Ziel-Slot auch belegt ist — sonst
    # verschwindet das Covering-Piece komplett.
    suppressed_slots = set()
    for _target, (_cfrag, _cslot) in partially_covered.items():
        if pieces.get(_target):
            suppressed_slots.add(_cslot)

    # Pieces in fester Slot-Reihenfolge sammeln (abgedeckte + unterdrueckte Slots ueberspringen).
    # Multi-Slot-Pieces werden nur EINMAL gerendert — im ersten ihrer slots, der in
    # VALID_PIECE_SLOTS-Reihenfolge auftaucht (Render-Slot).
    piece_fragments: List[str] = []
    rendered_items: set = set()
    for slot in VALID_PIECE_SLOTS:
        if slot in covered_slots:
            continue
        if slot in suppressed_slots:
            continue
        iid = pieces.get(slot)
        if not iid:
            continue
        if iid in rendered_items:
            continue  # Mirror-Slot eines bereits gerenderten Multi-Slot-Pieces
        it = get_item(iid)
        if not it:
            continue
        rendered_items.add(iid)
        frag = (it.get("prompt_fragment") or "").strip()
        if frag:
            _color = _slot_color(slot)
            if _color:
                frag = f"{_color} {frag}"
            if slot in partially_covered:
                frag = f"{frag} underneath {partially_covered[slot][0]}"
            piece_fragments.append(frag)

    # Equipped Items: Reihenfolge der Liste, dedup
    item_fragments: List[str] = []
    seen = set()
    for iid in items:
        if iid in seen:
            continue
        seen.add(iid)
        it = get_item(iid)
        if not it:
            continue
        frag = (it.get("prompt_fragment") or "").strip()
        if frag:
            item_fragments.append(frag)

    # Fallback-Prompts fuer unbekleidete Bereiche (per-Character aus Profil).
    def _resolve_fb(key: str) -> str:
        _raw = (profile.get(key) or "").strip()
        if not _raw:
            return ""
        try:
            from app.models.character_template import resolve_profile_tokens, get_template
            _tmpl = get_template(profile.get("template", ""))
            return resolve_profile_tokens(_raw, profile, template=_tmpl,
                                           target_key="character_appearance")
        except Exception:
            return _raw

    # Slot-Overrides (per-Character im Profil): fuer jeden Slot der leer und
    # nicht gecovert ist, wird ein Prompt-Override hinzugefuegt. Legacy-Felder
    # no_outfit_prompt_top/bottom greifen auf die Unterwaesche-Slots.
    _slot_overrides = profile.get("slot_overrides") or {}

    def _override_prompt(slot: str) -> str:
        entry = _slot_overrides.get(slot) or {}
        if isinstance(entry, dict):
            val = (entry.get("prompt") or "").strip()
            if val:
                return _resolve_tokens(val) if False else val
        return ""

    # Templated-Resolve fuer den Override-Prompt (optional Token-Resolution)
    def _resolve_tokens(raw: str) -> str:
        if not raw:
            return ""
        try:
            from app.models.character_template import resolve_profile_tokens, get_template
            _tmpl = get_template(profile.get("template", ""))
            return resolve_profile_tokens(raw, profile, template=_tmpl,
                                           target_key="character_appearance")
        except Exception:
            return raw

    # Layer-Check: missing underwear is not visible when the outer layer
    # covers it (sweater hides missing bra, jeans hide missing briefs).
    # Without this, the image prompt would falsely describe missing
    # pieces as exposed body parts even though the outfit is complete.
    def _outer_layer_covers(slot: str) -> bool:
        if slot == "underwear_top":
            return bool(pieces.get("top")) or "underwear_top" in covered_slots
        if slot == "underwear_bottom":
            return bool(pieces.get("bottom")) or "underwear_bottom" in covered_slots
        return False

    _fallback_parts: List[str] = []
    for _slot in VALID_PIECE_SLOTS:
        if pieces.get(_slot):
            continue
        if _slot in covered_slots:
            continue
        if _outer_layer_covers(_slot):
            continue
        # Slot-Override aus Profil
        _ov = _slot_overrides.get(_slot) or {}
        _ov_prompt = (_ov.get("prompt") or "").strip() if isinstance(_ov, dict) else ""
        if _ov_prompt:
            _fallback_parts.append(_resolve_tokens(_ov_prompt))
            continue
        # Legacy-Felder nur fuer Unterwaesche-Slots
        if _slot == "underwear_top":
            _fb = _resolve_fb("no_outfit_prompt_top")
            if _fb:
                _fallback_parts.append(_fb)
        elif _slot == "underwear_bottom":
            _fb = _resolve_fb("no_outfit_prompt_bottom")
            if _fb:
                _fallback_parts.append(_fb)

    parts: List[str] = []
    _fallback_text = ", ".join(_fallback_parts)
    if _fallback_text:
        parts.append(_fallback_text)
    if piece_fragments:
        parts.append("wearing: " + ", ".join(piece_fragments))
    if item_fragments:
        parts.append(", ".join(item_fragments))
    return ". ".join(parts)


def save_character_default_outfit(character_name: str, outfit: str):
    """Speichert das Default-Outfit"""
    profile = get_character_profile(character_name)
    profile["default_outfit"] = outfit
    save_character_profile(character_name, profile)


def get_character_default_outfit(character_name: str) -> str:
    """Gibt das Default-Outfit zurueck"""
    if not character_name:
        return ""
    return get_character_profile(character_name).get("default_outfit", "")


def generate_random_appearance() -> str:
    """Generiert ein zufaelliges Aussehen fuer einen Character"""
    genders = ["weiblich", "maennlich", "androgyn"]
    gender = random.choice(genders)
    hair_colors = ["blond", "bruenett", "schwarz", "rot", "silber", "platinblond", "kastanienbraun"]
    hair_color = random.choice(hair_colors)
    hair_lengths = ["kurz", "mittellang", "lang", "sehr lang"]
    hair_length = random.choice(hair_lengths)
    hairstyles = ["glatt", "wellig", "lockig", "geflochten", "hochgesteckt", "wild", "gepflegt"]
    hairstyle = random.choice(hairstyles)
    eye_colors = ["blau", "gruen", "braun", "grau", "bernsteinfarben", "haselnussbraun"]
    eye_color = random.choice(eye_colors)
    body_types = ["schlank", "athletisch", "kurvig", "kraeftig", "zierlich", "muskuloes"]
    body_type = random.choice(body_types)
    heights = ["klein", "durchschnittlich gross", "gross", "sehr gross"]
    height = random.choice(heights)
    clothing_styles = [
        "elegant und geschaeftlich",
        "laessig und sportlich",
        "freizuegig und verfuehrerisch",
        "gothic und dunkel",
        "verspielt und feminin",
        "minimalistisch und modern",
        "extravagant und auffaellig",
        "bequem und locker"
    ]
    clothing_style = random.choice(clothing_styles)

    appearance = f"{gender}, {height}, {body_type}, {hair_length}es {hair_color}es {hairstyle} Haar, {eye_color}e Augen, Kleidungsstil: {clothing_style}"
    return appearance


def list_available_characters() -> List[str]:
    """Listet alle verfuegbaren Characters aus der DB (Fallback: Dateisystem).

    Filterung:
      - Namen mit fuehrendem Underscore (_messaging_frame, _system) sind
        interne System-Characters
      - Reservierte Namen (admin, user, system, default, player, "") sind
        keine spielbaren Characters — sie repraesentieren Login-Accounts
        oder Sentinels und sollen niemals in Galerie/Roster/Chat-Auswahl
        auftauchen, auch wenn sie versehentlich als Row in der ``characters``
        Tabelle landen
    """
    def _is_real_character(name: str) -> bool:
        if not name or name.startswith("_"):
            return False
        if name.lower() in _RESERVED_NAMES:
            return False
        return True

    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT name FROM characters ORDER BY name ASC"
        ).fetchall()
        if rows:
            return [r[0] for r in rows if _is_real_character(r[0])]
    except Exception as e:
        logger.debug("list_available_characters DB-Fehler: %s", e)

    # Fallback: Dateisystem
    characters = []
    characters_dir = get_user_characters_dir()
    if characters_dir.exists():
        for character_dir in characters_dir.iterdir():
            if not character_dir.is_dir():
                continue
            if not _is_real_character(character_dir.name):
                continue
            profile_path = character_dir / "character_profile.json"
            old_path = character_dir / "profile.json"
            if not profile_path.exists() and old_path.exists():
                old_path.rename(profile_path)
            if profile_path.exists():
                characters.append(character_dir.name)
    return sorted(characters)


def get_character_user_data(character_name: str) -> Dict[str, Any]:
    """Laedt character-spezifische User-Daten (z.B. Anrede)"""
    character_dir = get_character_dir(character_name)
    user_data_path = character_dir / "user_data.json"

    if user_data_path.exists():
        try:
            return json.loads(user_data_path.read_text())
        except:
            pass

    return {
        "user_id": "",
        "address_form": "",
    }


def get_character_address_form(character_name: str) -> str:
    """Gibt die Anrede zurueck, die dieser Character fuer den User verwendet"""
    return get_character_user_data(character_name).get("address_form", "")


# --- Character Images ---

def get_character_images_dir(character_name: str) -> Path:
    """Gibt das Images-Verzeichnis fuer einen Character zurueck"""
    images_dir = get_character_dir(character_name) / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    return images_dir


def get_character_outfits_dir(character_name: str) -> Path:
    """Gibt das Outfits-Bildverzeichnis fuer einen Character zurueck."""
    outfits_dir = get_character_dir(character_name) / "outfits"
    outfits_dir.mkdir(parents=True, exist_ok=True)
    return outfits_dir


def get_character_images(character_name: str) -> List[str]:
    """Gibt eine Liste aller Galerie-Bild-Dateinamen zurueck.

    Scannt das images/ Verzeichnis nach Bilddateien.
    Sortiert nach created_at aus Metadaten (neueste zuerst), Fallback auf mtime.
    """
    images_dir = get_character_images_dir(character_name)
    files = []
    for f in images_dir.iterdir():
        if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            # created_at aus JSON laden (ISO-String sortiert lexikographisch = chronologisch)
            sort_key = None
            meta_path = images_dir / f"{f.stem}.json"
            if meta_path.exists():
                try:
                    m = json.loads(meta_path.read_text(encoding="utf-8"))
                    sort_key = m.get("created_at")
                except Exception:
                    pass
            if not sort_key:
                sort_key = datetime.fromtimestamp(
                    f.stat().st_mtime
                ).strftime("%Y-%m-%dT%H:%M:%S")
            files.append((sort_key, f))
    files.sort(key=lambda x: x[0], reverse=True)
    return [f.name for _, f in files]


def get_character_profile_image(character_name: str) -> str:
    """Gibt den Namen des Profilbildes zurueck"""
    profile = get_character_profile(character_name)
    return profile.get("profile_image", "")


def add_character_image(character_name: str, image_filename: str) -> bool:
    """Setzt das Profilbild falls noch keins vorhanden.

    Die Galerie wird ueber das Dateisystem verwaltet — ein Bild ist in der Galerie
    sobald es im images/ Verzeichnis liegt. Diese Funktion setzt nur noch das
    initiale Profilbild.
    """
    profile = get_character_profile(character_name)
    if not profile.get("profile_image"):
        profile["profile_image"] = image_filename
        save_character_profile(character_name, profile)
    return True


def set_character_profile_image(character_name: str, image_filename: str) -> bool:
    """Setzt das Profilbild"""
    images_dir = get_character_images_dir(character_name)
    if (images_dir / image_filename).exists():
        profile = get_character_profile(character_name)
        profile["profile_image"] = image_filename
        save_character_profile(character_name, profile)
        return True
    return False


def _get_image_meta_path(character_name: str, image_filename: str) -> Path:
    """Gibt den Pfad zur Metadaten-Datei eines Bildes zurueck (gleicher Name wie Bild, .json)."""
    stem = Path(image_filename).stem
    return get_character_images_dir(character_name) / f"{stem}.json"


def _load_single_image_meta(character_name: str, image_filename: str) -> Dict[str, Any]:
    """Laedt die Metadaten eines einzelnen Bildes."""
    meta_path = _get_image_meta_path(character_name, image_filename)
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_single_image_meta(character_name: str, image_filename: str, meta: Dict[str, Any]):
    """Speichert die Metadaten eines einzelnen Bildes."""
    meta_path = _get_image_meta_path(character_name, image_filename)
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def _load_all_image_meta(character_name: str) -> Dict[str, Dict[str, Any]]:
    """Laedt alle Bild-Metadaten als Dict {filename: meta}."""
    images_dir = get_character_images_dir(character_name)
    result = {}
    for meta_file in images_dir.glob("*.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            fn = meta.get("image_filename", meta_file.stem + ".png")
            result[fn] = meta
        except Exception:
            continue
    return result


def add_character_image_comment(character_name: str, image_filename: str, comment: str) -> None:
    """Speichert einen Bild-Kommentar in der separaten Metadaten-Datei."""
    meta = _load_single_image_meta(character_name, image_filename)
    meta["image_filename"] = image_filename
    meta["comment"] = comment
    _save_single_image_meta(character_name, image_filename, meta)


def get_character_image_comments(character_name: str) -> Dict[str, str]:
    """Gibt alle Bild-Kommentare zurueck als {filename: comment}."""
    all_meta = _load_all_image_meta(character_name)
    return {fn: m.get("comment", "") for fn, m in all_meta.items() if m.get("comment")}


def add_character_image_prompt(character_name: str, image_filename: str, prompt: str) -> None:
    """Speichert den Generierungs-Prompt in der separaten Metadaten-Datei."""
    meta = _load_single_image_meta(character_name, image_filename)
    meta["image_filename"] = image_filename
    meta["prompt"] = prompt
    _save_single_image_meta(character_name, image_filename, meta)


def get_character_image_prompts(character_name: str) -> Dict[str, str]:
    """Gibt alle gespeicherten Bild-Prompts zurueck als {filename: prompt}."""
    all_meta = _load_all_image_meta(character_name)
    return {fn: m.get("prompt", "") for fn, m in all_meta.items() if m.get("prompt")}


def add_character_image_metadata(character_name: str, image_filename: str, metadata: Dict[str, Any]
) -> None:
    """Speichert Generierungs-Metadaten zu einem Bild (Backend, Faceswap, Dauer etc.)."""
    meta = _load_single_image_meta(character_name, image_filename)
    meta["image_filename"] = image_filename
    meta.update(metadata)
    # created_at setzen falls noch nicht vorhanden (Fallback: Datei-mtime)
    if not meta.get("created_at"):
        img_path = get_character_images_dir(character_name) / image_filename
        try:
            mtime = img_path.stat().st_mtime
            meta["created_at"] = datetime.fromtimestamp(mtime).strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            meta["created_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    _save_single_image_meta(character_name, image_filename, meta)


def get_character_image_metadata(character_name: str) -> Dict[str, Any]:
    """Gibt alle Bild-Metadaten zurueck als {filename: {backend, workflow, ...}}."""
    from app.models.world import get_location_name as _get_loc_name
    all_meta = _load_all_image_meta(character_name)
    result = {}
    meta_keys = {
        "backend", "backend_type", "workflow", "faceswap", "duration_s",
        "created_at", "model", "loras", "seed", "image_analysis", "location",
        "animate_prompt", "prompt", "character_names", "room_id",
        "reference_images", "negative_prompt",
        # Adapter-Pipeline (fuer Re-Creation Dialog Spalte 3 + Rebuild-Button)
        "canonical", "canonical_source", "target_model",
        "template_prompt", "prompt_method",
        # Items (Props) die beim Erzeugen mitgegeben wurden — Regenerate nutzt dies
        "items_used",
        # Herkunft: wer hat das Bild erzeugt wenn es in einer fremden Galerie
        # liegt (z.B. NPC schickt Foto an Avatar -> from_character=NPC)
        "from_character",
        # FaceSwap-Detail (Methode + Fallback-Status + Enhance-Pass)
        "faceswap_method", "faceswap_fallback", "face_enhance",
        # Generation-Parameter fuer Bild-Info-Panel
        "guidance_scale", "num_inference_steps",
        # Regen-Zeitstempel: nutzt das Frontend als Cache-Bust-Key (?v=...)
        # damit ein regeneriertes Bild ohne Browser-Refresh sichtbar wird.
        "regenerated_at",
    }
    images_dir = get_character_images_dir(character_name)
    for fn, m in all_meta.items():
        entry = {k: m[k] for k in meta_keys if k in m}
        # Fallback: extract timestamp from filename (e.g. Kira_1771187718_xxx.png)
        if "created_at" not in entry:
            import re
            from datetime import datetime
            ts_match = re.search(r'_(\d{10})(?:_|\.)', fn)
            if ts_match:
                try:
                    ts = int(ts_match.group(1))
                    entry["created_at"] = datetime.fromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%S")
                except (ValueError, OSError):
                    pass
        # Location-ID zu Name aufloesen
        if "location" in entry and entry["location"]:
            loc_name = _get_loc_name(entry["location"])
            entry["location_name"] = loc_name if loc_name else entry["location"]
        if entry:
            result[fn] = entry
    return result


def get_single_image_meta(character_name: str, image_filename: str) -> Dict[str, Any]:
    """Gibt die kompletten Metadaten eines einzelnen Bildes zurueck."""
    return _load_single_image_meta(character_name, image_filename)



# --- Character Skill Config ---

def get_character_skills_dir(character_name: str) -> Path:
    """Gibt das Skills-Verzeichnis fuer einen Character zurueck"""
    skills_dir = get_character_dir(character_name) / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    return skills_dir


def get_character_skill_config(character_name: str, skill_name: str) -> Dict[str, Any]:
    """Laedt die character-spezifische Skill-Konfiguration"""
    skills_dir = get_character_skills_dir(character_name)
    config_path = skills_dir / f"{skill_name}.json"

    if config_path.exists():
        try:
            return json.loads(config_path.read_text())
        except:
            pass

    return {}


def save_character_skill_config(character_name: str, skill_name: str, config: Dict[str, Any]):
    """Speichert die character-spezifische Skill-Konfiguration.

    Skill-Configs gehoeren zu BESTEHENDEN Characters — ein unbekannter Name
    wird als Bug behandelt (z.B. LLM-Output mit Vornamen statt Vollnamen)
    und der Write verworfen, statt ein Geister-Verzeichnis anzulegen.
    """
    if character_name.lower() in _RESERVED_NAMES:
        logger.warning("save_character_skill_config: reservierter Name '%s' uebersprungen",
                       character_name)
        return
    _exists = False
    try:
        conn = get_connection()
        _row = conn.execute(
            "SELECT 1 FROM characters WHERE name=? LIMIT 1",
            (character_name,)).fetchone()
        _exists = bool(_row)
    except Exception:
        pass
    if not _exists:
        try:
            _exists = (get_user_characters_dir() / character_name).is_dir()
        except Exception:
            pass
    if not _exists:
        logger.warning("save_character_skill_config: Character '%s' existiert nicht — "
                       "kein Skill-Config-Save", character_name)
        return
    skills_dir = get_character_skills_dir(character_name)
    config_path = skills_dir / f"{skill_name}.json"
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2))


# --- Per-Character Scheduler Storage ---

def get_character_scheduler_dir(character_name: str) -> Path:
    """Gibt das Scheduler-Verzeichnis fuer einen Character zurueck"""
    scheduler_dir = get_character_dir(character_name) / "scheduler"
    scheduler_dir.mkdir(parents=True, exist_ok=True)
    return scheduler_dir


def get_character_scheduler_jobs(character_name: str) -> List[Dict[str, Any]]:
    """Laedt Scheduler-Jobs fuer einen Character aus der DB."""
    try:
        from app.core.db import get_connection as _get_conn
        conn = _get_conn()
        rows = conn.execute(
            "SELECT id, action, trigger, source, meta, created_at FROM scheduler_jobs "
            "WHERE character_name=? ORDER BY created_at ASC",
            (character_name,),
        ).fetchall()
        jobs = []
        for r in rows:
            meta = {}
            try:
                meta = json.loads(r[4] or "{}")
            except Exception:
                pass
            job = meta if "id" in meta else {
                "id": r[0],
                "character": character_name,
                "source": r[3] or "",
                "created_at": r[5] or "",
            }
            try:
                job["action"] = json.loads(r[1]) if r[1] else {}
            except Exception:
                job["action"] = {"type": r[1] or ""}
            try:
                job["trigger"] = json.loads(r[2]) if r[2] else {}
            except Exception:
                job["trigger"] = {}
            jobs.append(job)
        return jobs
    except Exception as e:
        get_logger("character").warning("get_character_scheduler_jobs DB-Fehler fuer %s: %s", character_name, e)
    # Fallback: JSON-Datei
    scheduler_dir = get_character_scheduler_dir(character_name)
    jobs_path = scheduler_dir / "jobs.json"
    if jobs_path.exists():
        try:
            data = json.loads(jobs_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data.get("jobs", [])
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def save_character_scheduler_jobs(character_name: str, jobs: List[Dict[str, Any]]):
    """Speichert Scheduler-Jobs fuer einen Character in die DB (Upsert)."""
    now = datetime.now().isoformat()
    try:
        from app.core.db import transaction as _transaction
        with _transaction() as conn:
            existing_ids = {r[0] for r in conn.execute(
                "SELECT id FROM scheduler_jobs WHERE character_name=?",
                (character_name,),
            ).fetchall()}
            new_ids = {j.get("id") for j in jobs if j.get("id")}

            for jid in existing_ids - new_ids:
                conn.execute("DELETE FROM scheduler_jobs WHERE id=?", (jid,))

            for job in jobs:
                jid = job.get("id")
                if not jid:
                    continue
                action = job.get("action", {})
                trigger = job.get("trigger", {})
                conn.execute("""
                    INSERT INTO scheduler_jobs
                        (id, character_name, action, trigger, source, meta, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        character_name=excluded.character_name,
                        action=excluded.action,
                        trigger=excluded.trigger,
                        source=excluded.source,
                        meta=excluded.meta
                """, (
                    jid,
                    character_name,
                    json.dumps(action, ensure_ascii=False) if isinstance(action, dict) else str(action),
                    json.dumps(trigger, ensure_ascii=False) if isinstance(trigger, dict) else str(trigger),
                    job.get("source", ""),
                    json.dumps(job, ensure_ascii=False),
                    job.get("created_at", now),
                ))
    except Exception as e:
        get_logger("character").error("save_character_scheduler_jobs DB-Fehler fuer %s: %s", character_name, e)


def get_character_scheduler_logs(character_name: str) -> List[Dict[str, Any]]:
    """Laedt Scheduler-Logs fuer einen Character aus der DB."""
    try:
        from app.core.db import get_connection as _get_conn
        conn = _get_conn()
        # Alle Job-IDs des Characters
        job_ids = [r[0] for r in conn.execute(
            "SELECT id FROM scheduler_jobs WHERE character_name=?",
            (character_name,),
        ).fetchall()]
        if not job_ids:
            return []
        placeholders = ",".join("?" * len(job_ids))
        rows = conn.execute(
            f"SELECT job_id, ts, status, result FROM scheduler_logs "
            f"WHERE job_id IN ({placeholders}) ORDER BY ts DESC LIMIT 500",
            job_ids,
        ).fetchall()
        return [{"job_id": r[0], "timestamp": r[1], "status": r[2], "result": r[3] or ""} for r in rows]
    except Exception as e:
        get_logger("character").warning("get_character_scheduler_logs DB-Fehler fuer %s: %s", character_name, e)
    # Fallback: JSON-Datei
    scheduler_dir = get_character_scheduler_dir(character_name)
    logs_path = scheduler_dir / "job_logs.json"
    if logs_path.exists():
        try:
            return json.loads(logs_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def save_character_scheduler_logs(character_name: str, logs: List[Dict[str, Any]]):
    """Speichert Scheduler-Logs fuer einen Character in die DB."""
    try:
        from app.core.db import transaction as _transaction
        with _transaction() as conn:
            for log_entry in logs:
                job_id = log_entry.get("job_id", "")
                if not job_id:
                    continue
                _result = log_entry.get("result", "")
                # Dict/List serialisieren — SQLite kann nur str/int/float/bytes/None
                # binden. Manche Jobs liefern strukturierte Ergebnisse.
                if isinstance(_result, (dict, list)):
                    try:
                        _result = json.dumps(_result, ensure_ascii=False)
                    except Exception:
                        _result = str(_result)
                elif _result is None:
                    _result = ""
                conn.execute(
                    "INSERT INTO scheduler_logs (job_id, ts, status, result) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        job_id,
                        log_entry.get("timestamp", datetime.now().isoformat()),
                        log_entry.get("status", ""),
                        _result,
                    )
                )
    except Exception as e:
        get_logger("character").error("save_character_scheduler_logs DB-Fehler fuer %s: %s", character_name, e)


def _normalize_schedule_slots(raw_slots: Any) -> List[Dict[str, Any]]:
    """Bringt Slots auf das aktuelle Schema {hour, location, role, sleep}.

    Translates Legacy-Slots ohne Datenmigration: das alte ``activity``-Feld
    wird verworfen; die Sentinels ``__sleep__`` (in ``activity`` oder
    ``location``) werden zu ``sleep: True``; Stunden mit der alten
    ``__llm_choice__``-Location fallen weg, weil leere Stunden im neuen
    Schema implizit "KI waehlt" bedeuten.
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(raw_slots, list):
        return out
    for s in raw_slots:
        if not isinstance(s, dict):
            continue
        try:
            hour = int(s.get("hour"))
        except (TypeError, ValueError):
            continue
        if hour < 0 or hour > 23:
            continue
        # Sleep-Erkennung: explizites Flag oder Legacy-Sentinel.
        legacy_loc = (s.get("location") or "").strip()
        legacy_act = (s.get("activity") or "").strip()
        is_sleep = bool(s.get("sleep")) or legacy_loc == "__sleep__" or legacy_act == "__sleep__"
        if is_sleep:
            out.append({"hour": hour, "sleep": True, "location": "", "role": ""})
            continue
        # Legacy "Ortsunabhaengig" -> verwerfen (leer = KI waehlt im neuen Schema).
        if legacy_loc == "__llm_choice__":
            continue
        location = legacy_loc
        role = (s.get("role") or "").strip()
        if not location and not role:
            continue
        out.append({"hour": hour, "location": location, "role": role, "sleep": False})
    return out


def get_character_daily_schedule(character_name: str) -> Dict[str, Any]:
    """Laedt den Tagesablauf fuer einen Character aus der DB."""
    schedule: Dict[str, Any] = {"enabled": False, "slots": []}
    try:
        from app.core.db import get_connection as _get_conn
        conn = _get_conn()
        row = conn.execute(
            "SELECT enabled, slots, meta FROM daily_schedules WHERE character_name=?",
            (character_name,),
        ).fetchone()
        if row:
            meta = {}
            try:
                meta = json.loads(row[2] or "{}")
            except Exception:
                pass
            schedule = meta if isinstance(meta, dict) and "slots" in meta else {"character": character_name}
            schedule["enabled"] = bool(row[0])
            try:
                schedule["slots"] = json.loads(row[1] or "[]")
            except Exception:
                schedule["slots"] = []
    except Exception as e:
        get_logger("character").warning("get_character_daily_schedule DB-Fehler fuer %s: %s", character_name, e)
        # Fallback: JSON-Datei (Altdaten von vor der DB-Migration).
        path = get_character_scheduler_dir(character_name) / "daily_schedule.json"
        if path.exists():
            try:
                schedule = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
    schedule["slots"] = _normalize_schedule_slots(schedule.get("slots"))
    schedule.setdefault("enabled", False)
    return schedule


def save_character_daily_schedule(character_name: str, schedule: Dict[str, Any]):
    """Speichert den Tagesablauf fuer einen Character in die DB."""
    now = datetime.now().isoformat()
    schedule["last_updated"] = now
    try:
        from app.core.db import transaction as _transaction
        with _transaction() as conn:
            conn.execute("""
                INSERT INTO daily_schedules (character_name, enabled, slots, meta)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(character_name) DO UPDATE SET
                    enabled=excluded.enabled,
                    slots=excluded.slots,
                    meta=excluded.meta
            """, (
                character_name,
                1 if schedule.get("enabled") else 0,
                json.dumps(schedule.get("slots", []), ensure_ascii=False),
                json.dumps(schedule, ensure_ascii=False),
            ))
    except Exception as e:
        get_logger("character").error("save_character_daily_schedule DB-Fehler fuer %s: %s", character_name, e)


OFFMAP_SLEEP_SENTINEL = "__offmap__"


def enter_offmap_sleep(character_name: str) -> bool:
    """Character verschwindet von der Karte (offmap-Schlafstaette).

    Speichert die aktuelle Location/Raum als ``_offmap_return_location`` /
    ``_offmap_return_room`` im Profil, damit beim Aufwachen via
    :func:`wake_from_offmap` der vorherige Standort wiederhergestellt wird.
    Setzt aktuelle Location/Raum auf "" — der Character ist auf der Karte
    nicht mehr sichtbar (landet im "Ohne Ort"-Tray) und Pathfinder hat
    nichts mehr zu routen.

    Returns True wenn etwas geaendert wurde.
    """
    profile = get_character_profile(character_name) or {}
    current_loc = (profile.get("current_location") or "").strip()
    current_room = (profile.get("current_room") or "").strip()
    if not current_loc and not current_room:
        # Schon offmap (oder noch nie zugewiesen) — nichts zu speichern.
        return False
    profile["_offmap_return_location"] = current_loc
    profile["_offmap_return_room"] = current_room
    profile["current_location"] = ""
    profile["current_room"] = ""
    save_character_profile(character_name, profile)
    get_logger("character").info(
        "Offmap-Sleep: %s -> verschwindet von der Karte (return: %s/%s)",
        character_name, current_loc or "-", current_room or "-")
    return True


def wake_from_offmap(character_name: str) -> bool:
    """Stellt den vor-Offmap-Standort wieder her.

    Nur aktiv wenn der Character aktuell offmap ist (current_location leer)
    UND ``_offmap_return_location`` im Profil gesetzt ist. Idempotent —
    spaetere Aufrufe ohne Return-Marker sind no-ops.

    Returns True wenn der Character zurueckgeholt wurde.
    """
    profile = get_character_profile(character_name) or {}
    if (profile.get("current_location") or "").strip():
        # Steht schon irgendwo — nichts zu tun.
        return False
    return_loc = (profile.get("_offmap_return_location") or "").strip()
    if not return_loc:
        return False
    return_room = (profile.get("_offmap_return_room") or "").strip()
    profile["current_location"] = return_loc
    if return_room:
        profile["current_room"] = return_room
    profile.pop("_offmap_return_location", None)
    profile.pop("_offmap_return_room", None)
    save_character_profile(character_name, profile)
    get_logger("character").info(
        "Offmap-Wake: %s -> zurueck nach %s/%s",
        character_name, return_loc, return_room or "-")
    return True


def is_character_sleeping(character_name: str) -> bool:
    """Prueft ob der Character gerade wirklich schlaeft.

    STRIKT: nur ``current_activity == "sleeping"`` zaehlt. Der Daily-Schedule
    ist NUR ein Hinweis fuer den LLM im Prompt (``daily_schedule_block``) —
    er nimmt dem Char nicht den AgentLoop-Tick weg und markiert ihn auch
    nicht in Chats als "schlafend". Der LLM entscheidet selbst, ob er der
    Schlaf-Empfehlung folgt und ``set_activity:sleeping`` aufruft; erst
    DANN gilt der Char als wirklich schlafend (und der ``on_start``-Trigger
    der Sleeping-Activity verschickt ihn ggf. off-map).
    """
    try:
        profile = get_character_profile(character_name) or {}
        cur_act = (profile.get("current_activity") or "").strip().lower()
        return cur_act == "sleeping"
    except Exception:
        return False


def delete_character_daily_schedule(character_name: str) -> bool:
    """Loescht den Tagesablauf fuer einen Character."""
    path = get_character_scheduler_dir(character_name) / "daily_schedule.json"
    if path.exists():
        path.unlink()
        return True
    return False


def delete_character_image(character_name: str, image_filename: str) -> bool:
    """Loescht ein Bild, Metadaten und bereinigt Chat-Referenzen."""
    images_dir = get_character_images_dir(character_name)
    image_path = images_dir / image_filename

    if not image_path.exists():
        return False

    image_path.unlink()

    # Profilbild zuruecksetzen falls noetig
    profile = get_character_profile(character_name)
    if profile.get("profile_image") == image_filename:
        remaining = get_character_images(character_name)
        profile["profile_image"] = remaining[0] if remaining else ""
        save_character_profile(character_name, profile)

    # Metadaten-Datei loeschen
    meta_path = _get_image_meta_path(character_name, image_filename)
    if meta_path.exists():
        try:
            meta_path.unlink()
        except Exception:
            pass

    # Zugehoeriges Video loeschen (gleicher Stem, .mp4)
    stem = Path(image_filename).stem
    for video_ext in (".mp4", ".webm"):
        video_path = images_dir / f"{stem}{video_ext}"
        if video_path.exists():
            try:
                video_path.unlink()
            except Exception:
                pass
        # Auch Varianten mit _variant Suffix pruefen
        for variant in images_dir.glob(f"{stem}_*{video_ext}"):
            try:
                variant.unlink()
            except Exception:
                pass

    _cleanup_image_from_chats(character_name, image_filename)
    return True


def _cleanup_image_from_chats(character_name: str, image_filename: str):
    """Entfernt Bild-Referenzen aus allen Chat-Dateien."""
    import re
    chat_dir = get_character_dir(character_name) / "chats"
    if not chat_dir.exists():
        return

    # Match both old /agents/ and new /characters/ URL patterns
    pattern = re.compile(
        r'!\[[^\]]*\]\([^)]*' + re.escape(image_filename) + r'[^)]*\)\s*'
    )

    for chat_file in chat_dir.glob("*.json"):
        try:
            messages = json.loads(chat_file.read_text())
            if not isinstance(messages, list):
                continue

            changed = False
            for msg in messages:
                if not isinstance(msg, dict) or "content" not in msg:
                    continue
                cleaned = pattern.sub('', msg["content"])
                if cleaned != msg["content"]:
                    msg["content"] = cleaned
                    changed = True

            if changed:
                chat_file.write_text(json.dumps(messages, ensure_ascii=False, indent=2))
        except Exception:
            pass


def cleanup_orphaned_images(character_name: str) -> Dict[str, Any]:
    """Loescht Bilddateien die nicht im Profil registriert sind.

    Returns dict with deleted filenames and count.
    """
    profile = get_character_profile(character_name)
    registered = set(profile.get("images", []))
    images_dir = get_character_images_dir(character_name)

    deleted = []
    for f in images_dir.iterdir():
        if f.is_file() and f.name not in registered:
            f.unlink()
            deleted.append(f.name)

    if deleted:
        logger.info("%s: %d orphaned image(s) deleted", character_name, len(deleted))

    return {"character": character_name, "deleted": deleted, "count": len(deleted)}


