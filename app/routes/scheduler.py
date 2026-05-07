"""
Scheduler API Routes - Zeitgesteuerte Jobs per Character
"""

import threading
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Dict, Any, Optional, List
from app.core.log import get_logger

logger = get_logger("scheduler_route")

from app.scheduler.scheduler_manager import SchedulerManager


router = APIRouter()

# Globale SchedulerManager-Instanz (wird von server.py gesetzt)
_scheduler_manager = None


def set_scheduler_manager(manager: SchedulerManager):
    """Wird von server.py aufgerufen um die Singleton-Instanz zu teilen."""
    global _scheduler_manager
    _scheduler_manager = manager


def get_scheduler_manager() -> SchedulerManager:
    """Gibt globale SchedulerManager-Instanz zurueck"""
    global _scheduler_manager
    if _scheduler_manager is None:
        # Fallback: eigene Instanz erstellen (sollte nicht passieren)
        logger.warning("Erstelle eigene SchedulerManager-Instanz")
        _scheduler_manager = SchedulerManager()
    return _scheduler_manager


class JobCreate(BaseModel):
    """Request-Modell fuer Job-Erstellung"""
    character: str = ""
    agent: str = ""  # backward compat
    trigger: Dict[str, Any]
    action: Dict[str, Any]
    job_id: Optional[str] = None
    enabled: bool = True


@router.get("/jobs")
def list_jobs(agent: Optional[str] = None, character: Optional[str] = None):
    """Listet alle Jobs auf (optional gefiltert nach Character und User)."""
    try:
        manager = get_scheduler_manager()
        char_name = character or agent
        jobs = manager.get_jobs(agent=char_name)
        return {
            "status": "success",
            "count": len(jobs),
            "data": jobs
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/jobs")
def create_job(job: JobCreate):
    """Erstellt einen neuen Job."""
    try:
        manager = get_scheduler_manager()
        char_name = job.character or job.agent
        result = manager.add_job(
            agent=char_name,
            trigger=job.trigger,
            action=job.action,
            job_id=job.job_id,
            enabled=job.enabled
        )

        if result["success"]:
            return {
                "status": "success",
                "message": result["message"],
                "job_id": result["job_id"]
            }
        else:
            raise HTTPException(status_code=400, detail=result.get("error", "Unknown error"))

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    """Loescht einen Job."""
    try:
        manager = get_scheduler_manager()
        result = manager.remove_job(job_id)

        if result["success"]:
            return {
                "status": "success",
                "message": result["message"]
            }
        else:
            raise HTTPException(status_code=404, detail=result.get("error", "Job not found"))

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/jobs/{job_id}/toggle")
def toggle_job(job_id: str):
    """Aktiviert/Deaktiviert einen Job."""
    try:
        manager = get_scheduler_manager()
        result = manager.toggle_job(job_id)

        if result["success"]:
            return {
                "status": "success",
                "message": result["message"],
                "enabled": result["enabled"]
            }
        else:
            raise HTTPException(status_code=404, detail=result.get("error", "Job not found"))

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/jobs/{job_id}/run")
def run_job_now(job_id: str):
    """Fuehrt einen Job sofort aus (im Hintergrund-Thread)."""
    try:
        manager = get_scheduler_manager()

        # Pruefen ob Job existiert
        job = None
        for j in manager.jobs_data['jobs']:
            if j['id'] == job_id:
                job = j
                break

        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} nicht gefunden")

        # Job im Hintergrund-Thread ausfuehren (blockiert nicht den Event-Loop)
        thread = threading.Thread(
            target=manager._execute_job,
            args=(job,),
            daemon=True
        )
        thread.start()

        return {
            "status": "success",
            "message": f"Job {job_id} wird im Hintergrund ausgefuehrt"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/jobs/{job_id}/logs")
def get_job_logs(job_id: str, limit: int = 100,
                 character: Optional[str] = None):
    """Gibt Logs fuer einen Job zurueck."""
    try:
        manager = get_scheduler_manager()
        logs = manager.get_job_logs(job_id, limit, character=character)
        return {
            "status": "success",
            "count": len(logs),
            "data": logs
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/logs")
def get_all_logs(limit: int = 100,
                 character: Optional[str] = None):
    """Gibt alle Job-Logs zurueck."""
    try:
        manager = get_scheduler_manager()
        logs = manager.get_job_logs(None, limit, character=character)
        return {
            "status": "success",
            "count": len(logs),
            "data": logs
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Daily Schedule (Tagesablauf) ---

class DailyScheduleSlot(BaseModel):
    hour: int
    location: str = ""
    role: str = ""
    sleep: bool = False

class DailyScheduleSave(BaseModel):
    character: str
    enabled: bool = True
    slots: List[DailyScheduleSlot]


@router.get("/daily-schedule")
def get_daily_schedule(character: str):
    """Laedt den Tagesablauf fuer einen Character."""
    try:
        from app.models.character import get_character_daily_schedule
        schedule = get_character_daily_schedule(character)
        return {"status": "success", "schedule": schedule}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/daily-schedule")
def save_daily_schedule(data: DailyScheduleSave):
    """Speichert den Tagesablauf und synchronisiert Cron-Jobs."""
    try:
        from app.models.character import save_character_daily_schedule
        manager = get_scheduler_manager()
        schedule = {
            "enabled": data.enabled,
            "slots": [s.model_dump() for s in data.slots],
        }
        save_character_daily_schedule(data.character, schedule)
        jobs_created = manager.sync_daily_schedule(data.character, schedule)
        return {"status": "success", "jobs_created": jobs_created}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/daily-schedule")
def delete_daily_schedule(character: str):
    """Loescht den Tagesablauf und entfernt alle zugehoerigen Jobs."""
    try:
        from app.models.character import delete_character_daily_schedule
        manager = get_scheduler_manager()
        manager.sync_daily_schedule(character, {"enabled": False, "slots": []})
        delete_character_daily_schedule(character)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status")
def get_scheduler_status():
    """Gibt Status des Schedulers zurueck."""
    try:
        manager = get_scheduler_manager()
        jobs = manager.get_jobs()
        active_jobs = [j for j in jobs if j.get('enabled', True)]

        return {
            "status": "running",
            "total_jobs": len(jobs),
            "active_jobs": len(active_jobs),
            "inactive_jobs": len(jobs) - len(active_jobs)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
