"""Image-to-Video Animation - Multi-Service Architektur.

Unterstuetzte Services:
  1. ComfyUI (lokal) - img2video Workflow ueber ComfyUI-Backend
  2. Together.ai (Cloud) - Kling, Wan u.a. Video-Modelle via Together API

Konfiguration via .env:

  # --- ComfyUI Service ---
  ANIMATE_COMFY_ENABLED         - Service aktiviert (default: true)
  ANIMATE_COMFY_LABEL           - Anzeigename im UI (default: "ComfyUI Lokal")
  ANIMATE_COMFY_WORKFLOW_FILE   - Pfad zur Workflow-JSON
  ANIMATE_COMFY_BACKEND         - Name des ComfyUI-Backends
  ANIMATE_COMFY_UNET_LOW        - UNet Modell fuer Low Lighting
  ANIMATE_COMFY_UNET_HIGH       - UNet Modell fuer High Lighting
  ANIMATE_COMFY_CLIP            - CLIP Modell
  ANIMATE_COMFY_WIDTH           - Video-Breite (default: 640)
  ANIMATE_COMFY_HEIGHT          - Video-Hoehe (default: 640)
  ANIMATE_COMFY_POLL_INTERVAL   - Poll-Intervall in Sekunden
  ANIMATE_COMFY_MAX_WAIT        - Max Wartezeit in Sekunden

  # --- Together.ai Service ---
  TOGETHER_ANIMATE_ENABLED      - Service aktiviert (default: false)
  TOGETHER_ANIMATE_LABEL        - Anzeigename (default: "Together.ai Cloud")
  TOGETHER_ANIMATE_API_KEY      - API-Key (oder PROVIDER mit Together-Key)
  TOGETHER_ANIMATE_API_URL      - API-URL (default: https://api.together.xyz)
  TOGETHER_ANIMATE_MODEL        - Modell-ID (z.B. "kwaivgI/kling-2.1-standard")
  TOGETHER_ANIMATE_WIDTH        - Video-Breite (default: 768)
  TOGETHER_ANIMATE_HEIGHT       - Video-Breite (default: 768)
  TOGETHER_ANIMATE_SECONDS      - Video-Laenge in Sekunden (default: 5)
  TOGETHER_ANIMATE_POLL_INTERVAL - Poll-Intervall (default: 5.0)
  TOGETHER_ANIMATE_MAX_WAIT     - Max Wartezeit (default: 600)

Abwaertskompatibilitaet: COMFY_ANIMATE_* wird als Fallback gelesen wenn ANIMATE_COMFY_* fehlt.
"""

import base64
import json
import os
import random
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from app.core.log import get_logger
from app.skills.image_backends import get_active_comfyui_url

logger = get_logger("animate")


# ---------------------------------------------------------------------------
# Hilfsfunktion: .env mit Fallback auf alte COMFY_ANIMATE_* Keys
# ---------------------------------------------------------------------------

def _env(new_key: str, old_key: str = "", default: str = "") -> str:
    """Liest zuerst ANIMATE_COMFY_*, dann Fallback COMFY_ANIMATE_*."""
    val = os.environ.get(new_key, "").strip()
    if val:
        return val
    if old_key:
        val = os.environ.get(old_key, "").strip()
        if val:
            return val
    return default


# ═══════════════════════════════════════════════════════════════════════════
# Abstrakte Basis
# ═══════════════════════════════════════════════════════════════════════════

class AnimateService(ABC):
    """Abstrakte Basis fuer Animation-Services."""

    service_id: str = ""
    label: str = ""
    enabled: bool = False

    @abstractmethod
    def animate(self, source_image_path: str, prompt: str, output_path: str) -> bool:
        """Generiert ein Video aus einem Quellbild.

        Returns:
            True bei Erfolg, False bei Fehler.
        """

    def info(self) -> Dict[str, Any]:
        """Liefert Service-Info fuer das Frontend."""
        data: Dict[str, Any] = {"id": self.service_id, "label": self.label, "enabled": self.enabled}
        if hasattr(self, "default_loras_high"):
            data["has_loras"] = True
            data["default_loras_high"] = self.default_loras_high
            data["default_loras_low"] = self.default_loras_low
        return data


# ═══════════════════════════════════════════════════════════════════════════
# ComfyUI Service (bestehende Logik)
# ═══════════════════════════════════════════════════════════════════════════

class ComfyAnimateService(AnimateService):
    """Animation via ComfyUI img2video Workflow."""

    service_id = "comfy"

    def __init__(self):
        self.label = _env("ANIMATE_COMFY_LABEL", "", "ComfyUI Lokal")
        self.enabled = _env("ANIMATE_COMFY_ENABLED", "COMFY_ANIMATE_ENABLED", "true").lower() in ("true", "1", "yes")
        self.workflow_file = _env(
            "ANIMATE_COMFY_WORKFLOW_FILE", "COMFY_ANIMATE_WORKFLOW_FILE",
            os.path.join(os.path.dirname(__file__), "..", "..", "workflows", "img2video_workflow_api.json"))
        self.backend_name = _env("ANIMATE_COMFY_BACKEND", "COMFY_ANIMATE_BACKEND")
        self.unet_low = _env("ANIMATE_COMFY_UNET_LOW", "COMFY_ANIMATE_UNET_LOW")
        self.unet_high = _env("ANIMATE_COMFY_UNET_HIGH", "COMFY_ANIMATE_UNET_HIGH")
        self.clip = _env("ANIMATE_COMFY_CLIP", "COMFY_ANIMATE_CLIP")
        self.width = int(_env("ANIMATE_COMFY_WIDTH", "COMFY_ANIMATE_WIDTH", "640"))
        self.height = int(_env("ANIMATE_COMFY_HEIGHT", "COMFY_ANIMATE_HEIGHT", "640"))
        self.poll_interval = float(_env("ANIMATE_COMFY_POLL_INTERVAL", "COMFY_ANIMATE_POLL_INTERVAL", "2.0"))
        self.max_wait = int(_env("ANIMATE_COMFY_MAX_WAIT", "COMFY_ANIMATE_MAX_WAIT", "600"))

        # LoRA-Defaults aus .env laden (je 4 High + 4 Low)
        self.default_loras_high = self._load_lora_defaults("HIGH")
        self.default_loras_low = self._load_lora_defaults("LOW")

        pass  # Config done

    @staticmethod
    def _load_lora_defaults(variant: str) -> list:
        """Laedt 4 LoRA-Defaults fuer HIGH oder LOW aus .env."""
        loras = []
        for i in range(1, 5):
            name = _env(f"ANIMATE_COMFY_LORA_{variant}_{i:02d}", f"COMFY_ANIMATE_LORA_{variant}_{i:02d}") or "None"
            strength_str = _env(f"ANIMATE_COMFY_LORA_{variant}_{i:02d}_STRENGTH", f"COMFY_ANIMATE_LORA_{variant}_{i:02d}_STRENGTH", "1")
            try:
                strength = float(strength_str) if strength_str else 1.0
            except ValueError:
                strength = 1.0
            loras.append({"name": name, "strength": strength})
        return loras

    # --- ComfyUI URL Resolution ---

    def _resolve_url(self) -> str:
        if self.backend_name:
            for n in range(1, 20):
                prefix = f"SKILL_IMAGEGEN_{n}_"
                name = os.environ.get(f"{prefix}NAME", "").strip()
                if not name and not os.environ.get(f"{prefix}API_TYPE", ""):
                    break
                if name == self.backend_name:
                    api_type = os.environ.get(f"{prefix}API_TYPE", "").strip().lower()
                    if api_type != "comfyui":
                        logger.warning("ANIMATE_COMFY_BACKEND '%s' ist kein ComfyUI-Backend (type=%s)", self.backend_name, api_type)
                        break
                    enabled = os.environ.get(f"{prefix}ENABLED", "true").lower() in ("true", "1", "yes")
                    if not enabled:
                        logger.warning("ANIMATE_COMFY_BACKEND '%s' ist deaktiviert", self.backend_name)
                        break
                    url = os.environ.get(f"{prefix}API_URL", "").strip().rstrip("/")
                    if url:
                        logger.debug("Verwende konfiguriertes Backend: %s (%s)", self.backend_name, url)
                        return url
                    break
            logger.warning("ANIMATE_COMFY_BACKEND '%s' nicht gefunden, verwende Fallback", self.backend_name)
        return get_active_comfyui_url()

    # --- Animate ---

    def animate(self, source_image_path: str, prompt: str, output_path: str,
                loras_high: Optional[list] = None, loras_low: Optional[list] = None) -> bool:
        if not self.enabled:
            logger.warning("ComfyUI Animation ist deaktiviert")
            return False

        comfyui_url = self._resolve_url()
        if not comfyui_url:
            logger.error("Kein erreichbarer ComfyUI-Dienst gefunden")
            return False

        # 1. Load workflow
        if not os.path.exists(self.workflow_file):
            logger.error("Workflow-Datei nicht gefunden: %s", self.workflow_file)
            return False
        with open(self.workflow_file, encoding="utf-8") as f:
            workflow = json.load(f)

        # 2. Upload source image
        uploaded_name = self._upload_image(comfyui_url, source_image_path)
        if not uploaded_name:
            return False

        # 3. Set input_image node
        img_node_id = _find_node_by_title(workflow, "input_image")
        if not img_node_id:
            logger.error("Node 'input_image' nicht im Workflow gefunden")
            return False
        workflow[img_node_id]["inputs"]["image"] = uploaded_name

        # 4. Set input_prompt node
        prompt_node_id = _find_node_by_title(workflow, "input_prompt")
        if not prompt_node_id:
            logger.error("Node 'input_prompt' nicht im Workflow gefunden")
            return False
        workflow[prompt_node_id]["inputs"]["text"] = prompt

        # 5. Override models, dimensions, and LoRAs
        self._apply_model_overrides(workflow)
        self._apply_dimension_overrides(workflow)
        self._apply_lora_overrides(workflow, loras_high, loras_low)

        # 6. Randomize seed in KSamplerAdvanced nodes
        for node_id, node in workflow.items():
            if node.get("class_type") == "KSamplerAdvanced":
                inputs = node.get("inputs", {})
                if "noise_seed" in inputs and isinstance(inputs["noise_seed"], (int, float)):
                    inputs["noise_seed"] = random.randint(0, 2**63)

        # 7. Queue the prompt
        try:
            resp = requests.post(f"{comfyui_url}/prompt", json={"prompt": workflow}, timeout=30)
        except Exception as e:
            logger.error("ComfyUI Verbindungsfehler: %s", e)
            return False
        if resp.status_code != 200:
            logger.error("ComfyUI prompt queue fehlgeschlagen: HTTP %d - %s", resp.status_code, resp.text[:500])
            return False
        prompt_id = resp.json().get("prompt_id", "")
        if not prompt_id:
            logger.error("Keine prompt_id erhalten")
            return False
        logger.info("Animation in Warteschlange: %s (Backend: %s)", prompt_id, comfyui_url)

        # 8. Poll for completion
        start = time.time()
        outputs = None
        while time.time() - start < self.max_wait:
            time.sleep(self.poll_interval)
            try:
                hist_resp = requests.get(f"{comfyui_url}/history/{prompt_id}", timeout=10)
                if hist_resp.status_code != 200:
                    continue
                history = hist_resp.json()
                if prompt_id not in history:
                    continue
                status = history[prompt_id].get("status", {})
                if status.get("status_str") == "error":
                    msgs = status.get("messages", [])
                    logger.error("ComfyUI Ausfuehrungsfehler: %s", str(msgs)[:500])
                    return False
                outputs = history[prompt_id].get("outputs", {})
                if outputs:
                    logger.info("Animation fertig nach %.1fs", time.time() - start)
                    break
            except Exception as e:
                logger.warning("Poll-Fehler: %s", e)
                continue
        else:
            logger.error("Animation Timeout nach %ds", self.max_wait)
            return False

        # 9. Download video from outputs — bevorzugt output_final, sonst alle.
        final_node_id = _find_node_by_title(workflow, "output_final")
        target_outputs = (
            {final_node_id: outputs[final_node_id]}
            if final_node_id and final_node_id in outputs
            else outputs
        )
        for node_id, node_output in target_outputs.items():
            items = (
                node_output.get("gifs", [])
                + node_output.get("videos", [])
                + node_output.get("images", [])
            )
            for item in items:
                filename = item.get("filename", "")
                if not filename:
                    continue
                subfolder = item.get("subfolder", "")
                file_type = item.get("type", "output")
                params = {"filename": filename, "type": file_type}
                if subfolder:
                    params["subfolder"] = subfolder
                try:
                    dl_resp = requests.get(f"{comfyui_url}/view", params=params, timeout=120)
                    if dl_resp.status_code == 200 and len(dl_resp.content) > 1000:
                        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                        Path(output_path).write_bytes(dl_resp.content)
                        logger.info("Video gespeichert: %s (%d bytes)", output_path, len(dl_resp.content))
                        return True
                except Exception as e:
                    logger.error("Video-Download Fehler: %s", e)

        logger.error("Kein Video in ComfyUI-Ausgabe gefunden")
        return False

    # --- Hilfsmethoden ---

    def _apply_model_overrides(self, workflow: dict) -> None:
        # Model-Verfuegbarkeit: Pruefen ob Modell auf dem Backend existiert, sonst aehnlichstes finden
        unet_low = self.unet_low
        unet_high = self.unet_high
        if unet_low or unet_high:
            # Modelle auf dem Backend pruefen
            _url = self._resolve_url()
            if _url:
                from app.skills.image_generation_skill import ImageGenerationSkill
                _available = ImageGenerationSkill.fetch_models_from_url(_url, "unet")
                if _available:
                    if unet_low and unet_low not in _available:
                        _resolved = ImageGenerationSkill.find_closest_model(unet_low, _available)
                        if _resolved:
                            logger.info("Model-Resolve: unet_low %s -> %s", unet_low, _resolved)
                            unet_low = _resolved
                    if unet_high and unet_high not in _available:
                        _resolved = ImageGenerationSkill.find_closest_model(unet_high, _available)
                        if _resolved:
                            logger.info("Model-Resolve: unet_high %s -> %s", unet_high, _resolved)
                            unet_high = _resolved
        if unet_low or unet_high:
            # Methode 1: LoraLoaderModelOnly → referenzierten UNet-Node ueberschreiben
            for node_id, node in workflow.items():
                if node.get("class_type") != "LoraLoaderModelOnly":
                    continue
                lora_name = node.get("inputs", {}).get("lora_name", "")
                model_ref = node.get("inputs", {}).get("model")
                if not isinstance(model_ref, list) or len(model_ref) < 1:
                    continue
                unet_node_id = str(model_ref[0])
                unet_node = workflow.get(unet_node_id)
                if not unet_node or "unet_name" not in unet_node.get("inputs", {}):
                    continue
                if "LOW" in lora_name.upper() and unet_low:
                    unet_node["inputs"]["unet_name"] = unet_low
                    logger.debug("UNet LOW override via LoraLoader (node %s): %s", unet_node_id, unet_low)
                elif "HIGH" in lora_name.upper() and unet_high:
                    unet_node["inputs"]["unet_name"] = unet_high
                    logger.debug("UNet HIGH override via LoraLoader (node %s): %s", unet_node_id, unet_high)

            # Methode 2: Direkte UnetLoaderGGUF / UNETLoader Nodes per Title
            for node_id, node in workflow.items():
                ct = node.get("class_type", "")
                if ct not in ("UnetLoaderGGUF", "UNETLoader"):
                    continue
                title = node.get("_meta", {}).get("title", "").lower()
                inputs = node.get("inputs", {})
                if "unet_name" not in inputs:
                    continue
                if "high" in title and unet_high:
                    inputs["unet_name"] = unet_high
                    logger.debug("UNet HIGH override direkt (node %s): %s", node_id, unet_high)
                elif "low" in title and unet_low:
                    inputs["unet_name"] = unet_low
                    logger.debug("UNet LOW override direkt (node %s): %s", node_id, unet_low)

        if self.clip:
            for node_id, node in workflow.items():
                if node.get("class_type") == "CLIPLoader" and "clip_name" in node.get("inputs", {}):
                    node["inputs"]["clip_name"] = self.clip
                    logger.debug("CLIP override (node %s): %s", node_id, self.clip)
                    break

    def _apply_lora_overrides(self, workflow: dict,
                              loras_high: Optional[list] = None,
                              loras_low: Optional[list] = None) -> None:
        """Setzt LoRAs in input_lora_high / input_lora_low Nodes (Power Lora Loader)."""
        effective_high = loras_high or self.default_loras_high
        effective_low = loras_low or self.default_loras_low

        for node_id, node in workflow.items():
            title = node.get("_meta", {}).get("title", "").lower()
            if title == "input_lora_high":
                self._set_power_lora(node, effective_high)
                logger.debug("LoRA HIGH override (node %s): %s", node_id,
                             [l.get("name") for l in effective_high])
            elif title == "input_lora_low":
                self._set_power_lora(node, effective_low)
                logger.debug("LoRA LOW override (node %s): %s", node_id,
                             [l.get("name") for l in effective_low])

    @staticmethod
    def _set_power_lora(node: dict, loras: list) -> None:
        """Setzt lora_1..4 in einem Power Lora Loader (rgthree) Node."""
        inputs = node.get("inputs", {})
        for i, lora in enumerate(loras[:4], start=1):
            key = f"lora_{i}"
            name = lora.get("name", "None") or "None"
            strength = lora.get("strength", 1.0)
            on = name != "None"
            if key in inputs and isinstance(inputs[key], dict):
                inputs[key]["on"] = on
                inputs[key]["lora"] = name if on else "None"
                inputs[key]["strength"] = strength
            else:
                inputs[key] = {"on": on, "lora": name if on else "None", "strength": strength}

    def _apply_dimension_overrides(self, workflow: dict) -> None:
        for node_id, node in workflow.items():
            if node.get("class_type") == "LoadAndResizeImage":
                inputs = node.get("inputs", {})
                if "width" in inputs:
                    inputs["width"] = self.width
                if "height" in inputs:
                    inputs["height"] = self.height
                logger.debug("Dimension override (node %s): %dx%d", node_id, self.width, self.height)

    @staticmethod
    def _upload_image(comfyui_url: str, file_path: str) -> Optional[str]:
        path = Path(file_path)
        if not path.exists():
            logger.error("Upload: Datei nicht gefunden: %s", file_path)
            return None
        try:
            with open(file_path, "rb") as f:
                files = {"image": (path.name, f, "image/png")}
                data = {"subfolder": "", "type": "input", "overwrite": "true"}
                resp = requests.post(f"{comfyui_url}/upload/image", files=files, data=data, timeout=30)
            if resp.status_code == 200:
                name = resp.json().get("name", path.name)
                logger.info("Bild hochgeladen: %s -> %s", path.name, name)
                return name
            logger.error("Upload fehlgeschlagen: HTTP %d", resp.status_code)
        except Exception as e:
            logger.error("Upload Fehler: %s", e)
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Together.ai Service
# ═══════════════════════════════════════════════════════════════════════════

class TogetherAnimateService(AnimateService):
    """Animation via Together.ai Video Generation API (Kling, Wan, etc.)."""

    service_id = "together"

    def __init__(self):
        self.label = os.environ.get("TOGETHER_ANIMATE_LABEL", "Together.ai Cloud").strip()
        self.enabled = os.environ.get("TOGETHER_ANIMATE_ENABLED", "false").strip().lower() in ("true", "1", "yes")
        self.api_key = os.environ.get("TOGETHER_ANIMATE_API_KEY", "").strip()
        self.api_url = os.environ.get("TOGETHER_ANIMATE_API_URL", "https://api.together.xyz").strip().rstrip("/")
        self.model = os.environ.get("TOGETHER_ANIMATE_MODEL", "").strip()
        self.width = int(os.environ.get("TOGETHER_ANIMATE_WIDTH", "768"))
        self.height = int(os.environ.get("TOGETHER_ANIMATE_HEIGHT", "768"))
        self.seconds = int(os.environ.get("TOGETHER_ANIMATE_SECONDS", "5"))
        self.poll_interval = float(os.environ.get("TOGETHER_ANIMATE_POLL_INTERVAL", "5.0"))
        self.max_wait = int(os.environ.get("TOGETHER_ANIMATE_MAX_WAIT", "600"))

        # Fallback: API-Key aus Together-Provider holen
        if not self.api_key:
            self.api_key = self._find_together_api_key()

    @staticmethod
    def _find_together_api_key() -> str:
        """Sucht den Together-API-Key aus PROVIDER_*-Konfiguration."""
        for n in range(1, 20):
            name = os.environ.get(f"PROVIDER_{n}_NAME", "").strip()
            if not name:
                break
            api_base = os.environ.get(f"PROVIDER_{n}_API_BASE", "").strip()
            if "together" in name.lower() or "together" in api_base.lower():
                key = os.environ.get(f"PROVIDER_{n}_API_KEY", "").strip()
                if key:
                    return key
        return ""

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def animate(self, source_image_path: str, prompt: str, output_path: str) -> bool:
        if not self.enabled:
            logger.warning("Together Animation ist deaktiviert")
            return False
        if not self.api_key:
            logger.error("Together Animation: Kein API-Key konfiguriert")
            return False
        if not self.model:
            logger.error("Together Animation: Kein Modell konfiguriert")
            return False

        # 1. Bild als base64 lesen
        path = Path(source_image_path)
        if not path.exists():
            logger.error("Quelldatei nicht gefunden: %s", source_image_path)
            return False

        with open(source_image_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")

        # MIME-Type bestimmen
        suffix = path.suffix.lower()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(
            suffix.lstrip("."), "image/png"
        )
        data_uri = f"data:{mime};base64,{image_b64}"

        # 2. Video-Job erstellen
        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "width": self.width,
            "height": self.height,
            "seconds": self.seconds,
            "output_format": "MP4",
            "frame_images": [
                {"input_image": data_uri, "frame": "first"}
            ],
        }

        logger.info("Together Animation starten: model=%s, %dx%d, %ds", self.model, self.width, self.height, self.seconds)

        try:
            resp = requests.post(
                f"{self.api_url}/v2/videos",
                json=payload,
                headers=self._headers(),
                timeout=60)
        except Exception as e:
            logger.error("Together API Verbindungsfehler: %s", e)
            return False

        if resp.status_code not in (200, 201, 202):
            logger.error("Together Video-Job fehlgeschlagen: HTTP %d - %s", resp.status_code, resp.text[:500])
            return False

        job = resp.json()
        job_id = job.get("id", "")
        if not job_id:
            logger.error("Keine Job-ID erhalten: %s", str(job)[:300])
            return False

        logger.info("Together Video-Job erstellt: %s", job_id)

        # 3. Poll fuer Ergebnis
        start = time.time()
        while time.time() - start < self.max_wait:
            time.sleep(self.poll_interval)
            try:
                poll_resp = requests.get(
                    f"{self.api_url}/v2/videos/{job_id}",
                    headers=self._headers(),
                    timeout=30)
                if poll_resp.status_code != 200:
                    logger.warning("Together Poll HTTP %d", poll_resp.status_code)
                    continue

                status_data = poll_resp.json()
                status = status_data.get("status", "")

                if status == "failed":
                    error_info = status_data.get("error", {})
                    logger.error("Together Video fehlgeschlagen: %s", str(error_info)[:300])
                    return False

                if status == "completed":
                    outputs = status_data.get("outputs", {})
                    video_url = outputs.get("video_url", "")
                    if not video_url:
                        logger.error("Kein video_url in Together-Antwort")
                        return False

                    logger.info("Together Video fertig nach %.1fs, lade herunter...", time.time() - start)

                    # Video herunterladen
                    try:
                        dl_resp = requests.get(video_url, timeout=120)
                        if dl_resp.status_code == 200 and len(dl_resp.content) > 1000:
                            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                            Path(output_path).write_bytes(dl_resp.content)
                            logger.info("Video gespeichert: %s (%d bytes)", output_path, len(dl_resp.content))
                            return True
                        logger.error("Video-Download fehlgeschlagen: HTTP %d, %d bytes",
                                     dl_resp.status_code, len(dl_resp.content))
                    except Exception as e:
                        logger.error("Video-Download Fehler: %s", e)
                    return False

                # queued / in_progress → weiter warten
                logger.debug("Together Status: %s (%.0fs)", status, time.time() - start)

            except Exception as e:
                logger.warning("Together Poll-Fehler: %s", e)
                continue

        logger.error("Together Animation Timeout nach %ds", self.max_wait)
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Service Registry & oeffentliche API
# ═══════════════════════════════════════════════════════════════════════════

_services: Optional[Dict[str, AnimateService]] = None


def _load_services() -> Dict[str, AnimateService]:
    """Initialisiert alle konfigurierten Animation-Services."""
    global _services
    if _services is not None:
        return _services

    _services = {}

    comfy = ComfyAnimateService()
    if comfy.enabled:
        _services[comfy.service_id] = comfy
        logger.info("Animation Service geladen: %s (%s)", comfy.service_id, comfy.label)

    together = TogetherAnimateService()
    if together.enabled:
        _services[together.service_id] = together
        logger.info("Animation Service geladen: %s (%s)", together.service_id, together.label)

    if not _services:
        logger.warning("Keine Animation-Services aktiviert")

    return _services


def reload_animate_services() -> None:
    """Setzt den Service-Cache zurueck, damit Services bei naechstem Aufruf neu geladen werden."""
    global _services
    _services = None
    logger.info("Animation-Services Cache zurueckgesetzt")


def get_animate_services() -> List[Dict[str, Any]]:
    """Liefert Liste der verfuegbaren Animation-Services fuer das Frontend."""
    services = _load_services()
    return [svc.info() for svc in services.values()]


def animate_image(
    source_image_path: str,
    prompt: str,
    output_path: str,
    service: str = "",
    loras_high: Optional[list] = None,
    loras_low: Optional[list] = None) -> bool:
    """Animiert ein Bild als Video.

    Args:
        source_image_path: Pfad zum Quellbild.
        prompt: Text-Prompt fuer die Animation.
        output_path: Ausgabepfad fuer das Video.
        service: Service-ID ("comfy", "together"). Leer = erster verfuegbarer.
        loras_high: LoRA-Liste fuer High-Noise [{name, strength}, ...] oder None fuer Defaults.
        loras_low: LoRA-Liste fuer Low-Noise [{name, strength}, ...] oder None fuer Defaults.

    Returns:
        True bei Erfolg, False bei Fehler.
    """
    services = _load_services()
    if not services:
        logger.error("Keine Animation-Services verfuegbar")
        return False

    if service and service in services:
        svc = services[service]
    elif service:
        logger.warning("Unbekannter Animation-Service '%s', verwende Standard", service)
        svc = next(iter(services.values()))
    else:
        svc = next(iter(services.values()))

    logger.info("Animation mit Service '%s' (%s)", svc.service_id, svc.label)
    if isinstance(svc, ComfyAnimateService):
        return svc.animate(source_image_path, prompt, output_path,
                           loras_high=loras_high, loras_low=loras_low)
    return svc.animate(source_image_path, prompt, output_path)


# ═══════════════════════════════════════════════════════════════════════════
# Hilfsfunktionen (modulweit)
# ═══════════════════════════════════════════════════════════════════════════

def _find_node_by_title(workflow: dict, title: str) -> Optional[str]:
    """Find a workflow node ID by its _meta.title (case-insensitive)."""
    title_lower = title.lower()
    for node_id, node in workflow.items():
        if node.get("_meta", {}).get("title", "").lower() == title_lower:
            return node_id
    return None
