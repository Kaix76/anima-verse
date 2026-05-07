"""Image Generation Skill - Multi-Instance Dispatcher mit Kosten-basierter Auswahl"""
import base64
import enum
import json
import os
import re
import time
import uuid

# Erkennt 4xx-Status-Codes in Exception-Strings (z.B. "400 Client Error",
# "HTTP 422", "Bad Request"). 4xx = Service erreichbar, Payload kaputt —
# Backend NICHT als unavailable markieren.
_re_4xx = re.compile(r"\b(?:HTTP\s*)?4(?:00|01|03|04|05|22)\b|Bad Request|Unprocessable", re.IGNORECASE)
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class WorkflowKind(str, enum.Enum):
    """Workflow-Familie. Bestimmt Ref-Slot-Layout, Exclusion-Regeln und Post-Processing.

    QWEN_STYLE: Style-Conditioning ueber input_reference_image_1..4
                (Slots 1-3 = Personen, Slot 4 = Location). Kein FaceSwap noetig.
    FLUX_BG:    input_reference_image_background + input_reference_image_use.
                Background-only. Charaktere kommen ueber Post-Processing.
    Z_IMAGE:    Keine Ref-Slots. Charaktere kommen ueber Post-Processing.
    """
    QWEN_STYLE = "qwen_style"
    FLUX_BG = "flux_bg"
    Z_IMAGE = "z_image"


@dataclass
class ComfyWorkflow:
    """Definition eines ComfyUI-Workflows (entkoppelt von Backends)."""
    name: str
    workflow_file: str
    faceswap_needed: bool
    model: str
    kind: WorkflowKind = WorkflowKind.Z_IMAGE  # erkannt aus Workflow-JSON-Nodes
    compatible_backends: list = field(default_factory=list)  # Backend-Namen, leer = alle ComfyUI
    prompt_style: str = "photorealistic"  # Stil-Adjektiv / Style-Keywords (Summary + Style-Zeile)
    image_model: str = ""
    prompt_instruction: str = ""
    vram_required_mb: int = 0  # VRAM-Bedarf in MB (ueberschreibt Backend-Default)
    has_input_unet: bool = False  # Workflow hat eigenen input_unet Node (nicht input_model)
    has_input_safetensors: bool = False  # Workflow hat input_safetensors Node (z.B. Flux2 UNETLoader)
    clip: str = ""              # CLIP-Modell fuer den Workflow
    has_loras: bool = False     # Workflow hat input_loras/input_lora Node
    default_loras: list = field(default_factory=list)  # [{name, strength}, ...] aus .env
    model_type: str = ""        # "unet" | "checkpoint" | "" — erkannt aus input_model/input_unet Node
    has_seed: bool = False      # Workflow hat input_seed Node
    has_separated_prompt: bool = False  # Workflow hat input_prompt_character/pose/expression Nodes
    width: int = 0              # Default-Breite (0 = Backend-Default nutzen)
    height: int = 0             # Default-Hoehe (0 = Backend-Default nutzen)
    filter: str = ""            # Glob-Pattern fuer Model/LoRA-Filterung im Frontend (z.B. "Z-Image*")
    negative_prompt: str = ""   # Default-Negativ-Prompt (ueberschreibt Backend-Default)
    fallback_specific: str = ""  # Workflow-Override fuer Backend-Fallback (leer = Backend-Default)

import requests

from .base import BaseSkill, ToolSpec
from .image_backends import ImageBackend, BACKEND_REGISTRY

from app.core.log import get_logger
from app.core.task_queue import get_task_queue
from app.core.tool_formats import format_example
from app.models.character import (
    get_character_images_dir,
    add_character_image,
    add_character_image_comment,
    add_character_image_prompt,
    set_character_profile_image,
    get_character_profile_image,
    get_character_profile,
    get_character_config,
    get_character_skill_config,
    save_character_skill_config,
    build_equipped_outfit_prompt,
    get_character_current_location,
    get_character_current_activity,
    get_character_current_feeling,
    get_character_current_room)
from app.models.account import (
    get_user_profile,
    get_user_gender,
    get_user_profile_image,
    get_user_images_dir)
from app.models.world import get_background_path
from app.utils.image_prompt_logger import log_image_prompt

logger = get_logger("image_gen")


class ImageGenerationSkill(BaseSkill):
    """
    Multi-Instance Image Generation Skill.

    Verwaltet mehrere Backends (A1111/Forge, Mammouth/OpenAI-kompatibel)
    und waehlt automatisch die guenstigste verfuegbare Instanz.

    Konfiguration:
        .env: Nummerierte Instanz-Bloecke SKILL_IMAGEGEN_{N}_*
        Per-Agent: storage/users/{user}/agents/{agent}/skills/image_generation.json
    """

    SKILL_ID = "image_generation"
    DEFERRED = True  # Bild wird erst nach Chat-Antwort generiert

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        from app.core.prompt_templates import load_skill_meta
        meta = load_skill_meta("image_generation")
        self.name = meta["name"]
        self.description = meta["description"]

        # Letzter verwendeter enhanced_prompt (fuer Caller wie Instagram)
        self.last_enhanced_prompt: str = ""

        # Thread-lokaler Slot fuer last_image_meta — notwendig damit parallele
        # Aufrufer (z.B. Expression-Regen fuer mehrere Characters gleichzeitig)
        # nicht die Meta-Daten des anderen ueberschreiben. Die instance-Variante
        # self.last_image_meta bleibt als Backward-Compat bestehen.
        import threading as _th
        self._meta_tls = _th.local()

        # Cache fuer ComfyUI-Modelle und LoRAs pro Service (wird beim Start geladen)
        # Format: {service_name: [model1, model2, ...]}
        self._cached_checkpoints_by_service: Dict[str, List[str]] = {}
        self._cached_unet_models_by_service: Dict[str, List[str]] = {}
        self._cached_loras_by_service: Dict[str, List[str]] = {}
        self._cached_clip_models_by_service: Dict[str, List[str]] = {}
        self._model_cache_loaded: bool = False

        # Round-Robin Counter pro Workflow-Name. Verteilt Tasks ueber
        # gleich-cost Backends (z.B. zwei lokale ComfyUI mit cost=0).
        # Ersetzt das alte LOAD_COST_PENALTY-Verfahren.
        self._round_robin_counter: Dict[str, int] = {}
        self._round_robin_lock = __import__("threading").Lock()

        # Lade alle konfigurierten Instanzen
        self.backends: List[ImageBackend] = self._load_instances()

        if not self.backends:
            logger.warning("Keine Image-Generation Instanzen konfiguriert")
            self.enabled = False
            return

        # Pruefe Verfuegbarkeit aller enabled Instanzen
        available_count = 0
        enabled_backends = [b for b in self.backends if b.instance_enabled]
        logger.info("Pruefe %d von %d Instanz(en)...", len(enabled_backends), len(self.backends))
        for backend in enabled_backends:
            if backend.check_availability():
                available_count += 1

        if available_count == 0:
            logger.warning("Keine Instanz verfuegbar")
            self.enabled = False
        else:
            logger.info("%d/%d Instanz(en) verfuegbar", available_count, len(self.backends))

        # Lade ComfyUI-Workflow-Definitionen (entkoppelt von Backends)
        self.comfy_workflows: List[ComfyWorkflow] = self._load_comfy_workflows()

        # Default-Workflow aus .env lesen (COMFY_IMAGEGEN_DEFAULT), Fallback auf ersten Workflow
        self._default_workflow: Optional[ComfyWorkflow] = self._resolve_default_workflow()

        # ImageGen nutzt ein eigenes per-Instanz Config-System (_get_instance_config)
        # statt der generischen BaseSkill._defaults. Daher keine _defaults fuer
        # get_config_fields() — die Konfiguration geschieht ueber die instanzbasierte
        # Config in storage/users/{user}/characters/{char}/skills/imagegen.json
        self._defaults = {}

    def get_config_fields(self) -> Dict[str, Dict[str, Any]]:
        """Top-level Config-Felder fuer den Character-Editor.

        ``faceswap_enabled`` lebt jetzt im Character-Profil (Bild-Tab),
        nicht mehr hier — Single Source of Truth. Migration erfolgt
        lazy beim ersten Read in ``_character_swap_disabled``.
        """
        return {
            "swap_mode": {
                "type": "select",
                "default": "default",
                "label": "Swap-Modus",
                "options": [
                    {"value": "default", "label": "Server-Einstellung"},
                    {"value": "internal", "label": "Internal (Face Service)"},
                    {"value": "comfyui", "label": "ComfyUI (ReActor)"},
                    {"value": "multiswap", "label": "MultiSwap (Flux.2)"},
                ],
                "help": "Methode fuer den Gesichts-Swap nach der Bildgenerierung",
            },
        }

    def _load_instances(self) -> List[ImageBackend]:
        """Scannt .env nach SKILL_IMAGEGEN_{N}_* Bloecken und erstellt Backends."""
        instances = []

        for n in range(1, 20):
            prefix = f"SKILL_IMAGEGEN_{n}_"
            api_type = os.environ.get(f"{prefix}API_TYPE", "").strip().lower()
            if not api_type:
                continue

            name = os.environ.get(f"{prefix}NAME", f"Instance_{n}")
            api_url = os.environ.get(f"{prefix}API_URL", "").strip()
            cost = float(os.environ.get(f"{prefix}COST", "0"))

            if not api_url:
                logger.warning("Instanz %d (%s): Keine API_URL konfiguriert, ueberspringe", n, name)
                continue

            backend_class = BACKEND_REGISTRY.get(api_type)
            if not backend_class:
                logger.warning("Instanz %d (%s): Unbekannter API-Typ '%s'", n, name, api_type)
                logger.info("Verfuegbare Typen: %s", ", ".join(BACKEND_REGISTRY.keys()))
                continue

            try:
                if api_type in ("mammouth", "civitai"):
                    api_key = os.environ.get(f"{prefix}API_KEY", "")
                    model = os.environ.get(f"{prefix}MODEL", "")
                    backend = backend_class(
                        name=name, api_url=api_url, cost=cost,
                        env_prefix=prefix, api_key=api_key, model=model
                    )
                else:
                    backend = backend_class(
                        name=name, api_url=api_url, cost=cost, env_prefix=prefix
                    )

                instances.append(backend)
                enabled_str = "enabled" if backend.instance_enabled else "DISABLED"
                logger.info("Instanz geladen: %s (Typ=%s, Cost=%s, %s)", name, api_type, cost, enabled_str)

            except Exception as e:
                logger.error("Fehler beim Laden von Instanz %d (%s): %s", n, name, e)

        return instances

    def _load_comfy_workflows(self) -> List[ComfyWorkflow]:
        """Scannt .env nach COMFY_IMAGEGEN_{ID}_* Bloecken und erstellt ComfyWorkflow-Objekte."""
        workflows = []
        # Sammle alle Workflow-IDs aus Umgebungsvariablen (numerisch oder benannt)
        import re as _re
        _wf_ids = set()
        for key in os.environ:
            m = _re.match(r"^COMFY_IMAGEGEN_(.+?)_NAME$", key)
            if m:
                _wf_ids.add(m.group(1))
        for wf_id in sorted(_wf_ids):
            prefix = f"COMFY_IMAGEGEN_{wf_id}_"
            wf_name = os.environ.get(f"{prefix}NAME", "").strip()
            if not wf_name:
                continue
            wf_file = os.environ.get(f"{prefix}WORKFLOW_FILE", "").strip()
            if not wf_file:
                logger.warning("ComfyWorkflow %s (%s): Kein WORKFLOW_FILE, ueberspringe", wf_id, wf_name)
                continue
            wf_faceswap = os.environ.get(
                f"{prefix}FACESWAP_NEEDED", "false"
            ).strip().lower() in ("true", "1", "yes")
            wf_model = os.environ.get(f"{prefix}MODEL", "").strip()
            wf_prompt_style = os.environ.get(f"{prefix}PROMPT_STYLE", "").strip() or "photorealistic"
            wf_negative_prompt = os.environ.get(f"{prefix}PROMPT_NEGATIVE", "").strip()
            wf_image_model = os.environ.get(f"{prefix}IMAGE_MODEL", "").strip()
            wf_prompt_instruction = os.environ.get(f"{prefix}PROMPT_INSTRUCTION", "").strip()
            # Kompatible Backends (kommasepariert), leer = alle ComfyUI-Backends
            raw_skills = os.environ.get(f"{prefix}SKILL", "").strip()
            compatible = [s.strip() for s in raw_skills.split(",") if s.strip()] if raw_skills else []
            # VRAM-Bedarf pro Workflow (GB → MB), ueberschreibt Backend-Default
            vram_str = os.environ.get(f"{prefix}VRAM_REQUIRED", "").strip()
            vram_mb = int(float(vram_str) * 1024) if vram_str else 0
            # Bildgroesse pro Workflow (ueberschreibt Backend-Default)
            wf_width = int(os.environ.get(f"{prefix}WIDTH", "0").strip() or 0)
            wf_height = int(os.environ.get(f"{prefix}HEIGHT", "0").strip() or 0)
            # LoRA-Defaults aus .env + has_loras-Flag durch Workflow-JSON-Check
            default_loras = []
            for i in range(1, 5):
                lora_name = os.environ.get(f"{prefix}LORA_0{i}", "").strip() or "None"
                strength_str = os.environ.get(f"{prefix}LORA_0{i}_STRENGTH", "1").strip()
                try:
                    strength = float(strength_str) if strength_str else 1.0
                except ValueError:
                    strength = 1.0
                default_loras.append({"name": lora_name, "strength": strength})
            # Filter-Pattern fuer Model/LoRA-Auswahl im Frontend (Glob-Syntax, z.B. "Z-Image*")
            wf_filter = os.environ.get(f"{prefix}FILTER", "").strip()
            # Workflow-JSON pruefen ob input_loras/input_lora Node vorhanden + model_type ermitteln
            has_loras = False
            has_seed = False
            has_separated_prompt = False
            has_input_unet = False
            has_input_safetensors = False
            model_type = ""
            kind = WorkflowKind.Z_IMAGE
            try:
                import json as _json
                _wf_path = os.path.normpath(os.path.join(os.path.dirname(__file__), "../../", wf_file))
                if os.path.exists(_wf_path):
                    with open(_wf_path) as _f:
                        _wf_data = _json.load(_f)
                    _titles = {
                        node.get("_meta", {}).get("title", "")
                        for node in _wf_data.values() if isinstance(node, dict)
                    }
                    has_loras = bool(_titles & {"input_loras", "input_lora"})
                    has_seed = "input_seed" in _titles
                    has_separated_prompt = {
                        "input_prompt_character", "input_prompt_pose",
                        "input_prompt_expression"
                    }.issubset(_titles)
                    # Workflow-Familie klassifizieren:
                    # QWEN_STYLE hat Slot-4 (Location) und damit alle 4 Person/Location-Slots.
                    # FLUX_BG hat einen einzelnen Background-Slot mit Use-Schalter.
                    # Sonst Z_IMAGE (keine Ref-Slots).
                    if "input_reference_image_4" in _titles:
                        kind = WorkflowKind.QWEN_STYLE
                    elif "input_reference_image_background" in _titles:
                        kind = WorkflowKind.FLUX_BG
                    else:
                        kind = WorkflowKind.Z_IMAGE
                    _model_node = next(
                        (node for node in _wf_data.values()
                         if isinstance(node, dict) and node.get("_meta", {}).get("title") == "input_model"),
                        None)
                    if _model_node:
                        _ct = _model_node.get("class_type", "")
                        if _ct == "UNETLoader":
                            model_type = "unet"
                        elif _ct in ("CheckpointLoaderSimple", "CheckpointLoader"):
                            model_type = "checkpoint"
                    # input_unet Node erkennen (z.B. UnetLoaderGGUF)
                    if not model_type:
                        _unet_node = next(
                            (node for node in _wf_data.values()
                             if isinstance(node, dict) and node.get("_meta", {}).get("title") == "input_unet"),
                            None)
                        if _unet_node:
                            model_type = "unet"
                            has_input_unet = True
                    # input_safetensors Node erkennen (z.B. Flux2 UNETLoader)
                    if not model_type:
                        _st_node = next(
                            (node for node in _wf_data.values()
                             if isinstance(node, dict) and node.get("_meta", {}).get("title") == "input_safetensors"),
                            None)
                        if _st_node:
                            model_type = "unet"
                            has_input_safetensors = True
            except Exception as _e:
                logger.debug("Workflow-JSON Check fehlgeschlagen fuer %s: %s", wf_file, _e)
            wf_clip = os.environ.get(f"{prefix}CLIP", "").strip()
            wf_fallback_specific = os.environ.get(f"{prefix}FALLBACK_SPECIFIC", "").strip()
            wf = ComfyWorkflow(
                name=wf_name,
                workflow_file=wf_file,
                faceswap_needed=wf_faceswap,
                model=wf_model,
                kind=kind,
                has_input_unet=has_input_unet,
                has_input_safetensors=has_input_safetensors,
                clip=wf_clip,
                compatible_backends=compatible,
                prompt_style=wf_prompt_style,
                negative_prompt=wf_negative_prompt,
                image_model=wf_image_model,
                prompt_instruction=wf_prompt_instruction,
                vram_required_mb=vram_mb,
                has_loras=has_loras,
                has_seed=has_seed,
                has_separated_prompt=has_separated_prompt,
                default_loras=default_loras,
                model_type=model_type,
                width=wf_width,
                height=wf_height,
                filter=wf_filter,
                fallback_specific=wf_fallback_specific)
            workflows.append(wf)
            _size_info = f", size={wf_width}x{wf_height}" if wf_width or wf_height else ""
            _filter_info = f", filter={wf_filter}" if wf_filter else ""
            logger.info("ComfyWorkflow geladen: '%s' (file=%s, kind=%s, faceswap=%s, model=%s%s%s, backends=%s)", wf_name, wf_file, kind.value, wf_faceswap, wf_model, _size_info, _filter_info, compatible or "alle")
        if workflows:
            logger.info("%d ComfyUI-Workflow(s) geladen", len(workflows))
        return workflows

    def _resolve_default_workflow(self) -> Optional[ComfyWorkflow]:
        """Liest COMFY_IMAGEGEN_DEFAULT aus .env und findet den passenden Workflow.
        Fallback auf ersten Workflow wenn nicht gesetzt oder nicht gefunden."""
        if not self.comfy_workflows:
            return None
        default_name = os.environ.get("COMFY_IMAGEGEN_DEFAULT", "").strip()
        if default_name:
            # Exakter Match, dann case-insensitive Fallback
            for wf in self.comfy_workflows:
                if wf.name == default_name:
                    logger.info("Default-Workflow: '%s'", default_name)
                    return wf
            default_lower = default_name.lower()
            for wf in self.comfy_workflows:
                if wf.name.lower() == default_lower:
                    logger.info("Default-Workflow: '%s' (matched '%s')", wf.name, default_name)
                    return wf
            logger.warning(
                "COMFY_IMAGEGEN_DEFAULT='%s' nicht gefunden (verfuegbar: %s), nutze ersten Workflow",
                default_name, ", ".join(wf.name for wf in self.comfy_workflows))
        return self.comfy_workflows[0]

    def load_comfyui_model_cache(self) -> None:
        """Laedt Checkpoints, UNet-Modelle und LoRAs von ALLEN erreichbaren ComfyUI-Backends
        und speichert sie pro Service im Cache. Wird beim Serverstart aufgerufen."""
        comfyui_backends = [
            b for b in self.backends if getattr(b, "api_type", "") == "comfyui" and b.available
        ]
        if not comfyui_backends:
            logger.warning("Kein erreichbares ComfyUI-Backend fuer Model-Cache gefunden")
            return

        self._cached_checkpoints_by_service = {}
        self._cached_unet_models_by_service = {}
        self._cached_loras_by_service = {}

        for backend in comfyui_backends:
            svc_name = backend.name
            api_url = backend.api_url

            # Checkpoints laden
            checkpoints = []
            try:
                resp = requests.get(f"{api_url}/models/checkpoints", timeout=10)
                if resp.ok:
                    raw = resp.json()
                    if isinstance(raw, list):
                        checkpoints = sorted(set(raw))
            except Exception as e:
                logger.warning("Model-Cache [%s]: Checkpoints konnten nicht geladen werden: %s", svc_name, e)
            self._cached_checkpoints_by_service[svc_name] = checkpoints
            if checkpoints:
                logger.info("Model-Cache [%s]: %d Checkpoints", svc_name, len(checkpoints))

            # UNet/Diffusion-Modelle laden
            unet_models = []
            for ep in ["diffusion_models", "unet", "unet_gguf"]:
                try:
                    resp = requests.get(f"{api_url}/models/{ep}", timeout=10)
                    if resp.ok:
                        raw = resp.json()
                        if isinstance(raw, list):
                            unet_models.extend(raw)
                except Exception as e:
                    logger.warning("Model-Cache [%s]: %s konnten nicht geladen werden: %s", svc_name, ep, e)
            self._cached_unet_models_by_service[svc_name] = sorted(set(unet_models))
            if unet_models:
                logger.info("Model-Cache [%s]: %d UNet/Diffusion-Modelle", svc_name, len(self._cached_unet_models_by_service[svc_name]))

            # LoRAs laden
            loras = []
            try:
                resp = requests.get(f"{api_url}/models/loras", timeout=10)
                if resp.ok:
                    raw = resp.json()
                    if isinstance(raw, list):
                        loras = sorted(raw)
            except Exception as e:
                logger.warning("Model-Cache [%s]: LoRAs konnten nicht geladen werden: %s", svc_name, e)
            self._cached_loras_by_service[svc_name] = loras
            if loras:
                logger.info("Model-Cache [%s]: %d LoRAs", svc_name, len(loras))

            # CLIP / Text Encoder Modelle laden — inkl. GGUF-Varianten
            # (ComfyUI-GGUF city96 registriert sie unter /models/clip_gguf)
            clip_models = []
            for ep in ["clip", "text_encoders", "clip_gguf"]:
                try:
                    resp = requests.get(f"{api_url}/models/{ep}", timeout=10)
                    if resp.ok:
                        raw = resp.json()
                        if isinstance(raw, list):
                            clip_models.extend(raw)
                except Exception:
                    pass
            self._cached_clip_models_by_service[svc_name] = sorted(set(clip_models))
            if clip_models:
                logger.info("Model-Cache [%s]: %d CLIP/Text-Encoder", svc_name, len(self._cached_clip_models_by_service[svc_name]))

        self._model_cache_loaded = True
        total_cp = sum(len(v) for v in self._cached_checkpoints_by_service.values())
        total_unet = sum(len(v) for v in self._cached_unet_models_by_service.values())
        total_loras = sum(len(v) for v in self._cached_loras_by_service.values())
        logger.info("Model-Cache: %d Services, %d Checkpoints, %d UNets, %d LoRAs gesamt",
                     len(comfyui_backends), total_cp, total_unet, total_loras)

    def get_cached_checkpoints(self, model_type: str = "") -> List[str]:
        """Gibt alle gecachten Modelle zurueck (ueber alle Services kombiniert), gefiltert nach model_type."""
        all_checkpoints = set()
        all_unets = set()
        for models in self._cached_checkpoints_by_service.values():
            all_checkpoints.update(models)
        for models in self._cached_unet_models_by_service.values():
            all_unets.update(models)
        if model_type == "unet":
            return sorted(all_unets)
        elif model_type == "checkpoint":
            return sorted(all_checkpoints)
        else:
            return sorted(all_checkpoints | all_unets)

    def get_cached_checkpoints_by_service(self, model_type: str = "") -> Dict[str, List[str]]:
        """Gibt gecachte Modelle gruppiert nach Service zurueck."""
        result: Dict[str, List[str]] = {}
        for svc_name in set(list(self._cached_checkpoints_by_service.keys()) +
                            list(self._cached_unet_models_by_service.keys())):
            cp = self._cached_checkpoints_by_service.get(svc_name, [])
            unet = self._cached_unet_models_by_service.get(svc_name, [])
            if model_type == "unet":
                models = unet
            elif model_type == "checkpoint":
                models = cp
            else:
                models = sorted(set(cp + unet))
            if models:
                result[svc_name] = models
        return result

    def get_cached_loras(self) -> List[str]:
        """Gibt gecachte LoRA-Liste zurueck (ueber alle Services kombiniert)."""
        all_loras = set()
        for loras in self._cached_loras_by_service.values():
            all_loras.update(loras)
        return sorted(all_loras)

    def get_cached_loras_by_service(self) -> Dict[str, List[str]]:
        """Gibt gecachte LoRAs gruppiert nach Service zurueck."""
        return dict(self._cached_loras_by_service)

    def get_cached_clip_models(self) -> List[str]:
        """Gibt gecachte CLIP/Text-Encoder-Liste zurueck (ueber alle Services kombiniert)."""
        all_clips = set()
        for clips in self._cached_clip_models_by_service.values():
            all_clips.update(clips)
        return sorted(all_clips)

    @staticmethod
    def fetch_models_from_url(api_url: str, model_type: str = "") -> List[str]:
        """Holt Modelle direkt von einer ComfyUI-URL (live, nicht aus Cache)."""
        models = []
        if model_type in ("", "checkpoint"):
            try:
                resp = requests.get(f"{api_url}/models/checkpoints", timeout=10)
                if resp.ok:
                    raw = resp.json()
                    if isinstance(raw, list):
                        models.extend(raw)
            except Exception:
                pass
        if model_type in ("", "unet"):
            for ep in ["diffusion_models", "unet", "unet_gguf"]:
                try:
                    resp = requests.get(f"{api_url}/models/{ep}", timeout=10)
                    if resp.ok:
                        raw = resp.json()
                        if isinstance(raw, list):
                            models.extend(raw)
                except Exception:
                    pass
        return sorted(set(models))

    @staticmethod
    def find_closest_model(target: str, available: List[str]) -> str:
        """Findet das namentlich aehnlichste Modell in einer Liste.

        Vergleicht den Basis-Namen (ohne Dateiendung) und berechnet die
        Aehnlichkeit mittels SequenceMatcher. Gibt das beste Match zurueck,
        oder leer wenn kein ausreichend aehnliches Modell gefunden wird.
        """
        if not target or not available:
            return ""
        if target in available:
            return target

        from difflib import SequenceMatcher

        def _base(name: str) -> str:
            """Extrahiert Basis-Name ohne Dateiendung und Pfad."""
            basename = name.rsplit("/", 1)[-1] if "/" in name else name
            dot = basename.rfind(".")
            return basename[:dot].lower() if dot > 0 else basename.lower()

        target_base = _base(target)
        best_match = ""
        best_ratio = 0.0

        for m in available:
            m_base = _base(m)
            ratio = SequenceMatcher(None, target_base, m_base).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = m

        # Mindest-Aehnlichkeit: 40% damit nicht voellig andere Modelle gewaehlt werden
        if best_ratio >= 0.4:
            logger.info("find_closest_model: '%s' -> '%s' (%.0f%% Aehnlichkeit)", target, best_match, best_ratio * 100)
            return best_match

        logger.warning("find_closest_model: Kein ausreichend aehnliches Modell fuer '%s' gefunden (bestes: '%s' mit %.0f%%)",
                        target, best_match, best_ratio * 100)
        return ""

    def resolve_model_for_backend(self, model_name: str, backend: 'ImageBackend', model_type: str = "") -> str:
        """Prueft ob ein Modell auf dem Ziel-Backend verfuegbar ist und findet ggf. das aehnlichste.

        Nutzt den Cache fuer den Abgleich. Gibt den aufgeloesten Modellnamen zurueck.
        """
        if not model_name or not self._model_cache_loaded:
            return model_name
        svc_name = backend.name
        # Modelle dieses Backends aus Cache holen
        if model_type == "unet":
            svc_models = self._cached_unet_models_by_service.get(svc_name, [])
        elif model_type == "checkpoint":
            svc_models = self._cached_checkpoints_by_service.get(svc_name, [])
        else:
            svc_models = sorted(set(
                self._cached_checkpoints_by_service.get(svc_name, []) +
                self._cached_unet_models_by_service.get(svc_name, [])
            ))
        if not svc_models:
            return model_name  # Kein Cache fuer dieses Backend
        if model_name in svc_models:
            return model_name  # Modell direkt verfuegbar
        # Aehnlichstes Modell suchen
        resolved = self.find_closest_model(model_name, svc_models)
        if not resolved:
            logger.warning("resolve_model_for_backend: Modell '%s' nicht auf Backend '%s' verfuegbar und kein aehnliches gefunden — verwende erstes verfuegbares: '%s'",
                           model_name, svc_name, svc_models[0])
            return svc_models[0]
        return resolved

    def pick_lowest_cost(
        self,
        candidates: List[ImageBackend],
        rotation_key: str = "default",
    ) -> Optional[ImageBackend]:
        """Pickt aus den Kandidaten das billigste verfuegbare Backend.

        Bei mehreren gleich-cost Backends (z.B. zwei lokale ComfyUI mit
        cost=0) wird per Round-Robin verteilt — verhindert dass alle
        Tasks immer auf das gleiche Backend gehen ohne LOAD_COST_PENALTY.
        Counter ist pro `rotation_key` (typisch: workflow_name).
        """
        if not candidates:
            return None
        # Sortiere nach cost und gruppiere gleich-cost zusammen
        sorted_c = sorted(candidates, key=lambda b: b.effective_cost)
        cheapest_cost = sorted_c[0].effective_cost
        tier = [b for b in sorted_c if b.effective_cost == cheapest_cost]
        if len(tier) == 1:
            return tier[0]
        # Mehrere gleich-cost -> Round-Robin pro rotation_key
        with self._round_robin_lock:
            idx = self._round_robin_counter.get(rotation_key, 0)
            self._round_robin_counter[rotation_key] = idx + 1
        return tier[idx % len(tier)]

    def _select_backend(self) -> Optional[ImageBackend]:
        """Waehlt das guenstigste verfuegbare und global-enabled Backend."""
        available = [b for b in self.backends if b.available and b.instance_enabled]
        return self.pick_lowest_cost(available, rotation_key="_select_backend")

    def _ensure_agent_config(self, character_name: str) -> Dict[str, Any]:
        """Erstellt automatisch eine per-Agent Skill-Config mit .env-Defaults, falls noch keine existiert."""
        agent_config = get_character_skill_config(character_name, self.SKILL_ID)

        if agent_config and "instances" in agent_config:
            # Config existiert - pruefen ob neue Backends fehlen
            changed = False
            existing_names = set(agent_config["instances"].keys())
            backend_names = {b.name for b in self.backends}
            missing = backend_names - existing_names
            if missing:
                for b in self.backends:
                    if b.name in missing:
                        agent_config["instances"][b.name] = self._get_backend_defaults(b)
                        logger.info("Auto-Config: Backend '%s' fuer %s hinzugefuegt", b.name, character_name)
                changed = True
            # Auto-Migration: comfy_workflow Feld hinzufuegen falls fehlend
            if self._default_workflow and "comfy_workflow" not in agent_config:
                agent_config["comfy_workflow"] = self._default_workflow.name
                logger.info("Auto-Config: comfy_workflow='%s' fuer %s", self._default_workflow.name, character_name)
                changed = True
            if changed:
                save_character_skill_config(character_name, self.SKILL_ID, agent_config)
            return agent_config

        # Keine Config vorhanden - erstelle mit Defaults aller Backends
        agent_config = {
            "instances": {}
        }
        for b in self.backends:
            agent_config["instances"][b.name] = self._get_backend_defaults(b)
        # ComfyUI-Workflow Default setzen
        if self._default_workflow:
            agent_config["comfy_workflow"] = self._default_workflow.name

        save_character_skill_config(character_name, self.SKILL_ID, agent_config)
        logger.info("Auto-Config fuer %s erstellt: %s", character_name, list(agent_config["instances"].keys()))
        return agent_config

    def _select_backend_for_agent(self, character_name: str) -> Optional[ImageBackend]:
        """Waehlt die guenstigste Instanz unter Beruecksichtigung der per-Agent enabled Flags."""
        agent_config = self._ensure_agent_config(character_name)
        agent_instances = agent_config.get("instances", {})

        available = []
        for b in self.backends:
            if not b.available:
                continue
            # Per-Agent Override hat Vorrang, sonst .env Default
            agent_inst = agent_instances.get(b.name, {})
            if "enabled" in agent_inst:
                is_enabled = bool(agent_inst["enabled"])
            else:
                is_enabled = b.instance_enabled
            if is_enabled:
                # ComfyUI-Backends ohne Workflow und ohne Default-Workflow ueberspringen
                if b.api_type == "comfyui" and not getattr(b, 'workflow_file', "") and not self._default_workflow:
                    continue
                available.append(b)

        return self.pick_lowest_cost(
            available, rotation_key=f"agent:{character_name}")

    def _get_active_workflow(self, character_name: str) -> Optional[ComfyWorkflow]:
        """Liest den aktiven ComfyUI-Workflow fuer diesen Agent aus der per-Agent Config."""
        if not self.comfy_workflows:
            return None
        agent_config = get_character_skill_config(character_name, self.SKILL_ID)
        wf_name = (agent_config or {}).get("comfy_workflow", "")
        if wf_name:
            for wf in self.comfy_workflows:
                if wf.name == wf_name:
                    return wf
            logger.warning(
                "Konfigurierter Workflow '%s' fuer %s nicht gefunden "
                "(verfuegbar: %s)",
                wf_name, character_name,
                ", ".join(wf.name for wf in self.comfy_workflows))
            return None
        # Kein Workflow explizit konfiguriert — Default aus .env (COMFY_IMAGEGEN_DEFAULT)
        return self._default_workflow

    def _select_backend_for_workflow(self, workflow: ComfyWorkflow, character_name: str) -> Optional[ImageBackend]:
        """Waehlt den guenstigsten verfuegbaren Backend fuer einen Workflow.

        Beruecksichtigt kompatible Backends (workflow.compatible_backends),
        per-Agent enabled-Flags und globale Verfuegbarkeit.
        Bei mehreren gleich-cost Backends -> Round-Robin pro Workflow.
        """
        agent_config = self._ensure_agent_config(character_name)
        agent_instances = agent_config.get("instances", {})

        available = []
        for b in self.backends:
            if not b.available:
                continue
            # Nur ComfyUI-Backends kommen in Frage
            if b.api_type != "comfyui":
                continue
            # Kompatibilitaet pruefen
            if workflow.compatible_backends and b.name not in workflow.compatible_backends:
                continue
            # Per-Agent Override hat Vorrang
            agent_inst = agent_instances.get(b.name, {})
            if "enabled" in agent_inst:
                is_enabled = bool(agent_inst["enabled"])
            else:
                is_enabled = b.instance_enabled
            if is_enabled:
                available.append(b)

        return self.pick_lowest_cost(
            available, rotation_key=f"workflow:{workflow.name}")

    def list_available_backends(
        self,
        character_name: str = "",
        workflow: Optional[ComfyWorkflow] = None,
        comfyui_only: bool = False,
    ) -> List[ImageBackend]:
        """Liste aller verfuegbaren Backends fuer Helper-API + Engine.

        Filter:
        - b.available (live-status; channel_health setzt das ggf. False)
        - b.instance_enabled (.env Flag)
        - per-Agent Override (agent_config.instances[name].enabled)
        - workflow.compatible_backends (wenn workflow gegeben + Liste nicht leer)
        - api_type = comfyui wenn comfyui_only=True ODER workflow gegeben

        Sortiert aufsteigend nach effective_cost. KEIN Round-Robin hier —
        Selektor (oder UI) kuemmert sich um Verteilung. Diese Liste ist
        fuer Display und Engine gedacht.
        """
        agent_instances: Dict[str, Any] = {}
        if character_name:
            agent_config = self._ensure_agent_config(character_name)
            agent_instances = agent_config.get("instances", {}) or {}

        out: List[ImageBackend] = []
        for b in self.backends:
            if not b.available:
                continue
            if comfyui_only or workflow is not None:
                if b.api_type != "comfyui":
                    continue
            if workflow is not None and workflow.compatible_backends:
                if b.name not in workflow.compatible_backends:
                    continue
            agent_inst = agent_instances.get(b.name, {})
            if "enabled" in agent_inst:
                if not bool(agent_inst["enabled"]):
                    continue
            elif not b.instance_enabled:
                continue
            out.append(b)

        out.sort(key=lambda x: x.effective_cost)
        return out

    # ------------------------------------------------------------------
    # Backend-Fallback-Engine
    # ------------------------------------------------------------------

    def _pick_fallback_backend(
        self,
        failed: ImageBackend,
        workflow: Optional[ComfyWorkflow],
        character_name: str,
        exclude: Set[str],
    ) -> Optional[ImageBackend]:
        """Pickt das naechste Backend nach failed.fallback_mode.

        - none: kein Fallback, returnt None
        - specific: das in failed.fallback_specific eingetragene Backend
          (auch wenn anderer api_type — Caller muss damit umgehen).
          Workflow-Override (workflow.fallback_specific) gewinnt wenn gesetzt
          — so kann z.B. Z-Image und Qwen auf demselben Backend
          unterschiedliche Fallbacks haben.
        - next_cheaper: naechst-billigeres kompatibles Backend (workflow-
          aware wenn workflow gegeben)
        """
        mode = failed.fallback_mode

        # Workflow-Override greift ZUERST (unabhaengig vom Backend-mode):
        # wenn der aktive Workflow ein eigenes Fallback-Backend definiert,
        # nehmen wir das — sonst Backend-Default.
        wf_target = (workflow.fallback_specific.strip()
                     if workflow and getattr(workflow, "fallback_specific", "")
                     else "")
        if wf_target:
            target = next((b for b in self.backends if b.name == wf_target), None)
            if target and target.name not in exclude and target.available and target.instance_enabled:
                logger.info("Fallback via Workflow-Override '%s' -> %s",
                            workflow.name, target.name)
                return target
            logger.warning("Workflow-Override '%s' -> '%s' nicht verfuegbar, nutze Backend-Default",
                           workflow.name, wf_target)

        if mode == "none":
            return None

        if mode == "specific":
            target_name = failed.fallback_specific
            if not target_name:
                return None
            target = next((b for b in self.backends if b.name == target_name), None)
            if not target or target.name in exclude:
                return None
            if not target.available or not target.instance_enabled:
                return None
            return target

        # next_cheaper (Default)
        # Workflow-Compat respektieren wenn primary ein ComfyUI war und
        # wir auf ComfyUI bleiben wollen. Bei anderen Backend-Typen
        # (CivitAI, Together) gibt's keinen Workflow -> alle gleichwertig.
        if workflow is not None and failed.api_type == "comfyui":
            candidates = self.list_available_backends(
                character_name=character_name, workflow=workflow)
        else:
            candidates = self.list_available_backends(character_name=character_name)
        candidates = [b for b in candidates if b.name not in exclude]
        return candidates[0] if candidates else None

    def run_with_fallback(
        self,
        primary_backend: ImageBackend,
        op: Callable[[ImageBackend], Any],
        workflow: Optional[ComfyWorkflow] = None,
        character_name: str = "",
        max_attempts: int = 3,
    ) -> Tuple[Any, ImageBackend]:
        """Fuehrt op(backend) aus, faellt bei Fehler auf naechstes Backend zurueck.

        Strategie:
        - Versuche primary_backend
        - Bei Exception ODER leerer Liste: setze backend.available=False,
          waehle Fallback per backend.fallback_mode (none/next_cheaper/specific)
        - Wiederhole bis success, max_attempts erreicht oder Fallback-Kette aus
        - "NO_NEW_IMAGE" Sentinel-String wird durchgereicht (kein Fail)

        op(backend) -> List[bytes] | "NO_NEW_IMAGE" | [] | None
        Caller ist dafuer zustaendig, params/Workflow pro Backend anzupassen
        (z.B. unterschiedlicher api_type bei specific-Fallback).

        Returns (result, used_backend) bei Erfolg.
        Raises RuntimeError nach Erschoepfung.
        """
        if not primary_backend:
            raise RuntimeError("run_with_fallback: kein primary_backend uebergeben")

        tried: Set[str] = set()
        last_error: Optional[Exception] = None
        current = primary_backend

        for attempt in range(max_attempts):
            if not current or current.name in tried:
                break
            tried.add(current.name)

            logger.info("Fallback-Engine Versuch %d/%d: backend=%s (cost=%s, mode=%s)",
                        attempt + 1, max_attempts, current.name,
                        current.cost, current.fallback_mode)

            try:
                result = op(current)
            except Exception as e:
                last_error = e
                # Unterscheidung: Connection/Server-Probleme vs. Payload-Fehler.
                # 4xx-Fehler (HTTP 400/422 wie Workflow-Validation) bedeuten der
                # Service ist erreichbar — nur das gesendete JSON ist kaputt.
                # In dem Fall NICHT als unavailable markieren, sonst wird das
                # Backend vom Pool entfernt und nachgelagerte Steps (z.B.
                # MultiSwap) finden faelschlich kein ComfyUI mehr.
                _err_str = str(e)
                _is_payload_err = bool(_re_4xx.search(_err_str))
                if _is_payload_err:
                    logger.warning(
                        "Fallback-Engine: %s warf Payload-Fehler (%s: %s) — Backend bleibt verfuegbar, "
                        "versuche anderen Backend (Workflow/Prompt vermutlich inkompatibel)",
                        current.name, type(e).__name__, _err_str[:200])
                else:
                    logger.warning(
                        "Fallback-Engine: %s warf Exception (%s: %s) — Backend als unavailable markiert, versuche Fallback",
                        current.name, type(e).__name__, _err_str[:200])
                    current.available = False
                current = self._pick_fallback_backend(
                    current, workflow, character_name, tried)
                continue

            # Cache-Hit-Sentinel: erfolgreich, kein Fail
            if result == "NO_NEW_IMAGE":
                return result, current

            # Liste mit Bytes -> erfolgreich
            if result:
                return result, current

            # Leeres Resultat = Fail, naechstes probieren
            logger.warning("Fallback-Engine: %s lieferte leeres Ergebnis — versuche Fallback",
                           current.name)
            current.available = False
            current = self._pick_fallback_backend(
                current, workflow, character_name, tried)

        _err_suffix = f" (letzter Fehler: {type(last_error).__name__}: {last_error})" if last_error else ""
        raise RuntimeError(
            f"Fallback-Engine: alle {len(tried)} probierten Backends fehlgeschlagen "
            f"({', '.join(sorted(tried))}){_err_suffix}")

    _BACKEND_WAIT_INTERVAL = 10   # Sekunden zwischen Retry-Checks
    _BACKEND_WAIT_MAX = 120        # Max. Wartezeit in Sekunden

    def _wait_for_backend(
        self, workflow, character_name: str, workflow_only: bool = False):
        """Wartet bis ein passendes Backend verfuegbar wird.

        Args:
            workflow: ComfyWorkflow (oder None fuer beliebiges Backend)
            workflow_only: True = nur ComfyUI-Backends fuer Workflow pruefen

        Fail-fast: Wenn KEIN passendes Backend ueberhaupt instance_enabled ist
        (also nicht "gerade unavailable" sondern strukturell nicht konfiguriert),
        wird sofort abgebrochen — sonst stapeln sich Hintergrund-Threads
        (z.B. expression_regen) jeweils 120s lang an einer dauerhaft
        unmoeglichen Bedingung.
        """
        # Fail-fast: gibt es ueberhaupt ein Backend das fuer Workflow+Agent
        # in Frage kaeme, wenn alle "verfuegbar" waeren?
        compat = workflow.compatible_backends if workflow else []
        agent_instances: Dict[str, Any] = {}
        if character_name:
            try:
                _agent_cfg = get_character_skill_config(character_name, "image_generation") or {}
                agent_instances = _agent_cfg.get("instances", {}) or {}
            except Exception:
                agent_instances = {}
        plausible = []
        for b in self.backends:
            if not b.instance_enabled:
                continue
            if workflow_only and b.api_type != "comfyui":
                continue
            if compat and b.name not in compat:
                continue
            agent_inst = agent_instances.get(b.name, {})
            if "enabled" in agent_inst and not agent_inst["enabled"]:
                continue
            plausible.append(b)
        if not plausible:
            logger.warning(
                "_wait_for_backend: kein konfiguriertes Backend kann diesen "
                "Workflow/Agent jemals erfuellen (workflow=%s, agent=%s) — "
                "fail-fast statt %ds Warten",
                getattr(workflow, "name", None), character_name or "n/a",
                self._BACKEND_WAIT_MAX)
            return None

        # Eine einzige Pruefrunde — kein 120s-Polling mehr.
        # Hintergrund-Poller (channel_health) erkennt Recovery alle 30s
        # automatisch; der naechste Generate-Aufruf sieht den frischen
        # Status. Es bringt nichts, hier 12x in Serie zu pollen waehrend
        # man dem User die Hand auf den Spawn legt.
        for b in plausible:
            b.check_availability()
        if workflow:
            backend = self._select_backend_for_workflow(workflow, character_name)
        else:
            backend = self._select_backend_for_agent(character_name)
        if backend:
            return backend
        logger.warning(
            "_wait_for_backend: kein Backend verfuegbar (workflow=%s) — fail-fast "
            "(channel_health pollt im Hintergrund weiter, naechster Versuch sieht aktuellen Status)",
            getattr(workflow, "name", None))
        return None

    def _wait_for_explicit_backend(self, backend_name: str):
        """Pruefe einmalig ob ein explizit gewaehltes Backend verfuegbar ist.

        Frueher: 120s-Polling-Schleife. Jetzt: ein Check, fail-fast.
        Recovery erkennt der Background-Poller (channel_health) alle 30s.
        """
        target = next((b for b in self.backends if b.name == backend_name), None)
        if not target:
            return None
        target.check_availability()
        if target.available:
            return target
        logger.warning("Backend '%s' nicht verfuegbar — fail-fast", backend_name)
        return None

    def get_comfy_workflows(self, only_available: bool = False) -> List[Dict[str, Any]]:
        """Public: Gibt Liste aller definierten ComfyUI-Workflows zurueck (fuer API).

        only_available=True filtert Workflows aus, deren kompatible Backends
        alle nicht erreichbar sind.
        """
        result = []
        for wf in self.comfy_workflows:
            wf_available = self._workflow_has_available_backend(wf)
            if only_available and not wf_available:
                continue
            result.append({
                "name": wf.name,
                "workflow_file": wf.workflow_file,
                "faceswap_needed": wf.faceswap_needed,
                "model": wf.model,
                "compatible_backends": wf.compatible_backends,
                "prompt_style": wf.prompt_style,
                "negative_prompt": wf.negative_prompt,
                "has_loras": wf.has_loras,
                "has_seed": wf.has_seed,
                "default_loras": wf.default_loras,
                "model_type": wf.model_type,
                "default_model": wf.model,
                "width": wf.width,
                "height": wf.height,
                "filter": wf.filter,
                "available": wf_available,
            })
        return result

    def _workflow_has_available_backend(self, workflow: "ComfyWorkflow") -> bool:
        """True wenn mindestens ein kompatibles Backend des Workflows erreichbar ist."""
        for b in self.backends:
            if not b.available:
                continue
            if b.api_type != "comfyui":
                continue
            if not b.instance_enabled:
                continue
            if workflow.compatible_backends and b.name not in workflow.compatible_backends:
                continue
            return True
        return False

    def _get_backend_defaults(self, backend: ImageBackend) -> Dict[str, Any]:
        """Holt die Instanz-spezifischen Defaults (nur agent-level Overrides).

        Technische Backend-Parameter (guidance_scale, num_inference_steps, checkpoint,
        sampling_method, schedule_type, sampler, scheduler) kommen direkt vom Backend/.env
        und werden NICHT in die per-Agent Config geschrieben.
        """
        defaults = {
            "enabled": backend.instance_enabled,
            "prompt_prefix": backend.prompt_prefix,
            "negative_prompt": backend.negative_prompt,
        }
        if hasattr(backend, 'width'):
            defaults["width"] = backend.width
        if hasattr(backend, 'height'):
            defaults["height"] = backend.height
        # workflow_file ist ein technischer Backend-Parameter und wird
        # NICHT in die per-Agent Config geschrieben (kommt direkt vom Backend/.env)
        return defaults

    def _get_instance_config(self, character_name: str, backend: ImageBackend) -> Dict[str, Any]:
        """
        Laedt per-Agent per-Instanz Config.

        JSON-Format:
        {
            "instances": {
                "LocalSD": {"prompt_prefix": "...", "negative_prompt": "...", ...},
                "NanoBanana": {"prompt_prefix": "", ...}
            }
        }

        Merge-Logik: Agent-Instance-Override > Backend .env Default > leer
        """
        backend_defaults = self._get_backend_defaults(backend)

        if not character_name or not self.SKILL_ID:
            return backend_defaults

        agent_config = get_character_skill_config(character_name, self.SKILL_ID)

        # Migration: Falls altes flaches Format, konvertiere zu per-Instanz
        if agent_config and "instances" not in agent_config:
            logger.info("Migriere %s Config zu per-Instanz Format...", character_name)
            agent_config = self._migrate_flat_config(agent_config)
            save_character_skill_config(character_name, self.SKILL_ID, agent_config)
            logger.info("Migration abgeschlossen")

        if agent_config and "instances" in agent_config:
            # Migration: Alte ReActor-Keys entfernen + faceswap_enabled
            # raus aus der Skill-Config (lebt jetzt im Char-Profil/Bild-Tab,
            # _migrate_faceswap_flag uebernimmt den Wert beim ersten Read).
            migrated = False
            if "faceswap_enabled" in agent_config:
                # Falls noch nicht migriert: lazy-Migration im Caller-Pfad
                # zieht den Wert ins Profil. Hier nur lokal entfernen.
                del agent_config["faceswap_enabled"]
                migrated = True
            # Backend-Defaults fuer Vergleich laden (Name → Defaults)
            backend_defaults_map = {b.name: self._get_backend_defaults(b) for b in self.backends}
            for inst_name, inst_cfg in agent_config["instances"].items():
                reactor_keys = [k for k in inst_cfg if k.startswith("reactor_")]
                for k in reactor_keys:
                    del inst_cfg[k]
                    migrated = True
                # workflow_file ist technischer Backend-Param, gehoert nicht in per-Agent Config
                if "workflow_file" in inst_cfg:
                    del inst_cfg["workflow_file"]
                    migrated = True
                # Werte entfernen die dem Backend-Default (.env) entsprechen
                defaults = backend_defaults_map.get(inst_name, {})
                for key in list(inst_cfg.keys()):
                    if key in defaults and inst_cfg[key] == defaults[key]:
                        del inst_cfg[key]
                        migrated = True
            if migrated:
                save_character_skill_config(character_name, self.SKILL_ID, agent_config)
                logger.info("Config bereinigt fuer %s (nur Overrides gespeichert)", character_name)

            instance_overrides = agent_config["instances"].get(backend.name, {})
            if instance_overrides:
                # Merge: Override > Backend-Default
                result = dict(backend_defaults)
                for key, default_val in result.items():
                    if key in instance_overrides:
                        val = instance_overrides[key]
                        # bool VOR int pruefen (bool ist Subklasse von int)
                        if isinstance(default_val, bool):
                            result[key] = bool(val)
                        elif isinstance(default_val, float):
                            result[key] = float(val)
                        elif isinstance(default_val, int):
                            result[key] = int(val)
                        else:
                            result[key] = str(val).strip()
                return result
        else:
            # Erstelle per-Agent Config mit allen Instanzen beim ersten Aufruf
            new_config = self._build_initial_config()
            save_character_skill_config(character_name, self.SKILL_ID, new_config)
            logger.info("Per-Agent Config erstellt fuer %s: %s.json", character_name, self.SKILL_ID)

        return backend_defaults

    def _build_initial_config(self) -> Dict[str, Any]:
        """Erstellt initiale per-Agent Config mit allen Backend-Instanzen.

        Speichert nur leere Dicts pro Instanz — Defaults kommen aus .env/Backend.
        Nur echte Overrides (die vom Default abweichen) sollen hier gespeichert werden.

        ``faceswap_enabled`` lebt nicht mehr in der Skill-Config sondern im
        Character-Profil (Bild-Tab). Default dort: deaktiviert.
        """
        instances = {}
        for backend in self.backends:
            instances[backend.name] = {}
        config: Dict[str, Any] = {"instances": instances}
        # Default-Workflow setzen (aus .env COMFY_IMAGEGEN_DEFAULT)
        if self._default_workflow:
            config["comfy_workflow"] = self._default_workflow.name
        return config

    def _migrate_flat_config(self, old_config: Dict[str, Any]) -> Dict[str, Any]:
        """Migriert altes flaches Format zu per-Instanz Format."""
        instances = {}
        for backend in self.backends:
            backend_defaults = self._get_backend_defaults(backend)
            instance_cfg = {}
            for key, default_val in backend_defaults.items():
                if key in old_config:
                    instance_cfg[key] = old_config[key]
                else:
                    instance_cfg[key] = default_val
            instances[backend.name] = instance_cfg
        return {"instances": instances}

    def _migrate_faceswap_flag(self, character_name: str) -> None:
        """One-shot: alte Skill-Config-Variante von ``faceswap_enabled``
        in den Character-Profil (Bild-Tab) heben und aus der Skill-Config
        entfernen. Idempotent — ein No-Op nach der ersten Migration.
        """
        agent_config = get_character_skill_config(character_name, self.SKILL_ID)
        if not agent_config or "faceswap_enabled" not in agent_config:
            return
        legacy = bool(agent_config.pop("faceswap_enabled"))
        try:
            from app.models.character import get_character_config, save_character_config
            char_config = get_character_config(character_name) or {}
            # Nur uebernehmen wenn das Char-Profil noch keinen expliziten Wert hat
            if char_config.get("faceswap_enabled", "") in ("", None):
                char_config["faceswap_enabled"] = "true" if legacy else "false"
                save_character_config(character_name, char_config)
            save_character_skill_config(character_name, self.SKILL_ID, agent_config)
            logger.info("FaceSwap-Migration: %s -> faceswap_enabled=%s ins Char-Profil verschoben",
                        character_name, legacy)
        except Exception as e:
            logger.warning("FaceSwap-Migration fuer %s fehlgeschlagen: %s", character_name, e)

    def _character_swap_disabled(self, character_name: str) -> bool:
        """True wenn FaceSwap fuer den Character deaktiviert ist.

        Single Source of Truth: ``character_config.faceswap_enabled``
        (Bild-Tab im Character-Editor). Default = deaktiviert wenn nicht
        explizit auf "true" gesetzt.
        """
        # Lazy-Migration: alten Skill-Config-Wert ins Char-Profil heben
        self._migrate_faceswap_flag(character_name)
        from app.models.character import get_character_config
        char_config = get_character_config(character_name) or {}
        char_val = char_config.get("faceswap_enabled", "")
        if char_val != "" and char_val is not None:
            return str(char_val).lower() not in ("true", "1", "yes")
        # Kein expliziter Wert -> deaktiviert (Schema-Default)
        return True

    def _should_faceswap(self, character_name: str) -> bool:
        """Prueft ob standalone FaceSwap fuer diesen Agent aktiv ist.

        Single Source of Truth: ``character_config.faceswap_enabled``
        (Bild-Tab). Wenn aktiviert aber Face-Service nicht erreichbar →
        False. Wenn nicht explizit gesetzt → Auto-Modus (Face-Service-
        Verfuegbarkeit entscheidet).
        """
        from .face_client import is_available

        # Lazy-Migration: alten Skill-Config-Wert ins Char-Profil heben
        self._migrate_faceswap_flag(character_name)

        from app.models.character import get_character_config
        char_config = get_character_config(character_name) or {}
        char_val = char_config.get("faceswap_enabled", "")
        if char_val != "" and char_val is not None:
            enabled = str(char_val).lower() in ("true", "1", "yes")
            if enabled and not is_available():
                logger.info("FaceSwap: char config=enabled, but face service not reachable -> DEAKTIVIERT")
                return False
            logger.info("FaceSwap Config: char_config=%s -> %s", enabled, "AKTIV" if enabled else "DEAKTIVIERT")
            return enabled

        # Kein expliziter Wert -> Auto (Face Service Verfuegbarkeit)
        available = is_available()
        logger.info("FaceSwap Config: auto mode, face service %s -> %s",
                     "verfuegbar" if available else "nicht erreichbar",
                     "AKTIV" if available else "DEAKTIVIERT")
        return available

    def _apply_faceswap(self, images: List[bytes], character_name: str) -> List[bytes]:
        """Wendet standalone FaceSwap auf generierte Bilder an.

        Nutzt das Agent-Profilbild als Source Face.
        Originalbild bleibt erhalten falls Swap fuer ein einzelnes Bild fehlschlaegt.
        """
        from .face_client import apply_face_swap

        profile_img = get_character_profile_image(character_name)
        logger.info("FaceSwap: Profilbild-Lookup fuer %s -> '%s'", character_name, profile_img or "KEINS")
        if not profile_img:
            logger.warning("FaceSwap: ABBRUCH - Kein Profilbild gesetzt")
            return images

        images_dir = get_character_images_dir(character_name)
        profile_path = images_dir / profile_img
        logger.debug("FaceSwap: Source-Pfad: %s (exists=%s)", profile_path, profile_path.exists())
        if not profile_path.exists():
            logger.warning("FaceSwap: ABBRUCH - Datei nicht gefunden")
            return images

        source_bytes = profile_path.read_bytes()
        logger.debug("FaceSwap: Source Face geladen (%d bytes)", len(source_bytes))
        logger.info("FaceSwap: %d Bild(er) zu verarbeiten", len(images))

        result_images = []
        for i, img_bytes in enumerate(images):
            logger.debug("FaceSwap: Verarbeite Bild %d/%d (%d bytes)...", i + 1, len(images), len(img_bytes))
            swapped = apply_face_swap(img_bytes, source_bytes)
            if swapped is not None:
                result_images.append(swapped)
                logger.debug("FaceSwap: Bild %d geswapt (%d bytes)", i + 1, len(swapped))
            else:
                result_images.append(img_bytes)
                logger.warning("FaceSwap: Bild %d Original beibehalten", i + 1)

        return result_images

    def _detect_recipient_from_prompt(
        self, prompt: str, sender: str, all_chars: List[str]) -> Optional[str]:
        """Extrahiert den intendierten Empfaenger aus dem Prompt-Text.

        Erkennt Muster wie "Fuer Diego", "Für Enzo", "An Yuki", "For Bianca"
        und matched gegen bekannte Character-Namen (case-insensitive).

        Returns:
            Character-Name wenn EXAKT EIN Empfaenger eindeutig erkannt wird
            (und ungleich Sender). Sonst None.
        """
        if not prompt or not all_chars:
            return None
        import re as _re
        # Praepositionen die einen Empfaenger einleiten — Reihenfolge nach
        # Spezifitaet/Stoerungspotential (kuerzere zuerst koennen False-
        # Positives produzieren, deshalb mit Wortgrenzen).
        patterns = [
            r'\b(?:f[uü]er|f[oö]r)\s+([A-Z][a-zA-ZäöüÄÖÜß]+(?:\s+[A-Z][a-zA-ZäöüÄÖÜß]+)?)',
            r'\b(?:an|to|fuer|für)\s+([A-Z][a-zA-ZäöüÄÖÜß]+(?:\s+[A-Z][a-zA-ZäöüÄÖÜß]+)?)',
            r'\bvorstellung\s+f[uü]r\s+([A-Z][a-zA-ZäöüÄÖÜß]+)',
            r'\bsending\s+to\s+([A-Z][a-zA-ZäöüÄÖÜß]+)',
            r'\bschicke\s+(?:dies\s+)?(?:an\s+)?([A-Z][a-zA-ZäöüÄÖÜß]+)',
        ]
        candidates = set()
        sender_low = sender.lower()
        for pat in patterns:
            for m in _re.finditer(pat, prompt, _re.IGNORECASE):
                token = m.group(1).strip()
                token_low = token.lower()
                # Token gegen Character-Namen matchen — exakt oder Vorname-Match
                for c in all_chars:
                    if c == sender:
                        continue  # Selbstreferenz ignorieren
                    c_low = c.lower()
                    if token_low == c_low:
                        candidates.add(c)
                    elif " " in c and token_low == c_low.split()[0]:
                        # Vorname-Match (z.B. "Diego" -> "Diego Hidalgo")
                        candidates.add(c)
        # Auch Token-Match nur dann wenn der Sender NICHT identisch ist —
        # bereits oben gefiltert.
        if len(candidates) == 1:
            return candidates.pop()
        # Mehrdeutig oder nichts gefunden -> kein eindeutiger Empfaenger
        return None

    def _get_enhance_method(self, character_name: str) -> str:
        """Ermittelt ob Face Enhancement fuer diesen Agent aktiv ist.

        Prueft: 1) Globale Server-Einstellung, 2) per-Agent Config, 3) Face Service erreichbar.

        Returns:
            "face_service" oder "" (deaktiviert)
        """
        # Global deaktiviert?
        _global_enabled = os.environ.get("FACE_ENHANCE_ENABLED", "true")
        if isinstance(_global_enabled, str) and _global_enabled.lower() in ("false", "0", "no"):
            logger.info("FaceEnhance: Global deaktiviert (Server-Einstellung)")
            return ""

        # Per-Agent deaktiviert?
        agent_config = get_character_skill_config(character_name, self.SKILL_ID)
        if agent_config and "face_enhance_enabled" in agent_config:
            if not bool(agent_config["face_enhance_enabled"]):
                logger.info("FaceEnhance: per-Agent deaktiviert")
                return ""

        from .face_client import is_available as face_service_available

        if face_service_available():
            logger.info("FaceEnhance: Face Service (GFPGAN) -> AKTIV")
            return "face_service"

        logger.info("FaceEnhance: Face Service nicht verfuegbar -> DEAKTIVIERT")
        return ""

    # Gender-Werte die als "female" gelten
    _FEMALE_GENDERS = {"female", "woman", "girl", "weiblich", "futanari"}
    # Gender-Werte die als "male" gelten
    _MALE_GENDERS = {"male", "man", "maennlich", "männlich"}

    @staticmethod
    def _normalize_gender(gender: str) -> str:
        """Normalisiert Gender-String zu 'female' oder 'male'."""
        g = gender.strip().lower()
        if g in ImageGenerationSkill._FEMALE_GENDERS:
            return "female"
        if g in ImageGenerationSkill._MALE_GENDERS:
            return "male"
        return g

    @staticmethod
    def _get_current_expression_path(character_name: str) -> Optional[str]:
        """Gibt den Pfad der aktuellen Expression-Variante zurueck.

        Cache-Key = Character + angelegte Items + Mood + Activity.
        Returns None wenn keine Variante gecacht ist.
        """
        from app.core.expression_regen import get_cached_expression
        from app.models.character import (
            get_character_current_feeling, get_character_current_activity)
        from app.models.inventory import get_equipped_pieces, get_equipped_items

        mood = get_character_current_feeling(character_name) or ""
        activity = get_character_current_activity(character_name) or ""
        try:
            eq_p = get_equipped_pieces(character_name)
            eq_i = get_equipped_items(character_name)
        except Exception:
            eq_p, eq_i = None, None

        cached = get_cached_expression(character_name, mood, activity,
                                        equipped_pieces=eq_p, equipped_items=eq_i)
        if cached and cached.exists():
            return str(cached)
        return None

    def _resolve_face_references(
        self, appearances: list, character_name: str,
        max_slots: int = 4, profile_only: bool = False) -> dict:
        """Loest nummerierte Referenzbilder fuer ComfyUI-Workflows auf.

        Prioritaet:
        1. Character-/User-Profilbilder (mit Gender fuer FaceSwap)
        2. Freie Slots werden mit Location-Bildern gefuellt (type="location")

        Returns:
            dict mit keys:
                reference_images: {node_title: file_path}  — nummerierte Keys
                boolean_inputs: {node_title: bool}
                string_inputs: {node_title: str}  — _type Nodes (female/male/location/no)
                workflow_has_faceswap: bool (True wenn mindestens ein FaceSwap aktiv)
        """
        reference_images = {}
        boolean_inputs = {}
        string_inputs = {}

        user_profile = get_user_profile()
        user_name = user_profile.get("user_name", "")

        # Alle Faces mit Gender sammeln
        faces: list[tuple[str, str, str]] = []  # (path, normalized_gender, name)
        for person in appearances:
            name = person.get("name", "")
            if not name:
                continue

            # Gender und Referenzbild ermitteln
            # Prio 1: Expression-Variante (Equipped + Mood + Activity), Prio 2: Profilbild
            if name == user_name:
                gender = get_user_gender().strip().lower()
                profile_img = get_user_profile_image()
                if profile_img:
                    images_dir = get_user_images_dir()
                    candidate = images_dir / profile_img
                    ref_img_path = str(candidate) if candidate.exists() else None
                else:
                    ref_img_path = None
            else:
                profile = get_character_profile(name)
                gender = profile.get("gender", "").strip().lower()
                ref_img_path = None
                if not profile_only:
                    ref_img_path = self._get_current_expression_path(name)
                    if ref_img_path:
                        logger.debug("FaceRef: %s -> Expression-Variante: %s", name, ref_img_path)
                if not ref_img_path:
                    # Fallback (oder profile_only): Profilbild
                    profile_img = get_character_profile_image(name)
                    if profile_img:
                        images_dir = get_character_images_dir(name)
                        candidate = images_dir / profile_img
                        ref_img_path = str(candidate) if candidate.exists() else None

            if not ref_img_path:
                logger.debug("FaceRef: %s -> kein Referenzbild, ueberspringe", name)
                continue

            norm_gender = self._normalize_gender(gender)
            faces.append((ref_img_path, norm_gender, name))

        # Nummerierte Slots zuweisen: 1..max_slots-1 fuer Personen, letzter Slot fuer Location
        max_person_slots = max_slots - 1  # Slot 4 (letzter) fuer Location reserviert
        slot_idx = 1
        for path, norm_gender, name in faces:
            if slot_idx > max_person_slots:
                logger.debug("FaceRef: %s uebersprungen (max %d Personen-Slots)", name, max_person_slots)
                break
            reference_images[f"input_reference_image_{slot_idx}"] = path
            boolean_inputs[f"input_faceswap_{slot_idx}"] = True
            string_inputs[f"input_reference_image_{slot_idx}_type"] = norm_gender or "no"
            logger.debug("FaceRef: %s (gender=%s) -> Slot %d", name, norm_gender or "no", slot_idx)
            slot_idx += 1

        workflow_has_faceswap = len(reference_images) > 0

        # Letzter Slot (max_slots) fuer Location-Hintergrundbild
        location_id = get_character_current_location(character_name)
        if location_id:
            bg_path = get_background_path(location_id)
            if bg_path and bg_path.exists():
                reference_images[f"input_reference_image_{max_slots}"] = str(bg_path)
                boolean_inputs[f"input_faceswap_{max_slots}"] = False
                string_inputs[f"input_reference_image_{max_slots}_type"] = "location"
                logger.debug("FaceRef: Location-Bild -> Slot %d (%s)", max_slots, bg_path.name)
            else:
                logger.debug("FaceRef: Kein Location-Bild verfuegbar fuer '%s'", location_id)
        else:
            logger.debug("FaceRef: Kein Ort gesetzt fuer %s", character_name)

        if workflow_has_faceswap:
            logger.info("FaceRef: Workflow-FaceSwap aktiv (%d Face-Slots)", len([k for k, v in boolean_inputs.items() if v]))
        else:
            logger.debug("FaceRef: Keine Face-Referenzbilder gefunden")
        logger.debug("FaceRef: %d Slots insgesamt belegt", len(reference_images))

        return {
            "reference_images": reference_images,
            "boolean_inputs": boolean_inputs,
            "string_inputs": string_inputs,
            "workflow_has_faceswap": workflow_has_faceswap,
        }

    @staticmethod
    def _detect_media_extension(data: bytes) -> str:
        """Erkennt den Medien-Typ anhand der Magic Bytes."""
        if len(data) >= 12:
            if data[:4] == b'\x89PNG':
                return '.png'
            if data[:3] == b'\xff\xd8\xff':
                return '.jpg'
            if data[:4] == b'GIF8':
                return '.gif'
            if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
                return '.webp'
            if data[4:8] == b'ftyp':
                return '.mp4'
        return '.png'

    def _apply_enhance_on_files(
        self,
        saved_files: list,
        images_dir) -> list:
        """Wendet GFPGAN Face Enhancement auf gespeicherte Bilddateien an.

        Returns:
            Liste der erfolgreich enhanced Dateinamen.
        """
        from .face_client import apply_face_enhance_files

        enhanced = []
        for i, file_name in enumerate(saved_files, 1):
            image_path = images_dir / file_name
            logger.debug("FaceEnhance: Verarbeite %d/%d: %s", i, len(saved_files), file_name)
            result = apply_face_enhance_files(str(image_path))
            if result:
                enhanced.append(file_name)
                logger.info("FaceEnhance: %s enhanced", file_name)
            else:
                logger.warning("FaceEnhance: %s Original beibehalten", file_name)

        return enhanced

    def _apply_faceswap_on_files(
        self,
        saved_files: List[str],
        images_dir, character_name: str,
        appearances: Optional[List[Dict[str, str]]] = None) -> List[str]:
        """Wendet FaceSwap auf bereits gespeicherte Bilddateien an.

        Verwendet das Profilbild der dargestellten Person als Source Face:
        - 1 Person im Bild → deren Profilbild
        - Mehrere Personen oder keine → Profilbild des initiierenden Agents

        Returns:
            Liste der erfolgreich geswapten Dateinamen.
        """
        from .face_client import apply_face_swap_files

        # Bestimme wessen Gesicht verwendet wird
        swap_character = character_name
        if appearances:
            if len(appearances) == 1:
                depicted = appearances[0].get("name", character_name)
                if depicted != character_name:
                    logger.info("FaceSwap: Person im Bild ist %s (nicht %s)", depicted, character_name)
                    swap_character = depicted
            elif not any(p.get("name") == character_name for p in appearances):
                # Agent nicht im Bild (Photographer mode): erstes Subject verwenden
                swap_character = appearances[0].get("name", character_name)
                logger.info("FaceSwap (Photographer): Agent nicht in Appearances, verwende %s", swap_character)

        profile_img = get_character_profile_image(swap_character)
        logger.info("FaceSwap: Profilbild-Lookup fuer %s -> '%s'", swap_character, profile_img or "KEINS")
        if not profile_img:
            logger.warning("FaceSwap: ABBRUCH - Kein Profilbild gesetzt")
            return []

        # Source-Face aus dem images-Dir des dargestellten Characters
        swap_images_dir = get_character_images_dir(swap_character)
        source_path = swap_images_dir / profile_img
        logger.debug("FaceSwap: Source-Pfad: %s (exists=%s)", source_path, source_path.exists())
        if not source_path.exists():
            logger.warning("FaceSwap: ABBRUCH - Source-Datei nicht gefunden")
            return []

        swapped = []
        for i, file_name in enumerate(saved_files, 1):
            target_path = images_dir / file_name
            logger.debug("FaceSwap: Verarbeite %d/%d: %s", i, len(saved_files), file_name)
            result = apply_face_swap_files(str(target_path), str(source_path))
            if result:
                swapped.append(file_name)
                logger.info("FaceSwap: %s geswapt", file_name)
            else:
                logger.warning("FaceSwap: %s Original beibehalten", file_name)

        return swapped

    def _apply_comfyui_faceswap(
        self,
        saved_files: List[str],
        images_dir,
        face_refs: Dict[str, Any],
        generation_backend: Optional[ImageBackend] = None) -> List[str]:
        """Wendet ComfyUI FaceSwap-Workflow auf gespeicherte Bilder an.

        Pro Character (Face-Referenz) wird der Workflow einmal aufgerufen.
        Das Ergebnis des vorherigen Laufs wird als Target fuer den naechsten verwendet,
        sodass bei mehreren Characters alle Gesichter nacheinander geswapt werden.

        Args:
            saved_files: Dateinamen der generierten Bilder.
            images_dir: Verzeichnis der Bilder.
            face_refs: Ergebnis von _resolve_face_references() mit reference_images, boolean_inputs, string_inputs.
            generation_backend: Backend das die Bilder generiert hat (Fallback fuer FaceSwap-Backend).

        Returns:
            Liste der erfolgreich geswapten Dateinamen.
        """
        workflow_file = os.environ.get("COMFY_FACESWAP_WORKFLOW_FILE", "").strip()
        if not workflow_file or not os.path.exists(workflow_file):
            logger.warning("ComfyUI FaceSwap: Workflow-Datei nicht konfiguriert oder nicht gefunden: %s", workflow_file)
            return []

        # Backend fuer FaceSwap bestimmen — immer verfuegbare Backends bevorzugen
        backend_name = os.environ.get("COMFY_FACESWAP_BACKEND", "").strip()
        faceswap_backend = None
        if backend_name:
            for b in self.backends:
                if b.name == backend_name and b.api_type == "comfyui" and b.available:
                    faceswap_backend = b
                    break
            if not faceswap_backend:
                logger.info("ComfyUI FaceSwap: konfiguriertes Backend '%s' nicht verfuegbar, suche Fallback",
                            backend_name)
        if not faceswap_backend and generation_backend and \
                generation_backend.api_type == "comfyui" and generation_backend.available:
            faceswap_backend = generation_backend
        if not faceswap_backend:
            # Letzter Fallback: erstes verfuegbares ComfyUI-Backend
            for b in self.backends:
                if b.api_type == "comfyui" and b.available and b.instance_enabled:
                    faceswap_backend = b
                    logger.info("ComfyUI FaceSwap: Fallback auf verfuegbares Backend '%s'", b.name)
                    break
        if not faceswap_backend:
            logger.warning("ComfyUI FaceSwap: Kein ComfyUI-Backend verfuegbar")
            return []

        reference_images = face_refs.get("reference_images", {})
        boolean_inputs = face_refs.get("boolean_inputs", {})
        string_inputs = face_refs.get("string_inputs", {})

        # Face-Slots extrahieren: nur Slots mit aktivem FaceSwap (keine Location-Bilder)
        face_slots = []
        for slot_key, ref_path in sorted(reference_images.items()):
            # input_reference_image_1 -> input_faceswap_1
            slot_num = slot_key.split("_")[-1]
            faceswap_key = f"input_faceswap_{slot_num}"
            type_key = f"{slot_key}_type"
            if not boolean_inputs.get(faceswap_key, False):
                continue  # Location-Bild oder deaktiviert
            gender = string_inputs.get(type_key, "no")
            face_slots.append((ref_path, gender))

        if not face_slots:
            logger.warning("ComfyUI FaceSwap: Keine aktiven Face-Referenzen")
            return []

        logger.info("ComfyUI FaceSwap: %d Character(s) zu swappen", len(face_slots))

        swapped = []
        for i, file_name in enumerate(saved_files, 1):
            target_path = str(images_dir / file_name)
            logger.info("ComfyUI FaceSwap: Bild %d/%d %s (%d Durchlaeufe)",
                        i, len(saved_files), file_name, len(face_slots))

            success = True
            for pass_idx, (ref_path, gender) in enumerate(face_slots, 1):
                logger.info("ComfyUI FaceSwap: Pass %d/%d (gender=%s, ref=%s)",
                            pass_idx, len(face_slots), gender, os.path.basename(ref_path))
                result_bytes = faceswap_backend.run_faceswap_workflow(
                    workflow_file=workflow_file,
                    target_image_path=target_path,
                    reference_image_path=ref_path,
                    gender=gender)
                if result_bytes:
                    # Ergebnis speichern — wird zum Target fuer den naechsten Pass
                    with open(target_path, "wb") as f:
                        f.write(result_bytes)
                    logger.info("ComfyUI FaceSwap: Pass %d/%d erfolgreich (%d bytes)",
                                pass_idx, len(face_slots), len(result_bytes))
                else:
                    logger.warning("ComfyUI FaceSwap: Pass %d/%d fehlgeschlagen, breche ab",
                                   pass_idx, len(face_slots))
                    success = False
                    break

            if success:
                swapped.append(file_name)
                logger.info("ComfyUI FaceSwap: %s komplett (%d Passes)", file_name, len(face_slots))
            else:
                logger.warning("ComfyUI FaceSwap: %s fehlgeschlagen", file_name)

        return swapped

    # --- MultiSwap ---
    # Region-Selektion (face/hair/body/clothes/...) lebt jetzt komplett im
    # ComfyUI-Workflow (hartcodierter input_prompt_positive). Code patcht hier
    # nur noch Refs/Switches/UNet/CLIP, keine Mask- oder Prompt-Hints mehr.
    MULTISWAP_UNET_TITLES = ("input_safetensors", "input_unet", "input_model", "input_gguf")

    # Maximale Anzahl Character-Slots, die der MultiSwap-Workflow in einem
    # Pass entgegennimmt (heute: input_reference_image_1/_2). Bei mehr Characters
    # wird in 2er-Bloecken iteriert. Erweiterung auf 3 sobald der Workflow
    # input_reference_image_3 bekommt: Konstante hochsetzen, sonst keine Aenderung.
    MULTISWAP_MAX_REFS_PER_PASS = 2

    def _apply_multiswap(
        self,
        saved_files: List[str],
        images_dir,
        face_refs: Dict[str, Any],
        generation_backend: Optional[ImageBackend] = None) -> List[str]:
        """Wendet MultiSwap-Workflow auf gespeicherte Bilder an.

        Pro Pass werden bis zu MULTISWAP_MAX_REFS_PER_PASS Character-Refs
        gleichzeitig geswapt (Slots input_reference_image_1/_2). Bei mehr
        Characters wird in 2er-Bloecken iteriert (Ergebnis -> Target naechster
        Block). Standardfall (1-2 Characters) braucht damit nur 1 Pass.

        Welche Bildbereiche getauscht werden (face/hair/clothes/...) entscheidet
        ausschliesslich der ComfyUI-Workflow ueber seinen hartcodierten
        input_prompt_positive. Der Code patcht nur die Referenzbilder.

        Args:
            saved_files: Dateinamen der generierten Bilder.
            images_dir: Verzeichnis der Bilder.
            face_refs: Ergebnis von _resolve_face_references() (profile_only=True).
            generation_backend: Backend das die Bilder generiert hat.

        Returns:
            Liste der erfolgreich geswapten Dateinamen.
        """
        configured_path = os.environ.get("COMFY_MULTISWAP_WORKFLOW_FILE", "").strip()
        workflow_file = configured_path
        if not workflow_file or not os.path.exists(workflow_file):
            # Fallback: Repo-Root/workflows/multiswap_flux2_api.json suchen.
            # __file__ = app/skills/image_generation_skill.py, daher 3x dirname()
            # bis zum Repo-Root, NICHT 2x (das ergibt sonst app/workflows/...).
            _repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
            for candidate_name in ("multiswap_flux2_api.json", "multiswap_flux2_gguf_api.json"):
                _candidate = os.path.join(_repo_root, "workflows", candidate_name)
                if os.path.exists(_candidate):
                    workflow_file = _candidate
                    if configured_path:
                        logger.warning(
                            "MultiSwap: konfigurierte Datei '%s' nicht gefunden, "
                            "Fallback auf '%s'", configured_path, _candidate)
                    break
            else:
                logger.warning(
                    "MultiSwap: Workflow-Datei nicht gefunden (konfiguriert='%s', "
                    "Fallback in %s/workflows/ ebenfalls leer)",
                    configured_path, _repo_root)
                return []

        # Backend bestimmen
        backend_name = os.environ.get("COMFY_MULTISWAP_BACKEND",
                                      os.environ.get("COMFY_FACESWAP_BACKEND", "")).strip()
        multiswap_backend = None
        if backend_name:
            for b in self.backends:
                if b.name == backend_name and b.api_type == "comfyui" and b.available:
                    multiswap_backend = b
                    break
            if not multiswap_backend:
                logger.info("MultiSwap: konfiguriertes Backend '%s' nicht verfuegbar, suche Fallback",
                            backend_name)
        if not multiswap_backend and generation_backend and \
                generation_backend.api_type == "comfyui" and generation_backend.available:
            multiswap_backend = generation_backend
        if not multiswap_backend:
            for b in self.backends:
                if b.api_type == "comfyui" and b.available and b.instance_enabled:
                    multiswap_backend = b
                    logger.info("MultiSwap: Fallback auf verfuegbares Backend '%s'", b.name)
                    break
        if not multiswap_backend:
            logger.warning("MultiSwap: Kein ComfyUI-Backend verfuegbar")
            return []

        # Face-Slots extrahieren (gleiche Logik wie _apply_comfyui_faceswap)
        reference_images = face_refs.get("reference_images", {})
        boolean_inputs = face_refs.get("boolean_inputs", {})
        face_slots = []
        for slot_key, ref_path in sorted(reference_images.items()):
            slot_num = slot_key.split("_")[-1]
            faceswap_key = f"input_faceswap_{slot_num}"
            if not boolean_inputs.get(faceswap_key, False):
                continue  # Location-Bild oder deaktiviert
            if ref_path and os.path.exists(ref_path):
                face_slots.append(ref_path)

        if not face_slots:
            logger.warning("MultiSwap: Keine aktiven Face-Referenzen")
            return []

        # Workflow einmal laden, Node-IDs per Titel aufloesen — so ist die
        # JSON-Datei austauschbar (safetensors- vs. GGUF-Variante z.B.).
        node_overrides: Dict[str, Dict[str, Any]] = {}
        try:
            import json as _json
            with open(workflow_file) as _wf:
                _wf_data = _json.load(_wf)
        except Exception as _e:
            logger.warning("MultiSwap: Workflow-JSON laden fehlgeschlagen: %s", _e)
            return []

        def _find_by_title(title: str) -> Optional[str]:
            for _nid, _node in _wf_data.items():
                if isinstance(_node, dict) and (_node.get("_meta") or {}).get("title") == title:
                    return _nid
            return None

        # UNet + CLIP per Titel injizieren (zentral konfigurierbar via faceswap.multiswap_unet/clip)
        unet_name = os.environ.get("COMFY_MULTISWAP_UNET", "").strip()
        clip_name = os.environ.get("COMFY_MULTISWAP_CLIP", "").strip()
        unet_node_id = ""
        unet_default = ""
        for _nid, _node in _wf_data.items():
            if not isinstance(_node, dict):
                continue
            title = (_node.get("_meta") or {}).get("title", "")
            _ct = _node.get("class_type", "")
            # UNet-Loader per Titel ODER per Class-Type (auch ohne Titel-Treffer)
            is_unet_loader = (
                title in self.MULTISWAP_UNET_TITLES
                or _ct in ("UNETLoader", "LoaderGGUF", "UnetLoaderGGUF", "CheckpointLoaderSimple")
            )
            if is_unet_loader:
                # Class-aware: LoaderGGUF -> gguf_name, UNETLoader -> unet_name, Checkpoint -> ckpt_name
                if "GGUF" in _ct:
                    _key = "gguf_name"
                elif _ct == "CheckpointLoaderSimple":
                    _key = "ckpt_name"
                else:
                    _key = "unet_name"
                if _key not in (_node.get("inputs") or {}):
                    for k, v in (_node.get("inputs") or {}).items():
                        if isinstance(v, str):
                            _key = k
                            break
                unet_node_id = _nid
                unet_default = (_node.get("inputs") or {}).get(_key, "") or ""
                if unet_name:
                    node_overrides[_nid] = {_key: unet_name}
                    logger.info("MultiSwap: UNet-Loader '%s'/%s (%s.%s) ueberschrieben mit '%s'",
                                title, _ct, _nid, _key, unet_name)
            elif title == "input_clip" and clip_name:
                for k, v in (_node.get("inputs") or {}).items():
                    if isinstance(v, str):
                        node_overrides[_nid] = {k: clip_name}
                        logger.info("MultiSwap: input_clip (%s.%s) ueberschrieben mit '%s'", _nid, k, clip_name)
                        break

        # Hard-Check: Loader-Node existiert und hat einen nicht-leeren Modellnamen
        # (entweder per Admin-Override oder Workflow-Default). Sonst frueh
        # abbrechen statt ComfyUI eine kaputte Validation-400 zurueckspielen lassen.
        if unet_node_id and not (unet_name or unet_default):
            logger.error(
                "MultiSwap: input_safetensors/input_unet (Node %s) hat keinen Modellnamen — "
                "im Admin unter FaceSwap/MultiSwap → 'MultiSwap UNet (Model)' setzen "
                "oder im Workflow einen Default eintragen.",
                unet_node_id)
            return []

        # In Bloecke aufteilen (Standardfall 1-2 Chars -> 1 Pass).
        # Slot-Titel im MultiSwap-Workflow folgen der `input_reference_image_N`
        # Konvention (mit Underscore vor der Nummer) — analog zu den uebrigen
        # input_*-Slots im System.
        chunks = [
            face_slots[i:i + self.MULTISWAP_MAX_REFS_PER_PASS]
            for i in range(0, len(face_slots), self.MULTISWAP_MAX_REFS_PER_PASS)
        ]
        logger.info("MultiSwap: %d Character(s) in %d Pass(es)",
                     len(face_slots), len(chunks))

        swapped = []
        for i, file_name in enumerate(saved_files, 1):
            target_path = str(images_dir / file_name)
            logger.info("MultiSwap: Bild %d/%d %s", i, len(saved_files), file_name)

            success = True
            for pass_idx, chunk in enumerate(chunks, 1):
                ref_paths = {
                    f"input_reference_image_{slot_no}": ref
                    for slot_no, ref in enumerate(chunk, 1)
                }
                logger.info("MultiSwap: Pass %d/%d (refs=%s)",
                            pass_idx, len(chunks),
                            [os.path.basename(p) for p in chunk])
                result_bytes = multiswap_backend.run_faceswap_workflow(
                    workflow_file=workflow_file,
                    target_image_path=target_path,
                    reference_image_path="",  # nicht genutzt im Multi-Slot-Modus
                    gender="no",
                    node_overrides=node_overrides,
                    reference_image_paths=ref_paths)
                if result_bytes:
                    with open(target_path, "wb") as f:
                        f.write(result_bytes)
                    logger.info("MultiSwap: Pass %d/%d erfolgreich (%d bytes)",
                                pass_idx, len(chunks), len(result_bytes))
                else:
                    logger.warning("MultiSwap: Pass %d/%d fehlgeschlagen, breche ab",
                                   pass_idx, len(chunks))
                    success = False
                    break

            if success:
                swapped.append(file_name)
                logger.info("MultiSwap: %s komplett (%d Pass(es))", file_name, len(chunks))
            else:
                logger.warning("MultiSwap: %s fehlgeschlagen", file_name)

        return swapped

    def _merge_piece_loras(self, current_loras: List[Dict[str, Any]],
                           character_name: str, workflow_name: str,
                           equipped_override: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """Merged LoRAs aus equipped Outfit-Pieces in freie Workflow-Slots.

        - Quelle: equipped_override falls gesetzt (z.B. Set-Vorschau mit noch
          nicht angezogenem Set), sonst profile.equipped_pieces.
        - Filter: lora.model leer ODER == workflow_name
        - Duplikate (gleicher name): ueberspringen
        - Fuellt Slots mit name='None' (freie Plaetze), erweitert die Liste nicht
          ueber ihre urspruengliche Laenge hinaus (Workflow hat feste Slot-Zahl).
        """
        if not character_name or not current_loras:
            return current_loras

        from app.models.character import get_character_profile
        from app.models.inventory import get_item

        profile = get_character_profile(character_name) or {}
        equipped = equipped_override if equipped_override is not None else (profile.get("equipped_pieces") or {})
        if not equipped:
            return current_loras

        # Bereits belegte LoRA-Namen (Duplikat-Schutz)
        existing_names = {
            (l.get("name") or "").strip()
            for l in current_loras
            if (l.get("name") or "").strip() and (l.get("name") or "").strip() != "None"
        }

        # Piece-LoRAs sammeln die passen
        candidates: List[Dict[str, Any]] = []
        wf_key = (workflow_name or "").strip()
        for slot, iid in equipped.items():
            if not iid:
                continue
            it = get_item(iid)
            if not it:
                continue
            op = it.get("outfit_piece") or {}
            entry = op.get("lora")
            if not isinstance(entry, dict):
                continue
            nm = (entry.get("name") or "").strip()
            if not nm or nm.lower() == "none":
                continue
            if nm in existing_names:
                continue
            piece_wf = (entry.get("workflow") or entry.get("model") or "").strip()
            if piece_wf and piece_wf != wf_key:
                continue
            existing_names.add(nm)
            candidates.append({
                "name": nm,
                "strength": float(entry.get("strength", 1.0) or 1.0),
            })

        # Profil-Slot-Overrides: fuer leere UND nicht-gecoverte Slots
        # ein LoRA aus profile.slot_overrides[slot].lora anziehen.
        try:
            from app.models.character import _collect_covered_slots
            covered = _collect_covered_slots(equipped)
        except Exception:
            covered = set()
        slot_overrides = profile.get("slot_overrides") or {}
        from app.models.inventory import VALID_PIECE_SLOTS
        for _slot in VALID_PIECE_SLOTS:
            if equipped.get(_slot):
                continue
            if _slot in covered:
                continue
            _ov = slot_overrides.get(_slot) or {}
            if not isinstance(_ov, dict):
                continue
            _lora = _ov.get("lora") or {}
            if not isinstance(_lora, dict):
                continue
            nm = (_lora.get("name") or "").strip()
            if not nm or nm.lower() == "none":
                continue
            if nm in existing_names:
                continue
            _lora_wf = (_lora.get("workflow") or _lora.get("model") or "").strip()
            if _lora_wf and _lora_wf != wf_key:
                continue
            existing_names.add(nm)
            candidates.append({
                "name": nm,
                "strength": float(_lora.get("strength", 1.0) or 1.0),
            })

        if not candidates:
            return current_loras

        # Freie Slots finden (name == 'None' oder leer) und auffuellen
        merged = list(current_loras)
        it_cand = iter(candidates)
        for i, l in enumerate(merged):
            nm = (l.get("name") or "").strip()
            if nm == "" or nm == "None":
                try:
                    merged[i] = next(it_cand)
                except StopIteration:
                    break

        dropped = list(it_cand)
        if dropped:
            logger.warning(
                "Piece-/Slot-LoRAs konnten nicht zugewiesen werden — "
                "Workflow hat keine freien Slots (alle 4 belegt durch Char-Override/Defaults): %s",
                [l["name"] for l in dropped])
        return merged


    def _get_vision_llm_config(self, character_name: str) -> Dict[str, Any]:
        """Loads Vision LLM config via Router (Task: image_recognition)."""
        from app.core.llm_router import resolve_llm
        instance = resolve_llm("image_recognition", agent_name=character_name)
        if instance:
            return {
                "model": instance.model,
                "api_base": instance.api_base,
                "api_key": instance.api_key,
                "temperature": instance.temperature,
                "max_tokens": instance.max_tokens,
            }
        logger.warning("No LLM available for task=image_recognition (check LLM Routing)")
        return None

    def _generate_image_analysis(self, image_path: str, character_name: str) -> Optional[str]:
        """Objektive Bildanalyse via Vision-LLM - sachliche Beschreibung des Bildinhalts."""
        from app.core.llm_client import LLMClient

        if not os.path.exists(image_path):
            return None

        try:
            with open(image_path, 'rb') as f:
                image_bytes = f.read()
            base64_image = base64.b64encode(image_bytes).decode('utf-8')
        except Exception as e:
            logger.error("Fehler beim Laden des Bildes fuer Analyse: %s", e)
            return None

        prompt_text = os.environ.get("IMAGE_ANALYSIS_PROMPT", "").strip() or (
            "Describe this image in detail. Include:\n"
            "- People: appearance, clothing, pose, expression\n"
            "- Setting: location, environment, lighting\n"
            "- Objects and activities visible\n"
            "- Overall mood and atmosphere\n\n"
            "Be factual and objective. Respond ONLY with the description, "
            "no formatting, no markdown, no quotes. 2-4 sentences."
        )

        # Language from env (default: German)
        analysis_lang = os.environ.get("IMAGE_ANALYSIS_LANGUAGE", "de").strip()
        lang_name = "German" if analysis_lang == "de" else "English"

        try:
            vcfg = self._get_vision_llm_config(character_name)
            if not vcfg:
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
                {"role": "system", "content": f"You MUST answer in {lang_name}. This is mandatory."},
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
            return analysis
        except Exception as e:
            logger.error("Objektive Bildanalyse fehlgeschlagen: %s", e)
            return None

    def _generate_comment(self, character_name: str, rp_context: str = "",
                          photographer_subjects: Optional[List[str]] = None) -> Optional[str]:
        """Erzeugt eine kurze Situations-Beschreibung als Galerie-Caption.

        Beschreibt, welche Situation zum Foto gefuehrt hat (aus dem RP-Kontext),
        statt einer emotionalen Reaktion auf das Bild.

        Args:
            rp_context: Die Chat-Antwort, die das Bild ausgeloest hat.
            photographer_subjects: Liste der abgebildeten Personen (nur in Photographer mode).
        """
        if not rp_context or len(rp_context.strip()) < 15:
            logger.debug("Kein RP-Kontext fuer Situations-Kommentar vorhanden")
            return None

        # Aktions-Teile extrahieren (Text zwischen *...*), Dialog verwerfen
        action_chunks = re.findall(r'\*([^*]+)\*', rp_context)
        if action_chunks:
            clean = " ".join(action_chunks)
        else:
            clean = rp_context
        # Meta-Text und Markdown entfernen
        clean = re.sub(r'"[^"]*"', '', clean)
        clean = re.sub(r'\([^)]*\)', '', clean)
        clean = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', clean)
        clean = re.sub(r'\s+', ' ', clean).strip()

        if not clean or len(clean) < 15:
            return None

        try:
            from app.core.llm_client import LLMClient
            from app.core.llm_queue import get_llm_queue, Priority

            vcfg = self._get_vision_llm_config(character_name)
            if not vcfg:
                # Fallback: gekuerzten Rohtext verwenden
                return clean[:200]

            llm = LLMClient(
                model=vcfg["model"],
                api_key=vcfg["api_key"],
                api_base=vcfg["api_base"],
                temperature=0.5,
                max_tokens=100,
                request_timeout=30)

            if photographer_subjects:
                subject_names = ", ".join(photographer_subjects)
                who = f"{character_name} hat ein Foto von {subject_names} gemacht."
            else:
                who = f"Ein Foto von {character_name} ist entstanden."

            system = (
                "Fasse die Situation, die zu einem Foto gefuehrt hat, in 1 kurzen Satz zusammen "
                "(maximal 150 Zeichen, deutsch). "
                "Beschreibe WAS passiert ist und WARUM das Foto entstanden ist. "
                "Schreibe in der dritten Person. "
                "Antworte NUR mit dem Satz. Keine Anfuehrungszeichen, kein Markdown."
            )
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": f"{who}\n\nKontext:\n{clean[:500]}"},
            ]

            response = get_llm_queue().submit(
                task_type="image_comment",
                priority=Priority.NORMAL,
                llm=llm,
                messages_or_prompt=messages,
                agent_name=character_name)
            comment = response.content.strip()

            if comment.startswith('"') and comment.endswith('"'):
                comment = comment[1:-1]
            if comment.startswith("'") and comment.endswith("'"):
                comment = comment[1:-1]

            # Zeichenbegrenzung: max 200 Zeichen, am letzten Satzende abschneiden
            if len(comment) > 200:
                truncated = comment[:200]
                for sep in ['. ', '! ', '? ']:
                    idx = truncated.rfind(sep)
                    if idx > 50:
                        truncated = truncated[:idx + 1]
                        break
                comment = truncated

            logger.info("Situations-Kommentar generiert: %s", comment[:200])
            return comment
        except Exception as e:
            logger.error("Situations-Kommentar fehlgeschlagen: %s", e)
            logger.debug("Traceback:", exc_info=True)
            # Fallback: gekuerzten Rohtext verwenden
            return clean[:200] if clean else None

    # _detect_mentioned_appearances() wurde durch PromptBuilder.detect_persons() ersetzt.

    def _extract_rp_scene_context(
        self, rp_text: str, character_name: str) -> str:
        """Extrahiert die Pose aus der Character-Antwort via Vision-LLM.

        Gibt den Pose-String zurueck (fuer den Enhanced Prompt).
        """
        from app.models.character import is_outfit_locked

        # Outfit-Lock spart den kompletten Pose-Call (Pose ist an Agent-Call gekoppelt).
        agent_locked = is_outfit_locked(character_name)

        rp_text = (rp_text or "").strip()
        if not rp_text or agent_locked:
            return ""

        # Character-Quelle: nur Aktionen zwischen *...*, Dialog + Noise raus
        clean_rp = ""
        if rp_text:
            action_chunks = re.findall(r'\*([^*]+)\*', rp_text)
            clean_rp = " ".join(action_chunks) if action_chunks else rp_text
            clean_rp = re.sub(r'"[^"]*"', '', clean_rp)
            clean_rp = re.sub(r'\([^)]*\)', '', clean_rp)
            clean_rp = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', clean_rp)
            clean_rp = re.sub(r'\s+', ' ', clean_rp).strip()

        _instruction_fragments = [
            "description of body pose",
            "short english description",
        ]

        def _strip_instruction_echo(value: str, label: str) -> str:
            for frag in _instruction_fragments:
                if frag in value.lower():
                    logger.warning("RP-Scene: %s ist Echo der Instruktion, verworfen: '%s'",
                                   label, value)
                    return ""
            return value

        def _run_llm(vcfg: Dict[str, Any], system: str, source: str):
            try:
                from app.core.llm_client import LLMClient
                from app.core.llm_queue import get_llm_queue, Priority
                llm = LLMClient(
                    model=vcfg["model"],
                    api_key=vcfg["api_key"],
                    api_base=vcfg["api_base"],
                    temperature=0.1,
                    max_tokens=200,
                    request_timeout=30)
                response = get_llm_queue().submit(
                    task_type="tool",
                    priority=Priority.HIGH,
                    llm=llm,
                    messages_or_prompt=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": f"/no_think\n{source[:500]}"},
                    ],
                    agent_name=vcfg.get("_target_name", character_name))
                raw = response.content.strip()
                match = re.search(r'\{.*\}', raw, re.DOTALL)
                if not match:
                    logger.debug("RP-Scene: Kein JSON: %s", raw[:100])
                    return {}
                import json as _json
                return _json.loads(match.group())
            except Exception as e:
                logger.warning("RP-Scene LLM-Call Fehler: %s", e)
                return {}

        pose = ""

        # Character-Antwort → Pose (fuer Bild-Prompt).
        if clean_rp and len(clean_rp) >= 15:
            vcfg_agent = self._get_vision_llm_config(character_name)
            if vcfg_agent:
                vcfg_agent["_target_name"] = character_name
                sys_agent = (
                    f"The following is {character_name}'s roleplay action. "
                    "Extract a short English description of the character's "
                    "body pose and action.\n\n"
                    "Respond ONLY with JSON:\n"
                    '{"pose": "<pose or empty>"}'
                )
                data = _run_llm(vcfg_agent, sys_agent, clean_rp)
                pose = _strip_instruction_echo((data.get("pose") or "").strip(), "Pose")
                if pose:
                    logger.info("RP-Scene LLM: Pose -> '%s'", pose)
            else:
                logger.debug("RP-Scene: Kein Vision-LLM fuer Agent %s", character_name)

        return pose

    def _parse_input(self, prompt_input: str) -> Dict[str, Any]:
        """Parst optionales JSON-Inputformat fuer Tool-Aufrufe."""
        ctx = self._parse_base_input(prompt_input)

        data: Dict[str, Any] = {
            "prompt": ctx.get("prompt", ctx.get("input", prompt_input)),
            "agent_name": ctx.get("agent_name", ""),
            "set_profile": ctx.get("set_profile", False),
            "skip_gallery": ctx.get("skip_gallery", False),
            "appearances": ctx.get("appearances", None),
            "auto_enhance": ctx.get("auto_enhance", True),
            "force_faceswap": ctx.get("force_faceswap", False),
            "workflow": ctx.get("workflow", ""),
            "backend": ctx.get("backend", ""),
            "override_width": ctx.get("override_width"),
            "override_height": ctx.get("override_height"),
            "model_override": ctx.get("model_override", ""),
            "loras": ctx.get("loras", None),
            "character_prompt": ctx.get("character_prompt", ""),
            "pose_prompt": ctx.get("pose_prompt", ""),
            "expression_prompt": ctx.get("expression_prompt", ""),
            "rp_context": ctx.get("rp_context", ""),
            "user_input": ctx.get("user_input", ""),
            "profile_only": ctx.get("profile_only", False),
            "to_avatar_gallery": ctx.get("to_avatar_gallery", False),
        }

        if isinstance(data.get("prompt"), str):
            data["prompt"] = data["prompt"].strip()
        else:
            data["prompt"] = ""

        return data

    def execute(self, prompt: str) -> str:
        """
        Generiert ein Bild ueber die guenstigste verfuegbare Instanz.

        Args:
            prompt: Text-Beschreibung des gewuenschten Bildes (oder JSON mit Kontext)

        Returns:
            String mit Bild-Links oder Fehlermeldung
        """
        if not self.enabled:
            return "Image Generation Skill ist nicht verfuegbar. Keine Instanz konfiguriert oder erreichbar."

        # Input parsen (vor Backend-Auswahl, da per-Agent enabled beruecksichtigt wird)
        input_data = self._parse_input(prompt)
        prompt_text = input_data.get("prompt", "")
        character_name = input_data.get("agent_name", "").strip()
        set_profile = bool(input_data.get("set_profile"))
        skip_gallery = bool(input_data.get("skip_gallery"))
        # skip_post_processing: alle Face-Post-Schritte (Swap + Enhance)
        # ueberspringen. Genutzt fuer technische Bilder ohne Personen
        # (z.B. Messaging-Frame, UI-Assets).
        skip_post_processing = bool(input_data.get("skip_post_processing"))

        if not prompt_text or len(prompt_text.strip()) == 0:
            return "Fehler: Bitte gib eine Bildbeschreibung ein."

        if not character_name:
            return "Fehler: Agent-Name fehlt fuer Bildspeicherung."

        # Workflow + Backend auswaehlen (explizite Auswahl hat Vorrang)
        explicit_workflow = input_data.get("workflow", "").strip() if isinstance(input_data, dict) else ""
        explicit_backend = input_data.get("backend", "").strip() if isinstance(input_data, dict) else ""
        active_workflow = None
        backend = None

        if explicit_workflow:
            # Expliziter Workflow — kein Fallback auf andere Backends
            active_workflow = next((wf for wf in self.comfy_workflows if wf.name == explicit_workflow), None)
            if not active_workflow:
                return f"Fehler: Workflow '{explicit_workflow}' nicht gefunden."
            backend = self._wait_for_backend(
                active_workflow, character_name, workflow_only=True)
            if not backend:
                return f"Fehler: Kein ComfyUI-Backend fuer Workflow '{explicit_workflow}' verfuegbar (Timeout)."
            logger.info("Expliziter Workflow: %s -> %s", explicit_workflow, backend.name)
        elif explicit_backend:
            # Explizites Backend — kein Fallback
            backend = self._wait_for_explicit_backend(explicit_backend)
            if not backend:
                return f"Fehler: Backend '{explicit_backend}' nicht verfuegbar (Timeout)."
            logger.info("Explizites Backend: %s", explicit_backend)
            # ComfyUI-Backends brauchen ZWINGEND einen Workflow — sonst kennt der
            # Backend keine Workflow-Datei und _generate scheitert mit "Kein
            # Workflow konfiguriert". Auto-Pick: erster Workflow der den Backend
            # in compatible_backends fuehrt (oder None = jeder Backend).
            if backend.api_type == "comfyui" and self.comfy_workflows:
                active_workflow = next(
                    (wf for wf in self.comfy_workflows
                     if not wf.compatible_backends or backend.name in wf.compatible_backends),
                    None) or self._default_workflow
                if active_workflow:
                    logger.info("Auto-Workflow fuer Backend %s: %s",
                                explicit_backend, active_workflow.name)
                else:
                    return (f"Fehler: Backend '{explicit_backend}' ist ComfyUI, "
                            f"aber kein kompatibler Workflow konfiguriert.")

        if not backend:
            # Auto-Auswahl (Standard-Verhalten)
            active_workflow = self._get_active_workflow(character_name) if self.comfy_workflows else None
            if active_workflow:
                backend = self._wait_for_backend(
                    active_workflow, character_name, workflow_only=True)
                # Wenn der Workflow-bevorzugte Backend nicht verfuegbar ist
                # (ComfyUI down, kein Cloud-Match), nicht aufgeben sondern
                # auf irgendein anderes konfiguriertes Backend ausweichen.
                # Gilt nur fuer Auto-Auswahl, nicht fuer explizite User-Anfragen.
                if not backend:
                    logger.info(
                        "Workflow-bevorzugtes Backend nicht verfuegbar — "
                        "auto-Fallback auf beliebiges Backend (Cloud erlaubt)")
                    active_workflow = None  # Workflow-Bindung loesen
                    backend = self._wait_for_backend(
                        None, character_name, workflow_only=False)
            else:
                backend = self._wait_for_backend(
                    None, character_name, workflow_only=False)
        if not backend:
            return "Fehler: Keine Image-Generation Instanz ist aktuell verfuegbar (Timeout)."

        # Safety-Net: Auto-Pick eines Workflows fuer ComfyUI-Backends ohne Workflow.
        # Sonst scheitert _generate mit "Kein Workflow konfiguriert" — das passiert
        # wenn der Auto-Fallback die Workflow-Bindung loest aber dann doch ein
        # ComfyUI-Backend findet.
        if backend.api_type == "comfyui" and not active_workflow and self.comfy_workflows:
            active_workflow = next(
                (wf for wf in self.comfy_workflows
                 if not wf.compatible_backends or backend.name in wf.compatible_backends),
                None) or self._default_workflow
            if active_workflow:
                logger.info("Workflow-Auto-Pick fuer Backend %s: %s",
                            backend.name, active_workflow.name)

        # Lade per-Agent per-Instanz Config
        cfg = self._get_instance_config(character_name, backend)

        # Prompt Style: Workflow hat Prioritaet, dann Backend-Prefix (externe Provider)
        if active_workflow and active_workflow.prompt_style:
            prompt_style = active_workflow.prompt_style
        else:
            prompt_style = backend.prompt_prefix or "photorealistic"
        # Negative Prompt: Workflow hat Prioritaet, dann per-Agent Config, dann Backend-Default
        if active_workflow and active_workflow.negative_prompt:
            negative_prompt = active_workflow.negative_prompt
        else:
            negative_prompt = cfg.get("negative_prompt", backend.negative_prompt)

        # Task im Queue-System registrieren fuer einheitliche Sichtbarkeit
        _tq = get_task_queue()
        _track_id = _tq.track_start(
            "image_generation", "Bild generieren", agent_name=character_name,
            provider=backend.name)

        try:
            logger.info("=" * 80)
            logger.info("BILDGENERIERUNG GESTARTET")
            logger.info("=" * 80)
            logger.info("Instanz: %s (Typ=%s, Cost=%s)", backend.name, backend.api_type, backend.cost)
            logger.debug("User-ID: %s, Agent: %s, Set as Profile: %s", character_name, set_profile)
            logger.info("Original Prompt: %s", prompt_text)

            # Profilbild-Erkennung
            if not set_profile:
                lowered = prompt_text.lower()
                set_profile = "profilbild" in lowered or "profile image" in lowered or "avatar" in lowered

            # --- RP-Kontext verarbeiten (Deferred Execution) ---
            rp_context = input_data.get("rp_context", "").strip()
            user_text = input_data.get("user_input", "").strip()
            rp_scene_context = ""
            if rp_context and character_name:
                rp_scene_context = self._extract_rp_scene_context(
                    rp_context, character_name)

            # --- Kontext-Daten via PromptBuilder sammeln ---
            from app.core.prompt_builder import (
                PromptBuilder, EntryPointConfig,
                is_photographer_mode, detect_selfie)

            auto_enhance = input_data.get("auto_enhance", True)
            photographer_mode = is_photographer_mode(character_name)
            is_selfie = detect_selfie(prompt_text)
            if photographer_mode:
                logger.info("PHOTOGRAPHER MODE aktiv fuer %s", character_name)

            builder = PromptBuilder(character_name)

            if auto_enhance:
                config = EntryPointConfig.chat()

                # Personen-Detection: NUR aus dem expliziten image_prompt, nicht
                # aus dem RP-Scene-Context. Sonst wird jeder Character, der zufaellig
                # im RP-Kontext eines Tasks erwaehnt ist (z.B. "Logs zu Kai's
                # Aktivitaet"), als Person samt Reference-Bild ins Bild gepushed —
                # auch wenn der Character gar nicht im Bild sein soll.
                # Der User-Avatar wird bei rp_context separat ergaenzt (Block unten),
                # also nichts geht verloren.
                input_appearances = input_data.get("appearances")
                persons = builder.detect_persons(
                    prompt_text,
                    explicit_appearances=input_appearances)

                # Photographer-Filter idempotent anwenden (chat.py:visualize hat
                # ihn bereits aufgerufen, andere Entry Points wie Tool-Call/
                # Instagram noch nicht).
                persons = builder.apply_photographer_filter(
                    persons,
                    photographer_mode=photographer_mode,
                    is_selfie=is_selfie,
                    set_profile=set_profile)

                # Avatar-Augmentation entfernt: Wenn der User-Avatar im Bild
                # sein soll, muss ihn der Tool-LLM namentlich oder via
                # Du-Pronomen erwaehnen. detect_persons() faengt das bereits
                # ab. Automatisches Anhaengen war eine falsche Annahme aus
                # 1:1-Chat-Zeiten und brachte Avatare in Szenen wo sie gar
                # nicht waren (z.B. Bianca macht Selfie waehrend Avatar
                # schlaefft).

                # Item-IDs aus Input (vom Room-Items Panel) — werden in freie
                # Ref-Slots als Props gelegt und im Scene-Prompt beschrieben.
                _item_ids = input_data.get("item_ids") or []
                if isinstance(_item_ids, str):
                    _item_ids = [x.strip() for x in _item_ids.split(",") if x.strip()]

                # Kontext sammeln
                pv = builder.collect_context(
                    persons, config,
                    prompt_text=prompt_text,
                    photographer_mode=photographer_mode,
                    set_profile=set_profile,
                    item_ids=_item_ids)

                # Ausschlussregeln per WorkflowKind:
                #   QWEN_STYLE -> Outfit/Activity/Location aus Text strippen
                #                 (Style-Conditioning macht das visuell)
                #   FLUX_BG/Z_IMAGE -> alles im Text behalten (Charaktere kommen
                #                 ueber Post-Processing, Outfit muss im Prompt sein)
                _excl_kind = active_workflow.kind.value if active_workflow else None
                builder.apply_exclusion_rules(pv, kind=_excl_kind)

                # RP-Szene-Kontext als Scene-Prompt anhaengen
                pv.scene_prompt = prompt_text
                if rp_scene_context:
                    pv.scene_prompt += f", {rp_scene_context}"
                    logger.info("RP-Scene-Context angehaengt: %s", rp_scene_context[:120])

                # Items (Props) als Scene-Zusatz. Slot-Position wird im
                # resolve_reference_slots-Schritt zugeordnet; hier nur
                # Text-Beschreibung anhaengen.
                if pv.items:
                    _item_bits = []
                    for _it in pv.items:
                        _n = _it.get("name", "")
                        _d = _it.get("description", "")
                        if _n and _d:
                            _item_bits.append(f"{_n} ({_d})")
                        elif _n:
                            _item_bits.append(_n)
                    if _item_bits:
                        pv.scene_prompt += f", scene includes props: {', '.join(_item_bits)}"
                        logger.info("Item-Props angehaengt: %s", ", ".join(_item_bits))

                # scene_prompt bereinigen (Defense-in-Depth, Plan 4.2.1b)
                pv.scene_prompt = builder.sanitize_scene_prompt(pv.scene_prompt, pv)

                # Style und Negative-Prompt setzen
                pv.prompt_style = prompt_style
                pv.negative_prompt = negative_prompt

                if set_profile:
                    logger.info("PROFILBILD-MODUS AKTIVIERT")

                # Prompt zusammenbauen via Target-Model-Adapter
                from app.core.prompt_adapters import (
                    get_target_model, render as adapter_render,
                    canonical_to_dict, maybe_enhance_via_llm)
                _wf_image_model = getattr(active_workflow, "image_model", "") if active_workflow else ""
                _wf_file = getattr(active_workflow, "workflow_file", "") if active_workflow else ""
                _backend_model = getattr(backend, "model", "") if backend else ""
                _target_model = get_target_model(_wf_image_model, _wf_file, _backend_model)
                assembled = adapter_render(pv, _target_model)
                template_prompt = assembled["input_prompt_positiv"]
                prompt_without_style = assembled["prompt_without_style"]

                # Optional LLM-Enhancement ueber Workflow-Config (zentral, nicht per-Character)
                _wf_instruction = getattr(active_workflow, "prompt_instruction", "") if active_workflow else ""
                enhanced_prompt, _prompt_method = maybe_enhance_via_llm(
                    template_prompt, pv,
                    target_model=_target_model,
                    prompt_instruction=_wf_instruction)
                _canonical_dict = canonical_to_dict(pv)

                # Abwaertskompatible Variablen fuer restlichen Code
                appearances = [{"name": p.name, "appearance": p.appearance} for p in pv.persons]
                agent_mentioned = any(p.is_agent for p in pv.persons)
                no_person_detected = pv.no_person_detected
            else:
                # auto_enhance=False: Prompt bereits vom Caller angereichert
                input_appearances = input_data.get("appearances")
                if input_appearances is not None:
                    persons = builder.detect_persons(
                        prompt_text, explicit_appearances=input_appearances)
                else:
                    persons = builder.detect_persons(
                        "", character_names=[character_name] if character_name else [])

                from app.core.prompt_builder import PromptVariables
                pv = PromptVariables()
                pv.persons = persons
                pv.negative_prompt = negative_prompt

                # Reference-Bilder trotzdem aufloesen (fuer FaceSwap + Style-Conditioning)
                # profile_only: Profilbild statt Outfit-Bild (z.B. Outfit-Erstellung).
                # Bei set_profile=True (Profilbild-Erstellung) keine Refs —
                # sonst Self-Reference-Loop und kein FaceSwap (siehe Skip-Block weiter unten).
                if not set_profile:
                    _profile_only = bool(input_data.get("profile_only", False))
                    for idx, p in enumerate(persons, 1):
                        ref = builder._resolve_person_ref_image(p, profile_only=_profile_only)
                        if ref:
                            pv.ref_images[idx] = ref
                builder._collect_location(pv)

                appearances = [{"name": p.name, "appearance": p.appearance} for p in persons]
                agent_mentioned = any(p.is_agent for p in persons)
                no_person_detected = False

                enhanced_prompt = prompt_text
                prompt_without_style = prompt_text
                if prompt_style:
                    enhanced_prompt = f"{prompt_style} {enhanced_prompt}"
                logger.info("Auto-Enhance deaktiviert (Prompt vom Caller angereichert)")

                # Canonical-Metadaten fuer Re-Creation auch im auto_enhance=False Pfad
                # (z.B. Instagram). Der Original-Prompt geht 1:1 an ComfyUI, aber
                # canonical wird gespeichert damit "Prompt neu aufbauen" spaeter
                # mit Adapter rendern kann.
                from app.core.prompt_adapters import (
                    get_target_model, canonical_to_dict)
                _wf_image_model = getattr(active_workflow, "image_model", "") if active_workflow else ""
                _wf_file = getattr(active_workflow, "workflow_file", "") if active_workflow else ""
                _backend_model = getattr(backend, "model", "") if backend else ""
                _target_model = get_target_model(_wf_image_model, _wf_file, _backend_model)
                appearances = [{"name": p.name, "appearance": p.appearance} for p in pv.persons]
                pv.prompt_style = prompt_style or "photorealistic"
                pv.scene_prompt = prompt_text
                # Mood/Activity/Outfit aus aktuellem Character-State fuer Rebuild-Kontext
                try:
                    from app.models.character import (
                        get_character_current_feeling,
                        get_character_current_activity,
                        build_equipped_outfit_prompt)
                    if character_name:
                        _mood = get_character_current_feeling(character_name) or ""
                        if _mood:
                            pv.prompt_mood = _mood
                        _act = get_character_current_activity(character_name) or ""
                        if _act:
                            pv.prompt_activity = _act
                        _outfit = build_equipped_outfit_prompt(character_name) or ""
                        if _outfit and persons:
                            pv.prompt_outfits[1] = f"{persons[0].actor_label or persons[0].name} is wearing {_outfit}"
                except Exception:
                    pass
                template_prompt = enhanced_prompt
                _prompt_method = "caller_provided"
                _canonical_dict = canonical_to_dict(pv)

            # Enhanced Prompt fuer Caller verfuegbar machen — thread-local
            # zuerst, damit parallele Generationen sich nicht gegenseitig
            # ueberschreiben (Race-Condition zwischen Instagram-Post und
            # Expression-Regen). self.last_enhanced_prompt bleibt als
            # Backward-Compat fuer non-threaded Caller.
            self._meta_tls.last_enhanced_prompt = enhanced_prompt
            self.last_enhanced_prompt = enhanced_prompt

            # Start-Zeit fuer Logging merken
            _gen_start = time.time()

            # Generierung ueber Backend
            # Workflow-File und Model kommen vom aktiven ComfyWorkflow (falls definiert),
            # sonst Fallback auf Backend-Attribute (Legacy / non-ComfyUI).
            if active_workflow:
                workflow_file = active_workflow.workflow_file
                workflow_model = active_workflow.model
                faceswap_needed = active_workflow.faceswap_needed
                logger.info("Workflow: '%s' (faceswap=%s, model=%s)", active_workflow.name, faceswap_needed, workflow_model)
            else:
                workflow_file = getattr(backend, 'workflow_file', "")
                workflow_model = getattr(backend, 'model', "")
                faceswap_needed = getattr(backend, 'faceswap_needed', False)
            uses_custom_workflow = bool(workflow_file and os.path.exists(workflow_file))

            _ow = input_data.get("override_width")
            _oh = input_data.get("override_height")
            # Prioritaet: override > Workflow > per-Agent Config > Backend > 1024
            _wf_w = active_workflow.width if active_workflow else 0
            _wf_h = active_workflow.height if active_workflow else 0
            params = {
                "width": _ow or _wf_w or cfg.get("width", getattr(backend, 'width', 1024)),
                "height": _oh or _wf_h or cfg.get("height", getattr(backend, 'height', 1024)),
                "workflow_file": workflow_file,
            }
            logger.info("Size: %sx%s (override_w=%s, override_h=%s)", params["width"], params["height"], _ow, _oh)
            # Param-Key: bei input_unet/input_safetensors Workflows -> "unet", sonst "model"
            _model_key = "unet" if (active_workflow and (active_workflow.has_input_unet or active_workflow.has_input_safetensors)) else "model"
            if workflow_model:
                params[_model_key] = workflow_model
            elif active_workflow and active_workflow.model_type and self._model_cache_loaded:
                # Kein Model in .env konfiguriert — Fallback auf erstes Modell aus Cache
                _fallback_models = self.get_cached_checkpoints(active_workflow.model_type)
                if _fallback_models:
                    params[_model_key] = _fallback_models[0]
                    logger.info("Kein Model konfiguriert, Fallback auf: %s", _fallback_models[0])
            # Per-Character Model-Override (aus Skill-Config, ueberschreibt .env Default)
            if active_workflow:
                _agent_cfg = get_character_skill_config(character_name, self.SKILL_ID) or {}
                _char_model = (_agent_cfg.get("workflow_models") or {}).get(active_workflow.name, "").strip()
                if _char_model:
                    params[_model_key] = _char_model
                    logger.info("Per-Character Model: %s", _char_model)
            # Model-Override aus Dialog-Auswahl (hoechste Prioritaet)
            model_override = input_data.get("model_override", "").strip()
            if model_override:
                # Validierung: model_override muss zum model_type des Workflows passen
                if active_workflow and active_workflow.model_type and self._model_cache_loaded:
                    _compatible = self.get_cached_checkpoints(active_workflow.model_type)
                    if _compatible and model_override not in _compatible:
                        logger.warning(
                            "model_override '%s' nicht kompatibel mit Workflow '%s' (model_type=%s) — ignoriert",
                            model_override, active_workflow.name, active_workflow.model_type)
                        model_override = ""
                if model_override:
                    params[_model_key] = model_override
            # Model-Verfuegbarkeit: Pruefen ob Modell auf dem Ziel-Backend existiert, sonst aehnlichstes finden
            _current_model = params.get(_model_key, "")
            if _current_model and backend.api_type == "comfyui":
                _resolved = self.resolve_model_for_backend(_current_model, backend, active_workflow.model_type if active_workflow else "")
                if _resolved and _resolved != _current_model:
                    logger.info("Model-Resolve: %s -> %s (Backend: %s)", _current_model, _resolved, backend.name)
                    params[_model_key] = _resolved

            # Allowed-Models-Liste fuer das Backend mitschicken — der Backend-
            # Code braucht das, um bei Workflows mit MEHREREN Loader-Nodes
            # (z.B. Qwen safetensors+gguf) die jeweils ungenutzten Loader auf
            # eine vorhandene Datei zu setzen. ComfyUI validiert alle Loader,
            # auch wenn sie ueber einen Switch ausgeblendet werden.
            if backend.api_type == "comfyui" and self._model_cache_loaded:
                _all_unet = self._cached_unet_models_by_service.get(backend.name, [])
                _all_ckpt = self._cached_checkpoints_by_service.get(backend.name, [])
                params["allowed_models"] = sorted(set(_all_unet + _all_ckpt))
            # CLIP aus Workflow-Config setzen — gilt fuer alle Workflows die
            # einen externen CLIPLoader haben (Flux2 UND Z-Image etc.).
            if active_workflow and active_workflow.clip:
                params["clip_name"] = active_workflow.clip
                logger.info("CLIP: %s (Workflow: %s)", active_workflow.clip, active_workflow.name)

            if not uses_custom_workflow:
                # Sampling-Params nur fuer Default-/A1111-Workflows relevant
                if hasattr(backend, 'guidance_scale'):
                    params["guidance_scale"] = backend.guidance_scale
                if hasattr(backend, 'num_inference_steps'):
                    params["num_inference_steps"] = backend.num_inference_steps
                if hasattr(backend, 'checkpoint'):
                    params["checkpoint"] = backend.checkpoint
                if hasattr(backend, 'sampler'):
                    params["sampler"] = backend.sampler
                if hasattr(backend, 'scheduler'):
                    params["scheduler"] = backend.scheduler
                if hasattr(backend, 'sampling_method'):
                    params["sampling_method"] = backend.sampling_method
                if hasattr(backend, 'schedule_type'):
                    params["schedule_type"] = backend.schedule_type

            # Seed aus Character-Config (input_seed Node)
            if active_workflow and active_workflow.has_seed:
                _char_seed = int((_agent_cfg.get("comfy_seed") or 0))
                if _char_seed == 0:
                    import random as _rnd
                    _char_seed = _rnd.randint(1, 2**31 - 1)
                    _agent_cfg["comfy_seed"] = _char_seed
                    save_character_skill_config(character_name, self.SKILL_ID, _agent_cfg)
                    logger.info("Seed auto-generiert und gespeichert: %d", _char_seed)
                params["seed"] = _char_seed
                logger.info("Seed: %d", _char_seed)

            # Separated Prompt Workflow (character + pose + expression)
            if active_workflow and active_workflow.has_separated_prompt:
                from app.core.expression_pose_maps import DEFAULT_EXPRESSION, DEFAULT_POSE
                for _sp_key in ("character_prompt", "pose_prompt", "expression_prompt"):
                    _sp_val = input_data.get(_sp_key)
                    if _sp_val:
                        params[_sp_key] = _sp_val
                # Defaults for standard outfit generation (neutral pose + expression)
                if "pose_prompt" not in params:
                    params["pose_prompt"] = DEFAULT_POSE
                if "expression_prompt" not in params:
                    params["expression_prompt"] = DEFAULT_EXPRESSION
                # Aktive Conditions (drunk, exhausted, ...) ersetzen den Expression-Prompt.
                try:
                    from app.core.danger_system import get_active_condition_image_modifiers
                    _cond_mods = get_active_condition_image_modifiers(character_name)
                    if _cond_mods:
                        params["expression_prompt"] = _cond_mods
                        logger.info("Expression durch Condition ersetzt: %s", _cond_mods)
                except Exception as _cm_err:
                    logger.debug("Condition image_modifier Fehler: %s", _cm_err)
                if params.get("character_prompt"):
                    logger.info("Separated Prompt Workflow: character=%d, pose=%d, expression=%d chars",
                                len(params.get("character_prompt", "")),
                                len(params.get("pose_prompt", "")),
                                len(params.get("expression_prompt", "")))

            # LoRA-Inputs: Prioritaet: 1) input_data (Dialog), 2) per-Character Config, 3) Workflow/.env Defaults
            if active_workflow and active_workflow.has_loras:
                loras_override = input_data.get("loras")
                if loras_override is not None:
                    params["lora_inputs"] = list(loras_override)
                else:
                    # Per-Character LoRA-Override aus Skill-Config
                    _agent_cfg_lora = get_character_skill_config(character_name, self.SKILL_ID) or {}
                    _char_loras = (_agent_cfg_lora.get("workflow_loras") or {}).get(active_workflow.name)
                    if _char_loras is not None:
                        params["lora_inputs"] = list(_char_loras)
                        logger.info("Per-Character LoRAs: %s", [l.get("name") for l in _char_loras])
                    elif active_workflow.default_loras:
                        params["lora_inputs"] = list(active_workflow.default_loras)

                # Workflow hat feste Slot-Anzahl (lora_01..lora_04). Auf diese
                # Laenge padden mit 'None'-Plaetzen, damit Piece-LoRAs Platz
                # finden koennen — Char-Override liefert oft nur nicht-None
                # Eintraege (z.B. 2) und liesse sonst keine freien Slots.
                _LORA_SLOT_COUNT = 4
                _cur = params.get("lora_inputs") or []
                while len(_cur) < _LORA_SLOT_COUNT:
                    _cur.append({"name": "None", "strength": 1.0})
                params["lora_inputs"] = _cur

                # Piece-LoRAs: equipped Outfit-Pieces + profile.slot_overrides
                # bringen eigene LoRAs mit. Werden in freie Slots (Name='None')
                # eingefuellt, Duplikate vermeiden, Filter: piece.lora.workflow
                # leer oder == active_workflow.name.
                try:
                    _eq_override = input_data.get("equipped_pieces_override") if isinstance(input_data, dict) else None
                    params["lora_inputs"] = self._merge_piece_loras(
                        params.get("lora_inputs") or [],
                        character_name, active_workflow.name,
                        equipped_override=_eq_override if isinstance(_eq_override, dict) else None)
                except Exception as _ple:
                    logger.debug("Piece-LoRA-Merge fehlgeschlagen: %s", _ple)

                # Final: Log der tatsaechlich verwendeten LoRAs
                _final_names = [l.get("name") for l in (params.get("lora_inputs") or []) if l.get("name") and l.get("name") != "None"]
                if _final_names:
                    logger.info("Final LoRAs (%d/%d): %s", len(_final_names), _LORA_SLOT_COUNT, _final_names)

                # Validierung: LoRAs muessen auf dem ComfyUI-Backend verfuegbar sein
                if params.get("lora_inputs") and self._model_cache_loaded:
                    _avail_loras = self.get_cached_loras()
                    if _avail_loras:
                        _validated = []
                        for _l in params["lora_inputs"]:
                            _ln = _l.get("name", "None")
                            if _ln == "None" or not _ln or _ln in _avail_loras:
                                _validated.append(_l)
                            else:
                                logger.warning("LoRA '%s' nicht verfuegbar — uebersprungen", _ln)
                                _validated.append({"name": "None", "strength": 1.0})
                        params["lora_inputs"] = _validated

            # Face References via PromptBuilder aufloesen
            force_faceswap = input_data.get("force_faceswap", False)
            if not no_person_detected and pv:
                _wf_kind = active_workflow.kind.value if active_workflow else None
                face_refs = builder.resolve_reference_slots(pv, kind=_wf_kind)
                if faceswap_needed:
                    if face_refs.get("reference_images"):
                        params["reference_images"] = face_refs["reference_images"]
                        params["boolean_inputs"] = face_refs.get("boolean_inputs", {})
                        params["string_inputs"] = face_refs.get("string_inputs", {})
                        logger.info("FaceSwap: Referenzen fuer Generierung UND Post-Processing aufgeloest")
                    else:
                        logger.info("FaceSwap: Keine Referenzbilder vorhanden")
                else:
                    params["reference_images"] = face_refs["reference_images"]
                    params["boolean_inputs"] = face_refs["boolean_inputs"]
                    params["string_inputs"] = face_refs["string_inputs"]
            else:
                logger.info("Keine Person erkannt -> keine Referenzbilder")
                face_refs = {"reference_images": {}, "boolean_inputs": {}, "string_inputs": {}, "workflow_has_faceswap": False}

            # FaceSwap Post-Processing bestimmen — gesteuert ueber WorkflowKind:
            #   QWEN_STYLE -> SKIP (Style-Conditioning macht Gesichter visuell)
            #   FLUX_BG / Z_IMAGE -> Post-Processing erlaubt, gesteuert durch
            #                       Character-Config (faceswap_enabled, swap_mode)
            # Die konkrete Methode (internal/comfyui/multiswap) wird unten am
            # Post-Processing-Step entschieden — hier nur "do swap?".
            _kind = active_workflow.kind if active_workflow else None
            _kind_skips_post = (_kind == WorkflowKind.QWEN_STYLE)
            if not faceswap_needed and not _kind_skips_post:
                faceswap_needed = getattr(backend, "faceswap_needed", False)
            needs_comfyui_faceswap = False
            use_standalone_faceswap = False
            # Harter Per-Character-Opt-Out: wenn faceswap_enabled=False in der
            # Skill- oder Character-Config steht, werden alle Swap-Pfade
            # uebersprungen (z.B. Spoty). Ausnahme: force_faceswap.
            _character_opt_out = (
                character_name
                and not force_faceswap
                and not set_profile
                and self._character_swap_disabled(character_name)
            )
            if skip_post_processing:
                logger.info("Face-Post: skip_post_processing=True -> alle Face-Schritte uebersprungen")
                needs_comfyui_faceswap = False
                use_standalone_faceswap = False
                enhance_method = ""
            elif _character_opt_out:
                logger.info("Face-Post: per-Character disabled fuer %s -> alle Swap-Pfade uebersprungen",
                            character_name)
            elif _kind_skips_post:
                logger.info("Face-Post: WorkflowKind=%s -> uebersprungen (Style-Conditioning)", _kind.value)
            elif no_person_detected or set_profile:
                if set_profile:
                    logger.info("Face-Post: set_profile=True -> uebersprungen")
                else:
                    logger.info("Face-Post: Keine Person erkannt -> uebersprungen")
            elif not auto_enhance and not force_faceswap and not faceswap_needed:
                logger.info("Face-Post: auto_enhance=False, kein force/faceswap_needed -> uebersprungen")
            elif (faceswap_needed or force_faceswap) and face_refs.get("workflow_has_faceswap", False):
                # In-Workflow-FaceSwap-Nodes (Legacy Flux2 mit ReActor im Generation-Workflow)
                needs_comfyui_faceswap = True
                logger.info("Face-Post: In-Workflow-Nodes aktiviert (Swap-Methode wird unten per character.swap_mode bestimmt)")
            elif faceswap_needed or force_faceswap:
                # FLUX_BG / Z_IMAGE: Post-Processing-Pass — actual mode (multiswap /
                # comfyui-faceswap / internal) wird unten am Post-Step entschieden.
                use_standalone_faceswap = True
                logger.info("Face-Post: aktiviert (kind=%s, faceswap_needed=%s, force=%s) -- Swap-Methode wird unten gewaehlt",
                            _kind.value if _kind else "n/a", faceswap_needed, force_faceswap)
            else:
                # Weder faceswap_needed noch force, pruefe per-Agent Config
                faceswap_config_enabled = self._should_faceswap(character_name)
                use_standalone_faceswap = faceswap_config_enabled

            # Face Enhancement zusammen mit FaceSwap (ComfyUI oder Standalone)
            if needs_comfyui_faceswap or use_standalone_faceswap:
                enhance_method = self._get_enhance_method(character_name)
            else:
                enhance_method = ""

            _display_model = params.get("model") or getattr(backend, 'model', 'N/A')
            logger.info("Starte Bildgenerierung mit %s (%s)", backend.name, backend.api_url)
            if active_workflow:
                logger.info("Workflow: %s", active_workflow.name)
            logger.info("Model: %s", _display_model)
            logger.debug("Params: %s", params)

            _primary_backend = backend

            def _prepare_for_backend(b):
                """Passt Model + Negative-Prompt fuer Backend b an (Fallback-Pfad)."""
                _cur_model = params.get(_model_key, "")
                if _cur_model and b.api_type == "comfyui":
                    _resolved = self.resolve_model_for_backend(
                        _cur_model, b,
                        active_workflow.model_type if active_workflow else "")
                    if _resolved and _resolved != _cur_model:
                        logger.info("Model-Resolve: %s -> %s (Backend: %s)",
                                    _cur_model, _resolved, b.name)
                        params[_model_key] = _resolved
                elif _cur_model and b.api_type != "comfyui":
                    # Cross-Type-Fallback: ComfyUI-Modellnamen (z.B.
                    # "Flux.2-9B-Q5_K_M.gguf") sind auf Cloud-Backends
                    # (Together/CivitAI/Mammouth) ungueltig. Modell raus,
                    # Backend nutzt seinen konfigurierten Default.
                    if _cur_model != getattr(b, "model", ""):
                        logger.info(
                            "Cross-Type-Fallback: ComfyUI-Modell '%s' inkompatibel mit %s, "
                            "nutze Backend-Default '%s'",
                            _cur_model, b.api_type, getattr(b, "model", "?"))
                        params.pop("model", None)
                        params.pop("unet", None)
                        # LoRAs ebenfalls entfernen — Cloud-Backends akzeptieren
                        # keine lokalen LoRA-Dateinamen.
                        params.pop("lora_inputs", None)
                        params.pop("loras", None)
                _b_cfg = self._get_instance_config(character_name, b)
                _neg = _b_cfg.get("negative_prompt", b.negative_prompt) or negative_prompt
                return enhanced_prompt, _neg

            def _op(b):
                _p, _n = _prepare_for_backend(b)
                _is_local = b.api_type in ("comfyui", "a1111")
                if _is_local:
                    from app.core.llm_queue import get_llm_queue, Priority as _P
                    _vram = (active_workflow.vram_required_mb
                             if active_workflow and active_workflow.vram_required_mb
                             else b.vram_required_mb)
                    return get_llm_queue().submit_gpu_task(
                        task_type="image_generation",
                        priority=_P.IMAGE_GEN,
                        callable_fn=lambda: b.generate(_p, _n, params),
                        agent_name=character_name, label=b.name,
                        vram_required_mb=_vram,
                        gpu_type="comfyui")
                return b.generate(_p, _n, params)

            # Re-Check anderer Backends, falls sie beim Start unavailable waren
            for b in self.backends:
                if b.instance_enabled and not b.available and b != backend:
                    b.check_availability()

            try:
                images, backend = self.run_with_fallback(
                    primary_backend=backend, op=_op,
                    workflow=active_workflow, character_name=character_name)
            except RuntimeError as _err:
                logger.error("Bildgenerierung fehlgeschlagen (alle Backends): %s", _err)
                images = []

            # ComfyUI: Erfolgreich ausgefuehrt aber kein neues Bild (Duplikat/Cache)
            if images == "NO_NEW_IMAGE":
                _tq.track_finish(_track_id, error="Duplikat")
                return ("Das Bild wurde bereits mit diesem Seed und Model generiert. "
                        "Aendere den Seed oder den Prompt, um ein neues Bild zu erzeugen.")

            # Post-Processing-Entscheidung neu treffen, falls Fallback ein
            # anderes Backend gewaehlt hat (faceswap_needed kann differieren).
            if images and backend is not _primary_backend:
                logger.info("Fallback-Backend aktiv: %s -> Post-Processing-Entscheidung neu",
                            backend.name)
                fb_faceswap = (getattr(backend, "faceswap_needed", False)
                               or (active_workflow and active_workflow.faceswap_needed))
                if _kind_skips_post:
                    needs_comfyui_faceswap = False
                    use_standalone_faceswap = False
                elif fb_faceswap and face_refs.get("workflow_has_faceswap", False):
                    needs_comfyui_faceswap = True
                    use_standalone_faceswap = False
                elif fb_faceswap:
                    needs_comfyui_faceswap = False
                    use_standalone_faceswap = True
                else:
                    needs_comfyui_faceswap = False
                    use_standalone_faceswap = self._should_faceswap(character_name) and not set_profile
                if needs_comfyui_faceswap or use_standalone_faceswap:
                    enhance_method = self._get_enhance_method(character_name)
                else:
                    enhance_method = ""

            if not images:
                _tq.track_finish(_track_id, error="Keine Bilder generiert")
                return "API antwortete, aber keine Bilder enthalten."

            _gen_duration = time.time() - _gen_start
            logger.info("ERFOLG - %d Bild(er) generiert via %s (%.1fs)", len(images), backend.name, _gen_duration)

            # Image-Prompt in JSONL loggen (nach Generierung fuer korrekte Dauer)
            # Model-Name je nach Backend-Typ ermitteln
            # Bei ComfyUI: last_used_checkpoint hat den tatsaechlichen Wert aus dem Workflow
            _model_name = getattr(backend, 'last_used_checkpoint', '') or getattr(backend, 'model', '') or getattr(backend, 'checkpoint', '') or ''
            # For separated-prompt workflows, log what actually gets sent to ComfyUI
            _is_separated = active_workflow and active_workflow.has_separated_prompt
            _logged_prompt = params.get("character_prompt", enhanced_prompt) if _is_separated else enhanced_prompt
            log_image_prompt(
                agent_name=character_name,
                original_prompt=prompt_text,
                final_prompt=_logged_prompt,
                negative_prompt=negative_prompt,
                backend_name=backend.name,
                backend_type=backend.api_type,
                model=_model_name,
                appearances=appearances,
                agent_mentioned=agent_mentioned,
                auto_enhance=auto_enhance,
                context={
                    k: v for k, v in {
                        "mood": pv.prompt_mood if pv else "",
                        "activity": pv.prompt_activity if pv else "",
                        "location": pv.prompt_location if pv else "",
                    }.items() if v
                },
                duration_s=_gen_duration,
                seed=params.get("seed", 0),
                pose_prompt=params.get("pose_prompt", ""),
                expression_prompt=params.get("expression_prompt", ""),
                loras=params.get("lora_inputs", []),
                reference_images=params.get("reference_images") or face_refs.get("reference_images") or {})

            # 1. Zuerst Bilder/Videos auf die Platte speichern.
            # Gallery-Target-Routing (Prio von hoch nach niedrig):
            #  (a) `to_avatar_gallery=True` explizit -> Avatar (z.B. SendImage-Intent)
            #  (b) Empfaenger-Erkennung aus Prompt ("Fuer Diego", "An Enzo") ->
            #      Empfaenger's Galerie. Funktioniert auch fuer Background-Thoughts.
            #  (c) Avatar chattet AKTIV mit dem Erzeuger-NPC -> Avatar's Galerie.
            #  (d) Sonst -> Erzeuger behaelt das Bild (Background-Thoughts ohne
            #      klaren Empfaenger).
            # set_profile bleibt beim Agent — sonst landet das Profilbild
            # in der falschen Galerie.
            _explicit_avatar = bool(input_data.get("to_avatar_gallery"))
            gallery_character = character_name
            if not skip_gallery and not set_profile:
                try:
                    from app.models.account import get_active_character
                    from app.models.character import list_available_characters
                    _avatar = (get_active_character() or "").strip()
                    _all_chars = [c for c in list_available_characters() if not c.startswith("_")]

                    # (a) explicit-avatar override wins
                    if _explicit_avatar and _avatar and _avatar != character_name:
                        gallery_character = _avatar
                        logger.info(
                            "Bild wird in Avatar-Galerie gespeichert "
                            "(agent=%s -> avatar=%s, source=explicit)",
                            character_name, _avatar)
                    elif rp_context:
                        # (b) Empfaenger aus Prompt-Text extrahieren
                        recipient = self._detect_recipient_from_prompt(
                            prompt_text, character_name, _all_chars)
                        if recipient:
                            gallery_character = recipient
                            logger.info(
                                "Bild routed zu Empfaenger '%s' (agent=%s, prompt enthaelt 'fuer/an %s')",
                                recipient, character_name, recipient)
                        else:
                            # (c) avatar chattet aktiv mit creator?
                            _is_active_chat = False
                            try:
                                from app.routes.chat import _get_chat_partner
                                _is_active_chat = (_get_chat_partner() or "").strip() == character_name
                            except Exception:
                                pass
                            if _is_active_chat and _avatar and _avatar != character_name:
                                gallery_character = _avatar
                                logger.info(
                                    "Bild wird in Avatar-Galerie gespeichert "
                                    "(agent=%s -> avatar=%s, source=active_chat)",
                                    character_name, _avatar)
                            else:
                                logger.info(
                                    "Bild bleibt bei Erzeuger '%s' "
                                    "(rp_context=True, kein Empfaenger erkannt, kein aktiver Chat mit Avatar)",
                                    character_name)
                except Exception as _gt_err:
                    logger.debug("Gallery-Target-Resolve fehlgeschlagen: %s", _gt_err)

            images_dir = get_character_images_dir(gallery_character)
            saved_files = []
            timestamp = int(time.time())

            for i, image_bytes in enumerate(images, 1):
                ext = self._detect_media_extension(image_bytes)
                # Filename behaelt Agent-Namen (Herkunfts-Hinweis), liegt aber
                # unter gallery_character/images/.
                file_name = f"{character_name}_{timestamp}_{uuid.uuid4().hex[:8]}_{i}{ext}"
                image_path = images_dir / file_name
                image_path.write_bytes(image_bytes)
                if not skip_gallery:
                    add_character_image(gallery_character, file_name)
                    add_character_image_prompt(gallery_character, file_name, prompt_without_style)
                saved_files.append(file_name)

            if not saved_files:
                _tq.track_finish(_track_id, error="Bilder nicht gespeichert")
                return "Fehler: Bilder konnten nicht gespeichert werden."

            logger.info("Gespeicherte Bilder: %s", ", ".join(saved_files))

            if not set_profile:
                lowered = prompt_text.lower()
                set_profile = "profilbild" in lowered or "profile image" in lowered or "avatar" in lowered

            if set_profile:
                set_character_profile_image(character_name, saved_files[0])
                logger.info("Als Profilbild gesetzt: %s", saved_files[0])

            # 2. FaceSwap / MultiSwap Post-Processing
            #    swap_mode bestimmt die Methode: "internal", "comfyui", "multiswap"
            #    FaceSwap braucht nur Profilbilder (nicht Outfit/Expression)
            faceswap_applied = False
            faceswap_method = ""   # "multiswap" | "comfyui" | "internal"
            faceswap_fallback = False  # True wenn primaere Methode fehlschlug
            _do_swap = needs_comfyui_faceswap or use_standalone_faceswap
            if _do_swap:
                _skill_cfg = get_character_skill_config(character_name, "image_generation") if character_name else {}
                _swap_mode = _skill_cfg.get("swap_mode", "default")
                if _swap_mode == "default":
                    _swap_mode = os.environ.get("DEFAULT_SWAP_MODE", "comfyui")
                logger.info("Swap-Modus: %s", _swap_mode)

                # FaceSwap: Referenzbilder nur aus Profilbildern (nicht Outfit/Expression)
                face_refs = self._resolve_face_references(
                    appearances, character_name, profile_only=True)
                logger.info("FaceSwap Referenzen (profile_only): %s",
                            {k: os.path.basename(v) for k, v in face_refs.get("reference_images", {}).items()})

                _fs_vram = int(float(os.environ.get("COMFY_FACESWAP_VRAM_REQUIRED", "0")) * 1024) or (backend.vram_required_mb if backend else 0)

                if _swap_mode == "multiswap":
                    # --- MultiSwap (Flux.2) ---
                    # Bildbereich-Auswahl (face/hair/clothes/...) lebt im
                    # ComfyUI-Workflow selbst, nicht mehr im Code.
                    _tq.track_update_label(_track_id, "MultiSwap")
                    logger.info("MULTISWAP POST-PROCESSING START")
                    from app.core.llm_queue import get_llm_queue, Priority as _P
                    try:
                        swapped = get_llm_queue().submit_gpu_task(
                            task_type="multiswap",
                            priority=_P.IMAGE_GEN,
                            callable_fn=lambda: self._apply_multiswap(
                                saved_files, images_dir, face_refs, backend),
                            agent_name=character_name, label="MultiSwap",
                            vram_required_mb=_fs_vram,
                            gpu_type="comfyui")
                    except Exception as ms_err:
                        logger.error("MultiSwap GPU-Task fehlgeschlagen: %s", ms_err)
                        swapped = []
                    faceswap_applied = len(swapped) > 0
                    faceswap_method = "multiswap"
                    if faceswap_applied:
                        logger.info("MULTISWAP POST-PROCESSING ENDE (%d Bilder)", len(swapped))
                    else:
                        # ComfyUI nicht erreichbar / Workflow gescheitert —
                        # auf internen Face-Service ausweichen.
                        logger.warning("MultiSwap fehlgeschlagen, Fallback auf Face Service")
                        _tq.track_update_label(_track_id, "Face Swap (Fallback)")
                        swapped = self._apply_faceswap_on_files(saved_files, images_dir, character_name, appearances)
                        faceswap_applied = len(swapped) > 0
                        faceswap_method = "internal"
                        faceswap_fallback = True

                elif _swap_mode == "comfyui":
                    # --- ComfyUI FaceSwap (ReActor) ---
                    _tq.track_update_label(_track_id, "Face Swap (ComfyUI)")
                    logger.info("COMFYUI FACESWAP POST-PROCESSING START")
                    from app.core.llm_queue import get_llm_queue, Priority as _P
                    try:
                        swapped = get_llm_queue().submit_gpu_task(
                            task_type="faceswap",
                            priority=_P.IMAGE_GEN,
                            callable_fn=lambda: self._apply_comfyui_faceswap(saved_files, images_dir, face_refs, backend),
                            agent_name=character_name, label="FaceSwap",
                            vram_required_mb=_fs_vram,
                            gpu_type="comfyui")
                    except Exception as fs_err:
                        logger.error("FaceSwap GPU-Task fehlgeschlagen: %s", fs_err)
                        swapped = []
                    faceswap_applied = len(swapped) > 0
                    faceswap_method = "comfyui"
                    if faceswap_applied:
                        logger.info("COMFYUI FACESWAP POST-PROCESSING ENDE (%d Bilder)", len(swapped))
                    else:
                        # ComfyUI nicht erreichbar / ReActor-Workflow gescheitert —
                        # auf internen Face-Service ausweichen.
                        logger.warning("ComfyUI FaceSwap fehlgeschlagen, Fallback auf Face Service")
                        _tq.track_update_label(_track_id, "Face Swap (Fallback)")
                        swapped = self._apply_faceswap_on_files(saved_files, images_dir, character_name, appearances)
                        faceswap_applied = len(swapped) > 0
                        faceswap_method = "internal"
                        faceswap_fallback = True

                else:
                    # --- Internal / Standalone Face Service (default) ---
                    _tq.track_update_label(_track_id, "Face Swap")
                    logger.info("FACESWAP POST-PROCESSING START (internal)")
                    swapped = self._apply_faceswap_on_files(saved_files, images_dir, character_name, appearances)
                    faceswap_applied = len(swapped) > 0
                    faceswap_method = "internal"
                    logger.info("FACESWAP POST-PROCESSING ENDE")
            else:
                logger.debug("FaceSwap: Nicht aktiv")

            # 3. Face Enhancement auf gespeicherte Dateien anwenden
            #    NUR nach internem FaceSwap (face_service). MultiSwap und
            #    ComfyUI ReActor haben eigene Qualitaets-Pipelines —
            #    zusaetzlicher GFPGAN-Pass waere doppelt und verschlechtert
            #    oft das Resultat.
            face_enhance_applied = False
            if (enhance_method == "face_service"
                    and faceswap_method == "internal"
                    and faceswap_applied):
                _tq.track_update_label(_track_id, "Face Enhancement")
                logger.info("FACE ENHANCEMENT (GFPGAN) POST-PROCESSING START")
                try:
                    self._apply_enhance_on_files(saved_files, images_dir)
                    face_enhance_applied = True
                except Exception as _enh_err:
                    logger.warning("Face Enhancement Fehler: %s", _enh_err)
                logger.info("FACE ENHANCEMENT (GFPGAN) POST-PROCESSING ENDE")
            elif enhance_method == "face_service":
                logger.info("FaceEnhance: uebersprungen (Swap-Methode=%s, nur 'internal' bekommt GFPGAN-Pass)",
                            faceswap_method or "none")
            else:
                logger.debug("FaceEnhance: Nicht aktiv")

            # Bild-Metadaten speichern (Skill, Backend, Faceswap, Dauer)
            _wf_name = active_workflow.name if active_workflow else ""
            _location = get_character_current_location(character_name) or ""
            _room_id = get_character_current_room(character_name) or ""
            _lora_meta = [
                {"name": l.get("name", "None"), "strength": l.get("strength", 1.0)}
                for l in params.get("lora_inputs", [])
                if l.get("name") and l["name"] != "None"
            ]
            # Referenzbilder-Namen fuer Metadaten
            # Bei FaceSwap-Workflows liegen Referenzen in face_refs statt params
            _ref_source = params.get("reference_images") or face_refs.get("reference_images") or {}
            _ref_meta = {}
            for _rk, _rv in _ref_source.items():
                _ref_meta[_rk] = os.path.basename(_rv) if _rv else ""
            # Herkunft: wenn das Bild in einer FREMDEN Galerie landet (anderer
            # Character als der Erzeuger), wird der Erzeuger in `from_character`
            # vermerkt. Das Frontend zeigt dann einen Marker am Bild und die
            # Bild-Info nennt explizit von wem das Bild stammt.
            _from_character = character_name if gallery_character != character_name else ""
            _meta = {
                "backend": backend.name,
                "backend_type": backend.api_type,
                "workflow": _wf_name,
                "negative_prompt": negative_prompt,
                "from_character": _from_character,
                "faceswap": faceswap_applied,
                "faceswap_method": faceswap_method,  # "multiswap" | "comfyui" | "internal" | ""
                "faceswap_fallback": faceswap_fallback,  # True wenn primaere Methode fehlschlug
                "face_enhance": face_enhance_applied,  # GFPGAN/Codeformer-Pass erfolgreich
                "guidance_scale": params.get("guidance_scale"),
                "num_inference_steps": params.get("num_inference_steps") or params.get("steps"),
                "duration_s": round(_gen_duration, 1),
                "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                "location": _location,
                "room_id": _room_id,
                "seed": params.get("seed", 0),
                "loras": _lora_meta,
                # Model: Prio params (Dialog-Override / Workflow-Default) > backend.model
                # > backend.last_used_checkpoint > backend.checkpoint. Damit auch
                # bei Cloud-Backends ohne Workflow (Together/CivitAI) ein Modellname
                # in der Bild-Info erscheint.
                "model": (
                    params.get("model")
                    or params.get("unet")
                    or getattr(backend, "model", "")
                    or getattr(backend, "last_used_checkpoint", "")
                    or getattr(backend, "checkpoint", "")
                    or ""),
                "reference_images": _ref_meta,
                "target_model": locals().get("_target_model", ""),
                "canonical": locals().get("_canonical_dict", {}),
                "template_prompt": locals().get("template_prompt", ""),
                "prompt_method": locals().get("_prompt_method", "template"),
                "items_used": list(locals().get("_item_ids") or []),
            }
            # Thread-lokal speichern (fuer parallele Aufrufer)
            # + auf Instanz spiegeln (Backward-Compat fuer non-threaded Caller)
            self._meta_tls.last_image_meta = _meta
            self.last_image_meta = _meta

            if not skip_gallery:
                from app.models.character import add_character_image_metadata
                for fn in saved_files:
                    add_character_image_metadata(gallery_character, fn, _meta)

            # Situations-Kommentar + Bildanalyse generieren
            comment = None
            if not skip_gallery:
                _tq.track_update_label(_track_id, "Bildanalyse")
                logger.info("Starte Bildanalyse + Situations-Kommentar...")
                first_image_path = images_dir / saved_files[0]
                logger.debug("Bild-Datei: %s, Existiert: %s", first_image_path, first_image_path.exists())
                _subjects = [p["name"] for p in appearances] if photographer_mode and appearances else None
                # Comment wird aus Sicht des AGENTS generiert (er hat das Bild
                # gemacht), aber an das Bild des gallery_character geheftet.
                comment = self._generate_comment(
                    character_name, rp_context=rp_context,
                    photographer_subjects=_subjects)
                if comment:
                    logger.info("Situations-Kommentar gespeichert")
                    add_character_image_comment(gallery_character, saved_files[0], comment)
                else:
                    logger.debug("Kein Situations-Kommentar generiert (kein RP-Kontext)")
                # Objektive Bildanalyse: Vision-LLM-Aufruf nutzt Agent-Profil
                # (Sprache/Persoenlichkeit), Ergebnis landet am Bild im Gallery-Char.
                analysis = self._generate_image_analysis(str(first_image_path), character_name)
                if analysis:
                    from app.models.character import add_character_image_metadata
                    add_character_image_metadata(gallery_character, saved_files[0], {"image_analysis": analysis})
                    logger.info("Objektive Bildanalyse gespeichert")
            else:
                logger.debug("Bildanalyse uebersprungen (skip_gallery=True)")

            # Rueckgabe: Bild(er) + Kommentar
            output_lines = []
            output_lines.append(f"AKTION: Bild wurde GENERIERT und in der Galerie von {gallery_character} gespeichert. "
                                f"Das Bild wurde NICHT gesendet oder verschickt — es liegt in der Galerie.")
            for i, file_name in enumerate(saved_files, 1):
                image_url = f"/characters/{gallery_character}/images/{file_name}"
                output_lines.append(f"![Generated Image {i}]({image_url})")

            if comment:
                output_lines.append(f"CAPTION (nur zur Anzeige, NICHT als Fakt behandeln): {comment}")

            logger.info("=" * 80)
            logger.info("BILDGENERIERUNG ABGESCHLOSSEN (via %s)", backend.name)
            logger.info("=" * 80)

            _tq.track_finish(_track_id)
            return "\n\n".join(output_lines)

        except requests.exceptions.Timeout:
            error_msg = f"Bildgenerierung hat zu lange gedauert ({backend.name})"
            logger.error("Timeout: %s", error_msg)
            backend.available = False
            _tq.track_finish(_track_id, error=error_msg)
            return f"Fehler: {error_msg}"
        except requests.exceptions.ConnectionError:
            error_msg = f"Verbindung zu {backend.name} ({backend.api_url}) fehlgeschlagen"
            logger.error("ConnectionError: %s", error_msg)
            backend.available = False
            _tq.track_finish(_track_id, error=error_msg)
            return f"Fehler: {error_msg}"
        except Exception as e:
            error_msg = f"Bildgenerierung ({backend.name}): {e}"
            logger.error("Fehler bei %s", error_msg)
            _tq.track_finish(_track_id, error=error_msg)
            return f"Fehler bei {error_msg}"

    def get_usage_instructions(self, format_name: str = "", **kwargs) -> str:
        if 'usage_instructions' in self.config:
            return self.config['usage_instructions']

        fmt = format_name or "tag"

        return format_example(fmt, self.name, "young woman with blonde hair at the beach, sunset")

    def as_tool(self, **kwargs) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=f"{self.description}. Input should be a detailed description of the desired image.",
            func=self.execute
        )
