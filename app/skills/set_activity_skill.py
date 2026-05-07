"""SetActivity Skill - Aktivitaetswechsel per Chat

Leichtgewichtiger Skill, der dem Agenten erlaubt seine Aktivitaet zu aendern,
ohne den Standort wechseln zu muessen. Wird automatisch vom Chat-System erkannt
wenn der User z.B. sagt "Lass uns einen Kaffee trinken" oder "Ich lese ein Buch".
"""
import json
from typing import Any, Dict

from .base import BaseSkill, ToolSpec

from app.core.log import get_logger
logger = get_logger("set_activity")

from app.models.character import (
    save_character_current_activity,
    save_character_current_room,
    get_character_current_location)
from app.models.world import (
    get_location_by_id,
    find_room_by_activity)
from app.core.activity_engine import (
    evaluate_condition,
    check_cooldown,
    check_partner_available,
    set_cooldown_timestamp,
    execute_trigger,
    _find_activity_definition,
    get_last_matched_partner)


class SetActivitySkill(BaseSkill):
    """
    Skill zum Setzen der Aktivitaet eines Agenten am aktuellen Standort.

    Der Agent kann diesen Skill nutzen wenn der User die Aktivitaet aendern
    moechte, ohne den Ort zu wechseln. Der Skill validiert die Aktivitaet
    gegen die am aktuellen Standort definierten Aktivitaeten und setzt
    automatisch den passenden Raum.
    """

    SKILL_ID = "setactivity"

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        from app.core.prompt_templates import load_skill_meta
        meta = load_skill_meta("set_activity")
        self.name = meta["name"]
        self.description = meta["description"]
        self._defaults = {}

    def execute(self, raw_input: str) -> str:
        """Setzt die Aktivitaet fuer den Agenten am aktuellen Standort.

        Input-Format (vom LLM):
            Aktivitaetsname, z.B. "Kaffee trinken" oder "Lesen"
        """
        if not self.enabled:
            return "SetActivity Skill ist nicht verfuegbar."

        try:
            return self._execute_inner(raw_input)
        except Exception as e:
            logger.error("Fehler in SetActivity: %s", e)
            return f"Fehler beim Setzen der Aktivitaet: {e}"

    def _execute_inner(self, raw_input: str) -> str:
        ctx = self._parse_base_input(raw_input)
        input_text = ctx.get("input", raw_input).strip()
        agent_name = ctx.get("agent_name", "").strip()
        user_id = ctx.get("user_id", "").strip()

        if not agent_name:
            return "Fehler: Agent-Name fehlt."
        if not input_text:
            return "Fehler: Keine Aktivitaet angegeben."

        # Optional JSON-Input: {"activity": "...", "target": "..."} — target
        # ueberschreibt das Subjekt der Aktion. Plaintext bleibt selbst-bezogen.
        target_name = ""
        if input_text.startswith("{"):
            try:
                parsed = json.loads(input_text)
                if isinstance(parsed, dict):
                    _act = (parsed.get("activity") or parsed.get("name") or "").strip()
                    _tgt = (parsed.get("target") or "").strip()
                    if _act:
                        input_text = _act
                    if _tgt:
                        target_name = _tgt
            except Exception:
                pass

        # Effektives Subjekt der Aktion — Target falls angegeben, sonst Agent selbst.
        # Cooldowns, Effects, Conditions, Raum/Outfit-Compliance laufen alle
        # auf character_name; das passt natuerlich, weil das Subjekt der
        # Aktion das ist was sich aendert.
        character_name = target_name or agent_name
        if target_name and target_name != agent_name:
            # Existenz pruefen — sonst wuerden wir blind eine ID fabrizieren
            try:
                from app.models.character import list_available_characters
                if target_name not in list_available_characters():
                    return f"Fehler: Target '{target_name}' nicht bekannt."
            except Exception:
                pass

        requested_activity = input_text.strip()

        if target_name and target_name != agent_name:
            logger.info("Aktivitaetswechsel von %s fuer %s: '%s'",
                        agent_name, character_name, requested_activity)
        else:
            logger.info("Aktivitaetswechsel fuer %s: '%s'",
                        character_name, requested_activity)

        # Aktuellen Standort ermitteln
        current_loc_id = get_character_current_location(character_name)
        current_loc = get_location_by_id(current_loc_id) if current_loc_id else None

        activity = ""
        matched_room = None

        if current_loc:
            location_name = current_loc.get("name", current_loc_id)

            # Alle verfuegbaren Activities am aktuellen Standort (Bibliothek + Location + Character)
            from app.models.activity_library import get_available_activities as _get_avail, _get_all_names
            all_activities = _get_avail(character_name, current_loc_id)
            req_lower = requested_activity.lower()

            # Exakt-Match (alle Namensvarianten + ID)
            for act in all_activities:
                aid = act.get("id", "")
                all_names = _get_all_names(act)
                if aid.lower() == req_lower or any(n.lower() == req_lower for n in all_names):
                    activity = act.get("name", "")
                    break

            # Fuzzy-Match (alle Namensvarianten + ID)
            if not activity:
                for act in all_activities:
                    aid = act.get("id", "").lower()
                    all_names = [n.lower() for n in _get_all_names(act)]
                    if req_lower in aid or aid in req_lower:
                        activity = act.get("name", "")
                        break
                    for n in all_names:
                        if req_lower in n or n in req_lower:
                            activity = act.get("name", "")
                            break
                    if activity:
                        break

            # Freitext-Fallback: Activity nicht in der Liste, aber trotzdem setzen
            if not activity:
                activity = requested_activity

            # Passenden Raum zur Activity finden
            matched_room = find_room_by_activity(current_loc, activity)
        else:
            # Kein Standort gesetzt — Activity trotzdem als Freitext setzen
            location_name = ""
            activity = requested_activity

        # --- Role-Check (Bibliothek) ---
        # required_roles wird sonst nur in get_available_activities gefiltert.
        # Wenn das LLM via Freitext eine rollen-gebundene Activity aufruft
        # (z.B. "Posen" ohne 'photomodel'-Rolle), muss der Skill ablehnen —
        # sonst landet die Activity trotz Filter im current_activity.
        from app.models.activity_library import get_library_activity, find_library_activity_by_name
        lib_act = get_library_activity(activity) or find_library_activity_by_name(activity)
        if lib_act:
            req_roles_raw = lib_act.get("required_roles", []) or []
            if isinstance(req_roles_raw, str):
                req_roles_raw = req_roles_raw.split(",")
            req_roles = {str(r).strip().lower() for r in req_roles_raw if str(r).strip()}
            if req_roles:
                from app.models.character import get_character_config
                _cfg = get_character_config(character_name) or {}
                _char_roles_raw = _cfg.get("roles", []) or []
                if isinstance(_char_roles_raw, str):
                    _char_roles_raw = _char_roles_raw.split(",")
                _char_roles = {str(r).strip().lower() for r in _char_roles_raw if str(r).strip()}
                if not (req_roles & _char_roles):
                    return (f"Aktivitaet '{activity}' nicht verfuegbar: "
                            f"Erforderliche Rollen {sorted(req_roles)}, "
                            f"{character_name} hat {sorted(_char_roles) or 'keine'}.")

        # --- requires_partner Check (Bibliothek + Spezial) ---
        if lib_act and lib_act.get("requires_partner"):
            partner_ok, partner_reason = check_partner_available(character_name, current_loc_id or "")
            if not partner_ok:
                fallback_id = lib_act.get("fallback_activity", "")
                if fallback_id:
                    fb_act = get_library_activity(fallback_id) or find_library_activity_by_name(fallback_id)
                    if fb_act:
                        activity = fb_act.get("name", fallback_id)
                        logger.info("Partner-Fallback: '%s' -> '%s'", requested_activity, activity)
                        matched_room = find_room_by_activity(current_loc, activity) if current_loc else None
                    else:
                        return f"Aktivitaet '{activity}' braucht einen Partner: {partner_reason}"
                else:
                    return f"Aktivitaet '{activity}' braucht einen Partner: {partner_reason}"

        # --- Condition-Check fuer Spezial-Aktivitaeten ---
        act_def = _find_activity_definition(character_name, activity)
        if act_def:
            # requires_partner auch bei Spezial-Aktivitaeten pruefen
            if act_def.get("requires_partner") and not (lib_act and lib_act.get("requires_partner")):
                partner_ok, partner_reason = check_partner_available(character_name, current_loc_id or "")
                if not partner_ok:
                    fallback_id = act_def.get("fallback_activity", "")
                    if fallback_id:
                        fb_act = get_library_activity(fallback_id) or find_library_activity_by_name(fallback_id)
                        if fb_act:
                            activity = fb_act.get("name", fallback_id)
                            logger.info("Partner-Fallback (special): '%s' -> '%s'", requested_activity, activity)
                            matched_room = find_room_by_activity(current_loc, activity) if current_loc else None
                            act_def = _find_activity_definition(character_name, activity)
                        else:
                            return f"Aktivitaet '{activity}' braucht einen Partner: {partner_reason}"
                    else:
                        return f"Aktivitaet '{activity}' braucht einen Partner: {partner_reason}"

            # Condition pruefen
            condition = act_def.get("condition", "") if act_def else ""
            if condition:
                passed, reason = evaluate_condition(condition, character_name, current_loc_id or "")
                if not passed:
                    return f"Aktivitaet '{activity}' nicht verfuegbar: {reason}"

            # Cooldown pruefen
            cd_ok, cd_msg = check_cooldown(character_name, activity)
            if not cd_ok:
                return f"Aktivitaet '{activity}' nicht verfuegbar: {cd_msg}"

            # consumes_item: blockiert wenn Item fehlt, sonst spaeter (nach save) verbrauchen
            _consumes = (act_def.get("consumes_item") or "").strip()
            if _consumes:
                from app.models.inventory import has_item, get_item
                if not has_item(character_name, _consumes):
                    _it = get_item(_consumes)
                    _name = _it.get("name", _consumes) if _it else _consumes
                    return f"Aktivitaet '{activity}' braucht '{_name}' im Inventar — nicht verfuegbar."

        # Partner aus Condition-Matching ermitteln
        matched_partner = get_last_matched_partner() or ""

        # Bei Partner-Aktivitaeten ohne expliziten Match: Avatar als Default nehmen
        # (User chattet mit Agent -> Avatar ist am Ort und somit logischer Partner).
        _needs_partner = (lib_act and lib_act.get("requires_partner")) or (act_def and act_def.get("requires_partner"))
        if _needs_partner and not matched_partner:
            try:
                from app.models.account import get_active_character
                from app.models.character import get_character_current_location as _loc
                _avatar = get_active_character()
                if _avatar and _avatar != character_name and _loc(_avatar) == current_loc_id:
                    matched_partner = _avatar
            except Exception:
                pass

        # Fallback: kein Avatar (z.B. autonomer AgentLoop-Call ohne
        # Request-Kontext) → einen co-lokalen anderen Character nehmen,
        # bevorzugt mit hoher Beziehungs-Staerke / romantic_tension.
        # Verhindert "Sex (with niemand)"-Eintraege wenn Lirien autonom
        # eine partner-Activity setzt waehrend tatsaechlich jemand anwesend ist.
        if _needs_partner and not matched_partner and current_loc_id:
            try:
                from app.models.character import list_available_characters, get_character_current_location as _loc
                from app.models.relationship import get_relationship
                _candidates = []
                for _c in list_available_characters():
                    if _c == character_name:
                        continue
                    if _loc(_c) != current_loc_id:
                        continue
                    rel = get_relationship(character_name, _c) or {}
                    score = (rel.get("strength") or 0) + (rel.get("romantic_tension") or 0) * 50
                    _candidates.append((score, _c))
                if _candidates:
                    _candidates.sort(key=lambda x: x[0], reverse=True)
                    matched_partner = _candidates[0][1]
                    logger.info("set_activity: kein Avatar — Partner via co-located "
                                "Fallback: %s (score=%d)",
                                matched_partner, _candidates[0][0])
            except Exception as _pe:
                logger.debug("Co-located Partner-Fallback fehlgeschlagen: %s", _pe)

        # Partner-Consent: Der Initiator fragt den Partner, der Partner-LLM
        # entscheidet natuerlich. Bei Ablehnung -> fallback_activity.
        # Player-Character wird nicht gefragt — Player soll selbst entscheiden
        # (daher Fallback fuer den Initiator).
        if _needs_partner and matched_partner:
            partner_def = lib_act if (lib_act and lib_act.get("requires_partner")) else act_def
            partner_def = partner_def or {}
            from app.core.partner_consent import ask_partner_to_join
            accepted, reason = ask_partner_to_join(character_name, matched_partner, partner_def)
            if not accepted:
                logger.info("Partner-Consent abgelehnt (%s): %s -> %s",
                            reason, character_name, matched_partner)
                fallback_id = partner_def.get("fallback_activity", "")
                fb_act = (get_library_activity(fallback_id)
                          or find_library_activity_by_name(fallback_id)) if fallback_id else None
                if fb_act:
                    activity = fb_act.get("name", fallback_id)
                    matched_room = find_room_by_activity(current_loc, activity) if current_loc else None
                    act_def = _find_activity_definition(character_name, activity)
                    lib_act = get_library_activity(activity) or find_library_activity_by_name(activity)
                    matched_partner = ""  # Solo — kein Partner-Transfer
                else:
                    # Kein Fallback konfiguriert: Solo-Version ohne Partner
                    matched_partner = ""

        # Activity speichern — Partner-Transfer passiert zentral in
        # save_character_current_activity (fuer Library-Activities mit requires_partner).
        save_character_current_activity(character_name, activity, partner=matched_partner)

        # Raum aktualisieren wenn ein passender gefunden wurde — aber NICHT
        # fuer den Spieler-Avatar (User steuert dessen Position) und NICHT
        # wenn der Character gerade im aktiven Chat ist (sonst springt er
        # mitten im RP in einen anderen Raum).
        if matched_room:
            room_id = matched_room.get("id", "")
            room_name = matched_room.get("name", "")
            from app.models.account import is_player_controlled, get_chat_partner
            try:
                _is_chat_partner = (get_chat_partner() == character_name)
            except Exception:
                _is_chat_partner = False
            if is_player_controlled(character_name):
                logger.info("set_activity: Avatar %s — Raumwechsel uebersprungen (User steuert Position)",
                            character_name)
            elif _is_chat_partner:
                logger.info("set_activity: %s ist aktiver Chat-Partner — "
                            "Raumwechsel uebersprungen (kein RP-Sprung)",
                            character_name)
            else:
                save_character_current_room(character_name, room_id)
        else:
            room_name = ""

        # Outfit-Compliance pruefen — zentraler Helper waehlt
        # Activity > Raum > Location (Activity gewinnt, z.B. "sunbathing"
        # triggert swimwear auch wenn der Raum nur "Outdoor" ist).
        from app.models.inventory import apply_outfit_type_compliance
        from app.core.outfit_rules import resolve_target_outfit_type
        _target_type = resolve_target_outfit_type(character_name)
        if _target_type:
            apply_outfit_type_compliance(character_name, _target_type)

        # --- Spezial-Aktivitaet: Cooldown, Effects, Triggers ---
        if act_def:
            # consumes_item: jetzt tatsaechlich verbrauchen (Pre-Check oben hat bestanden)
            _consumes = (act_def.get("consumes_item") or "").strip()
            if _consumes:
                from app.models.inventory import consume_item
                consume_item(character_name, _consumes)

            # Cooldown-Timestamp setzen
            if act_def.get("cooldown_minutes", 0) > 0:
                set_cooldown_timestamp(character_name, activity)

            # Effects werden zeitproportional via save_character_current_activity
            # und hourly_status_tick angewendet (nicht mehr sofort).

            # on_start Trigger ausfuehren
            on_start = act_def.get("triggers", {}).get("on_start") if act_def.get("triggers") else None
            if on_start:
                execute_trigger(character_name, on_start)

            # Duration auto-complete laeuft seit dem world_admin_tick-Refactor
            # ueber periodic_jobs._sub_activity_expiry — pro Tick werden alle
            # Chars geprueft und Activities deren ``activity_started_at +
            # activity_duration_minutes`` ueberschritten ist beendet (mit
            # on_complete-Trigger). Profil-Felder werden von
            # save_character_current_activity() automatisch gesetzt; hier
            # ist nichts mehr zu planen.

        logger.info(f"Gesetzt: Activity='{activity}'"
                    + (f", Room='{room_name}'" if room_name else "")
                    + (f" @ {location_name}" if location_name else ""))

        # Bestaetigung
        if target_name and target_name != agent_name:
            result = f"Aktivitaet von {target_name} aktualisiert: {activity}"
        else:
            result = f"Aktivitaet aktualisiert: {activity}"
        if room_name:
            result += f" (Raum: {room_name})"
        if location_name:
            result += f" @ {location_name}"
        return result


    def get_usage_instructions(self, format_name: str = "", **kwargs) -> str:
        from app.core.tool_formats import format_example
        fmt = format_name or "tag"
        return format_example(fmt, self.name, "drinking coffee")

    def as_tool(self, **kwargs) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                f"{self.description}. "
                f"Input: activity name (e.g. 'drinking coffee', 'reading', 'cooking', 'watching TV'). "
                f"Use this tool when the user suggests doing an activity or wants to change what the character is doing, "
                f"WITHOUT changing the location."
            ),
            func=self.execute)
