"""
Chat Engine — shared logic for Web and Telegram chat paths.

Provides:
- build_chat_context(): prepares system prompt, history, tools
- post_process_response(): mood, location, memory, relationship extraction
"""
import asyncio
import json
import re
from typing import Dict, Any, Optional
from datetime import datetime

from app.core.log import get_logger

logger = get_logger("chat_engine")


def build_chat_context(
    owner_id: str,
    character_name: str,
    user_input: str,
    channel: str = "web",
    selected_skills: Optional[list] = None,
    speaker: str = "user",
    medium: Optional[str] = None,
    partner_name: str = "") -> Dict[str, Any]:
    """
    Build everything needed to run a chat: system prompt, message history,
    LLM instances, and tool setup.

    Args:
        owner_id: User who owns the character (storage path)
        character_name: Character name (der Antwortende)
        user_input: Current incoming message
        channel: "web" or "telegram"
        selected_skills: Optional skill filter
        speaker: "user" (default) oder Name des sprechenden Characters.
            Bei Character-zu-Character ist speaker der Name des Senders.
        medium: Kommunikationsmedium aus Sicht der Figuren:
            "in_person", "messaging", "telegram", "instagram".
            None = auto-derive aus channel + Speaker-Kontext.
        partner_name: Bei Character-zu-Character: der sprechende Character (= speaker
            wenn speaker != "user"). Fuer History-Dateinamen.

    Returns:
        Dict with keys: system_content, messages, llm, agent_config,
        tools_dict, tool_format, tool_llm, max_iterations,
        full_chat_history, user_display_name, lang_instruction,
        speaker, medium
    """
    from app.core.dependencies import get_skill_manager
    from app.core.llm_router import resolve_llm
    from app.models.character import (
        get_character_config,
        get_character_language_instruction)
    from app.models.account import get_user_name, get_active_character
    from app.models.chat import get_chat_history
    from app.utils.history_manager import get_time_based_history, get_cached_summary
    from app.routes.chat import _build_full_system_prompt, _strip_tool_hallucinations

    agent_config = get_character_config(character_name)
    _chat_instance = resolve_llm("chat_stream", agent_name=character_name)
    llm = _chat_instance.create_llm() if _chat_instance else None
    lang_instruction = get_character_language_instruction(character_name)
    # For web chat: player's active character is the conversation partner identity.
    # For telegram: use account name (telegram has no character-switching).
    if channel == "telegram":
        user_display_name = get_user_name() or owner_id
    else:
        user_display_name = get_active_character() or get_user_name() or owner_id

    # Auto-derive medium wenn nicht gesetzt
    if medium is None:
        if channel == "telegram":
            medium = "telegram"
        elif speaker != "user":
            # Character-zu-Character: in_person wenn am gleichen Ort, sonst messaging
            try:
                from app.models.character import get_character_current_location
                speaker_loc = get_character_current_location(speaker)
                self_loc = get_character_current_location(character_name)
                medium = "in_person" if (speaker_loc and speaker_loc == self_loc) else "messaging"
            except Exception:
                medium = "messaging"
        else:
            medium = "in_person"

    # Partner-Name fuer History-Dateinamen: bei C2C explizit; sonst active character
    _history_partner = partner_name if partner_name else (speaker if speaker != "user" else "")
    full_chat_history = get_chat_history(character_name, partner_name=_history_partner)

    # History window (zeitgesteuert)
    recent_history, old_history = get_time_based_history(full_chat_history)
    history_summary = get_cached_summary(character_name) if old_history else ""

    messages = []
    # Halluzinierter Template-Prefix von Send-Message-Hint:
    # "[Name, ]deine Antwort: '...'" wurde frueher als Antwort-Format gespeichert
    # weil das LLM den Format-Hinweis literal kopierte. Bereinigen, damit das
    # Muster nicht in neue Antworten echoed wird.
    _meta_prefix_re = re.compile(
        r'^(?:[A-Z][\wÄÖÜäöüß \-]{0,30},\s*)?(?:deine|meine|seine|ihre)\s+Antwort:\s*[\'\"]?',
        re.IGNORECASE)
    for msg in recent_history:
        content = msg["content"]
        if msg["role"] == "assistant":
            content = re.sub(r'^\[Gedanken-Nachricht[^\]]*\]\s*', '', content)
            cleaned = _meta_prefix_re.sub('', content).rstrip("'\"").strip()
            if cleaned and cleaned != content:
                content = cleaned
            if content != msg["content"]:
                content = content.strip()
                if not content:
                    continue
            content = _strip_tool_hallucinations(content)
            if not content:
                continue
            messages.append({"role": "assistant", "content": content})
        else:
            messages.append({"role": "user", "content": content})

    # Self-Reinforcement-Loop brechen: identische Assistant-Antworten
    # kollabieren (siehe chat.py fuer Details).
    _seen: set = set()
    _deduped: list = []
    _i = 0
    while _i < len(messages):
        m = messages[_i]
        if m["role"] == "assistant":
            key = m["content"].strip().lower()
            if key in _seen:
                if _deduped and _deduped[-1]["role"] == "user":
                    _deduped.pop()
                _i += 1
                continue
            _seen.add(key)
        _deduped.append(m)
        _i += 1
    if len(_deduped) < len(messages):
        logger.info("C2C-History: %d redundante Assistant-Duplikate entfernt",
                    len(messages) - len(_deduped))
        messages = _deduped

    # Tools aus aktivierten Skills ableiten
    sm = get_skill_manager()
    agent_tools = sm.get_agent_tools(character_name, check_limits=False)
    if selected_skills is not None:
        agent_tools = [t for t in agent_tools if t.name in selected_skills]

    # Modus-Erkennung: tool_llm frueh laden fuer determine_mode + System-Prompt
    from app.core.dependencies import determine_mode
    _tool_instance = resolve_llm("intent", agent_name=character_name) if agent_tools else None
    tool_llm = _tool_instance.create_llm() if _tool_instance else None
    mode = determine_mode(agent_tools, tool_llm, agent_config)
    tools_enabled = mode != "no_tools"

    # System prompt
    system_content = _build_full_system_prompt(character_name, lang_instruction, history_summary,
        tools_enabled=tools_enabled, agent_config=agent_config,
        selected_skills=selected_skills,
        channel=channel,
        has_tool_llm=(mode == "rp_first"),
        partner_override=(speaker if speaker != "user" else ""),
        medium=medium)

    # Tool setup
    tools_dict = {}
    tool_format = "tag"
    max_iterations = 1

    if agent_tools:
        # Initiator: wer hat diesen Chat-Turn ausgeloest. Bei user-chat = "user",
        # bei C2C = der sprechende Character. Wird an Skills durchgereicht, damit
        # talk_to/send_message pending_reports anlegen koennen.
        _tool_initiator = speaker if speaker else "user"
        for t in agent_tools:
            _orig_func = t.func
            def _make_ctx_wrapper(fn, _agent=character_name, _uid=owner_id, _init=_tool_initiator):
                def wrapper(raw_input):
                    ctx = {"input": raw_input, "agent_name": _agent, "user_id": _uid,
                           "initiator": _init, "skip_daily_limit": True}
                    if isinstance(raw_input, str) and raw_input.strip().startswith("{"):
                        try:
                            parsed = json.loads(raw_input)
                            if isinstance(parsed, dict):
                                for k, v in parsed.items():
                                    if k not in ("agent_name", "user_id", "initiator"):
                                        ctx[k] = v
                        except Exception:
                            pass
                    return fn(json.dumps(ctx))
                return wrapper
            tools_dict[t.name] = _make_ctx_wrapper(_orig_func)
        max_iterations = 3

        from app.core.tool_formats import get_format_for_model
        tool_model_name = _tool_instance.model if _tool_instance else (
            _chat_instance.model if _chat_instance else ""
        )
        tool_format = get_format_for_model(tool_model_name)

    return {
        "system_content": system_content,
        "messages": messages,
        "llm": llm,
        "agent_config": agent_config,
        "tools_dict": tools_dict,
        "tool_format": tool_format,
        "tool_llm": tool_llm,
        "max_iterations": max_iterations,
        "mode": mode,
        "full_chat_history": full_chat_history,
        "old_history": old_history,
        "user_display_name": user_display_name,
        "lang_instruction": lang_instruction,
        "speaker": speaker,
        "medium": medium,
        "partner_name": _history_partner,
    }


def run_chat_turn(
    owner_id: str,
    responder: str,
    speaker: str,
    incoming_message: str,
    medium: str = "in_person",
    task_type: str = "character_talk") -> str:
    """Laesst responder EINE Antwort auf incoming_message von speaker generieren.

    Synchron. Wird von talk_to / send_message Skills genutzt. Nutzt die
    existierende Chat-Engine (System-Prompt, History, Medium-Kontext) aber
    OHNE Streaming — ein einzelner llm_queue.submit() Call.

    Schreibt beide Seiten der Konversation in die Chat-History:
      - responder's file: "user" (speaker) → "assistant" (responder)
      - speaker's file: "assistant" (speaker) → "user" (responder)

    Returns:
        Aufbereiteter Response-Text des responders.
    """
    from app.core.llm_queue import get_llm_queue, Priority
    from app.models.chat import save_message
    from datetime import datetime

    # Wenn der angesprochene Responder vom Spieler gesteuert wird (Avatar),
    # darf KEIN LLM-Call laufen — der User soll selbst antworten. Eingehende
    # Nachricht wird trotzdem in beiden Chat-Histories gespeichert, damit
    # sie beim naechsten Refresh erscheint.
    # WICHTIG: is_player_controlled lebt in app.models.account, NICHT in
    # app.models.character. Frueher hier falscher Import → ImportError →
    # try/except schluckte → Avatar antwortete trotzdem (Vallerie/Kai-Bug).
    try:
        from app.models.account import is_player_controlled
        if is_player_controlled(responder):
            ts = datetime.now().isoformat()
            save_message({
                "role": "user", "content": incoming_message, "timestamp": ts,
                "speaker": speaker, "medium": medium,
            }, character_name=responder, partner_name=speaker)
            save_message({
                "role": "assistant", "content": incoming_message, "timestamp": ts,
                "speaker": speaker, "medium": medium,
            }, character_name=speaker, partner_name=responder)
            logger.info("run_chat_turn: Avatar %s — kein Auto-Reply, "
                        "Message von %s gespeichert (User antwortet selbst)",
                        responder, speaker)
            return ""
    except Exception as _pe:
        logger.warning("run_chat_turn: player_controlled-Check fehlgeschlagen "
                       "(Avatar-Schutz unwirksam): %s", _pe)

    ctx = build_chat_context(owner_id, responder, incoming_message,
        speaker=speaker, medium=medium,
        partner_name=speaker)

    if ctx["llm"] is None:
        logger.error("run_chat_turn: Kein LLM fuer %s verfuegbar", responder)
        return ""

    messages = [{"role": "system", "content": ctx["system_content"]}]
    messages.extend(ctx["messages"])
    messages.append({"role": "user", "content": incoming_message})

    # Label fuer Task-Panel — zeigt klar wer-zu-wem ueber welchen Trigger
    if task_type == "talk_to":
        _label = f"TalkTo: {speaker} → {responder}"
    elif task_type == "send_message":
        _label = f"Message: {speaker} → {responder}"
    else:
        _label = f"{task_type}: {speaker} → {responder}"

    try:
        response = get_llm_queue().submit(
            task_type=task_type,
            priority=Priority.CHAT,
            llm=ctx["llm"],
            messages_or_prompt=messages,
            agent_name=responder,
            label=_label)
        raw = getattr(response, "content", "") or ""
    except Exception as e:
        logger.error("run_chat_turn LLM error for %s: %s", responder, e)
        return ""

    clean = clean_response(raw)
    if not clean:
        logger.warning("run_chat_turn: leere Antwort von %s", responder)
        return ""

    ts = datetime.now().isoformat()

    # Beidseitig speichern (siehe plan-chat-history-redesign: Character↔Character)
    save_message({
        "role": "user", "content": incoming_message, "timestamp": ts,
        "speaker": speaker, "medium": medium,
    }, character_name=responder, partner_name=speaker)
    save_message({
        "role": "assistant", "content": clean, "timestamp": ts,
        "speaker": responder, "medium": medium,
    }, character_name=responder, partner_name=speaker)
    save_message({
        "role": "assistant", "content": incoming_message, "timestamp": ts,
        "speaker": speaker, "medium": medium,
    }, character_name=speaker, partner_name=responder)
    save_message({
        "role": "user", "content": clean, "timestamp": ts,
        "speaker": responder, "medium": medium,
    }, character_name=speaker, partner_name=responder)

    # Sofort-Trigger: Hat der Speaker einen offenen pending_report mit diesem
    # responder als target? Dann ist jetzt der richtige Moment, einen forcierten
    # Gedanken zu triggern, damit der Speaker zurueckmelden kann.
    try:
        from app.core.pending_reports import trigger_sofort_thought_if_applicable
        hint = trigger_sofort_thought_if_applicable(speaker, responder)
        if hint:
            _schedule_forced_thought(speaker, hint)
    except Exception as e:
        logger.debug("pending_report Sofort-Trigger Fehler: %s", e)

    return clean


def _schedule_forced_thought(owner_id: str, character_name: str, context_hint: str) -> None:
    """Schedult einen forcierten Thought-Turn.

    Laeuft asynchron im Hintergrund via BackgroundQueue — blockt run_chat_turn nicht.
    """
    try:
        from app.core.background_queue import get_background_queue
        get_background_queue().submit(
            "forced_thought",
            {"user_id": owner_id, "character_name": character_name, "context_hint": context_hint})
        logger.info("Forcierter Gedanke eingestellt: %s (hint=%s)", character_name, context_hint[:80])
    except Exception as e:
        logger.warning("Konnte forcierten Gedanken nicht einstellen: %s", e)


def clean_response(full_response: str) -> str:
    """Strip meta-tags from response for saving to history."""
    clean = full_response
    clean = re.sub(r'!\[([^\]]*)\]\(data:image/[^)]+\)', '', clean)
    clean = re.sub(r'\n?\s*\*\*I\s+feel\s+[^*]+\*\*\s*', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'\n?\s*\*\*I\s+am\s+at\s+[^*]+\*\*\s*', '', clean, flags=re.IGNORECASE)
    clean = re.sub(r'\n?\s*\*\*I\s+do\s+[^*]+\*\*\s*', '', clean, flags=re.IGNORECASE)
    from app.core.intent_engine import strip_intent_tags
    clean = strip_intent_tags(clean)
    from app.models.assignments import strip_assignment_tags
    clean = strip_assignment_tags(clean)
    # Strip tool hallucinations
    clean = re.sub(r'<tool_call>.*?</tool_call>', '', clean, flags=re.DOTALL)
    clean = re.sub(r'</?tool_(?:call|result)>', '', clean)
    # LLM-Tokenizer-Artefakte entfernen (z.B. <SPECIAL_28>, <|END_OF_TURN_TOKEN|>)
    clean = re.sub(r'<SPECIAL_\d+>|<\|[A-Z_]+\|>', '', clean)
    return clean.strip()


def post_process_response(
    owner_id: str,
    character_name: str,
    user_input: str,
    full_response: str,
    agent_config: Dict[str, Any],
    llm: Any,
    user_display_name: str,
    full_chat_history: list,
    history_window: int = 0,
    old_history: list = None,
    extraction_context: Dict[str, Any] = None) -> Dict[str, Any]:
    """
    Run all post-processing after a chat response: mood, location, activity,
    memory extraction, relationship updates, intent extraction.

    Can be called from both Web (in background) and Telegram.

    Args:
        owner_id: User who owns the character
        character_name: Character name
        user_input: Original user message
        full_response: Complete LLM response (before cleaning)
        agent_config: Character config dict
        llm: LLM instance used for chat
        user_display_name: Display name of the user
        full_chat_history: Full chat history (before this exchange)
        history_window: DEPRECATED — ignored, kept for backward compat
        old_history: Messages older than short-term window (for summary)

    Returns:
        Dict with extracted data: mood, location, activity (may be None)
    """
    from app.routes.chat import _extract_mood, _extract_location, _extract_activity
    from app.routes.chat import _extract_context_from_last_chat

    result = {"mood": None, "location": None, "activity": None}

    cleaned = clean_response(full_response)

    # Mood extraction
    try:
        mood = _extract_mood(character_name, full_response)
        if mood:
            result["mood"] = mood
    except Exception as e:
        logger.error("Mood extraction error: %s", e)

    # Location extraction (Location- oder Raum-Wechsel)
    try:
        loc_change = _extract_location(character_name, full_response)
        if loc_change:
            if loc_change.get("id"):
                # Echter Location-Wechsel — Frontend aktualisiert Hintergrund + Sidebar
                result["location"] = loc_change.get("name")
            elif loc_change.get("room") and loc_change.get("location_id"):
                # Reiner Raum-Wechsel — Location-ID mitsenden fuer Hintergrund-Update
                result["location"] = loc_change["location_id"]
            if loc_change.get("room"):
                result["room"] = loc_change["room"]
    except Exception as e:
        logger.error("Location extraction error: %s", e)

    # Activity extraction
    try:
        act_change = _extract_activity(character_name, full_response)
        if act_change:
            result["activity"] = act_change
    except Exception as e:
        logger.error("Activity extraction error: %s", e)

    # Assignment marker extraction (progress/done)
    try:
        from app.models.assignments import extract_assignment_markers
        markers = extract_assignment_markers(character_name, full_response)
        if markers:
            result["assignment_markers"] = markers
    except Exception as e:
        logger.error("Assignment marker extraction error: %s", e)

    # New assignment creation from chat
    try:
        from app.models.assignments import extract_new_assignment
        new_assignment = extract_new_assignment(character_name, full_response)
        if new_assignment:
            result["new_assignment"] = new_assignment
            logger.info("[%s] New assignment from chat: %s", character_name, new_assignment.get("title"))
    except Exception as e:
        logger.error("New assignment extraction error: %s", e)

    # Background extraction: memory + categories + relationships
    # Nur bei substantiellen Antworten (kurze/leere Antworten ueberspringen)
    if len(cleaned) < 20:
        logger.debug("[%s] Antwort zu kurz (%d Zeichen) — Background-Extraktion uebersprungen",
                      character_name, len(cleaned))
        return result

    def _background_extraction():
        # Memory extraction
        try:
            from app.core.memory_service import extract_memories_from_exchange, apply_extracted_memories
            extracted = extract_memories_from_exchange(character_name, user_input, cleaned, llm
            )
            if extracted:
                count = apply_extracted_memories(character_name, extracted,
                                                 extraction_context=extraction_context)
                logger.debug("[%s] Memory extraction: %d new", character_name, count)
        except Exception as e:
            logger.error("[%s] Memory extraction error: %s", character_name, e)

        # Relationship update
        try:
            from app.models.relationship import record_interaction, get_romantic_interests

            summary = f"{user_display_name}: {user_input[:100]}"
            if cleaned:
                summary += f" — {character_name}: {cleaned[:80]}"

            analysis = {"sentiment_a": 0.05, "sentiment_b": 0.05, "romantic_delta": 0.0}
            try:
                ri_user = get_romantic_interests(user_display_name)
                ri_char = get_romantic_interests(character_name)
                romantic_context = ""
                if ri_user or ri_char:
                    romantic_context = "\n\nRomantic interest context:\n"
                    if ri_user:
                        romantic_context += f"- {user_display_name}'s romantic interests: {ri_user}\n"
                    if ri_char:
                        romantic_context += f"- {character_name}'s romantic interests: {ri_char}\n"
                    romantic_context += "Only set romantic_delta > 0 if the conversation matches these interests.\n"

                rel_system_prompt = (
                    "Analyze this conversation between a user and a fictional character. "
                    "Return ONLY a JSON object with these fields:\n"
                    '"sentiment_a": how the user feels about the character after this (-0.3 to 0.3)\n'
                    '"sentiment_b": how the character feels about the user after this (-0.3 to 0.3)\n'
                    '"romantic_delta": change in romantic tension (-0.1 to 0.15). '
                    "Positive if flirting, affection, intimacy, love. Negative if cold/distant. Zero if neutral.\n\n"
                    "Values close to 0 for casual/neutral interactions. "
                    + romantic_context
                    + "Output ONLY the JSON, nothing else."
                )
                conversation_text = f"{user_display_name}: {user_input[:300]}"
                if cleaned:
                    conversation_text += f"\n{character_name}: {cleaned[:300]}"

                from app.core.llm_router import llm_call as _llm_call
                try:
                    resp = _llm_call(
                        task="relationship_summary",
                        system_prompt=rel_system_prompt,
                        user_prompt=conversation_text,
                        agent_name=character_name)
                    raw = re.sub(r'<SPECIAL_\d+>|<\|[A-Z_]+\|>', '', resp.content).strip()
                except RuntimeError:
                    raw = ""
                match = re.search(r'\{[^}]+\}', raw, re.DOTALL)
                if match:
                    data = json.loads(match.group(0))
                    analysis = {
                        "sentiment_a": max(-0.3, min(0.3, float(data.get("sentiment_a", 0.05)))),
                        "sentiment_b": max(-0.3, min(0.3, float(data.get("sentiment_b", 0.05)))),
                        "romantic_delta": max(-0.1, min(0.15, float(data.get("romantic_delta", 0.0)))),
                    }
            except Exception as rel_err:
                logger.debug("[%s] Relationship analysis failed (defaults): %s", character_name, rel_err)

            record_interaction(
                char_a=user_display_name,
                char_b=character_name,
                interaction_type="chat",
                summary=summary,
                strength_delta=2,
                sentiment_delta_a=analysis.get("sentiment_a", 0.05),
                sentiment_delta_b=analysis.get("sentiment_b", 0.05),
                romantic_delta=analysis.get("romantic_delta", 0.0))
        except Exception as rel_err:
            logger.error("[%s] Relationship update error: %s", character_name, rel_err)

    # Run background extraction in thread pool
    try:
        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, _background_extraction)
    except RuntimeError:
        # No event loop — run synchronously
        _background_extraction()

    # Intent extraction
    try:
        from app.core.intent_engine import process_response_intents_async
        from app.core.thoughts import get_thought_loop
        _pl = get_thought_loop()
        _sched = getattr(_pl, '_scheduler', None) if _pl else None
        asyncio.ensure_future(
            process_response_intents_async(full_response, character_name,
                                           agent_config, _sched)
        )
    except Exception as e:
        logger.error("[%s] Intent extraction error: %s", character_name, e)

    # Instagram interaction extraction
    try:
        from app.models.instagram import extract_instagram_interactions, apply_interactions_to_latest_post
        from app.models.assignments import strip_assignment_tags
        cleaned_for_instagram = strip_assignment_tags(full_response)
        interactions = extract_instagram_interactions(cleaned_for_instagram)
        if interactions:
            apply_interactions_to_latest_post(character_name, interactions)
    except Exception as e:
        logger.error("[%s] Instagram extraction error: %s", character_name, e)

    # Chat context extraction (activity + outfit from response)
    try:
        updated_history = full_chat_history + [
            {"role": "user", "content": user_input},
            {"role": "assistant", "content": full_response},
        ]
        _extract_context_from_last_chat(character_name, updated_history, agent_config)
    except Exception as e:
        logger.error("[%s] Context extraction error: %s", character_name, e)

    # History summary update (nur wenn es alte Nachrichten gibt)
    try:
        from app.utils.history_manager import update_summary_background
        # old_history kommt vom zeitgesteuerten Window; Fallback fuer alte Aufrufe
        old_messages = old_history
        if old_messages is None and history_window and len(full_chat_history) > history_window:
            old_messages = full_chat_history[:-history_window]
        if old_messages:
            loop = asyncio.get_event_loop()
            loop.run_in_executor(
                None, update_summary_background, character_name, old_messages
            )
    except Exception as e:
        logger.error("[%s] History summary error: %s", character_name, e)

    return result
