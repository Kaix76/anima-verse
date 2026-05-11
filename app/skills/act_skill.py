"""Act Skill — Storyteller-driven action.

Replaces the previous Announce-Skill. The actor (avatar or NPC) performs a
concrete in-scene action; a Storyteller-LLM narrates the immediate
consequence and may resolve an active disruption/danger event.

Pipeline:
  1. Resolve actor + scope (here|location)
  2. Cooldown check (sender)
  3. Build scene context (location, room, NPCs at scope, active events)
  4. Call Storyteller-LLM via task=storyteller, template=storyteller_react
  5. Parse the storyteller's narration for [EVENT_RESOLVED:…]
  6. Hybrid validation:
       danger    → validate_solution (independent judge) — must agree
       disruption → trust storyteller — resolve directly
       ambient   → not resolvable
  7. On resolve: resolve_event + delete_rules_by_event + diary
  8. Memory fragments for NPCs in scope (action_witnessed:{actor})
  9. AgentLoop bump for NPCs with perception_template=perceive_action
 10. Action-log entry for the actor
 11. Diary entry for the actor

Input JSON (when called by tool-LLM):
  {"text": "...", "scope": "here|location"}
"""
import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .base import BaseSkill, ToolSpec

from app.core.log import get_logger
logger = get_logger("act")


SENDER_COOLDOWN_MIN = 2
RECIPIENT_DEDUP_MIN = 30
RECIPIENT_CAP = 30

_EVENT_RESOLVED_RE = re.compile(r'\[EVENT_RESOLVED:\s*([^\]]+)\]', re.IGNORECASE)


class ActSkill(BaseSkill):
    """Concrete in-scene action witnessed by everyone in scope.

    Storyteller-LLM narrates consequence; active events may be resolved
    if the narration includes an [EVENT_RESOLVED:…] marker (gated by
    category — danger needs independent validator agreement).
    """

    SKILL_ID = "act"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        from app.core.prompt_templates import load_skill_meta
        meta = load_skill_meta("act")
        self.name = meta["name"]
        self.description = meta["description"]
        self._defaults = {"enabled": True}
        logger.info("Act Skill initialized")

    def execute(self, raw_input: str) -> str:
        if not self.enabled:
            return "Act Skill is disabled."

        ctx = self._parse_base_input(raw_input)
        actor = (ctx.get("agent_name") or "").strip()
        if not actor:
            return "Error: actor context missing."

        text, scope = _extract_text_and_scope(ctx)
        if not text:
            return "Error: empty action text."
        if scope not in ("here", "location"):
            scope = "here"

        if _sender_on_cooldown(actor, scope):
            return (f"You acted very recently — wait at least "
                    f"{SENDER_COOLDOWN_MIN} minutes before the next action.")

        # Sync entry point for tool-LLM callers — drive the async pipeline
        # via asyncio. Top-level routes use ``await perform_act(...)`` directly.
        import asyncio as _asyncio
        try:
            loop = _asyncio.get_event_loop()
            if loop.is_running():
                # Already inside an event loop — schedule and wait via thread.
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    fut = pool.submit(_asyncio.run, perform_act(actor, text, scope))
                    result = fut.result()
            else:
                result = _asyncio.run(perform_act(actor, text, scope))
        except RuntimeError:
            result = _asyncio.run(perform_act(actor, text, scope))
        return result.get("summary", "Action performed.")


    def get_usage_instructions(self, format_name: str = "", **kwargs) -> str:
        from app.core.tool_formats import format_example
        fmt = format_name or "tag"
        return format_example(
            fmt, self.name,
            '{"text": "draws her bow and scares off the wolves", "scope": "here"}')

    def as_tool(self, **kwargs) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                f"{self.description} "
                f"Input JSON: {{\"text\": \"<what you do>\", \"scope\": \"here\"|\"location\"}}."
            ),
            func=self.execute)


# ---------------------------------------------------------------------------
# Core pipeline — usable directly from API endpoints (avatar action route).
# ---------------------------------------------------------------------------


async def perform_act(actor: str, text: str, scope: str) -> Dict[str, Any]:
    """Run the full Storyteller pipeline for an action.

    Async — uses ``StreamingAgent`` so the configured chat mode (rp_first /
    single / no_tools) and the storyteller skill whitelist apply. Skills
    fire with ``agent_name=actor`` so effects (ChangeOutfit, ImageGenerator)
    land on the subject.

    Returns dict with keys:
      narration       — Storyteller text shown to the user
      resolved        — bool, True if an event was resolved
      event_id        — id of resolved event (or None)
      tools_fired     — list of tool names that fired during the action
      summary         — short status line
    """
    from app.models.character import (
        get_character_current_location, get_character_current_room)
    from app.models.world import (
        get_location_by_id, get_location_name, get_room_by_id)
    from app.models.events import list_events

    actor_loc = (get_character_current_location(actor) or "").strip()
    if not actor_loc:
        return {"narration": "", "resolved": False, "event_id": None,
                "tools_fired": [],
                "summary": "Action failed: no place to act in."}

    actor_room = (get_character_current_room(actor) or "").strip()
    location = get_location_by_id(actor_loc) or {}
    loc_name = get_location_name(actor_loc) or actor_loc
    room_name = ""
    if actor_room and location:
        room_obj = get_room_by_id(location, actor_room)
        if room_obj:
            room_name = room_obj.get("name", "") or ""

    # Recipients (other people at scope, excluding actor)
    recipients = resolve_recipients(scope, actor)

    # Active events at the actor's location
    active = list_events(location_id=actor_loc) or []

    # Storyteller-Agent (StreamingAgent w/ Storyteller-config + tools)
    narration, tools_fired = await _run_storyteller_agent(
        actor=actor, scope=scope, location_name=loc_name,
        room_name=room_name, location=location, active_events=active,
        recipients=recipients, user_action_text=text)

    # Extract [EVENT_RESOLVED:…] marker
    marker = ""
    m = _EVENT_RESOLVED_RE.search(narration or "")
    if m:
        marker = m.group(1).strip()
        # Strip marker from displayed narration
        narration = _EVENT_RESOLVED_RE.sub("", narration).strip()

    resolved_event_id = None
    resolved_flag = False
    if marker:
        resolved_flag, resolved_event_id = _try_resolve(
            actor=actor, location_id=actor_loc, active_events=active,
            marker_text=marker, user_text=text)

    # Side effects: action_log, NPC memory + bump, actor diary
    _log_action(
        actor=actor, scope=scope, location_id=actor_loc,
        room_id=actor_room, user_text=text, narration=narration,
        resolved=resolved_flag, event_id=resolved_event_id)

    _record_recipient_memories(
        actor=actor, narration=narration, recipients=recipients,
        scope=scope, text_for_dedup=text)

    _record_actor_diary(actor=actor, narration=narration, scope=scope)

    summary = _build_summary(
        recipients=recipients, resolved=resolved_flag,
        resolved_event_id=resolved_event_id,
        tools_fired=tools_fired)

    return {
        "narration": narration,
        "resolved": resolved_flag,
        "event_id": resolved_event_id,
        "tools_fired": tools_fired,
        "summary": summary,
    }


async def _run_storyteller_agent(
    actor: str, scope: str, location_name: str,
    room_name: str, location: Dict[str, Any],
    active_events: List[Dict[str, Any]],
    recipients: List[str], user_action_text: str
) -> Tuple[str, List[str]]:
    """Run the Storyteller via the same ``StreamingAgent`` infrastructure
    used by chat, but with the per-world storyteller config (chat mode,
    enabled skills).

    Returns (narration, tools_fired).
    """
    import json as _json
    from app.core.streaming import (
        StreamingAgent, ContentEvent, ToolStartEvent, ToolResultEvent,
        ToolErrorEvent, ToolEndEvent, HeartbeatEvent, RetryHintEvent,
        ExtractionEvent, DeferredToolEvent, LoopInfoEvent,
    )
    from app.core.llm_router import resolve_llm
    from app.core.prompt_templates import render_task
    from app.core.dependencies import get_skill_manager
    from app.core.tool_formats import get_format_for_model
    from app.models.storyteller import get_storyteller_config

    cfg = get_storyteller_config()
    chat_mode = cfg.get("chat_mode", "rp_first")
    llm_task = cfg.get("llm_task", "storyteller")
    enabled_skill_ids = {sid for sid, on
                          in (cfg.get("enabled_skills") or {}).items() if on}

    # ── Sprache ────────────────────────────────────────────────────────
    from app.models.account import get_user_profile
    _lang = (get_user_profile().get("system_language", "de") or "de")
    LANG_NAMES = {"de": "German", "en": "English", "fr": "French",
                  "es": "Spanish", "it": "Italian", "ja": "Japanese"}
    lang_name = LANG_NAMES.get(_lang, _lang)

    # ── Subject-Kontext ────────────────────────────────────────────────
    subject_profile = _short_subject_profile(actor)
    subject_outfit = _subject_outfit_text(actor)
    subject_mood = _subject_mood_text(actor)

    # ── Anwesende Personen mit Outfits ─────────────────────────────────
    present_people_block = _build_present_people_block(recipients[:RECIPIENT_CAP])

    # ── Active Events Block ────────────────────────────────────────────
    ev_lines = []
    for evt in active_events or []:
        if evt.get("resolved"):
            continue
        cat = (evt.get("category") or "").upper()
        ev_text = evt.get("text") or ""
        tag = f"[{cat}] " if cat else ""
        ev_lines.append(f"- {tag}{ev_text}")
    active_events_block = "\n".join(ev_lines)

    # ── Setting (Indoor/Outdoor) ───────────────────────────────────────
    indoor_flag = (location.get("indoor") or "").strip().lower() if location else ""
    if indoor_flag == "indoor":
        setting_block = ("Setting: Indoor (enclosed place — keep narration "
                          "coherent with an interior space).")
    elif indoor_flag == "outdoor":
        setting_block = ("Setting: Outdoor (open-air place — keep narration "
                          "coherent with an open natural environment).")
    else:
        setting_block = ""

    scope_label = "the whole place" if scope == "location" else "this room"

    # ── Template rendern ───────────────────────────────────────────────
    sys_prompt, user_prompt = render_task(
        "storyteller_react",
        subject_name=actor,
        subject_profile=subject_profile,
        subject_outfit=subject_outfit,
        subject_mood=subject_mood,
        location_name=location_name,
        room_name=room_name,
        scope_label=scope_label,
        setting_block=setting_block,
        active_events_block=active_events_block,
        present_people_block=present_people_block,
        user_action_text=user_action_text,
        language_name=lang_name)

    # ── LLMs aufloesen ─────────────────────────────────────────────────
    st_inst = resolve_llm(llm_task, agent_name=actor) \
        or resolve_llm("chat_stream", agent_name=actor)
    if st_inst is None:
        logger.error("Storyteller: no LLM available (task=%s, fallback chat_stream)",
                     llm_task)
        return ("", [])
    st_llm = st_inst.create_llm()

    tool_inst = resolve_llm("intent", agent_name=actor) or st_inst
    tool_llm = tool_inst.create_llm()

    tool_model_name = (tool_inst.model if tool_inst else "") or ""
    tool_format = get_format_for_model(tool_model_name) if tool_model_name else "tag"

    # ── Tools-Dict aus skill_manager, gefiltert per Storyteller-Config ─
    sm = get_skill_manager()
    tools_dict: Dict[str, Any] = {}
    _deferred_tools: set = set()
    _content_tools: set = set()
    if chat_mode != "no_tools" and enabled_skill_ids:
        for skill in sm.skills:
            sid = getattr(skill, "SKILL_ID", "")
            if not sid or sid not in enabled_skill_ids:
                continue
            # Per-Character-Limits ueberschreiben: Storyteller-Tools haben
            # ``skip_daily_limit=True`` (sonst greift z.B. das outfit-cap).
            t_spec = skill.as_tool(character_name=actor)
            t_name = t_spec.name
            t_orig = t_spec.func

            def _make_wrapper(fn, _agent=actor):
                def wrapper(raw_input):
                    ctx = {"input": raw_input, "agent_name": _agent,
                           "user_id": "", "skip_daily_limit": True}
                    if isinstance(raw_input, str) and raw_input.strip().startswith("{"):
                        try:
                            parsed = _json.loads(raw_input)
                            if isinstance(parsed, dict):
                                for k, v in parsed.items():
                                    if k not in ("agent_name", "user_id"):
                                        ctx[k] = v
                        except Exception:
                            pass
                    return fn(_json.dumps(ctx))
                return wrapper

            tools_dict[t_name] = _make_wrapper(t_orig)
            if getattr(skill, "DEFERRED", False):
                _deferred_tools.add(t_name)
            if getattr(skill, "CONTENT_TOOL", False):
                _content_tools.add(t_name)

    # ── Tool-System-Prompt (minimal, constrained-mode-Stil) ────────────
    if tools_dict:
        avail_names = ", ".join(tools_dict.keys())
        tool_system_content = (
            f"You decide which tools to call after a storyteller narration.\n"
            f"Available tools: {avail_names}\n"
            f"{actor} just performed an in-world action. The storyteller "
            f"narrated the consequence. Call the tools whose triggers match "
            f"either the action or the narration. If none apply, respond "
            f"with: NONE\n"
        )
    else:
        tool_system_content = ""

    agent = StreamingAgent(
        llm=st_llm,
        tool_llm=tool_llm,
        tool_format=tool_format,
        tools_dict=tools_dict,
        agent_name=actor,
        max_iterations=1 if chat_mode != "rp_first" else 2,
        tool_system_content=tool_system_content,
        log_task="storyteller",
        deferred_tools=_deferred_tools,
        content_tools=_content_tools,
        mode=chat_mode,
        constrained_tools=True)

    # ── Stream konsumieren ─────────────────────────────────────────────
    narration_chunks: List[str] = []
    tools_fired: List[str] = []
    try:
        async for event in agent.stream(sys_prompt, [], user_prompt):
            if isinstance(event, ContentEvent):
                narration_chunks.append(event.content or "")
            elif isinstance(event, ExtractionEvent):
                # Marker (EVENT_RESOLVED) an Narration anhaengen, damit der
                # spaetere _try_resolve den Marker findet, egal ob er aus
                # dem Storyteller-Output oder dem Tool-LLM-Pass kommt.
                if event.markers:
                    narration_chunks.append("\n" + event.markers)
            elif isinstance(event, ToolStartEvent):
                logger.info("Act tool start: %s", event.tool_name)
            elif isinstance(event, ToolResultEvent):
                if event.tool_name and event.tool_name not in tools_fired:
                    tools_fired.append(event.tool_name)
                logger.info("Act tool done: %s", event.tool_name)
            elif isinstance(event, ToolErrorEvent):
                logger.warning("Act tool error: %s — %s",
                                event.tool_name, event.error)
            elif isinstance(event, (HeartbeatEvent, ToolEndEvent,
                                     DeferredToolEvent, RetryHintEvent,
                                     LoopInfoEvent)):
                # nicht relevant fuer Narration
                pass
    except Exception as e:
        logger.error("Storyteller agent.stream failed: %s", e)

    narration = "".join(narration_chunks).strip()
    narration = re.sub(r"<SPECIAL_\d+>|<\|[A-Z_]+\|>", "", narration).strip()
    return narration, tools_fired


def _short_subject_profile(actor: str) -> str:
    """Brief trait hint for the storyteller's context."""
    try:
        from app.models.character import get_character_personality
        pers = get_character_personality(actor) or ""
        pers = pers.strip()
        if len(pers) > 200:
            pers = pers[:200].rsplit(" ", 1)[0] + "…"
        return pers
    except Exception:
        return ""


def _subject_outfit_text(actor: str) -> str:
    """Equipped outfit string (without the leading 'wearing: ' prefix
    duplication — the template already labels it)."""
    try:
        from app.models.character import build_equipped_outfit_prompt
        raw = build_equipped_outfit_prompt(actor) or ""
        return raw.strip()
    except Exception:
        return ""


def _subject_mood_text(actor: str) -> str:
    """Single-word mood hint (or empty)."""
    try:
        from app.models.character import get_character_current_feeling
        return (get_character_current_feeling(actor) or "").strip()
    except Exception:
        return ""


def _build_present_people_block(names: List[str]) -> str:
    """Multi-line bullet list with name + outfit per witnessing person."""
    if not names:
        return ""
    try:
        from app.models.character import build_equipped_outfit_prompt
    except Exception:
        return "\n".join(f"- {n}" for n in names)
    lines = []
    for n in names:
        try:
            outfit = (build_equipped_outfit_prompt(n) or "").strip()
        except Exception:
            outfit = ""
        if outfit:
            lines.append(f"- {n} — {outfit}")
        else:
            lines.append(f"- {n}")
    return "\n".join(lines)


def _try_resolve(actor: str, location_id: str,
                 active_events: List[Dict[str, Any]],
                 marker_text: str, user_text: str) -> Tuple[bool, Optional[str]]:
    """Apply hybrid resolution policy.

    danger    → second-pass validate_solution must agree
    disruption → trust storyteller marker, resolve directly
    ambient   → not resolvable

    Returns (resolved_bool, event_id_or_None).
    """
    from app.models.events import resolve_event, record_attempt
    from app.core.random_events import validate_solution, _on_resolution_cooldown

    # Pick the most recent unresolved actionable event (danger first, then disruption)
    candidates = [e for e in active_events
                  if e.get("category") in ("danger", "disruption")
                  and not e.get("resolved")
                  and not _on_resolution_cooldown(e)]
    if not candidates:
        return False, None
    # Danger before disruption, then newest first
    candidates.sort(key=lambda e: (
        0 if e.get("category") == "danger" else 1,
        e.get("created_at", "")), reverse=False)
    # Among same category, prefer newest
    pri = candidates[0].get("category")
    same_cat = [e for e in candidates if e.get("category") == pri]
    same_cat.sort(key=lambda e: e.get("created_at", ""), reverse=True)
    target = same_cat[0]
    cat = target.get("category", "")
    event_id = target.get("id", "")

    if cat == "ambient":
        return False, None

    if cat == "disruption":
        # Trust storyteller
        record_attempt(event_id, actor, user_text, outcome="success",
                       reason="storyteller-trusted")
        resolved = resolve_event(event_id, resolved_by=actor,
                                  resolved_text=marker_text or user_text)
        logger.info("Act: disruption event %s resolved by %s (trusted)", event_id, actor)
        try:
            from app.core.random_events import _diary_log_resolution
            _diary_log_resolution(actor, target, user_text, True)
        except Exception:
            pass
        return bool(resolved), event_id

    if cat == "danger":
        # Independent judge call
        val = validate_solution(target, user_text, actor)
        outcome = "success" if val.get("resolved") else "fail"
        record_attempt(event_id, actor, user_text,
                       outcome=outcome, reason=val.get("reason", ""))
        if val.get("resolved"):
            resolved = resolve_event(event_id, resolved_by=actor,
                                      resolved_text=marker_text or user_text)
            logger.info("Act: danger event %s resolved by %s (judge agreed)",
                        event_id, actor)
            try:
                from app.core.random_events import _diary_log_resolution
                _diary_log_resolution(actor, target, user_text, True)
            except Exception:
                pass
            return bool(resolved), event_id
        else:
            logger.info("Act: danger event %s judge declined: %s",
                        event_id, val.get("reason", ""))
            try:
                from app.core.random_events import _diary_log_resolution
                _diary_log_resolution(actor, target, user_text, False,
                                      reason=val.get("reason", ""))
            except Exception:
                pass
            return False, event_id

    return False, None


def _log_action(actor: str, scope: str, location_id: str, room_id: str,
                user_text: str, narration: str, resolved: bool,
                event_id: Optional[str]) -> None:
    """Persist into character_action_log."""
    try:
        from app.models.action_log import insert_action_log
        insert_action_log(
            character_name=actor, scope=scope,
            location_id=location_id, room_id=room_id,
            user_input=user_text, storyteller_response=narration,
            event_resolved=bool(resolved), event_id=event_id)
    except Exception as e:
        logger.debug("action_log insert failed: %s", e)


def _record_recipient_memories(actor: str, narration: str,
                                recipients: List[str], scope: str,
                                text_for_dedup: str) -> None:
    """Memory fragments for NPCs in scope + AgentLoop bump."""
    if not narration or not recipients:
        return
    for recipient in recipients[:RECIPIENT_CAP]:
        if _recipient_recently_perceived(recipient, actor, text_for_dedup):
            continue
        _record_perception(recipient, actor, narration, scope)
        _bump_with_perception(recipient, actor, narration, scope)


def _record_actor_diary(actor: str, narration: str, scope: str) -> None:
    """Sender's own memory of what they did."""
    if not narration:
        return
    try:
        from app.models.memory import add_memory
        scope_label = "the whole location" if scope == "location" else "this room"
        add_memory(
            actor,
            f"Acted before {scope_label}: {narration}",
            tags=["action_performed",
                  f"action_performed:{scope}"],
            importance=3)
    except Exception as e:
        logger.debug("Actor diary failed: %s", e)


def _build_summary(recipients: List[str], resolved: bool,
                   resolved_event_id: Optional[str],
                   tools_fired: Optional[List[str]] = None) -> str:
    parts = []
    if recipients:
        parts.append(f"{len(recipients)} present witnessed the action.")
    else:
        parts.append("Action performed; nobody was around to witness it.")
    if resolved and resolved_event_id:
        parts.append(f"An active event ({resolved_event_id}) was resolved.")
    if tools_fired:
        parts.append(f"Tools fired: {', '.join(tools_fired)}.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Recipient / cooldown / dedup helpers
# ---------------------------------------------------------------------------


def resolve_recipients(scope: str, actor: str) -> List[str]:
    """Return character names within the scope, excluding the actor.

    scope=here     -> same location AND same room as actor
    scope=location -> same location, all rooms
    """
    from app.models.character import (
        get_character_current_location,
        get_character_current_room)

    actor_loc = (get_character_current_location(actor) or "").strip()
    if not actor_loc:
        return []
    actor_room = (get_character_current_room(actor) or "").strip()

    if scope == "here":
        from app.core.room_entry import _list_characters_in_room
        return _list_characters_in_room(actor_loc, actor_room, exclude=actor)

    from app.models.character import list_available_characters
    out: List[str] = []
    for c in list_available_characters():
        if c == actor:
            continue
        if (get_character_current_location(c) or "").strip() == actor_loc:
            out.append(c)
    return out


def _sender_on_cooldown(actor: str, scope: str) -> bool:
    """True if the actor performed any action within the cooldown window."""
    try:
        from app.models.memory import load_memories
        cutoff = (datetime.now() - timedelta(minutes=SENDER_COOLDOWN_MIN)).isoformat()
        target_tag = f"action_performed:{scope}"
        for m in load_memories(actor):
            ts = m.get("timestamp") or ""
            if ts < cutoff:
                continue
            if target_tag in (m.get("tags") or []):
                return True
    except Exception as e:
        logger.debug("Sender cooldown check failed: %s", e)
    return False


def _recipient_recently_perceived(recipient: str, actor: str, text: str) -> bool:
    """True if this recipient already perceived a very similar action from this
    actor recently."""
    try:
        from app.models.memory import load_memories
        cutoff = (datetime.now() - timedelta(minutes=RECIPIENT_DEDUP_MIN)).isoformat()
        target_tag = f"action_witnessed:{actor}"
        text_norm = (text or "").strip().lower()[:80]
        if not text_norm:
            return False
        for m in load_memories(recipient):
            ts = m.get("timestamp") or ""
            if ts < cutoff:
                continue
            tags = m.get("tags") or []
            if target_tag not in tags:
                continue
            content = (m.get("content") or "").strip().lower()
            if text_norm in content:
                return True
    except Exception as e:
        logger.debug("Recipient dedup check failed: %s", e)
    return False


def _record_perception(recipient: str, actor: str, narration: str, scope: str) -> None:
    """Memory entry on the recipient — the Storyteller-narration is what
    they observed."""
    try:
        from app.models.memory import add_memory
        add_memory(
            recipient,
            f"Saw {actor} act: {narration}",
            tags=["action_witnessed",
                  f"action_witnessed:{actor}",
                  f"action_scope:{scope}"],
            importance=3,
            related_character=actor)
    except Exception as e:
        logger.debug("Recipient perception memory failed for %s: %s", recipient, e)


def _bump_with_perception(recipient: str, actor: str, narration: str, scope: str) -> None:
    """Queue the recipient for a focused perception turn on the next slot."""
    try:
        from app.core.agent_loop import get_agent_loop
        from app.models.relationship import get_relationship
    except Exception as e:
        logger.debug("Act bump imports failed: %s", e)
        return

    relationship_hint = ""
    try:
        rel = get_relationship(recipient, actor) or {}
        sentiment = rel.get("sentiment")
        strength = rel.get("strength")
        bits = []
        if isinstance(sentiment, str) and sentiment:
            bits.append(sentiment)
        if isinstance(strength, (int, float)):
            if strength >= 70:
                bits.append("close")
            elif strength <= 30:
                bits.append("distant")
        if bits:
            relationship_hint = ", ".join(bits)
    except Exception:
        pass

    actor_location_name = ""
    actor_room_name = ""
    try:
        from app.models.character import (
            get_character_current_location,
            get_character_current_room)
        from app.models.world import (
            get_location_name, get_location_by_id, get_room_by_id)

        actor_loc_id = (get_character_current_location(actor) or "").strip()
        if actor_loc_id:
            actor_location_name = get_location_name(actor_loc_id) or actor_loc_id
            actor_room_id = (get_character_current_room(actor) or "").strip()
            if actor_room_id:
                loc_obj = get_location_by_id(actor_loc_id)
                if loc_obj:
                    room_obj = get_room_by_id(loc_obj, actor_room_id)
                    if room_obj:
                        actor_room_name = room_obj.get("name", "") or ""
    except Exception as e:
        logger.debug("Act actor-location lookup failed: %s", e)

    perception_vars = {
        "action_actor": actor,
        "action_narration": narration,
        "action_scope": scope,
        "relationship_to_actor": relationship_hint,
        "action_actor_location": actor_location_name,
        "action_actor_room": actor_room_name,
    }

    try:
        get_agent_loop().bump(
            recipient,
            perception_template="tasks/perceive_action.md",
            perception_vars=perception_vars,
            tool_whitelist=["SetLocation"])
    except Exception as e:
        logger.debug("Act bump failed for %s: %s", recipient, e)


def _extract_text_and_scope(ctx: Dict[str, Any]) -> tuple:
    """Pull text + scope out of the JSON tool-input."""
    text = ""
    scope = "here"

    if isinstance(ctx.get("text"), str):
        text = ctx["text"].strip()
    if isinstance(ctx.get("scope"), str):
        scope = ctx["scope"].strip().lower() or "here"

    if text:
        return text, scope

    raw = ctx.get("input") or ""
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                text = (parsed.get("text") or "").strip()
                scope_v = (parsed.get("scope") or "").strip().lower()
                if scope_v:
                    scope = scope_v
        except Exception:
            pass

    if not text and isinstance(raw, str):
        text = raw.strip()

    return text, scope
