"""KnowledgeExtract Plugin-Skill — Batch-Extraktion von Wissen aus Dateien.

Liest Dateien aus konfigurierten Verzeichnissen und extrahiert Fakten per LLM.
Unterstuetzt Batch-Modus: Mehrere Dateien pro LLM-Call, gruppiert nach Tag.
Konfigurierbare batch_size, max_input_tokens und max_output_tokens.
"""
from typing import Any, Dict

from app.plugins.base import PluginSkill
from app.plugins.context import PluginContext
from app.skills.base import ToolSpec


class KnowledgeExtractPlugin(PluginSkill):
    """Extrahiert Fakten aus Dateien in einem konfigurierten Verzeichnis (Batch-Modus)."""

    SKILL_ID = "knowledge_extract"
    ALWAYS_LOAD = True

    def __init__(self, config: Dict[str, Any], ctx: PluginContext):
        super().__init__(config, ctx)
        self.name = "KnowledgeExtract"
        self.description = (
            "Searches local system files for information about characters, events, and internal data. "
            "Use this tool when asked to look up, check, or find information in the system. "
            "Do NOT use WebSearch for this — WebSearch is only for internet queries. "
            "Input should be a short search topic, e.g. 'Kira' or 'latest events'."
        )

        self._defaults = {
            "folder_path": "",
            "file_pattern": "*.json",
            "include_subdirs": False,
            "exclude_dirs": "",
            "max_age_days": 0,
            "extraction_prompt": "",
            "batch_size": ctx.get_env_int("SKILL_KNOWLEDGE_BATCH_SIZE", 5),
            "max_input_tokens": ctx.get_env_int("SKILL_KNOWLEDGE_MAX_INPUT_TOKENS", 12000),
            "max_output_tokens": ctx.get_env_int("SKILL_KNOWLEDGE_MAX_OUTPUT_TOKENS", 1500),
        }

    def get_config_fields(self):
        fields = super().get_config_fields()
        if "folder_path" in fields:
            fields["folder_path"]["help"] = "Kommagetrennte Pfade zu Verzeichnissen, z.B. /data/logs, /data/notes"
        if "file_pattern" in fields:
            fields["file_pattern"]["help"] = "Glob-Pattern fuer Dateien, z.B. *.json, *.md, *.txt"
        if "include_subdirs" in fields:
            fields["include_subdirs"]["help"] = "Auch Unterverzeichnisse rekursiv durchsuchen"
        if "exclude_dirs" in fields:
            fields["exclude_dirs"]["help"] = "Kommagetrennte Verzeichnisnamen zum Ausschliessen, z.B. archive, backup"
        if "max_age_days" in fields:
            fields["max_age_days"]["help"] = "Nur Dateien beruecksichtigen, die nicht aelter als X Tage sind. 0 = kein Filter"
            fields["max_age_days"]["label"] = "Max. Alter (Tage)"
        if "extraction_prompt" in fields:
            fields["extraction_prompt"]["help"] = "Optionaler Fokus fuer die Extraktion — worauf soll geachtet werden?"
            fields["extraction_prompt"]["examples"] = [
                "Extrahiere alle Personen, Orte und wichtige Ereignisse",
                "Finde Vorlieben, Abneigungen und Persoenlichkeitsmerkmale",
                "Sammle alle Fakten ueber Beziehungen zwischen Personen",
                "Extrahiere technische Details und Konfigurationen",
                "Finde alle Termine, Daten und Zeitangaben",
            ]
        if "batch_size" in fields:
            fields["batch_size"]["help"] = "Anzahl Dateien pro LLM-Aufruf"
        if "max_input_tokens" in fields:
            fields["max_input_tokens"]["help"] = "Max. Token-Limit pro Batch-Eingabe"
        if "max_output_tokens" in fields:
            fields["max_output_tokens"]["help"] = "Max. Token-Limit fuer LLM-Antwort"
        return fields

    def execute(self, raw_input: str) -> str:
        if not self.enabled:
            return "KnowledgeExtract Skill ist nicht verfuegbar."

        from plugins.knowledge.extract_utils import extract_knowledge_from_files

        ctx = self._parse_base_input(raw_input)
        character_name = ctx.get("agent_name", "").strip()
        user_id = ctx.get("user_id", "").strip()

        if not character_name or not user_id:
            return "Fehler: character_name und user_id werden benoetigt."

        cfg = self._get_effective_config(character_name)
        folder_path = cfg.get("folder_path", "")
        if not folder_path:
            return "Fehler: Kein folder_path konfiguriert. Bitte in den Skill-Einstellungen setzen."

        self.ctx.logger.info("Batch-Extraktion gestartet")
        self.ctx.logger.info("Ordner: %s, Pattern: %s, Batch: %s",
                             folder_path, cfg.get("file_pattern"), cfg.get("batch_size"))

        try:
            result = extract_knowledge_from_files(
                character_name=character_name,
                folder_path=folder_path,
                file_pattern=cfg.get("file_pattern", "*.json"),
                include_subdirs=cfg.get("include_subdirs", False),
                exclude_dirs=cfg.get("exclude_dirs", ""),
                extraction_prompt=cfg.get("extraction_prompt", ""),
                batch_size=int(cfg.get("batch_size", 5)),
                max_input_tokens=int(cfg.get("max_input_tokens", 12000)),
                max_output_tokens=int(cfg.get("max_output_tokens", 1500)),
                max_age_days=int(cfg.get("max_age_days", 0)))

            if not result.get("success"):
                return f"Fehler: {result.get('error', 'Unbekannter Fehler')}"

            parts = [f"{result.get('files_found', 0)} Dateien durchsucht"]
            parts.append(f"{result.get('extracted', 0)} Fakten extrahiert")
            batches = result.get("batches", 0)
            if batches:
                parts.append(f"in {batches} Batches")
            cached = result.get("cached", 0)
            if cached:
                parts.append(f"{cached} unveraendert (Cache)")
            cleaned = result.get("cleaned_stale", 0)
            if cleaned:
                parts.append(f"{cleaned} veraltete Eintraege bereinigt")
            return f"Wissensextraktion abgeschlossen: {', '.join(parts)}."

        except Exception as e:
            self.ctx.logger.error("Fehler bei der Wissensextraktion: %s", e)
            return f"Fehler bei der Wissensextraktion: {e}"

    def as_tool(self, **kwargs) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            func=self.execute)
