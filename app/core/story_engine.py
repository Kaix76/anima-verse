"""Story Arc Engine — koordinierte Multi-Character Storylines.

Generiert, verfolgt und schliesst Story Arcs ab, die sich ueber mehrere
Interaktionen entwickeln. Nutzt LLM-Calls via LLMQueue (Priority.LOW)
und persistiert Arcs ueber app.models.story_arcs.

Arc-Kontext wird in bestehende Prompts (Proactive, Social Dialog) injiziert —
keine zusaetzlichen LLM-Calls fuer die Injektion.

Konfiguration:
    STORY_ENGINE_ENABLED=true
    STORY_ENGINE_MAX_ACTIVE_ARCS=2
    STORY_ENGINE_COOLDOWN_HOURS=6
    STORY_ENGINE_MAX_BEATS=5
    STORY_ENGINE_BEAT_IMAGES=true
"""
import json
import os
from typing import Any, Dict, Optional

from app.core.log import get_logger

logger = get_logger("story_engine")

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------
ENABLED = os.environ.get("STORY_ENGINE_ENABLED", "true").lower() in ("true", "1", "yes")
MAX_ACTIVE_ARCS = int(os.environ.get("STORY_ENGINE_MAX_ACTIVE_ARCS", "2"))
COOLDOWN_HOURS = float(os.environ.get("STORY_ENGINE_COOLDOWN_HOURS", "6"))
MAX_BEATS = int(os.environ.get("STORY_ENGINE_MAX_BEATS", "5"))
BEAT_IMAGES = os.environ.get("STORY_ENGINE_BEAT_IMAGES", "true").lower() in ("true", "1", "yes")
BEAT_IMAGE_DEFAULT = os.environ.get("STORY_ENGINE_IMAGEGEN_DEFAULT", "").strip()  # "workflow:Name" or "backend:Name"
BEAT_IMAGE_FACESWAP = os.environ.get("STORY_ENGINE_BEAT_FACESWAP", "false").lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# StoryArcEngine
# ---------------------------------------------------------------------------
class StoryArcEngine:
    """Singleton fuer die Story Arc Verwaltung."""

    def __init__(self):
        self.enabled = ENABLED
        logger.info("StoryArcEngine initialisiert (enabled=%s)", self.enabled)

    # ------------------------------------------------------------------
    # Arc Generation — LLM analysiert Characters und erzeugt neuen Arc
    # ------------------------------------------------------------------
    def generate_arc(self) -> Optional[Dict[str, Any]]:
        """Generiert einen neuen Story Arc via LLM.

        Prueft vorher ob Generation erlaubt ist (Cooldown, max aktive Arcs).
        Analysiert Character-Konstellationen und erzeugt einen passenden Arc.
        """
        if not self.enabled:
            return None

        from app.models.story_arcs import can_generate, create_arc

        if not can_generate(MAX_ACTIVE_ARCS, COOLDOWN_HOURS):
            logger.debug("Generation nicht erlaubt (Cooldown/max aktive Arcs)")
            return None

        # Characters + Beziehungen laden
        from app.models.character import (
            list_available_characters,
            get_character_personality,
            get_character_config,
            get_character_current_location)
        from app.models.world import get_location_name

        characters = list_available_characters()
        if len(characters) < 2:
            logger.debug("Weniger als 2 Characters — kein Arc moeglich")
            return None

        char_infos = []
        for name in characters:
            personality = get_character_personality(name) or ""
            loc_id = get_character_current_location(name)
            loc_name = get_location_name(loc_id) if loc_id else "unbekannt"
            char_infos.append(
                f"- {name}: {personality[:200]}. Aktueller Ort: {loc_name}"
            )

        characters_text = "\n".join(char_infos)

        from app.core.prompt_templates import render_task
        sys_prompt, user_prompt = render_task(
            "story_arc_generation",
            characters_text=characters_text,
            max_beats=MAX_BEATS)

        # LLM-Call via Queue
        llm_result = self._llm_call(characters[0], sys_prompt, user_prompt)
        if not llm_result:
            return None

        # JSON parsen
        arc_data = self._parse_json(llm_result)
        if not arc_data:
            logger.error("Arc-Generation: ungueltige LLM-Antwort")
            return None

        # Participants validieren
        participants = arc_data.get("participants", [])
        participants = [p for p in participants if p in characters]
        if len(participants) < 2:
            logger.warning("Zu wenig gueltige Participants: %s", participants)
            return None

        arc = create_arc(
            title=arc_data.get("title", "Unbenannter Arc"),
            participants=participants,
            seed=arc_data.get("seed", ""),
            tension=arc_data.get("tension", 1),
            first_beat_hint=arc_data.get("first_beat_hint", ""),
            max_beats=arc_data.get("max_beats", MAX_BEATS))
        logger.info("Neuer Arc generiert: '%s' mit %s", arc["title"], participants)
        return arc

    # ------------------------------------------------------------------
    # Arc Advancement — nach einer Interaktion den Arc weiterentwickeln
    # ------------------------------------------------------------------
    def advance_arc(
        self, arc_id: str, interaction_summary: str
    ) -> Optional[Dict[str, Any]]:
        """Entwickelt einen Arc nach einer Interaktion weiter."""
        if not self.enabled:
            return None

        from app.models.story_arcs import get_arc, advance_arc

        arc = get_arc(arc_id)
        if not arc or arc.get("status") != "active":
            return None

        beat_count = len(arc.get("beats", []))
        max_beats = arc.get("max_beats", MAX_BEATS)

        from app.core.prompt_templates import render_task
        sys_prompt, user_prompt = render_task(
            "story_arc_advancement",
            arc_title=arc["title"],
            participants=", ".join(arc.get("participants", [])),
            seed=arc.get("seed", ""),
            current_state=arc.get("current_state", ""),
            interaction_summary=interaction_summary,
            beat_count=beat_count,
            max_beats=max_beats,
            tension=arc.get("tension", 1))

        participants = arc.get("participants", [])
        agent = participants[0] if participants else ""
        llm_result = self._llm_call(agent, sys_prompt, user_prompt)
        if not llm_result:
            return None

        result = self._parse_json(llm_result)
        if not result:
            logger.error("Arc-Advancement: ungueltige LLM-Antwort")
            return None

        updated = advance_arc(
            arc_id=arc_id,
            beat_summary=result.get("beat_summary", interaction_summary[:200]),
            new_state=result.get("new_state", arc.get("current_state", "")),
            tension=result.get("tension", arc.get("tension", 1)),
            next_beat_hint=result.get("next_beat_hint", ""),
            resolved=result.get("resolved", False))

        if updated and updated.get("status") == "resolved":
            self._on_arc_resolved(updated)

        # Relationship Graph: leichter Einfluss pro Beat
        if updated:
            self._update_relationships_from_beat(updated,
                result.get("tension", arc.get("tension", 1)),
                result.get("beat_summary", interaction_summary[:200]))

        # Beat-Bild generieren (best-effort, kein Fehler bei Ausfall)
        if updated and BEAT_IMAGES:
            beat_num = len(updated.get("beats", []))
            beat_summary = result.get("beat_summary", interaction_summary[:200])
            image_info = self._generate_beat_image(updated, beat_summary)
            if image_info:
                from app.models.story_arcs import attach_beat_image
                attach_beat_image(arc_id, beat_num, image_info)
                logger.info("Beat-Bild gespeichert: Arc '%s' Beat %d -> %s",
                            updated.get("title"), beat_num, image_info.get("filename"))

        return updated

    # ------------------------------------------------------------------
    # Arc Resolution — Knowledge-Eintraege fuer alle Teilnehmer
    # ------------------------------------------------------------------
    def resolve_arc(
        self, arc_id: str
    ) -> Optional[Dict[str, Any]]:
        """Schliesst einen Arc explizit ab und schreibt Knowledge."""
        if not self.enabled:
            return None

        from app.models.story_arcs import get_arc, resolve_arc as _resolve

        arc = get_arc(arc_id)
        if not arc or arc.get("status") != "active":
            return None

        participants = arc.get("participants", [])
        beats_text = "\n".join(
            f"  Beat {b['beat']}: {b['summary']}"
            for b in arc.get("beats", [])
        )

        from app.core.prompt_templates import render_task
        sys_prompt, user_prompt = render_task(
            "story_arc_resolve",
            arc_title=arc["title"],
            participants=", ".join(participants),
            beats_text=beats_text,
            current_state=arc.get("current_state", ""))

        agent = participants[0] if participants else ""
        llm_result = self._llm_call(agent, sys_prompt, user_prompt)
        if not llm_result:
            return None

        result = self._parse_json(llm_result)
        if not result:
            logger.error("Arc-Resolution: ungueltige LLM-Antwort")
            return None

        updated = _resolve(
            arc_id=arc_id,
            resolution=result.get("resolution", ""),
            character_outcomes=result.get("character_outcomes"),
            sequel_seed=result.get("sequel_seed", ""))

        if updated:
            self._write_knowledge(updated, result.get("character_outcomes", {}))

        return updated

    # ------------------------------------------------------------------
    # Arc Context Injection — Text fuer System-Prompt
    # ------------------------------------------------------------------
    def inject_arc_context(self, character_name: str) -> str:
        """Gibt formatierten Arc-Kontext fuer den System-Prompt zurueck.

        Wird in Proactive und Social Dialog Prompts eingebettet.
        Kein zusaetzlicher LLM-Call.
        """
        if not self.enabled:
            return ""

        from app.models.story_arcs import get_active_arcs

        arcs = get_active_arcs(character_name)
        if not arcs:
            return ""

        parts = ["[Active Story Arcs]"]
        for arc in arcs[:3]:  # Max 3 Arcs anzeigen
            beats = arc.get("beats", [])
            last_beat = beats[-1]["summary"] if beats else arc.get("seed", "")
            hint = arc.get("next_beat_hint", "")
            parts.append(
                f'- "{arc["title"]}" (Spannung: {arc.get("tension", 1)}/5, '
                f'Beat {len(beats)}/{arc.get("max_beats", MAX_BEATS)})\n'
                f'  Teilnehmer: {", ".join(arc.get("participants", []))}\n'
                f'  Stand: {arc.get("current_state", last_beat)}'
                + (f"\n  Hinweis: {hint}" if hint else "")
            )

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Interne Hilfsmethoden
    # ------------------------------------------------------------------
    def _llm_call(self, character_name: str, system_prompt: str,
                  user_prompt: str) -> Optional[str]:
        """Fuehrt einen LLM-Call via Router (Task: consolidation) durch."""
        from app.core.llm_router import llm_call

        try:
            response = llm_call(
                task="consolidation",
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                agent_name=character_name)
            return (response.content or "").strip()
        except Exception as e:
            logger.error("LLM-Call fehlgeschlagen: %s", e)
            return None

    def _parse_json(self, text: str) -> Optional[Dict[str, Any]]:
        """Parst JSON aus LLM-Antwort (mit Toleranz fuer Markdown-Bloecke)."""
        # Markdown-Code-Block entfernen
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            # Erste und letzte Zeile (```) entfernen
            lines = [l for l in lines if not l.strip().startswith("```")]
            cleaned = "\n".join(lines)

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Versuche JSON-Objekt zu extrahieren
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(cleaned[start:end + 1])
                except json.JSONDecodeError:
                    pass
        logger.warning("JSON-Parse fehlgeschlagen: %s", text[:200])
        return None

    def _generate_beat_image(
        self, arc: Dict[str, Any], beat_summary: str
    ) -> Optional[Dict[str, Any]]:
        """Generiert ein Szenen-Bild fuer einen Arc-Beat (best-effort)."""
        try:
            import json as _json
            import re
            from app.core.dependencies import get_skill_manager
            from app.models.character import get_character_config, get_character_appearance
            from app.models.character_template import resolve_profile_tokens, get_template
            from app.routes.chat import _generate_image_prompt

            participants = arc.get("participants", [])
            if not participants:
                return None

            primary_char = participants[0]
            agent_config = get_character_config(primary_char)

            appearances = []
            for char_name in participants:
                char_app = get_character_appearance(char_name)
                if char_app and "{" in char_app:
                    profile = get_character_config(char_name)
                    tmpl = get_template(profile.get("template", "human-default"))
                    char_app = resolve_profile_tokens(
                        char_app, profile, template=tmpl, target_key="character_appearance"
                    )
                if char_app:
                    appearances.append({"name": char_name, "appearance": char_app})

            setting_context = (
                f"Story Arc: {arc.get('title', '')}\n"
                f"Setting: {arc.get('current_state', '')}"
            )
            image_prompt = _generate_image_prompt(
                beat_summary, appearances,
                setting_context=setting_context,
                agent_config=agent_config)
            if not image_prompt:
                return None

            sm = get_skill_manager()
            img_skill = None
            for skill in sm.skills:
                if skill.__class__.__name__ == "ImageGenerationSkill":
                    img_skill = skill
                    break

            if not img_skill:
                return None

            input_data = {
                "prompt": image_prompt,
                "agent_name": primary_char,
                "user_id": "",
                "set_profile": False,
                "skip_gallery": True,
                "auto_enhance": False,
                "force_faceswap": BEAT_IMAGE_FACESWAP,
            }
            # Default-Backend/Workflow aus .env
            if BEAT_IMAGE_DEFAULT:
                _type, _, _name = BEAT_IMAGE_DEFAULT.partition(":")
                if _type == "workflow" and _name:
                    input_data["workflow"] = _name
                elif _type == "backend" and _name:
                    input_data["backend"] = _name
            input_json = _json.dumps(input_data)
            result_text = img_skill.execute(input_json)

            char_urls = re.findall(r'!\[.*?\]\((\/characters\/[^)]+)\)', result_text)
            if not char_urls:
                return None

            url = char_urls[0]
            filename = url.split("/")[-1].split("?")[0]
            return {
                "filename": filename,
                "character": primary_char,
                "prompt_used": image_prompt,
            }
        except Exception as e:
            logger.error("Beat-Bild-Generierung fehlgeschlagen: %s", e)
            return None

    def _on_arc_resolved(self, arc: Dict[str, Any]) -> None:
        """Callback wenn ein Arc durch Advancement aufgeloest wird."""
        outcomes = arc.get("character_outcomes", {})
        resolution = arc.get("resolution", "")
        if not resolution:
            resolution = f"Story '{arc['title']}' abgeschlossen nach {len(arc.get('beats', []))} Beats."
        self._write_knowledge(arc, outcomes)

        # Abschluss-Update: Tension des Gesamtarcs bestimmt finalen Einfluss
        # Etwas staerker als einzelne Beats, aber immer noch gedaempft
        final_tension = arc.get("tension", 3)
        self._update_relationships_from_beat(arc, final_tension,
            f"Arc resolved: {resolution[:150]}")

    def _update_relationships_from_beat(
        self, arc: Dict[str, Any], tension: int, beat_summary: str) -> None:
        """Leichter Relationship-Einfluss pro Story Beat.

        Tension steuert die Richtung:
          1-2 = positiv (friedlich, kooperativ)
          3   = neutral (kein Einfluss)
          4-5 = negativ (Konflikt, Spannung)

        Werte sind bewusst sehr niedrig gehalten, da Story Arcs
        unkontrolliert vom LLM generiert werden.
        """
        from app.models.relationship import record_interaction

        participants = arc.get("participants", [])
        if len(participants) < 2:
            return

        # Sentiment aus Tension ableiten: 1→+0.01, 2→+0.005, 3→0, 4→-0.005, 5→-0.01
        sentiment = (3 - tension) * 0.005  # Range: +0.01 bis -0.01

        for i, char_a in enumerate(participants):
            for char_b in participants[i + 1:]:
                try:
                    record_interaction(
                        char_a=char_a,
                        char_b=char_b,
                        interaction_type="story_arc_beat",
                        summary=f"[{arc.get('title', 'Arc')}] {beat_summary[:120]}",
                        strength_delta=0.5,
                        sentiment_delta_a=sentiment,
                        sentiment_delta_b=sentiment)
                except Exception as e:
                    logger.debug("Relationship-Update fehlgeschlagen: %s", e)

    def _write_knowledge(
        self, arc: Dict[str, Any], outcomes: Dict[str, str]
    ) -> None:
        """Schreibt Knowledge-Eintraege fuer alle Arc-Teilnehmer."""
        from app.models.memory import upsert_relationship_memory

        participants = arc.get("participants", [])
        title = arc.get("title", "Story Arc")

        for char in participants:
            others = [p for p in participants if p != char]
            outcome = outcomes.get(char, f"Beteiligte an Story '{title}'")
            for other in others:
                fact = f"[Story: {title}] {outcome}"
                upsert_relationship_memory(char, other, fact)
                logger.debug("Knowledge: %s -> %s: %s", char, other, fact[:80])

        from app.models.story_arcs import cleanup_old_arcs
        removed = cleanup_old_arcs()
        if removed:
            logger.info("%d alte Arcs bereinigt", removed)


# ---------------------------------------------------------------------------
# BackgroundQueue Handler
# ---------------------------------------------------------------------------
def _handle_story_arc_generate(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Handler fuer Arc-Generierung via BackgroundQueue."""
    user_id = payload.get("user_id", "")
    if not user_id:
        return {"success": False, "error": "user_id fehlt"}

    engine = get_story_engine()
    arc = engine.generate_arc()
    if arc:
        return {"success": True, "arc": arc}
    return {"success": False, "reason": "Generation nicht moeglich oder fehlgeschlagen"}


def _handle_story_arc_advance(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Handler fuer Arc-Advancement via BackgroundQueue."""
    user_id = payload.get("user_id", "")
    arc_id = payload.get("arc_id", "")
    summary = payload.get("interaction_summary", "")

    if not arc_id:
        return {"success": False, "error": "user_id oder arc_id fehlt"}

    engine = get_story_engine()
    updated = engine.advance_arc(arc_id, summary)
    if updated:
        return {"success": True, "arc": updated}
    return {"success": False, "reason": "Advancement fehlgeschlagen"}


def _handle_story_arc_resolve(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Handler fuer Arc-Resolution via BackgroundQueue."""
    user_id = payload.get("user_id", "")
    arc_id = payload.get("arc_id", "")

    if not arc_id:
        return {"success": False, "error": "user_id oder arc_id fehlt"}

    engine = get_story_engine()
    updated = engine.resolve_arc(arc_id)
    if updated:
        return {"success": True, "arc": updated}
    return {"success": False, "reason": "Resolution fehlgeschlagen"}


# ---------------------------------------------------------------------------
# Registrierung + Singleton
# ---------------------------------------------------------------------------
_story_engine: Optional[StoryArcEngine] = None


def get_story_engine() -> StoryArcEngine:
    """Gibt die globale StoryArcEngine-Instanz zurueck."""
    global _story_engine
    if _story_engine is None:
        _story_engine = StoryArcEngine()
    return _story_engine


def register_story_engine_handler():
    """Registriert Story-Engine-Handler bei der BackgroundQueue."""
    from app.core.background_queue import get_background_queue
    bq = get_background_queue()
    bq.register_handler("story_arc_generate", _handle_story_arc_generate)
    bq.register_handler("story_arc_advance", _handle_story_arc_advance)
    bq.register_handler("story_arc_resolve", _handle_story_arc_resolve)
    logger.info("BackgroundQueue-Handler registriert")
