"""Main FastAPI application - Refactored modular structure"""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from app.core.log import get_logger

logger = get_logger("server")


class _SuppressHealthPolling(logging.Filter):
    """Suppress noisy polling endpoints from uvicorn access logs."""
    _SUPPRESS = {
        "/queue/status",
        "/health",
        "/notifications/unread-count",
        "/history?limit=",  # Chat-History polling vom Frontend
    }

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(ep in msg for ep in self._SUPPRESS)


logging.getLogger("uvicorn.access").addFilter(_SuppressHealthPolling())
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path

# Initialize storage paths first (CLI --storage / --world / STORAGE_DIR env)
from app.core import paths as _paths
_paths.init()

# Load JSON config from the (now-known) storage directory
from app.core.config import load as _load_config
_load_config(_paths.get_config_path())

# Welt-DB initialisieren (idempotent, legt world.db an falls noetig)
from app.core.db import init_schema as _init_db_schema
_init_db_schema()

# Einmalige Migration: alte status_modifiers.json -> prompt_filters-Tabelle.
# Idempotent: nur wenn die Datei existiert + Eintraege noch nicht in der DB
# stehen. Datei wird danach in *.migrated umbenannt.
try:
    from app.core.prompt_filters import migrate_status_modifiers_once
    migrate_status_modifiers_once()
except Exception:
    pass

# Import routers
from app.routes import auth, store, characters, chat, group_chat, scheduler, instagram, world, telegram, templates, story, story_dev, world_dev, tts, queue as queue_route, logs, admin, notifications, dashboard, events, relationships, assignments, diary
from app.routes import admin_settings
from app.routes import user_gallery
from app.routes import secrets
from app.routes import inventory
from app.routes import account
from app.routes import activities as activities_route
from app.routes import i18n as i18n_route
from app.routes import state as state_route
from app.routes import game_admin as game_admin_route
from app.routes import world_setup as world_setup_route
from app.scheduler.scheduler_manager import SchedulerManager
from app.core.dependencies import initialize_channels, get_skill_manager
from app.core.provider_manager import initialize_provider_manager
from app.core.tts_service import initialize_tts_service, clear_tts_tmp

# Global Scheduler Instance
_scheduler_manager = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle Manager für Server-Start und Shutdown"""
    global _scheduler_manager

    # Startup
    # Temporaere Dateien loeschen
    from app.routes.story import clear_story_tmp
    clear_story_tmp()
    clear_tts_tmp()

    # Log-Retention: alte Eintraege aus llm_calls.jsonl + image_prompts.jsonl
    # entfernen (Default 5 Tage, konfigurierbar via server.log_retention_days
    # in der Admin-Config). Verhindert Wachstum ins Unendliche.
    try:
        from app.utils.llm_logger import prune_logs_on_startup
        _log_pruned = prune_logs_on_startup()
        if _log_pruned.get("llm_calls") or _log_pruned.get("image_prompts"):
            logger.info("Log-Retention: %s", _log_pruned)
    except Exception as _le:
        logger.warning("Log-Retention beim Start fehlgeschlagen: %s", _le)

    # Multiuser: Default-Admin bootstrappen falls noch kein User existiert
    from app.core.users import ensure_default_admin
    ensure_default_admin()

    # Migration: Persistente Location-IDs hinzufuegen, Filesystem bereinigen
    from app.models.world import migrate_location_ids
    migrate_location_ids()

    # Klon-Hygiene: off-map / Duplikate / Waisen entfernen.
    from app.models.world import cleanup_orphan_clones
    _cleanup_stats = cleanup_orphan_clones()
    if _cleanup_stats.get("removed"):
        logger.info("Klon-Cleanup beim Start: %s", _cleanup_stats)

    # Migration: Variant-Dateinamen mit Character-Name prefixen
    from app.core.expression_regen import migrate_variant_filenames
    migrate_variant_filenames()

    # Initialisiere Multi-Channel Support
    logger.info("Initialisiere Multi-Channel Support...")
    initialize_channels()

    logger.info("Initializing Providers...")
    provider_manager = initialize_provider_manager()

    logger.info("Initializing LLM Routing...")
    from app.core import config as _cfg
    _routing = _cfg.get("llm_routing", []) or []
    logger.info("llm_routing: %d Einträge", len(_routing))

    # Modelle mit preload_on_startup=True asynchron warm laden, damit
    # llama-swap & Co. das Model schon in den Speicher legen, bevor der
    # erste echte User-Request kommt. create_task = nicht blockierend.
    try:
        import asyncio as _asyncio
        from app.core.llm_router import preload_models as _preload_models
        _asyncio.create_task(_preload_models())
    except Exception as _pe:
        logger.warning("LLM-Preload Task konnte nicht gestartet werden: %s", _pe)

    logger.info("Initialisiere Skills (Image Backends, etc.)...")
    skill_manager = get_skill_manager()

    logger.info("Registriere Task-Queue Handler...")
    from app.core.social_reactions import register_social_reaction_handler
    register_social_reaction_handler()
    from app.core.social_dialog import register_social_dialog_handler
    register_social_dialog_handler()
    from app.core.story_engine import register_story_engine_handler
    register_story_engine_handler()
    from app.core.relationship_summary import register_relationship_summary_handler
    register_relationship_summary_handler()
    from app.core.relationship_decay import register_relationship_decay_handler
    register_relationship_decay_handler()
    from app.core.intent_engine import register_intent_handlers
    register_intent_handlers()
    from app.core.memory_service import register_consolidation_handler, register_migration_handler
    register_consolidation_handler()
    register_migration_handler()

    logger.info("Initializing TTS Service...")
    tts_service = initialize_tts_service()

    # ComfyUI Model-/LoRA-Cache beim Start laden
    imagegen = skill_manager.get_skill("image_generation")
    if imagegen and hasattr(imagegen, "load_comfyui_model_cache"):
        logger.info("Lade ComfyUI Model-/LoRA-Cache...")
        imagegen.load_comfyui_model_cache()

    logger.info("Checking Face Service...")
    from app.skills.face_client import is_available as face_is_available
    face_available = face_is_available()

    # rembg/u2net im Hintergrund vorladen — verhindert ~5s Event-Loop-Block
    # beim ersten Outfit-Postprocessing-Request.
    try:
        from app.models.character import preload_rembg_session
        preload_rembg_session()
    except Exception as _rembg_err:
        logger.warning("rembg-Preload nicht gestartet: %s", _rembg_err)

    # ── Startup Availability Summary ──
    import os as _os
    _summary_lines = ["-" * 80, "AVAILABILITY SUMMARY", "-" * 80]
    for prov in provider_manager.providers.values():
        status = "OK" if prov.available else "FAIL"
        vram = f", vram={prov.vram_mb}MB" if prov.vram_mb else ""
        _summary_lines.append(
            f"  Prov  {status:4s}  {prov.name} "
            f"({prov.type}, concurrent={prov.max_concurrent}{vram})")
    if not provider_manager.providers:
        _summary_lines.append("  Prov  --    No providers configured")
    for _entry in _routing:
        _ts = ", ".join(f"{t.get('task')}:{t.get('order')}" for t in (_entry.get("tasks") or []))
        _summary_lines.append(
            f"  LLM   OK    {_entry.get('provider','?')} / {_entry.get('model','?')} -> {_ts}")
    if not _routing:
        _summary_lines.append("  LLM   --    No routing entries configured")
    for skill in skill_manager.skills:
        _summary_lines.append(f"  Skill OK    {skill.name}")
    if not skill_manager.skills:
        _summary_lines.append("  Skill --    No skills loaded")
    from app.skills.image_backends import get_active_comfyui_url as _get_comfyui_url
    active_comfyui_url = _get_comfyui_url()
    face_url = _os.environ.get("FACE_SERVICE_URL", "http://localhost:8005")
    face_status = "OK" if face_available else "FAIL"
    _summary_lines.append(f"  Enhnc {face_status:4s}  GFPGAN Face Service ({face_url})")
    tts_info = tts_service.status_info()
    if tts_info["enabled"]:
        tts_status = "OK" if tts_info["available"] else "FAIL"
        _summary_lines.append(
            f"  TTS   {tts_status:4s}  {tts_info['backend'].upper()} "
            f"({tts_info['url']}, voice={tts_info['voice']})")
    else:
        _summary_lines.append(f"  TTS   --    Disabled")
    _summary_lines.append(f"  Tele  OK    Telegram Channel (per-agent bot tokens)")
    from app.core.beszel import check_status as _beszel_check_status
    _beszel = _beszel_check_status()
    if not _beszel["configured"]:
        _summary_lines.append(f"  Beszl --    Not configured")
    elif _beszel["ok"]:
        _summary_lines.append(f"  Beszl OK    GPU Monitoring ({_beszel['url']})")
    else:
        _summary_lines.append(
            f"  Beszl FAIL  GPU Monitoring ({_beszel['url']}) — {_beszel['error']}")
    _summary_lines.append("-" * 80)
    logger.info("\n%s", "\n".join(_summary_lines))

    # Character-Validierung (LLM-Overrides, etc.)
    logger.info("Validiere Character-Konfigurationen...")
    from app.core.character_validation import validate_all_characters
    validate_all_characters()

    logger.info("Initialisiere Scheduler...")
    _scheduler_manager = SchedulerManager()
    from app.routes.scheduler import set_scheduler_manager
    set_scheduler_manager(_scheduler_manager)
    logger.info("Scheduler bereit!")

    # Telegram Long Polling starten
    from app.core.telegram_polling import get_polling_manager
    _telegram_polling = get_polling_manager()
    await _telegram_polling.start()

    # Gedanken-Container instanziieren — kein Background-Task mehr,
    # nur Zugriffsobjekt fuer ``run_thought_turn``. AgentLoop ruft die
    # Funktion ueber ``get_thought_runner()``.
    from app.core.thoughts import ThoughtRunner, set_thought_runner
    _thought_runner = ThoughtRunner()
    set_thought_runner(_thought_runner)
    logger.info("ThoughtRunner initialisiert")

    # AgentLoop starten — kontinuierliche Gedanken-Schleife mit
    # importance-gewichtetem Round-Robin. Ersetzt den alten periodischen
    # Tick. Pause haengt am world-pause-Toggle (task_queue 'default').
    from app.core.agent_loop import get_agent_loop
    _agent_loop = get_agent_loop()
    await _agent_loop.start()
    logger.info("AgentLoop bereit!")

    # Task-Queue Worker erst starten, wenn ALLE Handler registriert sind
    # (sonst schlagen recovered persistierte Tasks beim Recovery fehl).
    from app.core.task_queue import get_task_queue
    get_task_queue().start()
    logger.info("Task-Queue Worker gestartet")

    # Chat-Task-Manager: Cleanup-Loop starten
    from app.core.chat_task_manager import get_chat_task_manager
    get_chat_task_manager().start_cleanup_loop()
    logger.info("ChatTaskManager bereit!")

    # Memory-System: Knowledge -> Memory Migration
    logger.info("Memory-System: Migration pruefen...")
    from app.core.memory_service import run_migration_for_all_users
    run_migration_for_all_users()
    logger.info("Memory-System bereit!")

    # Romantic Interests: aus Character-Profilen extrahieren (einmalig per LLM)
    logger.info("Romantic Interests: Extraktion pruefen...")
    from app.models.relationship import extract_romantic_interests
    extract_romantic_interests()
    logger.info("Romantic Interests bereit!")

    # Memory-Konsolidierung: periodisch im Hintergrund
    import asyncio as _aio

    async def _periodic_consolidation():
        """Konsolidiert Memories alle 6h, unabhaengig von Server-Neustarts."""
        from app.core.paths import get_storage_dir as _get_sd
        _ts_file = _get_sd() / ".last_consolidation"

        def _hours_since_last() -> float:
            if not _ts_file.exists():
                return 999.0
            try:
                from datetime import datetime as _dt
                last = _dt.fromisoformat(_ts_file.read_text().strip())
                return (_dt.now() - last).total_seconds() / 3600
            except Exception:
                return 999.0

        def _mark_done():
            from datetime import datetime as _dt
            _ts_file.parent.mkdir(parents=True, exist_ok=True)
            _ts_file.write_text(_dt.now().isoformat())

        await _aio.sleep(60)  # Kurz warten bis Server bereit
        while True:
            hours = _hours_since_last()
            if hours >= 6:
                try:
                    from app.core.memory_service import run_consolidation_for_all_users
                    run_consolidation_for_all_users()
                    _mark_done()
                except Exception as ce:
                    logger.error("Memory consolidation error: %s", ce)
            # Alle 30 Min pruefen ob 6h vergangen
            await _aio.sleep(30 * 60)

    _consolidation_task = _aio.create_task(_periodic_consolidation())

    # Periodic background jobs (replace old ThoughtRunner tick).
    from app.core import periodic_jobs
    periodic_jobs.start()

    from app.core import channel_health
    channel_health.start()

    from app.core import event_loop_watchdog
    event_loop_watchdog.start(tick=0.1, threshold_ms=1000.0)

    yield

    event_loop_watchdog.stop()
    _consolidation_task.cancel()
    try:
        from app.core import periodic_jobs
        await periodic_jobs.stop()
    except Exception as _pe:
        logger.debug("periodic_jobs stop failed: %s", _pe)

    # Shutdown
    await _telegram_polling.stop()
    try:
        from app.core.agent_loop import get_agent_loop
        await get_agent_loop().stop()
    except Exception as _ae:
        logger.debug("AgentLoop stop failed: %s", _ae)
    # ThoughtRunner hat keinen Background-Task mehr → kein stop() noetig
    if _scheduler_manager:
        logger.info("Fahre Scheduler herunter...")
        _scheduler_manager.shutdown()


# Initialize FastAPI app
app = FastAPI(title="Agent System API", version="2.0", lifespan=lifespan)

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Erlaubt alle Domains (nur für Entwicklung!)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"])

# User-Context-Middleware: setzt current_user_ctx aus Session-Cookie pro Request
from app.core.auth_dependency import user_context_middleware
app.middleware("http")(user_context_middleware)


# Include routers
app.include_router(auth.router)
app.include_router(store.router)
app.include_router(characters.router)
app.include_router(chat.router)
app.include_router(group_chat.router, tags=["group_chat"])
app.include_router(scheduler.router, prefix="/scheduler", tags=["scheduler"])
app.include_router(instagram.router, tags=["instagram"])
app.include_router(world.router, tags=["world"])
app.include_router(telegram.router, tags=["telegram"])
app.include_router(templates.router)
app.include_router(story.router)
app.include_router(story_dev.router)
app.include_router(world_dev.router)
app.include_router(tts.router)
app.include_router(queue_route.router)
app.include_router(logs.router)
app.include_router(dashboard.router)
app.include_router(admin.router)
app.include_router(admin_settings.router)
app.include_router(notifications.router, tags=["notifications"])
app.include_router(events.router, tags=["events"])
from app.routes import rules
app.include_router(rules.router, tags=["rules"])
app.include_router(relationships.router, tags=["relationships"])
app.include_router(assignments.router, tags=["assignments"])
app.include_router(diary.router, tags=["diary"])
app.include_router(user_gallery.router)
app.include_router(secrets.router, tags=["secrets"])
app.include_router(inventory.router, tags=["inventory"])
app.include_router(activities_route.router, tags=["activities"])
app.include_router(i18n_route.router, tags=["i18n"])
app.include_router(account.router)
app.include_router(state_route.router)
app.include_router(game_admin_route.router)
app.include_router(world_setup_route.router)

# Static files & templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Serve the main HTML page"""
    return templates.TemplateResponse(request, "index.html")


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "version": "2.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
