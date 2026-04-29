"""Instagram Feed Routes - Unified Feed API"""
import mimetypes
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response
from typing import Dict, Any

from fastapi import Request
from app.core.log import get_logger

logger = get_logger("instagram")

from app.models.instagram import load_feed, save_feed, get_post, delete_post, toggle_like, add_comment, add_character_like, get_instagram_dir, load_image_meta, save_image_meta
from app.models.memory import upsert_relationship_memory as upsert_character_relationship

router = APIRouter(prefix="/instagram", tags=["instagram"])


@router.get("/feed")
def get_feed(character_name: str = "", limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    """Gibt den unified Instagram-Feed zurueck. Optional nach character_name filtern."""
    try:
        feed = load_feed()

        # Optional nach Character filtern
        if character_name:
            feed = [p for p in feed if p.get("agent_name") == character_name]

        total = len(feed)
        paginated = feed[offset:offset + limit]

        # Bild-URL + Metadaten fuer jeden Post ergaenzen
        for post in paginated:
            post["image_url"] = f"/instagram/images/{post['image_filename']}"
            # Multi-Image URLs (Carousel)
            if post.get("image_filenames"):
                post["image_urls"] = [
                    f"/instagram/images/{fn}"
                    for fn in post["image_filenames"]
                ]
            # Video-URL ergaenzen falls vorhanden
            if post.get("video_filename"):
                post["video_url"] = f"/instagram/images/{post['video_filename']}"
            # Bild-Metadaten aus separater JSON-Datei laden
            post["image_meta"] = load_image_meta(post.get("image_filename", ""))

        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "posts": paginated,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/post/{post_id}")
def get_single_post(post_id: str) -> Dict[str, Any]:
    """Gibt einen einzelnen Post zurueck."""
    post = get_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post nicht gefunden")
    post["image_url"] = f"/instagram/images/{post['image_filename']}"
    return {"post": post}


@router.delete("/post/{post_id}")
def delete_single_post(post_id: str) -> Dict[str, Any]:
    """Loescht einen Post und das zugehoerige Bild."""
    post = get_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post nicht gefunden")

    # Bild-Datei und Metadaten loeschen
    image_filename = post.get("image_filename", "")
    if image_filename:
        instagram_dir = get_instagram_dir()
        image_path = instagram_dir / image_filename
        if image_path.exists():
            try:
                image_path.unlink()
            except Exception:
                pass
        # Metadaten-Datei loeschen
        from app.models.instagram import get_image_meta_path
        meta_path = get_image_meta_path(image_filename)
        if meta_path.exists():
            try:
                meta_path.unlink()
            except Exception:
                pass

    # Post aus Feed entfernen
    if delete_post(post_id):
        return {"status": "success", "message": f"Post {post_id} geloescht"}
    raise HTTPException(status_code=404, detail="Post nicht gefunden")


@router.delete("/post/{post_id}/image/{image_filename}")
def delete_post_image(post_id: str, image_filename: str) -> Dict[str, Any]:
    """Loescht ein einzelnes Bild aus einem Carousel-Post."""
    if ".." in image_filename or "/" in image_filename:
        raise HTTPException(status_code=400, detail="Ungueltiger Dateiname")
    from app.models.instagram import remove_post_image
    if remove_post_image(post_id, image_filename):
        return {"status": "success", "deleted_image": image_filename}
    raise HTTPException(status_code=404, detail="Bild nicht im Post gefunden")


@router.post("/post/{post_id}/like")
def like_post(post_id: str, liker_name: str = "") -> Dict[str, Any]:
    """Erhoeht den Like-Zaehler eines Posts."""
    if not liker_name:
        from app.models.account import get_active_character, get_user_name
        liker_name = get_active_character() or get_user_name() or "Player"
    # Nutze add_character_like fuer named tracking, Fallback auf toggle_like
    liked = add_character_like(post_id, liker_name)
    if not liked:
        # Bereits geliked oder nicht gefunden — trotzdem toggle_like als Fallback
        toggle_like(post_id)
    post_after = get_post(post_id)
    new_count = post_after.get("likes", 0) if post_after else None
    if new_count is not None:
        # Beziehungsfakt: Character merkt sich, dass jemand den Post geliked hat
        post = get_post(post_id)
        if post:
            character_name = post.get("agent_name", "")
            caption_preview = (post.get("caption") or "")[:60]
            if character_name and liker_name:
                upsert_character_relationship(
                    character_name=character_name,
                    related_character=liker_name,
                    new_fact=f'{liker_name} liked my Instagram post: "{caption_preview}"',
                    replace_prefix=f"{liker_name} liked my Instagram post:")
        return {"status": "success", "likes": new_count}
    raise HTTPException(status_code=404, detail="Post nicht gefunden")


@router.post("/post/{post_id}/comment")
async def comment_post(post_id: str, request: Request) -> Dict[str, Any]:
    """Fuegt einen Kommentar zu einem Post hinzu."""
    data = await request.json()
    user_id = data.get("user_id", "")
    commenter_name = data.get("commenter_name", "").strip()
    text = data.get("text", "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Kommentartext erforderlich")
    if not commenter_name:
        from app.models.account import get_active_character, get_user_name
        commenter_name = get_active_character() or get_user_name() or "Player"

    comment = add_comment(post_id, commenter_name, text)
    if comment:
        # character_name aus dem Post holen fuer Knowledge + Reaktion
        post = get_post(post_id)
        character_name = post.get("agent_name", "") if post else ""
        if character_name:
            try:
                upsert_character_relationship(
                    character_name=character_name,
                    related_character=commenter_name,
                    new_fact=f'{commenter_name} commented on my Instagram: "{text[:120]}"',
                    replace_prefix=f"{commenter_name} commented on my Instagram:")
            except Exception as e:
                logger.error("Knowledge-Eintrag fehlgeschlagen: %s", e)

            # Character-Reaktion auf User-Kommentar triggern
            # (nur wenn Kommentar nicht vom Character selbst ist)
            if commenter_name != character_name:
                try:
                    from app.core.social_reactions import trigger_user_comment_reaction
                    trigger_user_comment_reaction(
                        character_name=character_name,
                        post_id=post_id,
                        commenter_name=commenter_name,
                        comment_text=text,
                        comment_id=comment.get("id", ""),
                        post=post)
                except Exception as e:
                    logger.error("Character-Reaktion Trigger fehlgeschlagen: %s", e)

        return {"status": "success", "comment": comment}
    raise HTTPException(status_code=404, detail="Post nicht gefunden")


@router.post("/post/{post_id}/detect-characters")
async def detect_post_characters(post_id: str, request: Request) -> Dict[str, Any]:
    """Erkennt im Post verwendete Characters aus reference_images Metadaten."""
    from app.models.character import list_available_characters
    from app.models.account import get_user_profile

    data = await request.json()
    user_id = data.get("user_id", "")

    post = get_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post nicht gefunden")

    character_name = post.get("agent_name", "")
    all_chars = list_available_characters()
    user_profile = get_user_profile()
    user_name = user_profile.get("user_name", "")

    # 1. Primaer: explizit gespeicherte character_names
    img_meta = load_image_meta(post.get("image_filename", ""))
    saved_names = (img_meta or {}).get("character_names")
    if saved_names and isinstance(saved_names, list):
        detected_names = saved_names
    else:
        # 2. Fallback: aus reference_images ableiten
        #    Slot 4 ist Location-Hintergrund, keine Person
        detected_names = []
        ref_images = (img_meta or {}).get("reference_images", {})
        for _slot, ref_filename in ref_images.items():
            if _slot == "input_reference_image_4":
                continue  # Location-Slot
            matched = False
            for c in all_chars:
                if ref_filename.startswith(c + "_"):
                    if c not in detected_names:
                        detected_names.append(c)
                    matched = True
                    break
            if not matched and user_name and user_name not in detected_names:
                detected_names.append(user_name)

        # 3. Fallback: Prompt-basierte Erkennung
        if not detected_names:
            image_prompt = (img_meta or {}).get("prompt", "") or post.get("image_prompt", "")
            if image_prompt and character_name:
                from app.core.prompt_builder import PromptBuilder
                _pb = PromptBuilder(character_name)
                _persons = _pb.detect_persons(image_prompt)
                appearances = [{"name": p.name, "appearance": p.appearance} for p in _persons]
                detected_names = [p["name"] for p in appearances]

    available = []
    if character_name in all_chars:
        available.append({"name": character_name, "type": "agent"})
    if user_name:
        available.append({"name": user_name, "type": "user"})
    for c in all_chars:
        if c != character_name and c != user_name:
            available.append({"name": c, "type": "character"})

    return {"detected": detected_names, "available": available}


@router.post("/post/{post_id}/regenerate")
async def regenerate_post_image(post_id: str, request: Request):
    """Regeneriert das Bild eines Posts ueber die ImageGenerationSkill-Pipeline."""
    from app.skills.image_regenerate import regenerate_image
    from app.models.character import get_character_config

    data = await request.json()
    user_id = data.get("user_id", "")

    post = get_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post nicht gefunden")

    character_name = post.get("agent_name", "")
    image_filename = post.get("image_filename", "")
    instagram_dir = get_instagram_dir()
    image_path = instagram_dir / image_filename
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Bilddatei nicht gefunden")

    # Prompt aus Bild-Metadaten laden (neue Struktur) mit Fallback auf alten Feed-Eintrag
    img_meta = load_image_meta(image_filename)
    image_prompt = (img_meta or {}).get("prompt", "") or post.get("image_prompt", "")
    if not image_prompt:
        raise HTTPException(status_code=422, detail="Kein Prompt fuer dieses Bild gespeichert.")

    # Custom-Prompt aus Dialog uebernimmt gespeicherten Prompt
    custom_prompt = data.get("custom_prompt", "").strip()
    if custom_prompt:
        image_prompt = custom_prompt

    improvement_request = data.get("improvement_request", "").strip()
    workflow_name = data.get("workflow", "").strip()
    backend_name = data.get("backend", "").strip()
    loras = data.get("loras")  # Optional: [{name, strength}, ...]
    model_override = data.get("model_override", "").strip()
    character_names = data.get("character_names")  # Optional: explizite Character-Auswahl
    room_id = data.get("room_id", "").strip()
    negative_prompt_override = data.get("negative_prompt", "").strip()
    create_new = data.get("create_new", False)
    # Originale Location aus Bild-Metadaten
    original_location_id = (img_meta or {}).get("location", "")
    agent_config = get_character_config(character_name)

    from app.core.task_queue import get_task_queue
    from app.core.task_router import resolve_queue
    _tq = get_task_queue()
    _queue = resolve_queue("image_regenerate", {}, agent_name=character_name)
    _track_id = _tq.track_start(
        "image_regenerate", "Instagram Bild regenerieren", agent_name=character_name,
        provider=backend_name or workflow_name or "",
        queue_name=_queue,
        start_running=False)

    def _run_regen():
        try:
            _success, final_prompt, actual_path = regenerate_image(character_name, str(image_path),
                image_prompt, improvement_request, workflow_name, backend_name, agent_config,
                loras=loras, model_override=model_override,
                character_names=character_names,
                room_id=room_id,
                location_id=original_location_id,
                negative_prompt_override=negative_prompt_override,
                track_id=_track_id,
                create_new=bool(create_new))
            from pathlib import Path as _Path
            _actual_filename = _Path(actual_path).name
            if final_prompt != image_prompt:
                meta = load_image_meta(_actual_filename) or {}
                meta["prompt"] = final_prompt
                save_image_meta(_actual_filename, meta)
            # Bei create_new: neues Bild zum Post hinzufuegen (Carousel)
            if create_new and _actual_filename != image_filename:
                from app.models.instagram import add_post_image
                add_post_image(post_id, _actual_filename)
            _tq.track_finish(_track_id)
        except Exception as e:
            logger.error("Instagram Regenerierung fehlgeschlagen: %s", e)
            _tq.track_finish(_track_id, error=str(e))

    import threading
    threading.Thread(target=_run_regen, daemon=True).start()
    return {"status": "started", "post_id": post_id, "track_id": _track_id}


@router.get("/images/{image_filename}")
def get_instagram_image(image_filename: str):
    """Liefert ein Instagram-Bild aus dem unified Instagram-Ordner."""
    try:
        # Sicherheit: Path Traversal verhindern
        if ".." in image_filename or "/" in image_filename:
            raise HTTPException(status_code=400, detail="Ungueltiger Dateiname")

        instagram_dir = get_instagram_dir()
        image_path = instagram_dir / image_filename

        if not image_path.exists():
            return Response(status_code=204)

        media_type, _ = mimetypes.guess_type(str(image_path))
        return FileResponse(
            image_path,
            media_type=media_type or "application/octet-stream",
            headers={"Cache-Control": "no-cache"}
        )
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/post/{post_id}/suggest-animate-prompt")
async def suggest_instagram_animate_prompt(post_id: str, request: Request) -> Dict[str, str]:
    """Generiert einen Animation-Prompt fuer ein Instagram-Bild via Tools-LLM."""
    import asyncio

    data = await request.json()
    user_id = data.get("user_id", "")
    custom_system_prompt = data.get("system_prompt", "")
    llm_override = data.get("llm_override", "").strip()

    post = get_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post nicht gefunden")

    character_name = post.get("agent_name", "")
    image_filename = post.get("image_filename", "")
    instagram_dir = get_instagram_dir()
    image_path = instagram_dir / image_filename
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Bilddatei nicht gefunden")

    def _generate_prompt() -> str:
        meta = load_image_meta(image_filename) or {}

        # Bildanalyse aus Metadaten lesen oder neu generieren
        image_analysis = meta.get("image_analysis", "")
        if not image_analysis:
            logger.info("[suggest-animate] Instagram: Keine Bildanalyse, generiere neu...")
            try:
                from app.skills.image_generation_skill import ImageGenerationSkill
                skill = ImageGenerationSkill({})
                image_analysis = skill._generate_image_analysis(str(image_path), character_name)
                if image_analysis:
                    meta["image_analysis"] = image_analysis
                    save_image_meta(image_filename, meta)
            except Exception as e:
                logger.warning("[suggest-animate] Bildanalyse fehlgeschlagen: %s", e)

        if not image_analysis:
            raise ValueError("Bildanalyse nicht verfuegbar")

        logger.info("[suggest-animate] Instagram: Bildanalyse vorhanden, rufe LLM auf... (llm_override=%s)", llm_override or "")

        system_content = custom_system_prompt or (
            "You write short animation prompts for image-to-video AI models. "
            "The user gives you an image description. You respond with ONLY the MOTION instructions. "
            "Do NOT re-describe the image content, appearance, clothing, or scene. "
            "ONLY describe what MOVES and HOW it moves. Keep it to 1-3 sentences.\n\n"
            "Good example: 'She blinks slowly and tilts her head slightly. Her hair sways gently. "
            "Background lights pulse and flicker.'\n"
            "Bad example: 'A woman with pink hair wearing a dress stands in a room...' (this re-describes the image)\n\n"
            "Reply ONLY with the motion prompt. No explanations, no markdown."
        )

        from app.core.llm_router import llm_call
        response = llm_call(
            task="instagram_caption",
            system_prompt=system_content,
            user_prompt=f"Bildbeschreibung:\n{image_analysis}",
            agent_name=character_name)
        result = (response.content or "").strip().strip('"').strip("'")
        logger.info("[suggest-animate] Instagram Prompt: %s", result[:100])
        return result

    try:
        prompt = await asyncio.get_event_loop().run_in_executor(None, _generate_prompt)
        return {"prompt": prompt}
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error("[suggest-animate] Instagram fehlgeschlagen: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/post/{post_id}/animate")
async def animate_instagram_post(post_id: str, request: Request) -> Dict[str, Any]:
    """Animiert ein Instagram-Bild als Video via img2video ComfyUI Workflow."""
    data = await request.json()
    user_id = data.get("user_id", "")

    post = get_post(post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post nicht gefunden")

    character_name = post.get("agent_name", "")
    image_filename = post.get("image_filename", "")
    instagram_dir = get_instagram_dir()
    image_path = instagram_dir / image_filename
    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Bilddatei nicht gefunden")

    prompt = data.get("prompt", "").strip()
    if not prompt:
        img_meta = load_image_meta(image_filename)
        prompt = (img_meta or {}).get("prompt", "") or post.get("image_prompt", "")
    if not prompt:
        raise HTTPException(status_code=422, detail="Kein Prompt angegeben")

    service = data.get("service", "").strip()
    loras_high = data.get("loras_high")
    loras_low = data.get("loras_low")

    from app.core.task_queue import get_task_queue
    _tq = get_task_queue()
    _track_id = _tq.track_start(
        "image_animate", "Instagram Bild animieren", agent_name=character_name,
        start_running=False)

    def _run_animate():
        _tq.track_activate(_track_id)
        try:
            from app.skills.animate import animate_image
            from app.models.instagram import load_feed as _load_feed, save_feed as _save_feed
            from app.core.llm_queue import get_llm_queue, Priority as _P
            from pathlib import Path as _Path
            from datetime import datetime

            stem = _Path(image_filename).stem
            video_name = f"{stem}.mp4"
            output_path = str(instagram_dir / video_name)

            # Ueber Provider-Queue ausfuehren (GPU-Serialisierung + Queue-Panel)
            success = get_llm_queue().submit_gpu_task(
                task_type="image_animate",
                priority=_P.IMAGE_GEN,
                callable_fn=lambda: animate_image(
                    str(image_path), prompt, output_path,
                    service=service, loras_high=loras_high, loras_low=loras_low),
                agent_name=character_name,
                label="Instagram Animation",
                gpu_type="comfyui")

            if not success:
                _tq.track_finish(_track_id, error="Animation fehlgeschlagen")
                return

            # Bestehenden Post aktualisieren: video_filename setzen
            feed = _load_feed()
            for p in feed:
                if p.get("id") == post_id:
                    p["video_filename"] = video_name
                    break
            _save_feed(feed)

            meta = load_image_meta(image_filename) or {}
            meta["animate_prompt"] = prompt
            meta["animate_created_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            save_image_meta(image_filename, meta)
            _tq.track_finish(_track_id)
        except Exception as e:
            logger.error("Instagram Animation fehlgeschlagen: %s", e)
            _tq.track_finish(_track_id, error=str(e))

    import threading
    threading.Thread(target=_run_animate, daemon=True).start()
    return {"status": "started", "post_id": post_id, "track_id": _track_id}
