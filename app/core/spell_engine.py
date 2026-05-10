"""Spell-Engine — Magie als Item-Mechanik.

Magie ist kein eigenes System, sondern eine besondere Klasse von Items
mit folgenden meta-Feldern:

    incantation         — Phrase/Stichworte; vom Tool-LLM erkannt
    spell_mode          — "force" (Zauber) oder "gift" (Schriftrolle/Trank)
    clone_item_id       — Item das beim Ziel aktiviert wird (das "Effekt"-Item).
                          Default: Item selbst (z.B. ein Trank wirkt direkt).
    success_chance      — int 0-100 (default 100)
    copy_on_give        — bool. True = Quell-Item beim Caster wird beim
                          Uebergeben *kopiert* (Caster behaelt seine Instanz —
                          gelernter Zauberspruch). False = Quell-Item wird
                          beim Caster verbraucht (Einmal-Schriftrolle/Trank).
    success_text        — narrative Hinweis-Schnipsel fuer NPC bei Erfolg
    fail_text           — narrative Hinweis-Schnipsel fuer NPC bei Fehlschlag

Das eigentliche Wirken passiert ueber das bestehende Item-System
(``inventory.give_item`` mit mode=force fuer Zauber, mode=gift fuer
Schriftrollen). ``apply_condition`` etc. greift wie gewohnt.

Public API:
    build_spell_catalog(character) -> List[Dict]
    detect_cast(avatar, target, message) -> Optional[Dict]  (LLM call)
    execute_cast(avatar, target, spell_item) -> Dict (success/fail + hint text)
"""
import json
import random as _random
import re
from typing import Any, Dict, List, Optional

from app.core.log import get_logger

logger = get_logger("spell_engine")


def build_spell_catalog(character_name: str) -> List[Dict[str, Any]]:
    """Sammelt alle Spell-Items aus dem Inventar des Character.

    Spell-Item = jedes Inventar-Item dessen Definition ein
    ``incantation``-Feld hat. Funktioniert fuer Avatar (Zauber-Casting)
    genauso wie fuer NPCs (falls Du spaeter NPCs zaubern lassen willst).
    """
    try:
        from app.models.inventory import get_character_inventory, get_item
        inv = get_character_inventory(character_name, include_equipped=True) or {}
        items = inv.get("inventory", []) if isinstance(inv, dict) else []
        out: List[Dict[str, Any]] = []
        for entry in items:
            iid = entry.get("item_id") or ""
            if not iid:
                continue
            item = get_item(iid) or {}
            incant = (item.get("incantation") or "").strip()
            if not incant:
                continue
            out.append({
                "id": iid,
                "name": item.get("name") or iid,
                "incantation": incant,
                "description": (item.get("description") or "").strip(),
                "mode": (item.get("spell_mode") or "force").strip().lower(),
                "clone_item_id": (item.get("clone_item_id") or iid).strip(),
                "success_chance": int(item.get("success_chance") or 100),
                "copy_on_give": bool(item.get("copy_on_give", False)),
                "success_text": (item.get("success_text") or "").strip(),
                "fail_text": (item.get("fail_text") or "").strip(),
                "cast_activity": (item.get("cast_activity") or "").strip(),
                "anchor_item_id": (item.get("anchor_item_id") or "").strip(),
                "teleport_subject": (item.get("teleport_subject") or "caster").strip().lower(),
                "quantity": int(entry.get("quantity") or 1),
            })
        return out
    except Exception as e:
        logger.debug("build_spell_catalog failed for %s: %s", character_name, e)
        return []


def has_spell_detect_routing(avatar_name: str = "") -> bool:
    """True wenn der ``spell_detect``-Task ein gemapptes LLM hat.

    Pre-check fuer den Chat-Flow: wenn der Avatar Spell-Items hat aber
    der Task nicht geroutet ist, kann detect_cast nie feuern (resolve_llm
    liefert None, der try/except schluckt). Frontend kann mit dem
    Ergebnis eine sichtbare Warnung anzeigen.
    """
    try:
        from app.core.llm_router import resolve_llm
        return resolve_llm("spell_detect", agent_name=avatar_name) is not None
    except Exception:
        return False


def _format_catalog(catalog: List[Dict[str, Any]]) -> str:
    """Formatiert den Spell-Katalog fuer das LLM-Prompt-Template."""
    lines: List[str] = []
    for s in catalog:
        line = f"- id={s['id']} | incantation: \"{s['incantation']}\""
        if s.get("description"):
            line += f" | effect: {s['description'][:120]}"
        lines.append(line)
    return "\n".join(lines) if lines else "(none)"


def detect_cast(avatar_name: str, target_name: str, message: str,
                catalog: Optional[List[Dict[str, Any]]] = None
                ) -> Optional[Dict[str, Any]]:
    """Tool-LLM-Detection: hat der Avatar einen Zauber gewirkt?

    Returns das Spell-Catalog-Dict bei Match (mit confidence), sonst None.
    Confidence-Schwelle: >= 60. Darunter wird's verworfen — Erkennung
    soll konservativ sein, lieber missen als falsch positiv.

    Hard pre-filter: das Tool-LLM wird nur aufgerufen, wenn mindestens ein
    Incantation-Token (>=3 Zeichen, case-insensitive) im Text vorkommt.
    Sonst false-positives, weil das LLM thematisch verwandte Saetze
    ("Energie", "Magie") als Cast interpretiert obwohl die eigentliche
    Beschwoerung gar nicht gesprochen wurde.
    """
    if not message or not avatar_name:
        return None
    if catalog is None:
        catalog = build_spell_catalog(avatar_name)
    if not catalog:
        return None

    msg_lower = message.lower()
    incantation_match = False
    for s in catalog:
        inc = (s.get("incantation") or "").strip()
        if not inc:
            continue
        # Tokenize incantation, ignore short/filler tokens. Alle ueber 3 Zeichen
        # muessen vorkommen — die Beschwoerung ist in der Regel ein 1-3 Wort
        # Kunstwort, das im Alltagsgespraech nicht vorkommt.
        toks = [t for t in re.findall(r"[\wÄÖÜäöüß]+", inc.lower()) if len(t) >= 3]
        if toks and all(t in msg_lower for t in toks):
            incantation_match = True
            break
    if not incantation_match:
        logger.debug("spell_detect skip: keine Incantation-Tokens in '%s'", message[:60])
        return None

    try:
        from app.core.llm_router import llm_call
        from app.core.prompt_templates import render_task
        from app.models.character import get_character_language, LANGUAGE_MAP
        lang_code = (get_character_language(avatar_name) or "en").strip()
        language_name = LANGUAGE_MAP.get(lang_code, "English")
        sys_prompt, user_prompt = render_task(
            "spell_detect",
            avatar_name=avatar_name,
            target_name=target_name or "(no target)",
            message=message,
            spell_catalog=_format_catalog(catalog),
            language_name=language_name)
        response = llm_call(
            task="spell_detect",
            system_prompt=sys_prompt,
            user_prompt=user_prompt,
            agent_name=avatar_name)
        raw = (response.content or "").strip()
    except Exception as e:
        logger.debug("spell_detect llm_call failed: %s", e)
        return None

    # Robustes JSON-Parsing
    try:
        # Markdown-Fences strippen
        if raw.startswith("```"):
            raw = "\n".join(ln for ln in raw.split("\n") if not ln.strip().startswith("```"))
        # Erstes balanced JSON-Object suchen
        import re as _re
        m = _re.search(r"\{[^{}]*\}", raw)
        data = json.loads(m.group(0) if m else raw)
    except Exception:
        return None

    spell_id = (data.get("spell_id") or "").strip()
    if not spell_id:
        return None
    confidence = int(data.get("confidence") or 0)
    if confidence < 60:
        logger.info("spell_detect: low confidence (%d) for spell_id=%s — ignored",
                    confidence, spell_id)
        return None

    matched = next((s for s in catalog if s["id"] == spell_id), None)
    if not matched:
        logger.warning("spell_detect: LLM returned unknown spell_id=%s", spell_id)
        return None

    chat_substitute = (data.get("chat_substitute") or "").strip()
    return {**matched, "confidence": confidence, "chat_substitute": chat_substitute}


def execute_cast(avatar_name: str, target_name: str,
                 spell: Dict[str, Any]) -> Dict[str, Any]:
    """Wuerfelt Erfolg, klont das Effekt-Item auf das Ziel, verbraucht
    optional das Quell-Item, returns ein Result-Dict mit dem Hint-Text
    der dem NPC mitgegeben wird.

    Result keys:
        success      : bool
        hint         : str  (vorbereiteter Text fuer NPC-Prompt)
        spell_id     : str
        chance       : int
        roll         : int
    """
    chance = max(0, min(100, int(spell.get("success_chance", 100))))
    roll = _random.randint(1, 100)
    success = roll <= chance
    hint = ""
    spell_id = spell.get("id") or ""
    clone_id = spell.get("clone_item_id") or spell_id
    mode = spell.get("mode") or "force"
    anchor_item_id = (spell.get("anchor_item_id") or "").strip()
    teleport_subject = (spell.get("teleport_subject") or "caster").strip().lower()

    delivered_item_id = ""
    delivered_item_name = ""
    teleport_info: Dict[str, Any] = {}

    # Anker-Teleport-Pfad: ueberschreibt die normale give_item / effect-Logik.
    # Spell hat ein Anker-Item -> wir suchen seinen Standort, bewegen entweder
    # den Caster dorthin (subject=caster) oder holen den Anker-Traeger zum
    # Caster (subject=anchor_holder). Ist der Anker in einem Raum statt bei
    # einer Person, funktioniert nur subject=caster — anchor_holder failed
    # weil's keinen Traeger gibt (Feature, kein Bug).
    if success and anchor_item_id:
        try:
            from app.models.inventory import find_item_location
            from app.models.character import (
                get_character_current_location,
                get_character_current_room,
                save_character_current_location,
                save_character_current_room)
            # Caster nicht als Anker-Inhaber zaehlen — sonst "springt" er
            # zu sich selbst und nichts passiert.
            anchor = find_item_location(anchor_item_id, exclude_character=avatar_name)
            if not anchor:
                logger.info("Cast %s: Anker %s nirgends gefunden — Fail",
                            spell_id, anchor_item_id)
                success = False
            else:
                if teleport_subject == "anchor_holder":
                    # Anker-Traeger zum Caster ziehen. Geht nur bei character-Anker.
                    if anchor["kind"] != "character":
                        logger.info("Cast %s: subject=anchor_holder aber Anker im Raum — Fail",
                                    spell_id)
                        success = False
                    else:
                        moved = anchor["character"]
                        dest_loc = get_character_current_location(avatar_name) or ""
                        dest_room = get_character_current_room(avatar_name) or ""
                        if dest_loc:
                            save_character_current_location(moved, dest_loc)
                        if dest_room:
                            save_character_current_room(moved, dest_room)
                        teleport_info = {
                            "moved_character": moved,
                            "to_location": dest_loc,
                            "to_room": dest_room,
                            "subject": "anchor_holder",
                        }
                else:
                    # Caster zum Anker-Standort
                    if anchor["kind"] == "character":
                        holder = anchor["character"]
                        dest_loc = get_character_current_location(holder) or ""
                        dest_room = get_character_current_room(holder) or ""
                    else:
                        dest_loc = anchor.get("location_id") or ""
                        dest_room = anchor.get("room_id") or ""
                    if not dest_loc:
                        logger.info("Cast %s: Anker hat keinen Standort — Fail", spell_id)
                        success = False
                    else:
                        save_character_current_location(avatar_name, dest_loc)
                        if dest_room:
                            save_character_current_room(avatar_name, dest_room)
                        teleport_info = {
                            "moved_character": avatar_name,
                            "to_location": dest_loc,
                            "to_room": dest_room,
                            "subject": "caster",
                        }
        except Exception as e:
            logger.error("anchor teleport failed: %s", e)
            success = False
        # Quell-Item ggf. verbrauchen (Schriftrolle einmalig, gelernter Spruch
        # bleibt). Nur wenn der Teleport tatsaechlich erfolgreich war.
        if success and not spell.get("copy_on_give"):
            try:
                from app.models.inventory import remove_from_inventory
                remove_from_inventory(avatar_name, spell_id, quantity=1, force=True)
            except Exception as e:
                logger.debug("spell consume failed: %s", e)

    elif success:
        # Klassischer Pfad ohne Anker: Effekt-Item ans Ziel verteilen.
        # Quell-Item verbrauchen wenn nicht "kopiert beim Uebergeben".
        # copy_on_give=False (Default) heisst: Caster verliert sein Item
        # beim Wirken (Schriftrolle/Trank). copy_on_give=True heisst:
        # Caster behaelt seine Instanz (gelernter Zauberspruch).
        if not spell.get("copy_on_give"):
            try:
                from app.models.inventory import remove_from_inventory
                remove_from_inventory(avatar_name, spell_id, quantity=1, force=True)
            except Exception as e:
                logger.debug("spell consume failed: %s", e)
        # Self-cast mit Self-Clone (clone_item_id leer/== spell_id):
        # give_item wuerde hier korrupt arbeiten, weil das Inventar-Schema
        # UNIQUE(character_name, item_id) enforct — add_to_inventory + danach
        # consume_item collabieren zu einem Net-Minus-1, d.h. der Caster
        # verliert sein Item trotz copy_on_give=True. Stattdessen Effekte
        # direkt auf den Avatar anwenden, ohne das Inventar anzufassen.
        is_self_clone = (avatar_name == target_name and clone_id == spell_id)
        try:
            from app.models.inventory import give_item, get_item, apply_item_effects
            if is_self_clone:
                apply_item_effects(target_name, clone_id)
                _eff_item = get_item(clone_id) or {}
                delivered_item_id = clone_id
                delivered_item_name = (_eff_item.get("name") or clone_id).strip()
            else:
                # Effekt-Item ans Ziel — bei mode=force wird locked=True gesetzt
                # und apply_condition feuert sofort (siehe inventory.add_to_inventory)
                ok = give_item(
                    from_character=avatar_name,
                    to_character=target_name,
                    item_id=clone_id,
                    mode=mode,                  # "force" oder "gift"
                    consume_source=False,       # haben wir oben schon erledigt
                )
                if ok:
                    delivered_item_id = clone_id
                    _eff_item = get_item(clone_id) or {}
                    delivered_item_name = (_eff_item.get("name") or clone_id).strip()
                else:
                    logger.warning("give_item returned False during cast: %s -> %s (item=%s)",
                                   avatar_name, target_name, clone_id)
                    success = False
        except Exception as e:
            logger.error("give_item failed during cast: %s", e)
            success = False

    if success:
        # Optional: Avatar-Activity setzen (z.B. "Casting Spell"). Auto-Ablauf
        # laeuft ueber die duration_minutes der Activity (siehe periodic_jobs
        # _sub_activity_expiry). save_character_current_activity setzt
        # activity_started_at und activity_duration_minutes anhand der
        # Library-Definition automatisch.
        cast_activity = (spell.get("cast_activity") or "").strip()
        if cast_activity:
            try:
                from app.models.activity_library import (
                    get_library_activity, find_library_activity_by_name)
                from app.models.character import save_character_current_activity
                _act = (get_library_activity(cast_activity)
                        or find_library_activity_by_name(cast_activity))
                _act_name = (_act.get("name") if _act else cast_activity)
                save_character_current_activity(avatar_name, _act_name)
                logger.info("Cast activity set: %s -> %s", avatar_name, _act_name)
            except Exception as e:
                logger.warning("Cast activity set fehlgeschlagen: %s", e)

        hint = (spell.get("success_text") or "").strip()
        if not hint:
            if teleport_info:
                hint = (f"A teleport spell ({spell.get('name') or spell_id}) "
                        f"shifts the scene.")
            else:
                hint = f"A spell hits you ({spell.get('name') or spell_id})."
    else:
        hint = (spell.get("fail_text") or "").strip()
        if not hint:
            if anchor_item_id:
                hint = (f"The cast of {spell.get('name') or spell_id} fizzled — "
                        "the anchor could not be reached.")
            else:
                hint = (f"Someone tried to cast {spell.get('name') or spell_id} on you "
                        "but it failed — only a faint tingle.")

    logger.info("Cast %s by %s on %s: %s (roll %d/%d)",
                spell_id, avatar_name, target_name,
                "SUCCESS" if success else "FAIL", roll, chance)
    return {
        "success": success,
        "hint": hint,
        "spell_id": spell_id,
        "spell_name": (spell.get("name") or spell_id),
        "chance": chance,
        "roll": roll,
        "delivered_item_id": delivered_item_id,
        "delivered_item_name": delivered_item_name,
        # Anker-Teleport-Info (leer wenn kein anchor_item_id am Spell).
        # Frontend kann darauf reagieren (Map refresh, Toast "X wurde
        # versetzt nach Y") — oder ignorieren.
        "teleport": teleport_info,
        # Vom LLM gelieferter narrativer Ersatztext (statt Beschwoerung).
        # Wird in chat.py als user_input verwendet, damit das RP-LLM nicht
        # auf die rohe Incantation reagiert.
        "chat_substitute": (spell.get("chat_substitute") or "").strip(),
    }


def detect_and_cast(avatar_name: str, target_name: str,
                    message: str) -> Optional[Dict[str, Any]]:
    """Bequeme Combo: wenn der Avatar einen Spell wirkt, sofort ausfuehren.

    Returns das Result-Dict (siehe execute_cast) bei Match, sonst None.
    Aufrufer kann das ``hint``-Feld an den naechsten NPC-System-Prompt
    anhaengen, damit der Char narrativ darauf reagiert.
    """
    detected = detect_cast(avatar_name, target_name, message)
    if not detected:
        return None
    return execute_cast(avatar_name, target_name, detected)
