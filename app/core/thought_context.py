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
    room_id = profile.get("current_room", "") or ""
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
        # Additional context — currently rendered in agent_thought_in_chat.md.
        # Agent_thought.md ignores them silently (no template reference).
        "effects_block": _build_effects_block(character_name),
        "recent_chat_block": _build_recent_chat_block(character_name),
        "outfit_self_block": _build_outfit_block(character_name, "Your outfit"),
        "outfit_avatar_block": _build_avatar_outfit_block(),
        "room_items_block": _build_room_items_block(location_id, room_id),
        "inventory_block": _build_inventory_block(character_name),
        "present_people_block": _build_present_people_block(character_name, location_id),
        "tracker_block": _build_tracker_block(character_name, location_id),
        "known_locations_block": _build_known_locations_block(character_name, location_id),
        "travel_block": _build_travel_block(character_name, location_id),
        "available_activities_block": _build_available_activities_block(character_name, location_id, room_id),
        "daily_schedule_block": _build_daily_schedule_block(character_name),
        "tools_hint": tools_hint,
        "has_assignments": False,  # set below if assignments_block non-empty
    }
    ctx["has_assignments"] = bool(ctx["assignments_block"])

    # State-driven filters: drop blocks + inject modifier text based on
    # active conditions/stats (drunk, exhausted, …). Replaces the old
    # rules-based effects_block path.
    try:
        from app.core.prompt_filters import apply_filters
        apply_filters(character_name, ctx, location_id=location_id)
    except Exception as e:
        logger.debug("prompt_filters apply failed for %s: %s", character_name, e)

    # has_assignments may have changed if the filter dropped assignments_block
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
        # Saved comments use the field "author" (instagram.add_comment); legacy
        # data may have "by" or "character" — check all three.
        comments = post.get("comments") or []
        already = any(
            c.get("author") == character_name
            or c.get("by") == character_name
            or c.get("character") == character_name
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

    Returns empty when ``retrospect_enabled`` is false for this character
    (per-char config or template feature) — the agent_thought.md template
    skips the block via ``{% if retrospective_block %}``.

    Otherwise: shows most recent reflections (so they influence decisions)
    and adds a "time to reflect" hint when the last Retrospect was >24h ago.
    """
    try:
        from app.models.character_template import is_feature_enabled
        if not is_feature_enabled(character_name, "retrospect_enabled"):
            return ""
        from app.skills.retrospect_skill import (
            get_beliefs_path, get_improvements_path,
            get_seed_beliefs_path, get_seed_improvements_path,
            load_recent_lines, get_last_retrospect_at)

        # Beliefs/Improvements aus zwei Quellen ziehen: manuell kurierte
        # Seed-Datei + Retrospect-Output. Seed zuerst (Hintergrund/Identitaet),
        # dann frische Retrospect-Eintraege (jüngste Reflexion).
        seed_b = load_recent_lines(get_seed_beliefs_path(character_name), limit=5)
        retro_b = load_recent_lines(get_beliefs_path(character_name), limit=5)
        beliefs = seed_b + retro_b
        seed_i = load_recent_lines(get_seed_improvements_path(character_name), limit=5)
        retro_i = load_recent_lines(get_improvements_path(character_name), limit=5)
        improvements = seed_i + retro_i
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


# ---------------------------------------------------------------------------
# In-Chat extras (also useful for the regular template)
# ---------------------------------------------------------------------------

def _build_effects_block(character_name: str) -> str:
    """Active status modifiers + danger conditions (drunk, exhausted, etc.).

    Reuses ``danger_system.build_status_prompt_section`` which already
    knows how to combine stat-based modifiers, danger levels and active
    conditions into a single prompt section.
    """
    try:
        from app.core.danger_system import build_status_prompt_section
        return (build_status_prompt_section(character_name) or "").strip()
    except Exception as e:
        logger.debug("effects block failed for %s: %s", character_name, e)
        return ""


def _build_recent_chat_block(character_name: str, limit: int = 3) -> str:
    """Last N chat messages between this character and their most recent
    chat partner (avatar OR another NPC via TalkTo).

    The thought turn doesn't carry chat history by default — when the
    character is mid-conversation we want them to see the latest exchanges
    so a follow-up thought can refer to actual content. Newest first.

    Partner-Aufloesung: wir verlassen uns NICHT auf ``get_active_character``
    (im AgentLoop-Background-Kontext oft leer und greift bei NPC↔NPC-
    TalkTo-Konversationen ohnehin nicht). Stattdessen lesen wir das
    Gegenueber direkt aus dem letzten ``chat_messages``-Eintrag dieses
    Characters — egal in welcher Storage-Richtung.

    Anschliessend ``UnifiedChatManager.get_chat_history`` mergen beide
    Storage-Richtungen (A,B)/(B,A) und dedupen Doppel-Eintraege.
    """
    try:
        from app.core.db import get_connection
        from app.models.unified_chat import UnifiedChatManager
        conn = get_connection()
        # Letzten Partner aus beiden Speicher-Richtungen finden
        row = conn.execute(
            "SELECT ts, partner, character_name FROM chat_messages "
            "WHERE character_name=? OR partner=? "
            "ORDER BY ts DESC LIMIT 1",
            (character_name, character_name),
        ).fetchone()
        if not row:
            return ""
        # Partner = die Seite die NICHT character_name ist
        ts, p_partner, p_char = row
        partner = p_partner if p_char == character_name else p_char
        if not partner:
            return ""
        history = UnifiedChatManager.get_chat_history(
            character_name=character_name, partner_name=partner)
        if not history:
            return ""
        recent = history[-limit:] if limit else history
        lines: List[str] = []
        for msg in recent:
            role = getattr(msg, "role", "") or ""
            content = (getattr(msg, "content", "") or "").strip()
            if not content:
                continue
            speaker = partner if role == "user" else character_name
            if len(content) > 400:
                content = content[:400].rstrip() + " […]"
            lines.append(f"  {speaker}: {content}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("recent_chat block failed for %s: %s", character_name, e)
        return ""


def _build_outfit_block(character_name: str, label: str) -> str:
    """Equipped outfit fragment for any character. Returns ``label: ...`` or ''."""
    try:
        from app.models.character import build_equipped_outfit_prompt
        raw = (build_equipped_outfit_prompt(character_name) or "").strip()
        if not raw:
            return ""
        # build_equipped_outfit_prompt returns "wearing: <fragments>". Strip
        # the prefix so we can apply our own label.
        if raw.lower().startswith("wearing:"):
            raw = raw[len("wearing:"):].strip()
        return f"{label}: {raw}"
    except Exception as e:
        logger.debug("outfit block failed for %s: %s", character_name, e)
        return ""


def _build_avatar_outfit_block() -> str:
    """Avatar outfit. Returns 'Avatar outfit (<name>): ...' or ''."""
    try:
        from app.models.account import get_active_character
        avatar = (get_active_character() or "").strip()
        if not avatar:
            return ""
        return _build_outfit_block(avatar, f"Avatar outfit ({avatar})")
    except Exception as e:
        logger.debug("avatar outfit block failed: %s", e)
        return ""


def _build_room_items_block(location_id: str, room_id: str) -> str:
    """Items present in the current room (visible only — hidden ones skipped).

    Format: bullet list of "name (xN) — short description". Cap at 8.
    """
    if not location_id or not room_id:
        return ""
    try:
        from app.models.inventory import get_room_items, get_item
        items = get_room_items(location_id, room_id) or []
        if not items:
            return ""
        lines: List[str] = []
        for ri in items:
            if ri.get("hidden"):
                continue
            iid = ri.get("item_id") or ""
            qty = ri.get("quantity", 1) or 1
            item = get_item(iid) or {}
            name = (item.get("name") or iid or "?").strip()
            desc = (item.get("description") or "").strip()
            if len(desc) > 80:
                desc = desc[:80].rstrip() + "…"
            qty_str = f" (x{qty})" if qty > 1 else ""
            line = f"- {name}{qty_str}"
            if desc:
                line += f" — {desc}"
            lines.append(line)
            if len(lines) >= 8:
                break
        return "\n".join(lines)
    except Exception as e:
        logger.debug("room_items block failed (%s/%s): %s", location_id, room_id, e)
        return ""


def _build_inventory_block(character_name: str) -> str:
    """Character's carried inventory (excludes equipped pieces). Cap at 8.

    ``get_character_inventory`` enriches each entry with ``item_name`` /
    ``item_description`` (resolved from the items table). Falls back to
    item_id only if the lookup failed.
    """
    try:
        from app.models.inventory import get_character_inventory
        inv = get_character_inventory(character_name, include_equipped=False) or {}
        items = inv.get("inventory") if isinstance(inv, dict) else inv
        if not items:
            return ""
        lines: List[str] = []
        for entry in items[:8]:
            iid = entry.get("item_id") or ""
            qty = entry.get("quantity", 1) or 1
            name = (entry.get("item_name") or iid or "?").strip()
            qty_str = f" (x{qty})" if qty > 1 else ""
            lines.append(f"- {name}{qty_str}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("inventory block failed for %s: %s", character_name, e)
        return ""


def _build_tracker_block(character_name: str, current_location_id: str) -> str:
    """Lines for each carried item with a `tracks_character` field.

    Reveals the tracked character's current location to the carrier — the
    in-world fiction is a magical amulet/ring/sigil that pinpoints another
    being. Skips items whose target is missing, equals self, or has no
    location yet. The agent decides what to do with the info; movement still
    requires SetLocation.
    """
    try:
        from app.models.inventory import get_character_inventory, get_item
        from app.models.character import get_character_current_location
        from app.models.world import get_location_name
        inv = get_character_inventory(character_name, include_equipped=True) or {}
        items = inv.get("inventory") if isinstance(inv, dict) else inv
        if not items:
            return ""
        lines: List[str] = []
        seen_targets: set = set()
        for entry in items:
            iid = entry.get("item_id") or ""
            if not iid:
                continue
            item = get_item(iid)
            if not item:
                continue
            target = (item.get("tracks_character") or "").strip()
            if not target or target == character_name:
                continue
            if target in seen_targets:
                continue
            seen_targets.add(target)
            target_loc = get_character_current_location(target) or ""
            item_name = (item.get("name") or iid).strip()
            if not target_loc:
                lines.append(f"- Your {item_name} reaches for {target}, but cannot find them right now.")
                continue
            if target_loc == current_location_id:
                lines.append(f"- Your {item_name} hums softly: {target} is here with you.")
                continue
            loc_name = get_location_name(target_loc) or target_loc
            lines.append(f"- Your {item_name} reveals: {target} is at {loc_name}.")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("tracker block failed for %s: %s", character_name, e)
        return ""


def _build_present_people_block(character_name: str, location_id: str) -> str:
    """Characters at the same location, excluding self. Avatar marked."""
    if not location_id:
        return ""
    try:
        from app.models.group_chat import get_characters_at_location
        from app.models.account import get_active_character
        avatar = (get_active_character() or "").strip()
        people = get_characters_at_location(location_id) or []
        names = []
        for p in people:
            n = (p.get("name") or "").strip()
            if not n or n == character_name:
                continue
            label = f"{n} (avatar)" if n == avatar else n
            names.append(label)
        if not names:
            return ""
        return ", ".join(names)
    except Exception as e:
        logger.debug("present_people block failed for %s: %s", character_name, e)
        return ""


def _build_travel_block(character_name: str, current_location_id: str) -> str:
    """Active journey info: target name + remaining steps via known path.

    Empty string when no movement_target is set. Communicates that the
    system handles the movement automatically and re-issuing SetLocation
    is only needed to change the destination.
    """
    try:
        from app.models.character import get_movement_target
        target_id = get_movement_target(character_name)
        if not target_id:
            return ""
        from app.models.world import (
            get_location_name, find_path_through_known)
        from app.models.character import get_known_locations
        target_name = get_location_name(target_id) or target_id
        known = get_known_locations(character_name) or []
        path = find_path_through_known(current_location_id, target_id, known) \
            if current_location_id else None
        if path and len(path) >= 2:
            steps = len(path) - 1
            return (f"You are travelling to {target_name}. "
                    f"{steps} step(s) remaining — the system moves you one "
                    f"grid-cell per tick. Re-using SetLocation only to change "
                    f"the destination.")
        if path and len(path) == 1:
            # already at target — will be cleared next tick
            return f"You have arrived at {target_name}."
        return (f"You wanted to travel to {target_name}, but the path through "
                f"known places is no longer reachable. The destination will "
                f"be cleared on the next tick.")
    except Exception as e:
        logger.debug("travel block failed for %s: %s", character_name, e)
        return ""


def _build_known_locations_block(character_name: str, current_location_id: str) -> str:
    """Visibility-filtered location list the character can travel to.

    Uses ``list_locations_for_character`` (respects knowledge-item gating).
    Filters out passable tiles (Durchgangsorte) — the LLM never picks them
    as travel targets, but the pathfinder traverses them when known.
    Marks the current location with a chevron so the LLM doesn't propose
    "moving" there. Cap at 12 locations to keep the prompt slim.
    """
    try:
        from app.models.world import list_locations_for_character
        locs = list_locations_for_character(character_name) or []
        if not locs:
            return ""
        lines: List[str] = []
        count = 0
        for loc in locs:
            if loc.get("passable"):
                continue
            if count >= 12:
                break
            lid = (loc.get("id") or "").strip()
            name = (loc.get("name") or lid or "?").strip()
            marker = " (you are here)" if lid and lid == current_location_id else ""
            lines.append(f"- {name}{marker}")
            count += 1
        return "\n".join(lines)
    except Exception as e:
        logger.debug("known_locations block failed for %s: %s", character_name, e)
        return ""


def _build_available_activities_block(character_name: str,
                                       location_id: str,
                                       room_id: str) -> str:
    """Activities the character can pick at the current location/room.

    Filters by conditions/cooldowns so we don't suggest unreachable ones.
    Cap at 10 to keep prompt slim. Activities marked ``auto_pick=false``
    are excluded — they're meant for explicit narrative triggers, not
    casual selection.
    """
    if not location_id:
        return ""
    try:
        from app.models.activity_library import get_available_activities
        acts = get_available_activities(
            character_name, location_id, room_id, filter_conditions=True) or []
        names: List[str] = []
        for a in acts:
            if a.get("auto_pick") is False:
                continue
            n = (a.get("name") or a.get("id") or "").strip()
            if n and n not in names:
                names.append(n)
            if len(names) >= 10:
                break
        if not names:
            return ""
        return ", ".join(names)
    except Exception as e:
        logger.debug("available_activities block failed for %s: %s", character_name, e)
        return ""


def _build_daily_schedule_block(character_name: str) -> str:
    """Soft hint about the character's typical rhythm at the current hour.

    Each slot now carries ``location`` and ``role`` (or ``sleep: true``).
    Hours without a slot are intentionally left blank — the agent is free
    to choose. Sleep stays a hint; the energy-based rule decides whether
    it actually triggers. Returns '' if there is no usable hint.
    """
    try:
        from app.models.character import get_character_daily_schedule
        schedule = get_character_daily_schedule(character_name) or {}
        if not schedule.get("enabled"):
            return ""
        slots = schedule.get("slots") or []
        if not slots:
            return ""

        loc_id_to_name: Dict[str, str] = {}
        try:
            from app.models.world import list_locations
            for loc in list_locations() or []:
                lid = (loc.get("id") or loc.get("name") or "").strip()
                lname = (loc.get("name") or lid).strip()
                if lid:
                    loc_id_to_name[lid] = lname
        except Exception:
            pass

        slot_by_hour: Dict[int, Dict[str, Any]] = {}
        for s in slots:
            try:
                h = int(s.get("hour"))
            except (TypeError, ValueError):
                continue
            slot_by_hour[h] = s

        now = datetime.now()
        cur_h = now.hour
        next_h = (cur_h + 1) % 24

        def _fmt(slot: Dict[str, Any], hour: int) -> str:
            if slot.get("sleep"):
                return f"  {hour:02d}:00 — you usually sleep around now"
            loc = (slot.get("location") or "").strip()
            role = (slot.get("role") or "").strip()
            parts = []
            if loc:
                parts.append(f"location: {loc_id_to_name.get(loc, loc)}")
            if role:
                parts.append(f"role: {role}")
            if not parts:
                return ""
            return f"  {hour:02d}:00 — " + ", ".join(parts)

        lines: List[str] = []
        cur = slot_by_hour.get(cur_h)
        if cur:
            line = _fmt(cur, cur_h)
            if line:
                lines.append(line)
        nxt = slot_by_hour.get(next_h)
        if nxt and nxt is not cur:
            line = _fmt(nxt, next_h)
            if line:
                lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        logger.debug("daily_schedule block failed for %s: %s", character_name, e)
        return ""
