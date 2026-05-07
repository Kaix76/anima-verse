"""Expression & Pose Prompt Mapping — JSON-based presets with LLM fallback.

Maps mood strings to facial expression prompts and activity strings to
body pose prompts for the separated ComfyUI workflow.

Presets are loaded from external JSON files:
  shared/templates/expression/expression_presets.json
  shared/templates/expression/expression_presets_generated.json  (LLM-generated, not in git)
  shared/templates/pose/pose_presets.json
  shared/templates/pose/pose_presets_generated.json             (LLM-generated, not in git)
"""

import json
import threading
from pathlib import Path
from typing import Optional, Tuple

from app.core.log import get_logger
from app.core.paths import get_expression_presets_dir, get_pose_presets_dir

logger = get_logger(__name__)

_json_lock = threading.Lock()

# Hardcoded fallbacks (only used when JSON files are missing entirely)
_FALLBACK_EXPRESSION = (
    "confident smirk, one eyebrow slightly raised, "
    "eyes direct and steady, lips slightly pursed"
)
_FALLBACK_POSE = (
    "standing with one hand on hip, weight shifted to one leg, "
    "shoulder slightly raised, chin up"
)


def _load_presets_from_file(filepath: Path) -> Tuple[dict[str, str], str]:
    """Load preset map from a single JSON file.

    Returns (flat_dict, default_prompt).
    The entry with ``"_default": true`` is used as the default prompt.
    """
    result: dict[str, str] = {}
    default_prompt = ""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key, entry in data.get("presets", {}).items():
            prompt = entry.get("prompt", "")
            if entry.get("_default"):
                default_prompt = prompt
            result[key.strip().lower()] = prompt
            for syn in entry.get("synonyms", []):
                result[syn.strip().lower()] = prompt
    except FileNotFoundError:
        logger.warning("Preset-Datei nicht gefunden: %s — verwende leere Presets", filepath)
    except Exception as e:
        logger.error("Fehler beim Laden von %s: %s", filepath, e)
    return result, default_prompt


def _load_keymap_from_file(filepath: Path) -> dict[str, str]:
    """Build synonym -> primary-preset-key map from a preset JSON file.

    Used to canonicalize free-form activity/mood strings into a stable
    cache-key dimension (so 'serving beer' and 'bartending' collapse onto
    the same primary pose-preset key).
    """
    result: dict[str, str] = {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        for primary, entry in data.get("presets", {}).items():
            primary_low = primary.strip().lower()
            if not primary_low:
                continue
            result[primary_low] = primary_low
            for syn in entry.get("synonyms", []):
                result[syn.strip().lower()] = primary_low
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error("Fehler beim Laden Key-Map %s: %s", filepath, e)
    return result


def _load_partner_keys_from_file(filepath: Path) -> set[str]:
    """Collect primary keys of pose presets explicitly tagged ``"solo": false``.

    Partner activities depict two people interacting (kissing, embracing).
    The current image-gen pipeline only injects one character into the
    prompt, so the model duplicates the subject — producing the
    'character-hugs-themselves' artefact. Tagged keys are skipped at the
    trigger layer; the avatar keeps showing its last variant instead.
    """
    result: set[str] = set()
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        for primary, entry in data.get("presets", {}).items():
            if entry.get("solo") is False:
                primary_low = primary.strip().lower()
                if primary_low:
                    result.add(primary_low)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error("Fehler beim Laden Partner-Keys %s: %s", filepath, e)
    return result


def _load_presets(kind: str) -> Tuple[dict[str, str], str]:
    """Load curated presets and merge with generated presets from separate file.

    kind: "expression" oder "pose" — bestimmt den Ordner und die Dateinamen.
    """
    if kind == "expression":
        base_dir = get_expression_presets_dir()
        curated_name = "expression_presets.json"
        generated_name = "expression_presets_generated.json"
    else:
        base_dir = get_pose_presets_dir()
        curated_name = "pose_presets.json"
        generated_name = "pose_presets_generated.json"

    curated, default_prompt = _load_presets_from_file(base_dir / curated_name)
    generated, _ = _load_presets_from_file(base_dir / generated_name)
    # Merge: curated take precedence
    merged = {**generated, **curated}
    return merged, default_prompt


EXPRESSION_PRESETS, DEFAULT_EXPRESSION = _load_presets("expression")
if not DEFAULT_EXPRESSION:
    DEFAULT_EXPRESSION = _FALLBACK_EXPRESSION

POSE_PRESETS, DEFAULT_POSE = _load_presets("pose")
if not DEFAULT_POSE:
    DEFAULT_POSE = _FALLBACK_POSE

logger.info("Expression-Presets geladen: %d Eintraege", len(EXPRESSION_PRESETS))
logger.info("Pose-Presets geladen: %d Eintraege", len(POSE_PRESETS))


# ---------------------------------------------------------------------------
# Synonym -> primary key maps (used by variant cache to collapse equivalent
# activities/moods onto a single cache slot).
# ---------------------------------------------------------------------------

POSE_KEY_MAP: dict[str, str] = {}
PARTNER_POSE_KEYS: set[str] = set()
for _kind_name, _curated, _generated in [
    ("pose",
     get_pose_presets_dir() / "pose_presets.json",
     get_pose_presets_dir() / "pose_presets_generated.json"),
]:
    POSE_KEY_MAP.update(_load_keymap_from_file(_generated))
    POSE_KEY_MAP.update(_load_keymap_from_file(_curated))  # curated wins
    PARTNER_POSE_KEYS |= _load_partner_keys_from_file(_generated)
    PARTNER_POSE_KEYS |= _load_partner_keys_from_file(_curated)
if PARTNER_POSE_KEYS:
    logger.info("Partner-Pose-Keys (skip variant gen): %s",
                 sorted(PARTNER_POSE_KEYS))


def _load_mood_buckets() -> Tuple[dict[str, str], str]:
    """Load mood -> bucket map from expression_buckets.json.

    Auto-expands using synonyms from expression_presets.json so that any
    synonym of a bucketed mood maps to the same bucket.
    """
    bucket_file = get_expression_presets_dir() / "expression_buckets.json"
    mapping: dict[str, str] = {}
    default_bucket = "neutral"
    try:
        with open(bucket_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        default_bucket = (data.get("default") or "neutral").strip().lower()
        for bucket_name, mood_list in (data.get("buckets") or {}).items():
            b = bucket_name.strip().lower()
            for m in mood_list or []:
                mapping[m.strip().lower()] = b
    except FileNotFoundError:
        logger.warning("expression_buckets.json fehlt — alle Moods → '%s'", default_bucket)
        return {}, default_bucket
    except Exception as e:
        logger.error("Fehler beim Laden expression_buckets.json: %s", e)
        return {}, default_bucket

    # Expand via expression preset synonyms: if mood X is in a bucket and X is
    # the primary key of a preset, all of X's synonyms inherit the bucket too.
    expression_keymap: dict[str, str] = {}
    for _curated_name in ("expression_presets.json",
                           "expression_presets_generated.json"):
        expression_keymap.update(
            _load_keymap_from_file(get_expression_presets_dir() / _curated_name))
    for syn, primary in expression_keymap.items():
        if syn in mapping:
            continue
        if primary in mapping:
            mapping[syn] = mapping[primary]
    return mapping, default_bucket


MOOD_BUCKET_MAP, DEFAULT_MOOD_BUCKET = _load_mood_buckets()
logger.info("Mood-Buckets geladen: %d Eintraege, default='%s'",
             len(MOOD_BUCKET_MAP), DEFAULT_MOOD_BUCKET)

# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def get_expression_prompt(mood: str) -> Optional[str]:
    """Look up expression prompt from presets. Returns None if not found."""
    if not mood:
        return DEFAULT_EXPRESSION
    key = mood.strip().lower()
    if key in EXPRESSION_PRESETS:
        return EXPRESSION_PRESETS[key]
    for preset_key, prompt in EXPRESSION_PRESETS.items():
        if preset_key in key or key in preset_key:
            return prompt
    return None


def get_pose_prompt(activity: str) -> Optional[str]:
    """Look up pose prompt from presets. Returns None if not found."""
    if not activity:
        return DEFAULT_POSE
    key = activity.strip().lower()
    if key in POSE_PRESETS:
        return POSE_PRESETS[key]
    for preset_key, prompt in POSE_PRESETS.items():
        if preset_key in key or key in preset_key:
            return prompt
    return None


def is_partner_activity(activity: str) -> bool:
    """True if ``activity`` resolves to a pose preset tagged ``solo: false``.

    Used at the variant-generation trigger to bail out: we can't render a
    partner pose with only one character in the prompt without producing
    duplicate-subject artefacts.
    """
    if not activity:
        return False
    key = resolve_pose_key(activity)
    return bool(key and key in PARTNER_POSE_KEYS)


def resolve_pose_key(activity: str) -> Optional[str]:
    """Resolve free-form activity text to a canonical pose-preset primary key.

    Used by the variant cache to collapse synonymous activities onto a single
    cache slot. Pure preset/synonym lookup — does NOT call the LLM. Returns
    None if no preset matches; the caller can fall back to its own
    normalization (e.g. word-truncation).
    """
    if not activity:
        return None
    key = activity.strip().lower()
    if not key:
        return None
    if key in POSE_KEY_MAP:
        return POSE_KEY_MAP[key]
    # Substring match against primary keys only (synonyms are already in the
    # exact-match path). Prefer the longest matching primary key so 'cooking
    # dinner' matches 'cooking' rather than a shorter accidental substring.
    best = None
    for preset_key in POSE_KEY_MAP.values():
        if preset_key in key or key in preset_key:
            if best is None or len(preset_key) > len(best):
                best = preset_key
    return best


def mood_bucket(mood: str) -> str:
    """Map a mood string to a coarse body-language bucket.

    The returned bucket — not the raw mood — is what goes into the variant
    cache key. FaceSwap removes most fine mood detail anyway, so generating a
    separate image per mood synonym is wasted GPU time. Unknown moods fall
    back to ``DEFAULT_MOOD_BUCKET``.
    """
    if not mood:
        return DEFAULT_MOOD_BUCKET
    key = mood.strip().lower()
    if not key:
        return DEFAULT_MOOD_BUCKET
    if key in MOOD_BUCKET_MAP:
        return MOOD_BUCKET_MAP[key]
    # Substring fallback: catches mood-leakage like 'feels happy'
    for mk, bucket in MOOD_BUCKET_MAP.items():
        if mk and mk in key:
            return bucket
    return DEFAULT_MOOD_BUCKET


# ---------------------------------------------------------------------------
# Resolve: preset -> LLM fallback (persisted to JSON) -> default
# ---------------------------------------------------------------------------


def resolve_expression_prompt(mood: str) -> str:
    """Resolve expression prompt: preset -> LLM generate+persist -> default."""
    result = get_expression_prompt(mood)
    if result:
        return result
    result = _llm_generate_and_save("expression", mood)
    if result:
        return result
    return DEFAULT_EXPRESSION


def resolve_pose_prompt(activity: str) -> str:
    """Resolve pose prompt: preset -> LLM generate+persist -> default."""
    result = get_pose_prompt(activity)
    if result:
        return result
    result = _llm_generate_and_save("pose", activity)
    if result:
        return result
    return DEFAULT_POSE


# ---------------------------------------------------------------------------
# LLM generation + JSON persistence
# ---------------------------------------------------------------------------


def _llm_generate_and_save(prompt_type: str, value: str) -> Optional[str]:
    """Generate a prompt via LLM and persist it to the JSON preset file."""
    text = _llm_generate_prompt(prompt_type, value)
    if not text:
        return None

    key = value.strip().lower()

    # Add to in-memory presets
    if prompt_type == "expression":
        EXPRESSION_PRESETS[key] = text
    else:
        POSE_PRESETS[key] = text

    # Persist to generated JSON (separate file, not in git)
    if prompt_type == "expression":
        filepath = get_expression_presets_dir() / "expression_presets_generated.json"
    else:
        filepath = get_pose_presets_dir() / "pose_presets_generated.json"
    _save_preset_to_json(filepath, key, text)

    return text


def _save_preset_to_json(filepath: Path, key: str, prompt: str):
    """Append a new preset entry to the JSON file (thread-safe)."""
    with _json_lock:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {"presets": {}}

        presets = data.setdefault("presets", {})

        # Don't overwrite existing primary keys
        if key not in presets:
            presets[key] = {
                "prompt": prompt,
                "synonyms": [],
                "_generated": True
            }
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info("Neues Preset gespeichert in %s: '%s'", filepath.name, key)

    return True


def _llm_generate_prompt(prompt_type: str, value: str) -> Optional[str]:
    """Generate an expression or pose prompt via LLM call."""
    try:
        from app.core.llm_router import llm_call
        from app.core.prompt_templates import render_task

        sys_prompt, user_prompt = render_task(
            "expression_map", prompt_type=prompt_type, value=value)

        response = llm_call(
            task="expression_map",
            system_prompt=sys_prompt,
            user_prompt=user_prompt)
        text = (response.content or "").strip().strip('"').strip("'")
        if text and len(text) < 300:
            logger.info("LLM %s-Prompt fuer '%s': %s", prompt_type, value, text[:80])
            return text
        logger.warning("LLM %s-Prompt ungueltig: %s", prompt_type, text[:100])
    except RuntimeError:
        logger.debug("Kein LLM fuer %s-Prompt Generierung verfuegbar", prompt_type)
    except Exception as e:
        logger.error("LLM %s-Prompt Generierung fehlgeschlagen: %s", prompt_type, e)
    return None
