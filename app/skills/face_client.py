"""HTTP Client for Face Processing Microservice

Drop-in replacement for face_swap and face_enhance modules.
Same function signatures — calls the face service via HTTP instead of running locally.
Returns None on any failure (same as originals), so callers need no changes.

Configuration via .env:
  FACE_SERVICE_URL=http://localhost:8005
"""
import os
import threading
import time
from typing import Optional

import requests

from app.core.log import get_logger
logger = get_logger("face_client")

_base_url: Optional[str] = None
_available_cache: Optional[bool] = None
_available_cache_time: float = 0
_CACHE_TTL = 30  # seconds

# Serialisierung: der Face-Service ist single-threaded (CPU-only ONNX,
# 1 Modell, kein Worker-Pool). Parallele Swap-Requests verlangsamen sich
# gegenseitig und triggern Read-Timeouts. Ein Lock serialisiert sie auf
# Client-Seite — entspricht "Task in der Queue" wie bei ComfyUI.
_swap_lock = threading.Lock()
_enhance_lock = threading.Lock()


def _get_base_url() -> str:
    global _base_url
    if _base_url is None:
        _base_url = os.environ.get("FACE_SERVICE_URL", "http://localhost:8005").rstrip("/")
    return _base_url


def invalidate_cache() -> None:
    """Invalidate cached availability and base URL so next check is fresh."""
    global _base_url, _available_cache, _available_cache_time
    _base_url = None
    _available_cache = None
    _available_cache_time = 0


def is_available() -> bool:
    """Check if the face service is reachable. Result is cached for 30 seconds."""
    global _available_cache, _available_cache_time
    now = time.time()
    if _available_cache is not None and (now - _available_cache_time) < _CACHE_TTL:
        return _available_cache
    try:
        resp = requests.get(f"{_get_base_url()}/health", timeout=3)
        _available_cache = resp.status_code == 200
    except Exception:
        _available_cache = False
    _available_cache_time = now
    return _available_cache


def apply_face_swap(
    target_image_bytes: bytes,
    source_image_bytes: bytes) -> Optional[bytes]:
    """Face swap via HTTP. Same signature as face_swap.apply_face_swap.

    Serialisiert ueber _swap_lock — der Face-Service kann immer nur ein
    Swap gleichzeitig rechnen (single-Threaded ONNX-Inference). Paralleler
    Druck loest sonst Read-Timeouts aus.
    """
    timeout_s = int(os.environ.get("FACE_SERVICE_REQUEST_TIMEOUT", "300"))
    with _swap_lock:
        try:
            resp = requests.post(
                f"{_get_base_url()}/swap",
                files={
                    "target": ("target.png", target_image_bytes, "image/png"),
                    "source": ("source.png", source_image_bytes, "image/png"),
                },
                timeout=timeout_s)
            if resp.status_code == 200:
                return resp.content
            logger.error(f"Swap failed ({resp.status_code}): {resp.text}")
            return None
        except requests.ConnectionError:
            logger.warning("Face service not reachable (swap)")
            return None
        except Exception as e:
            logger.error(f"Swap error: {e}")
        return None


def apply_face_swap_files(
    target_path: str,
    source_path: str,
    output_path: Optional[str] = None) -> Optional[str]:
    """File-based face swap via HTTP. Same signature as face_swap.apply_face_swap_files."""
    if not os.path.exists(target_path):
        logger.error(f"Target file not found: {target_path}")
        return None
    if not os.path.exists(source_path):
        logger.error(f"Source file not found: {source_path}")
        return None

    target_bytes = open(target_path, "rb").read()
    source_bytes = open(source_path, "rb").read()

    result_bytes = apply_face_swap(target_bytes, source_bytes)
    if result_bytes is None:
        return None

    out = output_path or target_path
    with open(out, "wb") as f:
        f.write(result_bytes)
    logger.info(f"Swap result saved: {out} ({len(result_bytes)} bytes)")
    return out


def apply_face_enhance(
    image_bytes: bytes,
    face_app=None) -> Optional[bytes]:
    """Face enhancement via HTTP. Same signature as face_enhance.apply_face_enhance.

    Note: face_app parameter is accepted for signature compatibility but ignored
    (the face service manages its own FaceAnalysis instance).

    Serialisiert ueber _enhance_lock — gleicher Grund wie bei apply_face_swap.
    """
    timeout_s = int(os.environ.get("FACE_SERVICE_REQUEST_TIMEOUT", "300"))
    with _enhance_lock:
        try:
            resp = requests.post(
                f"{_get_base_url()}/enhance",
                files={
                    "image": ("image.png", image_bytes, "image/png"),
                },
                timeout=timeout_s)
            if resp.status_code == 200:
                return resp.content
            logger.error(f"Enhance failed ({resp.status_code}): {resp.text}")
            return None
        except requests.ConnectionError:
            logger.warning("Face service not reachable (enhance)")
            return None
        except Exception as e:
            logger.error(f"Enhance error: {e}")
            return None


def apply_face_enhance_files(
    image_path: str,
    output_path: Optional[str] = None) -> Optional[str]:
    """File-based face enhancement via HTTP. Same signature as face_enhance.apply_face_enhance_files."""
    if not os.path.exists(image_path):
        logger.error(f"File not found: {image_path}")
        return None

    image_bytes = open(image_path, "rb").read()
    result_bytes = apply_face_enhance(image_bytes)
    if result_bytes is None:
        return None

    out = output_path or image_path
    with open(out, "wb") as f:
        f.write(result_bytes)
    logger.info(f"Enhance result saved: {out} ({len(result_bytes)} bytes)")
    return out


def reset():
    """Reset models on the face service. Same signature as face_swap.reset / face_enhance.reset."""
    try:
        resp = requests.post(f"{_get_base_url()}/reset", timeout=10)
        if resp.status_code == 200:
            logger.info("Models reset on face service")
        else:
            logger.error(f"Reset failed ({resp.status_code}): {resp.text}")
    except requests.ConnectionError:
        logger.warning("Face service not reachable (reset)")
    except Exception as e:
        logger.error(f"Reset error: {e}")
