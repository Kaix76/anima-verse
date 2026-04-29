"""DescribeRoom Skill - Character kann Raum-Beschreibungen aendern und neue Raeume anlegen.

Erlaubt Characters, Raeume an ihren erlaubten Locations zu beschreiben und
neue Raeume hinzuzufuegen (bis zu max_custom_rooms pro Location).

Die Location wird als Parameter uebergeben (nicht mehr an aktuelle Position gebunden).
Funktioniert als Chat-Tool und via Intent (proaktiv).

Jeder Raum hat eine `description` (inhaltliche Beschreibung) und einen optionalen
`image_prompt` (englischer Prompt fuer die Bildgenerierung). Nach dem Speichern
wird automatisch ein neues Raum-Bild generiert.
"""
import json
import os
from typing import Any, Dict

from .base import BaseSkill, ToolSpec

from app.core.log import get_logger
from app.core.tool_formats import format_example

logger = get_logger("describe_room")

from app.models.world import (
    get_location_by_id,
    get_room_by_id,
    get_room_by_name,
    update_room_description,
    add_room)


class DescribeRoomSkill(BaseSkill):
    """Skill zum Aendern/Erstellen von Raum-Beschreibungen an erlaubten Locations.

    Input-Format (JSON):
        {
            "location_id": "hex-id des Orts",
            "room": "Raumname (bestehend oder neu)",
            "description": "Inhaltliche Beschreibung des Raums (Sprache des Users)",
            "image_prompt": "English image generation prompt for the room scene (used as day view)"
        }

    - Alle Raeume einer erlaubten Location koennen beschrieben werden
    - Pro Aufruf wird genau EIN Raum bearbeitet
    - Neue Raeume koennen angelegt werden (bis max_custom_rooms der Location)
    - Wenn der Raum nicht existiert, wird er automatisch erstellt
    - Nach dem Speichern wird automatisch ein Raum-Bild generiert
    """

    SKILL_ID = "describe_room"
    ALWAYS_LOAD = True

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.name = os.environ.get("SKILL_DESCRIBEROOM_NAME", "DescribeRoom")
        self.description = os.environ.get(
            "SKILL_DESCRIBEROOM_DESCRIPTION",
            "Describes or creates a room at an allowed location"
        )
        self._defaults = {
            "enabled": True,
            "max_custom_rooms": int(os.environ.get("SKILL_DESCRIBEROOM_MAX_ROOMS", "3")),
            "design_locations": [],  # Locations die der Character gestalten darf (leer = alle erlaubten)
        }

    def get_config_fields(self) -> dict:
        return {
            "max_custom_rooms": {
                "type": "int",
                "default": self._defaults["max_custom_rooms"],
                "label": "Max. neue Räume pro Location",
            },
            "design_locations": {
                "type": "locations",
                "default": [],
                "label": "Locations zum Gestalten",
            },
        }

    def _get_allowed_location_ids(self, character_name: str) -> list:
        """Gibt die Location-IDs zurueck, die der Character gestalten darf.

        Liest design_locations aus der Skill-Config.
        Leere Liste = alle besuchbaren Locations erlaubt.
        """
        cfg = self._get_effective_config(character_name)
        design_locs = cfg.get("design_locations", [])
        if design_locs:
            return design_locs

        # Fallback: alle Locations die der Character dank Wissens-Items sieht
        from app.models.world import list_locations_for_character
        visible = list_locations_for_character(character_name)
        return [loc.get("id") for loc in visible if loc.get("id")]

    def execute(self, raw_input: str) -> str:
        if not self.enabled:
            return "Fehler: DescribeRoom Skill ist nicht verfuegbar."

        try:
            return self._execute_inner(raw_input)
        except Exception as e:
            logger.error("Fehler in DescribeRoom: %s", e, exc_info=True)
            return f"Fehler: {e}"

    def _execute_inner(self, raw_input: str) -> str:
        ctx = self._parse_base_input(raw_input)
        character_name = ctx.get("agent_name", "").strip()
        user_id = ctx.get("user_id", "").strip()

        if not character_name:
            return "Fehler: Agent-Name fehlt."

        # JSON-Input parsen
        location_id = ctx.get("location_id", "").strip()
        room_name = ctx.get("room", "").strip()
        new_description = ctx.get("description", "").strip()
        new_image_prompt = ctx.get("image_prompt", "").strip()

        # Fallback: Legacy-Format "Raumname: Beschreibung" im input-Feld
        input_text = ctx.get("input", "").strip()
        if not location_id and not room_name and input_text:
            # Versuche altes Format zu parsen
            return self._execute_legacy(input_text, character_name)

        if not location_id:
            logger.warning("location_id fehlt im Tool-Input (agent=%s)", character_name)
            return "Fehler: location_id fehlt."
        if not room_name:
            logger.warning("room fehlt im Tool-Input (agent=%s, location=%s)", character_name, location_id)
            return "Fehler: room (Raumname) fehlt."
        if not new_description and not new_image_prompt:
            logger.warning("description und image_prompt fehlen (agent=%s, room=%s)", character_name, room_name)
            return "Fehler: description oder image_prompt muss angegeben werden."

        # Beschreibung bereinigen: Character-Referenzen, Tool-Tags, JSON entfernen
        if new_description:
            new_description = self._sanitize_room_description(new_description, character_name)
            if not new_description and not new_image_prompt:
                logger.warning("Beschreibung nach Bereinigung leer (agent=%s, room=%s)", character_name, room_name)
                return "Fehler: Beschreibung wurde bereinigt und ist leer — sie enthielt keine gueltige Raum-Beschreibung."

        # Berechtigung pruefen
        allowed_ids = self._get_allowed_location_ids(character_name)
        if location_id not in allowed_ids:
            logger.warning(
                "Location '%s' nicht erlaubt fuer %s (erlaubt: %s)",
                location_id, character_name, allowed_ids)
            return f"Fehler: Location '{location_id}' ist fuer {character_name} nicht erlaubt."

        # Location laden
        location = get_location_by_id(location_id)
        if not location:
            logger.warning("Location '%s' nicht gefunden (agent=%s)", location_id, character_name)
            return f"Fehler: Location '{location_id}' nicht gefunden."

        location_name = location.get("name", "?")
        rooms = get_location_rooms(location)

        # Raum suchen (fuzzy)
        room = get_room_by_name(location, room_name)

        if room:
            # Bestehenden Raum aktualisieren
            actual_room_name = room.get("name", room_name)
            if actual_room_name.lower() != room_name.lower():
                logger.info(
                    "Fuzzy-Match: '%s' -> bestehender Raum '%s' (%s)",
                    room_name, actual_room_name, location_name)
            old_desc = room.get("description", "")
            room_id = room.get("id", "")

            # Beschreibung und/oder Image-Prompt aktualisieren
            final_description = new_description if new_description else old_desc
            success = update_room_description(location_id, room_id, final_description,
                image_prompt_day=new_image_prompt if new_image_prompt else None
            )
            if not success:
                return "Fehler: Raum-Beschreibung konnte nicht aktualisiert werden."

            logger.info(
                "%s hat Raum '%s' (%s) aktualisiert: desc='%s', image_prompt='%s'",
                character_name, actual_room_name, location_name,
                final_description[:60], (new_image_prompt or "")[:60])

            # Bild generieren
            self._trigger_room_image(location_id, room_id)

            parts = [f"Raum aktualisiert: {actual_room_name} ({location_name})"]
            if new_description:
                parts.append(f"Beschreibung: {new_description}")
            if new_image_prompt:
                parts.append(f"Image-Prompt: {new_image_prompt}")
            parts.append("Bildgenerierung gestartet.")
            return "\n".join(parts)
        else:
            # Neuen Raum anlegen
            cfg = self._get_effective_config(character_name)
            max_custom = cfg.get("max_custom_rooms", 3)
            if max_custom > 0 and len(rooms) >= max_custom:
                room_names_list = [r.get("name", "") for r in rooms if r.get("name")]
                return (
                    f"Fehler: Maximum von {max_custom} Raeumen in '{location_name}' erreicht. "
                    f"Vorhandene Raeume: {', '.join(room_names_list)}"
                )

            new_room = add_room(location_id, room_name, new_description,
                                image_prompt_day=new_image_prompt or "")
            if not new_room:
                return f"Fehler: Raum '{room_name}' konnte nicht angelegt werden."

            logger.info(
                "%s hat neuen Raum '%s' in '%s' angelegt: desc='%s', image_prompt='%s'",
                character_name, room_name, location_name,
                new_description[:60], (new_image_prompt or "")[:60])

            # Bild generieren
            room_id = new_room.get("id", "")
            if room_id:
                self._trigger_room_image(location_id, room_id)

            parts = [f"Neuer Raum erstellt: {room_name} ({location_name})"]
            if new_description:
                parts.append(f"Beschreibung: {new_description}")
            if new_image_prompt:
                parts.append(f"Image-Prompt: {new_image_prompt}")
            if room_id:
                parts.append("Bildgenerierung gestartet.")
            return "\n".join(parts)

    @staticmethod
    def _trigger_room_image(location_id: str, room_id: str):
        """Startet die Bildgenerierung fuer einen Raum (fire-and-forget).

        Funktioniert sowohl aus async-Kontexten (Chat-Route) als auch aus
        synchronen Worker-Threads (Proaktiv/Intent via TaskQueue).
        """
        import threading

        def _generate():
            import time
            try:
                from app.core.dependencies import get_skill_manager
                from app.models.world import get_location_by_id, get_room_by_id
                from app.routes.world import get_gallery_dir, save_gallery_prompt, \
                    toggle_background_image, set_gallery_image_room
                from app.models.world import set_gallery_image_meta

                location = get_location_by_id(location_id)
                if not location:
                    return
                room = get_room_by_id(location, room_id)
                if not room:
                    return

                description = room.get("image_prompt_day", "") or room.get("description", "")
                if not description:
                    return

                prompt = (
                    f"{description}, wide angle establishing shot, no people, "
                    f"atmospheric, cinematic lighting, background wallpaper, 16:9 aspect ratio"
                )

                skill_manager = get_skill_manager()
                img_skill = None
                for skill in skill_manager.skills:
                    if getattr(skill, 'SKILL_ID', '') == "image_generation":
                        img_skill = skill
                        break
                if not img_skill:
                    logger.warning("ImageGeneration Skill nicht verfuegbar fuer Raum-Bild")
                    return

                # Backend+Workflow aus LOCATION_IMAGEGEN_DEFAULT aufloesen
                backend = None
                active_workflow = None
                loc_default = os.environ.get("LOCATION_IMAGEGEN_DEFAULT", "").strip()
                if loc_default:
                    if loc_default.startswith("workflow:"):
                        wf_name = loc_default[len("workflow:"):]
                        active_workflow = next(
                            (w for w in img_skill.comfy_workflows if w.name == wf_name), None)
                        if active_workflow:
                            compat = active_workflow.compatible_backends or []
                            candidates = [
                                b for b in img_skill.backends
                                if b.available and b.instance_enabled
                                and b.api_type == "comfyui"
                                and (not compat or b.name in compat)
                            ]
                            backend = img_skill.pick_lowest_cost(
                                candidates, rotation_key=f"describe_room:{active_workflow.name}")
                    elif loc_default.startswith("backend:"):
                        be_name = loc_default[len("backend:"):]
                        backend = next(
                            (b for b in img_skill.backends
                             if b.name == be_name and b.available and b.instance_enabled), None)
                if not backend:
                    backend = img_skill._select_backend()
                if not backend:
                    logger.warning("Kein Image-Backend verfuegbar fuer Raum-Bild")
                    return
                if not active_workflow:
                    active_workflow = getattr(img_skill, '_default_workflow', None)

                full_prompt = prompt
                if active_workflow and active_workflow.prompt_style:
                    full_prompt = f"{active_workflow.prompt_style}, {full_prompt}"
                elif backend.prompt_prefix:
                    full_prompt = f"{backend.prompt_prefix}, {full_prompt}"

                negative = backend.negative_prompt or ""
                params = {"width": 1280, "height": 720}
                if active_workflow and active_workflow.workflow_file:
                    params["workflow_file"] = active_workflow.workflow_file
                    if active_workflow.model:
                        _model_key = "unet" if active_workflow.has_input_unet else "model"
                        params[_model_key] = active_workflow.model
                    # CLIP aus Workflow-Config
                    if active_workflow.clip:
                        params["clip_name"] = active_workflow.clip

                logger.info("Raum-Bild Generierung gestartet fuer %s/%s", location_id, room_id)
                images = backend.generate(full_prompt, negative, params)
                if images == "NO_NEW_IMAGE":
                    logger.warning("Raum-Bild ComfyUI Cache-Hit fuer %s/%s — uebersprungen", location_id, room_id)
                    return
                if not images:
                    logger.warning("Raum-Bild Generierung fehlgeschlagen fuer %s/%s", location_id, room_id)
                    return

                loc_id = location.get("id", location_id)
                gallery_dir = get_gallery_dir(loc_id)
                gallery_dir.mkdir(parents=True, exist_ok=True)
                image_name = f"{int(time.time())}.png"
                image_path = gallery_dir / image_name
                image_path.write_bytes(images[0])

                save_gallery_prompt(loc_id, image_name, full_prompt)
                toggle_background_image(loc_id, image_name)
                set_gallery_image_room(loc_id, image_name, room_id)

                # prompt_changed Flag entfernen — Bild wurde aus dem Prompt erzeugt
                from app.models.world import clear_room_prompt_changed
                clear_room_prompt_changed(location_id, room_id)
                _model_used = (getattr(backend, 'last_used_checkpoint', '')
                               or getattr(backend, 'model', '')
                               or getattr(backend, 'checkpoint', '') or '')
                set_gallery_image_meta(loc_id, image_name, {
                    "backend": backend.name,
                    "model": _model_used,
                })

                logger.info(
                    "Raum-Bild generiert: %s/%s -> %s",
                    location.get("name", "?"), room.get("name", "?"), image_name)
            except Exception as e:
                logger.error("Fehler bei Raum-Bild Generierung: %s", e, exc_info=True)

        thread = threading.Thread(target=_generate, daemon=True)
        thread.start()

    @staticmethod
    def _sanitize_room_description(description: str, character_name: str) -> str:
        """Bereinigt Raum-Beschreibungen von unerwuenschtem Inhalt.

        Entfernt:
        - Eingebettete JSON-Objekte (halluzinierte Tool-Calls)
        - Tool-Call-Tags (<tool name=...>)
        - Character-Appearance-Beschreibungen
        - Character-bezogene Saetze
        """
        import re

        text = description.strip()
        if not text:
            return text

        # 1. Embedded JSON-Objekte entfernen (halluzinierte Tool-Calls)
        #    z.B. {"location_id": "...", "room": "...", ...}
        if text.startswith("{"):
            # Gesamter Text ist ein JSON-Objekt — kein gültiger Beschreibungstext
            try:
                import json as _json
                parsed = _json.loads(text)
                if isinstance(parsed, dict) and ("location_id" in parsed or "room" in parsed):
                    logger.info("Raum-Beschreibung ist ein JSON-Objekt — abgelehnt (LLM-Halluzination, Sanitizer hat uebernommen)")
                    return ""
            except Exception:
                pass
            # Versuche JSON-Praefix zu entfernen (JSON gefolgt von Text)
            brace_depth = 0
            json_end = -1
            for i, ch in enumerate(text):
                if ch == "{":
                    brace_depth += 1
                elif ch == "}":
                    brace_depth -= 1
                    if brace_depth == 0:
                        json_end = i
                        break
            if json_end > 0:
                remainder = text[json_end + 1:].strip()
                # Prüfe ob der Rest auch Tool-Tags enthält
                remainder = re.sub(r'<tool\s+name="[^"]*">[^<]*(?:</tool>)?', '', remainder).strip()
                if remainder:
                    logger.warning("JSON-Praefix aus Raum-Beschreibung entfernt (%d Zeichen)", json_end + 1)
                    text = remainder
                else:
                    logger.warning("Raum-Beschreibung besteht nur aus JSON + Tool-Tags — abgelehnt")
                    return ""

        # 2. Tool-Call-Tags entfernen (<tool name="...">...</tool> oder unclosed)
        text = re.sub(r'<tool\s+name="[^"]*">.*?</tool>', '', text, flags=re.DOTALL).strip()
        text = re.sub(r'<tool\s+name="[^"]*">.*', '', text, flags=re.DOTALL).strip()
        # Auch [ToolName](...) Format entfernen
        text = re.sub(r'\[[A-Z][A-Za-z]+\]\([^)]*\)', '', text).strip()

        if not text:
            logger.warning("Raum-Beschreibung besteht nur aus Tool-Tags — abgelehnt")
            return ""

        # 3. Appearance-Muster erkennen (physische Character-Beschreibungen)
        appearance_patterns = [
            r'\b\d+\s*years?\s*(young|old)\b',
            r'\b(blonde|brunette|redhead)\b.*\b(hair|eyes)\b',
            r'\b(short|tall|athletic|slim)\s+(frame|build|body)\b',
            r'\b(large|small|round|perfect)\s+(breasts?|butt|chest)\b',
            r'\bwearing\s+a\b',
            r'\bprofessional\s+portrait\b',
        ]
        appearance_score = sum(1 for p in appearance_patterns if re.search(p, text, re.IGNORECASE))
        if appearance_score >= 2:
            logger.warning("Raum-Beschreibung enthaelt Appearance-Daten (score=%d) — abgelehnt", appearance_score)
            return ""

        # 4. Character-bezogene Saetze entfernen
        sentences = re.split(r'(?<=[.!?])\s+', text)
        agent_lower = character_name.lower()

        character_indicators = [
            agent_lower,
            " steht ", " sitzt ", " liegt ", " kniet ",
            " er steht", " sie steht", " er sitzt", " sie sitzt",
            " seine augen", " ihre augen", " seinen ", " ihren ",
        ]

        filtered = []
        for sentence in sentences:
            s_lower = sentence.lower()
            if any(indicator in s_lower for indicator in character_indicators):
                logger.debug("Raum-Beschreibung bereinigt — Satz entfernt: %s", sentence[:60])
                continue
            filtered.append(sentence)

        result = " ".join(filtered).strip()
        if not result:
            logger.warning("Alle Saetze aus Raum-Beschreibung entfernt — Original beibehalten")
            return description
        return result

    def _execute_legacy(self, input_text: str, character_name: str) -> str:
        """Fallback fuer altes Format ohne explizite location_id.

        Verwendet die aktuelle Location des Characters.
        """
        from app.models.character import (
            get_character_current_location,
            get_character_current_room)

        location_id = get_character_current_location(character_name)
        if not location_id:
            return "Fehler: Kein aktueller Standort und keine location_id angegeben."

        location = get_location_by_id(location_id)
        if not location:
            return "Fehler: Standort nicht gefunden."

        # "Raumname: Beschreibung" oder nur "Beschreibung"
        room = None
        new_description = input_text

        if ":" in input_text:
            parts = input_text.split(":", 1)
            candidate_room_name = parts[0].strip()
            candidate_desc = parts[1].strip()
            matched = get_room_by_name(location, candidate_room_name)
            if matched and candidate_desc:
                room = matched
                new_description = candidate_desc

        if not room:
            room_id = get_character_current_room(character_name)
            if room_id:
                room = get_room_by_id(location, room_id)
            if not room:
                rooms = get_location_rooms(location)
                room_names = [r.get("name", "") for r in rooms if r.get("name")]
                return (
                    f"Fehler: Kein Raum angegeben. Verfuegbare Raeume: {', '.join(room_names)}. "
                    f"Format: 'Raumname: neue Beschreibung'"
                )

        old_desc = room.get("description", "")
        room_name = room.get("name", "?")
        room_id = room.get("id", "")
        location_name = location.get("name", "?")

        # Sanitization auch im Legacy-Pfad
        new_description = self._sanitize_room_description(new_description, character_name)
        if not new_description:
            return "Fehler: Beschreibung wurde bereinigt und ist leer — sie enthielt keine gueltige Raum-Beschreibung."

        success = update_room_description(location_id, room_id, new_description)
        if not success:
            return "Fehler: Raum-Beschreibung konnte nicht aktualisiert werden."

        logger.info(
            "%s hat Raum '%s' (%s) aktualisiert: '%s' -> '%s'",
            character_name, room_name, location_name,
            old_desc[:60], new_description[:60])
        return (
            f"Raum-Beschreibung aktualisiert: {room_name} ({location_name})\n"
            f"Neu: {new_description}"
        )

    def get_usage_instructions(self, format_name: str = "", character_name: str = "") -> str:
        fmt = format_name or "tag"
        return format_example(
            fmt, self.name,
            '{"location_id": "2964cf7c", "room": "Toilette", '
            '"description": "Der Boden ist nass und es riecht nach Reinigungsmittel", '
            '"image_prompt": "Wet tiled bathroom floor, industrial cleaning supplies, fluorescent lighting, '
            'mop bucket in corner, water puddles reflecting harsh light"}'
        )

    def _build_locations_hint(self, character_name: str) -> str:
        """Baut eine Liste der erlaubten Locations mit ID und Name fuer die Tool-Beschreibung."""
        if not character_name:
            return ""
        try:
            allowed_ids = self._get_allowed_location_ids(character_name)
            if not allowed_ids:
                return ""
            hints = []
            for loc_id in allowed_ids:
                loc = get_location_by_id(loc_id)
                if loc:
                    name = loc.get("name", "?")
                    rooms = get_location_rooms(loc)
                    room_names = [r.get("name", "") for r in rooms if r.get("name")]
                    if room_names:
                        hints.append(f"{loc_id} = {name} (rooms: {', '.join(room_names)})")
                    else:
                        hints.append(f"{loc_id} = {name}")
            if hints:
                return " Allowed locations: " + "; ".join(hints) + "."
        except Exception as e:
            logger.debug("Konnte Location-Hints nicht laden: %s", e)
        return ""

    def as_tool(self, character_name: str = "") -> ToolSpec:
        locations_hint = self._build_locations_hint(character_name)
        return ToolSpec(
            name=self.name,
            description=(
                f"{self.description}. "
                "Input JSON: {\"location_id\": \"<id>\", \"room\": \"<room name>\", "
                "\"description\": \"<plain text description>\", "
                "\"image_prompt\": \"<english image prompt>\"}. "
                "Updates an existing room or creates a new one if the name doesn't exist yet. "
                "Only one room per call. Only rooms at allowed locations can be changed. "
                "A new room image is automatically generated after saving."
                f"{locations_hint} "
                "IMPORTANT field rules: "
                "'description' MUST be plain natural text (e.g. 'Ein gemütliches Café mit Holztischen und warmem Licht'), "
                "NEVER nested JSON, NEVER tool tags, NEVER code. "
                "'image_prompt' MUST be plain English text (e.g. 'Cozy cafe with wooden tables, warm lighting, potted plants'), "
                "NEVER nested JSON. "
                "Both fields describe ONLY the room/environment — NO characters, NO actions, NO people."
            ),
            func=self.execute)
