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
        self._current_agent: str = ""
        self._recent: List[Dict[str, Any]] = []  # [{name, ts, action}]
        self._lock = asyncio.Lock()
        # Standby mode: set when no 'thought' LLM is reachable. Loop polls
        # availability on each idle tick instead of running turns.
        self._llm_standby: bool = False

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
                    # in_chat_skip is a healthy fast skip — don't penalize.
                    if outcome_val != "in_chat_skip":
                        bad_outcome = outcome_val in ("no_llm", "timeout") \
                            or str(outcome_val or "").startswith("error")
                        too_fast = last.get("duration_s", 0) < 1.0
                        if bad_outcome or too_fast:
                            await asyncio.sleep(_IDLE_SLEEP_SECONDS)
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
        silently skipped.
        """
        # 1) Bumped agents come first.
        while self._bump_queue:
            candidate = self._bump_queue.pop(0)
            if _is_agent_eligible(candidate):
                return candidate

        # 2) Current round.
        while self._tickets:
            candidate = self._tickets.pop(0)
            if _is_agent_eligible(candidate):
                return candidate

        # 3) Refill round.
        self._tickets = _build_round_tickets()
        if not self._tickets:
            return None
        while self._tickets:
            candidate = self._tickets.pop(0)
            if _is_agent_eligible(candidate):
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
                    logger.info("AgentLoop skip %s: in active chat (%.1f min ago)",
                                character_name, chat_age_min)
                    outcome = "in_chat_skip"
                    turn_info = {"preview": f"in-chat skip ({chat_age_min:.1f}min)",
                                 "tools": [], "intents": []}
                    return

                template_name = "chat/agent_thought.md"
                if (chat_age_min is not None
                        and _IN_CHAT_HOT_MIN <= chat_age_min < _IN_CHAT_WARM_MIN):
                    template_name = "chat/agent_thought_in_chat.md"

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
    """Returns minutes since the last chat message between this character
    and the player's avatar, or None if no such conversation exists.

    Used to gate AgentLoop turns: if a chat is active right now, the
    character should either skip or run a trimmed in-chat template instead
    of pursuing unrelated initiatives.
    """
    try:
        from app.models.account import get_active_character
        from app.core.db import get_connection
        avatar = (get_active_character() or "").strip()
        if not avatar:
            return None
        conn = get_connection()
        row = conn.execute(
            "SELECT MAX(ts) FROM chat_messages "
            "WHERE character_name=? AND partner=?",
            (character_name, avatar),
        ).fetchone()
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
        from app.models.character import is_character_sleeping
        if is_character_sleeping(character_name):
            return False
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
