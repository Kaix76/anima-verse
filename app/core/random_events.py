"""Random Event Generator — Erzeugt automatische Ereignisse an Locations.

Wird aus dem ThoughtLoop stuendlich aufgerufen (alle 60 Ticks bei
60s Tick-Intervall = 1x pro Stunde pro User).

Events werden per LLM generiert und ueber das bestehende Event-System
(app/models/events.py) gespeichert + im System-Prompt injiziert.

Konfiguration:
    EVENT_GENERATION_ENABLED=true          (default: true)
    EVENT_BASE_PROBABILITY=0.10            (default: 10% pro Stunde pro Location)

Pro Location konfigurierbar via event_settings in world.json:
    event_probability       — Wahrscheinlichkeit pro stuendlichem Check (0.0–1.0)
    allowed_categories      — Liste erlaubter Kategorien
    event_blacklist         — verbotene Event-Begriffe
    max_concurrent_events   — max. parallele Events pro Location
    event_cooldown_hours    — Mindestzeit zwischen Events in Stunden
"""
import os
import random
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.log import get_logger

logger = get_logger("random_events")

ENABLED = os.environ.get("EVENT_GENERATION_ENABLED", "true").lower() in ("true", "1", "yes")
BASE_PROBABILITY = float(os.environ.get("EVENT_BASE_PROBABILITY", "0.10"))
RESOLUTION_COOLDOWN_MIN = int(os.environ.get("EVENT_RESOLUTION_COOLDOWN_MINUTES", "15"))
RESOLUTION_PROACTIVE_ENABLED = os.environ.get("EVENT_RESOLUTION_PROACTIVE", "true").lower() in ("true", "1", "yes")

# Kategorie-Definitionen mit Basis-Gewichten und TTL
CATEGORIES = {
    "ambient": {
        "weight": 40,
        "ttl_hours": 2,
        "description": "Atmosphere, harmless — weather, sounds, smells, small observations",
    },
    "social": {
        "weight": 30,
        "ttl_hours": 4,
        "description": "Social interaction — a visitor, a message, a rumor, someone arriving",
    },
    "disruption": {
        "weight": 20,
        "ttl_hours": 6,
        "description": "Something that demands attention — a power outage, an accident, a discovery",
    },
    "danger": {
        "weight": 10,
        "ttl_hours": 8,
        "description": "Urgent, time pressure — a break-in, a fire, a storm, someone in danger",
    },
}

DEFAULT_EVENT_SETTINGS = {
    "event_probability": BASE_PROBABILITY,
    "allowed_categories": list(CATEGORIES.keys()),
    "event_blacklist": [],
    "max_concurrent_events": 1,
    "event_cooldown_hours": 2,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_and_generate():
    """Prueft alle besetzten Locations und generiert ggf. Events.

    Aufgerufen aus ThoughtLoop._tick() alle 60 Sekunden.
    """
    if not ENABLED:
        return

    try:
        from app.models.character import list_available_characters, get_character_current_location
        from app.models.world import list_locations

        chars = list_available_characters()
        if not chars:
            return

        # Feature-Gate: Characters die random_events NICHT wollen, bekommen keine
        from app.models.character_template import is_feature_enabled as _feat
        chars = [c for c in chars if _feat(c, "random_events_enabled")]
        if not chars:
            return

        # Besetzte Locations ermitteln (mit Characters)
        occupied: Dict[str, List[str]] = {}
        for char in chars:
            loc = get_character_current_location(char)
            if loc:
                occupied.setdefault(loc, []).append(char)

        if not occupied:
            return

        # Pro besetzte Location pruefen
        locations = {loc.get("id"): loc for loc in list_locations()}
        for loc_id, char_names in occupied.items():
            loc_data = locations.get(loc_id)
            if not loc_data:
                continue
            _try_generate_for_location(loc_id, loc_data, char_names)

    except Exception as e:
        logger.debug("check_and_generate error: %s", e)


def check_escalation():
    """Prueft ob disruption/danger Events ohne Reaktion eskaliert werden muessen.

    Aufgerufen alle 5 Minuten aus ThoughtLoop._tick().
    """
    if not ENABLED:
        return

    try:
        from app.models.events import list_events, get_all_events
        from app.models.chat import get_chat_history

        all_events = get_all_events()
        for event in all_events:
            cat = event.get("category", "")
            if cat not in ("disruption", "danger"):
                continue

            # Schon eskaliert? (hat ein Folge-Event)
            event_id = event.get("id", "")
            already_escalated = any(
                e.get("escalation_of") == event_id for e in all_events
            )
            if already_escalated:
                continue

            # Alter des Events pruefen (TTL/2 vergangen?)
            try:
                created = datetime.fromisoformat(event.get("created_at", ""))
                ttl_hours = event.get("ttl_hours", 6)
                half_ttl = timedelta(hours=ttl_hours / 2)
                if datetime.now() - created < half_ttl:
                    continue  # Noch nicht alt genug
            except (ValueError, TypeError):
                continue

            # Chat-Aktivitaet seit Event pruefen (an dieser Location)
            location_id = event.get("location_id", "")
            if not location_id:
                continue

            had_interaction = _had_chat_since(location_id, created)
            if had_interaction:
                continue  # Jemand hat reagiert (Chat)

            # Auch ein Loesungsversuch (erfolgreich ODER fehlgeschlagen)
            # zaehlt als Reaktion und blockiert die Eskalation.
            resolution = event.get("resolution") or {}
            if resolution.get("attempts"):
                continue  # Schon mindestens einmal versucht

            # Eskalieren
            _escalate_event(event)

    except Exception as e:
        logger.debug("check_escalation error: %s", e)


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------

def _get_event_settings(location: Dict[str, Any]) -> Dict[str, Any]:
    """Liest event_settings aus Location, mit Defaults."""
    settings = location.get("event_settings", {})
    result = dict(DEFAULT_EVENT_SETTINGS)
    result.update({k: v for k, v in settings.items() if v is not None})
    return result


def _get_category_weights(location: Dict[str, Any], settings: Dict[str, Any]) -> Dict[str, int]:
    """Berechnet Kategorie-Gewichte basierend auf danger_level."""
    from app.core.danger_system import get_danger_level

    weights = {k: v["weight"] for k, v in CATEGORIES.items()}
    danger = get_danger_level(location)

    # Danger-Level verschiebt Gewichte zu disruption/danger
    if danger >= 2:
        weights["danger"] += danger * 5
        weights["disruption"] += danger * 3
        weights["ambient"] = max(5, weights["ambient"] - danger * 3)

    # Nur erlaubte Kategorien
    allowed = settings.get("allowed_categories", list(CATEGORIES.keys()))
    weights = {k: v for k, v in weights.items() if k in allowed}

    return weights


def _pick_category(weights: Dict[str, int]) -> str:
    """Waehlt eine Kategorie nach Gewichtung."""
    cats = list(weights.keys())
    w = [weights[c] for c in cats]
    return random.choices(cats, weights=w, k=1)[0]


_ENTRY_ROLL_LAST: Dict[Tuple[str, str], datetime] = {}


def try_roll_on_entry(character_name: str, loc_id: str, location: Dict[str, Any]) -> None:
    """Sofort-Roll wenn ein Character eine Location betritt.

    - Globaler Master-Switch ``EVENT_ENTRY_ROLL_ENABLED``.
    - Per-(character, location) Cooldown ``EVENT_ENTRY_ROLL_COOLDOWN_MINUTES``
      gegen Wuerfel-Spam beim schnellen Rein-Raus.
    - Skip wenn an der Location bereits ein ungeloestes danger-Event steht
      (kein Doppel-Wolf).
    - Bei Treffer wird ``_generate_event`` mit Jitter (0..N s) im Hintergrund-
      Thread gefeuert, damit der Move-Handler nicht blockiert.

    Designentscheidung: nur Avatar — NPC-Walk-Step-Trigger heute bewusst aus.
    """
    if not loc_id or not location:
        return
    if os.environ.get("EVENT_ENTRY_ROLL_ENABLED", "1") not in ("1", "true", "True"):
        return
    if os.environ.get("EVENT_GENERATION_ENABLED", "1") not in ("1", "true", "True"):
        return

    # Cooldown
    try:
        cooldown_min = int(os.environ.get("EVENT_ENTRY_ROLL_COOLDOWN_MINUTES", "10"))
    except (ValueError, TypeError):
        cooldown_min = 10
    key = (character_name or "", loc_id)
    last = _ENTRY_ROLL_LAST.get(key)
    if last and (datetime.now() - last).total_seconds() < cooldown_min * 60:
        return
    _ENTRY_ROLL_LAST[key] = datetime.now()

    # Skip wenn an der Location bereits ein aktives ungeloestes
    # disruption/danger-Event steht (Constraint "ein blockendes Event pro
    # Location"). Ambient/social-Events sind erlaubt und blocken nicht.
    # list_events liefert auch globale Events (location_id=None) mit —
    # explizit auf die eigene Location filtern, sonst blockt ein globales
    # danger-Event jeden Entry-Roll weltweit.
    try:
        from app.models.events import list_events
        for e in list_events(location_id=loc_id) or []:
            if e.get("location_id") != loc_id:
                continue
            if e.get("category") in ("disruption", "danger") and not e.get("resolved"):
                return
    except Exception as _e:
        logger.debug("entry-roll skip-check fehlgeschlagen: %s", _e)

    settings = _get_event_settings(location)
    prob = settings.get("event_probability", BASE_PROBABILITY)
    if random.random() > prob:
        return

    weights = _get_category_weights(location, settings)
    if not weights:
        return
    category = _pick_category(weights)

    try:
        jitter_max = int(os.environ.get("EVENT_ENTRY_ROLL_JITTER_SECONDS", "3"))
    except (ValueError, TypeError):
        jitter_max = 3
    delay = random.uniform(0, max(0, jitter_max))

    def _spawn():
        try:
            from app.models.events import list_events as _le
            active = _le(location_id=loc_id)
            _generate_event(loc_id, location, category, [character_name], active, settings)
        except Exception as _e:
            logger.debug("entry-roll _spawn fehlgeschlagen: %s", _e)

    import threading
    threading.Timer(delay, _spawn).start()
    logger.info("Entry-Roll Treffer: %s @ %s — Event spawnt in %.1fs (cat=%s)",
                character_name, loc_id, delay, category)


def _try_generate_for_location(loc_id: str, location: Dict[str, Any], char_names: List[str]):
    """Versucht ein Event fuer eine Location zu generieren."""
    settings = _get_event_settings(location)

    # 1. Wahrscheinlichkeits-Check
    prob = settings.get("event_probability", BASE_PROBABILITY)
    if random.random() > prob:
        return

    # 2. Constraint: max ein aktives ungeloestes disruption/danger Event pro
    #    Location. Ambient/social duerfen daneben spawnen — sie blocken nichts
    #    und tragen zur Atmosphaere bei. list_events liefert auch globale
    #    Events (location_id=None) mit — die zaehlen hier NICHT, sonst wuerde
    #    ein einziges globales danger-Event jeden Location-Spawn weltweit
    #    blockieren.
    from app.models.events import list_events
    active_all = list_events(location_id=loc_id)
    active_local = [e for e in active_all if e.get("location_id") == loc_id]
    if any(e.get("category") in ("disruption", "danger") and not e.get("resolved")
           for e in active_local):
        return

    # 3. Max-Concurrent-Check (vor allem fuer ambient/social-Spam-Vermeidung) —
    #    ebenfalls strikt auf die eigene Location bezogen.
    max_concurrent = settings.get("max_concurrent_events", 1)
    if len(active_local) >= max_concurrent:
        return
    active = active_local

    # 4. Cooldown-Check (min Zeit seit letztem Event, in Stunden)
    # Legacy: event_cooldown_minutes weiterhin unterstuetzt
    cooldown_hours = settings.get("event_cooldown_hours")
    if cooldown_hours is None:
        legacy_min = settings.get("event_cooldown_minutes")
        cooldown_hours = (legacy_min / 60.0) if legacy_min is not None else 2
    if active:
        latest = max(active, key=lambda e: e.get("created_at", ""))
        try:
            last_time = datetime.fromisoformat(latest.get("created_at", ""))
            if (datetime.now() - last_time).total_seconds() < cooldown_hours * 3600:
                return
        except (ValueError, TypeError):
            pass

    # 5. Kategorie wuerfeln
    weights = _get_category_weights(location, settings)
    if not weights:
        return
    category = _pick_category(weights)

    # 6. Event per LLM generieren — mit kleiner Wahrscheinlichkeit ein Secret-Hint-Event
    reveal_chance = float(settings.get("secret_reveal_chance", 0.08))
    if reveal_chance > 0 and random.random() < reveal_chance:
        if _try_generate_secret_hint_event(loc_id, location, char_names, settings):
            return  # Hint-Event generiert
    _generate_event(loc_id, location, category, char_names, active, settings)


def _try_generate_secret_hint_event(loc_id: str,
    location: Dict[str, Any],
    char_names: List[str],
    settings: Dict[str, Any]) -> bool:
    """Generiert ein Event das auf ein Geheimnis eines anwesenden Characters hinweist.

    Waehlt einen zufaelligen Character mit mindestens einem Geheimnis am Ort
    (dessen Geheimnis nicht zu viele bereits kennen) und generiert einen
    subtilen Hinweis (kein Komplett-Reveal). Die anderen Anwesenden werden
    als hinted-to markiert.

    Returns True wenn ein Hint-Event erzeugt wurde.
    """
    if len(char_names) < 2:
        return False  # Braucht mindestens Ziel + Beobachter

    from app.models.secrets import list_secrets
    from app.models.events import add_event

    # Character mit Geheimnissen sammeln
    candidates = []
    for c in char_names:
        try:
            secs = [s for s in list_secrets(c)
                    if s.get("content") and len(s.get("known_by", [])) < 3]
            if secs:
                candidates.append((c, secs))
        except Exception:
            continue
    if not candidates:
        return False

    # Zufaellig waehlen, severity-gewichtet
    target_char, target_secrets = random.choice(candidates)
    # Severity als Gewicht (hoehere severity = spannenderer Hint)
    weights = [max(1, int(s.get("severity", 2))) for s in target_secrets]
    secret = random.choices(target_secrets, weights=weights, k=1)[0]

    # Prompt fuer subtilen Hinweis
    from app.core.llm_router import llm_call
    from app.models.account import get_user_profile as _get_prof
    _lang = (_get_prof().get("system_language", "de") or "de")
    LANG_NAMES = {"de": "German", "en": "English", "fr": "French", "es": "Spanish", "it": "Italian"}
    lang_name = LANG_NAMES.get(_lang, _lang)

    observers = [c for c in char_names if c != target_char]
    from app.core.prompt_templates import render_task
    sys_prompt, user_prompt = render_task(
        "random_event_secret_hint",
        location_name=location.get("name", loc_id),
        target_character=target_char,
        secret_content=secret.get("content", ""),
        observers_list=", ".join(observers),
        language_name=lang_name)

    try:
        response = llm_call(
            task="random_event",
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            agent_name="system")
        text = (response.content or "").strip()
        text = re.sub(r'<SPECIAL_\d+>|<\|[A-Z_]+\|>', '', text).strip().strip('"').strip("'").strip()
        if not text or len(text) < 10:
            return False

        # Event speichern mit Secret-Metadaten (Hint, keine volle Enthuellung)
        add_event(text, location_id=loc_id,
            ttl_hours=4, category="social",
            metadata={
                "hints_at_secret_id": secret.get("id", ""),
                "secret_owner": target_char,
                "observers": observers,
            })
        # Hinweis: Observers werden NICHT zu known_by hinzugefuegt — sie sehen nur
        # den Event-Text als Hint und muessen selbst die Verbindung ziehen.
        logger.info("Secret-Hint-Event @ %s: %s fuer %s (Ziel: %s)",
                    location.get("name", loc_id), text[:60], target_char, observers)
        return True
    except Exception as e:
        logger.debug("Secret-Hint-Event fehlgeschlagen: %s", e)
        return False


def _generate_event(loc_id: str,
    location: Dict[str, Any],
    category: str,
    char_names: List[str],
    active_events: List[Dict[str, Any]],
    settings: Dict[str, Any]):
    """Generiert ein Event per LLM und speichert es."""
    from app.core.llm_router import llm_call
    from app.models.events import add_event
    from app.models.character import get_character_current_feeling
    from app.core.danger_system import get_hazards

    # Kontext sammeln
    loc_name = location.get("name", loc_id)
    loc_desc = location.get("description", "")
    rooms = [r.get("name", "") for r in location.get("rooms", []) if r.get("name")]
    hazards = get_hazards(location)
    hazard_texts = [h.get("name", h.get("description", "")) for h in hazards if isinstance(h, dict)]

    # Characters mit Mood
    char_infos = []
    for c in char_names[:5]:
        mood = get_character_current_feeling(c) or ""
        char_infos.append(f"{c} (mood: {mood})" if mood else c)

    # Tageszeit
    hour = datetime.now().hour
    if 6 <= hour < 12:
        time_desc = "morning"
    elif 12 <= hour < 18:
        time_desc = "afternoon"
    elif 18 <= hour < 22:
        time_desc = "evening"
    else:
        time_desc = "night"

    # Letztes Event (Repeat-Vermeidung)
    last_event = ""
    if active_events:
        last_event = active_events[-1].get("text", "")

    # Blacklist
    blacklist = settings.get("event_blacklist", [])

    cat_info = CATEGORIES.get(category, {})
    cat_desc = cat_info.get("description", category)

    # Sprache des Accounts
    from app.models.account import get_user_profile
    _profile = get_user_profile()
    _lang = _profile.get("system_language", "de") or "de"
    LANG_NAMES = {"de": "German", "en": "English", "fr": "French", "es": "Spanish", "it": "Italian", "ja": "Japanese"}
    lang_name = LANG_NAMES.get(_lang, _lang)

    # Indoor/Outdoor-Setting der Location als Prompt-Hinweis (leer wenn nicht gesetzt).
    indoor_flag = (location.get("indoor") or "").strip().lower()
    if indoor_flag == "indoor":
        setting_block = ("Setting: Indoor (enclosed location — fit interior themes: "
                          "drafts, flickering light, knocking sounds, broken objects, "
                          "intruders, scents)")
    elif indoor_flag == "outdoor":
        setting_block = ("Setting: Outdoor (open-air location — fit weather, wildlife, "
                          "terrain, or arriving people)")
    else:
        setting_block = ""

    from app.core.prompt_templates import render_task
    sys_prompt, user_prompt = render_task(
        "random_event_general",
        location_name=loc_name,
        category=category,
        category_description=cat_desc,
        time_of_day=time_desc,
        location_description=loc_desc,
        setting_block=setting_block,
        rooms_block=f"Rooms: {', '.join(rooms[:6])}" if rooms else "",
        characters_block=f"Characters present: {', '.join(char_infos)}" if char_infos else "",
        hazards_block=f"Known hazards: {', '.join(hazard_texts)}" if hazard_texts else "",
        last_event_block=f'Last event here (avoid repetition): "{last_event}"' if last_event else "",
        blacklist_block=f"Do NOT mention: {', '.join(blacklist)}" if blacklist else "",
        language_name=lang_name)

    # LLM aufrufen
    try:
        response = llm_call(
            task="random_event",
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            agent_name="system")

        raw = (response.content or "").strip()
        # LLM-Artefakte bereinigen
        raw = re.sub(r'<SPECIAL_\d+>|<\|[A-Z_]+\|>', '', raw).strip()

        text, image_prompt = _parse_event_payload(raw)

        if not text or len(text) < 5:
            logger.debug("LLM-Event-Text zu kurz oder leer")
            return

        # Blacklist pruefen
        text_lower = text.lower()
        for bl in blacklist:
            if bl.lower() in text_lower:
                logger.info("Event geblockt (Blacklist '%s'): %s", bl, text[:60])
                return

        # Event speichern (image_prompt landet im metadata-Block — keine
        # Schema-Migration noetig, payload ist freier JSON-blob).
        ttl = cat_info.get("ttl_hours", 6)
        metadata: Dict[str, Any] = {}
        if image_prompt:
            metadata["image_prompt"] = image_prompt
        event = add_event(text, location_id=loc_id,
                         ttl_hours=ttl, category=category,
                         metadata=metadata or None)
        logger.info("Random Event [%s] @ %s: %s", category, loc_name, text[:80])

        # danger-Events blockieren das Verlassen der Location, bis sie geloest
        # sind (Avatar steckt fest, NPCs versuchen ueber random_events.resolve
        # zu loesen, Pathfinder routet drum herum).
        if category == "danger" and event and event.get("id") and loc_id:
            try:
                from app.models.rules import add_event_rule
                add_event_rule(
                    event_id=event["id"],
                    location_id=loc_id,
                    message=text,
                    action="leave",
                    name=f"Event @ {loc_name}")
            except Exception as _e:
                logger.debug("add_event_rule fehlgeschlagen: %s", _e)

        # Bild-Generierung: nur fuer disruption/danger und nur wenn die
        # Location ein background_image hat. Fehlt das BG, entsteht das
        # Event normal — nur die Bild-Ueberlagerung entfaellt.
        if event and category in ("disruption", "danger") and image_prompt:
            try:
                from app.core.event_images import trigger_event_image
                trigger_event_image(event["id"], loc_id, image_prompt, resolved=False)
            except Exception as _e:
                logger.debug("trigger_event_image fehlgeschlagen: %s", _e)

    except Exception as e:
        logger.error("Event-Generierung fehlgeschlagen: %s", e)


def _parse_event_payload(raw: str) -> Tuple[str, str]:
    """Parst das JSON-Output des Event-Generator-LLMs.

    Bei Parse-Fehler oder Fallback-Modus (kein JSON) wird der gesamte
    Text als Event-Text interpretiert und kein image_prompt zurueckgegeben.
    """
    import json as _json

    # Quick-Path: gueltiges JSON-Objekt
    text = raw
    # Markdown-Fences abstreifen falls vorhanden
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    if text.startswith("{"):
        try:
            payload = _json.loads(text)
            evt_text = str(payload.get("text", "")).strip().strip('"').strip("'").strip()
            image_prompt = str(payload.get("image_prompt", "")).strip()
            if evt_text:
                return evt_text, image_prompt
        except Exception:
            pass

    # Regex-Salvage: einzelnes Objekt im Output suchen
    m = re.search(r'\{[^{}]*"text"\s*:\s*"[^"]*"[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            payload = _json.loads(m.group(0))
            evt_text = str(payload.get("text", "")).strip().strip('"').strip("'").strip()
            image_prompt = str(payload.get("image_prompt", "")).strip()
            if evt_text:
                return evt_text, image_prompt
        except Exception:
            pass

    # Fallback: kein JSON erkannt — gesamter Text als Event-Text, kein Bild.
    fallback_text = raw.strip().strip('"').strip("'").strip()
    return fallback_text, ""


def _escalate_event(event: Dict[str, Any]):
    """Eskaliert ein unbeantwortetes disruption/danger Event."""
    from app.core.llm_router import llm_call
    from app.core.prompt_templates import render_task
    from app.models.events import add_event

    old_cat = event.get("category", "disruption")
    new_cat = "danger" if old_cat == "disruption" else "danger"
    old_text = event.get("text", "")
    location_id = event.get("location_id", "")

    # Sprache des Accounts
    from app.models.account import get_user_profile
    _profile = get_user_profile()
    _lang = _profile.get("system_language", "de") or "de"
    LANG_NAMES = {"de": "German", "en": "English", "fr": "French", "es": "Spanish", "it": "Italian", "ja": "Japanese"}
    lang_name = LANG_NAMES.get(_lang, _lang)

    sys_prompt, user_prompt = render_task(
        "random_event_escalation",
        old_text=old_text,
        new_category=new_cat,
        language_name=lang_name)

    try:
        response = llm_call(
            task="random_event",
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            agent_name="system")

        text = (response.content or "").strip()
        text = re.sub(r'<SPECIAL_\d+>|<\|[A-Z_]+\|>', '', text).strip()
        text = text.strip('"').strip("'").strip()

        if text and len(text) >= 5:
            ttl = CATEGORIES.get(new_cat, {}).get("ttl_hours", 8)
            add_event(text, location_id=location_id,
                     ttl_hours=ttl, category=new_cat,
                     escalation_of=event.get("id"))
            logger.info("Event eskaliert [%s→%s]: %s → %s",
                        old_cat, new_cat, old_text[:40], text[:40])

    except Exception as e:
        logger.error("Event-Eskalation fehlgeschlagen: %s", e)


def _had_chat_since(location_id: str, since: datetime) -> bool:
    """Prueft ob seit einem Zeitpunkt Chat-Aktivitaet an einer Location stattfand."""
    try:
        from app.models.character import list_available_characters, get_character_current_location
        from app.models.chat import get_chat_history

        chars = list_available_characters()
        for char in chars:
            if get_character_current_location(char) != location_id:
                continue
            history = get_chat_history(char)
            if not history:
                continue
            # Letzte Nachricht pruefen
            last = history[-1] if history else None
            if last:
                try:
                    ts = datetime.fromisoformat(last.get("timestamp", ""))
                    if ts > since:
                        return True
                except (ValueError, TypeError):
                    pass
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Event Resolution (validation, proactive solving, attempts)
# ---------------------------------------------------------------------------

def _on_resolution_cooldown(event: Dict[str, Any], cooldown_min: int = RESOLUTION_COOLDOWN_MIN) -> bool:
    """True wenn das Event noch im Resolution-Cooldown ist (letzter Versuch zu jung)."""
    try:
        last = (event.get("resolution") or {}).get("last_attempt_at")
        if not last:
            return False
        last_dt = datetime.fromisoformat(last)
        return datetime.now() - last_dt < timedelta(minutes=cooldown_min)
    except (ValueError, TypeError):
        return False


def validate_solution(event: Dict[str, Any],
    solution_text: str,
    actor_name: str,
    actors_joint: Optional[List[str]] = None) -> Dict[str, Any]:
    """Laesst das Tool-LLM pruefen ob die Lösung plausibel das Event aufloest.

    Character-Traits (aus den `hint_thresholds` der Stat-Felder im Template)
    fliessen als Kontext mit ein — ein mutiger/aufmerksamer Character hat bei
    Grenzfaellen bessere Karten, ein aengstlicher/unaufmerksamer scheitert eher.

    Returns dict: {"resolved": bool, "reason": str}
    """
    import json as _json
    from app.core.llm_router import llm_call
    from app.core.stat_hints import format_character_with_hints

    actor_with_caps = format_character_with_hints(actor_name)
    joint_with_caps = [format_character_with_hints(j) for j in (actors_joint or [])]
    joint_txt = f" (together with {', '.join(joint_with_caps)})" if joint_with_caps else ""

    from app.core.prompt_templates import render_task
    sys_prompt, user_prompt = render_task(
        "random_event_validate_solution",
        event_text=event.get("text", ""),
        event_category=event.get("category", ""),
        actor_with_caps=actor_with_caps,
        joint_block=joint_txt,
        solution_text=solution_text.strip())

    try:
        response = llm_call(
            task="random_event",
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            agent_name=actor_name)
        raw = (response.content or "").strip()
        raw = re.sub(r'<SPECIAL_\d+>|<\|[A-Z_]+\|>', '', raw).strip()
        # JSON extrahieren (LLM gibt manchmal Prosa davor/danach)
        m = re.search(r'\{[^{}]*"resolved"[^{}]*\}', raw, re.DOTALL)
        payload = _json.loads(m.group(0)) if m else _json.loads(raw)
        return {
            "resolved": bool(payload.get("resolved", False)),
            "reason": str(payload.get("reason", ""))[:200],
        }
    except Exception as e:
        logger.warning("validate_solution Fehler: %s", e)
        return {"resolved": False, "reason": f"validator-error: {e}"}


def try_resolve_events():
    """Proaktive Event-Aufloesung: Characters versuchen offene Events zu loesen.

    Aufgerufen alle 5 Minuten aus ThoughtLoop._tick(). Wahlt pro Call
    hoechstens 1 Event (globale Rate-Limitierung).
    """
    if not RESOLUTION_PROACTIVE_ENABLED:
        return

    try:
        from app.models.events import get_all_events, resolve_event, record_attempt
        from app.models.character import (
            list_available_characters, get_character_current_location,
            is_character_sleeping)
        from app.models.account import is_player_controlled

        # Offene disruption/danger Events ohne aktiven Cooldown
        candidates = []
        for evt in get_all_events():
            if evt.get("category") not in ("disruption", "danger"):
                continue
            if evt.get("resolved"):
                continue
            if not evt.get("location_id"):
                continue
            if _on_resolution_cooldown(evt):
                continue
            candidates.append(evt)
        if not candidates:
            return

        # Danger vor Disruption, sonst juengstes zuerst
        candidates.sort(key=lambda e: (0 if e.get("category") == "danger" else 1,
                                        e.get("created_at", "")), reverse=False)
        event = candidates[0]
        location_id = event["location_id"]

        # Characters an dieser Location (player-controlled ausgeschlossen
        # damit wir nicht dem User seine Chat-Antwort vorwegnehmen)
        actors = []
        for c in list_available_characters():
            if get_character_current_location(c) != location_id:
                continue
            if is_character_sleeping(c):
                continue
            try:
                if is_player_controlled(c):
                    continue
            except Exception:
                pass
            actors.append(c)
        if not actors:
            return

        # Primary + optional zweiter Helfer (Multi-Char-Kooperation)
        primary = actors[0]
        joint = actors[1:2] if len(actors) > 1 else []

        solution_text = _generate_solution_rp(primary, event, joint)
        if not solution_text:
            return

        result = validate_solution(event, solution_text, primary, joint)
        outcome = "success" if result["resolved"] else "fail"
        record_attempt(event["id"], primary, solution_text,
                       outcome=outcome, reason=result.get("reason", ""),
                       joint_with=joint)

        if result["resolved"]:
            resolve_event(event["id"], resolved_by=primary,
                          resolved_text=solution_text[:200])
            _diary_log_resolution(primary, event, solution_text, True, joint)
            _notify_resolution(primary, event, solution_text, joint)
        else:
            _diary_log_resolution(primary, event, solution_text, False,
                                   joint, reason=result.get("reason", ""))

    except Exception as e:
        logger.debug("try_resolve_events error: %s", e)


def _generate_solution_rp(actor: str, event: Dict[str, Any],
    joint: Optional[List[str]] = None) -> str:
    """Laesst den Character (RP-LLM) beschreiben wie er das Event loest. Kurze Antwort."""
    from app.models.character import get_character_personality
    from app.core.llm_router import llm_call

    personality = get_character_personality(actor) or ""
    joint_txt = f" You are with {', '.join(joint)}." if joint else ""

    from app.core.prompt_templates import render_task
    sys_prompt, user_prompt = render_task(
        "random_event_solution_rp",
        actor=actor,
        personality=personality,
        event_text=event.get("text", ""),
        joint_block=joint_txt)

    try:
        response = llm_call(
            task="thought",
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            agent_name=actor)
        text = (response.content or "").strip()
        text = re.sub(r'<SPECIAL_\d+>|<\|[A-Z_]+\|>', '', text).strip()
        # Marker entfernen (falls RP-LLM **I feel X** anhaengt)
        text = re.sub(r'\n?\s*\*\*I\s+(feel|do|am\s+at)\s+[^*]+\*\*', '', text, flags=re.IGNORECASE).strip()
        return text
    except Exception as e:
        logger.warning("solution RP-Generierung Fehler: %s", e)
        return ""


def _diary_log_resolution(actor: str, event: Dict[str, Any],
    solution_text: str, success: bool,
    joint: Optional[List[str]] = None,
    reason: str = ""):
    """Erfasst den Loesungsversuch als episodische Memory.

    Die Daily-Summary (Diary) wird automatisch aus Memories aggregiert —
    daher reicht ein Memory-Eintrag pro beteiligtem Character.
    """
    try:
        from app.models.memory import add_memory
        event_text = event.get("text", "")
        joint_txt = f" (mit {', '.join(joint)})" if joint else ""
        if success:
            content = f"Event geloest{joint_txt}: {event_text[:120]} — Aktion: {solution_text[:180]}"
            importance = 4
        else:
            content = f"Versuch fuer {event_text[:120]}{joint_txt}: {solution_text[:150]}. Erfolglos ({reason[:100]})."
            importance = 2
        all_actors = [actor] + list(joint or [])
        for p in all_actors:
            add_memory(p, content, memory_type="episodic",
                       importance=importance, tags=["event_resolution"])
    except Exception as e:
        logger.debug("Memory-Log Fehler: %s", e)


def _notify_resolution(actor: str, event: Dict[str, Any],
    solution_text: str, joint: Optional[List[str]] = None):
    """Sendet eine Proactive-Notification dass das Event geloest wurde."""
    try:
        from app.models.notifications import create_notification
        joint_txt = f" (mit {', '.join(joint)})" if joint else ""
        content = f"Event geloest{joint_txt}: {event.get('text', '')[:200]} — {solution_text[:500]}"
        create_notification(
            character=actor,
            content=content,
            notification_type="event_resolved",
            metadata={"event_id": event.get("id", ""), "joint_with": joint or []})
    except Exception as e:
        logger.debug("Notification Fehler: %s", e)
