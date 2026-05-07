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
            "SELECT id, condition, label, drop_blocks, prompt_modifier, "
            "enabled, meta, icon, image_modifier "
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
                "icon": r[7] or "",
                "image_modifier": r[8] or "",
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


def migrate_status_modifiers_once() -> int:
    """One-time migration: status_modifiers.json -> prompt_filters table.

    Falls die Welt eine ``status_modifiers.json`` mit Eintraegen hat und
    die ``prompt_filters``-Tabelle den jeweiligen Eintrag noch nicht
    enthaelt, wird er angelegt. Auto-generierte ``id`` aus condition.
    Idempotent — wenn die id schon in prompt_filters existiert, wird
    nichts ueberschrieben (User-Aenderungen bleiben erhalten).

    Returns: Anzahl der migrierten Eintraege.
    """
    try:
        from app.core.paths import get_storage_dir
        from app.core.db import transaction
        path = get_storage_dir() / "status_modifiers.json"
        if not path.exists():
            return 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("status_modifiers.json parse failed: %s", e)
            return 0
        modifiers = data.get("modifiers") or []
        if not modifiers:
            return 0

        # Bestehende ids aus prompt_filters laden — nicht ueberschreiben
        existing_ids = {(e.get("id") or "").strip() for e in _load_world()}
        migrated = 0
        with transaction() as conn:
            for mod in modifiers:
                cond = (mod.get("condition") or "").strip()
                if not cond:
                    continue
                # Auto-id: condition:drunk -> drunk, stamina<30 -> stamina_low
                fid = _id_from_condition(cond, mod.get("label", ""))
                if not fid or fid in existing_ids:
                    continue
                conn.execute("""
                    INSERT OR IGNORE INTO prompt_filters
                        (id, condition, label, drop_blocks, prompt_modifier,
                         enabled, meta, icon, image_modifier)
                    VALUES (?, ?, ?, '[]', ?, 1, '{}', ?, ?)
                """, (
                    fid, cond, (mod.get("label") or "").strip(),
                    (mod.get("prompt_modifier") or "").strip(),
                    (mod.get("icon") or "").strip(),
                    (mod.get("image_modifier") or "").strip(),
                ))
                existing_ids.add(fid)
                migrated += 1

        if migrated:
            logger.info("Migrated %d status_modifiers entries to prompt_filters", migrated)
            # Source-Datei umbenennen damit die Migration nicht erneut laeuft
            try:
                path.rename(path.with_suffix(".json.migrated"))
            except Exception:
                pass
        return migrated
    except Exception as e:
        logger.warning("migrate_status_modifiers_once failed: %s", e)
        return 0


def _id_from_condition(condition: str, label: str = "") -> str:
    """Auto-generate a stable id from a condition expression."""
    import re as _re
    cond = (condition or "").strip().lower()
    if cond.startswith("condition:"):
        return _re.sub(r"[^a-z0-9_]", "_", cond[10:]).strip("_")
    if "<" in cond or ">" in cond or "=" in cond:
        # stat<30 -> stamina_low (bei <), stamina_high (bei >), stamina_eq_30 (bei =)
        m = _re.match(r"^([a-z_]+)\s*([<>=])\s*(\d+)", cond)
        if m:
            stat, op, _ = m.groups()
            suffix = {"<": "low", ">": "high", "=": "eq"}.get(op, "x")
            return f"{stat}_{suffix}"
    if label:
        return _re.sub(r"[^a-z0-9_]", "_", label.lower()).strip("_") or "filter"
    return _re.sub(r"[^a-z0-9_]", "_", cond)[:32].strip("_") or "filter"


def get_filter_for_condition(condition_name: str) -> Optional[Dict[str, Any]]:
    """Return the merged filter entry whose id (or legacy condition expression)
    matches ``condition_name``.

    Used by UI badge + image generation to look up icon/label/image_modifier
    for an active condition. Match is case-insensitive.

    Reihenfolge:
        1. Filter-id (neues Modell — id IS der Condition-Name)
        2. Legacy ``condition: condition:<name>``-Expression (Bestandsdaten)
    Returns None when nothing matches.
    """
    if not condition_name:
        return None
    target_id = condition_name.strip().lower()
    target_expr = f"condition:{target_id}"
    legacy_match: Optional[Dict[str, Any]] = None
    for f in load_filters():
        fid = (f.get("id") or "").strip().lower()
        if fid == target_id:
            return f
        cond = (f.get("condition") or "").strip().lower()
        if cond == target_expr and legacy_match is None:
            legacy_match = f
    return legacy_match


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


def _has_tag_condition(character_name: str, tag: str) -> bool:
    """True if ``tag`` is in the character's profile.active_conditions list.

    Filter-id triggers implicitly when the same name is set as a tag
    (e.g. via apply_condition). So a filter ``id=drunk`` doesn't need
    ``condition: condition:drunk`` anymore — the id makes the tag entry
    point automatic.
    """
    if not tag or not character_name:
        return False
    try:
        from app.models.character import get_character_profile
        profile = get_character_profile(character_name) or {}
        active = profile.get("active_conditions", []) or []
        tag_l = tag.strip().lower()
        return any((c.get("name") or "").strip().lower() == tag_l for c in active)
    except Exception:
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

    # Resolve active avatar once per call so prompt_modifier templates can
    # reference {avatar}. Empty string when no avatar context (background
    # jobs, periodic ticks): substitution falls back to "the avatar".
    avatar_name = ""
    try:
        from app.models.account import get_active_character
        avatar_name = (get_active_character() or "").strip()
    except Exception:
        pass
    avatar_subst = avatar_name or "the avatar"

    for f in filters:
        if not f.get("enabled", True):
            continue
        fid = (f.get("id") or "").strip()
        condition = (f.get("condition") or "").strip()
        # Trigger via Filter-id als Tag-Condition (implizit) ODER ueber
        # explizite condition-Expression (stamina<10, mood:happy, …).
        # Filter ohne Tag UND ohne Expression bleiben inaktiv.
        triggered = (fid and _has_tag_condition(character_name, fid)) \
            or (bool(condition) and _evaluate(condition, character_name, location_id))
        if not triggered:
            continue
        for blk in (f.get("drop_blocks") or []):
            if isinstance(blk, str) and blk:
                dropped.add(blk)
        modifier = (f.get("prompt_modifier") or "").strip()
        if modifier:
            modifier = modifier.replace("{avatar}", avatar_subst)
            triggered_modifiers.append(modifier)
        logger.debug("prompt_filter triggered: %s for %s", fid, character_name)

    for blk in dropped:
        if blk in ctx:
            ctx[blk] = ""

    # effects_block = aggregated modifiers. Overrides whatever build_thought_context
    # populated for this key (typically empty now since rules are deactivated).
    effects = "\n".join(f"- {m}" for m in triggered_modifiers)
    if effects:
        ctx["effects_block"] = effects

    return ctx
