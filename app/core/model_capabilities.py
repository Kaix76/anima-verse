"""Model Capabilities — Lookup fuer Modell-Faehigkeiten (Tool-Calling, Vision, etc.)

Liest storage/model_capabilities.json und bietet Substring-basiertes Matching
(laengster Match gewinnt, analog zu tool_formats.MODEL_FORMAT_LIBRARY).
"""
import json
from typing import Any, Dict, Optional

from app.core.log import get_logger

logger = get_logger("model_capabilities")

from app.core.paths import get_storage_dir as _get_storage_dir

_cache: Optional[Dict[str, Any]] = None


def _load() -> Dict[str, Any]:
    """Laedt die Capabilities-Datei (lazy, cached)."""
    global _cache
    if _cache is not None:
        return _cache
    if not (_get_storage_dir() / "model_capabilities.json").exists():
        _cache = {}
        return _cache
    try:
        with open((_get_storage_dir() / "model_capabilities.json"), "r", encoding="utf-8") as f:
            data = json.load(f)
        _cache = data.get("models", {})
    except Exception as e:
        logger.error("Fehler beim Laden von %s: %s", (_get_storage_dir() / "model_capabilities.json"), e)
        _cache = {}
    return _cache


def _load_full_file() -> Dict[str, Any]:
    """Laedt die komplette JSON-Datei (inkl. _comment etc.)."""
    if not (_get_storage_dir() / "model_capabilities.json").exists():
        return {"_comment": "Model Capabilities.", "models": {}}
    try:
        with open((_get_storage_dir() / "model_capabilities.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"_comment": "Model Capabilities.", "models": {}}


def _save_full_file(data: Dict[str, Any]) -> None:
    """Speichert die komplette JSON-Datei und invalidiert den Cache."""
    global _cache
    (_get_storage_dir() / "model_capabilities.json").parent.mkdir(parents=True, exist_ok=True)
    with open((_get_storage_dir() / "model_capabilities.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    _cache = None


def get_model_capabilities(model_name: str) -> Dict[str, Any]:
    """Ermittelt Capabilities fuer ein Modell per Substring-Match.

    Laengster Match gewinnt. Fallback auf '_default' Eintrag.

    Args:
        model_name: Vollstaendiger Modellname (z.B. "OllamaChat::mistral:7b",
                     "mistral:7b", "hf.co/Naphula/Slimaki-24B-v1-GGUF:Q4_K_M")

    Returns:
        Dict mit capabilities: tool_calling, vision, notes_de, ...
    """
    models = _load()
    if not model_name or not models:
        return models.get("_default", {})

    # Provider-Prefix entfernen (z.B. "OllamaChat::mistral:7b" -> "mistral:7b")
    if "::" in model_name:
        model_name = model_name.split("::", 1)[1]

    model_lower = model_name.lower()

    # Exakter Match zuerst
    if model_lower in models:
        return models[model_lower]

    # Substring-Match (laengster Match gewinnt)
    best_match = ""
    best_caps = models.get("_default", {})

    for pattern, caps in models.items():
        if pattern.startswith("_"):
            continue
        if pattern.lower() in model_lower and len(pattern) > len(best_match):
            best_match = pattern
            best_caps = caps

    return best_caps


def get_all_capabilities() -> Dict[str, Any]:
    """Gibt alle Eintraege zurueck (fuer Admin-Seite)."""
    return dict(_load())


def save_model_capability(pattern: str, capabilities: Dict[str, Any]) -> None:
    """Speichert/aktualisiert einen Eintrag in model_capabilities.json."""
    data = _load_full_file()
    if "models" not in data:
        data["models"] = {}
    data["models"][pattern] = capabilities
    _save_full_file(data)


def delete_model_capability(pattern: str) -> bool:
    """Loescht einen Eintrag. Gibt True zurueck wenn er existierte."""
    data = _load_full_file()
    models = data.get("models", {})
    if pattern in models and not pattern.startswith("_"):
        del models[pattern]
        _save_full_file(data)
        return True
    return False
