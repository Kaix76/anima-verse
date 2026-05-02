"""Streaming Agent - Kapselt LLM-Streaming, Tool-Erkennung und Agent-Loop.

Liefert strukturierte Events statt roher SSE-Strings, damit die
Chat-Route nur noch als duenner Consumer/Formatter agiert.

Vier Modi (automatisch erkannt via determine_mode()):
- no_tools: Reiner Chat, kein Tool-Support (1 LLM-Call)
- single:   Chat-LLM macht alles (1-2 Calls)
- dual:     Tool-LLM entscheidet, Chat-LLM antwortet (1-2 Calls)
- rp_first: Chat-LLM antwortet zuerst (sauberes RP), dann entscheidet
            Tool-LLM basierend auf RP-Antwort + User-Input ueber Tools.
            Post-RP Tools (Image, Outfit) → ausfuehren, anhaengen.
            Pre-RP Tools (Suche) → ausfuehren, RP-Antwort verwerfen,
            Chat-LLM nochmal mit Tool-Ergebnissen.
"""
import asyncio
import re
import time

from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple, Union

from app.core.log import get_logger
from app.utils.llm_logger import get_model_name

logger = get_logger("agent_loop")

# Regex fuer LLM-Tokenizer-Artefakte (z.B. <SPECIAL_28> von Mistral-Modellen)
_SPECIAL_TOKEN_RE = re.compile(r'<SPECIAL_\d+>|<\|[A-Z_]+\|>')

# Regex fuer Intent/Assignment/EventResolved Marker in Tool-LLM Antworten
_INTENT_RE = re.compile(r'\[INTENT:[^\]]+\]')
_ASSIGNMENT_RE = re.compile(r'\[NEW_ASSIGNMENT:[^\]]+\]')
_EVENT_RESOLVED_RE = re.compile(r'\[EVENT_RESOLVED:\s*([^\]]+)\]')
_MOOD_MARKER_RE = re.compile(r'\*\*I\s+feel\s+(.+?)\*\*', re.IGNORECASE)
_ACTIVITY_MARKER_RE = re.compile(r'\*\*I\s+do\s+(.+?)\*\*', re.IGNORECASE)
_LOCATION_MARKER_RE = re.compile(r'\*\*I\s+am\s+at\s+(.+?)\*\*', re.IGNORECASE)


def _extract_markers(tool_response: str, rp_response: str = "") -> str:
    """Extrahiert Marker aus Tool-LLM Antwort.

    Nimmt [INTENT:...], [NEW_ASSIGNMENT:...] und [EVENT_RESOLVED:...] uebernehmen.
    Zusaetzlich Fallback-Marker **I feel**, **I do**, **I am at** — aber nur
    wenn sie NICHT bereits im rp_response vorhanden sind (Duplikat-Vermeidung).
    """
    markers = []
    for m in _INTENT_RE.finditer(tool_response):
        markers.append(m.group(0))
    for m in _ASSIGNMENT_RE.finditer(tool_response):
        markers.append(m.group(0))
    for m in _EVENT_RESOLVED_RE.finditer(tool_response):
        markers.append(m.group(0))
    # Fallback-Marker: nur wenn im RP fehlend
    if not _MOOD_MARKER_RE.search(rp_response):
        mm = _MOOD_MARKER_RE.search(tool_response)
        if mm:
            markers.append(mm.group(0))
    if not _ACTIVITY_MARKER_RE.search(rp_response):
        am = _ACTIVITY_MARKER_RE.search(tool_response)
        if am:
            markers.append(am.group(0))
    if not _LOCATION_MARKER_RE.search(rp_response):
        lm = _LOCATION_MARKER_RE.search(tool_response)
        if lm:
            markers.append(lm.group(0))
    return "\n".join(markers)

# ---------------------------------------------------------------------------
# Search-Intent Keywords (User-Input Detection)
# ---------------------------------------------------------------------------
# Erkennt ob der User nach realen/aktuellen Informationen fragt.
# Wenn erkannt, wird ein Forcing-Hint zum System-Prompt hinzugefuegt.

_SEARCH_INTENT_KEYWORDS_DE = [
    "was ist passiert", "was passiert", "was gibt es neues",
    "aktuelle nachrichten", "aktuelle news", "neuigkeiten",
    "letzte 24 stunden", "letzten 24 stunden",
    "was wirklich passiert", "wirklich passiert",
    "echte nachrichten", "echte news", "echte informationen",
    "suche nach", "such mal", "recherchiere", "recherchier mal",
    "google mal", "schau mal nach", "schau nach",
    "was passiert gerade", "was passiert in der welt",
    "informationen aus dem internet", "aus dem netz",
    "gibt es neuigkeiten", "gibt es news",
    "hast du nachrichten", "was sagen die nachrichten",
    "breaking news", "schlagzeilen",
]

_SEARCH_INTENT_KEYWORDS_EN = [
    "what happened", "what's happening", "what is happening",
    "latest news", "recent news", "current events",
    "last 24 hours", "past 24 hours",
    "real information", "real news", "actually happened",
    "search for", "look up", "google",
    "what's going on in the world", "what's new",
    "any news", "news about", "headlines",
    "breaking news", "current situation",
]

_SEARCH_INTENT_KEYWORDS = _SEARCH_INTENT_KEYWORDS_DE + _SEARCH_INTENT_KEYWORDS_EN

_SEARCH_INTENT_HINT = (
    "\n\n[SYSTEM HINT: The user is asking about real-world events or current information. "
    "You MUST use WebSearch to find this information. Do NOT answer from memory or make up facts. "
    "Call WebSearch with a relevant search query BEFORE giving your response.]"
)

# ---------------------------------------------------------------------------
# Deferred Tool Input Enrichment
# ---------------------------------------------------------------------------

def _inject_rp_context(tool_input: str, rp_response: str, user_input: str = "") -> str:
    """Injiziert die RP-Antwort und die User-Eingabe in den Tool-Input.

    rp_response: Character-Antwort → Quelle fuer Character-Outfit-Aenderungen.
    user_input: User-Eingabe → Quelle fuer Avatar-Outfit-Aenderungen (z.B.
    "Ich ziehe die Jacke aus"). Ohne user_input kann der Extractor im
    Image-Skill Avatar-Aenderungen nur aus der Character-Antwort ableiten.
    """
    if not rp_response and not user_input:
        return tool_input

    import json as _json
    stripped = tool_input.strip()
    if stripped.startswith("{"):
        try:
            data = _json.loads(stripped)
            if rp_response:
                data["rp_context"] = rp_response
            if user_input:
                data["user_input"] = user_input
            return _json.dumps(data, ensure_ascii=False)
        except Exception:
            pass

    # Fallback: JSON-Wrapper um den Text-Input
    wrapper: Dict[str, Any] = {"prompt": tool_input}
    if rp_response:
        wrapper["rp_context"] = rp_response
    if user_input:
        wrapper["user_input"] = user_input
    return _json.dumps(wrapper, ensure_ascii=False)


# Sentinel fuer Stream-Ende (safe_anext)
_STREAM_END = object()


async def _safe_anext(aiter):
    """Wrapper um __anext__ der StopAsyncIteration in Sentinel wandelt.

    asyncio.create_task kann StopAsyncIteration nicht propagieren
    (wird zu RuntimeError). Dieser Wrapper faengt es ab.
    """
    try:
        return await aiter.__anext__()
    except StopAsyncIteration:
        return _STREAM_END

from app.core.tool_formats import (
    TOOL_FORMATS, find_tool_calls, find_stream_tool_call,
    find_direct_tool_call
)


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------

@dataclass
class ContentEvent:
    """LLM-Text-Chunk."""
    content: str


@dataclass
class ToolStartEvent:
    """Tool-Ausfuehrung beginnt."""
    tool_name: str


@dataclass
class ToolResultEvent:
    """Tool-Ergebnis."""
    tool_name: str
    result: str


@dataclass
class ToolErrorEvent:
    """Tool-Fehler."""
    tool_name: str
    error: str


@dataclass
class ToolEndEvent:
    """Tool-Ausfuehrung beendet."""
    tool_name: str


@dataclass
class DeferredToolEvent:
    """Signalisiert dass ein Deferred Tool nach der Chat-Antwort ausgefuehrt wird."""
    tool_name: str


@dataclass
class LoopInfoEvent:
    """Debug-Info pro Iteration."""
    iteration: int
    max_iterations: int
    chunks: int
    response_length: int


@dataclass
class HeartbeatEvent:
    """SSE-Keepalive waehrend stiller Phasen (Tool-LLM, Model-Loading)."""
    pass


@dataclass
class RetryHintEvent:
    """Signalisiert dem Frontend dass die Antwort verworfen und neu generiert wird."""
    reason: str = ""


@dataclass
class ExtractionEvent:
    """Extrahierte Marker aus Tool-LLM (Intent, Assignment) die an full_response angehaengt werden."""
    markers: str = ""


StreamEvent = Union[
    ContentEvent, ToolStartEvent, ToolResultEvent,
    ToolErrorEvent, ToolEndEvent, LoopInfoEvent, HeartbeatEvent, RetryHintEvent, ExtractionEvent
]


# ---------------------------------------------------------------------------
# Stream result container
# ---------------------------------------------------------------------------

@dataclass
class _StreamState:
    """Mutable container for _stream_llm_response results."""
    response: str = ""
    tool_matches: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# StreamingAgent
# ---------------------------------------------------------------------------

class StreamingAgent:
    """Fuehrt LLM-Streaming mit Tool-Erkennung aus.

    Drei Modi (via mode-Parameter):
    - no_tools: Reiner Chat, kein Tool-Support
    - single:   Ein LLM macht alles (Chat + Tools)
    - dual:     Tool-LLM entscheidet, Chat-LLM antwortet
    """

    def __init__(
        self,
        llm,
        tool_format: str,
        tools_dict: Dict[str, Callable],
        tool_executor: Optional[Callable] = None,
        agent_name: str = "",
        max_iterations: int = 3,
        tool_llm=None,
        tool_system_content: str = "", log_task: str = "",
        deferred_tools: Optional[set] = None,
        content_tools: Optional[set] = None,
        mode: str = "no_tools",
        constrained_tools: bool = False,
        chat_task_id: str = ""):
        self.llm = llm
        self.tool_llm = tool_llm or llm  # Fallback auf Chat-LLM wenn kein Tool-LLM konfiguriert
        self.tool_system_content = tool_system_content  # Minimaler System-Prompt fuer Tool-LLM
        self.tool_format = tool_format
        self.tools_dict = tools_dict
        self.tool_executor = tool_executor
        self.agent_name = agent_name
        self.max_iterations = max_iterations
        # chat_task_id: when set, the agent reports iteration progress back
        # to the LLMQueue so the admin queue panel can show "iter N/M".
        self.chat_task_id = chat_task_id
        self.user_id = ""
        # constrained_tools=True signalisiert: das tools_dict ist bewusst auf
        # eine Whitelist eingeschraenkt (z.B. forced_thought mit tool_whitelist).
        # Dann: tool_decision_input wird minimiert — nur Action→Tool-Mapping
        # fuer die verfuegbaren Tools, KEINE EXTRACTION/FALLBACK-MARKER-Sektion.
        self.constrained_tools = constrained_tools
        self.log_task = log_task  # z.B. "chat_stream", "thought"
        self.deferred_tools = deferred_tools or set()
        self.content_tools = content_tools or set()
        self.mode = mode

    # Mapping: Tool-Name → Action-Trigger-Beschreibung (fuer constrained Mode).
    _TOOL_ACTION_HINTS = {
        "TalkTo":           "Character speaks to someone present in the room",
        "SendMessage":      "Character writes a remote text message to someone NOT in the same room (other character OR the user/avatar — use this to proactively reach out)",
        "ChangeOutfit":     "Character changes/puts on/takes off clothes (e.g. gets dressed because not alone anymore)",
        "OutfitCreation":   "Character creates a brand new outfit (when nothing fitting exists)",
        "SetActivity":      "Character starts a different activity (e.g. stops what they were doing)",
        "SetLocation":      "Character moves to a different room/location (e.g. flees, gives privacy)",
        "ImageGenerator":   "Character takes a photo / makes an image",
        "Instagram":        "Character posts to Instagram",
        "InstagramComment": "Character comments on someone's Instagram post",
        "InstagramReply":   "Character replies to a comment on their post",
        "WebSearch":        "Character looks something up on the web",
        "KnowledgeSearch":  "Character searches their own knowledge",
        "KnowledgeExtract": "Character extracts/saves a fact from the conversation",
        "EditSelf":         "Character writes a new belief/lesson/goal into their own mind",
        "DescribeRoom":     "Character looks around and describes what they see",
        "VideoGenerator":   "Character records a short video",
    }

    def _action_mapping_for_available_tools(self) -> str:
        """Liste der Action→Tool-Hinweise nur fuer aktuell verfuegbare Tools."""
        lines = []
        for name in self.tools_dict.keys():
            hint = self._TOOL_ACTION_HINTS.get(name, f"Character triggers {name}")
            lines.append(f"  - {hint} → {name}")
        return "\n".join(lines) if lines else "  (no tools available)"

    # ------------------------------------------------------------------
    # Search-intent detection (user input)
    # ------------------------------------------------------------------

    def _detect_search_intent(self, user_input: str) -> bool:
        """Prueft ob der User nach realen/aktuellen Informationen fragt."""
        if not user_input or not self.tools_dict:
            return False
        if "WebSearch" not in self.tools_dict:
            return False
        input_lower = user_input.lower()
        for kw in _SEARCH_INTENT_KEYWORDS:
            if kw in input_lower:
                logger.info("Search-Intent erkannt: Keyword '%s' in User-Input", kw)
                return True
        return False

    # ------------------------------------------------------------------
    # Direct tool call detection
    # ------------------------------------------------------------------

    def check_direct_tool_call(self, user_input: str) -> Optional[Tuple[str, str]]:
        """Prueft ob user_input selbst ein Tool-Call ist (z.B. vom Scheduler)."""
        result = find_direct_tool_call(self.tool_format, user_input)
        if result:
            return result

        # Fallback: andere Formate pruefen
        for fmt_name in TOOL_FORMATS:
            if fmt_name != self.tool_format:
                result = find_direct_tool_call(fmt_name, user_input)
                if result:
                    return result
        return None

    # ------------------------------------------------------------------
    # Main streaming method — dispatches to mode-specific method
    # ------------------------------------------------------------------

    async def stream(
        self,
        system_content: str,
        history: List,
        user_input: str) -> AsyncGenerator[StreamEvent, None]:
        """Streamt LLM-Antwort mit optionaler Tool-Erkennung.

        Dispatcht basierend auf self.mode zu:
        - _stream_chat_only(): Reiner Chat (no_tools)
        - _stream_single(): Chat-LLM mit Tools (single)
        - _stream_rp_first(): Chat-LLM zuerst, Tool-LLM danach (rp_first)
        """
        if self.mode == "rp_first":
            method = self._stream_rp_first
        elif self.mode == "single":
            method = self._stream_single
        else:
            method = self._stream_chat_only

        async for event in method(system_content, history, user_input):
            yield event

    # ------------------------------------------------------------------
    # Mode: no_tools — reiner Chat, ein LLM-Call
    # ------------------------------------------------------------------

    async def _stream_chat_only(
        self, system_content: str, history: List, user_input: str) -> AsyncGenerator[StreamEvent, None]:
        """Reiner Chat ohne Tools — ein LLM-Call."""
        _start = time.monotonic()
        state = _StreamState()
        async for event in self._stream_llm_response(
            state, self.llm, system_content, history, user_input,
            llm_label="LLM", detect_tools=False, iteration=1):
            yield event
        logger.info("Beendet: 1 Iteration, %.2fs", time.monotonic() - _start)

    # ------------------------------------------------------------------
    # Mode: single — Chat-LLM macht alles (1-2 Calls)
    # ------------------------------------------------------------------

    async def _stream_single(
        self, system_content: str, history: List, user_input: str) -> AsyncGenerator[StreamEvent, None]:
        """Chat-LLM mit Tool-Support — max 2 Phasen.

        Phase 1: LLM-Call mit Tool-Instruktionen
          → Nur Text: fertig (1 Call)
          → Tool erkannt: ausfuehren, weiter zu Phase 2
        Phase 2: LLM-Call mit Tool-Ergebnissen + Original-Nachricht
        """
        _start = time.monotonic()
        _search_hint = _SEARCH_INTENT_HINT if self._detect_search_intent(user_input) else ""

        # Phase 1: LLM-Call mit Tool-Instruktionen
        state1 = _StreamState()
        async for event in self._stream_llm_response(
            state1, self.llm, system_content + _search_hint, history, user_input,
            llm_label="LLM", detect_tools=True, iteration=1):
            yield event

        if not state1.tool_matches:
            # Keine Tools — fertig (1 Call)
            async for event in self._run_deferred_tools(state1.response, user_input=user_input):
                yield event
            logger.info("Beendet: 1 Iteration (keine Tools), %.2fs", time.monotonic() - _start)
            return

        # Tools ausfuehren
        tool_results = []
        pending_deferred = []
        async for event in self._execute_tools(state1.tool_matches, tool_results, pending_deferred):
            yield event

        if not tool_results:
            # Nur Deferred Tools erkannt — kein 2. LLM-Call noetig
            # RP-Kontext fuer Deferred Tools ist die Phase-1-Antwort
            async for event in self._run_deferred_tools(state1.response, pending_deferred, user_input=user_input):
                yield event
            logger.info("Beendet: 1 Iteration (nur deferred), %.2fs", time.monotonic() - _start)
            return

        # max_iterations=1: Tools ausgefuehrt, kein Follow-up-Call
        # (z.B. fast=True Forced-Thoughts — nur Tool-Effekt zaehlt)
        if self.max_iterations <= 1:
            async for event in self._run_deferred_tools(state1.response, pending_deferred, user_input=user_input):
                yield event
            logger.info("Beendet: 1 Iteration (max_iterations=1, Tools ausgefuehrt), %.2fs",
                        time.monotonic() - _start)
            return

        # Phase 2: LLM-Call mit Tool-Ergebnissen im System-Prompt + Original user_input
        tool_context = self._format_tool_context(tool_results)
        enhanced_system = system_content + "\n\n" + tool_context
        state2 = _StreamState()
        async for event in self._stream_llm_response(
            state2, self.llm, enhanced_system, history, user_input,
            llm_label="LLM", detect_tools=False, iteration=2):
            yield event

        # Deferred Tools mit Phase-2-Antwort als RP-Kontext
        async for event in self._run_deferred_tools(state2.response, pending_deferred, user_input=user_input):
            yield event
        logger.info("Beendet: 2 Iterationen, %.2fs", time.monotonic() - _start)

    # ------------------------------------------------------------------
    # Mode: rp_first — Chat-LLM antwortet, Tool-LLM entscheidet danach
    # ------------------------------------------------------------------

    async def _stream_rp_first(
        self, system_content: str, history: List, user_input: str) -> AsyncGenerator[StreamEvent, None]:
        """RP zuerst, Tools danach.

        Phase 1: Chat-LLM antwortet (sauberes RP, OHNE Tool-Instruktionen)
        Phase 2: Tool-LLM bekommt User-Input + RP-Antwort → Tool-Entscheidung
        Phase 3 (nur bei CONTENT_TOOL): Chat-LLM nochmal MIT Tool-Ergebnissen

        Tool-Kategorien:
          DEFERRED (Post-RP): ausfuehren, Ergebnis anhaengen (Image, Video, Instagram)
          CONTENT_TOOL: ausfuehren, RP verwerfen, Chat-LLM Retry (WebSearch, KnowledgeSearch)
          Seiteneffekt: ausfuehren, nichts weiter (SetActivity, Outfit, TalkTo, ...)
        """
        _start = time.monotonic()

        # Phase 1: Chat-LLM RP (ohne Tools im Prompt)
        state_rp = _StreamState()
        async for event in self._stream_llm_response(
            state_rp, self.llm, system_content, list(history), user_input,
            llm_label="Chat-LLM", detect_tools=False, iteration=1):
            yield event

        rp_response = state_rp.response.strip()
        if not rp_response:
            logger.info("rp_first: leere RP-Antwort, beendet")
            return

        # SKIP (Gedanken-Modus)
        if rp_response.upper() == "SKIP":
            logger.info("rp_first: SKIP → keine Aktion, %.2fs", time.monotonic() - _start)
            return

        # Phase 2: Tool-LLM entscheidet + extrahiert (Intent, Assignment, Fallback-Marker).
        # Bei constrained_tools (forced_thought mit Whitelist): minimaler Prompt —
        # kein Action→Tool-Mapping fuer Tools die gar nicht verfuegbar sind, keine
        # EXTRACTION (Intent/Assignment/Event-Resolved sind Chat-Konzepte, nicht
        # Reactor-Konzepte), keine FALLBACK MARKER (Markersynthese braucht den
        # vollen Kontext, nicht das eingeschraenkte Reactor-Set).
        if self.constrained_tools:
            _action_lines = self._action_mapping_for_available_tools()
            tool_decision_input = (
                f"The user said: {user_input}\n\n"
                f"The character responded:\n{rp_response}\n\n"
                f"Analyze the response and call any tool that the character's narrative "
                f"action triggers. The character NEVER writes tool calls themselves; you do that.\n\n"
                f"Available action → tool mapping:\n"
                f"{_action_lines}\n\n"
                f"Call every tool that applies. Multiple tools are fine. "
                f"If the response is purely conversational/observational and triggers "
                f"none of these actions, respond with: NONE"
            )
        else:
            tool_decision_input = (
                f"The user said: {user_input}\n\n"
                f"The character responded:\n{rp_response}\n\n"
                f"Analyze this conversation and do THREE things:\n\n"
                f"1. TOOLS: The character's narrative actions ARE the trigger — you execute the tools "
                f"that enact those actions. The character NEVER writes tool calls themselves; that is your job.\n"
                f"   Action → Tool mapping (fire the tool whenever the narrative shows the action, even if "
                f"phrased indirectly like \"she goes to change\", \"puts on a dress\", \"takes a photo\", "
                f"\"heads to the kitchen\"):\n"
                f"     - Character changes/puts on/takes off clothes, outfit, dress, shirt, etc. → ChangeOutfit\n"
                f"     - Character takes a photo / makes an image / shows a picture → ImageGenerator\n"
                f"     - Character posts to Instagram / shares a photo publicly → Instagram\n"
                f"     - Character moves to a different location or room → SetLocation\n"
                f"     - Character starts a new activity at the same location → SetActivity\n"
                f"     - Character looks something up / searches / checks facts → KnowledgeSearch or WebSearch\n"
                f"     - Character relays info to a third party not in chat → TalkTo\n"
                f"   Do NOT skip a tool because the character \"only described\" the action narratively — "
                f"narrative description IS the signal to call the tool. Call every tool that applies; "
                f"multiple tools are fine.\n\n"
                f"2. EXTRACTION: Check the character's response for:\n"
                f"   - Intent: If the character commits to a concrete action (posting something, "
                f"sending a message, doing something at a specific time), output:\n"
                f"   [INTENT: <type> | delay=<0/30m/2h/1d> | key=value]\n"
                f"   Types: instagram_post, send_message, remind, execute_tool\n"
                f"   - Assignment: If the user gave a task/mission, output:\n"
                f"   [NEW_ASSIGNMENT: <title> | <role> | <description> | <priority 1-5> | <duration_minutes>]\n"
                f"   - Event resolved: If the character actively resolved/fixed a disruption or danger "
                f"event (repaired something, helped someone, fixed a problem), output:\n"
                f"   [EVENT_RESOLVED: <short description of what they did>]\n\n"
                f"3. FALLBACK MARKERS — CRITICAL: Your job here is to EMIT markers the character forgot. "
                f"Plain prose like 'I feel happy' or 'Ich fuehle mich gluecklich' WITHOUT double-asterisks "
                f"means the marker is MISSING — you MUST emit it. A marker only counts as already-present "
                f"when it is wrapped in '**...**' literally in the text.\n"
                f"   Decision rule:\n"
                f"     - Emotion clearly shown in RP AND no '**I feel <X>**' in RP → EMIT **I feel <emotion>**\n"
                f"     - New activity clearly started AND no '**I do <X>**' in RP → EMIT **I do <activity>**\n"
                f"     - Location marker: ONLY emit **I am at <location>** when the RP text EXPLICITLY "
                f"describes the character PHYSICALLY MOVING to a NEW location (verbs like 'I walk to', "
                f"'ich gehe in', 'arriving at', 'ankommen in'). Do NOT emit it when the character simply "
                f"STAYS at their current location — even if props, furniture, or scene details suggest "
                f"another place. If in doubt: do NOT emit the location marker. Props on a stage do not "
                f"mean the location changed.\n"
                f"   Examples (study carefully):\n"
                f"     RP ends with 'Ich fuehle mich... gluecklich.' (no asterisks) → EMIT **I feel gluecklich**\n"
                f"     RP ends with 'I feel manipulative.' (no asterisks) → EMIT **I feel manipulative**\n"
                f"     RP ends with '**I feel happy**' (has asterisks) → do NOT emit (already present)\n"
                f"     RP mentions 'sits on a chair' without walking/moving → do NOT emit any location marker\n"
                f"     RP says 'I walk to the kitchen' without '**I am at ...**' → EMIT **I am at Küche**\n"
                f"   Use the SAME language as the character's RP response (German → German word, English → English).\n"
                f"   For activity/location: match the EXACT name from the Known locations / Available activities "
                f"lists provided in your system prompt. Do NOT invent names.\n"
                f"   Do NOT skip this step just because the text mentions the emotion in prose — that is "
                f"EXACTLY when you must emit the marker. The prose mention is the SIGNAL.\n\n"
                f"If nothing applies, respond with: NONE"
            )

        tool_system = self.tool_system_content or system_content

        state_tool = _StreamState()
        async for event in self._stream_llm_response(
            state_tool, self.tool_llm, tool_system, [], tool_decision_input,
            llm_label="Tool-LLM", detect_tools=True, is_tool_decision=True,
            iteration=2):
            yield event

        # Extrahierte Marker (Intent, Assignment, Fallback-Mood/Activity/Location)
        _extracted = _extract_markers(state_tool.response, rp_response)
        if _extracted:
            yield ExtractionEvent(markers=_extracted)
            logger.info("rp_first: extrahierte Marker: %s", _extracted[:100])

        if not state_tool.tool_matches:
            logger.info("rp_first: keine Tools → fertig (2 Calls, %.2fs)",
                        time.monotonic() - _start)
            return

        # Klassifizieren: Post-RP / Content / Seiteneffekt
        post_rp_matches = []
        content_matches = []
        side_fx_matches = []
        for tm in state_tool.tool_matches:
            t_name = tm[0] if isinstance(tm, (list, tuple)) else tm.get("name", "")
            if t_name in self.deferred_tools:
                post_rp_matches.append(tm)
            elif t_name in self.content_tools:
                content_matches.append(tm)
            else:
                side_fx_matches.append(tm)

        logger.info("rp_first: %d post-rp, %d content, %d side-fx Tools",
                     len(post_rp_matches), len(content_matches), len(side_fx_matches))

        # Seiteneffekt-Tools: ausfuehren, nichts weiter
        if side_fx_matches:
            _dummy_results = []
            _dummy_deferred = []
            async for event in self._execute_tools(side_fx_matches, _dummy_results, _dummy_deferred):
                yield event

        # Post-RP Tools (DEFERRED): ausfuehren mit RP-Kontext
        if post_rp_matches:
            pending_deferred = []
            for tm in post_rp_matches:
                t_name = tm[0] if isinstance(tm, (list, tuple)) else tm.get("name", "")
                t_input = tm[1] if isinstance(tm, (list, tuple)) else tm.get("input", "")
                pending_deferred.append((t_name, t_input))
            async for event in self._run_deferred_tools(rp_response, pending_deferred, user_input=user_input):
                yield event

        # Content-Tools (CONTENT_TOOL): ausfuehren, RP verwerfen, Chat-LLM Retry
        if content_matches:
            logger.info("rp_first: %d Content-Tool(s) → RP wird neu generiert",
                        len(content_matches))

            tool_results = []
            _dummy_deferred = []
            async for event in self._execute_tools(content_matches, tool_results, _dummy_deferred):
                yield event

            if tool_results:
                yield RetryHintEvent(
                    reason="Zusaetzliche Informationen gefunden — Antwort wird aktualisiert...")

                # Phase 3: Chat-LLM nochmal MIT Tool-Ergebnissen (voller Prompt)
                tool_context = self._format_tool_context(tool_results)
                retry_system = system_content + "\n\n" + tool_context

                state_retry = _StreamState()
                async for event in self._stream_llm_response(
                    state_retry, self.llm, retry_system, list(history), user_input,
                    llm_label="Chat-LLM", detect_tools=False, iteration=3):
                    yield event

                logger.info("rp_first: Retry nach Content-Tools, 3 Calls, %.2fs",
                            time.monotonic() - _start)
                return

        logger.info("rp_first: beendet, %.2fs", time.monotonic() - _start)

    # ------------------------------------------------------------------
    # Core: LLM streaming with heartbeat, retry, buffer, tool detection
    # ------------------------------------------------------------------

    async def _stream_llm_response(
        self,
        state: _StreamState,
        active_llm,
        system_content: str,
        history: List,
        user_input: str,
        *,
        llm_label: str = "LLM",
        detect_tools: bool = False,
        is_tool_decision: bool = False,
        iteration: int = 1) -> AsyncGenerator[StreamEvent, None]:
        """Core LLM streaming.

        Handles heartbeat, retry (model-loading), buffer management,
        and tool detection. Populates state.response and state.tool_matches.

        Args:
            state: Mutable container populated with response and tool_matches
            active_llm: LLM to use for this call
            system_content: System prompt
            history: Message history (list of dicts)
            user_input: User message (always the original)
            llm_label: Label for logging ("LLM", "Tool-LLM", "Chat-LLM")
            detect_tools: Whether to scan for tool calls in stream
            is_tool_decision: If True, suppress ContentEvents (Tool-LLM phase)
            iteration: Iteration number for LoopInfoEvent
        """
        _active_model = get_model_name(active_llm)
        logger.info("Iteration %d (%s: %s)", iteration, llm_label, _active_model)

        # Push iteration progress to the queue panel (admin sees "iter 2/3").
        if self.chat_task_id:
            try:
                from app.core.llm_queue import get_llm_queue
                get_llm_queue().register_chat_iteration(
                    self.chat_task_id, iteration, self.max_iterations)
            except Exception:
                pass  # Non-fatal — display only.

        # Message-Liste aufbauen
        stream_messages = [{"role": "system", "content": system_content}]
        stream_messages.extend(history)
        stream_messages.append({"role": "user", "content": user_input})

        # Konfiguration
        _HEARTBEAT_INTERVAL = 15  # Sekunden
        _EMPTY_RETRIES = 8 if not is_tool_decision else 0
        _RETRY_WAIT = 30  # Sekunden zwischen Retries
        _TOOL_BUFFER_SIZE = 60

        iteration_response = ""
        chunk_count = 0
        _iter_start = time.monotonic()

        for _attempt in range(_EMPTY_RETRIES + 1):
            if _attempt > 0:
                logger.debug("Leere Antwort — retry %d/%d nach %ds (Model-Loading?)",
                             _attempt, _EMPTY_RETRIES, _RETRY_WAIT)
                # Warten mit Heartbeats
                _waited = 0
                while _waited < _RETRY_WAIT:
                    _sleep = min(_HEARTBEAT_INTERVAL, _RETRY_WAIT - _waited)
                    await asyncio.sleep(_sleep)
                    _waited += _sleep
                    yield HeartbeatEvent()

            # Per-Attempt Variablen
            iteration_response = ""
            chunk_count = 0
            tool_call_detected = False
            tool_call_end_pos = -1
            count_sent = 0
            unsent_buffer = ""

            try:
                _aiter = active_llm.astream(stream_messages).__aiter__()
            except Exception as _init_err:
                self._log_llm_error(
                    active_llm, system_content, user_input, llm_label,
                    _init_err, _iter_start)
                raise

            _stream_done = False
            _pending_task = None
            while not _stream_done:
                # Task nur erstellen wenn keiner laeuft
                if _pending_task is None:
                    _pending_task = asyncio.create_task(_safe_anext(_aiter))
                done, _ = await asyncio.wait(
                    {_pending_task}, timeout=_HEARTBEAT_INTERVAL
                )
                if not done:
                    # Timeout — Heartbeat senden, Task laeuft weiter
                    yield HeartbeatEvent()
                    continue
                # Task fertig — Ergebnis holen
                try:
                    chunk = _pending_task.result()
                except Exception as _chunk_err:
                    _pending_task = None
                    self._log_llm_error(
                        active_llm, system_content, user_input, llm_label,
                        _chunk_err, _iter_start, partial_response=iteration_response)
                    raise _chunk_err
                _pending_task = None
                if chunk is _STREAM_END:
                    _stream_done = True
                    continue

                chunk_count += 1
                if chunk_count == 1:
                    logger.debug("Erster Chunk (Typ: %s)", type(chunk).__name__)
                    if hasattr(chunk, 'content'):
                        logger.debug("  - content length: %d",
                                     len(chunk.content) if chunk.content else 0)

                if not (hasattr(chunk, 'content') and chunk.content):
                    continue

                iteration_response += chunk.content

                if tool_call_detected:
                    # Halluzinierten Content nach Tool-Call verwerfen
                    continue

                if detect_tools:
                    # --- Tool-Detection mit Buffer ---
                    unsent_buffer += chunk.content

                    # Inkrementelle Tool-Pattern-Erkennung
                    tool_check = find_stream_tool_call(
                        self.tool_format, iteration_response, self.tools_dict
                    )
                    if tool_check:
                        tool_call_detected = True
                        tool_call_end_pos = tool_check.end()
                        if not is_tool_decision:
                            # Nur Text VOR dem Tool-Call senden
                            tool_start_in_response = tool_check.start()
                            safe_chars = tool_start_in_response - count_sent
                            if safe_chars > 0:
                                safe_text = _SPECIAL_TOKEN_RE.sub('', unsent_buffer[:safe_chars])
                                if safe_text:
                                    count_sent += len(safe_text)
                                    yield ContentEvent(content=safe_text)
                        unsent_buffer = ""
                        logger.info("Tool-Pattern im Stream erkannt - stoppe Ausgabe")
                    elif not is_tool_decision:
                        # Buffer: letzten Zeichen zurueckhalten (koennten Tool-Anfang sein)
                        safe_len = max(0, len(unsent_buffer) - _TOOL_BUFFER_SIZE)
                        if safe_len > 0:
                            to_send = _SPECIAL_TOKEN_RE.sub('', unsent_buffer[:safe_len])
                            if to_send:
                                count_sent += len(to_send)
                                yield ContentEvent(content=to_send)
                            unsent_buffer = unsent_buffer[safe_len:]
                elif not is_tool_decision:
                    # --- Kein Tool-Detection — Content direkt senden ---
                    clean = _SPECIAL_TOKEN_RE.sub('', chunk.content)
                    if clean:
                        yield ContentEvent(content=clean)

            # --- Retry-Check ---
            if iteration_response:
                if is_tool_decision:
                    break  # Tool-LLM: jede Antwort akzeptieren (SKIP/NONE/Tool)
                if iteration_response.strip().upper() != "SKIP":
                    break  # Gueltige Chat-Antwort
                # SKIP von Chat-LLM → retry (Model-Loading-Artefakt)
                logger.warning("LLM antwortete mit 'SKIP' — retry (%d/%d)",
                               _attempt + 1, _EMPTY_RETRIES)
                continue
            # Leere Antwort → retry (falls Retries uebrig)

        # --- Stream beendet: Buffer flushen ---
        if not tool_call_detected and unsent_buffer and not is_tool_decision:
            flush_text = _SPECIAL_TOKEN_RE.sub('', unsent_buffer)
            if flush_text:
                count_sent += len(flush_text)
                yield ContentEvent(content=flush_text)

        # --- Logging ---
        if not iteration_response and chunk_count > 0:
            logger.warning("Stream empfing %d Chunks ohne Content (Model-Loading?)", chunk_count)
        elif not iteration_response:
            logger.warning("Stream leer nach %d Versuchen", _EMPTY_RETRIES + 1)

        if tool_call_detected:
            hallucinated_len = len(iteration_response) - tool_call_end_pos
            logger.debug("Halluzinierter Content verworfen: %d Zeichen", hallucinated_len)

        logger.debug("Empfangene Chunks: %d", chunk_count)
        logger.debug("Response Laenge: %d Zeichen", len(iteration_response))

        yield LoopInfoEvent(
            iteration=iteration,
            max_iterations=self.max_iterations,
            chunks=chunk_count,
            response_length=len(iteration_response))

        # --- LLM-Logging ---
        self._log_llm_call(
            active_llm, system_content, user_input, iteration_response,
            llm_label, _iter_start)

        # --- Tool-Matches extrahieren ---
        if detect_tools and iteration_response:
            state.tool_matches = find_tool_calls(
                self.tool_format, iteration_response, self.tools_dict
            )
            if state.tool_matches:
                logger.info("%d Tool-Match(es) erkannt", len(state.tool_matches))

        state.response = iteration_response

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tools(
        self,
        tool_matches: List[Tuple[str, str]],
        tool_results: List[Tuple[str, str]],
        pending_deferred: List[Tuple[str, str]]) -> AsyncGenerator[StreamEvent, None]:
        """Fuehrt Tools aus. Yields Events, befuellt tool_results und pending_deferred."""
        for tool_name, tool_input_text in tool_matches:
            if tool_name not in self.tools_dict:
                available = ", ".join(self.tools_dict.keys())
                logger.warning("Tool '%s' halluziniert (verfuegbar: %s)", tool_name, available)
                error_msg = (
                    f"Tool '{tool_name}' is not available. "
                    f"Available tools: {available}. "
                    f"Please retry using the correct tool name."
                )
                tool_results.append((tool_name, f"[{tool_name} error]: {error_msg}"))
                continue

            tool_input_text = tool_input_text.strip()

            # Deferred Tools: merken statt ausfuehren → Chat-LLM antwortet erst
            if tool_name in self.deferred_tools:
                pending_deferred.append((tool_name, tool_input_text))
                logger.info("Tool DEFERRED: %s (wird nach Chat-Antwort ausgefuehrt)", tool_name)
                continue

            logger.info("Tool ausfuehrt: %s", tool_name)
            logger.debug("Input: %s", tool_input_text[:80])

            yield ToolStartEvent(tool_name=tool_name)

            try:
                if self.tool_executor:
                    tool_result = await self.tool_executor(tool_name, tool_input_text)
                else:
                    tool_func = self.tools_dict[tool_name]
                    tool_result = await asyncio.to_thread(tool_func, tool_input_text)

                result_preview = tool_result[:200] if len(tool_result) > 200 else tool_result
                logger.debug("Ergebnis: %s", result_preview)

                yield ToolEndEvent(tool_name=tool_name)
                yield ToolResultEvent(tool_name=tool_name, result=tool_result)
                tool_results.append((tool_name, tool_result))

            except Exception as tool_error:
                error_msg = f"Fehler beim {tool_name}: {str(tool_error)}"
                logger.error("%s", error_msg)
                yield ToolEndEvent(tool_name=tool_name)
                yield ToolErrorEvent(tool_name=tool_name, error=error_msg)
                tool_results.append((tool_name, f"Fehler: {tool_error}"))

    # ------------------------------------------------------------------
    # Deferred tool execution (after chat response)
    # ------------------------------------------------------------------

    async def _run_deferred_tools(
        self, rp_response: str, pending_deferred: List[Tuple[str, str]] = None,
        user_input: str = "") -> AsyncGenerator[StreamEvent, None]:
        """Fuehrt Deferred Tools nach Chat-Antwort mit RP-Kontext-Injektion aus.

        user_input wird mit injiziert, damit Tools (z.B. Bildgenerierung)
        Avatar-Aenderungen aus der User-Eingabe extrahieren koennen.
        """
        if not pending_deferred:
            return
        rp_text = rp_response.strip() if rp_response else ""
        user_text = user_input.strip() if user_input else ""
        logger.info("DEFERRED TOOLS: %d Tool(s) nach Chat-Antwort (rp_context=%d, user_input=%d chars)",
                     len(pending_deferred), len(rp_text), len(user_text))
        for tool_name, tool_input_text in pending_deferred:
            if tool_name not in self.tools_dict:
                continue
            # RP-Kontext und User-Eingabe in den Tool-Input injizieren
            enriched_input = _inject_rp_context(tool_input_text, rp_text, user_text)
            logger.info("Deferred Tool ausfuehrt: %s", tool_name)
            yield DeferredToolEvent(tool_name=tool_name)
            yield ToolStartEvent(tool_name=tool_name)
            try:
                if self.tool_executor:
                    tool_result = await self.tool_executor(tool_name, enriched_input)
                else:
                    tool_func = self.tools_dict[tool_name]
                    tool_result = await asyncio.to_thread(tool_func, enriched_input)
                yield ToolEndEvent(tool_name=tool_name)
                yield ToolResultEvent(tool_name=tool_name, result=tool_result)
                logger.info("Deferred Tool Ergebnis: %s -> %s", tool_name, tool_result[:100])
            except Exception as def_err:
                logger.error("Deferred Tool Fehler: %s: %s", tool_name, def_err)
                yield ToolEndEvent(tool_name=tool_name)
                yield ToolErrorEvent(tool_name=tool_name, error=str(def_err))

    # ------------------------------------------------------------------
    # Tool result formatting (system prompt context)
    # ------------------------------------------------------------------

    def _format_tool_context(self, tool_results: List[Tuple[str, str]]) -> str:
        """Formatiert Tool-Ergebnisse als System-Prompt-Kontext.

        Ersetzt das alte tool_exchange/\"Bitte fortfahren\" System.
        """
        parts = ["[Ausgefuehrte Aktionen:]"]
        for name, result in tool_results:
            parts.append(f"- {name}: {result}")
        parts.append("")
        parts.append("Beziehe diese Ergebnisse natuerlich in deine Antwort ein.")
        parts.append(
            "WICHTIG: Bilder werden GENERIERT und in der Galerie gespeichert — "
            "sie werden NICHT gesendet, geschickt oder verschickt. "
            "Verwende NIEMALS Woerter wie 'gesendet', 'geschickt', 'verschickt', "
            "'gepostet' oder 'hochgeladen' wenn du ueber generierte Bilder sprichst. "
            "Sage stattdessen z.B. 'Ich habe ein Bild gemacht' oder "
            "'Schau mal, das Bild ist in meiner Galerie'."
        )
        parts.append(
            "Falls eine CAPTION enthalten ist, behandle sie NICHT als Beschreibung einer Aktion."
        )
        # Roleplay: Tool-Fehler stillschweigend behandeln (in-character bleiben).
        # Chatbots: Fehler normal kommunizieren (der User will wissen wenn etwas schiefging).
        try:
            if self.agent_name:
                from app.models.character_template import is_roleplay_character
                if is_roleplay_character(self.agent_name):
                    parts.append(
                        "Falls ein Tool-Ergebnis 'Fehler' enthaelt: erwaehne den Fehler NICHT, "
                        "entschuldige dich NICHT fuer technische Probleme. "
                        "Antworte einfach normal, als haettest du kein Tool benutzt."
                    )
        except Exception:
            pass
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # LLM Logging helpers
    # ------------------------------------------------------------------

    def _log_llm_call(self, active_llm, system_content, user_input,
                      response, llm_label, start_time):
        """Logs a completed LLM call."""
        if not self.log_task:
            return
        try:
            from app.utils.llm_logger import log_llm_call, estimate_tokens, get_max_tokens
            prov = self._resolve_provider(active_llm)
            log_llm_call(
                task=self.log_task,
                model=get_model_name(active_llm),
                agent_name=self.agent_name,
                provider=prov,
                system_prompt=system_content,
                user_input=user_input,
                response=response,
                duration_s=time.monotonic() - start_time,
                tokens_input=estimate_tokens(system_content + user_input),
                tokens_output=estimate_tokens(response),
                max_tokens=get_max_tokens(active_llm),
                llm_role=llm_label)
        except Exception as e:
            logger.error("LLM-Log Fehler: %s", e)

    def _log_llm_error(self, active_llm, system_content, user_input,
                       llm_label, error, start_time, partial_response=""):
        """Logs a failed LLM call."""
        if not self.log_task:
            return
        try:
            from app.utils.llm_logger import log_llm_call, estimate_tokens, get_max_tokens
            prov = self._resolve_provider(active_llm)
            log_llm_call(
                task=self.log_task,
                model=get_model_name(active_llm),
                agent_name=self.agent_name,
                provider=prov,
                system_prompt=system_content[:500],
                user_input=user_input[:500],
                response=partial_response,
                duration_s=time.monotonic() - start_time,
                tokens_input=estimate_tokens(system_content + user_input),
                max_tokens=get_max_tokens(active_llm),
                error=str(error),
                llm_role=llm_label)
        except Exception:
            pass

    def _resolve_provider(self, llm) -> str:
        """Resolves provider name from LLM client."""
        try:
            api_base = (
                getattr(llm, "openai_api_base", "")
                or getattr(llm, "base_url", "")
                or ""
            ).rstrip("/")
            if api_base:
                from app.core.provider_manager import get_provider_manager
                for name, prov in get_provider_manager().providers.items():
                    if prov.api_base.rstrip("/") == api_base:
                        return name
        except Exception:
            pass
        return ""
