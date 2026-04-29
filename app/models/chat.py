"""Chat-History Verwaltung - User-spezifisch

DEPRECATED: Diese Funktionen sind für Backward-Kompatibilität.
Nutze stattdessen app.models.unified_chat.UnifiedChatManager
"""
from pathlib import Path
from typing import Dict, List
from datetime import datetime

from app.models.character import get_character_dir
from app.models.unified_chat import UnifiedChatManager
from app.models.channel import Message


def get_chat_dir(character_name: str) -> Path:
    """Gibt das Chat-Verzeichnis für einen User und Agent zurück"""
    chat_dir = get_character_dir(character_name) / "chats"
    chat_dir.mkdir(parents=True, exist_ok=True)
    return chat_dir


def get_chat_history(character_name: str = "", partner_name: str = "") -> List[Dict[str, str]]:
    """Lädt die Chat-History. partner_name: expliziter Partner-Character (fuer C2C)."""
    if not character_name:
        return []
    messages = UnifiedChatManager.get_chat_history(character_name, partner_name=partner_name)
    return [msg.to_dict() for msg in messages]


def save_message(message: Dict[str, str], character_name: str = "", partner_name: str = ""):
    """Speichert eine Nachricht. partner_name: expliziter Partner-Character (fuer C2C)."""
    if not character_name:
        return
    msg_obj = Message.from_dict(message.copy())
    UnifiedChatManager.save_message(msg_obj, character_name, partner_name=partner_name)
