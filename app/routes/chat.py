"""Chat routes - Main chat endpoint with character conversations"""
import asyncio
import base64
import os
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from app.core.log import get_logger
from app.core.chat_task_manager import get_chat_task_manager

logger = get_logger("chat")
from app.core.dependencies import get_skill_manager
from app.core.streaming import StreamingAgent, ContentEvent
from app.models.account import (
    get_user_name,
    get_user_profile,
    get_user_appearance)
from app.models.character import (
    get_character_address_form,
    build_equipped_outfit_prompt,
    get_character_config,
    get_character_profile,
    get_character_appearance,
    get_character_language_instruction,
    get_location_changed_at,
    get_character_current_location,
    get_character_current_activity,
    get_character_current_room,
    save_character_current_feeling,
    save_character_current_location,
    save_character_current_activity,
    is_character_sleeping,
    list_available_characters,
    get_character_images_dir)
from app.models.world import get_location, get_activity, get_room_by_id, list_locations, get_location_name, resolve_location
from app.models.character_template import (
    resolve_profile_tokens, get_template, build_prompt_section)
from app.models.chat import get_chat_history, save_message
from app.models.memory import build_memory_prompt_section, record_mood as record_mood_history
from app.models.events import build_events_prompt_section
from app.utils.llm_logger import estimate_tokens, get_model_name
from app.utils.history_manager import get_time_based_history, get_cached_summary, build_daily_summary_prompt_section

router = APIRouter(prefix="/chat", tags=["chat"])


def _strip_tool_hallucinations(text: str) -> str:
    """Entfernt halluzinierte Tool-Tags aus LLM-Antworten.

    Chat-LLMs schreiben manchmal <tool name="..."> Tags als Text statt
    echte Tool-Calls. Diese muessen bereinigt werden bevor die Antwort
    gespeichert oder dem LLM als History praesentiert wird.
    """
    if not text:
        return text

    # Send-Message-Template-Halluzination: "[Name, ]deine Antwort: '...'"
    # vom alten Hint-Wording. Prefix abschneiden, den eigentlichen Inhalt behalten.
    _meta = re.match(
        r'^(?:[A-Z][\wÄÖÜäöüß \-]{0,30},\s*)?(?:deine|meine|seine|ihre)\s+Antwort:\s*[\'\"]?(.*)$',
        text, flags=re.IGNORECASE | re.DOTALL)
    if _meta:
        text = _meta.group(1).rstrip("'\"").strip()

    if "<tool" in text:
        # <tool name="...">...</tool> (geschlossen)
        text = re.sub(r'<tool\s+name="[^"]*">[\s\S]*?</tool>', '', text)
        # <tool name="...">... (ungeschlossen — bis naechstes <tool oder Textende)
        text = re.sub(r'<tool\s+name="[^"]*">[^<]*', '', text)
    # *An:* / *Betreff:* / *Text:* Bloecke (Notification-Halluzinationen)
    text = re.sub(r'\n\s*\*(?:An|Betreff|Text|Target|Message):\*\s*[^\n]*', '', text, flags=re.IGNORECASE)
    # Mehrfache Leerzeilen
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# --- History Endpoint (muss vor /{user_id} stehen) ---

def _get_chat_partner() -> str:
    """Read the current chat partner (who we talk TO) — per-user via account."""
    from app.models.account import get_chat_partner
    return get_chat_partner()


@router.get("/{user_id}/history")
async def chat_history(limit: int = 2, offset: int = 0) -> Dict[str, Any]:
    """Gibt die letzten N Chat-Nachrichten zurueck (fuer Page-Reload).

    offset=0 bedeutet die neuesten Nachrichten, offset=2 ueberspringt die 2 neuesten usw.
    """
    current_agent = _get_chat_partner()
    if not current_agent:
        return {"messages": [], "agent_name": "", "total": 0}

    history = get_chat_history(current_agent)
    if not history:
        return {"messages": [], "agent_name": current_agent, "total": 0}

    total = len(history)
    if offset:
        last_messages = history[-(limit + offset):-offset] if (limit + offset) <= total else history[:max(0, total - offset)]
    else:
        last_messages = history[-limit:] if limit else history

    # Halluzinierten Send-Message-Template-Prefix in der angezeigten History
    # bereinigen ([Name, ]deine Antwort: "..."). Der Prefix kam vom alten
    # send_message-Hint und ist in vielen Bestands-Messages enthalten.
    _meta_re = re.compile(
        r'^(?:[A-Z][\wÄÖÜäöüß \-]{0,30},\s*)?(?:deine|meine|seine|ihre)\s+Antwort:\s*[\'\"]?',
        re.IGNORECASE)
    cleaned_messages = []
    for m in last_messages:
        if m.get("role") == "assistant":
            c = m.get("content", "")
            cleaned = _meta_re.sub('', c).rstrip("'\"").strip()
            if cleaned and cleaned != c:
                m = dict(m)
                m["content"] = cleaned
        cleaned_messages.append(m)
    return {"messages": cleaned_messages, "agent_name": current_agent, "total": total}


@router.get("/unread-summary")
async def chat_unread_summary() -> Dict[str, Any]:
    """Liefert pro Character den Timestamp der NEUESTEN Assistant-Message
    in dessen Chat mit dem aktuellen Avatar PLUS die letzten 30 Timestamps
    (fuer FE-seitige Unread-Count-Berechnung gegen localStorage seen-ts).

    Returns: {avatar, chats: {char: {latest, recent: [ts, ...]}}}
    """
    from app.models.account import get_active_character, get_user_name
    avatar = get_active_character() or get_user_name() or ""
    if not avatar:
        return {"avatar": "", "chats": {}}

    from app.core.db import get_connection
    try:
        conn = get_connection()
        # Pro Character: alle Assistant-Timestamps der letzten 7 Tage holen.
        # Limit 30 pro Character damit Antwort kompakt bleibt.
        rows = conn.execute(
            "SELECT character_name, ts FROM chat_messages "
            "WHERE partner=? AND role='assistant' AND character_name<>? "
            "  AND ts > datetime('now', '-7 days') "
            "ORDER BY ts DESC LIMIT 500",
            (avatar, avatar)).fetchall()
        per_char: Dict[str, Dict[str, Any]] = {}
        for char, ts in rows:
            if not char:
                continue
            slot = per_char.setdefault(char, {"latest": "", "recent": []})
            if not slot["latest"]:
                slot["latest"] = ts or ""
            if len(slot["recent"]) < 30:
                slot["recent"].append(ts or "")
        chats = per_char
    except Exception as e:
        return {"avatar": avatar, "chats": {}, "error": str(e)}
    return {"avatar": avatar, "chats": chats}


# --- Chat Image Upload Endpoint ---

from app.core.paths import get_storage_dir as _get_storage_dir

def _get_chat_upload_dir() -> Path:
    d = _get_storage_dir() / "chat_uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d

ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


@router.post("/{user_id}/upload-image")
async def upload_chat_image(request: Request) -> Dict[str, Any]:
    """Upload an image for use in chat. Returns a temporary image ID."""
    form = await request.form()
    file = form.get("file")
    if not file or not hasattr(file, "filename"):
        raise HTTPException(status_code=400, detail="No file uploaded")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")

    image_id = f"{uuid.uuid4().hex[:12]}{ext}"
    dest = _get_chat_upload_dir() / image_id
    content = await file.read()
    dest.write_bytes(content)

    return {"image_id": image_id, "filename": file.filename}


@router.get("/{user_id}/upload-image/{image_id}")
async def get_chat_upload(image_id: str):
    """Serve an uploaded chat image."""
    from fastapi.responses import Response
    if ".." in image_id or "/" in image_id:
        raise HTTPException(status_code=400, detail="Invalid image ID")
    path = _get_chat_upload_dir() / image_id
    if not path.exists():
        raise HTTPException(status_code=404, detail="Image not found")

    ext = path.suffix.lower()
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp", "gif": "image/gif"}.get(ext.lstrip("."), "image/png")
    return Response(content=path.read_bytes(), media_type=mime)


@router.get("/{user_id}/image-library")
async def chat_image_library(character: str = None) -> Dict[str, Any]:
    """List images from character libraries for linking in chat.

    If character is specified, returns only that character's images.
    Otherwise returns images from all characters.
    """
    from app.models.character import get_character_images, list_available_characters

    result = {}
    if character:
        characters = [character]
    else:
        characters = list_available_characters()

    for char_name in characters:
        images = get_character_images(char_name)
        if images:
            result[char_name] = [
                {
                    "filename": img,
                    "url": f"/characters/{char_name}/images/{img}",
                }
                for img in images[:50]  # Limit per character
            ]

    return {"characters": result}


# --- Visualize Endpoint (muss vor /{user_id} stehen) ---


@router.post("/detect-characters")
async def detect_characters(request: Request) -> Dict[str, Any]:
    """Erkennt im Text erwaehnte Characters und liefert alle verfuegbaren zurueck."""
    data = await request.json()
    user_id = data.get("user_id", "")
    agent_name = data.get("agent_name", "")
    text = data.get("text", "")

    if not agent_name:
        raise HTTPException(status_code=400, detail="user_id and agent_name required")

    # Erkannte Personen aus Text
    from app.core.prompt_builder import PromptBuilder as _PB
    _det_persons = _PB(agent_name).detect_persons(text) if text else []
    detected = [{"name": p.name, "appearance": p.appearance} for p in _det_persons]
    detected_names = [p.name for p in _det_persons]

    # Alle verfuegbaren Characters
    all_chars = list_available_characters()

    # Avatar = der Character den der User gerade steuert (falls gesetzt).
    # Wenn ein Avatar existiert, repraesentiert DIESER den User im Bild —
    # der blosse Login-Name (z.B. "admin") wird NICHT als extra Eintrag
    # gelistet, das waere eine Doppelung. Nur wenn kein Avatar gewaehlt ist
    # (reiner Admin ohne Rollenspiel-Character), wird der Login-Name angezeigt.
    from app.models.account import get_active_character
    avatar_name = (get_active_character() or "").strip()
    user_profile = get_user_profile()
    user_name = user_profile.get("user_name", "")

    available = []
    # Agent zuerst
    if agent_name in all_chars:
        available.append({"name": agent_name, "type": "agent"})
    # Avatar als "Du" (statt Login-Name)
    if avatar_name and avatar_name != agent_name:
        available.append({"name": avatar_name, "type": "user"})
    elif not avatar_name and user_name:
        # Kein Avatar — Login-Name als Fallback-"Du"
        available.append({"name": user_name, "type": "user"})
    # Andere Characters
    for c in all_chars:
        if c == agent_name or c == avatar_name:
            continue
        available.append({"name": c, "type": "character"})

    # Rooms der aktuellen Location
    rooms = []
    current_room_id = ""
    location_id = get_character_current_location(agent_name) or ""
    if location_id:
        loc_data = get_location(location_id)
        if loc_data:
            for room in loc_data.get("rooms", []):
                rooms.append({"id": room.get("id", ""), "name": room.get("name", "")})
        current_room_id = get_character_current_room(agent_name) or ""

    return {
        "detected": detected_names,
        "available": available,
        "rooms": rooms,
        "current_room_id": current_room_id,
        "location_id": location_id,
    }


@router.post("/visualize")
async def visualize(request: Request) -> Dict[str, Any]:
    """Generiert ein Bild aus einer Bot-Nachricht."""
    data = await request.json()
    user_id = data.get("user_id", "")
    agent_name = data.get("agent_name", "")
    text = data.get("text", "")
    workflow = data.get("workflow", "")
    backend = data.get("backend", "")
    loras = data.get("loras")  # Optional: [{name, strength}, ...]
    model_override = data.get("model_override", "").strip()
    character_names = data.get("character_names")  # Optional: explizite Character-Auswahl
    item_ids = data.get("item_ids") or []  # Optional: markierte Raum-Items als Props

    if not agent_name or not text:
        raise HTTPException(status_code=400, detail="user_id, agent_name and text required")

    logger.info("Start: user=%s agent=%s text=%s...", agent_name, text[:80])

    # Mood- und Location-Zeile entfernen (kommt manchmal aus dem Frontend-Text mit)
    text = re.sub(r'\n?\*{0,2}I feel\s+[^*\n]+\*{0,2}\s*$', '', text, flags=re.IGNORECASE).strip()
    text = re.sub(r'\n?\*{0,2}I am at\s+[^*\n]+\*{0,2}\s*$', '', text, flags=re.IGNORECASE).strip()
    text = re.sub(r'\n?\*{0,2}I do\s+[^*\n]+\*{0,2}\s*$', '', text, flags=re.IGNORECASE).strip()

    # 1. Appearances via PromptBuilder sammeln
    from app.core.prompt_builder import (
        PromptBuilder, is_photographer_mode, detect_selfie)
    _builder = PromptBuilder(agent_name)
    if character_names is not None:
        persons = _builder.detect_persons(text, character_names=character_names)
        logger.info("Explizite Character-Auswahl: %s", character_names)
    else:
        persons = _builder.detect_persons(text)

    agent_config_early = get_character_config(agent_name)
    is_photographer = is_photographer_mode(agent_name)
    is_selfie = detect_selfie(text)

    # Photographer-Filter zentral via Builder. Liefert die Subjects fuer
    # die LLM-Prompt-Generierung; appearances bleibt die volle Liste fuer
    # die Response (character_names) und execute() wendet den Filter
    # idempotent erneut an.
    llm_persons = _builder.apply_photographer_filter(
        persons,
        photographer_mode=is_photographer,
        is_selfie=is_selfie,
        set_profile=False)

    appearances = [{"name": p.name, "appearance": p.appearance} for p in persons]
    llm_appearances = [{"name": p.name, "appearance": p.appearance} for p in llm_persons]
    logger.debug("Appearances: %s", [p.name for p in persons])

    # 2. Szenen-Kontext aus Character-State aufbauen (nur Location + Activity)
    scene_parts = []
    try:
        raw_location = get_character_current_location(agent_name)
        if raw_location:
            loc_data = get_location(raw_location)
            loc_name = loc_data.get("name", raw_location) if loc_data else raw_location
            loc_desc = loc_data.get("description", "") if loc_data else ""
            scene_parts.append(f"Location: {loc_name}" + (f" ({loc_desc})" if loc_desc else ""))

        raw_activity = get_character_current_activity(agent_name)
        if raw_activity:
            act_data = get_activity(raw_activity)
            act_desc = act_data.get("description", "") if act_data else ""
            scene_parts.append(f"Activity: {raw_activity}" + (f" ({act_desc})" if act_desc else ""))
    except Exception as e:
        logger.error("Szenen-Kontext Fehler: %s", e)

    setting_context = "Setting: " + ", ".join(scene_parts) if scene_parts else ""

    # 3. Workflow-spezifische Prompt-Anweisungen ermitteln
    workflow_instruction = ""
    workflow_image_model = ""
    if workflow:
        sm = get_skill_manager()
        for skill in sm.skills:
            if skill.__class__.__name__ == "ImageGenerationSkill":
                for wf in getattr(skill, "comfy_workflows", []):
                    if wf.name == workflow:
                        workflow_image_model = wf.image_model
                        workflow_instruction = wf.prompt_instruction
                break

    # Image-Prompt via LLM generieren (mit Szenen-Kontext)
    agent_config = agent_config_early

    logger.debug("Generiere Image-Prompt via LLM...")
    image_prompt = await asyncio.to_thread(
        _generate_image_prompt, text, llm_appearances,
        setting_context, agent_config, workflow_image_model, workflow_instruction,
        is_photographer)
    if not image_prompt:
        logger.error("Image-Prompt leer")
        return {"error": "Failed to generate image prompt"}

    logger.debug("Image-Prompt: %s...", image_prompt[:120])

    # 4. Bild generieren via ImageGenerationSkill
    #    Wenn explizite character_names gesetzt: Appearances durchreichen,
    #    damit execute() diese statt der Auto-Detection verwendet
    vis_appearances = appearances if character_names is not None else None
    logger.debug("Generiere Bild via ImageGenerationSkill...")
    result = await asyncio.to_thread(
        _generate_visualization_image, agent_name, image_prompt,
        vis_appearances, workflow, backend, loras, model_override, item_ids)

    # Character-Namen in Response einfuegen (fuer Regenerierung)
    result["character_names"] = [p["name"] for p in appearances]

    logger.debug("Ergebnis: %s", list(result.keys()))
    return result



# _collect_appearances() und _resolve_appearances_by_names() wurden durch
# PromptBuilder.detect_persons() in app/core/prompt_builder.py ersetzt.


def _generate_image_prompt(
    text: str, appearances: List[Dict[str, str]],
    setting_context: str = "",
    agent_config: Dict[str, Any] = None, image_model: str = "",
    prompt_instruction: str = "",
    photographer_mode: bool = False) -> str:
    """Generiert einen Image-Prompt via Router (Task: image_prompt)."""
    # Character-Kontext fuer Pronomen-Aufloesung
    _agent_name = (agent_config or {}).get("name", "")
    _user_name = ""
    try:
        _user_name = get_user_name() or ""
    except Exception:
        pass

    # Pronomen-Mapping aufbauen: "Ich/mir/mich" = Agent, "Du/dir/dich" = User
    identity_lines = []
    if _agent_name:
        identity_lines.append(
            f'In the text, first-person pronouns ("Ich", "mir", "mich", "mein") refer to {_agent_name}.'
        )
    if _user_name:
        identity_lines.append(
            f'Second-person pronouns ("Du", "dir", "dich", "dein") refer to {_user_name}.'
        )
    identity_context = " ".join(identity_lines)

    # Workflow-spezifische Anweisungen
    model_context = f"The target image model is: {image_model}. " if image_model else ""
    instruction_context = f"{prompt_instruction} " if prompt_instruction else ""

    # Photographer-Kontext: Agent ist Fotograf, nicht im Bild
    _agent_name = (agent_config or {}).get("name", "")
    photographer_context = ""
    if photographer_mode and _agent_name:
        photographer_context = (
            f"IMPORTANT: {_agent_name} is the PHOTOGRAPHER and is NOT visible in the image. "
            f"Do NOT describe {_agent_name} or any of their actions (camera, photographing, etc.). "
            "Only describe the subjects who are being photographed. "
        )

    try:
        from app.core.llm_router import llm_call
        from app.core.prompt_templates import render_task
        _agent_name = (agent_config or {}).get("name", "")
        characters_present_block = ""
        if appearances:
            # Nur Namen senden – das LLM braucht sie fuer Pronomen-Aufloesung.
            # Volle Appearance-Texte werden spaeter vom ImageGenerationSkill
            # in den Prompt eingefuegt; hier wuerden sie vom LLM oft dupliziert.
            app_names = ", ".join(p["name"] for p in appearances)
            characters_present_block = f"Characters present: {app_names}"

        system_prompt, human_msg = render_task(
            "image_prompt_scene",
            model_context=model_context,
            instruction_context=instruction_context,
            photographer_context=photographer_context,
            identity_context=identity_context,
            scene_text=text,
            setting_block=setting_context,
            characters_present_block=characters_present_block)

        response = llm_call(
            task="image_prompt",
            system_prompt=system_prompt,
            user_prompt=human_msg,
            agent_name=_agent_name)
        return (response.content or "").strip()
    except Exception as e:
        logger.error("LLM error: %s", e)
        return ""


def _generate_visualization_image(agent_name: str, image_prompt: str,
    appearances: Optional[List[Dict[str, str]]] = None,
    workflow: str = "", backend: str = "",
    loras: Optional[List[Dict[str, Any]]] = None,
    model_override: str = "",
    item_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Generiert ein Bild via ImageGenerationSkill."""
    sm = get_skill_manager()
    img_skill = None
    for skill in sm.skills:
        if skill.__class__.__name__ == "ImageGenerationSkill":
            img_skill = skill
            break

    if not img_skill:
        return {"error": "ImageGenerationSkill not available"}

    payload = {
        "prompt": image_prompt,
        "agent_name": agent_name,
        "user_id": "",
        "set_profile": False,
        "skip_gallery": False,
        "auto_enhance": True,
    }
    if appearances is not None:
        payload["appearances"] = appearances
    if workflow:
        payload["workflow"] = workflow
    if backend:
        payload["backend"] = backend
    if loras is not None:
        payload["loras"] = loras
    if model_override:
        payload["model_override"] = model_override
    if item_ids:
        payload["item_ids"] = item_ids
    input_json = json.dumps(payload)

    result_text = img_skill.execute(input_json)
    logger.debug("Visualize result: %s", result_text[:200])

    # Fehler-String erkennen
    if result_text.startswith("Fehler:"):
        return {"error": result_text}

    # Extrahiere Bild-URLs aus dem Ergebnis
    image_urls = re.findall(r'!\[.*?\]\((\/characters\/[^)]+)\)', result_text)
    comment = ""
    # Kommentar extrahieren (Text nach dem letzten Bild)
    parts = re.split(r'!\[.*?\]\(\/characters\/[^)]+\)', result_text)
    if parts:
        comment = parts[-1].strip()

    return {
        "image_urls": image_urls,
        "comment": comment,
        "prompt_used": image_prompt,
    }


# --- Instagram Post Endpoint (muss vor /{user_id} stehen) ---


def _extract_image_description_from_text(text: str) -> str:
    """Extrahiert die Bild-Beschreibung aus der Bot-Antwort.

    Erkennt folgende Formate:
    - **(Bild: ...)** — Hauptformat (Kira etc.)
    - (Bild: ...) — ohne Bold
    - **Bild-Beschreibung:** ...
    - **Bildbeschreibung:** ...
    - **Image Description:** ...
    - **Bild-Prompt:** ...
    Returns leeren String wenn nichts gefunden.
    """
    # Prioritaet 1: **(Bild: ...)** oder (Bild: ...) — Klammer-Format
    paren_patterns = [
        r'\*{0,2}\(Bild:\s*(.+?)\)\*{0,2}',
        r'\*{0,2}\(Bild-?[Bb]eschreibung:\s*(.+?)\)\*{0,2}',
    ]
    for pattern in paren_patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            desc = match.group(1).strip()
            # "wird generiert" o.ae. ignorieren
            if 'wird generiert' in desc.lower():
                continue
            desc = re.sub(r'`[^`]*`', '', desc).strip()
            if len(desc) > 20:
                logger.debug("Bild-Beschreibung aus (Bild:) extrahiert (%d chars)", len(desc))
                return desc

    # Prioritaet 2: **Bild-Beschreibung:** Header-Format
    header_patterns = [
        r'\*\*Bild-?[Bb]eschreibung:?\*\*:?\s*(.+?)(?=\n\s*\n|\n\*\*|\Z)',
        r'\*\*Image\s+[Dd]escription:?\*\*:?\s*(.+?)(?=\n\s*\n|\n\*\*|\Z)',
        r'\*\*Bild-?[Pp]rompt:?\*\*:?\s*(.+?)(?=\n\s*\n|\n\*\*|\Z)',
    ]
    for pattern in header_patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            desc = match.group(1).strip()
            desc = re.sub(r'`[^`]*`', '', desc).strip()
            if len(desc) > 20:
                logger.debug("Bild-Beschreibung aus Header extrahiert (%d chars)", len(desc))
                return desc
    return ""


def _clean_caption(text: str) -> str:
    """Bereinigt Caption-Text von Metadaten und Artefakten."""
    # Gedanken-Nachricht Prefix entfernen (interne Metadaten)
    text = re.sub(r'^\[Gedanken-Nachricht[^\]]*\]\s*', '', text)
    # Assignment-Tags entfernen (interne Metadaten)
    text = re.sub(r'\n?\[ASSIGNMENT_(?:UPDATE|DONE):\s*[^\]]+\]', '', text)
    text = re.sub(r'\n?\[NEW_ASSIGNMENT:\s*[^\]]+\]', '', text)
    # @username · vor X Min. Zeilen entfernen
    text = re.sub(r'@[\w_]+\s*·?\s*(?:vor\s+\d+\s+\w+\.?|just\s+now)?\s*', '', text)
    # `@username` (backtick-Format) entfernen
    text = re.sub(r'`@[^`]*`\s*', '', text)
    # "Bildtext:" / "Caption:" / "Post Text:" Prefix entfernen
    text = re.sub(r'^(?:Bildtext|Caption|Post\s*Text|Text\s*\d*)\s*:\s*', '', text, flags=re.MULTILINE)
    # Bare timestamps (HH:MM) am Ende oder alleinstehend entfernen
    text = re.sub(r'\s+\d{1,2}:\d{2}\s*$', '', text)
    text = re.sub(r'^\d{1,2}:\d{2}\s*$', '', text, flags=re.MULTILINE)
    # **I feel <emotion>** oder "I feel <emotion>." Mood-Marker entfernen
    text = re.sub(r'\*\*I\s+feel\s+[^*]+\*\*\.?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\bI\s+feel\s+\w+(?:\s+\w+)?\.?\s*$', '', text, flags=re.IGNORECASE)
    # Fuehrende Hashtag-Gruppe entfernen wenn danach noch ausreichend Caption-Text folgt
    leading_match = re.match(r'^((?:\s*#\w+\s*)+)(.*)', text, re.DOTALL)
    if leading_match:
        after_tags = leading_match.group(2).strip()
        # Nur entfernen wenn genug echter Text nach den Hashtags kommt
        non_tag_text = re.sub(r'#\w+', '', after_tags).strip()
        if len(non_tag_text) > 15:
            text = after_tags
    # Anfuehrungszeichen um gesamten Text entfernen
    text = text.strip()
    if len(text) > 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1].strip()
    return ' '.join(text.split()).strip()


def _extract_caption_from_text(text: str) -> str:
    """Extrahiert eine Instagram-Caption aus der Bot-Antwort.

    Strategien (in Reihenfolge):
    0. Text nach letztem Bild-Marker (Bild:...) — zuverlaessigstes Pattern
    1. Blockquote mit Hashtags (> ...)
    2. Absatz mit 3+ Hashtags (bereinigt um Bild-Patterns und RP-Aktionen)
    3. Caption-Text nach @username-Zeile
    4. Konversationstext ohne Bild-Beschreibung/RP-Aktionen (Fallback)
    Returns leeren String wenn nichts Brauchbares gefunden.
    """
    # --- Strategie 0: Text nach letztem Bild-Marker ---
    # Bot-Antworten: [RP-Text] + [(Bild:...)] + [Caption] + [Hashtags]
    bild_end_pos = -1
    # (Bild: ...) Format
    for m in re.finditer(r'\*{0,2}\(Bild[^)]*\)\*{0,2}', text, re.DOTALL):
        if 'wird generiert' not in m.group().lower():
            bild_end_pos = max(bild_end_pos, m.end())
    # **Bild-Beschreibung:** Header-Format
    for m in re.finditer(
        r'\*\*Bild-?[Bb]eschreibung:?\*\*:?\s*.+?(?=\n\s*\n|\Z)', text, re.DOTALL
    ):
        bild_end_pos = max(bild_end_pos, m.end())

    if bild_end_pos > 0:
        after_bild = text[bild_end_pos:].strip()
        if after_bild:
            # RP-Aktionen entfernen (*text*)
            after_bild = re.sub(r'\*[^*]+\*', '', after_bild)
            # Weitere Bild-Muster entfernen
            after_bild = re.sub(r'\*{0,2}\(Bild[^)]*\)\*{0,2}', '', after_bild)
            # Bold-Header entfernen
            after_bild = re.sub(r'^\*\*[^*]+\*\*:?\s*$', '', after_bild, flags=re.MULTILINE)
            after_bild = re.sub(r'^#{1,6}\s+.*$', '', after_bild, flags=re.MULTILINE)
            after_bild = ' '.join(after_bild.split()).strip()
            # Markdown-Bold um gesamten Text entfernen
            after_bild = re.sub(r'^\*\*(.+)\*\*$', r'\1', after_bild)
            after_bild = _clean_caption(after_bild)
            if len(after_bild) > 15:
                logger.debug("Caption nach Bild-Beschreibung extrahiert (%d chars)", len(after_bild))
                return after_bild

    # --- Strategie 1: Blockquote-Bereich mit Hashtags (> Zeilen) ---
    hashtags_in_text = re.findall(r'#\w+', text)
    if len(hashtags_in_text) >= 3:
        blockquote_lines = []
        in_block = False
        for line in text.split('\n'):
            stripped = line.strip()
            if stripped.startswith('>'):
                in_block = True
                blockquote_lines.append(stripped.lstrip('> ').strip())
            elif in_block and not stripped:
                blockquote_lines.append("")
            else:
                if in_block and blockquote_lines:
                    block_text = ' '.join(l for l in blockquote_lines if l).strip()
                    block_text = _clean_caption(block_text)
                    if len(re.findall(r'#\w+', block_text)) >= 3 and len(block_text) > 20:
                        logger.debug("Caption aus Blockquote extrahiert (%d chars)", len(block_text))
                        return block_text
                in_block = False
                blockquote_lines = []
        if blockquote_lines:
            block_text = ' '.join(l for l in blockquote_lines if l).strip()
            block_text = _clean_caption(block_text)
            if len(re.findall(r'#\w+', block_text)) >= 3 and len(block_text) > 20:
                logger.debug("Caption aus Blockquote extrahiert (%d chars)", len(block_text))
                return block_text

        # --- Strategie 2: Absatz mit 3+ Hashtags ---
        paragraphs = re.split(r'\n\s*\n', text)
        for idx, para in enumerate(paragraphs):
            para_hashtags = re.findall(r'#\w+', para)
            if len(para_hashtags) >= 3:
                # Bild-Beschreibungen und RP-Aktionen entfernen
                cleaned = re.sub(r'\*{0,2}\(Bild[^)]*\)\*{0,2}', '', para, flags=re.DOTALL)
                cleaned = re.sub(r'\*[^*]+\*', '', cleaned)
                cleaned = re.sub(r'\*\*[^*]*\*\*:?\s*', '', cleaned)
                cleaned = re.sub(r'^>\s*', '', cleaned, flags=re.MULTILINE)
                cleaned = re.sub(r'^#{1,6}\s+.*$', '', cleaned, flags=re.MULTILINE)
                cleaned = ' '.join(cleaned.split()).strip()
                # Wenn Absatz nur Hashtags enthaelt, vorherigen Absatz dazunehmen
                non_hashtag = re.sub(r'#\w+', '', cleaned).strip()
                if len(non_hashtag) < 10 and idx > 0:
                    prev = paragraphs[idx - 1].strip()
                    prev = re.sub(r'\*{0,2}\(Bild[^)]*\)\*{0,2}', '', prev, flags=re.DOTALL)
                    prev = re.sub(r'\*[^*]+\*', '', prev)
                    prev = re.sub(r'\*\*[^*]*\*\*:?\s*', '', prev)
                    prev = re.sub(r'^>\s*', '', prev, flags=re.MULTILINE)
                    prev = ' '.join(prev.split()).strip()
                    if prev:
                        cleaned = prev + ' ' + cleaned
                cleaned = _clean_caption(cleaned)
                if len(cleaned) > 20:
                    logger.debug("Caption aus Absatz extrahiert (%d chars)", len(cleaned))
                    return cleaned

    # --- Strategie 3: Text nach @username-Zeile ---
    at_match = re.search(r'`@\w+[^`]*`\s*\n+(.*)', text, re.DOTALL)
    if at_match:
        after_at = at_match.group(1).strip()
        after_at = re.sub(r'\*{0,2}\(Bild[^)]*\)\*{0,2}', '', after_at)
        after_at = re.sub(r'\*[^*]+\*', '', after_at)
        after_at = re.sub(r'^#{1,6}\s+.*$', '', after_at, flags=re.MULTILINE)
        after_at = ' '.join(after_at.split()).strip()
        after_at = _clean_caption(after_at)
        if len(after_at) > 15:
            logger.debug("Caption nach @username extrahiert (%d chars)", len(after_at))
            return after_at

    # --- Strategie 4: Resttext ohne Bild-Patterns, RP-Aktionen und Header ---
    cleaned = text
    # RP-Aktionen entfernen (*text*)
    cleaned = re.sub(r'\*[^*]+\*', '', cleaned)
    # Bild-Beschreibungen entfernen: **(Bild: ...)** und (Bild: ...)
    cleaned = re.sub(r'\*{0,2}\(Bild[^)]*\)\*{0,2}', '', cleaned)
    # **Bild-Beschreibung:** Absatz komplett entfernen (Header + Inhalt bis Leerzeile)
    cleaned = re.sub(
        r'\*\*Bild-?[Bb]eschreibung:?\*\*:?\s*.+?(?=\n\s*\n|\Z)',
        '', cleaned, flags=re.DOTALL
    )
    cleaned = re.sub(
        r'\*\*Bild-?[Pp]rompt:?\*\*:?\s*.+?(?=\n\s*\n|\Z)',
        '', cleaned, flags=re.DOTALL
    )
    cleaned = re.sub(
        r'\*\*Image\s+[Dd]escription:?\*\*:?\s*.+?(?=\n\s*\n|\Z)',
        '', cleaned, flags=re.DOTALL
    )
    # Markdown-Header entfernen (### **Instagram Post** etc.)
    cleaned = re.sub(r'^#{1,6}\s+.*$', '', cleaned, flags=re.MULTILINE)
    # **Header:** Zeilen entfernen (reine Header ohne Inhalt auf gleicher Zeile)
    cleaned = re.sub(r'^\*\*[^*]+\*\*:?\s*$', '', cleaned, flags=re.MULTILINE)
    # `@username` Zeilen entfernen
    cleaned = re.sub(r'^`@[^`]*`\s*$', '', cleaned, flags=re.MULTILINE)
    # Bild-wird-generiert Platzhalter entfernen
    cleaned = re.sub(r'\*{0,2}\(Bild wird generiert\)\*{0,2}', '', cleaned)
    # Bereinigen
    cleaned = ' '.join(cleaned.split()).strip()
    # Markdown-Bold um den gesamten Text entfernen
    cleaned = re.sub(r'^\*\*(.+)\*\*$', r'\1', cleaned)

    cleaned = _clean_caption(cleaned)
    if len(cleaned) > 15:
        logger.debug("Caption als Resttext extrahiert (%d chars)", len(cleaned))
        return cleaned

    return ""


@router.post("/instagram-post")
async def instagram_post(request: Request) -> Dict[str, Any]:
    """Generiert ein Bild aus einer Bot-Nachricht und erstellt direkt einen Instagram-Post."""
    data = await request.json()
    user_id = data.get("user_id", "")
    agent_name = data.get("agent_name", "")
    text = data.get("text", "")

    if not agent_name or not text:
        raise HTTPException(status_code=400, detail="user_id, agent_name and text required")

    # Gedanken-Nachricht Prefix entfernen (interne Metadaten)
    text = re.sub(r'^\[Gedanken-Nachricht[^\]]*\]\s*', '', text).strip()
    # Mood-, Location-Zeile und Token-Metadaten entfernen (kommen manchmal aus dem Frontend-Text mit)
    text = re.sub(r'\n?\*{0,2}I feel\s+[^*\n]+\*{0,2}\s*$', '', text, flags=re.IGNORECASE).strip()
    text = re.sub(r'\n?\*{0,2}I am at\s+[^*\n]+\*{0,2}\s*$', '', text, flags=re.IGNORECASE).strip()
    text = re.sub(r'\n?\*{0,2}I do\s+[^*\n]+\*{0,2}\s*$', '', text, flags=re.IGNORECASE).strip()
    text = re.sub(r'\n?\d{2}:\d{2}.*?Tokens:.*$', '', text, flags=re.MULTILINE).strip()
    # Assignment-Tags entfernen (interne Metadaten, nicht fuer Posts)
    from app.models.assignments import strip_assignment_tags
    text = strip_assignment_tags(text)

    logger.info("Instagram-Post starte fuer %s", agent_name)

    # 1. Image-Prompt: zuerst aus Bot-Antwort extrahieren, Fallback auf LLM
    image_prompt = _extract_image_description_from_text(text)
    use_auto_enhance = True
    explicit_appearances = None

    if not image_prompt:
        # LLM generiert Prompt — Appearances vorher pruefen (Smart Detection)
        from app.core.prompt_builder import PromptBuilder as _PB_IG
        _ig_persons = _PB_IG(agent_name).detect_persons(text)
        appearances = [{"name": p.name, "appearance": p.appearance} for p in _ig_persons]
        agent_config = get_character_config(agent_name)

        image_prompt = await asyncio.to_thread(
            _generate_image_prompt, text, appearances,
            "", agent_config)
        if not image_prompt:
            return {"error": "Failed to generate image prompt"}
        use_auto_enhance = False
    else:
        agent_config = get_character_config(agent_name)
        from app.core.prompt_builder import PromptBuilder as _PB_IG2
        _ig_persons2 = _PB_IG2(agent_name).detect_persons(image_prompt)
        explicit_appearances = [{"name": p.name, "appearance": p.appearance} for p in _ig_persons2]

    logger.debug("Instagram-Post Image-Prompt: %s", image_prompt[:150])

    # 2. Bild generieren via ImageGenerationSkill (skip_gallery=True — nicht in Chat-Galerie)
    sm = get_skill_manager()
    img_skill = None
    for skill in sm.skills:
        if skill.__class__.__name__ == "ImageGenerationSkill":
            img_skill = skill
            break

    if not img_skill:
        return {"error": "ImageGenerationSkill not available"}

    img_payload = {
        "prompt": image_prompt,
        "agent_name": agent_name,
        "user_id": "",
        "set_profile": False,
        "skip_gallery": True,
        "auto_enhance": use_auto_enhance,
    }
    if explicit_appearances is not None:
        img_payload["appearances"] = explicit_appearances
    input_json = json.dumps(img_payload)

    result_text = await asyncio.to_thread(img_skill.execute, input_json)
    logger.debug("Instagram-Post ImageGen Result: %s", result_text[:200])

    # Bild-URL und Dateiname extrahieren
    image_urls = re.findall(r'!\[.*?\]\((\/characters\/[^)]+)\)', result_text)
    if not image_urls:
        return {"error": "Image generation failed"}

    image_url = image_urls[0]
    filename_match = re.search(r'/images/([^?]+)', image_url)
    if not filename_match:
        return {"error": "Could not extract filename from generated image"}
    image_filename = filename_match.group(1)

    # Bildpfad bestimmen
    images_dir = get_character_images_dir(agent_name)
    image_path = images_dir / image_filename

    # 3. Caption generieren — je nach caption_mode
    caption_mode = data.get("caption_mode", "vision")
    caption = None

    if caption_mode == "text":
        # Chat-Text als Basis fuer Caption
        logger.debug("Caption aus Chat-Text extrahieren...")
        caption = _extract_caption_from_text(text)

    if not caption:
        # Vision-LLM: Bild analysieren und Caption generieren
        logger.debug("Caption via Vision-LLM (Bildanalyse)...")
        insta_skill = None
        for skill in sm.skills:
            if skill.__class__.__name__ == "InstagramSkill":
                insta_skill = skill
                break

        if insta_skill:
            current_location = get_character_current_location(agent_name)
            current_activity = get_character_current_activity(agent_name)

            caption = await asyncio.to_thread(
                insta_skill._generate_caption,
                str(image_path), agent_name, "",
                current_location, current_activity, text
            )

    if not caption:
        caption = f"#{agent_name} #ai"

    hashtags = re.findall(r'#(\w+)', caption)

    # 4. Bild nach Instagram kopieren + Post erstellen
    from app.models.instagram import create_post, get_instagram_dir

    import shutil
    instagram_dir = get_instagram_dir()
    dst_path = instagram_dir / image_filename

    if not dst_path.exists():
        shutil.copy2(str(image_path), str(dst_path))

    post = create_post(
        agent_name=agent_name,
        image_filename=image_filename,
        caption=caption,
        hashtags=hashtags,
        image_prompt=image_prompt)

    image_post_url = f"/instagram/images/{image_filename}?user_id={user_id}"

    logger.info("Instagram-Post erstellt: %s", post['id'])
    logger.debug("Instagram-Post Caption: %s", caption[:100])

    return {
        "post_id": post["id"],
        "caption": caption,
        "image_url": image_post_url,
    }


# --- Vision Analysis for Chat Images ---

def _analyze_chat_image(image_path: str, agent_name: str, user_text: str = "") -> Optional[str]:
    """Analyze an image with the vision LLM and return a description.

    Used when user attaches an image to a chat message. The description is
    injected into the user message so the chat LLM can react to it.
    """
    from app.core.llm_router import resolve_llm

    if not os.path.exists(image_path):
        logger.warning("Chat image not found: %s", image_path)
        return None

    try:
        with open(image_path, 'rb') as f:
            image_bytes = f.read()
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
    except Exception as e:
        logger.error("Error loading chat image: %s", e)
        return None

    # Vision-LLM via Router (Task: image_recognition)
    instance = resolve_llm("image_recognition", agent_name=agent_name)
    if not instance:
        logger.warning("No image_recognition LLM available for chat image analysis")
        return None

    llm = instance.create_llm(temperature=0.3, max_tokens=500)

    # Get character language for the analysis
    lang_instruction = get_character_language_instruction(agent_name)
    analysis_lang = "German" if "deutsch" in lang_instruction.lower() or "german" in lang_instruction.lower() else "English"

    prompt_text = (
        "Describe this image in detail. Include:\n"
        "- People: appearance, clothing, pose, expression\n"
        "- Setting: location, environment, lighting\n"
        "- Objects and activities visible\n"
        "- Overall mood and atmosphere\n\n"
        "Be factual and objective. Respond ONLY with the description, "
        "no formatting, no markdown, no quotes. 2-4 sentences."
    )

    ext = Path(image_path).suffix.lower().lstrip(".")
    mime = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "webp": "webp", "gif": "gif"}.get(ext, "png")
    image_url = f"data:image/{mime};base64,{base64_image}"

    messages = [
        {"role": "system", "content": f"You MUST answer in {analysis_lang}. This is mandatory."},
        {"role": "user", "content": [
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]},
    ]

    try:
        from app.core.llm_queue import get_llm_queue, Priority
        response = get_llm_queue().submit(
            task_type="image_recognition",
            priority=Priority.HIGH,
            llm=llm,
            messages_or_prompt=messages,
            agent_name=agent_name)
        text = response.content.strip()
        logger.info("Chat image analysis complete: %d chars", len(text))
        return text if text else None
    except Exception as e:
        logger.error("Vision LLM error for chat image: %s", e)
        return None


# --- Chat Endpoint ---

@router.post("/{user_id}")
async def chat(request: Request) -> StreamingResponse:
    """Hauptendpoint fuer Chat mit Characters"""
    import time as _t
    _probe_req_id = uuid.uuid4().hex[:8]

    def _probe(label, t0):
        dt_ms = (_t.perf_counter() - t0) * 1000
        if dt_ms >= 50:
            logger.warning("[probe:%s] sync %s blocked event loop %.0fms",
                           _probe_req_id, label, dt_ms)

    data = await request.json()
    user_input = data.get("message", "")
    selected_skills = data.get("selected_skills", None)  # None = alle, list = nur diese
    # Image attachment: uploaded image ID or character library image URL
    image_id = data.get("image_id", "")          # from upload-image endpoint
    image_url = data.get("image_url", "")         # from character library (e.g. /characters/X/images/Y?user_id=Z)
    # Vom User markierte Raum-Items (Room-Items Panel) — werden als
    # Kontext-Hinweis in den System-Prompt eingebaut, damit der NPC
    # darauf Bezug nehmen kann ("zeigt auf den Stuhl").
    room_item_ids = data.get("room_item_ids", []) or []
    # Medium: "in_person" (default, am gleichen Ort) oder "messaging" (Phone-Chat,
    # Avatar nicht im Raum). Beeinflusst System-Prompt + Chat-Bubble-Styling.
    medium = (data.get("medium") or "in_person").strip() or "in_person"

    # NOTE: ThoughtLoop.record_interaction() removed — the AgentLoop is
    # continuous and importance-driven, no idle-time gating needed.

    # Chat partner = the character that responds (read from _chat_partner.txt)
    _t0 = _t.perf_counter()
    current_agent = _get_chat_partner()
    _probe("_get_chat_partner", _t0)
    # Display name = the player's active character (who is writing)
    from app.models.account import get_active_character
    _t0 = _t.perf_counter()
    user_display_name = get_active_character() or get_user_name()
    _probe("get_active_character+get_user_name", _t0)

    # Avatar-Mood aus User-Input ableiten (Keyword-Heuristik, kein LLM).
    # Silent Update ohne Toast — Expression regeneriert sich beim naechsten
    # Render automatisch.
    _avatar = get_active_character() or ""
    if _avatar and user_input:
        try:
            from app.core.avatar_mood_detect import detect_avatar_mood
            from app.models.character import (
                get_character_current_feeling as _gcf,
                save_character_current_feeling as _scf)
            _cur = _gcf(_avatar) or ""
            _new = detect_avatar_mood(user_input, _cur)
            if _new:
                _scf(_avatar, _new)
                logger.info("Avatar mood updated: %s -> %s (via keyword detect)",
                            _avatar, _new)
        except Exception as _mood_err:
            logger.debug("avatar mood detect failed: %s", _mood_err)

    # Avatar-Activity aus User-Input ableiten (nur Whitelist der Ort-Activities).
    if _avatar and user_input:
        try:
            from app.core.avatar_activity_detect import detect_avatar_activity
            from app.models.activity_library import get_available_activities as _gav
            from app.models.character import (
                get_character_current_activity as _gca,
                save_character_current_activity as _sca)
            _cur_act = _gca(_avatar) or ""
            _available = _gav(_avatar)
            _names = [(a.get("name") or "") for a in _available if a.get("name")]
            _new_act = detect_avatar_activity(user_input, _names, _cur_act)
            if _new_act:
                _sca(_avatar, _new_act)
                logger.info("Avatar activity updated: %s -> %s (via keyword detect)",
                            _avatar, _new_act)
        except Exception as _act_err:
            logger.debug("avatar activity detect failed: %s", _act_err)

    if not current_agent:
        async def gen_empty():
            yield f"data: {json.dumps({'content': ''})}\n\n"
        return StreamingResponse(gen_empty(), media_type="text/event-stream")

    # Sprach-Anweisung aus Character-Profil (Fallback: User-Level)
    _t0 = _t.perf_counter()
    lang_instruction = get_character_language_instruction(current_agent)
    _probe("get_character_language_instruction", _t0)

    # Character ist ausgewaehlt - LLM erstellen
    _t0 = _t.perf_counter()
    agent_config = get_character_config(current_agent)
    _probe("get_character_config", _t0)
    from app.core.llm_router import resolve_llm as _resolve_llm
    _chat_inst = _resolve_llm("chat_stream", agent_name=current_agent)
    llm = _chat_inst.create_llm() if _chat_inst else None

    if llm is None:
        async def gen_no_llm():
            yield f"data: {json.dumps({'content': 'Kein LLM fuer chat_stream konfiguriert. Admin-UI: LLM Routing.'})}\n\n"
        return StreamingResponse(gen_no_llm(), media_type="text/event-stream")

    logger.debug("LLM: %s/%s (task=chat_stream)", _chat_inst.provider_name, _chat_inst.model)

    _t0 = _t.perf_counter()
    full_chat_history = get_chat_history(current_agent)
    _probe("get_chat_history", _t0)

    if not user_input and not image_id and not image_url:
        async def gen_empty():
            yield f"data: {json.dumps({'content': ''})}\n\n"
        return StreamingResponse(gen_empty(), media_type="text/event-stream")

    # --- Image Attachment: Resolve path and start background analysis ---
    _image_analysis_task: Optional[asyncio.Task] = None
    _image_display_url = ""  # URL for frontend display in saved message
    _has_image = bool(image_id or image_url)
    if _has_image:
        image_path = None
        if image_id:
            # Uploaded image
            _upload_path = _get_chat_upload_dir() / image_id
            if _upload_path.exists() and ".." not in image_id:
                image_path = str(_upload_path)
                _image_display_url = f"/chat/upload-image/{image_id}"
        elif image_url:
            # Character library image — resolve to filesystem path
            # URL format: /characters/{name}/images/{filename}?user_id={uid}
            import urllib.parse
            _parsed = urllib.parse.urlparse(image_url)
            _parts = _parsed.path.strip("/").split("/")
            if len(_parts) >= 3 and _parts[0] == "characters" and _parts[2] == "images":
                _char_name = _parts[1]
                _img_file = "/".join(_parts[3:])
                if ".." not in _img_file and ".." not in _char_name:
                    _img_dir = get_character_images_dir(_char_name)
                    _resolved = _img_dir / _img_file
                    if _resolved.exists():
                        image_path = str(_resolved)
                        _image_display_url = image_url

        if image_path:
            # Start analysis in background — does NOT block the chat response
            logger.info("Chat image attached: %s — starting background analysis", image_path)
            _image_analysis_task = asyncio.create_task(
                asyncio.to_thread(
                    _analyze_chat_image, image_path, current_agent, user_input
                )
            )

    # Build effective user input — image analysis runs in background,
    # so tell the LLM an image was sent without waiting for details
    _effective_user_input = user_input
    if _has_image:
        _img_prefix = "[Der User hat ein Bild gesendet.]"
        if user_input:
            _effective_user_input = f"{_img_prefix}\n\n{user_input}"
        else:
            _effective_user_input = f"{_img_prefix}\n\nReagiere auf dieses Bild."

    # --- History Management (zeitgesteuert) ---
    recent_history, old_history = get_time_based_history(full_chat_history)
    # Gecachte Summary laden (sofort, kein LLM-Aufruf)
    history_summary = get_cached_summary(current_agent) if old_history else ""

    messages = []
    # Halluzinierter Send-Message-Template-Prefix bereinigen
    # ("[Name, ]deine Antwort: ..." → tatsaechlicher Antworttext)
    _meta_prefix_re = re.compile(
        r'^(?:[A-Z][\wÄÖÜäöüß \-]{0,30},\s*)?(?:deine|meine|seine|ihre)\s+Antwort:\s*[\'\"]?',
        re.IGNORECASE)
    for msg in recent_history:
        content = msg["content"]
        if msg["role"] == "assistant":
            # Meta-Tag Prefixe entfernen: verhindert dass das LLM
            # Patterns wie "[Gedanken-Nachricht | Ort | Datum]" oder "[Social Dialog | Ort]"
            # bei normalen Chats imitiert
            content = re.sub(r'^\[(?:Gedanken-Nachricht|Social Dialog|Instagram Post)[^\]]*\]\s*', '', content)
            cleaned = _meta_prefix_re.sub('', content).rstrip("'\"").strip()
            if cleaned and cleaned != content:
                content = cleaned
            if not content:
                continue  # Leere Gedanken-Nachrichten ueberspringen
            # Halluzinierte Tool-Tags bereinigen (aus frueheren Responses)
            content = _strip_tool_hallucinations(content)
            if not content:
                continue
            messages.append({"role": "assistant", "content": content})
        else:
            messages.append({"role": "user", "content": content})

    # Self-Reinforcement-Loop brechen: Wenn dieselbe Assistant-Antwort mehrfach
    # in der History steht, sieht das LLM ein Muster und kopiert es weiter
    # (Elias antwortet z.B. 4x identisch obwohl der User-Input wechselt). Wir
    # behalten nur das ERSTE Vorkommen jeder Assistant-Antwort und entfernen
    # spaetere Duplikate samt zugehoerigem User-Turn — so verschwindet das
    # Muster aus dem LLM-Input.
    _seen_assistant: set = set()
    _deduped: list = []
    _i = 0
    while _i < len(messages):
        m = messages[_i]
        if m["role"] == "assistant":
            key = m["content"].strip().lower()
            if key in _seen_assistant:
                # Pruefen ob der vorausgehende User-Turn auch ein Duplikat ist:
                # falls ja, beide raus. Sonst nur die Assistant-Antwort.
                if _deduped and _deduped[-1]["role"] == "user":
                    _deduped.pop()
                _i += 1
                continue
            _seen_assistant.add(key)
        _deduped.append(m)
        _i += 1
    if len(_deduped) < len(messages):
        logger.info("Chat-History: %d redundante Assistant-Duplikate entfernt (%d → %d)",
                    len(messages) - len(_deduped), len(messages), len(_deduped))
        messages = _deduped

    # --- SYSTEM PROMPT aufbauen ---
    # Tools aus aktivierten Skills ableiten (kein manueller tools_enabled Schalter)
    sm = get_skill_manager()
    agent_tools = sm.get_agent_tools(current_agent, check_limits=False)
    if selected_skills is not None:
        agent_tools = [t for t in agent_tools if t.name in selected_skills]

    # Modus-Erkennung: tool_llm frueh laden fuer determine_mode + System-Prompt
    from app.core.dependencies import determine_mode
    _tool_inst = _resolve_llm("intent", agent_name=current_agent) if agent_tools else None
    tool_llm = _tool_inst.create_llm() if _tool_inst else None
    mode = determine_mode(agent_tools, tool_llm, agent_config)
    tools_enabled = mode != "no_tools"
    _has_tool_llm = mode == "rp_first"

    system_content = _build_full_system_prompt(current_agent, lang_instruction, history_summary,
        tools_enabled=tools_enabled, agent_config=agent_config,
        selected_skills=selected_skills,
        has_tool_llm=_has_tool_llm,
        medium=medium,
        room_item_ids=room_item_ids)

    # --- Wake-Up Hinweis im System-Prompt ---
    _is_sleeping = is_character_sleeping(current_agent)
    if _is_sleeping:
        system_content += (
            "\n\nIMPORTANT: You were sleeping and the user just woke you up. "
            "React naturally as someone who was just woken from sleep — groggy, surprised, or sleepy. "
            "You are no longer sleeping after this message."
        )

    # --- Nicht-unterbrechbare Aktivitaet ---
    if not _is_sleeping:
        from app.core.activity_engine import is_character_interruptible
        _interruptible, _busy_activity = is_character_interruptible(current_agent)
        if not _interruptible:
            system_content += (
                f"\n\nIMPORTANT: You are currently deeply focused on '{_busy_activity}' and cannot be "
                f"easily interrupted. React annoyed, distracted, or briefly — you want to get back "
                f"to what you were doing. Keep your response very short (1-2 sentences max)."
            )

    # --- StreamingAgent Setup ---
    tools_dict = {}
    tool_format = "tag"
    _max_iter = 1

    if tools_enabled:
        # Tool-Funktionen mit Kontext (agent_name) wrappen
        for t in agent_tools:
            _orig_func = t.func
            def _make_ctx_wrapper(fn, _agent=current_agent, _uid=""):
                def wrapper(raw_input):
                    ctx = {"input": raw_input, "agent_name": _agent, "user_id": _uid,
                           "skip_daily_limit": True}
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
        _max_iter = 3

        # Tool-Format: auto-detect vom Router-Model
        from app.core.tool_formats import get_format_for_model
        model_for_format = (_tool_inst.model if _tool_inst else "") or (_chat_inst.model if _chat_inst else "")
        tool_format = get_format_for_model(model_for_format)

        # tool_llm wurde bereits oben fuer determine_mode geladen
        _tool_llm_model_name = get_model_name(tool_llm) if tool_llm else ""
        _chat_llm_model_name = get_model_name(llm) if llm else ""
        logger.debug("Tool-Setup: mode=%s, tool_llm=%s, chat_llm=%s, tools=%d skills=%d",
                     mode,
                     'OK (' + _tool_llm_model_name + ')' if tool_llm else 'NONE',
                     _chat_llm_model_name,
                     len(tools_dict), len(agent_tools))

    # Minimaler System-Prompt fuer Tool-LLM (nur im Dual-Modus)
    tool_system_content = ""
    if mode == "rp_first" and tools_dict and agent_tools:
        from app.core.tool_formats import build_tool_instruction, get_format_for_model
        tool_model_name = (_tool_inst.model if _tool_inst else "") or (_chat_inst.model if _chat_inst else "")
        _tool_fmt = get_format_for_model(tool_model_name) if tool_model_name else tool_format
        _tool_appearance = get_character_appearance(current_agent)
        _tool_usage = sm.get_agent_usage_instructions(current_agent, _tool_fmt, check_limits=False)
        user_name_for_tool = user_display_name or "Player"
        from app.core.prompt_builder import is_photographer_mode as _is_pm
        _tool_photographer = _is_pm(current_agent)
        _tool_user_appearance = get_user_appearance() or "" if _tool_photographer else ""
        from app.models.character_template import is_roleplay_character
        tool_instr_block = build_tool_instruction(
            _tool_fmt, agent_tools, _tool_appearance, _tool_usage, model_name=tool_model_name,
            photographer_mode=_tool_photographer, user_appearance=_tool_user_appearance,
            is_roleplay=is_roleplay_character(current_agent))
        available_tool_names = [t.name for t in agent_tools]
        # Locations + Activities fuer Fallback-Marker (Tool-LLM muss erraten
        # koennen welche Location/Activity gemeint ist wenn RP-LLM es vergessen hat)
        _tool_loc_id = get_character_current_location(current_agent)
        _tool_loc_list = ", ".join(l.get("name", "") for l in list_locations() if l.get("name"))
        _tool_act_list = ""
        if _tool_loc_id:
            from app.models.activity_library import get_activity_names as _tl_act_names
            from app.models.character import get_character_language as _tl_lang
            _tl_l = _tl_lang(current_agent)
            _tl_acts = _tl_act_names(current_agent, location_id=_tool_loc_id, lang=_tl_l)
            if _tl_acts:
                _tool_act_list = ", ".join(_tl_acts)
        # Aktuelles Outfit beider Gespraechspartner fuer ChangeOutfit-Kontext
        from app.models.character import build_equipped_outfit_prompt
        from app.models.account import get_active_character
        _tool_agent_outfit = build_equipped_outfit_prompt(current_agent) or "(nothing equipped)"
        _tool_avatar_name = get_active_character() or ""
        _tool_avatar_outfit = ""
        if _tool_avatar_name:
            _tool_avatar_outfit = build_equipped_outfit_prompt(_tool_avatar_name) or "(nothing equipped)"
        _outfit_block = f"\n{current_agent} currently wears: {_tool_agent_outfit}"
        if _tool_avatar_outfit and _tool_avatar_name:
            _outfit_block += f"\n{_tool_avatar_name} currently wears: {_tool_avatar_outfit}"

        tool_system_content = (
            f"Character: {current_agent}. Conversation partner: {user_name_for_tool}.\n"
            f"{tool_instr_block}\n\n"
            f"Available tools: {', '.join(available_tool_names)}\n"
            f"Decide which tools to call based on the conversation. "
            f"If no tools are needed, respond with: NONE\n"
            f"IMPORTANT: Do NOT use TalkTo for {user_name_for_tool} — they are already in the conversation.\n"
            f"\nKnown locations: {_tool_loc_list}\n"
            f"Available activities at current location: {_tool_act_list}"
            f"{_outfit_block}"
        )
        logger.debug("Tool-System-Prompt erstellt (%d Zeichen)", len(tool_system_content))

    # Tool-Kategorien: DEFERRED (post-RP) und CONTENT_TOOL (retry)
    _deferred_tools = set()
    _content_tools = set()
    if tools_dict:
        sm = get_skill_manager()
        for _tname in tools_dict:
            _sk = sm.get_skill_by_name(_tname)
            if _sk and getattr(_sk, 'DEFERRED', False):
                _deferred_tools.add(_tname)
            if _sk and getattr(_sk, 'CONTENT_TOOL', False):
                _content_tools.add(_tname)
        if _deferred_tools:
            logger.info("Deferred Tools: %s", ", ".join(_deferred_tools))
        if _content_tools:
            logger.info("Content Tools: %s", ", ".join(_content_tools))

    agent = StreamingAgent(
        llm=llm,
        tool_format=tool_format,
        tools_dict=tools_dict,
        agent_name=current_agent,
        max_iterations=_max_iter,
        tool_llm=tool_llm,
        tool_system_content=tool_system_content,
        log_task="chat_stream",
        deferred_tools=_deferred_tools,
        content_tools=_content_tools,
        mode=mode)

    async def generate():
        # Queue-Tracking: Chat als aktiv registrieren (pausiert nur Provider-Queue)
        from app.core.llm_queue import get_llm_queue
        _llm_queue = get_llm_queue()
        _llm_inst = _chat_inst
        _chat_task_id = await _llm_queue.register_chat_active_async(
            current_agent, llm_instance=_llm_inst,
            task_type="user_chat", label=f"Chat: {current_agent}")

        # Hand the task_id to the agent so iteration progress shows in panel.
        agent.chat_task_id = _chat_task_id

        # Tool-Executor: gibt Queue waehrend Tool-Ausfuehrung frei,
        # damit Tools die selbst LLM-Calls machen (z.B. KnowledgeExtract) nicht blockiert werden
        _chat_state = {"task_id": _chat_task_id}
        # (tool_name, raw_input) per executed tool — passed to the intent
        # engine so [INTENT:...] markers that duplicate a tool already run
        # in this turn get skipped (avoids double SendMessage / Instagram).
        _tool_executions: list = []

        async def _tool_executor(tool_name, tool_input):
            # Queue freigeben damit Tool-LLM-Calls durchkommen
            try:
                _tool_executions.append((tool_name, tool_input))
            except Exception:
                pass
            if _chat_state["task_id"]:
                _llm_queue.register_chat_done(_chat_state["task_id"])
                _chat_state["task_id"] = None
            try:
                tool_func = tools_dict[tool_name]
                return await asyncio.to_thread(tool_func, tool_input)
            finally:
                # Chat wieder als aktiv registrieren
                _chat_state["task_id"] = await _llm_queue.register_chat_active_async(
                    current_agent, llm_instance=_llm_inst,
                    task_type="user_chat", label=f"Chat: {current_agent}")

        agent.tool_executor = _tool_executor

        try:
            timestamp = datetime.now().isoformat()
            history_text = "\n".join([msg["content"] for msg in messages])
            tokens_input = estimate_tokens(system_content + history_text + _effective_user_input)
            full_response = ""
            _tool_image_urls = []  # Bild-URLs aus Tool-Results
            _tool_exec_counts = {}  # Tool-Name -> Ausfuehrungszaehler

            # --- Chunked TTS Setup ---
            from app.core.tts_service import ChunkedTTSHandler
            _tts = ChunkedTTSHandler(agent_config, require_auto=True)

            # --- Wake-Up: Schlafenden Character aufwecken ---
            _was_sleeping = is_character_sleeping(current_agent)
            if _was_sleeping:
                save_character_current_activity(current_agent, "")
                yield f"data: {json.dumps({'wake_up': True, 'activity': ''})}\n\n"
                logger.info("Character %s wurde durch Chat aufgeweckt", current_agent)

            # --- Model Info Event (damit Frontend sieht welches Tool-LLM aktiv ist) ---
            _model_info = {"chat_model": get_model_name(llm) if llm else ""}
            if tool_llm and tool_llm is not llm:
                _model_info["tool_model"] = get_model_name(tool_llm)
            yield f"data: {json.dumps({'model_info': _model_info})}\n\n"

            # Check if background image analysis has completed quickly
            _image_analysis = None
            if _image_analysis_task is not None and _image_analysis_task.done():
                try:
                    _image_analysis = _image_analysis_task.result()
                except Exception as _ia_err:
                    logger.error("Background image analysis error: %s", _ia_err)
                if _image_analysis:
                    logger.info("Image analysis ready before stream: %s", _image_analysis[:200])
                    yield f"data: {json.dumps({'image_analyzed': True})}\n\n"

            # --- Streaming ---
            from app.core.streaming import ToolStartEvent, ToolEndEvent, ToolResultEvent, ToolErrorEvent, HeartbeatEvent, DeferredToolEvent, RetryHintEvent, ExtractionEvent
            async for event in agent.stream(system_content, messages, _effective_user_input):
                if isinstance(event, HeartbeatEvent):
                    # SSE-Kommentar haelt Verbindung offen (Browser ignoriert es)
                    yield ": heartbeat\n\n"
                    continue
                elif isinstance(event, RetryHintEvent):
                    # rp_first: Antwort wird verworfen und neu generiert
                    yield f"data: {json.dumps({'retry_hint': event.reason})}\n\n"
                    full_response = ""  # Alte Antwort verwerfen
                    continue
                elif isinstance(event, ExtractionEvent):
                    # Extrahierte Marker (Intent, Assignment) an full_response anhaengen
                    # fuer Post-Processing — nicht ans Frontend senden
                    if event.markers:
                        full_response += "\n" + event.markers
                        # EVENT_RESOLVED: aktuellstes disruption/danger Event validieren
                        # durch Tool-LLM und erst bei "resolved=true" tatsaechlich aufloesen.
                        import re as _re_evt
                        for _er_match in _re_evt.finditer(r'\[EVENT_RESOLVED:\s*([^\]]+)\]', event.markers):
                            _resolve_text = _er_match.group(1).strip()
                            try:
                                from app.models.events import list_events, resolve_event, record_attempt
                                from app.core.random_events import validate_solution, _on_resolution_cooldown
                                _char_loc = get_character_current_location(current_agent)
                                if not _char_loc:
                                    continue
                                _loc_events = list_events(location_id=_char_loc)
                                _resolvable = [e for e in _loc_events
                                               if e.get("category") in ("disruption", "danger")
                                               and not e.get("resolved")
                                               and not _on_resolution_cooldown(e)]
                                if not _resolvable:
                                    continue
                                _target_evt = _resolvable[-1]
                                _val = validate_solution(_target_evt, _resolve_text, current_agent)
                                _outcome = "success" if _val.get("resolved") else "fail"
                                record_attempt(_target_evt["id"], current_agent,
                                                _resolve_text, outcome=_outcome,
                                                reason=_val.get("reason", ""))
                                if _val.get("resolved"):
                                    resolve_event(_target_evt["id"],
                                                  resolved_by=current_agent,
                                                  resolved_text=_resolve_text)
                                    logger.info("Event resolved by %s: %s", current_agent, _resolve_text[:60])
                                    # Diary (success)
                                    try:
                                        from app.core.random_events import _diary_log_resolution
                                        _diary_log_resolution(current_agent, _target_evt,
                                                              _resolve_text, True)
                                    except Exception:
                                        pass
                                else:
                                    logger.info("Event-Resolution abgelehnt (%s): %s", current_agent,
                                                 _val.get("reason", ""))
                                    try:
                                        from app.core.random_events import _diary_log_resolution
                                        _diary_log_resolution(current_agent, _target_evt,
                                                              _resolve_text, False,
                                                              reason=_val.get("reason", ""))
                                    except Exception:
                                        pass
                            except Exception as _er_err:
                                logger.debug("Event resolve error: %s", _er_err)
                    continue
                elif isinstance(event, DeferredToolEvent):
                    yield f"data: {json.dumps({'status': 'deferred_tool', 'tool': event.tool_name})}\n\n"
                    continue
                elif isinstance(event, ToolStartEvent):
                    yield f"data: {json.dumps({'status': 'tool_start', 'tool': event.tool_name})}\n\n"
                    continue
                elif isinstance(event, ToolEndEvent):
                    yield f"data: {json.dumps({'status': 'tool_end', 'tool': event.tool_name})}\n\n"
                    continue
                elif isinstance(event, ToolResultEvent):
                    yield f"data: {json.dumps({'tool_result': event.result[:500] if event.result else ''})}\n\n"
                    # Tool-Ausfuehrungen zaehlen
                    _tool_exec_counts[event.tool_name] = _tool_exec_counts.get(event.tool_name, 0) + 1
                    # Bild-URLs aus Tool-Result sammeln (ImageGeneration)
                    if event.result:
                        for _m in re.finditer(r'!\[[^\]]*\]\(/characters/[^)]+\)', event.result):
                            _tool_image_urls.append(_m.group(0))
                    continue
                elif isinstance(event, ToolErrorEvent):
                    yield f"data: {json.dumps({'tool_error': event.error})}\n\n"
                    continue
                elif isinstance(event, ContentEvent):
                    full_response += event.content
                    yield f"data: {json.dumps({'content': event.content})}\n\n"

                    # Chunked TTS
                    for _sse in _tts.feed(event.content):
                        yield _sse

            # --- Chunked TTS: Rest-Buffer + verbleibende Tasks ---
            for _sse in await _tts.flush():
                yield _sse

            # Token-Info senden
            tokens_output = estimate_tokens(full_response)
            tokens_total = tokens_input + tokens_output
            yield f"data: {json.dumps({'tokens': {'input': tokens_input, 'output': tokens_output, 'total': tokens_total}, 'timestamp': timestamp})}\n\n"

            # Tool-generierte Bilder an Response anhaengen
            if _tool_image_urls:
                full_response += '\n\n' + '\n'.join(_tool_image_urls)

            # Nachricht speichern (ohne base64-Daten, Meta-Tags und halluzinierte Tool-Tags)
            from app.core.chat_engine import clean_response as _clean_response, post_process_response
            clean_response = _strip_tool_hallucinations(full_response)
            clean_response = _clean_response(clean_response)
            # "SKIP" ist kein gueltiger Chat-Content — nicht speichern
            if clean_response.upper() == "SKIP":
                logger.warning("SKIP-Antwort erkannt — wird nicht gespeichert")
            else:
                # Save user message — include image markdown if image was attached
                _saved_user_content = user_input
                if _image_display_url:
                    _img_md = f"![Bild]({_image_display_url})"
                    _saved_user_content = f"{_img_md}\n\n{user_input}" if user_input else _img_md
                save_message({
                    "role": "user", "content": _saved_user_content, "timestamp": timestamp,
                    "speaker": "user", "medium": medium,
                }, current_agent)
                save_message({
                    "role": "assistant", "content": clean_response, "timestamp": timestamp,
                    "speaker": current_agent, "medium": medium,
                }, current_agent)

            # LLM-Logging erfolgt per-Iteration im StreamingAgent

            # TTS Auto-Mode: Nur wenn Chunked TTS NICHT aktiv ist
            # (Chunked TTS sendet audio_chunk Events stattdessen)
            if not _tts.enabled:
                try:
                    from app.core.tts_service import get_tts_service
                    tts = get_tts_service()
                    tts_cfg = tts.get_character_config(agent_config)
                    if tts.enabled and tts_cfg.get("enabled", True) and tts_cfg.get("auto", False):
                        yield f"data: {json.dumps({'tts_auto': True})}\n\n"
                except Exception as tts_err:
                    logger.error("TTS error: %s", tts_err)

            # Chat-Registrierung BEENDEN bevor Extraktion startet,
            # sonst Deadlock: Extraktion geht durch LLM Queue, die aber
            # pausiert ist solange Chat aktiv ist.
            if _chat_state["task_id"]:
                _llm_queue.register_chat_done(_chat_state["task_id"])
                _chat_state["task_id"] = None

            # Shared post-processing: mood, location, activity, memory,
            # relationships, intents, instagram, context, history summary
            _pp = post_process_response(
                owner_id="",
                character_name=current_agent,
                user_input=user_input,
                full_response=full_response,
                agent_config=agent_config,
                llm=llm,
                user_display_name=user_display_name,
                full_chat_history=full_chat_history,
                old_history=old_history,
                executed_tools=_tool_executions)

            # SSE events for frontend (mood, location, activity, assignments)
            if _pp.get("mood"):
                yield f"data: {json.dumps({'mood': _pp['mood']})}\n\n"
            if _pp.get("location"):
                _loc_event = {'location': _pp['location']}
                if _pp.get("room"):
                    _loc_event['room'] = _pp['room']
                yield f"data: {json.dumps(_loc_event)}\n\n"
            if _pp.get("activity"):
                yield f"data: {json.dumps({'activity': _pp['activity']})}\n\n"
            if _pp.get("new_assignment"):
                _na = _pp["new_assignment"]
                yield f"data: {json.dumps({'new_assignment': {'title': _na.get('title',''), 'id': _na.get('id','')}})}\n\n"

            # Auto-Progress: Tool-Ausfuehrungen als Assignment-Fortschritt zaehlen
            if _tool_exec_counts:
                try:
                    from app.models.assignments import auto_track_progress, TOOL_NAME_MAP
                    for _tn, _tc in _tool_exec_counts.items():
                        _tool_type = TOOL_NAME_MAP.get(_tn)
                        if _tool_type:
                            # Bilder: Anzahl nach URLs, nicht Tool-Calls (ein Call kann mehrere Bilder erzeugen)
                            _count = len(_tool_image_urls) if _tool_type == "image" and _tool_image_urls else _tc
                            _atp = auto_track_progress(current_agent, _tool_type, _count)
                            if _atp:
                                yield f"data: {json.dumps({'assignment_progress': _atp})}\n\n"
                except Exception as _ate:
                    logger.debug("Assignment auto-progress error: %s", _ate)

            # Background image analysis: await result after stream with timeout
            # Timeout verhindert dass der SSE-Stream haengt wenn die LLM-Queue
            # durch andere Tasks blockiert ist.
            _IMAGE_ANALYSIS_TIMEOUT = 15  # seconds
            if _image_analysis_task is not None and not _image_analysis_task.done():
                try:
                    _image_analysis = await asyncio.wait_for(
                        asyncio.shield(_image_analysis_task),
                        timeout=_IMAGE_ANALYSIS_TIMEOUT)
                    if _image_analysis:
                        logger.info("Background image analysis completed: %s", _image_analysis[:200])
                        yield f"data: {json.dumps({'image_analysis': _image_analysis, 'image_analyzed': True})}\n\n"
                    else:
                        logger.warning("Background image analysis returned empty result")
                except asyncio.TimeoutError:
                    logger.warning("Image analysis timeout (%ds) — SSE-Stream wird nicht blockiert",
                                   _IMAGE_ANALYSIS_TIMEOUT)
                except Exception as _ia_err:
                    logger.error("Background image analysis error: %s", _ia_err)

        except Exception as e:
            logger.error("Chat-Stream Fehler: %s", e, exc_info=True)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            if _chat_state["task_id"]:
                _llm_queue.register_chat_done(_chat_state["task_id"])

    from app.core.auth_dependency import get_current_user_optional
    _req_user = get_current_user_optional(request)
    _task_owner = (_req_user or {}).get("id", "") if _req_user else ""
    mgr = get_chat_task_manager()
    task_id = mgr.create_task(user_id=_task_owner)
    asyncio.create_task(mgr.feed_from_generator(task_id, generate()))
    return JSONResponse({"task_id": task_id})


@router.get("/{user_id}/stream/{task_id}")
async def chat_stream(task_id: str, request: Request) -> StreamingResponse:
    """SSE-Stream für einen laufenden oder abgeschlossenen Chat-Task.

    Reconnect-sicher: der Buffer wird ab offset=0 (oder ?offset=N) wiedergegeben,
    dann werden neue Chunks live gestreamt bis der Task fertig ist.

    Besitzcheck: Nur der User der den Task erstellt hat darf den Stream lesen.
    """
    from app.core.auth_dependency import get_current_user_optional
    from_offset = int(request.query_params.get("offset", "0"))
    mgr = get_chat_task_manager()
    task = mgr.get_task(task_id)

    if task is None:
        async def _not_found():
            yield 'data: {"error": "Task nicht gefunden oder abgelaufen"}\n\n'
        return StreamingResponse(_not_found(), media_type="text/event-stream")

    # Ownership: task.user_id muss zum eingeloggten User passen (leer = legacy/pre-auth
    # task → zulassen, da sonst alle Bestandschats nach Restart verloren)
    owner = mgr.get_task_owner(task_id)
    if owner:
        current = get_current_user_optional(request)
        if not current or current.get("id") != owner:
            async def _forbidden():
                yield 'data: {"error": "Forbidden"}\n\n'
            return StreamingResponse(_forbidden(), media_type="text/event-stream")

    async def _stream():
        async for chunk in mgr.subscribe(task_id, from_offset=from_offset):
            yield chunk

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# Post-Stream Helpers — ausgelagert aus generate() fuer Lesbarkeit
# ---------------------------------------------------------------------------

def _extract_mood(agent_name: str, response: str) -> Optional[str]:
    """Extrahiert Mood aus LLM-Antwort. Returns mood string or None."""
    config = get_character_config(agent_name)
    if not config.get("mood_tracking", False):
        return None
    # Primaer: **I feel <emotion>**
    match = re.search(r'\*\*I\s+feel\s+(.+?)\*\*', response, re.IGNORECASE)
    # Fallback: letztes **<emotion>** am Ende der Antwort
    if not match:
        match = re.search(
            r'\*\*([A-Za-z\u00C0-\u00FF][a-z\u00E0-\u00FF]+(?:\s+[a-z\u00E0-\u00FF]+)*)[.?!]*\*\*\s*$',
            response
        )
    if not match:
        return None
    mood = match.group(1).strip().rstrip('.!,')
    save_character_current_feeling(agent_name, mood)
    # Mood-History aufzeichnen
    try:
        record_mood_history(agent_name, mood)
    except Exception as mh_err:
        logger.error("Mood history error: %s", mh_err)
    logger.info("Mood %s: %s", agent_name, mood)
    return mood


def _extract_location(agent_name: str, response: str) -> Optional[Dict[str, str]]:
    """Extrahiert Location aus LLM-Antwort. Returns {'name': ..., 'id': ...} or None.

    Wenn der Chat-Character den Ort wechselt, geht der Spieler-Avatar
    automatisch mit (gemeinsam einen Ort besuchen).
    """
    from app.models.character import save_character_current_room, save_character_current_activity
    from app.models.account import get_active_character
    from app.models.world import get_location_by_id, get_room_by_name
    match = re.search(r'\*\*I\s+am\s+at\s+(.+?)\*\*', response, re.IGNORECASE)
    if not match:
        return None
    new_name = match.group(1).strip().rstrip('.!,')
    old_loc = get_character_current_location(agent_name)

    def _move_avatar_loc(new_loc_id: str):
        """Avatar folgt NICHT mehr automatisch bei Location-Wechsel.
        Der User entscheidet manuell ueber die Avatar-UI wo sein Charakter ist."""
        return

    def _move_avatar_room(room_id: str):
        """Avatar geht mit dem Chat-Character mit (Raum-Wechsel innerhalb
        derselben Location). Location-Follow ist bewusst abgeschaltet."""
        player = get_active_character()
        if player and player != agent_name:
            player_loc = get_character_current_location(player)
            agent_loc = get_character_current_location(agent_name)
            # Nur mitgehen wenn sie an der gleichen Location sind — sonst
            # wuerde der Avatar in einen Raum gesetzt, der nicht zu seiner
            # Location gehoert.
            if player_loc and player_loc == agent_loc:
                save_character_current_room(player, room_id)
                logger.info("Avatar %s folgt %s -> Room %s", player, agent_name, room_id)

    # 1. Raum-Match: Ist es ein Raum an der aktuellen Location?
    if old_loc:
        current_loc_data = get_location_by_id(old_loc)
        if current_loc_data:
            room = get_room_by_name(current_loc_data, new_name)
            if room:
                room_id = room.get("id", "")
                old_room = get_character_current_room(agent_name)
                if room_id != old_room:
                    save_character_current_room(agent_name, room_id)
                    _move_avatar_room(room_id)
                    logger.info("Room %s: %s -> %s (%s)", agent_name, old_room, room_id, new_name)
                    return {"name": new_name, "room": room_id, "location_id": old_loc}
                return None  # Schon im Raum

    # 2. Location-Match
    loc_obj = resolve_location(new_name)
    if loc_obj and loc_obj.get("id"):
        new_id = loc_obj["id"]
        if new_id != old_loc:
            # Rules pruefen — LLM-Narrative darf Blockade-Rules nicht umgehen.
            # Bei Block: access_denied loggen und Location NICHT wechseln.
            try:
                from app.models.rules import check_access
                allowed, reason = check_access(agent_name, new_id)
                if not allowed:
                    try:
                        from app.models.character import record_access_denied
                        record_access_denied(agent_name, new_id,
                                              loc_obj.get("name", new_name), reason or "")
                    except Exception:
                        logger.debug("record_access_denied im Chat-Extract fehlgeschlagen",
                                     exc_info=True)
                    logger.info("Location-Wechsel fuer %s via Chat blockiert: %s -> %s (%s)",
                                 agent_name, old_loc, new_id, reason)
                    return None
            except Exception as _rerr:
                logger.debug("Rules-Check im Chat-Location-Extract fehlgeschlagen: %s", _rerr)

            save_character_current_location(agent_name, new_id)
            save_character_current_room(agent_name, '')
            _move_avatar_loc(new_id)
            resolved = loc_obj.get("name", new_name)
            logger.info("Location %s: %s -> %s (%s)", agent_name, old_loc, new_id, resolved)
            return {"name": resolved, "id": new_id}
    elif new_name != old_loc:
        save_character_current_location(agent_name, new_name)
        save_character_current_room(agent_name, '')
        _move_avatar_loc(new_name)
        logger.info("Location %s: %s -> %s (temporaer)", agent_name, old_loc, new_name)
        return {"name": new_name}
    return None


def _extract_activity(agent_name: str, response: str) -> Optional[str]:
    """Extrahiert Activity aus LLM-Antwort. Returns activity name or None.

    Zwei-Ebenen-System:
    - current_activity: Kurzer Kategorie-Name (fuer Mechanik: Outfits, Effects, Conditions)
    - current_activity_detail: Originaler Freitext (fuer Prompt-Flavour, Bild-Generierung)

    Versucht zuerst ein direktes Match gegen bekannte Aktivitaeten.
    Falls kein Match: LLM-Call im Hintergrund um ein Kategorie-Wort zu bestimmen.
    """
    match = re.search(r'\*\*I\s+do\s+(.+?)\*\*', response, re.IGNORECASE)
    if not match:
        return None
    raw_activity = match.group(1).strip().rstrip('.!,')
    old_activity = get_character_current_activity(agent_name)
    if raw_activity.lower() == (old_activity or "").lower():
        return None

    # Bekannte Aktivitaeten am Standort sammeln (Bibliothek + Location + Character)
    current_loc_id = get_character_current_location(agent_name)
    loc_data = None
    if current_loc_id:
        from app.models.world import get_location_by_id
        loc_data = get_location_by_id(current_loc_id)

    from app.models.activity_library import get_available_activities, _get_all_names
    all_activities = get_available_activities(agent_name, current_loc_id or "")
    raw_lower = raw_activity.lower()

    # Direktes Match versuchen (alle Namensvarianten + ID)
    matched = ""
    for act in all_activities:
        aid = act.get("id", "").lower()
        all_names = _get_all_names(act)
        if aid == raw_lower or any(n.lower() == raw_lower for n in all_names):
            matched = act.get("name", "")
            break
    # Fuzzy-Match (Substring)
    if not matched:
        for act in all_activities:
            aid = act.get("id", "").lower()
            all_names = [n.lower() for n in _get_all_names(act)]
            if raw_lower in aid or aid in raw_lower:
                matched = act.get("name", "")
                break
            for n in all_names:
                if raw_lower in n or n in raw_lower:
                    matched = act.get("name", "")
                    break
            if matched:
                break
    # Wort-Ueberlappung mit Stammform (z.B. "sipping a cocktail" → "Drinking cocktails")
    if not matched:
        _stops = {"a", "an", "the", "at", "in", "on", "and", "or", "to", "with", "some", "my"}
        def _stems(words):
            """Einfaches Stemming: trailing s/ing/ed entfernen."""
            result = set()
            for w in words:
                result.add(w)
                if w.endswith("s") and len(w) > 3:
                    result.add(w[:-1])
                if w.endswith("ing") and len(w) > 5:
                    result.add(w[:-3])
                if w.endswith("ed") and len(w) > 4:
                    result.add(w[:-2])
            return result - _stops

        raw_stems = _stems(raw_lower.replace("_", " ").split())
        best_overlap = 0
        for act in all_activities:
            aid = act.get("id", "").lower().replace("_", " ")
            all_names = [n.lower() for n in _get_all_names(act)]
            act_words = set(aid.split())
            for n in all_names:
                act_words.update(n.split())
            act_stems = _stems(act_words)
            overlap = len(raw_stems & act_stems)
            if overlap > best_overlap and overlap >= 1:
                best_overlap = overlap
                matched = act.get("name", "")

    if matched:
        # Direkter Match — Kategorie-Name gefunden
        activity_name = matched
        detail = raw_activity if raw_activity.lower() != matched.lower() else ""
    else:
        # Kein Match — LLM im Hintergrund fuer Kategorie-Wort, Freitext sofort speichern
        activity_name = raw_activity  # Vorerst Freitext als Name
        detail = raw_activity
        _classify_activity_background(agent_name, raw_activity, all_activities)

    save_character_current_activity(agent_name, activity_name, detail=detail)

    # Passenden Raum und Outfit aktualisieren
    if current_loc_id and loc_data:
        from app.models.world import find_room_by_activity
        from app.models.character import save_character_current_room
        matched_room = find_room_by_activity(loc_data, activity_name)
        if matched_room:
            save_character_current_room(agent_name, matched_room.get("id", ""))
        # Outfit-Compliance nach Room/Location Dress-Code
        from app.models.inventory import apply_outfit_type_compliance
        _ot = ""
        if matched_room:
            _ot = (matched_room.get("outfit_type") or "").strip()
        if not _ot and loc_data:
            _ot = (loc_data.get("outfit_type") or "").strip()
        if _ot:
            apply_outfit_type_compliance(agent_name, _ot)

    # Effects werden zeitproportional via save_character_current_activity
    # und hourly_status_tick angewendet (nicht mehr sofort).

    logger.info("Activity %s: %s -> %s (detail: %s)", agent_name, old_activity, activity_name, detail[:50] if detail else "-")
    return activity_name


def _classify_activity_background(agent_name: str, raw_activity: str, known_activities: list):
    """DEPRECATED — classify is now auto-triggered by save_character_current_activity.
    Kept as wrapper for backward compatibility."""
    from app.core.activity_engine import classify_activity_background
    classify_activity_background(agent_name, raw_activity)


def _apply_extracted_outfit_to_pieces(character_name: str,
                                       extracted_outfit: str) -> None:
    """Gleicht den extrahierten Outfit-Freitext gegen die aktuell angelegten
    Pieces ab. Pieces deren Name oder prompt_fragment NICHT im extrahierten
    Text vorkommt, werden abgelegt (unequip).

    Kann keine neuen Pieces anlegen — dafuer muss das LLM OutfitCreation rufen.
    """
    if not extracted_outfit or not character_name:
        return
    try:
        from app.models.inventory import (
            get_equipped_pieces, get_item, unequip_piece)
        eq = get_equipped_pieces(character_name) or {}
        if not eq:
            return

        text_lower = extracted_outfit.strip().lower()
        unequipped = []

        for slot, iid in list(eq.items()):
            if not iid:
                continue
            it = get_item(iid)
            if not it:
                continue
            name = (it.get("name") or "").strip().lower()
            frag = (it.get("prompt_fragment") or "").strip().lower()

            # Pruefen ob das Piece im extrahierten Text erwaehnt wird.
            # Mehrere Heuristiken: voller Name, einzelne Woerter (>3 Zeichen),
            # prompt_fragment-Teilstrings.
            mentioned = False
            if name and name in text_lower:
                mentioned = True
            elif frag:
                # Jedes signifikante Wort aus dem Fragment pruefen
                frag_words = [w for w in frag.split() if len(w) >= 4]
                if frag_words:
                    mentioned = sum(1 for w in frag_words if w in text_lower) >= len(frag_words) * 0.5
            if not mentioned and name:
                # Einzelne Wort-Pruefung: mindestens ein signifikantes Namens-Wort
                name_words = [w for w in name.split() if len(w) >= 4]
                if name_words:
                    mentioned = any(w in text_lower for w in name_words)

            if not mentioned:
                r = unequip_piece(character_name, slot=slot)
                if r.get("status") == "ok":
                    unequipped.append(f"{it.get('name', iid)} ({slot})")

        # KEIN Auto-Equip durch den Extractor — das wuerde manuell abgelegte
        # Pieces sofort wieder anziehen (Race-Condition mit Wardrobe-Aenderungen).
        # Anziehen laeuft ausschliesslich ueber ChangeOutfit-Skill oder Wardrobe-UI.

        if unequipped:
            logger.info("Chat-Extraktion [%s]: %d Piece(s) abgelegt: %s",
                         character_name, len(unequipped), ", ".join(unequipped))
            # Runtime-Force-Skip: die abgelegten Slots bleiben "absichtlich leer"
            # bis zum naechsten Location-Wechsel. Verhindert dass Auto-Fill sie
            # bei naechstem Activity-Tick sofort wieder anzieht.
            try:
                from app.models.character import (
                    get_character_profile, save_character_profile)
                _prof = get_character_profile(character_name) or {}
                _skip = set(_prof.get("runtime_outfit_skip") or [])
                for _u in unequipped:
                    # _u ist "Name (slot)" — Slot extrahieren
                    _m = _u.rsplit("(", 1)
                    if len(_m) == 2:
                        _slot = _m[1].rstrip(")").strip()
                        if _slot:
                            _skip.add(_slot)
                _prof["runtime_outfit_skip"] = sorted(_skip)
                save_character_profile(character_name, _prof)
            except Exception as _fse:
                logger.debug("runtime_outfit_skip konnte nicht gesetzt werden: %s", _fse)

        # Expression-Variant neu generieren wenn sich was geaendert hat
        if unequipped:
            try:
                from app.core.expression_regen import trigger_expression_generation
                from app.models.inventory import get_equipped_pieces, get_equipped_items
                from app.models.character import (
                    get_character_current_feeling, get_character_current_activity)
                _mood = get_character_current_feeling(character_name) or ""
                _act = get_character_current_activity(character_name) or ""
                _eqp = get_equipped_pieces(character_name)
                _eqi = get_equipped_items(character_name)
                trigger_expression_generation(character_name, _mood, _act,
                    equipped_pieces=_eqp, equipped_items=_eqi,
                    ignore_cooldown=True)
            except Exception as _te:
                logger.debug("Expression-Trigger nach Extraktion fehlgeschlagen: %s", _te)
    except Exception as e:
        logger.warning("Chat-Extraktion [%s] Piece-Abgleich fehlgeschlagen: %s",
                        character_name, e)


def _extract_context_from_last_chat(agent_name: str,
    chat_history: List[Dict[str, str]],
    agent_config: Optional[Dict[str, Any]] = None) -> None:
    """Extrahiert Activity und Outfit-Aenderungen aus den letzten Chat-Nachrichten.

    Ueberschreibt die gespeicherte Activity und legt Pieces ab, deren Name im
    Narrativ nicht mehr erwaehnt wird (unequip-only, kein Auto-Equip).
    """
    if not chat_history:
        return

    # Letzte Assistant-Nachrichten sammeln (max. 3 fuer Kontext)
    last_assistant_msgs = []
    for msg in reversed(chat_history):
        if msg.get("role") == "assistant":
            content = msg.get("content", "").strip()
            if content:
                last_assistant_msgs.append(content)
                if len(last_assistant_msgs) >= 3:
                    break
    if not last_assistant_msgs:
        return

    # Auch letzte User-Nachricht fuer Kontext
    last_user_msg = ""
    for msg in reversed(chat_history):
        if msg.get("role") == "user":
            last_user_msg = msg.get("content", "").strip()
            break

    # Quellen strikt getrennt:
    # - Avatar-Aenderungen kommen aus der User-Eingabe ("Ich ziehe die Jacke aus")
    # - Character-Aenderungen kommen aus der Character-Antwort
    # Jeder Call sieht nur seine eigene Quelle → keine Fehlzuordnung moeglich.
    character_source = "\n".join(
        f"Character: {m}" for m in reversed(last_assistant_msgs)
    )
    avatar_source = f"User: {last_user_msg}" if last_user_msg else ""

    # Avatar-Name fuer User-Zuordnung (Full-Extraction: Outfit-Aenderungen
    # des Spielers landen auf seinem Avatar-Character, nicht auf dem Login-Konto).
    from app.models.account import get_active_character
    avatar_name = get_active_character() or ""

    # Basis-Outfits fuer Delta-Rekonstruktion: ohne Baseline kann der Extractor
    # aus "Ich ziehe die Jacke aus" nicht ableiten, was uebrig bleibt.
    # Direkt aus equipped_pieces bauen (kein Legacy get_current_outfit).
    from app.models.character import build_equipped_outfit_prompt
    def _strip_wearing(text: str) -> str:
        t = text.strip()
        if t.lower().startswith("wearing: "):
            return t[9:]
        if t.lower().startswith("wearing:"):
            return t[8:]
        return t
    agent_current_outfit = _strip_wearing(build_equipped_outfit_prompt(agent_name) or "")
    avatar_current_outfit = (
        _strip_wearing(build_equipped_outfit_prompt(avatar_name) or "")
        if avatar_name else ""
    )

    def _same_outfit(a: str, b: str) -> bool:
        return a.strip().lower() == b.strip().lower()

    def _extract_for_character(
        target_name: str, target_config: Optional[Dict[str, Any]],
        target_baseline: str, source_text: str, is_avatar: bool):
        """Ein LLM-Call fuer EINEN Character aus EINER Quelle.

        - is_avatar=False: Quelle = Character-Antwort. Extrahiert Agent-Outfit + Activity.
        - is_avatar=True:  Quelle = User-Eingabe. Extrahiert Avatar-Outfit.

        Laeuft unter der Tool-LLM-Config von target_name — Logs + LLM-Wahl
        sind korrekt dem jeweiligen Character zugeordnet.
        """
        if not source_text.strip():
            return
        from app.models.character import is_outfit_locked
        from app.core.llm_queue import get_llm_queue, Priority

        # Outfit-Lock: spart LLM-Call wenn der User Auto-Aenderungen fuer
        # diesen Character gesperrt hat. Beim Avatar gibt es nur Outfit-
        # Extraktion → kompletter Skip. Beim Agent laeuft Activity-Extraktion
        # weiter (anderer Zweck), nur das Outfit-Feld wird aus dem Prompt
        # entfernt und spaeter ignoriert.
        outfit_locked = is_outfit_locked(target_name)
        if outfit_locked and is_avatar:
            logger.debug("Chat-Kontext [%s]: Outfit-Lock aktiv, Avatar-Extraktion uebersprungen",
                         target_name)
            return

        source_label = "User input" if is_avatar else "Character reply"
        from app.core.prompt_templates import render_task
        sys_prompt, user_prompt = render_task(
            "extraction_chat_context",
            target_name=target_name,
            target_baseline=target_baseline or "(unknown)",
            source_label=source_label,
            source_text=source_text,
            outfit_locked=outfit_locked,
            is_avatar=is_avatar)

        try:
            from app.core.llm_router import llm_call as _llm_call
            response = _llm_call(
                task="extraction",
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
                agent_name=target_name)
        except RuntimeError:
            logger.debug("Chat-Kontext-Extraktion [%s]: kein LLM verfuegbar", target_name)
            return
        except Exception as e:
            logger.warning("Chat-Kontext-Extraktion [%s] Fehler: %s", target_name, e)
            return

        raw = (response.content or "").strip()
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if not json_match:
            logger.debug("Chat-Kontext [%s]: kein JSON in Antwort", target_name)
            return
        try:
            data = json.loads(json_match.group())
        except json.JSONDecodeError:
            logger.debug("Chat-Kontext [%s]: JSON-Parse-Fehler: %s", target_name, raw[:100])
            return

        # Outfit wird ignoriert wenn Lock aktiv ist — kein Write, kein Expression-Trigger
        extracted_outfit = "" if outfit_locked else (data.get("outfit") or "").strip()

        # Activity nur aus Character-Call
        if not is_avatar:
            extracted_activity = (data.get("activity") or "").strip()
            if extracted_activity:
                old_activity = get_character_current_activity(target_name)
                if extracted_activity.lower() != (old_activity or "").lower():
                    save_character_current_activity(target_name, extracted_activity)
                    logger.info("Chat-Kontext [%s]: Activity '%s' -> '%s'",
                                 target_name, old_activity, extracted_activity)

        # Extrahiertes Outfit gegen aktuell angelegte Pieces abgleichen:
        # Pieces deren Name/Fragment NICHT im extrahierten Text vorkommt
        # werden abgelegt (= Character hat sie im Narrativ ausgezogen).
        # Neue Pieces koennen nicht aus dem Freitext erzeugt werden —
        # dafuer muss das LLM den OutfitCreation-Skill rufen.
        if extracted_outfit:
            _apply_extracted_outfit_to_pieces(target_name, extracted_outfit)

    def _do_extraction():
        # Call 1: Character-Antwort → Agent-Outfit + Activity (unter Agent-Config)
        _extract_for_character(
            agent_name, agent_config, agent_current_outfit,
            source_text=character_source, is_avatar=False)
        # Call 2: User-Eingabe → Avatar-Outfit (unter Avatar-Config)
        if avatar_name and avatar_source:
            from app.models.character import get_character_config
            avatar_config = get_character_config(avatar_name)
            _extract_for_character(
                avatar_name, avatar_config, avatar_current_outfit,
                source_text=avatar_source, is_avatar=True)

    import asyncio
    asyncio.get_event_loop().run_in_executor(None, _do_extraction)


def _build_full_system_prompt(character_name: str,
    lang_instruction: str,
    history_summary: str,
    tools_enabled: bool = False,
    agent_config: Optional[Dict[str, Any]] = None,
    selected_skills: Optional[list] = None,
    channel: str = "web",
    has_tool_llm: bool = False,
    partner_override: str = "",
    skip_partner: bool = False,
    medium: str = "in_person",
    room_item_ids: Optional[list] = None) -> str:
    """Build the chat-stream / talk-to system prompt.

    Loads all data sections (character/soul template, partner template,
    memory, relationships, ...), then renders ``chat/chat_stream.md``.
    Pre-formatted blocks live in Python (``build_*_prompt_section``);
    static instruction text lives in the template.

    Args:
        skip_partner: True for group chat — partner section is skipped
            (participants get listed in the GROUP CONVERSATION block instead).
    """
    from app.core.prompt_templates import render

    char_profile = get_character_profile(character_name)
    char_template = get_template(char_profile.get("template", "human-default"))
    char_features = (char_template or {}).get("features", {})

    # ---- Conversation partner ----------------------------------------
    from app.models.account import get_active_character
    _partner_name = "" if skip_partner else (partner_override or get_active_character())
    _partner_lines: list = []
    partner_mode = "none"

    if _partner_name and _partner_name != character_name:
        if not char_features.get("relationships_enabled", True):
            partner_mode = "chatbot"
        else:
            partner_mode = "character"
            partner_profile = get_character_profile(_partner_name)
            partner_template = get_template(partner_profile.get("template", "human-default"))
            if partner_template:
                p_app = partner_profile.get("character_appearance", "")
                if p_app and "{" in p_app:
                    partner_profile["character_appearance"] = resolve_profile_tokens(
                        p_app, partner_profile, template=partner_template,
                        target_key="character_appearance")
                p_loc_id = partner_profile.get("current_location", "")
                if p_loc_id:
                    p_loc_name = get_location_name(p_loc_id)
                    partner_profile["current_location"] = p_loc_name or p_loc_id
                _partner_lines = build_prompt_section(
                    partner_template, partner_profile,
                    active_features=char_features,
                    is_partner=True, character_name=_partner_name)
            if not _partner_lines:
                _partner_lines = [f"Name: {_partner_name}"]

            try:
                _p_feeling = (partner_profile.get("current_feeling") or "").strip()
                if _p_feeling:
                    _partner_lines.append(f"Current mood: {_p_feeling}")
                _p_activity = (partner_profile.get("current_activity") or "").strip()
                if _p_activity:
                    _partner_lines.append(f"Currently doing: {_p_activity}")
            except Exception:
                pass

            partner_address = get_character_address_form(character_name)
            if partner_address:
                _partner_lines.append(f"Form of address: {partner_address}")
    elif not skip_partner:
        # Fallback: no active character — use account username
        _fallback_name = get_user_name() or ""
        if _fallback_name:
            _partner_name = _fallback_name
            partner_mode = "fallback"

    # ---- Self / partner wearing blocks --------------------------------
    def _strip_wearing_prefix(text: str) -> str:
        t = (text or "").strip()
        if t.lower().startswith("wearing:"):
            t = t[len("wearing:"):].lstrip()
        return t

    def _build_wearing_block(_cname: str, _label: str,
                             include_inventory: bool) -> str:
        try:
            from app.models.character import build_equipped_outfit_prompt
            from app.models.inventory import (
                get_character_inventory, get_equipped_item_ids)
            wearing = _strip_wearing_prefix(build_equipped_outfit_prompt(_cname))
            lines: list = []
            _is_self = (_label == "You")
            _v_wear = "are" if _is_self else "is"
            _v_carry = "have" if _is_self else "has"
            if wearing:
                lines.append(f"{_label} {_v_wear} currently wearing this clothing and equipment: {wearing}")
            if include_inventory:
                inv = get_character_inventory(_cname).get("inventory", [])
                equipped_set = set(get_equipped_item_ids(_cname))
                carried = [
                    (e.get("item_name") or e.get("item_id") or "?")
                    for e in inv
                    if e.get("item_id") not in equipped_set
                ]
                if carried:
                    lines.append(f"{_label} {_v_carry} on hand (not yet equipped / in inventory): {', '.join(carried)}")
            return "\n".join(lines)
        except Exception:
            return ""

    self_wearing = _build_wearing_block(character_name, "You", include_inventory=True)
    partner_wearing = ""
    if _partner_name and _partner_name != character_name and not skip_partner:
        partner_wearing = _build_wearing_block(_partner_name, _partner_name,
                                                include_inventory=False)

    # ---- Focused room items (in_person + room_item_ids) ---------------
    focused_items = ""
    if medium == "in_person" and room_item_ids:
        try:
            from app.models.inventory import get_item
            _item_lines = []
            for _iid in room_item_ids:
                _it = get_item(_iid)
                if not _it:
                    continue
                _n = _it.get("name", _iid)
                _d = (_it.get("description") or "").strip()
                _item_lines.append(f"- {_n}: {_d}" if _d else f"- {_n}")
            if _item_lines:
                focused_items = "\n".join(_item_lines)
        except Exception:
            pass

    # ---- Character template lines (the core "this is who you are") ----
    if char_template:
        appearance = char_profile.get("character_appearance", "")
        if appearance and "{" in appearance:
            char_profile["character_appearance"] = resolve_profile_tokens(
                appearance, char_profile, template=char_template,
                target_key="character_appearance")
        current_outfit = (build_equipped_outfit_prompt(character_name) or "").removeprefix("wearing: ")
        if current_outfit:
            char_profile["default_outfit"] = current_outfit
        loc_id = char_profile.get("current_location", "")
        if loc_id:
            loc_name = get_location_name(loc_id)
            char_profile["current_location"] = loc_name if loc_name else loc_id
        char_lines = build_prompt_section(
            char_template, char_profile,
            active_features=char_features, character_name=character_name)
    else:
        char_lines = [f"Name: {character_name}"]
        if char_profile.get("character_personality"):
            char_lines.append(f"Personality: {char_profile['character_personality']}")

    # ---- Feature-Check Helper -----------------------------------------
    from app.models.character_template import is_feature_enabled as _feat
    def _has(feat: str) -> bool:
        return _feat(character_name, feat)

    # ---- Active assignments -------------------------------------------
    assignment_section = ""
    if _has("assignments_enabled"):
        from app.models.assignments import build_assignment_prompt_section
        assignment_section = build_assignment_prompt_section(character_name) or ""

    # ---- Current situation block --------------------------------------
    current_location_id = get_character_current_location(character_name)
    current_location = get_location_name(current_location_id) if current_location_id else ""
    current_room_id = get_character_current_room(character_name)
    current_activity = get_character_current_activity(character_name)

    now = datetime.now()
    time_line = f"Current time: {now.strftime('%H:%M')} ({now.strftime('%A, %d %B %Y')})"
    situation_parts = [time_line]

    if _has("locations_enabled") and current_location:
        loc_data = get_location(current_location_id)
        loc_desc = loc_data.get("description", "") if loc_data else ""
        situation_parts.append(
            f"Location: {current_location} - {loc_desc}" if loc_desc
            else f"Location: {current_location}")
        if current_room_id and loc_data:
            room_data = get_room_by_id(loc_data, current_room_id)
            if not room_data:
                for r in loc_data.get("rooms", []):
                    if r.get("name", "").lower() == current_room_id.lower():
                        room_data = r
                        break
            if room_data:
                room_name = room_data.get("name", "")
                room_desc = room_data.get("description", "")
                if room_desc:
                    situation_parts.append(f"Room: {room_name} - {room_desc}")
                elif room_name:
                    situation_parts.append(f"Room: {room_name}")

    if _has("activities_enabled") and current_activity:
        act_data = get_activity(current_activity)
        act_desc = act_data.get("description", "") if act_data else ""
        act_detail = char_profile.get("current_activity_detail", "")
        display_desc = act_detail or act_desc
        situation_parts.append(
            f"Activity: {current_activity} - {display_desc}" if display_desc
            else f"Activity: {current_activity}")

    if len(situation_parts) == 1:
        situation_block = time_line
    else:
        situation_block = "Your current situation:\n" + "\n".join(situation_parts)

    # ---- Status effects / danger --------------------------------------
    status_section = ""
    if _has("status_effects_enabled"):
        from app.core.danger_system import build_status_prompt_section
        status_section = build_status_prompt_section(character_name) or ""

    # ---- Location events ----------------------------------------------
    events_section = ""
    if current_location_id and _has("locations_enabled"):
        events_section = build_events_prompt_section(location_id=current_location_id) or ""

    # ---- Memory --------------------------------------------------------
    memory_section = ""
    if _has("memory_enabled"):
        memory_section = build_memory_prompt_section(
            character_name, user_name=_partner_name, current_message="") or ""

    # ---- Relationships -------------------------------------------------
    relationships_section = ""
    if _has("relationships_enabled"):
        from app.models.relationship import build_relationship_prompt_section
        relationships_section = build_relationship_prompt_section(character_name) or ""

    # ---- Secrets -------------------------------------------------------
    secrets_section = ""
    if _has("secrets_enabled"):
        from app.models.secrets import build_secrets_prompt_section
        secrets_section = build_secrets_prompt_section(character_name) or ""

    # ---- Inventory: carrying + room visible ---------------------------
    inventory_carrying_section = ""
    inventory_room_section = ""
    if _has("inventory_enabled"):
        try:
            from app.models.inventory import (
                get_character_inventory, get_room_items, get_item,
                get_equipped_item_ids)

            def _localized(item: Dict[str, Any], field: str) -> str:
                if field == "name":
                    return (item.get("name") or "").strip()
                return (item.get(field) or item.get(f"{field}_de") or "").strip()

            inv_data = get_character_inventory(character_name)
            inv_items = inv_data.get("inventory", [])
            equipped_set = set(get_equipped_item_ids(character_name))
            if inv_items:
                inv_lines = []
                for entry in inv_items:
                    item_id = entry.get("item_id", "")
                    if item_id in equipped_set or entry.get("equipped"):
                        continue
                    full_item = get_item(item_id) or {}
                    name = _localized(full_item, "name") or entry.get("item_name", "?")
                    desc = _localized(full_item, "description")
                    qty = entry.get("quantity", 1)
                    line = f"- {name}" + (f" x{qty}" if qty > 1 else "")
                    if desc:
                        line += f" — {desc}"
                    inv_lines.append(line)
                if inv_lines:
                    inventory_carrying_section = "Items you are carrying:\n" + "\n".join(inv_lines)

            if current_location_id and current_room_id:
                room_entries = get_room_items(current_location_id, current_room_id) or []
                visible_lines = []
                for ri in room_entries:
                    if ri.get("hidden"):
                        continue
                    full_item = get_item(ri.get("item_id", "")) or {}
                    if not full_item:
                        continue
                    name = _localized(full_item, "name") or ri.get("item_id", "?")
                    desc = _localized(full_item, "description")
                    qty = ri.get("quantity", 1)
                    note = (ri.get("note") or "").strip()
                    line = f"- {name}" + (f" x{qty}" if qty > 1 else "")
                    if note:
                        line += f" ({note})"
                    elif desc:
                        line += f" — {desc}"
                    visible_lines.append(line)
                if visible_lines:
                    inventory_room_section = "Items visible in this room:\n" + "\n".join(visible_lines)
        except Exception:
            pass

    # ---- Mood tracking flag -------------------------------------------
    char_config = get_character_config(character_name)
    mood_tracking_enabled = bool(
        _has("mood_tracking_enabled") and char_config.get("mood_tracking", False))

    # ---- Location / activity change instructions ----------------------
    known_locations = ""
    if current_location_id and _has("locations_enabled"):
        location_names = [loc.get("name", "") for loc in list_locations()]
        if location_names:
            known_locations = ", ".join(location_names)

    known_activities = ""
    if current_location_id and _has("activities_enabled"):
        loc_data_act = get_location(current_location_id)
        if loc_data_act:
            from app.models.activity_library import get_activity_names as _get_act_names
            from app.models.character import get_character_language as _get_lang
            _char_lang = _get_lang(character_name)
            activity_names = _get_act_names(
                character_name, location_id=current_location_id, lang=_char_lang)
            if activity_names:
                known_activities = ", ".join(activity_names)

    # ---- Intent / assignment tracking flags ---------------------------
    intent_tracking_enabled = bool(tools_enabled and not has_tool_llm)
    assignment_tracking_enabled = bool(intent_tracking_enabled and _has("assignments_enabled"))
    other_characters = ""
    if assignment_tracking_enabled:
        from app.models.character import list_available_characters
        all_characters = list_available_characters()
        other_characters = ", ".join(c for c in all_characters if c != character_name)

    # ---- Tool instructions block (built externally — complex) ---------
    tool_instructions = ""
    if tools_enabled and agent_config and not has_tool_llm:
        from app.core.tool_formats import build_tool_instruction, get_format_for_model
        sm = get_skill_manager()
        agent_tools = sm.get_agent_tools(character_name, check_limits=False)
        if selected_skills is not None:
            agent_tools = [t for t in agent_tools if t.name in selected_skills]
        if agent_tools:
            from app.core.llm_router import resolve_llm as _resolve_llm_sp
            _sp_chat_inst = _resolve_llm_sp("chat_stream", agent_name=character_name)
            _model_for_fmt = _sp_chat_inst.model if _sp_chat_inst else ""
            fmt = get_format_for_model(_model_for_fmt)
            appearance = get_character_appearance(character_name)
            usage = sm.get_agent_usage_instructions(character_name, fmt, check_limits=False)
            from app.core.prompt_builder import is_photographer_mode as _is_pm
            _sp_photographer = _is_pm(character_name)
            _sp_user_app = get_user_appearance() or "" if _sp_photographer else ""
            from app.models.character_template import is_roleplay_character as _is_rp_char
            tool_instructions = build_tool_instruction(
                fmt, agent_tools, appearance, usage, model_name=_model_for_fmt,
                photographer_mode=_sp_photographer, user_appearance=_sp_user_app,
                is_roleplay=_is_rp_char(character_name))

    # ---- Long-term / daily / session summaries -----------------------
    longterm_section = ""
    daily_summary_section = ""
    history_summary_block = ""
    if _has("memory_enabled"):
        from app.utils.history_manager import build_longterm_summary_prompt_section
        longterm_section = build_longterm_summary_prompt_section(character_name) or ""
        daily_summary_section = build_daily_summary_prompt_section(character_name, max_days=5) or ""

        if history_summary:
            location_changed = get_location_changed_at(character_name)
            location_is_recent = False
            if location_changed:
                try:
                    changed_dt = datetime.fromisoformat(location_changed)
                    age_minutes = (datetime.now() - changed_dt).total_seconds() / 60
                    location_is_recent = age_minutes < 10
                except (ValueError, TypeError):
                    pass
            if location_is_recent:
                short = history_summary[:200]
                if len(history_summary) > 200:
                    short = short.rsplit(" ", 1)[0]
                history_summary_block = f"Previously: {short}"
            else:
                history_summary_block = f"Summary of previous conversations:\n{history_summary}"

    # ---- Recent activity ----------------------------------------------
    recent_activity_section = ""
    try:
        from app.core.system_prompt_builder import build_recent_activity_section
        recent_activity_section = build_recent_activity_section(character_name) or ""
    except Exception as _re:
        logger.debug("Recent-Activity-Section: %s", _re)

    # ---- Condition reminder -------------------------------------------
    condition_reminder = ""
    if _has("status_effects_enabled"):
        from app.core.danger_system import build_condition_reminder
        condition_reminder = build_condition_reminder(character_name) or ""

    return render(
        "chat/chat_stream.md",
        character_name=character_name,
        lang_instruction=lang_instruction,
        char_lines=char_lines,
        partner_mode=partner_mode,
        partner_name=_partner_name,
        partner_lines=_partner_lines,
        skip_partner=skip_partner,
        medium=medium,
        self_wearing=self_wearing,
        partner_wearing=partner_wearing,
        focused_items=focused_items,
        assignment_section=assignment_section,
        situation_block=situation_block,
        status_section=status_section,
        events_section=events_section,
        memory_section=memory_section,
        relationships_section=relationships_section,
        secrets_section=secrets_section,
        inventory_carrying_section=inventory_carrying_section,
        inventory_room_section=inventory_room_section,
        tools_enabled=tools_enabled,
        has_tool_llm=has_tool_llm,
        mood_tracking_enabled=mood_tracking_enabled,
        known_locations=known_locations,
        known_activities=known_activities,
        intent_tracking_enabled=intent_tracking_enabled,
        assignment_tracking_enabled=assignment_tracking_enabled,
        other_characters=other_characters,
        tool_instructions=tool_instructions,
        longterm_section=longterm_section,
        daily_summary_section=daily_summary_section,
        history_summary_block=history_summary_block,
        recent_activity_section=recent_activity_section,
        condition_reminder=condition_reminder)
