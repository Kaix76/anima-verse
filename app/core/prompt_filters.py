"""Prompt-Filter — pro Zustand drop blocks aus thought_context + add modifier.

Replaces the old ``danger_system.build_status_prompt_section`` rule path.
Each filter has:
    condition       — generic expression evaluated against character state
    drop_blocks     — list of *_block keys to clear from the prompt
    prompt_modifier — text rendered in the effects section so the LLM
                      sees what state is active

Storage:
    shared/prompt_filters/filters.json — versioned baseline
    world.db.prompt_filters             — per-world overlay (replaces by id)

Public API:
    apply_filters(character_name, ctx) -> dict
        mutates ctx in place: drops listed blocks, sets effects_block
        from accumulated modifier text. Returns the same ctx for chaining.
"""
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.core.log import get_logger

logger = get_logger("prompt_filters")


_SHARED_FILE = Path(__file__).resolve().parent.parent.parent / "shared" / "prompt_filters" / "filters.json"


def _load_shared() -> List[Dict[str, Any]]:
    """Read baseline filters from the shared JSON file. Empty on miss/parse-fail."""
    try:
        if not _SHARED_FILE.exists():
            return []
        data = json.loads(_SHARED_FILE.read_text(encoding="utf-8"))
        return list(data.get("filters") or [])
    except Exception as e:
        logger.warning("shared prompt_filters load failed: %s", e)
        return []


def _load_world() -> List[Dict[str, Any]]:
    """Read per-world filters from the prompt_filters table."""
    try:
        from app.core.db import get_connection
        conn = get_connection()
        rows = conn.execute(
            "SELECT id, condition, label, drop_blocks, prompt_modifier, enabled, meta "
            "FROM prompt_filters"
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for r in rows:
            try:
                drops = json.loads(r[3] or "[]")
            except Exception:
                drops = []
            try:
                meta = json.loads(r[6] or "{}")
            except Exception:
                meta = {}
            out.append({
                "id": r[0] or "",
                "condition": r[1] or "",
                "label": r[2] or "",
                "drop_blocks": drops if isinstance(drops, list) else [],
                "prompt_modifier": r[4] or "",
                "enabled": bool(r[5]),
                "meta": meta,
            })
        return out
    except Exception as e:
        logger.debug("world prompt_filters load failed: %s", e)
        return []


def load_filters() -> List[Dict[str, Any]]:
    """Merge shared + world filters. World entries override shared by id."""
    by_id: Dict[str, Dict[str, Any]] = {}
    for entry in _load_shared():
        fid = (entry.get("id") or "").strip()
        if fid:
            by_id[fid] = entry
    for entry in _load_world():
        fid = (entry.get("id") or "").strip()
        if fid:
            by_id[fid] = entry  # world overrides
    return list(by_id.values())


def _evaluate(condition: str, character_name: str, location_id: str = "") -> bool:
    """Reuse the existing condition evaluator (stamina<10, has_condition:X, …)."""
    if not condition:
        return False
    try:
        from app.core.activity_engine import evaluate_condition
        passed, _ = evaluate_condition(condition, character_name, location_id)
        return bool(passed)
    except Exception as e:
        logger.debug("evaluate_condition('%s') failed for %s: %s",
                     condition, character_name, e)
        return False


def apply_filters(character_name: str,
                  ctx: Dict[str, Any],
                  location_id: str = "") -> Dict[str, Any]:
    """Apply state-driven filters to a thought context dict.

    Drops blocks listed in triggered filters' ``drop_blocks`` (sets them to
    "" so the {% if %} gate skips them). Collects ``prompt_modifier`` text
    of all triggered filters into ``effects_block``.

    Mutates and returns the same ctx for caller convenience.
    """
    filters = load_filters()
    if not filters:
        return ctx

    triggered_modifiers: List[str] = []
    dropped: set = set()

    for f in filters:
        if not f.get("enabled", True):
            continue
        condition = (f.get("condition") or "").strip()
        if not condition:
            continue
        if not _evaluate(condition, character_name, location_id):
            continue
        for blk in (f.get("drop_blocks") or []):
            if isinstance(blk, str) and blk:
                dropped.add(blk)
        modifier = (f.get("prompt_modifier") or "").strip()
        if modifier:
            triggered_modifiers.append(modifier)
        logger.debug("prompt_filter triggered: %s for %s", f.get("id"), character_name)

    for blk in dropped:
        if blk in ctx:
            ctx[blk] = ""

    # effects_block = aggregated modifiers. Overrides whatever build_thought_context
    # populated for this key (typically empty now since rules are deactivated).
    effects = "\n".join(f"- {m}" for m in triggered_modifiers)
    if effects:
        ctx["effects_block"] = effects

    return ctx
