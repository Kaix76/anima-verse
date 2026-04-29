"""Plugin System - Dynamisches Laden von Skills aus dem plugins/ Verzeichnis"""

from .loader import load_all_plugins, discover_plugins
from .context import PluginContext
from .base import PluginSkill

__all__ = ['load_all_plugins', 'discover_plugins', 'PluginContext', 'PluginSkill']
