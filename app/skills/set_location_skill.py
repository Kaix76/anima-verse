"""SetLocation Skill - Ortswechsel per Chat

Leichtgewichtiger Skill, der dem Agenten erlaubt seinen Aufenthaltsort,
Raum und Aktivitaet zu aendern. Wird automatisch vom Chat-System erkannt
wenn der User z.B. sagt "Du bist jetzt zu Hause" oder "Reise ins Buero".
"""
import random
from typing import Any, Dict

from .base import BaseSkill, ToolSpec

from app.core.log import get_logger
logger = get_logger("set_location")

from app.models.character import (
    save_character_current_location,
    save_character_current_activity,
    save_character_current_room,
    get_character_current_location,
    get_character_config,
    set_movement_target,
    get_known_locations)
from app.models.world import (
    list_locations, get_location_rooms, get_room_by_name,
        find_room_by_activity,
        get_location_by_id,
        find_path_through_known)


class SetLocationSkill(BaseSkill):
    """
    Skill zum Setzen des Aufenthaltsortes, Raums und Aktivitaet eines Agenten.

    Der Agent kann diesen Skill nutzen wenn der User den Ort aendern moechte.
    Der Skill validiert den Ort gegen die definierten World-Locations und
    setzt automatisch einen passenden Raum und eine Aktivitaet.
    """

    SKILL_ID = "setlocation"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        from app.core.prompt_templates import load_skill_meta
        meta = load_skill_meta("set_location")
        self.name = meta["name"]
        self.description = meta["description"]
        self._defaults = {}

    def execute(self, raw_input: str) -> str:
        """Setzt Location, Raum und Activity fuer den Agenten.

        Input-Format (vom LLM):
            Ortsname, z.B. "zu Hause" oder "Buero"
            Mit Raum: "zu Hause, Kueche"
            Mit Raum + Activity: "zu Hause, Kueche, Kochen"
            Mit Activity (findet Raum automatisch): "zu Hause, Kochen"
        """
        if not self.enabled:
            return "SetLocation Skill ist nicht verfuegbar."

        try:
            return self._execute_inner(raw_input)
        except Exception as e:
            logger.error("Fehler in SetLocation: %s", e)
            return f"Fehler beim Setzen der Location: {e}"

    def _execute_inner(self, raw_input: str) -> str:
        ctx = self._parse_base_input(raw_input)
        input_text = ctx.get("input", raw_input).strip()
        character_name = ctx.get("agent_name", "").strip()
        user_id = ctx.get("user_id", "").strip()

        if not character_name:
            return "Fehler: Agent-Name fehlt."
        if not input_text:
            return "Fehler: Kein Ort angegeben."

        # Input parsen: "Location, Room/Activity, Activity"
        parts = [p.strip() for p in input_text.split(",")]
        requested_location = parts[0]
        requested_second = parts[1] if len(parts) > 1 else None
        requested_third = parts[2] if len(parts) > 2 else None

        logger.info(f"Ortswechsel fuer {character_name}")
        logger.info(f"Angefragt: Location='{requested_location}', "
              f"Second='{requested_second}', Third='{requested_third}'")

        # Location in World-Locations suchen (User-Level)
        locations = list_locations()
        matched_location = None
        for loc in locations:
            if loc["name"].lower() == requested_location.lower():
                matched_location = loc
                break

        # Fuzzy-Match: Teilstring-Suche als Fallback
        if not matched_location:
            for loc in locations:
                if requested_location.lower() in loc["name"].lower() or loc["name"].lower() in requested_location.lower():
                    matched_location = loc
                    break

        # Description-Match: Suchbegriff in der Ort-Beschreibung finden
        if not matched_location:
            for loc in locations:
                desc = loc.get("description", "").lower()
                if desc and requested_location.lower() in desc:
                    matched_location = loc
                    break

        # Fallback: Raum am aktuellen Standort matchen
        if not matched_location:
            current_loc_id = get_character_current_location(character_name)
            current_loc = get_location_by_id(current_loc_id) if current_loc_id else None
            if current_loc:
                current_rooms = get_location_rooms(current_loc)
                matched_room_fallback = None
                # Exakt
                for room in current_rooms:
                    if room.get("name", "").lower() == requested_location.lower():
                        matched_room_fallback = room
                        break
                # Fuzzy
                if not matched_room_fallback:
                    for room in current_rooms:
                        rn = room.get("name", "").lower()
                        rl = requested_location.lower()
                        if rl in rn or rn in rl:
                            matched_room_fallback = room
                            break
                if matched_room_fallback:
                    # Raum am aktuellen Ort gefunden — Location beibehalten, Raum wechseln
                    matched_location = current_loc
                    requested_second = matched_room_fallback.get("name", "")
                    requested_third = None  # Activity aus Raum ableiten
                    logger.info(f"Raum '{requested_location}' am aktuellen Ort "
                          f"'{current_loc.get('name', '')}' gefunden")

        # Home-Alias: "home", "zu hause", "zuhause" etc. auf home_location aus Character-Config aufloesen
        if not matched_location:
            home_aliases = {"home", "zu hause", "zuhause", "nach hause", "daheim"}
            if requested_location.lower() in home_aliases:
                cfg = get_character_config(character_name)
                home_loc_id = cfg.get("home_location", "")
                # Offmap-Sentinel: Char hat keine Karten-Heimat — er
                # verschwindet einfach von der Map. enter_offmap_sleep
                # speichert die letzte Position fuer den Wakeup.
                from app.models.character import (
                    OFFMAP_SLEEP_SENTINEL, enter_offmap_sleep)
                if home_loc_id == OFFMAP_SLEEP_SENTINEL:
                    if enter_offmap_sleep(character_name):
                        return f"{character_name} hat sich zurueckgezogen — offmap."
                    return f"{character_name} ist bereits offmap."
                if home_loc_id:
                    matched_location = get_location_by_id(home_loc_id)
                    if matched_location:
                        home_room_id = cfg.get("home_room", "")
                        if home_room_id and not requested_second:
                            # Home-Room als zweiten Part setzen (falls nicht explizit angegeben)
                            rooms = get_location_rooms(matched_location)
                            for r in rooms:
                                if r.get("id", "") == home_room_id:
                                    requested_second = r.get("name", "")
                                    break
                        logger.info(f"Home-Alias '{requested_location}' -> Location '{matched_location.get('name', '')}' (ID: {home_loc_id})")

        if not matched_location:
            available_parts = [loc["name"] for loc in locations] if locations else []
            # Raeume am aktuellen Standort anhaengen
            current_loc_id = get_character_current_location(character_name)
            current_loc = get_location_by_id(current_loc_id) if current_loc_id else None
            if current_loc:
                current_rooms = get_location_rooms(current_loc)
                room_names = [r.get("name", "") for r in current_rooms if r.get("name")]
                if room_names:
                    available_parts.extend(room_names)
            available = ", ".join(available_parts) if available_parts else "keine definiert"
            logger.warning(f"Ort nicht gefunden: '{requested_location}'. Verfuegbar: {available}")
            return f"Ort '{requested_location}' nicht gefunden. Verfuegbare Orte: {available}"

        location_name = matched_location["name"]
        location_id = matched_location.get("id", location_name)

        # Restrictions-Check: Darf der Character diesen Ort betreten?
        from app.core.danger_system import check_location_access
        allowed, deny_reason = check_location_access(character_name, matched_location)
        if not allowed:
            logger.info("Location-Zugang verweigert: %s -> %s: %s", character_name, location_name, deny_reason)
            return deny_reason

        # Rules-Engine: Blockade-Regeln pruefen
        from app.models.rules import check_access
        rules_ok, rules_reason = check_access(character_name, location_id)
        if not rules_ok:
            logger.info("Rule blockiert Zugang: %s -> %s: %s", character_name, location_name, rules_reason)
            try:
                from app.models.character import record_access_denied
                record_access_denied(character_name, location_id, location_name, rules_reason)
            except Exception:
                logger.debug("record_access_denied failed", exc_info=True)
            _trigger_access_denied_thought(character_name, location_name, rules_reason)
            return rules_reason

        # Passable-Tiles (Durchgangsorte) sind keine Ziele — der LLM darf
        # nicht direkt dort hinwandern. Pathfinder kann sie aber als
        # Zwischenschritt nutzen, wenn der Character sie kennt.
        if matched_location.get("passable"):
            logger.info("SetLocation auf Durchgangsort abgelehnt: %s -> %s",
                        character_name, location_name)
            return (f"{location_name} ist ein Durchgangsort, kein Ziel. "
                    f"Waehle einen richtigen Ort als Reiseziel.")

        # Walk-Modus: Cross-Location-Move => movement_target setzen, Schritt
        # erfolgt im naechsten AgentLoop-Tick. Same-Location (nur Raum-Wechsel)
        # bleibt instant. Legacy-Characters (kein known_locations-Feld)
        # teleportieren wie bisher — bekanntes Verhalten der alten Welten.
        current_loc_id_now = get_character_current_location(character_name) or ""
        known_list = get_known_locations(character_name)
        is_cross_location = bool(current_loc_id_now and current_loc_id_now != location_id)
        # Walk-Mode greift NUR bei NPCs — der Spieler-Avatar bewegt sich
        # direkt (der AgentLoop-Walk-Step skippt player-controlled, sodass
        # ein gesetzter movement_target sonst ewig auf "intent" stehen bleibt
        # waehrend der Char visuell schon woanders ist).
        from app.models.account import is_player_controlled as _is_player
        if is_cross_location and known_list is not None and not _is_player(character_name):
            path = find_path_through_known(current_loc_id_now, location_id, known_list)
            if not path:
                logger.info("Kein bekannter Pfad %s -> %s fuer %s",
                            current_loc_id_now, location_id, character_name)
                # Tagebuch-Eintrag: gescheiterter Reise-Versuch
                try:
                    from app.models.character import _record_state_change
                    _record_state_change(character_name, "travel_failed",
                        location_name,
                        metadata={"location_id": location_id,
                                  "reason": "no_known_path"})
                except Exception:
                    logger.debug("travel_failed record failed", exc_info=True)
                return (f"Du kennst den Weg nach {location_name} (noch) nicht. "
                        f"Du musst zuerst dorthin gefuehrt werden oder einen "
                        f"angrenzenden Ort entdecken.")
            set_movement_target(character_name, location_id)
            steps = max(0, len(path) - 1)
            logger.info("Walk-Mode: %s -> %s (%d Schritte)",
                        character_name, location_name, steps)
            return (f"Du machst dich auf den Weg nach {location_name}. "
                    f"Geschaetzt {steps} Schritt(e) ueber bekannte Orte.")

        rooms = get_location_rooms(matched_location)

        # Alle verfuegbaren Activities (Bibliothek + Location + Character)
        from app.models.activity_library import get_activity_names as _lib_act_names
        all_activity_names = _lib_act_names(character_name, location_id=location_id)

        # Raum und Activity bestimmen
        matched_room = None
        activity = ""

        if requested_second:
            # 1. Versuche zweiten Part als Raum-Name zu matchen
            matched_room = get_room_by_name(matched_location, requested_second)

            if matched_room:
                # Rules-Check fuer Raum
                room_rules_ok, room_rules_reason = check_access(character_name, location_id, room_id=matched_room.get("id", ""))
                if not room_rules_ok:
                    room_label = matched_room.get("name", "")
                    logger.info("Rule blockiert Raum: %s -> %s: %s",
                               character_name, room_label, room_rules_reason)
                    try:
                        from app.models.character import record_access_denied
                        record_access_denied(character_name, location_id,
                            f"{location_name} / {room_label}" if room_label else location_name,
                            room_rules_reason)
                    except Exception:
                        logger.debug("record_access_denied failed", exc_info=True)
                    _trigger_access_denied_thought(character_name,
                        f"{location_name} / {room_label}" if room_label else location_name,
                        room_rules_reason)
                    return room_rules_reason
                # Dritter Part = Activity innerhalb des Raums
                if requested_third:
                    room_acts = [
                        (a.get("name", "") if isinstance(a, dict) else str(a))
                        for a in matched_room.get("activities", [])
                    ]
                    # Exakt
                    for act_name in room_acts:
                        if act_name.lower() == requested_third.lower():
                            activity = act_name
                            break
                    # Fuzzy
                    if not activity:
                        for act_name in room_acts:
                            if requested_third.lower() in act_name.lower() or act_name.lower() in requested_third.lower():
                                activity = act_name
                                break
                    if not activity:
                        activity = requested_third  # Freitext
                else:
                    # Zufaellige Activity aus dem Raum
                    room_acts = [
                        (a.get("name", "") if isinstance(a, dict) else str(a))
                        for a in matched_room.get("activities", [])
                    ]
                    if room_acts:
                        activity = random.choice(room_acts)
            else:
                # 2. Zweiter Part ist kein Raum → versuche als Activity
                for act_name in all_activity_names:
                    if act_name.lower() == requested_second.lower():
                        activity = act_name
                        break
                if not activity:
                    for act_name in all_activity_names:
                        if requested_second.lower() in act_name.lower() or act_name.lower() in requested_second.lower():
                            activity = act_name
                            break
                if not activity:
                    activity = requested_second  # Freitext

                # Raum aus Activity ableiten
                if activity:
                    matched_room = find_room_by_activity(matched_location, activity)

        # Falls kein Raum gefunden: zufaelligen Raum waehlen
        if not matched_room and rooms:
            matched_room = random.choice(rooms)
            # Activity aus dem gewaehlten Raum
            if not activity:
                room_acts = [
                    (a.get("name", "") if isinstance(a, dict) else str(a))
                    for a in matched_room.get("activities", [])
                ]
                if room_acts:
                    activity = random.choice(room_acts)

        # Falls immer noch keine Activity aber welche vorhanden
        if not activity and all_activity_names:
            if len(all_activity_names) == 1:
                activity = all_activity_names[0]
            else:
                activity = random.choice(all_activity_names)

        room_id = matched_room.get("id", "") if matched_room else ""
        room_name = matched_room.get("name", "") if matched_room else ""

        # Status setzen: Location-ID speichern (nicht Name)
        save_character_current_location(character_name, location_id)
        save_character_current_room(character_name, room_id)
        if activity:
            save_character_current_activity(character_name, activity)

        # Avatar-Follow: Location-Wechsel laeuft NICHT mehr automatisch
        # (Avatar bleibt wo der User ihn hat). Nur Raum-Wechsel wird
        # uebernommen, falls der Avatar bereits an der gleichen Location ist.
        try:
            from app.models.account import get_active_character
            player = get_active_character()
            if player and player != character_name:
                player_loc = get_character_current_location(player)
                if player_loc and player_loc == location_id:
                    save_character_current_room(player, room_id)
                    logger.info("Avatar %s folgt %s -> Room %s", player, character_name, room_id)
        except Exception as _e:
            logger.warning("Avatar-Room-Follow fehlgeschlagen: %s", _e)

        # Outfit-Compliance nach Dress-Code der neuen Location/Room —
        # zentraler Helper waehlt Activity > Raum > Location.
        from app.models.inventory import apply_outfit_type_compliance
        from app.core.outfit_rules import resolve_target_outfit_type
        _target_type = resolve_target_outfit_type(character_name)
        if _target_type:
            _comp = apply_outfit_type_compliance(character_name, _target_type)
            if _comp.get("swapped") or _comp.get("removed"):
                logger.info("Outfit-Compliance [%s]: %d getauscht, %d entfernt (Typ: %s)",
                             character_name, len(_comp.get("swapped", [])),
                             len(_comp.get("removed", [])), _target_type)

        logger.info(f"Gesetzt: Location='{location_name}' (ID: {location_id}), "
              f"Room='{room_name}' (ID: {room_id}), Activity='{activity}'")

        # Bestaetigung
        result = f"Standort aktualisiert: {location_name}"
        if room_name:
            result += f", Raum: {room_name}"
        if activity:
            result += f" ({activity})"
        if matched_location.get("description"):
            result += f"\nOrt-Beschreibung: {matched_location['description']}"
        if matched_room and matched_room.get("description"):
            result += f"\nRaum-Beschreibung: {matched_room['description']}"

        return result

    def get_usage_instructions(self, format_name: str = "", **kwargs) -> str:
        from app.core.tool_formats import format_example
        fmt = format_name or "tag"
        return format_example(fmt, self.name, "Büro, Küche, kaffee_kochen")

    def _build_locations_hint(self, character_name: str) -> str:
        """Baut eine Liste der verfuegbaren Locations fuer die Tool-Beschreibung."""
        try:
            locations = list_locations()
            if not locations:
                return ""
            hints = []
            for loc in locations:
                name = loc.get("name", "")
                if not name:
                    continue
                rooms = get_location_rooms(loc)
                room_names = [r.get("name", "") for r in rooms if r.get("name")]
                if room_names:
                    hints.append(f"{name} (rooms: {', '.join(room_names)})")
                else:
                    hints.append(name)
            if hints:
                return " Available locations: " + "; ".join(hints) + "."
            return ""
        except Exception:
            return ""

    def as_tool(self, **kwargs) -> ToolSpec:
        user_id = kwargs.get("user_id", "")
        character_name = kwargs.get("agent_name", "")
        locations_hint = self._build_locations_hint(character_name)
        return ToolSpec(
            name=self.name,
            description=(
                f"{self.description}. "
                f"Input: location name, optionally with room and/or activity "
                f"(e.g. 'Büro, Küche' or 'home, bedroom, sleeping'). "
                f"Cross-location moves walk one grid-step per tick along a "
                f"path through locations you already know — you set the "
                f"destination once, the system carries you over multiple "
                f"ticks. Within the same location (room change), the move is "
                f"instant. "
                f"IMPORTANT: You MUST use one of the available location names exactly as listed. "
                f"Do NOT invent location names."
                f"{locations_hint}"
            ),
            func=self.execute)


def _trigger_access_denied_thought(character_name: str, location_label: str, reason: str) -> None:
    """Bumps the character in the AgentLoop so they handle the access-
    denied event in their next thought turn. The state_history entry
    written by the caller carries the actual context (location, reason);
    the recent_activity block in agent_thought.md surfaces it.
    """
    try:
        from app.core.agent_loop import get_agent_loop
        get_agent_loop().bump(character_name)
        logger.info("Access-Denied -> AgentLoop bump: %s @ %s",
                    character_name, location_label)
    except Exception as e:
        logger.debug("access_denied bump failed: %s", e)
