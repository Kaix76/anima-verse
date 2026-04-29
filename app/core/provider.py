"""LLM Provider - represents a single API endpoint (e.g. Ollama server, OpenAI API).

Multiple LLM instances can share one provider. Each provider has its own queue,
configurable VRAM budget, and concurrency settings.
"""
import time
import requests
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.core.log import get_logger

logger = get_logger("provider")


@dataclass
class GpuConfig:
    """Per-GPU configuration from PROVIDER_X_GPUN_* env vars."""
    index: int                          # 0, 1, 2, ...
    vram_mb: int                        # VRAM in MB
    device: str = ""                    # Beszel device key (e.g. "0", "card0") — Fallback wenn kein match_name greift
    types: List[str] = field(default_factory=list)  # ["ollama"], ["openai", "comfyui"]
    label: str = ""                     # Display name (e.g. "RTX 4090 #1")
    match_name: str = ""                # Case-insensitive Substring im Beszel-Namen (stabil ueber Reboots)
    max_concurrent: int = 1             # Max parallel tasks on this GPU


@dataclass
class Provider:
    """Represents a single LLM API endpoint."""
    name: str                           # e.g. "OllamaLocal"
    type: str                           # "ollama", "openai", etc.
    api_base: str                       # e.g. "http://localhost:11434/v1"
    api_key: str
    vram_mb: Optional[int] = None       # Total LLM VRAM budget in MB (auto-computed from gpu_configs)
    max_concurrent: int = 1             # Max parallel requests
    timeout: Optional[int] = None       # Request timeout in seconds (None = use global default)
    beszel_system_id: str = ""          # Beszel system ID for GPU VRAM monitoring
    gpu_configs: List[GpuConfig] = field(default_factory=list)  # Per-GPU config
    system: str = ""                    # Physical system name (e.g. "ASUS-GX10") for dashboard grouping
    system_specs: str = ""              # Human-readable specs (e.g. "128 GB Unified RAM")
    available: bool = False

    # VRAM tracking state (Ollama only)
    vram_used_mb: int = 0
    loaded_models: List[Dict[str, Any]] = field(default_factory=list)

    # VRAM poll cache
    _vram_cache_time: float = field(default=0.0, repr=False)

    # Model list cache
    _models_cache: List[Dict[str, Any]] = field(default_factory=list, repr=False)
    _models_cache_time: float = field(default=0.0, repr=False)

    @property
    def gpu_vram_overrides(self) -> Dict[str, int]:
        """Derives Beszel device -> VRAM overrides from gpu_configs."""
        return {g.device: g.vram_mb for g in self.gpu_configs if g.device}

    def get_native_api_base(self) -> str:
        """Returns API base without /v1 suffix (for native Ollama API calls)."""
        base = self.api_base.rstrip("/")
        if base.endswith("/v1"):
            return base[:-3]
        return base

    def check_availability(self) -> bool:
        """Checks if this provider endpoint is reachable."""
        try:
            headers = {}
            if self.api_key and self.api_key not in ("ollama-local", ""):
                headers["Authorization"] = f"Bearer {self.api_key}"

            if self.type == "ollama":
                # Try OpenAI-compatible /models endpoint
                url = f"{self.api_base.rstrip('/')}/models"
                resp = requests.get(url, headers=headers, timeout=10)
                if resp.status_code == 200:
                    self.available = True
                    logger.info("OK   %s (%s, %s)", self.name, self.type, self.api_base)
                    return True

                # Fallback: native Ollama /api/tags
                native = self.get_native_api_base()
                resp = requests.get(f"{native}/api/tags", timeout=10)
                if resp.status_code == 200:
                    self.available = True
                    logger.info("OK   %s (%s, %s)", self.name, self.type, self.api_base)
                    return True

                logger.warning("FAIL %s (%s): not reachable at %s", self.name, self.type, self.api_base)
                self.available = False
                return False

            elif self.type == "comfyui":
                # ComfyUI: GET /system_stats
                url = f"{self.api_base.rstrip('/')}/system_stats"
                resp = requests.get(url, timeout=10)
                self.available = resp.status_code == 200
                if self.available:
                    logger.info("OK   %s (comfyui, %s)", self.name, self.api_base)
                else:
                    logger.warning("FAIL %s (comfyui): HTTP %d at %s", self.name, resp.status_code, self.api_base)
                return self.available

            elif self.type == "anthropic":
                # Anthropic Claude API: GET /models mit x-api-key Header
                url = f"{self.api_base.rstrip('/')}/models"
                anth_headers = {
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                }
                resp = requests.get(url, headers=anth_headers, timeout=10)
                self.available = resp.status_code == 200
                if self.available:
                    logger.info("OK   %s (anthropic, %s)", self.name, self.api_base)
                else:
                    logger.warning("FAIL %s (anthropic): HTTP %d", self.name, resp.status_code)
                return self.available

            else:
                # OpenAI and other types: GET /models with auth
                url = f"{self.api_base.rstrip('/')}/models"
                resp = requests.get(url, headers=headers, timeout=10)
                self.available = resp.status_code == 200
                if self.available:
                    logger.info("OK   %s (%s, %s)", self.name, self.type, self.api_base)
                else:
                    logger.warning("FAIL %s (%s): HTTP %d", self.name, self.type, resp.status_code)
                return self.available

        except requests.exceptions.ConnectionError:
            logger.warning("FAIL %s (%s): connection refused at %s", self.name, self.type, self.api_base)
            self.available = False
        except Exception as e:
            logger.error("FAIL %s (%s): %s", self.name, self.type, e)
            self.available = False

        return self.available

    def poll_vram_usage(self, cache_ttl: float = 10.0) -> Dict[str, Any]:
        """Polls VRAM usage.

        Ollama: via /api/ps. ComfyUI: via /system_stats.
        Returns dict with vram_total_mb, vram_used_mb, vram_free_mb, loaded_models.
        Cached for cache_ttl seconds.
        """
        if self.type == "comfyui":
            return self._poll_comfyui_vram(cache_ttl)
        if self.type != "ollama":
            return {}

        now = time.monotonic()
        if (now - self._vram_cache_time) < cache_ttl and self.loaded_models is not None:
            return {
                "vram_total_mb": self.vram_mb,
                "vram_used_mb": self.vram_used_mb,
                "vram_free_mb": (self.vram_mb - self.vram_used_mb) if self.vram_mb else None,
                "loaded_models": self.loaded_models,
            }

        try:
            native = self.get_native_api_base()
            resp = requests.get(f"{native}/api/ps", timeout=5)
            if resp.status_code != 200:
                return {}

            data = resp.json()
            models = data.get("models", [])
            total_vram = 0
            loaded = []

            for m in models:
                size = m.get("size_vram", 0)  # Ollama reports in bytes
                size_mb = size // (1024 * 1024)
                total_vram += size_mb
                loaded.append({
                    "name": m.get("name", ""),
                    "size_mb": size_mb,
                    "digest": m.get("digest", "")[:12],
                })

            self.vram_used_mb = total_vram
            self.loaded_models = loaded
            self._vram_cache_time = now

            return {
                "vram_total_mb": self.vram_mb,
                "vram_used_mb": total_vram,
                "vram_free_mb": (self.vram_mb - total_vram) if self.vram_mb else None,
                "loaded_models": loaded,
            }

        except Exception as e:
            logger.error("VRAM poll failed for %s: %s", self.name, e)
            return {}

    def _poll_comfyui_vram(self, cache_ttl: float = 10.0) -> Dict[str, Any]:
        """Polls VRAM from ComfyUI /system_stats. Cached for cache_ttl seconds."""
        now = time.monotonic()
        if (now - self._vram_cache_time) < cache_ttl and self._vram_cache_time > 0:
            used = self.vram_used_mb or 0
            total = self.vram_mb or 0
            return {
                "vram_total_mb": total,
                "vram_used_mb": used,
                "vram_free_mb": total - used,
                "loaded_models": [],
                "source": "comfyui",
            }
        try:
            resp = requests.get(f"{self.api_base.rstrip('/')}/system_stats", timeout=5)
            if resp.status_code != 200:
                return {}
            data = resp.json()
            devices = data.get("devices", [])
            if devices:
                dev = devices[0]
                total_bytes = dev.get("vram_total", 0)
                free_bytes = dev.get("vram_free", 0)
                total = total_bytes // (1024 * 1024)
                free = free_bytes // (1024 * 1024)
                used = max(0, total - free)
            else:
                total = self.vram_mb or 0
                used = 0
                free = total
            # Prefer configured vram_mb as "total" if set (more reliable label)
            display_total = self.vram_mb or total
            self.vram_used_mb = used
            self._vram_cache_time = now
            return {
                "vram_total_mb": display_total,
                "vram_used_mb": used,
                "vram_free_mb": display_total - used,
                "loaded_models": [],
                "source": "comfyui",
            }
        except Exception as e:
            logger.error("ComfyUI VRAM poll failed for %s: %s", self.name, e)
            return {}

    def get_free_vram_mb(self) -> Optional[int]:
        """Returns free VRAM in MB, or None if unknown.

        Tries multiple sources:
        1. Ollama /api/ps (if type=ollama)
        2. Beszel GPU monitoring (if beszel_system_id configured)
        """
        # ComfyUI: poll /system_stats
        if self.type == "comfyui":
            vram = self._poll_comfyui_vram(cache_ttl=5.0)
            if vram and vram.get("vram_free_mb") is not None:
                return vram["vram_free_mb"]

        # Ollama: direct VRAM query
        if self.type == "ollama" and self.vram_mb:
            vram = self.poll_vram_usage(cache_ttl=5.0)
            if vram and vram.get("vram_free_mb") is not None:
                return vram["vram_free_mb"]

        # Beszel: real-time GPU stats
        if self.beszel_system_id:
            try:
                from app.core.beszel import get_gpu_stats
                stats = get_gpu_stats(self.beszel_system_id, self.gpu_vram_overrides)
                if stats and stats.get("gpu_free_mb") is not None:
                    return stats["gpu_free_mb"]
            except Exception as e:
                logger.debug("[%s] Beszel VRAM query failed: %s", self.name, e)

        return None

    def can_accommodate(self, vram_mb: int) -> bool:
        """Checks if this provider currently has enough free VRAM.

        Args:
            vram_mb: Required VRAM in MB. 0 = always True.

        Returns:
            True if enough free VRAM (or unknown/unmanaged).
        """
        if vram_mb <= 0:
            return True
        free = self.get_free_vram_mb()
        if free is None:
            return True  # Unknown = optimistic
        return free >= vram_mb

    def _is_comfyui_gpu(self, gpu_indices: Optional[List[int]] = None) -> bool:
        """Returns True if the given GPU indices are exclusively ComfyUI GPUs."""
        if not gpu_indices or not self.gpu_configs:
            return False
        for g in self.gpu_configs:
            if g.index in gpu_indices and "comfyui" in g.types:
                return True
        return False

    def ensure_vram_available(self, vram_mb: int, gpu_indices: Optional[List[int]] = None) -> bool:
        """Ensures enough free VRAM, unloading models if necessary.

        Args:
            vram_mb: Required VRAM in MB. Must be > 0 (all GPU tasks should specify VRAM_REQUIRED).
            gpu_indices: Which GPU(s) this task runs on. Used to skip LLM unload for ComfyUI GPUs.

        Returns:
            True if VRAM is available (or unmanaged), False if still not enough.
        """
        # ComfyUI has its own dedicated GPU — no LLM models to unload
        if self.type == "comfyui" or self._is_comfyui_gpu(gpu_indices):
            return True

        required = vram_mb
        if required <= 0:
            logger.warning("[%s] GPU task without VRAM requirement — skipping VRAM check", self.name)
            return True

        # Check current free VRAM
        free = self.get_free_vram_mb()
        if free is None:
            # Can't determine VRAM → unload to be safe + wait for release
            logger.info("[%s] Free VRAM unknown, unloading to be safe (need %dMB)",
                        self.name, required)
            self._do_unload()
            import time as _time
            _time.sleep(5)  # Give llama-swap time to release VRAM
            return True

        if free >= required:
            logger.info("[%s] VRAM OK: %dMB free >= %dMB required, skip unload",
                        self.name, free, required)
            return True

        # Not enough → unload and recheck
        logger.info("[%s] VRAM low: %dMB free < %dMB required, unloading models",
                    self.name, free, required)
        self._do_unload()

        # Invalidate Beszel cache so next poll gets fresh data
        self._vram_cache_time = 0.0

        # Poll until VRAM is freed (llama-swap needs time to release GPU memory)
        import time as _time
        for attempt in range(6):  # up to ~15s total
            _time.sleep(2 + attempt)  # 2, 3, 4, 5, 6, 7s
            self._vram_cache_time = 0.0  # force fresh read each iteration
            free_after = self.get_free_vram_mb()
            if free_after is None:
                logger.info("[%s] VRAM unknown after unload (attempt %d), proceeding",
                            self.name, attempt + 1)
                return True
            if free_after >= required:
                logger.info("[%s] VRAM freed after unload: %dMB free >= %dMB required (attempt %d)",
                            self.name, free_after, required, attempt + 1)
                return True
            logger.info("[%s] VRAM still low: %dMB free < %dMB required (attempt %d/6)",
                        self.name, free_after, required, attempt + 1)

        logger.warning("[%s] VRAM still low after unload + polling: %dMB free < %dMB required",
                       self.name, free_after, required)
        return False

    def _do_unload(self, timeout: int = 30) -> bool:
        """Actually unloads all models via /unload endpoint."""
        native = self.get_native_api_base()
        url = f"{native}/unload"
        try:
            logger.info("[%s] Unloading models: GET %s", self.name, url)
            resp = requests.get(url, timeout=timeout)
            if resp.status_code == 200:
                logger.info("[%s] Models unloaded (VRAM freed)", self.name)
                self._vram_cache_time = 0.0  # invalidate cache
                return True
            else:
                logger.warning("[%s] Unload returned HTTP %d", self.name, resp.status_code)
                return False
        except requests.exceptions.ConnectionError:
            logger.warning("[%s] Unload failed: connection refused at %s", self.name, url)
            return False
        except Exception as e:
            logger.error("[%s] Unload failed: %s", self.name, e)
            return False

    def list_models(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """Lists available models from this provider.

        Ollama: GET /api/tags -> models[].name, .size, .details
        OpenAI: GET /models -> data[].id
        Results are cached for 60 seconds.
        """
        if not force_refresh and self._models_cache and (time.time() - self._models_cache_time) < 60:
            return self._models_cache

        models: List[Dict[str, Any]] = []
        try:
            headers = {}
            if self.api_key and self.api_key not in ("ollama-local", ""):
                headers["Authorization"] = f"Bearer {self.api_key}"

            if self.type == "ollama":
                native = self.get_native_api_base()
                resp = requests.get(f"{native}/api/tags", headers=headers, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    for m in data.get("models", []):
                        raw_size = m.get("size", 0)
                        models.append({
                            "name": m.get("name", ""),
                            "size": raw_size,
                            "size_gb": round(raw_size / (1024**3), 1) if raw_size else 0,
                            "parameter_size": m.get("details", {}).get("parameter_size", ""),
                            "family": m.get("details", {}).get("family", ""),
                            "quantization": m.get("details", {}).get("quantization_level", ""),
                        })
            elif self.type == "anthropic":
                url = f"{self.api_base.rstrip('/')}/models"
                anth_headers = {
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                }
                resp = requests.get(url, headers=anth_headers, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    for m in data.get("data", []):
                        models.append({
                            "name": m.get("id", ""),
                            "size": 0,
                            "size_gb": 0,
                            "parameter_size": "",
                            "family": "claude",
                            "quantization": "",
                        })
            else:
                url = f"{self.api_base.rstrip('/')}/models"
                resp = requests.get(url, headers=headers, timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    raw = data.get("data", []) if isinstance(data, dict) else data
                    for m in raw:
                        # Skip non-chat models (image, video, audio, etc.)
                        mtype = m.get("type", "")
                        if mtype and mtype != "chat":
                            continue

                        # Serverless filter: skip models without per-token pricing
                        # (Together.ai provides pricing.input / pricing.output)
                        pricing = m.get("pricing")
                        if isinstance(pricing, dict):
                            has_serverless = (pricing.get("input", 0) or 0) > 0 or (pricing.get("output", 0) or 0) > 0
                            if not has_serverless:
                                continue

                        # Text-only filter: skip vision/VL/OCR models
                        mid = m.get("id", "").lower()
                        display = m.get("display_name", "").lower()
                        if any(tag in mid or tag in display for tag in ("-vl-", "-vl ", "vl-", "/vl-", "-ocr", "vision")):
                            continue

                        # Extract pricing for display
                        price_in = pricing.get("input", 0) if isinstance(pricing, dict) else 0
                        price_out = pricing.get("output", 0) if isinstance(pricing, dict) else 0
                        ctx_len = m.get("context_length", 0)
                        display_name = m.get("display_name", "") or m.get("id", "")

                        models.append({
                            "name": m.get("id", ""),
                            "display_name": display_name,
                            "size": 0,
                            "size_gb": 0,
                            "parameter_size": "",
                            "family": m.get("organization", ""),
                            "quantization": "",
                            "context_length": ctx_len,
                            "pricing": {"input": price_in, "output": price_out},
                        })

        except Exception as e:
            logger.error("list_models failed for %s: %s", self.name, e)

        self._models_cache = models
        self._models_cache_time = time.time()
        return models

    def has_model(self, model_name: str) -> bool:
        """Checks if this provider has the given model available.

        Handles Ollama tag normalization: 'model' matches 'model:latest'.
        """
        for m in self.list_models():
            if m["name"] == model_name:
                return True
            # 'Qwen3-8B' matches 'Qwen3-8B:latest' and vice versa
            if ":" not in model_name and m["name"] == f"{model_name}:latest":
                return True
            if model_name.endswith(":latest") and m["name"] == model_name[:-7]:
                return True
        return False
