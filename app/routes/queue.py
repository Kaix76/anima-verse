"""Queue Status Endpoint — zeigt alle laufenden und kuerzlichen Tasks."""
import os
from typing import Any, Dict, List, Set

import requests
from fastapi import APIRouter, HTTPException

from app.core.log import get_logger

logger = get_logger("queue_route")

router = APIRouter(prefix="/queue", tags=["queue"])


@router.get("/status")
async def queue_status() -> Dict[str, Any]:
    """Gibt den kombinierten Status aller Task-Quellen zurueck.

    Zusammenfuehrung von:
    - Provider Queues (per-Provider LLM-Calls, Chat-Streaming)
    - TaskQueue (persistente Background-Tasks + tracked GPU/TTS/Image Tasks)
    """
    import asyncio
    from app.core.provider_manager import get_provider_manager
    from app.core.task_queue import get_task_queue

    pm = get_provider_manager()
    # get_combined_status() macht synchrone HTTP-Calls (Beszel) — im Threadpool laufen
    # lassen, sonst blockiert der Event-Loop bei langsamem Beszel.
    combined = await asyncio.to_thread(pm.get_combined_status)
    tq = get_task_queue()
    tq_status = tq.get_status()

    # Active tracked tasks (running ODER pending) — Image-Gen, TTS, GPU-Tasks
    # die ausserhalb der TaskQueue laufen aber im Panel sichtbar sein sollen.
    _active = []
    for t in tq_status.get("running", []) or []:
        if t.get("task_origin") == "tracked":
            _active.append(t)
    for t in tq_status.get("pending", []) or []:
        if t.get("task_origin") == "tracked":
            _active.append(t)

    return {
        # Per-Channel Queues (each GPU = own channel with tasks)
        "providers": combined.get("providers", {}),
        # Flat fields (still used by UI)
        "chat_active": combined["chat_active"],
        "recent": combined["recent"],
        # Tracked + background tasks (unified)
        "active_tasks": _active,
        "recent_tasks": tq.get_tracked_recent(),
        # Background tasks (flat, no named queues)
        "bg_tasks": tq_status,
    }


@router.delete("/tasks/{task_id}")
async def cancel_task(task_id: str) -> Dict[str, Any]:
    """Bricht einen wartenden oder laufenden Task in der Queue ab."""
    from app.core.provider_manager import get_provider_manager
    from app.core.task_queue import get_task_queue

    pm = get_provider_manager()
    cancelled = pm.cancel_task(task_id)

    if not cancelled:
        # Fallback: Tracked tasks (image_generation, etc.)
        cancelled = get_task_queue().track_cancel(task_id)

    if cancelled:
        return {"status": "cancelled", "task_id": task_id}
    return {"status": "not_found", "task_id": task_id,
            "message": "Task nicht gefunden"}


@router.post("/force-resume")
async def force_resume_queues() -> Dict[str, Any]:
    """Erzwingt Fortsetzen aller pausierten Provider-Queues.

    Bereinigt veraltete Chat-Registrierungen die verhindern, dass
    Hintergrund-Tasks verarbeitet werden.
    """
    from app.core.provider_manager import get_provider_manager

    pm = get_provider_manager()
    resumed = []
    for name, pq in pm.channels.items():
        with pq._lock:
            if pq._chat_tasks:
                for tid, task in list(pq._chat_tasks.items()):
                    task.status = "completed"
                    pq._history.append(task)
                    resumed.append({"provider": name, "chat_task": tid, "agent": task.agent_name})
                pq._chat_tasks.clear()
                pq._chat_registered_at = 0.0
        if any(r["provider"] == name for r in resumed):
            pq._chat_active.set()
            logger.warning("Force-resume: %s — %d chat(s) cleared", name,
                           sum(1 for r in resumed if r["provider"] == name))

    if resumed:
        return {"status": "resumed", "cleared": resumed}
    return {"status": "ok", "message": "Keine pausierten Queues gefunden"}


@router.get("/vram")
async def vram_status() -> Dict[str, Any]:
    """VRAM-Status aller Ollama-Provider (via /api/ps)."""
    from app.core.provider_manager import get_provider_manager
    pm = get_provider_manager()
    return pm.poll_all_vram()


def _collect_comfyui_urls() -> List[str]:
    """Sammelt alle einzigartigen ComfyUI-URLs aus der Konfiguration."""
    urls: Set[str] = set()

    # Image Generation Instanzen (SKILL_IMAGEGEN_N_API_TYPE=comfyui, nur aktivierte)
    for i in range(1, 10):
        enabled = os.environ.get(f"SKILL_IMAGEGEN_{i}_ENABLED", "true").strip().lower() in ("true", "1", "yes")
        if not enabled:
            continue
        api_type = os.environ.get(f"SKILL_IMAGEGEN_{i}_API_TYPE", "").strip().lower()
        if api_type == "comfyui":
            url = os.environ.get(f"SKILL_IMAGEGEN_{i}_API_URL", "").strip().rstrip("/")
            if url:
                urls.add(url)

    return sorted(urls)


@router.post("/comfyui/free-vram")
def free_comfyui_vram() -> Dict[str, Any]:
    """Sendet POST /free an alle ComfyUI-Instanzen um VRAM freizugeben."""
    comfy_urls = _collect_comfyui_urls()
    if not comfy_urls:
        return {"status": "no_comfyui", "message": "Keine ComfyUI-Instanzen konfiguriert"}

    results = []
    for url in comfy_urls:
        try:
            resp = requests.post(
                f"{url}/free",
                json={"unload_models": True, "free_memory": True},
                timeout=10)
            ok = resp.status_code == 200
            results.append({"url": url, "ok": ok, "status": resp.status_code})
            logger.info("ComfyUI VRAM freed: %s (HTTP %d)", url, resp.status_code)
        except Exception as e:
            results.append({"url": url, "ok": False, "error": str(e)})
            logger.error("ComfyUI VRAM free failed: %s: %s", url, e)

    all_ok = all(r["ok"] for r in results)
    return {
        "status": "ok" if all_ok else "partial",
        "results": results,
    }


# ---------------------------------------------------------------------------
# Background Task Queue (persistent SQLite-backed)
# ---------------------------------------------------------------------------

@router.get("/tasks/status")
async def task_queue_status() -> Dict[str, Any]:
    """Gibt Status aller persistenten Task-Queues zurück."""
    from app.core.task_queue import get_task_queue
    return get_task_queue().get_status()


@router.post("/tasks/{queue_name}/pause")
async def pause_task_queue(queue_name: str) -> Dict[str, Any]:
    """Pausiert eine Task-Queue (persistent, überlebt Neustart)."""
    from app.core.task_queue import get_task_queue
    get_task_queue().pause_queue(queue_name)
    return {"status": "paused", "queue": queue_name}


@router.post("/tasks/{queue_name}/resume")
async def resume_task_queue(queue_name: str) -> Dict[str, Any]:
    """Setzt eine pausierte Task-Queue fort."""
    from app.core.task_queue import get_task_queue
    get_task_queue().resume_queue(queue_name)
    return {"status": "resumed", "queue": queue_name}


@router.delete("/tasks/item/{task_id}")
async def cancel_bg_task(task_id: str) -> Dict[str, Any]:
    """Bricht einen wartenden oder laufenden Background-Task ab."""
    from app.core.task_queue import get_task_queue
    ok = get_task_queue().cancel_task(task_id)
    if ok:
        return {"status": "cancelled", "task_id": task_id}
    raise HTTPException(status_code=404, detail="Task nicht gefunden oder nicht pending/running")


@router.post("/tasks/item/{task_id}/retry")
async def retry_bg_task(task_id: str) -> Dict[str, Any]:
    """Setzt einen fehlgeschlagenen Task auf 'pending' zurück."""
    from app.core.task_queue import get_task_queue
    ok = get_task_queue().retry_task(task_id)
    if ok:
        return {"status": "retrying", "task_id": task_id}
    raise HTTPException(status_code=404, detail="Task nicht gefunden oder nicht failed/cancelled")


@router.post("/tasks/item/{task_id}/move")
async def move_bg_task(task_id: str, queue_name: str = "") -> Dict[str, Any]:
    """Verschiebt einen pending Task in eine andere Queue."""
    if not queue_name:
        raise HTTPException(status_code=400, detail="queue_name erforderlich")
    from app.core.task_queue import get_task_queue
    ok = get_task_queue().move_task(task_id, queue_name)
    if ok:
        return {"status": "moved", "task_id": task_id, "queue": queue_name}
    raise HTTPException(status_code=404, detail="Task nicht gefunden oder nicht 'pending'")


@router.post("/tasks/item/{task_id}/priority")
async def change_bg_task_priority(task_id: str, priority: int = 20) -> Dict[str, Any]:
    """Ändert die Priorität eines pending Tasks (niedriger = schneller)."""
    from app.core.task_queue import get_task_queue
    ok = get_task_queue().change_priority(task_id, priority)
    if ok:
        return {"status": "updated", "task_id": task_id, "priority": priority}
    raise HTTPException(status_code=404, detail="Task nicht gefunden oder nicht 'pending'")


@router.delete("/tasks/clear")
async def clear_bg_tasks(hours: float = 24.0, status: str = "",
                          queue_name: str = "") -> Dict[str, Any]:
    """Löscht alte abgeschlossene Tasks aus der Datenbank."""
    from app.core.task_queue import get_task_queue
    deleted = get_task_queue().clear_completed(older_than_hours=hours, queue_name=queue_name)
    return {"status": "ok", "deleted": deleted}


@router.post("/story-arc/generate")
async def trigger_story_arc_generate() -> Dict[str, Any]:
    """Triggert manuell eine Story-Arc-Generierung."""
    from app.core.background_queue import get_background_queue
    bq = get_background_queue()
    bq.submit("story_arc_generate", {"user_id": ""})
    return {"status": "submitted", "user_id": ""}


@router.delete("/story-arc/{arc_id}")
async def delete_story_arc(arc_id: str) -> Dict[str, Any]:
    """Löscht einen einzelnen Story Arc."""
    from app.models.story_arcs import remove_arc
    success = remove_arc(arc_id)
    if not success:
        raise HTTPException(status_code=404, detail="Arc nicht gefunden")
    return {"status": "deleted", "arc_id": arc_id}


@router.get("/story-arc/status")
async def story_arc_status() -> Dict[str, Any]:
    """Zeigt alle Story Arcs eines Users."""
    from app.models.story_arcs import get_all_arcs
    arcs = get_all_arcs()
    active = [a for a in arcs if a.get("status") == "active"]
    resolved = [a for a in arcs if a.get("status") == "resolved"]
    return {
        "total": len(arcs),
        "active": len(active),
        "resolved": len(resolved),
        "arcs": arcs,
    }
