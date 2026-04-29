"""PluginContext - Service-API fuer Plugins.

Plugins erhalten einen PluginContext statt direkter Imports aus app.*.
So bleibt die Kopplung minimal und die Plugin-API stabil.
"""
import os
import logging
import requests as http_lib
from typing import Optional

from app.core.log import get_logger


class PluginContext:
    """Stellt Services bereit, die Plugins nutzen koennen."""

    def __init__(self, plugin_id: str):
        self.plugin_id = plugin_id
        self.logger: logging.Logger = get_logger(f"plugin.{plugin_id}")
        self.http = http_lib

    def get_env(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Liest eine Umgebungsvariable."""
        return os.environ.get(key, default)

    def get_env_int(self, key: str, default: int = 0) -> int:
        """Liest eine Umgebungsvariable als int."""
        return int(os.environ.get(key, str(default)))

    def get_env_bool(self, key: str, default: bool = False) -> bool:
        """Liest eine Umgebungsvariable als bool."""
        return os.environ.get(key, str(default)).lower() in ('true', '1', 'yes')
