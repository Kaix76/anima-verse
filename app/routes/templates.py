"""Character Template API Routes - Multiple templates in storage/templates/"""
from typing import Dict, Any, List
from fastapi import APIRouter, HTTPException, Request
from app.core.log import get_logger

logger = get_logger("templates")

from app.models.character_template import (
    list_templates,
    get_template,
    save_template,
    delete_template)
from app.models.character import get_character_profile, get_character_skill_config

router = APIRouter(prefix="/templates", tags=["templates"])


@router.get("/list")
async def list_all_templates(template_type: str = "") -> Dict[str, Any]:
    """List all available templates."""
    templates = list_templates(template_type=template_type or None)
    return {"templates": templates}


@router.get("/{template_name}")
async def get_template_route(template_name: str) -> Dict[str, Any]:
    """Get a template by name."""
    template = get_template(template_name)
    if template is None:
        raise HTTPException(status_code=404, detail=f"Template '{template_name}' not found")
    return template


@router.post("/{template_name}")
async def save_template_route(template_name: str, request: Request) -> Dict[str, Any]:
    """Create or update a template."""
    body = await request.json()
    template = body.get("template")

    if not template:
        raise HTTPException(status_code=400, detail="template required")

    ok = save_template(template_name, template)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to save template")

    return {"status": "ok", "name": template_name}


@router.delete("/{template_name}")
async def delete_template_route(template_name: str) -> Dict[str, Any]:
    """Delete a template (cannot delete 'human-default')."""
    if template_name == "human-default":
        raise HTTPException(status_code=400, detail="Cannot delete default template")
    ok = delete_template(template_name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Template '{template_name}' not found")
    return {"status": "ok"}


@router.get("/coverage/{character_name}")
async def get_template_coverage(character_name: str) -> Dict[str, Any]:
    """Pruefe welche Template-extra_activities in der Welt-Library vorhanden sind.

    Liefert pro extra_activity einen Status:
        - ``present`` (List[Dict])  — Activity ist in der Library (id matcht
          strict). Jeder Eintrag: {id, name}.
        - ``missing`` (List[str])   — Activity-id ist im Template referenziert
          aber nicht in der Library — Char wird sie nicht haben.

    Im Char-Editor als kleines Indikator-Panel rendern; via
    /coverage/{name}/seed kann der User die fehlenden Stubs anlegen.
    """
    profile = get_character_profile(character_name)
    if not profile:
        raise HTTPException(status_code=404, detail="Character not found")
    template_name = profile.get("template", "human-default")
    template = get_template(template_name)
    if template is None:
        raise HTTPException(status_code=404, detail=f"Template '{template_name}' not found")

    extras_raw = template.get("extra_activities") or []
    extras = [str(x).strip() for x in extras_raw if x and str(x).strip()]

    from app.models.activity_library import get_library_activity
    present: List[Dict[str, str]] = []
    missing: List[str] = []
    for eid in extras:
        act = get_library_activity(eid)
        if act:
            present.append({"id": act.get("id", eid),
                            "name": act.get("name") or eid})
        else:
            missing.append(eid)

    return {
        "character": character_name,
        "template": template_name,
        "extra_activities": extras,
        "present": present,
        "missing": missing,
    }


@router.post("/coverage/{character_name}/seed")
async def seed_template_coverage(character_name: str) -> Dict[str, Any]:
    """Legt fuer alle fehlenden Template-extra_activities Skelett-Eintraege
    in der Welt-Library an.

    Stub-Format: id == Template-Referenz, name == Title-Case der id,
    description "(Auto-generated stub for template '<X>')". Keine Effects,
    keine Conditions — der User soll die Werte im Game-Admin-Editor
    nachpflegen. Reload der Library wird automatisch invalidiert.
    """
    profile = get_character_profile(character_name)
    if not profile:
        raise HTTPException(status_code=404, detail="Character not found")
    template_name = profile.get("template", "human-default")
    template = get_template(template_name)
    if template is None:
        raise HTTPException(status_code=404, detail=f"Template '{template_name}' not found")

    extras_raw = template.get("extra_activities") or []
    extras = [str(x).strip() for x in extras_raw if x and str(x).strip()]

    from app.models.activity_library import (
        get_library_activity, save_library_activity, reload_library)
    created: List[str] = []
    for eid in extras:
        if get_library_activity(eid):
            continue
        # Title-Case fuer den Anzeige-Namen
        name = eid.replace("_", " ").strip().title() or eid
        stub = {
            "id": eid,
            "name": name,
            "description": f"(Auto-generated stub for template '{template_name}')",
            "category": "custom",
            "_group": "custom",
            "effects": {},
        }
        save_library_activity(stub, target_dir="world")
        created.append(eid)
    if created:
        reload_library()
    return {"created": created, "count": len(created)}


@router.get("/readiness/{character_name}")
async def get_readiness(character_name: str) -> Dict[str, Any]:
    """Berechnet abgeleitete Capabilities aus Kombinationen von Settings.

    Zeigt nur Features die sich aus mehreren Einstellungen ergeben — der User
    sieht einzelne Checks (personality.md vorhanden, Skill aktiv, etc.) im
    jeweiligen Tab selbst.

    Capabilities:
      - autonomy: Char kann selbststaendig handeln (Thoughts + LLM + Task)
      - instagram_autopost: Auto-Posten moeglich (Skills + Appearance)
      - cross_char: Kann andere Chars erreichen (talk_to/send_message + 2+ Chars)
      - faceswap: Bilder mit Face-Referenz (Flag + Profilbild + image-Skill)
    """

    profile = get_character_profile(character_name)
    if not profile:
        raise HTTPException(status_code=404, detail="Character not found")

    template_name = profile.get("template", "human-default")
    template = get_template(template_name)
    if template is None:
        raise HTTPException(status_code=404, detail=f"Template '{template_name}' not found")

    capabilities = _check_combination_features(character_name, profile, template)
    return {"character": character_name, "template": template_name,
            "capabilities": capabilities}


def _check_combination_features(character_name: str,
    profile: Dict[str, Any], template: Dict[str, Any]) -> Dict[str, Any]:
    """Pruefen welche abgeleiteten Faehigkeiten der Character hat."""
    from app.models.character import (
        get_character_dir, get_character_config,
        list_available_characters, get_character_skill_config)

    features = template.get("features", {}) or {}
    config = get_character_config(character_name) or {}
    char_dir = get_character_dir(character_name)

    def _skill_enabled(skill_id: str) -> bool:
        cfg = get_character_skill_config(character_name, skill_id) or {}
        return bool(cfg.get("enabled"))

    def _md_nonempty(rel: str) -> bool:
        p = char_dir / rel
        if not p.exists():
            return False
        text = p.read_text(encoding="utf-8")
        # Pruefen ob ueberhaupt Inhalt unter Headings steht
        for line in text.splitlines():
            if not line.strip() or line.strip().startswith("#"):
                continue
            return True
        return False

    # 1. Autonomy: thoughts_enabled + chat-LLM + tasks.md non-empty
    autonomy_missing = []
    if not config.get("thoughts_enabled"):
        autonomy_missing.append({"key": "thoughts_enabled",
                                 "label": "Thoughts im Config-Tab aktivieren"})
    if not _md_nonempty("soul/tasks.md"):
        autonomy_missing.append({"key": "tasks.md",
                                 "label": "Aufgaben in soul/tasks.md eintragen"})

    # 2. Instagram-Autopost
    insta_missing = []
    if not _skill_enabled("instagram"):
        insta_missing.append({"key": "instagram",
                              "label": "Instagram-Skill im Skills-Tab aktivieren"})
    if not _skill_enabled("image_generation"):
        insta_missing.append({"key": "image_generation",
                              "label": "Image-Generation-Skill aktivieren"})
    if not (profile.get("character_appearance") or "").strip():
        insta_missing.append({"key": "character_appearance",
                              "label": "Appearance im Charakter-Tab setzen"})

    # 4. Cross-Char Kommunikation
    cross_missing = []
    has_comm_skill = _skill_enabled("talk_to") or _skill_enabled("send_message")
    if not has_comm_skill:
        cross_missing.append({"key": "talk_to_or_send_message",
                              "label": "TalkTo oder SendMessage Skill aktivieren"})
    try:
        all_chars = list_available_characters()
        n_chars = sum(1 for c in all_chars if c)
    except Exception:
        n_chars = 1
    if n_chars < 2:
        cross_missing.append({"key": "min_chars",
                              "label": "Mindestens 2 Charaktere im System noetig"})

    # 5. Faceswap
    fs_missing = []
    img_cfg = get_character_skill_config(character_name, "image_generation") or {}
    if not img_cfg.get("faceswap_enabled"):
        fs_missing.append({"key": "faceswap_enabled",
                           "label": "Faceswap in Image-Generation-Skill aktivieren"})
    if not _skill_enabled("image_generation"):
        fs_missing.append({"key": "image_generation",
                           "label": "Image-Generation-Skill aktivieren"})
    if not (profile.get("profile_image") or "").strip():
        fs_missing.append({"key": "profile_image",
                           "label": "Profilbild als Face-Referenz setzen"})

    return {
        "autonomy":             {"ready": not autonomy_missing, "missing": autonomy_missing,
                                  "label": "Autonomy"},
        "instagram_autopost":   {"ready": not insta_missing, "missing": insta_missing,
                                  "label": "Instagram"},
        "cross_char":           {"ready": not cross_missing, "missing": cross_missing,
                                  "label": "Cross-Char"},
        "faceswap":             {"ready": not fs_missing, "missing": fs_missing,
                                  "label": "Faceswap"},
    }
