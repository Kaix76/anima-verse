"""Retrospect Skill — character self-reflection.

Loads recent daily/weekly summaries plus episodic memories, asks the LLM to
extract new beliefs and improvement intentions, and appends them to two
plain-text MD files under the character's ``soul/`` directory:

    worlds/<world>/characters/<char>/soul/retrospect_beliefs.md
    worlds/<world>/characters/<char>/soul/retrospect_improvements.md

Separated from the manually-curated ``soul/beliefs.md`` (seed file written
by the author) so Retrospect-output can grow without clobbering hand-tuned
content. The agent_thought prompt loads recent entries from BOTH the seed
and retrospect files as context, so beliefs and improvements influence
future decisions without touching the character_personality field.

Tool exposure: takes no arguments — the agent just decides "I want to
reflect now". The thought-context layer hints at this option when enough
new material has accumulated since the last retrospect.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from .base import BaseSkill, ToolSpec
from app.core.log import get_logger

logger = get_logger("retrospect")


def get_beliefs_path(character_name: str) -> Path:
    """Retrospect-output beliefs file. Lebt im soul/ Ordner getrennt von
    der manuell gepflegten ``beliefs.md`` (= seed). So kann Retrospect
    appenden ohne Hand-Inhalt zu ueberschreiben."""
    from app.models.character import get_character_dir
    return get_character_dir(character_name) / "soul" / "retrospect_beliefs.md"


def get_improvements_path(character_name: str) -> Path:
    from app.models.character import get_character_dir
    return get_character_dir(character_name) / "soul" / "retrospect_improvements.md"


def get_seed_beliefs_path(character_name: str) -> Path:
    """Manuell kuratierte Seed-Beliefs ('# Ueberzeugungen'-Datei). Wird
    von ``_build_retrospective_block`` zusaetzlich zur Retrospect-Output-
    Datei gelesen, damit beide Quellen den Prompt fuettern."""
    from app.models.character import get_character_dir
    return get_character_dir(character_name) / "soul" / "beliefs.md"


def get_seed_improvements_path(character_name: str) -> Path:
    from app.models.character import get_character_dir
    return get_character_dir(character_name) / "soul" / "improvements.md"


def load_recent_lines(path: Path, limit: int = 10) -> List[str]:
    """Read the last ``limit`` content lines from a beliefs/improvements file."""
    if not path.exists():
        return []
    try:
        lines = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            lines.append(stripped)
        return lines[-limit:]
    except Exception as e:
        logger.debug("load_recent_lines(%s) failed: %s", path, e)
        return []


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
    beliefs / improvement intentions."""

    SKILL_ID = "retrospect"
    ALWAYS_LOAD = True
    # ALWAYS_LOAD=True heisst: Skill wird in den Manager geladen, aber
    # versteckt fuer Characters die keinen `<char>/skills/retrospect.json`
    # mit `{"enabled": true}` haben. Die Migration in
    # migrations/2026-05-retrospect-enable.py legt diese Config beim ersten
    # Server-Start (oder per Hand) fuer alle bestehenden Chars an.

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.name = os.environ.get("SKILL_RETROSPECT_NAME", "Retrospect")
        self.description = os.environ.get(
            "SKILL_RETROSPECT_DESCRIPTION",
            "Reflect on your recent experience and notice what shifted in how "
            "you see the world or yourself. Use this when something happened "
            "that changes your view, or when you haven't reflected in a while. "
            "No input required."
        )
        self._defaults = {"enabled": True}
        logger.info("Retrospect Skill initialized")

    def execute(self, raw_input: str) -> str:
        if not self.enabled:
            return "Retrospect is disabled."

        ctx = self._parse_base_input(raw_input)
        character_name = (ctx.get("agent_name") or "").strip()
        if not character_name:
            return "Error: agent_name missing."

        from app.models.character import (
            get_character_profile, get_character_language, LANGUAGE_MAP)
        profile = get_character_profile(character_name)
        personality = (profile.get("character_personality", "") or "").strip()
        # Char-Sprache: bestimmt in welcher Sprache beliefs/improvements
        # geschrieben werden. Fallback "English" wenn Code nicht gemappt.
        lang_code = (get_character_language(character_name) or "en").strip()
        language_name = LANGUAGE_MAP.get(lang_code, "English")

        recent_summaries = self._gather_recent_summaries(character_name)
        recent_memories = self._gather_recent_memories(character_name)

        if not recent_summaries and not recent_memories:
            return "Nothing to reflect on yet — no recent material."

        existing_beliefs = "\n".join(load_recent_lines(get_beliefs_path(character_name), limit=10))
        existing_improvements = "\n".join(load_recent_lines(get_improvements_path(character_name), limit=10))

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
                existing_improvements=existing_improvements)

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

        new_beliefs = self._append_beliefs(character_name, data.get("beliefs") or [])
        new_improvements = self._append_improvements(character_name, data.get("improvements") or [])

        _mark_retrospect_done(character_name)

        if not new_beliefs and not new_improvements:
            return "Reflected — nothing new worth recording this time."

        bits = []
        if new_beliefs:
            bits.append(f"{new_beliefs} new belief(s)")
        if new_improvements:
            bits.append(f"{new_improvements} improvement(s)")
        return "Reflected: " + ", ".join(bits) + "."

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _gather_recent_summaries(self, character_name: str) -> str:
        """Most recent daily summaries (last 5 days)."""
        try:
            from app.utils.history_manager import load_daily_summaries
            daily = load_daily_summaries(character_name) or {}
        except Exception:
            return ""
        if not daily:
            return ""
        items = sorted(daily.items())[-5:]
        return "\n".join(f"- {day}: {text}" for day, text in items if text)

    def _gather_recent_memories(self, character_name: str) -> str:
        """Recent significant memories (importance >= 3, episodic+semantic)."""
        try:
            from app.models.memory import load_memories
            mems = load_memories(character_name) or []
        except Exception:
            return ""
        # Filter for relevance: importance >= 3 OR memory_type=='commitment'
        sig = [m for m in mems
               if (m.get("importance") or 0) >= 3
               or m.get("memory_type") == "commitment"]
        # Newest first, cap at 12
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
        # Strip markdown fences if any
        if cleaned.startswith("```"):
            lines = [ln for ln in cleaned.split("\n") if not ln.strip().startswith("```")]
            cleaned = "\n".join(lines)
        m = re.search(r"\{[\s\S]+\}", cleaned)
        try:
            return json.loads(m.group(0) if m else cleaned)
        except Exception:
            return None

    def _append_beliefs(self, character_name: str, beliefs: List[Any]) -> int:
        path = get_beliefs_path(character_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = set(load_recent_lines(path, limit=50))
        added = 0
        with path.open("a", encoding="utf-8") as f:
            for b in beliefs:
                if not isinstance(b, dict):
                    continue
                text = (b.get("text") or "").strip()
                if not text or any(text.lower() in line.lower() for line in existing):
                    continue
                target = (b.get("target") or "").strip()
                ts = datetime.now().strftime("%Y-%m-%d")
                target_str = f" [about {target}]" if target else ""
                f.write(f"- {ts}{target_str}: {text}\n")
                added += 1
        return added

    def _append_improvements(self, character_name: str, improvements: List[Any]) -> int:
        path = get_improvements_path(character_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = set(load_recent_lines(path, limit=50))
        added = 0
        with path.open("a", encoding="utf-8") as f:
            for it in improvements:
                if not isinstance(it, dict):
                    continue
                text = (it.get("text") or "").strip()
                if not text or any(text.lower() in line.lower() for line in existing):
                    continue
                ts = datetime.now().strftime("%Y-%m-%d")
                f.write(f"- {ts}: {text}\n")
                added += 1
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
