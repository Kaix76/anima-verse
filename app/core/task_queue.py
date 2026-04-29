"""Persistent Task Queue — SQLite-backed, multi-queue, prioritized, pausierbar.

Ersetzt BackgroundQueue mit persistenter, restartfähiger Queue.

Features:
  - Persistent via SQLite (kein Task-Verlust bei Neustart)
  - Mehrere named Queues (z.B. "default", "GamingPC", "EvoX2")
  - Prioritäten (niedriger Wert = höhere Priorität)
  - Pause/Resume pro Queue (überlebt Neustart)
  - Tasks verschiebbar (Queue, Priorität)
  - Auto-Retry konfigurierbar
  - Tracked Tasks: extern laufende Tasks (GPU, TTS) werden fuer
    einheitliche UI-Sichtbarkeit in derselben DB registriert
  - CLI: python queue_cli.py <command>

Config (.env):
    TASK_QUEUE_QUEUES=default:1
    TASK_QUEUE_MAX_RETRIES=0
"""
import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from app.core.log import get_logger

logger = get_logger("task_queue")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
from app.core.paths import get_storage_dir as _get_storage_dir
def _get_db_path() -> Path:
    return _get_storage_dir() / "task_queue.db"
MAX_RETRIES_DEFAULT = int(os.environ.get("TASK_QUEUE_MAX_RETRIES", "0"))
_NUM_WORKERS = 2  # Fixed background worker pool size


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_SCHEMA_STMTS = [
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    """CREATE TABLE IF NOT EXISTS tasks (
        task_id      TEXT    PRIMARY KEY,
        queue_name   TEXT    NOT NULL DEFAULT 'default',
        task_type    TEXT    NOT NULL,
        priority     INTEGER NOT NULL DEFAULT 20,
        status       TEXT    NOT NULL DEFAULT 'pending',
        payload      TEXT    NOT NULL DEFAULT '{}',
        result       TEXT    DEFAULT NULL,
        error        TEXT    DEFAULT '',
        created_at   TEXT    NOT NULL,
        started_at   TEXT    DEFAULT NULL,
        completed_at TEXT    DEFAULT NULL,
        duration_s   REAL    DEFAULT 0,
        agent_name   TEXT    DEFAULT '',
        user_id      TEXT    DEFAULT '',
        retry_count  INTEGER DEFAULT 0,
        max_retries  INTEGER DEFAULT 0
    )""",
    """CREATE INDEX IF NOT EXISTS idx_tasks_pending
       ON tasks (queue_name, priority, created_at)
       WHERE status = 'pending'""",
    """CREATE INDEX IF NOT EXISTS idx_tasks_status
       ON tasks (status, completed_at)""",
    """CREATE TABLE IF NOT EXISTS queue_paused (
        queue_name TEXT PRIMARY KEY,
        paused     INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT    NOT NULL
    )""",
]


# ---------------------------------------------------------------------------
# TaskQueue
# ---------------------------------------------------------------------------
class TaskQueue:
    """Persistent, multi-queue task processor backed by SQLite.

    Drop-in replacement for BackgroundQueue with the same submit() /
    register_handler() API plus management operations (pause, retry, move, …).
    """

    def __init__(self) -> None:
        self._db_path: Path = _get_db_path()
        self._handlers: Dict[str, Callable] = {}
        self._write_lock = threading.Lock()
        self._wake_event = threading.Event()
        self._workers: List[threading.Thread] = []
        self._started = False
        self._stopped = False
        self._track_start_times: Dict[str, float] = {}  # task_id → monotonic

        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._reset_stale_running()

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            for stmt in _SCHEMA_STMTS:
                conn.execute(stmt)
            # Migrations — neue Spalten fuer tracked tasks
            for col, typedef in [
                ("label", "TEXT DEFAULT ''"),
                ("provider", "TEXT DEFAULT ''"),
                ("task_origin", "TEXT DEFAULT 'queued'"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typedef}")
                except sqlite3.OperationalError:
                    pass  # Spalte existiert bereits
            conn.commit()
        logger.info("DB initialisiert: %s", self._db_path)

    def _reset_stale_running(self) -> None:
        """On startup: stale running tasks aufräumen.

        Queued tasks → pending (haben Handler, werden erneut verarbeitet).
        Tracked tasks → interrupted (extern gesteuert, kein Retry moeglich).
        """
        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock, self._connect() as conn:
            # Queued tasks: zurueck auf pending
            c1 = conn.execute(
                "UPDATE tasks SET status='pending', started_at=NULL, error='Server-Neustart'"
                " WHERE status='running' AND (task_origin='queued' OR task_origin IS NULL)"
            )
            # Tracked tasks: als interrupted markieren
            c2 = conn.execute(
                "UPDATE tasks SET status='interrupted', completed_at=?, error='Server-Neustart'"
                " WHERE status='running' AND task_origin='tracked'",
                (now,))
            conn.commit()
        if c1.rowcount:
            logger.warning("Stale queued Tasks wiederhergestellt: %d", c1.rowcount)
        if c2.rowcount:
            logger.warning("Stale tracked Tasks als interrupted markiert: %d", c2.rowcount)

    # ------------------------------------------------------------------
    # Public API — compatible with BackgroundQueue
    # ------------------------------------------------------------------

    def register_handler(self, task_type: str, handler: Callable) -> None:
        """Registers a handler function for a task type."""
        self._handlers[task_type] = handler
        logger.info("Handler registriert: %s", task_type)

    def submit(
        self,
        task_type: str,
        payload: Dict[str, Any],
        queue_name: str = "",
        priority: int = 20,
        agent_name: str = "", max_retries: int = -1,
        deduplicate: bool = False) -> str:
        """Persist a task and wake a worker. Returns task_id.

        queue_name: informational label (for UI display), no routing effect.
        deduplicate: if True, skip submission when a task with the same
                     task_type is already pending or running.
                     Returns empty string if skipped.
        """
        if max_retries < 0:
            max_retries = MAX_RETRIES_DEFAULT

        if not queue_name:
            queue_name = "background"

        task_id = f"task_{uuid.uuid4().hex[:12]}"
        now = datetime.now().isoformat(timespec="seconds")

        with self._write_lock, self._connect() as conn:
            # Dedup check: skip if same task_type (+ same agent if specified)
            # already pending/running. Ohne agent_name-Match wuerde z.B.
            # `submit_consolidation_for_all` nach dem ersten Char-Submit alle
            # weiteren Characters als Duplikat verwerfen.
            if deduplicate:
                if agent_name:
                    existing = conn.execute(
                        "SELECT task_id FROM tasks WHERE task_type=? AND agent_name=? AND status IN ('pending','running') LIMIT 1",
                        (task_type, agent_name)).fetchone()
                else:
                    existing = conn.execute(
                        "SELECT task_id FROM tasks WHERE task_type=? AND status IN ('pending','running') LIMIT 1",
                        (task_type,)).fetchone()
                if existing:
                    logger.debug(
                        "Dedupliziert: %s/%s laeuft bereits (%s), uebersprungen",
                        task_type, agent_name or "*", existing["task_id"])
                    return ""

            conn.execute(
                """INSERT INTO tasks
                   (task_id, queue_name, task_type, priority, status, payload,
                    created_at, agent_name, max_retries)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                (
                    task_id, queue_name, task_type, priority,
                    json.dumps(payload, ensure_ascii=False),
                    now, agent_name, max_retries))
            conn.commit()

        logger.info(
            "Task eingereicht: %s (%s) prio=%d",
            task_id, task_type, priority)
        self._wake_event.set()

        # Auto-start workers on first submit (backward compat)
        if not self._started:
            self.start()

        return task_id

    def start(self) -> None:
        """Start background worker threads.

        Called at server startup after all handlers are registered.
        """
        with self._write_lock:
            if self._started:
                return
            self._started = True

        self._recover_stale_running()
        self._ensure_workers()

        # Wake workers if there are pending tasks from previous session
        if self._has_pending():
            self._wake_event.set()
        logger.info("TaskQueue gestartet: %d workers", _NUM_WORKERS)

    def _recover_stale_running(self) -> None:
        """Reset tasks stuck in 'running' from a previous server session."""
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                """UPDATE tasks SET status='pending', started_at=NULL,
                          error='Server-Neustart'
                   WHERE status='running' AND task_origin='queued'"""
            )
            if cur.rowcount:
                conn.commit()
                logger.warning(
                    "Startup-Cleanup: %d haengende Tasks auf 'pending' zurueckgesetzt",
                    cur.rowcount)

    def _has_pending(self) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM tasks WHERE status='pending' AND (task_origin='queued' OR task_origin IS NULL) LIMIT 1"
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Management API
    # ------------------------------------------------------------------

    def pause_queue(self, queue_name: str) -> bool:
        """Pauses a queue (persisted — survives restart). Returns True."""
        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO queue_paused (queue_name, paused, updated_at)
                   VALUES (?, 1, ?)
                   ON CONFLICT(queue_name) DO UPDATE SET paused=1, updated_at=?""",
                (queue_name, now, now))
            conn.commit()
        logger.info("Queue pausiert: %s", queue_name)
        return True

    def resume_queue(self, queue_name: str) -> bool:
        """Resumes a paused queue. Starts workers if not running."""
        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO queue_paused (queue_name, paused, updated_at)
                   VALUES (?, 0, ?)
                   ON CONFLICT(queue_name) DO UPDATE SET paused=0, updated_at=?""",
                (queue_name, now, now))
            conn.commit()
        self._wake_event.set()
        logger.info("Queue fortgesetzt: %s", queue_name)
        return True

    def cancel_task(self, task_id: str) -> bool:
        """Cancels a pending or running task. Returns True if cancelled."""
        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                """UPDATE tasks SET status='cancelled', completed_at=?, error='Abgebrochen'
                   WHERE task_id=? AND status IN ('pending', 'running')""",
                (now, task_id))
            conn.commit()
        if cur.rowcount:
            logger.info("Task abgebrochen: %s", task_id)
            return True
        logger.warning("Task nicht gefunden oder nicht pending/running: %s", task_id)
        return False

    def retry_task(self, task_id: str) -> bool:
        """Resets a failed/cancelled task to pending for retry."""
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT queue_name FROM tasks WHERE task_id=? AND status IN ('failed','cancelled')",
                (task_id,)).fetchone()
            if not row:
                return False
            conn.execute(
                """UPDATE tasks
                   SET status='pending', error='', result=NULL,
                       started_at=NULL, completed_at=NULL, duration_s=0
                   WHERE task_id=?""",
                (task_id))
            conn.commit()
        self._wake_event.set()
        logger.info("Task wird wiederholt: %s", task_id)
        return True

    def move_task(self, task_id: str, new_queue: str) -> bool:
        """Moves a pending task to a different queue."""
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE tasks SET queue_name=? WHERE task_id=? AND status='pending'",
                (new_queue, task_id))
            conn.commit()
        if cur.rowcount:
            self._wake_event.set()
            logger.info("Task verschoben: %s → %s", task_id, new_queue)
            return True
        return False

    def change_priority(self, task_id: str, new_priority: int) -> bool:
        """Changes the priority of a pending task."""
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE tasks SET priority=? WHERE task_id=? AND status='pending'",
                (new_priority, task_id))
            conn.commit()
        if cur.rowcount:
            logger.info("Task-Priorität: %s → %d", task_id, new_priority)
            return True
        return False

    def clear_completed(self, older_than_hours: float = 24, queue_name: str = "") -> int:
        """Deletes completed/failed/cancelled tasks older than N hours."""
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(hours=older_than_hours)).isoformat(timespec="seconds")
        with self._write_lock, self._connect() as conn:
            if queue_name:
                cur = conn.execute(
                    """DELETE FROM tasks
                       WHERE status IN ('completed','failed','cancelled','interrupted')
                       AND completed_at < ? AND queue_name=?""",
                    (cutoff, queue_name))
            else:
                cur = conn.execute(
                    """DELETE FROM tasks
                       WHERE status IN ('completed','failed','cancelled','interrupted')
                       AND completed_at < ?""",
                    (cutoff))
            conn.commit()
        logger.info("Bereinigt: %d Tasks", cur.rowcount)
        return cur.rowcount

    def get_status(self) -> Dict[str, Any]:
        """Returns status for background tasks (queued + tracked)."""
        conn = self._connect()
        try:
            pending = [
                dict(r) for r in conn.execute(
                    """SELECT task_id, task_type, priority, created_at, queue_name,
                              agent_name, label, provider, task_origin
                       FROM tasks WHERE status='pending'
                       ORDER BY priority, created_at LIMIT 50""").fetchall()
            ]
            running_rows = conn.execute(
                """SELECT task_id, task_type, priority, started_at, queue_name,
                          agent_name, label, provider, task_origin
                   FROM tasks WHERE status='running'""").fetchall()
            running = []
            for r in running_rows:
                d = dict(r)
                if d.get("task_origin") == "tracked":
                    t0 = self._track_start_times.get(d["task_id"])
                    d["duration_s"] = round(time.monotonic() - t0, 2) if t0 else 0.0
                running.append(d)
            recent = [
                dict(r) for r in conn.execute(
                    """SELECT task_id, task_type, status, duration_s,
                              completed_at, agent_name, error, label, provider, task_origin
                       FROM tasks
                       WHERE status IN ('completed','failed','cancelled','interrupted')
                       ORDER BY completed_at DESC LIMIT 30""").fetchall()
            ]

            return {
                "workers": _NUM_WORKERS,
                "pending_count": len(pending),
                "pending": pending,
                "running": running,
                "recent": recent,
                "handlers": sorted(self._handlers.keys()),
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def is_task_cancelled(self, task_id: str) -> bool:
        """Check if a task has been cancelled (for long-running handlers)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            return bool(row and row["status"] == "cancelled")
        finally:
            conn.close()

    def _ensure_workers(self) -> None:
        alive = [t for t in self._workers if t.is_alive()]
        for i in range(_NUM_WORKERS - len(alive)):
            t = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name=f"TaskQueue-worker-{len(alive) + i}")
            alive.append(t)
            t.start()
            logger.debug("Worker gestartet: %s", t.name)
        self._workers = alive

    def _is_paused(self, queue_name: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT paused FROM queue_paused WHERE queue_name=?",
                (queue_name,)).fetchone()
            return bool(row and row["paused"])
        finally:
            conn.close()

    def _dequeue(self) -> Optional[Dict[str, Any]]:
        """Atomically picks the next pending queued task (not tracked)."""
        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                """SELECT task_id, task_type, payload, priority, queue_name,
                          agent_name, retry_count, max_retries
                   FROM tasks
                   WHERE status='pending'
                   AND (task_origin='queued' OR task_origin IS NULL)
                   ORDER BY priority ASC, created_at ASC
                   LIMIT 1""").fetchone()
            if not row:
                return None
            # Skip if queue is paused
            if self._is_paused(row["queue_name"]):
                return None
            conn.execute(
                "UPDATE tasks SET status='running', started_at=? WHERE task_id=?",
                (now, row["task_id"]))
            conn.commit()
        return dict(row)

    def _finish_task(
        self,
        task_id: str,
        status: str,
        result: Any = None,
        error: str = "",
        duration_s: float = 0.0,
        retry: bool = False) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock, self._connect() as conn:
            if retry:
                conn.execute(
                    """UPDATE tasks
                       SET status='pending', started_at=NULL,
                           error=?, retry_count=retry_count+1
                       WHERE task_id=?""",
                    (error, task_id))
            else:
                result_json = (
                    json.dumps(result, ensure_ascii=False, default=str)
                    if result is not None else None
                )
                conn.execute(
                    """UPDATE tasks
                       SET status=?, completed_at=?, duration_s=?, result=?, error=?
                       WHERE task_id=?""",
                    (status, now, round(duration_s, 3), result_json, error, task_id))
            conn.commit()

    # ------------------------------------------------------------------
    # Tracked Tasks — extern laufende Tasks in DB registrieren
    # ------------------------------------------------------------------

    def track_start(
        self,
        task_type: str,
        label: str,
        agent_name: str = "",
        provider: str = "",
        queue_name: str = "",
        start_running: bool = True) -> str:
        """Register an externally-running task for unified UI visibility.

        Used for GPU tasks, TTS, image generation etc. that are executed
        outside of TaskQueue workers but should appear in the unified UI.

        Args:
            start_running: If True (default), task starts as 'running' with
                timer immediately. If False, starts as 'pending' — call
                track_activate() later to start the timer when actual
                processing begins.

        Returns task_id.
        """
        task_id = f"track_{uuid.uuid4().hex[:8]}"
        now = datetime.now().isoformat(timespec="seconds")
        if not queue_name:
            # Resolve provider/backend name to a configured queue name
            try:
                from app.core.task_router import match_queue_name
                queue_name = match_queue_name(provider or "") or "default"
            except Exception:
                queue_name = provider or "default"

        if start_running:
            status = "running"
            started_at = now
        else:
            status = "pending"
            started_at = None

        with self._write_lock:
            if start_running:
                self._track_start_times[task_id] = time.monotonic()
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO tasks
                       (task_id, queue_name, task_type, priority, status, payload,
                        created_at, started_at, agent_name, label, provider, task_origin)
                       VALUES (?, ?, ?, 10, ?, '{}', ?, ?, ?, ?, ?, 'tracked')""",
                    (task_id, queue_name, task_type, status, now, started_at,
                     agent_name, label, provider))
                conn.commit()

        logger.info(
            "Track start: %s (%s) status=%s label=%s agent=%s provider=%s queue=%s",
            task_id, task_type, status, label, agent_name, provider, queue_name)
        return task_id

    def track_activate(self, task_id: str, queue_name: str = "", provider: str = "") -> None:
        """Transition a pending tracked task to 'running' and start the timer.

        Called when the actual processing (e.g. GPU work) begins, so that
        the elapsed time reflects real processing time, not queue wait time.

        Args:
            queue_name: If set, also moves the task to the correct queue
                (e.g. when the GPU provider is only known at execution time).
            provider: If set, updates the provider field (e.g. when the
                backend is only known at execution time).
        """
        now = datetime.now().isoformat(timespec="seconds")
        with self._write_lock:
            self._track_start_times[task_id] = time.monotonic()
            with self._connect() as conn:
                # Build dynamic SET clause
                set_parts = ["status='running'", "started_at=?"]
                params: list = [now]
                if queue_name:
                    set_parts.append("queue_name=?")
                    params.append(queue_name)
                if provider:
                    set_parts.append("provider=?")
                    params.append(provider)
                params.extend([task_id])
                conn.execute(
                    f"UPDATE tasks SET {', '.join(set_parts)} "
                    "WHERE task_id=? AND status='pending' AND task_origin='tracked'",
                    params)
                conn.commit()
        logger.debug("Track activate: %s queue=%s provider=%s", task_id, queue_name or "(unchanged)", provider or "(unchanged)")

    def track_update_label(self, task_id: str, label: str) -> None:
        """Update the label of a tracked task (sub-step visibility)."""
        with self._write_lock, self._connect() as conn:
            conn.execute(
                "UPDATE tasks SET label=? WHERE task_id=?",
                (label, task_id))
            conn.commit()

    def track_finish(self, task_id: str, error: str = "") -> None:
        """Mark a tracked task as completed or failed."""
        t0 = self._track_start_times.pop(task_id, None)
        duration_s = round(time.monotonic() - t0, 2) if t0 else 0.0
        status = "failed" if error else "completed"
        now = datetime.now().isoformat(timespec="seconds")

        with self._write_lock, self._connect() as conn:
            conn.execute(
                """UPDATE tasks
                   SET status=?, completed_at=?, duration_s=?, error=?
                   WHERE task_id=?""",
                (status, now, duration_s, error, task_id))
            conn.commit()

        log_fn = logger.warning if error else logger.info
        log_fn(
            "Track %s: %s (%.1fs)%s",
            status, task_id, duration_s, f" error={error}" if error else "")

        # VRAM auto-free wenn keine tracked tasks mehr laufen
        if not self._track_start_times:
            _auto_free_comfyui_vram()

    def track_cancel(self, task_id: str) -> bool:
        """Cancel a tracked task. Returns True if found."""
        t0 = self._track_start_times.pop(task_id, None)
        duration_s = round(time.monotonic() - t0, 2) if t0 else 0.0
        now = datetime.now().isoformat(timespec="seconds")

        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                """UPDATE tasks
                   SET status='cancelled', completed_at=?, duration_s=?,
                       error='Manuell abgebrochen'
                   WHERE task_id=? AND status IN ('running', 'pending') AND task_origin='tracked'""",
                (now, duration_s, task_id))
            conn.commit()

        if cur.rowcount:
            logger.info("Track cancelled: %s", task_id)
            return True
        return False

    def get_tracked_active(self) -> List[Dict[str, Any]]:
        """Returns currently running and pending tracked tasks (for UI)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT task_id, task_type, label, agent_name, status,
                          created_at, provider, started_at
                   FROM tasks
                   WHERE status IN ('running', 'pending') AND task_origin='tracked'
                   ORDER BY created_at ASC"""
            ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                # Berechne live elapsed time (nur fuer running tasks mit Timer)
                t0 = self._track_start_times.get(d["task_id"])
                d["duration_s"] = round(time.monotonic() - t0, 2) if t0 else 0.0
                if d.get("provider"):
                    d["provider_name"] = d["provider"]
                result.append(d)
            return result
        finally:
            conn.close()

    def get_tracked_recent(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Returns recently completed tracked tasks (for UI compatibility)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT task_id, task_type, label, agent_name, status,
                          created_at, duration_s, error, provider
                   FROM tasks
                   WHERE task_origin='tracked'
                   AND status IN ('completed','failed','interrupted','cancelled')
                   ORDER BY completed_at DESC LIMIT ?""",
                (limit,)).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if d.get("provider"):
                    d["provider_name"] = d["provider"]
                result.append(d)
            return result
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Internal — worker loop
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        """Continuous worker — picks and executes background tasks."""
        logger.info("Worker aktiv: %s", threading.current_thread().name)

        while not self._stopped:
            task = self._dequeue()
            if task is None:
                self._wake_event.wait(timeout=10)
                self._wake_event.clear()
                continue

            task_id = task["task_id"]
            task_type = task["task_type"]
            handler = self._handlers.get(task_type)

            if not handler:
                logger.error("Kein Handler: %s (%s)", task_id, task_type)
                self._finish_task(task_id, "failed", error=f"Kein Handler für '{task_type}'")
                continue

            logger.info(
                "Verarbeite: %s (%s) queue=%s agent=%s",
                task_id, task_type, task.get("queue_name", ""), task.get("agent_name", ""))
            t0 = time.monotonic()
            try:
                payload = json.loads(task["payload"])
                payload["_task_id"] = task_id  # Allow handlers to check cancellation
                result = handler(payload)
                duration_s = time.monotonic() - t0
                self._finish_task(task_id, "completed", result=result, duration_s=duration_s)
                logger.info("Fertig: %s (%s) %.1fs", task_id, task_type, duration_s)

            except Exception as e:
                duration_s = time.monotonic() - t0
                retry_count = task.get("retry_count", 0)
                max_retries = task.get("max_retries", 0)
                if retry_count < max_retries:
                    logger.warning(
                        "Fehler (Retry %d/%d): %s: %s",
                        retry_count + 1, max_retries, task_id, e)
                    self._finish_task(task_id, "failed", error=str(e),
                                      duration_s=duration_s, retry=True)
                else:
                    logger.error(
                        "Fehler: %s (%s): %s", task_id, task_type, e, exc_info=True)
                    self._finish_task(task_id, "failed", error=str(e), duration_s=duration_s)


# ---------------------------------------------------------------------------
# VRAM Auto-Free (moved from task_tracker.py)
# ---------------------------------------------------------------------------

def _auto_free_comfyui_vram() -> None:
    """Gibt ComfyUI VRAM frei wenn alle Queues idle sind. Laeuft im Hintergrund."""
    import os
    try:
        import requests as req
    except ImportError:
        return

    # Auch LLM Queue pruefen — nur freigeben wenn wirklich alles idle
    try:
        from app.core.llm_queue import get_llm_queue
        status = get_llm_queue().get_status()
        if status.get("current_task") or status.get("chat_active"):
            return
        if any(t.get("status") == "pending" for t in (status.get("pending") or [])):
            return
    except Exception:
        pass

    # TTS ComfyUI URL ausschliessen — sonst wird das TTS-Modell nach jedem
    # TTS-Job entladen und muss beim naechsten voicename-Call neu geladen werden.
    # URL wird aus TTS_COMFYUI_SKILL (SKILL_IMAGEGEN Backend) aufgeloest.
    tts_comfyui_url = ""
    _tts_skill = os.environ.get("TTS_COMFYUI_SKILL", "").strip()
    if _tts_skill:
        _targets = [s.strip() for s in _tts_skill.split(",") if s.strip()]
        for _si in range(1, 20):
            _sn = os.environ.get(f"SKILL_IMAGEGEN_{_si}_NAME", "")
            if not _sn:
                break
            if _sn.strip() in _targets and os.environ.get(f"SKILL_IMAGEGEN_{_si}_API_TYPE", "").strip().lower() == "comfyui":
                tts_comfyui_url = os.environ.get(f"SKILL_IMAGEGEN_{_si}_API_URL", "").strip().rstrip("/")
                break

    # ComfyUI URLs sammeln (nur aktivierte Image-Gen-Instanzen)
    urls = set()
    for i in range(1, 10):
        enabled = os.environ.get(f"SKILL_IMAGEGEN_{i}_ENABLED", "true").strip().lower() in ("true", "1", "yes")
        if not enabled:
            continue
        if os.environ.get(f"SKILL_IMAGEGEN_{i}_API_TYPE", "").strip().lower() == "comfyui":
            url = os.environ.get(f"SKILL_IMAGEGEN_{i}_API_URL", "").strip().rstrip("/")
            if url and url != tts_comfyui_url:
                urls.add(url)

    if not urls:
        return

    def _free():
        for comfy_url in urls:
            try:
                resp = req.post(
                    f"{comfy_url}/free",
                    json={"unload_models": True, "free_memory": True},
                    timeout=3)
                if resp.status_code == 200:
                    logger.debug("ComfyUI VRAM freigegeben: %s", comfy_url)
            except Exception:
                pass  # Server nicht erreichbar – kein VRAM zu befreien

    # Im Hintergrund ausfuehren um track_finish() nicht zu blockieren
    threading.Thread(target=_free, daemon=True, name="ComfyUI-VRAM-Free").start()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_task_queue: Optional[TaskQueue] = None
_init_lock = threading.Lock()


def get_task_queue() -> TaskQueue:
    """Returns the global TaskQueue singleton."""
    global _task_queue
    if _task_queue is None:
        with _init_lock:
            if _task_queue is None:
                _task_queue = TaskQueue()
    return _task_queue
