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

        if prompt_type == "expression":
            instruction = (
                f"Describe the facial expression for someone feeling '{value}' "
                f"in one short English sentence. Focus on eyes, eyebrows, mouth, jaw. "
                f"Example: 'warm genuine smile, bright sparkling eyes, raised cheeks, relaxed brow'. "
                f"Reply ONLY with the description, no explanation."
            )
        else:
            instruction = (
                f"Describe the body pose for someone who is '{value}' "
                f"in one short English sentence. Focus on body position, arms, legs, posture. "
                f"Example: 'sitting comfortably in a chair, legs crossed, hands resting on lap'. "
                f"Reply ONLY with the description, no explanation."
            )

        response = llm_call(
            task="expression_map",
            system_prompt="",
            user_prompt=instruction)
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
