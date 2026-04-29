"""NotifyUser Skill - Benachrichtigung an den User

Ermoeglicht einem Character, eigenstaendig eine Notification an den User zu senden.
Wird hauptsaechlich vom Gedanken-System genutzt, kann aber auch im normalen Chat
aufgerufen werden.

Die Notification wird gleichzeitig als Chat-Message in der History des Characters
mit dem aktiven User-Avatar gespeichert — damit der Character beim spaeteren
Chat im Kontext weiss was er angekuendigt hat.

Cooldown: pro Character max 1 Notification in NOTIFY_COOLDOWN_MIN Minuten
(Default 30) — verhindert Notification-Spam aus mehrfachen Thought-Loops.
"""
import os
import time
from typing import Any, Dict

from .base import BaseSkill

from app.core.log import get_logger
logger = get_logger("notify_user")

from app.models.notifications import create_notification

# Cooldown pro Character (in Minuten)
NOTIFY_COOLDOWN_MIN = int(os.environ.get("SKILL_NOTIFY_USER_COOLDOWN_MIN", "30"))
_last_notification_ts: Dict[str, float] = {}  # character_name -> monotonic ts


class NotifyUserSkill(BaseSkill):
    """
    Skill zum Senden einer Benachrichtigung an den User.

    Der Character kann diesen Skill nutzen um dem User eine Nachricht zu senden,
    ohne dass der User gerade aktiv im Chat ist.
    """

    SKILL_ID = "notify_user"
    ALWAYS_LOAD = True  # Immer laden, Aktivierung per Character

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        self.name = os.environ.get("SKILL_NOTIFY_USER_NAME", "SendNotification")
        self.description = os.environ.get(
            "SKILL_NOTIFY_USER_DESCRIPTION",
            "Send a notification message to the user. Use this to proactively inform the user about something important."
        )
        self._defaults = {}

    def execute(self, raw_input: str) -> str:
        """Erstellt eine Notification fuer den User.

        Input-Format (vom LLM):
            Einfach die Nachricht, z.B. "Vergiss nicht: Meeting um 14 Uhr!"
            Oder als JSON: {"input": "...", "agent_name": "...", "user_id": "..."}
        """
        if not self.enabled:
            return "SendNotification Skill ist nicht verfuegbar."

        ctx = self._parse_base_input(raw_input)
        input_text = ctx.get("input", raw_input).strip()
        character_name = ctx.get("agent_name", "").strip()
        user_id = ctx.get("user_id", "").strip()

        if not character_name:
            return "Fehler: Agent-Name fehlt."
        if not input_text:
            return "Fehler: Keine Nachricht angegeben."

        # Cooldown-Check: verhindert Notification-Spam
        now_mono = time.monotonic()
        last = _last_notification_ts.get(character_name, 0.0)
        wait_s = NOTIFY_COOLDOWN_MIN * 60 - (now_mono - last)
        if wait_s > 0:
            logger.info("Notification von %s blockiert: Cooldown noch %.0fs",
                        character_name, wait_s)
            return (f"Notification nicht gesendet: Cooldown aktiv "
                    f"(letzte Notification vor {(now_mono-last)/60:.1f} Min, "
                    f"Limit alle {NOTIFY_COOLDOWN_MIN} Min). "
                    f"Falls dringend: nutze SendMessage stattdessen.")

        try:
            nid = create_notification(
                character=character_name,
                content=input_text,
                notification_type="message",
                metadata={"trigger": "thought"})
            _last_notification_ts[character_name] = now_mono

            # WICHTIG: auch als Chat-Message in der History speichern, damit
            # der Character beim spaeteren Chat weiss was er ankuendigt hat.
            # Frueher: Notification existierte nur im Notif-Panel; klickte der
            # User auf "→ Chat" und schrieb, hatte der Character keinen
            # History-Eintrag und reagierte verwirrt.
            try:
                from app.models.chat import save_message
                from app.models.account import get_active_character, get_user_name
                from datetime import datetime as _dt
                avatar = get_active_character() or get_user_name() or ""
                if avatar and avatar != character_name:
                    ts = _dt.now().isoformat()
                    save_message({
                        "role": "assistant",
                        "content": input_text,
                        "timestamp": ts,
                        "speaker": character_name,
                        "medium": "messaging",
                    }, character_name=character_name, partner_name=avatar)
                    # Spiegel im Avatar-Inbox damit Reload den Eintrag zeigt
                    save_message({
                        "role": "user",
                        "content": input_text,
                        "timestamp": ts,
                        "speaker": character_name,
                        "medium": "messaging",
                    }, character_name=avatar, partner_name=character_name)
            except Exception as _se:
                logger.debug("Notification → Chat-History fehlgeschlagen: %s", _se)

            logger.info(f"Notification erstellt: {nid} ({character_name} -> {user_id})")
            return f"Notification erfolgreich gesendet: {input_text}"
        except Exception as e:
            logger.error(f"Fehler: {e}")
            return f"Fehler beim Senden der Notification: {e}"
