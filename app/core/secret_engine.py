"""Secret Engine - LLM-basierte Geheimnis-Generierung.

Generiert passende Geheimnisse basierend auf:
- Character-Profil (Persoenlichkeit, Hintergrund)
- Beziehungen (Staerke, Typ, Sentiment)
- Tagesszusammenfassungen (was ist passiert)
- Bestehende Geheimnisse (Duplikat-Vermeidung)
- Memories (was der Character weiss/erlebt hat)
"""
import json
import re
from typing import Any, Dict, List

from app.core.log import get_logger

logger = get_logger("secret_engine")


def generate_secrets(character_name: str,
    count: int = 2) -> List[Dict[str, Any]]:
    """Generiert neue Geheimnisse fuer einen Character via LLM.

    Args:
        user_id: User-ID
        character_name: Character-Name
        count: Gewuenschte Anzahl neuer Geheimnisse (1-3)

    Returns:
        Liste der neu erstellten Geheimnisse (bereits gespeichert)
    """
    from app.core.llm_router import llm_call
    from app.models.character import get_character_profile

    count = max(1, min(3, count))

    # Kontext sammeln
    context = _build_generation_context(character_name)
    if not context:
        logger.warning("Kein Kontext fuer %s — zu wenig Daten?", character_name)
        # Trotzdem versuchen mit minimalem Kontext
        profile = get_character_profile(character_name)
        context = f"Character: {character_name}\nPersonality: {profile.get('character_personality', 'unknown')}"

    from app.core.prompt_templates import render_task
    system_prompt, user_prompt = render_task(
        "secret_generation",
        character_name=character_name,
        context=context,
        count=count)

    try:
        response = llm_call(
            task="secret_generation",
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            agent_name=character_name)

        raw = re.sub(r'<SPECIAL_\d+>|<\|[A-Z_]+\|>', '', response.content).strip()
        secrets_data = _parse_llm_response(raw)

        if not secrets_data:
            logger.warning("LLM hat keine gueltige Antwort geliefert fuer %s", character_name)
            return []

        # Geheimnisse speichern
        from app.models.secrets import add_secret
        created = []
        for sd in secrets_data[:count]:
            secret = add_secret(
                character_name=character_name,
                content=sd.get("content", ""),
                category=sd.get("category", "personal"),
                severity=sd.get("severity", 2),
                related_characters=sd.get("related_characters", []),
                related_location=sd.get("related_location", ""),
                consequences_if_revealed=sd.get("consequences_if_revealed", ""),
                source="generated")
            created.append(secret)
            logger.info("Generiertes Geheimnis fuer %s: %s", character_name, secret["id"])

        return created

    except Exception as e:
        logger.error("Geheimnis-Generierung fehlgeschlagen fuer %s: %s", character_name, e)
        return []


def _build_generation_context(character_name: str) -> str:
    """Sammelt den vollstaendigen Kontext fuer die Generierung."""
    parts = []

    # 1. Character-Profil
    from app.models.character import get_character_profile
    profile = get_character_profile(character_name)
    personality = profile.get("character_personality", "")
    task = profile.get("character_task", "")
    feeling = profile.get("current_feeling", "")
    location = profile.get("current_location", "")

    if personality:
        parts.append(f"Personality: {personality}")
    if task:
        parts.append(f"Role/Task: {task}")
    if feeling:
        parts.append(f"Current mood: {feeling}")

    # 2. Beziehungen
    try:
        from app.models.relationship import build_relationship_prompt_section
        rel_section = build_relationship_prompt_section(character_name)
        if rel_section:
            parts.append(rel_section.strip())
    except Exception:
        pass

    # 3. Tagesszusammenfassungen (alle Partner — partner-Label im Prefix)
    try:
        from app.utils.history_manager import get_recent_daily_summaries
        summaries = get_recent_daily_summaries(character_name, days=5)
        if summaries:
            summary_lines = []
            for s in summaries[-5:]:
                date_label = s.get('date', '?')
                partner = s.get('partner', '') or ''
                if partner:
                    summary_lines.append(
                        f"- {date_label} with {partner}: {s.get('summary', '')}"
                    )
                else:
                    summary_lines.append(
                        f"- {date_label}: {s.get('summary', '')}"
                    )
            parts.append("Recent events:\n" + "\n".join(summary_lines))
    except Exception:
        pass

    # 4. Relevante Memories (top 10)
    try:
        from app.models.memory import retrieve_relevant_memories
        memories = retrieve_relevant_memories(character_name, context="secrets personality history", limit=10)
        if memories:
            mem_lines = [f"- {m.get('content', '')}" for m in memories]
            parts.append("Key memories:\n" + "\n".join(mem_lines))
    except Exception:
        pass

    # 5. Bestehende Geheimnisse (zur Duplikat-Vermeidung)
    try:
        from app.models.secrets import list_secrets
        existing = list_secrets(character_name)
        if existing:
            existing_lines = [f"- {s.get('content', '')}" for s in existing]
            parts.append("ALREADY EXISTING secrets (DO NOT repeat these):\n" + "\n".join(existing_lines))
    except Exception:
        pass

    return "\n\n".join(parts)


def _parse_llm_response(raw: str) -> List[Dict[str, Any]]:
    """Parsed die LLM-Antwort als JSON-Array."""
    # JSON-Block extrahieren (kann in Markdown-Codeblock sein)
    json_match = re.search(r'\[[\s\S]*\]', raw)
    if not json_match:
        logger.warning("Kein JSON-Array in LLM-Antwort gefunden")
        return []

    try:
        data = json.loads(json_match.group())
        if not isinstance(data, list):
            return []

        # Validierung
        valid = []
        for item in data:
            if not isinstance(item, dict):
                continue
            content = item.get("content", "").strip()
            if not content:
                continue
            # Defaults setzen
            item["content"] = content
            item["category"] = item.get("category", "personal")
            if item["category"] not in ("personal", "relationship", "location", "criminal"):
                item["category"] = "personal"
            item["severity"] = max(1, min(5, int(item.get("severity", 2))))
            item["related_characters"] = item.get("related_characters", [])
            item["related_location"] = item.get("related_location", "")
            item["consequences_if_revealed"] = item.get("consequences_if_revealed", "")
            valid.append(item)

        return valid

    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("JSON-Parse fehlgeschlagen: %s", e)
        return []
