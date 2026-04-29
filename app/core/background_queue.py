"""Background Queue — Backward-Compatible Wrapper über TaskQueue.

Bestehende Consumer können weiterhin get_background_queue().submit() nutzen.
Alle Calls werden an die persistente TaskQueue delegiert (queue='default').

Neue Verwendung: from app.core.task_queue import get_task_queue
"""
from app.core.task_queue import TaskQueue, get_task_queue
from app.core.log import get_logger

logger = get_logger("bg_queue")


class BackgroundQueue:
    """Thin wrapper — delegiert an TaskQueue (queue='default')."""

    def register_handler(self, task_type: str, handler) -> None:
        get_task_queue().register_handler(task_type, handler)

    def submit(self, task_type: str, payload: dict, deduplicate: bool = False, **kwargs) -> str:
        return get_task_queue().submit(task_type, payload, deduplicate=deduplicate, **kwargs)

    def get_status(self) -> dict:
        status = get_task_queue().get_status()
        q = status.get("queues", {}).get("default", {})
        return {
            "pending": q.get("pending_count", 0),
            "running": len(q.get("running", [])),
            "current_task": q.get("running", [{}])[0].get("task_id") if q.get("running") else None,
            "handlers": status.get("handlers", []),
        }


_background_queue = None


def get_background_queue() -> BackgroundQueue:
    """Backward-compatible singleton — use get_task_queue() for new code."""
    global _background_queue
    if _background_queue is None:
        _background_queue = BackgroundQueue()
    return _background_queue
