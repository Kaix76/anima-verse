"""Soul-File-Layout — Section-Namen + Pfade unter ``characters/<X>/soul/``.

Single source of truth fuer das Soul-Editor-UI (Game-Admin/World-Dev) und
die per-section Editierbarkeits-Whitelist.

Default-Schreibrechte:
    EDITABLE_SECTIONS — Sections die User/Tools standardmaessig
        bearbeiten duerfen.
    LOCKED_SECTIONS   — Sections die nur User/Admin schreiben darf.
        Einzelne Sections in einer locked-Datei koennen ueber den
        ``<!-- EDITABLE -->``-Marker direkt nach ihrem ``## Heading``
        einzeln freigegeben werden.
"""
EDITABLE_SECTIONS = {"beliefs", "lessons", "goals"}
LOCKED_SECTIONS = {"personality", "tasks", "presence", "roleplay_rules", "soul"}

SECTION_FILE_MAP = {
    "beliefs":        "soul/beliefs.md",
    "lessons":        "soul/lessons.md",
    "goals":          "soul/goals.md",
    "personality":    "soul/personality.md",
    "tasks":          "soul/tasks.md",
    "presence":       "soul/presence.md",
    "roleplay_rules": "soul/roleplay_rules.md",
    "soul":           "soul/soul.md",
}

ALL_SECTIONS = EDITABLE_SECTIONS | LOCKED_SECTIONS

EDITABLE_MARKER = "<!-- EDITABLE -->"
