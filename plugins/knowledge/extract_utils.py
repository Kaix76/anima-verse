"""Shared Logic fuer Fakten-Extraktion aus Dateien.

Wird vom KnowledgeExtract Plugin-Skill und vom Scheduler verwendet.
Unterstuetzt Batch-Extraktion: Mehrere Dateien pro LLM-Call, gruppiert nach Tag.
"""
import glob
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List

from app.core.log import get_logger
from app.core.llm_router import resolve_llm
from app.core.llm_queue import get_llm_queue, Priority
from app.models.character import (
    get_character_personality, get_character_config, get_character_dir)
from app.models.memory import add_memory, delete_memories_by_source

logger = get_logger("knowledge.extract")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_related_character(filepath: str) -> str:
    """Erkennt related_character aus dem Dateipfad.

    Pattern: .../characters/{CharName}/... -> CharName
    """
    parts = os.path.normpath(filepath).split(os.sep)
    for i, part in enumerate(parts):
        if part == "characters" and i + 1 < len(parts):
            candidate = parts[i + 1]
            if i + 2 < len(parts):
                return candidate
    return ""


def _make_file_key(filepath: str, folder_roots: list) -> str:
    """Erzeugt einen eindeutigen File-Key (relativer Pfad ab folder_root)."""
    norm = os.path.normpath(filepath)
    for root in folder_roots:
        norm_root = os.path.normpath(root)
        if norm.startswith(norm_root + os.sep):
            return norm[len(norm_root) + 1:]
    return os.path.basename(filepath)


def _extract_timestamp(filepath: str, content: str) -> str:
    """Ermittelt das echte Erstellungsdatum einer Datei (mehrere Strategien).

    1. JSON-Feld (created_at, date, timestamp, ...)
    2. Datum im Dateinamen (YYYY-MM-DD)
    3. Fallback: Datei-mtime
    """
    filename = os.path.basename(filepath)
    # 1. Aus JSON-Inhalt
    try:
        parsed_json = json.loads(content)
        target = None
        if isinstance(parsed_json, dict):
            target = parsed_json
        elif isinstance(parsed_json, list) and parsed_json and isinstance(parsed_json[0], dict):
            target = parsed_json[0]
        if target:
            for field in ("created_at", "date", "timestamp", "created", "created_date"):
                val = target.get(field, "")
                if val and isinstance(val, str):
                    datetime.fromisoformat(val)
                    return val
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    # 2. Datum aus Dateiname
    date_match = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
    if date_match:
        try:
            datetime.fromisoformat(date_match.group(1))
            return date_match.group(1) + "T00:00:00"
        except ValueError:
            pass
    # 3. mtime
    try:
        mtime = os.path.getmtime(filepath)
        return datetime.fromtimestamp(mtime).isoformat()
    except OSError:
        return ""


def _estimate_tokens(text: str) -> int:
    """Grobe Token-Schaetzung (1 Token ~ 3.5 Zeichen fuer gemischten DE/EN Text)."""
    return max(1, len(text) // 3)


def _parse_llm_facts(result_text: str) -> List[str]:
    """Parst LLM-Antwort und extrahiert Fakten-Liste."""
    json_text = result_text
    if '```' in json_text:
        match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', json_text, re.DOTALL)
        if match:
            json_text = match.group(1)
    try:
        parsed = json.loads(json_text)
        facts = parsed.get("facts", [])
    except (ValueError, KeyError):
        return []
    return [f for f in facts if f and isinstance(f, str) and len(f) > 5]


# ---------------------------------------------------------------------------
# File Discovery & Caching
# ---------------------------------------------------------------------------

def _discover_files(
    folder_path: str,
    file_pattern: str,
    include_subdirs: bool,
    exclude_dirs: str,
    max_age_days: int = 0) -> tuple:
    """Sammelt Dateien und gibt (files, folder_paths, exclude_set) zurueck."""
    folder_paths = [p.strip() for p in folder_path.split(",") if p.strip()]
    exclude_set = {d.strip() for d in exclude_dirs.split(",") if d.strip()} if exclude_dirs else set()

    files = []
    for fpath in folder_paths:
        if not os.path.isdir(fpath):
            logger.info("Ordner nicht gefunden, uebersprungen: %s", fpath)
            continue
        if include_subdirs:
            found = glob.glob(os.path.join(fpath, '**', file_pattern), recursive=True)
        else:
            found = glob.glob(os.path.join(fpath, file_pattern))
        files.extend(found)

    files = [f for f in files if os.path.isfile(f)]

    if exclude_set:
        before = len(files)
        files = [f for f in files
                 if not any(part in exclude_set
                            for part in os.path.normpath(f).split(os.sep))]
        excluded = before - len(files)
        if excluded:
            logger.info("%d Dateien durch exclude_dirs gefiltert: %s", excluded, exclude_set)

    # Datumsfilter: nur Dateien nicht aelter als max_age_days
    if max_age_days and max_age_days > 0:
        import time
        cutoff = time.time() - (max_age_days * 86400)
        before = len(files)
        files = [f for f in files if os.path.getmtime(f) >= cutoff]
        filtered = before - len(files)
        if filtered:
            logger.info("%d Dateien durch max_age_days=%d gefiltert", filtered, max_age_days)

    return files, folder_paths


def _load_mtime_cache(character_name: str) -> tuple:
    """Laedt mtime-Cache und gibt (cache_dict, cache_path) zurueck."""
    cache_path = str(get_character_dir(character_name) / "skills" / "knowledge_extract_cache.json")
    cache: Dict[str, float] = {}
    try:
        if os.path.exists(cache_path):
            with open(cache_path, 'r', encoding='utf-8') as cf:
                cache = json.load(cf)
    except Exception:
        pass
    return cache, cache_path


def _save_mtime_cache(cache: dict, cache_path: str, current_keys: set):
    """Speichert den mtime-Cache (bereinigt um geloeschte Dateien)."""
    for key in list(cache.keys()):
        if key not in current_keys:
            del cache[key]
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, 'w', encoding='utf-8') as cf:
            json.dump(cache, cf)
    except Exception as e:
        logger.info("Cache speichern fehlgeschlagen: %s", e)


# ---------------------------------------------------------------------------
# Batch-Extraction
# ---------------------------------------------------------------------------

def _build_batches(
    file_infos: List[Dict[str, Any]],
    batch_size: int,
    max_input_tokens: int) -> List[List[Dict[str, Any]]]:
    """Gruppiert Dateien in Batches nach Tag, dann nach Token-Limit.

    Dateien vom selben Tag landen bevorzugt im selben Batch.
    """
    # Nach Tag gruppieren
    by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for info in file_infos:
        ts = info.get("timestamp", "")
        try:
            day = datetime.fromisoformat(ts).strftime("%Y-%m-%d") if ts else "unknown"
        except (ValueError, TypeError):
            day = "unknown"
        by_day[day].append(info)

    batches: List[List[Dict[str, Any]]] = []
    current_batch: List[Dict[str, Any]] = []
    current_tokens = 0

    for day in sorted(by_day.keys()):
        for info in by_day[day]:
            tokens = info.get("tokens", 0)
            # Neuen Batch starten wenn Limits erreicht
            if current_batch and (
                len(current_batch) >= batch_size
                or current_tokens + tokens > max_input_tokens
            ):
                batches.append(current_batch)
                current_batch = []
                current_tokens = 0
            current_batch.append(info)
            current_tokens += tokens

    if current_batch:
        batches.append(current_batch)

    return batches


def _extract_batch(
    batch: List[Dict[str, Any]],
    character_name: str,
    personality: str,
    extraction_prompt: str,
    query: str,
    llm: Any, max_output_tokens: int) -> List[Dict[str, Any]]:
    """Extrahiert Fakten aus einem Batch von Dateien per LLM.

    Returns:
        Liste von {fact, timestamp, related_char, fkey} Dicts.
    """
    # Batch-Content zusammenbauen
    content_parts = []
    for info in batch:
        content_parts.append(f"=== Datei: {info['fkey']} ===\n{info['content']}")
    combined_content = "\n\n".join(content_parts)

    focus_parts = []
    if extraction_prompt:
        focus_parts.append(f"Fokus: {extraction_prompt}")
    if query:
        focus_parts.append(f"Suche speziell nach Informationen zu: {query}")
    focus = "\n" + "\n".join(focus_parts) if focus_parts else ""

    n_files = len(batch)
    max_facts = min(n_files * 3, 15)

    system_prompt = (
        f"Du bist {character_name}. {personality}\n\n"
        f"Lies die folgenden {n_files} Dateien und fasse die wichtigsten Informationen "
        f"zusammen. Erstelle WENIGE (max {max_facts}), aber GEHALTVOLLE Zusammenfassungen "
        f"statt vieler atomarer Einzelfakten.{focus}\n\n"
        f"REGELN:\n"
        f"- Fasse zusammengehoerige Informationen in einem Satz zusammen\n"
        f"- KEINE trivialen Fakten (Name, IDs, technische Metadaten, Timestamps)\n"
        f"- KEINE Einzelzeiten oder Einzeldaten ohne Kontext\n"
        f"- Fokussiere auf: Persoenlichkeit, Beziehungen, wichtige Ereignisse, "
        f"Verhaltensmuster, emotionale Dynamiken\n"
        f"- Jeder Fakt soll fuer sich stehend verstaendlich sein\n"
        f"- Fasse Informationen aus verschiedenen Dateien zusammen wenn sie sich ergaenzen\n\n"
        f"Antworte NUR mit einer JSON-Liste:\n"
        f'{{"facts": ["Zusammenfassung 1", "Zusammenfassung 2", ...]}}\n'
        f'Wenn keine relevanten Fakten: {{"facts": []}}'
    )

    user_msg = f"/no_think\n{combined_content}\n---\n\nExtrahiere die Fakten."

    try:
        response = get_llm_queue().submit(
            task_type="file_extraction",
            priority=Priority.LOW,
            llm=llm,
            messages_or_prompt=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            character_name=character_name)
        facts = _parse_llm_facts(response.content.strip())
    except Exception as e:
        fkeys = [i["fkey"] for i in batch]
        logger.info("LLM-Fehler fuer Batch %s: %s", fkeys[:3], e)
        return []

    if not facts:
        return []

    # Fakten mit Metadaten des Batches verknuepfen
    # Alle Dateien im Batch teilen sich die extrahierten Fakten
    timestamps = [i["timestamp"] for i in batch if i.get("timestamp")]
    batch_timestamp = min(timestamps) if timestamps else ""
    related_chars = {i["related_char"] for i in batch if i.get("related_char")}
    related_char = related_chars.pop() if len(related_chars) == 1 else ""
    fkeys = [i["fkey"] for i in batch]

    results = []
    for fact in facts:
        results.append({
            "fact": fact,
            "timestamp": batch_timestamp,
            "related_char": related_char,
            "fkeys": fkeys,
        })
    return results


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def extract_knowledge_from_files(character_name: str,
    folder_path: str,
    file_pattern: str = "*.json",
    include_subdirs: bool = False,
    extraction_prompt: str = "",
    llm_model_override: str = "",
    query: str = "",
    exclude_dirs: str = "",
    batch_size: int = 5,
    max_input_tokens: int = 12000,
    max_output_tokens: int = 1500,
    max_age_days: int = 0) -> Dict[str, Any]:
    """Liest Dateien aus Ordnern und extrahiert Wissen per LLM (Batch-Modus).

    Features:
    - Batch-Extraktion: Mehrere Dateien pro LLM-Call, gruppiert nach Tag
    - folder_path kann komma-separiert mehrere Pfade enthalten
    - exclude_dirs: Komma-separierte Verzeichnisnamen zum Ausschliessen
    - max_age_days: Nur Dateien beruecksichtigen, die nicht aelter als X Tage sind
    - Echter Timestamp aus Datei-Inhalt/Name/mtime
    - Konfigurierbare batch_size und max_input/output_tokens
    """
    if not folder_path:
        return {"success": False, "error": "Kein folder_path konfiguriert"}

    # 1. Dateien sammeln
    files, folder_paths = _discover_files(folder_path, file_pattern, include_subdirs, exclude_dirs, max_age_days)
    if not files:
        logger.info("Keine Dateien gefunden in %s (%s)", folder_paths, file_pattern)
        return {"success": True, "files_found": 0, "extracted": 0, "cleaned_stale": 0}
    logger.info("%d Dateien gefunden in %s (%s)", len(files), folder_paths, file_pattern)

    # 2. File-Keys und Bereinigung
    file_keys = {f: _make_file_key(f, folder_paths) for f in files}
    current_keys = set(file_keys.values())

    cleaned = delete_memories_by_source(character_name, "file_extraction")
    if cleaned:
        logger.info("%d alte file_extraction-Eintraege bereinigt", cleaned)

    # 3. mtime-Cache
    mtime_cache, cache_path = _load_mtime_cache(character_name)
    changed_files = []
    skipped_count = 0
    for filepath in files:
        fkey = file_keys[filepath]
        try:
            current_mtime = os.path.getmtime(filepath)
        except OSError:
            continue
        if current_mtime > mtime_cache.get(fkey, 0):
            changed_files.append(filepath)
        else:
            skipped_count += 1

    if skipped_count:
        logger.info("%d unveraendert (Cache-Hit), %d geaendert/neu", skipped_count, len(changed_files))

    if not changed_files:
        logger.info("Alle %d Dateien im Cache — keine LLM-Calls noetig", len(files))
        return {
            "success": True,
            "files_found": len(files),
            "extracted": 0,
            "cleaned_stale": cleaned,
            "cached": skipped_count,
        }

    # 4. LLM via Router (Task: extraction)
    agent_config = get_character_config(character_name)
    if llm_model_override:
        logger.info("llm_model_override ignoriert: Router entscheidet via Task (extraction)")

    _inst = resolve_llm("extraction", character_name=character_name)
    if not _inst:
        return {"success": False, "error": "Kein LLM fuer task=extraction verfuegbar"}
    llm = _inst.create_llm()

    # max_output_tokens am LLM setzen wenn moeglich
    if max_output_tokens and hasattr(llm, 'max_tokens'):
        llm.max_tokens = max_output_tokens

    logger.info("LLM: %s (batch_size=%d, max_input=%d, max_output=%d)",
                getattr(llm, 'model', '?'), batch_size, max_input_tokens, max_output_tokens)

    personality = get_character_personality(character_name) or ""
    new_cache = dict(mtime_cache)

    # 5. Dateien lesen und Metadaten sammeln
    file_infos: List[Dict[str, Any]] = []
    for filepath in changed_files:
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read(8000)
            if not content.strip():
                continue

            fkey = file_keys[filepath]
            timestamp = _extract_timestamp(filepath, content)
            tokens = _estimate_tokens(content)

            file_infos.append({
                "filepath": filepath,
                "fkey": fkey,
                "content": content,
                "timestamp": timestamp,
                "tokens": tokens,
                "related_char": _detect_related_character(filepath),
            })

            # mtime im Cache aktualisieren
            try:
                new_cache[fkey] = os.path.getmtime(filepath)
            except OSError:
                pass
        except Exception as e:
            logger.info("Fehler beim Lesen von %s: %s", filepath, e)

    # 6. Batches bilden und extrahieren
    batches = _build_batches(file_infos, batch_size, max_input_tokens)
    logger.info("%d Dateien in %d Batches aufgeteilt", len(file_infos), len(batches))

    all_extracted: List[Dict[str, Any]] = []
    for i, batch in enumerate(batches, 1):
        fkeys = [info["fkey"] for info in batch]
        logger.info("Batch %d/%d: %d Dateien (%s)", i, len(batches), len(batch),
                     ", ".join(fkeys[:3]) + ("..." if len(fkeys) > 3 else ""))

        results = _extract_batch(
            batch, character_name, personality, extraction_prompt, query,
            llm, max_output_tokens)
        all_extracted.extend(results)

        fact_count = len(results)
        ts_info = ""
        timestamps = [info.get("timestamp", "") for info in batch if info.get("timestamp")]
        if timestamps:
            days = sorted(set(
                datetime.fromisoformat(t).strftime("%Y-%m-%d")
                for t in timestamps if t
            ))
            ts_info = f" [{', '.join(days[:3])}]"
        logger.info("  -> %d Fakten extrahiert%s", fact_count, ts_info)

    # 7. Fakten pro Tag + related_character gruppiert speichern
    day_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in all_extracted:
        ts = item.get("timestamp", "")
        try:
            day_key = datetime.fromisoformat(ts).strftime("%Y-%m-%d") if ts else "unknown"
        except (ValueError, TypeError):
            day_key = "unknown"
        group_key = f"{day_key}|{item.get('related_char', '')}"
        day_groups[group_key].append(item)

    for group_key, items in day_groups.items():
        day_str, related_char = group_key.split("|", 1)
        facts = [it["fact"] for it in items]
        combined = "\n".join(facts)
        timestamps = [it["timestamp"] for it in items if it.get("timestamp")]
        group_timestamp = min(timestamps) if timestamps else ""
        all_fkeys = sorted(set(fk for it in items for fk in it.get("fkeys", [])))
        context = f"file:{all_fkeys[0]}" if len(all_fkeys) == 1 else f"files:{','.join(all_fkeys[:5])}"

        add_memory(
            character_name=character_name,
            content=combined,
            memory_type="semantic",
            importance=3,
            tags=["file_extraction"],
            context=context,
            related_character=related_char,
            timestamp=group_timestamp)

    # 8. Cache speichern
    _save_mtime_cache(new_cache, cache_path, current_keys)

    extracted_count = len(all_extracted)
    logger.info("Fertig: %d Dateien in %d Batches, %d Fakten, %d gecached",
                len(changed_files), len(batches), extracted_count, skipped_count)
    return {
        "success": True,
        "files_found": len(files),
        "extracted": extracted_count,
        "cleaned_stale": cleaned,
        "cached": skipped_count,
        "batches": len(batches),
    }
