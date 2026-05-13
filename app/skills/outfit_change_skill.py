"""ChangeOutfit Skill — Wechselt zwischen Outfit-Pieces aus dem Inventar.

Dieser Skill ERZEUGT KEINE neuen Outfits — er ruestet nur Pieces an
und ab, die der Character bereits in seinem Inventar hat. Fuer das
Erzeugen neuer Pieces / Outfits ist OutfitCreation zustaendig.

Input-Format (JSON oder Freitext):
  - JSON: {"equip": ["item_id1", "Name2"], "unequip_slots": ["outer"],
            "unequip_items": ["item_id3"], "outfit_preset": "Streetwear"}
  - Freitext: "Lederjacke, schwarze Sneaker" — wird gegen Inventar gematcht
"""
import json
import re
from typing import Any, Dict, List

from .base import BaseSkill, ToolSpec

from app.core.log import get_logger
from app.models.inventory import (
    get_character_inventory,
    get_item,
    resolve_item_id,
    equip_piece,
    unequip_piece,
    equip_item,
    unequip_item)

logger = get_logger("outfit_change")


class OutfitChangeSkill(BaseSkill):
    """Wechselt zwischen Pieces im Inventar — keine Erfindungen."""

    SKILL_ID = "outfit_change"
    ALWAYS_LOAD = True

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        from app.core.prompt_templates import load_skill_meta
        meta = load_skill_meta("outfit_change")
        self.name = meta["name"]
        self.description = meta["description"]
        self._defaults = {"enabled": True}
        logger.info("ChangeOutfit Skill initialized (swap-only)")

    def execute(self, raw_input: str) -> str:
        if not self.enabled:
            return "ChangeOutfit Skill ist deaktiviert."
        try:
            return self._execute_inner(raw_input)
        except Exception as e:
            logger.exception("ChangeOutfit Fehler: %s", e)
            return f"Fehler beim Outfit-Wechsel: {e}"

    def _execute_inner(self, raw_input: str) -> str:
        ctx = self._parse_base_input(raw_input)
        user_id = ctx.get("user_id", "").strip()
        character_name = ctx.get("agent_name", "").strip()
        if not character_name:
            return "Fehler: user_id/character_name fehlt."

        # Input parsen — JSON oder Freitext
        spec = self._parse_input_spec(ctx)

        results: List[str] = []
        errors: List[str] = []

        # 1) outfit_type: Komplett-Umkleide auf den Dress-Code wechseln.
        # Sucht pro Slot ein passendes Piece im Inventar (target-type oder neutral).
        if spec.get("outfit_type"):
            ttype = spec["outfit_type"].strip()
            # Dress-Code-Sanity-Check: wenn der Character an einem Ort ist
            # dessen outfit_type vom LLM-Vorschlag abweicht, Location gewinnt.
            # Verhindert "Sport-Outfit im Nachtclub"-Halluzinationen.
            try:
                from app.core.outfit_rules import known_outfit_types
                known = {t.strip().lower() for t in known_outfit_types()}
                loc_type = self._resolve_location_outfit_type(character_name)
                if loc_type and loc_type.lower() != ttype.lower() and ttype.lower() in known:
                    logger.info(
                        "ChangeOutfit [%s]: outfit_type '%s' ueberstimmt durch "
                        "Location-Dress-Code '%s'",
                        character_name, ttype, loc_type)
                    ttype = loc_type
            except Exception as _err:
                logger.debug("Dress-Code-Override pruefen fehlgeschlagen: %s", _err)

            picks = self._pick_pieces_by_type(character_name, ttype)
            # Multi-Slot-Pieces tauchen in picks unter mehreren Slots auf —
            # pro item_id nur einmal equip_piece aufrufen (das raeumt die
            # Mirror-Slots eigenstaendig).
            for iid in dict.fromkeys(picks.values()):
                r = equip_piece(character_name, iid)
                if r.get("status") == "ok":
                    nm = self._item_label(iid)
                    slots_str = "+".join(r.get("slots") or [])
                    results.append(f"'{nm}' angelegt (Slot {slots_str})")
                else:
                    errors.append(f"'{self._item_label(iid)}': {r.get('reason', 'equip fehlgeschlagen')}")
            if not picks:
                errors.append(f"Keine passenden Pieces fuer Typ '{ttype}' im Inventar.")

        # 2) Unequip Slots (z.B. "outer" -> Jacke ablegen)
        for slot in spec.get("unequip_slots", []):
            res = unequip_piece(character_name, slot=slot)
            if res.get("status") == "ok":
                results.append(f"Slot '{slot}' geleert")
            else:
                errors.append(f"Slot '{slot}': {res.get('reason', 'unbekannt')}")

        # 3) Unequip einzelne Items
        for token in spec.get("unequip_items", []):
            iid = resolve_item_id(token) or token
            # erst als Piece versuchen, dann als Item
            r = unequip_piece(character_name, item_id=iid)
            if r.get("status") != "ok":
                r = unequip_item(character_name, iid)
            if r.get("status") == "ok":
                results.append(f"'{self._item_label(iid)}' abgelegt")
            else:
                errors.append(f"'{token}': {r.get('reason', 'nicht equipped')}")

        # 4) Equip Tokens (Piece → equip_piece, sonst → equip_item)
        inv_items = self._inventory_item_index(character_name)
        for token in spec.get("equip", []):
            iid = self._match_inventory(token, inv_items)
            if not iid:
                errors.append(
                    f"'{token}' nicht im Inventar. Verfuegbar: "
                    + (", ".join(self._inventory_summary(inv_items)) or "(leer)")
                )
                continue
            it = get_item(iid)
            if not it:
                errors.append(f"'{token}': Item-Definition fehlt")
                continue
            if it.get("category") == "outfit_piece":
                r = equip_piece(character_name, iid)
                if r.get("status") == "ok":
                    slots_str = "+".join(r.get("slots") or [])
                    msg = f"'{it.get('name', iid)}' angelegt (Slot {slots_str})"
                    if r.get("displaced"):
                        labels = ", ".join(self._item_label(d) for d in r["displaced"])
                        msg += f", ersetzt '{labels}'"
                    results.append(msg)
                else:
                    errors.append(f"'{token}': {r.get('reason', 'equip fehlgeschlagen')}")
            else:
                r = equip_item(character_name, iid)
                if r.get("status") == "ok":
                    results.append(f"'{it.get('name', iid)}' an die Hand genommen")
                else:
                    errors.append(f"'{token}': {r.get('reason', 'equip fehlgeschlagen')}")

        # 5) Location/Activity-basierte Slot-Auffuellung.
        # Schliesst die Halluzinations-Luecke wenn das LLM Items nennt die
        # nicht im Inventar sind (oder gar nichts nennt). Equipped-State nach
        # Schritt 1-4 nehmen, dann fuer noch leere Slots ein passendes Piece
        # aus der Location-Type-Auswahl ergaenzen.
        #
        # ABER: wenn der Aufrufer explizit ``unequip_slots``/``unequip_items``
        # gesetzt hat, ist die Intent "ablegen" — die Auto-Auffuellung wuerde
        # den geleerten Slot direkt mit dem naechstbesten Piece (oft sogar
        # demselben) wieder fuellen und die Aktion verschlucken. Daher: bei
        # explizitem Unequip Auto-Auffuellung ueberspringen.
        explicit_unequip = bool(spec.get("unequip_slots")
                                 or spec.get("unequip_items"))
        loc_type = self._resolve_location_outfit_type(character_name)
        if loc_type and not spec.get("outfit_type") and not explicit_unequip:
            try:
                from app.models.inventory import get_equipped_pieces
                already_equipped = get_equipped_pieces(character_name) or {}
                loc_picks = self._pick_pieces_by_type(character_name, loc_type)
                # Pro distinct item_id einmal equip_piece — und nur wenn KEINER
                # der Slots, die das Piece belegen wuerde, schon besetzt ist.
                # Sonst wuerde ein Multi-Slot-Kleid einen bereits angezogenen
                # Top/Skirt verdraengen, was im Auto-Fill nicht gewollt ist.
                seen_iids: set = set()
                for slot, iid in loc_picks.items():
                    if iid in seen_iids:
                        continue
                    seen_iids.add(iid)
                    item_def = get_item(iid) or {}
                    item_slots = (item_def.get("outfit_piece") or {}).get("slots") or []
                    if any(already_equipped.get(s) for s in item_slots):
                        continue
                    r = equip_piece(character_name, iid)
                    if r.get("status") == "ok":
                        nm = self._item_label(iid)
                        slots_str = "+".join(r.get("slots") or [])
                        results.append(f"'{nm}' angelegt (Slot {slots_str}, Location-Match {loc_type})")
            except Exception as _e:
                logger.debug("Location-Auffuellung fehlgeschlagen: %s", _e)

        # 6) Pflicht-Slot-Check: ohne top ist das Outfit unvollstaendig.
        # (Multi-Slot-Pieces wie Kleid/Jumpsuit setzen top mit, also reicht
        # die Pruefung auf top.) Triggert OutfitCreation als Fallback.
        try:
            from app.models.inventory import get_equipped_pieces
            final_eq = get_equipped_pieces(character_name) or {}
        except Exception:
            final_eq = {}
        missing_essential = not final_eq.get("top")

        # Ergebnis zusammenfassen
        out_parts: List[str] = []
        if results:
            out_parts.append(" • ".join(results))
        if errors:
            out_parts.append("FEHLER: " + "; ".join(errors))
        if not out_parts:
            out_parts.append("Nichts geaendert — kein passendes Piece im Inventar.")

        # Fallback auf OutfitCreation. Triggert wenn:
        #   - Pflicht-Slots (top/full_body) bleiben leer (Inventar reicht nicht)
        #   - explizite equip-Tokens kamen, aber kein einziger Treffer
        #   - outfit_type gesetzt, aber kein Match
        #   - gar nichts spezifiziert UND nichts geaendert
        # Aber NICHT bei explizitem Unequip — sonst wird beim "Top
        # ausziehen" ein neues Top halluziniert.
        equip_tokens = spec.get("equip", [])
        type_no_match = bool(spec.get("outfit_type")) and not results
        empty_call = (
            not results and not spec.get("unequip_slots")
            and not spec.get("unequip_items")
            and not spec.get("outfit_type")
        )
        if not explicit_unequip and (
            missing_essential or (equip_tokens and not results)
            or type_no_match or empty_call):
            fallback_result = self._fallback_to_creation(
                character_name, ctx, location_type=loc_type)
            if fallback_result:
                return fallback_result

        return ". ".join(out_parts)

    # ------------------------------------------------------------------
    # Input-Parsing
    # ------------------------------------------------------------------

    def _parse_input_spec(self, ctx: Dict[str, Any]) -> Dict[str, Any]:
        """Normalisiert den Skill-Input in ein Dict mit equip/unequip/type Feldern."""
        spec: Dict[str, Any] = {
            "equip": [],
            "unequip_slots": [],
            "unequip_items": [],
            "outfit_type": "",
        }
        for key in ("equip", "unequip_slots", "unequip_items"):
            val = ctx.get(key)
            if isinstance(val, list):
                spec[key] = [str(x).strip() for x in val if str(x).strip()]
            elif isinstance(val, str) and val.strip():
                spec[key] = [val.strip()]
        if isinstance(ctx.get("outfit_type"), str):
            spec["outfit_type"] = ctx["outfit_type"].strip()

        # Freitext-Fallback (input-Feld) — Liste von Tokens, kommagetrennt.
        # Einzelner Token ohne Komma -> als outfit_type interpretieren.
        text = (ctx.get("input") or "").strip()
        if text and not (spec["equip"] or spec["unequip_slots"]
                          or spec["unequip_items"] or spec["outfit_type"]):
            tokens = [t.strip() for t in re.split(r"[,;]+", text) if t.strip()]
            if len(tokens) == 1 and " " not in tokens[0]:
                # Kurzer Single-Token -> wahrscheinlich outfit_type (Casual, Sport, ...)
                spec["outfit_type"] = tokens[0]
            else:
                spec["equip"] = tokens
        return spec

    # ------------------------------------------------------------------
    # Inventar-Matching
    # ------------------------------------------------------------------

    def _inventory_item_index(self, character_name: str) -> List[Dict[str, Any]]:
        """Liefert die Inventar-Items des Characters mit Item-Details."""
        inv = get_character_inventory(character_name, include_equipped=True)
        return inv.get("inventory", [])

    def _match_inventory(self, token: str, inv: List[Dict[str, Any]]) -> str:
        """Resolved einen Token (ID oder Name) gegen die Inventar-Items."""
        if not token:
            return ""
        token_l = token.strip().lower()
        # 1) ID exakt
        for entry in inv:
            if entry.get("item_id") == token:
                return token
        # 2) Name exakt (case-insensitive)
        for entry in inv:
            if (entry.get("item_name") or "").strip().lower() == token_l:
                return entry.get("item_id", "")
        # 3) Substring-Match auf Name (vermeide false positives bei zu kurzem Token)
        if len(token_l) >= 3:
            for entry in inv:
                name_l = (entry.get("item_name") or "").strip().lower()
                if token_l in name_l or name_l in token_l:
                    return entry.get("item_id", "")
        return ""

    def _inventory_summary(self, inv: List[Dict[str, Any]]) -> List[str]:
        return [
            (e.get("item_name") or e.get("item_id") or "?")
            for e in inv
            if e.get("item_category") in ("outfit_piece", "tool", "decoration", "gift", "consumable")
        ][:20]

    def _fallback_to_creation(self, character_name: str,
                              ctx: Dict[str, Any],
                              location_type: str = "") -> str:
        """Wenn ChangeOutfit nichts Passendes findet, OutfitCreation als
        Fallback ausfuehren — erzeugt neue Pieces via LLM.

        location_type wird (falls nicht im Input) als Kontext mitgegeben,
        damit OutfitCreation wenigstens den Dress-Code trifft.
        """
        try:
            from app.core.dependencies import get_skill_manager
            sm = get_skill_manager()
            creation_skill = sm.get_skill("outfit_creation")
            if not creation_skill:
                return ""
            # Pruefen ob der Skill fuer diesen Character aktiviert + nicht am Limit
            from app.models.character import get_character_skill_config
            cfg = get_character_skill_config(character_name, "outfit_creation")
            if cfg and not cfg.get("enabled", True):
                return ""
            if hasattr(creation_skill, 'is_limit_reached') and creation_skill.is_limit_reached(character_name):
                return ""
            # Hint aufbauen: Original-Input + Location-Type + Activity.
            hint = (ctx.get("input") or "").strip()
            extras: List[str] = []
            if location_type:
                extras.append(f"appropriate for {location_type}")
            try:
                from app.models.character import get_character_current_activity
                act = (get_character_current_activity(character_name) or "").strip()
                if act:
                    extras.append(f"while {act}")
            except Exception:
                pass
            if extras:
                ctx_str = ", ".join(extras)
                hint = f"{hint} ({ctx_str})" if hint else ctx_str
            import json as _json
            raw = _json.dumps({
                "user_id": "",
                "agent_name": character_name,
                "input": hint,
                "skip_daily_limit": False,
            })
            logger.info("ChangeOutfit Fallback -> OutfitCreation fuer %s (hint: %s)",
                         character_name, hint[:80])
            return creation_skill.execute(raw)
        except Exception as e:
            logger.warning("ChangeOutfit Fallback fehlgeschlagen: %s", e)
            return ""

    @staticmethod
    def _item_label(item_id: str) -> str:
        it = get_item(item_id)
        return it.get("name") if it else item_id

    # ------------------------------------------------------------------
    # outfit_type → vollstaendiges Piece-Set aus dem Inventar
    # ------------------------------------------------------------------

    def _resolve_location_outfit_type(self, character_name: str) -> str:
        """Liefert den outfit_type des aktuellen Raums, sonst der Location.
        Leer wenn der Character keinen Aufenthaltsort hat oder kein Dress-Code
        gesetzt ist.
        """
        try:
            from app.models.character import (
                get_character_current_location, get_character_profile)
            from app.models.world import get_location_by_id, get_room_by_id
            loc_id = get_character_current_location(character_name) or ""
            if not loc_id:
                return ""
            loc = get_location_by_id(loc_id) or {}
            room_id = (get_character_profile(character_name) or {}).get("current_room", "")
            if room_id:
                room = get_room_by_id(loc, room_id)
                if room:
                    rt = (room.get("outfit_type") or "").strip()
                    if rt:
                        return rt
            return (loc.get("outfit_type") or "").strip()
        except Exception:
            return ""

    def _pick_pieces_by_type(self, character_name: str,
                              target_type: str) -> Dict[str, str]:
        """Waehlt pro Slot ein Piece aus dem Inventar das zum target_type passt.

        Prioritaet:
        - Pieces, die target_type explizit in outfit_types haben (strikt)
        - Falls nichts Striktes vorhanden: aktuell angelegtes Piece in diesem
          Slot behalten (implizit, indem wir den Slot nicht anfassen)

        Liefert {slot: item_id} nur fuer Slots wo ein strikt passender Kandidat
        gefunden wurde — non-strikte Slots bleiben beim bestehenden Eintrag,
        damit neutrale Schuhe/Guertel nicht unnoetig getauscht werden.
        """
        inv = self._inventory_item_index(character_name)
        # Multi-Slot-Pieces zuerst behandeln, damit sie alle ihre Slots
        # reservieren bevor Single-Slot-Kandidaten reinrutschen — sonst
        # wuerde z.B. ein Skirt fuer 'bottom' gepickt, dann wuerde ein
        # spaeter equippter Kleid (top+bottom) den Skirt verdraengen.
        tlow = target_type.strip().lower()
        candidates: List[tuple] = []  # (slot_count_desc, iid, slots_list)
        for entry in inv:
            if entry.get("item_category") != "outfit_piece":
                continue
            op = entry.get("outfit_piece") or {}
            slots = [str(s or "").strip().lower() for s in (op.get("slots") or []) if s]
            slots = [s for s in slots if s]
            if not slots:
                continue
            types = [str(t).strip().lower() for t in (op.get("outfit_types") or [])]
            if tlow not in types:
                continue
            iid = entry.get("item_id") or ""
            if not iid:
                continue
            candidates.append((len(slots), iid, slots))
        # Multi-Slot-Pieces (mehr Slots) zuerst, dann Single-Slot.
        candidates.sort(key=lambda c: -c[0])
        by_slot_strict: Dict[str, str] = {}
        for _count, iid, slots in candidates:
            # Nur uebernehmen wenn KEINER der Slots schon belegt ist.
            if any(s in by_slot_strict for s in slots):
                continue
            for slot in slots:
                by_slot_strict[slot] = iid
        return by_slot_strict

    # ------------------------------------------------------------------
    # Tool-Spec
    # ------------------------------------------------------------------

    def as_tool(self, **kwargs) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=self.description,
            func=self.execute)
