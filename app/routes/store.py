"""Store routes - Generic key-value storage for user data"""
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import FileResponse
from pathlib import Path
from typing import Dict, Any
import mimetypes
import time
from app.core.log import get_logger

logger = get_logger("store")

from app.models.account import (
    get_user_name,
    save_user_name,
    get_current_agent,
    get_user_theme,
    save_user_theme,
    get_user_images_dir,
    get_user_profile_image)
from app.models.character import (
    get_character_personality,
    save_character_personality,
    get_character_appearance,
    get_character_address_form,
    get_character_profile)
from app.models.character_template import (
    get_template,
    resolve_profile_tokens)

router = APIRouter(prefix="/store", tags=["store"])


# --- User Profile Image (muss VOR den generischen /{user_id}/{key} Routen stehen) ---

@router.post("/{user_id}/profile-image")
async def upload_user_profile_image(request: Request) -> Dict[str, Any]:
    """Laedt ein Profilbild fuer den User hoch und setzt es als aktives Profilbild."""
    try:
        form = await request.form()
        file = form.get("file")

        if not file:
            raise HTTPException(status_code=400, detail="Keine Datei hochgeladen")

        allowed_extensions = {"png", "jpg", "jpeg", "gif", "webp"}
        filename = file.filename.lower()
        if not any(filename.endswith(ext) for ext in allowed_extensions):
            raise HTTPException(status_code=400, detail="Format nicht unterstuetzt")

        images_dir = get_user_images_dir()

        timestamp = int(time.time())
        file_ext = Path(filename).suffix
        image_filename = f"profile_{timestamp}{file_ext}"
        image_path = images_dir / image_filename

        contents = await file.read()
        image_path.write_bytes(contents)

        # Save profile image to active character (or legacy user profile)
        from app.models.account import get_active_character, get_user_profile as _gup_img, save_user_profile as _sup_img
        _active = get_active_character()
        if _active:
            from app.models.character import get_character_profile, save_character_profile
            _cp = get_character_profile(_active)
            _cp["profile_image"] = image_filename
            save_character_profile(_active, _cp)
        else:
            _p = _gup_img()
            _p["profile_image"] = image_filename
            _sup_img(_p)

        return {
            "status": "success",
            "filename": image_filename,
            "url": "/store/profile-image"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{user_id}/profile-image")
def get_user_profile_image_file():
    """Gibt das Profilbild des Users zurueck."""
    try:
        image_filename = get_user_profile_image()
        if not image_filename:
            raise HTTPException(status_code=404, detail="Kein Profilbild gesetzt")

        if ".." in image_filename or "/" in image_filename:
            raise HTTPException(status_code=400, detail="Ungueltiger Dateiname")

        images_dir = get_user_images_dir()
        image_path = images_dir / image_filename

        if not image_path.exists():
            raise HTTPException(status_code=404, detail="Profilbild nicht gefunden")

        media_type, _ = mimetypes.guess_type(str(image_path))
        return FileResponse(
            image_path,
            media_type=media_type or "application/octet-stream",
            headers={"Cache-Control": "no-cache"}
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{user_id}/{key}")
async def store_data(key: str, request: Request) -> Dict[str, str]:
    """Speichert einen Wert für einen bestimmten Key"""
    data = await request.json()
    value = data.get("value", "")

    # Verwende die Model-Funktionen für bekannte Keys
    if key == "user_name":
        save_user_name(value)
    elif key == "chat_partner":
        # Chat-Partner = Character mit dem der User spricht, per-User gespeichert.
        from app.models.account import get_chat_partner, set_chat_partner
        old_partner = get_chat_partner()
        if old_partner and old_partner != value:
            try:
                from app.models.character import set_outfit_locked, is_outfit_locked
                if is_outfit_locked(old_partner):
                    set_outfit_locked(old_partner, False)
            except Exception:
                pass
        set_chat_partner(value)
    elif key == "character_personality":
        current_agent = get_current_agent()
        if current_agent:
            save_character_personality(current_agent, value)
    elif key == "theme":
        save_user_theme(value)
    elif key == "user_profile":
        # Speichere mehrere User-Felder auf einmal (ein read/write Zyklus)
        fields = data.get("fields", {})
        from app.models.account import save_user_profile, get_user_profile
        profile = get_user_profile()
        if "template" in fields:
            profile["template"] = fields["template"]
        if "gender" in fields:
            profile["gender"] = fields["gender"]
        if "birthdate" in fields:
            profile["birthdate"] = fields["birthdate"]
        if "hobbies" in fields:
            hobbies = fields["hobbies"]
            if isinstance(hobbies, str):
                hobbies = [h.strip() for h in hobbies.split(",") if h.strip()]
            profile["hobbies"] = hobbies
        # Location (mit Timestamp)
        if "current_location" in fields:
            from datetime import datetime
            profile["current_location"] = fields["current_location"]
            profile["location_changed_at"] = datetime.now().isoformat()
        # System fields
        for k in ("system_language", "translation_mode"):
            if k in fields:
                profile[k] = fields[k]
        # Romantic interests
        if "romantic_interests" in fields:
            profile["romantic_interests"] = fields["romantic_interests"]
        # Appearance-Felder
        appearance_keys = ["body_type", "size", "hair_color", "hair_length", "eye_color", "user_appearance"]
        appearance_updates = {k: fields[k] for k in appearance_keys if k in fields}
        if appearance_updates:
            profile.update(appearance_updates)
        save_user_profile(profile)
        return {"status": "success", "key": key}
    else:
        raise HTTPException(status_code=400, detail="Unknown key")

    return {"status": "success", "key": key, "value": value}


@router.get("/{user_id}/{key}")
def get_data(key: str) -> Dict[str, Any]:
    """Gibt den Wert für einen bestimmten Key zurück"""
    # Verwende die Model-Funktionen für bekannte Keys
    if key == "user_name":
        value = get_user_name()
    elif key == "chat_partner":
        from app.models.account import get_chat_partner
        value = get_chat_partner()
    elif key == "character_personality":
        current_agent = get_current_agent()
        value = get_character_personality(current_agent) if current_agent else ""
    elif key == "character_appearance":
        current_agent = get_current_agent()
        value = get_character_appearance(current_agent) if current_agent else ""
        resolved = value
        if value and current_agent and "{" in value:
            try:
                profile = get_character_profile(current_agent)
                template_name = profile.get("template", "human-default")
                template = get_template(template_name)
                if template:
                    resolved = resolve_profile_tokens(value, profile, template, "character_appearance")
            except Exception:
                pass
        return {"key": key, "value": value, "resolved": resolved}
    elif key == "birthdate":
        from app.models.account import get_user_profile as _gup2
        value = _gup2().get("birthdate", "")
    elif key == "character_address_form":
        current_agent = get_current_agent()
        if current_agent:
            value = get_character_address_form(current_agent)
        else:
            value = ""
    elif key == "theme":
        value = get_user_theme()
    elif key == "user_profile":
        from app.models.account import get_user_profile as _gup, get_user_appearance
        profile = _gup()
        # Start with all profile fields (template-driven, future-proof)
        value = dict(profile)
        # Remove sensitive / internal fields
        for _k in ("_user_id", "password_hash"):
            value.pop(_k, None)
        # Ensure computed / default fields are present
        value.setdefault("system_language", "de")
        value.setdefault("translation_mode", "native")
        value["age"] = value.get("age", "")
        value["user_appearance_resolved"] = get_user_appearance()
        return {"key": key, "value": value}
    else:
        raise HTTPException(status_code=404, detail="Key nicht gefunden")

    return {"key": key, "value": value}
