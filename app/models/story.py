"""Story System - Markdown story parsing, tag filtering, and state management."""
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from app.core.log import get_logger
from app.core.db import get_connection, transaction

logger = get_logger("story_model")

from app.core.paths import get_storage_dir as _get_storage_dir


def list_stories() -> List[Dict[str, Any]]:
    """Scannt storage/stories/*.md und gibt Metadaten (nur Frontmatter) zurueck."""
    stories = []
    if not (_get_storage_dir() / "stories").exists():
        return stories

    for path in sorted((_get_storage_dir() / "stories").glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
            meta = _parse_frontmatter(content)
            if meta:
                meta["filename"] = path.name
                stories.append(meta)
        except Exception as e:
            logger.error("Fehler beim Lesen von %s: %s", path.name, e)

    return stories


def get_story(filename: str) -> Optional[Dict[str, Any]]:
    """Parst eine komplette Story-Datei.

    Returns:
        {
            "meta": {title, description, tags, setting, participants, options_format, language},
            "sections": {"start": {"prompt": "...", "options_map": {"A": "kaffee", ...}}, ...},
            "section_order": ["start", "kaffee", ...]
        }
    """
    path = (_get_storage_dir() / "stories") / filename
    if not path.exists():
        return None

    try:
        content = path.read_text(encoding="utf-8")
        return parse_story_file(content)
    except Exception as e:
        logger.error("Fehler beim Parsen von %s: %s", filename, e)
        return None


def parse_story_file(content: str) -> Dict[str, Any]:
    """Splittet YAML-Frontmatter und Markdown-Sections.

    Unterstuetzt zwei Formate:
    - Neu: Mermaid-Flowchart fuer Verzweigung + H2-Sections fuer Prompts
    - Alt: H1-Sections mit HTML-Kommentar-Options (Fallback)
    """
    meta = _parse_frontmatter(content)
    if not meta:
        meta = {}

    body = _strip_frontmatter(content)

    # Mermaid-Block erkennen
    mermaid_match = re.search(
        r"```mermaid\s*\n(.*?)```", body, re.DOTALL
    )

    if mermaid_match:
        return _parse_mermaid_story(meta, body, mermaid_match)
    else:
        return _parse_legacy_story(meta, body)


def _parse_legacy_story(
    meta: Dict[str, Any], body: str
) -> Dict[str, Any]:
    """Altes Format: H1-Sections mit HTML-Kommentar-Options."""
    sections = {}
    section_order = []
    current_id = None
    current_lines = []

    for line in body.split("\n"):
        heading_match = re.match(r"^#\s+(.+)$", line)
        if heading_match:
            if current_id is not None:
                sections[current_id] = _parse_section("\n".join(current_lines))
                section_order.append(current_id)
            current_id = heading_match.group(1).strip().lower().replace(" ", "-")
            current_lines = []
        else:
            current_lines.append(line)

    if current_id is not None:
        sections[current_id] = _parse_section("\n".join(current_lines))
        section_order.append(current_id)

    return {
        "meta": meta,
        "sections": sections,
        "section_order": section_order,
    }


def _parse_mermaid_story(
    meta: Dict[str, Any], body: str, mermaid_match: re.Match
) -> Dict[str, Any]:
    """Neues Format: Mermaid-Flowchart + H2-Sections."""
    graph = _parse_mermaid_graph(mermaid_match.group(1))

    # H2-Sections parsen (## heading)
    prompt_blocks = {}
    section_order = []
    current_id = None
    current_lines = []

    # Body nach dem Mermaid-Block
    after_mermaid = body[mermaid_match.end():]

    for line in after_mermaid.split("\n"):
        heading_match = re.match(r"^##\s+(.+)$", line)
        if heading_match:
            if current_id is not None:
                prompt_blocks[current_id] = "\n".join(current_lines).strip()
                section_order.append(current_id)
            current_id = heading_match.group(1).strip().lower().replace(" ", "-")
            current_lines = []
        else:
            current_lines.append(line)

    if current_id is not None:
        prompt_blocks[current_id] = "\n".join(current_lines).strip()
        section_order.append(current_id)

    # Sections aus Graph + Prompts zusammenbauen
    sections = _build_sections_from_mermaid(graph, prompt_blocks)

    return {
        "meta": meta,
        "sections": sections,
        "section_order": section_order,
    }


def filter_stories_for_character(
    stories: List[Dict[str, Any]], profile: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Filtert Stories basierend auf Tag-Matching mit dem Character-Profil."""
    result = []
    for story in stories:
        tags = story.get("tags", {})
        if not tags:
            # Keine Tags = passt zu jedem Character
            result.append(story)
            continue

        if _matches_tags(tags, profile):
            result.append(story)

    return result


def parse_options_from_response(response: str) -> List[Dict[str, str]]:
    """Parst Option-Muster aus LLM-Antworten.

    Unterstuetzte Formate:
        **Option A:** text
        **Option A**: text
        **Option A:**: text
        Option A: text

    Returns:
        [{"letter": "A", "text": "Zuerst Kaffee holen"}, ...]
    """
    options = []
    # Primaer: **Option X:** text (mit oder ohne [] um Text)
    for match in re.finditer(
        r"\*\*Option\s+([A-Z]):?\*\*:?\s*\[?([^\]\n]+)\]?", response
    ):
        options.append({
            "letter": match.group(1),
            "text": match.group(2).strip(),
        })
    # Fallback: Option X: text (ohne Bold)
    if not options:
        for match in re.finditer(
            r"(?:^|\n)\s*Option\s+([A-Z]):\s*(.+)", response
        ):
            options.append({
                "letter": match.group(1),
                "text": match.group(2).strip(),
            })
    return options


def _story_state_id(character_name: str, filename: str) -> str:
    """Erzeugt eine eindeutige ID fuer einen Story-State in der DB."""
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", filename.replace(".md", ""))
    return f"{character_name}::{safe}"


def get_story_state(character_name: str, filename: str
) -> Optional[Dict[str, Any]]:
    """Laedt den gespeicherten Story-State — DB-first, JSON-Fallback."""
    story_id = _story_state_id(character_name, filename)
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT meta FROM stories WHERE id=? AND character_name=?",
            (story_id, character_name),
        ).fetchone()
        if row:
            meta = {}
            try:
                meta = json.loads(row[0] or "{}")
            except Exception:
                pass
            state = meta.get("state")
            if state is not None:
                return state
    except Exception as e:
        logger.debug("get_story_state DB error: %s — falling back to JSON", e)

    # Fallback: JSON-Datei
    state_path = _get_state_path(character_name, filename)
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def save_story_state(character_name: str, filename: str, state: Dict[str, Any]
) -> None:
    """Speichert den Story-State in die DB."""
    story_id = _story_state_id(character_name, filename)
    now = datetime.now().isoformat()
    title = re.sub(r"[^a-zA-Z0-9_-]", "_", filename.replace(".md", ""))
    meta_blob = json.dumps({"state": state}, ensure_ascii=False)
    try:
        with transaction() as conn:
            conn.execute(
                """INSERT INTO stories (id, title, content, character_name, meta, created_at, updated_at)
                   VALUES (?, ?, '', ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       meta=excluded.meta,
                       updated_at=excluded.updated_at""",
                (story_id, title, character_name, meta_blob, now, now),
            )
    except Exception as e:
        logger.error("save_story_state DB error: %s", e)


def delete_story_state(character_name: str, filename: str
) -> bool:
    """Loescht den Story-State (Neustart) — aus DB und JSON-Datei."""
    story_id = _story_state_id(character_name, filename)
    deleted_db = False
    try:
        with transaction() as conn:
            cur = conn.execute(
                "DELETE FROM stories WHERE id=? AND character_name=?",
                (story_id, character_name),
            )
            deleted_db = cur.rowcount > 0
    except Exception as e:
        logger.error("delete_story_state DB error: %s", e)

    # Also remove JSON file
    state_path = _get_state_path(character_name, filename)
    deleted_file = False
    if state_path.exists():
        state_path.unlink()
        deleted_file = True

    return deleted_db or deleted_file


# ---------------------------------------------------------------------------
# Mermaid graph parsing
# ---------------------------------------------------------------------------

def _parse_mermaid_graph(mermaid_text: str) -> Dict[str, Any]:
    """Parst einen Mermaid-Flowchart in eine Graph-Struktur.

    Erkennt:
    - Edges: nodeA -->|"label"| nodeB  oder  nodeA --> nodeB
    - Node-Labels: nodeId["Label"] oder nodeId((Label))

    Returns:
        {
            "nodes": {"start": "Ankunft im Buero", ...},
            "edges": {"start": [{"to": "kaffee", "label": "Kaffee holen"}, ...], ...}
        }
    """
    nodes = {}
    edges = {}

    for line in mermaid_text.split("\n"):
        line = line.strip()
        if not line or line.startswith("flowchart") or line.startswith("graph"):
            continue

        # Edge mit Label: nodeA -->|"label"| nodeB  oder  nodeA --> |label| nodeB
        edge_match = re.match(
            r'(\S+)\s*-->\s*\|"?([^"|]+)"?\|\s*(\S+)', line
        )
        if edge_match:
            from_id = _clean_node_id(edge_match.group(1))
            label = edge_match.group(2).strip()
            to_raw = edge_match.group(3)
            to_id = _clean_node_id(to_raw)

            # Node-Labels aus to-Teil extrahieren
            _extract_node_label(to_raw, nodes)

            edges.setdefault(from_id, []).append({"to": to_id, "label": label})
            continue

        # Edge ohne Label: nodeA --> nodeB
        edge_no_label = re.match(r'(\S+)\s*-->\s*(\S+)', line)
        if edge_no_label:
            from_id = _clean_node_id(edge_no_label.group(1))
            to_raw = edge_no_label.group(2)
            to_id = _clean_node_id(to_raw)

            _extract_node_label(to_raw, nodes)

            edges.setdefault(from_id, []).append({"to": to_id, "label": ""})
            continue

        # Standalone Node-Definition: nodeId["Label"] oder nodeId((Label))
        node_match = re.match(r'(\w[\w-]*)\s*[\["(]', line)
        if node_match:
            _extract_node_label(line.strip(), nodes)

    return {"nodes": nodes, "edges": edges}


def _clean_node_id(raw: str) -> str:
    """Extrahiert die reine Node-ID aus z.B. 'kaffee[\"Kaffeekueche\"]'."""
    m = re.match(r'(\w[\w-]*)', raw)
    return m.group(1) if m else raw


def _extract_node_label(raw: str, nodes: Dict[str, str]):
    """Extrahiert Node-Label aus 'nodeId[\"Label\"]' oder 'nodeId((Label))'."""
    # nodeId["Label"] oder nodeId["Label"]
    m = re.match(r'(\w[\w-]*)\s*\[\s*"([^"]+)"\s*\]', raw)
    if m:
        nodes[m.group(1)] = m.group(2)
        return

    # nodeId((Label))
    m = re.match(r'(\w[\w-]*)\s*\(\(\s*([^)]+)\s*\)\)', raw)
    if m:
        nodes[m.group(1)] = m.group(2)


def _build_sections_from_mermaid(
    graph: Dict[str, Any], prompt_blocks: Dict[str, str]
) -> Dict[str, Dict[str, Any]]:
    """Baut Section-Dicts aus Graph-Edges und Prompt-Blocks.

    Jede Section bekommt:
    - prompt: Text aus dem H2-Block
    - options_map: {"A": "target_section", ...} aus Graph-Edges
    - options_labels: {"A": "Kaffee holen", ...} aus Edge-Labels
    """
    edges = graph.get("edges", {})
    sections = {}

    # Alle Nodes sammeln die als Source oder Target vorkommen
    all_node_ids = set(prompt_blocks.keys())
    for from_id, edge_list in edges.items():
        all_node_ids.add(from_id)
        for e in edge_list:
            all_node_ids.add(e["to"])

    for node_id in all_node_ids:
        if node_id == "_end":
            continue

        prompt = prompt_blocks.get(node_id, "")
        node_edges = edges.get(node_id, [])

        options_map = {}
        options_labels = {}
        for i, edge in enumerate(node_edges):
            letter = chr(ord("A") + i)
            options_map[letter] = edge["to"]
            if edge.get("label"):
                options_labels[letter] = edge["label"]

        section = {
            "prompt": prompt,
            "options_map": options_map,
        }
        if options_labels:
            section["options_labels"] = options_labels

        sections[node_id] = section

    return sections


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_state_path(character_name: str, filename: str) -> Path:
    """Pfad zur State-Datei: storage/users/{user}/characters/{char}/stories/{file}.state.json"""
    from app.models.character import get_character_dir

    stories_dir = get_character_dir(character_name) / "stories"
    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", filename.replace(".md", ""))
    return stories_dir / f"{safe_name}.state.json"


def _parse_frontmatter(content: str) -> Optional[Dict[str, Any]]:
    """Extrahiert YAML-Frontmatter aus Markdown-Content."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return None
    try:
        return yaml.safe_load(match.group(1))
    except Exception as e:
        logger.error("YAML-Fehler: %s", e)
        return None


def _strip_frontmatter(content: str) -> str:
    """Entfernt den YAML-Frontmatter und gibt den Body zurueck."""
    match = re.match(r"^---\s*\n.*?\n---\s*\n", content, re.DOTALL)
    if match:
        return content[match.end():]
    return content


def _parse_section(text: str) -> Dict[str, Any]:
    """Parst eine einzelne Section: Prompt-Text und Options-Mapping.

    Extrahiert <!-- options: A: section_id --> Bloecke.
    """
    # Options-Block extrahieren
    options_map = {}
    options_match = re.search(
        r"<!--\s*options:\s*\n(.*?)-->", text, re.DOTALL
    )
    if options_match:
        options_text = options_match.group(1)
        for line in options_text.strip().split("\n"):
            line = line.strip()
            opt_match = re.match(r"([A-Z]):\s*(.+)", line)
            if opt_match:
                options_map[opt_match.group(1)] = opt_match.group(2).strip()

    # Prompt-Text: alles ausser dem Options-Block
    prompt = text
    if options_match:
        prompt = text[:options_match.start()]
    prompt = prompt.strip()

    return {
        "prompt": prompt,
        "options_map": options_map,
    }


def _matches_tags(tags: Dict[str, Any], profile: Dict[str, Any]) -> bool:
    """Prueft ob ein Character-Profil alle Story-Tags erfuellt."""
    # Gender-Check
    gender_tags = tags.get("gender")
    if gender_tags:
        if isinstance(gender_tags, str):
            gender_tags = [gender_tags]
        char_gender = profile.get("gender", "")
        if char_gender not in gender_tags:
            return False

    # Template-Check
    template_tags = tags.get("template")
    if template_tags:
        if isinstance(template_tags, str):
            template_tags = [template_tags]
        char_template = profile.get("template", "")
        if char_template not in template_tags:
            return False

    # Min-Age Check
    min_age = tags.get("min_age")
    if min_age is not None:
        age = profile.get("age")
        if age is None:
            return False
        try:
            if int(age) < int(min_age):
                return False
        except (ValueError, TypeError):
            return False

    return True


