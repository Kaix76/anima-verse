"""Image post-processing — downscale generated images after backend output.

ComfyUI must generate at high resolution (lower workflow sizes crash the
backend), but for some classes of images we only need a fraction of that
on disk. This module:

1. Provides ``downscale_bytes`` — applied centrally in
   ``ImageBackend.generate`` after every successful generation.
2. Provides ``migrate_tree`` — one-shot walk over existing image
   directory trees to re-compress files that were saved before this hook
   existed.

Use-cases (see ``ui`` config section, "Image Downscaling" group):

* ``item`` → items in shared/items/<id>/ (default cap 512 px)
* ``map``  → map-icon thumbnails (gallery images tagged image_type=map,
             default cap 400 px)
* anything else / unset → bypass (full resolution kept)

Location/room backgrounds (day/night/scene/description) are NOT
downscaled — they're used as full-screen scene art.

PNG is preserved as the output format so existing references and rembg
alpha channels stay intact.
"""
from __future__ import annotations

import io
import re
import time
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from app.core.log import get_logger

logger = get_logger("image_postprocess")


# Numeric PNG filenames (timestamp-style) — only these are touched by
# migrate_tree. Other PNGs (preview thumbs, frame, manual uploads) stay.
_NUMERIC_PNG_RE = re.compile(r"^\d+\.png$")


def _config():
    """Lazy import to avoid circular import at module load."""
    from app.core import config as _cfg
    return _cfg


_DEFAULT_MAX_DIMS = {"item": 512, "map": 400}


def _max_dim_for(use_case: str) -> Optional[int]:
    """Return target max dimension for *use_case*, or None to bypass.

    Falls back to ``_DEFAULT_MAX_DIMS`` when the admin config has no value
    yet (fresh install, never saved).
    """
    if not use_case:
        return None
    cfg = _config()
    if not cfg.get("ui.downscale_enabled", True):
        return None
    default = _DEFAULT_MAX_DIMS.get(use_case)
    if default is None:
        return None
    key = f"ui.downscale_{use_case}_max_dim"
    val = cfg.get(key, default)
    try:
        ival = int(val)
    except (TypeError, ValueError):
        return default
    return ival if ival > 0 else None


def downscale_bytes(data: bytes, use_case: str) -> bytes:
    """Resize *data* to the configured max-dim for *use_case*.

    Returns the original bytes unchanged when:
    * use_case has no configured target (outfit, avatar, unknown, ...)
    * the image is already smaller than the target
    * Pillow is unavailable or decoding fails
    """
    target = _max_dim_for(use_case)
    if not target:
        return data

    try:
        from PIL import Image
    except Exception as exc:
        logger.debug("Pillow not available — downscale skipped: %s", exc)
        return data

    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            w, h = im.size
            if max(w, h) <= target:
                return data
            # thumbnail keeps aspect; LANCZOS = high-quality downscale
            im.thumbnail((target, target), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            save_kwargs = {"format": "PNG", "optimize": True}
            # Preserve alpha channel as-is (rembg output for items).
            im.save(buf, **save_kwargs)
            out = buf.getvalue()
            logger.info(
                "downscaled %s %dx%d → %dx%d (%d → %d bytes, -%d%%)",
                use_case, w, h, im.size[0], im.size[1],
                len(data), len(out),
                round(100 * (1 - len(out) / max(1, len(data)))))
            return out
    except Exception as exc:
        logger.warning("downscale failed (%s) — keeping original: %s",
                       use_case, exc)
        return data


# ---------------------------------------------------------------------------
# One-shot migration
# ---------------------------------------------------------------------------

def _resize_file_in_place(path: Path, max_dim: int) -> Tuple[int, int, bool]:
    """Returns (bytes_before, bytes_after, resized)."""
    try:
        from PIL import Image
    except Exception:
        return (path.stat().st_size, path.stat().st_size, False)

    before = path.stat().st_size
    try:
        with Image.open(path) as im:
            im.load()
            w, h = im.size
            if max(w, h) <= max_dim:
                return (before, before, False)
            im.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
            tmp = path.with_suffix(path.suffix + ".tmp")
            im.save(tmp, format="PNG", optimize=True)
        # atomic-ish replace
        tmp.replace(path)
        after = path.stat().st_size
        return (before, after, True)
    except Exception as exc:
        logger.warning("migrate skip %s: %s", path, exc)
        return (before, before, False)


def _walk_targets(use_case: str, *, world_scope: str = "current") -> Iterable[Path]:
    """Yield image files to consider for *use_case*.

    ``world_scope``:
      * ``"current"`` (default) — only the active storage world (or
        ``shared/`` for the item case, which is cross-world by design)
      * ``"all"`` — walk every sibling under ``<project>/worlds/``

    For ``map``, only PNGs whose filename appears in the surrounding
    ``gallery_meta.json`` with ``image_type=="map"`` are returned.
    Backgrounds (day/night/scene/description) are skipped entirely.
    """
    import json as _json
    from app.core.paths import get_shared_dir, get_storage_dir
    project_root = get_shared_dir().parent
    worlds_root = project_root / "worlds"

    if use_case == "item":
        # Items leben in zwei Orten:
        #   1) shared/items/ — cross-world Library (_shared=True)
        #   2) worlds/<welt>/items/ — welt-spezifische Items (Default fuer
        #      neu erzeugte Pieces / Inventory-Eintraege)
        items_shared = get_shared_dir() / "items"
        if items_shared.exists():
            for png in items_shared.glob("item_*/[0-9]*.png"):
                yield png
        if world_scope == "all":
            for png in worlds_root.glob("*/items/item_*/[0-9]*.png"):
                yield png
        else:
            world_items = get_storage_dir() / "items"
            if world_items.exists():
                for png in world_items.glob("item_*/[0-9]*.png"):
                    yield png
        return

    if use_case == "map":
        if world_scope == "all":
            if not worlds_root.exists():
                return
            meta_paths = worlds_root.glob("*/world_gallery/*/gallery_meta.json")
        else:
            gallery_root = get_storage_dir() / "world_gallery"
            if not gallery_root.exists():
                return
            meta_paths = gallery_root.glob("*/gallery_meta.json")

        for meta_path in meta_paths:
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    meta = _json.load(f)
            except Exception as exc:
                logger.warning("skipping unreadable gallery_meta %s: %s", meta_path, exc)
                continue
            image_types = meta.get("image_types") or {}
            gallery_dir = meta_path.parent
            for fname, t in image_types.items():
                if t != "map":
                    continue
                p = gallery_dir / fname
                if p.exists() and _NUMERIC_PNG_RE.match(p.name):
                    yield p


def migrate_tree(
    use_case: str,
    *,
    dry_run: bool = True,
    world_scope: str = "current",
) -> Dict:
    """Walk shared/items or worlds/<scope>/world_gallery and downscale.

    ``world_scope`` is ``"current"`` (default) or ``"all"``. For items the
    scope is irrelevant (items live under shared/).

    Returns a summary dict with totals and per-bucket breakdown.
    """
    if world_scope not in ("current", "all"):
        return {"ok": False, "use_case": use_case,
                "error": f"world_scope must be 'current' or 'all'"}
    target = _max_dim_for(use_case)
    if not target:
        return {
            "ok": False,
            "use_case": use_case,
            "error": f"No downscale target configured for use_case={use_case}",
        }

    started = time.time()
    files_scanned = 0
    files_resized = 0
    bytes_before_total = 0
    bytes_after_total = 0
    by_bucket: Dict[str, Dict] = {}

    for path in _walk_targets(use_case, world_scope=world_scope):
        files_scanned += 1
        # Bucket: shared/items/... -> "shared", worlds/<w>/... -> "<w>"
        try:
            bucket = path.parts[path.parts.index("worlds") + 1]
        except (ValueError, IndexError):
            bucket = "shared"
        b = by_bucket.setdefault(bucket, {
            "files_scanned": 0, "files_resized": 0,
            "bytes_before": 0, "bytes_after": 0,
        })
        b["files_scanned"] += 1

        if dry_run:
            try:
                from PIL import Image
                size_before = path.stat().st_size
                with Image.open(path) as im:
                    w, h = im.size
                if max(w, h) > target:
                    # Estimate post-resize bytes via area ratio. Empirically
                    # PNGs in this codebase compress slightly worse than a
                    # pure area ratio would predict (smoother content after
                    # downscale → less variance for predictor → larger PNG
                    # than naive area math). A 0.95× nudge tracks observed
                    # cases (1328² → 1024² saw 43% save vs. 41% predicted).
                    ratio = target / max(w, h)
                    est_after = int(size_before * (ratio ** 2) / 0.95)
                    files_resized += 1
                    bytes_before_total += size_before
                    bytes_after_total += est_after
                    b["files_resized"] += 1
                    b["bytes_before"] += size_before
                    b["bytes_after"] += est_after
                else:
                    bytes_before_total += size_before
                    bytes_after_total += size_before
                    b["bytes_before"] += size_before
                    b["bytes_after"] += size_before
            except Exception as exc:
                logger.warning("dry-run skip %s: %s", path, exc)
        else:
            before, after, resized = _resize_file_in_place(path, target)
            bytes_before_total += before
            bytes_after_total += after
            b["bytes_before"] += before
            b["bytes_after"] += after
            if resized:
                files_resized += 1
                b["files_resized"] += 1

    return {
        "ok": True,
        "use_case": use_case,
        "dry_run": dry_run,
        "world_scope": world_scope,
        "target_max_dim": target,
        "files_scanned": files_scanned,
        "files_resized": files_resized,
        "bytes_before": bytes_before_total,
        "bytes_after": bytes_after_total,
        "bytes_saved": bytes_before_total - bytes_after_total,
        "elapsed_seconds": round(time.time() - started, 2),
        "by_bucket": by_bucket,
    }
