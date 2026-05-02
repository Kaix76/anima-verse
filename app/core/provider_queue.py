"""Per-Provider LLM Queue with configurable concurrency.

Each Provider gets its own ProviderQueue. Tasks are processed in priority order
with up to max_concurrent workers running simultaneously.

Chat/Story streaming bypasses the queue (direct invoke) but registers for tracking.
While chat is active on a provider, that provider's background tasks pause.
"""
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime
from typing import Any, Dict, List, Optional

from .provider import Provider
from .llm_queue import LLMTask, Priority
from app.utils.llm_logger import get_model_name

from app.core.log import get_logger
logger = get_logger("provider_queue")


class _CancelledByUser(Exception):
    """Internal signal: task was cancelled by user."""


def _wait_for_future(future, task, timeout: float, poll_interval: float = 2.0):
    """Waits for a future with periodic cancel checks.

    Instead of blocking for the full timeout, polls in short intervals
    and checks task._cancelled between polls. This allows cancel requests
    to take effect within poll_interval seconds instead of waiting for
    the full timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        if task._cancelled:
            raise _CancelledByUser()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FuturesTimeoutError()
        try:
            return future.result(timeout=min(poll_interval, remaining))
        except FuturesTimeoutError:
            continue  # Re-check cancel flag


# Errors that indicate a retriable GPU OOM condition
_RETRIABLE_GPU_ERRORS = (
    "OutOfMemoryError",
    "CUDA out of memory",
    "torch.OutOfMemoryError")

_GPU_MAX_RETRIES = 2  # Max retry attempts for retriable GPU errors


class ProviderQueue:
    """Per-provider (or per-GPU) queue with configurable concurrency and chat pause."""

    def __init__(self, provider: Provider, queue_name: str = "",
                 max_concurrent: int = 0, chat_pause_enabled: bool = True,
                 gpu_indices: Optional[List[int]] = None):
        self.provider = provider
        self._queue_name = queue_name or provider.name
        self._chat_pause_enabled = chat_pause_enabled
        self._gpu_indices = gpu_indices  # Which GPU(s) this queue serves
        effective_concurrent = max_concurrent if max_concurrent > 0 else provider.max_concurrent
        self._max_concurrent = effective_concurrent
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._seq_counter: int = 0
        self._lock = threading.Lock()
        self._chat_active = threading.Event()
        self._chat_active.set()  # Initially free (no chat active)
        self._tasks_idle = threading.Event()
        self._tasks_idle.set()  # Initially no tasks running
        self._running = False
        self._workers: List[threading.Thread] = []
        self._semaphore = threading.Semaphore(effective_concurrent)
        self._current_tasks: List[LLMTask] = []
        # Multiple concurrent chats supported (keyed by task_id)
        self._chat_tasks: Dict[str, LLMTask] = {}
        self._chat_registered_at: float = 0.0  # monotonic timestamp (latest)
        self._history: List[LLMTask] = []
        self._history_limit: int = 30
        self._pending_tasks: List[LLMTask] = []
        self._futures: Dict[str, Any] = {}  # task_id -> Future (for running task cancel)

    def submit(
        self,
        task_type: str,
        priority: int,
        llm: Any,
        messages_or_prompt: Any,
        agent_name: str = "", label: str = "") -> Any:
        """Submits an LLM call to this provider's queue. Blocks until result ready.

        Args:
            task_type: e.g. "image_prompt", "extraction", "social_reaction"
            priority: Priority.LOW / NORMAL / HIGH
            llm: LLMClient instance
            messages_or_prompt: List[Message] or str prompt
            label: Optional detail for logging (e.g. related character)
            agent_name: Character name
            user_id: User ID

        Returns:
            AIMessage (LLM response)

        Raises:
            Exception: if LLM call fails
        """
        task = LLMTask(
            task_id=f"llm_{uuid.uuid4().hex[:8]}",
            task_type=task_type,
            priority=priority,
            agent_name=agent_name,
            created_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            provider_name=self.provider.name,
            model=get_model_name(llm),
            _llm=llm,
            _messages=messages_or_prompt)

        with self._lock:
            self._seq_counter += 1
            seq = self._seq_counter
            self._pending_tasks.append(task)

        self._queue.put((priority, seq, task))
        label_suffix = f" label={label}" if label else ""
        logger.info("[%s] Eingereicht: %s (%s) prio=%d agent=%s%s",
                    self._queue_name, task.task_id, task_type, priority, agent_name, label_suffix)

        self._ensure_workers()

        # Block until worker sets the result
        task._done_event.wait()

        # Remove from pending
        with self._lock:
            if task in self._pending_tasks:
                self._pending_tasks.remove(task)

        if task.status == "cancelled":
            raise Exception(f"LLM Queue task cancelled: {task.task_id}")

        if task.status == "failed":
            raise Exception(f"LLM Queue task failed: {task.error}")

        return task.result

    def submit_gpu_task(
        self,
        task_type: str,
        priority: int,
        callable_fn,
        agent_name: str = "", label: str = "",
        vram_required_mb: int = 0) -> Any:
        """Submits a GPU-holding task to this provider's queue.

        The callable runs while holding the GPU slot (semaphore). Used for image
        generation on backends that share a GPU with this LLM provider.
        Blocks until the callable completes and returns its result.
        """
        task = LLMTask(
            task_id=f"gpu_{uuid.uuid4().hex[:8]}",
            task_type=task_type,
            priority=priority,
            agent_name=agent_name,
            created_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            provider_name=self.provider.name,
            model=label,
            vram_required_mb=vram_required_mb)
        # Store the callable on the task object
        task._gpu_callable = callable_fn

        with self._lock:
            self._seq_counter += 1
            seq = self._seq_counter
            self._pending_tasks.append(task)

        self._queue.put((priority, seq, task))
        logger.info("[%s] GPU-Task eingereicht: %s (%s) prio=%d agent=%s label=%s",
                    self._queue_name, task.task_id, task_type, priority, agent_name, label)

        self._ensure_workers()

        # Block until worker sets the result
        task._done_event.wait()

        # Remove from pending
        with self._lock:
            if task in self._pending_tasks:
                self._pending_tasks.remove(task)

        if task.status == "cancelled":
            raise Exception(f"GPU task cancelled: {task.task_id}")
        if task.status == "failed":
            raise Exception(f"GPU task failed: {task.error}")

        return task.result

    def register_chat_active(self, agent_name: str, model: str = "",
                             task_type: str = "chat_stream",
                             label: str = "") -> str:
        """Registers active chat/story. Pauses this provider's queue.

        Waits for any running background task to finish before returning,
        preventing conflicts on single-model servers like llama-swap where
        a new request would kill the running one.

        Supports multiple concurrent chats (e.g. Pixel + Diego on same provider).

        Args:
            task_type: differentiates entry types in panel
                ("chat_stream", "thought", "talk_to", "send_message", ...)
            label: short user-friendly label, falls back to task_type when empty

        Returns:
            task_id for register_chat_done()
        """
        task = LLMTask(
            task_id=f"chat_{uuid.uuid4().hex[:8]}",
            task_type=task_type,
            priority=Priority.CHAT,
            agent_name=agent_name,
            created_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            status="chat_active",
            provider_name=self.provider.name,
            model=model,
            label=label)
        # Monotonic-Timestamp fuer Stale-Detection (pro Task, nicht global)
        task._monotonic_created = time.monotonic()
        # Dauer-Schaetzung ohne Input-Skalierung — Messages liegen beim
        # Registrieren noch nicht vor, also Median-Dauer aus (model,task) zeigen.
        _attach_duration_estimate(task)

        with self._lock:
            self._chat_tasks[task.task_id] = task
            self._chat_registered_at = time.monotonic()

        # Pause queue: workers won't start NEW tasks
        self._chat_active.clear()

        # Bei max_concurrent=1: auf laufenden Task warten (z.B. llama-swap)
        # Bei max_concurrent>1: Server kann parallele Requests verarbeiten
        if self.provider.max_concurrent <= 1 and not self._tasks_idle.is_set():
            logger.info("[%s] Chat wartet auf laufenden Task (max_concurrent=%d)...",
                        self._queue_name, self.provider.max_concurrent)
            if not self._tasks_idle.wait(timeout=300):
                logger.warning("[%s] Task laeuft noch nach 300s, Chat startet trotzdem",
                               self._queue_name)

        active_count = len(self._chat_tasks)
        logger.info("[%s] Chat aktiv: %s (%s) — Queue pausiert (%d aktive Chats)",
                    self._queue_name, agent_name, task.task_id, active_count)
        return task.task_id

    def register_chat_iteration(self, task_id: str,
                                 iteration: int, max_iterations: int) -> None:
        """Update iteration progress on a chat_active task.

        Called by StreamingAgent at each iteration boundary so the queue
        panel can show "iter 2/3" while the turn streams.
        """
        with self._lock:
            task = self._chat_tasks.get(task_id)
            if task is None:
                return
            task.current_iteration = iteration
            task.max_iterations = max_iterations

    def register_chat_done(self, task_id: str) -> None:
        """Chat/story finished. Resumes queue only when ALL chats are done."""
        with self._lock:
            task = self._chat_tasks.pop(task_id, None)
            if task:
                task.status = "completed"
                task.duration_s = 0
                self._history.append(task)
                if len(self._history) > self._history_limit:
                    self._history = self._history[-self._history_limit:]
                agent = task.agent_name
            else:
                agent = "?"
            remaining = len(self._chat_tasks)

        # Resume queue only when NO more active chats on this provider
        if remaining == 0:
            self._chat_active.set()
            logger.info("[%s] Chat beendet: %s (%s) — Queue fortgesetzt (keine aktiven Chats)",
                        self._queue_name, agent, task_id)
        else:
            logger.info("[%s] Chat beendet: %s (%s) — Queue bleibt pausiert (%d aktive Chats)",
                        self._queue_name, agent, task_id, remaining)

    def cancel_task(self, task_id: str) -> bool:
        """Cancels a pending or running task. Returns True if found and cancelled."""
        with self._lock:
            # 1. Pending tasks
            for task in self._pending_tasks:
                if task.task_id == task_id and task.status == "pending":
                    task._cancelled = True
                    task.status = "cancelled"
                    task.error = "Abgebrochen"
                    self._pending_tasks.remove(task)
                    self._history.append(task)
                    if len(self._history) > self._history_limit:
                        self._history = self._history[-self._history_limit:]
                    task._done_event.set()
                    logger.info("[%s] Abgebrochen (pending): %s (%s) agent=%s",
                                self._queue_name, task.task_id, task.task_type, task.agent_name)
                    return True

            # 2. Running tasks — set cancelled flag + cancel future
            for task in self._current_tasks:
                if task.task_id == task_id and task.status == "running":
                    task._cancelled = True
                    future = self._futures.get(task_id)
                    if future:
                        future.cancel()
                    logger.info("[%s] Abbruch angefordert (running): %s (%s) agent=%s",
                                self._queue_name, task.task_id, task.task_type, task.agent_name)
                    return True
        return False

    def has_pending_tasks(self) -> bool:
        """Returns True if there are pending tasks in the queue."""
        with self._lock:
            return len(self._pending_tasks) > 0 or not self._queue.empty()

    def get_status(self) -> Dict[str, Any]:
        """Queue status for this provider."""
        with self._lock:
            chat_list = [t.to_dict() for t in self._chat_tasks.values()]
            # Backwards-compatible: single chat_active field
            chat = chat_list[0] if len(chat_list) == 1 else (chat_list if chat_list else None)
            current = [t.to_dict() for t in self._current_tasks]
            pending = [t.to_dict() for t in self._pending_tasks
                       if t.status == "pending"]
            recent = [t.to_dict() for t in reversed(self._history[-20:])]

        return {
            "provider": self.provider.name,
            "queue_name": self._queue_name,
            "type": self.provider.type,
            "available": self.provider.available,
            "max_concurrent": self._max_concurrent,
            "gpu_indices": self._gpu_indices,
            "chat_active": chat,
            "current_tasks": current,
            "pending": pending,
            "recent": recent,
        }

    def _ensure_workers(self) -> None:
        """Starts worker threads if not already running."""
        with self._lock:
            if self._running:
                return
            self._running = True
            for i in range(self._max_concurrent):
                t = threading.Thread(
                    target=self._worker_loop,
                    daemon=True,
                    name=f"ProviderQueue-{self._queue_name}-{i}")
                self._workers.append(t)
                t.start()

    _STALE_CHAT_TIMEOUT = 660  # 11 Minuten — Notbremse fuer haengende Chat-Registrierungen.
    # Muss > THOUGHT_CALL_TIMEOUT (default 600s) sein, damit der
    # regulaere asyncio.wait_for in thoughts.py vor dieser Notbremse greift.
    # Diese Stale-Bereinigung ist nur Backup falls auch der Cancel nicht
    # greift (z.B. echter Browser-Reload waehrend Streaming, Tool-Crash ohne finally).

    def _check_stale_chat(self) -> None:
        """Prueft ob Chat-Registrierungen veraltet sind und raeumt auf.

        Nutzt das per-Task _monotonic_created — der globale
        _chat_registered_at wird bei jeder neuen Registrierung ueberschrieben
        und ist deshalb unbrauchbar fuer die Alterung einzelner Stuck-Tasks.
        Typischer Stuck-Fall: Browser-Reload waehrend Streaming, generate()
        feuert finally nicht mehr, chat_tasks-Eintrag bleibt liegen.
        """
        now = time.monotonic()
        cleaned = []
        with self._lock:
            if not self._chat_tasks:
                return
            stale_ids = []
            for tid, task in self._chat_tasks.items():
                t_created = getattr(task, "_monotonic_created", 0.0)
                if t_created <= 0:
                    # Alt-Eintrag ohne Timestamp → sofort als stale behandeln
                    stale_ids.append(tid)
                    continue
                age = now - t_created
                if age > self._STALE_CHAT_TIMEOUT:
                    stale_ids.append(tid)

            for tid in stale_ids:
                task = self._chat_tasks.pop(tid)
                task.status = "completed"
                self._history.append(task)
                cleaned.append((task.agent_name, tid))

            if len(self._history) > self._history_limit:
                self._history = self._history[-self._history_limit:]
            remaining = len(self._chat_tasks)
            if remaining == 0:
                self._chat_registered_at = 0.0

        for agent, tid in cleaned:
            logger.warning("[%s] Stale Chat bereinigt: %s (%s) nach %ds",
                          self._queue_name, agent, tid, self._STALE_CHAT_TIMEOUT)
        if cleaned and remaining == 0:
            self._chat_active.set()
            logger.info("[%s] Queue fortgesetzt (alle stale Chats bereinigt)", self._queue_name)

    def _worker_loop(self) -> None:
        """Processes LLM tasks. Pauses when chat is active on this provider."""
        while True:
            # Wait until chat is not active on this provider (mit Stale-Check)
            if self._chat_pause_enabled:
                while not self._chat_active.wait(timeout=30):
                    self._check_stale_chat()

            try:
                _, _, task = self._queue.get(timeout=5.0)
            except queue.Empty:
                with self._lock:
                    if self._queue.empty():
                        self._running = False
                        self._workers = [
                            w for w in self._workers
                            if w is not threading.current_thread()
                        ]
                        if not self._workers:
                            logger.debug("[%s] Worker idle, beendet", self._queue_name)
                        return
                continue

            # Skip cancelled tasks
            if task._cancelled:
                self._queue.task_done()
                continue

            # Acquire semaphore (limits concurrency)
            self._semaphore.acquire()

            # Re-check chat pause after acquiring semaphore
            if self._chat_pause_enabled:
                while not self._chat_active.wait(timeout=30):
                    self._check_stale_chat()

            # Re-check cancelled after wait
            if task._cancelled:
                self._semaphore.release()
                self._queue.task_done()
                continue

            with self._lock:
                self._current_tasks.append(task)
                self._tasks_idle.clear()
            task.status = "running"
            task.started_at = datetime.now().isoformat(timespec="seconds")
            _attach_duration_estimate(task)
            logger.info("[%s] Verarbeite: %s (%s) agent=%s",
                        self._queue_name, task.task_id, task.task_type, task.agent_name)

            t0 = time.monotonic()
            task_timeout = self.provider.timeout or 300  # Default 5 Min
            gpu_callable = getattr(task, '_gpu_callable', None)

            if gpu_callable:
                # GPU-Slot-Task: VRAM pruefen und ggf. Models entladen
                vram_needed = task.vram_required_mb
                self.provider.ensure_vram_available(vram_needed, gpu_indices=self._gpu_indices)

                # GPU-Slot-Task: callable ausfuehren (z.B. image generation)
                try:
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(gpu_callable)
                        with self._lock:
                            self._futures[task.task_id] = future
                        result = _wait_for_future(future, task, task_timeout)
                    task.result = result
                    task.status = "completed"
                    task.duration_s = round(time.monotonic() - t0, 2)
                    logger.info("[%s] GPU-Task fertig: %s (%ss)",
                                self._queue_name, task.task_id, task.duration_s)
                except _CancelledByUser:
                    task.status = "cancelled"
                    task.error = "Abgebrochen"
                    task.duration_s = round(time.monotonic() - t0, 2)
                    logger.info("[%s] GPU-Task abgebrochen: %s (%ss)",
                                self._queue_name, task.task_id, task.duration_s)
                except FuturesTimeoutError:
                    task.status = "failed"
                    task.error = f"Task timeout nach {task_timeout}s"
                    task.duration_s = round(time.monotonic() - t0, 2)
                    logger.error("[%s] GPU-Task Timeout: %s nach %ds",
                                 self._queue_name, task.task_id, task_timeout)
                except Exception as e:
                    if task._cancelled:
                        task.status = "cancelled"
                        task.error = "Abgebrochen"
                        task.duration_s = round(time.monotonic() - t0, 2)
                        logger.info("[%s] GPU-Task abgebrochen: %s", self._queue_name, task.task_id)
                    else:
                        err_str = str(e)
                        is_retriable = any(pat in err_str for pat in _RETRIABLE_GPU_ERRORS)
                        if is_retriable and task._retry_count < _GPU_MAX_RETRIES:
                            # Retry: unload VRAM, re-queue task
                            task._retry_count += 1
                            task.duration_s = round(time.monotonic() - t0, 2)
                            logger.warning(
                                "[%s] GPU-Task OOM, Retry %d/%d: %s — VRAM freigeben und erneut versuchen",
                                self._queue_name, task._retry_count, _GPU_MAX_RETRIES, task.task_id)
                            if not self.provider._is_comfyui_gpu(self._gpu_indices):
                                self.provider._do_unload()
                                time.sleep(5)  # Wait for VRAM to be freed by llama-swap
                            # Re-queue: reset status, put back in queue
                            task.status = "pending"
                            task.error = ""
                            with self._lock:
                                if task in self._current_tasks:
                                    self._current_tasks.remove(task)
                                if not self._current_tasks:
                                    self._tasks_idle.set()
                                self._seq_counter += 1
                                seq = self._seq_counter
                                self._pending_tasks.append(task)
                                self._futures.pop(task.task_id, None)
                            self._queue.put((task.priority, seq, task))
                            self._semaphore.release()
                            self._queue.task_done()
                            continue  # Skip normal cleanup — task is re-queued
                        else:
                            task.status = "failed"
                            task.error = err_str
                            task.duration_s = round(time.monotonic() - t0, 2)
                            if is_retriable:
                                logger.error("[%s] GPU-Task OOM nach %d Retries: %s: %s",
                                             self._queue_name, task._retry_count, task.task_id, e)
                            else:
                                logger.error("[%s] GPU-Task Fehler: %s: %s",
                                             self._queue_name, task.task_id, e, exc_info=True)
                finally:
                    with self._lock:
                        self._futures.pop(task.task_id, None)
            else:
                # Standard LLM-Task
                model_name = get_model_name(task._llm)
                max_tokens = _get_max_tokens_safe(task._llm)
                api_base = getattr(task._llm, 'openai_api_base', None) or getattr(task._llm, 'base_url', '?')
                logger.debug("[%s] -> model=%s api_base=%s max_tokens=%s", self._queue_name, model_name, api_base, max_tokens)

                try:
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(task._llm.invoke, task._messages)
                        with self._lock:
                            self._futures[task.task_id] = future
                        response = _wait_for_future(future, task, task_timeout)
                    # Strip thinking tags from response (Qwen3.5 etc.)
                    response = _strip_thinking(response)

                    # Retry wenn Response leer (Modell hat alle Tokens fuer Thinking verbraucht)
                    resp_content = getattr(response, "content", None) or ""
                    if not resp_content.strip() and max_tokens and max_tokens > 0:
                        logger.warning(
                            "[%s] Leere Response nach Thinking-Strip: %s (%s) agent=%s — "
                            "Retry mit max_tokens=%d (vorher %d)",
                            self._queue_name, task.task_id, task.task_type,
                            task.agent_name, max_tokens * 2, max_tokens)
                        # max_tokens verdoppeln und erneut versuchen
                        orig_max = task._llm.max_tokens
                        task._llm.max_tokens = max_tokens * 2
                        try:
                            with self._lock:
                                self._futures.pop(task.task_id, None)
                            with ThreadPoolExecutor(max_workers=1) as executor2:
                                future2 = executor2.submit(task._llm.invoke, task._messages)
                                with self._lock:
                                    self._futures[task.task_id] = future2
                                response = _wait_for_future(future2, task, task_timeout)
                            response = _strip_thinking(response)
                        finally:
                            task._llm.max_tokens = orig_max

                    task.result = response
                    task.status = "completed"
                    task.duration_s = round(time.monotonic() - t0, 2)
                    logger.info("[%s] Fertig: %s (%ss)", self._queue_name, task.task_id, task.duration_s)

                    _log_task_result(task, model_name, max_tokens, response)
                except _CancelledByUser:
                    task.status = "cancelled"
                    task.error = "Abgebrochen"
                    task.duration_s = round(time.monotonic() - t0, 2)
                    logger.info("[%s] Task abgebrochen: %s (%s) (%ss)",
                                self._queue_name, task.task_id, task.task_type, task.duration_s)
                except FuturesTimeoutError:
                    task.status = "failed"
                    task.error = f"Task timeout nach {task_timeout}s"
                    task.duration_s = round(time.monotonic() - t0, 2)
                    logger.error("[%s] Task Timeout: %s (%s) nach %ds — Queue wird fortgesetzt",
                                 self._queue_name, task.task_id, task.task_type, task_timeout)
                except Exception as e:
                    if task._cancelled:
                        task.status = "cancelled"
                        task.error = "Abgebrochen"
                        task.duration_s = round(time.monotonic() - t0, 2)
                        logger.info("[%s] Task abgebrochen: %s (%s)",
                                    self._queue_name, task.task_id, task.task_type)
                    else:
                        task.status = "failed"
                        task.error = str(e)
                        task.duration_s = round(time.monotonic() - t0, 2)
                        logger.error("[%s] Fehler: %s: %s", self._queue_name, task.task_id, e, exc_info=True)
                        # Backend-side crash (5xx, process exit, conn drop):
                        # cooldown the provider so resolve_llm skips it and
                        # the routing chain falls through. Streaming consumers
                        # that don't go through llm_call benefit too.
                        try:
                            from app.core.llm_router import _is_upstream_failure, _UPSTREAM_COOLDOWN_SECONDS
                            if _is_upstream_failure(e):
                                self.provider.mark_unhealthy(
                                    f"upstream-fail [{task.task_type}]: {str(e)[:120]}",
                                    _UPSTREAM_COOLDOWN_SECONDS)
                        except Exception:
                            pass
                finally:
                    with self._lock:
                        self._futures.pop(task.task_id, None)

            # Move to history
            with self._lock:
                if task in self._current_tasks:
                    self._current_tasks.remove(task)
                if not self._current_tasks:
                    self._tasks_idle.set()
                self._history.append(task)
                if len(self._history) > self._history_limit:
                    self._history = self._history[-self._history_limit:]

            # Release LLM/messages (memory)
            task._llm = None
            task._messages = None

            # Unblock caller
            task._done_event.set()

            # Release semaphore
            self._semaphore.release()

            self._queue.task_done()


_THINK_RE = re.compile(r'<think>.*?</think>\s*', re.DOTALL)
_THINK_OPEN_RE = re.compile(r'<think>.*', re.DOTALL)


def _strip_thinking(response) -> Any:
    """Remove <think>...</think> blocks from LLM response content.

    Handles both complete blocks and truncated thinking (when max_tokens
    cuts off mid-think without closing </think> tag).
    """
    content = getattr(response, "content", None)
    if not content or not isinstance(content, str):
        return response
    if "<think>" not in content:
        return response
    # First remove complete <think>...</think> blocks
    cleaned = _THINK_RE.sub("", content).strip()
    # Then remove truncated <think>... (no closing tag, hit max_tokens)
    if "<think>" in cleaned:
        cleaned = _THINK_OPEN_RE.sub("", cleaned).strip()
    response.content = cleaned
    return response


def _get_max_tokens_safe(llm) -> int:
    """Extracts max_tokens from LLM instance."""
    val = getattr(llm, "max_tokens", None)
    return int(val) if val else 0


def _log_task_result(task: LLMTask, model_name: str, max_tokens: int, response) -> None:
    """Logs a completed LLM task result to the JSONL file."""
    try:
        from app.utils.llm_logger import log_llm_call, extract_token_info, estimate_tokens

        token_info = extract_token_info(response)
        response_text = getattr(response, "content", None)
        if response_text is None:
            response_text = str(response)

        # System-Prompt vom restlichen Prompt trennen damit der Logger
        # beide Felder sauber ausgibt (sonst landet system im user-Feld).
        system_text = ""
        prompt_text = ""
        if isinstance(task._messages, list):
            user_parts = []
            for m in task._messages:
                if isinstance(m, dict):
                    role = m.get("role", "?")
                    content = m.get("content", "")
                    if isinstance(content, list):
                        # Vision-Messages mit multi-content
                        content = " ".join(
                            p.get("text", "") for p in content
                            if isinstance(p, dict) and p.get("type") == "text"
                        )
                    elif content is None:
                        content = ""
                else:
                    role = getattr(m, "type", "?")
                    content = getattr(m, "content", str(m))
                if role == "system" and not system_text:
                    system_text = content
                else:
                    user_parts.append(f"[{role}] {content}")
            prompt_text = "\n".join(user_parts)
        elif isinstance(task._messages, str):
            prompt_text = task._messages

        tokens_in = token_info.get("input_tokens", 0) or estimate_tokens(system_text + prompt_text)
        tokens_out = token_info.get("output_tokens", 0) or estimate_tokens(response_text)

        log_llm_call(
            task=task.task_type,
            model=model_name,
            agent_name=task.agent_name,
            provider=task.provider_name,
            system_prompt=system_text,
            user_input=prompt_text,
            response=response_text,
            duration_s=task.duration_s,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            max_tokens=max_tokens)
    except Exception as e:
        logger.error("Logging-Fehler: %s", e, exc_info=True)


def _attach_duration_estimate(task: LLMTask) -> None:
    """Berechnet aus historischen Stats die voraussichtliche Dauer dieses Calls
    und legt sie an `task` ab (sichtbar fuer das Queue-Panel).

    Wird beim Verarbeitungsstart aufgerufen — nicht beim Submit — damit nur
    Calls, die wirklich auf einem Provider laufen, eine Schaetzung bekommen.
    GPU-Slot-Tasks (Bildgenerierung) bekommen keine LLM-Schaetzung.
    """
    if getattr(task, "_gpu_callable", None) is not None:
        return
    if not task.model or not task.task_type:
        return
    try:
        from app.utils.llm_logger import estimate_tokens
        from app.utils.llm_stats import estimate_duration

        # Chat-Active-Tasks haben kein _messages — dann ohne Input-Skalierung,
        # estimate_duration faellt automatisch auf reinen Median zurueck.
        in_tokens = _estimate_in_tokens(task._messages, estimate_tokens) \
            if task._messages is not None else 0
        est = estimate_duration(task.model, task.task_type,
                                provider=task.provider_name, in_tokens=in_tokens)
        if not est:
            return
        task.estimated_in_tokens = in_tokens
        task.estimated_duration_s = est["est_duration_s"]
        task.estimated_p90_s = est["p90_duration_s"]
        task.estimated_samples = est["samples"]
    except Exception as e:
        logger.debug("Dauer-Schaetzung fehlgeschlagen: %s", e)


def _estimate_in_tokens(messages, estimate_fn) -> int:
    """Sammelt den Text aus messages_or_prompt und schaetzt die Token-Zahl."""
    if isinstance(messages, str):
        return estimate_fn(messages)
    if not isinstance(messages, list):
        return 0
    total = 0
    for m in messages:
        if isinstance(m, dict):
            content = m.get("content", "")
        else:
            content = getattr(m, "content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        elif content is None:
            content = ""
        if content:
            total += estimate_fn(content)
    return total
