"""User-Galerie Verwaltung

Zeigt die Galerie des aktiven Characters (frueher: separate User-Galerie).
Bilder koennen vom User hochgeladen oder von Characters gesendet werden.
"""
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime
import json
import shutil

from app.core.paths import get_storage_dir


def get_user_gallery_dir() -> Path:
    """Return the gallery directory for the player's active character.

    Falls back to a world-level gallery if no active character is set.
    """
    from app.models.account import get_active_character
    active = get_active_character()

    if active:
        gallery_dir = get_storage_dir() / "characters" / active / "gallery"
    else:
        gallery_dir = get_storage_dir() / "gallery"

    gallery_dir.mkdir(parents=True, exist_ok=True)
    return gallery_dir


def _get_meta_path(gallery_dir: Path, image_filename: str) -> Path:
    """Gibt den Pfad zur Metadaten-JSON eines Bildes zurueck."""
    stem = Path(image_filename).stem
    return gallery_dir / f"{stem}.json"


def _load_meta(gallery_dir: Path, image_filename: str) -> Dict[str, Any]:
    """Laedt die Metadaten eines einzelnen Bildes."""
    meta_path = _get_meta_path(gallery_dir, image_filename)
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_meta(gallery_dir: Path, image_filename: str, meta: Dict[str, Any]):
    """Speichert die Metadaten eines einzelnen Bildes."""
    meta_path = _get_meta_path(gallery_dir, image_filename)
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def get_user_gallery_images() -> List[str]:
    """Gibt eine sortierte Liste aller User-Galerie-Bilder zurueck (neueste zuerst)."""
    gallery_dir = get_user_gallery_dir()
    files = []
    for f in gallery_dir.iterdir():
        if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            sort_key = None
            meta = _load_meta(gallery_dir, f.name)
            sort_key = meta.get("created_at")
            if not sort_key:
                sort_key = datetime.fromtimestamp(
                    f.stat().st_mtime
                ).strftime("%Y-%m-%dT%H:%M:%S")
            files.append((sort_key, f))
    files.sort(key=lambda x: x[0], reverse=True)
    return [f.name for _, f in files]


def get_user_gallery_metadata() -> Dict[str, Dict[str, Any]]:
    """Gibt alle Bild-Metadaten der User-Galerie zurueck."""
    gallery_dir = get_user_gallery_dir()
    result = {}
    for meta_file in gallery_dir.glob("*.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            fn = meta.get("image_filename", meta_file.stem + ".png")
            result[fn] = meta
        except Exception:
            continue
    return result


def get_user_gallery_comments() -> Dict[str, str]:
    """Gibt alle Bild-Kommentare der User-Galerie zurueck."""
    all_meta = get_user_gallery_metadata()
    return {fn: m.get("comment", "") for fn, m in all_meta.items() if m.get("comment")}


def save_user_gallery_comment(image_filename: str, comment: str):
    """Speichert einen Kommentar fuer ein User-Galerie-Bild."""
    gallery_dir = get_user_gallery_dir()
    meta = _load_meta(gallery_dir, image_filename)
    meta["image_filename"] = image_filename
    meta["comment"] = comment
    _save_meta(gallery_dir, image_filename, meta)


def delete_user_gallery_image(image_filename: str) -> bool:
    """Loescht ein Bild, seine Metadaten und zugehoerige Videos aus der User-Galerie."""
    gallery_dir = get_user_gallery_dir()
    image_path = gallery_dir / image_filename
    meta_path = _get_meta_path(gallery_dir, image_filename)

    deleted = False
    if image_path.exists():
        image_path.unlink()
        deleted = True
    if meta_path.exists():
        meta_path.unlink()
        deleted = True

    # Zugehoeriges Video loeschen (gleicher Stem, .mp4/.webm)
    stem = Path(image_filename).stem
    for video_ext in (".mp4", ".webm"):
        video_path = gallery_dir / f"{stem}{video_ext}"
        if video_path.exists():
            try:
                video_path.unlink()
            except Exception:
                pass

    return deleted


