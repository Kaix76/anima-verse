"""Story Development routes - Generate stories via LLM with parameter form."""
import json
import re
import uuid
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from app.core.log import get_logger

logger = get_logger("story_dev")

from app.core.llm_router import create_llm_instance
from app.core.streaming import StreamingAgent, ContentEvent

router = APIRouter(prefix="/story-dev", tags=["story-dev"])

from app.core.paths import get_storage_dir as _get_storage_dir

def _stories_dir() -> Path:
    return _get_storage_dir() / "stories"

# In-memory session store
_sessions: Dict[str, Dict[str, Any]] = {}


def _build_story_prompt(params: Dict[str, str]) -> str:
    """Reads the story template and fills in user parameters."""
    template = (_stories_dir() / "Template für ein LLM.md").read_text(encoding="utf-8")

    thema = params.get("thema", "Ein Abenteuer")
    laenge = params.get("laenge", "8-15")
    verzweigungen = params.get("verzweigungen", "3")
    ton = params.get("ton", "spannend")
    max_antwort = params.get("max_antwort", "800")

    template = re.sub(
        r'- Thema: \[.*?\]',
        f'- Thema: {thema}',
        template
    )
    template = re.sub(
        r'- Laenge: \[.*?\] Szenen.*',
        f'- Laenge: {laenge} Szenen',
        template
    )
    template = re.sub(
        r'- Verzweigungen: \[.*?\] Entscheidungspunkte.*',
        f'- Verzweigungen: {verzweigungen} Entscheidungspunkte',
        template
    )
    template = re.sub(
        r'- Ton: \[.*?\]',
        f'- Ton: {ton}',
        template
    )
    template = re.sub(
        r'- Max\. Antwortlaenge: \[.*?\] Zeichen.*',
        f'- Max. Antwortlaenge: {max_antwort} Zeichen pro Antwort',
        template
    )

    return template


def _slugify(title: str) -> str:
    """Converts a title to a URL/filename-safe slug."""
    s = title.lower().strip()
    s = s.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    s = re.sub(r'[^a-z0-9\s-]', '', s)
    s = re.sub(r'[\s]+', '-', s)
    s = re.sub(r'-+', '-', s).strip('-')
    return s or 'story'


def _create_llm(model: str, provider: str = ""):
    """Creates an LLMClient + LLMInstance for the given model."""
    instance = create_llm_instance(
        task="chat",
        model=model,
        provider_name=provider)
    if not instance:
        return None, None
    return instance.create_llm(), instance


@router.post("/generate")
async def generate_story(request: Request):
    """Streams a generated story from the template + parameters."""
    data = await request.json()
    model = data.get("model", "")
    provider = data.get("provider", "")
    session_id = data.get("session_id", "")
    params = data.get("params", {})

    if not model:
        raise HTTPException(status_code=400, detail="model erforderlich")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id erforderlich")

    if not (_stories_dir() / "Template für ein LLM.md").exists():
        raise HTTPException(status_code=404, detail="Story-Template nicht gefunden")

    prompt_text = _build_story_prompt(params)
    llm, llm_instance = _create_llm(model, provider)
    if not llm:
        raise HTTPException(status_code=500, detail=f"Kein Provider fuer Model '{model}' gefunden")

    system_content = "Du bist ein kreativer Story-Autor. Folge den Anweisungen exakt und gib NUR die fertige Markdown-Datei aus, ohne Erklaerungen."

    agent = StreamingAgent(
        llm=llm,
        tool_format="tag",
        tools_dict={},
        agent_name="StoryDev",
        max_iterations=1,
        log_task="story_dev_generate")

    async def generate():
        from app.core.llm_queue import get_llm_queue
        _llm_queue = get_llm_queue()
        _task_id = await _llm_queue.register_chat_active_async(
            "StoryDev", llm_instance=llm_instance,
            task_type="story_dev", label="Story Dev Chat")
        full_response = ""
        try:
            async for event in agent.stream(system_content, [], prompt_text):
                if isinstance(event, ContentEvent):
                    full_response += event.content
                    yield f"data: {json.dumps({'content': event.content})}\n\n"

            # Session speichern
            _sessions[session_id] = {
                "model": model,
                "provider": provider,
                "messages": [
                    {"role": "user", "content": prompt_text},
                    {"role": "assistant", "content": full_response},
                ],
                "result": full_response,
            }

            yield f"data: {json.dumps({'done': True})}\n\n"

            # LLM-Logging erfolgt per-Iteration im StreamingAgent

        except Exception as e:
            logger.error("Generate error: %s", e)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            _llm_queue.register_chat_done(_task_id)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/refine")
async def refine_story(request: Request):
    """Streams a refined version of the story based on user instructions."""
    data = await request.json()
    session_id = data.get("session_id", "")
    instruction = data.get("instruction", "").strip()

    if not session_id or session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session nicht gefunden")
    if not instruction:
        raise HTTPException(status_code=400, detail="instruction erforderlich")

    session = _sessions[session_id]
    model = session["model"]
    provider = session["provider"]

    llm, llm_instance = _create_llm(model, provider)
    if not llm:
        raise HTTPException(status_code=500, detail=f"Kein Provider fuer Model '{model}'")

    system_content = (
        "Du bist ein kreativer Story-Autor. Der Benutzer hat zuvor eine interaktive Story "
        "im Markdown-Format generiert. Wende die folgenden Aenderungen an und gib die "
        "KOMPLETTE aktualisierte Story im gleichen Format aus. "
        "Gib NUR die Markdown-Datei aus, ohne Erklaerungen."
    )

    agent = StreamingAgent(
        llm=llm,
        tool_format="tag",
        tools_dict={},
        agent_name="StoryDev",
        max_iterations=1,
        log_task="story_dev_generate")

    # Bisherige History + neue Anweisung
    history = list(session["messages"])
    user_input = instruction

    async def generate():
        from app.core.llm_queue import get_llm_queue
        _llm_queue = get_llm_queue()
        _task_id = await _llm_queue.register_chat_active_async(
            "StoryDev", llm_instance=llm_instance,
            task_type="story_dev", label="Story Dev Chat")
        full_response = ""
        try:
            async for event in agent.stream(system_content, history, user_input):
                if isinstance(event, ContentEvent):
                    full_response += event.content
                    yield f"data: {json.dumps({'content': event.content})}\n\n"

            # Session aktualisieren
            session["messages"].append({"role": "user", "content": user_input})
            session["messages"].append({"role": "assistant", "content": full_response})
            session["result"] = full_response

            yield f"data: {json.dumps({'done': True})}\n\n"

        except Exception as e:
            logger.error("Refine error: %s", e)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            _llm_queue.register_chat_done(_task_id)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/load")
async def load_story(request: Request):
    """Loads an existing story file into a dev session for LLM refinement."""
    data = await request.json()
    model = data.get("model", "")
    provider = data.get("provider", "")
    content = data.get("content", "")
    source_filename = data.get("source_filename", "")

    if not model:
        raise HTTPException(status_code=400, detail="model erforderlich")
    if not content.strip():
        raise HTTPException(status_code=400, detail="content erforderlich")

    session_id = f"sd-load-{uuid.uuid4().hex[:8]}"

    _sessions[session_id] = {
        "model": model,
        "provider": provider,
        "messages": [
            {"role": "assistant", "content": content},
        ],
        "result": content,
        "source_filename": source_filename,
    }

    logger.info("Story geladen in Session %s: %s", session_id, source_filename)
    return {"status": "ok", "session_id": session_id}


@router.post("/save")
async def save_story(request: Request):
    """Saves the current story result as a .md file."""
    data = await request.json()
    session_id = data.get("session_id", "")

    if not session_id or session_id not in _sessions:
        raise HTTPException(status_code=404, detail="Session nicht gefunden")

    result = _sessions[session_id].get("result", "")
    if not result.strip():
        raise HTTPException(status_code=400, detail="Kein Story-Inhalt vorhanden")

    # Titel aus YAML-Frontmatter extrahieren
    title_match = re.search(r'title:\s*["\']?([^"\'\n]+)', result)
    title = title_match.group(1).strip() if title_match else "Neue Story"
    filename = data.get("filename", "").strip() or _slugify(title)

    # Einzigartigen Dateinamen sicherstellen
    target = _stories_dir() / f"{filename}.md"
    counter = 2
    while target.exists():
        target = _stories_dir() / f"{filename}-{counter}.md"
        counter += 1

    _stories_dir().mkdir(parents=True, exist_ok=True)
    target.write_text(result, encoding="utf-8")

    logger.info("Story gespeichert: %s", target.name)
    return {"status": "success", "filename": target.name}


@router.post("/cleanup")
async def cleanup_session(request: Request):
    """Removes a story dev session from memory."""
    data = await request.json()
    session_id = data.get("session_id", "")
    _sessions.pop(session_id, None)
    return {"status": "ok"}
