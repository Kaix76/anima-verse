"""Pending-Reports — Rueckmeldungs-Kette.

Wenn ein Character waehrend eines Chat-Turns einen `talk_to`/`send_message`
Tool-Call macht (z.B. weil der User ihn bittet, jemand anderen zu fragen),
wird automatisch ein Report-Eintrag angelegt. Sobald das Ziel antwortet,
muss der Character an den Initiator zurueckmelden.

Das System triggert den Sofort-Trigger: sobald die Antwort kommt, wird
ein neuer thought_turn fuer den Reporter gefeuert mit Hint auf offene
Reports. Ausserdem zeigt der Thought-System-Prompt offene Reports prominent.

Datei: `characters/{Character}/pending_reports.json`
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.log import get_logger

logger = get_logger("pending_reports")

_DEFAULT_TTL_HOURS = 24


def _get_file(character_name: str) -> Path:
    from app.models.character import get_character_dir
    return get_character_dir(character_name) / "pending_reports.json"


def _load(character_name: str) -> List[Dict[str, Any]]:
    f = _get_file(character_name)
    if not f.exists():
        return []
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data.get("reports", [])
    except Exception as e:
        logger.warning("pending_reports load fail (%s/%s): %s", character_name, e)
        return []


def _save(character_name: str, reports: List[Dict[str, Any]]) -> None:
    f = _get_file(character_name)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"reports": reports}, ensure_ascii=False, indent=2),
                 encoding="utf-8")


def _is_expired(report: Dict[str, Any]) -> bool:
    try:
        created = datetime.fromisoformat(report.get("created_at", ""))
    except Exception:
        return True
    ttl = report.get("ttl_hours", _DEFAULT_TTL_HOURS)
    return datetime.now() - created > timedelta(hours=ttl)


def list_open(character_name: str) -> List[Dict[str, Any]]:
    """Gibt offene (nicht resolved, nicht abgelaufene) Reports zurueck.

    Abgelaufene werden dabei aus der Datei entfernt.
    """
    reports = _load(character_name)
    kept = []
    open_reports = []
    changed = False
    for r in reports:
        if r.get("resolved"):
            kept.append(r)
            continue
        if _is_expired(r):
            logger.info("pending_report abgelaufen: %s (%s→%s)",
                        r.get("id"), character_name, r.get("to"))
            changed = True
            continue
        kept.append(r)
        open_reports.append(r)
    if changed:
        _save(character_name, kept)
    return open_reports


def add_report(reporter: str,          # Character der schulden hat (from)
    initiator: str,         # Wer urspruenglich gefragt hat (to)
    initiator_type: str,    # "user" oder "character"
    target: str,            # Wen reporter gerade befragt / kontaktiert hat
    trigger_type: str = "talk_to_response",
    trigger_message_id: str = "",
    ttl_hours: int = _DEFAULT_TTL_HOURS) -> str:
    """Legt einen neuen pending_report an.

    Returns:
        Report-ID.
    """
    reports = _load(reporter)
    # Doppelt-Anlage vermeiden: gleicher initiator + target + trigger offen?
    for r in reports:
        if r.get("resolved"):
            continue
        if (r.get("to") == initiator and
                r.get("trigger", {}).get("target") == target):
            logger.debug("pending_report existiert bereits: %s", r["id"])
            return r["id"]

    rid = f"rep_{uuid.uuid4().hex[:8]}"
    now = datetime.now()
    report = {
        "id": rid,
        "from": reporter,
        "to": initiator,
        "to_type": initiator_type,
        "trigger_message_id": trigger_message_id,
        "trigger": {
            "type": trigger_type,
            "target": target,
            "since": now.isoformat(timespec="seconds"),
        },
        "created_at": now.isoformat(timespec="seconds"),
        "ttl_hours": ttl_hours,
        "resolved": False,
    }
    reports.append(report)
    _save(reporter, reports)
    logger.info("pending_report angelegt: %s (%s schuldet %s → Antwort von %s)",
                rid, reporter, initiator, target)
    return rid


def mark_resolved(reporter: str, report_id: str) -> bool:
    reports = _load(reporter)
    for r in reports:
        if r.get("id") == report_id:
            r["resolved"] = True
            r["resolved_at"] = datetime.now().isoformat(timespec="seconds")
            _save(reporter, reports)
            logger.info("pending_report aufgeloest: %s", report_id)
            return True
    return False


def find_matching_report(reporter: str,
    target: str) -> Optional[Dict[str, Any]]:
    """Sucht offenen Report mit trigger.target == target (erster Treffer)."""
    for r in list_open(reporter):
        if r.get("trigger", {}).get("target") == target:
            return r
    return None


def build_prompt_section(character_name: str) -> str:
    """Baut einen Prompt-Abschnitt mit offenen Rueckmeldungen fuer den Thought-Prompt.

    Leer wenn keine offenen Reports.
    """
    open_reports = list_open(character_name)
    if not open_reports:
        return ""

    lines = ["# Offene Rueckmeldungen (wichtig!)"]
    for r in open_reports:
        target = r.get("trigger", {}).get("target", "")
        to_who = r.get("to", "")
        lines.append(
            f"- An {to_who}: du hast {target} kontaktiert und schuldest eine Rueckmeldung "
            f"an {to_who}. Nutze SendMessage/TalkTo um zu berichten."
        )
    return "\n".join(lines) + "\n"


def trigger_sofort_thought_if_applicable(reporter: str,
    partner: str) -> Optional[str]:
    """Pruefe ob es einen offenen Report gibt, bei dem partner der Target war.

    Wenn ja: triggere thought_turn fuer reporter mit context_hint.
    Wird direkt nach Lunas Antwort aufgerufen (run_chat_turn).

    Returns:
        context_hint text wenn triggered, sonst None.
    """
    match = find_matching_report(reporter, partner)
    if not match:
        return None

    to_who = match.get("to", "")
    hint = (
        f"{partner} hat gerade auf deine Frage geantwortet. "
        f"{to_who} wartet noch auf deine Rueckmeldung. "
        f"Berichte {to_who} was {partner} gesagt hat (SendMessage oder TalkTo)."
    )
    logger.info("pending_report Trigger: %s → %s (Antwort von %s)", reporter, to_who, partner)
    return hint
