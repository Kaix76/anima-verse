"""Group Chat Routes — Multi-character conversations at the same location.

Mirrors the patterns from chat.py but iterates over multiple responders
per user message, streaming each character's response sequentially.
"""
import asyncio
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.log import get_logger
from app.core.chat_task_manager import get_chat_task_manager
from app.core.dependencies import get_skill_manager, determine_mode
from app.core.streaming import (
    StreamingAgent, ContentEvent, ToolStartEvent, ToolEndEvent,
    ToolResultEvent, HeartbeatEvent)
from app.core.turn_taking import calculate_response_scores
from app.models.character import (
    get_character_config,
    get_character_current_activity,
    get_character_language_instruction,
    get_character_profile)
from app.models.group_chat import (
    close_session,
    create_group_session,
    get_active_session,
    get_characters_at_location,
    get_group_chat_history,
    save_group_message)
from app.models.memory import upsert_relationship_memory
from app.models.relationship import record_interaction
from app.models.account import get_user_profile, get_active_character, get_player_identity
from app.models.world import (
    get_location_name,
    resolve_location)
from app.core.tts_service import ChunkedTTSHandler

logger = get_logger("group_chat")

router = APIRouter(prefix="/chat", tags=["group_chat"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _character_color(name: str) -> str:
    """Deterministic HSL color from character name (matches frontend hash)."""
    h = 0
    for c in (name or ""):
        h = ord(c) + ((h << 5) - h)
    hue = abs(h) % 360
    return f"hsl({hue}, 45%, 45%)"


def _build_group_system_prompt(character_name: str,
    participants: List[str],
    location_name: str,
    chat_context: List[Dict[str, Any]]) -> str:
    """Build system prompt for a character in a group conversation.

    Uses the standard _build_full_system_prompt and appends group context.
    """
    from app.routes.chat import _build_full_system_prompt
    from app.core.dependencies import get_skill_manager

    lang_instruction = get_character_language_instruction(character_name)
    agent_config = get_character_config(character_name)

    # Tools aus aktivierten Skills ableiten + Modus bestimmen
    from app.core.dependencies import determine_mode
    from app.core.llm_router import resolve_llm as _resolve_llm_tmp
    _agent_tools = get_skill_manager().get_agent_tools(character_name)
    _tool_inst_tmp = _resolve_llm_tmp("intent") if _agent_tools else None
    _tool_llm_tmp = _tool_inst_tmp.create_llm() if _tool_inst_tmp else None
    _mode = determine_mode(_agent_tools, _tool_llm_tmp, agent_config)
    _char_tools = _mode != "no_tools"
    base_prompt = _build_full_system_prompt(character_name, lang_instruction, "",
        tools_enabled=_char_tools, agent_config=agent_config,
        has_tool_llm=(_mode == "rp_first"),
        skip_partner=True,  # Teilnehmer kommen in die GROUP CONVERSATION Sektion
    )

    # Build participant info with relationship context + activity visibility
    from app.models.relationship import get_relationship
    from app.models.character import get_character_current_activity
    from app.core.activity_engine import resolve_activity_visibility
    user_name = get_player_identity("Player")

    # Alle anwesenden Personen auflisten (NPCs + Spieler-Avatar)
    all_present = list(participants)
    if user_name and user_name not in all_present:
        all_present.append(user_name)

    participant_lines = []
    for p in all_present:
        if p == character_name:
            continue
        rel = get_relationship(character_name, p)
        # Activity mit Visibility-Check (am selben Ort = immer sichtbar)
        p_activity = get_character_current_activity(p) or ""
        visible_activity = resolve_activity_visibility(p, character_name, p_activity, same_location=True) if p_activity else ""
        activity_info = f", doing: {visible_activity}" if visible_activity else ""
        is_player = (p == user_name)
        player_tag = ", you are talking to them" if is_player else ""

        if rel:
            rel_type = rel.get("type", "neutral")
            strength = rel.get("strength", 10)
            participant_lines.append(f"- {p} ({rel_type}, closeness {int(strength)}/100{activity_info}{player_tag})")
        else:
            suffix = f" ({visible_activity})" if visible_activity else ""
            participant_lines.append(f"- {p}{suffix}{' — ' + player_tag.strip(', ') if is_player else ''}")

    others_text = "\n".join(participant_lines) if participant_lines else "- (none)"

    # Recent conversation context (last few messages for awareness)
    # Only include messages from current participants (filter out departed characters)
    participant_set = {p.lower() for p in participants}
    recent_lines = []
    for msg in chat_context[-10:]:
        if msg["role"] == "user":
            recent_lines.append(f"{user_name}: {msg['content'][:200]}")
        elif msg.get("character"):
            if msg["character"].lower() in participant_set:
                recent_lines.append(f"{msg['character']}: {msg['content'][:200]}")
    context_text = "\n".join(recent_lines) if recent_lines else "(conversation just started)"

    # Build list of OTHER character names for the anti-puppeting rule
    other_names = [p for p in participants if p != character_name]
    other_names_str = ", ".join(other_names) if other_names else ""

    group_section = (
        f"\n\n--- GROUP CONVERSATION ---\n"
        f"You are in a group conversation at {location_name}.\n"
        f"Other people present:\n{others_text}\n\n"
        f"Recent conversation:\n{context_text}\n\n"
        f"CRITICAL RULES — YOU MUST FOLLOW ALL OF THESE:\n"
        f"1. You are {character_name}. Write ONLY your OWN response.\n"
        f"2. NEVER write dialogue or actions for other characters ({other_names_str}).\n"
        f"   Do NOT write lines like \"{other_names[0] if other_names else 'X'}: ...\" or "
        f"\"*{other_names[0] if other_names else 'X'} says...*\".\n"
        f"3. Do NOT narrate what others do, say, think, or feel.\n"
        f"4. Write ONLY as {character_name} — first person, your own words.\n"
        f"5. Keep it short (1-4 sentences). This is a group chat, not a monologue.\n"
        f"6. You may ADDRESS others by name (e.g. \"Hey {other_names[0] if other_names else 'X'}!\") "
        f"but never SPEAK FOR them.\n"
        f"7. Do NOT prefix your response with your own name. Just respond directly.\n"
        f"8. Do NOT use **I am at** tags (you stay at this location during a group chat).\n"
        f"   You MAY still use **I feel <emotion>** and **I do <activity>** tags if your mood or activity changes."
        + ("" if _char_tools else "\n9. Do NOT use [INTENT:] tags.")
    )

    return base_prompt + group_section


def _check_activity_discovery(all_chars: List[str], location_id: str):
    """Prueft ob Characters hidden Activities anderer an der Location entdecken.

    Wahrscheinlichkeits-basiert: discovery_difficulty vs. Zufall.
    Bei Entdeckung: on_discovered Trigger wird ausgefuehrt.
    """
    import random
    from app.models.character import get_character_current_activity
    from app.core.activity_engine import _find_activity_definition, execute_trigger

    for actor in all_chars:
        activity = get_character_current_activity(actor) or ""
        if not activity:
            continue

        sa = _find_activity_definition(actor, activity)
        if not sa:
            continue

        visibility = sa.get("visibility", "visible")
        if visibility == "visible":
            continue

        difficulty = sa.get("discovery_difficulty", 3)
        triggers = sa.get("triggers", {}) or {}
        on_discovered = triggers.get("on_discovered")

        # Jeder andere Character hat eine Chance zu entdecken
        for observer in all_chars:
            if observer == actor:
                continue

            # Observer-Aufmerksamkeit aus aktuellem Status-Effect (default 100)
            from app.models.character import get_character_profile as _gcp
            obs_status = _gcp(observer).get("status_effects", {})
            perception = int(obs_status.get("attention", obs_status.get("alertness", 100)))

            # Basis-Chance nach Schwierigkeit, modifiziert durch Perception
            # difficulty 1 = 40%, 2 = 25%, 3 = 15%, 4 = 8%, 5 = 3% (bei perception 50)
            base_chance = {1: 0.40, 2: 0.25, 3: 0.15, 4: 0.08, 5: 0.03}.get(difficulty, 0.10)
            # Perception-Modifier: 0 perception = 0.5x, 50 = 1.0x, 100 = 1.5x
            perception_mod = 0.5 + (perception / 100.0)
            chance = min(0.80, base_chance * perception_mod)

            if random.random() < chance:
                logger.info("DISCOVERY: %s hat bemerkt dass %s '%s' macht (was als '%s' getarnt war)",
                            observer, actor, activity, visibility)

                # on_discovered Trigger
                if on_discovered:
                    execute_trigger(actor, on_discovered, context={"observer": observer})

                # Memory fuer den Observer
                try:
                    from app.models.memory import add_memory
                    add_memory(
                        character_name=observer,
                        content=f"Habe bemerkt dass {actor} etwas Verdaechtiges tut: {activity}",
                        memory_type="episodic",
                        importance=3,
                        related_character=actor)
                except Exception:
                    pass

                break  # Nur ein Observer pro Runde entdeckt es


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/{user_id}/group/session")
async def get_group_session(location_id: str = "") -> Dict[str, Any]:
    """Get or create a group session for the user's current location."""
    # Resolve location
    if not location_id:
        user_profile = get_user_profile()
        location_id = user_profile.get("current_location", "")
    if not location_id:
        raise HTTPException(status_code=400, detail="No location set")

    loc = resolve_location(location_id)
    loc_name = loc.get("name", location_id) if loc else location_id
    loc_id = loc.get("id", location_id) if loc else location_id

    # Get characters at this location (Avatar rausfiltern — ist der User)
    _player_char = get_active_character()
    chars = get_characters_at_location(loc_id)
    npc_chars = [c for c in chars if c["name"] != _player_char]
    if len(npc_chars) < 2:
        raise HTTPException(
            status_code=400,
            detail="Mindestens 2 Charaktere muessen am selben Ort sein"
        )

    participant_names = [c["name"] for c in npc_chars]

    # Get or create session
    session = get_active_session(loc_id)
    if not session:
        session = create_group_session(loc_id, participant_names)
    else:
        # Update participants (characters may have moved)
        if set(session.get("participants", [])) != set(participant_names):
            session["participants"] = participant_names
            # Persist updated participants to storage
            from app.models.group_chat import load_sessions, save_sessions
            all_sessions = load_sessions()
            for s in all_sessions:
                if s["id"] == session["id"]:
                    s["participants"] = participant_names
                    break
            save_sessions(all_sessions)

    history = get_group_chat_history(session["id"], limit=30)

    return {
        "session_id": session["id"],
        "location_id": loc_id,
        "location_name": loc_name,
        "participants": npc_chars,
        "chat_history": history,
    }


@router.post("/{user_id}/group/reset")
async def reset_group_session(request: Request):
    """Close the current group session and create a fresh one."""
    data = await request.json()
    session_id = data.get("session_id", "")
    location_id = data.get("location_id", "")

    if session_id:
        close_session(session_id)

    if not location_id:
        user_profile = get_user_profile()
        location_id = user_profile.get("current_location", "")
    if not location_id:
        raise HTTPException(status_code=400, detail="No location set")

    loc = resolve_location(location_id)
    loc_id = loc.get("id", location_id) if loc else location_id
    loc_name = loc.get("name", location_id) if loc else location_id

    _player_char_r = get_active_character()
    chars = get_characters_at_location(loc_id)
    npc_chars = [c for c in chars if c["name"] != _player_char_r]
    if len(npc_chars) < 2:
        raise HTTPException(
            status_code=400,
            detail="Mindestens 2 Charaktere muessen am selben Ort sein"
        )

    participant_names = [c["name"] for c in npc_chars]
    session = create_group_session(loc_id, participant_names)
    history = get_group_chat_history(session["id"], limit=30)

    return {
        "session_id": session["id"],
        "location_id": loc_id,
        "location_name": loc_name,
        "participants": npc_chars,
        "chat_history": history,
    }


@router.post("/{user_id}/group")
async def group_chat(request: Request):
    """Send a message to the group chat. Returns task_id for SSE stream."""
    data = await request.json()
    user_message = data.get("message", "").strip()
    session_id = data.get("session_id", "")
    whisper_to = (data.get("whisper_to") or "").strip()

    # Dynamic turn-taking settings from frontend
    tt_settings = data.get("turn_taking", {})
    tt_threshold = tt_settings.get("threshold")
    tt_min = tt_settings.get("min_responders")
    tt_max = tt_settings.get("max_responders")

    if not user_message:
        raise HTTPException(status_code=400, detail="Message required")

    # Resolve session
    if not session_id:
        user_profile = get_user_profile()
        loc_id = user_profile.get("current_location", "")
        if loc_id:
            session = get_active_session(loc_id)
            if session:
                session_id = session["id"]

    if not session_id:
        raise HTTPException(status_code=400, detail="No active group session")

    # Load session data
    session = None
    from app.models.group_chat import load_sessions
    for s in load_sessions():
        if s["id"] == session_id:
            session = s
            break
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    location_id = session["location_id"]
    loc_name = get_location_name(location_id) or location_id

    # Refresh participants (characters may have moved)
    chars_at_loc = get_characters_at_location(location_id)
    # Spieler-Avatar aus der Teilnehmerliste filtern — der Avatar ist der User,
    # nicht ein NPC der "zuhoert" oder antwortet.
    _player_char = get_active_character()
    participant_names = [c["name"] for c in chars_at_loc if c["name"] != _player_char]
    if len(participant_names) < 1:
        raise HTTPException(status_code=400, detail="No characters at this location")

    # Persist updated participants if changed
    if set(session.get("participants", [])) != set(participant_names):
        from app.models.group_chat import save_sessions
        all_sessions = load_sessions()
        for s in all_sessions:
            if s["id"] == session_id:
                s["participants"] = participant_names
                break
        save_sessions(all_sessions)

    # Build participant info map (ohne Avatar)
    char_info = {c["name"]: c for c in chars_at_loc if c["name"] != _player_char}

    # Save user message (ggf. als Whisper)
    if whisper_to and whisper_to not in participant_names:
        whisper_to = ""  # Ungueltiges Ziel ignorieren
    save_group_message(session_id, "user", user_message, whisper_to=whisper_to)

    # Get chat context for turn-taking
    chat_context = get_group_chat_history(session_id, limit=20)

    # Calculate who responds (with optional dynamic overrides)
    tt_kwargs = {}
    if tt_threshold is not None:
        tt_kwargs["threshold"] = float(tt_threshold)
    if tt_min is not None:
        tt_kwargs["min_responders"] = int(tt_min)
    if tt_max is not None:
        tt_kwargs["max_responders"] = int(tt_max)
    if whisper_to:
        # Fluestern: nur der Ziel-Character antwortet, alle anderen passiv
        responders = [whisper_to]
        passive = [n for n in participant_names if n != whisper_to]
    else:
        responders, passive = calculate_response_scores(
            user_message, participant_names, chat_context, **tt_kwargs)

    user_display_name = get_player_identity("Player")

    async def generate():
        """Generate group chat responses — one character at a time."""

        # Background TTS queue: collects SSE strings from async TTS tasks
        _tts_queue: asyncio.Queue = asyncio.Queue()
        _tts_pending: list = []  # track background tasks for final drain

        async def _run_tts_background(tts_handler: ChunkedTTSHandler, text: str, char: str):
            """Generate TTS in background, push SSE strings into queue."""
            try:
                for sse in tts_handler.feed(text):
                    await _tts_queue.put(sse)
                for sse in await tts_handler.flush():
                    await _tts_queue.put(sse)
            except Exception as e:
                logger.error("Background TTS error for %s: %s", char, e)

        def _drain_tts_queue():
            """Yield any ready TTS SSE strings without blocking."""
            results = []
            while not _tts_queue.empty():
                try:
                    results.append(_tts_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            return results

        # Emit group_start
        yield f"data: {json.dumps({'group_start': {'participants': participant_names, 'responders': responders}})}\n\n"

        # Emit passive characters
        for p_name in passive:
            info = char_info.get(p_name, {})
            yield f"data: {json.dumps({'char_passive': {'character': p_name, 'avatar_url': info.get('avatar_url', ''), 'color': _character_color(p_name)}})}\n\n"

        # Build up conversation context as we go
        running_context = list(chat_context)

        for char_name in responders:
            # Drain any ready TTS chunks from previous characters
            for _sse in _drain_tts_queue():
                yield _sse

            info = char_info.get(char_name, {})
            color = _character_color(char_name)
            avatar_url = info.get("avatar_url", "")

            # Emit char_start
            yield f"data: {json.dumps({'char_start': {'character': char_name, 'avatar_url': avatar_url, 'color': color}})}\n\n"
            # Prompt Mode Event (Gruppenchat baut pro Teilnehmer immer den vollen Prompt)
            yield f"data: {json.dumps({'prompt_mode': 'full'})}\n\n"

            try:
                # Build system prompt for this character
                system_content = _build_group_system_prompt(char_name, participant_names, loc_name, running_context)

                # Build messages from group history
                # Only include messages from current participants
                # Deduplicate: skip consecutive identical assistant responses
                participant_set = {n.lower() for n in participant_names}
                messages = []
                _last_assistant_content = None

                def _visible_content_for(msg: Dict[str, Any], viewer: str) -> str:
                    """Liefert die fuer einen Character sichtbare Nachricht.

                    Bei Whisper: nur Sender und Ziel sehen den Inhalt, alle anderen
                    sehen einen Platzhalter '[sender fluestert ziel etwas zu]'.
                    """
                    wto = msg.get("whisper_to")
                    if not wto:
                        return msg.get("content", "")
                    sender = user_display_name if msg.get("role") == "user" else msg.get("character", "?")
                    if viewer == wto or viewer == sender:
                        # Ziel oder Sender sieht den Inhalt (mit Marker dass es geflsuestert ist)
                        return f"(whisper to {wto}) {msg.get('content', '')}"
                    # Dritter: nur Platzhalter, keine Inhalts-Info
                    return f"[{sender} whispers something to {wto}]"

                # Kontext so aufteilen dass die LETZTE Nachricht der "prompt" wird
                # (user_msg_for_llm), und der Rest als History geht. Damit reagiert
                # Character N auf die juengste Aussage (Real-Life Gruppen-Dynamik),
                # nicht immer auf die initiale User-Nachricht.
                _ctx_window = list(running_context[-15:])
                # Auf relevante Messages fuer diesen Character filtern
                _relevant = []
                for msg in _ctx_window:
                    if msg["role"] == "user":
                        _relevant.append(msg)
                    elif msg.get("character") == char_name:
                        _relevant.append(msg)
                    elif msg.get("character", "").lower() in participant_set:
                        _relevant.append(msg)
                    # departed characters: skip

                # Letzte Message wird prompt — Rest ist history
                _last_ctx_msg = _relevant[-1] if _relevant else None
                _history_msgs = _relevant[:-1] if _relevant else []

                for msg in _history_msgs:
                    visible = _visible_content_for(msg, char_name)
                    if msg["role"] == "user":
                        messages.append({"role": "user", "content": visible})
                        _last_assistant_content = None
                    elif msg.get("character") == char_name:
                        if msg["content"] == _last_assistant_content:
                            logger.debug("Skipping duplicate assistant msg for %s", char_name)
                            continue
                        _last_assistant_content = msg["content"]
                        messages.append({"role": "assistant", "content": visible})
                    elif msg.get("character", "").lower() in participant_set:
                        speaker = msg.get("character", "?")
                        messages.append({
                            "role": "user",
                            "content": f"[{speaker}]: {visible}"
                        })

                # Prompt fuer das LLM: letzte Nachricht (meistens von vorigem
                # Character bzw. initialer User-Input beim ersten Responder).
                if _last_ctx_msg:
                    _last_visible = _visible_content_for(_last_ctx_msg, char_name)
                    if _last_ctx_msg["role"] == "user":
                        user_msg_for_llm = _last_visible
                    elif _last_ctx_msg.get("character") == char_name:
                        # Sollte kaum vorkommen (eigene Nachricht als letzte) — Fallback
                        user_msg_for_llm = user_message
                    else:
                        _speaker = _last_ctx_msg.get("character", "?")
                        user_msg_for_llm = f"[{_speaker}]: {_last_visible}"
                else:
                    user_msg_for_llm = user_message

                # Get LLM for this character (Router, Task: group_chat_stream)
                agent_config = get_character_config(char_name)
                from app.core.llm_router import resolve_llm as _resolve_llm_gc
                _chat_inst_gc = _resolve_llm_gc("group_chat_stream", agent_name=char_name)
                llm = _chat_inst_gc.create_llm() if _chat_inst_gc else None
                if not llm:
                    yield f"data: {json.dumps({'content': f'[{char_name}: kein LLM verfuegbar]'})}\n\n"
                    yield f"data: {json.dumps({'char_end': {'character': char_name}})}\n\n"
                    continue

                # --- Optional skills/tools per character ---
                _tools_dict = {}
                _tool_format = "tag"
                _max_iter = 1
                _tool_system_content = ""
                _deferred_tools = set()
                # Tools + Modus pro Character bestimmen
                sm = get_skill_manager()
                agent_tools = sm.get_agent_tools(char_name, check_limits=False)
                _tool_inst_gc = _resolve_llm_gc("intent", agent_name=char_name) if agent_tools else None
                tool_llm = _tool_inst_gc.create_llm() if _tool_inst_gc else None
                _char_mode = determine_mode(agent_tools, tool_llm, agent_config)
                _char_tools_enabled = _char_mode != "no_tools"
                if _char_tools_enabled:
                    for t in agent_tools:
                        _orig = t.func
                        def _wrap(fn, _a=char_name, _u=""):
                            def w(raw):
                                ctx = {"input": raw, "agent_name": _a, "user_id": _u, "skip_daily_limit": True}
                                if isinstance(raw, str) and raw.strip().startswith("{"):
                                    try:
                                        for k, v in json.loads(raw).items():
                                            if k not in ("agent_name", "user_id"):
                                                ctx[k] = v
                                    except Exception:
                                        pass
                                return fn(json.dumps(ctx))
                            return w
                        _tools_dict[t.name] = _wrap(_orig)
                    _max_iter = 3
                    from app.core.tool_formats import get_format_for_model, build_tool_instruction
                    _model_for_fmt = (_tool_inst_gc.model if _tool_inst_gc else "") or (_chat_inst_gc.model if _chat_inst_gc else "")
                    _tool_format = get_format_for_model(_model_for_fmt)

                    # Tool-System-Prompt fuer RP-First Modus (Tool-LLM)
                    if _char_mode == "rp_first":
                        from app.models.character import get_character_appearance
                        _tool_model_name = _model_for_fmt
                        _tool_fmt = _tool_format
                        _tool_appearance = get_character_appearance(char_name)
                        _tool_usage = sm.get_agent_usage_instructions(char_name, _tool_fmt, check_limits=False)
                        from app.models.character_template import is_roleplay_character as _is_rp_gc
                        _tool_instr_block = build_tool_instruction(
                            _tool_fmt, agent_tools, _tool_appearance, _tool_usage, model_name=_tool_model_name,
                            is_roleplay=_is_rp_gc(char_name))
                        _available_tool_names = [t.name for t in agent_tools]
                        _tool_system_content = (
                            f"Character: {char_name}. Group chat at {loc_name}.\n"
                            f"{_tool_instr_block}\n\n"
                            f"RULE: You MUST call a tool — do NOT answer from memory.\n"
                            f"Available tools: {', '.join(_available_tool_names)}\n"
                            f"Call the most relevant tool based on the conversation."
                        )

                    # Tool-Kategorien
                    _content_tools = set()
                    for _tname in _tools_dict:
                        _sk = sm.get_skill_by_name(_tname)
                        if _sk and getattr(_sk, 'DEFERRED', False):
                            _deferred_tools.add(_tname)
                        if _sk and getattr(_sk, 'CONTENT_TOOL', False):
                            _content_tools.add(_tname)

                # Register with LLM queue
                from app.core.llm_queue import get_llm_queue
                _llm_queue = get_llm_queue()
                _llm_inst = _chat_inst_gc
                _chat_task_id = await _llm_queue.register_chat_active_async(
                    char_name, llm_instance=_llm_inst,
                    task_type="group_chat", label=f"Group Chat: {char_name}")

                try:
                    agent = StreamingAgent(
                        llm=llm,
                        tool_format=_tool_format,
                        tools_dict=_tools_dict,
                        agent_name=char_name,
                        max_iterations=_max_iter,
                        tool_llm=tool_llm,
                        tool_system_content=_tool_system_content,
                        deferred_tools=_deferred_tools,
                        content_tools=_content_tools if _char_tools_enabled else set(),
                        log_task="group_chat",
                        mode=_char_mode,
                        chat_task_id=_chat_task_id)

                    # Tool-Executor: Queue freigeben waehrend Tool-Ausfuehrung,
                    # damit Tools die selbst LLM-Calls machen nicht blockiert werden
                    _chat_state = {"task_id": _chat_task_id}

                    async def _tool_executor(tool_name, tool_input,
                                             _state=_chat_state, _queue=_llm_queue,
                                             _cname=char_name, _uid="", _inst=_llm_inst):
                        if _state["task_id"]:
                            _queue.register_chat_done(_state["task_id"])
                            _state["task_id"] = None
                        try:
                            tool_func = _tools_dict[tool_name]
                            return await asyncio.to_thread(tool_func, tool_input)
                        finally:
                            _state["task_id"] = await _queue.register_chat_active_async(
                                _cname, llm_instance=_inst,
                                task_type="group_chat", label=f"Group Chat: {_cname}")

                    agent.tool_executor = _tool_executor

                    full_response = ""
                    _tool_image_urls = []
                    async for event in agent.stream(system_content, messages, user_msg_for_llm):
                        if isinstance(event, HeartbeatEvent):
                            yield ": heartbeat\n\n"
                        elif isinstance(event, ContentEvent):
                            full_response += event.content
                        elif isinstance(event, ToolStartEvent) and _char_tools_enabled:
                            yield f"data: {json.dumps({'status': 'tool_start', 'tool': event.tool_name})}\n\n"
                        elif isinstance(event, ToolEndEvent) and _char_tools_enabled:
                            yield f"data: {json.dumps({'status': 'tool_end', 'tool': event.tool_name})}\n\n"
                        elif isinstance(event, ToolResultEvent) and _char_tools_enabled:
                            yield f"data: {json.dumps({'tool_result': event.result[:500] if event.result else ''})}\n\n"
                            if event.result:
                                for _m in re.finditer(r'!\[[^\]]*\]\(/characters/[^)]+\)', event.result):
                                    _tool_image_urls.append(_m.group(0))

                finally:
                    _final_task_id = _chat_state.get("task_id") if _char_tools_enabled else _chat_task_id
                    if _final_task_id:
                        _llm_queue.register_chat_done(_final_task_id)

                # --- Post-processing: strip other characters' dialogue ---
                clean_response = _strip_foreign_dialogue(
                    full_response, char_name, participant_names + [user_display_name]
                )
                # Strip meta tags
                clean_response = re.sub(r'\n?\s*\*\*I\s+feel\s+[^*]+\*\*\s*', '', clean_response, flags=re.IGNORECASE)
                clean_response = re.sub(r'\n?\s*\*\*I\s+am\s+at\s+[^*]+\*\*\s*', '', clean_response, flags=re.IGNORECASE)
                clean_response = re.sub(r'\n?\s*\*\*I\s+do\s+[^*]+\*\*\s*', '', clean_response, flags=re.IGNORECASE)
                from app.core.intent_engine import strip_intent_tags
                clean_response = strip_intent_tags(clean_response).strip()

                # Fallback: if stripping removed everything but LLM did respond,
                # log a warning (response was likely only meta-tags)
                if not clean_response and full_response.strip():
                    logger.warning(
                        "Response from %s was stripped to empty (original %d chars): %s",
                        char_name, len(full_response), full_response[:200])

                # Send the cleaned response as content + save
                if clean_response:
                    # Duplicate guard: don't save if identical to this char's last response
                    _last_own = None
                    for _prev in reversed(running_context):
                        if _prev.get("character") == char_name:
                            _last_own = _prev.get("content", "")
                            break
                    if _last_own and clean_response.strip() == _last_own.strip():
                        logger.warning("Duplicate response from %s — skipping save", char_name)
                    else:
                        yield f"data: {json.dumps({'content': clean_response})}\n\n"
                        # Save to group history
                        save_group_message(session_id, "assistant", clean_response, character=char_name)

                    # --- TTS for this character (non-blocking) ---
                    _tts = ChunkedTTSHandler(agent_config, require_auto=True)
                    if _tts.enabled:
                        # Launch TTS in background — does not block next character
                        _tts_task = asyncio.create_task(
                            _run_tts_background(_tts, clean_response, char_name))
                        _tts_pending.append(_tts_task)
                    else:
                        # Fallback: tts_auto signal per character
                        try:
                            from app.core.tts_service import get_tts_service
                            tts_svc = get_tts_service()
                            tts_cfg = tts_svc.get_character_config(agent_config)
                            if tts_svc.enabled and tts_cfg.get("enabled", True) and tts_cfg.get("auto", False):
                                yield f"data: {json.dumps({'tts_auto': True, 'tts_character': char_name})}\n\n"
                        except Exception:
                            pass

                    # Add to running context for next character
                    running_context.append({
                        "role": "assistant",
                        "character": char_name,
                        "content": clean_response,
                        "timestamp": datetime.now().isoformat(),
                    })

                # Extract mood + activity from response (save to character)
                try:
                    from app.routes.chat import _extract_mood, _extract_activity
                    _extract_mood(char_name, full_response)
                    _extract_activity(char_name, full_response)
                except Exception:
                    pass

            except Exception as e:
                logger.error("Group chat error for %s: %s", char_name, e, exc_info=True)
                yield f"data: {json.dumps({'content': f'[Fehler bei {char_name}]'})}\n\n"

            # Emit char_end
            yield f"data: {json.dumps({'char_end': {'character': char_name}})}\n\n"

        # Post-processing: relationships & memory (background)
        def _background_updates():
            try:
                for rname in responders:
                    # Relationship with user
                    record_interaction(
                        char_a=rname, char_b=user_display_name,
                        interaction_type="group_chat",
                        summary=f"Group conversation at {loc_name}",
                        strength_delta=1.5)
                    # Relationships between responders
                    for other in responders:
                        if other != rname:
                            record_interaction(
                                char_a=rname, char_b=other,
                                interaction_type="group_chat",
                                summary=f"Group chat at {loc_name}",
                                strength_delta=1.0)
                    # Memory
                    other_names = [r for r in responders if r != rname]
                    if other_names:
                        upsert_relationship_memory(
                            character_name=rname,
                            related_character=user_display_name,
                            new_fact=f"Had a group conversation with {', '.join(other_names)} at {loc_name}")
                # Discovery-Check: Haben Characters hidden Activities bemerkt?
                _check_activity_discovery(responders + passive, location_id)

            except Exception as bg_err:
                logger.error("Background group updates error: %s", bg_err)

        asyncio.get_event_loop().run_in_executor(None, _background_updates)

        # Await remaining TTS tasks and drain queue before ending
        if _tts_pending:
            await asyncio.gather(*_tts_pending, return_exceptions=True)
            for _sse in _drain_tts_queue():
                yield _sse

        # Emit group_end
        yield f"data: {json.dumps({'group_end': {}})}\n\n"

    mgr = get_chat_task_manager()
    task_id = mgr.create_task()
    asyncio.create_task(mgr.feed_from_generator(task_id, generate()))
    return JSONResponse({"task_id": task_id})


@router.get("/{user_id}/group/stream/{task_id}")
async def group_chat_stream(task_id: str, request: Request) -> StreamingResponse:
    """SSE stream for a group chat task (reuses chat_task_manager)."""
    from_offset = int(request.query_params.get("offset", "0"))
    mgr = get_chat_task_manager()
    task = mgr.get_task(task_id)

    if task is None:
        async def _not_found():
            yield 'data: {"error": "Task nicht gefunden"}\n\n'
        return StreamingResponse(_not_found(), media_type="text/event-stream")

    async def _stream():
        async for chunk in mgr.subscribe(task_id, from_offset=from_offset):
            yield chunk

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _strip_foreign_dialogue(
    response: str,
    current_char: str,
    all_names: List[str]) -> str:
    """Remove lines where the LLM speaks as other characters.

    RP-LLMs often generate full group scripts like:
        Diego: Hey!
        Hellena: Hi there!
        Kira: *laughs* Oh hey!

    This strips everything except the current character's lines.
    If no character prefixes are found, the response is returned as-is
    (meaning the LLM correctly responded only as itself).
    """
    if not response or not all_names:
        return response

    other_names = [n for n in all_names if n.lower() != current_char.lower()]
    if not other_names:
        return response

    # Build pattern: "Name:" at line start (with optional bold/italic markers)
    # Matches: "Diego:", "**Diego:**", "*Diego:*", "Diego :", etc.
    name_pattern = "|".join(re.escape(n) for n in other_names)
    own_pattern = re.escape(current_char)

    # Check if any foreign character prefix exists in the response
    foreign_prefix_re = re.compile(
        r'^\s*\*{0,2}(?:' + name_pattern + r')\s*(?::|\*{0,2}:)',
        re.MULTILINE | re.IGNORECASE
    )
    if not foreign_prefix_re.search(response):
        # No foreign prefixes found — response is clean, strip own name prefix if present
        own_prefix_re = re.compile(
            r'^\s*\*{0,2}(?:' + own_pattern + r')\s*(?::|\*{0,2}:)\s*',
            re.IGNORECASE
        )
        return own_prefix_re.sub('', response).strip()

    # Foreign dialogue detected — extract only our character's lines
    logger.warning(
        "Foreign dialogue detected in %s's response, stripping other characters",
        current_char)

    # Split into blocks by character prefix
    all_pattern = "|".join(re.escape(n) for n in all_names)
    split_re = re.compile(
        r'^(\s*\*{0,2}(?:' + all_pattern + r')\s*(?::|\*{0,2}:))',
        re.MULTILINE | re.IGNORECASE
    )

    lines = response.split('\n')
    own_lines = []
    is_own_block = False

    for line in lines:
        # Check if this line starts a character's dialogue
        match = re.match(
            r'^\s*\*{0,2}(' + all_pattern + r')\s*(?::|\*{0,2}:)',
            line, re.IGNORECASE
        )
        if match:
            speaker = match.group(1).strip()
            is_own_block = speaker.lower() == current_char.lower()
            if is_own_block:
                # Remove the "CharName:" prefix from our own line
                cleaned = split_re.sub('', line).strip()
                if cleaned:
                    own_lines.append(cleaned)
        elif is_own_block:
            # Continuation of our character's block
            own_lines.append(line)
        # else: line belongs to another character's block — skip

    result = '\n'.join(own_lines).strip()

    # Fallback: if stripping removed everything, return original
    # (better to show imperfect output than nothing)
    if not result:
        logger.warning("Stripping removed all content for %s, using original", current_char)
        return response.strip()

    return result
