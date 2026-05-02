"""Agent inbox built on top of the unified chat_messages table.

When character A sends a message to character B (TalkTo / SendMessage),
the message is recorded twice:
  - in B's chat history with partner=A, role='user'  (B's perspective: incoming)
  - in A's chat history with partner=B, role='assistant'  (A's perspective: outgoing)

The "inbox" of B is then any chat_messages row with character_name=B,
role='user', ts > last_thought_at(B). No separate table — the chat history
IS the inbox, which keeps history consistent across UI and prompt.

`last_thought_at` lives in `character_state.last_thought_at` and is set to
``now()`` every time the agent loop completes a thought turn for that
character. Skipped/idle turns also bump the timestamp so unread does not
pile up indefinitely.

Public API:
    get_last_thought_at(character_name) -> str   # ISO timestamp, "" if never
    mark_thought_processed(character_name)
    load_unread_messages(character_name, max_per_sender=3, context_messages=2)
        -> dict[sender_name, list[Message]]
"""
from datetime import datetime
from typing import Dict, List

from app.core.db import get_connection, transaction
from app.core.log import get_logger

logger = get_logger("agent_inbox")


def get_last_thought_at(character_name: str) -> str:
    """Return ISO timestamp of the last processed thought, or '' if never."""
    if not character_name:
        return ""
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT last_thought_at FROM character_state WHERE character_name=?",
            (character_name,),
        ).fetchone()
        return (row[0] or "") if row else ""
    except Exception as e:
        logger.debug("get_last_thought_at(%s) failed: %s", character_name, e)
        return ""


def mark_thought_processed(character_name: str, ts: str = "") -> None:
    """Stamp ``last_thought_at`` for an agent. Bump even if no action was taken
    so the inbox does not accumulate unread state from skipped turns."""
    if not character_name:
        return
    ts = ts or datetime.now().isoformat()
    try:
        with transaction() as conn:
            # UPSERT: character_state row may not exist yet for fresh characters.
            conn.execute("""
                INSERT INTO character_state (character_name, last_thought_at)
                VALUES (?, ?)
                ON CONFLICT(character_name) DO UPDATE SET
                    last_thought_at=excluded.last_thought_at
            """, (character_name, ts))
    except Exception as e:
        logger.error("mark_thought_processed(%s) failed: %s", character_name, e)


def load_unread_messages(character_name: str,
    max_per_sender: int = 3,
    context_messages: int = 2) -> Dict[str, List[Dict]]:
    """Return unread incoming messages per sender, with a small context tail.

    Args:
        character_name: the recipient
        max_per_sender: keep at most this many unread messages per sender
            (newest kept) — protects against runaway prompt growth
        context_messages: include up to this many already-read messages
            preceding the unread block, so the recipient sees the thread
            context rather than just the latest blurt

    Returns:
        ``{sender_name: [message_dict, ...]}`` where each message dict has
        ``role``, ``content``, ``ts``, ``unread`` (bool). Messages are ordered
        oldest-first; unread ones come last in each list.
    """
    if not character_name:
        return {}

    cutoff = get_last_thought_at(character_name)

    try:
        conn = get_connection()
        # Find senders with unread incoming messages (role='user') after cutoff.
        if cutoff:
            sender_rows = conn.execute("""
                SELECT DISTINCT partner FROM chat_messages
                WHERE character_name=? AND role='user' AND ts > ?
            """, (character_name, cutoff)).fetchall()
        else:
            sender_rows = conn.execute("""
                SELECT DISTINCT partner FROM chat_messages
                WHERE character_name=? AND role='user'
            """, (character_name,)).fetchall()
        senders = [r[0] for r in sender_rows if r[0]]
        if not senders:
            return {}

        result: Dict[str, List[Dict]] = {}
        for sender in senders:
            # Pull recent messages with this partner — both directions.
            rows = conn.execute("""
                SELECT ts, role, content FROM chat_messages
                WHERE character_name=? AND partner=?
                ORDER BY ts DESC
                LIMIT ?
            """, (character_name, sender, max_per_sender + context_messages + 4)).fetchall()
            # Reverse to oldest-first for natural reading order.
            msgs = []
            for ts, role, content in reversed(rows):
                msgs.append({
                    "ts": ts,
                    "role": role,
                    "content": content,
                    "unread": bool(role == "user" and (not cutoff or ts > cutoff)),
                })
            # Trim: take last (context_messages + max_per_sender) entries.
            keep = context_messages + max_per_sender
            if len(msgs) > keep:
                msgs = msgs[-keep:]
            result[sender] = msgs
        return result
    except Exception as e:
        logger.error("load_unread_messages(%s) failed: %s", character_name, e)
        return {}
