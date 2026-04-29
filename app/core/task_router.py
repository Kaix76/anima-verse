"""Task Router — resolves informational queue labels for background tasks.

With the unified channel system, actual task routing happens via
ProviderManager.find_channel(). This module only provides informational
queue_name labels for UI display and task tracking.
"""
from typing import Optional

from app.core.log import get_logger

logger = get_logger("task_router")


def resolve_queue(
    task_type: str,
    payload: dict = None,
    agent_name: str = "") -> str:
    """Returns an informational queue label for a background task.

    This does NOT affect routing — channels handle that dynamically.
    The label is stored in TaskQueue DB for UI display.
    """
    return "background"


def match_queue_name(name: str) -> Optional[str]:
    """Resolve a provider/channel name to a channel key.

    Handles formats: "Provider:gpuN" (exact), "Provider:N" → "Provider:gpuN",
    "Provider" → first matching channel.
    """
    if not name:
        return None
    try:
        from app.core.provider_manager import get_provider_manager
        pm = get_provider_manager()
        # Exact channel match
        if name in pm.channels:
            return name
        # "Provider:N" → "Provider:gpuN"
        if ":" in name:
            parts = name.split(":", 1)
            gpu_key = f"{parts[0]}:gpu{parts[1]}"
            if gpu_key in pm.channels:
                return gpu_key
        # Provider name → first channel for this provider
        for key in pm.channels:
            prov = key.split(":")[0] if ":" in key else key
            if prov == name:
                return key
    except Exception:
        pass
    return None


def invalidate_cache() -> None:
    """No-op — kept for backward compatibility."""
    pass
