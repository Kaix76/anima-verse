"""Task-Disable-Zustand fuer den LLM-Router.

Ersetzt den alten globalen llm_queue.pause() — statt alle LLM-Calls zu
blockieren, koennen einzelne Tasks (oder Preset-Gruppen) deaktiviert werden.
Ein deaktivierter Task wird vom Router als "kein LLM verfuegbar" behandelt
(resolve_llm liefert None, llm_call wirft RuntimeError). Aufrufer haben
bereits Fallbacks fuer diesen Fall.

Zwei Ebenen:
- Persistent:    `llm_task_state.disabled_tasks` in config.json
- Runtime-only:  in-memory Set, ueberschreibt persistent fuer die Session
                 (z.B. World-Dev-Builder aktiviert "world_dev"-Preset fuer
                 die Dauer der Session, ohne Config zu veraendern)
"""
from threading import RLock
from typing import List, Set

from app.core import config
from app.core.llm_tasks import TASK_TYPES
from app.core.log import get_logger

logger = get_logger("llm_task_state")


# Preset → Liste deaktivierter Tasks
PRESETS = {
    "background": [
        "social_reaction", "random_event", "thought", "secret_generation",
        "memory_consolidation", "consolidation", "relationship_summary",
        "outfit_generation", "image_comment",
    ],
    "world_dev": [
        # Alle Hintergrund-LLM-Aktivitaeten, die im World-Dev-Modus stoeren
        "social_reaction", "random_event", "thought", "secret_generation",
        "memory_consolidation", "consolidation", "relationship_summary",
        "instagram_caption", "image_comment", "image_prompt", "image_recognition",
        "image_analysis", "outfit_generation", "expression_map",
    ],
    "chat_only": [
        # alles ausser chat_stream, group_chat_stream und extraction
    ],
}


def _chat_only_disabled() -> List[str]:
    keep = {"chat_stream", "group_chat_stream", "extraction"}
    return [t for t in TASK_TYPES.keys() if t not in keep]


PRESETS["chat_only"] = _chat_only_disabled()


_lock = RLock()
_runtime_disabled: Set[str] = set()  # nicht persistent — World-Dev etc.


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_enabled(task: str) -> bool:
    """True wenn der Task aktuell weder persistent noch runtime deaktiviert ist."""
    with _lock:
        if task in _runtime_disabled:
            return False
    persisted = _persisted_disabled()
    return task not in persisted


def disabled_tasks() -> List[str]:
    """Liste aller aktuell deaktivierten Tasks (persistent + runtime, dedupliziert)."""
    persisted = _persisted_disabled()
    with _lock:
        combined = set(persisted) | set(_runtime_disabled)
    return sorted(combined)


def set_runtime_disabled(tasks: List[str]) -> None:
    """Setzt die Runtime-Disable-Liste (ueberschreibt bestehendes Set).

    Leere Liste = alle Runtime-Disables aufgehoben (persistente bleiben).
    """
    with _lock:
        _runtime_disabled.clear()
        _runtime_disabled.update(t for t in tasks if t in TASK_TYPES)
    logger.info("Runtime-disabled: %s", sorted(_runtime_disabled))


def activate_preset_runtime(preset: str) -> List[str]:
    """Aktiviert ein Preset als Runtime-Disable. Gibt die Task-Liste zurueck."""
    tasks = PRESETS.get(preset, [])
    set_runtime_disabled(tasks)
    return tasks


def clear_runtime() -> None:
    """Hebt alle Runtime-Disables auf."""
    with _lock:
        _runtime_disabled.clear()
    logger.info("Runtime-Disables aufgehoben")


def runtime_disabled_tasks() -> List[str]:
    with _lock:
        return sorted(_runtime_disabled)


def get_presets() -> dict:
    return {k: list(v) for k, v in PRESETS.items()}


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _persisted_disabled() -> Set[str]:
    """Liest die persistent deaktivierten Tasks aus der Config."""
    val = config.get("llm_task_state.disabled_tasks", [])
    if isinstance(val, list):
        return {str(t) for t in val if t}
    return set()
