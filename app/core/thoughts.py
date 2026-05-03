"""ThoughtRunner - Autonomes NachdenkSystem fuer Characters.

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

_thought_runner: Optional["ThoughtRunner"] = None


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


def get_thought_runner() -> Optional["ThoughtRunner"]:
    return _thought_runner


def set_thought_runner(loop: "ThoughtRunner"):
    global _thought_runner
    _thought_runner = loop


class ThoughtRunner:
    """Container fuer ``run_thought_turn`` — die gemeinsame Einstiegsfunktion
    fuer Gedanken-Turns. Scheduling laeuft im ``AgentLoop``, dieser Class
    haelt nur noch die Turn-Ausfuehrung.

    Name "Loop" ist historisch (frueher periodischer Tick); kein Background
    Task mehr. Rename auf z.B. ``ThoughtRunner`` ist optional und wuerde
    Anpassungen in agent_loop.py / chat_engine.py / memory_service.py /
    world_dev.py erfordern.
    """

    def __init__(self):
        self._lock = asyncio.Lock()


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
    async def run_thought_turn(self, character_name: str,
                                context_hint: str = "", fast: bool = False,
                                tool_whitelist=None,
                                suppress_notification: bool = False,
                                llm_task: str = "thought",
                                reply_only_to: str = "",
                                system_prompt_override: str = ""):
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
        from app.models.chat import save_message

        profile = get_character_profile(character_name)
        config = get_character_config(character_name)
        from app.models.account import get_player_identity
        # Avatar-Identitaet, niemals Login-Name. Wuerde sonst als "admin"
        # in den System-Prompt sickern.
        user_name = get_player_identity("user")

        # Turn-Summary fuer den Agent-Loop / Admin-Panel.
        # tools = list of tool-name strings actually invoked (stream + narrative).
        # intents = list of intent-type strings dispatched after the stream.
        # preview = first ~140 chars of the cleaned narrative output.
        _turn_info: Dict[str, Any] = {"tools": [], "intents": [], "preview": ""}

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
            _turn_info["preview"] = "[no LLM available]"
            _turn_info["status"] = "no_llm"
            return _turn_info

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

        # Tools-Hint: Im Single-Modus muss das LLM die Tool-Calls SELBST schreiben.
        # Im Dual-Modus (rp_first) uebernimmt das Tool-LLM die Tool-Entscheidung.
        available_tool_names = [t.name for t in agent_tools] if agent_tools else []
        tools_hint = ""
        if available_tool_names and mode != "rp_first":
            appearance = get_character_appearance(character_name)
            usage = sm.get_agent_usage_instructions(character_name, tool_format)
            tools_hint = (
                f"\n"
                + build_tool_instruction(
                    tool_format, agent_tools, appearance, usage,
                    model_name=tool_model_name,
                    is_roleplay=False)
            )

        # System-Prompt: AgentLoop passes a fully-rendered slim prompt via
        # ``system_prompt_override``. Other callers (e.g. world-dev debug
        # endpoint) get the slim prompt built fresh here, with optional
        # context_hint prepended as a "Trigger" block.
        if system_prompt_override:
            system_prompt = system_prompt_override
        else:
            from app.core.thought_context import build_thought_context
            from app.core.prompt_templates import render
            ctx = build_thought_context(character_name, tools_hint=tools_hint)
            system_prompt = render("chat/agent_thought.md", **ctx)
            if context_hint:
                # Manual trigger (admin debug, scripted): prepend the hint
                # so the agent sees what's expected before the situation.
                system_prompt = f"# Triggered thought\n{context_hint}\n\n{system_prompt}"
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
            try:
                _tool_executions.append((tool_name, tool_input))
            except Exception:
                pass
            tool_func = tools_dict[tool_name]
            return await asyncio.to_thread(tool_func, tool_input)
        agent.tool_executor = _tool_executor

        # Thoughts haben keine recent_history. Frischer Gespraechsfaden
        # kommt ueber den Inbox-Block, Versprechen ueber Commitments,
        # Fakten ueber Memory — alles im System-Prompt gebuendelt. Echte
        # Conversation-Turns wuerden hier nur Avatar-zentrierten Bias und
        # alte (stale) Wortlaute reinbringen.
        recent_history: List[Dict[str, str]] = []

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
        # (tool_name, raw_input) per executed tool — used by the intent
        # engine to detect and skip redundant [INTENT:...] markers that
        # duplicate a tool already executed in this turn.
        _tool_executions: List = []

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

        # Hand the chat task_id to the agent so it can report iteration
        # progress (admin queue panel shows "iter N/M" while streaming).
        agent.chat_task_id = _thought_task_id

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
            # Capture tool invocation (for INTENT-skip-logic). The raw
            # tool_input is the freetext or JSON the LLM emitted — exactly
            # what we compare against [INTENT:...] payloads.
            try:
                _tool_executions.append((tool_name, tool_input))
            except Exception:
                pass
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

        # Turn-Summary: Tools (Stream + Narrative) und Preview erfassen,
        # damit das Admin-Panel sehen kann was tatsaechlich passiert ist.
        try:
            _tool_names = list(_tool_exec_counts.keys()) + list(_narrative_exec_counts.keys())
            # Dedup, Reihenfolge erhalten.
            _seen = set()
            _turn_info["tools"] = [t for t in _tool_names if not (t in _seen or _seen.add(t))]
            _preview_src = (full_response or "").strip()
            if _preview_src:
                _turn_info["preview"] = (_preview_src[:140] + "…") if len(_preview_src) > 140 else _preview_src
        except Exception:
            pass

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
            if not _turn_info["preview"]:
                _turn_info["preview"] = "SKIP"
            return _turn_info

        # Suppress-Notification: nur Tool-Effekte behalten, Narrativ-Text
        # wird verworfen. Fuer Observer-Gedanken (Instagram-Reaction etc.)
        # wo der Context-Hint fremden Inhalt traegt, der nicht als Kiras
        # Gedanke in die Chat-History leaken darf.
        if suppress_notification:
            logger.info("%s: suppress_notification=True — "
                        "Narrativ wird verworfen, nur Tool-Effekte bleiben",
                        character_name)
            return _turn_info

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
                    return _turn_info
                ts = datetime.now()
                date_str = ts.strftime("%d.%m.%Y %H:%M")
                from app.models.account import get_player_identity as _get_pi_save
                save_message({
                    "role": "assistant",
                    "content": f"[Gedanken-Nachricht | {location_name} | {date_str}] {clean_content}",
                    "timestamp": ts.isoformat(),
                }, character_name, partner_name=_get_pi_save(""))
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

        # Intent-Extraktion aus Gedanken-Antwort. ``_tool_executions`` is
        # the list of tools the agent already ran during streaming —
        # process_response_intents skips INTENT markers that duplicate
        # any of them (same tool family, matching content blob).
        if full_response or notification_content:
            try:
                from app.core.intent_engine import process_response_intents
                from app.routes.scheduler import get_scheduler_manager
                _intents = process_response_intents(
                    full_response or notification_content, character_name, config,
                    get_scheduler_manager(),
                    executed_tools=_tool_executions,
                )
                try:
                    _turn_info["intents"] = [i.type for i in (_intents or [])]
                except Exception:
                    pass
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

        return _turn_info
