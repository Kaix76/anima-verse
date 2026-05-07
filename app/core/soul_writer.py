"""Soul-File Writer — appends entries to categorized sections of editable
soul .md files (``beliefs.md``, ``lessons.md``, ``goals.md``).

Used by Retrospect (and any future skill that updates these files). Knows
the section structure of each file and how to find the right ``##``
sub-heading in either German or English. New ``##`` headings, when a file
or category is empty, are written in the character's language.
"""
from pathlib import Path
from typing import Dict, List, Tuple

from app.core.log import get_logger

logger = get_logger("soul_writer")


SOUL_FILE_SCHEMA: Dict[str, Dict] = {
    "beliefs": {
        "filename": "beliefs.md",
        "top_de": "Ueberzeugungen",
        "top_en": "Beliefs",
        "categories": [
            ("about_self",   "Ueber mich",     "About myself"),
            ("about_others", "Ueber andere",   "About others"),
            ("about_world",  "Ueber die Welt", "About the world"),
        ],
    },
    "lessons": {
        "filename": "lessons.md",
        "top_de": "Gelernte Lektionen",
        "top_en": "Lessons learned",
        "categories": [
            ("from_people",     "Aus Erfahrungen mit Menschen", "From experiences with people"),
            ("from_situations", "Aus Situationen",              "From situations"),
        ],
    },
    "goals": {
        "filename": "goals.md",
        "top_de": "Persoenliche Ziele",
        "top_en": "Personal Goals",
        "categories": [
            ("short_term", "Kurzfristig",   "Short-term"),
            ("mid_term",   "Mittelfristig", "Mid-term"),
            ("long_term",  "Langfristig",   "Long-term"),
        ],
    },
}


def get_soul_file_path(character_name: str, file_id: str) -> Path:
    from app.models.character import get_character_dir
    return get_character_dir(character_name) / "soul" / SOUL_FILE_SCHEMA[file_id]["filename"]


def list_categories(file_id: str) -> List[str]:
    return [cid for cid, _, _ in SOUL_FILE_SCHEMA[file_id]["categories"]]


def _category_headings(file_id: str, category_id: str) -> Tuple[str, str]:
    for cid, de, en in SOUL_FILE_SCHEMA[file_id]["categories"]:
        if cid == category_id:
            return de, en
    raise KeyError(f"Unknown category: {file_id}/{category_id}")


def _scaffold(file_id: str, language: str) -> str:
    schema = SOUL_FILE_SCHEMA[file_id]
    title = schema["top_de"] if language == "de" else schema["top_en"]
    parts = [f"# {title}", ""]
    for _, de, en in schema["categories"]:
        head = de if language == "de" else en
        parts.append(f"## {head}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def append_entry(character_name: str, file_id: str, category_id: str,
                 line: str, language: str = "en") -> bool:
    """Append a bullet line under the right ``##`` subsection.

    - Empty/missing file: scaffold with all categories first.
    - Subsection heading missing: append a new ``##`` heading at end.
    - ``language`` ('de'/'en') decides which heading variant to write when
      a heading must be created.

    Returns True if the line was written (always; de-dup is the caller's job).
    """
    if file_id not in SOUL_FILE_SCHEMA:
        raise KeyError(f"Unknown soul file: {file_id}")
    de_head, en_head = _category_headings(file_id, category_id)
    candidates = {de_head.lower(), en_head.lower()}
    fallback_head = de_head if language == "de" else en_head

    path = get_soul_file_path(character_name, file_id)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if not text.strip():
        text = _scaffold(file_id, language)

    bullet = line if line.lstrip().startswith("- ") else f"- {line}"
    new_text = _insert_under_heading(text, candidates, bullet, fallback_head)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    return True


def _insert_under_heading(text: str, candidate_lower: set,
                          new_line: str, fallback_head: str) -> str:
    """Insert ``new_line`` just before the next ``##``/``#`` heading after a
    matching ``##`` heading, or append a new ``## fallback_head`` block at EOF
    if no match. Returns the new text (always ends with ``\n``)."""
    lines = text.splitlines()
    target = -1
    for i, l in enumerate(lines):
        if l.startswith("## ") and l[3:].strip().lower() in candidate_lower:
            target = i
            break
    if target < 0:
        out = list(lines)
        if out and out[-1].strip():
            out.append("")
        out.append(f"## {fallback_head}")
        out.append(new_line)
        return "\n".join(out) + "\n"

    insert_at = len(lines)
    for j in range(target + 1, len(lines)):
        if lines[j].startswith("# ") or lines[j].startswith("## "):
            insert_at = j
            break
    while insert_at - 1 > target and lines[insert_at - 1].strip() == "":
        insert_at -= 1
    out = lines[:insert_at] + [new_line] + lines[insert_at:]
    return "\n".join(out) + "\n"


def load_section_lines(character_name: str, file_id: str,
                       category_id: str) -> List[str]:
    """All non-empty body lines under matching ``##`` heading."""
    path = get_soul_file_path(character_name, file_id)
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    de, en = _category_headings(file_id, category_id)
    candidates = {de.lower(), en.lower()}
    lines = text.splitlines()
    target = -1
    for i, l in enumerate(lines):
        if l.startswith("## ") and l[3:].strip().lower() in candidates:
            target = i
            break
    if target < 0:
        return []
    out: List[str] = []
    for j in range(target + 1, len(lines)):
        if lines[j].startswith("# ") or lines[j].startswith("## "):
            break
        s = lines[j].strip()
        if s:
            out.append(s)
    return out


def load_all_body_lines(character_name: str, file_id: str,
                        limit: int = 20) -> List[str]:
    """Last ``limit`` non-heading body lines across the whole file."""
    path = get_soul_file_path(character_name, file_id)
    if not path.exists():
        return []
    out: List[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out[-limit:]
