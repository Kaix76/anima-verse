"""Channel Health Monitor: pollt Backend-Verfuegbarkeit pro GPU-Channel.

Jeder GPU-Channel (z.B. `Evo-X2:gpu2`) ist ueber `gpu_provider` in den
image_generation.backends[] an konkrete ComfyUI-Endpoints gebunden
(z.B. http://192.168.8.31:4070). Wenn alle diesem Channel zugeordneten
Backends down sind, soll `find_channel` den Channel nicht mehr waehlen —
sonst landen Tasks in einer Warteschlange, deren physische Ausfuehrung
nur noch ueber Fallback-Backends auf fremden Channels laeuft.

Der Monitor pollt alle POLL_INTERVAL_S Sekunden und cached das Ergebnis.
`is_healthy(channel_key)` wird synchron aus der find_channel-Heisspfad-
Auflistung gelesen (Mutex-geschuetzt, sehr billig).

Fuer nicht-comfyui-Channels (LLM) greift diese Logik nicht — die
werden weiterhin nur ueber `provider.available` gesteuert.
"""
import threading
import time
from typing import Dict, Iterable, Tuple

import requests

from app.core import config
from app.core.log import get_logger

logger = get_logger("channel_health")

POLL_INTERVAL_S = 30
REQUEST_TIMEOUT_S = 3
STARTUP_DELAY_S = 5


class ChannelHealthMonitor:
    """Background-Poller fuer Channel-Health-Status."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # channel_key -> (is_healthy, last_check_ts)
        self._status: Dict[str, Tuple[bool, float]] = {}
        self._started = False
        self._thread = None

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="channel-health")
        self._thread.start()
        logger.info("ChannelHealthMonitor gestartet (poll alle %ds)", POLL_INTERVAL_S)

    def is_healthy(self, channel_key: str, gpu_type: str = "") -> bool:
        """Liefert True wenn der Channel nutzbar ist.

        Nicht-comfyui-Channels (ollama/openai): immer True — Provider-Level-Check
        reicht.
        Channels ohne zugeordnetes Backend (z.B. Cloud): immer True.
        Channels mit nur unreachable Backends: False.
        """
        if gpu_type and gpu_type != "comfyui":
            return True
        with self._lock:
            entry = self._status.get(channel_key)
        if entry is None:
            # Noch nie gepollt — optimistisch True. Nach Startup-Delay sind
            # alle Channels vermessen.
            return True
        return entry[0]

    def force_poll(self) -> None:
        """Explizit alle Channels neu pollen. Nutzbar fuer Admin-Endpoints."""
        self._poll_all()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _iter_comfyui_backends_for_channel(self, channel_key: str) -> Iterable[dict]:
        """Listet enabled comfyui-Backends die diesem Channel zugeordnet sind."""
        img_gen = config.get("image_generation", {}) or {}
        backends = img_gen.get("backends", []) or []
        for b in backends:
            if not isinstance(b, dict):
                continue
            if b.get("api_type") != "comfyui":
                continue
            if not b.get("enabled"):
                continue
            gpu_provider = (b.get("gpu_provider") or "").strip()
            if not gpu_provider:
                continue
            # "Evo-X2:2" -> "Evo-X2:gpu2"
            parts = gpu_provider.split(":", 1)
            if len(parts) != 2:
                continue
            key = f"{parts[0]}:gpu{parts[1]}"
            if key == channel_key:
                yield b

    def _check_backend(self, backend: dict) -> bool:
        url = (backend.get("api_url") or "").rstrip("/")
        if not url:
            return False
        # /system_stats kann auf manchen ComfyUI-Versionen 500 werfen (kaputter
        # Custom-Node oder VRAM-Init-Fehler) obwohl der Server insgesamt OK
        # ist. Fallback auf /queue — leichter Endpoint, in jeder Version da.
        try:
            resp = requests.get(f"{url}/system_stats", timeout=REQUEST_TIMEOUT_S)
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        try:
            resp = requests.get(f"{url}/queue", timeout=REQUEST_TIMEOUT_S)
            return resp.status_code == 200
        except Exception:
            return False

    def _poll_channel(self, channel_key: str) -> None:
        backends = list(self._iter_comfyui_backends_for_channel(channel_key))
        if not backends:
            # Kein zugeordnetes comfyui-Backend — nicht unsere Zustaendigkeit.
            # True reporten, damit der Channel fuer andere Typen frei bleibt.
            with self._lock:
                self._status[channel_key] = (True, time.time())
            return

        healthy = False
        failed_urls = []
        for b in backends:
            if self._check_backend(b):
                healthy = True
                break
            failed_urls.append(b.get("api_url", "?"))

        with self._lock:
            prev_entry = self._status.get(channel_key)
            prev_healthy = prev_entry[0] if prev_entry else None
            self._status[channel_key] = (healthy, time.time())

        if prev_healthy is None:
            state = "healthy" if healthy else "unhealthy"
            if not healthy:
                logger.warning("Channel %s initial %s (backends down: %s)",
                               channel_key, state, ", ".join(failed_urls))
            else:
                logger.debug("Channel %s initial %s", channel_key, state)
        elif prev_healthy != healthy:
            if healthy:
                logger.info("Channel %s recovered (backend erreichbar)", channel_key)
            else:
                logger.warning("Channel %s jetzt unhealthy (backends down: %s)",
                               channel_key, ", ".join(failed_urls))

    def _poll_all(self) -> None:
        try:
            from app.core.provider_manager import get_provider_manager
            pm = get_provider_manager()
            channel_keys = list(pm.channels.keys())
        except Exception as e:
            logger.debug("ChannelHealth: Channels-Liste nicht lesbar: %s", e)
            return
        for key in channel_keys:
            self._poll_channel(key)

    def _loop(self) -> None:
        time.sleep(STARTUP_DELAY_S)
        while True:
            try:
                self._poll_all()
            except Exception as e:
                logger.warning("ChannelHealth-Loop Fehler: %s", e)
            time.sleep(POLL_INTERVAL_S)


# ----------------------------------------------------------------------
# Singleton + Public API
# ----------------------------------------------------------------------
_monitor: ChannelHealthMonitor | None = None


def get_monitor() -> ChannelHealthMonitor:
    global _monitor
    if _monitor is None:
        _monitor = ChannelHealthMonitor()
    return _monitor


def is_healthy(channel_key: str, gpu_type: str = "") -> bool:
    """Public-API fuer find_channel (und Tests)."""
    return get_monitor().is_healthy(channel_key, gpu_type)


def start() -> None:
    """Public-API fuer Server-Lifespan. Idempotent."""
    get_monitor().start()
