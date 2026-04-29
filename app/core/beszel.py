"""Beszel GPU monitoring integration.

Queries Beszel (PocketBase-based) for real-time GPU VRAM usage.
Used by ProviderQueue to decide whether to unload LLM models before GPU tasks.

Config (.env):
    BESZEL_URL=http://192.168.8.7:8090
    BESZEL_EMAIL=admin@example.com
    BESZEL_PASSWORD=secret
"""
import os
import time
import threading
import requests
from typing import Optional, Dict, Any

from app.core.log import get_logger

logger = get_logger("beszel")

_auth_token: Optional[str] = None
_auth_expires: float = 0.0
_auth_lock = threading.Lock()

# Cache fuer get_gpu_stats: TTL=5s. Beszel selbst sammelt nur alle 60s,
# also haben oeftere Polls (Frontend) keinen Mehrwert. Schuetzt Event-Loop +
# Beszel-Server vor Last.
_stats_cache: Dict[str, tuple] = {}  # system_id -> (timestamp, stats_dict_or_None)
_stats_cache_lock = threading.Lock()
_STATS_CACHE_TTL = 5.0


def _get_config() -> tuple:
    """Returns (url, email, password) from env."""
    url = os.environ.get("BESZEL_URL", "").strip().rstrip("/")
    email = os.environ.get("BESZEL_EMAIL", "").strip()
    password = os.environ.get("BESZEL_PASSWORD", "").strip()
    return url, email, password


def _authenticate(url: str, email: str, password: str) -> Optional[str]:
    """Authenticates with PocketBase and returns auth token."""
    global _auth_token, _auth_expires
    try:
        resp = requests.post(
            f"{url}/api/collections/users/auth-with-password",
            json={"identity": email, "password": password},
            timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            _auth_token = data.get("token")
            # Token valid for ~1 hour, refresh after 50 min
            _auth_expires = time.monotonic() + 3000
            logger.debug("Beszel auth OK")
            return _auth_token
        else:
            logger.warning("Beszel auth failed: HTTP %d", resp.status_code)
    except Exception as e:
        logger.warning("Beszel auth error: %s", e)
    return None


def _get_token() -> Optional[str]:
    """Returns a valid auth token, refreshing if needed."""
    global _auth_token, _auth_expires
    with _auth_lock:
        if _auth_token and time.monotonic() < _auth_expires:
            return _auth_token

        url, email, password = _get_config()
        if not url or not email or not password:
            return None
        return _authenticate(url, email, password)


def get_gpu_stats(system_id: str, vram_overrides: Optional[Dict[str, int]] = None) -> Optional[Dict[str, Any]]:
    """Queries Beszel for the latest GPU stats (with TTL cache).

    Args:
        system_id: Beszel system ID (e.g. "d7yl0g95g02t0f7")
        vram_overrides: Optional dict of gpu_id -> total VRAM in MB to override Beszel values.

    Returns:
        Dict with gpu_used_mb, gpu_total_mb, gpu_free_mb, gpu_util_pct, gpus
        or None if unavailable.
    """
    # Cache-Key inkludiert vram_overrides (verschiedene Aufrufer koennen
    # verschiedene Overrides haben). Tuple aus sorted items ist hashable.
    cache_key = (system_id, tuple(sorted((vram_overrides or {}).items())))
    now = time.monotonic()
    with _stats_cache_lock:
        cached = _stats_cache.get(cache_key)
        if cached and (now - cached[0]) < _STATS_CACHE_TTL:
            return cached[1]

    result = _fetch_gpu_stats(system_id, vram_overrides)
    with _stats_cache_lock:
        _stats_cache[cache_key] = (now, result)
    return result


def _fetch_gpu_stats(system_id: str, vram_overrides: Optional[Dict[str, int]] = None) -> Optional[Dict[str, Any]]:
    """Eigentlicher HTTP-Call ohne Cache. Nicht direkt aufrufen — geht ueber get_gpu_stats."""
    url, _, _ = _get_config()
    if not url:
        return None

    token = _get_token()
    if not token:
        return None

    try:
        resp = requests.get(
            f"{url}/api/collections/system_stats/records",
            params={
                "sort": "-created",
                "filter": f"(system='{system_id}')",
                "perPage": "1",
            },
            headers={"Authorization": token},
            timeout=10)
        if resp.status_code != 200:
            logger.warning("Beszel stats query failed: HTTP %d", resp.status_code)
            return None

        data = resp.json()
        items = data.get("items", [])
        if not items:
            return None

        record = items[0]
        inner = record.get("stats", record)
        gpus = inner.get("g", {})

        # System memory stats (for unified memory fallback)
        # m=total GB, mu=used GB, mp=used %, mb=buffer/cache GB
        sys_mem_total_mb = int((inner.get("m", 0) or 0) * 1024)
        sys_mem_used_mb = int((inner.get("mu", 0) or 0) * 1024)

        # No GPU entries: fall back to system RAM (unified memory systems like GB10)
        if not gpus:
            if not sys_mem_total_mb:
                return None
            return {
                "gpu_used_mb": sys_mem_used_mb,
                "gpu_total_mb": sys_mem_total_mb,
                "gpu_free_mb": sys_mem_total_mb - sys_mem_used_mb,
                "gpu_util_pct": 0,
                "gpus": [{
                    "id": "mem",
                    "name": "System RAM",
                    "used_mb": sys_mem_used_mb,
                    "total_mb": sys_mem_total_mb,
                    "util_pct": 0,
                    "power_w": 0,
                }],
            }

        # g is a dict keyed by GPU index ("0", "1", "card0", ...).
        # Each entry: n=name, mu=memory used (MB), mt=memory total (MB), u=utilization %, p=power
        # Aggregate all GPUs and return per-GPU details
        total_used = 0
        total_mem = 0
        total_util = 0
        gpu_list = []
        for key, gpu in gpus.items():
            if not isinstance(gpu, dict):
                continue
            mu = gpu.get("mu", 0) or 0
            mt = gpu.get("mt", 0) or 0
            # Apply VRAM override if configured
            if vram_overrides and key in vram_overrides:
                mt = vram_overrides[key]
            # Unified memory fallback: GPU without mu/mt → use system RAM stats
            if not gpu.get("mu") and not gpu.get("mt") and sys_mem_total_mb:
                mu = sys_mem_used_mb
                if not mt:
                    mt = sys_mem_total_mb
            total_used += mu
            total_mem += mt
            total_util += gpu.get("u", 0) or 0
            gpu_list.append({
                "id": key,
                "name": gpu.get("n", key),
                "used_mb": int(mu),
                "total_mb": int(mt),
                "util_pct": gpu.get("u", 0),
                "power_w": gpu.get("p", 0),
            })

        if not gpu_list:
            return None

        return {
            "gpu_used_mb": int(total_used),
            "gpu_total_mb": int(total_mem),
            "gpu_free_mb": int(total_mem - total_used) if total_mem else 0,
            "gpu_util_pct": round(total_util / len(gpu_list), 1),
            "gpus": gpu_list,
        }

    except Exception as e:
        logger.warning("Beszel GPU stats error: %s", e)
        return None
