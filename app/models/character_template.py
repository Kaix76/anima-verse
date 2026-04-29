"""Character Template System

Templates live in shared/templates/character/ as JSON files (e.g. human-default.json).
Each character stores which template it uses in its profile ("template": "...").

Character templates support a base system: a template with "base": "base-character"
is merged with base-character.json at load time. The base provides core fields,
the extension adds profile-specific fields.

Field types:
  - text: Single-line or multi-line text (multiline: true)
  - number: Numeric value
  - date: Date value (YYYY-MM-DD)
  - select: Dropdown with predefined options [{value, label}]
"""
import copy
import json
from pathlib import Path
from typing import Dict, Any, List, Optional

from app.core.log import get_logger

logger = get_logger("char_template")

# Valid field types
FIELD_TYPES = {"text", "number", "date", "select"}

# Templates directory
from app.core.paths import get_templates_dir


def list_templates(template_type: Optional[str] = None) -> List[Dict[str, str]]:
    """List all character templates.

    Liest ausschliesslich aus shared/templates/character/ — Preset-Dateien fuer
    Expression/Pose liegen in eigenen Unterordnern und werden nicht mehr
    faelschlich als Character-Templates angeboten.

    template_type: nur noch akzeptiert fuer Abwaertskompatibilitaet;
    alles im character/-Ordner ist bereits type="character".
    """
    get_templates_dir().mkdir(parents=True, exist_ok=True)
    templates = []
    for path in sorted(get_templates_dir().glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("selectable") is False:
                continue
            templates.append({
                "name": path.stem,
                "label": data.get("label", path.stem),
                "type": "character",
            })
        except Exception:
            templates.append({"name": path.stem, "label": path.stem, "type": "character"})
    return templates


def _merge_templates(base: Dict[str, Any], extension: Dict[str, Any]) -> Dict[str, Any]:
    """Merge an extension template into a base template.

    - features: merged (extension values win)
    - tabs: extension fully overrides if present
    - sections with matching ID: column/row overridable, fields merged with
      key-based deduplication (extension field wins over base field of same key)
    - new sections from extension: appended in order
    - top-level metadata (label, template_version): taken from extension
    """
    result = copy.deepcopy(base)

    # features: merge dicts, extension wins per key
    if "features" in extension:
        result.setdefault("features", {}).update(extension["features"])

    # tabs: extension fully overrides if present
    if "tabs" in extension:
        result["tabs"] = copy.deepcopy(extension["tabs"])

    # sections: merge by id
    base_section_index = {s["id"]: s for s in result.get("sections", [])}

    for ext_section in extension.get("sections", []):
        sid = ext_section["id"]
        if sid in base_section_index:
            base_sec = base_section_index[sid]
            # Allow extension to override layout attributes
            for attr in ("column", "row"):
                if attr in ext_section:
                    base_sec[attr] = ext_section[attr]
            # Merge fields: section row determines order (lower row = fields first).
            # Extension fields override base fields with matching key.
            base_fields = base_sec.get("fields", [])
            ext_fields = copy.deepcopy(ext_section.get("fields", []))
            ext_by_key = {f["key"]: f for f in ext_fields if "key" in f}
            ext_key_set = set(ext_by_key.keys())
            base_key_set = {f["key"] for f in base_fields if "key" in f}
            base_row = base_sec.get("row", 1)
            ext_row = ext_section.get("row", 1)
            if ext_row < base_row:
                # Extension fields first, then base-only fields at end
                merged = list(ext_fields)
                for bf in base_fields:
                    if bf.get("key") not in ext_key_set:
                        merged.append(copy.deepcopy(bf))
            else:
                # Base fields first (extension overrides matching keys), then ext-only at end
                merged = []
                for bf in base_fields:
                    key = bf.get("key")
                    if key and key in ext_by_key:
                        merged.append(ext_by_key[key])  # extension wins
                    else:
                        merged.append(copy.deepcopy(bf))
                for ef in ext_fields:
                    if ef.get("key") not in base_key_set:
                        merged.append(ef)
            base_sec["fields"] = merged
        else:
            result.setdefault("sections", []).append(copy.deepcopy(ext_section))

    # top-level metadata from extension
    for key in ("label", "template_version"):
        if key in extension:
            result[key] = extension[key]

    # Alle restlichen Top-Level-Felder aus der Extension uebernehmen —
    # Extension gewinnt. Ohne das gehen Felder wie extra_activities,
    # outfit_imagegen, outfit_exceptions etc. stumm verloren.
    _handled = {"features", "tabs", "sections", "label",
                "template_version", "base"}
    for key, value in extension.items():
        if key in _handled:
            continue
        result[key] = copy.deepcopy(value)

    return result


# Template-Cache — Templates aendern sich nur durch Admin-Save.
# Der Cache wird bei save_template() invalidiert. Verhindert Disk-I/O im
# asyncio-Main-Loop bei Hot-Path-Calls wie ThoughtLoop._get_eligible_characters.
_template_cache: Dict[str, Optional[Dict[str, Any]]] = {}
_template_cache_lock = __import__("threading").RLock()


def clear_template_cache() -> None:
    """Invalidiert den Template-Cache — aufgerufen nach save_template()."""
    with _template_cache_lock:
        _template_cache.clear()


def get_template(name: str = "default") -> Optional[Dict[str, Any]]:
    """Load a character template by name from shared/templates/character/.

    If the template declares "base": "<name>", the base template is loaded
    first and the extension is merged on top via _merge_templates.
    Base templates themselves must not declare a base (no recursion).
    Returns None if the template file is not found.

    Ergebnis wird modulweit gecacht — bei Aenderungen muss clear_template_cache
    aufgerufen werden (save_template macht das automatisch).
    """
    with _template_cache_lock:
        if name in _template_cache:
            cached = _template_cache[name]
            # Deep-Copy damit Consumer das Cache-Original nicht mutieren koennen
            if cached is None:
                return None
            return copy.deepcopy(cached)

    path = get_templates_dir() / f"{name}.json"
    if not path.exists():
        with _template_cache_lock:
            _template_cache[name] = None
        return None
    try:
        tmpl = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Error loading template '%s': %s", name, e)
        return None

    base_name = tmpl.get("base")
    if base_name:
        base_path = get_templates_dir() / f"{base_name}.json"
        if not base_path.exists():
            logger.warning("Base '%s' not found, using '%s' standalone", base_name, name)
        else:
            try:
                base = json.loads(base_path.read_text(encoding="utf-8"))
                tmpl = _merge_templates(base, tmpl)
            except Exception as e:
                logger.error("Error loading base '%s': %s", base_name, e)

    with _template_cache_lock:
        _template_cache[name] = tmpl
    return copy.deepcopy(tmpl)


def is_roleplay_character(character_name: str) -> bool:
    """True wenn der Character ein RP-Character ist (vs. Chatbot).

    Kriterium: profile["roleplay_instructions"] ist gesetzt. Dieses Feld
    existiert nur in human-roleplay* Templates und enthaelt die "Important
    rules" (Stay in character, Du bist eine reale Person...).
    """
    try:
        from app.models.character import get_character_profile
        profile = get_character_profile(character_name) or {}
        return bool((profile.get("roleplay_instructions") or "").strip())
    except Exception:
        return False


def is_feature_enabled(character_name: str, feature: str) -> bool:
    """Prueft ob ein Feature fuer den Character aktiviert ist.

    Liest das Template des Characters und schaut in die features-Sektion.
    Default: True (fail-open fuer Characters ohne Template oder ohne Eintrag).

    Typische Features (siehe Templates):
      memory_enabled, relationships_enabled,
      relationship_summary_enabled, secrets_enabled, inventory_enabled,
      activities_enabled, locations_enabled, mood_tracking_enabled,
      thoughts_enabled, status_effects_enabled, social_dialog_enabled,
      random_events_enabled, outfit_system_enabled, expression_variants_enabled,
      story_enabled, storydev_enabled
    """
    try:
        from app.models.character import get_character_profile
        profile = get_character_profile(character_name) or {}
        tmpl_name = profile.get("template", "human-default")
        tmpl = get_template(tmpl_name)
        if not tmpl:
            return True
        features = tmpl.get("features", {}) or {}
        return bool(features.get(feature, True))
    except Exception:
        return True


def save_template(name: str, template: Dict[str, Any]) -> bool:
    """Save a template by name."""
    get_templates_dir().mkdir(parents=True, exist_ok=True)
    path = get_templates_dir() / f"{name}.json"
    try:
        path.write_text(
            json.dumps(template, ensure_ascii=False, indent=2),
            encoding="utf-8")
        clear_template_cache()
        return True
    except Exception as e:
        logger.error("Error saving template '%s': %s", name, e)
        return False


def delete_template(name: str) -> bool:
    """Delete a template by name. Cannot delete 'human-default'."""
    if name == "human-default":
        return False
    path = get_templates_dir() / f"{name}.json"
    if path.exists():
        path.unlink()
        return True
    return False


def get_all_template_fields(template: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten all fields from all sections into a single list."""
    fields = []
    for section in template.get("sections", []):
        fields.extend(section.get("fields", []))
    return fields


import re
from datetime import date


def get_prompt_fields(template: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return all fields where in_prompt is true, in template order."""
    fields = []
    for section in template.get("sections", []):
        for field in section.get("fields", []):
            if field.get("in_prompt", False):
                fields.append(field)
    return fields


def build_prompt_section(
    template: Dict[str, Any], data: Dict[str, Any],
    active_features: Optional[Dict[str, Any]] = None,
    is_partner: bool = False, character_name: str = "") -> List[str]:
    """Build prompt lines from template + profile data.

    Handles: empty values (skip), lists (join), age computation.
    If active_features is given, fields with "prompt_requires_feature"
    are only included when that feature is true in active_features.
    When is_partner=True, fields with "prompt_self_only": true are skipped
    (e.g. traits, goals, beliefs, rules belong only into the own character
    block, never into the partner block).

    Felder mit "source_file" werden aus einer MD-Datei im Character-Verzeichnis
    geladen statt aus den Profil-Daten (user_id+character_name muessen dafuer
    gesetzt sein). Fehlende Datei = Feld wird uebersprungen.

    Returns a list of "Label: value" strings.
    """
    lines = []
    for field in get_prompt_fields(template):
        # Feature-Gate: Feld nur aufnehmen wenn Feature aktiv
        required_feature = field.get("prompt_requires_feature")
        if required_feature and active_features is not None:
            if not active_features.get(required_feature):
                continue
        # Self-only: Im Partner-Block (Avatar) solche Felder auslassen
        if is_partner and field.get("prompt_self_only"):
            continue

        key = field["key"]

        # source_file: Inhalt aus MD-Datei statt aus Profil-Daten
        source_file = field.get("source_file")
        if source_file and character_name:
            value = _load_source_file(character_name, source_file)
            if not value:
                # Fallback: alter Wert im Profil (waehrend Migration)
                value = data.get(key)
        else:
            value = data.get(key)

        if not value:
            # Fall back to template default if profile has no value
            value = field.get("default")
            if not value:
                continue

        label = field.get("prompt_label") or field.get("label", key)

        # Special handling
        if field.get("prompt_compute") == "age":
            value = _compute_age(str(value))
            if not value:
                continue
        elif field.get("prompt_format") == "list":
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value if v)
                if not value:
                    continue

        # Mehrzeiliger Wert (z.B. strukturiertes MD): Label auf eigene Zeile
        if isinstance(value, str) and "\n" in value:
            lines.append(f"{label}:")
            lines.append(value)
        else:
            lines.append(f"{label}: {value}")
    return lines


def _load_source_file(character_name: str, relpath: str) -> str:
    """Laedt den Inhalt einer MD/Text-Datei aus dem Character-Verzeichnis.

    Aufbereitung fuer System-Prompt:
    - Lock-/Edit-Marker entfernen (HTML-Kommentare)
    - Top-Heading `# ...` weglassen (Label im Prompt uebernimmt diese Rolle)
    - Leere `## Section`-Headings entfernen (kein Body → Noise)

    Leerer String wenn Datei fehlt, leer oder nur leere Sections.
    """
    try:
        from app.models.character import get_character_dir
        import re as _re
        p = get_character_dir(character_name) / relpath
        if not p.exists():
            return ""
        text = p.read_text(encoding="utf-8")
        # Marker entfernen (HTML-Kommentare wie <!-- EDITABLE -->)
        text = _re.sub(r"<!--\s*[A-Z]+\s*-->\s*\n?", "", text)

        # In Sections zerlegen, leere Sections entfernen, Top-Heading droppen.
        lines = text.splitlines()
        result_parts = []
        cur_heading = None  # None = vor erstem ## Heading
        cur_body = []

        def _flush():
            if cur_heading is None:
                # Praeludium vor erstem ## (Top-`# Title` und Leerzeilen) — verwerfen
                return
            body_text = "\n".join(cur_body).strip()
            if body_text:
                result_parts.append(f"## {cur_heading}")
                result_parts.append(body_text)
                result_parts.append("")  # Trenner

        for line in lines:
            if line.startswith("# ") and not line.startswith("## "):
                # Top-Heading ignorieren
                _flush()
                cur_heading = None
                cur_body = []
            elif line.startswith("## "):
                _flush()
                cur_heading = line[3:].strip()
                cur_body = []
            else:
                cur_body.append(line)
        _flush()

        return "\n".join(result_parts).strip()
    except Exception:
        return ""


def _compute_age(date_str: str) -> Optional[str]:
    """Compute age from a date string (YYYY-MM-DD)."""
    try:
        parts = date_str.strip().split("-")
        birth = date(int(parts[0]), int(parts[1]), int(parts[2]))
        today = date.today()
        age = today.year - birth.year
        if (today.month, today.day) < (birth.month, birth.day):
            age -= 1
        return str(age) if age >= 0 else None
    except Exception:
        return None


def build_replacement_map(
    template: Dict[str, Any], profile: Dict[str, Any], target_key: str
) -> Dict[str, str]:
    """Build a map of {token} -> resolved value for a given target field.

    Only fields with a "replacement" config whose "target" matches target_key
    are included. Supports computed values (e.g. "age" from a birthdate field).
    """
    token_map = {}
    for field in get_all_template_fields(template):
        # Skip fields hidden by visible_when
        if not _is_field_visible(field, profile):
            continue
        repl = field.get("replacement")
        if not repl:
            continue
        # Check target match (string or list)
        targets = repl.get("target", "")
        if isinstance(targets, str):
            targets = [targets]
        if target_key not in targets:
            continue

        token = repl.get("token", field["key"])
        source_key = field["key"]
        raw_value = profile.get(source_key)

        if raw_value is None or not str(raw_value).strip() or str(raw_value) == "__custom__":
            continue

        # Apply compute transform
        compute = repl.get("compute")
        if compute == "age":
            computed = _compute_age(str(raw_value))
            if computed:
                token_map[token] = computed
        else:
            token_map[token] = str(raw_value)

    return token_map


def resolve_profile_tokens(
    text: str,
    profile: Dict[str, Any],
    template: Optional[Dict[str, Any]] = None,
    target_key: str = ""
) -> str:
    """Replace {key} tokens in text with values from the profile.

    When template is provided, only tokens explicitly configured via
    "replacement" fields targeting target_key are resolved.
    When template is None, falls back to replacing all {key} with profile values.
    """
    if not text or "{" not in text:
        return text

    if template and target_key:
        token_map = build_replacement_map(template, profile, target_key)
        def replacer(match):
            key = match.group(1)
            return token_map.get(key, match.group(0))
    else:
        # Fallback: replace any {key} with profile value
        def replacer(match):
            key = match.group(1)
            value = profile.get(key)
            if value is not None and str(value).strip() and str(value) != "__custom__":
                return str(value)
            return match.group(0)

    return re.sub(r"\{(\w+)\}", replacer, text)


def _is_field_visible(field: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """Check if a field is visible given the current data (visible_when support)."""
    vw = field.get("visible_when")
    if not vw:
        return True
    dep_key = vw.get("field", "")
    dep_value = data.get(dep_key, "")
    return dep_value in vw.get("values", [])




