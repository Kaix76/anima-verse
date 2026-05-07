"""
Unified Chat Manager - Multi-Channel Chat Verwaltung

Verwaltet Chat-Verlauf über mehrere Kanäle (Web, Telegram, WhatsApp, etc.)
Alle Nachrichten werden in einem einheitlichen Format gespeichert.

Storage: world.db — Tabelle chat_messages
"""
from pathlib import Path
from typing import Dict, List, Optional
from datetime import datetime
import json

from app.core.log import get_logger
from app.core.db import get_connection, transaction

logger = get_logger("unified_chat")

# get_user_name absichtlich nicht importiert — Login-Name wuerde sonst als
# Partner-Key in chat_messages leaken. Stattdessen wird get_player_identity
# in _resolve_partner_key benutzt (lazy import dort).
from app.models.channel import Message, ChannelType, ChannelInterface


class ChannelManager:
    """
    Verwaltet registrierte Kanäle und Kommunikation zwischen Kanälen
    """
    
    def __init__(self):
        self._channels: Dict[ChannelType, ChannelInterface] = {}
    
    def register_channel(self, channel: ChannelInterface) -> None:
        """Registriere einen neuen Kanal"""
        self._channels[channel.channel_type] = channel
    
    def unregister_channel(self, channel_type: ChannelType) -> None:
        """Deregistriere einen Kanal"""
        if channel_type in self._channels:
            del self._channels[channel_type]
    
    def get_channel(self, channel_type: ChannelType) -> Optional[ChannelInterface]:
        """Hole einen registrierten Kanal"""
        return self._channels.get(channel_type)
    
    def list_channels(self) -> List[ChannelType]:
        """Liste alle verfügbaren Kanäle auf"""
        return list(self._channels.keys())


class UnifiedChatManager:
    """
    Verwaltet Chat-Verlauf über mehrere Kanäle
    
    Alle Nachrichten werden im gleichen Format gespeichert, unabhängig vom Kanal.
    Dies ermöglicht:
    - Konsistente Chat-Historie über alle Kanäle
    - Kanalübergreifenden Nachrichtenverlauf
    - Explizites Routing an bestimmte Kanäle
    """
    
    def __init__(self, channel_manager: ChannelManager):
        self.channel_manager = channel_manager
    
    @staticmethod
    def get_chat_dir(character_name: str) -> Path:
        """Legacy-Kompatibilitaet: Gibt das Chat-Verzeichnis zurueck (existiert noch fuer Backups)."""
        from app.core.paths import get_storage_dir
        chat_dir = get_storage_dir() / "characters" / character_name / "chats"
        chat_dir.mkdir(parents=True, exist_ok=True)
        return chat_dir

    @staticmethod
    def _resolve_partner_key(partner_name: str = "", character_name: str = "") -> str:
        """Bestimmt den Partner-Key (fuer Logging und Legacy-Compat).

        Neues System: Character-Name des Partners.
        Fallback: aktiver Character des Users, dann Username.
        """
        if partner_name:
            return partner_name
        try:
            from app.models.account import get_player_identity
            active = get_player_identity("")
            if active and active != character_name:
                return active
        except Exception:
            pass
        # Login-Name (z.B. "admin") taugt nicht als Partner-Key — wuerde
        # neue Chats unter dem Login speichern statt unter dem Avatar.
        # Leerer String bedeutet hier: kein Partner-Filter.
        return ""

    @staticmethod
    def get_chat_history(character_name: str = "",
        channel: Optional[ChannelType] = None,
        limit: Optional[int] = None,
        partner_name: str = "") -> List[Message]:
        """Laedt Chat-History aus der DB.

        Args:
            character_name: Character-Name (der Character dessen History geladen wird)
            channel: Wenn gesetzt, nur Nachrichten von diesem Kanal filtern
            limit: Maximale Anzahl von Nachrichten (letzte N)
            partner_name: Expliziter Partner-Character-Name (fuer Character-zu-Character)

        Returns:
            Liste von Message-Objekten
        """
        if not character_name:
            return []

        partner = UnifiedChatManager._resolve_partner_key(partner_name, character_name)

        try:
            conn = get_connection()
            if partner:
                # Konversation A↔B kann je nach damaligem Avatar als
                # (A, partner=B) ODER (B, partner=A) gespeichert sein —
                # je nachdem wer der "antwortende" Character war. Beim
                # Avatar-Wechsel wuerde sonst die alte Richtung versteckt
                # bleiben. Wir vereinen beide Richtungen und tauschen die
                # Rollen im flipped-Batch, damit aus Sicht des aktuellen
                # character_name role=assistant immer DIESEN Character
                # bedeutet.
                base_q = (
                    "SELECT id, ts, role, content, channel, "
                    "channel_message_id, metadata FROM chat_messages "
                    "WHERE character_name=? AND partner=?")
                rows_direct = conn.execute(
                    base_q, (character_name, partner)).fetchall()
                rows_flipped = conn.execute(
                    base_q, (partner, character_name)).fetchall()

                merged = list(rows_direct)
                for r in rows_flipped:
                    # role swap: was als (B,A) gespeichert wurde, sieht aus
                    # A's Sicht andersrum aus.
                    new_role = "user" if r[2] == "assistant" else "assistant"
                    merged.append((r[0], r[1], new_role, r[3], r[4], r[5], r[6]))
                merged.sort(key=lambda x: (x[1] or "", x[0]))
                # Dedup: TalkTo-Konversationen (NPC↔NPC) werden in BEIDE
                # Buckets geschrieben, damit jeder Char eine vollstaendige
                # eigene History hat. Nach role-swap kollidiert das im
                # merged-View. Eindeutiger Schluessel: (ts, role, content).
                # Avatar↔NPC-Messages haben unique ts/content kombiniert,
                # werden also nicht entfernt.
                seen: set = set()
                deduped: List[tuple] = []
                for r in merged:
                    key = (r[1] or "", r[2] or "", r[3] or "")
                    if key in seen:
                        continue
                    seen.add(key)
                    deduped.append(r)
                rows = deduped
                # Frueher gab es einen Fallback auf den Account-Login-Namen
                # (get_user_name) als Partner — der hat aber nur bestehende
                # Admin-Chats sichtbar gemacht und neue Avatar-Chats blieben
                # leer wenn jemand kurzzeitig keinen Avatar gewaehlt hatte.
                # Entfernt: Login-Name ist kein gueltiger Partner-Key.
            else:
                rows = conn.execute("""
                    SELECT id, ts, role, content, channel, channel_message_id, metadata
                    FROM chat_messages
                    WHERE character_name=?
                    ORDER BY ts ASC
                """, (character_name,)).fetchall()

            history: List[Message] = []
            for r in rows:
                row_id, ts, role, content, ch, ch_msg_id, meta_json = r
                try:
                    meta = json.loads(meta_json or "{}")
                except Exception:
                    meta = {}
                ch_type = ChannelType.WEB
                if ch:
                    try:
                        ch_type = ChannelType(ch)
                    except (ValueError, KeyError):
                        pass
                msg = Message(
                    content=content,
                    role=role,
                    timestamp=ts,
                    channel=ch_type,
                    channel_message_id=ch_msg_id,
                    id=row_id,
                    **{k: v for k, v in meta.items()
                       if k not in ("content", "role", "timestamp", "channel",
                                    "channel_message_id", "id")},
                )
                if channel is None or msg.channel == channel:
                    history.append(msg)

            if limit and len(history) > limit:
                history = history[-limit:]
            return history

        except Exception as e:
            logger.error("get_chat_history DB-Fehler fuer %s/%s: %s",
                         character_name, partner_name, e)
            return []

    @staticmethod
    def save_message(message: Message,
        character_name: str = "",
        partner_name: str = "") -> None:
        """Speichert eine Nachricht in der DB.

        Args:
            message: Message-Objekt
            character_name: Character-Name (in dessen History die Nachricht gespeichert wird)
            partner_name: Expliziter Partner-Character-Name (fuer Character-zu-Character)
        """
        if not character_name:
            return

        if not message.timestamp:
            message.timestamp = datetime.now().isoformat()

        partner = UnifiedChatManager._resolve_partner_key(partner_name, character_name)

        # Meta: alle Felder ausser Standard-Felder
        msg_dict = message.to_dict()
        meta = {k: v for k, v in msg_dict.items()
                if k not in ("content", "role", "timestamp", "channel",
                             "channel_message_id")}

        ch_value = (message.channel.value if hasattr(message.channel, "value")
                    else str(message.channel or "web"))

        try:
            with transaction() as conn:
                conn.execute("""
                    INSERT INTO chat_messages
                    (character_name, partner, ts, role, content,
                     channel, channel_message_id, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    character_name,
                    partner,
                    message.timestamp,
                    message.role,
                    message.content,
                    ch_value,
                    getattr(message, "channel_message_id", None),
                    json.dumps(meta, ensure_ascii=False),
                ))
        except Exception as e:
            logger.error("save_message DB-Fehler fuer %s/%s: %s",
                         character_name, partner_name, e)
    
    async def send_message_to_channel(
        self, character_name: str,
        content: str,
        target_channel: ChannelType,
        **kwargs
    ) -> Optional[str]:
        """
        Sende eine Nachricht an einen bestimmten Kanal
        
        Dies ermöglicht Anweisungen wie:
        "Suche Fußballergebnisse und sende sie an Telegram"
        
        Args:
            user_id: Benutzer-ID
            character_name: Agent-Name
            content: Nachrichteninhalt
            target_channel: Zielkanal (z.B. ChannelType.TELEGRAM)
            **kwargs: Kanal-spezifische Optionen
        
        Returns:
            Kanal-spezifische Message-ID falls verfügbar
        """
        channel = self.channel_manager.get_channel(target_channel)
        if not channel:
            raise ValueError(f"Kanal {target_channel.value} nicht registriert")
        
        # Sende Nachricht über den Kanal
        channel_message_id = await channel.send_message(character_name, content, **kwargs
        )
        
        # Speichere im einheitlichen Chat-Format
        message = Message(
            content=content,
            role="assistant",
            channel=target_channel,
            channel_message_id=channel_message_id
        )
        self.save_message(message, character_name)
        
        # Rufe Channel Hook auf
        await channel.on_message_sent(character_name, content, channel_message_id
        )
        
        return channel_message_id
    
    async def broadcast_message(
        self, character_name: str,
        content: str,
        channels: Optional[List[ChannelType]] = None,
        **kwargs
    ) -> Dict[ChannelType, Optional[str]]:
        """
        Sende eine Nachricht an mehrere Kanäle gleichzeitig
        
        Args:
            user_id: Benutzer-ID
            character_name: Agent-Name
            content: Nachrichteninhalt
            channels: Liste von Zielkanälen. Falls None, sende an alle verfügbaren.
            **kwargs: Kanal-spezifische Optionen
        
        Returns:
            Dict mapping Kanäle zu ihren Message-IDs
        """
        target_channels = channels or self.channel_manager.list_channels()
        results = {}
        
        for channel_type in target_channels:
            try:
                msg_id = await self.send_message_to_channel(character_name, content, channel_type, **kwargs
                )
                results[channel_type] = msg_id
            except Exception as e:
                logger.error("Error sending to %s: %s", channel_type.value, e)
                results[channel_type] = None
        
        return results


# Globale Channel Manager Instanz
_channel_manager = None


def get_channel_manager() -> ChannelManager:
    """Hole die globale Channel Manager Instanz"""
    global _channel_manager
    if _channel_manager is None:
        _channel_manager = ChannelManager()
    return _channel_manager


def get_unified_chat_manager() -> UnifiedChatManager:
    """Hole die globale Unified Chat Manager Instanz"""
    return UnifiedChatManager(get_channel_manager())


# Legacy-Kompatibilität mit bestehender API
def get_chat_history(character_name: str = "") -> List[Dict]:
    """Legacy-Funktion für Backward-Kompatibilität"""
    messages = UnifiedChatManager.get_chat_history(character_name)
    # Konvertiere zu altem Format
    return [msg.to_dict() for msg in messages]


def save_message(message: Dict, character_name: str = ""):
    """Legacy-Funktion für Backward-Kompatibilität"""
    # Falls es noch ein altes Dict ist, konvertiere zu Message
    if isinstance(message, dict):
        msg_obj = Message.from_dict(message)
    else:
        msg_obj = message
    
    UnifiedChatManager.save_message(msg_obj, character_name)
