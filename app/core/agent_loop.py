"""Continuous AgentLoop — replaces the old probabilistic ThoughtRunner tick.

Picks the next agent via weighted round-robin (importance 1=Low, 2=Medium,
3=High → 1/2/3 tickets per agent, reshuffled each round). Runs one thought
turn at a time (LLM/GPU is the bottleneck). Sleeping characters and the
user-controlled avatar are excluded.

Eligibility (per turn):
    - thoughts_enabled feature is true for the character
    - character is not currently sleeping
    - character is not the user-controlled avatar
    - global pause is off (see _is_paused)

Pause source: shared with the existing TaskQueue admin pause for the
"default" queue. When that's paused, the AgentLoop sleeps too. Persistent
across restarts because the TaskQueue pause lives in the world DB.

Public API:
    get_agent_loop() -> AgentLoop
    AgentLoop.start() / stop() — bootstrap hooks
    AgentLoop.status() -> dict — current/recent/queue snapshot for admin

The forced_thought handler stays on ThoughtRunner (registered separately at
startup); this loop does not handle external triggers.
"""
import asyncio
import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.core.log import get_logger

logger = get_logger("agent_loop")


# Sleep when nothing is eligible (everyone sleeping, world paused, etc.)
_IDLE_SLEEP_SECONDS = 30
# Per-turn timeout — guards a hung LLM call from blocking the loop forever.
_TURN_TIMEOUT_SECONDS = 600
# Cap on importance (defensive — config could be junk).
_MIN_IMPORTANCE = 1
_MAX_IMPORTANCE = 3
# How many recent agent picks to keep for the admin status panel.
_RECENT_HISTORY = 20

# In-chat window: defines what counts as "currently chatting with avatar".
# < HOT_MIN: skip the turn entirely — the player is actively writing, the
#   character has nothing useful to offer mid-message.
# HOT_MIN .. WARM_MIN: use the trimmed in-chat template (focus stays on
#   the conversation, no random initiatives).
# > WARM_MIN: regular thought template.
_IN_CHAT_HOT_MIN = 10
_IN_CHAT_WARM_MIN = 30

# Pacing — vermeiden dass bei wenigen Charakteren der Loop zu eng taktet.
# Werte werden live aus der Welt-Config gelesen (Admin-Tab "Gedanken"):
#   thoughts.min_turn_gap_seconds        (default 30)
#   thoughts.min_per_char_cooldown_minutes (default 5)
# Beide Cooldowns gelten zusaetzlich zum Importance-Round-Robin und zu
# in-chat-skip / no_llm-Backoff.
_MIN_TURN_GAP_DEFAULT = 30
_MIN_PER_CHAR_COOLDOWN_MIN_DEFAULT = 5


def _get_min_turn_gap() -> int:
    """Read thoughts.min_turn_gap_seconds from config (live)."""
    try:
        from app.core import config as _cfg
        return int(_cfg.get("thoughts.min_turn_gap_seconds") or _MIN_TURN_GAP_DEFAULT)
    except Exception:
        return _MIN_TURN_GAP_DEFAULT


def _get_per_char_cooldown_min() -> int:
    """Read thoughts.min_per_char_cooldown_minutes from config (live)."""
    try:
        from app.core import config as _cfg
        return int(_cfg.get("thoughts.min_per_char_cooldown_minutes")
                   or _MIN_PER_CHAR_COOLDOWN_MIN_DEFAULT)
    except Exception:
        return _MIN_PER_CHAR_COOLDOWN_MIN_DEFAULT


class AgentLoop:
    """Asyncio task that ticks one agent thought turn at a time."""

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._tickets: List[str] = []
        # Priority bumps — characters that should think on the very next
        # available slot, ahead of the round-robin schedule. Used by
        # external triggers (avatar enters room, message received,
        # access-denied, etc.). FIFO; deduplicated.
        self._bump_queue: List[str] = []
        # Optional hints attached to a bump. Pop'd in _run_turn and passed
        # to run_thought_turn as context_hint so the agent sees a "you
        # planned to do X — decide now" prompt prefix. Multiple hints for
        # the same character accumulate (newline-joined).
        self._bump_hints: Dict[str, str] = {}
        # Per-character last-real-turn timestamp for cooldown enforcement.
        # Real = full LLM turn (not in_chat_skip / no_llm / error). Used to
        # skip the same char if they ran within _MIN_PER_CHAR_COOLDOWN_MIN.
        self._last_real_turn_at: Dict[str, datetime] = {}
        self._current_agent: str = ""
        self._recent: List[Dict[str, Any]] = []  # [{name, ts, action}]
        self._lock = asyncio.Lock()
        # Standby mode: set when no 'thought' LLM is reachable. Loop polls
        # availability on each idle tick instead of running turns.
        self._llm_standby: bool = False
        # Per-Character "im aktiven Chat" Cooldown. Wenn ein in_chat_skip
        # ausgeloest wurde, wird der Char bis zu diesem Zeitpunkt aus der
        # Eligibility ausgenommen — sonst spinnt der Loop in 100Hz auf ihn
        # und floodet das Log. Wird automatisch verworfen sobald die Zeit
        # erreicht ist.
        self._chat_skip_until: Dict[str, datetime] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            logger.debug("AgentLoop already running")
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run_forever())
        logger.info("AgentLoop gestartet")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("AgentLoop gestoppt")

    # ------------------------------------------------------------------
    # Status (admin panel)
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return {
            "running": self._task is not None and not self._stop.is_set(),
            "paused": _is_paused(),
            "standby": self._llm_standby,
            "current_agent": self._current_agent,
            "remaining_in_round": list(self._tickets),
            "bumped": list(self._bump_queue),
            "recent": list(self._recent),
        }

    def bump(self, character_name: str, hint: str = "") -> bool:
        """Mark a character for priority processing — they think next.

        Used by external triggers (avatar room entry, incoming message,
        access-denied, etc.) when the recipient should react sooner than
        their normal importance-quota would allow. Bumps stack FIFO and
        are deduplicated. Bumped characters skip the normal round-robin
        once; afterwards they fall back to importance scheduling.

        Optional ``hint`` is plaintext context that will be prepended to
        the next thought turn for this character (via run_thought_turn's
        context_hint parameter). Multiple hints accumulate. Use this to
        pass scheduler-style "you planned to send Kai a message — decide
        now whether to send it" prompts so the LLM can act, adjust, or
        skip on its own.

        Returns True if the bump was registered, False if the character
        is ineligible (sleeping / disabled / avatar / unknown).
        """
        if not character_name:
            return False
        if not _is_agent_eligible(character_name):
            logger.debug("AgentLoop.bump skipped: %s ineligible", character_name)
            return False
        if hint:
            existing = self._bump_hints.get(character_name, "")
            self._bump_hints[character_name] = (
                existing + "\n" + hint if existing else hint)
        if character_name in self._bump_queue:
            return True  # already bumped
        self._bump_queue.append(character_name)
        logger.info("AgentLoop.bump: %s queued for next slot%s",
                    character_name, " (with hint)" if hint else "")
        return True

    def pop_hint(self, character_name: str) -> str:
        """Pop accumulated hint text for the character. Returns empty string
        if there is none. Mutates internal state — caller must use the
        returned text in this turn or the hint is lost.
        """
        return self._bump_hints.pop(character_name, "")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _run_forever(self) -> None:
        # Brief delay so the rest of the server finishes wiring up before we
        # start firing thought turns.
        try:
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            return

        while not self._stop.is_set():
            try:
                if _is_paused():
                    await asyncio.sleep(_IDLE_SLEEP_SECONDS)
                    continue

                # Health gate: don't pick an agent if no 'thought' LLM is
                # reachable. Without this, the loop would burn through every
                # character in milliseconds (each turn early-returns "no_llm")
                # — flooding logs and blocking the admin UI you'd use to fix
                # the LLM config. State transitions are logged once.
                if not _thought_llm_available():
                    if not self._llm_standby:
                        logger.warning("AgentLoop standby: kein 'thought' LLM erreichbar — Loop pausiert")
                        self._llm_standby = True
                    await asyncio.sleep(_IDLE_SLEEP_SECONDS)
                    continue
                if self._llm_standby:
                    logger.info("AgentLoop resumed: 'thought' LLM wieder erreichbar")
                    self._llm_standby = False

                agent = self._pick_next_agent()
                if not agent:
                    await asyncio.sleep(_IDLE_SLEEP_SECONDS)
                    continue

                await self._run_turn(agent)

                # Back-off guard: if the last turn returned almost instantly
                # (no LLM, instant error) the loop would otherwise spin
                # through every character in milliseconds — saturating the
                # log and starving the rest of the server (incl. the admin
                # UI you'd use to fix the LLM config). Sleep when we detect
                # the symptom instead of trying to enumerate causes.
                last = self._recent[-1] if self._recent else None
                if last:
                    outcome_val = last.get("outcome")
                    if outcome_val == "in_chat_skip":
                        # in_chat_skip ist OK + schnell, aber wir brauchen einen
                        # minimalen Atemzug damit der Loop nicht in 100Hz andere
                        # eligible Chars durchmaht (oder bei nur einem Char in
                        # einem Hot-Spin haengt — der Per-Char-Cooldown faengt
                        # ihn ab, aber wir wollen auch keinen zu engen Tick).
                        await asyncio.sleep(2)
                        continue
                    bad_outcome = outcome_val in ("no_llm", "timeout") \
                        or str(outcome_val or "").startswith("error")
                    too_fast = last.get("duration_s", 0) < 1.0
                    if bad_outcome or too_fast:
                        await asyncio.sleep(_IDLE_SLEEP_SECONDS)
                        continue
                    # Echter Turn — globaler Min-Abstand zum naechsten,
                    # damit der Loop bei wenigen Charakteren nicht zu
                    # eng taktet. Wert kommt aus Admin-Config (Tab
                    # "Gedanken" → Min Turn Gap).
                    gap = _get_min_turn_gap()
                    if gap > 0:
                        await asyncio.sleep(gap)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("AgentLoop tick error: %s", e, exc_info=True)
                # Avoid hot-spinning on persistent errors.
                await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # Agent selection (weighted round-robin)
    # ------------------------------------------------------------------

    def _pick_next_agent(self) -> Optional[str]:
        """Pop the next agent from priority bumps OR the current round.

        Order:
          1. Priority bumps (FIFO) — external triggers wanting immediate attention
          2. Round-robin tickets — importance-weighted regular schedule
          3. Refill round and try again

        Agents that became ineligible (sleep, disabled, removed) are
        silently skipped. Per-Char-Cooldown wird ebenfalls hier gefiltert
        — ein Char der vor < _MIN_PER_CHAR_COOLDOWN_MIN einen echten Turn
        hatte wird uebersprungen. Bumps umgehen den Cooldown bewusst
        (externe Trigger wie Avatar-Roomentry sollen sofort wirken).
        """
        # 1) Bumped agents come first — Cooldown ignorieren (Bump = Prioritaet).
        while self._bump_queue:
            candidate = self._bump_queue.pop(0)
            if _is_agent_eligible(candidate):
                return candidate

        cooldown = timedelta(minutes=_get_per_char_cooldown_min())
        now = datetime.now()

        def _on_cooldown(name: str) -> bool:
            last = self._last_real_turn_at.get(name)
            if not last:
                return False
            return (now - last) < cooldown

        def _in_chat_skip(name: str) -> bool:
            until = self._chat_skip_until.get(name)
            if not until:
                return False
            if until <= now:
                # abgelaufen — Eintrag verwerfen damit das Dict nicht waechst
                self._chat_skip_until.pop(name, None)
                return False
            return True

        # 2) Current round.
        while self._tickets:
            candidate = self._tickets.pop(0)
            if not _is_agent_eligible(candidate):
                continue
            if _on_cooldown(candidate):
                continue  # naechstes Ticket — diesen Char ueberspringen
            if _in_chat_skip(candidate):
                continue  # Char ist gerade im Chat — kein Thought
            return candidate

        # 3) Refill round.
        self._tickets = _build_round_tickets()
        if not self._tickets:
            return None
        while self._tickets:
            candidate = self._tickets.pop(0)
            if not _is_agent_eligible(candidate):
                continue
            if _on_cooldown(candidate):
                continue
            if _in_chat_skip(candidate):
                continue
            return candidate
        return None

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------

    async def _run_turn(self, character_name: str) -> None:
        """Run a single thought turn for the given character."""
        async with self._lock:
            self._current_agent = character_name
            started_at = datetime.now()
            outcome = "ok"
            turn_info: Dict[str, Any] = {}

            try:
                from app.core.thought_context import build_thought_context
                from app.core.prompt_templates import render
                from app.core.thoughts import get_thought_runner
                from app.core.agent_inbox import mark_thought_processed

                # In-chat gating: HOT (<10min) skip, WARM (10-30min) use the
                # trimmed in-chat template, otherwise regular thought.
                chat_age_min = _minutes_since_last_chat_with_avatar(character_name)
                if chat_age_min is not None and chat_age_min < _IN_CHAT_HOT_MIN:
                    # Per-Char-Cooldown setzen: wenn der Chat noch HOT ist,
                    # erst nach dem fehlenden Restbetrag wieder eligible.
                    # Sonst spinnt der Loop diesen Char 100Hz an.
                    remaining_s = max(60.0,
                        (_IN_CHAT_HOT_MIN - chat_age_min) * 60.0)
                    self._chat_skip_until[character_name] = (
                        datetime.now() + timedelta(seconds=remaining_s))
                    logger.info(
                        "AgentLoop skip %s: in active chat (%.1f min ago) "
                        "— cooldown %.0fs",
                        character_name, chat_age_min, remaining_s)
                    outcome = "in_chat_skip"
                    turn_info = {"preview": f"in-chat skip ({chat_age_min:.1f}min)",
                                 "tools": [], "intents": []}
                    return

                # Deterministischer Auto-Sleep: bei Erschoepfung (stamina<10)
                # bringt der Loop den Char selbststaendig nach Hause / offmap,
                # ohne LLM-Thought zu konsultieren. Verhindert dass ein
                # exhausted Character ewig irgendwo am Map herumsteht weil das
                # LLM die "go home"-Anweisung nicht in einen Tool-Call umsetzt.
                _auto_sleep = self._maybe_auto_sleep(character_name)
                if _auto_sleep:
                    outcome = _auto_sleep["outcome"]
                    turn_info = {"preview": _auto_sleep["preview"],
                                 "tools": _auto_sleep.get("tools", []),
                                 "intents": []}
                    return

                template_name = "chat/agent_thought.md"
                if (chat_age_min is not None
                        and _IN_CHAT_HOT_MIN <= chat_age_min < _IN_CHAT_WARM_MIN):
                    template_name = "chat/agent_thought_in_chat.md"

                # Programmierter Walk-Step: ein Grid-Schritt Richtung
                # movement_target. LLM hat das Ziel vorher gewaehlt; der Tick
                # wandert es ohne LLM ab. Wird waehrend WARM-Window pausiert
                # (Character noch im Gespraech). Bei Ankunft loescht
                # save_character_current_location das Target automatisch.
                try:
                    if chat_age_min is None or chat_age_min >= _IN_CHAT_WARM_MIN:
                        from app.models.character import (
                            get_movement_target, clear_movement_target,
                            save_character_current_location)
                        from app.models.world import next_step_toward
                        target = get_movement_target(character_name)
                        if target:
                            next_loc = next_step_toward(character_name, target)
                            if next_loc is None:
                                clear_movement_target(character_name)
                                logger.info(
                                    "Movement-Target %s fuer %s nicht erreichbar — Target geloescht",
                                    target, character_name)
                                try:
                                    from app.models.character import _record_state_change
                                    from app.models.world import get_location_name
                                    _name = get_location_name(target) or target
                                    _record_state_change(
                                        character_name, "travel_failed", _name,
                                        metadata={"location_id": target,
                                                  "reason": "path_lost_in_transit"})
                                except Exception:
                                    pass
                            else:
                                save_character_current_location(
                                    character_name, next_loc,
                                    _preserve_movement_target=True)
                                logger.info(
                                    "AgentLoop walk: %s -> %s (Ziel: %s)",
                                    character_name, next_loc, target)
                except Exception as _we:
                    logger.debug("Walk-Step fuer %s fehlgeschlagen: %s",
                                 character_name, _we)

                # Discovery-Check: vor dem Thought-Build, damit der entdeckte
                # Ort sofort im list_locations_for_character-Kontext auftaucht
                # und der Character im aktuellen Tick darueber nachdenken kann.
                try:
                    from app.models.rules import check_discover_rules
                    check_discover_rules(character_name)
                except Exception as _de:
                    logger.debug("Discover-Check fuer %s fehlgeschlagen: %s",
                                 character_name, _de)

                ctx = build_thought_context(character_name)
                system_prompt = render(template_name, **ctx)

                thought_loop = get_thought_runner()
                if thought_loop is None:
                    logger.warning("ThoughtRunner instance missing — cannot run turn for %s",
                                   character_name)
                    outcome = "no_thought_runner"
                    return

                # Pop bump-hint (e.g. "scheduled message: …") and forward
                # it to the thought turn so the LLM sees the trigger.
                hint = self.pop_hint(character_name)

                try:
                    result = await asyncio.wait_for(
                        thought_loop.run_thought_turn(
                            character_name,
                            context_hint=hint,
                            system_prompt_override=system_prompt),
                        timeout=_TURN_TIMEOUT_SECONDS)
                    if isinstance(result, dict):
                        turn_info = result
                        if turn_info.get("status") == "no_llm":
                            outcome = "no_llm"
                except asyncio.TimeoutError:
                    logger.error("AgentLoop turn TIMEOUT (%ds) for %s",
                                 _TURN_TIMEOUT_SECONDS, character_name)
                    outcome = "timeout"

                # Mark inbox as processed regardless of outcome — even if the
                # agent ignored unread messages, we don't want them to pile
                # up indefinitely on every future turn.
                mark_thought_processed(character_name)

            except Exception as e:
                logger.error("AgentLoop turn error for %s: %s",
                             character_name, e, exc_info=True)
                outcome = f"error: {type(e).__name__}"
            finally:
                self._record_turn(character_name, started_at, outcome, turn_info)
                self._current_agent = ""

    def _maybe_auto_sleep(self, character_name: str) -> Optional[Dict[str, Any]]:
        """Bei Erschoepfung (stamina<10) den Char autonom nach Hause schicken.

        Drei Pfade:
          1. Char hat home_location=__offmap__ → enter_offmap_sleep direkt
          2. Char ist bereits am home_location → SetActivity Sleeping
          3. Char ist anderswo → SetLocation home (Walk oder Direct-Move)

        Returns dict {outcome, preview, tools} bei Aktion, sonst None.
        Cooldown via _chat_skip_until — ein erschoepfter Char wird nicht in
        jedem Tick neu geprueft, sonst spinnt der Loop.
        """
        try:
            from app.models.character import (
                get_character_profile, get_character_config,
                get_character_current_location, OFFMAP_SLEEP_SENTINEL,
                enter_offmap_sleep, save_character_current_activity,
                set_movement_target)
            profile = get_character_profile(character_name) or {}
            stamina = (profile.get("status_effects") or {}).get("stamina")
            if stamina is None or stamina >= 10:
                return None  # nicht erschoepft

            cfg = get_character_config(character_name) or {}
            home_loc = (cfg.get("home_location") or "").strip()
            if not home_loc:
                return None  # kein home_location -> wir koennen nichts tun

            cur_loc = (get_character_current_location(character_name) or "").strip()
            already_offmap = not cur_loc

            # Kein Cooldown via _chat_skip_until — sonst koennte der Char
            # zwischen den Walk-Steps nicht erneut gepickt werden und
            # wuerde nur alle 5 min einen Schritt machen. Die maybe_auto_sleep-
            # Logik ist idempotent: bereits-zuhause → SetActivity Sleep, en
            # route → naechster Schritt, offmap → continue. Jeder Tick
            # bringt den Char einen Step naeher.

            # Pfad 1: home ist offmap
            if home_loc == OFFMAP_SLEEP_SENTINEL:
                if already_offmap:
                    save_character_current_activity(character_name, "Sleeping")
                    logger.info("Auto-Sleep: %s bereits offmap, Activity=Sleeping",
                                character_name)
                    return {"outcome": "auto_sleep_offmap_continue",
                            "preview": f"already offmap, sleeping (stamina={stamina})",
                            "tools": ["SetActivity"]}
                if enter_offmap_sleep(character_name):
                    save_character_current_activity(character_name, "Sleeping")
                    logger.info("Auto-Sleep: %s erschoepft (stamina=%s) -> offmap",
                                character_name, stamina)
                    return {"outcome": "auto_sleep_offmap",
                            "preview": f"exhausted (stamina={stamina}) → offmap sleep",
                            "tools": ["SetLocation", "SetActivity"]}

            # Pfad 2/3: home ist eine reguläre Location
            if cur_loc == home_loc:
                # Schon zuhause — Activity auf Sleeping setzen
                save_character_current_activity(character_name, "Sleeping")
                logger.info("Auto-Sleep: %s zuhause, Activity=Sleeping",
                            character_name)
                return {"outcome": "auto_sleep_at_home",
                        "preview": f"home & exhausted (stamina={stamina}) → sleeping",
                        "tools": ["SetActivity"]}

            # Anderswo — movement_target setzen UND sofort den ersten
            # Schritt ausfuehren. Der Walk-Step im normalen Tick-Flow wird
            # nicht erreicht weil _maybe_auto_sleep frueh returnt; ohne den
            # Inline-Step wuerde der Char waehrend der naechsten 5min
            # (_chat_skip_until-Cooldown) gar nicht laufen.
            set_movement_target(character_name, home_loc)
            steps_taken = 0
            arrived = False
            try:
                from app.models.character import (
                    save_character_current_location,
                    clear_movement_target)
                from app.models.world import next_step_toward
                next_loc = next_step_toward(character_name, home_loc)
                if next_loc is None:
                    # Pfad nicht erreichbar — Target loeschen, sonst haengt
                    # der Char ewig im "walking home"-State.
                    clear_movement_target(character_name)
                    logger.warning(
                        "Auto-Sleep walk: kein Pfad von %s nach %s — Target geloescht",
                        character_name, home_loc)
                else:
                    save_character_current_location(
                        character_name, next_loc, _preserve_movement_target=True)
                    steps_taken = 1
                    arrived = (next_loc == home_loc)
                    logger.info(
                        "Auto-Sleep walk: %s -> %s (Ziel: %s)%s",
                        character_name, next_loc, home_loc,
                        " — angekommen" if arrived else "")
            except Exception as _we:
                logger.debug("Auto-Sleep walk-step fehlgeschlagen: %s", _we)

            # Wenn beim ersten Step schon angekommen: Activity sofort auf
            # Sleeping setzen, sonst kommt Char zwar an, ist aber wach.
            if arrived:
                save_character_current_activity(character_name, "Sleeping")
                logger.info("Auto-Sleep: %s zuhause angekommen, Activity=Sleeping",
                            character_name)
                return {"outcome": "auto_sleep_arrived_home",
                        "preview": f"exhausted (stamina={stamina}) → arrived home → sleeping",
                        "tools": ["SetLocation", "SetActivity"]}

            logger.info("Auto-Sleep: %s erschoepft (stamina=%s) -> Reise nach Hause (%s) [Schritte: %d]",
                        character_name, stamina, home_loc, steps_taken)
            return {"outcome": "auto_sleep_walking_home",
                    "preview": f"exhausted (stamina={stamina}) → walking home (step {steps_taken})",
                    "tools": ["SetLocation"]}
        except Exception as e:
            logger.debug("_maybe_auto_sleep failed for %s: %s",
                         character_name, e)
            return None

    def _record_turn(self, name: str, started_at: datetime, outcome: str,
                     turn_info: Optional[Dict[str, Any]] = None) -> None:
        info = turn_info or {}
        self._recent.append({
            "agent": name,
            "started_at": started_at.isoformat(),
            "duration_s": round((datetime.now() - started_at).total_seconds(), 1),
            "outcome": outcome,
            "tools": list(info.get("tools") or []),
            "intents": list(info.get("intents") or []),
            "preview": str(info.get("preview") or ""),
        })
        if len(self._recent) > _RECENT_HISTORY:
            self._recent = self._recent[-_RECENT_HISTORY:]
        # Per-Char Cooldown nur fuer ECHTE Turns (LLM lief, hat Output
        # produziert oder Tools getriggert). Skips/Errors triggern den
        # Cooldown bewusst NICHT — sonst wuerde ein in_chat_skip einen
        # 5min-Block ausloesen, obwohl gar nichts passiert ist.
        is_real = outcome == "ok" or (outcome or "").startswith("ok")
        if is_real:
            self._last_real_turn_at[name] = datetime.now()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_paused() -> bool:
    """Global pause indicator. Mirrors the existing world-pause toggle so
    Admin/World-Dev pause buttons stop the AgentLoop too."""
    try:
        from app.core.task_queue import get_task_queue
        tq = get_task_queue()
        return bool(tq and tq._is_paused("default"))
    except Exception:
        return False


def _thought_llm_available() -> bool:
    """Probe whether the global 'thought' route resolves to a live provider.

    Per-character overrides are not considered — this is the cheap loop-wide
    gate. False positives (override exists but global down) just mean a
    handful of agents skip a round, which is acceptable.
    """
    try:
        from app.core.llm_router import resolve_llm
        return resolve_llm("thought") is not None
    except Exception:
        return False


def _minutes_since_last_chat_with_avatar(character_name: str) -> Optional[float]:
    """Returns minutes since this character's last chat message **with an
    avatar (player-controlled character)**, or None if there is no such
    message.

    Used to gate AgentLoop turns: if a chat is active right now, the
    character should either skip or run a trimmed in-chat template instead
    of pursuing unrelated initiatives.

    Wichtig: TalkTo NPC↔NPC-Nachrichten zaehlen NICHT als "in-chat" — der
    Skip soll nur bei aktiver Avatar↔Char-Konversation greifen. Frueher
    hat die Funktion blind den letzten chat_messages-Eintrag genommen,
    was Rosi vom Denken aussperrte sobald sie via TalkTo mit einem NPC
    sprach (0.5min ago = "in chat" → skip).

    Implementierung: alle aktuellen Avatare einsammeln (siehe
    ``account.get_all_avatars`` — multi-user, beruecksichtigt
    users.settings.active_character) und nur Nachrichten zaehlen wo der
    Partner ein Avatar ist.
    """
    try:
        from app.core.db import get_connection
        from app.models.account import get_all_avatars
        avatars = get_all_avatars() or set()
        # Char selbst raus (er ist nie sein eigener Avatar — und wenn doch,
        # wuerde der Loop ihn schon vorher als is_player_controlled skippen)
        avatars = {a for a in avatars if a and a != character_name}
        if not avatars:
            return None
        # Latest message wo character_name im Chat ist UND partner ein
        # Avatar — beide Storage-Richtungen (A,B)/(B,A) abdecken.
        placeholders = ",".join(["?"] * len(avatars))
        params = (
            list(avatars) + [character_name]   # Bedingung 1: char=Avatar AND partner=this
            + [character_name] + list(avatars) # Bedingung 2: char=this AND partner=Avatar
        )
        sql = (
            f"SELECT MAX(ts) FROM chat_messages WHERE "
            f"(character_name IN ({placeholders}) AND partner=?) "
            f"OR (character_name=? AND partner IN ({placeholders}))"
        )
        conn = get_connection()
        row = conn.execute(sql, params).fetchone()
        if not row or not row[0]:
            return None
        try:
            last = datetime.fromisoformat(row[0])
        except (ValueError, TypeError):
            return None
        delta = datetime.now() - last
        return delta.total_seconds() / 60.0
    except Exception as e:
        logger.debug("chat-age check failed for %s: %s", character_name, e)
        return None


def _is_agent_eligible(character_name: str) -> bool:
    """Check thoughts_enabled feature, sleep state, and avatar exclusion."""
    if not character_name:
        return False
    try:
        from app.models.account import is_player_controlled
        if is_player_controlled(character_name):
            return False
    except Exception:
        pass
    try:
        from app.models.character import is_character_sleeping, wake_from_offmap
        if is_character_sleeping(character_name):
            return False
        # Char nicht mehr im Schlaf-Slot, aber evtl. noch offmap-vergessen?
        # Lazy zurueckholen damit der Loop danach normal mit ihm arbeitet.
        wake_from_offmap(character_name)
    except Exception:
        pass
    try:
        from app.models.character_template import is_feature_enabled
        if not is_feature_enabled(character_name, "thoughts_enabled"):
            return False
    except Exception:
        return False
    return True


def _build_round_tickets() -> List[str]:
    """Fresh tickets list for one scheduling round.

    Each eligible character contributes ``importance`` tickets (1/2/3).
    The list is shuffled so order within a round varies, but the count
    guarantees High runs 3x as often as Low across rounds.
    """
    try:
        from app.models.character import (
            list_available_characters, get_character_config)
    except Exception as e:
        logger.error("AgentLoop: cannot list characters: %s", e)
        return []

    tickets: List[str] = []
    for name in list_available_characters():
        if not _is_agent_eligible(name):
            continue
        try:
            cfg = get_character_config(name)
            raw = cfg.get("importance", 1)
            try:
                weight = int(raw)
            except (TypeError, ValueError):
                weight = 1
            weight = max(_MIN_IMPORTANCE, min(_MAX_IMPORTANCE, weight))
        except Exception:
            weight = 1
        tickets.extend([name] * weight)

    random.shuffle(tickets)
    return tickets


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_agent_loop: Optional[AgentLoop] = None


def get_agent_loop() -> AgentLoop:
    global _agent_loop
    if _agent_loop is None:
        _agent_loop = AgentLoop()
    return _agent_loop
