#!/usr/bin/env python3
"""Migrate outfit_piece schema from {slot, additional_slots} to symmetric {slots: [...]}.

Run once after the code refactor lands, BEFORE re-enabling agent loops.

What it touches:
  1. shared/items/items.json — every outfit_piece item
  2. worlds/*/world.db items.pieces JSON column — every outfit_piece item
  3. worlds/*/world.db character_state.meta.equipped_pieces — re-mirror multi-slot
     pieces and drop orphans (slots that point to an item that no longer
     claims them).

Idempotent: an item that already has `slots` and no `slot` is skipped.
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARED_ITEMS = ROOT / "shared" / "items" / "items.json"
WORLDS_DIR = ROOT / "worlds"

VALID_SLOTS = {
    "underwear_top", "underwear_bottom", "legs",
    "top", "bottom", "outer", "feet", "neck", "head",
}
SLOT_ORDER = ("underwear_top", "underwear_bottom", "legs",
              "top", "bottom", "outer", "feet", "neck", "head")


def transform_outfit_piece(op: dict) -> tuple[bool, list[str]]:
    """Mutates op in-place; returns (changed, warnings)."""
    warnings = []
    if not isinstance(op, dict):
        return False, warnings
    has_new = isinstance(op.get("slots"), list) and op.get("slots")
    has_old = "slot" in op or "additional_slots" in op
    if has_new and not has_old:
        return False, warnings  # already migrated
    new_slots: list[str] = []
    seen = set()
    old_slot = (op.pop("slot", None) or "").strip().lower() if isinstance(op.get("slot"), str) or "slot" in op else ""
    if old_slot and old_slot in VALID_SLOTS:
        new_slots.append(old_slot)
        seen.add(old_slot)
    for s in (op.pop("additional_slots", None) or []):
        if not isinstance(s, str):
            continue
        sl = s.strip().lower()
        if sl and sl in VALID_SLOTS and sl not in seen:
            new_slots.append(sl)
            seen.add(sl)
    if not new_slots and isinstance(op.get("slots"), list):
        for s in op["slots"]:
            if not isinstance(s, str):
                continue
            sl = s.strip().lower()
            if sl and sl in VALID_SLOTS and sl not in seen:
                new_slots.append(sl)
                seen.add(sl)
    if not new_slots:
        warnings.append("piece has no valid slots after migration — KEEP empty")
    op["slots"] = new_slots
    # covers/partially_covers must not include any of the piece's own slots
    for k in ("covers", "partially_covers"):
        if isinstance(op.get(k), list):
            op[k] = [s for s in op[k] if isinstance(s, str) and s not in new_slots]
    return True, warnings


def migrate_shared_items() -> dict:
    summary = {"file": str(SHARED_ITEMS), "items": 0, "migrated": 0, "warnings": []}
    if not SHARED_ITEMS.exists():
        return summary
    data = json.loads(SHARED_ITEMS.read_text(encoding="utf-8"))
    items = data.get("items", [])
    for it in items:
        if it.get("category") != "outfit_piece":
            continue
        summary["items"] += 1
        op = it.get("outfit_piece") or {}
        changed, warns = transform_outfit_piece(op)
        if changed:
            it["outfit_piece"] = op
            summary["migrated"] += 1
            for w in warns:
                summary["warnings"].append(f"{it.get('id')} ({it.get('name')}): {w}")
    if summary["migrated"]:
        SHARED_ITEMS.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    return summary


def migrate_world_items(db_path: Path) -> dict:
    summary = {"db": str(db_path), "items": 0, "migrated": 0, "warnings": []}
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT id, name, pieces FROM items WHERE category='outfit_piece'"
        ).fetchall()
        for iid, name, raw in rows:
            summary["items"] += 1
            try:
                op = json.loads(raw or "{}")
            except json.JSONDecodeError:
                summary["warnings"].append(f"{iid} ({name}): invalid pieces JSON, skip")
                continue
            changed, warns = transform_outfit_piece(op)
            if changed:
                con.execute(
                    "UPDATE items SET pieces=?, updated_at=datetime('now') WHERE id=?",
                    (json.dumps(op, ensure_ascii=False), iid))
                summary["migrated"] += 1
                for w in warns:
                    summary["warnings"].append(f"{iid} ({name}): {w}")
        con.commit()
    finally:
        con.close()
    return summary


def cleanup_equipped(db_path: Path) -> dict:
    """Re-mirror multi-slot pieces in every character's equipped_pieces."""
    summary = {"db": str(db_path), "characters": 0, "fixed": 0, "details": []}
    con = sqlite3.connect(db_path)
    try:
        # Build item -> slots map. Shared library zuerst, World ueberschreibt
        # gleichnamige Eintraege (sollte selten sein).
        item_slots: dict[str, list[str]] = {}
        if SHARED_ITEMS.exists():
            try:
                shared = json.loads(SHARED_ITEMS.read_text(encoding="utf-8"))
                for it in shared.get("items", []) or []:
                    if it.get("category") != "outfit_piece":
                        continue
                    op = it.get("outfit_piece") or {}
                    slots = op.get("slots") or []
                    if isinstance(slots, list):
                        item_slots[it.get("id", "")] = [
                            s for s in slots if isinstance(s, str) and s in VALID_SLOTS]
            except Exception:
                pass
        for iid, raw in con.execute(
            "SELECT id, pieces FROM items WHERE category='outfit_piece'"
        ).fetchall():
            try:
                op = json.loads(raw or "{}")
            except Exception:
                continue
            slots = op.get("slots") or []
            if isinstance(slots, list):
                item_slots[iid] = [s for s in slots if isinstance(s, str) and s in VALID_SLOTS]

        rows = con.execute(
            "SELECT character_name, meta FROM character_state"
        ).fetchall()
        for char_name, raw_meta in rows:
            summary["characters"] += 1
            try:
                meta = json.loads(raw_meta or "{}")
            except json.JSONDecodeError:
                continue
            eq = meta.get("equipped_pieces") or {}
            if not isinstance(eq, dict) or not eq:
                continue

            new_eq: dict[str, str] = {}
            note: list[str] = []
            # Reihenfolge der Konflikt-Aufloesung:
            #   Pass 1 — Multi-Slot-Pieces (size >= 2) zuerst, in SLOT_ORDER.
            #   Pass 2 — Single-Slot-Pieces, in SLOT_ORDER.
            # Damit verlieren Single-Slot-Pieces gegen Multi-Slot in ueberlappenden
            # Slots (sonst wuerde der naechste Slot-Tick einen Single-Slot-Piece
            # einen Mirror-Slot eines Multi-Slot-Pieces "stehlen" und das Multi-
            # Slot-Piece bricht auseinander).
            seen_pieces: set[str] = set()
            locked: dict[str, str] = {}  # slot -> piece_id

            def _process_pass(min_slots: int):
                for s in SLOT_ORDER:
                    iid = eq.get(s)
                    if not iid or iid in seen_pieces:
                        continue
                    slots_for_item = item_slots.get(iid)
                    if slots_for_item is None:
                        # Unbekanntes Item — wie urspruenglich belassen
                        new_eq[s] = iid
                        seen_pieces.add(iid)
                        note.append(f"{iid} unknown (kept in {s})")
                        continue
                    if not slots_for_item:
                        note.append(f"{iid} has no slots (dropped from {s})")
                        seen_pieces.add(iid)
                        continue
                    if len(slots_for_item) < min_slots:
                        continue  # Pass 2 nimmt es
                    if s not in slots_for_item:
                        note.append(f"{iid} doesn't claim slot {s} (orphan dropped)")
                        seen_pieces.add(iid)
                        continue
                    blocked = next(
                        (ts for ts in slots_for_item
                         if ts in locked and locked[ts] != iid),
                        None)
                    if blocked is not None:
                        note.append(f"{iid} (slots={slots_for_item}) blocked at {blocked} by {locked[blocked]} — dropped")
                        seen_pieces.add(iid)
                        continue
                    for ts in slots_for_item:
                        new_eq[ts] = iid
                        locked[ts] = iid
                    seen_pieces.add(iid)

            _process_pass(min_slots=2)  # Multi-Slot zuerst
            _process_pass(min_slots=1)  # dann Single-Slot

            # Items in eq that didn't get a chance because none of their slots
            # appear in eq's keys — already covered by the loop above. Items
            # in eq that have ALL their slots already taken by earlier items —
            # they get dropped, which is the intended outcome.
            if new_eq != eq:
                meta["equipped_pieces"] = new_eq
                # equipped_pieces_meta cleanup: drop entries for slots that
                # don't appear in new_eq (color belongs to slot+item).
                pmeta = meta.get("equipped_pieces_meta") or {}
                if isinstance(pmeta, dict):
                    for k in list(pmeta.keys()):
                        if k not in new_eq:
                            pmeta.pop(k, None)
                    meta["equipped_pieces_meta"] = pmeta
                con.execute(
                    "UPDATE character_state SET meta=? WHERE character_name=?",
                    (json.dumps(meta, ensure_ascii=False), char_name))
                summary["fixed"] += 1
                summary["details"].append({"character": char_name, "before": eq, "after": new_eq, "notes": note})
        con.commit()
    finally:
        con.close()
    return summary


def main():
    print("=" * 60)
    print("Outfit-Slot-Schema Migration")
    print("=" * 60)

    print("\n[1] Shared items library:")
    s = migrate_shared_items()
    print(f"  {s['file']}")
    print(f"  outfit_piece items: {s['items']}, migrated: {s['migrated']}")
    for w in s["warnings"]:
        print(f"  WARN: {w}")

    db_paths = sorted(WORLDS_DIR.glob("*/world.db"))
    print(f"\n[2] World DBs ({len(db_paths)}):")
    total_fixed = 0
    for db in db_paths:
        s = migrate_world_items(db)
        print(f"\n  {db}")
        print(f"    items migrated: {s['migrated']}/{s['items']}")
        for w in s["warnings"]:
            print(f"    WARN: {w}")

        s2 = cleanup_equipped(db)
        print(f"    characters with equipped_pieces fixed: {s2['fixed']}/{s2['characters']}")
        for d in s2["details"]:
            print(f"      [{d['character']}]")
            for n in d["notes"]:
                print(f"        - {n}")
        total_fixed += s2["fixed"]

    print("\n" + "=" * 60)
    print(f"Done. {total_fixed} character equipped-states fixed.")
    print("=" * 60)


if __name__ == "__main__":
    main()
