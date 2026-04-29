"""Story routes - Interactive branching story system."""
import asyncio
import json
import re
import shutil
from datetime import datetime
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from app.core.log import get_logger

logger = get_logger("story")
from app.core.paths import get_storage_dir as _get_storage_dir


from app.core.dependencies import get_skill_manager
from app.core.streaming import StreamingAgent, ContentEvent
from app.models.account import get_user_profile
from app.models.character import (
    get_character_config,
    get_character_profile,
    get_character_appearance,
    get_character_language_instruction,
    build_equipped_outfit_prompt,
    get_character_images_dir)
from app.models.character_template import (
    resolve_profile_tokens, get_template, build_prompt_section)
from app.models.story import (
    list_stories,
    get_story,
    filter_stories_for_character,
    parse_options_from_response,
    get_story_state,
    save_story_state,
    delete_story_state)
from app.models.world import get_location_name
from app.core.task_queue import get_task_queue

router = APIRouter(prefix="/story", tags=["story"])


def clear_story_tmp():
    """Loescht alle temporaeren Story-Bilder. Wird beim Server-Start aufgerufen."""
    if (_get_storage_dir() / "tmp" / "story_images").exists():
        shutil.rmtree((_get_storage_dir() / "tmp" / "story_images"))
        logger.info("Temp-Verzeichnis geleert: %s", (_get_storage_dir() / "tmp" / "story_images"))
    (_get_storage_dir() / "tmp" / "story_images").mkdir(parents=True, exist_ok=True)


@router.get("/tmp/{filename}")
def serve_story_tmp_image(filename: str):
    """Liefert ein temporaeres Story-Bild aus."""
    path = (_get_storage_dir() / "tmp" / "story_images") / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Bild nicht gefunden")
    return FileResponse(path)


@router.get("/list")
def list_stories_route(character_name: str = Query("", alias="character_name")) -> Dict[str, Any]:
    """Gibt gefilterte Story-Liste fuer den aktuellen Character zurueck."""
    if not character_name:
        return {"stories": []}

    all_stories = list_stories()
    profile = get_character_profile(character_name)
    filtered = filter_stories_for_character(all_stories, profile)

    # has_saved_state Flag hinzufuegen
    result = []
    for story in filtered:
        state = get_story_state(character_name, story["filename"])
        result.append({
            "filename": story["filename"],
            "title": story.get("title", story["filename"]),
            "description": story.get("description", ""),
            "language": story.get("language", ""),
            "has_saved_state": state is not None,
            "current_section": state.get("current_section", "") if state else "",
        })

    return {"stories": result}


@router.get("/detail/{filename}")
def get_story_detail(
    filename: str,
    character_name: str = Query("")) -> Dict[str, Any]:
    """Gibt die vollstaendig geparste Story + gespeicherten State zurueck."""
    story = get_story(filename)
    if not story:
        raise HTTPException(status_code=404, detail="Story nicht gefunden")

    state = None
    if character_name:
        state = get_story_state(character_name, filename)

    return {"story": story, "state": state}


@router.post("/play")
async def play_story_section(request: Request) -> StreamingResponse:
    """Spielt eine Story-Section ab: LLM-Streaming + Options-Parsing + Visualisierung."""
    data = await request.json()
    user_id = data.get("user_id", "")
    character_name = data.get("character_name", "")
    story_filename = data.get("story_filename", "")
    section_id = data.get("section_id", "start")
    user_choice = data.get("user_choice")  # None oder {"letter": "A", "text": "..."}

    if not character_name or not story_filename:
        raise HTTPException(
            status_code=400,
            detail="user_id, character_name und story_filename erforderlich")

    # Story und Section laden
    story = get_story(story_filename)
    if not story:
        raise HTTPException(status_code=404, detail="Story nicht gefunden")

    section = story["sections"].get(section_id)
    if not section:
        raise HTTPException(status_code=404, detail=f"Section '{section_id}' nicht gefunden")

    # LLM laden (via Router, Task: story_stream)
    agent_config = get_character_config(character_name)
    from app.core.llm_router import resolve_llm
    _story_inst = resolve_llm("story_stream", agent_name=character_name)
    llm = _story_inst.create_llm() if _story_inst else None
    if llm is None:
        raise HTTPException(status_code=500, detail="Kein LLM verfuegbar (task=story_stream)")

    # System-Prompt bauen (inkl. Szenen-Anweisung und Wahl-Kontext)
    system_content = _build_story_system_prompt(character_name, story["meta"], section_id,
        section_prompt=section["prompt"],
        user_choice=user_choice)

    # User-Prompt: nur Options-Format (keine Szenen-Anweisung, kein Wahl-Text)
    options_map = section.get("options_map", {})
    options_labels = section.get("options_labels", {})
    user_prompt = "Play this scene."
    if options_map:
        num_options = len(options_map)
        letters = sorted(options_map.keys())
        if options_labels:
            lines = []
            for l in letters:
                label = options_labels.get(l, "")
                if label:
                    lines.append(f"  **Option {l}:** {label}")
                else:
                    lines.append(f"  **Option {l}:** ...")
        else:
            lines = [f"  **Option {l}:** [description]" for l in letters]
        user_prompt = (
            f"End your response with exactly {num_options} options in this format:\n"
            + "\n".join(lines)
        )

    logger.info("%s: Section '%s'", character_name, section_id)

    # Streaming
    agent = StreamingAgent(
        llm=llm,
        tool_format="tag",
        tools_dict={},
        agent_name=character_name,
        max_iterations=1,
        log_task="story_stream")

    async def generate():
        from app.core.llm_queue import get_llm_queue
        _llm_queue = get_llm_queue()
        _llm_inst = _story_inst
        _story_task_id = await _llm_queue.register_chat_active_async(
            character_name, llm_instance=_llm_inst,
            task_type="story", label=f"Story: {character_name}")
        full_response = ""
        _viz_track_id = None
        _tts_track_id = None
        _tq = None
        try:
            # --- Chunked TTS Setup ---
            from app.core.tts_service import ChunkedTTSHandler
            _tts = ChunkedTTSHandler(agent_config) if story["meta"].get("tts", True) else None

            from app.core.streaming import HeartbeatEvent
            async for event in agent.stream(system_content, [], user_prompt):
                if isinstance(event, HeartbeatEvent):
                    yield ": heartbeat\n\n"
                    continue
                elif isinstance(event, ContentEvent):
                    full_response += event.content
                    yield f"data: {json.dumps({'content': event.content})}\n\n"

                    # Chunked TTS
                    if _tts:
                        for _sse in _tts.feed(event.content):
                            yield _sse

            # --- Chunked TTS: Rest-Buffer + verbleibende Tasks ---
            if _tts:
                for _sse in await _tts.flush():
                    yield _sse

            # Optionen aus Antwort parsen
            options = parse_options_from_response(full_response)

            # Pruefen ob Story endet
            options_map = section.get("options_map", {})
            options_labels = section.get("options_labels", {})
            is_end = False
            if not options and not options_map:
                is_end = True
            elif len(options) == 1 and options_map.get(options[0]["letter"]) == "_end":
                is_end = True

            # Fallback: Wenn Parser keine Optionen findet aber options_map existiert,
            # verwende die vordefinierten Labels aus dem Flowchart
            if not options and options_map and not is_end:
                logger.warning("Options-Parser fand keine Optionen im LLM-Text, verwende Flowchart-Labels")
                for letter in sorted(options_map.keys()):
                    label = options_labels.get(letter, f"Option {letter}")
                    options.append({"letter": letter, "text": label})

            if is_end:
                yield f"data: {json.dumps({'story_end': True})}\n\n"
            elif options:
                yield f"data: {json.dumps({'options': options})}\n\n"

            # State speichern
            state = get_story_state(character_name, story_filename) or {
                "history": [],
                "started_at": datetime.now().isoformat(),
            }

            if user_choice:
                state["history"].append({
                    "section": section_id,
                    "choice": user_choice.get("letter", ""),
                    "choice_text": user_choice.get("text", ""),
                    "timestamp": datetime.now().isoformat(),
                })

            # Aktuelle Section speichern (nicht die naechste — der User hat noch nicht gewaehlt)
            state["current_section"] = section_id

            save_story_state(character_name, story_filename, state)

            yield f"data: {json.dumps({'section_complete': True, 'current_section': section_id})}\n\n"

            # LLM-Logging erfolgt per-Iteration im StreamingAgent

            # Queue freigeben — Streaming ist fertig, TTS/Visualisierung brauchen die Queue
            _llm_queue.register_chat_done(_story_task_id)
            _story_task_id = None

            # TTS und Visualisierung parallel starten
            do_tts = False
            do_viz = story["meta"].get("visualize", True)
            tts_task = None
            viz_task = None

            # Nur Komplett-TTS wenn Chunked TTS NICHT aktiv ist
            if not (_tts and _tts.enabled) and story["meta"].get("tts", True):
                try:
                    from app.core.tts_service import get_tts_service, clean_text_for_tts
                    tts = get_tts_service()
                    tts_cfg = tts.get_character_config(agent_config)
                    if tts.enabled and tts_cfg.get("enabled", True):
                        do_tts = True
                        clean_text = clean_text_for_tts(full_response)
                        tts_task = asyncio.create_task(asyncio.to_thread(
                            tts.generate,
                            text=clean_text,
                            voice=tts_cfg.get("voice", ""),
                            speaker_wav=tts_cfg.get("speaker_wav", ""),
                            language=tts_cfg.get("language", "de"),
                            voice_description=tts_cfg.get("voice_description", ""),
                            character_name=tts_cfg.get("character_name", "")))
                except Exception as tts_err:
                    logger.error("TTS Init Fehler: %s", tts_err)

            if do_viz:
                viz_task = asyncio.create_task(asyncio.to_thread(
                    _visualize_scene, character_name, full_response,
                    story_meta=story["meta"]))

            # Status-Events senden
            if do_tts or do_viz:
                status = {}
                if do_tts:
                    status["generating_audio"] = True
                if do_viz:
                    status["generating_image"] = True
                yield f"data: {json.dumps(status)}\n\n"

            # TaskQueue registrieren (tracked tasks)
            _tq = get_task_queue()
            _tts_track_id = _tq.track_start("story_tts", "Story TTS", agent_name=character_name) if do_tts else None
            _viz_track_id = _tq.track_start("story_visualization", "Story Szene", agent_name=character_name) if do_viz else None

            # TTS-Ergebnis abholen (normalerweise schneller)
            if tts_task:
                try:
                    audio_path = await tts_task
                    if audio_path:
                        yield f"data: {json.dumps({'audio': {'url': f'/tts/tmp/{audio_path.name}'}})}\n\n"
                    else:
                        yield f"data: {json.dumps({'generating_audio': False})}\n\n"
                    if _tts_track_id:
                        _tq.track_finish(_tts_track_id)
                except Exception as tts_err:
                    logger.error("TTS Fehler: %s", tts_err)
                    yield f"data: {json.dumps({'generating_audio': False})}\n\n"
                    if _tts_track_id:
                        _tq.track_finish(_tts_track_id, error=str(tts_err))

            # Visualisierung-Ergebnis abholen (laeuft parallel, evtl. schon fertig)
            if viz_task:
                try:
                    viz_result = await viz_task
                    if viz_result and viz_result.get("image_urls"):
                        yield f"data: {json.dumps({'visualization': viz_result})}\n\n"
                    else:
                        yield f"data: {json.dumps({'generating_image': False})}\n\n"
                    if _viz_track_id:
                        _tq.track_finish(_viz_track_id)
                except Exception as viz_err:
                    logger.error("Visualisierung Fehler: %s", viz_err)
                    yield f"data: {json.dumps({'generating_image': False})}\n\n"
                    if _viz_track_id:
                        _tq.track_finish(_viz_track_id, error=str(viz_err))

        except Exception as e:
            logger.error("Streaming Fehler: %s", e)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            if _story_task_id:
                _llm_queue.register_chat_done(_story_task_id)
            # Tracked tasks aufraeumen falls SSE-Verbindung abgebrochen
            try:
                if _viz_track_id:
                    _tq.track_finish(_viz_track_id, error="abgebrochen")
            except Exception:
                pass
            try:
                if _tts_track_id:
                    _tq.track_finish(_tts_track_id, error="abgebrochen")
            except Exception:
                pass

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/restart")
async def restart_story(request: Request) -> Dict[str, Any]:
    """Loescht den gespeicherten State einer Story (Neustart)."""
    data = await request.json()
    user_id = data.get("user_id", "")
    character_name = data.get("character_name", "")
    story_filename = data.get("story_filename", "")

    if not character_name or not story_filename:
        raise HTTPException(status_code=400, detail="Pflichtfelder fehlen")

    delete_story_state(character_name, story_filename)
    return {"status": "success"}


@router.get("/raw/{filename}")
def get_story_raw(filename: str) -> Dict[str, Any]:
    """Gibt den rohen Markdown-Inhalt einer Story zurueck."""
    STORIES_DIR = _get_storage_dir() / "stories"
    path = STORIES_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Story nicht gefunden")
    return {"filename": filename, "content": path.read_text(encoding="utf-8")}


@router.put("/raw/{filename}")
async def save_story_raw(filename: str, request: Request) -> Dict[str, Any]:
    """Speichert den bearbeiteten Markdown-Inhalt einer Story."""
    STORIES_DIR = _get_storage_dir() / "stories"
    data = await request.json()
    content = data.get("content", "")
    if not content.strip():
        raise HTTPException(status_code=400, detail="Leerer Inhalt")

    path = STORIES_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Story nicht gefunden")

    path.write_text(content, encoding="utf-8")
    logger.info("Story gespeichert: %s", filename)
    return {"status": "success", "filename": filename}


@router.delete("/file/{filename}")
async def delete_story_file(filename: str) -> Dict[str, Any]:
    """Loescht eine Story-Datei."""
    STORIES_DIR = _get_storage_dir() / "stories"
    path = STORIES_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Story nicht gefunden")

    path.unlink()
    logger.info("Story geloescht: %s", filename)
    return {"status": "success", "filename": filename}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_story_system_prompt(character_name: str,
    story_meta: Dict[str, Any],
    section_id: str,
    section_prompt: str = "",
    user_choice: Dict[str, Any] | None = None) -> str:
    """Baut den System-Prompt fuer Story-Interaktionen."""
    parts = []

    # 1. Sprach-Anweisung (aus Character-Profil, Fallback: User-Level)
    lang = get_character_language_instruction(character_name)
    if lang:
        parts.append(lang)

    # 2. Player-Character-Infos (Player ist selbst ein Character)
    from app.models.account import get_active_character
    player_name = get_active_character()
    if player_name and player_name != character_name:
        player_profile = get_character_profile(player_name)
        player_template = get_template(player_profile.get("template", "human-default"))
        if player_template:
            player_lines = build_prompt_section(
                player_template, player_profile, character_name=player_name)
            if player_lines:
                parts.append(f"\nYour interlocutor ({player_name}):\n" + "\n".join(player_lines))

    # 3. Character-Infos (reuse pattern from chat.py)
    char_profile = get_character_profile(character_name)
    char_template = get_template(char_profile.get("template", "human-default"))

    if char_template:
        appearance = char_profile.get("character_appearance", "")
        if appearance and "{" in appearance:
            char_profile["character_appearance"] = resolve_profile_tokens(
                appearance, char_profile, template=char_template,
                target_key="character_appearance")
        current_outfit = (build_equipped_outfit_prompt(character_name) or "").removeprefix("wearing: ")
        if current_outfit:
            char_profile["default_outfit"] = current_outfit

        # Location-ID zu Name aufloesen (in_prompt wuerde sonst die rohe ID ausgeben)
        loc_id = char_profile.get("current_location", "")
        if loc_id:
            loc_name = get_location_name(loc_id)
            char_profile["current_location"] = loc_name if loc_name else loc_id

        char_lines = build_prompt_section(
            char_template, char_profile, character_name=character_name)
    else:
        char_lines = [f"Name: {character_name}"]

    if char_lines:
        parts.append("\nYou are this character:\n" + "\n".join(char_lines))

    # 4. Story-Kontext
    setting = story_meta.get("setting", "")
    title = story_meta.get("title", "")
    story_block = "\n=== INTERACTIVE STORY ==="
    if title:
        story_block += f"\nTitle: {title}"
    if setting:
        story_block += f"\nSetting: {setting}"

    # 5. Szenen-Anweisung (als System-Instruktion, nicht User-Nachricht)
    if user_choice:
        choice_text = user_choice.get("text", "")
        story_block += f"\n\nThe player chose: {choice_text}"
    if section_prompt:
        story_block += f"\n\nScene direction: {section_prompt}"

    story_block += (
        "\n\nIMPORTANT: Stay fully in character. Be vivid and descriptive. "
        "Do NOT repeat or echo the scene direction or player choice in your response. "
        "Just narrate the scene directly."
    )

    # 6. Antwortlaenge begrenzen (aus Frontmatter)
    max_len = story_meta.get("max_response_length")
    if max_len:
        story_block += f"\nKeep your response under {max_len} characters."

    parts.append(story_block)

    return "\n".join(parts)


def _visualize_scene(character_name: str, text: str,
    story_meta: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Generiert ein temporaeres Szenen-Bild (keine Galerie, kein Kommentar)."""
    # Inline import to avoid circular import (chat.py <-> story.py)
    from app.routes.chat import _generate_image_prompt
    from app.core.prompt_builder import PromptBuilder

    _pb = PromptBuilder(character_name)
    _persons = _pb.detect_persons(text)
    appearances = [{"name": p.name, "appearance": p.appearance} for p in _persons]

    # Story-Visualisierung: Nur den Character zeigen, nicht den User
    user_profile = get_user_profile()
    user_name = user_profile.get("user_name", "")
    if user_name:
        appearances = [p for p in appearances if p["name"] != user_name]

    # Story: Character immer einbeziehen — er erlebt die Story
    char_names = [p["name"] for p in appearances]
    if character_name not in char_names:
        char_appearance = get_character_appearance(character_name)
        if char_appearance and "{" in char_appearance:
            profile = get_character_profile(character_name)
            template = get_template(profile.get("template", "human-default"))
            char_appearance = resolve_profile_tokens(
                char_appearance, profile, template=template,
                target_key="character_appearance")
        if char_appearance:
            appearances.insert(0, {"name": character_name, "appearance": char_appearance})

    agent_config = get_character_config(character_name)

    # Story-Setting als Kontext fuer den Image-Prompt
    setting_context = ""
    if story_meta:
        parts = []
        if story_meta.get("title"):
            parts.append(f"Story: {story_meta['title']}")
        if story_meta.get("setting"):
            parts.append(f"Setting: {story_meta['setting']}")
        if parts:
            setting_context = "\n".join(parts)

    image_prompt = _generate_image_prompt(
        text, appearances, setting_context=setting_context,
        agent_config=agent_config)
    if not image_prompt:
        return {}

    # Bild generieren mit skip_gallery=True (keine Galerie, kein Kommentar)
    sm = get_skill_manager()
    img_skill = None
    for skill in sm.skills:
        if skill.__class__.__name__ == "ImageGenerationSkill":
            img_skill = skill
            break

    if not img_skill:
        return {}

    import json as _json
    input_json = _json.dumps({
        "prompt": image_prompt,
        "agent_name": character_name,
        "user_id": "",
        "set_profile": False,
        "skip_gallery": True,
        "auto_enhance": False,
    })

    result_text = img_skill.execute(input_json)
    logger.info("Visualize Result: %s", result_text[:200])

    # Bild-URLs aus Ergebnis parsen
    char_urls = re.findall(r'!\[.*?\]\((\/characters\/[^)]+)\)', result_text)
    if not char_urls:
        return {}

    # Bilder von Character-Dir nach Temp-Dir verschieben
    images_dir = get_character_images_dir(character_name)
    (_get_storage_dir() / "tmp" / "story_images").mkdir(parents=True, exist_ok=True)

    tmp_urls = []
    for url in char_urls:
        # URL: /characters/Name/images/file.png?user_id=...
        filename = url.split("/")[-1].split("?")[0]
        src = images_dir / filename
        if src.exists():
            dst = (_get_storage_dir() / "tmp" / "story_images") / filename
            shutil.move(str(src), str(dst))
            tmp_urls.append(f"/story/tmp/{filename}")
            logger.debug("Bild verschoben: %s -> %s", src, dst)

    return {
        "image_urls": tmp_urls,
        "prompt_used": image_prompt,
    }
