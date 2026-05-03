#!/usr/bin/env python3
"""Enable Retrospect skill for every existing character in every world.

Retrospect uses ALWAYS_LOAD=True (opt-in per character via
``<char>/skills/retrospect.json``). For characters that existed before the
skill was added, that config file is missing — so the skill stays hidden
from the LLM and never runs.

This migration writes ``{"enabled": true}`` for every character that
doesn't already have a retrospect config. Idempotent: existing files are
not overwritten (so an explicit ``{"enabled": false}`` survives).

Usage:
    python migrations/2026-05-retrospect-enable.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORLDS_DIR = ROOT / "worlds"


def main() -> int:
    if not WORLDS_DIR.exists():
        print(f"!! worlds dir missing: {WORLDS_DIR}", file=sys.stderr)
        return 1

    total_worlds = 0
    total_chars = 0
    total_created = 0
    total_skipped = 0

    for world_dir in sorted(WORLDS_DIR.iterdir()):
        if not world_dir.is_dir():
            continue
        chars_dir = world_dir / "characters"
        if not chars_dir.is_dir():
            continue
        total_worlds += 1
        print(f"\n=== {world_dir.name} ===")

        for char_dir in sorted(chars_dir.iterdir()):
            if not char_dir.is_dir():
                continue
            total_chars += 1
            skills_dir = char_dir / "skills"
            skills_dir.mkdir(parents=True, exist_ok=True)
            cfg_path = skills_dir / "retrospect.json"
            if cfg_path.exists():
                total_skipped += 1
                print(f"  skip {char_dir.name} (config exists)")
                continue
            cfg_path.write_text(
                json.dumps({"enabled": True}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            total_created += 1
            print(f"  create {char_dir.name}/skills/retrospect.json")

    print(f"\nWorlds:    {total_worlds}")
    print(f"Chars:     {total_chars}")
    print(f"Created:   {total_created}")
    print(f"Skipped:   {total_skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
