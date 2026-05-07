"""MarkdownWriter Skill - Ermoeglicht dem Agenten, Markdown-Dateien zu schreiben.

Der Agent kann:
- Neue MD-Dateien erstellen (Tagebuch, Zusammenfassungen, Anleitungen, etc.)
- Bestehende Dateien fortsetzen / erweitern
- Sich seine letzten Dateien merken und spaeter weiterschreiben
- In konfigurierbare Verzeichnisse schreiben (mehrere moeglich)

Pro Character wird ein Index (recent_files.json) gefuehrt, der die zuletzt
bearbeiteten Dateien trackt. So kann der Agent nahtlos an frueheren Texten
weiterarbeiten.
"""
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import BaseSkill, ToolSpec

from app.core.log import get_logger
from app.core.tool_formats import format_example
from app.models.character import get_character_dir

logger = get_logger("markdown_writer")

# Max. Anzahl der getrackten Recent-Files pro Character
MAX_RECENT_FILES = 20


class MarkdownWriterSkill(BaseSkill):
    """Skill zum Erstellen und Fortsetzen von Markdown-Dateien.

    Input-Format (JSON):
        {
            "action": "write" | "append" | "list" | "read",
            "folder": "diary" | "guides" | ... (konfigurierter Ordnername),
            "filename": "mein-tagebuch.md" (optional bei write - wird auto-generiert),
            "title": "Tagebuch-Eintrag 15. März" (optional, fuer neue Dateien),
            "content": "Der Markdown-Inhalt ..."
        }

    Aktionen:
        write   - Neue Datei erstellen oder bestehende UEBERSCHREIBEN
        append  - An bestehende Datei anhaengen (oder neue erstellen)
        list    - Zeigt die letzten Dateien des Characters in einem Ordner
        read    - Liest eine bestehende Datei (zum Weiterschreiben)
    """

    SKILL_ID = "markdown_writer"
    ALWAYS_LOAD = True

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        from app.core.prompt_templates import load_skill_meta
        meta = load_skill_meta("markdown_writer")
        self.name = meta["name"]
        self.description = meta["description"]
        # Konfigurierbare Ordner als kommagetrennte Liste
        default_folders = os.environ.get("SKILL_MARKDOWN_WRITER_FOLDERS", "diary,notes,guides")
        self._defaults = {
            "enabled": True,
            "folders": default_folders,
            "max_file_size_kb": int(os.environ.get("SKILL_MARKDOWN_WRITER_MAX_SIZE_KB", "512")),
            "max_files_per_folder": int(os.environ.get("SKILL_MARKDOWN_WRITER_MAX_FILES", "50")),
            "default_folder": os.environ.get("SKILL_MARKDOWN_WRITER_DEFAULT_FOLDER", "diary"),
        }

    def get_config_fields(self) -> dict:
        return {
            "folders": {
                "type": "str",
                "default": self._defaults["folders"],
                "label": "Verfügbare Ordner (kommagetrennt)",
                "label_de": "Verfügbare Ordner (kommagetrennt)",
            },
            "max_file_size_kb": {
                "type": "int",
                "default": self._defaults["max_file_size_kb"],
                "label": "Max. Dateigröße (KB)",
            },
            "max_files_per_folder": {
                "type": "int",
                "default": self._defaults["max_files_per_folder"],
                "label": "Max. Dateien pro Ordner",
            },
            "default_folder": {
                "type": "str",
                "default": self._defaults["default_folder"],
                "label": "Standard-Ordner",
            },
        }

    # ------------------------------------------------------------------
    # Pfad-Helfer
    # ------------------------------------------------------------------

    def _get_base_dir(self, character_name: str) -> Path:
        """Basis-Verzeichnis fuer Markdown-Dateien eines Characters."""
        return get_character_dir(character_name) / "documents"

    def _get_folder_path(self, character_name: str, folder: str) -> Optional[Path]:
        """Gibt den Pfad eines konfigurierten Ordners zurueck (oder None)."""
        cfg = self._get_effective_config(character_name)
        allowed = [f.strip() for f in cfg.get("folders", "diary").split(",") if f.strip()]
        folder_clean = folder.strip().lower()

        if folder_clean not in [f.lower() for f in allowed]:
            return None

        # Originalen Namen beibehalten (Case)
        for f in allowed:
            if f.lower() == folder_clean:
                folder_clean = f
                break

        path = self._get_base_dir(character_name) / folder_clean
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _get_allowed_folders(self, character_name: str) -> List[str]:
        """Gibt die konfigurierten Ordnernamen zurueck."""
        cfg = self._get_effective_config(character_name)
        return [f.strip() for f in cfg.get("folders", "diary").split(",") if f.strip()]

    # ------------------------------------------------------------------
    # Recent-Files Tracking
    # ------------------------------------------------------------------

    def _get_recent_files_path(self, character_name: str) -> Path:
        base = self._get_base_dir(character_name)
        base.mkdir(parents=True, exist_ok=True)
        return base / "recent_files.json"

    def _load_recent_files(self, character_name: str) -> List[Dict[str, str]]:
        path = self._get_recent_files_path(character_name)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def _save_recent_files(self, character_name: str, entries: List[Dict[str, str]]):
        path = self._get_recent_files_path(character_name)
        # Nur die letzten MAX_RECENT_FILES behalten
        entries = entries[-MAX_RECENT_FILES:]
        path.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")

    def _track_file(self, character_name: str, folder: str,
                    filename: str, title: str, action: str):
        """Fuegt eine Datei zum Recent-Tracking hinzu oder aktualisiert sie."""
        entries = self._load_recent_files(character_name)

        # Existierenden Eintrag aktualisieren oder neuen hinzufuegen
        found = False
        for entry in entries:
            if entry.get("folder") == folder and entry.get("filename") == filename:
                entry["last_modified"] = datetime.now().isoformat()
                entry["last_action"] = action
                if title:
                    entry["title"] = title
                found = True
                break

        if not found:
            entries.append({
                "folder": folder,
                "filename": filename,
                "title": title or filename,
                "created": datetime.now().isoformat(),
                "last_modified": datetime.now().isoformat(),
                "last_action": action,
            })

        self._save_recent_files(character_name, entries)

    # ------------------------------------------------------------------
    # Dateinamen-Helfer
    # ------------------------------------------------------------------

    @staticmethod
    def _sanitize_filename(name: str) -> str:
        """Bereinigt einen Dateinamen (nur sichere Zeichen)."""
        # Entferne unsichere Zeichen
        name = re.sub(r'[^\w\s\-.]', '', name)
        name = re.sub(r'\s+', '-', name.strip())
        name = name.strip('-.')
        if not name:
            name = "dokument"
        # .md Endung sicherstellen
        if not name.lower().endswith('.md'):
            name += '.md'
        return name

    @staticmethod
    def _generate_filename(title: str = "", folder: str = "") -> str:
        """Generiert einen Dateinamen basierend auf Datum und optionalem Titel."""
        date_str = datetime.now().strftime("%Y-%m-%d")
        if title:
            slug = re.sub(r'[^\w\s\-]', '', title)
            slug = re.sub(r'\s+', '-', slug.strip()).lower()[:40]
            return f"{date_str}_{slug}.md"
        return f"{date_str}_{folder or 'doc'}.md"

    # ------------------------------------------------------------------
    # Aktionen
    # ------------------------------------------------------------------

    def _action_write(self, character_name: str, folder: str,
                      filename: str, title: str, content: str, cfg: dict) -> str:
        """Neue Datei erstellen oder bestehende ueberschreiben."""
        folder_path = self._get_folder_path(character_name, folder)
        if not folder_path:
            allowed = self._get_allowed_folders(character_name)
            return f"Fehler: Ordner '{folder}' nicht erlaubt. Erlaubt: {', '.join(allowed)}"

        if not content:
            return "Fehler: content darf nicht leer sein."

        # Dateiname
        if not filename:
            filename = self._generate_filename(title, folder)
        filename = self._sanitize_filename(filename)

        # Limit pruefen: Anzahl Dateien
        max_files = cfg.get("max_files_per_folder", 50)
        existing = list(folder_path.glob("*.md"))
        file_path = folder_path / filename
        if not file_path.exists() and len(existing) >= max_files:
            return f"Fehler: Maximum von {max_files} Dateien in '{folder}' erreicht."

        # Inhalt zusammenbauen
        md_content = ""
        if title:
            md_content = f"# {title}\n\n"
        md_content += content

        # Groesse pruefen
        max_kb = cfg.get("max_file_size_kb", 512)
        if len(md_content.encode("utf-8")) > max_kb * 1024:
            return f"Fehler: Inhalt ueberschreitet das Limit von {max_kb} KB."

        file_path.write_text(md_content, encoding="utf-8")
        self._track_file(character_name, folder, filename, title, "write")

        logger.info("%s hat Datei geschrieben: %s/%s", character_name, folder, filename)
        return f"Datei geschrieben: {folder}/{filename} ({len(md_content)} Zeichen)"

    def _action_append(self, character_name: str, folder: str,
                       filename: str, title: str, content: str, cfg: dict) -> str:
        """An bestehende Datei anhaengen oder neue erstellen."""
        folder_path = self._get_folder_path(character_name, folder)
        if not folder_path:
            allowed = self._get_allowed_folders(character_name)
            return f"Fehler: Ordner '{folder}' nicht erlaubt. Erlaubt: {', '.join(allowed)}"

        if not content:
            return "Fehler: content darf nicht leer sein."

        if not filename:
            # Versuche die zuletzt bearbeitete Datei im Ordner zu finden
            recent = self._load_recent_files(character_name)
            folder_recent = [r for r in recent if r.get("folder") == folder]
            if folder_recent:
                filename = folder_recent[-1]["filename"]
            else:
                filename = self._generate_filename(title, folder)

        filename = self._sanitize_filename(filename)
        file_path = folder_path / filename

        # Limit: neue Datei?
        max_files = cfg.get("max_files_per_folder", 50)
        if not file_path.exists():
            existing = list(folder_path.glob("*.md"))
            if len(existing) >= max_files:
                return f"Fehler: Maximum von {max_files} Dateien in '{folder}' erreicht."

        # Bestehenden Inhalt laden
        existing_content = ""
        if file_path.exists():
            existing_content = file_path.read_text(encoding="utf-8")

        # Anhaengen
        separator = "\n\n---\n\n" if existing_content else ""
        if title and not existing_content:
            new_part = f"# {title}\n\n{content}"
        else:
            new_part = content

        final = existing_content + separator + new_part

        # Groesse pruefen
        max_kb = cfg.get("max_file_size_kb", 512)
        if len(final.encode("utf-8")) > max_kb * 1024:
            return f"Fehler: Datei wuerde {max_kb} KB ueberschreiten. Erstelle eine neue Datei."

        file_path.write_text(final, encoding="utf-8")
        self._track_file(character_name, folder, filename, title, "append")

        logger.info("%s hat an Datei angehaengt: %s/%s", character_name, folder, filename)
        return f"Angehaengt an: {folder}/{filename} ({len(final)} Zeichen gesamt)"

    def _action_list(self, character_name: str, folder: str) -> str:
        """Listet die letzten Dateien eines Ordners."""
        if folder:
            folder_path = self._get_folder_path(character_name, folder)
            if not folder_path:
                allowed = self._get_allowed_folders(character_name)
                return f"Fehler: Ordner '{folder}' nicht erlaubt. Erlaubt: {', '.join(allowed)}"

        recent = self._load_recent_files(character_name)
        if folder:
            recent = [r for r in recent if r.get("folder") == folder]

        if not recent:
            return f"Keine Dateien gefunden{' in ' + folder if folder else ''}."

        lines = []
        for entry in reversed(recent[-10:]):
            modified = entry.get("last_modified", "?")
            if modified != "?":
                try:
                    dt = datetime.fromisoformat(modified)
                    modified = dt.strftime("%d.%m.%Y %H:%M")
                except Exception:
                    pass
            lines.append(
                f"- {entry.get('folder', '?')}/{entry.get('filename', '?')} "
                f"— \"{entry.get('title', '')}\" (zuletzt: {modified})"
            )

        return "Letzte Dateien:\n" + "\n".join(lines)

    def _action_read(self, character_name: str, folder: str,
                     filename: str) -> str:
        """Liest eine bestehende Datei."""
        if not folder:
            return "Fehler: folder muss angegeben werden."
        if not filename:
            # Letzte Datei im Ordner
            recent = self._load_recent_files(character_name)
            folder_recent = [r for r in recent if r.get("folder") == folder]
            if folder_recent:
                filename = folder_recent[-1]["filename"]
            else:
                return f"Fehler: Keine Dateien in '{folder}' bekannt. Verwende 'list' zum Anzeigen."

        folder_path = self._get_folder_path(character_name, folder)
        if not folder_path:
            allowed = self._get_allowed_folders(character_name)
            return f"Fehler: Ordner '{folder}' nicht erlaubt. Erlaubt: {', '.join(allowed)}"

        filename = self._sanitize_filename(filename)
        file_path = folder_path / filename

        if not file_path.exists():
            # Versuche Fuzzy-Match
            candidates = list(folder_path.glob("*.md"))
            name_lower = filename.lower().replace('.md', '')
            for c in candidates:
                if name_lower in c.stem.lower():
                    file_path = c
                    filename = c.name
                    break
            else:
                available = [c.name for c in candidates[:10]]
                if available:
                    return f"Datei '{filename}' nicht gefunden. Vorhanden: {', '.join(available)}"
                return f"Datei '{filename}' nicht gefunden und keine Dateien in '{folder}'."

        content = file_path.read_text(encoding="utf-8")

        # Kuerzen falls sehr lang (damit LLM-Kontext nicht ueberlastet wird)
        max_chars = 4000
        truncated = ""
        if len(content) > max_chars:
            content = content[:max_chars]
            truncated = f"\n\n[... gekuerzt, {len(content)} von {len(file_path.read_text(encoding='utf-8'))} Zeichen angezeigt]"

        self._track_file(character_name, folder, filename, "", "read")
        return f"=== {folder}/{filename} ===\n{content}{truncated}"

    # ------------------------------------------------------------------
    # execute
    # ------------------------------------------------------------------

    def execute(self, raw_input: str) -> str:
        if not self.enabled:
            return "Fehler: MarkdownWriter Skill ist nicht verfuegbar."

        try:
            return self._execute_inner(raw_input)
        except Exception as e:
            logger.error("Fehler in MarkdownWriter: %s", e, exc_info=True)
            return f"Fehler: {e}"

    def _execute_inner(self, raw_input: str) -> str:
        ctx = self._parse_base_input(raw_input)
        character_name = ctx.get("agent_name", "").strip()
        user_id = ctx.get("user_id", "").strip()

        if not character_name:
            return "Fehler: Agent-Name fehlt."

        cfg = self._get_effective_config(character_name)

        action = ctx.get("action", "append").strip().lower()
        folder = ctx.get("folder", "").strip()
        filename = ctx.get("filename", "").strip()
        title = ctx.get("title", "").strip()
        content = ctx.get("content", "").strip()

        # Default-Ordner falls keiner angegeben
        if not folder:
            folder = cfg.get("default_folder", "diary")

        if action == "write":
            return self._action_write(character_name, folder, filename, title, content, cfg)
        elif action == "append":
            return self._action_append(character_name, folder, filename, title, content, cfg)
        elif action == "list":
            return self._action_list(character_name, folder)
        elif action == "read":
            return self._action_read(character_name, folder, filename)
        else:
            return f"Fehler: Unbekannte Aktion '{action}'. Erlaubt: write, append, list, read"

    # ------------------------------------------------------------------
    # memorize_result (fuer Scheduler)
    # ------------------------------------------------------------------

    def memorize_result(self, result: str, character_name: str) -> bool:
        """Speichert das Ergebnis als Memory, damit der Agent weiss was er geschrieben hat."""
        if "Fehler:" in result:
            return False
        try:
            from app.models.memory import add_memory
            add_memory(character_name,
                content=f"Dokument-Aktion: {result}",
                memory_type="semantic",
                importance=2,
                tags=["document", "markdown"])
            return True
        except Exception as e:
            logger.warning("Memory-Speicherung fehlgeschlagen: %s", e)
            return False

    # ------------------------------------------------------------------
    # Tool-Beschreibung
    # ------------------------------------------------------------------

    def get_usage_instructions(self, format_name: str = "", character_name: str = "") -> str:
        fmt = format_name or "tag"
        return format_example(
            fmt, self.name,
            '{"action": "append", "folder": "diary", "title": "15. März", '
            '"content": "Heute war ein aufregender Tag..."}'
        )

    def _build_folders_hint(self, character_name: str) -> str:
        """Baut eine Liste der verfuegbaren Ordner fuer die Tool-Beschreibung."""
        if not character_name:
            return ""
        try:
            folders = self._get_allowed_folders(character_name)
            if folders:
                return f" Available folders: {', '.join(folders)}."
        except Exception:
            pass
        return ""

    def _build_recent_hint(self, character_name: str) -> str:
        """Zeigt die zuletzt bearbeiteten Dateien in der Tool-Beschreibung."""
        if not character_name:
            return ""
        try:
            recent = self._load_recent_files(character_name)
            if not recent:
                return ""
            # Letzte 3 Dateien
            hints = []
            for entry in recent[-3:]:
                hints.append(f"{entry.get('folder', '?')}/{entry.get('filename', '?')}")
            return f" Recent files: {', '.join(hints)}."
        except Exception:
            pass
        return ""

    def as_tool(self, character_name: str = "") -> ToolSpec:
        folders_hint = self._build_folders_hint(character_name)
        recent_hint = self._build_recent_hint(character_name)

        return ToolSpec(
            name=self.name,
            description=(
                f"{self.description}. "
                "Input JSON: {\"action\": \"write|append|list|read\", "
                "\"folder\": \"<folder name>\", "
                "\"filename\": \"<optional filename.md>\", "
                "\"title\": \"<optional title>\", "
                "\"content\": \"<markdown text>\"}. "
                "Actions: "
                "'append' adds to an existing file (or creates new) — best for diary entries and ongoing documents. "
                "'write' creates or overwrites a file. "
                "'list' shows recent files in a folder. "
                "'read' reads a file to continue writing later. "
                "If no filename is given for append, the last edited file in that folder is used automatically. "
                "Content should be plain Markdown text."
                f"{folders_hint}{recent_hint}"
            ),
            func=self.execute)
