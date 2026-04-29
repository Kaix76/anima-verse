"""PluginLoader - Entdeckt und laedt Plugins aus dem plugins/ Verzeichnis.

Scan-Ablauf:
1. Durchsucht PLUGIN_DIR nach Unterordnern mit plugin.yaml
2. Liest Metadaten aus plugin.yaml
3. Importiert die Skill-Klasse(n) dynamisch
4. Erstellt PluginContext und instanziiert die Skills

Unterstuetzt Single-Skill und Multi-Skill Plugins:
  Single:  skill_id + module (Default: skill.py)
  Multi:   skills: [{skill_id, module}, ...]
"""
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

import yaml

from app.core.log import get_logger
from app.plugins.context import PluginContext
from app.plugins.base import PluginSkill

logger = get_logger("plugin_loader")

# Standard-Verzeichnis: <project_root>/plugins/
PLUGIN_DIR = Path(__file__).resolve().parent.parent.parent / "plugins"


def _load_yaml(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _import_skill_class(plugin_dir: Path, module_file: str) -> Optional[Type[PluginSkill]]:
    """Importiert die Skill-Klasse aus dem Plugin-Modul."""
    module_path = plugin_dir / module_file
    if not module_path.exists():
        logger.error("Modul nicht gefunden: %s", module_path)
        return None

    module_name = f"plugins.{plugin_dir.name}.{module_path.stem}"

    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        logger.error("Kann Modul-Spec nicht erstellen: %s", module_path)
        return None

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    # Finde die PluginSkill-Subklasse im Modul
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (isinstance(attr, type)
                and issubclass(attr, PluginSkill)
                and attr is not PluginSkill):
            return attr

    logger.error("Keine PluginSkill-Subklasse gefunden in %s", module_path)
    return None


def discover_plugins(plugin_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Entdeckt alle Plugins im Plugin-Verzeichnis.

    Returns:
        Liste von Plugin-Metadaten-Dicts mit keys:
        - id, name, version, description, dir, always_load, env_prefix
        - skill_id + module (Single-Skill) ODER skills (Multi-Skill)
    """
    base = plugin_dir or PLUGIN_DIR
    if not base.exists():
        logger.info("Plugin-Verzeichnis nicht gefunden: %s", base)
        return []

    plugins = []
    for entry in sorted(base.iterdir()):
        manifest = entry / "plugin.yaml"
        if not entry.is_dir() or not manifest.exists():
            continue

        try:
            meta = _load_yaml(manifest)
        except Exception as e:
            logger.error("Fehler beim Lesen von %s: %s", manifest, e)
            continue

        has_single = bool(meta.get("skill_id"))
        has_multi = bool(meta.get("skills"))
        if not meta.get("name") or not (has_single or has_multi):
            logger.warning("Plugin %s: name und skill_id/skills fehlt in plugin.yaml", entry.name)
            continue

        meta["id"] = entry.name
        meta["dir"] = entry
        meta.setdefault("version", "0.1.0")
        meta.setdefault("description", "")

        # Single-Skill: skill_id + module
        if has_single and not has_multi:
            meta.setdefault("module", "skill.py")

        plugins.append(meta)

    return plugins


def load_plugin(meta: Dict[str, Any]) -> List[Tuple[str, PluginSkill]]:
    """Laedt ein Plugin und gibt Liste von (skill_id, skill_instance) zurueck.

    Unterstuetzt Single-Skill (skill_id + module) und Multi-Skill (skills: [...]).
    """
    plugin_id = meta["id"]
    plugin_dir = meta["dir"]

    # Enabled-Check auf Plugin-Ebene
    env_prefix = meta.get("env_prefix", f"SKILL_{plugin_id.upper()}_")
    enabled_key = f"{env_prefix}ENABLED"
    if not meta.get("always_load", False):
        if os.getenv(enabled_key, "false").lower() != "true":
            logger.debug("Plugin '%s' deaktiviert (env: %s)", plugin_id, enabled_key)
            return []

    ctx = PluginContext(plugin_id)

    # Multi-Skill Plugin: skills: [{skill_id, module}, ...]
    skills_list = meta.get("skills")
    if skills_list and isinstance(skills_list, list):
        results = []
        for skill_def in skills_list:
            sid = skill_def.get("skill_id")
            mod = skill_def.get("module", "skill.py")
            if not sid:
                logger.warning("Plugin '%s': skills-Eintrag ohne skill_id", plugin_id)
                continue
            skill_class = _import_skill_class(plugin_dir, mod)
            if skill_class is None:
                continue
            try:
                skill = skill_class({"enabled": True}, ctx)
                logger.info("Plugin geladen: %s/%s (skill_id=%s)", plugin_id, mod, sid)
                results.append((sid, skill))
            except Exception as e:
                logger.error("Fehler beim Instanziieren von '%s/%s': %s", plugin_id, mod, e)
        return results

    # Single-Skill Plugin: skill_id + module
    skill_id = meta.get("skill_id", plugin_id)
    module_file = meta.get("module", "skill.py")
    skill_class = _import_skill_class(plugin_dir, module_file)
    if skill_class is None:
        return []

    try:
        skill = skill_class({"enabled": True}, ctx)
        logger.info("Plugin geladen: %s (skill_id=%s)", plugin_id, skill_id)
        return [(skill_id, skill)]
    except Exception as e:
        logger.error("Fehler beim Instanziieren von Plugin '%s': %s", plugin_id, e)
        return []


def load_all_plugins(plugin_dir: Optional[Path] = None) -> Dict[str, PluginSkill]:
    """Entdeckt und laedt alle Plugins.

    Returns:
        Dict[skill_id, skill_instance]
    """
    results = {}
    for meta in discover_plugins(plugin_dir):
        for skill_id, skill in load_plugin(meta):
            results[skill_id] = skill
    return results
