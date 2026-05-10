"""World-level "setup" / "premise" — a free-form description of the world
that prefixes every roleplay and World-Dev LLM prompt.

The chat / WorldDev / RP templates each receive this text as a ``world_setup``
variable. The LLM gets it as a short briefing before the situation-specific
context (locations, characters, etc.) so it knows the world's tone, era,
genre constraints, etc.

Storage: ``worlds/<world>/world_setup.json`` — single field ``description``,
multi-line text. Empty string when the user hasn't filled it in yet.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from app.core.log import get_logger
from app.core.paths import get_storage_dir

logger = get_logger("world_setup")


def _path() -> Path:
    sd = get_storage_dir()
    sd.mkdir(parents=True, exist_ok=True)
    return sd / "world_setup.json"


def get_world_setup() -> Dict[str, str]:
    """Read the current world setup. Always returns a dict with at least
    ``description`` (string, possibly empty) so callers don't need to
    null-check."""
    p = _path()
    if not p.exists():
        return {"description": ""}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"description": ""}
        return {"description": str(data.get("description") or "").rstrip()}
    except Exception as e:
        logger.warning("world_setup load failed: %s", e)
        return {"description": ""}


def save_world_setup(description: str) -> Dict[str, str]:
    """Write the world setup to disk. Trims trailing whitespace; empty
    strings are persisted as-is so the file always exists once edited."""
    p = _path()
    cleaned = (description or "").rstrip()
    p.write_text(json.dumps({"description": cleaned}, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return {"description": cleaned}


def get_world_setup_text() -> str:
    """Convenience for template injection — returns just the description
    string (empty when unset). Templates should ``{% if world_setup %}``-
    gate the block so the LLM doesn't see an empty heading."""
    return get_world_setup().get("description", "")
