"""Live template preview — render any MD template under
``shared/templates/llm/`` against real character data **using the same
production code paths**.

Approach
--------
For task templates we invoke the actual production function that owns
the template, with a monkey-patched ``llm_call`` that captures the
``(system_prompt, user_prompt)`` arguments and aborts the call before
any side effects (LLM, save/delete) run.

This guarantees the preview matches what production would build — if
production code changes how it gathers data, the preview reflects that
automatically without parallel maintenance.

The capture exception is a ``BaseException`` subclass so that
``except Exception`` blocks inside production code do not swallow it.

Public API
----------
    list_templates() -> list[dict]
    read_template(rel_path) -> str
    save_template(rel_path, content) -> None
    render_with_real_data(rel_path, agent, avatar) -> dict
        {ok, output, note}
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from app.core.log import get_logger

logger = get_logger("template_preview")


_TEMPLATE_ROOT = Path(__file__).resolve().parents[2] / "shared" / "templates" / "llm"


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------

def list_templates() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in sorted(_TEMPLATE_ROOT.rglob("*.md")):
        rel = p.relative_to(_TEMPLATE_ROOT).as_posix()
        kind = rel.split("/", 1)[0] if "/" in rel else ""
        out.append({
            "path": rel,
            "kind": kind,
            "has_preview": rel in _PREVIEW_DRIVERS,
        })
    return out


def _resolve_safe(rel_path: str) -> Path:
    p = (_TEMPLATE_ROOT / rel_path).resolve()
    if not str(p).startswith(str(_TEMPLATE_ROOT.resolve())):
        raise ValueError(f"Path escapes template root: {rel_path}")
    if p.suffix != ".md":
        raise ValueError(f"Only .md files allowed: {rel_path}")
    return p


def read_template(rel_path: str) -> str:
    p = _resolve_safe(rel_path)
    if not p.exists():
        raise FileNotFoundError(rel_path)
    return p.read_text(encoding="utf-8")


def save_template(rel_path: str, content: str) -> None:
    p = _resolve_safe(rel_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    try:
        from app.core.prompt_templates import _env, _invalidate_skill_meta_cache
        _env.cache.clear()
        _invalidate_skill_meta_cache()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Capture mechanism — intercepts llm_call inside production functions
# ---------------------------------------------------------------------------

class _Captured(BaseException):
    """Sentinel raised by the patched ``llm_call``. Subclasses
    BaseException so production ``except Exception:`` blocks don't
    swallow it."""
    def __init__(self, task: str, system_prompt: str, user_prompt: str):
        self.task = task
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt


def _capturing_llm_call(task=None, system_prompt="", user_prompt="", **kwargs):
    raise _Captured(task or kwargs.get("task", ""),
                    system_prompt or kwargs.get("system_prompt", ""),
                    user_prompt or kwargs.get("user_prompt", ""))


@contextmanager
def _capture_llm_call():
    """Monkey-patch app.core.llm_router.llm_call. Production functions
    do lazy imports of llm_call inside their bodies, so patching the
    module attribute reaches them on the next call."""
    import app.core.llm_router as lr
    orig = lr.llm_call
    lr.llm_call = _capturing_llm_call
    try:
        yield
    finally:
        lr.llm_call = orig


def _capture_render(production_call: Callable[[], Any]) -> Tuple[str, str, str]:
    """Run a production callable, capture its first llm_call.

    Returns (task, system_prompt, user_prompt). Raises if the call
    completed without invoking llm_call (i.e. early return / no LLM).
    """
    with _capture_llm_call():
        try:
            production_call()
        except _Captured as c:
            return c.task, c.system_prompt, c.user_prompt
    raise RuntimeError(
        "Production function returned without invoking llm_call — "
        "either no LLM is needed for the inputs (early-out) or the call "
        "site uses llm_queue.submit() directly (vision LLMs).")


def _format(task: str, system_prompt: str, user_prompt: str) -> str:
    if system_prompt.strip():
        return f"## task: {task}\n\n## system\n{system_prompt}\n\n## user\n{user_prompt}"
    return f"## task: {task}\n\n## user\n{user_prompt}"


# ---------------------------------------------------------------------------
# Per-template drivers — each invokes the real production function
# ---------------------------------------------------------------------------

PreviewResult = Dict[str, Any]
PreviewDriver = Callable[[str, str], PreviewResult]


def _last_exchange(agent: str, partner: str) -> Tuple[str, str]:
    try:
        from app.models.chat import get_chat_history
        history = get_chat_history(agent, partner_name=partner) or []
        last_user = ""
        last_assistant = ""
        for m in reversed(history):
            role = m.get("role", "") if isinstance(m, dict) else getattr(m, "role", "")
            content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
            if not last_assistant and role == "assistant":
                last_assistant = content
            if not last_user and role == "user":
                last_user = content
            if last_user and last_assistant:
                break
        return last_user, last_assistant
    except Exception as e:
        logger.debug("_last_exchange failed: %s", e)
        return "", ""


# --- chat/agent_thought.md (production: AgentLoop._run_turn) --------------

def _drive_agent_thought(agent: str, avatar: str) -> PreviewResult:
    """Same path as AgentLoop._run_turn: build_thought_context + render."""
    return _drive_agent_thought_template(agent, "chat/agent_thought.md")


def _drive_agent_thought_in_chat(agent: str, avatar: str) -> PreviewResult:
    """In-chat-Variante (10–30min seit letztem Chat-Turn). Selbe Datenbasis."""
    return _drive_agent_thought_template(agent, "chat/agent_thought_in_chat.md")


def _drive_agent_thought_template(agent: str, template_rel: str) -> PreviewResult:
    from app.core.thought_context import build_thought_context
    from app.core.prompt_templates import render
    tools_hint = "# Available tools: SendMessage, TalkTo, Retrospect, OutfitChange, SetActivity, SetLocation, ImageGeneration"
    ctx = build_thought_context(agent, tools_hint=tools_hint)
    out = render(template_rel, **ctx)
    return {"ok": True, "output": out,
            "note": (f"Production path: agent_loop.AgentLoop._run_turn → "
                     f"thought_context.build_thought_context → render('{template_rel}'). "
                     f"Avatar selection is unused by this template.")}


# --- chat/chat_stream.md (production: _build_full_system_prompt) ----------

def _drive_chat_stream(agent: str, avatar: str) -> PreviewResult:
    """Same path as routes/chat.py uses to build the chat-stream prompt.

    Liefert System-Prompt + messages-Array (recent_history nach Gap-Cut).
    Die Summary wird aus dem Cache gelesen — kein synchroner LLM-Refresh,
    auch wenn die Summary stale ist (Hinweis im note-Feld).
    """
    from app.routes.chat import _build_full_system_prompt
    from app.models.character import get_character_config
    from app.models.chat import get_chat_history
    from app.utils.history_manager import (
        get_time_based_history, get_cached_summary,
        _summary_updated_at, get_memory_thresholds)
    from datetime import datetime

    cfg = get_character_config(agent)

    # Recent_history wie im Production-Pfad rechnen (inkl. Session-Gap-Cut)
    full_history = get_chat_history(agent, partner_name=avatar) or []
    recent, old = get_time_based_history(full_history)
    summary = get_cached_summary(agent) if old else ""

    # Stale-Hinweis: wuerde Production einen synchronen Refresh ausloesen?
    stale_hint = ""
    if old:
        updated_at = _summary_updated_at(agent)
        newest_old = None
        for m in old:
            ts = m.get("timestamp") or ""
            try:
                t = datetime.fromisoformat(ts)
                if newest_old is None or t > newest_old:
                    newest_old = t
            except (ValueError, TypeError):
                continue
        if not summary:
            stale_hint = "⚠ Cached Summary leer — beim Chat wird sie sync generiert."
        elif updated_at and newest_old and newest_old > updated_at:
            stale_hint = (f"⚠ Cached Summary deckt nicht alle old_messages ab "
                          f"(newest_old {newest_old.isoformat(timespec='minutes')} > "
                          f"updated_at {updated_at.isoformat(timespec='minutes')}). "
                          f"Beim Chat wird sie sync regeneriert.")

    sys_prompt = _build_full_system_prompt(
        character_name=agent,
        lang_instruction="Respond in English.",
        history_summary=summary,
        tools_enabled=False,
        agent_config=cfg,
        channel="web",
        partner_override=avatar,
        medium="in_person",
    )

    # Messages-Block formatieren
    gap_h = get_memory_thresholds().get("session_gap_hours", 4)
    parts = [f"## task: chat_stream\n\n## system\n{sys_prompt}"]
    if recent:
        parts.append(
            f"\n\n## messages ({len(recent)} turn{'s' if len(recent) != 1 else ''} "
            f"nach Session-Gap-Cut > {gap_h}h)")
        for i, m in enumerate(recent, 1):
            role = m.get("role", "?")
            ts = m.get("timestamp", "")
            ts_short = ts[:16] if ts else "no-ts"
            parts.append(f"\n--- [{i}] {role} @ {ts_short} ---\n{m.get('content', '')}")
    else:
        parts.append("\n\n## messages\n(empty — keine Turns nach Gap-Cut)")

    note_lines = [
        f"Production path: routes/chat._build_full_system_prompt(agent={agent!r}, "
        f"partner_override={avatar!r}, medium='in_person', tools_enabled=False).",
        f"Recent: {len(recent)} turn(s), Old (in Summary): {len(old)} turn(s).",
    ]
    if stale_hint:
        note_lines.append(stale_hint)

    return {"ok": True, "output": "".join(parts),
            "note": " ".join(note_lines)}


# --- task drivers via capture ---------------------------------------------

def _drive_extraction_memory(agent: str, avatar: str) -> PreviewResult:
    user_msg, asst_msg = _last_exchange(agent, avatar)
    if not user_msg:
        return {"ok": False, "output": "",
                "note": "Need at least one user message in the chat history "
                        f"between {avatar!r} and {agent!r}."}
    from app.core.memory_service import extract_memories_from_exchange
    task, sys, user = _capture_render(
        lambda: extract_memories_from_exchange(agent, avatar, user_msg, asst_msg, llm=None))
    return {"ok": True, "output": _format(task, sys, user),
            "note": "Production: memory_service.extract_memories_from_exchange "
                    "with the latest avatar↔agent exchange."}


def _drive_consolidation_daily(agent: str, avatar: str) -> PreviewResult:
    from app.core.memory_service import _consolidate_episodics_to_daily
    task, sys, user = _capture_render(lambda: _consolidate_episodics_to_daily(agent))
    return {"ok": True, "output": _format(task, sys, user),
            "note": "Production: memory_service._consolidate_episodics_to_daily."}


def _drive_consolidation_weekly(agent: str, avatar: str) -> PreviewResult:
    from app.core.memory_service import _consolidate_daily_to_weekly
    task, sys, user = _capture_render(lambda: _consolidate_daily_to_weekly(agent))
    return {"ok": True, "output": _format(task, sys, user),
            "note": "Production: memory_service._consolidate_daily_to_weekly."}


def _drive_consolidation_monthly(agent: str, avatar: str) -> PreviewResult:
    from app.core.memory_service import _consolidate_weekly_to_monthly
    task, sys, user = _capture_render(lambda: _consolidate_weekly_to_monthly(agent))
    return {"ok": True, "output": _format(task, sys, user),
            "note": "Production: memory_service._consolidate_weekly_to_monthly."}


def _drive_consolidation_today(agent: str, avatar: str) -> PreviewResult:
    """history_manager._create_daily_summary needs a list of messages.
    Build it the same way history_manager would build today's slice."""
    try:
        from app.models.chat import get_chat_history
        history = get_chat_history(agent, partner_name=avatar) or []
    except Exception:
        history = []
    today = datetime.now().date().isoformat()
    todays = []
    for m in history:
        ts = m.get("timestamp", "") if isinstance(m, dict) else getattr(m, "timestamp", "")
        if ts.startswith(today):
            todays.append(m)
    if not todays:
        return {"ok": False, "output": "",
                "note": f"No chat messages from today ({today}) between {avatar!r} and {agent!r}."}
    from app.utils.history_manager import _create_daily_summary
    task, sys, user = _capture_render(lambda: _create_daily_summary(todays, agent))
    return {"ok": True, "output": _format(task, sys, user),
            "note": "Production: history_manager._create_daily_summary "
                    "with today's avatar↔agent transcript."}


def _drive_consolidation_history_summary(agent: str, avatar: str) -> PreviewResult:
    try:
        from app.models.chat import get_chat_history
        history = get_chat_history(agent, partner_name=avatar) or []
    except Exception:
        history = []
    if not history:
        return {"ok": False, "output": "",
                "note": "No chat history between selected avatar and agent."}
    from app.utils.history_manager import create_summary
    task, sys, user = _capture_render(lambda: create_summary(history[-30:], agent))
    return {"ok": True, "output": _format(task, sys, user),
            "note": "Production: history_manager.create_summary with last 30 msgs."}


def _drive_consolidation_daily_diary(agent: str, avatar: str) -> PreviewResult:
    """routes/diary._generate_summary_sync expects pre-built day_text.
    Build it via diary.build_daily_summary_input."""
    try:
        from app.models.diary import build_daily_summary_input
        day = datetime.now().date().isoformat()
        day_text = build_daily_summary_input(agent, day) or ""
    except Exception as e:
        return {"ok": False, "output": "",
                "note": f"build_daily_summary_input failed: {e}"}
    if not day_text:
        return {"ok": False, "output": "",
                "note": "No events for today — nothing to diary about."}
    from app.routes.diary import _generate_summary_sync
    task, sys, user = _capture_render(lambda: _generate_summary_sync(agent, "", day_text))
    return {"ok": True, "output": _format(task, sys, user),
            "note": "Production: routes/diary._generate_summary_sync with today's diary input."}


def _drive_relationship_summary_pair(agent: str, avatar: str) -> PreviewResult:
    """Find a relationship-memory entry (agent → some other char) and
    let production._generate_summary build the prompt for it."""
    from app.models.memory import load_memories
    entries = load_memories(agent) or []
    rel = next((e for e in entries
                if "relationship" in (e.get("tags") or [])
                and (e.get("content") or "").strip()
                and e.get("related_character")), None)
    if not rel:
        return {"ok": False, "output": "",
                "note": "Agent has no relationship-memory entries to summarize."}
    from app.core.relationship_summary import _generate_summary
    task, sys, user = _capture_render(
        lambda: _generate_summary(agent, rel["related_character"],
                                   rel["content"], rel.get("summary", "")))
    return {"ok": True, "output": _format(task, sys, user),
            "note": "Production: relationship_summary._generate_summary "
                    f"for {agent} → {rel['related_character']}."}


def _drive_relationship_summary_romantic_interests(agent: str, avatar: str) -> PreviewResult:
    """models/relationship.extract_romantic_interests early-exits if any
    character already has the field populated. We can't easily preview it
    without hacking. Document the limitation."""
    return {"ok": False, "output": "",
            "note": "Preview unavailable: extract_romantic_interests is a "
                    "one-time bootstrap that early-exits when ANY character "
                    "already has romantic_interests set. See production "
                    "code in models/relationship.py for the exact prompt "
                    "build (kwargs assembled inline before llm_call)."}


def _drive_classify_activity(agent: str, avatar: str) -> PreviewResult:
    """activity_engine has a complex async classifier. The prompt build
    happens inside _do_classify which runs in an executor. We invoke it
    by triggering save_character_current_activity which runs the
    classifier — capture intercepts the llm_call before it fires."""
    raw = "drinking coffee at the cafe"
    # _do_classify is nested inside save_character_current_activity. Call
    # the inner classifier directly via the activity_engine module's
    # function to avoid touching the agent's stored state.
    try:
        # Build the prompt path manually by re-invoking what the
        # production classifier does — there is no top-level helper.
        # Best we can do is replicate the exact kwargs gathering from
        # activity_engine._do_classify and then call render_task.
        from app.models.activity_library import get_available_activities
        from app.models.character import get_character_current_location
        loc_id = get_character_current_location(agent) or ""
        all_acts = get_available_activities(agent, loc_id) or []
        known_lines = []
        for a in all_acts[:25]:
            n, d = a.get("name", ""), a.get("description", "")
            known_lines.append(f"- {n}: {d}" if (n and d) else f"- {n}")
        known_list = "\n".join(known_lines) if known_lines else "(none)"
        from app.core.prompt_templates import render_task
        sys, user = render_task("classify_activity",
            raw_activity=raw, known_list=known_list)
        return {"ok": True, "output": _format("classify_activity", sys, user),
                "note": (f"Production kwargs reproduced from activity_engine._do_classify. "
                         f"Sample raw_activity={raw!r}, real known_list from agent's location.")}
    except Exception as e:
        return {"ok": False, "output": "", "note": f"Failed: {e}"}


def _drive_retrospect(agent: str, avatar: str) -> PreviewResult:
    """RetrospectSkill.execute parses input but the prompt build only
    needs agent_name. Run with a stub raw_input."""
    import json
    from app.skills.retrospect_skill import RetrospectSkill
    skill = RetrospectSkill({})
    raw_input = json.dumps({"input": "", "agent_name": agent, "user_id": ""})
    task, sys, user = _capture_render(lambda: skill.execute(raw_input))
    return {"ok": True, "output": _format(task, sys, user),
            "note": "Production: RetrospectSkill.execute(agent_name=agent)."}


def _drive_secret_generation(agent: str, avatar: str) -> PreviewResult:
    from app.core.secret_engine import generate_secrets
    task, sys, user = _capture_render(lambda: generate_secrets(agent, count=2))
    return {"ok": True, "output": _format(task, sys, user),
            "note": "Production: secret_engine.generate_secrets(agent, count=2)."}


def _drive_outfit_generation(agent: str, avatar: str) -> PreviewResult:
    """OutfitCreationSkill builds a prompt then calls llm_call. The skill
    has many input variants; pick a minimal one that exercises the path."""
    import json
    from app.skills.outfit_creation_skill import OutfitCreationSkill
    skill = OutfitCreationSkill({})
    raw_input = json.dumps({
        "input": "create a fitting outfit for the current context",
        "agent_name": agent,
        "user_id": "",
    })
    try:
        task, sys, user = _capture_render(lambda: skill.execute(raw_input))
        return {"ok": True, "output": _format(task, sys, user),
                "note": "Production: OutfitCreationSkill.execute (current context)."}
    except RuntimeError as e:
        return {"ok": False, "output": "",
                "note": f"OutfitCreationSkill returned before llm_call: {e}"}


def _drive_random_event_general(agent: str, avatar: str) -> PreviewResult:
    """random_events._generate_event needs a location dict + chars + active events."""
    from app.models.character import get_character_current_location, list_available_characters
    from app.models.world import get_location
    from app.core.random_events import _generate_event, _get_event_settings
    loc_id = get_character_current_location(agent) or ""
    if not loc_id:
        return {"ok": False, "output": "", "note": f"{agent} has no current location."}
    location = get_location(loc_id)
    if not location:
        return {"ok": False, "output": "", "note": f"Location {loc_id} not found."}
    chars = [c for c in list_available_characters() if c != agent][:5]
    settings = _get_event_settings(location)
    task, sys, user = _capture_render(
        lambda: _generate_event(loc_id, location, "social", [agent] + chars, [], settings))
    return {"ok": True, "output": _format(task, sys, user),
            "note": f"Production: random_events._generate_event(location={loc_id!r}, category='social')."}


def _drive_random_event_escalation(agent: str, avatar: str) -> PreviewResult:
    sample_event = {
        "id": "preview",
        "category": "disruption",
        "text": "A door slams shut suddenly somewhere upstairs.",
        "location_id": "",
    }
    from app.core.random_events import _escalate_event
    task, sys, user = _capture_render(lambda: _escalate_event(sample_event))
    return {"ok": True, "output": _format(task, sys, user),
            "note": "Production: random_events._escalate_event with a sample disruption event."}


def _drive_random_event_secret_hint(agent: str, avatar: str) -> PreviewResult:
    """_try_generate_secret_hint_event picks a target+secret randomly.
    Without controlled input we can't deterministically capture, so we
    note that and use the same kwargs build manually."""
    return {"ok": False, "output": "",
            "note": ("Preview unavailable: _try_generate_secret_hint_event picks a "
                     "random target+secret across the world. The production prompt "
                     "build is inline (see app/core/random_events.py). To preview, "
                     "select an event in production logs.")}


def _drive_random_event_validate_solution(agent: str, avatar: str) -> PreviewResult:
    sample_event = {
        "id": "preview",
        "category": "danger",
        "text": "A small grease fire starts on the kitchen stove.",
    }
    solution = "I grab the lid and slam it down on the pan."
    from app.core.random_events import validate_solution
    task, sys, user = _capture_render(
        lambda: validate_solution(sample_event, solution, agent))
    return {"ok": True, "output": _format(task, sys, user),
            "note": "Production: random_events.validate_solution with a sample fire event."}


def _drive_random_event_solution_rp(agent: str, avatar: str) -> PreviewResult:
    sample_event = {"id": "preview", "category": "danger",
                    "text": "A small grease fire starts on the kitchen stove."}
    from app.core.random_events import _generate_solution_rp
    task, sys, user = _capture_render(
        lambda: _generate_solution_rp(agent, sample_event))
    return {"ok": True, "output": _format(task, sys, user),
            "note": "Production: random_events._generate_solution_rp with sample fire event."}


def _drive_story_arc_generation(agent: str, avatar: str) -> PreviewResult:
    from app.core.story_engine import get_story_engine
    task, sys, user = _capture_render(lambda: get_story_engine().generate_arc())
    return {"ok": True, "output": _format(task, sys, user),
            "note": "Production: story_engine.generate_arc."}


def _drive_story_arc_advancement(agent: str, avatar: str) -> PreviewResult:
    from app.models.story_arcs import get_active_arcs
    arcs = get_active_arcs(agent) or []
    if not arcs:
        return {"ok": False, "output": "",
                "note": f"No active story arc for {agent}."}
    arc = arcs[0]
    from app.core.story_engine import get_story_engine
    task, sys, user = _capture_render(
        lambda: get_story_engine().advance_arc(arc["id"], "Brief tense exchange."))
    return {"ok": True, "output": _format(task, sys, user),
            "note": f"Production: story_engine.advance_arc({arc['id']!r}) "
                    f"with a sample interaction summary."}


def _drive_story_arc_resolve(agent: str, avatar: str) -> PreviewResult:
    from app.models.story_arcs import get_active_arcs
    arcs = get_active_arcs(agent) or []
    if not arcs:
        return {"ok": False, "output": "",
                "note": f"No active story arc for {agent}."}
    arc = arcs[0]
    from app.core.story_engine import get_story_engine
    task, sys, user = _capture_render(
        lambda: get_story_engine().resolve_arc(arc["id"]))
    return {"ok": True, "output": _format(task, sys, user),
            "note": f"Production: story_engine.resolve_arc({arc['id']!r})."}


def _drive_image_prompt_scene(agent: str, avatar: str) -> PreviewResult:
    from app.routes.chat import _generate_image_prompt
    appearances = [{"name": agent, "appearance": ""}]
    if avatar and avatar != agent:
        appearances.append({"name": avatar, "appearance": ""})
    sample_text = "They share a quiet moment by the window."
    task, sys, user = _capture_render(
        lambda: _generate_image_prompt(sample_text, appearances,
                                        agent_config={"name": agent}))
    return {"ok": True, "output": _format(task, sys, user),
            "note": ("Production: routes/chat._generate_image_prompt with a sample scene. "
                     "Appearances list includes selected agent and avatar.")}


def _drive_image_prompt_improver(agent: str, avatar: str) -> PreviewResult:
    from app.skills.image_regenerate import enhance_prompt
    task, sys, user = _capture_render(
        lambda: enhance_prompt(
            f"{agent} sitting in a sunlit room, wearing casual clothes",
            "make it night, add candlelight",
            agent_config={"name": agent}))
    return {"ok": True, "output": _format(task, sys, user),
            "note": "Production: image_regenerate.enhance_prompt with sample prompts."}


def _drive_image_prompt_enhance(agent: str, avatar: str) -> PreviewResult:
    from app.core.prompt_adapters import _llm_enhance
    task, sys, user = _capture_render(
        lambda: _llm_enhance(
            f"{agent}, casual outfit, in a kitchen",
            None,  # PromptVariables — unused inside _llm_enhance for the prompt build
            target_model="flux",
            prompt_instruction="cinematic, high contrast, 35mm film"))
    return {"ok": True, "output": _format(task, sys, user),
            "note": "Production: prompt_adapters._llm_enhance with sample template prompt."}


def _drive_animation_prompt(agent: str, avatar: str) -> PreviewResult:
    """The animation_prompt template is rendered inline in routes/instagram
    and routes/characters suggest-animate handlers. They take an
    image_analysis param. Build kwargs the same way."""
    from app.core.prompt_templates import render_task
    sample_analysis = (f"{agent} stands by a window, wearing a light jacket. "
                       f"Soft daylight, calm expression.")
    sys, user = render_task("animation_prompt", image_analysis=sample_analysis)
    return {"ok": True, "output": _format("instagram_caption", sys, user),
            "note": ("Production kwargs reproduced (animation prompt is rendered "
                     "inline in suggest-animate handlers; only image_analysis is "
                     "passed in — sample used here).")}


def _drive_expression_map(agent: str, avatar: str) -> PreviewResult:
    from app.core.expression_pose_maps import _llm_generate_prompt
    task, sys, user = _capture_render(
        lambda: _llm_generate_prompt("expression", "wistful"))
    return {"ok": True, "output": _format(task, sys, user),
            "note": "Production: expression_pose_maps._llm_generate_prompt('expression', 'wistful')."}


def _drive_extraction_chat_context(agent: str, avatar: str) -> PreviewResult:
    """The chat-context extractor is nested deep inside chat.py and runs
    asynchronously. It's invoked per request — for preview we'd need
    to call the inner _extract_for_character. Reproduce kwargs."""
    from app.core.prompt_templates import render_task
    _, asst_msg = _last_exchange(agent, avatar)
    if not asst_msg:
        return {"ok": False, "output": "", "note": "No recent assistant message."}
    piece_list = ""
    try:
        from app.models.inventory import get_equipped_pieces, get_item
        _eq = get_equipped_pieces(agent) or {}
        _names: List[str] = []
        _seen = set()
        for _slot, _iid in _eq.items():
            if not _iid or _iid in _seen:
                continue
            _seen.add(_iid)
            _it = get_item(_iid) or {}
            _n = (_it.get("name") or "").strip()
            if _n:
                _names.append(_n)
        piece_list = "\n".join(f"- {n}" for n in _names)
    except Exception:
        piece_list = ""
    sys, user = render_task("extraction_chat_context",
        target_name=agent,
        piece_list=piece_list,
        source_label="Character reply",
        source_text=asst_msg,
        outfit_locked=False,
        is_avatar=False)
    return {"ok": True, "output": _format("extraction", sys, user),
            "note": ("Production kwargs reproduced (extraction is async/per-request "
                     "in routes/chat._extract_context_from_last_chat). Latest "
                     "assistant message used.")}


def _drive_relationship_summary(agent: str, avatar: str) -> PreviewResult:
    """relationship_summary is rendered inside chat_engine.post_process_response,
    deeply nested inside background extraction. Build kwargs identically."""
    user_msg, asst_msg = _last_exchange(agent, avatar)
    if not user_msg:
        return {"ok": False, "output": "", "note": "No chat exchange."}
    try:
        from app.models.relationship import get_romantic_interests
        ri_user = get_romantic_interests(avatar) if avatar else ""
        ri_char = get_romantic_interests(agent)
        romantic_context = ""
        if ri_user or ri_char:
            romantic_context = "\nRomantic interest context:\n"
            if ri_user:
                romantic_context += f"- {avatar}'s romantic interests: {ri_user}\n"
            if ri_char:
                romantic_context += f"- {agent}'s romantic interests: {ri_char}\n"
            romantic_context += "Only set romantic_delta > 0 if the conversation matches these interests."
    except Exception:
        romantic_context = ""
    from app.core.prompt_templates import render_task
    sys, user = render_task("relationship_summary",
        user_display_name=avatar,
        character_name=agent,
        user_input=user_msg[:300],
        cleaned=asst_msg[:300] if asst_msg else "",
        romantic_context=romantic_context)
    return {"ok": True, "output": _format("relationship_summary", sys, user),
            "note": ("Production kwargs reproduced from chat_engine.post_process_response "
                     "background extraction. Latest exchange + real romantic_interests.")}


def _drive_image_analysis(agent: str, avatar: str) -> PreviewResult:
    """image_analysis goes through llm_queue.submit directly (Vision-LLM
    with image_url) — capture mechanism does not reach it. Reproduce
    kwargs."""
    from app.core.prompt_templates import render_task
    sys, user = render_task("image_analysis", language_name="English")
    return {"ok": True, "output": _format("image_analysis", sys, user),
            "note": ("Production: instagram_skill._analyze_image submits via "
                     "llm_queue.submit() directly (vision LLM message format) — "
                     "kwargs reproduced for preview.")}


def _drive_instagram_caption(agent: str, avatar: str) -> PreviewResult:
    """instagram caption also bypasses llm_call (vision LLM). Reproduce."""
    from app.core.prompt_templates import render_task
    from app.models.character import get_character_personality
    p = get_character_personality(agent) or ""
    sys, user = render_task("instagram_caption",
        character_name=agent,
        style_description=f"Your style: {p}" if p else "Your style: friendly, confident and approachable.",
        caption_style="casual",
        hashtag_count=3,
        language_name="English",
        context_info="")
    return {"ok": True, "output": _format("instagram_caption", sys, user),
            "note": ("Production: instagram_skill._generate_caption submits via "
                     "llm_queue.submit() with a vision message — kwargs reproduced.")}


# ---------------------------------------------------------------------------
# Driver registry
# ---------------------------------------------------------------------------

_PREVIEW_DRIVERS: Dict[str, PreviewDriver] = {
    "chat/agent_thought.md": _drive_agent_thought,
    "chat/agent_thought_in_chat.md": _drive_agent_thought_in_chat,
    "chat/chat_stream.md": _drive_chat_stream,
    "tasks/extraction_memory.md": _drive_extraction_memory,
    "tasks/extraction_chat_context.md": _drive_extraction_chat_context,
    "tasks/consolidation_daily.md": _drive_consolidation_daily,
    "tasks/consolidation_weekly.md": _drive_consolidation_weekly,
    "tasks/consolidation_monthly.md": _drive_consolidation_monthly,
    "tasks/consolidation_today.md": _drive_consolidation_today,
    "tasks/consolidation_history_summary.md": _drive_consolidation_history_summary,
    "tasks/consolidation_daily_diary.md": _drive_consolidation_daily_diary,
    "tasks/relationship_summary.md": _drive_relationship_summary,
    "tasks/relationship_summary_pair.md": _drive_relationship_summary_pair,
    "tasks/relationship_summary_romantic_interests.md": _drive_relationship_summary_romantic_interests,
    "tasks/classify_activity.md": _drive_classify_activity,
    "tasks/retrospect.md": _drive_retrospect,
    "tasks/secret_generation.md": _drive_secret_generation,
    "tasks/outfit_generation.md": _drive_outfit_generation,
    "tasks/random_event_general.md": _drive_random_event_general,
    "tasks/random_event_escalation.md": _drive_random_event_escalation,
    "tasks/random_event_secret_hint.md": _drive_random_event_secret_hint,
    "tasks/random_event_validate_solution.md": _drive_random_event_validate_solution,
    "tasks/random_event_solution_rp.md": _drive_random_event_solution_rp,
    "tasks/story_arc_generation.md": _drive_story_arc_generation,
    "tasks/story_arc_advancement.md": _drive_story_arc_advancement,
    "tasks/story_arc_resolve.md": _drive_story_arc_resolve,
    "tasks/image_prompt_scene.md": _drive_image_prompt_scene,
    "tasks/image_prompt_improver.md": _drive_image_prompt_improver,
    "tasks/image_prompt_enhance.md": _drive_image_prompt_enhance,
    "tasks/image_analysis.md": _drive_image_analysis,
    "tasks/instagram_caption.md": _drive_instagram_caption,
    "tasks/animation_prompt.md": _drive_animation_prompt,
    "tasks/expression_map.md": _drive_expression_map,
}


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

def render_with_real_data(rel_path: str, agent: str, avatar: str) -> Dict[str, Any]:
    driver = _PREVIEW_DRIVERS.get(rel_path)
    if not driver:
        return {"ok": False, "output": "",
                "note": f"No preview driver implemented for {rel_path}."}
    if not agent:
        return {"ok": False, "output": "",
                "note": "Pick an agent character."}
    try:
        return driver(agent, avatar or "")
    except RuntimeError as e:
        return {"ok": False, "output": "",
                "note": f"Production function returned without invoking llm_call: {e}"}
    except Exception as e:
        logger.error("render_with_real_data(%s, %s, %s) failed: %s",
                     rel_path, agent, avatar, e, exc_info=True)
        return {"ok": False, "output": "",
                "note": f"Render error: {type(e).__name__}: {e}"}
