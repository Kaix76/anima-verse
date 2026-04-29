"""Admin Settings Routes — JSON-based configuration management."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from typing import Any, Dict
import httpx

from app.core.log import get_logger
from app.core import config
from app.core.config_schema import get_schema
from app.core.auth_dependency import require_admin

logger = get_logger("admin_settings")

router = APIRouter(prefix="/admin", tags=["admin-settings"],
                   dependencies=[Depends(require_admin)])


# ── API Endpoints ──

@router.get("/settings", response_class=HTMLResponse)
async def settings_page():
    """Serve the admin settings HTML page."""
    return HTMLResponse(content=_build_settings_html())


@router.get("/users", response_class=HTMLResponse)
async def users_page():
    """Serve the user-management HTML page."""
    return HTMLResponse(
        content=_build_users_html(),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/outfit-rules", response_class=HTMLResponse)
async def outfit_rules_page():
    """Serve the outfit-rules admin HTML page."""
    return HTMLResponse(content=_build_outfit_rules_html())


@router.get("/outfit-rules/data")
async def outfit_rules_get(user=Depends(require_admin)):
    """Liefert shared/config/outfit_rules.json + Liste gueltiger Slots."""
    import json as _json
    from app.models.inventory import VALID_PIECE_SLOTS
    from app.core.paths import get_config_dir
    path = get_config_dir() / "outfit_rules.json"
    data = {"outfit_types": {}}
    if path.exists():
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "outfit_types": data.get("outfit_types") or {},
        "valid_slots": list(VALID_PIECE_SLOTS),
    }


@router.put("/outfit-rules/data")
async def outfit_rules_save(request: Request, user=Depends(require_admin)):
    """Speichert outfit_rules.json und invalidiert den Rules-Cache."""
    import json as _json
    from app.models.inventory import VALID_PIECE_SLOTS
    from app.core.paths import get_config_dir
    from app.core.outfit_rules import reload_rules

    body = await request.json()
    incoming = body.get("outfit_types") or {}
    valid = set(VALID_PIECE_SLOTS)

    cleaned = {}
    for otype, entry in incoming.items():
        key = (otype or "").strip()
        if not key:
            continue
        if not isinstance(entry, dict):
            continue
        req = entry.get("required") or []
        if not isinstance(req, list):
            continue
        entry_out = {"required": [s for s in req if s in valid]}
        description = (entry.get("description") or "").strip()
        if description:
            entry_out["description"] = description
        cleaned[key] = entry_out

    path = get_config_dir() / "outfit_rules.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"outfit_types": cleaned}
    path.write_text(_json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    reload_rules()
    return {"status": "ok", "outfit_types": cleaned}


@router.get("/settings/data")
async def settings_data(user=Depends(require_admin)):
    """Return full config with sensitive fields masked.

    Leere Felder werden mit Schema-Defaults vorbelegt, damit der User
    sofort sieht welcher Fallback-Wert greift.
    """
    import copy
    data = copy.deepcopy(config.get_all())
    _apply_schema_defaults(data)
    return config.mask_sensitive(data)


@router.get("/settings/raw")
async def settings_raw(user=Depends(require_admin)):
    """Return full config without masking (for save round-trip).

    Empty fields are pre-filled with their schema default so the admin UI
    immediately shows what value would apply if left untouched.
    """
    import copy
    data = copy.deepcopy(config.get_all())
    _apply_schema_defaults(data)
    return data


@router.post("/settings/save")
async def settings_save(request: Request, user=Depends(require_admin)):
    """Save config. Fields with masked values (***...) are kept from current config."""
    new_data = await request.json()

    # Merge: keep current values for masked sensitive fields
    current = config.get_all()
    merged = _merge_sensitive(new_data, current)

    # Structural validation (e.g. llm_routing order uniqueness)
    err = _validate_llm_routing(merged.get("llm_routing"))
    if err:
        raise HTTPException(status_code=400, detail=err)

    # Diagnose: was kommt im llm_routing wirklich an?
    try:
        _routing_in = merged.get("llm_routing") or []
        _task_log = []
        for _e in _routing_in:
            if not isinstance(_e, dict):
                continue
            for _t in (_e.get("tasks") or []):
                if isinstance(_t, dict) and _t.get("task"):
                    _task_log.append(
                        f"{_t.get('task')}@{_t.get('order','?')}->"
                        f"{_e.get('provider','?')}/{_e.get('model','?')}")
        from app.core.log import get_logger as _gl
        _gl("admin_settings").info(
            "settings_save: llm_routing %d Eintraege, %d Task-Mappings: %s",
            len(_routing_in), len(_task_log), _task_log)
    except Exception:
        pass

    _autofill_imagegen_defaults(merged)

    config.save(merged)
    # Env sofort aktualisieren — vermeidet Server-Restart-Pflicht fuer Felder
    # die ueber os.environ.get() gelesen werden (z.B. COMFY_MULTISWAP_UNET).
    try:
        config._flatten_to_env(merged)
    except Exception as _ee:
        # Nicht hart fehlschlagen — Save selbst war erfolgreich.
        from app.core.log import get_logger as _gl
        _gl("admin_settings").warning("env-flatten nach Save fehlgeschlagen: %s", _ee)
    return {"status": "success", "message": "Configuration saved (env updated)."}


@router.get("/settings/llm-tasks")
async def settings_llm_tasks(user=Depends(require_admin)):
    """Liefert die Liste bekannter LLM-Task-Typen fuer den Admin-UI-Selector."""
    from app.core.llm_tasks import TASK_TYPES, CATEGORY_LABELS
    return [
        {
            "id": tid,
            "label": t.get("label", tid),
            "category": t.get("category", ""),
            "category_label": CATEGORY_LABELS.get(str(t.get("category", "")), ""),
        }
        for tid, t in TASK_TYPES.items()
    ]


@router.get("/settings/llm-task-state")
async def llm_task_state_get(user=Depends(require_admin)):
    from app.core.llm_task_state import (
        disabled_tasks, runtime_disabled_tasks, get_presets)
    return {
        "disabled": disabled_tasks(),
        "runtime_disabled": runtime_disabled_tasks(),
        "presets": get_presets(),
    }


@router.post("/settings/llm-task-state/runtime-preset")
async def llm_task_state_runtime_preset(request: Request, user=Depends(require_admin)):
    """Aktiviert ein Preset als Runtime-Disable (nicht persistent)."""
    data = await request.json()
    preset = (data.get("preset") or "").strip()
    from app.core.llm_task_state import activate_preset_runtime, clear_runtime
    if not preset or preset == "none":
        clear_runtime()
        return {"status": "cleared"}
    tasks = activate_preset_runtime(preset)
    return {"status": "ok", "preset": preset, "disabled": tasks}


def _autofill_imagegen_defaults(cfg: Dict[str, Any]) -> None:
    """When the user has at least one image-gen backend, fill empty
    outfit/expression/location default-backend fields with the first enabled
    backend. Does not overwrite existing selections."""
    img = cfg.get("image_generation") or {}
    backends = img.get("backends") or []
    if not isinstance(backends, list) or not backends:
        return
    chosen = next(
        (b.get("name") for b in backends
         if isinstance(b, dict) and b.get("enabled") and b.get("name")),
        None,
    ) or next(
        (b.get("name") for b in backends
         if isinstance(b, dict) and b.get("name")),
        None,
    )
    if not chosen:
        return
    target = f"backend:{chosen}"
    for field in ("outfit_imagegen_default",
                  "expression_imagegen_default",
                  "location_imagegen_default"):
        if not img.get(field):
            img[field] = target
    cfg["image_generation"] = img


def _validate_llm_routing(routing) -> str:
    """Prueft llm_routing: pro (task, order) darf es nur einen Eintrag geben.

    Returns leere String wenn OK, sonst Fehlermeldung.
    """
    if not isinstance(routing, list):
        return ""
    seen: dict = {}  # (task, order) -> entry_index
    for idx, entry in enumerate(routing):
        if not isinstance(entry, dict):
            continue
        tasks = entry.get("tasks") or []
        if not isinstance(tasks, list):
            continue
        for t in tasks:
            if not isinstance(t, dict):
                continue
            task_id = t.get("task")
            order = t.get("order")
            if not task_id or order is None:
                continue
            key = (task_id, int(order))
            if key in seen:
                return (f"LLM Routing: task '{task_id}' mit order {order} "
                        f"ist doppelt (Eintrag #{seen[key]+1} und #{idx+1}).")
            seen[key] = idx
    return ""


@router.get("/settings/schema")
async def settings_schema(user=Depends(require_admin)):
    """Return field schema for UI rendering."""
    return get_schema()


@router.get("/settings/imagegen-targets")
async def imagegen_targets(user=Depends(require_admin)):
    """Liefert die kombinierte Liste der Image-Gen-Targets fuer Admin-Selects:
    ComfyUI-Workflows + Cloud-Backends (Together/CivitAI/Mammouth).

    Format: [{"value": "workflow:Z-Image", "label": "...", "type": "workflow", "available": True}, ...]
    """
    try:
        from app.core.dependencies import get_skill_manager
        sm = get_skill_manager()
        img = sm.get_skill("image_generation")
        if not img:
            return {"targets": []}
    except Exception as e:
        return {"targets": [], "error": str(e)}

    out = []
    # ComfyUI-Workflows zuerst (sortiert nach Name)
    for wf in sorted(img.comfy_workflows, key=lambda w: w.name.lower()):
        # Verfuegbarkeit: existiert mind. 1 kompatibles, available, instance_enabled Backend?
        compat = wf.compatible_backends or []
        avail = False
        for b in img.backends:
            if not b.instance_enabled or not b.available:
                continue
            if b.api_type != "comfyui":
                continue
            if compat and b.name not in compat:
                continue
            avail = True
            break
        out.append({
            "value": f"workflow:{wf.name}",
            "label": f"ComfyUI: {wf.name}",
            "type": "workflow",
            "available": avail,
        })
    # Cloud-Backends (non-comfyui)
    for b in img.backends:
        if b.api_type == "comfyui":
            continue
        if not b.instance_enabled:
            continue
        out.append({
            "value": f"backend:{b.name}",
            "label": f"{b.name} ({b.api_type})",
            "type": "backend",
            "available": bool(b.available),
        })
    return {"targets": out}


@router.get("/settings/imagegen-backends/{backend_name}/models")
async def imagegen_backend_models(backend_name: str, user=Depends(require_admin)):
    """Liefert Modellliste fuer ein Image-Generation-Backend (Cloud).

    - Together: holt Live-Liste via /v1/models (image-Modelle filtern)
    - CivitAI/Mammouth: aktuell nur das konfigurierte backend.model
    - ComfyUI: leitet auf comfyui-models um
    """
    img_gen = config.get("image_generation", {}) or {}
    backends = img_gen.get("backends", []) or []
    b = next((x for x in backends if x.get("name") == backend_name), None)
    if not b:
        raise HTTPException(404, f"Backend '{backend_name}' nicht gefunden")
    api_type = (b.get("api_type") or "").lower()
    api_key = b.get("api_key", "")
    api_url = (b.get("api_url") or "").rstrip("/")
    cur_model = b.get("model", "")
    models: list = []
    try:
        if api_type == "together":
            base = api_url if api_url.endswith("/v1") else (api_url + "/v1")
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{base}/models",
                                        headers={"Authorization": f"Bearer {api_key}"})
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                    for m in items:
                        if not isinstance(m, dict):
                            continue
                        if m.get("type") and m.get("type") != "image":
                            continue
                        mid = m.get("id") or m.get("name")
                        if mid:
                            models.append(mid)
            models.sort()
        elif api_type == "civitai":
            # CivitAI hat keine sinnvolle Modell-Liste via API — nur das
            # konfigurierte AIR URN als einzige Option zurueckgeben.
            if cur_model:
                models = [cur_model]
        elif api_type == "mammouth":
            if cur_model:
                models = [cur_model]
        elif api_type == "comfyui":
            # ComfyUI: Modelle (Checkpoints + UNets) aus dem ImageGen-Skill
            # Cache holen — der enthaelt die per-Backend gescannten Modelle.
            try:
                from app.core.dependencies import get_skill_manager
                _sm = get_skill_manager()
                _img = _sm.get_skill("image_generation")
                if _img and getattr(_img, "_model_cache_loaded", False):
                    _ckpt = _img._cached_checkpoints_by_service.get(backend_name, [])
                    _unet = _img._cached_unet_models_by_service.get(backend_name, [])
                    models = sorted(set(_ckpt + _unet))
            except Exception as _e:
                logger.warning("ComfyUI-Models-Cache nicht lesbar: %s", _e)
    except Exception as e:
        return {"backend": backend_name, "models": [], "error": str(e)}
    # cur_model immer dabei haben (auch wenn es nicht in der Liste ist)
    if cur_model and cur_model not in models:
        models.insert(0, cur_model)
    return {"backend": backend_name, "models": models, "current": cur_model}


@router.get("/settings/providers/{provider_name}/models")
async def provider_models(provider_name: str, user=Depends(require_admin)):
    """Fetch available models from a provider (live query)."""
    providers = config.get("providers", [])
    provider = None
    for p in providers:
        if p.get("name") == provider_name:
            provider = p
            break
    if not provider:
        raise HTTPException(404, f"Provider '{provider_name}' not found")

    api_base = provider.get("api_base", "")
    api_key = provider.get("api_key", "not-needed")
    ptype = provider.get("type", "openai")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if ptype == "ollama":
                # Ollama: /api/tags
                base = api_base.rstrip("/v1").rstrip("/")
                resp = await client.get(f"{base}/api/tags")
                resp.raise_for_status()
                data = resp.json()
                models = [m.get("name", "") for m in data.get("models", [])]
            else:
                # OpenAI-compatible: /v1/models
                headers = {}
                if api_key and api_key != "not-needed":
                    headers["Authorization"] = f"Bearer {api_key}"
                base = api_base.rstrip("/")
                if not base.endswith("/v1"):
                    base += "/v1"
                resp = await client.get(f"{base}/models", headers=headers)
                resp.raise_for_status()
                data = resp.json()
                # OpenAI: {"data": [{id, ...}]}, Together.ai: [{id, ...}]
                items = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                models = []
                for m in items:
                    if isinstance(m, dict):
                        mid = m.get("id") or m.get("name") or ""
                        if mid:
                            models.append(mid)
                    elif isinstance(m, str):
                        models.append(m)

        models.sort()
        return {"provider": provider_name, "models": models}
    except Exception as e:
        return {"provider": provider_name, "models": [], "error": str(e)}


@router.post("/settings/validate")
async def settings_validate(request: Request, user=Depends(require_admin)):
    """Validate config and return list of issues."""
    from app.core.config_validator import validate_config
    data = await request.json()
    issues = validate_config(data)
    return {"issues": issues, "errors": sum(1 for i in issues if i["level"] == "error"), "warnings": sum(1 for i in issues if i["level"] == "warning")}


@router.post("/settings/restart")
async def settings_restart(user=Depends(require_admin)):
    """Trigger a skill/service reload (same as existing reload endpoint)."""
    from app.core.dependencies import reload_skill_manager
    result = reload_skill_manager()
    return {"status": "success", "result": result}


@router.post("/settings/memory-consolidate")
async def settings_memory_consolidate(request: Request, user=Depends(require_admin)):
    """Triggert Memory-Konsolidierung sofort.

    Body (alles optional):
      - character: Wenn gesetzt, NUR fuer diesen Character. Sonst: alle.
      - phase2_iterations: Wieviel mal Phase 2 hintereinander pro Character laufen
        soll (Default 1). Pro Iteration werden bis zu 3 Tage Episodics
        konsolidiert. Hilfreich um grosse Backlogs in einem Rutsch abzubauen.
    """
    body = await request.json() if request.headers.get('content-type','').startswith('application/json') else {}
    character = (body.get('character') or '').strip()
    iterations = max(1, min(20, int(body.get('phase2_iterations', 1))))

    from app.core.background_queue import get_background_queue
    from app.models.character import list_available_characters

    targets = [character] if character else list_available_characters()
    bq = get_background_queue()
    submitted = 0
    for ch in targets:
        for _ in range(iterations):
            bq.submit(
                task_type="memory_consolidation",
                payload={"character_name": ch},
                priority=30,
                agent_name=ch,
                deduplicate=False)  # explizit kein dedup damit alle iter laufen
            submitted += 1
    return {"status": "success", "submitted": submitted, "characters": len(targets), "iterations": iterations}


@router.get("/settings/comfyui-models")
async def comfyui_models(user=Depends(require_admin)):
    """Return cached ComfyUI checkpoints and LoRAs."""
    try:
        from app.core.dependencies import get_skill_manager
        sm = get_skill_manager()
        imagegen = sm.get_skill("image_generation") if sm else None
        if not imagegen:
            return {"checkpoints": [], "loras": []}
        return {
            "checkpoints": imagegen.get_cached_checkpoints(),
            "loras": imagegen.get_cached_loras(),
            "clip_models": imagegen.get_cached_clip_models(),
        }
    except Exception as e:
        return {"checkpoints": [], "loras": [], "error": str(e)}


# ── Helpers ──

def _apply_schema_defaults(data: dict) -> None:
    """Fuellt leere Config-Felder mit Schema-Defaults vor.

    Iteriert ueber SECTIONS aus config_schema und traegt fehlende oder leere
    Werte ein, wenn ein 'default' definiert ist — damit der Admin-User sofort
    sieht, welcher Fallback aktiv waere.
    """
    schema = get_schema()
    for section_key, section_def in schema.items():
        is_array = section_def.get("is_array", False)
        fields = section_def.get("fields", {})
        if is_array:
            items = data.get(section_key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                _fill_defaults(item, fields)
                for nested_key, nested_def in fields.items():
                    if isinstance(nested_def, dict) and nested_def.get("is_array"):
                        nested_items = item.get(nested_key)
                        if isinstance(nested_items, list):
                            nested_fields = nested_def.get("item_fields", {})
                            for ni in nested_items:
                                if isinstance(ni, dict):
                                    _fill_defaults(ni, nested_fields)
        else:
            section_data = data.get(section_key)
            if not isinstance(section_data, dict):
                section_data = data
            _fill_defaults(section_data, fields)


def _fill_defaults(obj: dict, fields: dict) -> None:
    """Setzt fehlende/leere Werte in obj auf den field-default."""
    for key, field_def in fields.items():
        if not isinstance(field_def, dict):
            continue
        default = field_def.get("default")
        if default is None:
            continue
        current = obj.get(key)
        if current is None or current == "":
            obj[key] = default
            logger.debug("Config-Default gesetzt: %s = %r", key, default)


def _merge_sensitive(new: Any, current: Any) -> Any:
    """Recursively merge, keeping current values where new has masked placeholders.

    WICHTIG: Keys die im neuen Dict FEHLEN wurden bewusst geloescht und
    werden NICHT aus current wiederhergestellt. Nur bei Leaf-Werten mit
    '***'-Maskierung greift der Sensitive-Schutz.
    """
    if isinstance(new, dict) and isinstance(current, dict):
        result = {}
        # Nur Keys aus new uebernehmen — fehlende Keys = geloescht.
        for key in new:
            if key in current:
                result[key] = _merge_sensitive(new[key], current[key])
            else:
                result[key] = new[key]
        return result
    if isinstance(new, list) and isinstance(current, list):
        return [
            _merge_sensitive(new[i], current[i]) if i < len(current) else new[i]
            for i in range(len(new))
        ]
    # If new value is a masked placeholder, keep current
    if isinstance(new, str) and new.startswith("***"):
        return current
    return new


def _build_settings_html() -> str:
    """Build the complete admin settings HTML page."""
    return '''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin Settings</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0d1117; color: #c9d1d9; display: flex; height: 100vh; overflow: hidden; }

/* ── Sidebar ── */
.sidebar {
    width: 220px; min-width: 220px; background: #161b22; border-right: 1px solid #30363d;
    overflow-y: auto; padding: 12px 0;
}
.sidebar h1 { font-size: 15px; padding: 8px 16px; color: #58a6ff; border-bottom: 1px solid #30363d; margin-bottom: 8px; }
.sidebar a {
    display: block; padding: 7px 16px; color: #8b949e; text-decoration: none;
    font-size: 13px; border-left: 3px solid transparent; transition: all 0.15s;
}
.sidebar a:hover { color: #c9d1d9; background: #1c2128; }
.sidebar a.active { color: #58a6ff; border-left-color: #58a6ff; background: #1c2128; }
.sidebar .nav-icon { margin-right: 6px; }
.sidebar .nav-section-label {
    padding: 10px 16px 4px; margin-top: 10px; font-size: 11px; font-weight: 700;
    color: #8b949e; text-transform: uppercase; letter-spacing: 0.8px;
    border-top: 1px solid #30363d;
}
.sidebar .nav-section-label:first-of-type { margin-top: 4px; border-top: none; }

/* ── Main ── */
.main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.toolbar {
    background: #161b22; border-bottom: 1px solid #30363d;
    padding: 10px 20px; display: flex; gap: 10px; align-items: center;
}
.toolbar .spacer { flex: 1; }
.content { flex: 1; overflow-y: auto; padding: 20px; }

/* ── Buttons ── */
.btn {
    background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
    padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px;
    display: inline-flex; align-items: center; gap: 4px;
}
.btn:hover { background: #30363d; }
.btn-primary { background: #238636; border-color: #2ea043; color: #fff; }
.btn-primary:hover { background: #2ea043; }
.btn-danger { background: #da3633; border-color: #f85149; color: #fff; }
.btn-danger:hover { background: #b62324; }
.btn-sm { padding: 4px 8px; font-size: 12px; }

/* ── Section ── */
.section { display: none; }
.section.active { display: block; }
.section-title { font-size: 18px; font-weight: 600; margin-bottom: 16px; color: #e6edf3; }
.subsection { margin: 16px 0; padding: 16px; background: #161b22; border: 1px solid #30363d; border-radius: 8px; }
.subsection-title { font-size: 14px; font-weight: 600; margin-bottom: 12px; color: #58a6ff; }

/* ── Form Fields ── */
.field { margin-bottom: 12px; display: flex; align-items: flex-start; gap: 12px; }
.field label { width: 180px; min-width: 180px; font-size: 13px; color: #8b949e; padding-top: 7px; text-align: right; }
.field .input-wrap { flex: 1; }
.field input[type="text"], .field input[type="number"], .field input[type="password"],
.field select, .field textarea {
    width: 100%; background: #0d1117; color: #c9d1d9; border: 1px solid #30363d;
    padding: 6px 10px; border-radius: 6px; font-size: 13px; font-family: inherit;
}
.field input:focus, .field select:focus, .field textarea:focus { border-color: #58a6ff; outline: none; }
.field textarea { min-height: 60px; resize: vertical; }
.field .desc { font-size: 11px; color: #6e7681; margin-top: 3px; }
.field input[type="checkbox"] { margin-top: 8px; }

/* Toggle for password */
.pw-wrap { position: relative; }
.pw-wrap input { padding-right: 36px; }
.pw-toggle {
    position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
    background: none; border: none; color: #8b949e; cursor: pointer; font-size: 14px;
}

/* ── Array Items (Providers, Backends) ── */
.array-item {
    border: 1px solid #30363d; border-radius: 8px; margin-bottom: 12px;
    background: #0d1117;
}
.array-item-header {
    display: flex; align-items: center; padding: 8px 12px;
    background: #161b22; border-radius: 8px 8px 0 0; cursor: pointer;
    border-bottom: 1px solid #30363d;
}
.array-item-header .title { flex: 1; font-weight: 600; font-size: 13px; }
.array-item-header .badge { font-size: 11px; color: #8b949e; margin-right: 8px; }
.array-item-body { padding: 12px; display: none; }
.array-item.open .array-item-body { display: block; }
.array-item-header .chevron { transition: transform 0.2s; color: #8b949e; }
.array-item.open .array-item-header .chevron { transform: rotate(90deg); }

/* LoRA rows */
.lora-row { display: flex; gap: 8px; margin-bottom: 6px; align-items: center; }
.lora-row input:first-child { flex: 3; }
.lora-row input:last-child { flex: 1; max-width: 80px; }

/* GPU rows */
.gpu-row { display: flex; gap: 8px; margin-bottom: 6px; align-items: center; }
.gpu-row input, .gpu-row select { flex: 1; }

/* ── Toast ── */
.toast {
    position: fixed; bottom: 20px; right: 20px; padding: 12px 20px;
    border-radius: 8px; font-size: 13px; z-index: 1000;
    opacity: 0; transition: opacity 0.3s; pointer-events: none;
}
.toast.show { opacity: 1; }
.toast.success { background: #238636; color: #fff; }
.toast.error { background: #da3633; color: #fff; }

/* ── Loading ── */
.loading { text-align: center; padding: 60px; color: #8b949e; }
.spinner { display: inline-block; width: 24px; height: 24px; border: 3px solid #30363d; border-top-color: #58a6ff; border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* Status indicator */
.status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; }
.status-dot.on { background: #3fb950; }
.status-dot.off { background: #6e7681; }

/* Validation results */
.validate-results { margin: 16px 0; padding: 16px; background: #161b22; border: 1px solid #30363d; border-radius: 8px; }
.validate-results h3 { font-size: 14px; margin-bottom: 10px; }
.validate-results.has-errors h3 { color: #f85149; }
.validate-results.all-ok h3 { color: #3fb950; }
.validate-issue { padding: 6px 10px; margin: 4px 0; border-radius: 4px; font-size: 13px; display: flex; align-items: flex-start; gap: 8px; }
.validate-issue.error { background: #da363322; border-left: 3px solid #f85149; }
.validate-issue.warning { background: #d2992222; border-left: 3px solid #d29922; }
.validate-issue .badge { font-size: 11px; font-weight: 600; padding: 1px 6px; border-radius: 3px; white-space: nowrap; }
.validate-issue.error .badge { background: #da363344; color: #f85149; }
.validate-issue.warning .badge { background: #d2992244; color: #d29922; }
.validate-issue .section-link { color: #58a6ff; cursor: pointer; font-size: 12px; text-decoration: underline; margin-left: auto; white-space: nowrap; }

/* Embedded iframe for tool pages */
.content iframe { width: 100%; height: 100%; border: none; }
.content.iframe-mode { padding: 0; overflow: hidden; }
</style>
</head>
<body>

<nav class="sidebar">
    <h1>Admin</h1>
    <div class="nav-section-label">Server-Einstellungen</div>
    <div id="nav-links"></div>
    <div class="nav-section-label">Verwaltung</div>
    <a href="#" data-section="_users" onclick="event.preventDefault(); activateIframe('_users', '/admin/users', 'User-Verwaltung')"><span class="nav-icon">👥</span> User-Verwaltung</a>
    <a href="#" data-section="_outfit_rules" onclick="event.preventDefault(); activateIframe('_outfit_rules', '/admin/outfit-rules', 'Outfit-Regeln')"><span class="nav-icon">👗</span> Outfit-Regeln</a>
    <a href="#" data-section="_models" onclick="event.preventDefault(); activateIframe('_models', '/admin/models', 'Model Capabilities')"><span class="nav-icon">🧩</span> Model Capabilities</a>
    <div class="nav-section-label">Logs & Monitoring</div>
    <a href="#" data-section="_dashboard" onclick="event.preventDefault(); activateIframe('_dashboard', '/dashboard', 'Dashboard')"><span class="nav-icon">📊</span> Dashboard</a>
    <a href="#" data-section="_llm_log" onclick="event.preventDefault(); activateIframe('_llm_log', '/logs/llm', 'LLM Log')"><span class="nav-icon">📝</span> LLM Log</a>
    <a href="#" data-section="_image_log" onclick="event.preventDefault(); activateIframe('_image_log', '/logs/image-prompts', 'Image Prompt Log')"><span class="nav-icon">🖼</span> Image Prompt Log</a>
</nav>

<div class="main">
    <div class="toolbar" id="settings-toolbar">
        <button class="btn btn-primary" onclick="saveConfig()" id="btn-save">Save</button>
        <button class="btn" onclick="validateConfig()" id="btn-validate" style="border-color:#d29922; color:#d29922;">Validate</button>
        <button class="btn" onclick="reloadServices()">Reload Services</button>
        <span class="spacer"></span>
        <span id="status-msg" style="font-size: 12px; color: #8b949e;"></span>
    </div>
    <div class="content" id="content">
        <div class="loading"><div class="spinner"></div><p style="margin-top: 12px;">Loading configuration...</p></div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
let CONFIG = {};
let SCHEMA = {};
let PROVIDERS_CACHE = {};
let ACTIVE_SECTION = null;

// ── Init ──
async function init() {
    try {
        const [dataResp, schemaResp] = await Promise.all([
            fetch('/admin/settings/raw', { credentials: 'same-origin' }),
            fetch('/admin/settings/schema', { credentials: 'same-origin' })
        ]);
        if (dataResp.status === 401 || dataResp.status === 403) {
            const ret = encodeURIComponent(window.location.pathname + window.location.hash);
            window.location.href = '/?return=' + ret;
            return;
        }
        CONFIG = await dataResp.json();
        SCHEMA = await schemaResp.json();
        buildNav();
        // Activate first section
        const first = Object.keys(SCHEMA)[0];
        if (first) activateSection(first);
    } catch (e) {
        document.getElementById('content').innerHTML = '<div class="loading" style="color:#f85149;">Error loading config: ' + e.message + '</div>';
    }
}

function authHeaders() {
    // Cookie-basiert: Browser sendet Session-Cookie automatisch. Nur Content-Type explizit setzen.
    return { 'Content-Type': 'application/json' };
}

// ── Navigation ──
function buildNav() {
    const nav = document.getElementById('nav-links');
    nav.innerHTML = '';
    for (const [key, sec] of Object.entries(SCHEMA)) {
        const a = document.createElement('a');
        a.href = '#' + key;
        a.innerHTML = '<span class="nav-icon">' + (sec.icon || '') + '</span> ' + sec.label;
        a.dataset.section = key;
        a.onclick = (e) => { e.preventDefault(); activateSection(key); };
        nav.appendChild(a);
    }
}

function activateSection(key) {
    ACTIVE_SECTION = key;
    // Update nav
    document.querySelectorAll('.sidebar a').forEach(a => a.classList.remove('active'));
    const link = document.querySelector('.sidebar a[data-section="' + key + '"]');
    if (link) link.classList.add('active');
    // Show settings toolbar, restore content mode
    document.getElementById('settings-toolbar').style.display = 'flex';
    const content = document.getElementById('content');
    content.classList.remove('iframe-mode');
    // Render section
    renderSection(key);
}

function activateIframe(key, url, title) {
    ACTIVE_SECTION = key;
    // Update nav
    document.querySelectorAll('.sidebar a').forEach(a => a.classList.remove('active'));
    const link = document.querySelector('.sidebar a[data-section="' + key + '"]');
    if (link) link.classList.add('active');
    // Hide settings toolbar
    document.getElementById('settings-toolbar').style.display = 'none';
    // Load iframe
    const content = document.getElementById('content');
    content.classList.add('iframe-mode');
    content.innerHTML = '<iframe src="' + url + '" title="' + esc(title) + '"></iframe>';
}

// ── Render Section ──
function renderSection(key) {
    const sec = SCHEMA[key];
    const data = CONFIG[key] !== undefined ? CONFIG[key] : (sec.is_array ? [] : {});
    const content = document.getElementById('content');

    let html = '<div class="section active">';
    html += '<h1 class="section-title">' + (sec.icon || '') + ' ' + sec.label + '</h1>';

    // Top-level fields (skip for array sections — fields are rendered per item)
    if (sec.fields && !sec.is_array) {
        html += renderFields(sec.fields, data, key);
    }

    // Subsections
    if (sec.subsections) {
        for (const [subKey, sub] of Object.entries(sec.subsections)) {
            const subData = data[subKey] || {};
            html += '<div class="subsection">';
            html += '<div class="subsection-title">' + sub.label + '</div>';
            html += renderFields(sub.fields, subData, key + '.' + subKey);
            html += '</div>';
        }
    }

    // Sub-arrays (like backends, comfyui_workflows)
    if (sec.sub_arrays) {
        for (const [arrKey, arrDef] of Object.entries(sec.sub_arrays)) {
            html += '<div class="subsection">';
            html += '<div class="subsection-title" style="display:flex; align-items:center; justify-content:space-between;">';
            html += arrDef.label;
            html += '<button class="btn btn-sm" onclick="addArrayItem(\\'' + key + '.' + arrKey + '\\', \\'' + (arrDef.is_dict ? 'dict' : 'array') + '\\')">+ Add</button>';
            html += '</div>';
            if (arrDef.is_dict) {
                html += renderDictItems(arrDef, data[arrKey] || {}, key + '.' + arrKey);
            } else {
                html += renderArrayItems(arrDef, data[arrKey] || [], key + '.' + arrKey);
            }
            html += '</div>';
        }
    }

    // Array sections (providers)
    if (sec.is_array) {
        if (key === 'llm_routing') {
            // Zweispaltig: links Editor, rechts Task-View (read-only)
            html += '<div style="display:grid; grid-template-columns: 1fr 1fr; gap:20px;">';
            html += '<div>';
            html += '<div style="margin-bottom: 12px;">';
            html += '<button class="btn btn-sm" onclick="addArrayItem(\\'' + key + '\\', \\'array\\')">+ Add LLM</button>';
            html += '</div>';
            html += renderArrayItems(sec, data || [], key);
            html += '</div>';
            html += '<div>';
            html += '<div class="subsection-title" style="margin-bottom:8px;">Sichtweise pro Task</div>';
            html += '<div id="llm-task-view"><div class="desc">Lade...</div></div>';
            html += '</div>';
            html += '</div>';
            setTimeout(() => renderLlmTaskView(data || []), 0);
        } else {
            html += '<div style="margin-bottom: 12px;">';
            html += '<button class="btn btn-sm" onclick="addArrayItem(\\'' + key + '\\', \\'array\\')">+ Add ' + sec.label + '</button>';
            html += '</div>';
            html += renderArrayItems(sec, data || [], key);
        }
    }

    html += '</div>';
    content.innerHTML = html;
    // image_preview-Felder Meta nachladen (kein <script> via innerHTML moeglich)
    populateImagePreviewMetas();
}

async function populateImagePreviewMetas() {
    const els = document.querySelectorAll('.image-preview-meta[data-meta-url]');
    for (const el of els) {
        const url = el.dataset.metaUrl;
        if (!url) continue;
        try {
            const r = await fetch(url);
            if (!r.ok) continue;
            const d = await r.json();
            if (d.has_frame && d.bbox && d.frame_size) {
                el.textContent = 'Frame ' + d.frame_size[0] + '×' + d.frame_size[1]
                    + ' — Window ' + d.bbox.w + '×' + d.bbox.h
                    + ' @ (' + d.bbox.x + ',' + d.bbox.y + ')'
                    + (d.generated_at ? ' — generiert ' + d.generated_at : '');
            } else {
                el.textContent = 'Noch nicht generiert.';
            }
        } catch (e) { /* ignore */ }
    }
}

async function renderLlmTaskView(entries) {
    const tasks = await loadLlmTasks();
    const view = document.getElementById('llm-task-view');
    if (!view) return;

    // State vom Server laden (runtime + persistent + presets)
    let state = { disabled: [], runtime_disabled: [], presets: {} };
    try {
        const r = await fetch('/admin/settings/llm-task-state', { credentials: 'same-origin' });
        if (r.ok) state = await r.json();
    } catch (e) {}

    // Persistent disabled aus CONFIG (UI-Quelle fuer Toggles)
    const persistentDisabled = new Set(
        ((CONFIG.llm_task_state || {}).disabled_tasks || [])
    );
    const runtimeDisabled = new Set(state.runtime_disabled || []);

    // task_id -> [{order, provider, model}]
    const byTask = {};
    for (const entry of (entries || [])) {
        if (!entry || typeof entry !== 'object') continue;
        const prov = entry.provider || '';
        const mod = entry.model || '';
        for (const t of (entry.tasks || [])) {
            if (!t || !t.task) continue;
            (byTask[t.task] = byTask[t.task] || []).push({
                order: t.order || 999,
                provider: prov,
                model: mod,
            });
        }
    }
    for (const k in byTask) byTask[k].sort((a, b) => a.order - b.order);

    let html = '';
    // Preset-Selector (runtime, nicht persistent — gilt nur fuer diese Server-Session)
    html += '<div style="margin-bottom:10px; padding:8px 10px; background:#161b22; border:1px solid #30363d; border-radius:6px;">';
    html += '<div style="font-size:12px; color:#8b949e; margin-bottom:6px;">Runtime-Preset (nicht persistent):</div>';
    html += '<select id="llm-task-preset" onchange="applyTaskPreset(this.value)" style="background:#0d1117; color:#c9d1d9; border:1px solid #30363d; padding:6px; border-radius:4px; width:100%;">';
    html += '<option value="none">— keins (alle Tasks aktiv) —</option>';
    for (const p of Object.keys(state.presets || {})) {
        html += '<option value="' + esc(p) + '">' + esc(p) + ' — ' + (state.presets[p] || []).length + ' Tasks aus</option>';
    }
    html += '</select>';
    if (runtimeDisabled.size) {
        html += '<div style="font-size:11px; color:#d29922; margin-top:4px;">Aktiv: ' + runtimeDisabled.size + ' Tasks runtime-deaktiviert</div>';
    }
    html += '</div>';

    for (const t of tasks) {
        const rows = byTask[t.id] || [];
        const isEmpty = rows.length === 0;
        const isPersistDisabled = persistentDisabled.has(t.id);
        const isRuntimeDisabled = runtimeDisabled.has(t.id);
        const disabledStyle = (isPersistDisabled || isRuntimeDisabled) ? 'opacity:0.5;' : '';
        html += '<div style="margin-bottom:10px; padding:8px 10px; background:#0d1117; border:1px solid #30363d; border-radius:6px; ' + disabledStyle + '">';
        html += '<div style="display:flex; justify-content:space-between; align-items:center;">';
        let catBadge = '';
        if (t.category_label) {
            catBadge = ' <span style="font-size:10px; color:#8b949e; font-weight:400; background:#21262d; padding:1px 6px; border-radius:8px; margin-left:4px;">' + esc(t.category_label) + '</span>';
        }
        html += '<div style="font-size:12px; color:#58a6ff; font-weight:600;">' + esc(t.label) + catBadge + ' <span style="color:#6e7681; font-weight:400;">— ' + esc(t.id) + '</span></div>';
        html += '<label style="display:inline-flex; align-items:center; gap:4px; font-size:11px; color:#8b949e; cursor:pointer;">';
        html += '<input type="checkbox" ' + (isPersistDisabled ? '' : 'checked') + ' onchange="toggleTaskPersistent(\\'' + t.id + '\\', !this.checked)"> aktiv';
        html += '</label>';
        html += '</div>';
        if (isRuntimeDisabled) {
            html += '<div style="font-size:11px; color:#d29922;">runtime-deaktiviert (Preset)</div>';
        }
        if (isEmpty) {
            html += '<div class="desc" style="color:#d29922;">kein LLM zugeordnet</div>';
        } else {
            html += '<div style="margin-top:4px;">';
            for (const r of rows) {
                html += '<div style="font-size:12px; color:#c9d1d9; display:flex; gap:8px;">';
                html += '<span style="color:#6e7681; min-width:22px;">' + r.order + '.</span>';
                html += '<span>' + esc(r.provider) + ' / ' + esc(r.model) + '</span>';
                html += '</div>';
            }
            html += '</div>';
        }
        html += '</div>';
    }
    view.innerHTML = html;
}

function toggleTaskPersistent(taskId, disable) {
    if (!CONFIG.llm_task_state) CONFIG.llm_task_state = { disabled_tasks: [] };
    const arr = CONFIG.llm_task_state.disabled_tasks || [];
    const idx = arr.indexOf(taskId);
    if (disable && idx < 0) arr.push(taskId);
    if (!disable && idx >= 0) arr.splice(idx, 1);
    CONFIG.llm_task_state.disabled_tasks = arr;
    toast('Aenderung erst nach Save aktiv', 'success');
    renderLlmTaskView(CONFIG.llm_routing || []);
}

async function applyTaskPreset(preset) {
    try {
        const resp = await fetch('/admin/settings/llm-task-state/runtime-preset', {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify({ preset: preset }),
        });
        const data = await resp.json();
        if (preset === 'none') {
            toast('Runtime-Preset aufgehoben', 'success');
        } else {
            toast('Runtime-Preset "' + preset + '" aktiv (' + (data.disabled || []).length + ' Tasks aus)', 'success');
        }
        renderLlmTaskView(CONFIG.llm_routing || []);
    } catch (e) {
        toast('Preset-Fehler: ' + e.message, 'error');
    }
}

// ── Render Fields ──
function renderFields(fields, data, path) {
    let html = '';
    for (const [fKey, f] of Object.entries(fields)) {
        if (f.type === 'group_header') {
            // Visueller Trenner ohne Daten-Binding (gruppiert nachfolgende Felder)
            html += '<div class="subsection-title" style="margin-top:18px;">' + f.label + '</div>';
            continue;
        }
        if (f.type === 'button') {
            // Action-Button — kein Daten-Binding, ruft Endpoint mit
            // body aus angegebenen Geschwister-Feldern auf.
            const btnId = 'btn-' + (path + '.' + fKey).replace(/\\W+/g, '-');
            const bodyFrom = JSON.stringify(f.body_from || []);
            const confirmMsg = f.confirm ? esc(f.confirm) : '';
            const previewUrl = f.preview_url ? esc(f.preview_url) : '';
            html += '<div class="field">';
            html += '<label></label>';
            html += '<div class="input-wrap">';
            html += '<button type="button" id="' + btnId + '" class="btn btn-primary" '
                + 'onclick="runActionButton(\\'' + esc(f.endpoint) + '\\', \\'' + (f.method || 'POST') + '\\', '
                + '\\'' + path + '\\', ' + bodyFrom.replace(/"/g, '&quot;') + ', \\'' + confirmMsg + '\\', this, \\'' + previewUrl + '\\')">'
                + esc(f.label) + '</button>';
            if (f.description) html += '<div class="desc">' + f.description + '</div>';
            html += '</div></div>';
            continue;
        }
        if (f.type === 'image_preview') {
            // Live-Preview eines Bild-Endpoints (z.B. generiertes Frame)
            const imgId = 'img-' + (path + '.' + fKey).replace(/\\W+/g, '-');
            const url = esc(f.url);
            const metaUrl = f.meta_url ? esc(f.meta_url) : '';
            html += '<div class="field">';
            html += '<label>' + esc(f.label) + '</label>';
            html += '<div class="input-wrap">';
            html += '<div id="' + imgId + '-wrap" class="image-preview-wrap" style="background:'
                + ' repeating-conic-gradient(#777 0% 25%, #555 0% 50%) 50% / 16px 16px;'
                + ' display:inline-block; padding:6px; border:1px solid #444; border-radius:6px; max-width:300px;">';
            html += '<img id="' + imgId + '" src="' + url + '?_=' + Date.now() + '" '
                + 'style="max-width:280px; max-height:380px; display:block;" '
                + 'onerror="this.style.display=\\'none\\'; this.nextElementSibling.style.display=\\'block\\';">';
            html += '<div style="display:none; color:#888; font-size:12px; padding:20px;">noch nicht generiert</div>';
            html += '</div>';
            if (metaUrl) {
                // Meta-URL als data-attribute hinterlegen — populateImagePreviewMetas()
                // wird nach renderSection aufgerufen und befuellt alle solche Elemente.
                html += '<div id="' + imgId + '-meta" class="desc image-preview-meta" '
                    + 'data-meta-url="' + metaUrl + '" '
                    + 'style="margin-top:6px; font-family:monospace; font-size:11px;"></div>';
            }
            if (f.description) html += '<div class="desc">' + f.description + '</div>';
            html += '</div></div>';
            continue;
        }
        if (f.type === 'array' && fKey === 'gpus') {
            html += renderGpuField(data[fKey] || [], path + '.' + fKey);
            continue;
        }
        if (f.type === 'lora_array') {
            html += renderLoraField(data[fKey] || [], path + '.' + fKey, f.max_items || 4);
            continue;
        }
        if (f.type === 'task_order_list') {
            html += renderTaskOrderList(data[fKey] || [], path + '.' + fKey, f);
            continue;
        }
        const val = data[fKey] !== undefined ? data[fKey] : (f.default !== undefined ? f.default : '');
        const fullPath = path + '.' + fKey;
        html += '<div class="field">';
        html += '<label for="f-' + fullPath + '">' + f.label + '</label>';
        html += '<div class="input-wrap">';
        html += renderInput(f, val, fullPath);
        if (f.description) html += '<div class="desc">' + f.description + '</div>';
        html += '</div></div>';
    }
    return html;
}

function renderInput(f, val, path) {
    const id = 'f-' + path;
    switch (f.type) {
        case 'bool':
            return '<input type="checkbox" id="' + id + '" ' + (val ? 'checked' : '') + ' onchange="setVal(\\'' + path + '\\', this.checked)">';
        case 'int':
            return '<input type="number" id="' + id + '" value="' + esc(val) + '" '
                + (f.min !== undefined ? 'min="' + f.min + '" ' : '')
                + (f.max !== undefined ? 'max="' + f.max + '" ' : '')
                + 'step="1" onchange="setVal(\\'' + path + '\\', parseInt(this.value) || 0)">';
        case 'float':
            return '<input type="number" id="' + id + '" value="' + esc(val) + '" '
                + (f.min !== undefined ? 'min="' + f.min + '" ' : '')
                + (f.max !== undefined ? 'max="' + f.max + '" ' : '')
                + 'step="' + (f.step || 0.1) + '" onchange="setVal(\\'' + path + '\\', parseFloat(this.value) || 0)">';
        case 'select':
            let opts = (f.choices || []).map(c => '<option value="' + esc(c) + '"' + (c == val ? ' selected' : '') + '>' + esc(c) + '</option>').join('');
            return '<select id="' + id + '" onchange="setVal(\\'' + path + '\\', this.value)">' + opts + '</select>';
        case 'password':
            return '<div class="pw-wrap"><input type="password" id="' + id + '" value="' + esc(val) + '" onchange="setVal(\\'' + path + '\\', this.value)">'
                + '<button class="pw-toggle" type="button" onclick="togglePw(this)">👁</button></div>';
        case 'text':
            return '<textarea id="' + id + '" onchange="setVal(\\'' + path + '\\', this.value)">' + esc(val) + '</textarea>';
        case 'provider_select':
            return renderProviderSelect(val, path);
        case 'gpu_select':
            return renderGpuSelect(val, path);
        case 'model_select':
            return renderModelSelect(val, path);
        case 'workflow_select':
            return renderWorkflowSelect(val, path);
        case 'imagegen_select':
            return renderImagegenSelect(val, path);
        case 'comfyui_model_select':
            return renderComfyModelSelect(val, path);
        case 'comfyui_backend_select':
            return renderComfyBackendSelect(val, path, f.multi);
        case 'imagegen_backend_select':
            return renderImagegenBackendSelect(val, path);
        case 'imagegen_model_select':
            return renderImagegenModelSelect(val, path);
        case 'imagegen_target_select':
            return renderImagegenTargetSelect(val, path);
        case 'comfyui_clip_select':
            return renderComfyClipSelect(val, path);
        default: // str
            return '<input type="text" id="' + id + '" value="' + esc(val) + '" '
                + (f.placeholder ? 'placeholder="' + esc(f.placeholder) + '" ' : '')
                + 'onchange="setVal(\\'' + path + '\\', this.value)">';
    }
}

function renderProviderSelect(val, path) {
    const providers = CONFIG.providers || [];
    let opts = '<option value="">— Auto —</option>';
    for (const p of providers) {
        opts += '<option value="' + esc(p.name) + '"' + (p.name === val ? ' selected' : '') + '>' + esc(p.name) + ' (' + p.type + ')</option>';
    }
    return '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value); refreshModelSelect(\\'' + path + '\\')">' + opts + '</select>';
}

function renderGpuSelect(val, path) {
    const providers = CONFIG.providers || [];
    let opts = '<option value="">— Keine —</option>';
    for (const p of providers) {
        const gpus = p.gpus || [];
        for (let i = 0; i < gpus.length; i++) {
            const g = gpus[i];
            const types = Array.isArray(g.types) ? g.types : (g.types || '').split(',');
            if (!types.some(t => t.trim() === 'comfyui')) continue;
            const key = p.name + ':' + i;
            const label = g.label || ('GPU ' + i);
            const vram = g.vram_gb ? ' — ' + g.vram_gb + ' GB' : '';
            opts += '<option value="' + esc(key) + '"' + (key === val ? ' selected' : '') + '>' + esc(p.name) + ' / ' + esc(label) + vram + '</option>';
        }
    }
    return '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">' + opts + '</select>';
}

function renderModelSelect(val, path) {
    // Try to find provider from sibling "provider" field
    const parts = path.split('.');
    parts[parts.length - 1] = 'provider';
    const provPath = parts.join('.');
    const provName = getVal(provPath) || '';

    let select = '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">';
    select += '<option value="' + esc(val) + '" selected>' + esc(val || '— select —') + '</option>';
    select += '</select>';
    select += ' <button class="btn btn-sm" onclick="loadModels(\\'' + path + '\\', \\'' + esc(provName) + '\\')">Load Models</button>';
    return select;
}

function renderWorkflowSelect(val, path) {
    const workflows = CONFIG.image_generation?.comfyui_workflows || {};
    let opts = '<option value="">— None —</option>';
    for (const [wid, wf] of Object.entries(workflows)) {
        const name = wf.name || wid;
        opts += '<option value="' + esc(wid) + '"' + (wid === val ? ' selected' : '') + '>' + esc(name) + '</option>';
    }
    return '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">' + opts + '</select>';
}

function renderImagegenSelect(val, path) {
    const workflows = CONFIG.image_generation?.comfyui_workflows || {};
    const backends = CONFIG.image_generation?.backends || [];
    let opts = '<option value="">— Auto —</option>';
    // Workflow options
    if (Object.keys(workflows).length) {
        opts += '<optgroup label="Workflows">';
        for (const [wid, wf] of Object.entries(workflows)) {
            const v = 'workflow:' + (wf.name || wid);
            opts += '<option value="' + esc(v) + '"' + (v === val ? ' selected' : '') + '>' + esc(wf.name || wid) + '</option>';
        }
        opts += '</optgroup>';
    }
    // Backend options
    if (backends.length) {
        opts += '<optgroup label="Backends">';
        for (const be of backends) {
            const v = 'backend:' + be.name;
            opts += '<option value="' + esc(v) + '"' + (v === val ? ' selected' : '') + '>' + esc(be.name) + ' (' + esc(be.api_type || '') + ')</option>';
        }
        opts += '</optgroup>';
    }
    return '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">' + opts + '</select>';
}

function renderComfyBackendSelect(val, path, multi) {
    // ComfyUI backends are image_generation.backends where api_type === 'comfyui'
    const backends = (CONFIG.image_generation?.backends || []).filter(b => b.api_type === 'comfyui');
    if (multi) {
        // Multi-select: value is comma-separated string
        const selected = (val || '').split(',').map(s => s.trim()).filter(Boolean);
        let html = '<div id="f-' + path + '-wrap">';
        for (const be of backends) {
            const checked = selected.includes(be.name) ? 'checked' : '';
            html += '<label style="display:inline-flex; align-items:center; gap:4px; margin-right:12px; font-size:13px; color:#c9d1d9; cursor:pointer;">';
            html += '<input type="checkbox" value="' + esc(be.name) + '" ' + checked + ' onchange="updateMultiBackend(\\'' + path + '\\')">';
            html += esc(be.name) + '</label>';
        }
        if (!backends.length) html += '<span style="color:#6e7681; font-size:12px;">No ComfyUI backends configured</span>';
        html += '</div>';
        return html;
    }
    // Single select
    let opts = '<option value="">— Auto —</option>';
    for (const be of backends) {
        opts += '<option value="' + esc(be.name) + '"' + (be.name === val ? ' selected' : '') + '>' + esc(be.name) + '</option>';
    }
    return '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">' + opts + '</select>';
}

function renderImagegenBackendSelect(val, path) {
    // ALLE Image-Backends (ComfyUI, Together, CivitAI, Mammouth, ...)
    const backends = CONFIG.image_generation?.backends || [];
    let opts = '<option value="">— None —</option>';
    for (const be of backends) {
        const lbl = be.name + (be.api_type ? ' (' + be.api_type + ')' : '');
        opts += '<option value="' + esc(be.name) + '"' + (be.name === val ? ' selected' : '') + '>' + esc(lbl) + '</option>';
    }
    // onchange: setVal + Geschwister-Modell-Select neu fuellen falls vorhanden
    return '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value); refreshImagegenModelSelect(\\'' + path + '\\')">' + opts + '</select>';
}

// Geschwister-Modell-Select neu laden wenn Backend gewechselt wird
function refreshImagegenModelSelect(backendPath) {
    const parts = backendPath.split('.');
    parts[parts.length - 1] = 'model';
    const modelPath = parts.join('.');
    const modelEl = document.getElementById('f-' + modelPath);
    if (!modelEl) return;
    const backendName = getVal(backendPath) || '';
    if (!backendName) {
        modelEl.innerHTML = '<option value="">— Backend zuerst waehlen —</option>';
        return;
    }
    loadImagegenBackendModels(modelPath, backendName);
}

let IMAGEGEN_MODELS_CACHE = {};

async function loadImagegenBackendModels(path, backendName) {
    const sel = document.getElementById('f-' + path);
    if (!sel) return;
    const currentVal = sel.value || getVal(path) || '';
    if (!IMAGEGEN_MODELS_CACHE[backendName]) {
        sel.innerHTML = '<option>Loading...</option>';
        try {
            const resp = await fetch('/admin/settings/imagegen-backends/' + encodeURIComponent(backendName) + '/models',
                { credentials: 'same-origin' });
            const data = await resp.json();
            if (data.error) toast('Models laden fehlgeschlagen: ' + data.error, 'error');
            const list = data.models || [];
            if (list.length > 0) IMAGEGEN_MODELS_CACHE[backendName] = list;
        } catch (e) {
            toast('Models laden fehlgeschlagen: ' + e.message, 'error');
        }
    }
    const models = IMAGEGEN_MODELS_CACHE[backendName] || [];
    let opts = '<option value="">— Backend-Default —</option>';
    for (const m of models) {
        opts += '<option value="' + esc(m) + '"' + (m === currentVal ? ' selected' : '') + '>' + esc(m) + '</option>';
    }
    if (currentVal && !models.includes(currentVal)) {
        opts = '<option value="' + esc(currentVal) + '" selected>' + esc(currentVal) + ' (custom)</option>' + opts;
    }
    sel.innerHTML = opts;
}

// Kombinierte Auswahl: ComfyUI-Workflows + Cloud-Backends.
// Wert-Format: "workflow:<name>" oder "backend:<name>" (wie /workflows-Endpoint)
let IMAGEGEN_TARGETS_CACHE = null;

async function loadImagegenTargets() {
    if (IMAGEGEN_TARGETS_CACHE) return IMAGEGEN_TARGETS_CACHE;
    try {
        const r = await fetch('/admin/settings/imagegen-targets', { credentials: 'same-origin' });
        const d = await r.json();
        IMAGEGEN_TARGETS_CACHE = d.targets || [];
    } catch {
        IMAGEGEN_TARGETS_CACHE = [];
    }
    return IMAGEGEN_TARGETS_CACHE;
}

function renderImagegenTargetSelect(val, path) {
    // Initial mit aktuellem Wert rendern; Liste wird async nachgeladen
    let html = '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">';
    if (val) html += '<option value="' + esc(val) + '" selected>' + esc(val) + '</option>';
    html += '<option value="">— Auto (Cloud bevorzugt) —</option>';
    html += '</select>';
    // Async populate
    setTimeout(async () => {
        const targets = await loadImagegenTargets();
        const sel = document.getElementById('f-' + path);
        if (!sel) return;
        let opts = '<option value="">— Auto (Cloud bevorzugt) —</option>';
        for (const t of targets) {
            const dis = t.available ? '' : ' disabled';
            const tag = t.available ? '' : ' (offline)';
            const sl = t.value === val ? ' selected' : '';
            opts += '<option value="' + esc(t.value) + '"' + sl + dis + '>' + esc(t.label + tag) + '</option>';
        }
        sel.innerHTML = opts;
    }, 0);
    return html;
}

function renderImagegenModelSelect(val, path) {
    // Backend aus Geschwister-Feld lesen
    const parts = path.split('.');
    parts[parts.length - 1] = 'backend';
    const backendPath = parts.join('.');
    const backendName = getVal(backendPath) || '';
    let html = '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">';
    if (val) {
        html += '<option value="' + esc(val) + '" selected>' + esc(val) + '</option>';
    } else {
        html += '<option value="">— Backend-Default —</option>';
    }
    html += '</select>';
    html += ' <button class="btn btn-sm" onclick="loadImagegenBackendModels(\\'' + path + '\\', \\'' + esc(backendName) + '\\')">Load Models</button>';
    return html;
}

function updateMultiBackend(path) {
    const wrap = document.getElementById('f-' + path + '-wrap');
    if (!wrap) return;
    const checked = [...wrap.querySelectorAll('input[type=checkbox]:checked')].map(cb => cb.value);
    setVal(path, checked.join(','));
}

// ── ComfyUI Model / LoRA selects ──
let COMFY_CACHE = null; // {checkpoints: [], loras: []}

async function loadComfyModels() {
    if (COMFY_CACHE) return COMFY_CACHE;
    try {
        const resp = await fetch('/admin/settings/comfyui-models', { credentials: 'same-origin' });
        COMFY_CACHE = await resp.json();
    } catch (e) {
        COMFY_CACHE = { checkpoints: [], loras: [] };
    }
    return COMFY_CACHE;
}

function renderComfyModelSelect(val, path) {
    let html = '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">';
    html += '<option value="' + esc(val) + '" selected>' + esc(val || '— select —') + '</option>';
    html += '</select>';
    html += ' <button class="btn btn-sm" onclick="populateComfySelect(\\'' + path + '\\', \\'checkpoints\\')">Load Models</button>';
    return html;
}

async function populateComfySelect(path, type) {
    const cache = await loadComfyModels();
    const items = cache[type] || [];
    const sel = document.getElementById('f-' + path);
    if (!sel) return;
    const current = sel.value;
    let opts = '<option value="">— none —</option>';
    for (const m of items) {
        opts += '<option value="' + esc(m) + '"' + (m === current ? ' selected' : '') + '>' + esc(m) + '</option>';
    }
    sel.innerHTML = opts;
    if (current && !items.includes(current)) {
        sel.insertAdjacentHTML('afterbegin', '<option value="' + esc(current) + '" selected>' + esc(current) + ' (not on server)</option>');
    }
    if (!items.length) toast('No models found. Server running?', 'error');
}

function renderComfyClipSelect(val, path) {
    let html = '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">';
    html += '<option value="' + esc(val) + '" selected>' + esc(val || '— select —') + '</option>';
    html += '</select>';
    html += ' <button class="btn btn-sm" onclick="populateComfySelect(\\'' + path + '\\', \\'clip_models\\')">Load CLIP Models</button>';
    return html;
}

// ── Array/Dict Items ──
// _itemLabel: gleiche Logik wie in renderArrayItem — fuer Sortierung
function _itemLabel(item, labelField, fallback) {
    return String((item && item[labelField]) || fallback || '');
}

function renderArrayItems(def, items, path) {
    let html = '<div id="arr-' + path + '">';
    // Index erhalten (Pfade referenzieren echten Array-Index), Reihenfolge
    // alphabetisch wenn def.sort_alphabetically gesetzt ist.
    const order = items.map((it, i) => ({ idx: i, label: _itemLabel(it, def.item_label_field, 'Item ' + i) }));
    if (def.sort_alphabetically) {
        order.sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: 'base' }));
    }
    for (const o of order) {
        html += renderArrayItem(def, items[o.idx], path + '[' + o.idx + ']', o.idx, def.item_label_field);
    }
    html += '</div>';
    return html;
}

function renderDictItems(def, items, path) {
    let html = '<div id="arr-' + path + '">';
    const entries = Object.entries(items).map(([k, item]) => ({ key: k, item, label: _itemLabel(item, def.item_label_field, k) }));
    if (def.sort_alphabetically) {
        entries.sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: 'base' }));
    }
    for (const e of entries) {
        html += renderArrayItem(def, e.item, path + '.' + e.key, e.key, def.item_label_field);
    }
    html += '</div>';
    return html;
}

function renderArrayItem(def, item, path, index, labelField) {
    const label = item[labelField] || ('Item ' + index);
    let html = '<div class="array-item" id="item-' + path + '">';
    html += '<div class="array-item-header" onclick="this.parentElement.classList.toggle(\\'open\\')">';
    html += '<span class="chevron">▶</span> ';
    html += '<span class="title" style="margin-left:6px;">' + esc(label) + '</span>';
    if (item.enabled === false) html += '<span class="badge">deaktiviert</span>';
    if (item.type) html += '<span class="badge">' + esc(item.type || item.api_type || '') + '</span>';
    html += '<button class="btn btn-sm" style="margin-left:8px;" title="Als neuen Eintrag duplizieren" onclick="event.stopPropagation(); duplicateItem(\\'' + path + '\\')">⧉</button>';
    html += '<button class="btn btn-sm btn-danger" style="margin-left:4px;" onclick="event.stopPropagation(); removeItem(\\'' + path + '\\')">✕</button>';
    html += '</div>';
    html += '<div class="array-item-body">';
    html += renderFields(def.fields, item, path);
    html += '</div></div>';
    return html;
}

// ── Task/Order List (llm_routing.tasks) ──
let LLM_TASKS_CACHE = null;

async function loadLlmTasks(forceRefresh) {
    if (LLM_TASKS_CACHE && !forceRefresh) return LLM_TASKS_CACHE;
    try {
        // cache-bust per Query-Param damit Browser nicht aus dem HTTP-Cache
        // serviert (z.B. nach Server-Neustart mit neuen Sub-Tasks).
        const resp = await fetch('/admin/settings/llm-tasks?_=' + Date.now(),
            { credentials: 'same-origin', cache: 'no-store' });
        LLM_TASKS_CACHE = await resp.json();
    } catch (e) {
        LLM_TASKS_CACHE = [];
    }
    return LLM_TASKS_CACHE;
}

function renderTaskOrderList(items, path, f) {
    // items: [{task: 'chat_stream', order: 1}, ...]
    let html = '<div class="field"><label>' + f.label + '</label><div class="input-wrap">';
    if (f.description) html += '<div class="desc" style="margin-bottom:6px;">' + f.description + '</div>';
    html += '<div id="tasks-' + path + '">';
    for (let i = 0; i < items.length; i++) {
        html += renderTaskOrderRow(items[i] || {}, path, i);
    }
    html += '</div>';
    html += '<div style="margin-top:6px; display:flex; flex-wrap:wrap; gap:4px;">';
    html += '<button class="btn btn-sm" onclick="addTaskOrderRow(\\'' + path + '\\')">+ Task</button>';
    html += '<button class="btn btn-sm" title="Add all Image-Input tasks not yet assigned" onclick="addTaskGroup(\\'' + path + '\\', \\'image\\')">+ All Image</button>';
    html += '<button class="btn btn-sm" title="Add all Tool tasks not yet assigned" onclick="addTaskGroup(\\'' + path + '\\', \\'tool\\')">+ All Tools</button>';
    html += '<button class="btn btn-sm" title="Add all Large Chat Model tasks not yet assigned" onclick="addTaskGroup(\\'' + path + '\\', \\'chat\\')">+ All Chat</button>';
    html += '<button class="btn btn-sm" title="Add all Small Helper tasks not yet assigned" onclick="addTaskGroup(\\'' + path + '\\', \\'helper\\')">+ All Helper</button>';
    html += '</div>';
    html += '</div></div>';
    // Async: Dropdowns fuellen nachdem DOM da ist
    setTimeout(() => populateTaskSelects(path), 0);
    return html;
}

function renderTaskOrderRow(item, path, i) {
    const task = item.task || '';
    const order = (item.order !== undefined ? item.order : 1);
    let html = '<div class="gpu-row" id="taskrow-' + path + '-' + i + '">';
    html += '<select data-taskrow="' + path + '-' + i + '" style="flex:3;" onchange="setVal(\\'' + path + '[' + i + '].task\\', this.value)">';
    html += '<option value="' + esc(task) + '" selected>' + esc(task || '— select —') + '</option>';
    html += '</select>';
    html += '<input type="number" value="' + order + '" min="1" step="1" style="max-width:70px;" title="Order" onchange="setVal(\\'' + path + '[' + i + '].order\\', parseInt(this.value) || 1)">';
    html += '<button class="btn btn-sm btn-danger" onclick="removeTaskOrderRow(\\'' + path + '\\', ' + i + ')">✕</button>';
    html += '</div>';
    return html;
}

async function populateTaskSelects(path) {
    const tasks = await loadLlmTasks();
    // Group tasks by category for guidance — show grouped <optgroup>s in the dropdown.
    const order = ['image', 'tool', 'chat', 'helper', ''];
    const grouped = {};
    for (const t of tasks) {
        const cat = t.category || '';
        (grouped[cat] = grouped[cat] || []).push(t);
    }
    const selects = document.querySelectorAll('select[data-taskrow^="' + path + '-"]');
    selects.forEach(sel => {
        const current = sel.value;
        let opts = '<option value="">— select —</option>';
        for (const cat of order) {
            const list = grouped[cat];
            if (!list || !list.length) continue;
            const groupLabel = list[0].category_label || 'Other';
            opts += '<optgroup label="' + esc(groupLabel) + '">';
            for (const t of list) {
                opts += '<option value="' + esc(t.id) + '"' + (t.id === current ? ' selected' : '') + '>'
                     + esc(t.label) + ' — ' + esc(t.id) + '</option>';
            }
            opts += '</optgroup>';
        }
        sel.innerHTML = opts;
    });
}

function addTaskOrderRow(path) {
    const obj = _ensureContainer(path, 'array');
    // order=1 is the default primary slot. Increase only when this LLM is meant
    // as a fallback for a task another LLM already serves at order=1.
    obj.push({ task: '', order: 1 });
    rerenderTaskOrderList(path);
}

async function addTaskGroup(path, category) {
    const tasks = await loadLlmTasks();
    const obj = _ensureContainer(path, 'array');
    const existing = new Set((obj || []).map(it => it && it.task).filter(Boolean));
    let added = 0;
    for (const t of tasks) {
        if (t.category !== category) continue;
        if (existing.has(t.id)) continue;
        obj.push({ task: t.id, order: 1 });
        added++;
    }
    rerenderTaskOrderList(path);
    if (added) toast('Added ' + added + ' task' + (added === 1 ? '' : 's'), 'success');
    else toast('All tasks of this group are already assigned', 'success');
}

function removeTaskOrderRow(path, index) {
    const parts = parsePath(path);
    let obj = CONFIG;
    for (const p of parts) obj = obj[p];
    obj.splice(index, 1);
    rerenderTaskOrderList(path);
}

function rerenderTaskOrderList(path) {
    // Re-render nur den Tasks-Container statt die ganze Section — damit
    // das umgebende Array-Item offen bleibt.
    const parts = parsePath(path);
    let obj = CONFIG;
    for (const p of parts) obj = obj && obj[p];
    const items = Array.isArray(obj) ? obj : [];
    const wrap = document.getElementById('tasks-' + path);
    if (!wrap) { renderSection(ACTIVE_SECTION); return; }
    let html = '';
    for (let i = 0; i < items.length; i++) {
        html += renderTaskOrderRow(items[i] || {}, path, i);
    }
    wrap.innerHTML = html;
    populateTaskSelects(path);
    // Sichtweise rechts mit aktualisieren wenn wir im llm_routing-Tab sind
    if (ACTIVE_SECTION === 'llm_routing') {
        renderLlmTaskView(CONFIG.llm_routing || []);
    }
}

// ── GPU Field ──
function renderGpuField(gpus, path) {
    let html = '<div class="field"><label>GPUs</label><div class="input-wrap">';
    html += '<div id="gpu-' + path + '">';
    for (let i = 0; i < gpus.length; i++) {
        const g = gpus[i];
        html += '<div class="gpu-row">';
        html += '<input type="text" value="' + esc(g.label || '') + '" placeholder="Label" style="max-width:120px;" onchange="setVal(\\'' + path + '[' + i + '].label\\', this.value)">';
        html += '<input type="number" value="' + (g.vram_gb || 0) + '" placeholder="VRAM GB" style="max-width:80px;" onchange="setVal(\\'' + path + '[' + i + '].vram_gb\\', parseInt(this.value))">';
        html += '<input type="text" value="' + esc(g.match_name || '') + '" placeholder="Match-Name (z.B. 4070)" title="Case-insensitive Substring im Beszel-GPU-Namen — wird zuerst probiert (stabil ueber Reboots)" style="max-width:140px;" onchange="setVal(\\'' + path + '[' + i + '].match_name\\', this.value)">';
        html += '<input type="text" value="' + esc(g.device || '') + '" placeholder="Device (Fallback)" title="Beszel device-id — nur noetig wenn Match-Name nicht eindeutig greift (z.B. zwei gleiche Modelle, oder Beszel meldet falschen Namen)" style="max-width:100px;opacity:0.7;" onchange="setVal(\\'' + path + '[' + i + '].device\\', this.value)">';
        const typesStr = Array.isArray(g.types) ? g.types.join(',') : (g.types || '');
        html += '<input type="text" value="' + esc(typesStr) + '" placeholder="ollama,openai,comfyui" onchange="setVal(\\'' + path + '[' + i + '].types\\', this.value.split(\\',\\').map(s=>s.trim()))">';
        html += '<input type="number" value="' + (g.max_concurrent || 1) + '" placeholder="MC" title="Max Concurrent" min="1" max="50" style="max-width:55px;" onchange="setVal(\\'' + path + '[' + i + '].max_concurrent\\', parseInt(this.value) || 1)">';
        html += '<button class="btn btn-sm btn-danger" onclick="removeSubItem(\\'' + path + '\\', ' + i + ')">✕</button>';
        html += '</div>';
    }
    html += '</div>';
    html += '<button class="btn btn-sm" style="margin-top:4px;" onclick="addGpu(\\'' + path + '\\')">+ GPU</button>';
    html += '</div></div>';
    return html;
}

// ── LoRA Field ──
function renderLoraField(loras, path, maxItems) {
    let html = '<div class="field"><label>LoRAs</label><div class="input-wrap">';
    for (let i = 0; i < maxItems; i++) {
        const l = loras[i] || { file: '', strength: 1 };
        const selId = 'lora-' + path + '-' + i;
        html += '<div class="lora-row">';
        html += '<select id="' + selId + '" style="flex:3;" onchange="setLoraVal(\\'' + path + '\\', ' + i + ', \\'file\\', this.value)">';
        html += '<option value="">— none —</option>';
        if (l.file) html += '<option value="' + esc(l.file) + '" selected>' + esc(l.file) + '</option>';
        html += '</select>';
        html += '<input type="number" value="' + (l.strength || 1) + '" step="0.1" min="0" max="2" style="flex:1; max-width:80px;" onchange="setLoraVal(\\'' + path + '\\', ' + i + ', \\'strength\\', parseFloat(this.value))">';
        html += '</div>';
    }
    html += '<button class="btn btn-sm" style="margin-top:4px;" onclick="populateLoraSelects(\\'' + path + '\\', ' + maxItems + ')">Load LoRAs</button>';
    html += '</div></div>';
    return html;
}

async function populateLoraSelects(path, maxItems) {
    const cache = await loadComfyModels();
    const items = cache.loras || [];
    if (!items.length) { toast('No LoRAs found. Server running?', 'error'); return; }
    for (let i = 0; i < maxItems; i++) {
        const sel = document.getElementById('lora-' + path + '-' + i);
        if (!sel) continue;
        const current = sel.value;
        let opts = '<option value="">— none —</option>';
        for (const m of items) {
            opts += '<option value="' + esc(m) + '"' + (m === current ? ' selected' : '') + '>' + esc(m) + '</option>';
        }
        sel.innerHTML = opts;
        if (current && !items.includes(current)) {
            sel.insertAdjacentHTML('afterbegin', '<option value="' + esc(current) + '" selected>' + esc(current) + '</option>');
        }
    }
    toast(items.length + ' LoRAs loaded', 'success');
}

// ── Data Access ──
function setVal(path, value) {
    const parts = parsePath(path);
    let obj = CONFIG;
    for (let i = 0; i < parts.length - 1; i++) {
        const p = parts[i];
        if (obj[p] === undefined) {
            obj[p] = (typeof parts[i+1] === 'number') ? [] : {};
        }
        obj = obj[p];
    }
    obj[parts[parts.length - 1]] = value;
}

function getVal(path) {
    const parts = parsePath(path);
    let obj = CONFIG;
    for (const p of parts) {
        if (obj === undefined || obj === null) return undefined;
        obj = obj[p];
    }
    return obj;
}

function parsePath(path) {
    // "providers[0].name" => ["providers", 0, "name"]
    const result = [];
    for (const part of path.split('.')) {
        const m = part.match(/^([^\\[]+)(?:\\[(\\d+)\\])?$/);
        if (m) {
            result.push(m[1]);
            if (m[2] !== undefined) result.push(parseInt(m[2]));
        } else {
            result.push(part);
        }
    }
    return result;
}

function setLoraVal(path, index, field, value) {
    const parts = parsePath(path);
    let obj = CONFIG;
    for (const p of parts) {
        if (obj[p] === undefined) obj[p] = [];
        obj = obj[p];
    }
    while (obj.length <= index) obj.push({ file: '', strength: 1 });
    obj[index][field] = value;
}

// Walks `path` inside CONFIG, creating any missing levels. Intermediate levels
// are always created as {}; only the leaf takes the requested `leafType`
// ('array' or 'dict'). Returns the leaf container.
function _ensureContainer(path, leafType) {
    const parts = parsePath(path);
    let obj = CONFIG;
    for (let i = 0; i < parts.length; i++) {
        const p = parts[i];
        if (obj[p] === undefined) {
            obj[p] = (i === parts.length - 1)
                ? (leafType === 'dict' ? {} : [])
                : {};
        }
        obj = obj[p];
    }
    return obj;
}

// ── Actions ──
function addArrayItem(path, type) {
    const obj = _ensureContainer(path, type);
    if (type === 'dict') {
        const id = prompt('Workflow ID (e.g. FLUX, QWEN):');
        if (!id) return;
        obj[id] = { name: id, loras: [{file:'',strength:1},{file:'',strength:1},{file:'',strength:1},{file:'',strength:1}] };
    } else {
        if (path === 'llm_routing') {
            obj.push({ provider: '', model: '', temperature: 0.7, tasks: [] });
        } else {
            obj.push({ name: 'New', enabled: true, gpus: [] });
        }
    }
    renderSection(ACTIVE_SECTION);
}

function removeItem(path) {
    if (!confirm('Remove this item?')) return;
    const parts = parsePath(path);
    let obj = CONFIG;
    for (let i = 0; i < parts.length - 1; i++) {
        obj = obj[parts[i]];
    }
    const last = parts[parts.length - 1];
    if (typeof last === 'number') {
        obj.splice(last, 1);
    } else {
        delete obj[last];
    }
    renderSection(ACTIVE_SECTION);
}

// Dupliziert einen Array- oder Dict-Eintrag (LLM-Routing, Backends,
// ComfyUI-Workflows etc.). Bei Dicts wird ein neuer Key abgefragt; bei
// Arrays wird der Klon ans Ende angehaengt. `name`-Felder bekommen ein
// "(Kopie)"-Suffix, damit der duplizierte Eintrag direkt unterscheidbar ist.
function duplicateItem(path) {
    const parts = parsePath(path);
    let parent = CONFIG;
    for (let i = 0; i < parts.length - 1; i++) {
        parent = parent[parts[i]];
    }
    const last = parts[parts.length - 1];
    const original = (typeof last === 'number') ? parent[last] : parent[last];
    if (!original) { toast('Eintrag nicht gefunden', 'error'); return; }
    // Deep clone — Defaults sollen nicht mit dem Original geteilt werden.
    const copy = JSON.parse(JSON.stringify(original));
    if (copy && typeof copy === 'object' && 'name' in copy && copy.name) {
        copy.name = String(copy.name) + ' (Kopie)';
    }
    if (typeof last === 'number') {
        // Array: direkt hinter Original einfuegen
        parent.splice(last + 1, 0, copy);
    } else {
        // Dict: neuen Key vom User abfragen
        const newKey = prompt('Neuer Schluessel fuer den Klon:', String(last) + '_copy');
        if (!newKey) return;
        if (parent[newKey] !== undefined) { toast('Schluessel existiert bereits: ' + newKey, 'error'); return; }
        parent[newKey] = copy;
    }
    renderSection(ACTIVE_SECTION);
}

function removeSubItem(path, index) {
    const parts = parsePath(path);
    let obj = CONFIG;
    for (const p of parts) obj = obj[p];
    obj.splice(index, 1);
    renderSection(ACTIVE_SECTION);
}

function addGpu(path) {
    const parts = parsePath(path);
    let obj = CONFIG;
    for (const p of parts) {
        if (obj[p] === undefined) obj[p] = [];
        obj = obj[p];
    }
    obj.push({ vram_gb: 0, types: ['openai'], match_name: '', device: '' });
    renderSection(ACTIVE_SECTION);
}

async function loadModels(path, provName) {
    if (!provName) {
        // Try to detect from sibling
        const parts = path.split('.');
        parts[parts.length - 1] = 'provider';
        provName = getVal(parts.join('.'));
    }
    if (!provName) { toast('Select a provider first', 'error'); return; }

    const sel = document.getElementById('f-' + path);
    if (!sel) return;
    const currentVal = sel.value;

    // Cache: leere Listen NICHT cachen (sonst blockt eine fehlgeschlagene
    // Abfrage alle Retry-Versuche bis zum Page-Reload).
    if (!PROVIDERS_CACHE[provName] || PROVIDERS_CACHE[provName].length === 0) {
        sel.innerHTML = '<option>Loading...</option>';
        try {
            const resp = await fetch('/admin/settings/providers/' + encodeURIComponent(provName) + '/models', { credentials: 'same-origin' });
            const data = await resp.json();
            if (data.error) { toast('Error: ' + data.error, 'error'); }
            const list = data.models || [];
            if (list.length > 0) {
                PROVIDERS_CACHE[provName] = list;
            } else {
                delete PROVIDERS_CACHE[provName];
            }
        } catch (e) {
            toast('Failed to load models: ' + e.message, 'error');
            delete PROVIDERS_CACHE[provName];
        }
    }

    const models = PROVIDERS_CACHE[provName];
    let opts = '<option value="">— select —</option>';
    for (const m of models) {
        opts += '<option value="' + esc(m) + '"' + (m === currentVal ? ' selected' : '') + '>' + esc(m) + '</option>';
    }
    sel.innerHTML = opts;
    if (currentVal && !models.includes(currentVal)) {
        sel.innerHTML = '<option value="' + esc(currentVal) + '" selected>' + esc(currentVal) + ' (not on server)</option>' + opts;
    }
}

function refreshModelSelect(provPath) {
    // When provider changes, clear model cache
    const parts = provPath.split('.');
    parts[parts.length - 1] = 'model';
    const modelPath = parts.join('.');
    const provName = getVal(provPath);
    if (provName) loadModels(modelPath, provName);
}

async function validateConfig() {
    const btn = document.getElementById('btn-validate');
    btn.disabled = true;
    btn.textContent = 'Validating...';
    try {
        const resp = await fetch('/admin/settings/validate', {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify(CONFIG)
        });
        const result = await resp.json();
        const issues = result.issues || [];
        const content = document.getElementById('content');

        let html = '<div class="validate-results ' + (result.errors > 0 ? 'has-errors' : 'all-ok') + '">';
        if (issues.length === 0) {
            html += '<h3>Keine Probleme gefunden</h3>';
        } else {
            html += '<h3>' + result.errors + ' Fehler, ' + result.warnings + ' Warnungen</h3>';
            for (const issue of issues) {
                html += '<div class="validate-issue ' + issue.level + '">';
                html += '<span class="badge">' + (issue.level === 'error' ? 'ERROR' : 'WARN') + '</span>';
                html += '<span>' + esc(issue.message) + '</span>';
                html += '<span class="section-link" onclick="activateSection(\\'' + issue.section + '\\')">' + issue.section + '</span>';
                html += '</div>';
            }
        }
        html += '</div>';

        // Show below current section or as standalone
        if (ACTIVE_SECTION && !ACTIVE_SECTION.startsWith('_')) {
            content.insertAdjacentHTML('afterbegin', html);
        } else {
            content.innerHTML = html;
        }
        if (result.errors > 0) toast(result.errors + ' Fehler gefunden', 'error');
        else if (result.warnings > 0) toast(result.warnings + ' Warnungen', 'success');
        else toast('Alles OK!', 'success');
    } catch (e) {
        toast('Validation failed: ' + e.message, 'error');
    }
    btn.disabled = false;
    btn.textContent = 'Validate';
}

// Generischer Action-Button-Handler — schickt POST/DELETE/etc an einen Endpoint
// mit Body aus angegebenen Geschwister-Feldern. Genutzt von schema-Type "button".
async function runActionButton(endpoint, method, path, bodyFrom, confirmMsg, btn, previewUrl) {
    if (confirmMsg && !confirm(confirmMsg)) return;
    const body = {};
    // Werte aus DOM lesen (frischste Quelle — auch wenn User getippt aber
    // noch nicht gespeichert hat). Fallback auf CONFIG, dann auf
    // f-input-element.value als letzten Strohhalm fuer Defaults.
    for (const fld of (bodyFrom || [])) {
        const sibling = path + '.' + fld;
        let v = undefined;
        // 1. Versuche das DOM-Input direkt
        const el = document.getElementById('f-' + sibling);
        if (el && 'value' in el) {
            v = el.value;
        }
        // 2. Fallback: gespeicherter CONFIG-Wert
        if (v === undefined || v === null || v === '') {
            v = getVal(sibling);
        }
        if (v !== undefined && v !== null && v !== '') body[fld] = v;
    }
    const origLabel = btn.textContent;
    btn.disabled = true;
    btn.textContent = '⏳ ' + origLabel;
    try {
        const opts = { method, headers: authHeaders(), credentials: 'same-origin' };
        if (method !== 'GET' && method !== 'DELETE') {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body);
        }
        const resp = await fetch(endpoint, opts);
        const data = await resp.json().catch(() => ({}));
        if (resp.ok) {
            const detail = data.bbox ? ` (bbox ${data.bbox.w}×${data.bbox.h})` : '';
            toast((data.status || 'OK') + detail, 'success');
            // Preview-Bild neu laden (Cache-Bust via Timestamp) und Meta refreshen
            if (previewUrl) {
                document.querySelectorAll('img[src^="' + previewUrl + '"]').forEach(img => {
                    img.src = previewUrl + '?_=' + Date.now();
                    img.style.display = '';
                    if (img.nextElementSibling) img.nextElementSibling.style.display = 'none';
                });
                if (typeof populateImagePreviewMetas === 'function') {
                    populateImagePreviewMetas();
                }
            }
        } else {
            toast('Fehler: ' + (data.detail || data.error || resp.status), 'error');
        }
    } catch (e) {
        toast('Aufruf fehlgeschlagen: ' + e.message, 'error');
    }
    btn.disabled = false;
    btn.textContent = origLabel;
}

async function saveConfig() {
    const btn = document.getElementById('btn-save');
    btn.disabled = true;
    btn.textContent = 'Saving...';
    try {
        const resp = await fetch('/admin/settings/save', {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify(CONFIG)
        });
        const result = await resp.json();
        if (resp.ok) {
            // URL/Key-Aenderungen sollen sofort greifen, ohne Page-Reload.
            // Provider-Model-Cache + ComfyUI-Model-Cache invalidieren.
            for (const k of Object.keys(PROVIDERS_CACHE)) delete PROVIDERS_CACHE[k];
            COMFY_CACHE = null;
            toast(result.message || 'Saved!', 'success');
        } else {
            toast('Error: ' + (result.detail || result.message), 'error');
        }
    } catch (e) {
        toast('Save failed: ' + e.message, 'error');
    }
    btn.disabled = false;
    btn.textContent = 'Save';
}

async function reloadServices() {
    if (!confirm('Reload all services? Active requests may be interrupted.')) return;
    try {
        const resp = await fetch('/admin/settings/restart', {
            method: 'POST',
            headers: authHeaders()
        });
        const result = await resp.json();
        toast('Services reloaded', 'success');
    } catch (e) {
        toast('Reload failed: ' + e.message, 'error');
    }
}

function togglePw(btn) {
    const input = btn.parentElement.querySelector('input');
    input.type = input.type === 'password' ? 'text' : 'password';
}

// ── Helpers ──
function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function toast(msg, type) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast ' + type + ' show';
    setTimeout(() => t.classList.remove('show'), 3000);
}

// Start
init();
</script>
</body>
</html>'''


def _build_users_html() -> str:
    """User-Verwaltungs-Seite (Admin-only)."""
    return '''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<title>User-Verwaltung</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }
h1 { font-size: 18px; margin-bottom: 16px; color: #e6edf3; }
.toolbar { margin-bottom: 16px; }
.btn { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; }
.btn:hover { background: #30363d; }
.btn-primary { background: #238636; border-color: #2ea043; color: #fff; }
.btn-primary:hover { background: #2ea043; }
.btn-danger { background: #da3633; border-color: #f85149; color: #fff; }
.btn-danger:hover { background: #b62324; }
.btn-sm { padding: 4px 8px; font-size: 12px; }

table { width: 100%; border-collapse: collapse; background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }
th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid #30363d; }
th { background: #1c2128; font-size: 12px; color: #8b949e; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
tr:last-child td { border-bottom: none; }
tr:hover { background: #1c2128; }
.role-admin { color: #58a6ff; font-weight: 600; }
.role-user { color: #8b949e; }
.chars { font-size: 11px; color: #8b949e; }
td .actions { display: flex; gap: 6px; }

.modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: none; align-items: center; justify-content: center; z-index: 1000; }
.modal-bg.show { display: flex; }
.modal { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; width: 480px; max-width: 92vw; max-height: 90vh; overflow-y: auto; }
.modal h2 { font-size: 16px; margin-bottom: 14px; color: #e6edf3; }
.field { margin-bottom: 12px; }
.field label { display: block; font-size: 12px; color: #8b949e; margin-bottom: 4px; }
.field input, .field select { width: 100%; background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; padding: 6px 10px; border-radius: 6px; font-size: 13px; }
.field input:focus, .field select:focus { border-color: #58a6ff; outline: none; }
.chars-box {
    max-height: 160px; overflow-y: auto; overflow-x: hidden;
    border: 1px solid #30363d; padding: 8px; border-radius: 6px;
    background: #0d1117;
}
/* Eigene Klasse statt Tag-Selector — kein Cascading-Risiko */
.char-row {
    display: block !important;
    width: 100% !important;
    padding: 3px 0 !important;
    margin: 0 !important;
    font-size: 12px;
    color: #c9d1d9 !important;
    cursor: pointer;
    text-align: left !important;
    white-space: nowrap;
    line-height: 1.6;
}
.char-row > input[type="checkbox"] {
    display: inline-block !important;
    margin: 0 8px 0 0 !important;
    padding: 0 !important;
    vertical-align: middle !important;
    float: none !important;
    width: auto !important;
    min-width: 0 !important;
}
.char-row > span {
    display: inline-block !important;
    vertical-align: middle !important;
    color: #c9d1d9 !important;
}
.modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px; border-top: 1px solid #30363d; padding-top: 12px; }
.error-msg { color: #f85149; font-size: 12px; margin-bottom: 8px; display: none; }
.toast { position: fixed; bottom: 20px; right: 20px; background: #238636; color: #fff; padding: 10px 16px; border-radius: 6px; font-size: 13px; opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 2000; }
.toast.show { opacity: 1; }
.toast.error { background: #da3633; }
</style>
</head>
<body>

<h1>User-Verwaltung</h1>
<div class="toolbar">
    <button class="btn btn-primary" onclick="openEdit(null)">+ Neuer User</button>
</div>

<table id="users-table">
    <thead>
        <tr><th>Benutzername</th><th>Rolle</th><th>Characters</th><th>Letzter Login</th><th></th></tr>
    </thead>
    <tbody id="users-tbody">
        <tr><td colspan="5" style="text-align:center;color:#8b949e;">Lade...</td></tr>
    </tbody>
</table>

<div class="modal-bg" id="modal-bg">
    <div class="modal">
        <h2 id="modal-title">User anlegen</h2>
        <div class="error-msg" id="modal-error"></div>
        <div class="field">
            <label>Benutzername</label>
            <input type="text" id="edit-username" autocomplete="off">
        </div>
        <div class="field">
            <label>Rolle</label>
            <select id="edit-role">
                <option value="user">User</option>
                <option value="admin">Admin</option>
            </select>
        </div>
        <div class="field">
            <label id="edit-password-label">Passwort</label>
            <input type="password" id="edit-password" autocomplete="new-password">
        </div>
        <div class="field">
            <label style="display:flex;align-items:center;gap:8px;">
                Zugeordnete Characters
                <button type="button" class="btn btn-sm" onclick="toggleAllChars(true)">Alle</button>
                <button type="button" class="btn btn-sm" onclick="toggleAllChars(false)">Keiner</button>
            </label>
            <div class="chars-box" id="edit-chars-box"></div>
        </div>
        <div class="modal-actions">
            <button class="btn" onclick="closeEdit()">Abbrechen</button>
            <button class="btn btn-primary" onclick="saveEdit()">Speichern</button>
        </div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
let USERS = [];
let CHARS = [];
let EDIT_ID = null;

async function loadAll() {
    try {
        const [uResp, cResp] = await Promise.all([
            fetch('/auth/users'),
            fetch('/characters/list'),
        ]);
        if (uResp.status === 401 || uResp.status === 403) {
            const ret = encodeURIComponent(window.location.pathname);
            window.location.href = '/?return=' + ret;
            return;
        }
        USERS = (await uResp.json()).users || [];
        CHARS = (await cResp.json()).characters || [];
        renderTable();
    } catch (e) {
        toast('Fehler beim Laden: ' + e.message, 'error');
    }
}

function renderTable() {
    const tb = document.getElementById('users-tbody');
    if (!USERS.length) {
        tb.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#8b949e;">Keine User</td></tr>';
        return;
    }
    tb.innerHTML = USERS.map(u => {
        const charList = (u.allowed_characters || []).join(', ') || '—';
        const roleClass = u.role === 'admin' ? 'role-admin' : 'role-user';
        return '<tr>' +
            '<td>' + escapeHtml(u.username) + '</td>' +
            '<td class="' + roleClass + '">' + escapeHtml(u.role) + '</td>' +
            '<td class="chars">' + escapeHtml(charList) + '</td>' +
            '<td>' + escapeHtml(u.last_login || '—') + '</td>' +
            '<td class="actions">' +
                '<button class="btn btn-sm" onclick="openEdit(\\'' + u.id + '\\')">Edit</button>' +
                '<button class="btn btn-sm btn-danger" onclick="deleteUser(\\'' + u.id + '\\')">Del</button>' +
            '</td>' +
        '</tr>';
    }).join('');
}

function openEdit(userId) {
    EDIT_ID = userId;
    const u = userId ? USERS.find(x => x.id === userId) : null;
    document.getElementById('modal-title').textContent = u ? 'User bearbeiten' : 'User anlegen';
    document.getElementById('edit-username').value = u ? u.username : '';
    document.getElementById('edit-role').value = u ? u.role : 'user';
    document.getElementById('edit-password').value = '';
    document.getElementById('edit-password-label').textContent = u ? 'Passwort (leer = nicht aendern)' : 'Passwort';
    document.getElementById('modal-error').style.display = 'none';

    const assigned = new Set(u ? u.allowed_characters : []);
    document.getElementById('edit-chars-box').innerHTML = CHARS.map(c =>
        '<label class="char-row"><input type="checkbox" value="' + escapeHtml(c) + '"' + (assigned.has(c) ? ' checked' : '') + '><span>' + escapeHtml(c) + '</span></label>'
    ).join('');
    document.getElementById('modal-bg').classList.add('show');
}

function toggleAllChars(checked) {
    document.querySelectorAll('#edit-chars-box input[type="checkbox"]').forEach(cb => { cb.checked = !!checked; });
}

function closeEdit() {
    document.getElementById('modal-bg').classList.remove('show');
    EDIT_ID = null;
}

async function saveEdit() {
    const username = document.getElementById('edit-username').value.trim();
    const role = document.getElementById('edit-role').value;
    const password = document.getElementById('edit-password').value;
    const chars = Array.from(document.querySelectorAll('#edit-chars-box input:checked')).map(i => i.value);
    const err = document.getElementById('modal-error');
    err.style.display = 'none';

    if (!username) { err.textContent = 'Benutzername erforderlich'; err.style.display = 'block'; return; }
    if (!EDIT_ID && !password) { err.textContent = 'Passwort erforderlich'; err.style.display = 'block'; return; }

    try {
        let resp;
        if (EDIT_ID) {
            const body = { username, role, allowed_characters: chars };
            if (password) body.password = password;
            resp = await fetch('/auth/users/' + EDIT_ID, {
                method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
            });
        } else {
            resp = await fetch('/auth/users', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password, role, allowed_characters: chars })
            });
        }
        if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            err.textContent = d.detail || 'Fehler beim Speichern';
            err.style.display = 'block';
            return;
        }
        closeEdit();
        toast(EDIT_ID ? 'User aktualisiert' : 'User angelegt');
        await loadAll();
    } catch (e) {
        err.textContent = 'Verbindungsfehler: ' + e.message;
        err.style.display = 'block';
    }
}

async function deleteUser(userId) {
    const u = USERS.find(x => x.id === userId);
    if (!u) return;
    if (!confirm('User "' + u.username + '" wirklich loeschen?')) return;
    try {
        const resp = await fetch('/auth/users/' + userId, { method: 'DELETE' });
        if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            toast(d.detail || 'Fehler', 'error');
            return;
        }
        toast('User geloescht');
        await loadAll();
    } catch (e) { toast('Fehler: ' + e.message, 'error'); }
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"\\']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function toast(msg, type) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast ' + (type === 'error' ? 'error' : '') + ' show';
    setTimeout(() => t.classList.remove('show'), 2500);
}

loadAll();
</script>
</body>
</html>'''


def _build_outfit_rules_html() -> str:
    """Outfit-Regeln-Admin-Seite."""
    return '''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<title>Outfit-Regeln</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }
h1 { font-size: 18px; margin-bottom: 8px; color: #e6edf3; }
.hint { font-size: 12px; color: #8b949e; margin-bottom: 16px; }
.toolbar { margin-bottom: 14px; display: flex; gap: 8px; align-items: center; }
.btn { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; }
.btn:hover { background: #30363d; }
.btn-primary { background: #238636; border-color: #2ea043; color: #fff; }
.btn-primary:hover { background: #2ea043; }
.btn-danger { background: #da3633; border-color: #f85149; color: #fff; }
.btn-danger:hover { background: #b62324; }
.btn-sm { padding: 3px 8px; font-size: 12px; }
.add-row { display: flex; gap: 6px; margin-bottom: 12px; }
.add-row input { flex: 1; background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; padding: 6px 10px; border-radius: 6px; font-size: 13px; }

table { width: 100%; border-collapse: collapse; background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }
th, td { padding: 8px 10px; text-align: center; border-bottom: 1px solid #30363d; font-size: 12px; color: #c9d1d9; }
th { background: #1c2128; color: #8b949e; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; font-size: 11px; }
th.type-col, td.type-col { text-align: left; min-width: 150px; }
tr:last-child td { border-bottom: none; }
tr:hover { background: #1c2128; }
td.actions-col { text-align: right; white-space: nowrap; }
input[type="checkbox"] { cursor: pointer; }

.toast { position: fixed; bottom: 20px; right: 20px; background: #238636; color: #fff; padding: 10px 16px; border-radius: 6px; font-size: 13px; opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 2000; }
.toast.show { opacity: 1; }
.toast.error { background: #da3633; }
</style>
</head>
<body>

<h1>Outfit-Regeln</h1>
<p class="hint">Definiert welche Slots pro outfit_type angezogen sein MUESSEN. Auto-Fill bei Location-Wechsel nutzt diese Regeln. Character-Exceptions (pro Character, in der Garderobe) koennen einzelne Slots ueberschreiben.</p>

<div class="toolbar">
    <button class="btn btn-primary" onclick="saveAll()">Speichern</button>
    <span id="status" style="font-size:12px;color:#8b949e;"></span>
</div>

<div class="add-row">
    <input type="text" id="new-type" placeholder="Neuer outfit_type (z.B. 'streetwear')">
    <button class="btn" onclick="addType()">+ Hinzufuegen</button>
</div>

<table id="rules-table">
    <thead id="rules-thead"></thead>
    <tbody id="rules-tbody"><tr><td colspan="20">Lade...</td></tr></tbody>
</table>

<div class="toast" id="toast"></div>

<script>
let RULES = {};  // { type: {required: [slots]} }
let SLOTS = [];
// Anzeige-Reihenfolge wie in der Garderobe (SLOT_ORDER in script.js)
const SLOT_DISPLAY_ORDER = ['head', 'neck', 'outer', 'top', 'underwear_top', 'bottom', 'underwear_bottom', 'legs', 'feet'];

async function loadAll() {
    try {
        const resp = await fetch('/admin/outfit-rules/data');
        if (resp.status === 401 || resp.status === 403) {
            const ret = encodeURIComponent(window.location.pathname);
            window.location.href = '/?return=' + ret;
            return;
        }
        const data = await resp.json();
        RULES = data.outfit_types || {};
        const rawSlots = data.valid_slots || [];
        // Sortieren nach SLOT_DISPLAY_ORDER, unbekannte Slots ans Ende
        SLOTS = [...rawSlots].sort((a, b) => {
            const ia = SLOT_DISPLAY_ORDER.indexOf(a);
            const ib = SLOT_DISPLAY_ORDER.indexOf(b);
            return (ia === -1 ? 999 : ia) - (ib === -1 ? 999 : ib);
        });
        renderTable();
    } catch (e) {
        toast('Fehler: ' + e.message, 'error');
    }
}

function renderTable() {
    const thead = document.getElementById('rules-thead');
    const tbody = document.getElementById('rules-tbody');
    let th = '<tr><th class="type-col">outfit_type</th>';
    for (const s of SLOTS) th += '<th>' + escapeHtml(s) + '</th>';
    th += '<th class="actions-col"></th></tr>';
    thead.innerHTML = th;

    const types = Object.keys(RULES).sort();
    if (!types.length) {
        tbody.innerHTML = '<tr><td colspan="' + (SLOTS.length + 2) + '" style="text-align:center;color:#8b949e;">Noch keine outfit_types</td></tr>';
        return;
    }
    tbody.innerHTML = types.map(t => {
        const req = new Set((RULES[t] || {}).required || []);
        const desc = (RULES[t] || {}).description || '';
        let cells = '';
        for (const s of SLOTS) {
            cells += '<td><input type="checkbox" data-type="' + escapeHtml(t) + '" data-slot="' + escapeHtml(s) + '"' + (req.has(s) ? ' checked' : '') + ' onchange="onToggle(this)"></td>';
        }
        const rowMain = '<tr><td class="type-col"><b>' + escapeHtml(t) + '</b></td>' + cells +
               '<td class="actions-col"><button class="btn btn-sm btn-danger" onclick="deleteType(\\'' + t + '\\')">Del</button></td></tr>';
        const rowDesc = '<tr class="desc-row"><td colspan="' + (SLOTS.length + 2) + '" style="padding:2px 8px 10px 8px;">' +
               '<textarea data-type="' + escapeHtml(t) + '" rows="2" placeholder="Beschreibung fuer LLM (z.B. Club-Stil: eng, bauchfrei, neon/schwarz)" style="width:100%;font-size:12px;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:4px;padding:4px 6px;" onchange="onDescChange(this)">' +
               escapeHtml(desc) + '</textarea></td></tr>';
        return rowMain + rowDesc;
    }).join('');
}

function onDescChange(el) {
    const t = el.dataset.type;
    if (!RULES[t]) RULES[t] = { required: [] };
    RULES[t].description = el.value || '';
}

function onToggle(cb) {
    const t = cb.dataset.type;
    const s = cb.dataset.slot;
    if (!RULES[t]) RULES[t] = { required: [] };
    const req = new Set(RULES[t].required || []);
    if (cb.checked) req.add(s); else req.delete(s);
    RULES[t].required = Array.from(req);
}

function addType() {
    const el = document.getElementById('new-type');
    const name = (el.value || '').trim().toLowerCase();
    if (!name) return;
    if (RULES[name]) { toast('Typ existiert schon', 'error'); return; }
    RULES[name] = { required: [] };
    el.value = '';
    renderTable();
}

function deleteType(t) {
    if (!confirm('outfit_type \\'' + t + '\\' loeschen?')) return;
    delete RULES[t];
    renderTable();
}

async function saveAll() {
    const status = document.getElementById('status');
    status.textContent = 'Speichere...';
    status.style.color = '#8b949e';
    try {
        const r = await fetch('/admin/outfit-rules/data', {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ outfit_types: RULES }),
        });
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            status.textContent = 'Fehler: ' + (d.detail || r.status);
            status.style.color = '#f85149';
            return;
        }
        status.textContent = 'Gespeichert.';
        status.style.color = '#8fd17f';
        toast('Gespeichert');
    } catch (e) {
        status.textContent = 'Fehler: ' + e.message;
        status.style.color = '#f85149';
    }
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"\\']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function toast(msg, type) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast ' + (type === 'error' ? 'error' : '') + ' show';
    setTimeout(() => t.classList.remove('show'), 2000);
}

loadAll();
</script>
</body>
</html>'''
