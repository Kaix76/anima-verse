"""Backward-Compat Wrapper — leitet an plugins/knowledge/extract_utils.py weiter.

Der Scheduler und andere interne Module importieren hieraus.
Die eigentliche Logik liegt jetzt im Knowledge-Plugin.
"""
from plugins.knowledge.extract_utils import (  # noqa: F401
    extract_knowledge_from_files,
    _detect_related_character,
    _make_file_key)
