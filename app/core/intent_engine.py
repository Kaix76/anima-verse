"""Intent Engine — extracts character action commitments from chat responses and executes them.

Character emits: [INTENT: type | delay=0 | key=value]
Supported types: instagram_post, send_message, remind, execute_tool, change_outfit, describe_room
Delay formats: 0/now/sofort, 30m, 2h, 1d, HH:MM

LLM fallback (async, language-agnostic): triggers for responses > 150 chars without tag.
"""
import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from app.core.log import get_logger

logger = get_logger("intent_engine")


# ---------------------------------------------------------------------------
# Intent dataclass
# ---------------------------------------------------------------------------

@dataclass
class Intent:
    type: str
    delay_seconds: int = 0
    params: Dict[str, str] = field(default_factory=dict)
    raw: str = ""


# ---------------------------------------------------------------------------
# Tag parsing
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r'\[INTENT:\s*([^\]]+)\]', re.IGNORECASE)


def parse_intent_tags(text: str) -> List[Intent]:
    """Parse [INTENT: type | delay=... | key=value] tags from text."""
    intents = []
    for match in _TAG_RE.finditer(text):
        parts = [p.strip() for p in match.group(1).split('|')]
        if not parts or not parts[0].strip():
            continue
        intent_type = parts[0].strip()
        params: Dict[str, str] = {}
        for part in parts[1:]:
            if '=' in part:
                k, v = part.split('=', 1)
                params[k.strip()] = v.strip()
        delay_seconds = _parse_delay(params.pop('delay', '0'))
        intents.append(Intent(type=intent_type, delay_seconds=delay_seconds,
                               params=params, raw=match.group(0)))
    return intents


def strip_intent_tags(text: str) -> str:
    """Remove [INTENT: ...] tags from text before storing in history."""
    return _TAG_RE.sub('', text).strip()


def _parse_delay(delay_str: str) -> int:
    """Convert delay string to seconds.
    Supported: 0/now/sofort, 30m, 2h, 1d, HH:MM (today or tomorrow).
    """
    s = delay_str.strip().lower()
    if not s or s in ('0', 'now', 'sofort', 'immediately', 'jetzt'):
        return 0
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(s|sec|m|min|h|hr|d|day)', s)
    if m:
        v, unit = float(m.group(1)), m.group(2)
        factor = {'s': 1, 'sec': 1, 'm': 60, 'min': 60,
                  'h': 3600, 'hr': 3600, 'd': 86400, 'day': 86400}[unit]
        return int(v * factor)
    tm = re.fullmatch(r'(\d{1,2}):(\d{2})', s)
    if tm:
        now = datetime.now()
        target = now.replace(hour=int(tm.group(1)), minute=int(tm.group(2)),
                              second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return int((target - now).total_seconds())
    logger.warning("Unbekanntes delay-Format: %s", delay_str)
    return 0


# ---------------------------------------------------------------------------
# Execution routing
# ---------------------------------------------------------------------------

_KNOWN_TYPES = {"instagram_post", "send_message", "remind", "execute_tool", "change_outfit", "describe_room"}

# Lowercase identifier, 3-40 chars. Rejects template placeholders the LLM
# echoes verbatim from the system prompt (e.g. "<type>", "...", "key=value")
# which otherwise leak into the commitment-memory as ghost plans.
_PLAUSIBLE_INTENT_TYPE_RE = re.compile(r'^[a-z][a-z0-9_]{2,39}$')


def _is_plausible_intent_type(t: str) -> bool:
    return bool(t) and bool(_PLAUSIBLE_INTENT_TYPE_RE.match(t))

# Mapping: intent type -> tool names that, when executed with matching
# content in the same turn, make the INTENT marker redundant. RP-finetunes
# often emit BOTH a real <tool> call AND a [INTENT: ...] marker for the
# same action — running both duplicates the action.
_TOOL_FOR_INTENT = {
    "send_message":   {"SendMessage"},
    "instagram_post": {"Instagram", "InstagramPost"},
    "change_outfit":  {"ChangeOutfit", "OutfitChange"},
    "describe_room":  {"DescribeRoom"},
    # remind, execute_tool: no direct tool equivalent — always run via INTENT
}


def _normalize_text(s: str) -> str:
    """Collapse whitespace + lowercase for fuzzy comparison."""
    if not s:
        return ""
    return " ".join(s.split()).lower()


def _intent_payload(intent: "Intent") -> str:
    """Extract the comparable content blob from an INTENT marker."""
    p = intent.params or {}
    if intent.type == "send_message":
        return _normalize_text(p.get("content") or p.get("message") or "")
    if intent.type == "instagram_post":
        return _normalize_text(p.get("caption") or "")
    if intent.type == "change_outfit":
        return _normalize_text(p.get("hint") or p.get("style") or "")
    if intent.type == "describe_room":
        return _normalize_text(p.get("description") or "")
    return ""


def _tool_payload(tool_name: str, raw_input: str) -> str:
    """Extract the comparable content blob from a tool invocation's raw input.

    Tool inputs are either freetext ("Recipient, message...") or JSON.
    """
    if not raw_input:
        return ""
    s = raw_input.strip()
    if tool_name == "SendMessage":
        # Convention: "Name, message text" — first comma splits.
        parts = s.split(",", 1)
        return _normalize_text(parts[1] if len(parts) > 1 else s)
    if tool_name in ("Instagram", "InstagramPost"):
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                return _normalize_text(d.get("caption") or d.get("input") or "")
        except Exception:
            pass
        return _normalize_text(s)
    if tool_name in ("ChangeOutfit", "OutfitChange"):
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                return _normalize_text(d.get("hint") or d.get("style") or d.get("input") or "")
        except Exception:
            pass
        return _normalize_text(s)
    if tool_name == "DescribeRoom":
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                return _normalize_text(d.get("description") or d.get("input") or "")
        except Exception:
            pass
        return _normalize_text(s)
    return ""


def _is_intent_redundant(intent: "Intent",
                          executed_tools: Optional[List]) -> bool:
    """True when an INTENT marker matches a tool already executed in the
    same turn (same tool family, same/overlapping content blob).

    ``executed_tools``: list of ``(tool_name, raw_input)`` tuples captured
    by the tool executor during the streaming phase.

    Comparison: normalized text equality, OR one contains the other (≥30
    chars). Different content → both run; identical or near-identical →
    INTENT skipped.
    """
    if not executed_tools:
        return False
    candidate_tools = _TOOL_FOR_INTENT.get(intent.type)
    if not candidate_tools:
        return False
    intent_text = _intent_payload(intent)
    if not intent_text:
        return False
    for (tname, raw) in executed_tools:
        if tname not in candidate_tools:
            continue
        tool_text = _tool_payload(tname, raw)
        if not tool_text:
            continue
        if tool_text == intent_text:
            return True
        # Allow small variation — one is contained in the other
        shorter, longer = (tool_text, intent_text) if len(tool_text) <= len(intent_text) else (intent_text, tool_text)
        if len(shorter) >= 30 and shorter in longer:
            return True
    return False


def execute_intent(intent: Intent, character_name: str,
                   scheduler_manager: Any = None) -> None:
    """Route intent to TaskQueue (immediate) or Scheduler DateTrigger (deferred)."""
    if intent.delay_seconds == 0:
        _submit_to_task_queue(intent, character_name)
    elif scheduler_manager:
        _schedule_intent(intent, character_name, scheduler_manager)
    else:
        logger.warning("Kein SchedulerManager für deferred intent %s — sofortige Ausführung",
                       intent.type)
        _submit_to_task_queue(intent, character_name)


def _submit_to_task_queue(intent: Intent, character_name: str) -> None:
    try:
        from app.core.task_queue import get_task_queue
        payload = {"user_id": "", "agent_name": character_name, **intent.params}
        task_id = get_task_queue().submit(
            task_type=f"intent_{intent.type}",
            payload=payload,
            queue_name="default",
            agent_name=character_name)
        logger.info("Intent → TaskQueue: %s (task=%s)", intent.type, task_id)
    except Exception as e:
        logger.error("Intent TaskQueue submit: %s", e)


def _schedule_intent(intent: Intent, character_name: str,
                     scheduler_manager: Any) -> None:
    try:
        run_at = (datetime.now() + timedelta(seconds=intent.delay_seconds)).isoformat()
        job_id = f"intent_{character_name}_{int(datetime.now().timestamp())}_{intent.type}"

        if intent.type == "send_message":
            action = {
                "type": "send_message",
                "message": intent.params.get("message", ""),
                "character": character_name,
            }
        else:
            action = {
                "type": "execute_tool",
                "tool": intent.type,
                "params": {"user_id": "", "agent_name": character_name, **intent.params},
            }

        result = scheduler_manager.add_job(
            agent=character_name,
            trigger={"type": "date", "run_date": run_at, "one_time": True},
            action=action, job_id=job_id)
        delay_h = intent.delay_seconds / 3600
        logger.info("Intent → Scheduler: %s in %.1fh (job=%s)", intent.type, delay_h,
                    result.get("job_id"))
    except Exception as e:
        logger.error("Intent Scheduler submit: %s", e)


def _save_commitment(intent: Intent, character_name: str) -> None:
    """Fallback: store intent as memory so character remembers it."""
    try:
        from app.models.memory import add_memory
        content = f"Planned action: {intent.type}"
        if intent.params:
            content += f" — {json.dumps(intent.params, ensure_ascii=False)}"
        if intent.delay_seconds:
            h = intent.delay_seconds / 3600
            content += f" (in {h:.1f}h)"
        # importance=3 (kein Cleanup-Schutz): das ist ein automatisch erzeugter
        # Plan, kein vom User markierter wichtiger Commitment. source=intent
        # markiert die Provenance fuer spaeteren Event-Cleanup.
        add_memory(
            character_name=character_name,
            content=content, memory_type="commitment", importance=3,
            tags=["intent"],
            extra_meta={"source": "intent"})
        logger.info("Commitment → Memory: %s", intent.type)
    except Exception as e:
        logger.warning("Commitment Memory save: %s", e)


# ---------------------------------------------------------------------------
# TaskQueue handler registration + handlers
# ---------------------------------------------------------------------------

def register_intent_handlers() -> None:
    """Register TaskQueue handlers for all intent types. Call at server startup."""
    from app.core.task_queue import get_task_queue
    tq = get_task_queue()
    tq.register_handler("intent_instagram_post", _handle_instagram_post)
    tq.register_handler("intent_send_message", _handle_send_message)
    tq.register_handler("intent_remind", _handle_remind)
    tq.register_handler("intent_execute_tool", _handle_execute_tool)
    tq.register_handler("intent_change_outfit", _handle_change_outfit)
    tq.register_handler("intent_describe_room", _handle_describe_room)
    logger.info("Intent-Handler registriert")


def _handle_instagram_post(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Executes Instagram Skill to generate image + caption + post."""
    import json as _json
    user_id = payload.get("user_id", "")
    character_name = payload.get("agent_name", "")
    caption_hint = payload.get("caption", "")
    try:
        from app.core.dependencies import get_skill_manager
        sm = get_skill_manager()
        skill = sm.get_skill("Instagram")
        if not skill:
            return {"success": False, "error": "Instagram Skill nicht geladen"}
        # execute() expects JSON string with input, agent_name, user_id
        raw_input = _json.dumps({
            "input": caption_hint or "Erstelle einen Instagram-Post",
            "agent_name": character_name,
            "user_id": "",
        })
        result = skill.execute(raw_input)
        success = result and "Fehler" not in str(result)
        return {"success": success, "result": str(result)[:500]}
    except Exception as e:
        logger.error("Intent instagram_post fehlgeschlagen: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


def _handle_send_message(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Character sends a follow-up message via chat endpoint."""
    user_id = payload.get("user_id", "")
    character_name = payload.get("agent_name", "")
    message = payload.get("message", "")
    if not message:
        return {"success": False, "error": "Keine Nachricht"}
    try:
        import requests
        port = os.environ.get("PORT", "8000")
        resp = requests.post(
            f"http://localhost:{port}/chat/{user_id}",
            json={"agent": character_name, "message": message, "silent": True},
            timeout=60)
        return {"success": resp.ok, "status": resp.status_code}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _handle_remind(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Store reminder in character memory."""
    user_id = payload.get("user_id", "")
    character_name = payload.get("agent_name", "")
    note = (payload.get("note") or payload.get("message") or "").strip()
    if not note:
        logger.warning("Intent remind ohne note/message — kein Memory erzeugt (agent=%s)",
                       character_name)
        return {"success": False, "error": "missing_note"}
    try:
        from app.models.memory import add_memory
        add_memory(
            character_name=character_name,
            content=f"Reminder: {note}", memory_type="commitment", importance=4,
            tags=["reminder"],
            extra_meta={"source": "intent"})
        return {"success": True, "note": note}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _handle_execute_tool(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a named skill. Extend mapping as needed."""
    tool = payload.get("tool", "")
    logger.info("Intent execute_tool: %s (payload=%s)", tool, list(payload.keys()))
    return {"success": True, "tool": tool, "note": "Tool mapping pending"}


def _handle_change_outfit(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Executes OutfitChange Skill to generate a new outfit."""
    import json as _json
    user_id = payload.get("user_id", "")
    character_name = payload.get("agent_name", "")
    hint = payload.get("hint", payload.get("style", ""))
    try:
        from app.core.dependencies import get_skill_manager
        sm = get_skill_manager()
        skill = sm.get_skill("outfit_change")
        if not skill:
            return {"success": False, "error": "OutfitChange Skill nicht geladen"}
        raw_input = _json.dumps({
            "input": hint or "Wechsle dein Outfit",
            "agent_name": character_name,
            "user_id": "",
            "skip_daily_limit": True,
        })
        result = skill.execute(raw_input)
        success = result and "Fehler" not in str(result)
        return {"success": success, "result": str(result)[:500]}
    except Exception as e:
        logger.error("Intent change_outfit fehlgeschlagen: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


def _handle_describe_room(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Executes DescribeRoom Skill to describe or create a room."""
    import json as _json
    user_id = payload.get("user_id", "")
    character_name = payload.get("agent_name", "")
    location_id = payload.get("location_id", "")
    room = payload.get("room", "")
    description = payload.get("description", "")
    image_prompt = payload.get("image_prompt", "")
    try:
        from app.core.dependencies import get_skill_manager
        sm = get_skill_manager()
        skill = sm.get_skill("describe_room")
        if not skill:
            return {"success": False, "error": "DescribeRoom Skill nicht geladen"}
        skill_payload = {
            "location_id": location_id,
            "room": room,
            "description": description,
            "agent_name": character_name,
            "user_id": "",
        }
        if image_prompt:
            skill_payload["image_prompt"] = image_prompt
        raw_input = _json.dumps(skill_payload)
        result = skill.execute(raw_input)
        success = result and "Fehler" not in str(result)
        return {"success": success, "result": str(result)[:500]}
    except Exception as e:
        logger.error("Intent describe_room fehlgeschlagen: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Main entry point — called from chat.py post-stream
# ---------------------------------------------------------------------------

def process_response_intents(
    response: str, character_name: str,
    agent_config: Dict[str, Any],
    scheduler_manager: Any = None,
    executed_tools: Optional[List] = None) -> List[Intent]:
    """Sync entry: extract tag-based intents and execute/schedule them.

    ``executed_tools``: optional list of ``(tool_name, raw_input)`` tuples
    captured during the same streaming turn. INTENT markers whose action
    matches a tool already run with the same payload are skipped to avoid
    double-execution (e.g. duplicate Instagram posts when the LLM emits
    both ``<tool name="Instagram">`` and ``[INTENT: instagram_post]``).
    """
    intents = parse_intent_tags(response)
    for intent in intents:
        if _is_intent_redundant(intent, executed_tools):
            logger.info("INTENT skipped (redundant with executed tool): %s",
                        intent.type)
            continue
        if intent.type in _KNOWN_TYPES:
            execute_intent(intent, character_name, scheduler_manager)
        elif _is_plausible_intent_type(intent.type):
            _save_commitment(intent, character_name)
        else:
            logger.info("INTENT discarded (implausible type): %r", intent.type)
    return intents


async def process_response_intents_async(
    response: str, character_name: str,
    agent_config: Dict[str, Any],
    scheduler_manager: Any = None,
    executed_tools: Optional[List] = None) -> List[Intent]:
    """Async entry: tag-based intent extraction.

    The LLM fallback has been removed — the tag-based [INTENT: ...] path
    stays for the user-facing streaming chat where the chat-LLM emits
    intent markers. See ``process_response_intents`` for the
    ``executed_tools`` skip semantics.
    """
    intents = parse_intent_tags(response)
    for intent in intents:
        if _is_intent_redundant(intent, executed_tools):
            logger.info("INTENT skipped (redundant with executed tool): %s",
                        intent.type)
            continue
        if intent.type in _KNOWN_TYPES:
            execute_intent(intent, character_name, scheduler_manager)
        elif _is_plausible_intent_type(intent.type):
            _save_commitment(intent, character_name)
        else:
            logger.info("INTENT discarded (implausible type): %r", intent.type)
    return intents
