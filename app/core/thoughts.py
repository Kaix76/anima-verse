"""ThoughtLoop - Autonomes NachdenkSystem fuer Characters.

Characters koennen periodisch selbststaendig "nachdenken" und basierend auf
ihrer Aufgabe (character_task) entscheiden, ob sie den User benachrichtigen
oder Tools nutzen.

Bedingungen pro Tick (alle 60s):
  - User idle >= THOUGHT_MIN_IDLE_MINUTES (default 4)
  - Naechster Scheduler-Job >= THOUGHT_MIN_SCHEDULER_GAP_MINUTES (default 5)
  - Kein anderer Gedanken-Call aktiv (globaler Lock)
  - Character hat character_task + thoughts_enabled=true in config
  - Tages-Limit und Cooldown eingehalten
  - Wahrscheinlichkeitswurf bestanden
"""
import asyncio
import os
import random
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.core.log import get_logger
logger = get_logger("thought")

_thought_loop: Optional["ThoughtLoop"] = None


def _check_cascade_brake(tool_input, allowed_target: str) -> str:
    """Cascade-Brake-Helper: extrahiert das Target aus SendMessage/TalkTo-Input
    und prueft gegen allowed_target.

    SendMessage/TalkTo-Input ist "TargetName, message" oder JSON. Wenn das
    Target NICHT der allowed_target ist, return das Target (= Block-Reason).
    Sonst leerer String (= Pass).
    """
    raw = (tool_input or "").strip() if isinstance(tool_input, str) else str(tool_input or "")
    target = ""
    if raw.startswith("{"):
        try:
            import json as _json
            d = _json.loads(raw)
            target = (d.get("target") or d.get("input") or "").split(",", 1)[0].strip()
        except Exception:
            pass
    if not target:
        target = raw.split(",", 1)[0].strip()
    if not target:
        return ""
    # case-insensitive Match (Vornamen-Verkuerzungen wie "Kira" vs "Kira Voss"
    # tolerieren wir hier nicht — wer addressed werden soll, soll exakt matchen)
    if target.lower() == allowed_target.strip().lower():
        return ""
    return target


def get_thought_loop() -> Optional["ThoughtLoop"]:
    return _thought_loop


def set_thought_loop(loop: "ThoughtLoop"):
    global _thought_loop
    _thought_loop = loop


class ThoughtLoop:
    """Asyncio Background-Loop der Characters autonom agieren laesst."""

    def __init__(self, scheduler_manager):
        self._scheduler = scheduler_manager
        self._lock = asyncio.Lock()
        # Per user_id: Zeitpunkt der letzten Interaktion
        self._last_interaction: Dict[str, datetime] = {}
        # Per user_id -> {character_name: count}
        self._daily_counts: Dict[str, int] = {}   # character_name -> count
        self._daily_counts_date: Optional[str] = None  # YYYY-MM-DD
        # Per user_id -> {character_name: datetime}
        self._last_thought: Dict[str, datetime] = {}   # character_name -> last ts
        self._task: Optional[asyncio.Task] = None
        self._paused: bool = False
        self._tick_count: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self):
        """Startet den Background-Loop als asyncio Task."""
        # Main-Loop-Referenz fuer forcierte Gedanken aus Worker-Threads
        self._main_loop = asyncio.get_running_loop()
        self._seed_users()
        self._task = asyncio.create_task(self._loop())
        # Background-Queue-Handler fuer forcierte Gedanken registrieren
        try:
            from app.core.background_queue import get_background_queue
            get_background_queue().register_handler(
                "forced_thought", self._handle_forced_thought
            )
            logger.info("Handler 'forced_thought' registriert")
        except Exception as e:
            logger.warning("Konnte forced_thought Handler nicht registrieren: %s", e)
        logger.info("Gestartet")

    def _handle_forced_thought(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """TaskQueue-Handler: triggert run_thought_turn mit Context-Hint.

        Laeuft in einem Worker-Thread, submittet die Coroutine auf den Main-Loop.

        Payload-Felder:
            user_id, character_name, context_hint — wie bisher
            fast (bool) — wenn True, wird der Gedanke ueber das Tool-LLM
                statt Chat-LLM gerechnet (deutlich schneller, einsetzbar fuer
                Instagram-Kommentare und aehnliche Kurz-Reaktionen).
            tool_whitelist (list[str]) — falls gesetzt, werden nur diese Tools
                an den Agent uebergeben. Alle anderen Skills sind fuer diesen
                Call blockiert. Sperre gegen Rollenkonfusion bei passiven
                Observer-Gedanken (z.B. "du siehst einen fremden IG-Post").
            suppress_notification (bool) — falls True, wird der Narrativ-Text
                des LLM NICHT als Notification/Chat-Message gespeichert. Nur
                die Tool-Aufrufe haben Wirkung. Verhindert dass halluzinierter
                Prompt-Inhalt in die History leakt.
        """
        user_id = payload.get("user_id", "")
        character_name = payload.get("character_name", "")
        context_hint = payload.get("context_hint", "")
        fast = bool(payload.get("fast", False))
        tool_whitelist = payload.get("tool_whitelist") or None
        suppress_notification = bool(payload.get("suppress_notification", False))
        # reply_only_to: Cascade-Brake. Wenn gesetzt, darf SendMessage NUR an
        # diesen Empfaenger gehen (= der urspruengliche Sender). Verhindert
        # Diego→Luna→Enzo-Kaskaden in denen jeder Empfaenger Dritte anschreibt.
        reply_only_to = (payload.get("reply_only_to") or "").strip()
        # llm_task: optionaler Sub-Task fuer LLM-Routing (z.B. "thought_greeting"
        # bei Avatar-Eintritt). Default: "thought" (Sammeltask). Sub-Tasks fallen
        # automatisch auf "thought" zurueck wenn nicht geroutet (siehe llm_router).
        llm_task = (payload.get("llm_task") or "thought").strip() or "thought"
        if not character_name:
            return {"success": False, "error": "character_name missing"}
        # Avatar-Schutz: vom User gesteuerte Characters denken nicht autonom.
        # Andernfalls leakt Pixel→Avatar→Bianca-Forwarding (siehe Pad-Bug).
        # Der idle-Loop hat denselben Check, forced_thought hatte ihn nicht.
        try:
            from app.models.account import is_player_controlled
            if is_player_controlled(character_name):
                logger.info("forced_thought uebersprungen: %s ist player-controlled (Avatar)",
                             character_name)
                return {"success": True, "skipped": "player_controlled"}
        except Exception:
            pass
        # Feature-Gate respektieren — wenn thoughts_enabled=False, kein Forced-Thought.
        try:
            from app.models.character_template import is_feature_enabled
            if not is_feature_enabled(character_name, "thoughts_enabled"):
                logger.info("forced_thought uebersprungen: %s hat thoughts_enabled=False",
                             character_name)
                return {"success": True, "skipped": "thoughts_disabled"}
        except Exception:
            pass
        logger.info("forced_thought startet: %s (hint=%s, fast=%s, whitelist=%s, suppress=%s, llm_task=%s)",
                    character_name, context_hint[:80], fast,
                    tool_whitelist, suppress_notification, llm_task)
        try:
            import asyncio as _asyncio
            main_loop = getattr(self, "_main_loop", None)
            if main_loop and main_loop.is_running():
                fut = _asyncio.run_coroutine_threadsafe(
                    self.run_thought_turn(character_name,
                                          context_hint=context_hint, fast=fast,
                                          tool_whitelist=tool_whitelist,
                                          suppress_notification=suppress_notification,
                                          llm_task=llm_task,
                                          reply_only_to=reply_only_to),
                    main_loop)
                try:
                    fut.result(timeout=600)
                except TimeoutError:
                    # Coroutine im main_loop laeuft sonst weiter, blockiert
                    # Channel-chat_active-Counter unbegrenzt. Cancel triggert
                    # das finally in run_thought_turn → register_chat_done.
                    fut.cancel()
                    logger.error("forced_thought timeout 600s: %s — Coroutine gecancelt",
                                 character_name)
                    raise
            else:
                # Fallback: neuer Loop (Tests / edge cases)
                _asyncio.run(self.run_thought_turn(character_name,
                                                    context_hint=context_hint, fast=fast,
                                                    tool_whitelist=tool_whitelist,
                                                    suppress_notification=suppress_notification,
                                                    llm_task=llm_task,
                                                    reply_only_to=reply_only_to))
            logger.info("forced_thought abgeschlossen: %s", character_name)
            return {"success": True}
        except Exception as e:
            logger.error("forced_thought Fehler: %s", e, exc_info=True)
            return {"success": False, "error": str(e)}

    def _seed_users(self):
        """Seed _last_interaction, damit der ThoughtLoop nach Server-Neustart
        sofort funktioniert."""
        seed_time = datetime.now() - timedelta(minutes=self._min_idle_minutes() + 1)
        if "" not in self._last_interaction:
            self._last_interaction[""] = seed_time
            logger.info("Seed: ThoughtLoop initialisiert")

    async def stop(self):
        """Stoppt den Loop."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Gestoppt")

    def record_interaction(self):
        """Wird vom Chat-Endpoint bei jeder User-Nachricht aufgerufen."""
        self._last_interaction[""] = datetime.now()

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self):
        """Pausiert den ThoughtLoop (Ticks werden uebersprungen)."""
        self._paused = True
        logger.info("ThoughtLoop pausiert")

    def resume(self):
        """Setzt den ThoughtLoop fort."""
        self._paused = False
        logger.info("ThoughtLoop fortgesetzt")

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _min_idle_minutes() -> float:
        return float(os.environ.get("THOUGHT_MIN_IDLE_MINUTES", "4"))

    @staticmethod
    def _min_scheduler_gap_minutes() -> float:
        return float(os.environ.get("THOUGHT_MIN_SCHEDULER_GAP_MINUTES", "5"))

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _loop(self):
        """Haupt-Loop: alle 60s pruefen."""
        # Kurz warten nach Server-Start damit alles initialisiert ist
        await asyncio.sleep(30)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Fehler im Tick: %s", e)
            await asyncio.sleep(60)

    async def _tick(self):
        """Ein Durchlauf: Pruefe alle User, waehle Character, fuehre LLM-Call aus."""
        if self._paused:
            return

        # Tages-Reset
        today = datetime.now().strftime("%Y-%m-%d")
        if self._daily_counts_date != today:
            self._daily_counts = {}
            self._daily_counts_date = today

        # Assignment-Expiry: abgelaufene Aufgaben markieren
        for user_id in list(self._last_interaction.keys()):
            try:
                from app.models.assignments import expire_overdue
                expired = expire_overdue()
                for ea in expired:
                    logger.info("Assignment abgelaufen: %s '%s' (user=%s)",
                                ea["id"], ea.get("title"))
            except Exception as e:
                logger.debug("Assignment expiry error: %s", e)

        # Kein User hat je interagiert
        if not self._last_interaction:
            return

        # Lock: nur ein Gedanken-Call gleichzeitig
        if self._lock.locked():
            return

        for user_id, last_ts in list(self._last_interaction.items()):
            idle_minutes = (datetime.now() - last_ts).total_seconds() / 60.0

            # Urgente Events: disruption/danger ueberspringen Idle-Timer
            _urgent_char = None
            if idle_minutes < self._min_idle_minutes():
                _urgent_char = self._find_urgent_event_character()
                if not _urgent_char:
                    continue

            if not self._is_scheduler_clear():
                continue

            if _urgent_char:
                # Urgent Event: direkt diesen Character nehmen
                char_name, char_config, idle_min = _urgent_char
                logger.info("URGENT EVENT: %s reagiert sofort", char_name)
            else:
                eligible = await self._get_eligible_characters(idle_minutes)
                if not eligible:
                    continue

                winner = self._roll_character(eligible)
                if not winner:
                    continue

                char_name, char_config, idle_min = winner

            # Gedanken-Call ausfuehren (unter Lock, mit Timeout)
            _THOUGHT_CALL_TIMEOUT = int(os.environ.get("THOUGHT_CALL_TIMEOUT", "600"))  # 10min default
            async with self._lock:
                logger.info("Gedanken-Call: %s (idle: %.0fmin)", char_name, idle_min)
                try:
                    await asyncio.wait_for(
                        self.run_thought_turn(char_name),
                        timeout=_THOUGHT_CALL_TIMEOUT)
                except asyncio.TimeoutError:
                    logger.error("Gedanken-Call TIMEOUT nach %ds: %s (Lock wird freigegeben)",
                                 _THOUGHT_CALL_TIMEOUT, char_name)
                except Exception as e:
                    logger.error("Fehler bei Gedanken-Call fuer %s: %s", char_name, e)

                # Zaehler und Cooldown aktualisieren
                self._daily_counts[char_name] = self._daily_counts.get(char_name, 0) + 1
                self._last_thought[char_name] = datetime.now()

            # Nur ein Character pro Tick
            break

        # Story Arc: automatisch neue Arcs generieren wenn moeglich
        for user_id in list(self._last_interaction.keys()):
            try:
                from app.models.story_arcs import can_generate
                from app.core.story_engine import ENABLED, MAX_ACTIVE_ARCS, COOLDOWN_HOURS
                if ENABLED and can_generate(MAX_ACTIVE_ARCS, COOLDOWN_HOURS):
                    from app.core.background_queue import get_background_queue
                    get_background_queue().submit("story_arc_generate", {"user_id": ""})
                    logger.info("Story Arc Generierung getriggert (user=%s)")
            except Exception as e:
                logger.debug("Story Arc Auto-Generate Fehler: %s", e)

        # Cross-Character Memory abgeschafft: Char-zu-Char Wissen entsteht
        # jetzt natuerlich durch tatsaechliche Konversationen (TalkTo / SendMessage),
        # nicht mehr per "telepathischer" LLM-Analyse von User-Chats.

        # Relationship Summaries: periodisch stale Beziehungen zusammenfassen
        try:
            from app.core.relationship_summary import ENABLED as RS_ENABLED, INTERVAL_MINUTES as RS_INTERVAL
            if RS_ENABLED:
                now = datetime.now()
                if not hasattr(self, '_last_relationship_summary') or \
                        (now - self._last_relationship_summary).total_seconds() >= RS_INTERVAL * 60:
                    self._last_relationship_summary = now
                    from app.core.background_queue import get_background_queue
                    for user_id in list(self._last_interaction.keys()):
                        get_background_queue().submit(
                            "relationship_summary", {"user_id": ""},
                            deduplicate=True)
                    logger.debug("Relationship Summary getriggert")
        except Exception as e:
            logger.debug("Relationship Summary Fehler: %s", e)

        # Relationship Graph Decay: woechentlich Staerke reduzieren bei Inaktivitaet
        try:
            _DECAY_INTERVAL = 6 * 3600  # alle 6 Stunden pruefen
            now = datetime.now()
            if not hasattr(self, '_last_relationship_decay') or \
                    (now - self._last_relationship_decay).total_seconds() >= _DECAY_INTERVAL:
                self._last_relationship_decay = now
                from app.core.background_queue import get_background_queue
                for user_id in list(self._last_interaction.keys()):
                    get_background_queue().submit(
                        "relationship_decay", {"user_id": ""},
                        deduplicate=True)
                logger.debug("Relationship Decay getriggert")
        except Exception as e:
            logger.debug("Relationship Decay Fehler: %s", e)

        # Character Evolution: Periodisch beliefs/lessons/goals per LLM aktualisieren
        try:
            from app.core.character_evolution import ENABLED as CE_ENABLED, INTERVAL_HOURS as CE_INTERVAL
            if CE_ENABLED:
                now = datetime.now()
                if not hasattr(self, '_last_character_evolution') or \
                        (now - self._last_character_evolution).total_seconds() >= CE_INTERVAL * 3600:
                    self._last_character_evolution = now
                    from app.core.background_queue import get_background_queue
                    for user_id in list(self._last_interaction.keys()):
                        get_background_queue().submit(
                            "character_evolution", {"user_id": ""},
                            deduplicate=True)
                    logger.debug("Character Evolution getriggert")
        except Exception as e:
            logger.debug("Character Evolution Fehler: %s", e)

        # Hourly Status Tick: Decay/Regen fuer alle Character-Statuswerte
        # In Worker-Thread auslagern — die Schleife ueber alle Characters mit
        # File-I/O pro Character blockiert sonst den Event-Loop (>1s bei vielen
        # Characters), was den Watchdog ausloest.
        try:
            from app.core.activity_engine import apply_hourly_status_tick
            from app.models.character import list_available_characters
            import asyncio as _asyncio
            def _run_all_hourly_ticks():
                chars = list_available_characters()
                for char_name in chars:
                    try:
                        apply_hourly_status_tick(char_name)
                    except Exception as e:
                        logger.debug("Hourly status tick Fehler (%s): %s", char_name, e)
            for _user_id in list(self._last_interaction.keys()):
                # to_thread schiebt die ganze Schleife in den Threadpool —
                # Event-Loop bleibt waehrenddessen frei fuer andere Tasks.
                _asyncio.create_task(_asyncio.to_thread(_run_all_hourly_ticks))
                # Pro User-Loop nur einmal triggern; ein Worker-Thread arbeitet alle Chars ab.
                break
        except Exception as e:
            logger.debug("Hourly Status Tick Import-Fehler: %s", e)

        # Random Events generieren (stuendlich, nicht jeden Tick)
        # Diese Calls machen synchrone llm_queue.submit() — in Executor auslagern,
        # sonst blockiert ein haengender/paused Provider den gesamten Event Loop.
        self._tick_count += 1
        if self._tick_count % 60 == 0:
            try:
                from app.core.random_events import check_and_generate
                for user_id in list(self._last_interaction.keys()):
                    await asyncio.to_thread(check_and_generate)
            except Exception as e:
                logger.debug("Random events error: %s", e)
        # Eskalation alle 5 Min pruefen
        if self._tick_count % 5 == 0:
            try:
                from app.core.random_events import check_escalation
                for user_id in list(self._last_interaction.keys()):
                    await asyncio.to_thread(check_escalation)
            except Exception as e:
                logger.debug("Random events escalation error: %s", e)
        # Event-Aufloesung alle 5 Min (versetzt um 2 Min gg. Escalation)
        if self._tick_count % 5 == 2:
            try:
                from app.core.random_events import try_resolve_events
                for user_id in list(self._last_interaction.keys()):
                    await asyncio.to_thread(try_resolve_events)
            except Exception as e:
                logger.debug("Event resolution error: %s", e)

    # ------------------------------------------------------------------
    # Telegram delivery
    # ------------------------------------------------------------------

    @staticmethod
    async def _send_to_telegram(character_name: str, content: str):
        """Send thought notification via Telegram if the character has a bot."""
        from app.core.telegram_polling import get_polling_manager

        pm = get_polling_manager()
        key = character_name
        poller = pm.pollers.get(key)
        if not poller or not poller._running:
            return  # No active Telegram bot for this character

        # Find all registered chat_ids for this user
        from app.models.telegram_channel import get_telegram_channel
        telegram = get_telegram_channel()

        sent = False
        for chat_id, mapped_user in telegram.chat_to_user_mapping.items():
            # Send to all registered Telegram users
            await poller.send_message(chat_id, content, parse_mode="")
            sent = True

        if sent:
            logger.info("[%s] Gedanken-Nachricht an Telegram gesendet", character_name)

    # ------------------------------------------------------------------
    # Condition checks
    # ------------------------------------------------------------------

    def _is_scheduler_clear(self) -> bool:
        """Prueft ob kein Scheduler-Job in den naechsten N Minuten laeuft.

        Ausgenommen: der globale `world_hourly_tick` — er laeuft stuendlich und
        wuerde sonst jede Stunde eine 5-Minuten-Dead-Zone (XX:55-XX:00) im
        Thought-Loop erzeugen. Der Tick arbeitet alle Characters sequenziell
        in ca. 30s ab — der Thought-Loop nimmt das hin.
        """
        gap_minutes = self._min_scheduler_gap_minutes()
        try:
            now = datetime.now()
            threshold = now + timedelta(minutes=gap_minutes)
            for job in self._scheduler.scheduler.get_jobs():
                if getattr(job, "id", "") == "world_hourly_tick":
                    continue
                nrt = job.next_run_time
                if nrt and nrt.replace(tzinfo=None) <= threshold:
                    return False
        except Exception as e:
            logger.error("Scheduler-Check Fehler: %s", e)
            return False
        return True

    def _is_in_active_group_chat(self, character_name: str, minutes: int = 10) -> bool:
        """Prueft ob der Character in einem aktiven Group Chat ist (letzte N Minuten)."""
        try:
            from app.models.group_chat import load_sessions
            now_ts = datetime.now().timestamp()
            threshold = minutes * 60
            for s in load_sessions():
                if not s.get("active", True):
                    continue
                if character_name not in s.get("participants", []):
                    continue
                last_activity = s.get("last_activity", "")
                if last_activity:
                    activity_ts = datetime.fromisoformat(last_activity).timestamp()
                    if (now_ts - activity_ts) < threshold:
                        return True
        except Exception as e:
            logger.debug("Group-Chat-Check Fehler: %s", e)
        return False

    async def _get_eligible_characters(self, idle_minutes: float) -> List[tuple]:
        """Gibt eligible Characters zurueck: (name, config, idle_minutes)."""
        from app.models.character import (
            list_available_characters,
            get_character_profile,
            get_character_config,
            is_character_sleeping)

        characters = list_available_characters()
        eligible = []

        from app.models.account import is_player_controlled

        from app.models.character_template import is_feature_enabled as _feat
        for char_name in characters:
            # Player-controlled characters never act autonomously
            if is_player_controlled(char_name):
                continue

            # Feature-Gate: thoughts_enabled=false -> skip (Chatbots ohne Gedanken-Aktionen)
            if not _feat(char_name, "thoughts_enabled"):
                continue

            # Sleep-Check: Character schlaeft laut Tagesablauf
            if is_character_sleeping(char_name):
                continue

            # Group-Chat-Check: Character ist in aktivem Group Chat (letzte 10 Min)
            if self._is_in_active_group_chat(char_name):
                logger.debug("%s: Uebersprungen (aktiver Group Chat)", char_name)
                continue

            config = get_character_config(char_name)
            # Config-Werte kommen als Strings aus JSON — robust casten
            enabled_raw = config.get("thoughts_enabled", False)
            if isinstance(enabled_raw, str):
                enabled_raw = enabled_raw.lower() in ("true", "1", "yes")
            if not enabled_raw:
                continue

            profile = get_character_profile(char_name)
            task = (profile.get("character_task", "") or "").strip()
            if not task:
                continue

            # Aktive Assignments pruefen — boost Tages-Limit, Cooldown und Probability
            try:
                from app.models.assignments import list_assignments
                active_assignments = list_assignments(character_name=char_name, status="active")
                highest_prio = min((a.get("priority", 5) for a in active_assignments), default=5) if active_assignments else 5
            except Exception:
                active_assignments = []
                highest_prio = 5

            # Tages-Limit (erhoeht bei aktiven Assignments)
            try:
                max_daily = int(config.get("thoughts_max_daily", 5))
            except (ValueError, TypeError):
                max_daily = 5
            # Assignment-Boost: Prio 1 → x3, Prio 2 → x2
            if active_assignments and highest_prio <= 2:
                max_daily = max_daily * (4 - highest_prio)  # p1→x3, p2→x2
            current_count = self._daily_counts.get(char_name, 0)
            if current_count >= max_daily:
                continue

            # Cooldown (verkuerzt bei aktiven Assignments)
            try:
                cooldown_min = float(config.get("thoughts_cooldown_minutes", 30))
            except (ValueError, TypeError):
                cooldown_min = 30.0
            # Assignment-Boost: Prio 1 → 1/3 Cooldown, Prio 2 → 1/2
            if active_assignments and highest_prio <= 2:
                cooldown_min = cooldown_min / (4 - highest_prio)  # p1→/3, p2→/2
            last = self._last_thought.get(char_name)
            if last:
                elapsed = (datetime.now() - last).total_seconds() / 60.0
                if elapsed < cooldown_min:
                    continue

            # Speichere Assignment-Boost fuer den Roll
            config["_assignment_boost"] = highest_prio if active_assignments else 0

            eligible.append((char_name, config, idle_minutes))

        return eligible

    @staticmethod
    def _roll_character(eligible: List[tuple]) -> Optional[tuple]:
        """Wuerfelt basierend auf thoughts_probability. Gibt Gewinner oder None zurueck.

        Characters mit aktiven Assignments bekommen einen Probability-Boost:
        Prio 1 (Dringend) → Probability x2, Prio 2 (Hoch) → x1.5
        """
        candidates = []
        for char_name, config, idle_min in eligible:
            try:
                prob = float(config.get("thoughts_probability", 0.3))
            except (ValueError, TypeError):
                prob = 0.3
            # Assignment-Boost
            boost_prio = config.get("_assignment_boost", 0)
            if boost_prio == 1:
                prob = min(1.0, prob * 2.0)
            elif boost_prio == 2:
                prob = min(1.0, prob * 1.5)
            if random.random() < prob:
                candidates.append((char_name, config, idle_min))

        if not candidates:
            return None
        # Zufaellig einen Gewinner aus allen die den Wurf bestanden haben
        return random.choice(candidates)

    # ------------------------------------------------------------------
    # LLM Call
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_hallucinated_tools(text: str, real_tool_names: List[str]) -> str:
        """Entfernt halluzinierte Tool-Calls aus der Chat-LLM-Antwort.

        Das Chat-LLM schreibt manchmal Tool-Aufrufe als Prosa statt sie
        tatsaechlich aufzurufen. Diese Fake-Calls werden hier entfernt.
        """
        if not text:
            return text

        # [ToolName](...) — Markdown-Link-artige Fake-Calls
        text = re.sub(r'\[(?:' + '|'.join(re.escape(t) for t in real_tool_names) + r')\]\([^)]*\)', '', text)

        # [ToolName: ...] — Bracket-Style Fake-Calls mit Beschreibung
        text = re.sub(r'\[(?:' + '|'.join(re.escape(t) for t in real_tool_names) + r'):[^\]]*\]', '', text)

        # [ToolName] allein stehend (ohne Link)
        text = re.sub(r'\[(?:' + '|'.join(re.escape(t) for t in real_tool_names) + r')\]', '', text)

        # Nackte Tool-Namen als eigene Zeile (z.B. "WebSearch\n" oder "KnowledgeExtract\n")
        for tn in real_tool_names:
            text = re.sub(rf'^\s*{re.escape(tn)}\s*$', '', text, flags=re.MULTILINE)

        # <tool name="...">...</tool> Tags (geschlossen)
        text = re.sub(r'<tool\s+name="[^"]*">[\s\S]*?</tool>', '', text)

        # <tool name="...">... (ungeschlossen — bis Zeilenende oder naechstes <tool)
        text = re.sub(r'<tool\s+name="[^"]*">[^<]*', '', text)

        # (**An:** ...) / (**Generiere:** ...) — fake Parameter-Blöcke
        text = re.sub(r'\(\*\*[^)]{0,200}\*\*[^)]{0,500}\)', '', text)

        # Mehrfache Leerzeilen zusammenfassen
        text = re.sub(r'\n{3,}', '\n\n', text)

        return text.strip()

    def _find_urgent_event_character(self):
        """Findet einen Character der auf ein disruption/danger Event reagieren sollte.

        Returns (char_name, char_config, idle_min) oder None.
        Waehlt zufaellig aus Characters an betroffenen Locations.
        """
        try:
            import random as _rnd
            from app.models.events import get_all_events
            from app.models.character import (
                list_available_characters, get_character_current_location,
                get_character_config, is_player_controlled)

            urgent_events = [
                e for e in get_all_events()
                if e.get("category") in ("disruption", "danger") and e.get("location_id")
            ]
            if not urgent_events:
                return None

            # Betroffene Locations: Event-Ort + Nachbarn bei disruption, alle bei danger
            from app.models.world import get_neighbor_location_ids
            urgent_locations = set()
            for evt in urgent_events:
                evt_loc = evt["location_id"]
                urgent_locations.add(evt_loc)
                if evt.get("category") == "danger":
                    # Danger: alle Locations betroffen (wir nehmen trotzdem nur Nachbarn + Ort)
                    # damit nicht die ganze Welt reagiert
                    try:
                        urgent_locations.update(get_neighbor_location_ids(evt_loc))
                    except Exception:
                        pass
                elif evt.get("category") == "disruption":
                    try:
                        urgent_locations.update(get_neighbor_location_ids(evt_loc))
                    except Exception:
                        pass

            # Characters an diesen Locations (nur autonome, nicht der Spieler)
            candidates = []
            for char in list_available_characters():
                if is_player_controlled(char):
                    continue
                loc = get_character_current_location(char)
                if loc in urgent_locations:
                    # Cooldown pruefen (nicht zu oft reagieren)
                    last = self._last_thought.get(char)
                    if last and (datetime.now() - last).total_seconds() < 120:
                        continue  # 2 Min Cooldown zwischen Gedanken-Calls
                    config = get_character_config(char)
                    candidates.append((char, config, 0))

            if candidates:
                return _rnd.choice(candidates)
        except Exception as e:
            logger.debug("_find_urgent_event_character error: %s", e)
        return None

    @staticmethod
    def _build_thought_events(location_id: str) -> str:
        """Baut Events-Kontext fuer den Thought-Prompt."""
        try:
            from app.models.events import build_events_prompt_section
            section = build_events_prompt_section(location_id=location_id)
            if section:
                return section + "\n"
        except Exception:
            pass
        return ""

    async def run_thought_turn(self, character_name: str,
                                context_hint: str = "", fast: bool = False,
                                tool_whitelist=None,
                                suppress_notification: bool = False,
                                llm_task: str = "thought",
                                reply_only_to: str = ""):
        """Fuehrt den Gedanken-LLM-Call mit Tool-Support aus.

        Nutzt den gleichen StreamingAgent wie der Chat, aber mit
        einem speziellen System-Prompt der auf die Aufgabe fokussiert ist.

        fast: Wenn True, laeuft der ganze Gedanke ueber das Tool-LLM
            (schneller, fuer Kurz-Reaktionen wie Instagram-Kommentare).
            Kein Dual-LLM-Pass — der Tool-LLM uebernimmt Prompt + Tool-Calls
            in einem Durchgang.
        tool_whitelist: Falls gesetzt (z.B. ["InstagramComment"]) werden
            agent_tools hart auf diese Tool-Namen gefiltert. Alle anderen
            Skills sind fuer diesen Call unerreichbar.
        suppress_notification: Falls True wird der Narrativ-Text des LLM
            NICHT als Notification/Chat-Message gespeichert. Nur Tool-Calls
            haben Wirkung. Verhindert Halluzinations-Leaks in die History
            bei passiven Observer-Gedanken.
        reply_only_to: Cascade-Brake. Wenn gesetzt, blockt der Tool-Executor
            jeden SendMessage/TalkTo-Aufruf an einen ANDEREN Empfaenger als
            diesen. Verhindert dass der Empfaenger einer DM weitere DMs an
            Dritte schickt (Diego→Luna→Enzo-Kette). Reply muss explizit an
            den urspruenglichen Sender gehen.
        """
        from app.models.character import (
            get_character_profile,
            get_character_config,
            get_character_appearance)
        from app.models.world import get_location_name
        from app.models.character import get_character_current_location
        from app.core.dependencies import get_skill_manager
        from app.core.llm_router import resolve_llm
        from app.core.streaming import StreamingAgent, ContentEvent, ToolResultEvent
        from app.core.tool_formats import build_tool_instruction, get_format_for_model
        from app.models.notifications import create_notification
        from app.models.chat import save_message, get_chat_history

        profile = get_character_profile(character_name)
        config = get_character_config(character_name)
        from app.models.account import get_user_name
        user_name = get_user_name() or ""

        # Character-Daten sammeln
        task = (profile.get("character_task", "") or "").strip()
        location_id = profile.get("current_location", "")
        location_name = get_location_name(location_id) if location_id else "Unbekannt"
        activity = profile.get("current_activity", "") or "Keine"
        feeling = profile.get("current_feeling", "") or "Neutral"
        now = datetime.now()
        time_of_day = now.strftime("%H:%M")

        # LLM und Tools erstellen (via Router, Task aus llm_task-Parameter,
        # default "thought"). Sub-Tasks wie "thought_greeting" fallen automatisch
        # auf "thought" zurueck wenn nicht explizit geroutet.
        _thought_inst = resolve_llm(llm_task, agent_name=character_name)
        llm = _thought_inst.create_llm() if _thought_inst else None
        if not llm:
            logger.warning("Kein LLM fuer %s (task=%s)", character_name, llm_task)
            return

        # Skills laden und Modus bestimmen
        sm = get_skill_manager()
        agent_tools = sm.get_agent_tools(character_name)

        # Tool-Whitelist: haeltert agent_tools auf erlaubte Namen.
        # Kommt bei Observer-Gedanken (z.B. Instagram-Reaction) zum Einsatz,
        # um Rollenkonfusion im Tool-LLM zu verhindern.
        if tool_whitelist:
            _allowed = set(tool_whitelist)
            _before = [t.name for t in agent_tools]
            agent_tools = [t for t in agent_tools if t.name in _allowed]
            logger.info("Tool-Whitelist aktiv fuer %s: %s von %s",
                        character_name, [t.name for t in agent_tools], _before)

        _tool_inst = resolve_llm("intent", agent_name=character_name) if agent_tools else None
        tool_llm = _tool_inst.create_llm() if _tool_inst else None

        # Fast-Modus: Tool-LLM uebernimmt den ganzen Gedanken (kein Dual-Pass).
        # Mode wird auf "single" forciert, damit das LLM die Tool-Calls
        # selbst schreibt statt auf ein nicht-existentes Tool-LLM zu warten.
        _is_fast = False
        if fast and tool_llm:
            llm = tool_llm
            tool_llm = None
            _is_fast = True
            logger.info("Fast-Modus: Tool-LLM uebernimmt Gedanken statt Chat-LLM")

        from app.core.dependencies import determine_mode
        mode = "single" if _is_fast else determine_mode(agent_tools, tool_llm, config)
        tools_dict = {}
        for t in agent_tools:
            _orig_func = t.func
            def _make_ctx_wrapper(fn, _agent=character_name, _uid=""):
                def wrapper(raw_input):
                    import json
                    ctx = {"input": raw_input, "agent_name": _agent, "user_id": _uid}
                    # JSON-Tool-Input mergen damit Felder direkt verfuegbar sind
                    if isinstance(raw_input, str) and raw_input.strip().startswith("{"):
                        try:
                            parsed = json.loads(raw_input)
                            if isinstance(parsed, dict):
                                for k, v in parsed.items():
                                    if k not in ("agent_name", "user_id"):
                                        ctx[k] = v
                        except Exception:
                            pass
                    return fn(json.dumps(ctx))
                return wrapper
            tools_dict[t.name] = _make_ctx_wrapper(_orig_func)

        # Tool-Format: auto-detect vom Router-Model
        tool_model_name = _tool_inst.model if _tool_inst else ""
        model_for_format = tool_model_name or (_thought_inst.model if _thought_inst else "")
        tool_format = get_format_for_model(model_for_format)

        # System-Prompt via zentralen Builder
        from app.core.system_prompt_builder import (
            build_system_prompt, THOUGHT_FULL, THOUGHT_REACTION)

        # Forced-Thought mit tool_whitelist → minimaler Prompt (kein Task,
        # keine Presence, keine strikten Regeln — nur Identity + Situation).
        # Normaler Thought → voller Prompt.
        if context_hint and tool_whitelist:
            _sections = THOUGHT_REACTION
        else:
            _sections = THOUGHT_FULL

        # Tools-Hint: Im Single-Modus muss das LLM die Tool-Calls SELBST schreiben.
        # Im Dual-Modus (rp_first) uebernimmt das Tool-LLM die Tool-Entscheidung.
        available_tool_names = [t.name for t in agent_tools] if agent_tools else []
        tools_hint = ""
        if available_tool_names and mode != "rp_first":
            # Single-Modus (inkl. fast=True): LLM muss Tool-Format kennen
            appearance = get_character_appearance(character_name)
            usage = sm.get_agent_usage_instructions(character_name, tool_format)
            tools_hint = (
                f"\n"
                + build_tool_instruction(
                    tool_format, agent_tools, appearance, usage,
                    model_name=tool_model_name,
                    is_roleplay=False)
            )

        system_prompt = build_system_prompt(character_name,
            sections=_sections,
            context_hint=context_hint,
            tool_whitelist=tool_whitelist,
            tools_hint=tools_hint)
        if context_hint:
            logger.info("Thought fuer %s mit context_hint: %s",
                        character_name, context_hint[:100])

        # arc_context fuer spaeteres Arc-Advancement (Zeile ~1214)
        _arc_context_loaded = False
        try:
            from app.core.story_engine import get_story_engine
            arc_context = get_story_engine().inject_arc_context(character_name) or ""
            _arc_context_loaded = True
        except Exception:
            arc_context = ""

        # Tool-System-Prompt fuer Tool-LLM (erweiterter Kontext fuer autonome Entscheidungen)
        # Im Gedanken-Modus muss das Tool-LLM den vollen Situationskontext kennen,
        # da es eigenstaendig entscheidet welche Tools aufgerufen werden.
        # Budget-System: Sektionen werden gekuerzt falls Token-Limit knapp.
        tool_system_content = ""
        if mode == "rp_first" and tools_dict and agent_tools:
            from app.core.system_prompt_builder import load_prompt_data, THOUGHT_FULL
            _td = load_prompt_data(character_name, THOUGHT_FULL)

            appearance = get_character_appearance(character_name)
            usage = sm.get_agent_usage_instructions(character_name, tool_format)
            _tool_fmt = tool_format
            from app.models.character_template import is_roleplay_character as _is_rp_pa
            tool_instr_block = build_tool_instruction(
                _tool_fmt, agent_tools, appearance, usage, model_name=tool_model_name,
                is_roleplay=_is_rp_pa(character_name))

            # Kontext-Sektionen mit Budget aufbauen (Prio-Reihenfolge)
            _ctx_parts = []
            # Prio 1: Essentials (immer)
            _ctx_parts.append(
                f"Character: {character_name}.\n"
                f"Aufgabe: {_td.get('task', '')}\n"
                f"Uhrzeit: {_td.get('time_of_day', '')}."
            )
            # Prio 2: Tool-Instruktionen (immer)
            _ctx_parts.append(tool_instr_block)
            # Prio 3: Aktuelle Situation
            _ctx_parts.append(
                f"Aktuelle Situation:\n"
                f"- Ort: {_td.get('location_name', 'Unbekannt')}\n"
                f"- Aktivitaet: {_td.get('activity', 'Keine')}\n"
                f"- Stimmung: {_td.get('feeling', 'Neutral')}"
            )
            # Prio 4: Assignments (max ~800 Zeichen)
            if _td.get("assignment_section"):
                _ctx_parts.append(_td["assignment_section"][:800])
            # Prio 5: Nearby Characters
            if _td.get("nearby_hint"):
                _ctx_parts.append(_td["nearby_hint"][:400])
            # Prio 6: Persoenlichkeit (gekuerzt)
            if _td.get("personality"):
                _ctx_parts.append(f"Persoenlichkeit: {_td['personality'][:400]}")
            # Prio 7: Memory (gekuerzt, max ~1200 Zeichen)
            if _td.get("memory_section"):
                _ctx_parts.append(_td["memory_section"][:1200])
            # Prio 8: Story Arc (gekuerzt, max ~800 Zeichen)
            if _td.get("arc_context"):
                _ctx_parts.append(_td["arc_context"][:800])

            # Abschluss: Instruktion
            _ctx_parts.append(
                f"Available tools: {', '.join(available_tool_names)}\n"
                f"Based on the task and current situation, decide which tools to call. "
                f"You may call MULTIPLE tools in a single response.\n"
                f"If there is nothing relevant to do, respond with SKIP.\n"
                f"IMPORTANT: When tool input is JSON, all field values must be plain natural text. "
                f"NEVER nest JSON objects or tool tags inside field values."
            )
            tool_system_content = "\n\n".join(_ctx_parts)

        # Tool-Kategorien
        _deferred_tools = set()
        _content_tools = set()
        if tools_dict:
            from app.core.dependencies import get_skill_manager
            sm = get_skill_manager()
            for _tname in tools_dict:
                _sk = sm.get_skill_by_name(_tname)
                if _sk and getattr(_sk, 'DEFERRED', False):
                    _deferred_tools.add(_tname)
                if _sk and getattr(_sk, 'CONTENT_TOOL', False):
                    _content_tools.add(_tname)

        # Reply-Forced-Thoughts (Cascade-Brake aktiv) bekommen max 1 Iteration:
        # genau 1 Tool-Call (= Reply), kein Tool-Loop. Sonst: bisherige Defaults.
        max_iter = 1 if (_is_fast or reply_only_to) else (2 if tools_dict else 1)
        agent = StreamingAgent(
            llm=llm,
            tool_format=tool_format,
            tools_dict=tools_dict,
            agent_name=character_name,
            max_iterations=max_iter,
            tool_llm=tool_llm,
            tool_system_content=tool_system_content,
            log_task="thought",
            deferred_tools=_deferred_tools,
            content_tools=_content_tools,
            mode=mode,
            # Bei tool_whitelist (z.B. forced_thought fuer Avatar-Eintritt):
            # Tool-Decision-Prompt minimieren — keine EXTRACTION/FALLBACK-MARKER,
            # nur Action-Mapping fuer die tatsaechlich erlaubten Tools.
            constrained_tools=bool(tool_whitelist))

        # Tool-Executor (synchrone Ausfuehrung in Thread).
        # Cascade-Brake: bei reply_only_to wird SendMessage/TalkTo an andere
        # Empfaenger geblockt — der Reply-Thought darf nur an den Sender zurueck.
        async def _tool_executor(tool_name, tool_input):
            if reply_only_to and tool_name in ("SendMessage", "TalkTo"):
                _blocked = _check_cascade_brake(tool_input, reply_only_to)
                if _blocked:
                    logger.info("Cascade-Brake: %s.%s an %s blockiert (reply_only_to=%s)",
                                character_name, tool_name, _blocked, reply_only_to)
                    return (f"Tool {tool_name} blockiert: dies ist ein Reply-Thought, "
                            f"Du darfst nur {reply_only_to} antworten — keine Nachrichten "
                            f"an andere ({_blocked}).")
            tool_func = tools_dict[tool_name]
            return await asyncio.to_thread(tool_func, tool_input)
        agent.tool_executor = _tool_executor

        # Letzte Chat-Messages laden (damit Tool-LLM und Chat-LLM
        # Versprechen, Anweisungen und Kontext aus dem letzten Gespraech kennen)
        _THOUGHT_HISTORY_WINDOW = 6
        recent_history = []
        try:
            # Partner-Name explizit setzen: User-Name (nicht aktiver Character,
            # da dieser wechseln kann waehrend der Gedanken-Call laeuft)
            from app.models.account import get_user_name as _get_uname
            _thought_partner = _get_uname() or ""
            full_history = get_chat_history(character_name, partner_name=_thought_partner)
            if full_history:
                recent_history = [
                    {"role": m["role"], "content": m["content"]}
                    for m in full_history[-_THOUGHT_HISTORY_WINDOW:]
                    if m.get("role") in ("user", "assistant") and m.get("content")
                ]
                logger.debug("Gedanken-History geladen: %d Messages", len(recent_history))
        except Exception as hist_err:
            logger.debug("History laden fehlgeschlagen: %s", hist_err)

        # Agent ausfuehren und Response sammeln
        full_response = ""
        had_notification_tool = False
        notification_tool_content = ""
        user_input = (
            f"Denke ueber deine Aufgabe nach und entscheide was du jetzt tun moechtest. "
            f"Nutze die passenden Tools um deine Aufgabe zu erfuellen."
        )

        logger.info("Starte Agent-Loop fuer %s (tools: %d, tool_llm: %s, history: %d)",
                    character_name, len(tools_dict), 'JA' if tool_llm else 'NEIN', len(recent_history))

        _tool_exec_counts = {}  # Tool-Ausfuehrungszaehler

        # Queue-Tracking: Gedanke als aktiv registrieren (wie Chat),
        # damit der Task im Queue-Panel sichtbar ist und GPU-Routing greift.
        from app.core.llm_queue import get_llm_queue
        _llm_queue = get_llm_queue()
        _llm_inst = _thought_inst
        _is_forced = bool(context_hint)
        _thought_label = (
            f"Forced Thought: {character_name}" if _is_forced
            else f"Thought: {character_name}"
        )
        _thought_task_id = await _llm_queue.register_chat_active_async(
            character_name, llm_instance=_llm_inst,
            task_type="thought", label=_thought_label)

        # Tool-Executor: Queue waehrend Tool-Ausfuehrung freigeben.
        # character_name/user_id werden bereits von _make_ctx_wrapper (Zeile ~778) injiziert.
        _thought_state = {"task_id": _thought_task_id}

        async def _tool_executor_queued(tool_name, tool_input):
            # Cascade-Brake (siehe sibling _tool_executor)
            if reply_only_to and tool_name in ("SendMessage", "TalkTo"):
                _blocked = _check_cascade_brake(tool_input, reply_only_to)
                if _blocked:
                    logger.info("Cascade-Brake: %s.%s an %s blockiert (reply_only_to=%s)",
                                character_name, tool_name, _blocked, reply_only_to)
                    return (f"Tool {tool_name} blockiert: dies ist ein Reply-Thought, "
                            f"Du darfst nur {reply_only_to} antworten — keine Nachrichten "
                            f"an andere ({_blocked}).")
            if _thought_state["task_id"]:
                _llm_queue.register_chat_done(_thought_state["task_id"])
                _thought_state["task_id"] = None
            try:
                tool_func = tools_dict[tool_name]
                return await asyncio.to_thread(tool_func, tool_input)
            finally:
                _thought_state["task_id"] = await _llm_queue.register_chat_active_async(
                    character_name, llm_instance=_llm_inst,
                    task_type="thought", label=_thought_label)
        agent.tool_executor = _tool_executor_queued

        try:
            async for event in agent.stream(system_prompt, recent_history, user_input):
                if isinstance(event, ContentEvent):
                    full_response += event.content
                elif isinstance(event, ToolResultEvent):
                    _tool_exec_counts[event.tool_name] = _tool_exec_counts.get(event.tool_name, 0) + 1
                    if event.tool_name == "SendNotification":
                        had_notification_tool = True
                        notification_tool_content = event.result
                    logger.debug("Tool-Result: %s -> %s", event.tool_name, event.result[:100])
        finally:
            if _thought_state["task_id"]:
                _llm_queue.register_chat_done(_thought_state["task_id"])
                _thought_state["task_id"] = None

        # Ergebnis verarbeiten
        full_response = full_response.strip()

        # Narrativ beschriebene Tool-Calls erkennen und ausfuehren BEVOR
        # die Halluzinations-Bereinigung den Text entfernt.
        # Chat-LLM schreibt z.B. "[ImageGeneration: prompt text]" als Text
        # wenn das Tool-LLM versagt hat → Tool tatsaechlich ausfuehren.
        _narrative_exec_counts = {}
        try:
            if full_response and tools_dict:
                _narrative_pattern = re.findall(
                    r'\[(\w+):\s*([^\]]+)\]', full_response
                )
                for _tn, _tinput in _narrative_pattern:
                    # Nur ausfuehren wenn das Tool nicht bereits echt ausgefuehrt wurde
                    if _tn in tools_dict and _tn not in _tool_exec_counts:
                        logger.info("%s: Narrativer Tool-Call erkannt: %s -> fuehre aus",
                                    character_name, _tn)
                        try:
                            _tool_func = tools_dict[_tn]
                            _tool_result = await asyncio.to_thread(_tool_func, _tinput.strip())
                            _narrative_exec_counts[_tn] = _narrative_exec_counts.get(_tn, 0) + 1
                            logger.info("%s: Narrativer Tool-Call %s ausgefuehrt: %s",
                                        character_name, _tn, str(_tool_result)[:100])
                        except Exception as _te:
                            logger.error("%s: Narrativer Tool-Call %s Fehler: %s",
                                         character_name, _tn, _te)
        except Exception as _nte:
            logger.debug("Narrative tool-call execution error: %s", _nte)

        # Halluzinierte Tool-Calls aus der Antwort entfernen
        if full_response and available_tool_names:
            cleaned = self._clean_hallucinated_tools(full_response, available_tool_names)
            if cleaned != full_response:
                logger.info("%s: Halluzinierte Tool-Calls entfernt (%d -> %d Zeichen)",
                            character_name, len(full_response), len(cleaned))
                full_response = cleaned

        # LLM-Logging erfolgt per-Iteration im StreamingAgent

        # State-Marker extrahieren (Location, Activity, Mood, Assignments)
        # Gleiche Logik wie im regulaeren Chat — damit Ortswechsel, Outfit-Reset
        # und Activity-Updates auch im Gedanken-Modus funktionieren.
        if full_response and full_response.strip().upper() != "SKIP":
            try:
                from app.core.chat_engine import post_process_response
                _pp_result = post_process_response(
                    owner_id="",
                    character_name=character_name,
                    user_input=user_input,
                    full_response=full_response,
                    agent_config=config,
                    llm=llm,
                    user_display_name=user_name,
                    full_chat_history=recent_history,
                    old_history=[],  # Gedanken: kein Summary-Update noetig
                    extraction_context={"source": "thought", "is_background": True},
                )
                if _pp_result.get("location"):
                    logger.info("%s gedanke: Location -> %s", character_name, _pp_result["location"])
                if _pp_result.get("activity"):
                    logger.info("%s gedanke: Activity -> %s", character_name, _pp_result["activity"])
                if _pp_result.get("mood"):
                    logger.info("%s gedanke: Mood -> %s", character_name, _pp_result["mood"])
            except Exception as pp_err:
                logger.error("%s: post_process_response Fehler: %s", character_name, pp_err)

        # Auto-Progress: Tool-Ausfuehrungen als Assignment-Fortschritt zaehlen
        try:
            from app.models.assignments import auto_track_progress, TOOL_NAME_MAP

            # 1. Echte Tool-Calls aus dem Stream
            for _tn, _tc in _tool_exec_counts.items():
                _tool_type = TOOL_NAME_MAP.get(_tn)
                if _tool_type:
                    _atp = auto_track_progress(character_name, _tool_type, _tc)
                    if _atp:
                        logger.info("%s: Assignment auto-progress: %s +%d (%s)%s",
                                    character_name, _atp.get("title"), _tc, _tn,
                                    " -> COMPLETED" if _atp.get("completed") else "")

            # 2. Narrative Tool-Calls (oben bereits ausgefuehrt) als Fortschritt zaehlen
            for _tn, _tc in _narrative_exec_counts.items():
                _tool_type = TOOL_NAME_MAP.get(_tn)
                if _tool_type:
                    _atp = auto_track_progress(character_name, _tool_type, _tc)
                    if _atp:
                        logger.info("%s: Assignment auto-progress (narrativ): %s +%d (%s)%s",
                                    character_name, _atp.get("title"), _tc, _tn,
                                    " -> COMPLETED" if _atp.get("completed") else "")
        except Exception as _ate:
            logger.debug("Assignment auto-progress error: %s", _ate)

        if "SKIP" in full_response and not had_notification_tool:
            logger.info("%s: SKIP (nichts zu melden)", character_name)
            return

        # Suppress-Notification: nur Tool-Effekte behalten, Narrativ-Text
        # wird verworfen. Fuer Observer-Gedanken (Instagram-Reaction etc.)
        # wo der Context-Hint fremden Inhalt traegt, der nicht als Kiras
        # Gedanke in die Chat-History leaken darf.
        if suppress_notification:
            logger.info("%s: suppress_notification=True — "
                        "Narrativ wird verworfen, nur Tool-Effekte bleiben",
                        character_name)
            return

        # Notification-Inhalt bestimmen.
        # Konzept-Aenderung: Notifications sind jetzt nur noch fuer System-
        # Events (Random Events, Scheduler, Welt-Updates). Charakter-Gedanken
        # erzeugen KEINE Notifications mehr automatisch — wenn der Character
        # den User proaktiv ansprechen will, muss er explizit das Tool
        # SendMessage benutzen (das laeuft als Chat-Nachricht in die Chat-
        # Historie und triggert den Chat-Unread-Indikator).
        # Frueher: jeder Thought-Output >10 Zeichen wurde als Notification
        # gespeichert -> Notification-Spam, Inhalt nicht im Chat-Kontext.
        notification_content = ""
        if had_notification_tool:
            # Nur wenn der Character explizit SendNotification (nur fuer
            # System-zugewiesene Tasks aktiviert) gerufen hat — Inhalt fuer
            # optionale Telegram-Weiterleitung sammeln.
            if full_response:
                notification_content = full_response
            elif notification_tool_content:
                _prefix = "Notification erfolgreich gesendet: "
                if notification_tool_content.startswith(_prefix):
                    notification_content = notification_tool_content[len(_prefix):]
                else:
                    notification_content = notification_tool_content
            logger.info("%s: Notification via SendNotification Skill erstellt", character_name)
        else:
            # Narrativer Thought-Output ohne Tool — wird verworfen. Wenn der
            # Character was sagen wollte, haette er SendMessage rufen muessen.
            if full_response and len(full_response) > 10:
                logger.info("%s: Thought-Narrativ verworfen (%d Zeichen) — keine SendMessage genutzt",
                            character_name, len(full_response))
            else:
                logger.info("%s: Keine verwertbare Antwort", character_name)

        # Gedanken-Nachricht an Telegram senden (wenn Character einen Bot hat)
        if notification_content:
            try:
                await self._send_to_telegram(character_name, notification_content)
            except Exception as tg_err:
                logger.debug("Telegram thought send error: %s", tg_err)

        # In Chat-History speichern, damit der Character sich spaeter erinnern kann
        if notification_content:
            try:
                # Halluzinierte Tool-Tags und Intent-Tags bereinigen bevor gespeichert wird
                from app.routes.chat import _strip_tool_hallucinations
                from app.core.intent_engine import strip_intent_tags
                clean_content = _strip_tool_hallucinations(notification_content)
                clean_content = strip_intent_tags(clean_content)
                if not clean_content or clean_content.strip().upper() == "SKIP":
                    logger.info("%s: Gedanken-Nachricht nach Bereinigung leer/SKIP — nicht gespeichert", character_name)
                    return
                ts = datetime.now()
                date_str = ts.strftime("%d.%m.%Y %H:%M")
                from app.models.account import get_user_name as _get_un_save
                save_message({
                    "role": "assistant",
                    "content": f"[Gedanken-Nachricht | {location_name} | {date_str}] {clean_content}",
                    "timestamp": ts.isoformat(),
                }, character_name, partner_name=_get_un_save() or "")
                logger.info("%s: In Chat-History gespeichert", character_name)

            except Exception as e:
                logger.error("Chat-History Fehler: %s", e)

        # Cross-Memory entfaellt: wenn der Character will dass andere etwas
        # erfahren, soll er sie ueber TalkTo / SendMessage selbst kontaktieren —
        # nicht per "telepathischer" LLM-Analyse.

        # Story Arc Advancement triggern wenn Arc-Teilnehmer interagiert hat
        if arc_context and (full_response or had_notification_tool):
            try:
                from app.models.story_arcs import get_active_arcs
                from app.core.background_queue import get_background_queue
                active_arcs = get_active_arcs(character_name)
                for arc in active_arcs:
                    get_background_queue().submit("story_arc_advance", {
                        "user_id": "",
                        "arc_id": arc["id"],
                        "interaction_summary": (full_response or notification_content)[:300],
                    })
                    logger.debug("Arc-Advancement getriggert: %s", arc["id"])
            except Exception as e:
                logger.debug("Arc-Advancement Fehler: %s", e)

        # Intent-Extraktion aus Gedanken-Antwort
        if full_response or notification_content:
            try:
                from app.core.intent_engine import process_response_intents
                process_response_intents(
                    full_response or notification_content, character_name, config, self._scheduler
                )
            except Exception as e:
                logger.debug("Intent extraction error: %s", e)

        # Assignment-Marker-Extraktion aus Gedanken-Antwort
        if full_response or notification_content:
            try:
                from app.models.assignments import extract_assignment_markers
                extract_assignment_markers(character_name,
                    full_response or notification_content)
            except Exception as e:
                logger.debug("Assignment marker extraction error: %s", e)
