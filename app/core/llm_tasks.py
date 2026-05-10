"""Kanonische Liste aller LLM-Task-Typen.

Das ersetzt perspektivisch die starre Aufteilung in llm_defaults.chat /
llm_defaults.tools / llm_defaults.image_prompt. Statt Rollen haben wir
eine flache Liste von Tasks, die im llm_routing-Admin-Tab per LLM/Order
auf konkrete Provider+Model gemappt werden.
"""
from typing import Dict

from app.core.llm_queue import Priority


# Task-Katalog: task_id -> {label, priority, gate?}
# `gate` = dot-Path auf ein bool-Feld in der Config. Wenn das Feld False ist,
# muss fuer diesen Task KEIN Routing-Eintrag existieren (Feature inaktiv).
# Task categories — guidance for which LLM kind a task expects.
# Shown as a label next to each task in the admin LLM-Routing UI.
#   "image"  → vision-capable model required (image input)
#   "tool"   → reliable tool-calling / structured-output needed
#   "chat"   → big chat / RP model (creative writing, streaming)
#   "helper" → small/cheap helper model is enough
TASK_TYPES: Dict[str, Dict[str, object]] = {
    # Streaming / RP
    "chat_stream":        {"label": "Chat (Stream)",            "priority": Priority.CHAT,   "category": "chat"},
    "story_stream":       {"label": "Story (Stream)",           "priority": Priority.HIGH,   "category": "chat",   "gate": "story_engine.enabled"},
    "group_chat_stream":  {"label": "Group-Chat (Stream)",      "priority": Priority.CHAT,   "category": "chat"},

    # Tool / Decision LLM
    "extraction":         {"label": "Memory Extraction",        "priority": Priority.NORMAL, "category": "helper"},
    "social_reaction":    {"label": "Social Reaction (Thought)","priority": Priority.LOW,    "category": "tool",   "gate": "social_reactions.enabled"},
    "random_event":       {"label": "Random Event",             "priority": Priority.LOW,    "category": "tool",   "gate": "random_events.enabled"},
    "secret_generation":  {"label": "Secret Generation",        "priority": Priority.LOW,    "category": "tool"},
    "outfit_generation":  {"label": "Outfit Generation",        "priority": Priority.NORMAL, "category": "tool",   "gate": "image_generation.enabled"},
    "send_message":       {"label": "Send Message",             "priority": Priority.NORMAL, "category": "chat",   "gate": "skills.send_message.enabled"},
    "talk_to":            {"label": "Talk-To (Char-to-Char)",   "priority": Priority.LOW,    "category": "chat",   "gate": "skills.talk_to.enabled"},
    "thought":            {"label": "Thought (Fallback)",       "priority": Priority.LOW,    "category": "chat"},
    "thought_greeting":   {"label": "Thought: Avatar-Begruessung", "priority": Priority.LOW, "category": "chat"},
    # "intent" bleibt als Fallback wenn ein spezifischer intent_*-Task nicht
    # geroutet ist (siehe llm_router.resolve_llm). Direkt nutzen sollte ihn
    # neuer Code nicht mehr — stattdessen einen der intent_*-Sub-Tasks.
    "intent":             {"label": "Intent (Fallback)",        "priority": Priority.NORMAL, "category": "tool"},
    "classify_activity":  {"label": "Classify Activity",            "priority": Priority.NORMAL, "category": "tool"},
    "spell_detect":       {"label": "Spell Cast Detection",      "priority": Priority.NORMAL, "category": "tool"},
    # `world_dev_validate` removed: validator model is now picked
    # dynamically in the World Dev UI right next to the chat model — no
    # separate task entry to maintain in /admin/settings → LLM Routing.

    # Summaries
    "memory_consolidation":  {"label": "Memory Consolidation",     "priority": Priority.LOW, "category": "helper"},
    "consolidation":         {"label": "Consolidation (3-Tier)",   "priority": Priority.LOW, "category": "helper"},
    "relationship_summary":  {"label": "Relationship Summary",     "priority": Priority.LOW, "category": "helper", "gate": "relationships.summary_enabled"},

    # Image / Prompt
    "image_prompt":       {"label": "Image Prompt Enhancer",    "priority": Priority.NORMAL, "category": "helper", "gate": "image_generation.enabled"},
    "image_comment":      {"label": "Image Comment",            "priority": Priority.NORMAL, "category": "helper", "gate": "image_generation.enabled"},
    "instagram_caption":  {"label": "Instagram Caption",        "priority": Priority.NORMAL, "category": "image",  "gate": "skills.instagram.enabled"},

    # Vision
    "image_recognition":  {"label": "Image Recognition",        "priority": Priority.NORMAL, "category": "image",  "gate": "image_generation.enabled"},
    "image_analysis":     {"label": "Image Analysis",           "priority": Priority.NORMAL, "category": "image",  "gate": "image_generation.enabled"},

    # Sonstiges
    "translation":        {"label": "Translation",              "priority": Priority.NORMAL, "category": "helper"},
    "expression_map":     {"label": "Expression Map",           "priority": Priority.LOW,    "category": "tool",   "gate": "image_generation.enabled"},
}


# Human-readable label per category (used in the admin UI).
CATEGORY_LABELS: Dict[str, str] = {
    "image":  "Image Input",
    "tool":   "Tools Required",
    "chat":   "Large Chat Model",
    "helper": "Small Helper Model",
}


def _get_by_path(obj: dict, path: str):
    parts = path.split(".")
    cur: object = obj
    for p in parts:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def is_task_gated_off(task: str, cfg: dict) -> bool:
    """True wenn der Task aufgrund eines deaktivierten Feature-Gates nicht benoetigt wird."""
    entry = TASK_TYPES.get(task)
    if not entry:
        return False
    gate = entry.get("gate")
    if not gate:
        return False
    val = _get_by_path(cfg, str(gate))
    return val is False


def get_default_priority(task: str) -> int:
    entry = TASK_TYPES.get(task)
    if entry:
        return int(entry.get("priority", Priority.NORMAL))
    return int(Priority.NORMAL)