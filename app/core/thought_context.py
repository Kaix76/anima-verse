"""Pre-decision data loader for the AgentLoop's slim thought prompt.

Gathers inbox / events / assignments / commitments / arc / outfit-trigger
data and formats each as a ready-to-render block string. The slim template
``shared/templates/llm/chat/agent_thought.md`` only emits a section when
its block is non-empty — so what we don't load here, the agent doesn't see.

Public API:
    build_thought_context(character_name, tools_hint='') -> dict

Returns a kwargs dict that can be passed straight into
``render('chat/agent_thought.md', **ctx)``.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List

from app.core.log import get_logger

logger = get_logger("thought_context")


# Window during which "you just moved" justifies an outfit-decision hint.
_OUTFIT_AFTER_LOCATION_MINUTES = 10
# Hours since last retrospect that count as "boost — time to reflect".
_RETROSPECT_BOOST_HOURS = 24


def build_thought_context(character_name: str, tools_hint: str = "") -> Dict[str, Any]:
    """Build the kwargs dict for ``chat/agent_thought.md``.

    Loads only what's needed: each block is computed lazily and only set
    when it has content. The template renders nothing for empty blocks.
    """
    from app.models.character import (
        get_character_profile, get_character_current_location)
    from app.models.world import get_location_name

    profile = get_character_profile(character_name)
    location_id = profile.get("current_location", "") or ""
    location_name = get_location_name(location_id) if location_id else "Unknown"

    ctx: Dict[str, Any] = {
        "character_name": character_name,
        "personality": (profile.get("character_personality", "") or "").strip(),
        "location_name": location_name,
        "activity": (profile.get("current_activity", "") or "None"),
        "feeling": (profile.get("current_feeling", "") or "Neutral"),
        "time_of_day": datetime.now().strftime("%H:%M"),
        # Defaults for optional blocks — keep them present so StrictUndefined
        # doesn't raise on missing keys.
        "inbox_block": _build_inbox_block(character_name),
        "events_block": _build_events_block(location_id),
        "assignments_block": _build_assignments_block(character_name),
        "general_task": _build_general_task(profile),
        "commitments_block": _build_commitments_block(character_name),
        "outfit_decision_block": _build_outfit_decision_block(character_name),
        "arc_block": _build_arc_block(character_name),
        "retrospective_block": _build_retrospective_block(character_name),
        "instagram_pending_block": _build_instagram_pending_block(character_name),
        "tools_hint": tools_hint,
        "has_assignments": False,  # set below if assignments_block non-empty
    }
    ctx["has_assignments"] = bool(ctx["assignments_block"])
    return ctx


# ---------------------------------------------------------------------------
# Block builders
# ---------------------------------------------------------------------------

def _build_inbox_block(character_name: str) -> str:
    """Pre-formatted inbox block: per-sender unread messages with context."""
    try:
        from app.core.agent_inbox import load_unread_messages
        unread = load_unread_messages(character_name,
            max_per_sender=3, context_messages=2)
        if not unread:
            return ""
        lines: List[str] = []
        for sender, msgs in unread.items():
            lines.append(f"From {sender}:")
            for m in msgs:
                marker = "[NEW]" if m.get("unread") else "[seen]"
                role = m.get("role", "")
                # Speaker label: 'user' role = the sender; 'assistant' = self
                speaker = sender if role == "user" else character_name
                content = (m.get("content") or "").strip()
                if not content:
                    continue
                # Truncate very long messages so the prompt stays slim.
                if len(content) > 400:
                    content = content[:400].rstrip() + " […]"
                lines.append(f"  {marker} {speaker}: {content}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("inbox block failed for %s: %s", character_name, e)
        return ""


def _build_events_block(location_id: str) -> str:
    """Active events at the character's location."""
    if not location_id:
        return ""
    try:
        from app.models.events import build_events_prompt_section
        return (build_events_prompt_section(location_id=location_id) or "").strip()
    except Exception as e:
        logger.debug("events block failed: %s", e)
        return ""


def _build_assignments_block(character_name: str) -> str:
    """Active assignments for this character."""
    try:
        from app.models.assignments import build_assignment_prompt_section
        return (build_assignment_prompt_section(character_name) or "").strip()
    except Exception as e:
        logger.debug("assignments block failed for %s: %s", character_name, e)
        return ""


def _build_general_task(profile: Dict[str, Any]) -> str:
    """Static general task from the character profile (long-running purpose)."""
    return (profile.get("character_task", "") or "").strip()


def _build_commitments_block(character_name: str) -> str:
    """Open commitments — promises this character made and hasn't fulfilled."""
    try:
        from app.models.memory import load_memories
        memories = load_memories(character_name)
        open_ones = [
            m for m in memories
            if m.get("memory_type") == "commitment"
            and "completed" not in (m.get("tags") or [])
        ]
        if not open_ones:
            return ""
        # Newest first, cap at 5 to keep prompt slim.
        open_ones.sort(key=lambda m: m.get("timestamp", ""), reverse=True)
        lines = []
        for m in open_ones[:5]:
            content = (m.get("content") or "").strip()
            if not content:
                continue
            delay = (m.get("delay") or "").strip()
            suffix = f" (when: {delay})" if delay else ""
            lines.append(f"- {content}{suffix}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("commitments block failed for %s: %s", character_name, e)
        return ""


def _build_outfit_decision_block(character_name: str) -> str:
    """Outfit-decision hint when:
      a) location changed within the last N minutes, OR
      b) the agent just woke up (activity changed away from 'Sleeping'
         within the last N minutes).

    Both signal "you're in a new context — the outfit you have on may
    not fit". The agent is free to ignore the hint via SKIP.
    """
    try:
        from app.core.db import get_connection
        conn = get_connection()
        row = conn.execute(
            "SELECT location_changed_at, activity_changed_at, current_activity "
            "FROM character_state WHERE character_name=?",
            (character_name,),
        ).fetchone()
        if not row:
            return ""
        loc_changed_at, activity_changed_at, current_activity = row
        now = datetime.now()
        cur_activity_lc = (current_activity or "").strip().lower()

        # (a) Recent location change
        if loc_changed_at:
            try:
                changed = datetime.fromisoformat(loc_changed_at)
                if now - changed <= timedelta(minutes=_OUTFIT_AFTER_LOCATION_MINUTES):
                    return (
                        "You recently changed location. Consider whether your "
                        "current outfit still fits the new context — if not, "
                        "use OutfitChange.")
            except (ValueError, TypeError):
                pass

        # (b) Recent wake-up: activity transitioned AWAY from Sleeping. We
        # detect by checking the most recent state_history activity entries
        # — if the previous activity was Sleeping and the change was within
        # the wake-up window, signal an outfit decision.
        if cur_activity_lc != "sleeping" and activity_changed_at:
            try:
                changed = datetime.fromisoformat(activity_changed_at)
            except (ValueError, TypeError):
                changed = None
            if changed and now - changed <= timedelta(minutes=_OUTFIT_AFTER_LOCATION_MINUTES * 2):
                # Look at the previous activity in state_history.
                try:
                    prev = conn.execute(
                        "SELECT state_json FROM state_history "
                        "WHERE character_name=? AND ts < ? "
                        "ORDER BY ts DESC LIMIT 5",
                        (character_name, activity_changed_at),
                    ).fetchall()
                    import json as _json
                    for (sj,) in prev:
                        try:
                            d = _json.loads(sj or "{}")
                        except Exception:
                            continue
                        if d.get("type") == "activity":
                            prev_val = (d.get("value") or "").strip().lower()
                            if prev_val == "sleeping":
                                return (
                                    "You just woke up. Consider whether your "
                                    "sleepwear still fits the day ahead — if "
                                    "not, use OutfitChange.")
                            break  # only check the most recent activity
                except Exception:
                    pass

        return ""
    except Exception as e:
        logger.debug("outfit-decision block failed for %s: %s", character_name, e)
        return ""


def _build_instagram_pending_block(character_name: str) -> str:
    """Recent Instagram posts the agent might want to comment on / reply to.

    Window length: ``skills.instagram.pending_window_hours`` from admin
    config (default 4). Excludes the agent's own posts. Limits to the
    5 newest within the window. If the agent already commented on a
    post, it's skipped to avoid re-comment spam.
    """
    try:
        from app.core import config as _cfg
        window_h = int((_cfg.get("skills.instagram.pending_window_hours") or 4))
    except Exception:
        window_h = 4
    try:
        from app.models.instagram import load_feed
        feed = load_feed() or []
    except Exception as e:
        logger.debug("instagram_pending feed load failed: %s", e)
        return ""
    if not feed:
        return ""

    cutoff = datetime.now() - timedelta(hours=window_h)
    relevant = []
    for post in feed:
        ts = post.get("timestamp", "") or ""
        try:
            post_dt = datetime.fromisoformat(ts.replace("Z", ""))
        except Exception:
            continue
        if post_dt < cutoff:
            continue
        if post.get("agent_name") == character_name:
            continue
        # Skip if this character already commented on the post.
        comments = post.get("comments") or []
        already = any(c.get("by") == character_name or c.get("character") == character_name
                      for c in comments)
        if already:
            continue
        relevant.append((post_dt, post))

    if not relevant:
        return ""
    relevant.sort(key=lambda x: x[0], reverse=True)
    relevant = relevant[:5]

    lines = []
    for _, post in relevant:
        poster = post.get("agent_name", "?")
        post_id = post.get("post_id") or post.get("id") or ""
        caption = (post.get("caption") or "").strip()
        if len(caption) > 140:
            caption = caption[:140].rstrip() + "…"
        # Try to surface image_analysis when available — gives the agent
        # something concrete to react to without us shipping the actual
        # image (vision-LLM already did that earlier).
        analysis = ""
        meta = post.get("image_meta") or {}
        if isinstance(meta, dict):
            analysis = (meta.get("image_analysis") or "").strip()
        line = f"- [{post_id}] {poster}: \"{caption}\""
        if analysis:
            if len(analysis) > 140:
                analysis = analysis[:140].rstrip() + "…"
            line += f"\n    Image: {analysis}"
        lines.append(line)
    return "Recent Instagram posts you haven't reacted to yet:\n" + "\n".join(lines)


def _build_arc_block(character_name: str) -> str:
    """Active story arc context (low priority)."""
    try:
        from app.core.story_engine import get_story_engine
        return (get_story_engine().inject_arc_context(character_name) or "").strip()
    except Exception as e:
        logger.debug("arc block failed for %s: %s", character_name, e)
        return ""


def _build_retrospective_block(character_name: str) -> str:
    """Existing beliefs/improvements + a hint to reflect when overdue.

    Always shows the most recent reflections (so they actually influence
    decisions) and adds a "time to reflect" hint when the character hasn't
    done a Retrospect in over 24h. The hint surfaces the option to the LLM
    without forcing it.
    """
    try:
        from app.skills.retrospect_skill import (
            get_beliefs_path, get_improvements_path,
            load_recent_lines, get_last_retrospect_at)

        beliefs = load_recent_lines(get_beliefs_path(character_name), limit=5)
        improvements = load_recent_lines(get_improvements_path(character_name), limit=5)
        last_at = get_last_retrospect_at(character_name)

        overdue = True
        if last_at:
            try:
                last_dt = datetime.fromisoformat(last_at)
                overdue = datetime.now() - last_dt > timedelta(hours=_RETROSPECT_BOOST_HOURS)
            except (ValueError, TypeError):
                pass

        lines: List[str] = []
        if beliefs:
            lines.append("Your beliefs so far:")
            lines.extend(f"  {b}" for b in beliefs)
        if improvements:
            lines.append("Improvement intentions on record:")
            lines.extend(f"  {i}" for i in improvements)
        if overdue:
            lines.append("(It's been a while since you last reflected — consider Retrospect.)")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("retrospective block failed for %s: %s", character_name, e)
        return ""
