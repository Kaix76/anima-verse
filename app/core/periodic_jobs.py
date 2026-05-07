"""World-Admin-Tick — zentraler Hintergrund-Job fuer alle administrativen
Welt-Aktionen.

Frueher liefen mehrere periodic-Tasks parallel (status, assignment-expiry,
random-events, relationship-decay). Konsolidiert auf EINEN Tick mit
konfigurierbarem Intervall (Default 60s, ``world.admin_tick_interval_seconds``).
Sub-Tasks haben eigene Frequenzen (per Modulo / Last-Run-Tracking) und
laufen sequenziell innerhalb des Ticks — verhindert Race-Conditions
zwischen den vorherigen parallelen Tasks und gibt einen einzigen
Anpassungspunkt fuer die Welt-Tick-Frequenz.

Sub-Tasks die laufen pro Tick (mit eigener Sub-Frequenz):
    - status_tick               — apply_hourly_status_tick (interner 1h-Gate)
    - force_rules               — Pruefe alle Force-Rules pro Char (jeder Tick)
    - assignment_expiry         — expire_overdue (jeder Tick)
    - random_events_generate    — alle 3600s
    - random_events_escalate    — alle 300s
    - random_events_resolve     — alle 300s
    - relationship_decay        — alle 24h (Handler hat eigenen Cooldown)

Tick-Intervall ist im Game Admin → Settings → Server konfigurierbar.
Bereich 10s-3600s. Jobs sind nicht einzeln deaktivierbar — wenn du sie
nicht willst, setz das Intervall hoch oder deaktiviere die jeweilige
Welt-Pause-Schalter.

Public API:
    start() / stop() — registriert vom server.py lifespan
"""
import asyncio
from datetime import datetime
from typing import Callable, Dict, List, Optional

from app.core.log import get_logger

logger = get_logger("periodic_jobs")


# Default-Tick-Intervall wenn config-Wert fehlt oder ungueltig.
_DEFAULT_TICK_SECONDS = 60
# Untere/obere Grenzen — schuetzt vor versehentlich kaputter Config (z.B.
# 0s wuerde den Loop sofort spammen).
_MIN_TICK_SECONDS = 10
_MAX_TICK_SECONDS = 3600


def _is_paused() -> bool:
    """Mirrors AgentLoop pause source — task_queue 'default' pause flag."""
    try:
        from app.core.task_queue import get_task_queue
        tq = get_task_queue()
        return bool(tq and tq._is_paused("default"))
    except Exception:
        return False


def _get_tick_interval() -> int:
    """Liest ``server.world_admin_tick_interval_seconds`` aus der Config,
    geclamped auf [_MIN_TICK_SECONDS, _MAX_TICK_SECONDS]."""
    try:
        from app.core import config as _cfg
        val = int(_cfg.get("server.world_admin_tick_interval_seconds")
                  or _DEFAULT_TICK_SECONDS)
    except Exception:
        val = _DEFAULT_TICK_SECONDS
    return max(_MIN_TICK_SECONDS, min(_MAX_TICK_SECONDS, val))


# ---------------------------------------------------------------------------
# Sub-Task Implementierungen — laufen synchron im Threadpool, weil sie
# DB/Disk-IO machen und das Event-Loop sonst blockieren wuerden.
# ---------------------------------------------------------------------------

def _sub_status_tick():
    """Apply hourly stat decay to all characters. Internal 1h gating in
    apply_hourly_status_tick — billig auch jede Minute aufzurufen."""
    try:
        from app.core.activity_engine import apply_hourly_status_tick
        from app.models.character import list_available_characters
        for name in list_available_characters():
            try:
                apply_hourly_status_tick(name)
            except Exception as e:
                logger.debug("status_tick failed for %s: %s", name, e)
    except Exception as e:
        logger.debug("status_tick sub error: %s", e)


def _sub_force_rules():
    """Force-Rules pro Char pruefen — z.B. Wake-Rule (stamina>X +
    activity:sleeping → activity=""). Vorher nur 1x/h im hourly tick;
    jetzt jeden Welt-Admin-Tick — Reaktion auf Schwellwert-Wechsel
    innerhalb Minuten statt Stunden."""
    try:
        from app.models.rules import (
            check_force_rules, resolve_force_destination)
        from app.models.character import (
            list_available_characters,
            get_character_current_location, get_character_current_room,
            get_character_current_activity,
            save_character_current_location,
            save_character_current_room,
            save_character_current_activity,
            enter_offmap_sleep, wake_from_offmap,
            OFFMAP_SLEEP_SENTINEL,
            _record_state_change)
        from app.models.account import is_player_controlled
        for name in list_available_characters():
            try:
                if is_player_controlled(name):
                    continue  # Avatar nicht zwingen
                force = check_force_rules(name)
                if not force:
                    continue
                go_to = force.get("go_to") or "stay"
                dest_loc, dest_room = resolve_force_destination(name, go_to)

                # Vorher-Snapshot: erlaubt am Ende zu erkennen ob die Regel
                # tatsaechlich was geaendert hat. Ohne Aenderung kein
                # _record_state_change(forced_action) — sonst spammt eine
                # Erschoepfungs-/Wake-Regel jede Minute das Tagebuch.
                _before_loc = (get_character_current_location(name) or "").strip()
                _before_room = (get_character_current_room(name) or "").strip()
                _before_act = (get_character_current_activity(name) or "").strip().lower()

                # Offmap-Sentinel: nicht als echte Location speichern. Wenn
                # home=__offmap__ → Char vom Grid nehmen via enter_offmap_sleep.
                if dest_loc == OFFMAP_SLEEP_SENTINEL:
                    if _before_loc:  # nur wechseln wenn der Char gerade auf der Karte ist
                        try:
                            enter_offmap_sleep(name)
                        except Exception:
                            pass
                    dest_loc = ""  # kein weiterer save_character_current_location
                    dest_room = ""

                if dest_loc:
                    save_character_current_location(name, dest_loc)
                if dest_room:
                    save_character_current_room(name, dest_room)
                if "set_activity" in force:
                    new_act = (force.get("set_activity") or "").strip()
                    save_character_current_activity(name, new_act)
                    if not new_act:
                        try:
                            wake_from_offmap(name)
                        except Exception:
                            pass

                # Nur loggen + ins Tagebuch wenn sich was geaendert hat.
                _after_loc = (get_character_current_location(name) or "").strip()
                _after_room = (get_character_current_room(name) or "").strip()
                _after_act = (get_character_current_activity(name) or "").strip().lower()
                _changed = (_before_loc != _after_loc
                            or _before_room != _after_room
                            or _before_act != _after_act)
                if not _changed:
                    continue

                _record_state_change(
                    name, "forced_action",
                    force.get("message") or force.get("rule_name") or "",
                    metadata={"rule": force.get("rule_name", ""),
                              "go_to": go_to,
                              "activity": force.get("set_activity", "")})
                logger.info("Force-Rule %s -> %s (%s)",
                            force.get("rule_name", "?"), name, go_to)
            except Exception as _ce:
                logger.debug("force_rules check failed for %s: %s", name, _ce)
    except Exception as e:
        logger.debug("force_rules sub error: %s", e)


def _sub_activity_expiry():
    """Setzt Activities zurueck deren ``duration_minutes`` ueberschritten ist.

    Ersetzt den frueheren APScheduler-One-Time-``activity_done_*``-Job-Pfad
    in ``set_activity_skill._schedule_duration_complete``. Profil-Felder
    ``activity_started_at`` + ``activity_duration_minutes`` werden von
    ``save_character_current_activity`` geschrieben — hier nur lesen +
    ggf. Activity loeschen + on_complete-Trigger feuern.
    """
    try:
        from app.models.character import (
            list_available_characters, get_character_profile,
            save_character_current_activity)
        from app.core.activity_engine import _find_activity_definition, execute_trigger
        from datetime import datetime as _dt
        now = _dt.now()
        for name in list_available_characters():
            try:
                prof = get_character_profile(name) or {}
                act = (prof.get("current_activity") or "").strip()
                if not act:
                    continue
                dur = int(prof.get("activity_duration_minutes") or 0)
                if dur <= 0:
                    continue  # endlose Activity — laeuft bis aktiv geaendert
                started_iso = (prof.get("activity_started_at") or "").strip()
                if not started_iso:
                    continue
                try:
                    started = _dt.fromisoformat(started_iso)
                except (ValueError, TypeError):
                    continue
                elapsed_min = (now - started).total_seconds() / 60.0
                if elapsed_min < dur:
                    continue
                # Abgelaufen — on_complete-Trigger feuern bevor Activity raus
                try:
                    act_def = _find_activity_definition(name, act) or {}
                    on_complete = (act_def.get("triggers") or {}).get("on_complete")
                    if on_complete:
                        execute_trigger(name, on_complete,
                                        context={"completed_activity": act,
                                                 "elapsed_minutes": round(elapsed_min, 1)})
                except Exception as _te:
                    logger.debug("on_complete trigger failed for %s/%s: %s",
                                 name, act, _te)
                save_character_current_activity(name, "")
                logger.info("Activity-Expiry: %s / '%s' nach %.1f min beendet (dur=%d)",
                            name, act, elapsed_min, dur)
            except Exception as _ce:
                logger.debug("activity_expiry check failed for %s: %s", name, _ce)
    except Exception as e:
        logger.debug("activity_expiry sub error: %s", e)


def _sub_assignment_expiry():
    try:
        from app.models.assignments import expire_overdue
        expire_overdue()
    except Exception as e:
        logger.debug("assignment_expiry sub error: %s", e)


def _sub_random_events_generate():
    try:
        from app.core.random_events import check_and_generate
        check_and_generate()
    except Exception as e:
        logger.debug("random_events_generate sub error: %s", e)


def _sub_random_events_escalate():
    try:
        from app.core.random_events import check_escalation
        check_escalation()
    except Exception as e:
        logger.debug("random_events_escalate sub error: %s", e)


def _sub_random_events_resolve():
    try:
        from app.core.random_events import try_resolve_events
        try_resolve_events()
    except Exception as e:
        logger.debug("random_events_resolve sub error: %s", e)


def _sub_relationship_decay():
    try:
        from app.core.background_queue import get_background_queue
        get_background_queue().submit(
            "relationship_decay", {"user_id": ""}, deduplicate=True)
    except Exception as e:
        logger.debug("relationship_decay sub error: %s", e)


def _sub_variant_prune():
    """LRU-Eviction stale Expression-Variants pro Character.

    Cap kommt aus ``server.variants_max_per_character`` (Default 30). Variants
    mit aktuellstem ``last_used_at`` ueberleben.
    """
    try:
        from app.core.expression_regen import prune_variants_all
        removed = prune_variants_all()
        if removed:
            logger.info("variant_prune: %d alte Variants entfernt", removed)
    except Exception as e:
        logger.debug("variant_prune sub error: %s", e)


# Sub-Task-Tabelle: (callable, min_interval_seconds, label).
# min_interval_seconds = wie oft soll dieser Sub-Task LAUFEN. Der Tick
# selbst feuert haeufiger; jeder Sub-Task wird nur ausgefuehrt wenn seit
# der letzten Ausfuehrung mindestens min_interval_seconds vergangen sind.
# Sub-Tasks ohne minimum (=0) laufen jeden Tick.
_SUB_TASKS: List[tuple] = [
    # (func,                         min_interval_s,        label)
    (_sub_status_tick,               60,                    "status_tick"),
    (_sub_force_rules,               0,                     "force_rules"),
    (_sub_activity_expiry,           0,                     "activity_expiry"),
    (_sub_assignment_expiry,         60,                    "assignment_expiry"),
    (_sub_random_events_generate,    3600,                  "random_events_generate"),
    (_sub_random_events_escalate,    300,                   "random_events_escalate"),
    (_sub_random_events_resolve,     300,                   "random_events_resolve"),
    (_sub_relationship_decay,        24 * 3600,             "relationship_decay"),
    (_sub_variant_prune,             3600,                  "variant_prune"),
]


# ---------------------------------------------------------------------------
# Tick-Loop
# ---------------------------------------------------------------------------

_tick_task: Optional[asyncio.Task] = None
_last_run: Dict[str, float] = {}  # label -> unix-ts of last successful run


async def _world_admin_tick_loop():
    """Eine asyncio-Task fuer alle Welt-Admin-Aktionen. Ruft Sub-Tasks
    sequenziell, jeder mit eigener Sub-Frequenz."""
    # Initial-Delay: vermeidet dass alle Sub-Tasks beim ersten Tick gemeinsam
    # feuern (CPU-Spike). Default 30s damit der Server zuerst hochkommt.
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        return

    while True:
        try:
            interval = _get_tick_interval()
            if not _is_paused():
                now = datetime.now().timestamp()
                for func, min_iv, label in _SUB_TASKS:
                    last = _last_run.get(label, 0.0)
                    if min_iv and (now - last) < min_iv:
                        continue
                    started = datetime.now()
                    try:
                        await asyncio.to_thread(func)
                        _last_run[label] = datetime.now().timestamp()
                    except Exception as _se:
                        logger.error("admin_tick sub %s error: %s",
                                     label, _se, exc_info=True)
                    duration = (datetime.now() - started).total_seconds()
                    if duration > 5:
                        logger.info("admin_tick %s done in %.1fs", label, duration)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("admin_tick loop error: %s", e, exc_info=True)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return


def start() -> None:
    """Startet den zentralen World-Admin-Tick als asyncio-Task."""
    global _tick_task
    if _tick_task is not None and not _tick_task.done():
        return
    _tick_task = asyncio.create_task(_world_admin_tick_loop())
    logger.info("World-Admin-Tick gestartet (interval=%ds)", _get_tick_interval())


async def stop() -> None:
    global _tick_task
    if _tick_task is None:
        return
    _tick_task.cancel()
    try:
        await _tick_task
    except (asyncio.CancelledError, Exception):
        pass
    _tick_task = None
    _last_run.clear()
    logger.info("World-Admin-Tick gestoppt")
