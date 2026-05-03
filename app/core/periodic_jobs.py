"""Periodic background jobs that used to be driven by the ThoughtLoop tick.

The old ``ThoughtLoop._loop`` ticked every 60 seconds and triggered:
    - hourly stat decay (apply_hourly_status_tick, has internal hourly gating)
    - random event generation (every 60 ticks = 60 min)
    - random event escalation (every 5 ticks = 5 min)
    - event resolution attempts (every 5 ticks, offset 2)
    - assignment expiry (every tick)
    - relationship decay (every 24h, has internal cooldown)
    - character evolution (interval from config)

With the AgentLoop replacing the periodic tick, these need their own
schedule. Each is a simple asyncio task that respects the world pause
toggle (same source as the AgentLoop).

Public API:
    start() / stop() — registered from server.py lifespan
"""
import asyncio
from datetime import datetime
from typing import List, Optional

from app.core.log import get_logger

logger = get_logger("periodic_jobs")


def _is_paused() -> bool:
    """Mirrors AgentLoop pause source — task_queue 'default' pause flag."""
    try:
        from app.core.task_queue import get_task_queue
        tq = get_task_queue()
        return bool(tq and tq._is_paused("default"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Job runners
# ---------------------------------------------------------------------------

async def _job_status_tick():
    """Apply hourly stat decay to all characters. Has internal hourly gating
    inside ``apply_hourly_status_tick`` — cheap to invoke every minute."""
    try:
        from app.core.activity_engine import apply_hourly_status_tick
        from app.models.character import list_available_characters

        def _run():
            for name in list_available_characters():
                try:
                    apply_hourly_status_tick(name)
                except Exception as e:
                    logger.debug("status_tick failed for %s: %s", name, e)

        await asyncio.to_thread(_run)
    except Exception as e:
        logger.debug("status_tick job error: %s", e)


async def _job_assignment_expiry():
    try:
        from app.models.assignments import expire_overdue
        await asyncio.to_thread(expire_overdue)
    except Exception as e:
        logger.debug("assignment_expiry job error: %s", e)


async def _job_random_events_generate():
    """Hourly random event generation across all locations."""
    try:
        from app.core.random_events import check_and_generate
        await asyncio.to_thread(check_and_generate)
    except Exception as e:
        logger.debug("random_events_generate job error: %s", e)


async def _job_random_events_escalate():
    """Escalate unanswered disruption/danger events every 5 min."""
    try:
        from app.core.random_events import check_escalation
        await asyncio.to_thread(check_escalation)
    except Exception as e:
        logger.debug("random_events_escalate job error: %s", e)


async def _job_random_events_resolve():
    """Try resolving open events with character actions (every 5 min)."""
    try:
        from app.core.random_events import try_resolve_events
        await asyncio.to_thread(try_resolve_events)
    except Exception as e:
        logger.debug("random_events_resolve job error: %s", e)


async def _job_relationship_decay():
    """Submit a relationship-decay job once per day (handler has its own
    24h cooldown so submitting more often is harmless)."""
    try:
        from app.core.background_queue import get_background_queue
        await asyncio.to_thread(
            lambda: get_background_queue().submit(
                "relationship_decay", {"user_id": ""}, deduplicate=True))
    except Exception as e:
        logger.debug("relationship_decay submit error: %s", e)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

# (job_callable, interval_seconds, initial_delay_seconds, label)
# Initial delay staggers job kickoff so they don't all fire at once.
_JOB_TABLE = [
    (_job_status_tick,             60,    20, "status_tick"),
    (_job_assignment_expiry,       60,    25, "assignment_expiry"),
    (_job_random_events_generate,  3600,  60, "random_events_generate"),
    (_job_random_events_escalate,  300,   90, "random_events_escalate"),
    (_job_random_events_resolve,   300,   210, "random_events_resolve"),
    (_job_relationship_decay,      24 * 3600, 600, "relationship_decay"),
]


_tasks: List[asyncio.Task] = []


async def _runner(job, interval: int, initial_delay: int, label: str):
    """One asyncio task per job. Sleeps initial_delay, then loops forever."""
    try:
        await asyncio.sleep(initial_delay)
    except asyncio.CancelledError:
        return

    while True:
        try:
            if not _is_paused():
                started = datetime.now()
                await job()
                duration = (datetime.now() - started).total_seconds()
                if duration > 5:
                    logger.info("periodic %s done in %.1fs", label, duration)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("periodic %s error: %s", label, e, exc_info=True)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return


def start() -> None:
    """Spawn one asyncio task per periodic job."""
    if _tasks:
        return
    for job, interval, initial_delay, label in _JOB_TABLE:
        task = asyncio.create_task(_runner(job, interval, initial_delay, label))
        _tasks.append(task)
    logger.info("Periodic jobs gestartet: %d Tasks", len(_tasks))


async def stop() -> None:
    for t in _tasks:
        t.cancel()
    for t in _tasks:
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    _tasks.clear()
    logger.info("Periodic jobs gestoppt")
