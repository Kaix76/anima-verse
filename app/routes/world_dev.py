"""World Development routes - Chat with LLM to create/edit world elements."""
import json
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Depends
from app.core.auth_dependency import require_admin
from fastapi.responses import StreamingResponse
from app.core.log import get_logger

logger = get_logger("world_dev")

from app.core.llm_router import create_llm_instance
from app.core.streaming import StreamingAgent, ContentEvent
from app.models.world import get_location_by_id, add_location
from app.models.character import (
    get_character_profile, save_character_profile,
    get_character_config, save_character_config,
    add_character_outfit)

router = APIRouter(prefix="/world-dev", tags=["world-dev"],
                   dependencies=[Depends(require_admin)])

from app.core.paths import get_schemas_dir as _get_schemas_dir

# In-memory session store
_sessions: Dict[str, Dict[str, Any]] = {}


def _load_schema(schema_name: str, **kwargs) -> str:
    """Loads a schema definition file and fills in placeholders."""
    path = _get_schemas_dir() / f"{schema_name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Schema '{schema_name}' nicht gefunden: {path}")
    content = path.read_text(encoding="utf-8")
    for key, value in kwargs.items():
        content = content.replace(f"{{{{{key}}}}}", str(value))
    return content


def _create_llm(model: str, provider: str = "", max_tokens: Optional[int] = None):
    """Creates an LLMClient + LLMInstance for the given model."""
    instance = create_llm_instance(
        task="chat",
        model=model,
        provider_name=provider,
        max_tokens=max_tokens)
    if not instance:
        return None, None
    return instance.create_llm(), instance


def _format_generable_fields_for_templates(selected_template: str = "") -> str:
    """Generates a Markdown description of generable fields for the LLM.

    If selected_template is set, only that template's fields are shown.
    Otherwise all templates are listed.
    """
    from app.models.character_template import get_template

    if selected_template:
        template_names = [selected_template]
    else:
        template_names = ["human-default", "human-roleplay", "animal-default"]
    sections = []

    for tname in template_names:
        tmpl = get_template(tname)
        if not tmpl:
            continue

        label = tmpl.get("label", tname)
        fields_desc = []

        for section in tmpl.get("sections", []):
            for field in section.get("fields", []):
                if not field.get("llm_generable"):
                    continue
                key = field["key"]
                flabel = field.get("label_de", field.get("label", key))
                ftype = field.get("type", "text")
                required = " (Pflicht)" if field.get("required") else ""
                store = " [config]" if field.get("store") == "config" else ""

                # Build type description
                if ftype == "select" and "options" in field:
                    opts = [o["value"] for o in field["options"]]
                    type_info = f"Auswahl: {' | '.join(opts)}"
                elif ftype == "date":
                    type_info = "Datum (YYYY-MM-DD)"
                elif ftype == "number":
                    type_info = "Zahl (0-100)"
                else:
                    type_info = "Text"

                hint = field.get("hint_de", field.get("hint", ""))
                visible = field.get("visible_when")
                vis_note = ""
                if visible:
                    vis_note = f" (nur bei {visible['field']}={', '.join(visible['values'])})"

                default = field.get("default", "")
                if default and isinstance(default, str):
                    # Truncate long defaults but show enough context
                    if len(default) > 120:
                        default_note = f' Default (BEIBEHALTEN!): "{default[:120]}..."'
                    else:
                        default_note = f' Default (BEIBEHALTEN!): "{default}"'
                else:
                    default_note = ""

                fields_desc.append(
                    f"- `{key}` — {flabel} (PFLICHT){store}{vis_note}: {type_info}.{' ' + hint if hint else ''}{default_note}"
                )

        if fields_desc:
            sections.append(f"### {label} (`{tname}`)\n\n" + "\n".join(fields_desc))

    # Always-available fields
    always = [
        "- `character_name` — Name (Pflicht): Text. Eindeutiger Name des Characters.",
        "- `template` — Template (Pflicht): Auswahl: human-default | human-roleplay | animal-default",
        "- `outfits` — Outfit-Liste: Array von Objekten (siehe Outfit-Sektion unten).",
    ]
    header = "### Immer verfuegbare Felder\n\n" + "\n".join(always)

    return header + "\n\n" + "\n\n".join(sections)


def _format_context_locations(location_ids: list) -> str:
    """Formats selected locations as detailed context for the LLM.

    Liefert nur die Bullet-Body-Zeilen (kein eigener Header) — der Header
    kommt aus dem Schema-Template (`## Bestehende Orte`).
    """
    if not location_ids:
        return ""
    lines: list = []
    for loc_id in location_ids:
        try:
            loc = get_location_by_id(loc_id)
        except Exception as e:
            logger.warning("Konnte Location %s nicht laden: %s", loc_id, e)
            continue
        if not loc or not isinstance(loc, dict):
            continue
        loc_name = loc.get("name") or loc.get("id") or loc_id
        lines.append(f"### {loc_name}")
        desc = loc.get("description", "")
        if desc:
            lines.append(f"{desc}\n")
        for room in (loc.get("rooms", []) or []):
            if not isinstance(room, dict):
                continue
            lines.append(f"**Raum: {room.get('name', '?')}**")
            room_desc = room.get("description", "")
            if room_desc:
                lines.append(f"{room_desc}")
            activities = [a.get("name", "") for a in (room.get("activities", []) or [])
                          if isinstance(a, dict) and a.get("name")]
            if activities:
                lines.append(f"Aktivitaeten: {', '.join(activities)}")
            lines.append("")
    return "\n".join(lines)


def _format_context_characters(character_names: list) -> str:
    """Formats selected characters as detailed context for the LLM.

    Liefert nur die Bullet-Body-Zeilen (kein eigener Header) — der Header
    kommt aus dem Schema-Template (`## Bestehende Characters`).
    """
    if not character_names:
        return ""
    lines: list = []
    # Fields aus Profil-JSON
    context_keys = [
        "character_name", "template", "gender", "age", "language",
        "species", "breed", "communication",
    ]
    # Soul-Files: Inhalt aus MD-Datei (Source of Truth seit Plan 2)
    soul_files = [
        ("character_personality", "soul/personality.md"),
        ("character_task",        "soul/tasks.md"),
    ]
    for name in character_names:
        try:
            profile = get_character_profile(name)
        except Exception as e:
            logger.warning("Konnte Profil %s nicht laden: %s", name, e)
            continue
        if not profile or not profile.get("character_name"):
            continue
        lines.append(f"### {profile.get('character_name', name)}")
        for key in context_keys:
            val = profile.get(key)
            if val and key != "character_name":
                lines.append(f"- **{key}**: {val}")
        # Soul-MD-Inhalte
        try:
            from app.models.character import get_character_dir
            char_dir = get_character_dir(name)
            for key, rel in soul_files:
                md_path = char_dir / rel
                if not md_path.exists():
                    continue
                content = md_path.read_text(encoding="utf-8").strip()
                # Headings entfernen fuer kompakten Kontext, max 300 Zeichen
                clean = "\n".join(l for l in content.splitlines() if not l.startswith("#")).strip()
                if clean:
                    lines.append(f"- **{key}**: {clean[:300]}{'...' if len(clean) > 300 else ''}")
        except Exception as _se:
            logger.debug("Soul-Files fuer %s nicht lesbar: %s", name, _se)
        # Appearance (shortened)
        appearance = profile.get("character_appearance", "")
        if appearance:
            lines.append(f"- **character_appearance**: {appearance[:200]}{'...' if len(appearance) > 200 else ''}")
        # Outfit names — outfits.json statt embedded
        try:
            from app.models.character import get_character_outfits as _get_outfits
            outfits = _get_outfits(name)
        except Exception:
            outfits = []
        if outfits:
            outfit_names = [o.get("name", "?") for o in outfits
                            if isinstance(o, dict) and o.get("name")]
            if outfit_names:
                lines.append(f"- **outfits**: {', '.join(outfit_names)}")
        lines.append("")
    return "\n".join(lines)


def _extract_json_block(text: str, block_type: str) -> Dict[str, Any] | None:
    """Extracts a JSON from ```json:<block_type> ... ``` code blocks."""
    import re
    # Match ```json:type with optional whitespace, newlines, and closing ```
    pattern = rf'```json:{re.escape(block_type)}\s*\n(.*?)```'
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        # Fallback: try without the :type suffix (some LLMs omit it)
        return None
    raw = match.group(1).strip()
    try:
        parsed = json.loads(raw)
        logger.info("Extracted %s JSON: %d keys", block_type, len(parsed))
        return parsed
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse %s JSON: %s\nRaw: %s", block_type, e, raw[:200])
        return None


def _validate_character_fields(char_data: Dict[str, Any], selected_template: str = "") -> list[str]:
    """Checks which expected fields are missing or empty in character data.

    Returns a list of missing field descriptions for the LLM to complete.
    """
    from app.models.character_template import get_template

    missing = []

    # Always-required fields
    if not char_data.get("character_name"):
        missing.append("character_name (Name des Characters)")
    if not char_data.get("character_personality"):
        missing.append("character_personality (Persoenlichkeit)")
    if not char_data.get("character_appearance"):
        missing.append("character_appearance (Aussehen-Prompt)")

    # Determine template
    template = selected_template or char_data.get("template", "")
    if not template:
        missing.append("template (human-default | human-roleplay | animal-default)")
        return missing

    tmpl = get_template(template)
    if not tmpl:
        return missing

    for section in tmpl.get("sections", []):
        for field in section.get("fields", []):
            if not field.get("llm_generable"):
                continue
            key = field["key"]
            # Skip fields already checked above
            if key in ("character_name", "character_personality", "character_appearance"):
                continue

            value = char_data.get(key)
            is_empty = value is None or value == "" or value == []

            if is_empty:
                flabel = field.get("label_de", field.get("label", key))
                vis = field.get("visible_when")
                # Only flag visible_when fields if condition is met
                if vis:
                    cond_field = vis.get("field", "")
                    cond_vals = vis.get("values", [])
                    actual = char_data.get(cond_field, "")
                    if actual not in cond_vals:
                        continue
                missing.append(f"{key} ({flabel})")

    # Note: popularity, trustworthiness, social_dialog_probability are already
    # covered by the template iteration above (llm_generable + store=config).

    # Check outfits
    if not char_data.get("outfits"):
        missing.append("outfits (Outfit-Liste)")

    return missing


def _extract_location_json(text: str) -> Dict[str, Any] | None:
    """Extracts a location JSON from ```json:location ... ``` code blocks."""
    return _extract_json_block(text, "location")


def _extract_character_json(text: str) -> Dict[str, Any] | None:
    """Extracts a character JSON from ```json:character ... ``` code blocks."""
    return _extract_json_block(text, "character")


# Sub-Block-Extraktoren fuer granulare Updates (LLM muss nicht das gesamte
# Character-JSON neu generieren wenn nur ein Outfit / eine Soul-Section /
# einzelne Profil-Felder geaendert werden sollen).

def _extract_outfit_json(text: str) -> Dict[str, Any] | None:
    """```json:outfit ... ``` — einzelnes Outfit anhaengen/aktualisieren.

    Erwartet: {"character_name": "...", "outfit": {name, pieces, ...}}
    """
    return _extract_json_block(text, "outfit")


def _extract_soul_json(text: str) -> Dict[str, Any] | None:
    """```json:soul ... ``` — eine Soul-MD-Section ueberschreiben.

    Erwartet: {"character_name": "...", "section": "personality|tasks|...", "content": "..."}
    """
    return _extract_json_block(text, "soul")


def _extract_profile_patch_json(text: str) -> Dict[str, Any] | None:
    """```json:profile-patch ... ``` — Subset von Profil-Feldern updaten.

    Erwartet: {"character_name": "...", "fields": {"current_feeling": "...", ...}}
    """
    return _extract_json_block(text, "profile-patch")


@router.get("/schemas")
def get_available_schemas() -> Dict[str, Any]:
    """Lists available schema types for world development."""
    schemas = []
    if _get_schemas_dir().exists():
        for f in sorted(_get_schemas_dir().glob("*.md")):
            schemas.append({
                "name": f.stem,
                "label": f.stem.replace("_", " ").title(),
            })
    return {"schemas": schemas}


@router.post("/chat")
async def world_dev_chat(request: Request):
    """Chat with LLM for world development. Streams response."""
    data = await request.json()
    model = data.get("model", "")
    provider = data.get("provider", "")
    session_id = data.get("session_id", "")
    message = data.get("message", "").strip()
    schema = data.get("schema", "location")
    character_template = data.get("character_template", "")
    user_id = data.get("user_id", "")
    edit_location_id = data.get("edit_location_id", "")
    context_location_ids = data.get("context_location_ids", [])
    context_character_names = data.get("context_character_names", [])

    if not model:
        raise HTTPException(status_code=400, detail="model erforderlich")
    if not message:
        raise HTTPException(status_code=400, detail="message erforderlich")

    # New or existing session
    if session_id and session_id in _sessions:
        session = _sessions[session_id]
    else:
        session_id = f"wd-{uuid.uuid4().hex[:8]}"
        # Build system prompt from schema. Nur die in der GUI ausgewaehlten
        # Orte/Characters wandern in den Prompt — vollstaendige Listen wuerden
        # den Prompt unnoetig aufblaehen.
        try:
            existing_locations = _format_context_locations(context_location_ids) \
                or "Keine Orte ausgewaehlt — der Benutzer hat keine als Kontext markiert."
            existing_characters = _format_context_characters(context_character_names) \
                or "Keine Characters ausgewaehlt — der Benutzer hat keine als Kontext markiert."
            generable_fields = _format_generable_fields_for_templates(character_template) if schema == "character" else ""
            if character_template:
                selected_template_text = f"Der Benutzer hat das Template **`{character_template}`** gewaehlt. Verwende GENAU dieses Template und setze `\"template\": \"{character_template}\"` im JSON."
            else:
                selected_template_text = (
                    "Verfuegbare Templates:\n"
                    "- **human-default**: Standard-Mensch (einfach, ohne Roleplay-Regeln)\n"
                    "- **human-roleplay**: Mensch mit Roleplay-Regeln und detaillierten Koerpermerkmalen\n"
                    "- **animal-default**: Tier-Character (Hund, Katze, Fuchs, etc.)\n\n"
                    "Frage den Benutzer oder waehle basierend auf dem Kontext."
                )
            system_prompt = _load_schema(
                schema,
                existing_locations=existing_locations,
                existing_characters=existing_characters,
                generable_fields=generable_fields,
                selected_template=selected_template_text)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

        # (Selected-Context wird oben direkt in {existing_locations} /
        # {existing_characters} eingespielt — kein zusaetzlicher Append noetig.)

        # If editing an existing element, inject its data into system prompt
        edit_context = ""
        if edit_location_id:
            if schema == "character":
                # edit_location_id is reused as character name for characters
                profile = get_character_profile(edit_location_id)
                if profile and profile.get("character_name"):
                    # Use template from existing profile if not explicitly set
                    existing_template = profile.get("template", "")
                    if existing_template and not character_template:
                        character_template = existing_template
                        # Rebuild generable fields + template text for correct template
                        generable_fields = _format_generable_fields_for_templates(character_template)
                        selected_template_text = f"Der Benutzer hat das Template **`{character_template}`** gewaehlt. Verwende GENAU dieses Template und setze `\"template\": \"{character_template}\"` im JSON."
                        system_prompt = _load_schema(
                            schema,
                            existing_locations=existing_locations,
                            existing_characters=existing_characters,
                            generable_fields=generable_fields,
                            selected_template=selected_template_text)

                    # Also inject config fields into edit data
                    edit_config = get_character_config(edit_location_id)
                    config_inject = {}
                    for ck in ("popularity", "trustworthiness", "social_dialog_probability",
                               "allowed_locations"):
                        if ck in edit_config:
                            config_inject[ck] = edit_config[ck]

                    # Strip internal fields
                    skip_keys = {"created_by", "profile_image", "images",
                                 "current_location", "current_activity", "current_room",
                                 "current_feeling", "current_outfit", "location_changed_at"}
                    edit_data = {k: v for k, v in profile.items() if k not in skip_keys}
                    edit_data.update(config_inject)
                    edit_context = (
                        "\n\n## Aktuell zu bearbeitender Character\n\n"
                        "Der Benutzer moechte folgenden bestehenden Character bearbeiten. "
                        "Zeige ihm zuerst eine Zusammenfassung und frage, was er aendern moechte. "
                        "Wenn du das finale JSON ausgibst, MUSS der Name exakt gleich bleiben "
                        "(damit das System den Character aktualisiert statt einen neuen zu erstellen).\n\n"
                        f"```json\n{json.dumps(edit_data, ensure_ascii=False, indent=2)}\n```"
                    )
                    system_prompt += edit_context
            else:
                loc = get_location_by_id(edit_location_id)
                if loc:
                    # Strip fields not relevant for editing
                    edit_data = {k: v for k, v in loc.items()
                                 if k not in ("id", "background_images", "grid_x", "grid_y")}
                    edit_context = (
                        "\n\n## Aktuell zu bearbeitender Ort\n\n"
                        "Der Benutzer moechte folgenden bestehenden Ort bearbeiten. "
                        "Zeige ihm zuerst eine Zusammenfassung und frage, was er aendern moechte. "
                        "Wenn du das finale JSON ausgibst, MUSS der Name exakt gleich bleiben "
                        "(damit das System den Ort aktualisiert statt einen neuen zu erstellen).\n\n"
                        f"```json\n{json.dumps(edit_data, ensure_ascii=False, indent=2)}\n```"
                    )
                    system_prompt += edit_context

        session = {
            "model": model,
            "provider": provider,
            "schema": schema,
            "system_prompt": system_prompt,
            "messages": [],
            "user_id": user_id,
            "edit_location_id": edit_location_id,
            "selected_template": character_template,
            "cost_total": 0.0,
            "tokens_total_in": 0,
            "tokens_total_out": 0,
        }
        _sessions[session_id] = session

    llm, llm_instance = _create_llm(model, provider, max_tokens=16384)
    if not llm:
        raise HTTPException(status_code=500, detail=f"Kein Provider fuer Model '{model}' gefunden")

    agent = StreamingAgent(
        llm=llm,
        tool_format="tag",
        tools_dict={},
        agent_name="WorldDev",
        max_iterations=1,
        log_task="world_dev_chat")

    history = list(session["messages"])

    # Resolve pricing for cost tracking
    _pricing = {"input": 0.0, "output": 0.0}
    try:
        from app.core.provider_manager import get_provider_manager
        _pm = get_provider_manager()
        _prov_obj = _pm.get_provider(provider) if provider else _pm.find_provider_for_model(model)
        if _prov_obj:
            for _m in _prov_obj.list_models():
                if _m["name"] == model:
                    _pricing = _m.get("pricing", _pricing)
                    break
    except Exception:
        pass

    def _track_cost(input_text: str, output_text: str):
        """Estimate tokens and accumulate session cost. Pricing is per 1M tokens."""
        from app.utils.llm_logger import estimate_tokens
        tok_in = estimate_tokens(input_text)
        tok_out = estimate_tokens(output_text)
        cost = (tok_in * _pricing["input"] / 1_000_000) + (tok_out * _pricing["output"] / 1_000_000)
        session["tokens_total_in"] = session.get("tokens_total_in", 0) + tok_in
        session["tokens_total_out"] = session.get("tokens_total_out", 0) + tok_out
        session["cost_total"] = session.get("cost_total", 0.0) + cost

    async def generate():
        from app.core.llm_queue import get_llm_queue
        _llm_queue = get_llm_queue()
        _task_id = await _llm_queue.register_chat_active_async(
            "WorldDev", llm_instance=llm_instance,
            task_type="world_dev", label="World Dev Chat")
        full_response = ""
        try:
            # Send session_id in first chunk
            yield f"data: {json.dumps({'session_id': session_id})}\n\n"

            async for event in agent.stream(session["system_prompt"], history, message):
                if isinstance(event, ContentEvent):
                    full_response += event.content
                    yield f"data: {json.dumps({'content': event.content})}\n\n"

            # Update session
            session["messages"].append({"role": "user", "content": message})
            session["messages"].append({"role": "assistant", "content": full_response})

            # Track cost (system prompt + history + user input → output)
            all_input = session["system_prompt"] + " ".join(
                m["content"] for m in history) + message
            _track_cost(all_input, full_response)

            # Check if response contains extractable JSON
            location_data = _extract_location_json(full_response)
            if location_data:
                yield f"data: {json.dumps({'location_data': location_data})}\n\n"

            # Granulare Sub-Block-Updates (Outfit / Soul-Section / Profil-Patch).
            outfit_data = _extract_outfit_json(full_response)
            if outfit_data:
                yield f"data: {json.dumps({'outfit_data': outfit_data})}\n\n"
            soul_data = _extract_soul_json(full_response)
            if soul_data:
                yield f"data: {json.dumps({'soul_data': soul_data})}\n\n"
            profile_patch_data = _extract_profile_patch_json(full_response)
            if profile_patch_data:
                yield f"data: {json.dumps({'profile_patch_data': profile_patch_data})}\n\n"

            character_data = _extract_character_json(full_response)
            if character_data:
                # Validate fields — auto-request missing ones
                selected_tmpl = session.get("selected_template", "") or character_data.get("template", "")
                missing = _validate_character_fields(character_data, selected_tmpl)

                if missing:
                    # Ask LLM to complete missing fields
                    missing_list = "\n".join(f"- {f}" for f in missing)
                    completion_msg = (
                        f"Im JSON fehlen noch folgende Felder:\n{missing_list}\n\n"
                        "Bitte ergaenze ALLE fehlenden Felder und gib das KOMPLETTE JSON nochmal aus "
                        "(mit ```json:character markiert). Fuer Select-Felder waehle passende Werte. "
                        "Fuer Zahlen-Felder (0-100) waehle zum Character passende Werte."
                    )
                    yield f"data: {json.dumps({'content': '\n\n---\n\n*Prüfe fehlende Felder...*\n\n'})}\n\n"

                    # Add to history and request completion
                    session["messages"].append({"role": "user", "content": completion_msg})
                    completion_history = list(session["messages"])

                    completion_response = ""
                    async for event in agent.stream(session["system_prompt"], completion_history[:-1], completion_msg):
                        if isinstance(event, ContentEvent):
                            completion_response += event.content
                            yield f"data: {json.dumps({'content': event.content})}\n\n"

                    session["messages"].append({"role": "assistant", "content": completion_response})

                    # Track cost for completion turn
                    comp_input = session["system_prompt"] + " ".join(
                        m["content"] for m in completion_history[:-1]) + completion_msg
                    _track_cost(comp_input, completion_response)

                    # Try to extract completed JSON
                    completed_data = _extract_character_json(completion_response)
                    if completed_data:
                        character_data = completed_data
                    else:
                        # Merge: fill gaps from first attempt with second attempt's raw data
                        logger.warning("Completion response had no extractable JSON, using partial data")

                yield f"data: {json.dumps({'character_data': character_data})}\n\n"

            # Send session cost info
            yield f"data: {json.dumps({'usage': {'tokens_in': session['tokens_total_in'], 'tokens_out': session['tokens_total_out'], 'cost_total': round(session['cost_total'], 6)}})}\n\n"

            yield f"data: {json.dumps({'done': True})}\n\n"

        except Exception as e:
            logger.error("WorldDev chat error: %s", e)
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
        finally:
            _llm_queue.register_chat_done(_task_id)

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/apply")
async def apply_world_data(request: Request):
    """Applies generated location/room data to the world."""
    data = await request.json()
    user_id = data.get("user_id", "")
    location_data = data.get("location_data", {})
    if not location_data or not location_data.get("name"):
        raise HTTPException(status_code=400, detail="location_data mit name erforderlich")

    name = location_data["name"]
    description = location_data.get("description", "")
    rooms = location_data.get("rooms", [])
    image_prompt_day = location_data.get("image_prompt_day")
    image_prompt_night = location_data.get("image_prompt_night")
    image_prompt_map = location_data.get("image_prompt_map")

    # Ensure room structure and normalize image_prompt fields
    for room in rooms:
        if "activities" not in room:
            room["activities"] = []
        # LLM generates "image_prompt" but system reads "image_prompt_day"/"image_prompt_night"
        if "image_prompt" in room and "image_prompt_day" not in room:
            room["image_prompt_day"] = room.pop("image_prompt")
        if "image_prompt_night" not in room:
            room["image_prompt_night"] = ""

    result = add_location(
        name=name,
        description=description,
        rooms=rooms,
        image_prompt_day=image_prompt_day,
        image_prompt_night=image_prompt_night,
        image_prompt_map=image_prompt_map)

    logger.info("WorldDev: Location '%s' applied for user %s", name)
    return {"status": "success", "location": result}


def _get_generable_fields(template_name: str) -> tuple[set, set]:
    """Returns (profile_fields, config_fields) that the LLM may set for a template.

    Reads the merged template and collects all field keys with llm_generable=True.
    Fields with store="config" go to config_fields, the rest to profile_fields.
    'character_name' and 'template' are always profile fields.
    """
    from app.models.character_template import get_template

    profile_fields = {"character_name", "template"}
    config_fields: set[str] = set()

    tmpl = get_template(template_name)
    if not tmpl:
        # Fallback: allow basic fields
        profile_fields.update({"language", "gender", "age",
                               "character_personality", "character_appearance"})
        return profile_fields, config_fields

    for section in tmpl.get("sections", []):
        for field in section.get("fields", []):
            if not field.get("llm_generable"):
                continue
            key = field.get("key", "")
            if not key:
                continue
            if field.get("store") == "config":
                config_fields.add(key)
            else:
                profile_fields.add(key)

    return profile_fields, config_fields


@router.post("/apply-character")
async def apply_character_data(request: Request):
    """Applies generated character data (profile + outfits)."""
    data = await request.json()
    user_id = data.get("user_id", "")
    char_data = data.get("character_data", {})
    if not char_data or not char_data.get("character_name"):
        raise HTTPException(status_code=400, detail="character_data mit character_name erforderlich")

    char_name = char_data["character_name"]
    selected_template = data.get("selected_template", "")
    template = char_data.get("template") or selected_template or "human-default"
    # Ensure template is in char_data for profile saving
    char_data["template"] = template

    # Determine allowed fields from template
    profile_fields, config_fields = _get_generable_fields(template)

    # Load existing profile or create minimal one
    profile = get_character_profile(char_name)
    if not profile.get("character_name"):
        # New character — set essentials
        profile["character_name"] = char_name
        profile["template"] = template
        profile["created_by"] = user_id

    # Felder mit source_file werden als MD-Datei geschrieben (plan-character-md-files),
    # alle anderen wandern ins Profil-JSON. Mapping: template-Feld kennt seinen MD-Pfad.
    from app.models.character_template import get_template
    from app.models.character import get_character_dir
    tmpl = get_template(template)
    soul_field_map: Dict[str, str] = {}  # field_key -> relative MD path
    if tmpl:
        for section in tmpl.get("sections", []):
            for field in section.get("fields", []):
                fk = field.get("key", "")
                sf = field.get("source_file", "")
                if fk and sf:
                    soul_field_map[fk] = sf

    # Apply LLM-generated profile fields (ohne soul-Felder — die landen in MD)
    for key in profile_fields:
        if key in soul_field_map:
            continue
        if key in char_data:
            profile[key] = char_data[key]

    # Fill missing fields with template defaults, merge where needed
    if tmpl:
        for section in tmpl.get("sections", []):
            for field in section.get("fields", []):
                key = field.get("key", "")
                default = field.get("default")
                if not key or default is None:
                    continue
                if key in soul_field_map:
                    continue  # source_file-Felder sind MD, kein JSON-Default
                if key not in profile:
                    # Field missing entirely — use default
                    profile[key] = default
                elif key == "roleplay_instructions" and isinstance(default, str) and default:
                    # Merge: ensure default rules are always included
                    current = str(profile[key])
                    if default not in current:
                        profile[key] = default + "\n\n" + current

    # Stale soul-Felder aus dem Profil-JSON entfernen (Migrations-Hygiene fuer
    # vorhandene Characters, die noch character_personality etc. im Blob haben).
    for k in list(profile.keys()):
        if k in soul_field_map:
            profile.pop(k, None)

    # Save profile
    save_character_profile(char_name, profile)

    # Soul-Dateien aus den generierten Inhalten anlegen / ueberschreiben
    if soul_field_map:
        char_dir = get_character_dir(char_name)
        for fk, rel_path in soul_field_map.items():
            content = char_data.get(fk)
            if content is None or not str(content).strip():
                continue
            md_path = char_dir / rel_path
            md_path.parent.mkdir(parents=True, exist_ok=True)
            md_path.write_text(str(content).rstrip() + "\n", encoding="utf-8")

    # Apply config fields
    config = get_character_config(char_name)
    config_changed = False
    for key in config_fields:
        if key in char_data:
            config[key] = str(char_data[key])
            config_changed = True

    # allowed_locations abgeschafft — wird ignoriert wenn der LLM es trotzdem
    # im JSON schickt (Backwards-Compat, keine Warnung).
    char_data.pop("allowed_locations", None)

    if config_changed:
        save_character_config(char_name, config)

    # Apply outfits via Helper (Pieces-Format mit Dedupe + Freitext-Fallback).
    for outfit in char_data.get("outfits", []):
        _apply_one_outfit(char_name, outfit)

    logger.info("WorldDev: Character '%s' (template=%s) applied",
                char_name, template)
    return {"status": "success", "character": char_name, "template": template}


# ---------------------------------------------------------------------------
# Granulare Apply-Routes — kleinere Updates ohne komplettes Character-JSON
# ---------------------------------------------------------------------------

def _apply_one_outfit(char_name: str, outfit: Dict[str, Any]) -> Dict[str, Any]:
    """Legt EIN Outfit an (Pieces-Format mit Dedupe oder Freitext-Fallback).

    Returns: {name, pieces_created, pieces_reused}
    """
    if not outfit.get("name"):
        return {"name": "", "pieces_created": 0, "pieces_reused": 0}
    raw_pieces = outfit.get("pieces") or []
    if raw_pieces and isinstance(raw_pieces[0], dict):
        from app.models.inventory import (
            add_item, add_to_inventory, find_inventory_piece_by_name_slot,
            VALID_PIECE_SLOTS)
        piece_ids: List[str] = []
        created = reused = 0
        for p in raw_pieces:
            slot = (p.get("slot") or "").strip().lower()
            name = (p.get("name") or "").strip()
            fragment = (p.get("prompt_fragment") or "").strip()
            if not slot or not name or slot not in VALID_PIECE_SLOTS:
                logger.warning("WorldDev outfit '%s': Piece skipped (slot=%r, name=%r)",
                               outfit.get("name"), slot, name)
                continue
            existing = find_inventory_piece_by_name_slot(char_name, name, slot)
            if existing:
                piece_ids.append(existing)
                reused += 1
                continue
            item = add_item(
                name=name,
                description=(p.get("description") or "").strip(),
                category="outfit_piece",
                image_prompt="",
                prompt_fragment=fragment,
                outfit_piece={
                    "slot": slot,
                    "outfit_types": p.get("outfit_types") or [],
                    "additional_slots": p.get("additional_slots") or [],
                    "covers": p.get("covers") or [],
                    "partially_covers": p.get("partially_covers") or [],
                })
            iid = item.get("id")
            if not iid:
                logger.warning("WorldDev outfit '%s': add_item failed for '%s'",
                               outfit.get("name"), name)
                continue
            add_to_inventory(character_name=char_name, item_id=iid,
                obtained_method="generated", obtained_from="world_dev")
            piece_ids.append(iid)
            created += 1
        frags = [(p.get("prompt_fragment") or "").strip()
                 for p in raw_pieces if isinstance(p, dict)]
        outfit_text = "wearing: " + ", ".join(f for f in frags if f) if any(frags) else ""
        add_character_outfit(char_name, {
            "name": outfit["name"],
            "outfit": outfit_text,
            "pieces": piece_ids,
            "locations": outfit.get("locations") or [],
            "activities": outfit.get("activities") or [],
            "excluded_locations": outfit.get("excluded_locations") or [],
        })
        logger.info("WorldDev outfit '%s': %d neu, %d reused", outfit["name"], created, reused)
        return {"name": outfit["name"], "pieces_created": created, "pieces_reused": reused}
    # Alter Pfad: Freitext-Outfit (Backwards-Compat).
    add_character_outfit(char_name, outfit)
    return {"name": outfit["name"], "pieces_created": 0, "pieces_reused": 0}


@router.post("/apply-outfit")
async def apply_outfit_data(request: Request):
    """Granulares Apply: ein einzelnes Outfit anhaengen / aktualisieren.

    Body: {character_name, outfit: {name, pieces, locations?, activities?, excluded_locations?}}
    """
    data = await request.json()
    char_name = (data.get("character_name") or "").strip()
    outfit = data.get("outfit") or {}
    if not char_name:
        raise HTTPException(status_code=400, detail="character_name erforderlich")
    if not outfit.get("name"):
        raise HTTPException(status_code=400, detail="outfit.name erforderlich")
    profile = get_character_profile(char_name)
    if not profile.get("character_name"):
        raise HTTPException(status_code=404, detail=f"Character '{char_name}' nicht gefunden")
    result = _apply_one_outfit(char_name, outfit)
    return {"status": "success", "character": char_name, **result}


@router.post("/apply-soul")
async def apply_soul_data(request: Request):
    """Granulares Apply: eine einzelne Soul-MD-Section ueberschreiben.

    Body: {character_name, section: "personality"|"tasks"|..., content: "..."}
    """
    data = await request.json()
    char_name = (data.get("character_name") or "").strip()
    section = (data.get("section") or "").strip()
    content = data.get("content") or ""
    if not char_name:
        raise HTTPException(status_code=400, detail="character_name erforderlich")
    if not section:
        raise HTTPException(status_code=400, detail="section erforderlich")
    from app.skills.edit_self_skill import SECTION_FILE_MAP
    if section not in SECTION_FILE_MAP:
        raise HTTPException(status_code=400,
            detail=f"Unbekannte section '{section}' (erlaubt: {sorted(SECTION_FILE_MAP.keys())})")
    profile = get_character_profile(char_name)
    if not profile.get("character_name"):
        raise HTTPException(status_code=404, detail=f"Character '{char_name}' nicht gefunden")

    from app.models.character import get_character_dir
    char_dir = get_character_dir(char_name)
    md_path = char_dir / SECTION_FILE_MAP[section]
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(str(content).rstrip() + "\n", encoding="utf-8")
    logger.info("WorldDev: Soul '%s/%s' (%d chars) geschrieben",
                char_name, section, len(content))
    return {"status": "success", "character": char_name,
            "section": section, "size": len(content)}


@router.post("/apply-profile-patch")
async def apply_profile_patch_data(request: Request):
    """Granulares Apply: Subset von Profil-Feldern updaten.

    Body: {character_name, fields: {key: value, ...}}
    Soul-Felder (source_file) werden ignoriert — die laufen ueber /apply-soul.
    """
    data = await request.json()
    char_name = (data.get("character_name") or "").strip()
    fields = data.get("fields") or {}
    if not char_name:
        raise HTTPException(status_code=400, detail="character_name erforderlich")
    if not isinstance(fields, dict) or not fields:
        raise HTTPException(status_code=400, detail="fields (dict) erforderlich")
    profile = get_character_profile(char_name)
    if not profile.get("character_name"):
        raise HTTPException(status_code=404, detail=f"Character '{char_name}' nicht gefunden")

    # Soul-Felder rausfiltern (gehen ueber /apply-soul)
    template = profile.get("template", "")
    soul_field_keys: set = set()
    if template:
        from app.models.character_template import get_template
        tmpl = get_template(template)
        if tmpl:
            for sec in tmpl.get("sections", []):
                for fld in sec.get("fields", []):
                    if fld.get("source_file") and fld.get("key"):
                        soul_field_keys.add(fld["key"])
    applied = {}
    for k, v in fields.items():
        if k in soul_field_keys:
            logger.info("WorldDev profile-patch: '%s' uebersprungen (Soul-Feld)", k)
            continue
        profile[k] = v
        applied[k] = v
    save_character_profile(char_name, profile)
    logger.info("WorldDev: Profile-Patch fuer '%s' (%d Felder)", char_name, len(applied))
    return {"status": "success", "character": char_name,
            "applied_fields": list(applied.keys())}


@router.post("/cleanup")
async def cleanup_session(request: Request):
    """Removes a world dev session from memory."""
    data = await request.json()
    session_id = data.get("session_id", "")
    _sessions.pop(session_id, None)
    return {"status": "ok"}


# ── Global Pause / Resume ──

def _get_pause_state() -> Dict[str, bool]:
    """Returns the current pause state of all subsystems.

    Ersetzt llm_queue.paused: statt globaler LLM-Pause nutzen wir das
    Runtime-Preset "world_dev" im llm_task_state (disabled Tasks).
    """
    from app.core.thoughts import get_thought_loop
    from app.core.task_queue import get_task_queue
    from app.routes.scheduler import get_scheduler_manager
    from app.core.llm_task_state import runtime_disabled_tasks

    thought = get_thought_loop()
    task_queue = get_task_queue()
    scheduler_mgr = get_scheduler_manager()

    thought_paused = thought.paused if thought else False
    queue_paused = task_queue._is_paused("default") if task_queue else False
    llm_paused = bool(runtime_disabled_tasks())
    scheduler_paused = False
    if scheduler_mgr and hasattr(scheduler_mgr, '_global_paused'):
        scheduler_paused = scheduler_mgr._global_paused

    all_paused = thought_paused and queue_paused and scheduler_paused and llm_paused
    return {
        "paused": all_paused,
        "thought_paused": thought_paused,
        "queue_paused": queue_paused,
        "llm_paused": llm_paused,
        "scheduler_paused": scheduler_paused,
    }


@router.post("/pause-all")
async def pause_all():
    """Pauses all LLM and background activities (except World Dev Chat).

    Ersetzt llm_queue.pause durch Runtime-Preset "world_dev" (Task-Disable).
    Chat/Story bleiben aktiv — nur Hintergrund-Tasks werden deaktiviert.
    """
    from app.core.thoughts import get_thought_loop
    from app.core.task_queue import get_task_queue
    from app.routes.scheduler import get_scheduler_manager
    from app.core.llm_task_state import activate_preset_runtime

    disabled = activate_preset_runtime("world_dev")
    logger.info("LLM-Task-Disable aktiv (world_dev): %d Tasks aus", len(disabled))

    thought = get_thought_loop()
    if thought:
        thought.pause()

    task_queue = get_task_queue()
    if task_queue:
        task_queue.pause_queue("default")

    scheduler_mgr = get_scheduler_manager()
    if scheduler_mgr:
        for job in scheduler_mgr.scheduler.get_jobs():
            job.pause()
        scheduler_mgr._global_paused = True
        logger.info("Scheduler pausiert (%d Jobs)", len(scheduler_mgr.scheduler.get_jobs()))

    logger.info("Hintergrund-Aktivitaeten pausiert (World-Dev-Modus)")
    return _get_pause_state()


@router.post("/resume-all")
async def resume_all():
    """Resumes all LLM and background activities."""
    from app.core.thoughts import get_thought_loop
    from app.core.task_queue import get_task_queue
    from app.routes.scheduler import get_scheduler_manager
    from app.core.llm_task_state import clear_runtime

    clear_runtime()

    thought = get_thought_loop()
    if thought:
        thought.resume()

    task_queue = get_task_queue()
    if task_queue:
        task_queue.resume_queue("default")

    scheduler_mgr = get_scheduler_manager()
    if scheduler_mgr:
        for job in scheduler_mgr.scheduler.get_jobs():
            job.resume()
        scheduler_mgr._global_paused = False
        logger.info("Scheduler fortgesetzt (%d Jobs)", len(scheduler_mgr.scheduler.get_jobs()))

    logger.info("Hintergrund-Aktivitaeten fortgesetzt")
    return _get_pause_state()


@router.get("/pause-status")
def get_pause_status():
    """Returns the current pause state."""
    return _get_pause_state()


@router.post("/trigger-thought")
async def trigger_thought(character_name: str,
    context_hint: str = "",
    fast: bool = False,
    tool_whitelist: str = "",
    suppress_notification: bool = False):
    """Forciert einen Gedanken-Tick fuer einen bestimmten Character.

    Umgeht Idle-Check, Cooldown, Probability. Nuetzlich zum Debuggen /
    Antreten ohne auf den 60s-Loop zu warten.

    Call: POST /world-dev/trigger-thought?user_id=XYZ&character_name=Kira
    Optional: context_hint, fast, tool_whitelist (kommasepariert), suppress_notification
    """
    from app.core.thoughts import get_thought_loop
    tl = get_thought_loop()
    if not tl:
        raise HTTPException(status_code=503, detail="ThoughtLoop nicht aktiv")

    _whitelist = [t.strip() for t in tool_whitelist.split(",") if t.strip()] or None

    import asyncio
    try:
        await asyncio.wait_for(
            tl.run_thought_turn(character_name,
                                context_hint=context_hint, fast=fast,
                                tool_whitelist=_whitelist,
                                suppress_notification=suppress_notification),
            timeout=300)
        return {"ok": True, "character": character_name, "context_hint": context_hint[:80]}
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Thought-Turn Timeout (>300s)")
    except Exception as e:
        logger.error("Thought trigger error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
