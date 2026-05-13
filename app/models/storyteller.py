"""Storyteller-Konfiguration pro Welt.

Konfiguriert das Verhalten des Storyteller-Pipelines (act-Skill): welche
Skills dürfen feuern, in welchem Chat-Modus läuft der Agent (rp_first /
single / no_tools), welcher LLM-Task wird gerouted.

Storage: ``storage/<world>/storyteller.json`` — single JSON-Objekt. Wird
beim ersten Lesen mit Defaults befüllt.

Whitelist-Skills sind absichtlich klein gehalten — Storyteller ist ein
GM-Werkzeug, kein vollwertiger Chat-Partner. Defaults aktivieren nur die
"world action"-Skills (Outfit, Image, Activity, ConsumeItem), während
Kommunikations- und Bewegungs-Skills aus sind (Avatar steuert seine
Bewegung über UI, Dialog läuft über reguläre Chat-Sessions).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from app.core.log import get_logger
from app.core.paths import get_storage_dir

logger = get_logger("storyteller")


# Komplette Liste der Skills, die der Storyteller-Pipeline angeboten
# werden _können_. Andere Skills sind im Storyteller-Modus generell aus
# (z.B. retrospect, instagram_*, markdown_writer).
_SKILL_KEYS = [
    "outfit_change", "image_generation", "setactivity", "consume_item",
    "setlocation", "talk_to", "send_message", "act",
    "instagram", "markdown_writer", "retrospect", "describe_room",
    "notify_user", "video_generation", "instagram_comment", "instagram_reply",
    "outfit_creation",
]

_DEFAULTS: Dict[str, Any] = {
    "chat_mode": "rp_first",
    "llm_task": "storyteller",
    "enabled_skills": {
        "outfit_change": True,
        "image_generation": True,
        "setactivity": True,
        "consume_item": True,
        "setlocation": False,
        "talk_to": False,
        "send_message": False,
        "act": False,
        "instagram": False,
        "markdown_writer": False,
        "retrospect": False,
        "describe_room": False,
        "notify_user": False,
        "video_generation": False,
        "instagram_comment": False,
        "instagram_reply": False,
        "outfit_creation": False,
    },
}

_VALID_MODES = {"rp_first", "single", "no_tools"}


def _path() -> Path:
    sd = get_storage_dir()
    sd.mkdir(parents=True, exist_ok=True)
    return sd / "storyteller.json"


def _normalize(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Merge mit Defaults + Validierung."""
    out: Dict[str, Any] = {
        "chat_mode": _DEFAULTS["chat_mode"],
        "llm_task": _DEFAULTS["llm_task"],
        "enabled_skills": dict(_DEFAULTS["enabled_skills"]),
    }
    if not isinstance(cfg, dict):
        return out

    mode = str(cfg.get("chat_mode") or "").strip().lower()
    if mode in _VALID_MODES:
        out["chat_mode"] = mode

    task = str(cfg.get("llm_task") or "").strip()
    if task:
        out["llm_task"] = task

    skills = cfg.get("enabled_skills") or {}
    if isinstance(skills, dict):
        for k in _SKILL_KEYS:
            if k in skills:
                out["enabled_skills"][k] = bool(skills[k])
    return out


def get_storyteller_config() -> Dict[str, Any]:
    """Liest die Storyteller-Config der aktuellen Welt. Fehlende Felder
    werden mit Defaults gefüllt, damit Aufrufer nicht null-checken
    müssen."""
    p = _path()
    if not p.exists():
        return _normalize({})
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return _normalize(data if isinstance(data, dict) else {})
    except Exception as e:
        logger.warning("storyteller load failed: %s", e)
        return _normalize({})


def save_storyteller_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Persistiert die Storyteller-Config der aktuellen Welt. Werte werden
    auf bekannte Felder beschränkt und auf gültige Modi/Skills normiert."""
    cleaned = _normalize(cfg or {})
    p = _path()
    p.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return cleaned


def list_skill_keys() -> list:
    """Stabile Reihenfolge aller bekannten Skill-Keys — für die UI."""
    return list(_SKILL_KEYS)
