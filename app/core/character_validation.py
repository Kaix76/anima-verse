"""Character Validation - Startup checks for all characters.

Runs at server startup and validates character configurations.
Reports problems as notifications to the user.

Note: LLM-Overrides pro Character werden jetzt ueber den Router verwaltet
(Task-basiert), deshalb entfaellt die Pruefung von llm_chat/llm_tools/etc.
"""
from typing import Dict, List, Tuple

from app.core.log import get_logger

logger = get_logger("character_validation")


def validate_all_characters():
    """Validates all characters. Called at startup."""
    issues = validate_user_characters()
    if issues:
        logger.warning("Character-Validation: %d Problem(e) gefunden", len(issues))
    else:
        logger.info("Character-Validation: Alle Characters OK")


def validate_user_characters() -> List[Tuple[str, str]]:
    """Validates all characters of a user. Returns list of (character, issue)."""
    from app.models.character import list_available_characters, get_character_config

    characters = list_available_characters()
    all_issues: List[Tuple[str, str]] = []

    for char_name in characters:
        config = get_character_config(char_name)
        issues = _validate_character(char_name, config)
        all_issues.extend(issues)

    if all_issues:
        _notify_issues(all_issues)

    return all_issues


def _validate_character(char_name: str, config: Dict
) -> List[Tuple[str, str]]:
    """Runs all validation checks on a single character.

    Aktuell keine Checks — Platzhalter fuer zukuenftige Pruefungen
    (z.B. Template-Existenz, Profilbild, ...).
    """
    return []


def _notify_issues(issues: List[Tuple[str, str]]):
    """Creates a notification summarizing validation issues."""
    from app.models.notifications import create_notification

    by_char: Dict[str, List[str]] = {}
    for char_name, issue in issues:
        by_char.setdefault(char_name, []).append(issue)

    for char_name, char_issues in by_char.items():
        lines = [f"Config-Problem bei {char_name}:"]
        for issue in char_issues:
            lines.append(f"- {issue}")
        lines.append("Bitte in den Character-Einstellungen korrigieren.")

        create_notification(
            character=char_name,
            content="\n".join(lines),
            notification_type="warning")
        logger.info("Notification erstellt fuer %s: %d Problem(e)", char_name, len(char_issues))
