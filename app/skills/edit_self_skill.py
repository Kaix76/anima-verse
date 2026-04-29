"""EditSelf Skill — Character editiert eigene Soul-Dateien (Self-Evolution).

Erlaubt einem Character, gezielt einen Abschnitt seiner weichen Identitaet
zu aendern: Ueberzeugungen (beliefs), gelernte Lektionen (lessons), Ziele (goals).

NICHT erlaubt: personality, task, roleplay_rules — das sind harte Identitaets-
Anker, die nur User/Admin aendern darf.

Input-Format (JSON empfohlen):
  {
    "section": "beliefs|lessons|goals",
    "operation": "append|replace_section",
    "heading": "Ueber mich",       # Section-Ueberschrift unter ##
    "content": "Ich vertraue ...",  # neuer Text
    "reason": "Kai war heute besonders ehrlich zu mir"
  }
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List

from .base import BaseSkill, ToolSpec

from app.core.log import get_logger
logger = get_logger("edit_self")


# File-Default: editable (Character darf via EditSelf reinschreiben)
EDITABLE_SECTIONS = {"beliefs", "lessons", "goals"}
# File-Default: locked (User/Admin only). Einzelne Sections koennen via
# <!-- EDITABLE --> Marker direkt nach der ## Heading freigegeben werden.
LOCKED_SECTIONS = {"personality", "tasks", "roleplay_rules", "soul"}

SECTION_FILE_MAP = {
    "beliefs":        "soul/beliefs.md",
    "lessons":        "soul/lessons.md",
    "goals":          "soul/goals.md",
    "personality":    "soul/personality.md",
    "tasks":          "soul/tasks.md",
    "roleplay_rules": "soul/roleplay_rules.md",
    "soul":           "soul/soul.md",
}

ALL_SECTIONS = EDITABLE_SECTIONS | LOCKED_SECTIONS

EDITABLE_MARKER = "<!-- EDITABLE -->"


class EditSelfSkill(BaseSkill):
    """Character editiert eigene Soul-Felder (beliefs/lessons/goals)."""

    SKILL_ID = "edit_self"
    ALWAYS_LOAD = True

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.name = os.environ.get("SKILL_EDIT_SELF_NAME", "EditSelf")
        self.description = os.environ.get(
            "SKILL_EDIT_SELF_DESCRIPTION",
            "Update YOUR OWN inner identity based on important experiences. "
            "Editable: beliefs, lessons, goals (always). "
            "Restricted: personality, tasks, roleplay_rules, soul — "
            "only sections explicitly opened by the user/admin can be edited there."
        )
        self._defaults = {"enabled": True, "max_chars_per_file": 4000}
        logger.info("EditSelf Skill initialized")

    def execute(self, raw_input: str) -> str:
        if not self.enabled:
            return "EditSelf Skill is disabled."

        ctx = self._parse_base_input(raw_input)
        character_name = ctx.get("agent_name", "").strip()
        if not character_name:
            return "Error: agent context missing."

        section = (ctx.get("section") or "").strip().lower()
        operation = (ctx.get("operation") or "append").strip().lower()
        heading = (ctx.get("heading") or "").strip()
        content = (ctx.get("content") or "").strip()
        reason = (ctx.get("reason") or "").strip()

        if not section or section not in ALL_SECTIONS:
            return (f"Error: section must be one of {sorted(ALL_SECTIONS)}. "
                    f"Got: '{section}'")
        if operation not in ("append", "replace_section"):
            return f"Error: operation must be append or replace_section. Got: '{operation}'"
        if not content:
            return "Error: empty content."
        if not heading:
            return "Error: heading required."

        from app.models.character import get_character_dir
        char_dir = get_character_dir(character_name)
        md_path = char_dir / SECTION_FILE_MAP[section]
        md_path.parent.mkdir(parents=True, exist_ok=True)

        old_text = md_path.read_text(encoding="utf-8") if md_path.exists() else ""

        # Lock-Pruefung pro Section
        # - Editable Files (beliefs/lessons/goals): immer erlaubt
        # - Locked Files (personality/tasks/roleplay_rules/soul):
        #     nur erlaubt wenn die Section <!-- EDITABLE --> direkt unter dem Heading hat
        if section in LOCKED_SECTIONS:
            if not _section_is_editable(old_text, heading):
                return (f"Error: section '{heading}' in {section}.md is locked "
                        f"(no <!-- EDITABLE --> marker). Only User/Admin can edit.")

        new_text = _apply_edit(old_text, section, heading, content, operation)

        # Self-Reinforcement-Schutz: nach jedem Save Duplikate abfangen.
        # Bei wiederholten Reflexionsläufen erzeugt der LLM oft semantisch
        # identische Bullets oder ganze ##-Sections — die werden hier kollabiert.
        deduped = dedupe_soul_text(new_text)
        if deduped != new_text:
            removed_chars = len(new_text) - len(deduped)
            logger.info("EditSelf-Dedup: %s/%s — %d Zeichen redundant entfernt",
                        character_name, section, removed_chars)
            new_text = deduped

        # Size-Gatekeeper (Phase 3 minimal: hartes Truncate mit Warnung statt LLM-Konsolidierung)
        cfg = self._get_effective_config(character_name)
        max_chars = int(cfg.get("max_chars_per_file", 4000))
        if len(new_text) > max_chars:
            logger.warning("EditSelf: %s/%s/%s ueberschritten %d Zeichen — gekuerzt",
                           character_name, section, heading, max_chars)
            new_text = new_text[:max_chars] + "\n\n<!-- truncated to {} chars -->\n".format(max_chars)

        md_path.write_text(new_text, encoding="utf-8")

        # Auch ins Profil spiegeln (waehrend Migration noch Backup) —
        # nur fuer Felder die ein Profil-Aequivalent haben
        try:
            field_key_map = {
                "beliefs":     "character_beliefs",
                "lessons":     "character_lessons",
                "goals":       "character_goals",
                "personality": "character_personality",
                "tasks":       "character_task",
                "soul":        "character_soul",
            }
            field_key = field_key_map.get(section)
            if field_key:
                from app.models.character import get_character_profile, save_character_profile
                profile = get_character_profile(character_name)
                # Strip Marker fuer Profil-Spiegelung (Spec: nur Inhalt im JSON)
                import re as _re
                clean = _re.sub(r"<!--\s*[A-Z]+\s*-->\s*\n?", "", new_text).strip()
                profile[field_key] = clean
                profile["character_evolution_updated"] = datetime.now().isoformat()
                save_character_profile(character_name, profile)
        except Exception as _pe:
            logger.debug("Profil-Spiegelung fehlgeschlagen: %s", _pe)

        # History-Eintrag
        _append_evolution_history(character_name, {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "section": section,
            "operation": operation,
            "heading": heading,
            "content": content[:200],
            "reason": reason[:200],
            "trigger": "self_edit",
        })

        logger.info("EditSelf: %s/%s/%s (%s, %d chars) — reason: %s",
                    character_name, section, heading, operation, len(content), reason[:80])
        return f"Updated {section}/{heading} ({operation}). New file size: {len(new_text)} chars."

    def get_usage_instructions(self, format_name: str = "", **kwargs) -> str:
        from app.core.tool_formats import format_example
        fmt = format_name or "tag"
        example = (
            '{"section":"beliefs","operation":"append","heading":"About Kai",'
            '"content":"Kai was honest about something difficult today.",'
            '"reason":"Built trust"}'
        )
        return format_example(fmt, self.name, example)

    def as_tool(self, **kwargs) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                f"{self.description} "
                f"Input JSON: {{section: 'beliefs|lessons|goals|personality|tasks|soul', "
                f"operation: 'append|replace_section', heading: '...', "
                f"content: '...', reason: '...'}}. "
                f"Use SPARINGLY — only when an experience truly shifts your perspective."
            ),
            func=self.execute)


# ---------------------------------------------------------------------------
# MD-Section-Manipulation
# ---------------------------------------------------------------------------

def _section_is_editable(text: str, heading: str) -> bool:
    """Prueft ob die Section mit gegebenem heading durch <!-- EDITABLE -->
    markiert ist (direkt nach der ## Heading-Zeile)."""
    if not text or not heading:
        return False
    lines = text.splitlines()
    h_lower = heading.lower()
    for i, line in enumerate(lines):
        if line.startswith("## ") and line[3:].strip().lower() == h_lower:
            # Naechste nicht-leere Zeile auf Marker pruefen
            for j in range(i + 1, min(i + 4, len(lines))):
                stripped = lines[j].strip()
                if not stripped:
                    continue
                return EDITABLE_MARKER in stripped
            return False
    return False


def _apply_edit(old_text: str, section: str, heading: str,
                content: str, operation: str) -> str:
    """Wendet die Edit-Operation auf den MD-Text an.

    Format-Annahme:
      # <Top-Heading (= section title)>
      ## <heading 1>
      bullet/text
      ## <heading 2>
      ...

    Bei Bedarf wird Top-Heading und/oder Sub-Heading neu angelegt.
    """
    title_map = {"beliefs": "Ueberzeugungen", "lessons": "Lektionen", "goals": "Ziele"}
    top_title = title_map.get(section, section.title())

    lines = old_text.splitlines() if old_text else []
    if not any(line.startswith("# ") for line in lines):
        # Top-Heading fehlt → davor setzen
        lines = [f"# {top_title}", ""] + lines

    sections = _parse_sections(lines)  # [(level, heading, [body lines]), ...]

    target_idx = None
    for i, (lvl, h, _) in enumerate(sections):
        if lvl == 2 and h.lower() == heading.lower():
            target_idx = i
            break

    if target_idx is None:
        # Heading existiert noch nicht → anhaengen
        sections.append((2, heading, _content_to_lines(content)))
    else:
        lvl, h, body = sections[target_idx]
        # EDITABLE-Marker erhalten falls vorhanden
        marker_lines = [ln for ln in body if EDITABLE_MARKER in ln]
        if operation == "replace_section":
            new_body = marker_lines + _content_to_lines(content)
            sections[target_idx] = (lvl, h, new_body)
        else:  # append
            new_body = list(body)
            new_body.extend(_content_to_lines(content))
            sections[target_idx] = (lvl, h, new_body)

    return _render_sections(sections) + "\n"


def _parse_sections(lines: List[str]) -> List[tuple]:
    """Zerlegt Zeilen in (level, heading, body_lines)-Tupel.

    Top-Heading (#) wird als section[0] mit level=1 erfasst, danach folgen ##.
    """
    result = []
    cur_lvl = 0
    cur_heading = ""
    cur_body: List[str] = []

    def flush():
        if cur_lvl > 0 or cur_body:
            result.append((cur_lvl, cur_heading, cur_body[:]))

    for line in lines:
        if line.startswith("# ") and not line.startswith("## "):
            flush()
            cur_lvl = 1
            cur_heading = line[2:].strip()
            cur_body.clear()
        elif line.startswith("## "):
            flush()
            cur_lvl = 2
            cur_heading = line[3:].strip()
            cur_body.clear()
        else:
            cur_body.append(line)
    flush()
    return result


def _render_sections(sections: List[tuple]) -> str:
    parts = []
    for lvl, heading, body in sections:
        if lvl == 1:
            parts.append(f"# {heading}")
        elif lvl == 2:
            parts.append(f"## {heading}")
        # Body: trailing leere Zeilen wegtrimmen
        body_clean = list(body)
        while body_clean and not body_clean[-1].strip():
            body_clean.pop()
        if body_clean:
            parts.extend(body_clean)
        parts.append("")  # leere Zeile als Section-Trenner
    return "\n".join(parts).rstrip()


def dedupe_soul_text(text: str) -> str:
    """Bereinigt Soul-MD von Duplikaten:
      - Identische ## Headings (case-insensitive) werden zusammengelegt
        (Body-Lines aus allen Vorkommen kombiniert).
      - Innerhalb jeder Section: identische Bullets/Zeilen entfernen
        (case-insensitive nach Whitespace-Normalisierung).
      - <!-- EDITABLE --> Marker bleiben erhalten.

    Wird nach jedem EditSelf-Save aufgerufen, um Self-Reinforcement-
    Duplicates abzufangen die der LLM beim wiederholten Reflektieren
    erzeugt.
    """
    if not text or not text.strip():
        return text
    lines = text.splitlines()
    sections = _parse_sections(lines)

    # Gruppieren nach Heading (case-insensitive). Erste Reihenfolge erhalten.
    order: List[tuple] = []
    grouped: Dict[str, Dict[str, Any]] = {}
    for lvl, h, body in sections:
        key = (lvl, h.strip().lower())
        if key not in grouped:
            grouped[key] = {"lvl": lvl, "heading": h, "bodies": []}
            order.append(key)
        grouped[key]["bodies"].append(body)

    new_sections: List[tuple] = []
    for key in order:
        g = grouped[key]
        merged: List[str] = []
        seen_norm = set()
        marker_added = False
        for body in g["bodies"]:
            for ln in body:
                stripped = ln.strip()
                if EDITABLE_MARKER in stripped:
                    if not marker_added:
                        merged.append(ln)
                        marker_added = True
                    continue
                if not stripped:
                    # Eine Leerzeile als Trenner reicht
                    if merged and merged[-1].strip():
                        merged.append(ln)
                    continue
                norm = re.sub(r"\s+", " ", stripped.lower())
                if norm in seen_norm:
                    continue
                seen_norm.add(norm)
                merged.append(ln)
        new_sections.append((g["lvl"], g["heading"], merged))

    return _render_sections(new_sections) + "\n"


def _content_to_lines(content: str) -> List[str]:
    """Normalisiert content zu MD-Bullet-Liste wenn es einfacher Text ist."""
    content = content.strip()
    if not content:
        return [""]
    # wenn schon mit - oder * beginnt: belassen
    if content.startswith(("-", "*")) or "\n-" in content or "\n*" in content:
        return content.splitlines()
    # einzelner Satz / Absatz → als Bullet
    return [f"- {line}" if line.strip() else "" for line in content.splitlines()]


def _append_evolution_history(character_name: str, entry: Dict[str, Any]):
    """Append entry to character_evolution_history.json (capped)."""
    try:
        from app.models.character import get_character_dir
        path = get_character_dir(character_name) / "character_evolution_history.json"
        history = []
        if path.exists():
            try:
                history = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                history = []
        history.append(entry)
        if len(history) > 100:
            history = history[-100:]
        path.write_text(json.dumps(history, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    except Exception as e:
        logger.debug("evolution_history append failed: %s", e)
