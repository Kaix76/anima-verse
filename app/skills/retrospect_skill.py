"""Retrospect Skill — character self-reflection.

Loads recent daily/weekly summaries plus episodic memories, asks the LLM to
extract new beliefs, lessons and goals from recent experience, and appends
them under the right ``##`` subsection of the character's editable soul
files (``soul/beliefs.md``, ``soul/lessons.md``, ``soul/goals.md``).

The same files are also user-editable via the Soul-Editor UI — Retrospect
just appends to the existing structure rather than maintaining a parallel
set of output files.

Tool exposure: takes no arguments — the agent just decides "I want to
reflect now". The thought-context layer hints at this option when enough
new material has accumulated since the last retrospect.
"""
import json
import re
from datetime import datetime
from typing import Any, Dict, List

from .base import BaseSkill, ToolSpec
from app.core.log import get_logger
from app.core.soul_writer import (
    SOUL_FILE_SCHEMA, append_entry, list_categories, load_all_body_lines)

logger = get_logger("retrospect")


def get_last_retrospect_at(character_name: str) -> str:
    """Return ISO timestamp of the most recent retrospect, or '' if never."""
    try:
        from app.core.db import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT value FROM world_kv WHERE key=?",
            (f"retrospect.last_at:{character_name}",),
        ).fetchone()
        return (row[0] or "") if row else ""
    except Exception:
        return ""


def _mark_retrospect_done(character_name: str) -> None:
    try:
        from app.core.db import transaction
        with transaction() as conn:
            conn.execute(
                "INSERT INTO world_kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"retrospect.last_at:{character_name}",
                 datetime.now().isoformat()))
    except Exception as e:
        logger.debug("mark_retrospect_done failed for %s: %s", character_name, e)


class RetrospectSkill(BaseSkill):
    """Lets a character reflect on recent experience and update their
    beliefs / lessons / goals."""

    SKILL_ID = "retrospect"
    ALWAYS_LOAD = True
    # ALWAYS_LOAD=True heisst: Skill wird in den Manager geladen, aber
    # versteckt fuer Characters die keinen `<char>/skills/retrospect.json`
    # mit `{"enabled": true}` haben.

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        from app.core.prompt_templates import load_skill_meta
        meta = load_skill_meta("retrospect")
        self.name = meta["name"]
        self.description = meta["description"]
        self._defaults = {"enabled": True}
        logger.info("Retrospect Skill initialized")

    def execute(self, raw_input: str) -> str:
        if not self.enabled:
            return "Retrospect is disabled."

        ctx = self._parse_base_input(raw_input)
        character_name = (ctx.get("agent_name") or "").strip()
        if not character_name:
            return "Error: agent_name missing."

        # Per-Character Master-Switch: UI-Toggle "Retrospektive: Nein"
        # ueberschreibt das Skill-Default.
        from app.models.character_template import is_feature_enabled
        if not is_feature_enabled(character_name, "retrospect_enabled"):
            return "Retrospect disabled for this character."

        from app.models.character import (
            get_character_profile, get_character_language, LANGUAGE_MAP)
        profile = get_character_profile(character_name)
        personality = (profile.get("character_personality", "") or "").strip()
        lang_code = (get_character_language(character_name) or "en").strip()
        language_name = LANGUAGE_MAP.get(lang_code, "English")

        recent_summaries = self._gather_recent_summaries(character_name)
        recent_memories = self._gather_recent_memories(character_name)

        if not recent_summaries and not recent_memories:
            return "Nothing to reflect on yet — no recent material."

        existing_beliefs = "\n".join(load_all_body_lines(character_name, "beliefs", limit=15))
        existing_lessons = "\n".join(load_all_body_lines(character_name, "lessons", limit=15))
        existing_goals = "\n".join(load_all_body_lines(character_name, "goals", limit=15))

        try:
            from app.core.llm_router import llm_call
            from app.core.prompt_templates import render_task

            sys_prompt, user_prompt = render_task(
                "retrospect",
                character_name=character_name,
                personality=personality or "(not specified)",
                language_name=language_name,
                recent_summaries=recent_summaries or "(none)",
                recent_memories=recent_memories or "(none)",
                existing_beliefs=existing_beliefs,
                existing_lessons=existing_lessons,
                existing_goals=existing_goals)

            response = llm_call(
                task="consolidation",
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
                agent_name=character_name)
            raw = (response.content or "").strip()
        except Exception as e:
            logger.error("Retrospect LLM call failed for %s: %s", character_name, e)
            return f"Reflection failed: {e}"

        data = self._parse_json(raw)
        if data is None:
            logger.warning("Retrospect JSON parse failed for %s: %s",
                           character_name, raw[:200])
            return "Reflection produced no usable output."

        counts = {
            "beliefs": self._append_entries(character_name, "beliefs",
                                            data.get("beliefs") or [], lang_code),
            "lessons": self._append_entries(character_name, "lessons",
                                            data.get("lessons") or [], lang_code),
            "goals":   self._append_entries(character_name, "goals",
                                            data.get("goals") or [], lang_code),
        }

        _mark_retrospect_done(character_name)

        added = [(k, v) for k, v in counts.items() if v > 0]
        if not added:
            return "Reflected — nothing new worth recording this time."
        bits = [f"{n} new {k}" for k, n in added]
        return "Reflected: " + ", ".join(bits) + "."

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _gather_recent_summaries(self, character_name: str) -> str:
        try:
            from app.utils.history_manager import load_daily_summaries_combined
            daily = load_daily_summaries_combined(character_name) or {}
        except Exception:
            return ""
        if not daily:
            return ""
        items = sorted(daily.items())[-5:]
        return "\n".join(f"- {day}: {text}" for day, text in items if text)

    def _gather_recent_memories(self, character_name: str) -> str:
        try:
            from app.models.memory import load_memories
            mems = load_memories(character_name) or []
        except Exception:
            return ""
        sig = [m for m in mems
               if (m.get("importance") or 0) >= 3
               or m.get("memory_type") == "commitment"]
        sig.sort(key=lambda m: m.get("timestamp", ""), reverse=True)
        out: List[str] = []
        for m in sig[:12]:
            content = (m.get("content") or "").strip()
            if not content:
                continue
            out.append(f"- {content}")
        return "\n".join(out)

    def _parse_json(self, raw: str):
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = [ln for ln in cleaned.split("\n") if not ln.strip().startswith("```")]
            cleaned = "\n".join(lines)
        m = re.search(r"\{[\s\S]+\}", cleaned)
        try:
            return json.loads(m.group(0) if m else cleaned)
        except Exception:
            return None

    def _append_entries(self, character_name: str, file_id: str,
                        entries: List[Any], lang_code: str) -> int:
        valid_categories = set(list_categories(file_id))
        existing_lower = [s.lower() for s in load_all_body_lines(
            character_name, file_id, limit=50)]
        added = 0
        for e in entries:
            if not isinstance(e, dict):
                continue
            text = (e.get("text") or "").strip()
            category = (e.get("category") or "").strip()
            if not text or category not in valid_categories:
                continue
            tl = text.lower()
            if any(tl in line for line in existing_lower):
                continue
            try:
                append_entry(character_name, file_id, category, text, language=lang_code)
                existing_lower.append(tl)
                added += 1
            except Exception as ex:
                logger.warning("append_entry failed [%s/%s]: %s",
                               file_id, category, ex)
        return added

    def get_usage_instructions(self, format_name: str = "", **kwargs) -> str:
        from app.core.tool_formats import format_example
        fmt = format_name or "tag"
        return format_example(fmt, self.name, "")

    def as_tool(self, **kwargs) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            func=self.execute)
