"""Image Generation Backends - Verschiedene API-Typen fuer Bildgenerierung"""
import base64
import json
import os
import random
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import requests

from app.core.log import get_logger
logger = get_logger("image_backends")


_comfyui_url_cache: dict = {"url": None, "expires": 0.0}
_COMFYUI_CACHE_TTL = 60.0


def get_active_comfyui_url() -> str:
    """Gibt die URL des ersten erreichbaren ComfyUI Image-Gen-Dienstes zurueck.

    Durchsucht SKILL_IMAGEGEN_1_*, SKILL_IMAGEGEN_2_*, ... und testet jeden
    aktivierten ComfyUI-Dienst auf Erreichbarkeit. Gibt die URL des ersten
    antwortenden Dienstes zurueck. Ergebnis wird 60s gecacht.

    Returns:
        API-URL ohne trailing slash, oder "" wenn kein ComfyUI-Dienst erreichbar.
    """
    global _comfyui_url_cache
    import time as _t
    now = _t.time()
    if _comfyui_url_cache["url"] is not None and now < _comfyui_url_cache["expires"]:
        return _comfyui_url_cache["url"]

    i = 1
    while True:
        name = os.environ.get(f"SKILL_IMAGEGEN_{i}_NAME")
        if name is None:
            break
        enabled = os.environ.get(f"SKILL_IMAGEGEN_{i}_ENABLED", "true").strip().lower() in ("true", "1", "yes")
        api_type = os.environ.get(f"SKILL_IMAGEGEN_{i}_API_TYPE", "").strip().lower()
        if enabled and api_type == "comfyui":
            url = os.environ.get(f"SKILL_IMAGEGEN_{i}_API_URL", "").strip().rstrip("/")
            if url:
                try:
                    resp = requests.get(f"{url}/system_stats", timeout=3)
                    if resp.status_code == 200:
                        logger.info(f"Aktiver Dienst: {name} ({url})")
                        _comfyui_url_cache["url"] = url
                        _comfyui_url_cache["expires"] = now + _COMFYUI_CACHE_TTL
                        return url
                    # /system_stats kann 500 werfen (kaputter Custom-Node);
                    # Fallback /queue probieren bevor wir den Dienst aufgeben.
                    q_resp = requests.get(f"{url}/queue", timeout=3)
                    if q_resp.status_code == 200:
                        logger.info(f"Aktiver Dienst: {name} ({url}) — via /queue, /system_stats wirft {resp.status_code}")
                        _comfyui_url_cache["url"] = url
                        _comfyui_url_cache["expires"] = now + _COMFYUI_CACHE_TTL
                        return url
                    logger.warning(f"{name} ({url}): HTTP {resp.status_code}, uebersprungen")
                except Exception:
                    logger.warning(f"{name} ({url}): nicht erreichbar, uebersprungen")
        i += 1

    logger.warning("Kein erreichbarer ComfyUI-Dienst gefunden")
    _comfyui_url_cache["url"] = ""
    _comfyui_url_cache["expires"] = now + _COMFYUI_CACHE_TTL
    return ""


class ImageBackend(ABC):
    """
    Basisklasse fuer Image Generation Backends.

    Jedes Backend implementiert ein spezifisches API-Protokoll
    (z.B. A1111/Forge, Mammouth/OpenAI-kompatibel).
    """

    # Throttle fuer "nicht erreichbar"-Warnungen — log spam vermeiden bei
    # dauerhaft offline Backends. Erstes Mal WARNING, dann fuer
    # _UNREACHABLE_THROTTLE_SEC Sekunden auf DEBUG reduziert. Wiederkehr
    # (avail True nach False) wird als INFO geloggt.
    _UNREACHABLE_THROTTLE_SEC = 300

    def __init__(self, name: str, api_url: str, cost: float, api_type: str, env_prefix: str):
        self.name = name
        self.api_url = api_url.rstrip("/")
        self.cost = cost
        self.api_type = api_type
        self.env_prefix = env_prefix
        self.available = False
        self._active_jobs = 0
        self._jobs_lock = __import__("threading").Lock()
        # Letzter Zeitpunkt an dem wir "nicht erreichbar" als WARNING geloggt haben
        self._last_unreachable_warn_ts: float = 0.0
        # Vorheriger Verfuegbarkeitsstatus — Uebergang False->True triggert "wieder da"
        self._was_available: Optional[bool] = None

        # Instanz-Enabled aus .env (globaler Default)
        self.instance_enabled = os.environ.get(f"{env_prefix}ENABLED", "true").strip().lower() in ("true", "1", "yes")

        # Prompt-Konfiguration pro Instanz aus .env
        self.prompt_prefix = os.environ.get(f"{env_prefix}PROMPT_PREFIX", "").strip()
        self.negative_prompt = os.environ.get(f"{env_prefix}NEGATIVE_PROMPT", "").strip()

        # FaceSwap: Gibt an ob generierte Bilder FaceSwap benoetigen (Post-Processing)
        self.faceswap_needed = os.environ.get(f"{env_prefix}FACESWAP_NEEDED",
            os.environ.get(f"{env_prefix}FACESWAP", "false")  # Backward-Compat
        ).strip().lower() in ("true", "1", "yes")

        # Fallback-Strategie: was tun wenn dieses Backend nicht verfuegbar ist?
        #   none           → Fehler an Caller, kein Versuch eines anderen Backends
        #   next_cheaper   → naechst-billigeres Backend mit dessen Default-Workflow
        #   specific       → genau das in fallback_specific eingetragene Backend
        # Default: next_cheaper (sanftester Fallback).
        _fb_mode = os.environ.get(f"{env_prefix}FALLBACK_MODE", "next_cheaper").strip().lower()
        if _fb_mode not in ("none", "next_cheaper", "specific"):
            _fb_mode = "next_cheaper"
        self.fallback_mode = _fb_mode
        self.fallback_specific = os.environ.get(f"{env_prefix}FALLBACK_SPECIFIC", "").strip()

        # VRAM-Bedarf in GB (fuer intelligentes VRAM-Management)
        vram_str = os.environ.get(f"{env_prefix}VRAM_REQUIRED", "").strip()
        self.vram_required_mb = int(float(vram_str) * 1024) if vram_str else 0

        # NVFP4-Architektur: Backend benoetigt NVFP4-quantisierte Modelle (legacy)
        self.nvfp4 = os.environ.get(
            f"{env_prefix}NVFP4", "false"
        ).strip().lower() in ("true", "1", "yes")

    @property
    def effective_cost(self) -> float:
        """Semantische Kosten des Backends. Wird vom Selektor zur
        Bevorzugung genutzt (lokal=0, Cloud > 0). Keine Last-Penalty mehr —
        Verteilung gleich-cost Backends laeuft ueber Round-Robin im Skill.
        """
        return self.cost

    def _log_unreachable(self, reason: str = "") -> None:
        """Throttled-Logging fuer 'nicht erreichbar'-Zustaende.

        WARNING beim ersten Mal und alle 5 Minuten erneut, dazwischen DEBUG.
        Verhindert Log-Flut wenn Polling/check_availability oft laeuft.
        """
        msg = f"{self.name} nicht erreichbar: {self.api_url}"
        if reason:
            msg += f" ({reason})"
        now = time.time()
        if now - self._last_unreachable_warn_ts >= self._UNREACHABLE_THROTTLE_SEC:
            logger.warning(msg)
            self._last_unreachable_warn_ts = now
        else:
            logger.debug(msg)

    def _mark_available(self, info: str = "") -> None:
        """Markiert Backend als verfuegbar; loggt INFO bei Recovery."""
        _was_recovery = (self._was_available is False)
        if _was_recovery:
            logger.info("%s wieder erreichbar%s", self.name, f": {info}" if info else "")
        elif self._was_available is None:
            # Erster erfolgreicher Check — kurze INFO, kein "wieder"
            logger.info("%s erreichbar%s", self.name, f": {info}" if info else "")
        self._was_available = True
        # Throttle-Counter zuruecksetzen, damit naechstes Down-Event sofort warnt
        self._last_unreachable_warn_ts = 0.0
        self.available = True
        # Recovery: channel_health sofort neu pollen, damit GPU-Task-Routing
        # (find_channel/is_healthy) den frischen Status sieht und nicht erst
        # bis zum naechsten 30s-Poll wartet. Sonst schlaegt MultiSwap/Faceswap
        # direkt nach Recovery noch fehl, obwohl das Backend wieder online ist.
        if _was_recovery:
            try:
                from app.core.channel_health import get_monitor as _ch_monitor
                _ch_monitor().force_poll()
            except Exception as _ch_err:
                logger.debug("channel_health force_poll fehlgeschlagen: %s", _ch_err)

    def _mark_unavailable(self, reason: str = "") -> None:
        """Markiert Backend als unavailable; throttled-Logging."""
        self._log_unreachable(reason)
        self._was_available = False
        self.available = False

    @abstractmethod
    def check_availability(self) -> bool:
        """Prueft ob die API erreichbar ist. Setzt self.available."""
        pass

    def generate(self, prompt: str, negative_prompt: str, params: Dict[str, Any]) -> List[bytes]:
        """Generiert Bilder mit automatischem Job-Tracking fuer Load-Balancing.

        Wendet zentrales Downscale-Postprocessing an, wenn ``params`` einen
        ``image_use_case`` enthaelt (item / location). Caller, die volle
        Aufloesung brauchen (Outfit, Avatar), setzen den Key nicht.
        """
        with self._jobs_lock:
            self._active_jobs += 1
        try:
            result = self._generate(prompt, negative_prompt, params)
        finally:
            with self._jobs_lock:
                self._active_jobs = max(0, self._active_jobs - 1)

        # Sentinel-String "NO_NEW_IMAGE" oder Fehler-Listen unveraendert weitergeben.
        use_case = (params or {}).get("image_use_case") or ""
        if use_case and isinstance(result, list) and result:
            try:
                from app.core.image_postprocess import downscale_bytes
                result = [
                    downscale_bytes(img, use_case) if isinstance(img, (bytes, bytearray)) else img
                    for img in result
                ]
            except Exception as _exc:
                logger.warning("Downscale-Postprocess fehlgeschlagen: %s", _exc)
        return result

    @abstractmethod
    def _generate(self, prompt: str, negative_prompt: str, params: Dict[str, Any]) -> List[bytes]:
        """
        Generiert Bilder und gibt sie als Liste von PNG-Bytes zurueck.

        Args:
            prompt: Der fertige Prompt (inkl. Prefix/Suffix)
            negative_prompt: Negative Prompt
            params: Zusaetzliche Parameter (guidance_scale, steps, etc.)

        Returns:
            Liste von PNG-Bildern als bytes
        """
        pass

    def __repr__(self):
        status = "verfuegbar" if self.available else "nicht verfuegbar"
        return f"{self.name} ({self.api_type}, cost={self.cost}, {status})"


class A1111Backend(ImageBackend):
    """
    Backend fuer Stable Diffusion WebUI (A1111/Forge).
    API: POST {url}/sdapi/v1/txt2img
    """

    def __init__(self, name: str, api_url: str, cost: float, env_prefix: str):
        super().__init__(name, api_url, cost, api_type="a1111", env_prefix=env_prefix)

        self.guidance_scale = float(os.environ.get(f"{env_prefix}GUIDANCE_SCALE", "7.5"))
        self.num_inference_steps = int(os.environ.get(f"{env_prefix}NUM_INFERENCE_STEPS", "50"))
        self.checkpoint = os.environ.get(f"{env_prefix}CHECKPOINT", "").strip()
        self.sampling_method = os.environ.get(f"{env_prefix}SAMPLING_METHOD", "").strip()
        self.schedule_type = os.environ.get(f"{env_prefix}SCHEDULE_TYPE", "").strip()
        self.width = int(os.environ.get(f"{env_prefix}WIDTH", "1024"))
        self.height = int(os.environ.get(f"{env_prefix}HEIGHT", "1024"))

    def check_availability(self) -> bool:
        """Testet Verbindung via GET /info."""
        try:
            resp = requests.get(f"{self.api_url}/info", timeout=5)
            self.available = resp.status_code == 200
            if self.available:
                logger.info(f"{self.name} erreichbar: {self.api_url}")
            else:
                logger.warning(f"{self.name} antwortet mit Status {resp.status_code}")
        except requests.exceptions.ConnectionError:
            logger.warning(f"{self.name} nicht erreichbar: {self.api_url}")
            self.available = False
        except Exception as e:
            logger.error(f"{self.name} Fehler: {e}")
            self.available = False
        return self.available

    def _generate(self, prompt: str, negative_prompt: str, params: Dict[str, Any]) -> List[bytes]:
        """Generiert Bilder ueber A1111 txt2img API."""
        guidance = params.get("guidance_scale", self.guidance_scale)
        steps = params.get("num_inference_steps", self.num_inference_steps)
        checkpoint = params.get("checkpoint", self.checkpoint)
        sampler = params.get("sampling_method", self.sampling_method)
        scheduler = params.get("schedule_type", self.schedule_type)
        width = int(params.get("width", self.width))
        height = int(params.get("height", self.height))

        payload = {
            "prompt": prompt,
            "guidance_scale": guidance,
            "steps": steps,
            "width": width,
            "height": height,
        }

        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        if checkpoint:
            payload["sd_model_checkpoint"] = checkpoint
        if sampler:
            payload["sampler_name"] = sampler
        if scheduler:
            payload["scheduler"] = scheduler

        logger.info(f"{self.name} API-Request: {self.api_url}/sdapi/v1/txt2img")
        logger.info(f"Prompt ({len(prompt)} chars): {prompt}")
        if negative_prompt:
            logger.info(f"Negative-Prompt: {negative_prompt}")
        logger.info(f"Params: guidance={guidance}, steps={steps}, size={width}x{height}")
        if checkpoint:
            logger.info(f"Checkpoint: {checkpoint}")
        if sampler:
            logger.info(f"Sampler: {sampler}")
        if scheduler:
            logger.info(f"Scheduler: {scheduler}")
        logger.debug("Timeout: 600s")

        try:
            resp = requests.post(
                f"{self.api_url}/sdapi/v1/txt2img",
                json=payload,
                timeout=600
            )
        except requests.exceptions.Timeout:
            logger.error(f"{self.name} Timeout nach 600s")
            raise
        except requests.exceptions.ConnectionError as e:
            logger.error(f"{self.name} Verbindungsfehler: Kann API nicht erreichen ({self.api_url}): {str(e)[:200]}")
            raise
        except Exception as e:
            logger.error(f"{self.name} Request-Fehler: {str(e)[:200]}")
            raise

        logger.info(f"{self.name} Response: HTTP {resp.status_code}, {len(resp.content)} bytes")
        logger.debug(f"Content-Type: {resp.headers.get('content-type', 'N/A')}")

        if resp.status_code != 200:
            error_body = resp.text[:500] if hasattr(resp, 'text') else 'N/A'
            logger.error(f"HTTP {resp.status_code}: {error_body}")
            resp.raise_for_status()

        try:
            result = resp.json()
            logger.debug("JSON geparsed")
        except Exception as e:
            logger.error(f"JSON-Parsing fehlgeschlagen: {str(e)}")
            logger.debug(f"Response: {resp.text[:500]}")
            raise

        images_b64 = result.get("images", [])
        logger.info(f"Gefunden: {len(images_b64)} base64-Bild(er) in Response")

        images = []
        for idx, img_b64 in enumerate(images_b64, 1):
            try:
                img_bytes = base64.b64decode(img_b64)
                images.append(img_bytes)
                logger.debug(f"Bild {idx} dekodiert: {len(img_bytes)} bytes")
            except Exception as e:
                logger.error(f"Bild {idx} Dekodierung fehlgeschlagen: {str(e)[:100]}")
                continue

        if images:
            logger.info(f"{self.name}: {len(images)} Bild(er) erfolgreich generiert")
        else:
            logger.warning(f"{self.name}: Keine Bilder dekodiert")
            logger.debug(f"Response-Struktur: Top-Level Keys: {list(result.keys())}")
            logger.debug(f"'images' Key vorhanden: {'images' in result}, Laenge: {len(images_b64)}")
            if result.get("info"):
                logger.debug(f"Info: {str(result['info'])[:200]}")
            logger.debug(f"Tipp: Prüfe ob Stable Diffusion Model geladen ist")
            logger.debug(f"Komplette Response (erste 500 chars): {str(result)[:500]}")

        return images


class MammouthBackend(ImageBackend):
    """
    Backend fuer Mammouth AI (OpenAI-kompatible API mit Bild-Modellen).
    API: POST {url}/chat/completions
    Response: choices[0].message.images[].image_url.url (data:image/png;base64,...)
    """

    def __init__(self, name: str, api_url: str, cost: float, env_prefix: str,
                 api_key: str = "", model: str = "gemini-2.5-flash-image"):
        super().__init__(name, api_url, cost, api_type="mammouth", env_prefix=env_prefix)

        self.api_key = api_key or os.environ.get(f"{env_prefix}API_KEY", "")
        self.model = model or os.environ.get(f"{env_prefix}MODEL", "gemini-2.5-flash-image")

    def _headers(self) -> Dict[str, str]:
        """Erstellt Auth-Headers."""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def check_availability(self) -> bool:
        """Testet Verbindung via GET /models und prueft ob das Modell existiert."""
        try:
            resp = requests.get(
                f"{self.api_url}/models",
                headers=self._headers(),
                timeout=10
            )
            if resp.status_code != 200:
                logger.warning(f"{self.name} antwortet mit Status {resp.status_code}")
                self.available = False
                return False

            # Pruefe ob das konfigurierte Modell verfuegbar ist
            try:
                models_data = resp.json()
                model_ids = [m.get("id", "") for m in models_data.get("data", [])]
                if self.model in model_ids:
                    logger.info(f"{self.name} erreichbar, Modell '{self.model}' verfuegbar")
                    self.available = True
                else:
                    # Suche nach aehnlichen Modellnamen
                    image_models = [m for m in model_ids if "image" in m.lower()]
                    logger.warning(f"{self.name}: Modell '{self.model}' nicht gefunden!")
                    if image_models:
                        logger.info(f"Verfuegbare Image-Modelle: {', '.join(image_models)}")
                    self.available = False
            except Exception:
                # JSON-Parsing fehlgeschlagen, API erreichbar aber Modell unbekannt
                logger.info(f"{self.name} erreichbar: {self.api_url}")
                self.available = True

        except requests.exceptions.ConnectionError:
            logger.warning(f"{self.name} nicht erreichbar: {self.api_url}")
            self.available = False
        except Exception as e:
            logger.error(f"{self.name} Fehler: {e}")
            self.available = False
        return self.available

    def _generate(self, prompt: str, negative_prompt: str, params: Dict[str, Any]) -> List[bytes]:
        """Generiert Bilder ueber Mammouth chat/completions API."""
        # Prompt fuer Image-Modell aufbauen
        full_prompt = f"Generate an image: {prompt}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "user", "content": full_prompt}
            ]
        }

        logger.info(f"{self.name} API-Request: {self.api_url}/chat/completions")
        logger.info(f"Model: {self.model}, Prompt ({len(full_prompt)} chars): {full_prompt}")
        logger.debug(f"Bearer Token {'gesetzt' if self.api_key else 'FEHLT'}, Timeout: 120s")

        try:
            resp = requests.post(
                f"{self.api_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=120
            )
        except requests.exceptions.Timeout as e:
            logger.error(f"{self.name} Timeout nach 120s")
            raise
        except requests.exceptions.ConnectionError as e:
            logger.error(f"{self.name} Verbindungsfehler: {self.api_url}: {str(e)[:200]}")
            raise
        except Exception as e:
            logger.error(f"{self.name} Unerwarteter Fehler: {type(e).__name__}: {str(e)[:200]}")
            raise

        # Response-Status pruefen
        logger.info(f"{self.name} Response: HTTP {resp.status_code}, {len(resp.content)} bytes")
        logger.debug(f"Content-Type: {resp.headers.get('content-type', 'N/A')}")

        # Bei Fehler: Response-Body loggen fuer Debugging
        if resp.status_code != 200:
            error_body = ""
            try:
                error_body = resp.text[:500]
            except Exception:
                pass
            logger.error(f"{self.name} HTTP {resp.status_code}: {error_body}")
            resp.raise_for_status()

        # JSON Response parsen
        try:
            result = resp.json()
            logger.debug("JSON erfolgreich geparsed")
        except Exception as e:
            logger.error(f"JSON-Parsing fehlgeschlagen: {str(e)}")
            logger.debug(f"Response (500 chars): {resp.text[:500]}")
            raise

        images = []

        # Parse Response: choices[0].message.images[].image_url.url
        logger.debug("Analysiere Response-Struktur...")
        choices = result.get("choices", [])
        if not choices:
            logger.warning(f"{self.name}: Keine choices in Response, Keys: {list(result.keys())}")
            return images

        message = choices[0].get("message", {})
        image_list = message.get("images", [])

        # Falls keine images-Liste: Content auf eingebettete base64-Bilder pruefen
        if not image_list:
            content = message.get("content", "")
            if content:
                logger.info(f"{self.name}: Keine 'images' in message, pruefe Content ({len(content)} chars)")
                b64_matches = re.findall(r"data:image/[^;]+;base64,([A-Za-z0-9+/=]+)", content)
                for b64_data in b64_matches:
                    try:
                        images.append(base64.b64decode(b64_data))
                    except Exception:
                        continue
                if images:
                    logger.info(f"{self.name}: {len(images)} Bild(er) aus Content extrahiert")
                    return images

        for img_entry in image_list:
            image_url = img_entry.get("image_url", {})
            url = image_url.get("url", "") if isinstance(image_url, dict) else str(image_url)

            if not url:
                continue

            # Extrahiere base64 aus data:image/png;base64,...
            b64_match = re.match(r"data:image/[^;]+;base64,(.+)", url)
            if b64_match:
                try:
                    img_bytes = base64.b64decode(b64_match.group(1))
                    images.append(img_bytes)
                    logger.debug(f"Bild {len(images)} dekodiert: {len(img_bytes)} bytes")
                except Exception as e:
                    logger.error(f"Base64-Dekodierung fehlgeschlagen: {str(e)[:100]}")
                    continue

        if images:
            logger.info(f"{self.name}: {len(images)} Bild(er) erfolgreich generiert")
            for idx, img in enumerate(images, 1):
                logger.debug(f"Bild {idx}: {len(img)} bytes")

        if not images:
            logger.warning(f"{self.name}: Keine Bilder in Response gefunden")
            logger.debug(f"Response-Struktur: Top-Level Keys: {list(result.keys())}, Choices: {bool(choices)}")
            if choices:
                logger.debug(f"Choices[0] Keys: {list(choices[0].keys())}, Message Keys: {list(message.keys())}")
                logger.debug(f"Images-Liste: vorhanden={bool(image_list)}, Laenge={len(image_list)}")
                if message.get("content"):
                    content_preview = str(message['content'])[:300]
                    logger.debug(f"Content-Preview (300 chars): {content_preview}")
                    if any(err in content_preview.lower() for err in ['error', 'fail', 'exception', 'invalid']):
                        logger.warning("Content enthaelt moeglicherweise Fehlermeldung!")
            logger.debug(f"Komplette Response (erste 500 chars): {str(result)[:500]}")

        return images


class ComfyUIBackend(ImageBackend):
    """
    Backend fuer ComfyUI.
    API: POST /prompt → GET /history/{id} → GET /view?filename=...
    Nutzt einen gespeicherten Workflow (API-Format) der zur Laufzeit parametrisiert wird.
    """

    def __init__(self, name: str, api_url: str, cost: float, env_prefix: str):
        super().__init__(name, api_url, cost, api_type="comfyui", env_prefix=env_prefix)
        # Lock: schuetzt die Slot-Upload → Queue-Submit Sequenz,
        # damit parallele Generierungen die festen Referenz-Slots nicht ueberschreiben.
        self._slot_lock = __import__("threading").Lock()
        # Letzte (workflow_file, model_name) Kombination, die auf diesem Backend
        # geladen wurde. Wenn der naechste Run dieselbe Kombination nutzt,
        # ueberspringen wir /free — das Modell ist schon im VRAM und kann
        # wiederverwendet werden (spart 1-3s Disk-Load pro identischem Run).
        self._last_loaded_signature: str = ""

        self.checkpoint = os.environ.get(f"{env_prefix}CHECKPOINT", "").strip()
        self.sampler = os.environ.get(f"{env_prefix}SAMPLER", "euler").strip()
        self.scheduler = os.environ.get(f"{env_prefix}SCHEDULER", "normal").strip()
        self.guidance_scale = float(os.environ.get(f"{env_prefix}GUIDANCE_SCALE", "7.0"))
        self.num_inference_steps = int(os.environ.get(f"{env_prefix}NUM_INFERENCE_STEPS", "20"))
        self.width = int(os.environ.get(f"{env_prefix}WIDTH", "1024"))
        self.height = int(os.environ.get(f"{env_prefix}HEIGHT", "1024"))
        self.poll_interval = float(os.environ.get(f"{env_prefix}POLL_INTERVAL", "1.0"))
        self.max_wait = int(os.environ.get(f"{env_prefix}MAX_WAIT", "600"))

    def _load_workflow(self, workflow_override: str = "") -> Dict:
        """Laedt einen Custom-Workflow aus einer JSON-Datei.

        Returns:
            workflow_dict

        Raises:
            FileNotFoundError: Wenn keine Workflow-Datei angegeben oder gefunden wurde
        """
        wf_path = workflow_override
        if not wf_path:
            raise FileNotFoundError(
                f"{self.name}: Kein Workflow konfiguriert. "
                f"Bitte einen ComfyUI-Workflow (COMFY_IMAGEGEN_*) in der .env definieren."
            )
        if not os.path.exists(wf_path):
            raise FileNotFoundError(
                f"{self.name}: Workflow-Datei nicht gefunden: {wf_path}"
            )
        with open(wf_path) as f:
            wf = json.load(f)
        logger.info(f"{self.name}: Custom-Workflow geladen: {wf_path}")
        return wf

    def _find_node_by_class(self, workflow: Dict, class_type: str) -> Optional[str]:
        """Findet die erste Node-ID mit dem gegebenen class_type."""
        for node_id, node in workflow.items():
            if node.get("class_type") == class_type:
                return node_id
        return None

    def _find_node_by_title(self, workflow: Dict, title: str) -> Optional[str]:
        """Findet eine Node-ID anhand ihres _meta.title (case-insensitive)."""
        title_lower = title.lower()
        for node_id, node in workflow.items():
            node_title = node.get("_meta", {}).get("title", "").lower()
            if node_title == title_lower:
                return node_id
        return None

    def _find_positive_clip(self, workflow: Dict) -> Optional[str]:
        """Findet die Positive-CLIP-Node (anhand _meta.title oder erste CLIPTextEncode)."""
        clip_nodes = []
        for node_id, node in workflow.items():
            if node.get("class_type") == "CLIPTextEncode":
                meta_title = node.get("_meta", {}).get("title", "").lower()
                if "positive" in meta_title or "prompt" in meta_title:
                    return node_id
                clip_nodes.append(node_id)
        return clip_nodes[0] if clip_nodes else None

    def _find_negative_clip(self, workflow: Dict) -> Optional[str]:
        """Findet die Negative-CLIP-Node."""
        clip_nodes = []
        for node_id, node in workflow.items():
            if node.get("class_type") == "CLIPTextEncode":
                meta_title = node.get("_meta", {}).get("title", "").lower()
                if "negative" in meta_title:
                    return node_id
                clip_nodes.append(node_id)
        # Zweite CLIPTextEncode = Negative (wenn mindestens 2 vorhanden)
        return clip_nodes[1] if len(clip_nodes) > 1 else None

    # Feste Dateinamen fuer Referenzbild-Slots auf dem ComfyUI-Server.
    # Da immer nur eine Generierung gleichzeitig laeuft (Queue/GPU-Lock),
    # werden diese Slots vor jedem Run mit Placeholder-Bildern ueberschrieben,
    # damit keine Reste aus vorherigen Generierungen im Workflow landen.
    _REF_SLOT_NAMES = [
        "_slot_ref_1.png",
        "_slot_ref_2.png",
        "_slot_ref_3.png",
        "_slot_ref_4.png",
        "_slot_target.png",
    ]

    def _upload_image(self, file_path: str, slot_name: Optional[str] = None) -> Optional[str]:
        """Laedt ein Bild zu ComfyUI hoch (fuer LoadImage-Nodes).

        Args:
            file_path: Lokaler Pfad zur Bilddatei.
            slot_name: Slot-Identifier. Es wird ein eindeutiger Dateiname pro
                       Upload erzeugt (Slot-Praefix + Microsekunden-Timestamp),
                       sonst cached ComfyUI's Prompt-Executor das LoadImage-
                       Tensor anhand des Filenames und liefert beim naechsten
                       Run das alte Bild zurueck (selbst wenn die Datei
                       ueberschrieben wurde).
                       Wenn None, wird der Original-Dateiname verwendet
                       (kollisionsfrei, da Source-Filenames eindeutig sind).

        Returns:
            Dateiname auf dem ComfyUI-Server, oder None bei Fehler.
        """
        from pathlib import Path
        path = Path(file_path)
        if not path.exists():
            logger.error(f"Upload: Datei nicht gefunden: {file_path}")
            return None
        if slot_name:
            # Slot-Praefix vom .png trennen und Timestamp einfuegen
            _stem, _, _ext = slot_name.rpartition(".")
            if not _stem:
                _stem, _ext = slot_name, "png"
            _us = int(time.time() * 1_000_000)
            upload_name = f"{_stem}_{_us}.{_ext}"
        else:
            upload_name = path.name
        try:
            with open(file_path, "rb") as f:
                files = {"image": (upload_name, f, "image/png")}
                data = {"subfolder": "", "type": "input", "overwrite": "true"}
                resp = requests.post(
                    f"{self.api_url}/upload/image",
                    files=files,
                    data=data,
                    timeout=30)
            if resp.status_code == 200:
                filename = resp.json().get("name", upload_name)
                logger.info(f"Upload OK: {filename}")
                return filename
            else:
                logger.error(f"Upload fehlgeschlagen: HTTP {resp.status_code}")
                return None
        except Exception as e:
            logger.error(f"Upload Fehler: {e}")
            return None

    def _upload_bytes(self, data_bytes: bytes, upload_name: str) -> Optional[str]:
        """Laedt rohe Bytes als Bild zu ComfyUI hoch.

        Returns:
            Dateiname auf dem ComfyUI-Server, oder None bei Fehler.
        """
        import io
        try:
            files = {"image": (upload_name, io.BytesIO(data_bytes), "image/png")}
            data = {"subfolder": "", "type": "input", "overwrite": "true"}
            resp = requests.post(
                f"{self.api_url}/upload/image",
                files=files,
                data=data,
                timeout=30)
            if resp.status_code == 200:
                filename = resp.json().get("name", upload_name)
                return filename
            else:
                logger.error(f"Upload-Bytes fehlgeschlagen: HTTP {resp.status_code}")
                return None
        except Exception as e:
            logger.error(f"Upload-Bytes Fehler: {e}")
            return None

    def _reset_reference_slots(self) -> None:
        """Ueberschreibt alle Referenzbild-Slots auf dem ComfyUI-Server mit
        einem 8x8 schwarzen Placeholder-PNG.

        Muss vor jedem Workflow-Run aufgerufen werden, damit keine Bilder aus
        vorherigen Generierungen in den LoadImage-Nodes landen.
        """
        placeholder = self._create_placeholder_png(size=8)
        for slot in self._REF_SLOT_NAMES:
            self._upload_bytes(placeholder, slot)
        logger.debug(f"Referenz-Slots zurueckgesetzt ({len(self._REF_SLOT_NAMES)} Slots)")

    def _free_comfyui_memory(self, signature: str = "") -> None:
        """Triggert ComfyUI's /free Endpoint: VRAM freigeben + Modelle unloaden.

        Wenn `signature` uebergeben wird und identisch zur letzten ist, wird
        /free uebersprungen — das Modell ist schon im VRAM und wird
        wiederverwendet (spart Disk-Load). Bei Wechsel oder ohne Signature
        wird /free ausgefuehrt und die neue Signature gemerkt.

        Damit teilen sich aufeinanderfolgende Generierungen auf demselben
        ComfyUI-Server nicht den VRAM-Cache (Flux + Z-Image + MultiSwap-Stack
        passt sonst nicht in 24GB).

        Per Env abschaltbar (`COMFY_FREE_MEMORY_BEFORE_RUN=false`), falls
        die wiederholten Modell-Loads zu viel I/O kosten.
        """
        if os.environ.get("COMFY_FREE_MEMORY_BEFORE_RUN", "true").strip().lower() in ("false", "0", "no"):
            logger.info(f"{self.name}: /free uebersprungen (COMFY_FREE_MEMORY_BEFORE_RUN=false)")
            return
        # Wenn wir eine Signature haben und sie matched -> /free skippen
        if signature and signature == self._last_loaded_signature:
            logger.info(f"{self.name}: /free uebersprungen — gleiche Signature: {signature}")
            return
        try:
            resp = requests.post(
                f"{self.api_url}/free",
                json={"unload_models": True, "free_memory": True},
                timeout=10)
            if resp.status_code == 200:
                logger.info(f"{self.name}: /free OK (alte Signature='{self._last_loaded_signature}', neue='{signature or '(keine)'}')")
                self._last_loaded_signature = signature
            else:
                logger.warning(f"{self.name}: /free HTTP {resp.status_code} — Body: {resp.text[:200]}")
                # Trotz HTTP-Fehler die Signature merken, damit bei demselben
                # naechsten Call nicht erneut versucht wird.
                self._last_loaded_signature = signature
        except Exception as e:
            # /free ist Best-Effort — wenn ComfyUI es nicht unterstuetzt oder
            # nicht erreichbar ist, einfach weitermachen.
            logger.warning(f"{self.name}: /free fehlgeschlagen ({type(e).__name__}: {e})")
            # Hier bewusst keine Signature-Aktualisierung: beim naechsten Call
            # wird's nochmal versucht.

    def _create_placeholder_png(self, size: int = 8) -> bytes:
        """Erzeugt ein minimales transparentes PNG (default 8x8)."""
        import struct
        import zlib

        def _chunk(chunk_type: bytes, data: bytes) -> bytes:
            raw = chunk_type + data
            return struct.pack(">I", len(data)) + raw + struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF)

        sig = b'\x89PNG\r\n\x1a\n'
        ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # NxN RGBA
        # Jede Zeile: filter-byte (0) + N * 4 Bytes (RGBA transparent)
        row = b'\x00' + b'\x00\x00\x00\x00' * size
        raw_rows = row * size
        idat = zlib.compress(raw_rows)
        return sig + _chunk(b'IHDR', ihdr) + _chunk(b'IDAT', idat) + _chunk(b'IEND', b'')

    def _remove_empty_load_images(self, workflow: Dict) -> None:
        """Entfernt leere LoadImage-Nodes und deren Verbindungen aus dem Workflow.

        Wenn ein LoadImage-Node kein echtes Referenzbild erhalten hat (image leer),
        wird er komplett entfernt. Alle Verbindungen anderer Nodes zu diesem Node
        werden ebenfalls entfernt. So verhaelt sich der Workflow wie beim
        ComfyUI-Bypass: der TextEncoder arbeitet ohne Referenzbild-Conditioning.
        """
        # Leere LoadImage-Nodes sammeln
        empty_node_ids = set()
        for node_id, node in workflow.items():
            if node.get("class_type") not in ("LoadImage", "LoadAndResizeImage"):
                continue
            img_val = node.get("inputs", {}).get("image", "")
            if not img_val:
                title = node.get("_meta", {}).get("title", node_id)
                empty_node_ids.add(node_id)
                logger.debug(f"Leerer LoadImage-Node '{title}' (Node {node_id}) wird entfernt")

        if not empty_node_ids:
            return

        # Verbindungen zu leeren Nodes aus allen anderen Nodes entfernen.
        # Crystools-Switches haben on_true UND on_false beide als required —
        # wenn nur einer gekappt wuerde, validiert ComfyUI den Switch nicht mehr.
        # Daher: vor dem Entfernen eines on_true-Wires den noch gueltigen
        # on_false-Wire kopieren (und umgekehrt), damit der Switch wohlgeformt
        # bleibt. Der boolean-Flag (vom Upload-Pfad gesetzt) entscheidet weiter
        # welche Quelle tatsaechlich genutzt wird.
        for node_id, node in workflow.items():
            if node_id in empty_node_ids:
                continue
            inputs = node.get("inputs", {})
            is_switch = node.get("class_type") == "Switch any [Crystools]"
            keys_to_remove = []
            for key, value in inputs.items():
                if not (isinstance(value, list) and len(value) == 2):
                    continue
                if str(value[0]) not in empty_node_ids:
                    continue
                title = node.get("_meta", {}).get("title", node_id)
                if is_switch and key in ("on_true", "on_false"):
                    other = "on_false" if key == "on_true" else "on_true"
                    other_val = inputs.get(other)
                    if (isinstance(other_val, list) and len(other_val) == 2
                            and str(other_val[0]) not in empty_node_ids):
                        # Rewire auf die noch gueltige Quelle (deepcopy nicht
                        # noetig — die Liste wird nicht mutiert).
                        inputs[key] = list(other_val)
                        logger.debug(
                            "Switch '%s' (Node %s): %s -> Node %s entfernt, rewired auf %s (Node %s)",
                            title, node_id, key, value[0], other, other_val[0])
                        # boolean so setzen, dass die jetzt KORREKTE Quelle
                        # genutzt wird (key=on_true gekappt → boolean=False).
                        if "boolean" in inputs:
                            inputs["boolean"] = (key == "on_false")
                        continue
                logger.debug(f"Verbindung '{key}' in '{title}' (Node {node_id}) -> Node {value[0]} entfernt")
                keys_to_remove.append(key)
            for key in keys_to_remove:
                del inputs[key]

        # Leere Nodes aus Workflow entfernen
        for node_id in empty_node_ids:
            del workflow[node_id]

        # IPAdapter-Nodes ohne image-Input bypassen:
        # Wenn ein IPAdapter-Node sein Referenzbild verloren hat, wird er
        # entfernt und sein model-Input direkt an die Konsumenten weitergereicht.
        bypass_node_ids = set()
        for node_id, node in workflow.items():
            class_type = node.get("class_type", "")
            if "IPAdapter" not in class_type or "Loader" in class_type:
                continue
            inputs = node.get("inputs", {})
            if "image" not in inputs:
                bypass_node_ids.add(node_id)
                title = node.get("_meta", {}).get("title", node_id)
                logger.debug(f"IPAdapter-Node '{title}' (Node {node_id}) hat kein image-Input, wird bypassed")

        for bypass_id in bypass_node_ids:
            bypass_node = workflow[bypass_id]
            # model-Input des IPAdapter-Nodes (der Upstream-Model-Anschluss)
            model_source = bypass_node.get("inputs", {}).get("model")
            # Alle Nodes umverdrahten, die auf den bypassed Node zeigen
            for node_id, node in workflow.items():
                if node_id == bypass_id:
                    continue
                inputs = node.get("inputs", {})
                for key, value in list(inputs.items()):
                    if isinstance(value, list) and len(value) == 2 and str(value[0]) == bypass_id:
                        if model_source and key == "model":
                            inputs[key] = model_source
                            logger.debug(f"Node {node_id} '{key}' umverdrahtet: {bypass_id} -> {model_source}")
                        else:
                            del inputs[key]
                            logger.debug(f"Node {node_id} '{key}' -> {bypass_id} entfernt (kein Bypass moeglich)")
            # Zugehoerige Nodes entfernen (IPAdapterLoader, CLIPVision) wenn nicht anderweitig genutzt
            for dep_key in ("ipadapter", "clip_vision"):
                dep_ref = bypass_node.get("inputs", {}).get(dep_key)
                if isinstance(dep_ref, list) and len(dep_ref) == 2:
                    dep_id = str(dep_ref[0])
                    # Pruefen ob noch andere Nodes diesen dep_id nutzen
                    still_used = False
                    for nid, n in workflow.items():
                        if nid == bypass_id or nid == dep_id:
                            continue
                        for v in n.get("inputs", {}).values():
                            if isinstance(v, list) and len(v) == 2 and str(v[0]) == dep_id:
                                still_used = True
                                break
                        if still_used:
                            break
                    if not still_used and dep_id in workflow:
                        title = workflow[dep_id].get("_meta", {}).get("title", dep_id)
                        logger.debug(f"Ungenutzer Node '{title}' (Node {dep_id}) wird entfernt")
                        del workflow[dep_id]
            del workflow[bypass_id]

    def check_availability(self) -> bool:
        """Testet Verbindung via GET /system_stats — Fallback /queue."""
        try:
            resp = requests.get(f"{self.api_url}/system_stats", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                version = data.get("system", {}).get("comfyui_version", "?")
                devices = data.get("devices", [])
                gpu_name = devices[0].get("name", "?") if devices else "keine GPU"
                self._mark_available(f"ComfyUI v{version} ({gpu_name})")
                return self.available
            # /system_stats nicht-200 (z.B. 500 durch kaputten Custom-Node):
            # Fallback /queue als Liveness-Check — leichter Endpoint, immer da.
            logger.debug(f"{self.name}: /system_stats HTTP {resp.status_code}, probiere /queue")
            try:
                q_resp = requests.get(f"{self.api_url}/queue", timeout=5)
                if q_resp.status_code == 200:
                    self._mark_available(f"via /queue, /system_stats wirft {resp.status_code}")
                    return self.available
            except Exception:
                pass
            self._mark_unavailable(f"HTTP {resp.status_code}")
        except requests.exceptions.ConnectionError:
            self._mark_unavailable()
        except Exception as e:
            logger.error(f"{self.name} Fehler: {e}")
            self.available = False
            self._was_available = False
        return self.available

    def _generate(self, prompt: str, negative_prompt: str, params: Dict[str, Any]) -> List[bytes]:
        """Generiert Bilder ueber ComfyUI Prompt-Queue API."""
        workflow = self._load_workflow(params.get("workflow_file", ""))

        # --- Workflow parametrisieren ---
        # Seed randomisieren, aber nur wenn kein input_seed Node vorhanden
        # (input_seed Workflows steuern den Seed ueber PrimitiveInt -> KSampler/RandomNoise Referenz)
        has_input_seed = self._find_node_by_title(workflow, "input_seed") is not None
        sampler_node = self._find_node_by_class(workflow, "KSampler")
        if sampler_node and not has_input_seed:
            workflow[sampler_node]["inputs"]["seed"] = random.randint(0, 2**63)
        # SamplerCustomAdvanced nutzt RandomNoise-Node fuer den Seed
        noise_node = self._find_node_by_class(workflow, "RandomNoise")
        if noise_node and not has_input_seed:
            workflow[noise_node]["inputs"]["noise_seed"] = random.randint(0, 2**63)

        # Input-Nodes per Title befuellen

        # Separated Prompt Workflow (character + pose + expression)
        _char_node = self._find_node_by_title(workflow, "input_prompt_character")
        _pose_prompt_node = self._find_node_by_title(workflow, "input_prompt_pose")
        _expr_node = self._find_node_by_title(workflow, "input_prompt_expression")

        if _char_node and _pose_prompt_node and _expr_node:
            workflow[_char_node]["inputs"]["text"] = params.get("character_prompt", prompt)
            # Fallback: wenn pose/expression leer, vollen Prompt in character_node
            # und neutrale Defaults fuer pose/expression (z.B. Location-Bilder)
            _pose = params.get("pose_prompt", "")
            _expr = params.get("expression_prompt", "")
            if not _pose and not _expr:
                from app.core.expression_pose_maps import DEFAULT_EXPRESSION, DEFAULT_POSE
                _pose = DEFAULT_POSE
                _expr = DEFAULT_EXPRESSION
            workflow[_pose_prompt_node]["inputs"]["text"] = _pose
            workflow[_expr_node]["inputs"]["text"] = _expr
            logger.info("Separated prompts: character=%d, pose=%d, expression=%d chars",
                        len(params.get("character_prompt", prompt)),
                        len(params.get("pose_prompt", "")),
                        len(params.get("expression_prompt", "")))
        else:
            # Positive Prompt (Format 1: PrimitiveString, Format 2: CLIPTextEncode)
            pos_input = self._find_node_by_title(workflow, "input - prompt - positive")
            if pos_input:
                workflow[pos_input]["inputs"]["value"] = prompt
            else:
                pos_clip = self._find_node_by_title(workflow, "input_prompt_positiv")
                if pos_clip:
                    # "value" fuer PrimitiveStringMultiline, "prompt" fuer TextEncodeQwenImageEditPlus, "text" fuer CLIPTextEncode
                    inputs = workflow[pos_clip]["inputs"]
                    prompt_field = "value" if "value" in inputs else ("prompt" if "prompt" in inputs else "text")
                    workflow[pos_clip]["inputs"][prompt_field] = prompt
                else:
                    pos_node = self._find_positive_clip(workflow)
                    if pos_node:
                        workflow[pos_node]["inputs"]["text"] = prompt

        # Negative Prompt (Format 1: PrimitiveString, Format 2: CLIPTextEncode/TextEncode)
        neg_input = self._find_node_by_title(workflow, "input - prompt - negative")
        if neg_input:
            workflow[neg_input]["inputs"]["value"] = negative_prompt or ""
        else:
            neg_clip = self._find_node_by_title(workflow, "input_prompt_negativ")
            if neg_clip:
                inputs = workflow[neg_clip]["inputs"]
                prompt_field = "value" if "value" in inputs else ("prompt" if "prompt" in inputs else "text")
                workflow[neg_clip]["inputs"][prompt_field] = negative_prompt or ""
            else:
                neg_node = self._find_negative_clip(workflow)
                if neg_node:
                    workflow[neg_node]["inputs"]["text"] = negative_prompt or ""

        # Width / Height — Prioritaetskette:
        #   1. caller-explizit via params['width']/['height'] (gewinnt)
        #   2. Workflow-eigene input_*-Nodes bleiben unangetastet (deren JSON-Wert wirkt)
        #   3. 1024 als finaler Fallback nur wenn Flux2Scheduler einen Literal-Wert braucht
        #
        # Items, Default-Items und Outfit-Pieces uebergeben kein width/height,
        # daher rendert ComfyUI in der Aufloesung die im Workflow-JSON steht.
        # World-Backgrounds, Map-Icons, Day/Night-Variants und Expression-
        # Variants uebergeben hingegen explizite Werte und ueberschreiben damit.
        explicit_w = params.get("width")
        explicit_h = params.get("height")
        has_explicit = explicit_w is not None and explicit_h is not None

        def _read_workflow_dim(dim_attr: str) -> Optional[int]:
            """Liest den effektiven Workflow-Wert aus den input_*-Nodes
            (input_width / input_height fuer dim_attr='value', input_size
            fuer dim_attr in ('width','height')). Gibt None zurueck wenn
            der Workflow keinen passenden Node hat."""
            attr_title = "input_width" if dim_attr == "width" else "input_height"
            n = self._find_node_by_title(workflow, attr_title)
            if n is not None:
                v = workflow[n].get("inputs", {}).get("value")
                if isinstance(v, int):
                    return v
            n = self._find_node_by_title(workflow, "input_size")
            if n is not None:
                v = workflow[n].get("inputs", {}).get(dim_attr)
                if isinstance(v, int):
                    return v
            n = self._find_node_by_title(workflow, f"input - {dim_attr}")
            if n is not None:
                v = workflow[n].get("inputs", {}).get("value")
                if isinstance(v, int):
                    return v
            return None

        if has_explicit:
            w = int(explicit_w)
            h = int(explicit_h)
            # Caller-Override: alle bekannten input_*-Patterns patchen, damit
            # der Effekt erreicht wird egal wie der Workflow strukturiert ist.
            for title in ("input - width", "input_width"):
                n = self._find_node_by_title(workflow, title)
                if n is not None:
                    workflow[n]["inputs"]["value"] = w
            for title in ("input - height", "input_height"):
                n = self._find_node_by_title(workflow, title)
                if n is not None:
                    workflow[n]["inputs"]["value"] = h
            n = self._find_node_by_title(workflow, "input_size")
            if n is not None:
                workflow[n]["inputs"]["width"] = w
                workflow[n]["inputs"]["height"] = h
            logger.info(f"caller-override size: {w}x{h}")
        else:
            # Kein Caller-Override → Workflow-Wert wirken lassen. Nur
            # auslesen fuer das Flux2Scheduler-Literal-Hardening unten.
            w = _read_workflow_dim("width") or 1024
            h = _read_workflow_dim("height") or 1024
            logger.info(f"workflow size: {w}x{h} (no caller override)")

        # Flux2Scheduler haerten: im Workflow-Design sind width/height oft an
        # ein LoadAndResizeImage (input_reference_image_background) verkabelt.
        # Wenn dieses Bild leer ist, wird der Node spaeter durch
        # _remove_empty_load_images entfernt — und Flux2Scheduler verliert
        # damit seine width/height-Verbindungen. Praeventiv: Literal-Werte
        # direkt setzen, dann ist die Verkabelung nicht mehr noetig.
        scheduler_node = self._find_node_by_class(workflow, "Flux2Scheduler")
        if scheduler_node:
            workflow[scheduler_node]["inputs"]["width"] = w
            workflow[scheduler_node]["inputs"]["height"] = h
            logger.debug("Flux2Scheduler width/height auf Literal-Werte gesetzt: %dx%d", w, h)

        # Model-Name in input_model Node setzen (PrimitiveString, CheckpointLoaderSimple oder UNETLoader)
        model_name = params.get("model", "")
        if model_name:
            model_node = self._find_node_by_title(workflow, "input_model")
            if model_node:
                class_type = workflow[model_node].get("class_type", "")
                if class_type == "UNETLoader":
                    workflow[model_node]["inputs"]["unet_name"] = model_name
                elif class_type == "CheckpointLoaderSimple":
                    workflow[model_node]["inputs"]["ckpt_name"] = model_name
                else:
                    workflow[model_node]["inputs"]["value"] = model_name

        # UNet-Modell in den passenden Loader-Node schreiben.
        # Workflows wie Qwen haben BEIDE Loader (input_safetensors + input_gguf)
        # plus einen safetensors_gguf Switch — der Code muss anhand der Datei-
        # Endung den richtigen Loader befuellen UND den Switch entsprechend
        # auf boolean=True (safetensors) bzw. False (gguf) setzen.
        # Workflows mit nur einem Loader (z.B. Flux only-safetensors) werden
        # weiterhin korrekt bedient — der jeweils nicht vorhandene Node
        # wird ignoriert.
        _model_for_loader = params.get("unet", "") or params.get("model", "")
        if _model_for_loader:
            _is_gguf = _model_for_loader.lower().endswith(".gguf")
            _safe_node = self._find_node_by_title(workflow, "input_safetensors")
            _gguf_node = self._find_node_by_title(workflow, "input_gguf")
            _unet_node = self._find_node_by_title(workflow, "input_unet")

            # Ziel-Loader anhand Endung waehlen, Fallback auf den vorhandenen.
            if _is_gguf and _gguf_node:
                _target_node = _gguf_node
                _target_kind = "gguf"
            elif (not _is_gguf) and _safe_node:
                _target_node = _safe_node
                _target_kind = "safetensors"
            elif _gguf_node and _is_gguf:
                _target_node = _gguf_node
                _target_kind = "gguf"
            else:
                _target_node = _safe_node or _unet_node or _gguf_node
                _target_kind = "safetensors" if _target_node == _safe_node else "gguf" if _target_node == _gguf_node else "unet"

            if _target_node:
                _cls = workflow[_target_node].get("class_type", "")
                _inputs = workflow[_target_node].get("inputs", {})
                # UnetLoaderGGUF + UNETLoader nutzen beide `unet_name`. Nur die
                # aelteren `LoaderGGUF`-Varianten (city96) haben `gguf_name`.
                _key = "gguf_name" if "gguf_name" in _inputs else "unet_name"
                _inputs[_key] = _model_for_loader
                logger.debug(f"Model -> {_target_kind} ({_cls}.{_key}): {_model_for_loader}")

            # input_unet (Legacy) auch befuellen, falls separat vorhanden
            if _unet_node and _unet_node != _target_node:
                workflow[_unet_node]["inputs"]["unet_name"] = _model_for_loader
                logger.debug(f"input_unet (Legacy) auch gesetzt: {_model_for_loader}")

            # safetensors_gguf Switch automatisch auf passenden Branch.
            # Verkabelung der aktuellen Workflows (qwen, flux2):
            #   on_true  -> input_gguf
            #   on_false -> input_safetensors
            # → boolean=True bei .gguf-Modellen, False bei .safetensors.
            #
            # Crystools "Switch any" fuehrt BEIDE Branches eager aus (nicht
            # lazy). Der ungenutzte Loader wuerde mit leerem oder falsch-Format
            # Modell scheitern und den KSampler crashen. Loesung: den ungenutzten
            # Branch auf den genutzten Branch umbiegen — dadurch hat der
            # ungenutzte Loader-Node keinen Consumer mehr und ComfyUI ignoriert
            # ihn beim Build des Execution-Graph.
            _gguf_switch = self._find_node_by_title(workflow, "safetensors_gguf")
            if _gguf_switch:
                _switch_inputs = workflow[_gguf_switch].get("inputs", {})
                if "boolean" in _switch_inputs:
                    _switch_inputs["boolean"] = _is_gguf
                    used_branch = "on_true" if _is_gguf else "on_false"
                    unused_branch = "on_false" if _is_gguf else "on_true"
                    used_ref = _switch_inputs.get(used_branch)
                    if isinstance(used_ref, list) and len(used_ref) == 2:
                        _switch_inputs[unused_branch] = list(used_ref)
                        logger.info(
                            f"safetensors_gguf Switch: boolean={_is_gguf} "
                            f"({'gguf' if _is_gguf else 'safetensors'}) — "
                            f"{unused_branch} rewired to {used_branch}={used_ref} "
                            f"(model={_model_for_loader})")
                    else:
                        logger.info(
                            f"safetensors_gguf Switch: boolean={_is_gguf} "
                            f"({'gguf' if _is_gguf else 'safetensors'}) — model={_model_for_loader}")

        # CLIP in input_clip Node setzen (z.B. Flux2 CLIPLoader)
        clip_name = params.get("clip_name", "")
        if clip_name:
            clip_node = self._find_node_by_title(workflow, "input_clip")
            if clip_node:
                workflow[clip_node]["inputs"]["clip_name"] = clip_name
                logger.debug(f"CLIP: {clip_name}")

        # Seed in input_seed Node setzen (PrimitiveInt)
        seed_value = params.get("seed")
        if seed_value is not None:
            seed_node = self._find_node_by_title(workflow, "input_seed")
            if seed_node:
                workflow[seed_node]["inputs"]["value"] = int(seed_value)
                logger.debug(f"Seed: {seed_value}")

        # Alle Referenz-Slots mit Placeholder ueberschreiben, damit keine
        # Bilder aus vorherigen Generierungen im Workflow landen.
        # Lock schuetzt die gesamte Sequenz: Slot-Reset → Upload → Queue-Submit.
        ref_images = params.get("reference_images", {})
        # Workflows koennen entweder pro Slot einen Node haben
        # ("input_reference_image_1/_2/...") oder einen einzelnen Node ohne
        # Suffix ("input_reference_image") oder einen benannten Slot
        # ("input_reference_image_background" bei Flux2). Alle erkennen,
        # damit der Slot-Reset auch bei Flux2 ohne Background sauber laeuft.
        _has_ref_nodes = bool(
            self._find_node_by_title(workflow, "input_reference_image_1")
            or self._find_node_by_title(workflow, "input_reference_image")
            or self._find_node_by_title(workflow, "input_reference_image_background"))
        self._slot_lock.acquire()
        # VRAM des ComfyUI-Servers freigeben — aber nur wenn das Modell-Setup
        # sich gegenueber dem letzten Run auf diesem Backend geaendert hat.
        # Signature: workflow_file + Hauptmodell (model/unet/gguf/checkpoint).
        _gen_sig = "|".join([
            params.get("workflow_file", "") or "",
            str(params.get("model", "") or params.get("unet", "")
                or params.get("gguf", "") or params.get("checkpoint", "") or ""),
        ])
        self._free_comfyui_memory(signature=_gen_sig)
        if ref_images or _has_ref_nodes:
            self._reset_reference_slots()

        # Referenzbilder hochladen und in LoadImage-Nodes einsetzen.
        # Jeder Slot bekommt einen festen Dateinamen (_slot_ref_N.png),
        # der beim naechsten Run wieder ueberschrieben wird.
        _activated_switches = set()  # Switch-Nodes die aktiviert wurden
        _slot_map = {}  # node_title -> slot_name Zuordnung
        import re as _re
        for i, (node_title, file_path) in enumerate(ref_images.items()):
            ref_node = self._find_node_by_title(workflow, node_title)
            # Fallback: wenn der Workflow nur einen einzelnen Slot ohne
            # Suffix hat (Flux.2 GGUF), input_reference_image_1 darauf mappen.
            if not ref_node and node_title == "input_reference_image_1":
                ref_node = self._find_node_by_title(workflow, "input_reference_image")
            if ref_node and file_path:
                # Slot-Name aus Node-Title ableiten damit Datei und Node
                # semantisch zusammenpassen: input_reference_image_N → _slot_ref_N.png
                _m = _re.search(r'_(\d+)$', node_title)
                if _m:
                    _n = int(_m.group(1))
                    if 1 <= _n <= len(self._REF_SLOT_NAMES):
                        slot_idx = _n - 1
                    else:
                        slot_idx = min(i, len(self._REF_SLOT_NAMES) - 1)
                else:
                    slot_idx = min(i, len(self._REF_SLOT_NAMES) - 1)
                slot_name = self._REF_SLOT_NAMES[slot_idx]
                _slot_map[node_title] = slot_name
                uploaded = self._upload_image(file_path, slot_name=slot_name)
                if uploaded:
                    workflow[ref_node]["inputs"]["image"] = uploaded
                    # Width/Height auf LoadAndResizeImage-Nodes setzen (z.B. Qwen-Workflow)
                    if "width" in workflow[ref_node]["inputs"]:
                        workflow[ref_node]["inputs"]["width"] = w
                        workflow[ref_node]["inputs"]["height"] = h
                    # Switch-Node aktivieren. Naming-Konventionen:
                    #   "<title>_on"        — Multi-Slot Workflows (Z-Image)
                    #   "<title>_use"       — Multi-Slot mit explizitem use-Switch
                    #   "input_reference_image_use" — Single-Slot (Flux.2 GGUF)
                    on_node = (
                        self._find_node_by_title(workflow, f"{node_title}_on")
                        or self._find_node_by_title(workflow, f"{node_title}_use")
                        or self._find_node_by_title(workflow, "input_reference_image_use"))
                    if on_node:
                        workflow[on_node]["inputs"]["boolean"] = True
                        _activated_switches.add(on_node)
                        logger.debug(f"Switch '{on_node}' (fuer {node_title}): true")
                    logger.debug(f"Ref-Image '{node_title}': {uploaded} (slot: {slot_name})")

        # Inaktive Switch-Nodes: boolean=false setzen.
        # Workflows sind so designt, dass on_false auf EmptyImage zeigt —
        # der Switch routet bei boolean=False sauber dorthin, ohne dass wir
        # die Verkabelung anfassen muessen.
        for node_id, node in workflow.items():
            if node.get("class_type") != "Switch any [Crystools]":
                continue
            if node_id in _activated_switches:
                continue
            inputs = node.get("inputs", {})
            if "boolean" in inputs:
                inputs["boolean"] = False
            logger.debug(f"Switch '{node.get('_meta', {}).get('title', node_id)}': inaktiv")

        # Boolean-Inputs in PrimitiveBoolean-Nodes setzen
        for node_title, value in params.get("boolean_inputs", {}).items():
            bool_node = self._find_node_by_title(workflow, node_title)
            if bool_node:
                workflow[bool_node]["inputs"]["value"] = bool(value)
                logger.debug(f"Boolean '{node_title}': {value}")

        # String-Inputs in PrimitiveString-Nodes setzen (z.B. _type Nodes)
        for node_title, value in params.get("string_inputs", {}).items():
            str_node = self._find_node_by_title(workflow, node_title)
            if str_node:
                workflow[str_node]["inputs"]["value"] = str(value)
                logger.debug(f"String '{node_title}': {value}")

        # LoRA-Inputs in input_loras Node setzen (z.B. "Lora Loader Stack (rgthree)")
        lora_inputs = params.get("lora_inputs", [])
        if lora_inputs:
            lora_node = self._find_node_by_title(workflow, "input_loras")
            if lora_node:
                for i, lora in enumerate(lora_inputs[:4], start=1):
                    lora_name = (lora.get("name") or "None").strip() or "None"
                    try:
                        lora_strength = float(lora.get("strength", 1.0))
                    except (TypeError, ValueError):
                        lora_strength = 1.0
                    workflow[lora_node]["inputs"][f"lora_0{i}"] = lora_name
                    workflow[lora_node]["inputs"][f"strength_0{i}"] = lora_strength
                    logger.debug(f"LoRA {i}: {lora_name} (strength={lora_strength})")

        # LoRA-Inputs in input_lora Node setzen (z.B. "Power Lora Loader (rgthree)")
        # Format: lora_1: {on: bool, lora: str, strength: float}
        if lora_inputs:
            power_lora_node = self._find_node_by_title(workflow, "input_lora")
            if power_lora_node:
                for i, lora in enumerate(lora_inputs[:4], start=1):
                    lora_name = (lora.get("name") or "").strip()
                    try:
                        lora_strength = float(lora.get("strength", 1.0))
                    except (TypeError, ValueError):
                        lora_strength = 1.0
                    lora_active = bool(lora_name and lora_name != "None")
                    workflow[power_lora_node]["inputs"][f"lora_{i}"] = {
                        "on": lora_active,
                        "lora": lora_name if lora_active else "",
                        "strength": lora_strength,
                    }
                    logger.debug(f"Power LoRA {i}: {lora_name} (on={lora_active}, strength={lora_strength})")

        # Flux.2 Placeholder-Workaround NUR fuer alte Workflows ohne
        # input_reference_image_use-Switch. Neue Workflows leiten ueber
        # den Switch direkt auf EmptyImage, der Workaround wuerde dort
        # ungewollt das alte Ref-Bild aus _slot_ref_1.png reaktivieren.
        _use_switch = self._find_node_by_title(workflow, "input_reference_image_use")
        if not _use_switch:
            _ref1_node = (
                self._find_node_by_title(workflow, "input_reference_image_1")
                or self._find_node_by_title(workflow, "input_reference_image"))
            if _ref1_node:
                _ref1_image = workflow[_ref1_node]["inputs"].get("image", "")
                if not _ref1_image:
                    workflow[_ref1_node]["inputs"]["image"] = self._REF_SLOT_NAMES[0]
                    # Width/Height auf Ziel-Dimensionen setzen (statt 8x8 vom Placeholder)
                    # damit Flux2Scheduler korrekte Dimensionen bekommt
                    if "width" in workflow[_ref1_node]["inputs"]:
                        workflow[_ref1_node]["inputs"]["width"] = w
                        workflow[_ref1_node]["inputs"]["height"] = h
                    logger.debug("Flux.2 Legacy-Placeholder gesetzt (%s, %dx%d)",
                                 self._REF_SLOT_NAMES[0], w, h)

        # Leere LoadImage-Nodes entfernen (inkl. Verbindungen)
        # Verhindert, dass Placeholder-Bilder als Referenz-Conditioning wirken
        # und z.B. ungewollte Personen in Location-Bildern erzeugen.
        self._remove_empty_load_images(workflow)

        wf_name = params.get("workflow_file", "")
        # Checkpoint/Model aus Workflow extrahieren (fuer Logging + Image Prompt Log)
        wf_checkpoint = "N/A"
        _model_node = self._find_node_by_title(workflow, "input_model")
        if _model_node:
            wf_checkpoint = workflow[_model_node]["inputs"].get("value", "?")
        if wf_checkpoint in ("N/A", "?", ""):
            ckpt_node = self._find_node_by_class(workflow, "CheckpointLoaderSimple")
            wf_checkpoint = workflow[ckpt_node]["inputs"].get("ckpt_name", "?") if ckpt_node else "N/A"
        if wf_checkpoint in ("N/A", "?", ""):
            unet_node = self._find_node_by_class(workflow, "UNETLoader")
            wf_checkpoint = workflow[unet_node]["inputs"].get("unet_name", "?") if unet_node else "N/A"
        if wf_checkpoint in ("N/A", "?", ""):
            unet_gguf_node = self._find_node_by_class(workflow, "UnetLoaderGGUF")
            wf_checkpoint = workflow[unet_gguf_node]["inputs"].get("unet_name", "?") if unet_gguf_node else "N/A"
        # Seed aus KSampler oder RandomNoise extrahieren
        if sampler_node:
            wf_seed = workflow[sampler_node]["inputs"].get("seed", "?")
        elif noise_node:
            wf_seed = workflow[noise_node]["inputs"].get("noise_seed", "?")
        else:
            wf_seed = "?"
        # KSampler-Parameter aus Workflow lesen (Fallback: SamplerCustomAdvanced-Kette)
        if sampler_node:
            wf_steps = workflow[sampler_node]["inputs"].get("steps", "?")
            wf_cfg = workflow[sampler_node]["inputs"].get("cfg", "?")
            wf_sampler_name = workflow[sampler_node]["inputs"].get("sampler_name", "?")
            wf_scheduler = workflow[sampler_node]["inputs"].get("scheduler", "?")
        else:
            # SamplerCustomAdvanced: Steps in Scheduler-Node, CFG in CFGGuider
            scheduler_node = self._find_node_by_class(workflow, "Flux2Scheduler")
            wf_steps = workflow[scheduler_node]["inputs"].get("steps", "?") if scheduler_node else "?"
            cfg_node = self._find_node_by_class(workflow, "CFGGuider")
            wf_cfg = workflow[cfg_node]["inputs"].get("cfg", "?") if cfg_node else "?"
            sampler_select = self._find_node_by_class(workflow, "KSamplerSelect")
            wf_sampler_name = workflow[sampler_select]["inputs"].get("sampler_name", "?") if sampler_select else "?"
            wf_scheduler = "flux2" if scheduler_node else "?"
        logger.info(f"{self.name} ComfyUI Workflow: {self.api_url}/prompt")
        logger.info(f"Workflow: {wf_name}, Checkpoint: {wf_checkpoint}, Size: {w}x{h}")
        logger.info(f"Seed: {wf_seed}, Steps: {wf_steps}, CFG: {wf_cfg}, Sampler: {wf_sampler_name}/{wf_scheduler}")
        logger.info(f"Prompt: {prompt}")
        if negative_prompt:
            logger.info(f"Negative: {negative_prompt}")
        for rt, rp in params.get("reference_images", {}).items():
            logger.debug(f"RefImage: {rt} -> {rp}")
        for bt, bv in params.get("boolean_inputs", {}).items():
            logger.debug(f"Boolean: {bt} = {bv}")
        for st, sv in params.get("string_inputs", {}).items():
            logger.debug(f"String: {st} = {sv}")
        self.last_used_checkpoint = wf_checkpoint

        # 1) Prompt in Queue senden + 2) Pollen + 3) Download — alles unter
        # gehaltenem _slot_lock. Sonst kann ein paralleler Task die Slot-
        # Dateien (_slot_ref_N.png) ueberschreiben, bevor ComfyUI sie bei
        # der Execution liest — ComfyUI liest sie NICHT beim /prompt-Submit.
        # Gleiche Logik wie run_faceswap_workflow (siehe dort den Comment).
        payload = {"prompt": workflow}
        try:
            try:
                resp = requests.post(f"{self.api_url}/prompt", json=payload, timeout=30)
            except requests.exceptions.ConnectionError as e:
                logger.error(f"{self.name} Verbindungsfehler: {str(e)[:200]}")
                raise
            except Exception as e:
                logger.error(f"{self.name} Request-Fehler: {str(e)[:200]}")
                raise

            if resp.status_code != 200:
                error_body = resp.text[:500] if hasattr(resp, 'text') else 'N/A'
                logger.error(f"{self.name} HTTP {resp.status_code}: {error_body}")
                resp.raise_for_status()

            result = resp.json()
            prompt_id = result.get("prompt_id", "")
            if not prompt_id:
                logger.error(f"{self.name}: Keine prompt_id in Response: {result}")
                return []

            logger.info(f"Prompt queued: {prompt_id}")

            # 2) Pollen bis fertig
            outputs = {}
            start_time = time.time()
            while time.time() - start_time < self.max_wait:
                time.sleep(self.poll_interval)
                try:
                    hist_resp = requests.get(f"{self.api_url}/history/{prompt_id}", timeout=10)
                    if hist_resp.status_code != 200:
                        continue
                    history = hist_resp.json()
                    if prompt_id not in history:
                        elapsed = int(time.time() - start_time)
                        if elapsed % 10 == 0:
                            logger.info(f"Warte auf Ergebnis... ({elapsed}s)")
                        continue

                    # Fertig!
                    outputs = history[prompt_id].get("outputs", {})
                    status = history[prompt_id].get("status", {})
                    if status.get("status_str") == "error":
                        msgs = status.get("messages", [])
                        err_detail = str(msgs)[:1000] if msgs else "Unbekannter Fehler"
                        logger.error(f"{self.name} Ausfuehrungsfehler: {err_detail}")
                        raise RuntimeError(f"ComfyUI Fehler: {err_detail}")

                    elapsed = round(time.time() - start_time, 1)
                    logger.info(f"Fertig nach {elapsed}s")
                    break
                except RuntimeError:
                    raise  # ComfyUI Ausfuehrungsfehler sofort weitergeben
                except Exception as e:
                    logger.warning(f"Poll-Fehler: {e}")
                    continue
            else:
                logger.error(f"{self.name}: Timeout nach {self.max_wait}s")
                return []

            # 3) Bilder und Videos aus Output-Nodes extrahieren
            # ComfyUI liefert Videos (AnimateDiff, VHS_VideoCombine) unter "gifs" statt "images"
            # Wenn ein "output_final" Node existiert, nur von diesem downloaden
            output_final_id = self._find_node_by_title(workflow, "output_final")
            if output_final_id and output_final_id in outputs:
                target_outputs = {output_final_id: outputs[output_final_id]}
            else:
                target_outputs = outputs

            images = []
            for node_id, node_output in target_outputs.items():
                output_items = node_output.get("images", []) + node_output.get("gifs", [])
                for img_info in output_items:
                    filename = img_info.get("filename", "")
                    subfolder = img_info.get("subfolder", "")
                    img_type = img_info.get("type", "output")
                    if not filename:
                        continue

                    view_params = {"filename": filename, "type": img_type}
                    if subfolder:
                        view_params["subfolder"] = subfolder

                    try:
                        img_resp = requests.get(
                            f"{self.api_url}/view",
                            params=view_params,
                            timeout=30
                        )
                        if img_resp.status_code == 200:
                            images.append(img_resp.content)
                            logger.debug(f"Bild {len(images)}: {filename} ({len(img_resp.content)} bytes)")
                        else:
                            logger.error(f"Bild-Download fehlgeschlagen: HTTP {img_resp.status_code}")
                    except Exception as e:
                        logger.error(f"Bild-Download Fehler: {str(e)[:200]}")

            if images:
                logger.info(f"{self.name}: {len(images)} Bild(er) erfolgreich generiert")
            else:
                logger.warning(f"{self.name}: Keine Bilder in Output gefunden (Duplikat/Cache?)")
                logger.debug(f"Outputs: {json.dumps(outputs, indent=2)[:500]}")
                # Leere Liste aber kein Fehler — Sentinel-String signalisiert "kein neues Bild"
                return "NO_NEW_IMAGE"

            return images
        finally:
            self._slot_lock.release()


    def run_faceswap_workflow(
        self,
        workflow_file: str,
        target_image_path: str,
        reference_image_path: str,
        gender: str = "no",
        node_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
        reference_image_paths: Optional[Dict[str, str]] = None) -> Optional[bytes]:
        """Fuehrt einen separaten ComfyUI FaceSwap/MultiSwap-Workflow aus.

        Der Workflow wird pro Pass einmal aufgerufen. Bei klassischem ReActor-
        FaceSwap wird ein einzelnes Referenzbild verwendet. MultiSwap kann mit
        `reference_image_paths` mehrere Slots in einem Pass befuellen.

        Args:
            workflow_file: Pfad zur FaceSwap-Workflow-JSON.
            target_image_path: Pfad zum Bild, in das geswapt wird.
            reference_image_path: Single-Slot Referenz-Gesicht (ReActor-Default).
                                  Bei reference_image_paths != None ignoriert.
            gender: Gender fuer detect_gender_input/source ("male", "female", "no").
            node_overrides: Dict von node_id -> {input_key: value} zum Ueberschreiben
                           von Workflow-Inputs (z.B. MultiSwap Mask-Flags).
            reference_image_paths: Dict node_title -> file_path fuer Multi-Slot-
                                   Workflows (z.B. {"input_reference_image_1": p1,
                                   "input_reference_image_2": p2}). Slots ohne
                                   Eintrag werden ueber den zugehoerigen
                                   <title>_use Switch-Node deaktiviert.

        Returns:
            Bild-Bytes des FaceSwap-Ergebnisses oder None bei Fehler.
        """
        import copy

        # Workflow laden
        try:
            with open(workflow_file, "r") as f:
                workflow = json.load(f)
        except Exception as e:
            logger.error(f"FaceSwap-Workflow laden fehlgeschlagen: {e}")
            return None

        workflow = copy.deepcopy(workflow)

        # Referenz-Slots zuruecksetzen (Placeholder), damit keine alten Bilder bleiben
        self._slot_lock.acquire()
        # VRAM des ComfyUI-Servers freigeben — aber nur wenn das MultiSwap/
        # FaceSwap-Modell-Setup sich vom letzten Run auf diesem Backend
        # unterscheidet. Bei aufeinanderfolgenden MultiSwaps mit gleichem UNet
        # (z.B. Yuki-Variant dann Kira-Variant) bleibt das Modell im VRAM.
        _fs_unet = os.environ.get("COMFY_MULTISWAP_UNET", "").strip() or os.environ.get("COMFY_FACESWAP_BACKEND", "").strip()
        _fs_sig = f"{workflow_file}|{_fs_unet}"
        self._free_comfyui_memory(signature=_fs_sig)
        self._reset_reference_slots()

        # Target-Bild hochladen und in input_target_image setzen
        target_node = self._find_node_by_title(workflow, "input_target_image")
        if not target_node:
            logger.error("FaceSwap-Workflow: Node 'input_target_image' nicht gefunden")
            return None

        uploaded_target = self._upload_image(target_image_path, slot_name=self._REF_SLOT_NAMES[4])
        if not uploaded_target:
            logger.error("FaceSwap: Target-Bild Upload fehlgeschlagen")
            return None
        workflow[target_node]["inputs"]["image"] = uploaded_target

        # Referenzbild(er) hochladen
        if reference_image_paths:
            # Multi-Slot Modus (z.B. MultiSwap-Workflow mit input_reference_image_1/_2)
            # Reihenfolge der Eintraege bestimmt Slot-Datei (slot_ref_1, slot_ref_2, ...)
            for slot_idx, (node_title, ref_path) in enumerate(reference_image_paths.items()):
                ref_node = self._find_node_by_title(workflow, node_title)
                if not ref_node:
                    logger.warning("FaceSwap: Node '%s' nicht im Workflow gefunden", node_title)
                    continue
                slot_name = self._REF_SLOT_NAMES[slot_idx] if slot_idx < len(self._REF_SLOT_NAMES) - 1 else self._REF_SLOT_NAMES[0]
                uploaded_ref = self._upload_image(ref_path, slot_name=slot_name)
                if not uploaded_ref:
                    logger.error("FaceSwap: Referenzbild '%s' Upload fehlgeschlagen", ref_path)
                    return None
                workflow[ref_node]["inputs"]["image"] = uploaded_ref
                # Use-Switch fuer diesen Slot aktivieren (sofern vorhanden)
                use_node = self._find_node_by_title(workflow, f"{node_title}_use")
                if use_node:
                    workflow[use_node]["inputs"]["boolean"] = True
                logger.debug("FaceSwap Slot[%s]: %s", node_title, uploaded_ref)
            # Inaktive Switch-Nodes (z.B. input_reference_image_2_use bei nur 1 Ref)
            # auf False setzen — gleiche Logik wie in _generate.
            _active_titles = set(reference_image_paths.keys())
            for node_id, node in workflow.items():
                if node.get("class_type") != "Switch any [Crystools]":
                    continue
                title = (node.get("_meta") or {}).get("title", "")
                if not title.endswith("_use"):
                    continue
                base = title[:-len("_use")]
                if base in _active_titles:
                    continue
                inputs = node.get("inputs", {})
                if "boolean" in inputs:
                    inputs["boolean"] = False
                logger.debug("FaceSwap Switch '%s': inaktiv", title)
        else:
            # Single-Slot Modus (ReActor): input_reference_image_1
            ref_node = self._find_node_by_title(workflow, "input_reference_image_1")
            if ref_node:
                uploaded_ref = self._upload_image(reference_image_path, slot_name=self._REF_SLOT_NAMES[0])
                if uploaded_ref:
                    workflow[ref_node]["inputs"]["image"] = uploaded_ref
                    logger.debug(f"FaceSwap Ref: {uploaded_ref}")
                else:
                    logger.error("FaceSwap: Referenzbild Upload fehlgeschlagen")
                    return None

        # Gender auf allen ReActor-Nodes setzen
        for node_id, node in workflow.items():
            if node.get("class_type") == "ReActorFaceSwap":
                node["inputs"]["detect_gender_input"] = gender
                node["inputs"]["detect_gender_source"] = gender
                logger.debug(f"FaceSwap ReActor {node_id}: gender={gender}")

        # Node-Overrides anwenden (z.B. MultiSwap Mask-Flags)
        if node_overrides:
            for node_id, overrides in node_overrides.items():
                if node_id in workflow:
                    for key, value in overrides.items():
                        workflow[node_id]["inputs"][key] = value
                        logger.debug("Node-Override: %s.inputs.%s = %s", node_id, key, value)
                else:
                    logger.warning("Node-Override: Node '%s' nicht im Workflow", node_id)

        _wf_label = "MultiSwap" if "multiswap" in workflow_file.lower() else "FaceSwap"
        logger.info(f"{_wf_label}-Workflow starten: {workflow_file}")

        # WICHTIG: Das _slot_lock bleibt bis Polling+Download fertig gehalten.
        # Die Slot-Dateinamen (_slot_target.png, _slot_ref_N.png) sind fix —
        # wenn zwischen Submit und Node-Execution ein parallel laufender Task
        # die gleichen Files ueberschreibt, liest ComfyUI kaputte/halbe Dateien
        # (haeufig: "height and width must be > 0" an LoadAndResizeImage).
        # Submit + Poll (gleiche Logik wie _generate)
        payload = {"prompt": workflow}
        try:
            try:
                resp = requests.post(f"{self.api_url}/prompt", json=payload, timeout=30)
            except Exception as e:
                logger.error(f"{_wf_label} Submit-Fehler: {e}")
                return None

            if resp.status_code != 200:
                logger.error(f"{_wf_label} HTTP {resp.status_code}: {resp.text[:500]}")
                return None

            prompt_id = resp.json().get("prompt_id", "")
            if not prompt_id:
                logger.error(f"{_wf_label}: Keine prompt_id in Response")
                return None

            logger.info(f"{_wf_label} queued: {prompt_id}")

            start_time = time.time()
            outputs = {}
            while time.time() - start_time < self.max_wait:
                time.sleep(self.poll_interval)
                try:
                    hist_resp = requests.get(f"{self.api_url}/history/{prompt_id}", timeout=10)
                    if hist_resp.status_code != 200:
                        continue
                    history = hist_resp.json()
                    if prompt_id not in history:
                        continue

                    status = history[prompt_id].get("status", {})
                    if status.get("status_str") == "error":
                        msgs = status.get("messages", [])
                        # ComfyUI status.messages ist eine Liste [(event, data), ...].
                        # Den relevanten 'execution_error' Eintrag herausfischen und
                        # konkret loggen — sonst frisst das 500-Char-Truncate die
                        # eigentliche Exception (node_type/exception_message/traceback).
                        err_data = {}
                        for entry in msgs:
                            if isinstance(entry, (list, tuple)) and len(entry) == 2 and entry[0] == "execution_error":
                                err_data = entry[1] if isinstance(entry[1], dict) else {}
                                break
                        if err_data:
                            _node_id = err_data.get("node_id", "?")
                            _node_type = err_data.get("node_type", "?")
                            _exc_type = err_data.get("exception_type", "?")
                            _exc_msg = err_data.get("exception_message", "")
                            _tb = err_data.get("traceback", [])
                            _tb_tail = ""
                            if isinstance(_tb, list) and _tb:
                                _tb_tail = "\n".join(_tb[-3:])[:600]
                            logger.error(
                                "FaceSwap Fehler in Node %s (%s): %s: %s%s",
                                _node_id, _node_type, _exc_type, _exc_msg,
                                f"\n  Traceback (Tail):\n{_tb_tail}" if _tb_tail else "")
                        else:
                            logger.error(f"FaceSwap Fehler: {str(msgs)[:1500]}")
                        return None

                    outputs = history[prompt_id].get("outputs", {})
                    elapsed = round(time.time() - start_time, 1)
                    logger.info(f"{_wf_label} fertig nach {elapsed}s")
                    break
                except Exception as e:
                    logger.warning(f"{_wf_label} Poll-Fehler: {e}")
                    continue
            else:
                logger.error(f"{_wf_label} Timeout nach {self.max_wait}s")
                return None

            # Ergebnis aus output_final extrahieren
            output_final_id = self._find_node_by_title(workflow, "output_final")
            if output_final_id and output_final_id in outputs:
                target_outputs = {output_final_id: outputs[output_final_id]}
            else:
                target_outputs = outputs

            for node_id, node_output in target_outputs.items():
                for img_info in node_output.get("images", []):
                    filename = img_info.get("filename", "")
                    if not filename:
                        continue
                    view_params = {"filename": filename, "type": img_info.get("type", "output")}
                    subfolder = img_info.get("subfolder", "")
                    if subfolder:
                        view_params["subfolder"] = subfolder
                    try:
                        img_resp = requests.get(f"{self.api_url}/view", params=view_params, timeout=30)
                        if img_resp.status_code == 200:
                            logger.info(f"{_wf_label} Ergebnis: {filename} ({len(img_resp.content)} bytes)")
                            return img_resp.content
                    except Exception as e:
                        logger.error(f"{_wf_label} Download-Fehler: {e}")

            logger.warning(f"{_wf_label}: Kein Ergebnis-Bild gefunden")
            return None
        finally:
            # Slot-Lock erst jetzt freigeben — ab hier duerfen andere Tasks
            # die Slot-Files neu befuellen.
            self._slot_lock.release()


class CivitAIBackend(ImageBackend):
    """
    Backend fuer CivitAI Cloud API (asynchron mit Polling).
    API: POST /v1/consumer/jobs → Polling → Download blobUrl
    Modelle im AIR URN Format: urn:air:{ecosystem}:checkpoint:civitai:{modelId}@{versionId}
    """

    def __init__(self, name: str, api_url: str, cost: float, env_prefix: str,
                 api_key: str = "", model: str = ""):
        super().__init__(name, api_url, cost, api_type="civitai", env_prefix=env_prefix)

        self.api_key = api_key or os.environ.get(f"{env_prefix}API_KEY", "")
        self.model = model or os.environ.get(f"{env_prefix}MODEL", "")
        self.scheduler = os.environ.get(f"{env_prefix}SCHEDULER", "DPM2MKarras").strip()
        self.guidance_scale = float(os.environ.get(f"{env_prefix}GUIDANCE_SCALE", "7.0"))
        self.num_inference_steps = int(os.environ.get(f"{env_prefix}NUM_INFERENCE_STEPS", "40"))
        self.width = int(os.environ.get(f"{env_prefix}WIDTH", "1024"))
        self.height = int(os.environ.get(f"{env_prefix}HEIGHT", "1024"))
        self.clip_skip = int(os.environ.get(f"{env_prefix}CLIP_SKIP", "2"))
        self.poll_interval = float(os.environ.get(f"{env_prefix}POLL_INTERVAL", "5.0"))
        self.max_wait = int(os.environ.get(f"{env_prefix}MAX_WAIT", "300"))

    def _headers(self) -> Dict[str, str]:
        """Erstellt Auth-Headers fuer CivitAI API."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def check_availability(self) -> bool:
        """Prueft ob der API-Key gueltig ist und die API erreichbar."""
        if not self.api_key:
            logger.warning(f"{self.name}: Kein API_KEY konfiguriert")
            self.available = False
            return False
        if not self.model:
            logger.warning(f"{self.name}: Kein MODEL konfiguriert (AIR URN erforderlich)")
            self.available = False
            return False

        try:
            resp = requests.get(
                f"{self.api_url}/v1/consumer/jobs",
                params={"token": "__ping__"},
                headers=self._headers(),
                timeout=10)
            if resp.status_code in (401, 403):
                logger.warning(f"{self.name}: API-Key ungueltig (Status {resp.status_code})")
                self.available = False
                return False

            # Jeder andere Status (auch 400/404) bedeutet: API erreichbar, Key akzeptiert
            logger.info(f"{self.name} erreichbar (CivitAI, Modell: {self.model})")
            self.available = True
            return True
        except requests.ConnectionError:
            logger.warning(f"{self.name} nicht erreichbar")
            self.available = False
            return False
        except Exception as e:
            logger.error(f"{self.name} Fehler: {e}")
            self.available = False
            return False

    def _generate(self, prompt: str, negative_prompt: str, params: Dict[str, Any]) -> List[bytes]:
        """Generiert ein Bild ueber die CivitAI API (async mit Polling)."""
        # CivitAI erfordert AIR URN Format - lokale Dateinamen aus params ignorieren
        param_model = params.get("model", "")
        if param_model and param_model.startswith("urn:air:"):
            model = param_model
        else:
            model = self.model
            if param_model:
                logger.debug(f"{self.name}: Ignoriere nicht-AIR Modell aus params: {param_model}, nutze Backend-Modell: {model}")
        if not model:
            logger.error(f"{self.name}: Kein Modell konfiguriert")
            return []

        width = params.get("width") or self.width
        height = params.get("height") or self.height
        # CivitAI erfordert Dimensionen als Vielfache von 64
        width = round(width / 64) * 64 or 1024
        height = round(height / 64) * 64 or 1024

        # BaseModel aus AIR URN ableiten: urn:air:{ecosystem}:checkpoint:...
        # Ecosystem-Segment direkt extrahieren statt Heuristik — CivitAI nutzt
        # exakt diesen Token als baseModel-Tag.
        model_lower = model.lower()
        ecosystem = ""
        try:
            _parts = model_lower.split(":")
            # urn:air:{ecosystem}:... -> Index 2 ist ecosystem
            if len(_parts) >= 3 and _parts[0] == "urn" and _parts[1] == "air":
                ecosystem = _parts[2]
        except Exception:
            pass

        is_flux = ecosystem.startswith("flux") or ":flux" in model_lower
        is_turbo = "turbo" in ecosystem
        if ecosystem.startswith("zimage") or ecosystem.startswith("z_image"):
            base_model = "Z_Image_Turbo" if is_turbo else "Z_Image"
        elif ecosystem.startswith("flux2"):
            base_model = "Flux2"
        elif ecosystem.startswith("flux"):
            base_model = "Flux1"
        elif ecosystem == "sdxl":
            base_model = "SDXL"
        elif ecosystem.startswith("sd3"):
            base_model = "SD3"
        elif ecosystem == "pony":
            base_model = "Pony"
        elif ecosystem == "illustrious":
            base_model = "Illustrious"
        elif ecosystem == "noobai":
            base_model = "NoobAI"
        elif ecosystem == "hidream":
            base_model = "HiDream"
        else:
            base_model = "SD_1_5"

        # Flux-Modelle unterstuetzen nur "Euler" als Scheduler bei CivitAI
        scheduler = "Euler" if is_flux else self.scheduler

        gen_params = {
            "prompt": prompt,
            "scheduler": scheduler,
            "steps": params.get("num_inference_steps") or self.num_inference_steps,
            "width": width,
            "height": height,
            "seed": -1,
        }
        if is_flux:
            # FLUX: kein cfgScale, kein clipSkip, kein negativePrompt
            pass
        else:
            gen_params["negativePrompt"] = negative_prompt or self.negative_prompt
            gen_params["cfgScale"] = params.get("guidance_scale") or self.guidance_scale
            gen_params["clipSkip"] = self.clip_skip

        payload = {
            "$type": "textToImage",
            "baseModel": base_model,
            "model": model,
            "params": gen_params,
            "quantity": 1,
        }

        logger.info(f"{self.name}: Starte Generierung...")
        logger.info(f"Modell: {model} (baseModel={base_model}), Groesse: {width}x{height}, Steps: {gen_params['steps']}")
        logger.info(f"Prompt: {prompt[:120]}...")

        # 1. Job erstellen
        try:
            resp = requests.post(
                f"{self.api_url}/v1/consumer/jobs",
                json=payload,
                headers=self._headers(),
                timeout=30)
            if resp.status_code not in (200, 202):
                import json as _json
                error_msg = resp.text[:500] or '(leer)'
                logger.error(f"{self.name}: Job-Erstellung fehlgeschlagen (Status {resp.status_code})")
                logger.error(f"CivitAI Response: {error_msg}")
                logger.error(f"Gesendeter Payload: {_json.dumps(payload, indent=2)[:800]}")
                raise RuntimeError(f"{self.name}: Job fehlgeschlagen (HTTP {resp.status_code}): {error_msg[:200]}")

            data = resp.json()
            token = data.get("token", "")
            jobs = data.get("jobs", [])
            if not token or not jobs:
                logger.error(f"{self.name}: Unerwartete Response: {resp.text[:300]}")
                return []

            job_id = jobs[0].get("jobId", "unknown")
            logger.info(f"Job erstellt: {job_id}")
        except Exception as e:
            logger.error(f"{self.name}: Fehler beim Erstellen des Jobs: {e}")
            return []

        # 2. Polling — Result kann dict oder list sein
        #    Format: result=[{blobKey, available, seed, blobUrl?}] oder result={blobUrl, ...}
        #    available=true + blobUrl vorhanden = fertig
        start_time = time.time()
        blob_url = None

        while time.time() - start_time < self.max_wait:
            time.sleep(self.poll_interval)
            elapsed = int(time.time() - start_time)

            try:
                status_resp = requests.get(
                    f"{self.api_url}/v1/consumer/jobs",
                    params={"token": token},
                    headers=self._headers(),
                    timeout=15)
                if status_resp.status_code not in (200, 202):
                    logger.warning(f"Polling-Fehler (Status {status_resp.status_code}), versuche erneut... ({elapsed}s)")
                    continue

                status_data = status_resp.json()
                status_jobs = status_data.get("jobs", [])
                if not status_jobs:
                    continue

                job_result = status_jobs[0].get("result")
                if job_result is None:
                    continue

                # Result kann list oder dict sein
                result_item = job_result[0] if isinstance(job_result, list) and job_result else job_result
                if isinstance(result_item, dict):
                    available = result_item.get("available", False)
                    url = result_item.get("blobUrl", "")
                    if available and url:
                        blob_url = url
                        logger.info(f"Generierung abgeschlossen ({elapsed}s)")
                        break
                    elif not available:
                        # Noch in Bearbeitung
                        continue
                    elif url:
                        # available fehlt aber URL da
                        blob_url = url
                        logger.info(f"Generierung abgeschlossen ({elapsed}s)")
                        break

            except Exception as e:
                logger.warning(f"Polling-Fehler: {e} ({elapsed}s)")

        if not blob_url:
            logger.error(f"{self.name}: Timeout nach {self.max_wait}s (kein blobUrl)")
            return []

        # 3. Bild herunterladen
        try:
            img_resp = requests.get(blob_url, timeout=60)
            if img_resp.status_code != 200:
                logger.error(f"{self.name}: Bild-Download fehlgeschlagen (Status {img_resp.status_code})")
                return []

            image_bytes = img_resp.content
            logger.info(f"Bild heruntergeladen: {len(image_bytes)} bytes")
            return [image_bytes]
        except Exception as e:
            logger.error(f"{self.name}: Fehler beim Herunterladen: {e}")
            return []


class TogetherBackend(ImageBackend):
    """
    Backend fuer Together.ai Image Generation API.
    API: POST https://api.together.xyz/v1/images/generations
    Unterstuetzt FLUX.1, FLUX.2 und weitere Modelle.
    """

    def __init__(self, name: str, api_url: str, cost: float, env_prefix: str,
                 api_key: str = "", model: str = ""):
        super().__init__(name, api_url, cost, api_type="together", env_prefix=env_prefix)

        self.api_key = api_key or os.environ.get(f"{env_prefix}API_KEY", "")
        self.model = model or os.environ.get(f"{env_prefix}MODEL", "")
        # Komma-getrennte Modellliste fuer Auswahl im UI
        models_str = os.environ.get(f"{env_prefix}MODELS", "").strip()
        self.available_models: List[str] = [m.strip() for m in models_str.split(",") if m.strip()] if models_str else []
        self.num_inference_steps = int(os.environ.get(f"{env_prefix}NUM_INFERENCE_STEPS", "20"))
        self.width = int(os.environ.get(f"{env_prefix}WIDTH", "1024"))
        self.height = int(os.environ.get(f"{env_prefix}HEIGHT", "1024"))
        self.disable_safety = os.environ.get(
            f"{env_prefix}DISABLE_SAFETY", "false"
        ).strip().lower() in ("true", "1", "yes")
        self.timeout = int(os.environ.get(f"{env_prefix}TIMEOUT", "120"))

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def check_availability(self) -> bool:
        if not self.api_key:
            logger.warning(f"{self.name}: Kein API_KEY konfiguriert")
            self.available = False
            return False
        if not self.model:
            logger.warning(f"{self.name}: Kein MODEL konfiguriert")
            self.available = False
            return False

        try:
            resp = requests.get(
                f"{self.api_url}/v1/models",
                headers=self._headers(),
                timeout=10)
            if resp.status_code in (401, 403):
                logger.warning(f"{self.name}: API-Key ungueltig (Status {resp.status_code})")
                self.available = False
                return False

            # Bild-Modelle live aus API extrahieren
            if resp.status_code == 200:
                try:
                    body = resp.json()
                    # Together.ai liefert {"data": [...]}, ggf. auch direkt eine Liste
                    all_models = body.get("data", body) if isinstance(body, dict) else body
                    live_models = sorted(
                        [m["id"] for m in all_models if m.get("type") == "image"])
                    if live_models:
                        self.available_models = live_models
                        logger.info(f"{self.name}: {len(live_models)} Bild-Modelle verfuegbar")
                except (ValueError, KeyError):
                    pass  # JSON-Parsing fehlgeschlagen — available_models bleibt wie konfiguriert

            logger.info(f"{self.name} erreichbar (Together.ai, Modell: {self.model})")
            self.available = True
            return True
        except requests.ConnectionError:
            logger.warning(f"{self.name} nicht erreichbar")
            self.available = False
            return False
        except Exception as e:
            logger.error(f"{self.name} Fehler: {e}")
            self.available = False
            return False

    # Parameter-Keys die bei 400 einzeln entfernt werden koennen
    _OPTIONAL_KEYS = ("steps", "negative_prompt", "width", "height", "n")

    def _post_with_retry(self, payload: Dict[str, Any],
                         optional: Dict[str, Any]) -> requests.Response:
        """Sendet den Request und entfernt bei 400 den beanstandeten Parameter.

        Erkennt aus der Fehlermeldung welcher Parameter nicht unterstuetzt wird
        und wiederholt den Request ohne diesen. Maximal len(optional) Retries.
        """
        remaining = dict(optional)

        for _ in range(len(remaining) + 1):
            resp = requests.post(
                f"{self.api_url}/v1/images/generations",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout)
            if resp.status_code != 400:
                if resp.status_code != 200:
                    error_msg = resp.text[:500] or '(leer)'
                    logger.error(f"{self.name}: Generierung fehlgeschlagen (Status {resp.status_code})")
                    logger.error(f"Response: {error_msg}")
                    raise RuntimeError(f"{self.name}: HTTP {resp.status_code}: {error_msg[:200]}")
                return resp

            # 400: herausfinden welcher Parameter stoert
            error_text = resp.text.lower()
            removed = False
            for key in list(remaining.keys()):
                if key in error_text or (key in ("width", "height") and "dimension" in error_text):
                    logger.warning(f"{self.name}: Parameter '{key}' nicht unterstuetzt, wiederhole ohne")
                    payload.pop(key, None)
                    # width/height immer zusammen entfernen
                    if key in ("width", "height"):
                        payload.pop("width", None)
                        payload.pop("height", None)
                        remaining.pop("width", None)
                        remaining.pop("height", None)
                    else:
                        remaining.pop(key, None)
                    removed = True
                    break

            if not removed:
                # Unbekannter 400-Fehler — nicht wiederholbar
                error_msg = resp.text[:500] or '(leer)'
                logger.error(f"{self.name}: Generierung fehlgeschlagen (Status 400)")
                logger.error(f"Response: {error_msg}")
                raise RuntimeError(f"{self.name}: HTTP 400: {error_msg[:200]}")

        # Sollte nicht erreicht werden
        raise RuntimeError(f"{self.name}: Retry-Limit erreicht")

    def _generate(self, prompt: str, negative_prompt: str, params: Dict[str, Any]) -> List[bytes]:
        """Generiert ein Bild ueber die Together.ai API.

        Baut den Payload mit optionalen Parametern (steps, width/height, negative_prompt)
        auf. Falls die API einen Parameter ablehnt (400), wird der betreffende Parameter
        entfernt und der Request automatisch wiederholt.
        """
        model = params.get("model") or self.model
        width = params.get("width") or self.width
        height = params.get("height") or self.height
        steps = params.get("num_inference_steps") or self.num_inference_steps

        # Together.ai erfordert Dimensionen als Vielfache von 8
        width = round(width / 8) * 8 or 1024
        height = round(height / 8) * 8 or 1024

        payload: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "response_format": "b64_json",
        }

        if self.disable_safety:
            payload["disable_safety_checker"] = True

        # Seed aus params (optional)
        seed = params.get("seed")
        if seed and seed > 0:
            payload["seed"] = seed

        # Optionale Parameter — werden bei 400 einzeln entfernt und wiederholt
        optional_params: Dict[str, Any] = {
            "n": 1,
        }
        if steps:
            optional_params["steps"] = steps
        if width and height:
            optional_params["width"] = width
            optional_params["height"] = height
        if negative_prompt:
            optional_params["negative_prompt"] = negative_prompt

        payload.update(optional_params)

        logger.info(f"{self.name}: Starte Generierung...")
        logger.info(f"Modell: {model}, Groesse: {width}x{height}, Steps: {steps}")
        logger.info(f"Prompt: {prompt[:120]}...")

        try:
            resp = self._post_with_retry(payload, optional_params)

            data = resp.json()
            images = []
            for item in data.get("data", []):
                b64 = item.get("b64_json", "")
                if b64:
                    images.append(base64.b64decode(b64))
                    logger.info(f"Bild erhalten: {len(images[-1])} bytes")
                else:
                    # Fallback: URL-basierte Response
                    url = item.get("url", "")
                    if url:
                        img_resp = requests.get(url, timeout=60)
                        if img_resp.status_code == 200:
                            images.append(img_resp.content)
                            logger.info(f"Bild heruntergeladen: {len(images[-1])} bytes")

            if not images:
                logger.error(f"{self.name}: Keine Bilder in Response")
            return images

        except requests.Timeout:
            logger.error(f"{self.name}: Timeout nach {self.timeout}s")
            return []
        except RuntimeError:
            return []
        except Exception as e:
            logger.error(f"{self.name}: Fehler: {e}")
            return []


# Registry der verfuegbaren Backend-Typen
BACKEND_REGISTRY = {
    "a1111": A1111Backend,
    "mammouth": MammouthBackend,
    "comfyui": ComfyUIBackend,
    "civitai": CivitAIBackend,
    "together": TogetherBackend,
}
