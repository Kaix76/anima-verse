"""Bild-Regenerierung — ersetzt ein bestehendes Bild ueber die ImageGenerationSkill-Pipeline.

Nutzt den gespeicherten Prompt + optionalen User-Verbesserungswunsch,
waehlt Workflow + Backend, generiert neu und ueberschreibt die Datei.
"""
import os
from pathlib import Path
from typing import Optional, Tuple

from app.core.log import get_logger
logger = get_logger("image_regen")



def enhance_prompt(
    original_prompt: str,
    improvement_request: str,
    agent_config: Optional[dict] = None) -> str:
    """Verbessert einen Image-Prompt basierend auf User-Feedback via LLM.

    Returns:
        Verbesserter Prompt, oder original_prompt bei Fehler/leerem Request.
    """
    if not improvement_request or not improvement_request.strip():
        return original_prompt

    from app.core.llm_router import llm_call

    character_name = (agent_config or {}).get("name", "")

    system_prompt = (
        "You are an image prompt improver. You receive an existing image generation prompt "
        "and a user's improvement request in natural language. "
        "Modify the original prompt to incorporate the requested improvements. "
        "Keep the core scene and subjects intact, but adjust details as requested. "
        "Respond ONLY with the improved prompt, nothing else."
    )

    human_msg = (
        f"/no_think\nOriginal prompt:\n{original_prompt}\n\n"
        f"Requested improvements:\n{improvement_request}"
    )

    try:
        response = llm_call(
            task="image_prompt",
            system_prompt=system_prompt,
            user_prompt=human_msg,
            agent_name=character_name)
        improved = response.content.strip()
        if improved:
            logger.info(f"Verbesserter Prompt: {improved[:120]}...")
            return improved
        logger.warning("LLM gab leere Antwort, verwende Original-Prompt")
        return original_prompt
    except Exception as e:
        logger.error(f"LLM Fehler: {e}")
        return original_prompt


def _save_analysis(output_path: str, analysis: str, character_name: str) -> None:
    """Speichert die Bildanalyse in den passenden Metadaten (Instagram oder Character-Image)."""
    from pathlib import Path as _Path
    filename = _Path(output_path).name

    # Instagram-Bild?
    if "/instagram/" in output_path:
        try:
            from app.models.instagram import (
                load_image_meta, save_image_meta, load_feed, save_feed)
            # Separate Meta-Datei aktualisieren
            meta = load_image_meta(filename) or {}
            meta["image_analysis"] = analysis
            save_image_meta(filename, meta)
            # Feed-Eintrag aktualisieren (image_meta.image_analysis)
            feed = load_feed()
            for post in feed:
                if post.get("image_filename") == filename:
                    if "image_meta" in post and isinstance(post["image_meta"], dict):
                        post["image_meta"]["image_analysis"] = analysis
                    break
            save_feed(feed)
            logger.info("Bildanalyse in Instagram-Meta gespeichert")
        except Exception as e:
            logger.warning("Instagram-Meta Speichern fehlgeschlagen: %s", e)
        return

    # Character-Bild
    if character_name:
        try:
            from app.models.character import add_character_image_metadata
            add_character_image_metadata(character_name, filename, {"image_analysis": analysis})
            logger.info("Bildanalyse in Character-Image-Meta gespeichert")
        except Exception as e:
            logger.warning("Character-Image-Meta Speichern fehlgeschlagen: %s", e)


def _do_standalone_faceswap(output_path: str, character_name: str) -> bool:
    """Standalone FaceSwap via Face Service (Fallback).

    Wird im Task-Queue-Panel als eigener Track-Eintrag (provider=FaceService)
    sichtbar gemacht — analog zu ComfyUI-Tasks. Serialisierung haendelt der
    face_client (siehe _swap_lock) damit parallele Calls den Service nicht
    in Read-Timeouts treiben.

    Returns True bei Erfolg, False sonst — Caller kann den Status fuer Meta
    persistieren.
    """
    from app.core.task_queue import get_task_queue
    _tq = get_task_queue()
    _track_id = _tq.track_start(
        "faceswap", "Face Swap (intern)",
        agent_name=character_name, provider="FaceService")
    try:
        from app.skills.face_client import apply_face_swap_files, is_available as face_available
        if not face_available():
            logger.warning("FaceSwap: Face Service nicht erreichbar")
            _tq.track_finish(_track_id, error="Face Service nicht erreichbar")
            return False
        from app.models.character import get_character_profile_image, get_character_images_dir
        profile_img = get_character_profile_image(character_name)
        if not profile_img:
            logger.warning(f"FaceSwap: Kein Profilbild fuer {character_name}")
            _tq.track_finish(_track_id, error="Kein Profilbild")
            return False
        images_dir = get_character_images_dir(character_name)
        source_path = str(images_dir / profile_img)
        result = apply_face_swap_files(output_path, source_path, output_path)
        if result:
            logger.info("Standalone FaceSwap erfolgreich")
            _tq.track_finish(_track_id)
            return True
        else:
            logger.warning("Standalone FaceSwap fehlgeschlagen")
            _tq.track_finish(_track_id, error="Swap fehlgeschlagen")
            return False
    except Exception as e:
        logger.error(f"Standalone FaceSwap Fehler: {e}")
        _tq.track_finish(_track_id, error=str(e)[:200])
        return False


def regenerate_image(character_name: str,
    output_path: str,
    original_prompt: str,
    improvement_request: str = "",
    workflow_name: str = "",
    backend_name: str = "",
    agent_config: Optional[dict] = None,
    loras: Optional[list] = None,
    model_override: str = "",
    character_names: Optional[list] = None,
    room_id: str = "",
    location_id: str = "",
    negative_prompt_override: str = "",
    track_id: str = "",
    create_new: bool = False) -> Tuple[bool, str, str]:
    """Generiert ein Bild neu. Bei create_new=True wird eine neue Datei angelegt statt zu ueberschreiben.

    Returns:
        (success, final_prompt, actual_output_path)

    Args:
        user_id: User-ID
        character_name: Character-Name (leer fuer Welt-Bilder)
        output_path: Pfad der zu ueberschreibenden Bilddatei
        original_prompt: Gespeicherter Image-Prompt
        improvement_request: Optionaler Verbesserungswunsch
        workflow_name: Optionaler ComfyUI-Workflow-Name
        backend_name: Optionaler Backend-Name (direkte Backend-Auswahl, nicht-ComfyUI)
        agent_config: Per-Agent Config (fuer LLM-Override)

    Returns:
        (success, final_prompt) — final_prompt ist der tatsaechlich verwendete Prompt
    """
    final_prompt = original_prompt

    # 1b. Prompt verbessern wenn User-Feedback vorhanden
    if improvement_request:
        final_prompt = enhance_prompt(final_prompt, improvement_request, agent_config)

    # 1c. Room-Override: "setting: ..." aus Prompt entfernen und ggf. durch neuen Raum ersetzen
    if room_id and character_name:
        import re as _re
        # "setting: Meetingraum (A bright meeting room ...)" oder ", setting: ..." entfernen
        final_prompt = _re.sub(r',?\s*setting:\s*[^,]*(?:\([^)]*\))?', '', final_prompt).strip()
        final_prompt = _re.sub(r',\s*$', '', final_prompt).strip()
        # Neuen Raum-Text einfuegen (originale Location verwenden)
        from app.models.world import get_location, get_room_by_id
        from app.models.character import get_character_current_location
        _loc_id = location_id or get_character_current_location(character_name)
        if _loc_id:
            _loc_data = get_location(_loc_id)
            if _loc_data:
                _room_data = get_room_by_id(_loc_data, room_id)
                if _room_data:
                    _room_name = _room_data.get("name", "")
                    _room_desc = _room_data.get("image_prompt_day", "") or _room_data.get("description", "")
                    _setting = f", setting: {_room_name}" + (f" ({_room_desc})" if _room_desc else "")
                    final_prompt += _setting
                    logger.info("Room-Override: %s", _room_name)

    # 2. ImageGenerationSkill holen
    from app.core.dependencies import get_skill_manager
    skill_mgr = get_skill_manager()
    skill = skill_mgr.get_skill("image_generation") if skill_mgr else None
    if not skill:
        raise RuntimeError("ImageGenerationSkill nicht verfuegbar")

    # 3. Backend + Workflow bestimmen
    active_workflow = None
    backend = None

    if backend_name:
        # Direkte Backend-Auswahl (nicht-ComfyUI)
        for b in skill.backends:
            if b.name == backend_name and b.available:
                backend = b
                break
        if not backend:
            raise RuntimeError(f"Backend '{backend_name}' nicht verfuegbar")
    else:
        # Workflow-basierte Auswahl (ComfyUI)
        if workflow_name and skill.comfy_workflows:
            for wf in skill.comfy_workflows:
                if wf.name == workflow_name:
                    active_workflow = wf
                    break
        if not active_workflow and skill.comfy_workflows:
            active_workflow = skill._get_active_workflow(character_name) if character_name else skill.comfy_workflows[0]

        if active_workflow:
            backend = skill._select_backend_for_workflow(active_workflow, character_name)
            if not backend:
                # Retry nach Availability-Refresh (Backend evtl. seit Init wieder online)
                for b in skill.backends:
                    if b.api_type == "comfyui" and b.instance_enabled and not b.available:
                        b.check_availability()
                backend = skill._select_backend_for_workflow(active_workflow, character_name)
            if not backend:
                raise RuntimeError(f"Kein kompatibles ComfyUI-Backend fuer Workflow '{active_workflow.name}' verfuegbar")
        if not backend:
            backend = skill._select_backend_for_agent(character_name)

    if not backend:
        raise RuntimeError("Kein Backend verfuegbar")

    # 5. Config + Params
    cfg = skill._get_instance_config(character_name, backend) if character_name else skill._get_backend_defaults(backend)
    negative_prompt = negative_prompt_override or cfg.get("negative_prompt", getattr(backend, "negative_prompt", ""))

    params = {
        "width": cfg.get("width", getattr(backend, "width", 1024)),
        "height": cfg.get("height", getattr(backend, "height", 1024)),
    }
    if active_workflow:
        params["workflow_file"] = active_workflow.workflow_file
        # Param-Key: bei input_unet/input_safetensors Workflows -> "unet", sonst "model"
        _model_key = "unet" if (active_workflow.has_input_unet or active_workflow.has_input_safetensors) else "model"
        if active_workflow.model:
            params[_model_key] = active_workflow.model
        # Per-Character Model-Override (aus Skill-Config, ueberschreibt .env Default)
        from app.models.character import get_character_skill_config
        _agent_cfg = get_character_skill_config(character_name, "image_generation") or {}
        _char_model = (_agent_cfg.get("workflow_models") or {}).get(active_workflow.name, "").strip()
        if _char_model:
            params[_model_key] = _char_model
            logger.info("Per-Character Model: %s", _char_model)
        # LoRA-Inputs: User-Override oder Workflow-Defaults
        if active_workflow.has_loras:
            if loras is not None:
                params["lora_inputs"] = loras
            elif active_workflow.default_loras:
                params["lora_inputs"] = active_workflow.default_loras
        # Model-Override (User-Auswahl im Dialog, hoechste Prioritaet)
        if model_override:
            params[_model_key] = model_override
        # Model-Verfuegbarkeit pruefen und ggf. aehnlichstes Modell finden
        _current_model = params.get(_model_key, "")
        if _current_model and backend.api_type == "comfyui" and skill:
            _resolved = skill.resolve_model_for_backend(
                _current_model, backend, active_workflow.model_type if active_workflow else "")
            if _resolved and _resolved != _current_model:
                logger.info("Model-Resolve: %s -> %s (Backend: %s)", _current_model, _resolved, backend.name)
                params[_model_key] = _resolved
        # Allowed-Models-Liste mitgeben — der Backend-Code braucht sie um
        # bei Multi-Loader-Workflows (z.B. Qwen safetensors+gguf) die jeweils
        # ungenutzten Loader auf eine vorhandene Datei zu setzen. ComfyUI
        # validiert sonst den ungenutzten Workflow-Default und lehnt ab.
        if backend.api_type == "comfyui" and skill and getattr(skill, "_model_cache_loaded", False):
            _all_unet = skill._cached_unet_models_by_service.get(backend.name, [])
            _all_ckpt = skill._cached_checkpoints_by_service.get(backend.name, [])
            params["allowed_models"] = sorted(set(_all_unet + _all_ckpt))
        # CLIP-Model setzen wenn im Workflow konfiguriert
        if active_workflow.clip:
            params["clip_name"] = active_workflow.clip
            logger.info("CLIP: %s", active_workflow.clip)
    else:
        # Cloud-Backend ohne Workflow (Together.ai, CivitAI, Mammouth):
        # Dialog-Auswahl (model_override) muss auch hier wirken, sonst
        # bleibt das im Backend konfigurierte Default-Modell aktiv.
        if model_override:
            params["model"] = model_override
            logger.info("Model-Override (Cloud-Backend): %s", model_override)

    # 6. Referenzbilder aufloesen
    needs_comfyui_faceswap = False
    use_standalone_faceswap = False
    face_refs = {"reference_images": {}, "boolean_inputs": {}, "string_inputs": {}, "workflow_has_faceswap": False}
    appearances: list = []

    # Personen-Detection laeuft IMMER wenn character_name vorhanden — auch
    # ohne ComfyUI-Workflow (z.B. CivitAI/Together direkt). Das Post-
    # Processing (FaceSwap/MultiSwap) braucht appearances, sonst hat es
    # keine Refs zum Swappen.
    if character_name:
        try:
            from app.core.prompt_builder import PromptBuilder
            _regen_builder_for_appearances = PromptBuilder(character_name)
            if character_names is not None:
                _persons = _regen_builder_for_appearances.detect_persons(final_prompt, character_names=character_names)
                logger.info("Explizite Character-Auswahl: %s", character_names)
            else:
                _persons = _regen_builder_for_appearances.detect_persons(final_prompt)
            if not _persons:
                _persons = _regen_builder_for_appearances.detect_persons("", character_names=[character_name])
            appearances = [{"name": p.name, "appearance": p.appearance} for p in _persons]
        except Exception as _ape:
            logger.warning("Appearance-Detection fehlgeschlagen: %s", _ape)

    if active_workflow and character_name:
        try:
            from app.core.prompt_builder import PromptBuilder, PromptVariables
            _regen_builder = PromptBuilder(character_name)
            # persons fuer Workflow-Slots erneut aufloesen (mit ref_images etc.)
            if character_names is not None:
                persons = _regen_builder.detect_persons(final_prompt, character_names=character_names)
            else:
                persons = _regen_builder.detect_persons(final_prompt)
            if not persons:
                persons = _regen_builder.detect_persons("", character_names=[character_name])
            _regen_pv = PromptVariables(persons=persons)
            _regen_pv.ref_images = {}
            for idx, p in enumerate(persons, 1):
                ref = _regen_builder._resolve_person_ref_image(p)
                if ref:
                    _regen_pv.ref_images[idx] = ref
            _regen_builder._collect_location(_regen_pv)

            # Room-Override: Hintergrundbild fuer gewaehlten Raum einsetzen.
            # Wird VOR resolve_reference_slots gesetzt, damit der Slot
            # korrekt fuer den Workflow-Kind gemappt wird (Qwen: Slot 4,
            # Flux: input_reference_image_background).
            if room_id:
                from app.models.world import get_background_path
                from app.models.character import get_character_current_location
                _loc_id = location_id or get_character_current_location(character_name)
                if _loc_id:
                    _bg = get_background_path(_loc_id, room=room_id)
                    if _bg and _bg.exists():
                        _regen_pv.ref_image_room = str(_bg)
                        logger.info("Room-Override Bild: %s", _bg.name)

            from app.skills.image_generation_skill import WorkflowKind
            _wf_kind = active_workflow.kind.value if active_workflow else None
            face_refs = _regen_builder.resolve_reference_slots(_regen_pv, kind=_wf_kind)

            if active_workflow.kind == WorkflowKind.QWEN_STYLE:
                # Style-Conditioning: Referenzen direkt in die Generierung injizieren
                params["reference_images"] = face_refs["reference_images"]
                params["boolean_inputs"] = face_refs["boolean_inputs"]
                params["string_inputs"] = face_refs["string_inputs"]
            else:
                # FLUX_BG / Z_IMAGE: Charaktere kommen ueber Post-Processing.
                # Bei FLUX_BG wird zusaetzlich das Background-Ref-Bild via
                # face_refs in die Generierung injiziert (siehe unten).
                if active_workflow.kind == WorkflowKind.FLUX_BG:
                    params["reference_images"] = face_refs["reference_images"]
                    params["boolean_inputs"] = face_refs["boolean_inputs"]
                # Post-Processing aktivieren — die konkrete Methode (multiswap /
                # comfyui-faceswap / internal) wird unten anhand character.swap_mode
                # gewaehlt. Ohne dieses Flag wuerde der Swap-Block komplett
                # uebersprungen werden (gleiche Logik wie in execute()).
                needs_comfyui_faceswap = bool(face_refs.get("workflow_has_faceswap", False))
                if not needs_comfyui_faceswap:
                    use_standalone_faceswap = True
                logger.info("Post-Processing: kind=%s, in_workflow_swap=%s, standalone=%s",
                            active_workflow.kind.value, needs_comfyui_faceswap, use_standalone_faceswap)
        except Exception as e:
            logger.warning(f"Face-Referenzen Fehler: {e}")
    elif getattr(backend, "faceswap_needed", False) and character_name:
        use_standalone_faceswap = True
        logger.info(f"Standalone-FaceSwap aktiviert (Backend {backend.name})")

    # 6b. Prompt Prefix/Suffix vom Workflow anwenden (Prompt wird ohne Affixe gespeichert)
    generation_prompt = final_prompt
    if active_workflow and active_workflow.prompt_style:
        generation_prompt = f"{active_workflow.prompt_style} {generation_prompt}"
    elif hasattr(backend, "prompt_prefix") and backend.prompt_prefix:
        generation_prompt = f"{backend.prompt_prefix} {generation_prompt}"

    logger.info(f"Backend={backend.name}, Workflow={active_workflow.name if active_workflow else 'default'}")
    logger.info(f"Prompt (clean): {final_prompt[:120]}...")
    if generation_prompt != final_prompt:
        logger.info(f"Prompt (with affixes): {generation_prompt[:120]}...")
    logger.info(f"Output: {output_path}")

    # 7. Generieren (via GPU-Queue wenn Provider zugeordnet)
    import time as _time
    _gen_start = _time.time()

    # Track-Aktivierung: Timer erst starten wenn GPU-Arbeit tatsaechlich beginnt
    def _activate_track(provider_name: str = ""):
        if track_id:
            try:
                from app.core.task_queue import get_task_queue
                from app.core.task_router import match_queue_name
                resolved_queue = match_queue_name(provider_name) if provider_name else ""
                get_task_queue().track_activate(track_id, queue_name=resolved_queue or "")
            except Exception:
                pass

    # Backend-Fallback-Engine: bei Fehler des primaeren Backends wird
    # automatisch auf Fallback (per backend.fallback_mode) gewechselt.
    # Der op-Callback adaptiert generation_prompt/params pro Backend —
    # wichtig wenn ein "specific" Fallback einen anderen api_type hat.
    def _build_op(_orig_prompt: str, _orig_neg: str, _orig_params: dict, _orig_wf):
        def _op(b):
            _activate_track(getattr(b, "name", ""))
            # Prompt-Style anpassen pro Backend (Workflow hat Prio)
            _gen_prompt = _orig_prompt
            if _orig_wf and _orig_wf.prompt_style:
                _gen_prompt = f"{_orig_wf.prompt_style} {_gen_prompt}"
            elif hasattr(b, "prompt_prefix") and b.prompt_prefix:
                _gen_prompt = f"{b.prompt_prefix} {_gen_prompt}"
            # Negative-Prompt: per-Backend aus dessen cfg, falls dieser Aufruf
            # ein Override hat bleibt _orig_neg gewinnt
            _gen_neg = _orig_neg or getattr(b, "negative_prompt", "")
            # Params: Modell-Resolve fuer dieses Backend
            _bp = dict(_orig_params)
            _model_key = "unet" if (_orig_wf and (_orig_wf.has_input_unet or _orig_wf.has_input_safetensors)) else "model"
            _cur_model = _bp.get(_model_key, "")
            if _cur_model and b.api_type == "comfyui" and skill:
                _resolved = skill.resolve_model_for_backend(
                    _cur_model, b, _orig_wf.model_type if _orig_wf else "")
                if _resolved and _resolved != _cur_model:
                    logger.info("Regen-Fallback Model-Resolve: %s -> %s (Backend: %s)",
                                _cur_model, _resolved, b.name)
                    _bp[_model_key] = _resolved
            # GPU-Queue fuer lokale Backends, direkt sonst
            if getattr(b, "api_type", "") in ("comfyui", "a1111"):
                from app.core.llm_queue import get_llm_queue, Priority
                _vram = getattr(b, "vram_required_mb", 0) or 0
                logger.info("GPU-Task (Backend=%s, VRAM %dMB)", b.name, _vram)
                return get_llm_queue().submit_gpu_task(
                    task_type="image_regen",
                    priority=Priority.NORMAL,
                    callable_fn=lambda: b.generate(_gen_prompt, _gen_neg, _bp),
                    agent_name=character_name,
                    vram_required_mb=_vram,
                    gpu_type="comfyui")
            return b.generate(_gen_prompt, _gen_neg, _bp)
        return _op

    try:
        _op = _build_op(final_prompt, negative_prompt, params, active_workflow)
        try:
            images, backend = skill.run_with_fallback(
                primary_backend=backend,
                op=_op,
                workflow=active_workflow,
                character_name=character_name)
        except RuntimeError as _fb_err:
            msg = str(_fb_err)
            logger.error("Regen Fallback-Engine: %s", msg)
            raise
        if images == "NO_NEW_IMAGE":
            msg = "Keine neuen Bilder — Seed oder Model unveraendert (Duplikat/Cache)"
            logger.warning(msg)
            raise RuntimeError(msg)
        if not images:
            msg = "Backend gab keine Bilder zurueck"
            logger.error(msg)
            raise RuntimeError(msg)

        _gen_duration = _time.time() - _gen_start

        # Bei create_new: neue Datei anlegen statt ueberschreiben
        actual_output_path = output_path
        if create_new:
            _orig = Path(output_path)
            _suffix = _orig.suffix
            _stem = _orig.stem
            import uuid as _uuid
            _new_name = f"{_stem}_v{_uuid.uuid4().hex[:6]}{_suffix}"
            actual_output_path = str(_orig.parent / _new_name)
            logger.info(f"create_new: Neues Bild als {_new_name}")

        Path(actual_output_path).write_bytes(images[0])
        logger.info(f"Bild erfolgreich geschrieben ({len(images[0])} bytes, {_gen_duration:.1f}s)")

        # FaceSwap / MultiSwap Post-Processing (swap_mode aus Character-Config)
        _do_swap = needs_comfyui_faceswap or use_standalone_faceswap
        _faceswap_method = ""    # "multiswap" | "comfyui" | "internal"
        _faceswap_success = False
        _faceswap_fallback = False
        if _do_swap:
            from app.models.character import get_character_skill_config as _get_skill_cfg
            _regen_skill_cfg = _get_skill_cfg(character_name, "image_generation") if character_name else {}
            _swap_mode = _regen_skill_cfg.get("swap_mode", "default")
            if _swap_mode == "default":
                _swap_mode = os.environ.get("DEFAULT_SWAP_MODE", "comfyui")
            logger.info("Regen Swap-Modus: %s", _swap_mode)

            # FaceSwap: Referenzbilder nur aus Profilbildern
            face_refs = skill._resolve_face_references(
                appearances, character_name, profile_only=True)

            output_file = Path(actual_output_path)
            fs_vram = int(float(os.environ.get("COMFY_FACESWAP_VRAM_REQUIRED", "0")) * 1024) or vram_needed

            if _swap_mode == "multiswap":
                # MultiSwap-Bereich aus Per-Character-Skill-Config (Default face_hair).
                _ms_mode = (_regen_skill_cfg.get("multiswap_mode") or "face_hair").strip()
                if _ms_mode not in skill.MULTISWAP_MODES:
                    _ms_mode = "face_hair"
                logger.info("Regen MultiSwap-Mode: %s", _ms_mode)
                # MultiSwap: face_refs durchreichen (iteriert ueber alle Characters)
                _ms_fn = lambda: skill._apply_multiswap(
                    [output_file.name], output_file.parent, face_refs, _ms_mode, backend
                )
                swapped = None
                _faceswap_method = "multiswap"
                try:
                    from app.core.llm_queue import get_llm_queue, Priority
                    swapped = get_llm_queue().submit_gpu_task(
                        task_type="multiswap_regen",
                        priority=Priority.NORMAL,
                        callable_fn=_ms_fn,
                        agent_name=character_name,
                        vram_required_mb=fs_vram,
                        gpu_type="comfyui")
                    if swapped:
                        logger.info("MultiSwap erfolgreich")
                        _faceswap_success = True
                    else:
                        logger.warning("MultiSwap fehlgeschlagen, Fallback auf Face Service")
                except Exception as ms_err:
                    logger.error("MultiSwap Fehler: %s — Fallback auf Face Service", ms_err)
                if not swapped:
                    # MultiSwap braucht ComfyUI; bei nicht-erreichbarem Backend
                    # auf internen Face-Service ausweichen (nur Gesicht, keine
                    # Haare — besser als gar kein Swap).
                    _faceswap_success = _do_standalone_faceswap(actual_output_path, character_name)
                    _faceswap_method = "internal"
                    _faceswap_fallback = True

            elif _swap_mode == "comfyui":
                # ComfyUI FaceSwap-Workflow (ReActor) — wird unabhaengig
                # davon gefahren, ob die Generation-Pipeline schon Refs hatte.
                # face_refs kommt frisch aus _resolve_face_references(profile_only=True).
                _faceswap_method = "comfyui"
                try:
                    _fs_fn = lambda: skill._apply_comfyui_faceswap(
                        [output_file.name], output_file.parent, face_refs, backend
                    )
                    from app.core.llm_queue import get_llm_queue, Priority
                    swapped = get_llm_queue().submit_gpu_task(
                        task_type="faceswap_regen",
                        priority=Priority.NORMAL,
                        callable_fn=_fs_fn,
                        agent_name=character_name,
                        vram_required_mb=fs_vram,
                        gpu_type="comfyui")
                    if swapped:
                        logger.info("ComfyUI FaceSwap erfolgreich")
                        _faceswap_success = True
                    else:
                        logger.warning("ComfyUI FaceSwap fehlgeschlagen, Fallback auf Face Service")
                        _faceswap_success = _do_standalone_faceswap(actual_output_path, character_name)
                        _faceswap_method = "internal"
                        _faceswap_fallback = True
                except Exception as fs_err:
                    logger.error("ComfyUI FaceSwap Fehler: %s", fs_err)

            else:
                # internal / standalone
                _faceswap_method = "internal"
                _faceswap_success = _do_standalone_faceswap(actual_output_path, character_name)

        # Face Enhancement (GFPGAN/Codeformer) — laeuft NUR nach dem
        # internen FaceSwap. MultiSwap und ComfyUI ReActor bringen ihre
        # eigene Qualitaets-Pipeline mit — ein zusaetzlicher GFPGAN-Pass
        # waere doppelte Arbeit und kann das Ergebnis sogar verschlechtern.
        _enhance_applied = False
        try:
            _enh_method = skill._get_enhance_method(character_name) if character_name else ""
        except Exception:
            _enh_method = ""
        if _faceswap_method == "internal" and _faceswap_success and _enh_method == "face_service":
            from app.core.task_queue import get_task_queue as _get_tq
            _enh_tq = _get_tq()
            _enh_track = _enh_tq.track_start(
                "face_enhance", "Face Enhancement (GFPGAN)",
                agent_name=character_name, provider="FaceService")
            try:
                logger.info("FACE ENHANCEMENT (GFPGAN) START")
                skill._apply_enhance_on_files(
                    [Path(actual_output_path).name], Path(actual_output_path).parent)
                _enhance_applied = True
                logger.info("FACE ENHANCEMENT (GFPGAN) ENDE")
                _enh_tq.track_finish(_enh_track)
            except Exception as _enh_err:
                logger.warning("Face Enhancement Fehler: %s", _enh_err)
                _enh_tq.track_finish(_enh_track, error=str(_enh_err)[:200])

        # Metadaten aktualisieren (Backend, Workflow, Duration, FaceSwap)
        _wf_name = active_workflow.name if active_workflow else ""
        # faceswap-Bool reflektiert jetzt den TATSAECHLICHEN Erfolg, nicht nur
        # die Absicht (vorher: needs_comfyui_faceswap or use_standalone_faceswap)
        _faceswap_applied = _faceswap_success
        import os as _os
        from app.models.character import get_character_current_location
        # Bei FaceSwap-Workflows liegen Referenzen in face_refs statt params
        _ref_source = params.get("reference_images") or face_refs.get("reference_images") or {}
        _ref_meta = {}
        for _rk, _rv in _ref_source.items():
            _ref_meta[_rk] = _os.path.basename(_rv) if _rv else ""
        _now_iso = __import__("datetime").datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # Original-Metadaten laden (fuer location, created_at und andere Felder)
        _orig_filename = Path(output_path).name
        _orig_meta = None
        if "/instagram/" in output_path:
            try:
                from app.models.instagram import load_image_meta as _load_ig_meta
                _orig_meta = _load_ig_meta(_orig_filename)
            except Exception:
                pass
        elif character_name:
            try:
                from app.models.character import _load_single_image_meta
                _orig_meta = _load_single_image_meta(character_name, _orig_filename)
            except Exception:
                pass
        _orig_meta = _orig_meta or {}

        # Location aus Original-Meta oder aus uebergebenem Parameter
        _location_val = location_id or _orig_meta.get("location", "")
        if not _location_val and character_name:
            _location_val = get_character_current_location(character_name) or ""

        _regen_meta = {
            "prompt": final_prompt,
            "negative_prompt": negative_prompt,
            "backend": backend.name,
            "backend_type": backend.api_type,
            "workflow": _wf_name,
            "faceswap": _faceswap_applied,
            "faceswap_method": _faceswap_method,
            "faceswap_fallback": _faceswap_fallback,
            "face_enhance": _enhance_applied,
            "guidance_scale": params.get("guidance_scale"),
            "num_inference_steps": params.get("num_inference_steps") or params.get("steps"),
            "duration_s": round(_gen_duration, 1),
            "regenerated_at": _now_iso,
            "reference_images": _ref_meta,
            "character_names": character_names if character_names is not None else [p["name"] for p in appearances],
            "room_id": room_id or _orig_meta.get("room_id", ""),
            "location": _location_val,
            "seed": params.get("seed", 0),
            # Model: Dialog-Override > backend.model > backend.last_used_checkpoint
            # — auch fuer Together/CivitAI sichtbar in der Bild-Info.
            "model": (
                params.get("unet")
                or params.get("model")
                or getattr(backend, "model", "")
                or getattr(backend, "last_used_checkpoint", "")
                or getattr(backend, "checkpoint", "")
                or ""),
            "loras": params.get("lora_inputs", []),
            # from_character: bei Regen aus Original-Meta erben — sonst geht
            # die Herkunft (z.B. "von Diego an Avatar gesendet") verloren.
            "from_character": _orig_meta.get("from_character", ""),
        }
        # Bei create_new: alle Original-Metadaten als Basis nehmen, dann mit neuen Werten ueberschreiben
        if create_new:
            _base_meta = dict(_orig_meta)
            # Felder entfernen die nicht uebernommen werden sollen
            _base_meta.pop("image_filename", None)
            _base_meta.pop("image_analysis", None)
            _base_meta.update(_regen_meta)
            _regen_meta = _base_meta
            # created_at vom Original uebernehmen (damit sie zusammen sortiert werden)
            if _orig_meta.get("created_at"):
                _regen_meta["created_at"] = _orig_meta["created_at"]
            else:
                _regen_meta["created_at"] = _now_iso
            _regen_meta["variant_of"] = _orig_filename
        _regen_filename = Path(actual_output_path).name
        if "/instagram/" in actual_output_path:
            try:
                from app.models.instagram import load_image_meta, save_image_meta
                existing_meta = load_image_meta(_regen_filename) or {}
                existing_meta.update(_regen_meta)
                save_image_meta(_regen_filename, existing_meta)
                logger.info("Instagram-Meta aktualisiert: workflow=%s, backend=%s", _wf_name, backend.name)
            except Exception as meta_err:
                logger.warning("Instagram-Meta Update fehlgeschlagen: %s", meta_err)
        elif character_name:
            try:
                from app.models.character import add_character_image_metadata
                add_character_image_metadata(character_name, _regen_filename, _regen_meta)
                logger.info("Character-Image-Meta aktualisiert: workflow=%s, backend=%s", _wf_name, backend.name)
            except Exception as meta_err:
                logger.warning("Character-Image-Meta Update fehlgeschlagen: %s", meta_err)

        # Image-Prompt loggen
        try:
            from app.utils.image_prompt_logger import log_image_prompt
            _model_name = getattr(backend, 'last_used_checkpoint', '') or getattr(backend, 'model', '') or getattr(backend, 'checkpoint', '') or ''
            log_image_prompt(
                agent_name=character_name,
                original_prompt=original_prompt,
                final_prompt=final_prompt,
                negative_prompt=negative_prompt,
                backend_name=backend.name,
                backend_type=backend.api_type,
                model=_model_name,
                auto_enhance=bool(improvement_request),
                duration_s=_gen_duration,
                loras=params.get("lora_inputs", []),
                reference_images=params.get("reference_images") or face_refs.get("reference_images") or {})
        except Exception as log_err:
            logger.warning(f"Logging-Fehler: {log_err}")

        # Bildanalyse via Vision-LLM (aktualisiert Metadaten)
        try:
            analysis = skill._generate_image_analysis(actual_output_path, character_name)
            if analysis:
                logger.info("Bildanalyse: %s", analysis[:120])
                _save_analysis(actual_output_path, analysis, character_name)
            else:
                logger.warning("Bildanalyse leer oder fehlgeschlagen")
        except Exception as ana_err:
            logger.warning("Bildanalyse-Fehler: %s", ana_err)

        return True, final_prompt, actual_output_path
    except Exception as e:
        logger.error(f"Generierung fehlgeschlagen: {e}")
        logger.debug("Traceback:", exc_info=True)
        raise
