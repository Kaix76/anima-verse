"""Zentrale LLM-Queue — Fassade ueber ProviderManager.

Behaelt LLMTask und Priority als kanonische Definitionen bei.
LLMQueue selbst delegiert alle Aufrufe an den ProviderManager,
der sie an die richtige Provider-Queue routet.

Bestehende Consumer (social_reactions, instagram_skill, talkto_skill, etc.)
koennen weiterhin get_llm_queue().submit() nutzen ohne Aenderungen.

Verwendung:
    from app.core.llm_queue import get_llm_queue, Priority

    queue = get_llm_queue()
    response = queue.submit("image_prompt", Priority.NORMAL, llm, messages, agent_name="Pixel")
"""
import threading  # noqa: F401  (weiterhin fuer LLMTask._done_event genutzt)
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any, Dict, Optional

from app.core.log import get_logger

logger = get_logger("llm_queue")


class Priority(IntEnum):
    """Task-Prioritaeten. Niedrigerer Wert = hoehere Prioritaet."""
    CHAT = 0        # Nur Tracking (nicht queued)
    HIGH = 10       # story_stream
    NORMAL = 20     # image_prompt, extraction, history_summary, instagram_caption, image_comment
    IMAGE_GEN = 25  # image_generation via GPU-Slot (zwischen NORMAL und LOW)
    LOW = 30        # social_reaction, talkto


@dataclass
class LLMTask:
    """Ein LLM-Task in der Queue."""
    task_id: str
    task_type: str
    priority: int
    agent_name: str
    created_at: str
    status: str = "pending"       # pending, running, completed, failed, cancelled, chat_active
    result: Any = None
    error: str = ""
    duration_s: float = 0.0
    provider_name: str = ""
    model: str = ""
    started_at: str = ""        # Zeitpunkt wann Task tatsaechlich verarbeitet wird (nicht Queue-Einreichung)
    vram_required_mb: int = 0   # VRAM in MB die dieser Task braucht (0 = unbekannt/unmanaged)
    label: str = ""             # Optional user-friendly label fuer Task-Panel
    # Iteration tracking fuer chat_active Tasks (StreamingAgent kann mehrere
    # LLM-Calls pro Turn machen: Initial → Tool-Call → Follow-Up). Wird vom
    # Agent via register_chat_iteration() aktualisiert. 0 = noch nicht gestartet.
    current_iteration: int = 0
    max_iterations: int = 1
    # Dauer-Schaetzung — wird bei Verarbeitungsstart gesetzt (nicht beim Submit),
    # damit nur Calls eine Anzeige bekommen, die wirklich auf einem Provider laufen.
    estimated_duration_s: float = 0.0
    estimated_p90_s: float = 0.0
    estimated_in_tokens: int = 0
    estimated_samples: int = 0
    # Interne Felder (nicht in Status-Output)
    _llm: Any = field(default=None, repr=False)
    _messages: Any = field(default=None, repr=False)
    _done_event: threading.Event = field(default_factory=threading.Event, repr=False)
    _cancelled: bool = field(default=False, repr=False)
    _retry_count: int = field(default=0, repr=False)
    # Monoton steigender Timestamp — wird fuer Stale-Detection genutzt,
    # damit Server-Zeitumstellungen/Drifts die Erkennung nicht verfaelschen.
    _monotonic_created: float = field(default=0.0, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        """Serialisiert fuer REST-Endpoint (ohne interne Felder)."""
        d = {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "priority": self.priority,
            "agent_name": self.agent_name,
            "created_at": self.created_at,
            "status": self.status,
            "error": self.error,
            "duration_s": self.duration_s,
        }
        if self.started_at:
            d["started_at"] = self.started_at
        if self.provider_name:
            d["provider_name"] = self.provider_name
        if self.model:
            d["model"] = self.model
        if self.label:
            d["label"] = self.label
        if self.current_iteration > 0:
            d["iteration"] = self.current_iteration
            d["max_iterations"] = self.max_iterations
        if self._retry_count > 0:
            d["retry_count"] = self._retry_count
        if self.estimated_duration_s > 0:
            d["estimated_duration_s"] = round(self.estimated_duration_s, 1)
            d["estimated_p90_s"] = round(self.estimated_p90_s, 1)
            d["estimated_samples"] = self.estimated_samples
        return d


class LLMQueue:
    """Fassade ueber ProviderManager — routet Tasks an die richtige Provider-Queue.

    Globale Pause-Funktionalitaet entfernt — Task-Disable laeuft jetzt ueber
    den Router (app/core/llm_task_state.py). Deaktivierte Tasks bekommen vom
    Router kein LLM und der Aufrufer faellt in seinen eigenen Fallback.
    """

    def submit(
        self,
        task_type: str,
        priority: int,
        llm: Any,
        messages_or_prompt: Any,
        agent_name: str = "", label: str = "") -> Any:
        """Gibt einen LLM-Call in die richtige Provider-Queue.

        Bestimmt den Provider anhand der api_base des LLMClient-Objekts
        und delegiert an die entsprechende ProviderQueue.
        """
        from .provider_manager import get_provider_manager

        pm = get_provider_manager()
        provider_name = self._resolve_provider_name(llm)

        if provider_name:
            pq = pm.get_queue_for_provider(provider_name)
            if pq:
                return pq.submit(task_type, priority, llm, messages_or_prompt,
                                 agent_name, label=label)

        # Fallback: first available queue
        pq = pm.get_first_queue()
        if pq:
            return pq.submit(task_type, priority, llm, messages_or_prompt,
                             agent_name, label=label)

        raise Exception("No provider queue available for LLM call")

    def submit_gpu_task(
        self,
        provider_name: str = "",
        task_type: str = "",
        priority: int = 20,
        callable_fn=None,
        agent_name: str = "", label: str = "",
        vram_required_mb: int = 0,
        gpu_type: str = "") -> Any:
        """Gibt einen GPU-Slot-Task in die Provider-Queue.

        Routing: gpu_type fuer dynamisches Routing (bevorzugt), provider_name als Fallback.
        """
        from .provider_manager import get_provider_manager
        pm = get_provider_manager()
        return pm.submit_gpu_task(provider_name, task_type, priority, callable_fn,
                                  agent_name, label, vram_required_mb,
                                  gpu_type=gpu_type)

    def register_chat_active(self, agent_name: str, llm_instance: Any = None,
                             task_type: str = "chat_stream",
                             label: str = "") -> str:
        """Registriert aktiven Chat/Story. Pausiert die Provider-Queue.

        Args:
            agent_name: Character name
            user_id: User ID
            llm_instance: Optional LLMInstance for provider-aware routing.
                          If not given, uses first available queue.
            task_type: differentiates entries in panel
                ("chat_stream", "thought", "talk_to", "send_message", ...)
            label: short user-friendly label

        Returns:
            task_id fuer register_chat_done()
        """
        from .provider_manager import get_provider_manager

        pm = get_provider_manager()

        if llm_instance:
            return pm.register_chat_active(llm_instance, agent_name, task_type=task_type, label=label)

        # Fallback: first available queue
        pq = pm.get_first_queue()
        if pq:
            return pq.register_chat_active(agent_name, task_type=task_type, label=label)

        raise Exception("No provider queue available for chat registration")

    async def register_chat_active_async(self, agent_name: str, llm_instance: Any = None,
                                          task_type: str = "chat_stream",
                                          label: str = "") -> str:
        """Async-Wrapper: laeuft den blockierenden _tasks_idle.wait() im Threadpool,
        damit der Event-Loop nicht haengt waehrend wir auf Provider-Idle warten.

        Async-Code MUSS diese Variante nutzen. Sync-Code (Worker-Threads) nutzt
        weiterhin register_chat_active().
        """
        import asyncio
        return await asyncio.to_thread(
            self.register_chat_active,
            agent_name, llm_instance=llm_instance,
            task_type=task_type, label=label,
        )

    def register_chat_done(self, task_id: str) -> None:
        """Chat/Story beendet. Queue wird fortgesetzt."""
        from .provider_manager import get_provider_manager
        pm = get_provider_manager()
        pm.register_chat_done(task_id)

    def register_chat_iteration(self, task_id: str,
                                 iteration: int, max_iterations: int) -> None:
        """Update iteration count on a chat_active task.

        Called by StreamingAgent at the start of each iteration so the
        admin queue panel can show "iter 2/3" while the turn runs.
        """
        from .provider_manager import get_provider_manager
        pm = get_provider_manager()
        pm.register_chat_iteration(task_id, iteration, max_iterations)

    def has_pending_tasks(self) -> bool:
        """Returns True if any provider queue has pending tasks."""
        from .provider_manager import get_provider_manager
        pm = get_provider_manager()
        return pm.has_pending_tasks()

    def get_status(self) -> Dict[str, Any]:
        """Queue-Status aggregiert ueber alle Provider."""
        from .provider_manager import get_provider_manager
        pm = get_provider_manager()
        return pm.get_combined_status()

    def _resolve_provider_name(self, llm: Any) -> Optional[str]:
        """Resolves provider name from a LLMClient's api_base.

        Matches the LLMClient's api_base against all known providers.
        Returns provider name if found, None otherwise.
        """
        try:
            from .provider_manager import get_provider_manager

            api_base = (getattr(llm, "openai_api_base", "")
                        or getattr(llm, "base_url", "")
                        or "")
            api_base = api_base.rstrip("/")
            if not api_base:
                return None

            pm = get_provider_manager()
            for name, provider in pm.providers.items():
                if provider.api_base.rstrip("/") == api_base:
                    return name

        except Exception:
            pass

        return None


# ---------------------------------------------------------------------------
# Modul-Singleton
# ---------------------------------------------------------------------------
_llm_queue: Optional[LLMQueue] = None


def get_llm_queue() -> LLMQueue:
    """Gibt die globale LLMQueue-Fassade zurueck."""
    global _llm_queue
    if _llm_queue is None:
        _llm_queue = LLMQueue()
        logger.info("Fassade initialisiert (delegiert an ProviderManager)")
    return _llm_queue
