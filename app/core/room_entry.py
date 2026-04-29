"""Avatar-Raumwechsel: Anwesende Characters bemerken den Eintritt.

Wird aufgerufen wenn der User-Avatar einen Raum (oder Location) betritt.
Waehlt einen geeigneten Character im Zielraum, der via forced_thought +
TalkTo-Whitelist auf den Eintritt reagiert.

Cooldown: pro (reactor, avatar) max 1 Greeting in GREETING_COOLDOWN_MIN
Minuten — gespeichert als Memory-Tag `room_greeting:{avatar}`.
"""
import random
from datetime import datetime, timedelta
from typing import List, Optional

from app.core.log import get_logger

logger = get_logger("room_entry")

GREETING_COOLDOWN_MIN = 30


def _list_characters_in_room(location_id: str, room_id: str, exclude: str = "") -> List[str]:
    """Listet alle Characters die im selben Raum (oder bei leerem room_id im Location) sind.

    char_room kann historisch entweder Raum-ID oder Raum-Name enthalten —
    Match gegen beides.
    """
    from app.models.character import (
        list_available_characters,
        get_character_current_location,
        get_character_current_room)
    from app.models.world import get_location, get_room_by_id

    target_id = ""
    target_name = ""
    if room_id and location_id:
        loc = get_location(location_id)
        if loc:
            r = get_room_by_id(loc, room_id)
            if r:
                target_id = r.get("id", "") or room_id
                target_name = r.get("name", "")
            else:
                for r in (loc.get("rooms") or []):
                    if r.get("name") == room_id:
                        target_id = r.get("id", "")
                        target_name = room_id
                        break
                else:
                    target_id = room_id
                    target_name = room_id

    out: List[str] = []
    for c in list_available_characters():
        if c == exclude:
            continue
        if get_character_current_location(c) != location_id:
            continue
        char_room = (get_character_current_room(c) or "").strip()
        if room_id and char_room and char_room not in (target_id, target_name):
            continue
        out.append(c)
    return out


def _is_interruptible(char_name: str) -> bool:
    """True wenn die aktuelle Activity unterbrechbar ist (oder keine gesetzt)."""
    from app.models.character import get_character_current_activity
    act = (get_character_current_activity(char_name) or "").strip()
    if not act:
        return True
    try:
        from app.models.activity_library import (
            get_library_activity, find_library_activity_by_name)
        lib = get_library_activity(act) or find_library_activity_by_name(act)
        if lib and lib.get("interruptible") is False:
            return False
    except Exception:
        pass
    return True


def _is_in_cooldown(reactor: str, avatar: str) -> bool:
    """True wenn der reactor den avatar in den letzten N Min schon begruesst hat."""
    try:
        from app.models.memory import load_memories
        cutoff = datetime.now() - timedelta(minutes=GREETING_COOLDOWN_MIN)
        cutoff_iso = cutoff.isoformat()
        target_tag = f"room_greeting:{avatar}"
        for m in load_memories(reactor):
            ts = m.get("timestamp") or ""
            if ts < cutoff_iso:
                continue
            tags = m.get("tags") or []
            if target_tag in tags:
                return True
    except Exception as e:
        logger.debug("Cooldown-Check fuer %s/%s fehlgeschlagen: %s", reactor, avatar, e)
    return False


def _reaction_chance(reactor: str, avatar: str) -> float:
    """Reaktionswahrscheinlichkeit (0..1) als Mittel aus

    - reactor.social_dialog_probability (wie gespraechig ist der Character)
    - avatar.popularity (wie sehr zieht der Avatar Aufmerksamkeit auf sich)

    Beide Werte sind 0-100 in der character_config (Default jeweils 50).
    """
    try:
        from app.models.character import get_character_config
        rcfg = get_character_config(reactor) or {}
        acfg = get_character_config(avatar) or {}
        sdp = float(rcfg.get("social_dialog_probability", 50) or 50)
        pop = float(acfg.get("popularity", 50) or 50)
    except Exception:
        sdp, pop = 50.0, 50.0
    avg = max(0.0, min(100.0, (sdp + pop) / 2.0))
    return avg / 100.0


def pick_reactor(avatar: str, candidates: List[str]):
    """Waehlt EINEN Character der den Avatar begruesst — oder None.

    Returns: (reactor_or_None, silent_noticers_list)
      - reactor: Character der das Greeting ausloest (oder None)
      - silent_noticers: Characters die im Raum waren, den Cooldown-Filter
        bestanden haben aber den Reaktions-Wuerfel verloren — fuer UI-Hinweis
        "X hat Dich bemerkt, sagt aber nichts".
    """
    if not candidates:
        return None, []

    pool_int = [c for c in candidates if _is_interruptible(c)]
    pool_cd = [c for c in pool_int if not _is_in_cooldown(c, avatar)]

    pool: List[str] = []
    silent: List[str] = []
    rolls: List[str] = []
    for c in pool_cd:
        chance = _reaction_chance(c, avatar)
        roll = random.random()
        rolls.append(f"{c}(p={chance:.2f},roll={roll:.2f})")
        if roll < chance:
            pool.append(c)
        else:
            silent.append(c)

    logger.info("pick_reactor: candidates=%s -> interruptible=%s -> nach_cooldown=%s -> reagieren=%s silent=%s [%s]",
                candidates, pool_int, pool_cd, pool, silent, "; ".join(rolls))
    if not pool:
        return None, silent

    try:
        from app.models.relationship import get_relationship
    except Exception:
        get_relationship = None  # type: ignore

    scored = []
    for c in pool:
        strength = 50.0
        if get_relationship is not None:
            try:
                rel = get_relationship(c, avatar) or {}
                strength = float(rel.get("strength", 50) or 50)
            except Exception:
                pass
        score = strength * (0.5 + random.random())
        scored.append((score, c))
    scored.sort(reverse=True)
    return scored[0][1], silent


def _record_greeting(reactor: str, avatar: str, room_label: str) -> None:
    """Schreibt eine Memory mit Cooldown-Tag — der naechste Lookup blockt N Min."""
    try:
        from app.models.memory import add_memory
        add_memory(
            reactor,
            f"Hat {avatar} in {room_label} bemerkt und kurz begruesst.",
            tags=["room_greeting", f"room_greeting:{avatar}"])
    except Exception as e:
        logger.debug("Greeting-Memory fuer %s fehlgeschlagen: %s", reactor, e)


def on_avatar_room_entry(avatar_name: str,
                          location_id: str,
                          room_id: str = "",
                          location_label: str = "",
                          room_label: str = "") -> dict:
    """Hook nach Avatar-Raumwechsel. Triggert ggf. eine Greeting-Reaktion.

    Returns: dict mit
      - reactor: Name des reagierenden Characters (oder "")
      - silent_noticers: Liste der Characters die bemerkt aber geschwiegen haben
    """
    result = {"reactor": "", "silent_noticers": []}
    if not avatar_name or not location_id:
        return result
    candidates = _list_characters_in_room(location_id, room_id, exclude=avatar_name)
    logger.info("on_avatar_room_entry: avatar=%s loc=%s room=%s -> %d Kandidaten: %s",
                 avatar_name, location_id, room_id, len(candidates), candidates)
    if not candidates:
        return result

    reactor, silent = pick_reactor(avatar_name, candidates)
    result["silent_noticers"] = silent
    if not reactor:
        logger.info("on_avatar_room_entry: kein Reactor gewaehlt (avatar=%s, %d Kandidaten, %d silent)",
                     avatar_name, len(candidates), len(silent))
        return result

    label = (room_label or location_label or "den Raum").strip() or "den Raum"
    hint = (
        f"{avatar_name} ist gerade in {label} reingekommen. "
        f"Du bist auch hier. Begruesse {avatar_name} kurz wenn es zu dir passt — "
        f"oder ignoriere die Person falls du gerade nicht in Stimmung bist."
    )

    try:
        from app.core.background_queue import get_background_queue
        get_background_queue().submit("forced_thought", {
            "user_id": "",
            "character_name": reactor,
            "context_hint": hint,
            # Sinnvolle Reaktionen auf Avatar-Eintritt:
            # - TalkTo: Begruessung
            # - ChangeOutfit: schnell anziehen wenn nicht mehr allein
            # - SetActivity: Activity wechseln (wegen Anwesenheit)
            # - SetLocation: in anderen Raum fliehen / Privatsphaere geben
            "tool_whitelist": ["TalkTo", "ChangeOutfit", "SetActivity", "SetLocation"],
            # Eigener LLM-Task: per Admin-UI separat zuweisbar (faellt automatisch
            # auf "thought" zurueck wenn nicht geroutet).
            "llm_task": "thought_greeting",
        })
        _record_greeting(reactor, avatar_name, label)
        logger.info("Avatar-Eintritt: %s reagiert auf %s in '%s'",
                    reactor, avatar_name, label)
        result["reactor"] = reactor
        return result
    except Exception as e:
        logger.warning("on_avatar_room_entry: forced_thought submit failed: %s", e)
        return result
