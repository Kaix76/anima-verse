"""Zentrales Logging fuer die gesamte Anwendung.

Verwendung in jedem Modul:
    from app.core.log import get_logger
    logger = get_logger("module_name")
    logger.info("Nachricht")
    logger.debug("Detail")
    logger.error("Fehler: %s", err)

Log-Level via Umgebungsvariable LOG_LEVEL (default: INFO).
"""
import logging
import os

_configured = False


def _configure_once():
    global _configured
    if _configured:
        return
    _configured = True
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    # force=True ueberschreibt eventuell von uvicorn bereits am Root-Logger
    # angehaengte Handler. Ohne force ist basicConfig ein No-Op, wenn schon
    # Handler existieren — und uvicorn ist VOR uns dran.
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        force=True)
    # uvicorn-Logger an unser Root-Format/Level binden — sonst behalten sie
    # ihre eigenen Handler ("INFO: ... GET /..." ohne Zeit/Name) und ignorieren
    # LOG_LEVEL komplett.
    for _name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        _lg = logging.getLogger(_name)
        _lg.handlers = []
        _lg.propagate = True
        _lg.setLevel(level)
    # Externe Libraries leiser stellen
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Gibt einen konfigurierten Logger zurueck."""
    _configure_once()
    return logging.getLogger(name)
