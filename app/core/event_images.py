"""Event-Illustration Pipeline.

Generiert Bilder fuer disruption/danger-Events, die den Hintergrund der
Location waehrend des Events ueberlagern. Auch das "After"-Bild bei
Resolution wird hier produziert.

Ablauf:
1. ``trigger_event_image(event_id, location_id, image_prompt)`` wird beim
   Event-Spawn aufgerufen. Sind background_image und ein passendes
   Backend/Workflow vorhanden, wird ein Bild generiert und
   ``image_path`` im Event-Payload gesetzt.
2. ``trigger_event_resolved_image(event_id, ...)`` wird im
   ``resolve_event``-Pfad aufgerufen. Output: ``resolved_image_path``.
3. ``get_effective_background(location_id, room, hour)`` liefert den
   Pfad, der vom ``/locations/{id}/background``-Endpoint ausgeliefert
   wird — Event-Bild bei aktivem ungeloesten Event, Resolved-Bild im
   Linger-Fenster, sonst normaler Location-Background.

Per-Welt-Default: ``EVENT_IMAGEGEN_DEFAULT`` aus config.json wird wie bei
Location-/Outfit-Bildern aufgeloest (Format ``workflow:<name>`` oder
``backend:<name>``).
"""

from __future__ import annotations

import os
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.log import get_logger
from app.core.paths import get_storage_dir

logger = get_logger("event_images")


# ---------------------------------------------------------------------------
# Pfade
# ---------------------------------------------------------------------------

def get_events_image_dir() -> Path:
    d = get_storage_dir() / "events"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _event_image_filename(event_id: str, resolved: bool) -> str:
    return f"{event_id}_resolved.png" if resolved else f"{event_id}.png"


def _event_image_path(event_id: str, resolved: bool) -> Path:
    return get_events_image_dir() / _event_image_filename(event_id, resolved)


# ---------------------------------------------------------------------------
# Backend / Workflow Auswahl
# ---------------------------------------------------------------------------

def _resolve_backend_and_workflow():
    """Resolves backend + workflow from ``EVENT_IMAGEGEN_DEFAULT`` env.

    Falls auf Location-Default zurueck (Backgrounds), wenn nichts gesetzt
    ist. Liefert ``(backend, workflow)``; ``workflow`` kann None sein
    (z.B. Cloud-Backend ohne ComfyUI).
    """
    try:
        from app.core.dependencies import get_skill_manager
    except Exception:
        return None, None

    sm = get_skill_manager()
    img_skill = sm.get_skill("image_generation") if sm else None
    if not img_skill:
        return None, None

    default = (os.environ.get("EVENT_IMAGEGEN_DEFAULT", "").strip()
               or os.environ.get("LOCATION_IMAGEGEN_DEFAULT", "").strip())
    backend = None
    workflow = None

    if default.startswith("workflow:"):
        wf_name = default[len("workflow:"):]
        workflow = next((w for w in img_skill.comfy_workflows if w.name == wf_name), None)
        if workflow:
            compat = workflow.compatible_backends or []
            candidates = [
                b for b in img_skill.backends
                if b.available and b.instance_enabled
                and b.api_type == "comfyui"
                and (not compat or b.name in compat)
            ]
            backend = img_skill.pick_lowest_cost(
                candidates, rotation_key=f"event_image:{workflow.name}")
    elif default.startswith("backend:"):
        be_name = default[len("backend:"):]
        backend = next(
            (b for b in img_skill.backends
             if b.name == be_name and b.available and b.instance_enabled), None)

    if not backend:
        backend = img_skill._select_backend()
    if backend and not workflow and backend.api_type == "comfyui":
        workflow = getattr(img_skill, "_default_workflow", None)

    return backend, workflow


# ---------------------------------------------------------------------------
# Effective Background Lookup
# ---------------------------------------------------------------------------

def get_effective_background_event(location_id: str) -> Optional[Path]:
    """Wenn ein aktives oder gerade-aufgeloestes (im Linger-Fenster)
    disruption/danger-Event mit Bild an dieser Location existiert,
    liefert diese Funktion den Pfad zum Event-Bild. Sonst None.

    Liefert den am juengsten erstellten Match — falls mehrere
    disruption/danger-Events (z.B. eines davon resolved+lingernd, eines
    aktiv) gleichzeitig existieren, gewinnt das aktive.
    """
    if not location_id:
        return None
    try:
        from app.models.events import list_events
    except Exception:
        return None

    try:
        linger_min = int(os.environ.get("EVENT_RESOLVED_IMAGE_LINGER_MINUTES", "30"))
    except (TypeError, ValueError):
        linger_min = 30

    candidates = []
    for evt in list_events(location_id=location_id):
        if evt.get("location_id") != location_id:
            continue
        cat = evt.get("category", "")
        if cat not in ("disruption", "danger"):
            continue
        candidates.append(evt)

    # Aktive Events bevorzugt vor resolved Events; innerhalb beider
    # Gruppen das juengste.
    active = [e for e in candidates if not e.get("resolved")]
    resolved = [e for e in candidates if e.get("resolved")]

    def _created(e):
        return e.get("created_at", "")

    if active:
        active.sort(key=_created, reverse=True)
        for evt in active:
            img = evt.get("image_path")
            if img and Path(img).exists():
                return Path(img)
        # Aktive Events ohne fertiges Bild → kein Swap. Wir warten.

    # Resolved-Linger: zeige resolved_image_path solange linger-Fenster offen.
    if resolved:
        resolved.sort(key=_created, reverse=True)
        for evt in resolved:
            resolved_at = evt.get("resolved_at")
            if not resolved_at:
                continue
            try:
                age_sec = (datetime.now() - datetime.fromisoformat(resolved_at)).total_seconds()
            except (TypeError, ValueError):
                continue
            if age_sec > linger_min * 60:
                continue
            rimg = evt.get("resolved_image_path") or evt.get("image_path")
            if rimg and Path(rimg).exists():
                return Path(rimg)

    return None


# ---------------------------------------------------------------------------
# Subscribers fuer SSE (Background-Ready Push)
# ---------------------------------------------------------------------------

import asyncio
from typing import AsyncIterator, List, Tuple

_subscribers: List[Tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = []
_subs_lock = threading.Lock()


def publish_image_ready(event_id: str, location_id: str, kind: str) -> None:
    """Broadcast: Event-Bild fertig. ``kind`` ist 'event' oder 'resolved'."""
    payload = {"event_id": event_id, "location_id": location_id, "kind": kind}
    with _subs_lock:
        subs = list(_subscribers)
    for q, loop in subs:
        try:
            loop.call_soon_threadsafe(q.put_nowait, payload)
        except Exception as e:
            logger.debug("publish_image_ready: %s", e)


async def subscribe() -> AsyncIterator[Dict[str, Any]]:
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    entry = (q, loop)
    with _subs_lock:
        _subscribers.append(entry)
    try:
        while True:
            yield await q.get()
    finally:
        with _subs_lock:
            if entry in _subscribers:
                _subscribers.remove(entry)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def _read_image_dimensions(path: Path) -> Optional[tuple]:
    """Liest (width, height) des Referenzbildes. Bei Fehler None."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            return im.size  # (width, height)
    except Exception as e:
        logger.debug("Konnte Bildgroesse nicht lesen (%s): %s", path, e)
        return None


def _safe_dims(width: int, height: int) -> tuple:
    """Snap auf 8er-Vielfache (ComfyUI/Latents-Constraint), Cap auf 2048."""
    w = max(64, min(2048, (width // 8) * 8))
    h = max(64, min(2048, (height // 8) * 8))
    return w, h


def _do_generate(event_id: str,
                  location_id: str,
                  image_prompt: str,
                  resolved: bool) -> Optional[Path]:
    """Synchrone Generierung (bereits in der Queue serialisiert).

    Returns Pfad zum gespeicherten Bild oder None.
    """
    from app.models.world import get_background_path
    from app.models.events import update_event_fields

    # Referenzbild: aktuelles Location-Background. Tageszeit (Stunde) wird
    # mitgegeben, damit get_background_path das passende Tag/Nacht-Bild
    # waehlt — gleiche Logik wie der /background-Endpoint. Ohne Stunde
    # waere die Auswahl rein zufaellig zwischen allen Bg-Bildern, was zu
    # einer "alten" oder Tag/Nacht-falschen Vorlage fuer das Event-Bild
    # fuehrt.
    bg_path = get_background_path(location_id, hour=datetime.now().hour)
    if not bg_path or not bg_path.exists():
        logger.info("Event-Bild [%s]: kein Background — skip", event_id)
        return None

    dims = _read_image_dimensions(bg_path)
    if not dims:
        logger.info("Event-Bild [%s]: Background-Dimensionen unbekannt — skip", event_id)
        return None
    w, h = _safe_dims(*dims)

    backend, active_workflow = _resolve_backend_and_workflow()
    if not backend:
        logger.warning("Event-Bild [%s]: kein Backend verfuegbar", event_id)
        return None

    full_prompt = image_prompt.strip()
    if active_workflow and active_workflow.prompt_style:
        full_prompt = f"{active_workflow.prompt_style}, {full_prompt}"
    elif getattr(backend, "prompt_prefix", ""):
        full_prompt = f"{backend.prompt_prefix}, {full_prompt}"

    negative = getattr(backend, "negative_prompt", "") or ""
    if active_workflow and getattr(active_workflow, "negative_prompt", ""):
        negative = active_workflow.negative_prompt

    try:
        denoise = float(os.environ.get("EVENT_IMAGE_DENOISE_STRENGTH", "0.7"))
    except (TypeError, ValueError):
        denoise = 0.7

    params: Dict[str, Any] = {
        "width": w,
        "height": h,
        # Bild-Generierung soll nicht aus Cache kommen — neuer Seed pro Run.
        "seed": random.randint(1, 2**31 - 1),
    }

    if active_workflow and active_workflow.workflow_file:
        params["workflow_file"] = active_workflow.workflow_file
        if active_workflow.model:
            _model_key = "unet" if active_workflow.has_input_unet else "model"
            params[_model_key] = active_workflow.model
        if active_workflow.clip:
            params["clip_name"] = active_workflow.clip

    # Reference-Image als Background-Slot. Workflows die einen einzelnen
    # Slot mit Titel ``input_reference_image`` haben (ohne Suffix) werden
    # vom Backend auf den Slot-1-Eintrag im reference_images-Dict
    # gemapped. Per-Workflow Switch ``input_reference_image_use`` wird
    # automatisch auf boolean=True gesetzt.
    params["reference_images"] = {"input_reference_image_1": str(bg_path)}

    # Denoise-Strength: PrimitiveFloat-Node ``input_denoise_strength``
    # (vom User im Workflow zu definieren). Falls der Workflow keinen
    # solchen Node hat, ignoriert das Backend den Wert.
    params["float_inputs"] = {"input_denoise_strength": denoise}

    try:
        from app.core.llm_queue import get_llm_queue, Priority as _P
        is_local = backend.api_type in ("comfyui", "a1111")
        vram = 0
        if is_local:
            vram = (active_workflow.vram_required_mb
                    if active_workflow and active_workflow.vram_required_mb
                    else getattr(backend, "vram_required_mb", 0))
            images = get_llm_queue().submit_gpu_task(
                task_type="event_image",
                priority=_P.IMAGE_GEN,
                callable_fn=lambda: backend.generate(full_prompt, negative, params),
                agent_name="system",
                label=f"Event: {event_id}{' (after)' if resolved else ''}",
                vram_required_mb=vram,
                gpu_type="comfyui")
        else:
            images = backend.generate(full_prompt, negative, params)
    except Exception as e:
        logger.error("Event-Bild [%s] Backend-Fehler: %s", event_id, e)
        return None

    if images == "NO_NEW_IMAGE":
        logger.warning("Event-Bild [%s]: ComfyUI Cache-Hit — skip", event_id)
        return None
    if not images:
        logger.warning("Event-Bild [%s]: leeres Backend-Ergebnis", event_id)
        return None

    out_path = _event_image_path(event_id, resolved)
    try:
        out_path.write_bytes(images[0])
    except Exception as e:
        logger.error("Event-Bild [%s] write_bytes Fehler: %s", event_id, e)
        return None

    field = "resolved_image_path" if resolved else "image_path"
    update_event_fields(event_id, **{field: str(out_path)})
    publish_image_ready(event_id, location_id, "resolved" if resolved else "event")
    logger.info("Event-Bild [%s] %s generiert: %s (%dx%d)",
                event_id, "resolved" if resolved else "active", out_path.name, w, h)
    return out_path


def trigger_event_image(event_id: str,
                         location_id: str,
                         image_prompt: str,
                         resolved: bool = False) -> None:
    """Fire-and-forget Event-Bild-Generierung.

    Wird sowohl vom synchronen ``_generate_event``-Hot-Path als auch von
    Background-Threads aufgerufen — Generation wird in den GPU-Queue
    (submit_gpu_task) verschoben, das hier startet nur einen Thread, der
    die Submission macht. Der Thread blockiert bis das Bild geschrieben
    ist.
    """
    if not event_id or not location_id or not image_prompt:
        return

    def _run():
        try:
            _do_generate(event_id, location_id, image_prompt, resolved)
        except Exception as e:
            logger.error("Event-Bild [%s] Thread-Fehler: %s", event_id, e, exc_info=True)

    threading.Thread(target=_run, daemon=True, name=f"event-image-{event_id[:8]}").start()


def trigger_event_resolved_image(event_id: str,
                                  location_id: str,
                                  resolved_image_prompt: str) -> None:
    """Wie ``trigger_event_image`` — generiert das After-Bild bei Resolution."""
    trigger_event_image(event_id, location_id, resolved_image_prompt, resolved=True)


def trigger_resolved_image_from_text(event_id: str) -> None:
    """Hook fuer ``resolve_event``: erzeugt das After-Bild aus
    ``resolved_text`` + Ursprungs-Image-Prompt.

    Laeuft in einem Background-Thread, damit der Resolve-Pfad nicht
    blockiert. Bei fehlendem Background oder fehlender ``image_prompt``-
    Saat (Event ohne Bild beim Spawn — z.B. Welt-Daten-Lueck) wird
    sauber abgebrochen.
    """
    def _run():
        try:
            from app.models.events import get_event
            evt = get_event(event_id)
            if not evt:
                return
            loc_id = evt.get("location_id") or ""
            if not loc_id:
                return
            if evt.get("category") not in ("disruption", "danger"):
                return
            # Konsistenz: kein "After"-Bild ohne sichtbares "Before"-Bild.
            # Wenn der Ursprungs-Render nie geliefert hat (kein Background
            # zur Spawn-Zeit, Cache-Hit, Backend-Fehler), bleibt die
            # Location bei ihrem normalen Bild.
            if not evt.get("image_path"):
                return

            resolved_text = (evt.get("resolved_text") or "").strip()
            original_image_prompt = (evt.get("metadata", {}) or {}).get("image_prompt", "")

            new_prompt = _generate_resolved_image_prompt(
                event_text=evt.get("text", ""),
                resolved_text=resolved_text,
                original_image_prompt=original_image_prompt)
            if not new_prompt:
                return
            _do_generate(event_id, loc_id, new_prompt, resolved=True)
        except Exception as e:
            logger.error("Event-Resolved-Bild [%s] Fehler: %s", event_id, e, exc_info=True)

    threading.Thread(target=_run, daemon=True, name=f"event-image-resolved-{event_id[:8]}").start()


def _generate_resolved_image_prompt(event_text: str,
                                     resolved_text: str,
                                     original_image_prompt: str) -> str:
    """Tool-LLM: erzeugt einen englischen Bild-Prompt fuer das After-Bild.

    Ist klein gehalten — gibt bei Fehler einen einfachen Fallback zurueck
    (Ursprung minus "smoke/fire/danger" Schlagworte), damit das Bild
    nicht ausfaellt nur weil das LLM gerade nicht antwortet.
    """
    from app.core.llm_router import llm_call
    from app.core.prompt_templates import render_task

    try:
        sys_prompt, user_prompt = render_task(
            "random_event_resolved_image",
            event_text=event_text or "",
            resolved_text=resolved_text or "",
            original_image_prompt=original_image_prompt or "")
        response = llm_call(
            task="random_event",
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            agent_name="system")
        text = (response.content or "").strip()
        # Code-Fences abschneiden, Quotes trimmen, Markdown-Artefakte raus.
        import re as _re
        text = _re.sub(r"```[a-z]*\s*|\s*```", "", text).strip()
        text = text.strip('"').strip("'").strip()
        if text and len(text) > 10:
            return text
    except Exception as e:
        logger.debug("resolved_image_prompt LLM-Fehler: %s", e)

    # Fallback: Original-Prompt mit "aftermath"-Marker
    if original_image_prompt:
        return f"aftermath of: {original_image_prompt} — now resolved, calm, no active threat"
    return ""
