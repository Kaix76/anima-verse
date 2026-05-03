"""
Channel-Modell für verschiedene Eingabe-/Ausgabekanäle (Web, Telegram, WhatsApp, etc.)

Generische Schnittstelle für Multi-Channel Support in der Chat-Anwendung
"""
from enum import Enum
from typing import Optional, Dict, Any
from datetime import datetime
from abc import ABC, abstractmethod


class ChannelType(str, Enum):
    """Verfügbare Kommunikationskanäle"""
    WEB = "web"          # Web-Oberfläche (Standard)
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    DISCORD = "discord"
    SLACK = "slack"


class Message:
    """Einheitliche Nachrichtenstruktur über alle Kanäle.

    speaker: wer hat die Nachricht geschrieben.
        "user"         -> User-Input (Standard fuer role=user)
        "<CharName>"   -> ein anderer Character spricht (auch role=user aus Sicht des antwortenden Characters)
        Bei role=assistant: Name des antwortenden Characters.

    medium: Kommunikationskanal aus Sicht der Figuren.
        "in_person"    -> persoenlich am gleichen Ort
        "messaging"    -> Nachrichtenfenster (Fernkommunikation)
        "telegram"     -> Telegram-Chat
        "instagram"    -> Instagram-Kommentar / DM
    """

    def __init__(
        self,
        content: str,
        role: str,  # "user" oder "assistant"
        channel: ChannelType,
        timestamp: Optional[str] = None,
        channel_message_id: Optional[str] = None,  # ID der Nachricht im externen Kanal
        metadata: Optional[Dict[str, Any]] = None,
        speaker: str = "user",
        medium: str = "in_person",
        id: Optional[int] = None):  # noqa: A002 - DB row id
        self.content = content
        self.role = role
        self.channel = channel
        self.timestamp = timestamp or datetime.now().isoformat()
        self.channel_message_id = channel_message_id
        self.metadata = metadata or {}
        self.speaker = speaker
        self.medium = medium
        # DB-Rowid (chat_messages.id). Wird beim Lesen aus der DB gesetzt;
        # bei frisch erzeugten Messages noch None bis save_message commited.
        self.id = id

    def to_dict(self) -> Dict[str, Any]:
        """Konvertiere zu Dictionary für JSON-Speicherung"""
        return {
            "id": self.id,
            "content": self.content,
            "role": self.role,
            "channel": self.channel.value,
            "timestamp": self.timestamp,
            "channel_message_id": self.channel_message_id,
            "metadata": self.metadata,
            "speaker": self.speaker,
            "medium": self.medium,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Message":
        """Erstelle Message aus Dictionary.

        Alte Messages ohne speaker/medium bekommen Defaults
        ('user' / 'in_person') — keine Backfill-Migration.
        """
        return Message(
            content=data["content"],
            role=data["role"],
            channel=ChannelType(data.get("channel", "web")),
            timestamp=data.get("timestamp"),
            channel_message_id=data.get("channel_message_id"),
            metadata=data.get("metadata", {}),
            speaker=data.get("speaker", "user"),
            medium=data.get("medium", "in_person"),
            id=data.get("id"))


class ChannelInterface(ABC):
    """
    Abstrakte Basisklasse für alle Kommunikationskanäle
    
    Jeder Kanal (Telegram, WhatsApp, etc.) muss diese Schnittstelle implementieren
    """
    
    def __init__(self, channel_type: ChannelType):
        self.channel_type = channel_type
    
    @abstractmethod
    async def send_message(
        self, character_name: str,
        content: str,
        **kwargs
    ) -> Optional[str]:
        """
        Sende eine Nachricht über diesen Kanal
        
        Args:
            user_id: Eindeutige Benutzer-ID
            character_name: Name des Agenten
            content: Nachrichteninhalt
            **kwargs: Kanal-spezifische Parameter
        
        Returns:
            Kanal-spezifische Message-ID falls verfügbar, sonst None
        """
        pass
    
    @abstractmethod
    async def receive_message(self) -> Optional[Dict[str, Any]]:
        """
        Empfange eine Nachricht über diesen Kanal
        
        Returns:
            Dictionary mit user_id, content, und channel_message_id
            None wenn keine Nachricht verfügbar
        """
        pass
    
    @abstractmethod
    async def is_available(self) -> bool:
        """Prüfe ob der Kanal verfügbar/verbunden ist"""
        pass
    
    async def on_message_received(
        self, content: str,
        channel_message_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Hook wenn eine Nachricht empfangen wird
        Kann überschrieben werden für kanalspezifische Logik
        """
        return {
            "user_id": "",
            "content": content,
            "channel_message_id": channel_message_id
        }
    
    async def on_message_sent(
        self, character_name: str,
        content: str,
        channel_message_id: Optional[str] = None
    ) -> None:
        """
        Hook wenn eine Nachricht gesendet wird
        Kann überschrieben werden für kanalspezifische Logik (z.B. Read-Status)
        """
        pass


class WebChannel(ChannelInterface):
    """
    Standard Web-Kanal (bestehende Web-Oberfläche)
    
    Dies ist die Standard-Implementierung für die bestehende Web-UI
    """
    
    def __init__(self):
        super().__init__(ChannelType.WEB)
    
    async def send_message(
        self, character_name: str,
        content: str,
        **kwargs
    ) -> Optional[str]:
        # Web-Kanal wird über SSE/Streaming gesendet
        # Die MessageID wird von der Frontend generiert
        return kwargs.get("message_id")
    
    async def receive_message(self) -> Optional[Dict[str, Any]]:
        # Web-Kanal empfängt über HTTP POST
        return None  # Wird durch HTTP-Request gehandhabt
    
    async def is_available(self) -> bool:
        return True  # Web ist immer verfügbar
