"""Diary routes — Timeline / Daily Diary API.

The diary is generated on-demand from existing data sources,
not stored in real-time. The Generate button rebuilds the view.
"""
from fastapi import APIRouter, HTTPException, Request
from typing import Any, Dict, List, Optional

from app.core.log import get_logger
from app.models.diary import (
    generate_for_day,
    get_available_dates_fast,
    has_daily_summary,
    build_daily_summary_input,
    add_summary,
    ENTRY_TYPES,
    ENTRY_ICONS)

logger = get_logger("diary_route")

router = APIRouter(prefix="/diary", tags=["diary"])


@router.get("/{user_id}/{character_name}")
def get_diary_entries(character_name: str,
    type: Optional[str] = None,
    date: Optional[str] = None,
    limit: int = 100,
    offset: int = 0) -> Dict[str, Any]:
    """Get diary entries for a character, generated from all sources.

    Query params:
        type: Filter by entry type
        date: Filter by date (YYYY-MM-DD), default today
        limit: Max entries
        offset: Pagination offset
    """
    from datetime import datetime
    if not date or date == "all":
        # Alle Tage: alle verfuegbaren Tage aggregieren
        all_dates = get_available_dates_fast(character_name)
        entries = []
        for d in all_dates:
            entries.extend(generate_for_day(character_name, d))
    else:
        entries = generate_for_day(character_name, date)

    if type:
        entries = [e for e in entries if e.get("type") == type]

    # Newest first — deterministisch per Timestamp sortieren (auch ueber Tage).
    entries.sort(key=lambda e: e.get("timestamp", "") or "", reverse=True)

    return {
        "entries": entries[offset:offset + limit],
        "types": ENTRY_TYPES,
        "icons": ENTRY_ICONS,
    }


@router.get("/{user_id}/{character_name}/dates")
def get_diary_dates(character_name: str) -> List[str]:
    """Get list of dates that have data (newest first)."""
    return get_available_dates_fast(character_name)


@router.post("/{user_id}/{character_name}/summary")
async def generate_daily_summary(character_name: str, request: Request
) -> Dict[str, Any]:
    """Generate a daily summary (LLM Tagebucheintrag) in background.

    Body: {"date": "2026-03-30"} (optional, defaults to today)
    Returns immediately — summary appears on next panel refresh.
    """
    data = await request.json()
    date = data.get("date")

    if has_daily_summary(character_name, date):
        raise HTTPException(status_code=409, detail="Tagesrueckblick existiert bereits")

    day_text = build_daily_summary_input(character_name, date)
    if not day_text:
        raise HTTPException(status_code=404, detail="Keine Ereignisse fuer diesen Tag")

    import asyncio
    asyncio.get_event_loop().run_in_executor(
        None, _generate_summary_sync, character_name, date or "", day_text)

    return {"status": "generating"}


def _generate_summary_sync(character_name: str, date: str, day_text: str):
    """Background: LLM generates personal diary entry from day's events."""
    from app.models.character import get_character_profile
    from app.core.llm_router import llm_call

    profile = get_character_profile(character_name)
    char_name = profile.get("character_name", character_name)
    personality = profile.get("character_personality", "")

    system_prompt = (
        f"Du bist {char_name}.\n"
        f"{f'Persoenlichkeit: {personality}' if personality else ''}\n\n"
        f"Schreibe einen kurzen Tagebucheintrag (3-5 Saetze) basierend auf den "
        f"folgenden Ereignissen deines Tages. Schreibe in der Ich-Form, "
        f"persoenlich und emotional. Fasse die wichtigsten Momente zusammen."
    )

    try:
        response = llm_call(
            task="consolidation",
            system_prompt=system_prompt,
            user_prompt=day_text,
            agent_name=character_name)
        summary = (response.content or "").strip() if response else ""
        if summary:
            if not date:
                from datetime import datetime
                date = datetime.now().strftime("%Y-%m-%d")
            add_summary(character_name, summary, date)
            logger.info("Diary summary generated: %s/%s", character_name)
    except Exception as e:
        logger.error("Diary summary error for %s: %s", character_name, e)
