"""TTS routes - Text-to-Speech endpoints."""
import asyncio
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from app.core.log import get_logger

logger = get_logger("tts_route")

from app.core.tts_service import get_tts_service, clean_text_for_tts
from app.core.paths import get_storage_dir
from app.models.character import get_character_config

router = APIRouter(prefix="/tts", tags=["tts"])


@router.get("/status")
def tts_status() -> Dict[str, Any]:
    """Returns TTS service availability and config info."""
    service = get_tts_service()
    return service.status_info()


@router.post("/speak")
async def speak(request: Request) -> Dict[str, Any]:
    """Generiert Audio on-demand (Button-Klick).

    Request body:
        text: Zu sprechender Text
        user_id: User-ID
        character_name: Character-Name
    """
    data = await request.json()
    text = data.get("text", "")
    user_id = data.get("user_id", "")
    character_name = data.get("character_name", "")

    if not text.strip():
        raise HTTPException(status_code=400, detail="text is required")

    service = get_tts_service()
    if not service.enabled:
        return {"error": "TTS is disabled"}

    # Per-character config
    agent_config = {}
    if user_id and character_name:
        agent_config = get_character_config(character_name)
    tts_config = service.get_character_config(agent_config)

    clean_text = clean_text_for_tts(text)
    if not clean_text.strip():
        return {"error": "No speakable text after cleanup"}

    audio_path = await asyncio.to_thread(
        service.generate,
        text=clean_text,
        voice=tts_config.get("voice", ""),
        speaker_wav=tts_config.get("speaker_wav", ""),
        language=tts_config.get("language", "de"),
        voice_description=tts_config.get("voice_description", ""),
        character_name=tts_config.get("character_name", ""),
        comfyui_mode=tts_config.get("comfyui_mode", ""))

    if audio_path and audio_path.exists():
        return {"audio_url": f"/tts/tmp/{audio_path.name}"}

    return {"error": "TTS generation failed"}


@router.get("/tmp/{filename}")
def serve_tts_audio(filename: str):
    """Liefert eine temporaere TTS-Audio-Datei aus."""
    # Sicherheit: nur Dateiname, kein Pfad-Traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    path = get_storage_dir() / "tmp" / "tts_audio" / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="Audio nicht gefunden")

    media_type = "audio/wav"
    if filename.endswith(".mp3"):
        media_type = "audio/mpeg"

    return FileResponse(path, media_type=media_type)


@router.get("/speakers")
def list_speakers() -> Dict[str, Any]:
    """Listet verfuegbare Speaker-WAV-Dateien aus dem voices/ Verzeichnis."""
    voices_dir = Path("./voices")
    speakers: List[Dict[str, str]] = []
    if voices_dir.exists():
        for f in sorted(voices_dir.iterdir()):
            if f.suffix.lower() in (".wav", ".mp3", ".flac", ".ogg"):
                # Label: Dateiname ohne _ref und Extension
                label = f.stem
                if label.endswith("_ref"):
                    label = label[:-4]
                speakers.append({
                    "value": f"./voices/{f.name}",
                    "label": label,
                })
    return {"speakers": speakers}


@router.get("/voices")
def list_voices() -> Dict[str, Any]:
    """Listet verfuegbare TTS Voices."""
    service = get_tts_service()
    voices: List[Dict[str, str]] = []

    if service.backend == "magpie" and service.magpie_url:
        # Riva /v1/audio/list_voices: {"lang-group": {"voices": [...]}}
        try:
            import requests
            resp = requests.get(f"{service.magpie_url}/v1/audio/list_voices", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    for lang_group, info in data.items():
                        voice_list = info.get("voices", []) if isinstance(info, dict) else []
                        for v in voice_list:
                            if isinstance(v, str):
                                voices.append({"value": v, "label": v})
        except Exception:
            pass

    # Fallback: Aktuelle Default-Voice als einzige Option
    if not voices:
        if service.backend == "magpie" and service.magpie_voice:
            voices.append({"value": service.magpie_voice, "label": service.magpie_voice})

    return {"voices": voices}
