"""i18n routes — supported language list + translation maps for the client.

The client loads ``/i18n/languages`` once on bootstrap (populates the header
language selector) and ``/i18n/translations/<lang>`` whenever the active
language changes. Both reads are cached in-process; ``reload_translations``
flips the cache when admins edit the JSON files via the live config tooling
(separate plan).
"""
from fastapi import APIRouter, HTTPException
from typing import Any, Dict

from app.core.i18n import _load_translations, list_languages

router = APIRouter(prefix="/i18n", tags=["i18n"])


@router.get("/languages")
def get_languages() -> Dict[str, Any]:
    """Supported UI languages from shared/config/languages.json."""
    return {"languages": list_languages()}


@router.get("/translations/{lang}")
def get_translations(lang: str) -> Dict[str, Any]:
    """Translation map for a single language code.

    English is implicit (returns an empty map — the client uses the source
    string directly when ``lang == "en"``).
    """
    lang = (lang or "").strip().lower()
    if not lang:
        raise HTTPException(status_code=400, detail="lang required")
    table = _load_translations(lang)
    return {"lang": lang, "translations": table}
