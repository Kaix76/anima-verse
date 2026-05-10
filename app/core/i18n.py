"""Internationalization helpers (UI strings + localized data fields).

Two distinct mechanisms — see development_instructions/plan-multilanguage.md:

1. ``t(en, lang)``: takes an English source string from the code and looks it
   up in ``shared/languages/<lang>.json``. Used for static UI strings emitted
   by Python-rendered admin pages, toasts forwarded from the server, etc.
   When ``lang == "en"`` (or unknown) the source string is returned unchanged.

2. ``localized(obj, field, lang)``: takes a data dict (activity, item, rule,
   template field) and returns ``obj[f"{field}_{lang}"]`` if present and
   non-empty, otherwise ``obj[field]``. Centralizes the older
   ``activity_library.get_localized_field`` so every renderer uses the same
   fallback rule.

The translation map is cached in-process. Restart the server to pick up
edits to ``shared/languages/<lang>.json``.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from app.core.log import get_logger
from app.core.paths import get_config_dir, get_languages_dir

logger = get_logger("i18n")

# ---- Translation map (UI strings) ------------------------------------------

_TRANSLATIONS_CACHE: Dict[str, Dict[str, str]] = {}
_MISSING_KEYS_LOGGED: set = set()


def _load_translations(lang: str) -> Dict[str, str]:
    """Load translations for ``lang``. Returns empty dict on error/missing."""
    if not lang or lang == "en":
        return {}
    if lang in _TRANSLATIONS_CACHE:
        return _TRANSLATIONS_CACHE[lang]
    path = get_languages_dir() / f"{lang}.json"
    data: Dict[str, str] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            data = dict(raw.get("translations", {}))
        except Exception as e:
            logger.warning("failed to load %s: %s", path, e)
    _TRANSLATIONS_CACHE[lang] = data
    return data


def reload_translations(lang: Optional[str] = None) -> None:
    """Drop the in-process cache so the next ``t(...)`` call re-reads disk."""
    if lang is None:
        _TRANSLATIONS_CACHE.clear()
        _MISSING_KEYS_LOGGED.clear()
    else:
        _TRANSLATIONS_CACHE.pop(lang, None)


def t(en: str, lang: Optional[str] = None) -> str:
    """Translate an English UI source string into ``lang``.

    Falls back to the English source on missing entries (never raises).
    Each missing key is logged once at DEBUG level so the build can flag
    coverage gaps without spamming the log.
    """
    if not en:
        return en
    if not lang or lang == "en":
        return en
    table = _load_translations(lang)
    if en in table and table[en]:
        return table[en]
    cache_key = (lang, en)
    if cache_key not in _MISSING_KEYS_LOGGED:
        _MISSING_KEYS_LOGGED.add(cache_key)
        logger.debug("missing translation [%s]: %r", lang, en)
    return en


# ---- Localized data fields -------------------------------------------------

def localized(obj: Optional[Dict[str, Any]], field: str, lang: str = "en") -> Any:
    """Return ``obj[field_<lang>]`` if non-empty, else ``obj[field]``.

    Mirrors the legacy ``activity_library.get_localized_field`` so every
    renderer (activities, rules, items, prompt filters, character templates)
    can use one helper. Empty strings count as missing.
    """
    if not obj:
        return ""
    if lang and lang != "en":
        v = obj.get(f"{field}_{lang}")
        if v not in (None, ""):
            return v
    return obj.get(field, "")


# ---- Language list (single source of truth) -------------------------------

_LANGUAGES_CACHE: Optional[List[Dict[str, str]]] = None


def list_languages() -> List[Dict[str, str]]:
    """Return the supported-language list from ``shared/config/languages.json``.

    Each entry is ``{"value": "<code>", "label": "<English name>", ...}``.
    The script.js used to maintain a parallel list — that copy is now gone;
    callers should pull from this single source.
    """
    global _LANGUAGES_CACHE
    if _LANGUAGES_CACHE is not None:
        return _LANGUAGES_CACHE
    path = get_config_dir() / "languages.json"
    items: List[Dict[str, str]] = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items = list(data.get("languages", []))
        except Exception as e:
            logger.warning("failed to load %s: %s", path, e)
    _LANGUAGES_CACHE = items
    return items
