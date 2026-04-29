"""Event-Loop-Watchdog mit Stack-Dump waehrend des Blocks.

Zwei-Teiler:
1. asyncio-Task aktualisiert alle `tick` Sekunden einen Heartbeat-Timestamp.
2. Ein separater OS-Thread prueft periodisch wie alt der Heartbeat ist.
   Wenn der Event-Loop > threshold nicht geantwortet hat, dumpt der Thread
   die Frames aller Threads — inkl. Main-Thread mit dem aktuell blockierenden
   Call. So sehen wir den Blocker live, nicht erst nachdem er vorbei ist.
"""
import asyncio
import sys
import threading
import time
import traceback

from app.core.log import get_logger

logger = get_logger("event_loop_watchdog")

_heartbeat: float = 0.0
_asyncio_task: asyncio.Task = None
_thread: threading.Thread = None
_stop_event = threading.Event()


async def _heartbeat_loop(tick: float) -> None:
    global _heartbeat
    while True:
        _heartbeat = time.monotonic()
        try:
            await asyncio.sleep(tick)
        except asyncio.CancelledError:
            return


def _format_all_threads() -> str:
    """Formatiert Stacks aller Threads fuer das Log."""
    frames = sys._current_frames()
    out = []
    for th in threading.enumerate():
        frame = frames.get(th.ident)
        if frame is None:
            continue
        out.append(f"--- Thread {th.name!r} (id={th.ident}, daemon={th.daemon}) ---")
        out.append("".join(traceback.format_stack(frame)))
    return "\n".join(out)


def _watchdog_thread(threshold_ms: float, check_interval: float) -> None:
    """OS-Thread: prueft Heartbeat und dumpt wenn Event-Loop blockiert ist."""
    threshold_s = threshold_ms / 1000.0
    already_dumped = False
    block_started_at = 0.0
    logger.info("Watchdog-Thread aktiv (check=%.0fms, threshold=%.0fms)",
                check_interval * 1000, threshold_ms)
    while not _stop_event.is_set():
        time.sleep(check_interval)
        if _heartbeat == 0.0:
            continue
        lag = time.monotonic() - _heartbeat
        if lag >= threshold_s:
            if not already_dumped:
                block_started_at = _heartbeat
                already_dumped = True
                stacks = _format_all_threads()
                logger.warning(
                    "Event-Loop blockiert seit %.0fms — Thread-Stacks:\n%s",
                    lag * 1000, stacks)
        else:
            if already_dumped:
                total_block = time.monotonic() - block_started_at
                logger.warning(
                    "Event-Loop wieder frei nach %.0fms Block", total_block * 1000)
            already_dumped = False
    logger.info("Watchdog-Thread beendet")


def start(tick: float = 0.1, threshold_ms: float = 1000.0,
          check_interval: float = 0.2) -> None:
    global _asyncio_task, _thread, _heartbeat
    if _asyncio_task and not _asyncio_task.done():
        return
    _stop_event.clear()
    _heartbeat = time.monotonic()
    _asyncio_task = asyncio.create_task(_heartbeat_loop(tick))
    _thread = threading.Thread(
        target=_watchdog_thread,
        args=(threshold_ms, check_interval),
        name="EventLoopWatchdog",
        daemon=True)
    _thread.start()


def stop() -> None:
    global _asyncio_task, _thread
    _stop_event.set()
    if _asyncio_task and not _asyncio_task.done():
        _asyncio_task.cancel()
    _asyncio_task = None
    _thread = None
