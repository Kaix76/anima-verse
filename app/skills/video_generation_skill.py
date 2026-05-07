"""Video Generation Skill - Erzeugt ein Bild und animiert es zu einem Video.

Ablauf:
  1. Bild generieren via ImageGenerationSkill (voller Flow inkl. Analyse)
  2. Bild mit dem Animation Service animieren (ComfyUI lokal oder Together.ai)

Per-Character Konfiguration (skills/video_generation.json):
  - imagegen_backend:    Backend-Name fuer die Bildgenerierung
  - imagegen_workflow:   Workflow-Name fuer die Bildgenerierung
  - imagegen_model:      Model-Override fuer die Bildgenerierung
  - imagegen_loras:      LoRA-Liste fuer die Bildgenerierung [{name, strength}, ...]
  - animate_service:     Animation-Service ("comfy" oder "together")
  - animate_loras_high:  LoRA-Liste fuer Animation High-Lighting [{name, strength}, ...]
  - animate_loras_low:   LoRA-Liste fuer Animation Low-Lighting [{name, strength}, ...]
"""

import json
import re
import time
from typing import Any, Dict

from .base import BaseSkill, ToolSpec

from app.core.log import get_logger
from app.core.tool_formats import format_example
from app.models.character import (
    get_character_images_dir,
    add_character_image_metadata)

logger = get_logger("video_gen")


class VideoGenerationSkill(BaseSkill):
    """
    Video Generation Skill.

    Generiert ein Bild ueber den ImageGenerationSkill und animiert es
    anschliessend mit dem konfigurierten Animation Service.

    Input (JSON):
        prompt:        Bildbeschreibung (wie bei ImageGenerator)
        action_prompt: Beschreibung der Bewegung/Aktion fuer die Animation
        character_name:    Character-Name
        user_id:       User-ID
    """

    SKILL_ID = "video_generation"
    ALWAYS_LOAD = True  # Aktivierung per Character
    DEFERRED = True  # Video wird erst nach Chat-Antwort generiert

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        from app.core.prompt_templates import load_skill_meta
        meta = load_skill_meta("video_generation")
        self.name = meta["name"]
        self.description = meta["description"]

        self._defaults = {
            "imagegen_backend": "",
            "imagegen_workflow": "",
            "imagegen_model": "",
            "imagegen_loras": [],
            "animate_service": "",
            "animate_loras_high": [],
            "animate_loras_low": [],
        }

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def get_config_fields(self) -> Dict[str, Dict[str, Any]]:
        """Config-Felder werden dynamisch im Frontend gerendert (Dropdowns).

        Gibt leer zurueck — die eigentliche Config-UI wird via
        _loadVideoGenConfig() im Frontend aufgebaut (analog zu ImageGen).
        """
        return {}

    # ------------------------------------------------------------------
    # ImageGen Skill Referenz
    # ------------------------------------------------------------------

    @staticmethod
    def _get_image_skill():
        """Holt eine Referenz auf den ImageGenerationSkill aus dem SkillManager."""
        from app.skills.image_generation_skill import ImageGenerationSkill
        from app.core.dependencies import get_skill_manager
        _sm = get_skill_manager()
        for skill in _sm.skills:
            if isinstance(skill, ImageGenerationSkill):
                return skill
        return None

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    def execute(self, prompt: str) -> str:
        """
        Generiert ein Video: Bild erzeugen -> analysieren -> animieren.

        Args:
            prompt: JSON mit prompt, action_prompt, character_name, user_id
                    (oder einfacher Text-Prompt)

        Returns:
            String mit Bild- und Video-Links oder Fehlermeldung
        """
        from app.core.task_queue import get_task_queue

        # 1. Input parsen
        ctx = self._parse_base_input(prompt)
        image_prompt = ctx.get("prompt", ctx.get("input", prompt))
        action_prompt = ctx.get("action_prompt", "")
        character_name = ctx.get("agent_name", "").strip()
        user_id = ctx.get("user_id", "").strip()
        rp_context = ctx.get("rp_context", "")

        if not image_prompt or not image_prompt.strip():
            return "Fehler: Bitte gib eine Bildbeschreibung ein."
        if not character_name:
            return "Fehler: Agent-Name fehlt."
        if not action_prompt or not action_prompt.strip():
            return "Fehler: action_prompt fehlt (Beschreibung der Bewegung/Aktion fuer die Animation)."

        # 2. Per-Character Config laden
        cfg = self._get_effective_config(character_name)

        # 3. ImageGenerationSkill holen
        image_skill = self._get_image_skill()
        if not image_skill:
            return "Fehler: ImageGenerationSkill ist nicht verfuegbar."

        # Task im Queue-System registrieren
        _tq = get_task_queue()
        _track_id = _tq.track_start(
            "video_generation", "Video generieren", agent_name=character_name)

        try:
            # ============================================================
            # Schritt 1: Bild generieren (voller ImageGen-Flow)
            # ============================================================
            logger.info("=" * 80)
            logger.info("VIDEOGENERIERUNG GESTARTET")
            logger.info("=" * 80)
            logger.info("Agent: %s", character_name)
            logger.info("Image Prompt: %s", image_prompt)
            logger.info("Action Prompt: %s", action_prompt)

            _tq.track_update_label(_track_id, "Bild generieren")

            imagegen_input = {
                "prompt": image_prompt,
                "agent_name": character_name,
                "user_id": user_id,
                "set_profile": False,
                "skip_gallery": False,
                "auto_enhance": True,
                "rp_context": rp_context,
            }

            # Per-Character ImageGen-Overrides anwenden
            _backend = cfg.get("imagegen_backend", "")
            _workflow = cfg.get("imagegen_workflow", "")
            _model = cfg.get("imagegen_model", "")
            _loras = cfg.get("imagegen_loras")

            if _backend:
                imagegen_input["backend"] = _backend
            if _workflow:
                imagegen_input["workflow"] = _workflow
            if _model:
                imagegen_input["model_override"] = _model
            if _loras:
                imagegen_input["loras"] = _loras

            img_result = image_skill.execute(json.dumps(imagegen_input))

            # Dateiname aus dem Ergebnis extrahieren
            # Format: ![Generated Image 1](/characters/Name/images/filename.png?user_id=...)
            match = re.search(r'/images/([^?)\n]+)', img_result)
            if not match:
                logger.error("Kein Bild im ImageGen-Ergebnis gefunden: %s", img_result[:300])
                _tq.track_finish(_track_id, error="Bildgenerierung fehlgeschlagen")
                return img_result  # Fehlermeldung von ImageGen durchreichen

            image_filename = match.group(1)
            images_dir = get_character_images_dir(character_name)
            image_path = images_dir / image_filename

            if not image_path.exists():
                logger.error("Generiertes Bild nicht gefunden: %s", image_path)
                _tq.track_finish(_track_id, error="Bild nicht gefunden")
                return f"Fehler: Generiertes Bild nicht gefunden: {image_filename}"

            logger.info("Bild generiert: %s", image_filename)

            # ============================================================
            # Schritt 2: Bild animieren
            # ============================================================
            _tq.track_update_label(_track_id, "Video animieren")
            logger.info("ANIMATION STARTEN")
            logger.info("Source: %s", image_path)
            logger.info("Action: %s", action_prompt)

            from app.skills.animate import animate_image

            # Video-Dateiname: gleicher Stem wie Bild + .mp4
            video_stem = image_path.stem
            video_filename = f"{video_stem}.mp4"
            video_path = images_dir / video_filename

            animate_service = cfg.get("animate_service", "")
            animate_loras_high = cfg.get("animate_loras_high") or None
            animate_loras_low = cfg.get("animate_loras_low") or None

            _anim_start = time.time()
            success = animate_image(
                source_image_path=str(image_path),
                prompt=action_prompt,
                output_path=str(video_path),
                service=animate_service,
                loras_high=animate_loras_high,
                loras_low=animate_loras_low)
            _anim_duration = time.time() - _anim_start

            if not success:
                logger.error("Animation fehlgeschlagen")
                _tq.track_finish(_track_id, error="Animation fehlgeschlagen")
                # Bild-Ergebnis trotzdem zurueckgeben
                return img_result + "\n\nFehler: Video-Animation fehlgeschlagen."

            logger.info("Animation erfolgreich (%.1fs): %s", _anim_duration, video_filename)

            # Video-Metadaten am BILD speichern (wie bei manueller Animation)
            # Die Galerie entdeckt das Video ueber die image_videos-Map ({stem}.mp4)
            # und zeigt es als Companion des Bildes an — mit dessen Beschreibung/Prompt.
            from datetime import datetime as _dt
            _meta = {
                "animate_prompt": action_prompt,
                "animate_service": animate_service or "auto",
                "animate_created_at": _dt.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "animate_duration_s": round(_anim_duration, 1),
            }
            add_character_image_metadata(character_name, image_filename, _meta)

            # Rueckgabe: nur Video-Link (Bild ist Zwischenschritt, nicht im Chat anzeigen)
            video_url = f"/characters/{character_name}/images/{video_filename}?user_id={user_id}"
            video_line = f"![Generated Video]({video_url})"

            # Caption aus dem ImageGen-Ergebnis extrahieren (falls vorhanden)
            _caption_match = re.search(r'CAPTION[^:]*:\s*(.+)', img_result)
            _caption = _caption_match.group(1).strip() if _caption_match else ""

            result_text = (
                f"AKTION: Video wurde GENERIERT und in der Galerie von {character_name} gespeichert.\n\n"
                f"{video_line}"
            )
            if _caption:
                result_text += f"\n\nCAPTION (nur zur Anzeige, NICHT als Fakt behandeln): {_caption}"

            logger.info("=" * 80)
            logger.info("VIDEOGENERIERUNG ABGESCHLOSSEN")
            logger.info("=" * 80)

            _tq.track_finish(_track_id)
            return result_text

        except Exception as e:
            error_msg = f"Videogenerierung: {e}"
            logger.error("Fehler: %s", error_msg, exc_info=True)
            _tq.track_finish(_track_id, error=error_msg)
            return f"Fehler bei {error_msg}"

    # ------------------------------------------------------------------
    # Tool Interface
    # ------------------------------------------------------------------

    def get_usage_instructions(self, format_name: str = "", **kwargs) -> str:
        if "usage_instructions" in self.config:
            return self.config["usage_instructions"]
        fmt = format_name or "tag"
        return format_example(
            fmt, self.name,
            '{"prompt": "young woman dancing at sunset on the beach", '
            '"action_prompt": "she spins around gracefully with her arms raised"}'
        )

    def as_tool(self, **kwargs) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                f"{self.description}. "
                "Input: JSON with 'prompt' (image description) and "
                "'action_prompt' (motion/action description for animation)."
            ),
            func=self.execute)
