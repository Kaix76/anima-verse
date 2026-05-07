"""Zentrales LLM Call Logging — schreibt alle LLM-Aufrufe als JSONL nach logs/llm_calls.jsonl.

Jeder Eintrag enthaelt: Start/End-Timestamp, Task, Model, Character, User, Prompt, Response,
Dauer, Token-Nutzung (real oder geschaetzt), max. Context-Laenge.
"""
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.log import get_logger

logger = get_logger("llm_log")

LOG_DIR = Path("./logs")
LOG_FILE = LOG_DIR / "llm_calls.jsonl"
_lock = threading.Lock()


def _template_basename(template_path: str, fallback_task: str) -> str:
    """Schneidet Path und .md-Suffix vom Template-Pfad weg.

    Beispiel: ``shared/templates/llm/tasks/classify_activity.md`` →
    ``classify_activity``. Akzeptiert auch reine Basenames oder
    Sub-Path-Form (``tasks/classify_activity.md``). Fallback auf den
    Task-Namen wenn kein Template uebergeben wurde.
    """
    if not template_path:
        return fallback_task or ""
    name = template_path.replace("\\", "/").rsplit("/", 1)[-1]
    if name.endswith(".md"):
        name = name[:-3]
    return name or fallback_task or ""


def log_llm_call(
    task: str,
    model: str,
    agent_name: str = "", provider: str = "",
    system_prompt: str = "",
    user_input: str = "",
    response: str = "",
    duration_s: float = 0.0,
    tokens_input: int = 0,
    tokens_output: int = 0,
    max_tokens: int = 0,
    messages: Optional[List[Dict[str, str]]] = None,
    error: str = "",
    llm_role: str = "",
    template: str = ""):
    """Loggt einen LLM-Aufruf als JSONL-Zeile und gibt eine kurze Zeile auf stdout aus.

    Args:
        task: Art des Aufrufs (chat_stream, image_prompt, social_reaction, etc.)
        model: Model-Name (z.B. mistral:latest)
        agent_name: Character-Name
        provider: Provider-Name (z.B. OllamaLocal, OpenAI-API)
        system_prompt: System-Prompt Text
        user_input: User-/Human-Nachricht
        response: LLM-Antwort
        duration_s: Dauer in Sekunden
        tokens_input: Input-Tokens (real oder geschaetzt)
        tokens_output: Output-Tokens (real oder geschaetzt)
        max_tokens: Max. Tokens / Context-Laenge
        messages: Optionale volle Message-Liste fuer multi-message calls
        llm_role: Rolle des LLM-Aufrufs (Tool-LLM, Chat-LLM, LLM)
        template: Voller Pfad oder Dateiname des verwendeten Jinja-Templates
            (z.B. ``shared/templates/llm/tasks/classify_activity.md``).
            Wird auf den Basenamen ohne ``.md`` reduziert und im Log-Viewer
            als drittes Badge angezeigt — erleichtert Fehlersuche, weil man
            sofort sieht welche Template-Datei gerendert wurde. Bei leerem
            Wert faellt der Logger auf ``task`` zurueck.
    """
    template_basename = _template_basename(template, task)
    end_time = datetime.now()
    start_time = end_time - timedelta(seconds=duration_s)
    entry: Dict[str, Any] = {
        "starttime": start_time.isoformat(timespec="seconds"),
        "endtime": end_time.isoformat(timespec="seconds"),
        "task": task,
        "template": template_basename,
        "llm_role": llm_role or task,
        "provider": provider,
        "model": model,
        "service": agent_name,
        "user_id": "",
        "duration_s": round(duration_s, 2),
        "tokens": {
            "input": tokens_input,
            "output": tokens_output,
            "max": max_tokens,
        },
        "prompt": {},
        "response": response,
    }

    if error:
        entry["error"] = error

    # Prompt aufbauen
    if system_prompt:
        entry["prompt"]["system"] = system_prompt
    if user_input:
        entry["prompt"]["user"] = user_input
    if messages:
        entry["prompt"]["messages"] = messages

    # JSONL schreiben
    with _lock:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # Kurze Zeile fuer strukturiertes Logging
    tok_str = ""
    if tokens_input or tokens_output:
        tok_str = " | %d\u2192%d tok" % (tokens_input, tokens_output)
    prov_str = "%s/" % provider if provider else ""
    role_str = "[%s] " % llm_role if llm_role else ""
    if error:
        logger.error(
            "%s%s | %s | %s%s | %.2fs%s | %s",
            role_str, task, agent_name or "-", prov_str, model, duration_s, tok_str, error[:200])
    else:
        logger.info(
            "%s%s | %s | %s%s | %.2fs%s",
            role_str, task, agent_name or "-", prov_str, model, duration_s, tok_str)

    if not error and tokens_input > 0 and tokens_output > 0 and duration_s > 0:
        try:
            from app.utils.llm_stats import record_call
            record_call(model, task, provider, tokens_input, tokens_output, duration_s,
                        agent_name=agent_name, max_tokens=max_tokens)
        except Exception as e:
            logger.warning("llm_stats.record_call fehlgeschlagen: %s", e)


def extract_token_info(response) -> Dict[str, int]:
    """Extrahiert Token-Info aus einem LLM Response.

    Unterstuetzt LLMResponse.usage (dict mit prompt_tokens/completion_tokens).
    """
    info = {"input_tokens": 0, "output_tokens": 0}

    usage = getattr(response, "usage", None)
    if usage and isinstance(usage, dict):
        info["input_tokens"] = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
        info["output_tokens"] = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)

    return info


def estimate_tokens(text: str) -> int:
    """Grobe Token-Schaetzung: ~4 Zeichen pro Token."""
    return len(text) // 4


def get_model_name(llm) -> str:
    """Extrahiert den Model-Namen aus einem LLMClient."""
    return (
        getattr(llm, "model_name", "")
        or getattr(llm, "model", "")
        or "unknown"
    )


def get_max_tokens(llm) -> int:
    """Extrahiert max_tokens aus einem LLMClient."""
    val = getattr(llm, "max_tokens", None)
    return int(val) if val else 0


def prune_jsonl_log(path: Path, retention_days: int) -> int:
    """Entfernt JSONL-Eintraege deren ``starttime`` aelter als
    ``retention_days`` Tage ist. Schreibt die Datei atomic neu (tmp + rename).

    Beide Logs (llm_calls.jsonl, image_prompts.jsonl) haben ``starttime``
    als ISO-String — gleiche Schema-Annahme. Eintraege ohne starttime
    werden konservativ behalten (kein Timestamp = nicht alterbar).

    Returns: Anzahl entfernter Eintraege (0 wenn nichts zu tun).
    """
    if retention_days < 1:
        return 0
    if not path.exists():
        return 0
    cutoff = datetime.now() - timedelta(days=retention_days)
    cutoff_iso = cutoff.isoformat()

    kept: List[str] = []
    removed = 0
    with _lock:
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line_strip = line.strip()
                    if not line_strip:
                        continue
                    try:
                        obj = json.loads(line_strip)
                    except Exception:
                        # nicht-parsebare Zeile lieber behalten
                        kept.append(line_strip)
                        continue
                    ts = (obj.get("starttime") or "").strip()
                    if ts and ts < cutoff_iso:
                        removed += 1
                        continue
                    kept.append(line_strip)
            if removed:
                tmp = path.with_suffix(path.suffix + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    for ln in kept:
                        f.write(ln + "\n")
                tmp.replace(path)
                logger.info("Log-Cleanup %s: %d alte Eintraege entfernt (>%d Tage)",
                            path.name, removed, retention_days)
        except Exception as e:
            logger.warning("Log-Cleanup fuer %s fehlgeschlagen: %s", path, e)
            return 0
    return removed


def prune_logs_on_startup() -> Dict[str, int]:
    """Wird vom Server-Lifespan beim Start aufgerufen. Liest die Aufbewahrungs-
    dauer aus der Config (server.log_retention_days, Default 5) und schneidet
    llm_calls.jsonl + image_prompts.jsonl auf den Zeitraum.
    """
    try:
        from app.core import config as _cfg
        days = int(_cfg.get("server.log_retention_days") or 5)
    except Exception:
        days = 5
    out = {
        "llm_calls": prune_jsonl_log(LOG_FILE, days),
        "image_prompts": prune_jsonl_log(LOG_DIR / "image_prompts.jsonl", days),
        "retention_days": days,
    }
    return out
