"""PluginSkill - Basisklasse fuer Plugin-basierte Skills.

Erbt von BaseSkill fuer volle Kompatibilitaet mit dem SkillManager,
erhaelt aber zusaetzlich einen PluginContext fuer Service-Zugriff.
"""
from typing import Any, Dict

from app.skills.base import BaseSkill
from app.plugins.context import PluginContext


class PluginSkill(BaseSkill):
    """Basisklasse fuer Skills die als Plugin geladen werden.

    Unterschied zu BaseSkill:
    - Erhaelt einen PluginContext mit Service-Zugriff (HTTP, Logging, Env)
    - Plugins sollen NICHT direkt aus app.* importieren
    - Konfiguration erfolgt ueber plugin.yaml + Umgebungsvariablen
    """

    def __init__(self, config: Dict[str, Any], ctx: PluginContext):
        super().__init__(config)
        self.ctx = ctx
