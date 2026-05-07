"""Jinja2-based prompt template loader.

Templates live under `shared/templates/llm/`:

    tasks/<task>.md       — one file per llm_call() task; split into
                            `## system` and `## user` sections via YAML
                            frontmatter + body markers.
    sections/<name>.md    — reusable building blocks for the system
                            prompt builder (identity, situation, ...).
    chat/<scenario>.md    — top-level chat/thought composites that
                            include sections.
    skills/<skill>.md     — name + description metadata per skill;
                            frontmatter `name`, body = description.

Public API:
    render_task(task, **vars)          -> (system_prompt, user_prompt)
    render(template_path, **vars)      -> str
    load_skill_meta(skill_file)        -> {"name": str, "description": str}

The loader is intentionally minimal: Jinja2 with autoescape disabled
(prompts are plain text, not HTML), `StrictUndefined` (missing
placeholders raise loud errors instead of silently rendering empty),
and `trim_blocks`/`lstrip_blocks` so that `{% if %}` blocks don't leak
extra whitespace.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Tuple

from jinja2 import Environment, FileSystemLoader, StrictUndefined

# Resolve template dir relative to repo root: <repo>/shared/templates/llm
_TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "shared" / "templates" / "llm"

_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=False,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=False,
)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SECTION_SPLIT_RE = re.compile(r"^##\s+(system|user)\s*$", re.MULTILINE | re.IGNORECASE)


def _strip_frontmatter(text: str) -> str:
    """Drop YAML frontmatter if present. Frontmatter is documentation
    (purpose, placeholders) that must not be sent to the LLM."""
    m = _FRONTMATTER_RE.match(text)
    if m:
        return text[m.end():]
    return text


def _split_system_user(body: str) -> Tuple[str, str]:
    """Split a task body into (system, user) chunks at `## system` / `## user`
    markers. Either section may be empty."""
    parts = _SECTION_SPLIT_RE.split(body)
    # parts = [pre, "system", system_body, "user", user_body, ...]
    # If the first marker is the very start, parts[0] is "".
    if len(parts) < 3:
        # No section markers — treat whole body as user prompt.
        return "", body.strip()

    system = ""
    user = ""
    # Walk pairs of (label, body)
    for i in range(1, len(parts) - 1, 2):
        label = parts[i].lower()
        chunk = parts[i + 1].strip()
        if label == "system":
            system = chunk
        elif label == "user":
            user = chunk

    return system, user


def render_task(task: str, **vars) -> Tuple[str, str]:
    """Render `tasks/<task>.md` and return (system_prompt, user_prompt).

    Raises if the template is missing or a placeholder is undefined.
    """
    template_name = f"tasks/{task}.md"
    raw = _env.loader.get_source(_env, template_name)[0]
    body = _strip_frontmatter(raw)
    # Render the body (after frontmatter strip) so `{% include %}` etc. still
    # works. We render via from_string to avoid double frontmatter handling.
    rendered = _env.from_string(body).render(**vars)
    return _split_system_user(rendered)


def render(template_path: str, **vars) -> str:
    """Render any single template file (sections/, chat/, ...) and return
    the result as a plain string."""
    raw = _env.loader.get_source(_env, template_path)[0]
    body = _strip_frontmatter(raw)
    return _env.from_string(body).render(**vars).strip()


def template_exists(template_path: str) -> bool:
    try:
        _env.loader.get_source(_env, template_path)
        return True
    except Exception:
        return False


_SKILL_META_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_FRONT_KEY_RE = re.compile(r"^\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(.+?)\s*$", re.MULTILINE)

_skill_meta_cache: Dict[str, Dict[str, str]] = {}


def load_skill_meta(skill_file: str) -> Dict[str, str]:
    """Load name + description for a skill from
    ``shared/templates/llm/skills/<skill_file>.md``.

    File format:
        ---
        name: SendMessage
        ---
        Full description text (rest of the file).

    Returns ``{"name": "...", "description": "..."}``. Description is
    stripped of leading/trailing whitespace so the template can use
    multi-line bodies. Result is cached per process — the cache is
    invalidated when ``save_template`` clears the Jinja env cache (admin
    template editor save path).
    """
    if skill_file in _skill_meta_cache:
        return _skill_meta_cache[skill_file]

    template_name = f"skills/{skill_file}.md"
    raw = _env.loader.get_source(_env, template_name)[0]
    m = _SKILL_META_RE.match(raw)
    if not m:
        raise ValueError(
            f"Skill template {template_name} missing YAML frontmatter "
            f"(expected ---\\nname: ...\\n---)"
        )
    front_text, body = m.group(1), m.group(2)
    front: Dict[str, str] = {}
    for fm in _FRONT_KEY_RE.finditer(front_text):
        front[fm.group(1)] = fm.group(2).strip()
    name = front.get("name", "").strip()
    if not name:
        raise ValueError(
            f"Skill template {template_name} missing `name:` in frontmatter"
        )
    description = body.strip()
    meta = {"name": name, "description": description}
    _skill_meta_cache[skill_file] = meta
    return meta


def _invalidate_skill_meta_cache() -> None:
    """Called from template_preview.save_template after a write."""
    _skill_meta_cache.clear()
