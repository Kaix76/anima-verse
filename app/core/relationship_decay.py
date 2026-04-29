"""Relationship Decay — Periodischer Background-Job der inaktive Beziehungen
abschwaechen und Typen reklassifizieren laesst.

Laeuft in der BackgroundQueue, getriggert via ThoughtLoop (alle 6h).

Logik:
  - Beziehungen ohne Interaktion seit >7 Tagen verlieren Staerke (-1 pro Woche)
  - Nach Staerke-Update wird der Typ neu klassifiziert
  - romantic_tension zerfaellt ebenfalls leicht bei Inaktivitaet (-0.02/Woche)
"""
import os
from datetime import datetime
from typing import Any, Dict

from app.core.log import get_logger

logger = get_logger("relationship_decay")

DECAY_STRENGTH_PER_WEEK = float(os.environ.get("RELATIONSHIP_DECAY_STRENGTH", "1"))
DECAY_ROMANTIC_PER_WEEK = float(os.environ.get("RELATIONSHIP_DECAY_ROMANTIC", "0.02"))


def handle_relationship_decay(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Background-Queue Handler: Wendet Decay auf alle Beziehungen eines Users an."""
    user_id = payload.get("user_id", "")
    if not user_id:
        return {"error": "user_id missing"}

    from app.models.relationship import (
        load_relationships,
        save_relationships,
        classify_type)

    rels = load_relationships()
    if not rels:
        return {"skipped": True, "reason": "no relationships"}

    now = datetime.now()
    strength_affected = 0
    romantic_affected = 0

    for rel in rels:
        try:
            last = datetime.fromisoformat(rel.get("last_interaction", ""))
            days_since = (now - last).total_seconds() / 86400
        except (ValueError, TypeError):
            days_since = 30

        if days_since < 7:
            continue

        # How many weeks since last interaction
        weeks = days_since / 7.0

        # Strength decay
        old_strength = rel.get("strength", 0)
        # Apply proportional decay: decay_per_week * weeks_since_check (max 1 week per run)
        decay = min(DECAY_STRENGTH_PER_WEEK * 2, DECAY_STRENGTH_PER_WEEK * min(weeks, 2))
        new_strength = max(0, old_strength - decay)
        if new_strength != old_strength:
            rel["strength"] = round(new_strength, 1)
            strength_affected += 1

        # Romantic tension decay (slower)
        old_romantic = rel.get("romantic_tension", 0)
        if old_romantic > 0:
            rom_decay = min(DECAY_ROMANTIC_PER_WEEK * 2, DECAY_ROMANTIC_PER_WEEK * min(weeks, 2))
            new_romantic = max(0, old_romantic - rom_decay)
            if new_romantic != old_romantic:
                rel["romantic_tension"] = round(new_romantic, 3)
                romantic_affected += 1

        # Reclassify type after decay
        from app.models.relationship import are_romantically_compatible
        compatible = are_romantically_compatible(rel.get("character_a", ""), rel.get("character_b", ""))
        rel["type"] = classify_type(
            rel.get("strength", 0),
            rel.get("sentiment_a_to_b", 0),
            rel.get("sentiment_b_to_a", 0),
            rel.get("romantic_tension", 0),
            romantic_compatible=compatible)

    if strength_affected or romantic_affected:
        save_relationships(rels)
        logger.info(
            "Decay fuer User %s: %d strength, %d romantic updates", strength_affected, romantic_affected)

    return {
        "success": True,
        "strength_affected": strength_affected,
        "romantic_affected": romantic_affected,
    }


def register_relationship_decay_handler():
    """Registriert den Handler in der BackgroundQueue."""
    from app.core.background_queue import get_background_queue
    bq = get_background_queue()
    bq.register_handler("relationship_decay", handle_relationship_decay)
    logger.info("Relationship Decay Handler registriert")
