"""ConsumeItem Skill — Character verbraucht ein Item aus dem eigenen Inventar.

Nutzt die bestehende ``inventory.consume_item`` Pipeline:
  - removed 1 Stueck aus dem Inventar
  - wendet ``effects`` (stat changes, mood_influence) an
  - setzt ``apply_condition`` falls definiert (mit ``condition_duration_hours``)

Anwendung: Avatar oder NPC ueberreicht dem Character ein Item per Gift, der
Tool-LLM des Empfaengers entscheidet im Chat ob er es trinkt/isst/anwendet
und ruft dafuer diesen Skill mit ``item_id`` oder ``name`` auf.

Input (JSON oder Plaintext):
    {"item_id": "item_xxxxxxx"}
    {"name": "Mondtrank"}
    "Mondtrank"
"""
import json
from typing import Any, Dict

from .base import BaseSkill, ToolSpec

from app.core.log import get_logger
logger = get_logger("consume_item")


class ConsumeItemSkill(BaseSkill):
    """Character konsumiert ein Item aus seinem Inventar."""

    SKILL_ID = "consume_item"
    ALWAYS_LOAD = True

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        from app.core.prompt_templates import load_skill_meta
        meta = load_skill_meta("consume_item")
        self.name = meta["name"]
        self.description = meta["description"]
        self._defaults = {"enabled": False}

    def execute(self, raw_input: str) -> str:
        if not self.enabled:
            return "ConsumeItem Skill ist nicht verfuegbar."
        try:
            return self._execute_inner(raw_input)
        except Exception as e:
            logger.error("ConsumeItem-Fehler: %s", e)
            return f"Fehler beim Konsumieren: {e}"

    def _execute_inner(self, raw_input: str) -> str:
        ctx = self._parse_base_input(raw_input)
        input_text = (ctx.get("input", raw_input) or "").strip()
        character_name = (ctx.get("agent_name") or "").strip()
        if not character_name:
            return "Fehler: Character-Name fehlt."
        if not input_text:
            return "Fehler: Item-Name oder -ID fehlt."

        # JSON oder Plaintext
        token = ""
        if input_text.startswith("{"):
            try:
                parsed = json.loads(input_text)
                if isinstance(parsed, dict):
                    token = (parsed.get("item_id")
                             or parsed.get("name")
                             or parsed.get("item")
                             or "").strip()
            except Exception:
                pass
        if not token:
            token = input_text

        # Token zur Item-ID aufloesen (id, name, item_<name>)
        from app.models.inventory import resolve_item_id, get_item, has_item, consume_item
        item_id = resolve_item_id(token)
        if not item_id:
            return f"Item '{token}' nicht in der Bibliothek gefunden."

        item = get_item(item_id) or {}
        item_name = item.get("name") or item_id

        # Pruefen ob im eigenen Inventar
        if not has_item(character_name, item_id):
            return f"'{item_name}' ist nicht in deinem Inventar."

        # Verbrauchen
        result = consume_item(character_name, item_id)
        if not result.get("success"):
            return f"Konnte '{item_name}' nicht konsumieren."

        # Bestaetigung mit Effekt-Summary
        msg = f"'{item_name}' konsumiert."
        changes = result.get("changes") or {}
        if isinstance(changes, dict) and changes:
            chunks = []
            for stat, info in changes.items():
                if isinstance(info, dict):
                    delta = info.get("new", 0) - info.get("old", 0)
                    if delta:
                        sign = "+" if delta > 0 else ""
                        chunks.append(f"{stat} {sign}{delta}")
            if chunks:
                msg += " Effekt: " + ", ".join(chunks) + "."
        cond = result.get("condition_applied")
        if cond:
            msg += f" Zustand aktiv: {cond}."
        return msg
