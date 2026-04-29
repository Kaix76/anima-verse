"""Messaging-Frame fuer Phone-Chat-Layout.

Pro Welt kann ein Frame (Smartphone, Spiegel, Kristallkugel, Schautafel) per
LLM-Image-Prompt generiert werden. Pipeline:

  1. image_skill generiert ein Bild mit "<frame> mit pure-green-Anzeigeflaeche"
  2. rembg entfernt den aeusseren Hintergrund (Frame-Edge transparent)
  3. Chroma-Key: alle gruenen Pixel finden -> alpha=0
  4. Bounding-Box der Green-Region berechnen -> sidecar-JSON
  5. Frontend stapelt: Charakter-Bild im bbox-Bereich + Frame-Overlay

Files:
  worlds/<world>/ui/messaging_frame.png    (Frame mit transparenter Anzeigeflaeche)
  worlds/<world>/ui/messaging_frame.json   {prompt, bbox, frame_size, generated_at}
"""
import json
import time
from pathlib import Path
from typing import Dict, Any, Optional, Tuple

from app.core.log import get_logger

logger = get_logger("messaging_frame")


def _ui_dir() -> Path:
    from app.core.paths import get_storage_dir
    d = get_storage_dir() / "ui"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_frame_path() -> Path:
    return _ui_dir() / "messaging_frame.png"


def get_frame_meta_path() -> Path:
    return _ui_dir() / "messaging_frame.json"


def load_frame_meta() -> Optional[Dict[str, Any]]:
    p = get_frame_meta_path()
    if not p.exists():
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Frame-Meta laden fehlgeschlagen: %s", e)
        return None


def has_frame() -> bool:
    return get_frame_path().exists() and get_frame_meta_path().exists()


def _process_chroma_key(image_bytes: bytes) -> Tuple[bytes, Dict[str, Any]]:
    """Wendet rembg + Chroma-Key auf das generierte Frame-Bild an.

    Args:
        image_bytes: PNG/JPEG bytes vom Image-Backend.

    Returns:
        (processed_png_bytes, meta_dict mit bbox + frame_size)

    Raises:
        ValueError wenn keine gruene Anzeigeflaeche gefunden.
    """
    import io
    import numpy as np
    from PIL import Image

    # 1. Bild laden + RGBA
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    arr = np.array(img)  # shape (H, W, 4)
    h, w = arr.shape[:2]
    logger.info("Frame-Source: %dx%d", w, h)

    # 2. rembg fuer aeusseren Hintergrund — Frame-Kontur isolieren
    try:
        from app.models.character import _get_rembg_session
        from rembg import remove
        session = _get_rembg_session()
        # rembg erwartet bytes
        rembg_out = remove(image_bytes, session=session)
        rembg_img = Image.open(io.BytesIO(rembg_out)).convert("RGBA")
        arr = np.array(rembg_img)
        logger.info("rembg angewendet (Hintergrund entfernt)")
    except Exception as e:
        # Nicht fatal — ohne rembg ist nur der aeussere Rand opak. Frame
        # funktioniert trotzdem, wirkt aber rechteckig statt freigestellt.
        logger.warning("rembg uebersprungen (%s) — Frame bleibt rechteckig", e)

    # 3. Chroma-Key: gruene Pixel finden
    # HSV-Klassifikation wuerde mehr Toleranz bringen; hier reicht RGB-Heuristik.
    rgb = arr[:, :, :3].astype(np.int16)
    a = arr[:, :, 3]
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    # "Gruen" = G deutlich hoeher als R+B
    is_green = (g > 100) & (g > r + 30) & (g > b + 30) & (a > 100)
    green_count = int(is_green.sum())
    logger.info("Green-Pixel: %d von %d (%.1f%%)", green_count, h * w, 100.0 * green_count / max(h * w, 1))
    if green_count < 1000:
        raise ValueError(
            f"Keine ausreichende gruene Anzeigeflaeche im Bild gefunden "
            f"({green_count} Pixel). Pass den Prompt an — die Anzeigeflaeche muss "
            f"deutlich gruen sein (z.B. 'pure green screen', 'chroma green')."
        )

    # 4. Bounding-Box der Green-Region (groesste zusammenhaengende Region):
    # Vereinfacht: wir nehmen die Min/Max-Koordinaten aller Green-Pixel.
    # Annahme: das Modell macht eine zusammenhaengende Anzeigeflaeche.
    ys, xs = np.where(is_green)
    bbox_x = int(xs.min())
    bbox_y = int(ys.min())
    bbox_w = int(xs.max() - xs.min() + 1)
    bbox_h = int(ys.max() - ys.min() + 1)
    logger.info("Bounding-Box: x=%d y=%d w=%d h=%d", bbox_x, bbox_y, bbox_w, bbox_h)

    # 5. Green-Pixel transparent machen
    new_arr = arr.copy()
    new_arr[is_green, 3] = 0  # alpha=0

    # 6. Trim: alle vollstaendig transparenten (alpha=0) Spalten und Zeilen
    # am Rand abschneiden. Dadurch entspricht das Frame-PNG exakt der
    # sichtbaren Flaeche — die Profilbild-Positionierung im Frontend wird
    # unabhaengig von der vom Modell gerenderten Leerflaeche.
    alpha = new_arr[:, :, 3]
    visible_cols = (alpha > 0).any(axis=0)
    visible_rows = (alpha > 0).any(axis=1)
    nonzero_x = np.where(visible_cols)[0]
    nonzero_y = np.where(visible_rows)[0]
    if len(nonzero_x) > 0 and len(nonzero_y) > 0:
        crop_left = int(nonzero_x.min())
        crop_right = int(nonzero_x.max() + 1)  # exklusiv fuer Slicing
        crop_top = int(nonzero_y.min())
        crop_bottom = int(nonzero_y.max() + 1)
        new_arr = new_arr[crop_top:crop_bottom, crop_left:crop_right, :]
        # bbox-Koordinaten relativ zum neuen Bild-Anker korrigieren
        bbox_x = max(0, bbox_x - crop_left)
        bbox_y = max(0, bbox_y - crop_top)
        new_w = crop_right - crop_left
        new_h = crop_bottom - crop_top
        if (crop_left > 0 or crop_right < w or crop_top > 0 or crop_bottom < h):
            logger.info(
                "Trim: %d L + %d R + %d T + %d B entfernt -> %dx%d (war %dx%d)",
                crop_left, w - crop_right, crop_top, h - crop_bottom,
                new_w, new_h, w, h)
        w, h = new_w, new_h

    # 7. Speichern
    out_img = Image.fromarray(new_arr, mode="RGBA")
    out_buf = io.BytesIO()
    out_img.save(out_buf, format="PNG")
    meta = {
        "bbox": {"x": bbox_x, "y": bbox_y, "w": bbox_w, "h": bbox_h},
        "frame_size": [w, h],
        "green_pixel_count": green_count,
    }
    return out_buf.getvalue(), meta


def generate_frame(prompt: str, target: str = "") -> Dict[str, Any]:
    """Erzeugt das Messaging-Frame-Bild via image_skill und persistiert es.

    Args:
        prompt: Bild-Prompt (z.B. "modern smartphone, pure green screen, isolated").
        target: Auswahl-String wie vom /admin/settings/imagegen-targets-Endpoint:
            "workflow:Z-Image", "backend:Together-Fast", oder leer = Auto.

    Returns:
        dict mit status, path, bbox, frame_size oder error.
    """
    if not prompt or not prompt.strip():
        return {"status": "error", "error": "Prompt fehlt"}

    # 1. image_skill via Skill-Manager holen
    try:
        from app.core.dependencies import get_skill_manager
        sm = get_skill_manager()
        image_skill = sm.get_skill("image_generation")
        if not image_skill:
            return {"status": "error", "error": "image_generation skill nicht verfuegbar"}
    except Exception as e:
        return {"status": "error", "error": f"Skill-Manager fehlt: {e}"}

    # 2. Target parsen: "workflow:Name" / "backend:Name" / "" (= Auto)
    workflow_name = ""
    backend_name = ""
    if target:
        if target.startswith("workflow:"):
            workflow_name = target[len("workflow:"):].strip()
        elif target.startswith("backend:"):
            backend_name = target[len("backend:"):].strip()
        else:
            return {"status": "error", "error":
                f"Ungueltiges Target-Format: '{target}'. Erwartet 'workflow:Name' oder 'backend:Name'."}

    # 3. Generieren — alles ueber image_skill.execute() (nutzt Skill-Pipeline mit
    # allen Workflow-Konventionen: input_*-Patches, Switches, allowed_models,
    # Seed, model_resolve, Cloud-Cross-Type-Fallback).
    try:
        import json as _json
        payload = {
            "prompt": prompt.strip(),
            "input": prompt.strip(),
            "agent_name": "_messaging_frame",
            "user_id": "",
            "set_profile": False,
            "skip_gallery": True,
            "skip_post_processing": True,  # kein FaceSwap/Enhance fuer UI-Asset
            "auto_enhance": False,
            "force_faceswap": False,
            "negative_prompt": "person, people, face, reflection, text, watermark, blurry, lowres",
        }
        if workflow_name:
            payload["workflow"] = workflow_name
        if backend_name:
            payload["backend"] = backend_name
        logger.info("Frame-Generierung: target=%s prompt=%.80s", target or "auto", prompt)
        img_result = image_skill.execute(_json.dumps(payload))

        # execute() liefert einen Status/Pfad-String — Pfad extrahieren
        import re as _re
        m = _re.search(r"/images/([^?\s\)\n]+)", img_result or "")
        if not m:
            return {"status": "error",
                    "error": f"Generierung lieferte kein Bild: {(img_result or '')[:400]}"}
        from app.models.character import get_character_images_dir
        src_path = get_character_images_dir("_messaging_frame") / m.group(1)
        if not src_path.exists():
            return {"status": "error", "error": f"Datei nicht gefunden: {src_path}"}
        raw_bytes = src_path.read_bytes()
        # Aufraeumen — komplettes _messaging_frame-Pseudo-Character-Verzeichnis
        # entfernen, damit weder Bild-Datei noch profile/Outfits/Meta-Files
        # liegenbleiben. Der Skill legt die beim execute() automatisch an.
        try:
            import shutil
            char_dir = src_path.parent.parent  # images/<file> -> char_dir
            if char_dir.exists() and char_dir.name == "_messaging_frame":
                shutil.rmtree(char_dir, ignore_errors=True)
        except Exception as _cleanup_err:
            logger.debug("Cleanup _messaging_frame fehlgeschlagen: %s", _cleanup_err)
        # Auch den DB-Eintrag falls die _ensure_agent_config einen erstellt hat
        try:
            from app.core.db import get_connection as _conn
            with _conn() as _c:
                _c.execute("DELETE FROM characters WHERE name = ?", ("_messaging_frame",))
                _c.commit()
        except Exception:
            pass
    except Exception as e:
        logger.error("Frame-Generierung fehlgeschlagen: %s", e)
        return {"status": "error", "error": str(e)[:200]}

    # 4. Chroma-Key + rembg
    try:
        processed_bytes, meta = _process_chroma_key(raw_bytes)
    except ValueError as e:
        return {"status": "error", "error": str(e)}
    except Exception as e:
        logger.error("Post-Processing fehlgeschlagen: %s", e)
        return {"status": "error", "error": f"Post-Processing: {str(e)[:200]}"}

    # 5. Speichern
    frame_path = get_frame_path()
    frame_path.write_bytes(processed_bytes)
    meta["prompt"] = prompt
    meta["target"] = target or "auto"
    meta["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with open(get_frame_meta_path(), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    logger.info("Frame gespeichert: %s (bbox=%s)", frame_path, meta["bbox"])
    return {
        "status": "ok",
        "path": str(frame_path),
        "bbox": meta["bbox"],
        "frame_size": meta["frame_size"],
        "target": target or "auto",
        "generated_at": meta["generated_at"],
    }
