"""
Tool Format System - Konfigurierbares Tool-Calling Format fuer verschiedene LLMs.

Jedes Format definiert:
- instruction: Wie das LLM Tool-Calls formatieren soll (fuer System-Prompt)
- example: Template fuer Beispiele (mit {tool_name} und {input} Platzhaltern)
- pattern: Regex zum Erkennen von Tool-Calls in der LLM-Antwort
- stream_pattern: Regex fuer fruehe Erkennung waehrend des Streamings
- direct_pattern: Regex fuer direkte Tool-Calls in User-Nachrichten
- format_call: Funktion zum Erzeugen eines Tool-Calls (z.B. fuer Scheduler)
"""
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from app.core.log import get_logger

logger = get_logger("tool_formats")


# ============================================================================
# Format-Definitionen
# ============================================================================

TOOL_FORMATS: Dict[str, Dict[str, Any]] = {
    "tag": {
        "name": "XML Tag",
        "description": "XML-artiges Tag-Format. Sehr zuverlaessig, da LLMs Tags gut kennen.",
        "instruction": (
            "To use a tool, write EXACTLY this format:\n"
            "<tool name=\"ToolName\">your detailed input here</tool>\n"
            "RULES:\n"
            "- The tool name must be EXACTLY one of the available tool names\n"
            "- Write your input between the opening and closing tags\n"
            "- Do NOT add any text after the closing </tool> tag\n"
            "- WRONG: I will use ImageGenerator to create...\n"
            "- RIGHT: <tool name=\"ImageGenerator\">[image description]</tool>"
        ),
        "example": '<tool name="{tool_name}">{input}</tool>',
        "pattern": r'<tool\s+name="(\w+)">([\s\S]*?)</tool>',
        "stream_pattern": r'<tool\s+name="(\w+)">([\s\S]*?)</tool>',
        "stream_start": r'<tool\s+name=',
        "direct_pattern": r'^<tool\s+name="(\w+)">([\s\S]*?)</tool>$',
    },
    "natural_en": {
        "name": "English Natural",
        "description": "Englisches natuerliches Format. Gut fuer englisch-trainierte Modelle.",
        "instruction": (
            "To use a tool, write EXACTLY this format:\n"
            "Use ToolName for: your detailed input here\n"
            "RULES:\n"
            "- Write EXACTLY 'Use' then the tool name then 'for:' WITH COLON\n"
            "- Then the details. Do NOT use brackets [] in real tool calls.\n"
            "- Example: Use ImageGenerator for: a beautiful sunset at the beach"
        ),
        "example": "Use {tool_name} for: {input}",
        "pattern": r"(?:I\s+)?[Uu]se\s+(\w+)\s+for:\s*(.*?)(?:\n|$)",
        "stream_pattern": r"(?:I\s+)?[Uu]se\s+(\w+)\s+for:\s*(.*?)(?:\n|$)",
        "stream_start": r"[Uu]se\s+\w+\s+for:",
        "direct_pattern": r"^(?:I\s+)?[Uu]se\s+(\w+)\s+for:\s*(.*?)$",
    },
    "natural_de": {
        "name": "German Natural",
        "description": "Deutsches natuerliches Format. Das bisherige Standard-Format.",
        "instruction": (
            "Um ein Tool zu nutzen, schreibe EXAKT dieses Format:\n"
            "Ich nutze ToolName für: deine detaillierte Eingabe hier\n"
            "REGELN:\n"
            "- Schreibe GENAU 'Ich nutze' gefolgt vom Tool-Namen (ein Wort)\n"
            "- Dann 'für:' MIT DOPPELPUNKT\n"
            "- Dann die Details/Beschreibung\n"
            "- FALSCH: Ich nutze die Skills des ImageGenerator für Dich\n"
            "- RICHTIG: Ich nutze ImageGenerator für: [Bildbeschreibung]"
        ),
        "example": "Ich nutze {tool_name} für: {input}",
        "pattern": r"(?:Ich\s+)?[Nn]utze\s+(\w+)\s+f(?:ü|ue)r:\s*(.*?)(?:\n|$)",
        "stream_pattern": r"(?:Ich\s+)?[Nn]utze\s+(\w+)\s+f(?:ü|ue)r:\s*(.*?)(?:\n|$)",
        "stream_start": r"[Nn]utze\s+\w+\s+f(?:ü|ue)r:",
        "direct_pattern": r"^(?:Ich\s+)?[Nn]utze\s+(\w+)\s+f(?:ü|ue)r:\s*(.*?)$",
    },
}


# ============================================================================
# Modell-zu-Format Bibliothek
# Mapping: Modell-Name (oder Teilstring) -> empfohlenes Format
# Wird von unten nach oben durchsucht - spezifischere Eintraege zuerst
# ============================================================================

MODEL_FORMAT_LIBRARY: Dict[str, str] = {
    # --- Grosse Modelle (>30B) - Tag-Format empfohlen ---
    "gpt-4": "tag",
    "gpt-3.5": "tag",
    "claude": "tag",
    "qwen3": "tag",
    "qwen2.5-coder": "tag",
    "deepseek": "tag",
    "codestral": "tag",
    "command-r": "tag",
    "gemma2": "tag",

    # --- Mittlere Modelle (7B-13B) ---
    "mistral": "natural_en",
    "llama3": "natural_en",
    "llama2": "natural_en",
    "phi3": "natural_en",
    "phi4": "tag",
    "gemma": "natural_en",
    "solar": "natural_en",
    "yi": "natural_en",
    "internlm": "natural_en",
    "glm": "tag",

    # --- Kleine/Uncensored Modelle ---
    "wizardlm": "natural_en",
    "wizard-vicuna": "natural_en",
    "dolphin": "natural_en",
    "openhermes": "natural_en",
    "nous-hermes": "natural_en",
    "neural-chat": "natural_en",
    "orca": "natural_en",
    "stablelm": "natural_en",
    "tinyllama": "natural_en",

    # --- Fallback ---
    "_default": "tag",
}


# ============================================================================
# Hilfsfunktionen
# ============================================================================

def get_format(format_name: str) -> Dict[str, Any]:
    """Gibt ein Tool-Format zurueck. Fallback auf 'tag' wenn nicht gefunden."""
    return TOOL_FORMATS.get(format_name, TOOL_FORMATS["tag"])


def get_format_for_model(model_name: str) -> str:
    """Ermittelt das empfohlene Format fuer ein Modell anhand der Bibliothek.

    Durchsucht MODEL_FORMAT_LIBRARY nach Teil-Matches im Modellnamen.
    """
    if not model_name:
        return MODEL_FORMAT_LIBRARY.get("_default", "tag")

    model_lower = model_name.lower()

    # Exakter Match zuerst
    if model_lower in MODEL_FORMAT_LIBRARY:
        return MODEL_FORMAT_LIBRARY[model_lower]

    # Teil-Match (laengster Match gewinnt)
    best_match = ""
    best_format = MODEL_FORMAT_LIBRARY.get("_default", "tag")

    for pattern, fmt in MODEL_FORMAT_LIBRARY.items():
        if pattern.startswith("_"):
            continue
        if pattern.lower() in model_lower and len(pattern) > len(best_match):
            best_match = pattern
            best_format = fmt

    return best_format


def format_example(format_name: str, tool_name: str, example_input: str) -> str:
    """Erzeugt ein Beispiel fuer den System-Prompt."""
    fmt = get_format(format_name)
    return fmt["example"].format(tool_name=tool_name, input=example_input)


_DEFAULT_TOOL_INSTRUCTION = (
    "WHEN TO USE TOOLS:\n"
    "- The user asks about current events, news, real-world facts, what happened recently, "
    "or anything requiring up-to-date information → you MUST call WebSearch. "
    "Do NOT answer from memory, do NOT make up information.\n"
    "- The user asks for an image or picture → use ImageGenerator\n"
    "- The user asks for a search or to look something up → use WebSearch or KnowledgeSearch\n"
    "- The user asks to go somewhere or change location → use the location tool\n"
    "HOW: Write your in-character response, then add the tool call at the end. "
    "The system will execute the tool automatically.\n"
    "TOOL INPUT RULES: When a tool expects JSON input, field values must be plain text — "
    "NEVER put JSON objects or tool tags inside field values."
)

# Zusatzklausel nur fuer Roleplay-Characters (Chatbots koennen/sollen Tools frei nutzen)
_ROLEPLAY_TOOL_NOUSE_CLAUSE = (
    "WHEN NOT TO USE TOOLS:\n"
    "- The user is just chatting, asking about your feelings, or discussing fiction/roleplay."
)


def _get_tool_instruction_for_model(model_name: str) -> str:
    """Laedt die tool_instruction aus model_capabilities.json fuer ein Modell.

    Fallback auf _DEFAULT_TOOL_INSTRUCTION wenn nicht konfiguriert.
    """
    if not model_name:
        return _DEFAULT_TOOL_INSTRUCTION
    try:
        from app.core.model_capabilities import get_model_capabilities
        caps = get_model_capabilities(model_name)
        custom = caps.get("tool_instruction", "")
        if custom:
            return custom
    except Exception:
        pass
    return _DEFAULT_TOOL_INSTRUCTION


def build_tool_instruction(format_name: str, tools: List[Any],
                           appearance: str = "", usage_instructions: str = "",
                           model_name: str = "",
                           photographer_mode: bool = False,
                           user_appearance: str = "",
                           is_roleplay: bool = True) -> str:
    """Baut den kompletten Tool-Instruktions-Block fuer den System-Prompt.

    Args:
        format_name: Name des Tool-Formats (tag, natural_en, natural_de)
        tools: Liste der verfuegbaren Tools (mit .name und .description)
        appearance: Agent-Appearance fuer Appearance-Hinweis
        usage_instructions: Skill-spezifische Nutzungsanweisungen
        model_name: Modellname fuer modellspezifische Tool-Instruktionen
        photographer_mode: True wenn Agent=Fotograf (nicht im Bild)
        user_appearance: User-Appearance (fuer Photographer-Modus)
        is_roleplay: True fuer RP-Characters (fuegt "WHEN NOT TO USE TOOLS"
            Klausel hinzu die Chatting/Feelings/Fiction aus Tool-Calls ausschliesst).
            Chatbots = False.
    """
    fmt = get_format(format_name)

    parts = ["\n\n=== AVAILABLE TOOLS ==="]
    for tool in tools:
        parts.append(f"- {tool.name}: {tool.description}")

    parts.append("\n=== HOW TO USE TOOLS ===")
    parts.append(fmt["instruction"])

    # Appearance-Hinweis fuer ImageGenerator
    tool_names = [t.name for t in tools]
    if "ImageGenerator" in tool_names:
        if photographer_mode:
            # Photographer-Modus: Agent ist Fotograf, nicht im Bild
            photographer_hint = (
                "\nYou are a PHOTOGRAPHER. When generating images, describe ONLY the subjects "
                "you are photographing. Do NOT include yourself or your own appearance in the "
                "image description. When the user says 'Foto von mir' or 'photo of me', "
                "they mean themselves — describe THEM, not yourself."
            )
            if user_appearance:
                photographer_hint += f"\nThe user's appearance: {user_appearance}"
            parts.append(photographer_hint)
        elif appearance:
            # Normal-Modus: Agent-Appearance fuer Selbstbilder
            parts.append(
                f"\nWhen generating images of yourself, always include your appearance: {appearance}"
            )

    # Skill-spezifische Beispiele (jeweils eine Zeile pro Skill)
    if usage_instructions:
        for line in usage_instructions.split('\n'):
            if line.strip():
                parts.append(f"- {line.strip()}")

    # Modellspezifische Tool-Instruktion (aus model_capabilities.json)
    instruction = _get_tool_instruction_for_model(model_name)
    parts.append(f"\n{instruction}")

    # RP-only: Hinweis dass Chatting/Feelings/Fiction keine Tools triggern
    if is_roleplay:
        parts.append(_ROLEPLAY_TOOL_NOUSE_CLAUSE)

    return "\n".join(parts)


def build_minimal_tool_reminder(format_name: str, tool_names: List[str]) -> str:
    """Baut einen minimalen Tool-Reminder fuer den reduzierten System-Prompt."""
    fmt = get_format(format_name)
    # Zeige Format-Schema mit ToolName-Platzhalter statt konkretem Tool
    # damit das LLM nicht auf ein bestimmtes Tool biased wird
    schema = fmt["example"].format(
        tool_name="ToolName",
        input="your input"
    )
    return (
        f"\n\nAvailable tools: {', '.join(tool_names)}"
        f"\nOnly use a tool when the user ASKS for it. Always respond with conversation FIRST."
        f"\nEXACT format: {schema}"
        f"\nReplace ToolName with the EXACT tool name from the list above."
    )


def _is_placeholder_input(tool_input: str) -> bool:
    """Erkennt ob ein Tool-Input ein halluzinierter Platzhalter ist.

    Kleine LLMs kopieren oft die Beispiele aus dem System-Prompt als echte Tool-Calls.
    Diese Funktion filtert offensichtliche Platzhalter-Inputs heraus.
    """
    stripped = tool_input.strip()
    if not stripped:
        return False
    # "[search query or question]", "[detailed image description]", "[mood/feeling]"
    if stripped.startswith("[") and stripped.endswith("]"):
        return True
    # "your detailed input here", "deine detaillierte Eingabe hier", "your input"
    if stripped.lower() in ("your detailed input here", "deine detaillierte eingabe hier", "your input"):
        return True
    return False


def find_tool_calls(format_name: str, text: str,
                    known_tools: Optional[Dict] = None) -> List[Tuple[str, str]]:
    """Findet alle Tool-Calls in einem Text.

    Prueft ALLE bekannten Formate (nicht nur das konfigurierte),
    da LLMs oft ein anderes Format verwenden als angewiesen.

    Args:
        format_name: Name des konfigurierten Tool-Formats (wird zuerst geprueft)
        text: Der zu durchsuchende Text
        known_tools: Optional - Dict der bekannten Tools fuer Fallback-Matching

    Returns:
        Liste von (tool_name, tool_input) Tuples
    """
    raw_matches = []

    # 1. Konfiguriertes Format zuerst pruefen
    fmt = get_format(format_name)
    matches = re.findall(fmt["pattern"], text, re.IGNORECASE)
    if matches:
        raw_matches = [(name, inp.strip()) for name, inp in matches]
    else:
        # 2. Alle anderen Formate durchprobieren
        for other_name, other_fmt in TOOL_FORMATS.items():
            if other_name == format_name:
                continue
            matches = re.findall(other_fmt["pattern"], text, re.IGNORECASE)
            if matches:
                logger.debug("Tool erkannt via '%s' Format (konfiguriert: '%s')", other_name, format_name)
                raw_matches = [(name, inp.strip()) for name, inp in matches]
                break

        # 3. Fallback: Flexibles Matching mit bekannten Tool-Namen
        if not raw_matches and known_tools:
            tool_names_pattern = "|".join(re.escape(name) for name in known_tools.keys())
            # Universaler Fallback: Tool-Name gefolgt von fuer:/for: und Text
            # Doppelpunkt ist OBLIGATORISCH (verhindert Matches auf Fliesstext)
            fallback = rf"(?:[Nn]utze|[Uu]se)\s+({tool_names_pattern})\s+(?:f(?:ü|ue)r|for):\s*(.*?)(?:\n|$)"
            matches = re.findall(fallback, text, re.IGNORECASE)
            if matches:
                # Mehrere Fallback-Matches = Massen-Halluzination → alle verwerfen
                if len(matches) > 1:
                    logger.debug("Fallback: %d Matches gefunden - Halluzination, alle verworfen", len(matches))
                    return []
                logger.debug("Fallback-Pattern hat Tool erkannt: %s", matches)
                raw_matches = [(name, inp.strip()) for name, inp in matches]

    if not raw_matches:
        return []

    # Verschachtelte Tag-Tool-Calls aufloesen:
    # Wenn ein LLM das schliessende </tool> vergisst, landet der naechste
    # <tool name="..."> im Input des vorherigen Calls. Hier aufsplitten.
    _nested_tag = re.compile(r'<tool\s+name="(\w+)">([\s\S]*)', re.IGNORECASE)
    split_matches = []
    for name, inp in raw_matches:
        nested = _nested_tag.search(inp)
        if nested:
            clean_inp = inp[:nested.start()].strip()
            split_matches.append((name, clean_inp))
            nested_name = nested.group(1)
            nested_inp = re.sub(r'</tool>\s*$', '', nested.group(2)).strip()
            split_matches.append((nested_name, nested_inp))
            logger.debug("Verschachtelter Tool-Call aufgeloest: %s + %s", name, nested_name)
        else:
            split_matches.append((name, inp))
    raw_matches = split_matches

    # Massen-Halluzination erkennen: Wenn ein Tool-Name mehrfach vorkommt
    # UND die Inputs identisch sind, sind die Calls halluziniert.
    # Unterschiedliche Inputs = legitime Mehrfach-Nutzung (z.B. mehrere Raeume beschreiben).
    from collections import defaultdict
    tool_inputs_by_name = defaultdict(list)
    for name, inp in raw_matches:
        tool_inputs_by_name[name].append(inp)
    hallucinated_tools = set()
    for name, inputs in tool_inputs_by_name.items():
        if len(inputs) > 1:
            unique_inputs = set(inputs)
            if len(unique_inputs) == 1:
                # Alle Inputs identisch → Halluzination
                hallucinated_tools.add(name)
                logger.debug("Halluzination erkannt (identische Inputs): %s (%dx)", name, len(inputs))
            else:
                logger.debug("Mehrfach-Call mit unterschiedlichen Inputs akzeptiert: %s (%dx)", name, len(inputs))

    # Platzhalter, Duplikate und halluzinierte Tools filtern
    filtered = []
    for name, inp in raw_matches:
        # "ToolName" ist der Platzhalter aus der Instruktion
        if name.lower() == "toolname":
            logger.debug("Platzhalter-Tool 'ToolName' uebersprungen")
            continue
        # Platzhalter-Inputs wie "[search query or question]"
        if _is_placeholder_input(inp):
            logger.debug("Platzhalter-Input uebersprungen: %s -> %.60s", name, inp)
            continue
        # Tools mit identischen Mehrfach-Calls sind halluziniert → alle verwerfen
        if name in hallucinated_tools:
            continue
        filtered.append((name, inp))

    return filtered


def find_stream_tool_call(format_name: str, text: str,
                          known_tools: Optional[Dict] = None) -> Optional[re.Match]:
    """Prueft ob ein Tool-Call im Streaming-Text erkannt wird.

    Prueft ALLE bekannten Formate, nicht nur das konfigurierte.

    Returns:
        re.Match Objekt wenn gefunden, sonst None
    """
    # 1. Konfiguriertes Format zuerst
    fmt = get_format(format_name)
    match = re.search(fmt["stream_pattern"], text, re.IGNORECASE)
    if match:
        return match

    # 2. Alle anderen Formate durchprobieren
    for other_name, other_fmt in TOOL_FORMATS.items():
        if other_name == format_name:
            continue
        match = re.search(other_fmt["stream_pattern"], text, re.IGNORECASE)
        if match:
            return match

    # 3. Universaler Fallback mit bekannten Tool-Namen
    # WICHTIG: Pattern muss mit find_tool_calls()-Fallback uebereinstimmen!
    # Doppelpunkt obligatorisch, keine Extra-Woerter zwischen Use/Nutze und Tool-Name
    if known_tools:
        tool_names_pattern = "|".join(re.escape(name) for name in known_tools.keys())
        fallback = rf"(?:[Nn]utze|[Uu]se)\s+({tool_names_pattern})\s+(?:f(?:ü|ue)r|for):\s*(.*?)(?:\n|$)"
        match = re.search(fallback, text, re.IGNORECASE)
        if match:
            return match

    return None


def find_direct_tool_call(format_name: str, text: str) -> Optional[Tuple[str, str]]:
    """Prueft ob der gesamte Text ein direkter Tool-Call ist (z.B. vom Scheduler).

    Prueft ALLE bekannten Formate, nicht nur das konfigurierte.

    Returns:
        (tool_name, tool_input) Tuple wenn gefunden, sonst None
    """
    stripped = text.strip()

    # 1. Konfiguriertes Format zuerst
    fmt = get_format(format_name)
    match = re.match(fmt["direct_pattern"], stripped, re.IGNORECASE | re.DOTALL)
    if match:
        return (match.group(1), match.group(2).strip())

    # 2. Alle anderen Formate durchprobieren
    for other_name, other_fmt in TOOL_FORMATS.items():
        if other_name == format_name:
            continue
        match = re.match(other_fmt["direct_pattern"], stripped, re.IGNORECASE | re.DOTALL)
        if match:
            return (match.group(1), match.group(2).strip())

    return None


