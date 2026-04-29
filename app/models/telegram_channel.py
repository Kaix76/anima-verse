"""
Telegram Channel Implementation

Implementiert die ChannelInterface für Telegram Bot Integration
Ermöglicht bidirektionale Kommunikation mit dem Agenten via Telegram
"""
from typing import Optional, Dict, Any
import os
import asyncio
import json
import aiohttp
from datetime import datetime

from app.core.log import get_logger

logger = get_logger("telegram_channel")

from app.models.channel import ChannelInterface, ChannelType, Message


class TelegramChannel(ChannelInterface):
    """
    Telegram Bot Integration für Multi-Channel Chat
    
    Features:
    - Sende/empfange Nachrichten über Telegram Bot API
    - Chat-ID Mapping zu User-IDs
    - Dokumentiere Chatverlauf im einheitlichen Format
    - Unterstütze verschiedene Parse-Modi (HTML, Markdown)
    - Error Handling und Retry Logic
    
    Konfiguration via Umgebungsvariablen:
    - TELEGRAM_BOT_TOKEN: Bot Token von BotFather
    - TELEGRAM_API_URL: Telegram API URL (optional)
    - TELEGRAM_WEBHOOK_URL: Webhook URL für Telegram (optional)
    
    Bot Setup:
    1. Mit BotFather Channel on Telegram: /newbot
    2. Token erhalten
    3. Webhook setzen mit setWebhook
    """
    
    def __init__(self, bot_token: Optional[str] = None):
        super().__init__(ChannelType.TELEGRAM)
        self.bot_token = bot_token or os.getenv("TELEGRAM_BOT_TOKEN")
        self.api_url = os.getenv("TELEGRAM_API_URL", "https://api.telegram.org/bot")
        self._is_available = False
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Mapping von Telegram Chat-IDs zu User-IDs (In DB in Produktion)
        self.chat_to_user_mapping: Dict[int, str] = {}
        
        # Queue für empfangene Nachrichten
        self.message_queue: asyncio.Queue = asyncio.Queue()
        
        # Laste Chat-ID Mapping aus Storage wenn vorhanden
        self._load_chat_mapping()
        
        if self.bot_token:
            self._is_available = True
            logger.info("Telegram Channel initialisiert (Bot Token vorhanden)")
    
    def _load_chat_mapping(self) -> None:
        """Lade Chat-ID Mapping aus DB (Fallback: JSON-Datei)."""
        try:
            from app.core.db import get_connection as _get_conn
            conn = _get_conn()
            rows = conn.execute(
                "SELECT chat_id, character_name FROM telegram_mapping"
            ).fetchall()
            if rows:
                self.chat_to_user_mapping = {}
                for r in rows:
                    try:
                        self.chat_to_user_mapping[int(r[0])] = r[1]
                    except (ValueError, TypeError):
                        self.chat_to_user_mapping[r[0]] = r[1]
                logger.info("%d Chat-ID Mappings geladen", len(self.chat_to_user_mapping))
                return
        except Exception as e:
            logger.warning("Mapping DB-Fehler: %s", e)
        # Fallback: JSON-Datei
        try:
            from app.core.paths import get_storage_dir
            mapping_file = get_storage_dir() / "telegram_mapping.json"
            if mapping_file.exists():
                data = json.loads(mapping_file.read_text())
                self.chat_to_user_mapping = {
                    int(k): v for k, v in data.items()
                }
                logger.info("%d Chat-ID Mappings aus JSON geladen", len(self.chat_to_user_mapping))
        except Exception as e:
            logger.error("Konnte Mapping nicht laden: %s", e)

    def _save_chat_mapping(self) -> None:
        """Speichern Chat-ID Mapping in DB + JSON-Backup."""
        now = datetime.now().isoformat()
        try:
            from app.core.db import transaction as _transaction
            with _transaction() as conn:
                for chat_id, char_name in self.chat_to_user_mapping.items():
                    conn.execute("""
                        INSERT INTO telegram_mapping (chat_id, character_name, created_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(chat_id) DO UPDATE SET
                            character_name=excluded.character_name
                    """, (str(chat_id), char_name or "", now))
        except Exception as e:
            logger.error("Mapping DB-Speicher-Fehler: %s", e)
        # JSON-Backup
        try:
            from app.core.paths import get_storage_dir
            _sd = get_storage_dir()
            mapping_file = _sd / "telegram_mapping.json"
            data = {str(k): v for k, v in self.chat_to_user_mapping.items()}
            mapping_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.error("Konnte Mapping nicht speichern: %s", e)
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Hole oder erstelle aiohttp Session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    def get_bot_token_for_agent(self, character_name: str) -> str:
        """Returns the bot token for a specific agent from agent_config.json.

        Each agent must have its own telegram_bot_token configured.
        No global fallback.
        """
        if character_name:
            try:
                from app.models.character import get_character_config
                cfg = get_character_config(character_name)
                return cfg.get("telegram_bot_token", "")
            except Exception:
                pass
        return ""

    async def _send_api_request(
        self,
        method: str,
        params: Dict[str, Any],
        bot_token: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Sende Request zur Telegram Bot API

        Args:
            method: API-Methode (z.B. "sendMessage")
            params: Parameter für die API
            bot_token: Optionaler per-Agent Bot-Token (Fallback: self.bot_token)

        Returns:
            API Response oder None bei Error
        """
        token = bot_token or self.bot_token
        try:
            url = f"{self.api_url}{token}/{method}"
            
            session = await self._get_session()
            
            async with session.post(url, json=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                
                if resp.status == 200 and data.get("ok"):
                    return data.get("result")
                else:
                    logger.error("API Error (%d): %s", resp.status, data)
                    return None
                    
        except asyncio.TimeoutError:
            logger.warning("API Timeout bei %s", method)
            return None
        except Exception as e:
            logger.error("API Request Error: %s", e)
            return None
    
    async def send_message(
        self, character_name: str,
        content: str,
        **kwargs
    ) -> Optional[str]:
        """
        Sende Nachricht zu Telegram
        
        Args:
            user_id: User-ID (wird für Logging genutzt)
            character_name: Name des Agenten
            content: Nachrichteninhalt
            **kwargs:
                - chat_id: Telegram Chat-ID (erforderlich)
                - parse_mode: 'HTML' oder 'Markdown' (default: 'HTML')
                - disable_notification: True/False (default: False)
        
        Returns:
            Telegram Message-ID falls erfolgreich
        """
        # Per-Agent Token aufloesen (Fallback auf globalen Token)
        agent_token = self.get_bot_token_for_agent(character_name)

        if not self._is_available and not agent_token:
            raise RuntimeError("Telegram-Kanal nicht verfügbar (kein Bot Token)")

        chat_id = kwargs.get("chat_id")
        if not chat_id:
            raise ValueError("chat_id erforderlich für Telegram")

        parse_mode = kwargs.get("parse_mode", "HTML")
        disable_notification = kwargs.get("disable_notification", False)

        # Telegram Max Message Length: 4096 Zeichen
        # Splitten wenn nötig
        if len(content) > 4096:
            # Splitten in mehrere Nachrichten
            messages = [
                content[i:i+4096]
                for i in range(0, len(content), 4096)
            ]
            last_message_id = None
            for msg in messages:
                msg_id = await self.send_message(character_name, msg,
                    chat_id=chat_id,
                    parse_mode=parse_mode,
                    disable_notification=disable_notification
                )
                last_message_id = msg_id
            return last_message_id

        # Sende via Telegram Bot API (mit per-Agent Token)
        result = await self._send_api_request("sendMessage", {
            "chat_id": chat_id,
            "text": content,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification
        }, bot_token=agent_token)
        
        if result:
            message_id = result.get("message_id")
            logger.info("Nachricht an %s gesendet (ID: %s)", chat_id, message_id)
            return str(message_id)
        else:
            logger.error("Fehler beim Senden an %s", chat_id)
            return None
    
    async def receive_message(self) -> Optional[Dict[str, Any]]:
        """
        Empfange eine Nachricht aus der Queue
        
        Returns:
            Dict mit user_id, content, channel_message_id
        """
        try:
            # Non-blocking receive mit Timeout
            message = await asyncio.wait_for(
                self.message_queue.get(),
                timeout=1.0
            )
            return message
        except asyncio.TimeoutError:
            return None
    
    async def is_available(self) -> bool:
        """Prüfe ob Telegram verfügbar ist"""
        if not self._is_available:
            return False
        
        # Verifiziere Bot Token mit getMe
        try:
            result = await self._send_api_request("getMe", {})
            return result is not None
        except:
            return False
    
    async def handle_webhook(self, data: Dict[str, Any]) -> None:
        """
        Verarbeite eingehenden Telegram Webhook
        
        Wird vom FastAPI Endpoint aufgerufen
        
        Args:
            data: Telegram Update Objekt
        """
        if "message" not in data:
            return
        
        message_data = data["message"]
        chat_id = message_data.get("chat", {}).get("id")
        text = message_data.get("text", "")
        message_id = message_data.get("message_id")
        from_user = message_data.get("from", {})
        
        if not text or not chat_id:
            return
        
        # Mappe Chat-ID zu User-ID
        user_id = self.chat_to_user_mapping.get(chat_id)
        if not user_id:
            # Auto-Registrierung mit Telegram User Info
            user_id = f"telegram_{chat_id}"
            # Nutze Telegram Name wenn vorhanden
            if from_user.get("first_name"):
                display_name = from_user.get("first_name")
                if from_user.get("last_name"):
                    display_name += f" {from_user['last_name']}"
                user_id = display_name
        
        # Füge zu Queue hinzu
        await self.message_queue.put({
            "user_id": "",
            "content": text,
            "channel_message_id": message_id,
            "chat_id": chat_id,
            "platform_data": {
                "from_user": from_user,
                "message_data": message_data
            }
        })
        
        logger.debug("Nachricht empfangen von %s: %s", text[:50])
    
    def register_user(self, telegram_chat_id: int) -> None:
        """
        Registriere ein Mapping von Telegram Chat-ID zu User-ID
        
        Args:
            telegram_chat_id: Telegram Chat ID
            user_id: Eindeutige User ID im System
        """
        self.chat_to_user_mapping[telegram_chat_id] = ""
        self._save_chat_mapping()
        logger.info("User registriert: %s", telegram_chat_id)
    
    async def on_message_received(
        self, content: str,
        channel_message_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Hook wenn Nachricht von Telegram empfangen wird"""
        return await super().on_message_received(content, channel_message_id)
    
    async def on_message_sent(
        self, character_name: str,
        content: str,
        channel_message_id: Optional[str] = None
    ) -> None:
        """Hook wenn Nachricht an Telegram gesendet wird"""
        await super().on_message_sent(character_name, content, channel_message_id)
    
    async def close(self) -> None:
        """Cleanup: Schließe aiohttp Session"""
        if self._session:
            await self._session.close()
            logger.info("Session geschlossen")


# Globale Telegram Channel Instanz
_telegram_channel: Optional[TelegramChannel] = None


def get_telegram_channel() -> TelegramChannel:
    """Hole die globale Telegram Channel Instanz"""
    global _telegram_channel
    if _telegram_channel is None:
        _telegram_channel = TelegramChannel()
    return _telegram_channel


