"""Centralized path module — single source of truth for all storage paths.

Every file that needs a storage path imports from here instead of
defining its own ``STORAGE_DIR = Path("./storage")``.

Initialization order (in server.py):
    1. paths.init(storage_dir)          # from CLI / env
    2. config.load(paths.get_config_path())  # config.json lives inside storage
"""

import os
from pathlib import Path
from typing import Optional, Union

_storage_dir: Optional[Path] = None
_project_root: Path = Path(__file__).resolve().parent.parent.parent


def init(storage_dir: Optional[Union[str, Path]] = None) -> Path:
    """Set the storage root.  Called once at server startup.

    Resolution order:
        1. Explicit *storage_dir* argument  (from CLI ``--storage`` / ``--world``)
        2. ``STORAGE_DIR`` environment variable
        3. ``./storage`` (default, backward-compatible)
    """
    global _storage_dir

    if storage_dir:
        _storage_dir = Path(storage_dir).resolve()
    else:
        _storage_dir = Path(os.environ.get("STORAGE_DIR", "./worlds/demo")).resolve()

    _storage_dir.mkdir(parents=True, exist_ok=True)
    return _storage_dir


def get_storage_dir() -> Path:
    """Return the base storage directory.  Auto-initializes on first call."""
    if _storage_dir is None:
        init()
    return _storage_dir


def get_account_path() -> Path:
    """Return the path to the account profile (login, settings)."""
    return get_storage_dir() / "account.json"


def get_shared_dir() -> Path:
    """Return the shared directory for cross-world files (templates, schemas)."""
    return _project_root / "shared"


def get_templates_dir() -> Path:
    """Character templates directory (shared across all worlds).

    Seit der Reorganisation gibt es Unterordner fuer Character/User/Expression/Pose —
    diese Funktion zeigt explizit auf `character/`, damit Listing kein Type-Filter
    mehr braucht. Expression/Pose-Presets haben eigene Helfer (get_expression_dir, etc.).
    """
    return get_shared_dir() / "templates" / "character"


def get_config_dir() -> Path:
    """Shared JSON-Config-Dateien (languages, etc.)."""
    return get_shared_dir() / "config"


def get_languages_dir() -> Path:
    """UI translation files (one JSON per language code)."""
    return get_shared_dir() / "languages"


def get_expression_presets_dir() -> Path:
    """Expression-Presets fuer Bild-Generierung."""
    return get_shared_dir() / "templates" / "expression"


def get_pose_presets_dir() -> Path:
    """Pose-Presets fuer Bild-Generierung."""
    return get_shared_dir() / "templates" / "pose"


def get_schemas_dir() -> Path:
    """World-dev schemas are shared across all worlds."""
    return get_shared_dir() / "world_dev_schemas"


def get_config_path() -> Path:
    return get_storage_dir() / "config.json"


def get_secrets_path() -> Path:
    """Sensitive overlay (api keys, passwords) — gitignored, merged on top of config.json at load time."""
    return get_storage_dir() / "secrets.json"
