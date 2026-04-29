"""Base Skill Class für alle Skills"""
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict

from app.core.log import get_logger
from app.core.tool_formats import format_example

logger = get_logger("skill_base")

from app.models.character import get_character_skill_config, save_character_skill_config


@dataclass
class ToolSpec:
    """Einfacher Tool-Descriptor (Ersatz fuer LangChain Tool)."""
    name: str
    description: str
    func: Callable


class BaseSkill(ABC):
    """
    Basis-Klasse für alle Skills.

    Jeder Skill definiert:
    - SKILL_ID: Identifier für per-Agent Config-Dateien (z.B. "image_generation")
    - name / description: Fest definiert in der Subklasse
    - _defaults: Dict mit Default-Werten aus .env

    Per-Agent Overrides werden zur Laufzeit aus
    storage/users/{user}/agents/{agent}/skills/{SKILL_ID}.json geladen.
    """

    SKILL_ID = ""  # Subklasse muss definieren
    ALWAYS_LOAD = False  # True = Skill wird immer geladen, Aktivierung nur per Character
    DEFERRED = False  # True = Tool-Intent wird erkannt aber erst nach Chat-Antwort ausgefuehrt
    CONTENT_TOOL = False  # True = Ergebnis muss ins RP einfliessen (Retry im rp_first Modus)

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.enabled = config.get('enabled', True)
        self.name = self.__class__.__name__
        self.description = ""
        self._defaults: Dict[str, Any] = {}

    @abstractmethod
    def execute(self, *args, **kwargs) -> str:
        pass

    def _parse_base_input(self, raw_input: str) -> Dict[str, Any]:
        """Extrahiert den standardisierten Kontext aus dem Tool-Input (JSON mit character_name)."""
        data: Dict[str, Any] = {"input": raw_input, "agent_name": "", "user_id": ""}

        if isinstance(raw_input, str) and raw_input.strip().startswith("{"):
            try:
                parsed = json.loads(raw_input)
                if isinstance(parsed, dict):
                    data.update(parsed)
            except Exception:
                pass

        return data

    def _get_effective_config(self, character_name: str) -> Dict[str, Any]:
        """Merged .env-Defaults mit per-Agent Overrides. Typen werden automatisch anhand der Defaults gecastet."""
        result = dict(self._defaults)

        if not character_name or not self.SKILL_ID:
            return result

        agent_config = get_character_skill_config(character_name, self.SKILL_ID)
        if agent_config:
            for key, default_val in result.items():
                if key in agent_config:
                    val = agent_config[key]
                    # bool VOR int pruefen (bool ist Subklasse von int)
                    if isinstance(default_val, bool):
                        result[key] = bool(val)
                    elif isinstance(default_val, float):
                        result[key] = float(val)
                    elif isinstance(default_val, int):
                        result[key] = int(val)
                    elif isinstance(default_val, list):
                        result[key] = val if isinstance(val, list) else []
                    else:
                        result[key] = str(val).strip()
        else:
            # Erstelle per-Agent Config mit Defaults beim ersten Aufruf
            save_character_skill_config(character_name, self.SKILL_ID, dict(self._defaults))
            logger.info(f"[{self.name}] Per-Agent Config erstellt für {character_name}: {self.SKILL_ID}.json")

        return result

    def get_config_fields(self) -> Dict[str, Dict[str, Any]]:
        """Gibt die konfigurierbaren Felder mit Typ-Info und Defaults zurueck.

        Returns:
            Dict[field_name, {"type": "str"|"bool"|"int"|"float", "default": value, "label": str}]
        """
        fields = {}
        for key, default_val in self._defaults.items():
            if key == "enabled":
                continue  # enabled wird separat per Checkbox gesteuert
            if isinstance(default_val, bool):
                field_type = "bool"
            elif isinstance(default_val, float):
                field_type = "float"
            elif isinstance(default_val, int):
                field_type = "int"
            else:
                field_type = "str"
            fields[key] = {
                "type": field_type,
                "default": default_val,
                "label": key.replace("_", " ").title(),
            }
        return fields

    def get_usage_instructions(self, format_name: str = "", character_name: str = "") -> str:
        if 'usage_instructions' in self.config:
            return self.config['usage_instructions']
        fmt = format_name or "tag"
        return format_example(fmt, self.name, "[input]")

    def memorize_result(self, result: str, character_name: str) -> bool:
        """Speichert das Execute-Ergebnis als Memory (optional).

        Default: Nichts speichern. Skills ueberschreiben dies bei Bedarf,
        damit Scheduler-Ergebnisse als Erinnerung erhalten bleiben.

        Returns:
            True wenn eine Memory gespeichert wurde, False sonst.
        """
        return False

    def as_tool(self, character_name: str = "") -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            func=self.execute)

    @classmethod
    def from_env(cls, env_prefix: str) -> 'BaseSkill':
        """Erstellt einen Skill aus Umgebungsvariablen."""
        config = {}
        for key, value in os.environ.items():
            if key.startswith(env_prefix):
                config_key = key[len(env_prefix):].lower()
                if value.lower() in ('true', 'false'):
                    value = value.lower() == 'true'
                config[config_key] = value
        return cls(config)
