"""User Gallery Routes - Eigene Bildergalerie des Users"""
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path
from typing import Dict, Any
import mimetypes
import time

from app.core.log import get_logger
from app.models.user_gallery import (
    get_user_gallery_dir,
    get_user_gallery_images,
    get_user_gallery_metadata,
    get_user_gallery_comments,
    save_user_gallery_comment,
    delete_user_gallery_image)

logger = get_logger("user_gallery")

router = APIRouter(prefix="/user/gallery", tags=["user_gallery"])


@router.get("")
async def list_user_gallery() -> Dict[str, Any]:
    """Listet alle User-Galerie-Bilder mit Metadaten."""
    try:
        images = get_user_gallery_images()
        metadata = get_user_gallery_metadata()
        comments = get_user_gallery_comments()

        return {
            "images": images,
            "image_metadata": metadata,
            "image_comments": comments,
        }
    except Exception as e:
        logger.error("Error listing user gallery: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{filename}")
async def serve_user_gallery_image(filename: str):
    """Gibt ein einzelnes User-Galerie-Bild zurueck."""
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Ungueltiger Dateiname")

    gallery_dir = get_user_gallery_dir()
    image_path = gallery_dir / filename

    if not image_path.exists():
        raise HTTPException(status_code=404, detail="Bild nicht gefunden")

    media_type, _ = mimetypes.guess_type(str(image_path))
    return FileResponse(
        image_path,
        media_type=media_type or "application/octet-stream",
        headers={"Cache-Control": "no-cache"})


@router.post("")
async def upload_user_gallery_image(request: Request) -> Dict[str, Any]:
    """Laedt ein Bild in die User-Galerie hoch."""
    try:
        form = await request.form()
        file = form.get("file")

        if not file:
            raise HTTPException(status_code=400, detail="Keine Datei hochgeladen")

        allowed_extensions = {"png", "jpg", "jpeg", "gif", "webp"}
        filename = file.filename.lower()
        if not any(filename.endswith(ext) for ext in allowed_extensions):
            raise HTTPException(status_code=400, detail="Format nicht unterstuetzt")

        gallery_dir = get_user_gallery_dir()
        timestamp = int(time.time())
        file_ext = Path(filename).suffix
        image_filename = f"user_{timestamp}{file_ext}"
        image_path = gallery_dir / image_filename

        contents = await file.read()
        image_path.write_bytes(contents)

        # Metadaten anlegen
        from app.models.user_gallery import _save_meta
        from datetime import datetime
        _save_meta(gallery_dir, image_filename, {
            "image_filename": image_filename,
            "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "source": "upload",
        })

        return {
            "status": "success",
            "filename": image_filename,
            "url": f"/user/gallery/{image_filename}",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error uploading to user gallery: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{filename}")
async def delete_gallery_image(filename: str) -> Dict[str, str]:
    """Loescht ein Bild aus der User-Galerie."""
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="Ungueltiger Dateiname")

    deleted = delete_user_gallery_image(filename)
    if not deleted:
        raise HTTPException(status_code=404, detail="Bild nicht gefunden")

    return {"status": "success"}


@router.post("/{filename}/comment")
async def save_gallery_comment(filename: str, request: Request) -> Dict[str, str]:
    """Speichert einen Kommentar fuer ein User-Galerie-Bild."""
    data = await request.json()
    comment = data.get("comment", "")
    save_user_gallery_comment(filename, comment)
    return {"status": "success"}
