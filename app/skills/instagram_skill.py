"""Instagram Skill - Erstellt Instagram-Posts mit KI-generiertem Bild und automatischem Caption"""
import base64
import json
import os
import re
from typing import Any, Dict, Optional

from .base import BaseSkill, ToolSpec

from app.core.log import get_logger
from app.core.llm_queue import get_llm_queue, Priority
from app.core.task_queue import get_task_queue
from app.core.tool_formats import format_example
from app.models.instagram import create_post, get_instagram_dir
from app.models.character import (
    get_character_images_dir,
    get_character_personality,
    get_character_appearance,
    get_character_profile,
    get_character_current_location,
    get_character_current_activity,
    save_character_current_feeling,
    list_available_characters)
from app.models.character_template import resolve_profile_tokens, get_template
from app.models.world import get_location, get_activity

logger = get_logger("instagram_skill")

# Note: get_location(name) and get_activity(name)
# are now User-Level (no character_name parameter)


class InstagramSkill(BaseSkill):
    """
    Erstellt Instagram-Posts fuer Agenten.

    3-Schritt-Flow:
    1. Bild generieren via ImageGenerationSkill (ohne Galerie-Eintrag)
    2. Generiertes Bild vom LLM analysieren lassen (Vision)
    3. Natuerlichen Instagram-Caption aus der Analyse generieren

    Input: Bildbeschreibung (z.B. "Sonnenuntergang am Meer")
    """

    SKILL_ID = "instagram"
    DEFERRED = True  # Post wird erst nach Chat-Antwort erstellt

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        self.name = os.environ.get('SKILL_INSTAGRAM_NAME', 'Instagram')
        self.description = os.environ.get(
            'SKILL_INSTAGRAM_DESCRIPTION',
            'Creates an Instagram post with a generated image and automatic caption'
        )

        self._defaults = {
            "enabled": True,
            "caption_style": os.environ.get('SKILL_INSTAGRAM_CAPTION_STYLE', 'casual'),
            "hashtag_count": int(os.environ.get('SKILL_INSTAGRAM_HASHTAG_COUNT', '5')),
            "caption_language": os.environ.get('SKILL_INSTAGRAM_CAPTION_LANGUAGE', 'de'),
            "popularity": int(os.environ.get('SKILL_INSTAGRAM_DEFAULT_POPULARITY', '50')),
            "imagegen_workflow": os.environ.get('SKILL_INSTAGRAM_IMAGEGEN_DEFAULT', ''),
            "post_cooldown_hours": int(os.environ.get('SKILL_INSTAGRAM_POST_COOLDOWN_HOURS', '12')),
        }

        logger.info("Instagram Skill initialized")

    def _get_vision_llm_config(self, character_name: str) -> Dict[str, Any]:
        """Loads Vision LLM config via Router (Task: image_recognition)."""
        from app.core.llm_router import resolve_llm
        instance = resolve_llm("image_recognition", agent_name=character_name)
        if instance:
            return {
                "model": instance.model,
                "api_base": instance.api_base,
                "api_key": instance.api_key,
            }

        logger.warning("No image_recognition LLM instance available. Check LLM_DEFAULT_IMAGE_RECOGNITION_MODEL in .env")
        return None

    def _get_image_skill(self):
        """Holt eine Referenz auf den ImageGenerationSkill aus dem SkillManager."""
        # Inline import to avoid circular dependency (instagram_skill <-> image_generation_skill)
        from app.skills.image_generation_skill import ImageGenerationSkill

        from app.core.dependencies import get_skill_manager  # lazy: circular import
        _sm = get_skill_manager()
        for skill in _sm.skills:
            if isinstance(skill, ImageGenerationSkill):
                return skill
        return None

    def _parse_input(self, raw_input: str) -> Dict[str, Any]:
        """Parst den Input.

        Akzeptiert:
          - JSON-Felder im ctx: {"image_prompt": "...", "caption": "..."}
          - Einfacher String: nur image_prompt
          - Legacy: 'IMAGE: ... | CAPTION: ...'
        """
        ctx = self._parse_base_input(raw_input)

        # JSON-Felder bevorzugen
        image_prompt_field = (ctx.get("image_prompt") or "").strip()
        caption_field = (ctx.get("caption") or "").strip()

        full_input = ctx.get("prompt", ctx.get("input", raw_input))
        image_prompt = image_prompt_field or full_input
        caption = caption_field

        # Legacy: IMAGE: ... | CAPTION: ...
        if "|" in full_input and "CAPTION" in full_input.upper():
            parts = full_input.split("|", 1)
            image_prompt = parts[0].strip()
            cap_part = parts[1].strip()
            if cap_part.upper().startswith("CAPTION:"):
                caption = caption or cap_part[8:].strip()

        # IMAGE: Prefix entfernen
        if image_prompt.upper().startswith("IMAGE:"):
            image_prompt = image_prompt[6:].strip()

        return {
            "image_prompt": image_prompt,
            "caption": caption,
            "agent_name": ctx.get("agent_name", ""),
            "user_id": ctx.get("user_id", ""),
        }

    def _extract_filename(self, image_result: str) -> str:
        """Extrahiert den Bild-Dateinamen aus dem ImageGenerationSkill-Ergebnis."""
        match = re.search(r'/images/([^?)\n]+)', image_result)
        return match.group(1) if match else ""

    def _extract_hashtags(self, caption: str) -> list:
        """Extrahiert #hashtags aus dem Caption-Text."""
        return re.findall(r'#(\w+)', caption)

    def _generate_caption(self, image_path: str, character_name: str, image_prompt: str, current_location: Optional[str] = None, current_activity: Optional[str] = None, chat_text: str = "", draft_caption: str = "") -> Optional[str]:
        """
        Schritt 2+3: LLM analysiert das generierte Bild und schreibt einen natuerlichen Caption.

        Nutzt ein Vision-faehiges LLM um das Bild zu analysieren und einen
        authentischen Instagram-Post-Text in der Persoenlichkeit des Agenten zu verfassen.

        Args:
            image_path: Pfad zum generierten Bild
            character_name: Name des Agenten
            image_prompt: Ursprüngliche Bildbeschreibung
            current_location: Aktuelle Location des Agenten (optional)
            current_activity: Aktuelle Activity des Agenten (optional)
            chat_text: Chat-Text mit der aktuellen Situation (optional)
            draft_caption: Vom aufrufenden LLM mitgegebene Caption-Idee (optional) —
                wird als Inspiration an das Vision-LLM uebergeben, das daraus
                einen vollstaendigen Post formuliert.
        """
        from app.core.llm_client import LLMClient

        logger.info("Instagram Caption-Generierung gestartet...")
        logger.info(f"Bildpfad: {image_path}")
        logger.info(f"Agent: {character_name}")
        
        # Prüfe ob Datei existiert
        if not os.path.exists(image_path):
            logger.error(f"Bilddatei existiert nicht: {image_path}")
            return None
        
        # Bild als Base64 laden
        try:
            with open(image_path, 'rb') as f:
                image_bytes = f.read()
            base64_image = base64.b64encode(image_bytes).decode('utf-8')
            logger.info(f"Bild geladen: {len(image_bytes)} bytes -> {len(base64_image)} chars Base64")
        except Exception as e:
            logger.error(f"Fehler beim Laden des Bildes: {e}")
            return None

        # Agent-Persoenlichkeit laden
        personality = get_character_personality(character_name) or ""

        # Per-Agent Config
        cfg = self._get_effective_config(character_name)
        caption_style = cfg.get("caption_style", "casual")
        hashtag_count = cfg.get("hashtag_count", 5)
        caption_language = cfg.get("caption_language", "de")

        lang_name = "Deutsch" if caption_language == "de" else "English"

        # Build context-aware prompt with location, activity, situation and persons
        context_info = ""
        context_parts = []
        if current_location:
            loc_data = get_location(current_location)
            loc_name = loc_data.get("name", current_location) if loc_data else current_location
            loc_desc = loc_data.get("description", "") if loc_data else ""
            context_parts.append(f"- Ort: {loc_name}" + (f" ({loc_desc})" if loc_desc else ""))
        if current_activity:
            act_data = get_activity(current_activity)
            act_desc = act_data.get("description", "") if act_data else ""
            context_parts.append(f"- Aktivitaet: {current_activity}" + (f" ({act_desc})" if act_desc else ""))
        if chat_text:
            context_parts.append(f"- Aktuelle Situation: {chat_text[:500]}")
        if context_parts:
            context_info = "\n\nKONTEXT MEINES MOMENTS:\n" + "\n".join(context_parts)
            context_info += "\nNutze diesen Kontext fuer einen passenden, authentischen Post.\n"

        # Draft-Caption aus dem Thought-LLM als Inspiration einfliessen lassen
        if draft_caption:
            context_info += (
                "\n\nMEINE KURZE NOTIZ-IDEE (dient als Einstieg/Inspiration, "
                "du darfst sie umformulieren und ausbauen — nicht woertlich "
                "uebernehmen, nicht nur verkuerzen):\n"
                f"\"{draft_caption}\"\n"
            )

        # Persoenlichkeit in den Prompt einbauen
        style_description = (
            f"Your style: {personality}" if personality
            else "Your style: friendly, confident and approachable.")

        from app.core.prompt_templates import render_task
        _, prompt_text = render_task(
            "instagram_caption",
            character_name=character_name,
            style_description=style_description,
            caption_style=caption_style,
            hashtag_count=hashtag_count,
            language_name=lang_name,
            context_info=context_info)

        try:
            # Per-Agent Vision-LLM Config (with LLM Service support)
            vcfg = self._get_vision_llm_config(character_name)

            # Temperature/max_tokens aus Routing-Entry
            caption_temperature = 0.8
            caption_max_tokens = 300
            try:
                from app.core.llm_router import resolve_llm
                _ir_inst = resolve_llm("image_recognition", agent_name=character_name)
                if _ir_inst:
                    caption_temperature = _ir_inst.temperature
                    caption_max_tokens = _ir_inst.max_tokens or 300
            except Exception:
                pass

            llm = LLMClient(
                model=vcfg["model"],
                api_key=vcfg["api_key"],
                api_base=vcfg["api_base"],
                temperature=caption_temperature,
                max_tokens=caption_max_tokens,
                request_timeout=int(os.environ.get("LLM_REQUEST_TIMEOUT", "120")))
            logger.info(f"LLM initialized: {vcfg['model']} @ {vcfg['api_base'] or 'default'}")

            # Message mit Bild und Text erstellen
            image_url = f"data:image/png;base64,{base64_image}"
            message = {"role": "user", "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url", "image_url": {"url": image_url}}
            ]}

            logger.debug("Message erstellt:")
            logger.debug(f"Text: {len(prompt_text)} chars")
            logger.debug(f"Image URL: data:image/png;base64,... ({len(image_url)} chars total)")
            logger.debug(f"Content items: {len(message['content'])}")
            logger.info(f"Sende an LLM {vcfg['model']}...")
            
            from app.core.llm_queue import get_llm_queue, Priority
            response = get_llm_queue().submit(
                task_type="instagram_caption",
                priority=Priority.NORMAL,
                llm=llm,
                messages_or_prompt=[message],
                agent_name=character_name)
            caption = response.content.strip()

            # Cleanup: Artefakte und Metadaten entfernen
            caption = re.sub(r'^(?:Bildtext|Caption|Post\s*Text|Text\s*\d*)\s*:\s*', '', caption, flags=re.MULTILINE)
            caption = re.sub(r'\*\*I\s+feel\s+[^*]+\*\*\.?\s*', '', caption, flags=re.IGNORECASE)
            caption = re.sub(r'\bI\s+feel\s+\w+(?:\s+\w+)?\.?\s*$', '', caption, flags=re.IGNORECASE)
            caption = ' '.join(caption.split()).strip()
            # Anfuehrungszeichen entfernen falls LLM sie einpackt
            if caption.startswith('"') and caption.endswith('"'):
                caption = caption[1:-1]
            if caption.startswith("'") and caption.endswith("'"):
                caption = caption[1:-1]

            logger.info(f"Caption generiert: {caption}")
            return caption

        except Exception as e:
            logger.error(f"Caption-Generation fehlgeschlagen: {e}")
            logger.debug("Traceback:", exc_info=True)
            return None

    def _analyze_image(self, image_path: str, character_name: str) -> Optional[str]:
        """Analysiert ein generiertes Bild via Vision-LLM und gibt eine sachliche Beschreibung zurueck.

        Wird nach der Bildgenerierung aufgerufen um eine objektive Bildbeschreibung
        in den Metadaten zu speichern.
        """
        from app.core.llm_client import LLMClient

        logger.info("Instagram Bildanalyse gestartet...")

        if not os.path.exists(image_path):
            logger.error("Bilddatei existiert nicht: %s", image_path)
            return None

        try:
            with open(image_path, 'rb') as f:
                image_bytes = f.read()
            base64_image = base64.b64encode(image_bytes).decode('utf-8')
        except Exception as e:
            logger.error("Fehler beim Laden des Bildes fuer Analyse: %s", e)
            return None

        # Detect language from caption config for this agent
        cfg = self._get_effective_config(character_name)
        caption_language = cfg.get("caption_language", "de")
        lang_name = "German" if caption_language == "de" else "English"

        from app.core.prompt_templates import render_task
        analysis_system, prompt_text = render_task(
            "image_analysis", language_name=lang_name)

        try:
            vcfg = self._get_vision_llm_config(character_name)
            if not vcfg:
                logger.warning("Kein Vision-LLM fuer Bildanalyse verfuegbar")
                return None

            llm = LLMClient(
                model=vcfg["model"],
                api_key=vcfg["api_key"],
                api_base=vcfg["api_base"],
                temperature=0.3,
                max_tokens=500,
                request_timeout=int(os.environ.get("LLM_REQUEST_TIMEOUT", "120")))

            image_url = f"data:image/png;base64,{base64_image}"
            messages = [
                {"role": "system", "content": analysis_system},
                {"role": "user", "content": [
                    {"type": "text", "text": prompt_text},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]},
            ]

            from app.core.llm_queue import get_llm_queue, Priority
            response = get_llm_queue().submit(
                task_type="image_analysis",
                priority=Priority.NORMAL,
                llm=llm,
                messages_or_prompt=messages,
                agent_name=character_name)
            analysis = response.content.strip()

            if analysis.startswith('"') and analysis.endswith('"'):
                analysis = analysis[1:-1]
            if analysis.startswith("'") and analysis.endswith("'"):
                analysis = analysis[1:-1]

            logger.info("Bildanalyse abgeschlossen: %s", analysis[:120])
            return analysis
        except Exception as e:
            logger.error("Bildanalyse fehlgeschlagen: %s", e)
            logger.debug("Traceback:", exc_info=True)
            return None

    def execute(self, raw_input: str) -> str:
        """
        Erstellt einen Instagram-Post in 3 Schritten:
        1. Bild generieren (ohne Galerie-Eintrag)
        2. Bild vom LLM analysieren lassen
        3. Natuerlichen Caption daraus generieren

        Args:
            raw_input: JSON-String mit input (Bildbeschreibung), character_name, user_id

        Returns:
            String mit Post-Bestaetigung und Bild-Link
        """
        input_data = self._parse_input(raw_input)
        image_prompt = input_data["image_prompt"]
        provided_caption = input_data.get("caption", "").strip()
        character_name = input_data["agent_name"]
        user_id = input_data["user_id"]

        if not character_name:
            return "Fehler: Agent-Name fehlt."
        if not image_prompt.strip():
            return "Fehler: Bildbeschreibung fehlt."

        # Per-Agent enabled Check
        cfg = self._get_effective_config(character_name)
        if not cfg.get("enabled", True):
            return f"Instagram ist fuer {character_name} deaktiviert."

        # Cooldown pruefen
        cooldown_hours = int(cfg.get("post_cooldown_hours", 12))
        if cooldown_hours > 0:
            from datetime import datetime, timedelta
            from app.models.character import get_character_skill_config
            agent_config = get_character_skill_config(character_name, self.SKILL_ID)
            last_ts = (agent_config or {}).get("last_post_timestamp")
            if last_ts:
                try:
                    last_post_time = datetime.fromisoformat(last_ts)
                    cooldown_end = last_post_time + timedelta(hours=cooldown_hours)
                    now = datetime.now()
                    if now < cooldown_end:
                        remaining = cooldown_end - now
                        hours_left = remaining.total_seconds() / 3600
                        logger.info("Cooldown aktiv: %s hat noch %.1fh Cooldown", character_name, hours_left)
                        return f"{character_name} hat noch {hours_left:.1f}h Cooldown bis zum naechsten Post (Cooldown: {cooldown_hours}h)."
                except (ValueError, TypeError):
                    pass

        # Schritt 1: ImageGenerationSkill holen und Bild generieren
        image_skill = self._get_image_skill()
        if not image_skill:
            return "Fehler: ImageGenerator Skill nicht verfuegbar. Bild kann nicht generiert werden."

        # Location und Activity fuer Caption-Generierung merken
        current_location = get_character_current_location(character_name)
        current_activity = get_character_current_activity(character_name)

        # Task-Tracking
        _tq = get_task_queue()
        _track_id = _tq.track_start("instagram_post", "Instagram: Bild generieren", agent_name=character_name)

        logger.info("=" * 60)
        logger.info(f"INSTAGRAM-POST ERSTELLEN fuer {character_name}")
        logger.info("=" * 60)
        if current_location:
            _il = get_location(current_location)
            logger.info(f"Location: {_il.get('name', current_location) if _il else current_location}")
        if current_activity:
            logger.info(f"Activity: {current_activity}")
        logger.info("Schritt 1: Bild generieren via Standard-Pipeline (auto_enhance=True)...")
        logger.info(f"Bild-Prompt: {image_prompt}")

        # Instagram-Spezifik: Personen aus dem Image-Prompt rausfischen
        # (Agent immer + erwaehnte Characters). Die Standard-Pipeline
        # uebernimmt dann Person/Outfit/Mood/Activity/Location/Adapter-Rendering
        # und optional LLM-Enhancement via Workflow `prompt_instruction`.
        _appearances = []
        _agent_app = get_character_appearance(character_name)
        if _agent_app and "{" in _agent_app:
            _prof = get_character_profile(character_name)
            _tmpl = get_template(_prof.get("template", "")) if _prof.get("template") else None
            _agent_app = resolve_profile_tokens(
                _agent_app, _prof, template=_tmpl, target_key="character_appearance"
            )
        if _agent_app:
            _appearances.append({"name": character_name, "appearance": _agent_app})

        # Andere Characters im Prompt erkennen (alles ist Character — auch der Spieler-Avatar
        # ist ein normaler Character in list_available_characters)
        _prompt_lower = f" {image_prompt.lower()} "
        for _char_name in list_available_characters():
            if _char_name == character_name:
                continue
            if f" {_char_name.lower()} " in _prompt_lower:
                _char_app = get_character_appearance(_char_name)
                if _char_app and "{" in _char_app:
                    _other_profile = get_character_profile(_char_name)
                    _other_tmpl = get_template(_other_profile.get("template", "human-default"))
                    _char_app = resolve_profile_tokens(
                        _char_app, _other_profile, template=_other_tmpl, target_key="character_appearance"
                    )
                if _char_app:
                    _appearances.append({"name": _char_name, "appearance": _char_app})

        # Instagram-spezifischer Workflow (per-Agent Config > .env Default > leer=Character-Default)
        _insta_workflow = cfg.get("imagegen_workflow", "").strip()
        if _insta_workflow:
            if _insta_workflow.startswith("workflow:"):
                _insta_workflow = _insta_workflow[len("workflow:"):]
            logger.info("Instagram Workflow: %s", _insta_workflow)

        image_payload = {
            "prompt": image_prompt,
            "input": image_prompt,
            "agent_name": character_name,
            "user_id": "",
            "set_profile": False,
            "skip_gallery": True,
            "auto_enhance": True,           # Standard-Pipeline: Adapter + optional LLM-Enhancement
            "force_faceswap": True,
            "appearances": _appearances,    # Instagram-Spezifik: Pre-detected Personen aus Prompt
        }
        if _insta_workflow:
            image_payload["workflow"] = _insta_workflow

        try:
            image_result = image_skill.execute(json.dumps(image_payload))
        except Exception as e:
            logger.error(f"Fehler bei Bildgenerierung: {e}")
            _tq.track_finish(_track_id, error=str(e))
            return f"Fehler bei Bildgenerierung: {e}"

        # Dateiname aus Ergebnis extrahieren
        image_filename = self._extract_filename(image_result)
        if not image_filename:
            logger.error(f"Konnte Dateiname nicht extrahieren: {image_result}")
            _tq.track_finish(_track_id, error="Dateiname nicht extrahiert")
            return f"Fehler: Bild wurde generiert, aber Dateiname konnte nicht extrahiert werden."

        logger.info(f"Bild generiert: {image_filename}")

        # Enhanced Prompt (wie an ComfyUI gesendet) fuer Post-Metadaten uebernehmen.
        # Thread-Local zuerst (race-safe) — siehe gen_meta-Block unten.
        final_image_prompt = ""
        try:
            _tls_p = getattr(image_skill, '_meta_tls', None)
            if _tls_p is not None:
                final_image_prompt = getattr(_tls_p, 'last_enhanced_prompt', '') or ""
        except Exception:
            pass
        if not final_image_prompt:
            final_image_prompt = getattr(image_skill, 'last_enhanced_prompt', '') or image_prompt

        # Generierungs-Metadaten vom Image-Skill uebernehmen.
        # WICHTIG: Thread-Local-Slot zuerst lesen — bei parallelen Generationen
        # (z.B. Instagram-Post + Expression-Regen gleichzeitig) wuerde die
        # Instance-Variante mit fremdem Meta ueberschrieben (race-condition,
        # Yuki's expression-regen-Meta landet faelschlich in Kira's Post).
        gen_meta = {}
        try:
            _tls = getattr(image_skill, '_meta_tls', None)
            if _tls is not None:
                gen_meta = getattr(_tls, 'last_image_meta', None) or {}
        except Exception:
            pass
        if not gen_meta:
            gen_meta = getattr(image_skill, 'last_image_meta', None) or {}

        # Bild von images/ nach instagram/ verschieben
        images_dir = get_character_images_dir(character_name)
        instagram_dir = get_instagram_dir()
        src_path = images_dir / image_filename
        dst_path = instagram_dir / image_filename

        logger.info(f"Verschiebe Bild: {src_path} -> {dst_path}")

        import shutil
        try:
            shutil.copy2(str(src_path), str(dst_path))
            if dst_path.exists():
                src_path.unlink()
                logger.info("Bild verschoben: images/ -> instagram/")
            else:
                logger.warning("Kopie fehlgeschlagen, verwende Originalpfad")
                dst_path = src_path
        except Exception as e:
            logger.warning(f"Fehler beim Verschieben: {e}, verwende Originalpfad")
            dst_path = src_path

        # Hole aktuelle Location und Activity
        current_activity = get_character_current_activity(character_name)
        
        # Schritt 2+3: LLM analysiert Bild und generiert Caption
        _tq.track_update_label(_track_id, "Instagram: Caption generieren")
        logger.info("=" * 60)
        logger.info("Schritt 2+3: Bild analysieren und Caption generieren...")
        logger.info(f"Bildpfad fuer Analyse: {dst_path}")
        logger.debug(f"Datei existiert: {dst_path.exists()}")
        if current_location:
            _il = get_location(current_location)
            logger.info(f"Location: {_il.get('name', current_location) if _il else current_location}")
        if current_activity:
            logger.info(f"Activity: {current_activity}")
        logger.info("=" * 60)

        # Primaer: Caption aus Thought-Tool-Argument (provided_caption). Der
        # aufrufende Thought-LLM hat die Caption schon im Character-Voice
        # formuliert — kein zweiter Vision-LLM-Call noetig.
        # Fallback: Wenn keine Caption uebergeben wurde, Vision-LLM generiert
        # aus dem Bild. Letzter Fallback: image_prompt + generische Hashtags.
        if provided_caption and provided_caption.strip():
            caption = provided_caption.strip()
            logger.info("Caption aus Tool-Argument genutzt (Vision-LLM uebersprungen)")
        else:
            caption = self._generate_caption(
                str(dst_path), character_name, image_prompt,
                current_location, current_activity,
                draft_caption="")
            if not caption:
                logger.warning("Fallback: Verwende einfachen Caption")
                caption = f"{image_prompt} #instagram #ai"

        # Hashtags extrahieren
        hashtags = self._extract_hashtags(caption)

        # Schritt 4: Bildanalyse via Vision-LLM
        _tq.track_update_label(_track_id, "Instagram: Bildanalyse")
        logger.info("Schritt 4: Bildanalyse via Vision-LLM...")
        image_analysis = self._analyze_image(str(dst_path), character_name)
        if image_analysis:
            logger.info("Bildanalyse: %s", image_analysis[:120])
        else:
            logger.warning("Bildanalyse fehlgeschlagen oder leer")

        # Post erstellen und speichern
        _tq.track_update_label(_track_id, "Instagram: Post erstellen")
        image_meta = {
            "backend": gen_meta.get("backend", ""),
            "backend_type": gen_meta.get("backend_type", ""),
            "workflow": gen_meta.get("workflow", ""),
            "faceswap": gen_meta.get("faceswap", False),
            "duration_s": gen_meta.get("duration_s", 0.0),
            "image_analysis": image_analysis or "",
        }
        # Referenzbilder in Metadaten übernehmen (für Regenerierung + Character-Erkennung)
        if gen_meta.get("reference_images"):
            image_meta["reference_images"] = gen_meta["reference_images"]
        # Erweiterte Felder fuer Re-Creation-Dialog + Bild-Info-Panel —
        # ohne diese laeuft Rebuild blind auf den aktuellen Character-State
        # ("Keine Original-Werte gespeichert"-Hinweis im UI).
        for _k in ("canonical", "canonical_source", "target_model",
                   "template_prompt", "prompt_method",
                   "character_names", "from_character",
                   "model", "loras", "seed", "negative_prompt",
                   "faceswap_method", "faceswap_fallback", "face_enhance",
                   "guidance_scale", "num_inference_steps",
                   "items_used", "location", "room_id"):
            _v = gen_meta.get(_k)
            if _v not in (None, "", [], {}):
                image_meta[_k] = _v
        post = create_post(
            character_name=character_name,
            image_filename=image_filename,
            caption=caption,
            hashtags=hashtags,
            image_prompt=final_image_prompt,
            image_meta=image_meta)

        logger.info(f"Post erstellt: {post['id']}")
        logger.info(f"Caption: {caption}")

        # Cooldown-Timestamp persistieren
        try:
            from datetime import datetime
            from app.models.character import get_character_skill_config, save_character_skill_config
            agent_config = get_character_skill_config(character_name, self.SKILL_ID) or {}
            agent_config["last_post_timestamp"] = datetime.now().isoformat()
            save_character_skill_config(character_name, self.SKILL_ID, agent_config)
            logger.info("Cooldown-Timestamp gespeichert fuer %s", character_name)
        except Exception as e:
            logger.warning("Cooldown-Timestamp konnte nicht gespeichert werden: %s", e)

        logger.info("=" * 60)
        logger.info("INSTAGRAM-POST FERTIG")
        logger.info("=" * 60)

        # Stimmung basierend auf Caption-Sentiment setzen
        try:
            caption_lower = caption.lower()
            mood = "inspiriert"  # Default

            if any(word in caption_lower for word in ["happy", "glücklich", "joy", "freud", "laugh", "lach", "smile", "lächel"]):
                mood = "glücklich"
            elif any(word in caption_lower for word in ["beautiful", "schön", "amazing", "toll", "love", "liebe", "wonderful", "wunderbar"]):
                mood = "begeistert"
            elif any(word in caption_lower for word in ["peace", "calm", "ruhig", "zen", "relax"]):
                mood = "entspannt"
            elif any(word in caption_lower for word in ["adventure", "abenteuer", "explore", "entdecken", "journey", "reise"]):
                mood = "abenteuerlustig"
            elif any(word in caption_lower for word in ["creative", "kreativ", "art", "kunst", "imagine", "vorstell"]):
                mood = "kreativ"

            save_character_current_feeling(character_name, mood)
            logger.info(f"Stimmung gesetzt: {mood}")
        except Exception as e:
            logger.warning(f"Stimmung konnte nicht gesetzt werden: {e}")

        _tq.track_finish(_track_id)

        # Ergebnis zurueckgeben
        image_url = f"/instagram/images/{image_filename}?user_id={user_id}"
        return (
            f"Instagram-Post erstellt!\n\n"
            f"![Post]({image_url})\n\n"
            f"**Caption:** {caption}\n\n"
            f"Post-ID: {post['id']}"
        )

    def get_usage_instructions(self, format_name: str = "", **kwargs) -> str:
        from app.core.tool_formats import format_example
        fmt = format_name or "tag"
        return format_example(fmt, self.name, "relaxing at a cafe with coffee")

    def memorize_result(self, result: str, character_name: str) -> bool:
        """Speichert Instagram-Posts als Memory (fuer Scheduler-Aufrufe)."""
        if not result or "Fehler" in result[:20] or "Error" in result[:20]:
            return False
        try:
            from app.models.memory import add_memory
            # Nur die wesentliche Info speichern (nicht die ganze URL etc.)
            content = f"Ich habe einen Instagram-Post erstellt: {result[:500]}"
            add_memory(
                character_name=character_name,
                content=content,
                memory_type="episodic",
                importance=2,
                tags=["scheduler_tool", "instagram_post"],
                context="scheduler:Instagram")
            return True
        except Exception:
            return False

    def as_tool(self, **kwargs) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                f"{self.description}. "
                f"Input options: "
                f"(a) JSON {{\"image_prompt\": \"...\", \"caption\": \"your caption text\"}} — "
                f"recommended, write the caption in your own voice. "
                f"(b) just the image description — caption auto-generated."
            ),
            func=self.execute)
