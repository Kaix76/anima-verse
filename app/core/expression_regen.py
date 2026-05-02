"""Expression Regeneration — generates outfit images with expression/pose variants.

Lazy on-demand: the frontend requests an expression image via the endpoint,
and this module generates it if not cached. Results are cached per mood+pose
combination in outfits/expressions/.
"""

import hashlib
import json
import os
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Set

from app.core.log import get_logger
from app.core.expression_pose_maps import (
    DEFAULT_EXPRESSION,
    DEFAULT_POSE,
    resolve_expression_prompt,
    resolve_pose_prompt)

logger = get_logger(__name__)

# In-flight generation tracking (character:cache_key)
_generating: Set[str] = set()
_failed: Set[str] = set()  # tracks recently failed generations to avoid retry loops
_generating_lock = threading.Lock()
# Per-Character-Mutex. Gleicher Character wird serialisiert (Datei-Kollision
# im Sidecar-Write und Ref-Bild-Sharing), verschiedene Characters laufen
# parallel — das erlaubt dass z.B. Kira auf ComfyUI-4070 und Kai auf
# ComfyUI-3090 gleichzeitig generieren. _select_backend_for_workflow nutzt
# Round-Robin auf gleich-cost Backends, um die Last gleichmaessig zu verteilen.
_char_generation_mutexes: Dict[str, threading.Lock] = {}
_char_mutexes_create_lock = threading.Lock()


def _get_char_mutex(character_name: str) -> threading.Lock:
    with _char_mutexes_create_lock:
        mx = _char_generation_mutexes.get(character_name)
        if mx is None:
            mx = threading.Lock()
            _char_generation_mutexes[character_name] = mx
        return mx

# Env config — Format: "workflow:Name" oder "backend:Name"


def _cleanup_stale_temps(expr_dir: Path) -> None:
    """Entfernt liegen gebliebene .tmp_ / _temp_ Dateien aus dem Expressions-Dir."""
    count = 0
    for f in expr_dir.iterdir():
        if not f.is_file():
            continue
        if f.name.startswith(".tmp_") or f.name.startswith("_temp_"):
            try:
                f.unlink()
                count += 1
            except OSError:
                pass
    if count:
        logger.info("Cleanup: %d veraltete temp Dateien entfernt", count)


def _get_expressions_dir(character_name: str) -> Path:
    """Returns the expressions cache directory for a character.

    Frueher war das ein 'variants/' Unterordner — seit dem Refactor liegen
    die Variant-Bilder direkt im outfits/ Ordner (Outfit-Bilder gibt es nicht
    mehr, Variants partitionieren sich via Cache-Key aus Character + Equipped +
    Pose + Expression).
    """
    from app.models.character import get_character_outfits_dir
    expr_dir = get_character_outfits_dir(character_name)
    expr_dir.mkdir(parents=True, exist_ok=True)
    return expr_dir


def _normalize_activity(activity: str) -> str:
    """Reduce verbose activity text to a short canonical form for stable cache keys.

    Long, detailed activity descriptions change with every LLM response,
    causing permanent cache misses.  We keep only the first 4 words
    (lowercased, stripped of punctuation) so that similar activities
    like 'kneeling on the floor' and 'kneeling on the floor looking up'
    map to the same bucket.
    """
    text = activity.strip().lower()
    # Remove punctuation except hyphens
    text = re.sub(r"[^\w\s-]", "", text)
    words = text.split()[:4]
    return " ".join(words)


def _safe_name(name: str) -> str:
    """Replace spaces with underscores for safe filenames."""
    return name.replace(" ", "_")


def _equipped_signature(equipped_pieces: Optional[Dict[str, str]] = None,
                        equipped_items: Optional[list] = None,
                        equipped_pieces_meta: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """Stable signature der getragenen Items (Pieces + sonstige Ausruestung).

    Slot-Reihenfolge fix sortiert, damit gleiche Equip-Sets immer den
    gleichen Hash erzeugen. Items alphabetisch. Farb-Overrides (meta)
    fliessen pro Slot als color=... mit in die Signatur ein.
    """
    parts = []
    if equipped_pieces:
        for slot in sorted(equipped_pieces.keys()):
            iid = (equipped_pieces[slot] or "").strip()
            if iid:
                color = ""
                if equipped_pieces_meta:
                    _m = equipped_pieces_meta.get(slot) or {}
                    if isinstance(_m, dict):
                        color = (_m.get("color") or "").strip()
                if color:
                    parts.append(f"{slot}={iid}#{color}")
                else:
                    parts.append(f"{slot}={iid}")
    if equipped_items:
        cleaned = sorted({(i or "").strip() for i in equipped_items if i})
        if cleaned:
            parts.append("items:" + ",".join(cleaned))
    return "|".join(parts)


def _cache_key(mood: str, activity: str,
               character_name: str = "",
               equipped_pieces: Optional[Dict[str, str]] = None,
               equipped_items: Optional[list] = None,
               equipped_pieces_meta: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """Build a deterministic cache key.

    Garbage-Activities (Mood-Leakage, "none", "No clothing changes", etc.)
    werden hier zentralisiert auf "" gemappt. Damit verwenden Lookup und
    Store IMMER denselben Key — sonst wuerde die Trigger-Filterung
    (die activity '' speichert) am Lookup vorbeilaufen, wenn der Frontend
    die Roh-Activity '...feels suspicious...' aus dem Character-State pollt.
    """
    act_filtered = _normalize_activity_for_trigger(activity, mood)
    act = _normalize_activity(act_filtered)
    eq = _equipped_signature(equipped_pieces, equipped_items, equipped_pieces_meta)
    raw = f"{mood.strip().lower()}:{act}:{eq}"
    h = hashlib.md5(raw.encode()).hexdigest()[:12]
    if character_name:
        return f"{_safe_name(character_name)}_{h}"
    return h


def get_cached_expression(character_name: str,
                          mood: str, activity: str,
                          equipped_pieces: Optional[Dict[str, str]] = None,
                          equipped_items: Optional[list] = None,
                          equipped_pieces_meta: Optional[Dict[str, Dict[str, Any]]] = None) -> Optional[Path]:
    """Check if a cached expression image exists. Returns path or None."""
    expr_dir = _get_expressions_dir(character_name)
    key = _cache_key(mood, activity, character_name, equipped_pieces, equipped_items, equipped_pieces_meta)
    for ext in (".png", ".jpg", ".webp"):
        path = expr_dir / f"{key}{ext}"
        if path.exists():
            return path
    return None


def is_generating(character_name: str, mood: str, activity: str,
                  equipped_pieces: Optional[Dict[str, str]] = None,
                  equipped_items: Optional[list] = None,
                  equipped_pieces_meta: Optional[Dict[str, Dict[str, Any]]] = None) -> bool:
    """True wenn Generation laeuft ODER im Coalesce-Fenster wartet.

    Coalesce-Pending zaehlt als generating, damit das FE-Polling nicht fuer
    jeden Poll einen neuen Trigger absetzt (was den Debounce-Timer resetten
    wuerde) und stattdessen 202 bekommt bis das Bild tatsaechlich da ist.
    """
    key = f"{character_name}:{_cache_key(mood, activity, character_name, equipped_pieces, equipped_items, equipped_pieces_meta)}"
    with _generating_lock:
        if key in _generating:
            return True
        pending = _pending_triggers.get(character_name)
        if pending:
            pending_key = f"{character_name}:{_cache_key(pending.get('mood', ''), pending.get('activity', ''), character_name, pending.get('equipped_pieces'), pending.get('equipped_items'), pending.get('equipped_pieces_meta'))}"
            if pending_key == key:
                return True
    return False


def has_failed(character_name: str, mood: str, activity: str,
               equipped_pieces: Optional[Dict[str, str]] = None,
               equipped_items: Optional[list] = None,
               equipped_pieces_meta: Optional[Dict[str, Dict[str, Any]]] = None) -> bool:
    """Check if generation recently failed for this combo (avoids retry loops)."""
    key = f"{character_name}:{_cache_key(mood, activity, character_name, equipped_pieces, equipped_items, equipped_pieces_meta)}"
    with _generating_lock:
        return key in _failed


def invalidate_variants_for_item(item_id: str) -> int:
    """Loescht gezielt die Variant-Dateien, die das geaenderte Item in ihrem
    equipped_pieces/items enthielten — via Sidecar-JSON neben dem PNG.

    Variants ohne Sidecar (z.B. aus alten Generationen vor dem Sidecar-Feld)
    werden uebersprungen, nicht pauschal mitgeloescht.
    """
    if not item_id:
        return 0
    try:
        from app.models.character import list_available_characters
    except Exception:
        return 0
    total = 0
    for char_name in list_available_characters():
        try:
            expr_dir = _get_expressions_dir(char_name)
            if not expr_dir.exists():
                continue
            for sidecar in expr_dir.glob("*.json"):
                try:
                    meta = json.loads(sidecar.read_text(encoding="utf-8"))
                except Exception:
                    continue
                eq_pieces = meta.get("equipped_pieces") or {}
                eq_items = meta.get("equipped_items") or []
                in_pieces = item_id in (eq_pieces.values() if isinstance(eq_pieces, dict) else [])
                in_items = item_id in eq_items
                if not (in_pieces or in_items):
                    continue
                # Zugehoeriges Bild + Sidecar loeschen
                for ext in (".png", ".jpg", ".webp"):
                    img = sidecar.with_suffix(ext)
                    if img.exists():
                        try:
                            img.unlink()
                            total += 1
                        except OSError:
                            pass
                try:
                    sidecar.unlink()
                except OSError:
                    pass
        except Exception as e:
            logger.debug("invalidate_variants_for_item %s/%s: %s", char_name, e)
    if total:
        logger.info("Variant-Invalidierung wegen Item %s: %d Dateien geloescht", item_id, total)
    return total


def clear_failed_marker(character_name: str, mood: str, activity: str,
                         equipped_pieces: Optional[Dict[str, str]] = None,
                         equipped_items: Optional[list] = None,
                         equipped_pieces_meta: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    """Entfernt den failed-Marker fuer eine bestimmte Kombination,
    damit die Generation erneut versucht werden kann."""
    key = f"{character_name}:{_cache_key(mood, activity, character_name, equipped_pieces, equipped_items, equipped_pieces_meta)}"
    with _generating_lock:
        _failed.discard(key)


def clear_expression_cache(character_name: str) -> int:
    """Clear all cached expression images for a character."""
    expr_dir = _get_expressions_dir(character_name)
    count = 0
    for f in expr_dir.iterdir():
        if not f.is_file():
            continue
        f.unlink()
        count += 1
    # Also clear failed-generation markers so variants are retried
    with _generating_lock:
        prefix = f"{character_name}:"
        stale = {k for k in _failed if k.startswith(prefix)}
        _failed.difference_update(stale)
    if count or stale:
        logger.info("Expression-Cache geleert: %d Dateien, %d Failed-Marker (%s)",
                     count, len(stale), character_name)
    return count


_EXPRESSION_COOLDOWN = 300  # Sekunden zwischen Expression-Generierungen pro Character
_last_expression_time: Dict[str, float] = {}  # character_name -> timestamp

# Coalesce-Fenster: Triggers die in dieser Zeit fuer denselben Character kommen
# werden gebuendelt (latest state wins). Verhindert dass ein einzelner Chat-Turn
# mehrere Varianten erzeugt (Mood-Change → Outfit-Unequip → Activity-Classify).
#
# Groesse: das Classify-LLM (2-LLM-Hops intent+extraction) braucht in Praxis
# 6-12s bis es den kanonischen Activity-Namen liefert. Mit einem Fenster < 12s
# feuert der Mood-getriggerte Variant noch mit der alten/unklassifizierten
# Activity, bevor der Classify-Trigger uebernehmen kann. 12s ist der Kompromiss
# zwischen "Chat-Ende bis Variant sichtbar" und "alle Triggers gebuendelt".
_COALESCE_WINDOW = 10.0
_pending_triggers: Dict[str, Dict[str, Any]] = {}   # character → request dict
_pending_timers: Dict[str, threading.Timer] = {}    # character → scheduled Timer

# Aktivitaets-Strings, die KEINE echte Aktivitaet sind und Expression-Variants
# nicht eigenstaendig triggern duerfen. Kommen typisch aus LLM-Extractoren die
# manchmal die Mood, einen Negativ-Marker ("nichts geaendert"), oder einen
# leeren String in den Activity-Slot schreiben. Wuerden sonst pro Variante
# einen eigenen Cache-Eintrag und damit einen kompletten Image-Gen-Cycle
# erzeugen (~60s GPU-Zeit pro Garbage-Activity).
_GARBAGE_ACTIVITIES = frozenset({
    "", "none", "n/a", "null", "nothing", "no",
    "no clothing changes", "no outfit changes", "no changes",
    "unchanged", "no change", "keine aenderung", "keine aktivitaet",
    "no activity", "no action", "idle",
})


def _normalize_activity_for_trigger(activity: str, mood: str) -> str:
    """Filtert Garbage-Activities und Mood-Leakage. Liefert "" wenn Activity
    nicht als eigenstaendiger Trigger zaehlen sollte.
    """
    a = (activity or "").strip()
    if not a:
        return ""
    a_low = a.lower()
    if a_low in _GARBAGE_ACTIVITIES:
        return ""
    # Mood-Leakage: LLM schreibt manchmal "feels suspicious" oder "is angry"
    # in den Activity-Slot, obwohl das die Stimmung ist.
    m_low = (mood or "").strip().lower()
    if m_low and a_low in (f"feels {m_low}", f"is {m_low}", f"feeling {m_low}", m_low):
        return ""
    return a


def trigger_expression_generation(character_name: str,
                                  mood: str, activity: str,
                                  equipped_pieces: Optional[Dict[str, str]] = None,
                                  equipped_items: Optional[list] = None,
                                  equipped_pieces_meta: Optional[Dict[str, Dict[str, Any]]] = None,
                                  ignore_cooldown: bool = False,
                                  ignore_feature_gate: bool = False,
                                  prompt_prefix: Optional[str] = None,
                                  coalesce: bool = True) -> bool:
    """Queue oder starte Expression-Generation.

    Per Default werden Triggers in einem kurzen Fenster gebuendelt
    (_COALESCE_WINDOW): ein Chat-Turn feuert typischerweise 3 Triggers hinter-
    einander (Mood, Outfit-Unequip, Activity-Classify), von denen nur der
    letzte den finalen State hat. Statt 3 Bilder zu generieren wartet der
    Trigger auf das Ende des Bursts und feuert dann einmal mit dem neusten
    State.

    coalesce=False umgeht den Debounce — fuer Aufrufer die garantiert nur ein
    einzelnes, sofortiges Bild wollen (Test-Hilfsmittel). Produktions-Pfade
    (Auto-Regen, Chat-Extraction, Garderobe-Preview, Skills) lassen coalesce
    auf True: die Vorschau-Pfade triggern typischerweise nur einmal, und der
    Preis ist die Debounce-Wartezeit die in der Praxis nicht stoert.

    Returns True wenn ein Trigger (oder pending Trigger) registriert wurde.
    """
    if prompt_prefix is None:
        prompt_prefix = os.environ.get("OUTFIT_IMAGE_PROMPT_PREFIX", "full body portrait").strip()

    if not ignore_feature_gate:
        try:
            from app.models.character_template import is_feature_enabled
            if not is_feature_enabled(character_name, "expression_variants_enabled"):
                return False
        except Exception:
            pass

    # Garbage-Activities (Mood-Leakage, "none", "No clothing changes", ...)
    # auf "" normalisieren — sonst landet jeder Quatsch-String als eigener
    # Cache-Key und triggert einen vollen Image-Gen+MultiSwap-Cycle.
    _normalized = _normalize_activity_for_trigger(activity, mood)
    if _normalized != activity:
        logger.info(
            "Expression-Trigger [%s]: activity '%s' -> '%s' normalisiert (Garbage-Filter)",
            character_name, activity, _normalized)
        activity = _normalized

    if not coalesce:
        return _do_trigger_expression_generation(
            character_name, mood, activity,
            equipped_pieces=equipped_pieces,
            equipped_items=equipped_items,
            equipped_pieces_meta=equipped_pieces_meta,
            ignore_cooldown=ignore_cooldown,
            prompt_prefix=prompt_prefix)

    new_key = _cache_key(mood, activity, character_name,
                         equipped_pieces, equipped_items, equipped_pieces_meta)
    request = {
        "mood": mood,
        "activity": activity,
        "equipped_pieces": equipped_pieces,
        "equipped_items": equipped_items,
        "equipped_pieces_meta": equipped_pieces_meta,
        "ignore_cooldown": ignore_cooldown,
        "prompt_prefix": prompt_prefix,
    }

    with _generating_lock:
        existing = _pending_triggers.get(character_name)
        if existing:
            existing_key = _cache_key(existing.get("mood", ""),
                                       existing.get("activity", ""),
                                       character_name,
                                       existing.get("equipped_pieces"),
                                       existing.get("equipped_items"),
                                       existing.get("equipped_pieces_meta"))
            if existing_key == new_key:
                # Identische Anfrage — Timer NICHT resetten (FE-Polling-Schutz)
                # aber ignore_cooldown hochziehen falls neuer Caller es setzt
                if ignore_cooldown and not existing.get("ignore_cooldown"):
                    existing["ignore_cooldown"] = True
                return True
            # State geaendert → alten Timer canceln, neuen setzen
            old_timer = _pending_timers.pop(character_name, None)
            if old_timer:
                try:
                    old_timer.cancel()
                except Exception:
                    pass

        _pending_triggers[character_name] = request
        timer = threading.Timer(_COALESCE_WINDOW,
                                _fire_coalesced_trigger,
                                args=[character_name])
        timer.daemon = True
        _pending_timers[character_name] = timer
        timer.start()
    return True


def _fire_coalesced_trigger(character_name: str) -> None:
    """Feuer der pending Trigger fuer einen Character am Ende des Coalesce-Fensters."""
    with _generating_lock:
        request = _pending_triggers.pop(character_name, None)
        _pending_timers.pop(character_name, None)
    if not request:
        return
    try:
        _do_trigger_expression_generation(character_name, **request)
    except Exception as e:
        logger.error("Coalesced Expression-Trigger fuer %s fehlgeschlagen: %s",
                      character_name, e)


def _do_trigger_expression_generation(character_name: str,
                                       mood: str, activity: str,
                                       equipped_pieces: Optional[Dict[str, str]] = None,
                                       equipped_items: Optional[list] = None,
                                       equipped_pieces_meta: Optional[Dict[str, Dict[str, Any]]] = None,
                                       ignore_cooldown: bool = False,
                                       prompt_prefix: str = "") -> bool:
    """Eigentliche Trigger-Logik: Cooldown-Check, Dedup, Thread-Spawn.

    Wird entweder direkt aufgerufen (coalesce=False) oder vom Timer am Ende
    des Coalesce-Fensters. Feature-Gate wurde schon im Wrapper geprueft.
    """
    import time as _time

    if not ignore_cooldown:
        now = _time.monotonic()
        last = _last_expression_time.get(character_name, 0)
        if now - last < _EXPRESSION_COOLDOWN:
            logger.debug("Expression cooldown aktiv fuer %s (noch %ds)",
                         character_name, int(_EXPRESSION_COOLDOWN - (now - last)))
            return False
    else:
        now = _time.monotonic()
    _last_expression_time[character_name] = now

    key = f"{character_name}:{_cache_key(mood, activity, character_name, equipped_pieces, equipped_items, equipped_pieces_meta)}"
    with _generating_lock:
        if key in _generating:
            return False
        _generating.add(key)

    def _run():
        # Pending-Eintrag im Panel waehrend Mutex-Wait, damit gestapelte
        # Expression-Triggers sichtbar sind.
        _pending_track_id = None
        try:
            from app.core.task_queue import get_task_queue
            _pending_track_id = get_task_queue().track_start(
                "expression_regen",
                f"Variant: {character_name} ({activity or 'idle'})",
                agent_name=character_name,
                start_running=False)
        except Exception:
            _pending_track_id = None
        try:
            # Per-Character-Mutex: gleicher Character seriell, verschiedene
            # Characters parallel (koennen auf unterschiedliche Backends/GPUs
            # verteilt werden).
            with _get_char_mutex(character_name):
                if _pending_track_id:
                    try:
                        get_task_queue().track_cancel(_pending_track_id)
                    except Exception:
                        pass
                    _pending_track_id = None
                result = generate_expression_image(character_name, mood, activity,
                                                    equipped_pieces, equipped_items,
                                                    equipped_pieces_meta=equipped_pieces_meta,
                                                    prompt_prefix=prompt_prefix)
            if result is None:
                with _generating_lock:
                    _failed.add(key)
        finally:
            if _pending_track_id:
                try:
                    get_task_queue().track_cancel(_pending_track_id)
                except Exception:
                    pass
            with _generating_lock:
                _generating.discard(key)

    t = threading.Thread(target=_run, daemon=True,
                         name=f"expr-regen-{character_name}")
    t.start()
    return True


def generate_expression_image(character_name: str,
                              mood: str, activity: str,
                              equipped_pieces: Optional[Dict[str, str]] = None,
                              equipped_items: Optional[list] = None,
                              equipped_pieces_meta: Optional[Dict[str, Dict[str, Any]]] = None,
                              prompt_prefix: str = "") -> Optional[Path]:
    """Generate an expression/pose variant.

    Character + Equipped-Items + Pose + Expression -> Text-Prompt-basierte
    Bildgenerierung + Faceswap. Kein Outfit-Referenzbild noetig.

    Returns the path to the generated image, or None on failure.
    """
    from app.skills.image_generation_skill import ImageGenerationSkill
    from app.core.dependencies import get_skill_manager
    from app.models.character import (
        get_character_appearance,
        get_character_images_dir,
        postprocess_outfit_image)

    # Equipped-State auflaufen lassen falls nicht mitgegeben
    if equipped_pieces is None or equipped_items is None or equipped_pieces_meta is None:
        try:
            from app.models.inventory import get_equipped_pieces, get_equipped_items
            from app.models.character import get_character_profile as _gcp
            if equipped_pieces is None:
                equipped_pieces = get_equipped_pieces(character_name)
            if equipped_items is None:
                equipped_items = get_equipped_items(character_name)
            if equipped_pieces_meta is None:
                _profile = _gcp(character_name) or {}
                equipped_pieces_meta = _profile.get("equipped_pieces_meta") or {}
        except Exception:
            equipped_pieces = equipped_pieces or {}
            equipped_items = equipped_items or []
            equipped_pieces_meta = equipped_pieces_meta or {}

    def _slot_color(s: str) -> str:
        if not equipped_pieces_meta:
            return ""
        _m = equipped_pieces_meta.get(s) or {}
        if isinstance(_m, dict):
            return (_m.get("color") or "").strip()
        return ""

    # Outfit-Text aus den uebergebenen Pieces/Items (nicht aus dem Profil,
    # damit Overrides wie die Set-Vorschau korrekt funktionieren).
    from app.models.inventory import VALID_PIECE_SLOTS, get_item as _get_item
    # 1) Welche Slots werden verdeckt (covers) bzw. teilverdeckt
    #    (partially_covers)? covers → Slot faellt komplett raus.
    #    partially_covers → Fragment wird "{fragment} unter {covering_fragment}".
    _covered_slots = set()
    _partially_covered = {}  # slot → (covering_frag, covering_slot)
    # Multi-Slot-Pieces nur einmal verarbeiten — vom ersten ihrer Slots
    # in VALID_PIECE_SLOTS-Reihenfolge.
    _seen_items_for_covers = set()
    for _slot in VALID_PIECE_SLOTS:
        _iid = (equipped_pieces or {}).get(_slot)
        if not _iid or _iid in _seen_items_for_covers:
            continue
        _seen_items_for_covers.add(_iid)
        _it = _get_item(_iid)
        if not _it:
            continue
        _op = _it.get("outfit_piece") or {}
        _covering_frag = (_it.get("prompt_fragment") or "").strip()
        _covering_color = _slot_color(_slot)
        if _covering_color and _covering_frag:
            _covering_frag = f"{_covering_color} {_covering_frag}"
        for _c in (_op.get("covers") or []):
            _cs = str(_c).strip().lower()
            if _cs:
                _covered_slots.add(_cs)
        for _c in (_op.get("partially_covers") or []):
            _cs = str(_c).strip().lower()
            if _cs and _covering_frag:
                _partially_covered[_cs] = (_covering_frag, _slot)

    # Slots deren Fragment bereits im "underneath"-Teil eines anderen Slots
    # enthalten ist — muessen nicht nochmal separat aufgefuehrt werden.
    # NUR unterdruecken wenn der Ziel-Slot auch tatsaechlich belegt ist,
    # sonst verschwindet das Covering-Piece komplett.
    _suppressed_slots = set()
    for _target_slot, (_cfrag, _covering_slot) in _partially_covered.items():
        if (equipped_pieces or {}).get(_target_slot):
            _suppressed_slots.add(_covering_slot)

    _piece_fragments = []
    _rendered_items = set()  # Dedup: Multi-Slot-Pieces nur einmal rendern
    for _slot in VALID_PIECE_SLOTS:
        if _slot in _covered_slots:
            continue
        if _slot in _suppressed_slots:
            continue
        _iid = (equipped_pieces or {}).get(_slot)
        if not _iid:
            continue
        if _iid in _rendered_items:
            continue
        _it = _get_item(_iid)
        if not _it:
            continue
        _rendered_items.add(_iid)
        _frag = (_it.get("prompt_fragment") or "").strip()
        if _frag:
            _c_color = _slot_color(_slot)
            if _c_color:
                _frag = f"{_c_color} {_frag}"
            if _slot in _partially_covered:
                _frag = f"{_frag} underneath {_partially_covered[_slot][0]}"
            _piece_fragments.append(_frag)

    _item_fragments = []
    _seen = set()
    for _iid in (equipped_items or []):
        if not _iid or _iid in _seen:
            continue
        _seen.add(_iid)
        _it = _get_item(_iid)
        if not _it:
            continue
        _frag = (_it.get("prompt_fragment") or "").strip()
        if _frag:
            _item_fragments.append(_frag)
    # Kein "wearing: "-Prefix — der Gesamt-Prompt wickelt outfit_desc spaeter
    # mit `{actor_label} is wearing ...` ein; ein zusaetzlicher "wearing: "
    # wuerde doppelt auftauchen.
    _parts = []
    if _piece_fragments:
        _parts.append(", ".join(_piece_fragments))
    if _item_fragments:
        _parts.append(", ".join(_item_fragments))
    outfit_desc = ". ".join(_parts)

    cache_key = _cache_key(mood, activity, character_name, equipped_pieces, equipped_items, equipped_pieces_meta)

    logger.info("Expression-Generierung: %s mood='%s' activity='%s' equipped=%d/%d",
                character_name, mood, activity,
                len(equipped_pieces or {}), len(equipped_items or []))

    # Resolve prompts via PromptBuilder — separiert fuer korrekte Reihenfolge
    from app.core.prompt_builder import PromptBuilder
    expression_prompt = resolve_expression_prompt(mood) if mood else DEFAULT_EXPRESSION
    pose_prompt = resolve_pose_prompt(activity) if activity else DEFAULT_POSE

    # Aktive Conditions (drunk, exhausted, ...) ersetzen den Expression-Prompt.
    # Activity-Pose bleibt unberuehrt.
    try:
        from app.core.danger_system import get_active_condition_image_modifiers
        _cond_mods = get_active_condition_image_modifiers(character_name)
        if _cond_mods:
            expression_prompt = _cond_mods
            logger.info("Expression-Regen: Expression durch Condition ersetzt: %s", _cond_mods)
    except Exception as _cm_err:
        logger.debug("Condition image_modifier Fehler: %s", _cm_err)

    _expr_builder = PromptBuilder(character_name)
    persons = _expr_builder.detect_persons("", character_names=[character_name])
    appearance = persons[0].appearance if persons else get_character_appearance(character_name)
    actor_label = persons[0].actor_label if persons else character_name

    # Prompt-Prefix: nur bei expliziter Vorschau (Garderobe) uebergeben,
    # bei automatischen Expression-Regens bleibt er leer.
    _prompt_prefix = (prompt_prefix or "").strip()

    # Separate Prompts: prefix, character (Appearance), outfit, pose, expression
    character_prompt = f"{actor_label}, {appearance}"
    # Fallback-Prompts fuer unbekleidete Bereiche (per-Character aus Profil).
    # Pruefen: Slot leer UND nicht von einem anderen Piece verdeckt.
    from app.models.character import get_character_profile as _gcp2
    from app.models.character_template import resolve_profile_tokens, get_template
    _prof_full = _gcp2(character_name) or {}
    _tmpl_obj = get_template(_prof_full.get("template", ""))

    def _resolve_tokens(raw: str) -> str:
        if not raw:
            return ""
        try:
            return resolve_profile_tokens(raw, _prof_full, template=_tmpl_obj,
                                           target_key="character_appearance")
        except Exception:
            return raw

    def _resolve_fallback(profile_key: str) -> str:
        return _resolve_tokens((_prof_full.get(profile_key) or "").strip())

    _slot_overrides = _prof_full.get("slot_overrides") or {}
    _fallback_parts = []
    for _slot in VALID_PIECE_SLOTS:
        if (equipped_pieces or {}).get(_slot):
            continue
        if _slot in _covered_slots:
            continue
        _ov = _slot_overrides.get(_slot) or {}
        _ov_prompt = (_ov.get("prompt") or "").strip() if isinstance(_ov, dict) else ""
        if _ov_prompt:
            _fallback_parts.append(_resolve_tokens(_ov_prompt))
            continue
        # Legacy-Felder nur fuer Unterwaesche-Slots
        if _slot == "underwear_top":
            _fb = _resolve_fallback("no_outfit_prompt_top")
            if _fb:
                _fallback_parts.append(_fb)
        elif _slot == "underwear_bottom":
            _fb = _resolve_fallback("no_outfit_prompt_bottom")
            if _fb:
                _fallback_parts.append(_fb)

    _fallback_text = ", ".join(_fallback_parts)

    # "is wearing" nur wenn mindestens ein Piece-Slot belegt ist.
    if outfit_desc:
        if _fallback_text:
            outfit_prompt = f"{_fallback_text}, {actor_label} is wearing {outfit_desc}"
        else:
            outfit_prompt = f"{actor_label} is wearing {outfit_desc}"
    else:
        outfit_prompt = _fallback_text

    # Find ImageGenerationSkill
    image_skill = None
    _sm = get_skill_manager()
    for skill in _sm.skills:
        if isinstance(skill, ImageGenerationSkill):
            image_skill = skill
            break
    if not image_skill:
        logger.warning("ImageGenerationSkill nicht verfuegbar")
        return None

    # Per-Character Override frueh lesen — erlaubt Workflow/Model/LoRA-
    # Overrides pro Character (im Character-Editor konfigurierbar).
    model_override = ""
    loras_override = None
    char_workflow_override = ""
    try:
        from app.models.character import get_character_profile as _gcp
        _prof = _gcp(character_name) or {}
        _char_override = _prof.get("outfit_imagegen") or {}
        if isinstance(_char_override, dict):
            char_workflow_override = (_char_override.get("workflow") or "").strip()
            m = (_char_override.get("model") or "").strip()
            l = _char_override.get("loras")
            if m:
                model_override = m
            if isinstance(l, list):
                loras_override = l
    except Exception as _err:
        logger.debug("Outfit-ImageGen-Override lesen fehlgeschlagen: %s", _err)

    # Workflow/Backend: Char-Override -> ENV-Defaults
    workflow_name = ""
    backend_name = ""
    if char_workflow_override:
        workflow_name = char_workflow_override
    else:
        _expr_default = os.environ.get("EXPRESSION_IMAGEGEN_DEFAULT", "").strip()
        if not _expr_default:
            _expr_default = os.environ.get("OUTFIT_IMAGEGEN_DEFAULT", "").strip()
        _outfit_default = _expr_default
        if _outfit_default.startswith("workflow:"):
            workflow_name = _outfit_default[len("workflow:"):].strip()
        elif _outfit_default.startswith("backend:"):
            backend_name = _outfit_default[len("backend:"):].strip()
        if not workflow_name and not backend_name:
            workflow_name = os.environ.get("COMFY_IMAGEGEN_DEFAULT", "").strip()
        if not workflow_name and not backend_name:
            if image_skill.comfy_workflows:
                workflow_name = image_skill.comfy_workflows[0].name
    if not workflow_name and not backend_name:
        logger.warning("Kein Workflow/Backend fuer Expression-Regen verfuegbar")
        return None
    logger.info("Expression-Regen: %s (char-override=%s)",
                f"workflow={workflow_name}" if workflow_name else f"backend={backend_name}",
                "yes" if char_workflow_override else "no")

    # Pruefen ob Workflow separated prompt hat — davon haengt Payload-Format ab
    _target_wf = None
    _is_separated = False
    if workflow_name:
        _target_wf = next((wf for wf in image_skill.comfy_workflows if wf.name == workflow_name), None)
        _is_separated = _target_wf and _target_wf.has_separated_prompt

    # Validate model_override against current workflow's model_type
    if model_override and _target_wf and _target_wf.model_type:
        _compatible = image_skill.get_cached_checkpoints(_target_wf.model_type)
        if _compatible and model_override not in _compatible:
            logger.warning(
                "model_override '%s' nicht kompatibel mit Workflow '%s' (model_type=%s) — ignoriert",
                model_override, workflow_name, _target_wf.model_type)
            model_override = ""

    # Validate loras_override against cached LoRA list
    if loras_override and image_skill._model_cache_loaded:
        _available_loras = image_skill.get_cached_loras()
        if _available_loras:
            _invalid = [l for l in loras_override
                        if l.get("name") and l["name"] != "None" and l["name"] not in _available_loras]
            if _invalid:
                logger.warning(
                    "LoRA-Override enthaelt inkompatible LoRAs fuer Workflow '%s': %s — ignoriert",
                    workflow_name, [l["name"] for l in _invalid])
                loras_override = None

    # Aufloesung aus Admin-Config (image_generation.outfit_image_width/height)
    # — Expression-Variants nutzen dieselbe Aufloesung wie Garderobe-Outfit-Bilder.
    # Wenn nicht gesetzt, faellt die Generation auf Workflow-/Backend-Default zurueck.
    outfit_w = int(os.environ.get("OUTFIT_IMAGE_WIDTH", 0) or 0) or None
    outfit_h = int(os.environ.get("OUTFIT_IMAGE_HEIGHT", 0) or 0) or None

    # Prompt + Payload abhaengig vom Workflow-Typ
    # Aktuell haben alle Workflows (Qwen, Z-Image, Flux.2) nur input_prompt_positiv,
    # daher ist _is_separated=False. Der Separated-Pfad bleibt fuer zukuenftige Workflows.
    if _is_separated:
        # Separated-Prompt Workflow (zukuenftig): character/pose/expression als einzelne Nodes
        _char_with_outfit = character_prompt
        if outfit_prompt:
            _char_with_outfit += f", {outfit_prompt}"
        full_prompt = ", ".join(p for p in [_prompt_prefix, _char_with_outfit] if p)
        payload = {
            "prompt": full_prompt,
            "input": full_prompt,
            "character_prompt": _char_with_outfit,
            "pose_prompt": pose_prompt,
            "expression_prompt": expression_prompt,
            "agent_name": character_name,
            "user_id": "",
            "set_profile": False,
            "skip_gallery": True,
            "auto_enhance": False,
            "workflow": workflow_name,
            "equipped_pieces_override": equipped_pieces or {},
        }
    else:
        # Single-Prompt Workflow (Z-Image/Flux.2/SDXL):
        # prefix + character + outfit + pose + expression in einem String
        parts = [_prompt_prefix, character_prompt]
        if outfit_prompt:
            parts.append(outfit_prompt)
        parts.append(pose_prompt)
        parts.append(expression_prompt)
        full_prompt = ", ".join(p for p in parts if p)
        payload = {
            "prompt": full_prompt,
            "input": full_prompt,
            "agent_name": character_name,
            "user_id": "",
            "set_profile": False,
            "skip_gallery": True,
            "auto_enhance": False,
            "force_faceswap": True,
            "workflow": workflow_name,
            "backend": backend_name,
            "equipped_pieces_override": equipped_pieces or {},
        }
        logger.info("Expression-Regen: Single-Prompt Modus (kein separated prompt)")

    if outfit_w:
        payload["override_width"] = outfit_w
    if outfit_h:
        payload["override_height"] = outfit_h

    if model_override:
        payload["model_override"] = model_override
    if loras_override is not None:
        payload["loras"] = loras_override

    try:
        img_result = image_skill.execute(json.dumps(payload))

        # Workflow-Fallback bei Timeout: wenn das primaere ComfyUI-Backend
        # nicht verfuegbar ist, pruefe ob der Workflow ein explizites
        # fallback_specific-Backend definiert hat (Admin → Workflow →
        # "Workflow-Fallback (override)") und nutze das. Wenn keins gesetzt,
        # auf Auto-Backend ausweichen (irgendein verfuegbares Cloud-Backend).
        if isinstance(img_result, str) and "Timeout" in img_result and ("Workflow" in img_result or "verfuegbar" in img_result):
            payload_fb = dict(payload)
            payload_fb.pop("workflow", None)
            payload_fb.pop("backend", None)
            # WICHTIG: Beim Wechsel auf ein anderes Backend das model_override
            # und loras entfernen — der ComfyUI-Modellname (z.B.
            # "Flux.2-9B-Q5_K_M.gguf") ist auf Cloud-Backends (Together)
            # nicht gueltig. Cloud-Backend nutzt seinen konfigurierten
            # Default-Modellnamen. LoRAs analog: Together unterstuetzt keine
            # lokalen LoRAs (auch nicht das spezielle FLUX.1-dev-lora-Modell
            # ohne explizit gesetzte huggingface-URLs).
            payload_fb.pop("model_override", None)
            payload_fb.pop("loras", None)
            # Workflow-Override aus image_skill.comfy_workflows lesen
            _wf_fallback = ""
            if workflow_name:
                try:
                    _wf_obj = next((w for w in image_skill.comfy_workflows
                                    if w.name == workflow_name), None)
                    if _wf_obj and getattr(_wf_obj, "fallback_specific", ""):
                        _wf_fallback = _wf_obj.fallback_specific.strip()
                except Exception:
                    pass
            if _wf_fallback:
                payload_fb["backend"] = _wf_fallback
                logger.warning(
                    "Expression-Regen: Workflow '%s' offline — Fallback auf konfiguriertes Backend '%s' (workflow.fallback_specific), model_override+loras zurueckgesetzt",
                    workflow_name, _wf_fallback)
            else:
                logger.warning(
                    "Expression-Regen: Workflow '%s' offline — kein workflow.fallback_specific gesetzt, nutze Auto-Backend",
                    workflow_name or backend_name or "?")
            img_result = image_skill.execute(json.dumps(payload_fb))

        # Extract filename from result
        match = re.search(r'/images/([^?)\n]+)', img_result)
        if not match:
            logger.warning("Konnte Dateiname nicht extrahieren: %s", img_result[:200])
            return None

        image_filename = match.group(1)
        images_dir = get_character_images_dir(character_name)
        src_path = images_dir / image_filename

        if not src_path.exists():
            logger.warning("Generiertes Bild nicht gefunden: %s", src_path)
            return None

        # Post-process (rembg + crop) in temporaerem Pfad,
        # erst danach in Cache verschieben — verhindert dass das Frontend
        # das unverarbeitete Bild per Polling abholt.
        expr_dir = _get_expressions_dir(character_name)
        tmp_path = expr_dir / f".tmp_{cache_key}{src_path.suffix}"
        shutil.move(str(src_path), str(tmp_path))

        try:
            final_tmp = postprocess_outfit_image(tmp_path)
        except Exception as pp_err:
            logger.warning("Post-Processing fehlgeschlagen, nutze Original: %s", pp_err)
            final_tmp = tmp_path

        # Atomar in den Cache-Pfad umbenennen
        final_path = expr_dir / f"{cache_key}{final_tmp.suffix}"
        final_tmp.rename(final_path)
        # Temporaere Datei aufräumen falls anderer Suffix (z.B. .jpg -> .png)
        if tmp_path != final_tmp and tmp_path.exists():
            tmp_path.unlink()

        # Cleanup: alle liegen gebliebenen temp-Dateien entfernen
        _cleanup_stale_temps(expr_dir)

        # Metadaten-JSON neben das Bild speichern. equipped_pieces/items werden
        # hier festgehalten, damit invalidate_variants_for_item gezielt nur die
        # Variants loescht, die das geaenderte Item tatsaechlich enthielten.
        # Thread-lokales Meta lesen — verhindert Kollision wenn parallele
        # Expression-Regens fuer verschiedene Characters laufen.
        _tls = getattr(image_skill, '_meta_tls', None)
        _gen_meta = getattr(_tls, 'last_image_meta', None) if _tls is not None else None
        if _gen_meta is None:
            _gen_meta = getattr(image_skill, 'last_image_meta', {}) or {}
        _expr_meta = {
            "provider": _gen_meta.get("backend_type", ""),
            "service": _gen_meta.get("backend", ""),
            "model": _gen_meta.get("model", ""),
            "loras": _gen_meta.get("loras", []),
            "prompt": full_prompt,
            "negative_prompt": _gen_meta.get("negative_prompt", ""),
            "characters": [character_name],
            "reference_images": _gen_meta.get("reference_images", {}),
            "seed": _gen_meta.get("seed", 0),
            "created_at": _gen_meta.get("created_at", ""),
            "duration_s": _gen_meta.get("duration_s", 0),
            "workflow": _gen_meta.get("workflow", workflow_name),
            "faceswap": _gen_meta.get("faceswap", False),
            "mood": mood,
            "activity": activity,
            "equipped_pieces": equipped_pieces or {},
            "equipped_items": equipped_items or [],
        }
        try:
            _meta_path = final_path.with_suffix(".json")
            _meta_path.write_text(json.dumps(_expr_meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as _me:
            logger.warning("Expression-Meta schreiben fehlgeschlagen: %s", _me)

        logger.info("Expression-Bild generiert: %s", final_path.name)
        return final_path

    except Exception as e:
        logger.error("Expression-Generierung fehlgeschlagen: %s", e)
        return None


def _update_refs_in_json(json_file: Path, rename_map: Dict[str, str],
                         oid_to_safe: Optional[Dict[str, str]] = None) -> None:
    """Update reference_images entries in a JSON file using the rename map.

    Also uses oid_to_safe to fix references to deleted variants not in rename_map.
    """
    # Pattern: {8-hex-outfit-id}_{12-hex-hash}.ext  (old format without char name)
    _old_variant_re = re.compile(r'^([0-9a-f]{8})_[0-9a-f]{12}\.\w+$')
    try:
        data = json.loads(json_file.read_text(encoding="utf-8"))
        refs = data.get("reference_images", {})
        changed = False
        for slot, ref_filename in refs.items():
            if ref_filename in rename_map:
                refs[slot] = rename_map[ref_filename]
                changed = True
            elif oid_to_safe:
                m = _old_variant_re.match(ref_filename)
                if m:
                    oid = m.group(1)
                    safe = oid_to_safe.get(oid)
                    if safe:
                        refs[slot] = f"{safe}_{ref_filename}"
                        changed = True
        if changed:
            json_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8")
    except Exception:
        pass


def migrate_variant_filenames() -> int:
    """Rename old-format variant files ({oid}_{hash}) to new format ({CharName}_{oid}_{hash}).

    Also updates reference_images entries in image JSON metadata.
    Returns number of files renamed.
    """
    from app.models.character import (
        get_character_outfits_dir, get_character_images_dir)
    from app.core.paths import get_storage_dir

    renamed = 0
    sd = get_storage_dir()
    chars_dir = sd / "characters"
    if not chars_dir.exists():
        return 0

    # Phase 1: Rename variant files and build global rename map
    global_rename_map: Dict[str, str] = {}  # old_name -> new_name
    _oid_to_safe: Dict[str, str] = {}  # outfit_id -> safe character name
    for char_dir in chars_dir.iterdir():
        if not char_dir.is_dir():
            continue
        character_name = char_dir.name
        safe = _safe_name(character_name)
        variants_dir = char_dir / "outfits" / "variants"
        if not variants_dir.exists():
            continue

        for f in variants_dir.iterdir():
            if not f.is_file() or f.name.startswith("."):
                continue
            if f.stem.startswith(f"{safe}_"):
                # Already migrated — record old->new mapping for JSON updates
                old_name = f.name[len(safe) + 1:]  # strip "{safe}_" prefix
                global_rename_map[old_name] = f.name
                continue
            new_name = f"{safe}_{f.name}"
            old_path = variants_dir / f.name
            new_path = variants_dir / new_name
            if not new_path.exists():
                old_path.rename(new_path)
                renamed += 1
            global_rename_map[f.name] = new_name

    # Also build outfit_id -> safe_name map from outfit configs
    # so we can fix references to deleted variant files too
    for char_dir in chars_dir.iterdir():
        if not char_dir.is_dir():
            continue
        safe = _safe_name(char_dir.name)
        # Outfits are stored in character_profile.json under "outfits"
        profile_json = char_dir / "character_profile.json"
        if profile_json.exists():
            try:
                profile_data = json.loads(profile_json.read_text(encoding="utf-8"))
                for o in profile_data.get("outfits", []):
                    oid = o.get("id", "")
                    if oid:
                        _oid_to_safe[oid] = safe
            except Exception:
                pass
        # Also derive from existing variant filenames (covers deleted outfits)
        variants_dir = char_dir / "outfits" / "variants"
        if variants_dir.exists():
            for f in variants_dir.iterdir():
                if f.is_file() and f.stem.startswith(f"{safe}_"):
                    # e.g. Zula_18d0d47a_hash -> oid=18d0d47a
                    parts = f.stem[len(safe) + 1:].split("_", 1)
                    if parts:
                        _oid_to_safe[parts[0]] = safe

    if global_rename_map or _oid_to_safe:
        # Phase 2: Update reference_images in ALL characters' image JSONs
        for char_dir in chars_dir.iterdir():
            if not char_dir.is_dir():
                continue
            images_dir = char_dir / "images"
            if not images_dir.exists():
                continue
            for json_file in images_dir.glob("*.json"):
                _update_refs_in_json(json_file, global_rename_map, _oid_to_safe)

        # Phase 3: Update reference_images in Instagram JSONs
        instagram_dir = sd / "instagram"
        if instagram_dir.exists():
            for json_file in instagram_dir.glob("*.json"):
                _update_refs_in_json(json_file, global_rename_map, _oid_to_safe)
            # Auch metadata/ Unterverzeichnis
            meta_dir = instagram_dir / "metadata"
            if meta_dir.exists():
                for json_file in meta_dir.glob("*.json"):
                    _update_refs_in_json(json_file, global_rename_map, _oid_to_safe)

    if renamed:
        logger.info("Variant-Migration: %d Dateien umbenannt (CharName-Prefix)", renamed)
    return renamed
