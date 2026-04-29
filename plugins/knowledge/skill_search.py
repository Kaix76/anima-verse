"""KnowledgeSearch Plugin-Skill — Sucht in extrahierten Memories.

Durchsucht die Memory-Datenbank nach relevanten Fakten ohne LLM-Calls.
Nutzt die Daten, die zuvor per KnowledgeExtract befuellt wurden.
Liefert die top 'max_return' Treffer per Keyword- und Importance-Scoring.

Fix: Liest aus memories.json (gleicher Store wie KnowledgeExtract schreibt).
"""
from typing import Any, Dict

from app.plugins.base import PluginSkill
from app.plugins.context import PluginContext
from app.skills.base import ToolSpec


class KnowledgeSearchPlugin(PluginSkill):
    """Sucht relevante Fakten in der Memory-Datenbank (keine LLM-Calls)."""

    SKILL_ID = "knowledge_search"
    ALWAYS_LOAD = True

    def __init__(self, config: Dict[str, Any], ctx: PluginContext):
        super().__init__(config, ctx)
        self.name = "KnowledgeSearch"
        self.description = (
            "Searches the knowledge database for information about characters, people, events, "
            "and internal data. Use this tool when asked to look up, check, or recall anything "
            "stored in the system. Fast — no re-extraction needed. "
            "Input should be a short search topic, e.g. 'Kira' or 'latest events'."
        )
        self._defaults = {
            "max_candidates": ctx.get_env_int("SKILL_KNOWLEDGE_SEARCH_MAX_CANDIDATES", 50),
            "max_return": ctx.get_env_int("SKILL_KNOWLEDGE_SEARCH_MAX_RETURN", 8),
            "source_tag": "file_extraction",
        }

    def execute(self, raw_input: str) -> str:
        if not self.enabled:
            return "KnowledgeSearch Skill ist nicht verfuegbar."

        try:
            from app.models.memory import get_memories_by_tag

            ctx = self._parse_base_input(raw_input)
            query = ctx.get("input", raw_input).strip()
            character_name = ctx.get("agent_name", "").strip()
            user_id = ctx.get("user_id", "").strip()

            if not character_name or not user_id:
                return "Fehler: character_name und user_id werden benoetigt."

            if not query:
                return "Fehler: Kein Suchbegriff angegeben."

            cfg = self._get_effective_config(character_name)
            max_candidates = int(cfg.get("max_candidates", 50))
            max_return = int(cfg.get("max_return", 8))
            source_tag = cfg.get("source_tag", "file_extraction")

            self.ctx.logger.info("Suche: '%s' fuer %s", query, character_name)

            # Eintraege aus memories.json laden (gleicher Store wie Extract)
            all_entries = get_memories_by_tag(character_name, tag=source_tag)

            if not all_entries:
                return "Keine Wissenseintraege vorhanden. Bitte zuerst KnowledgeExtract ausfuehren."

            # Relevanz-Scoring per Keyword-Match + Importance
            query_words = {w.lower() for w in query.split() if len(w) > 2}
            scored = []
            for entry in all_entries:
                content = entry.get("content", "")
                content_lower = content.lower()
                hits = sum(1 for w in query_words if w in content_lower)
                if hits:
                    importance = entry.get("importance", 1)
                    scored.append((hits * importance, content))

            scored.sort(key=lambda x: x[0], reverse=True)
            relevant = [text for _, text in scored[:max_candidates]]

            self.ctx.logger.info("%d Eintraege durchsucht, %d relevant, %d zurueckgegeben",
                                 len(all_entries), len(relevant), min(len(relevant), max_return))

            if not relevant:
                return f"Keine Informationen zu '{query}' gefunden."

            top = relevant[:max_return]
            facts_text = "\n".join(f"- {f}" for f in top)
            return f"Gefundene Informationen zu '{query}' ({len(top)} Treffer):\n{facts_text}"

        except Exception as e:
            self.ctx.logger.error("Fehler in KnowledgeSearch: %s", e)
            return f"Fehler bei der Wissenssuche: {e}"

    def memorize_result(self, result: str, character_name: str) -> bool:
        """Speichert Suchergebnisse als Memory (fuer Scheduler-Aufrufe)."""
        if not result or "Fehler" in result or "Keine" in result[:20]:
            return False
        try:
            from app.models.memory import add_memory
            content = result[:1500]
            add_memory(
                character_name=character_name,
                content=content,
                memory_type="semantic",
                importance=3,
                tags=["scheduler_tool", "knowledge_search"],
                context="scheduler:KnowledgeSearch")
            return True
        except Exception as e:
            self.ctx.logger.warning("memorize_result fehlgeschlagen: %s", e)
            return False

    def as_tool(self, **kwargs) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            func=self.execute)
