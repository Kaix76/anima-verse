"""Outfit-Change Event-Bus.

Pub/Sub fuer `apply_equipped_pieces`-Changes damit offene UIs per SSE
sofort refreshen koennen — auch wenn der Wechsel aus einem Scheduler-
oder Skill-Hintergrundthread kommt.

Thread-safe: Publisher koennen aus Sync- oder Async-Kontext kommen.
Subscriber sind asyncio-basierte SSE-Handler mit eigenem Loop.
"""

import asyncio
import threading
from typing import Any, AsyncIterator, Dict, List, Tuple

from app.core.log import get_logger

logger = get_logger("outfit_events")

# Global subscriber list (single-world model)
_subscribers: List[Tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = []
_lock = threading.Lock()


def publish(character_name: str, source: str = "") -> None:
    """Broadcast outfit change to all subscribers.

    Safe to call from threads or async code. Lost events when no one is
    subscribed — that is fine; the next Browser-Load refreshes anyway.
    """
    if not character_name:
        return
    event = {"character": character_name, "source": source or ""}
    with _lock:
        subs = list(_subscribers)
    for q, loop in subs:
        try:
            loop.call_soon_threadsafe(q.put_nowait, event)
        except RuntimeError:
            # Loop closed — subscriber wird beim naechsten iter ausgemustert.
            pass
        except Exception as e:
            logger.debug("publish [%s]: %s", character_name, e)


async def subscribe() -> AsyncIterator[Dict[str, Any]]:
    """Async-Iterator ueber Outfit-Events.

    Muss im Event-Loop des SSE-Handlers verbraucht werden; registriert
    sich und raeumt beim Beenden auf.
    """
    loop = asyncio.get_running_loop()
    q: asyncio.Queue = asyncio.Queue()
    entry = (q, loop)
    with _lock:
        _subscribers.append(entry)
    try:
        while True:
            event = await q.get()
            yield event
    finally:
        with _lock:
            if entry in _subscribers:
                _subscribers.remove(entry)
