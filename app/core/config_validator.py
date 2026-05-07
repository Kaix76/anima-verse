"""Configuration validator — checks for common misconfigurations.

Each check returns a list of issues: {"level": "error"|"warning", "section": str, "message": str}
"""
from pathlib import Path
from typing import Any, Dict, List

from app.core.log import get_logger

logger = get_logger("config_validator")


def validate_config(config: dict) -> List[Dict[str, Any]]:
    """Run all validation checks on the config. Returns a list of issues."""
    issues = []
    issues.extend(_check_providers(config))
    issues.extend(_check_llm_routing(config))
    issues.extend(_check_image_backends(config))
    issues.extend(_check_comfyui_workflows(config))
    issues.extend(_check_animation(config))
    issues.extend(_check_tts(config))
    issues.extend(_check_faceswap(config))
    issues.extend(_check_skills(config))
    issues.extend(_check_server(config))
    return issues


def _err(section: str, msg: str) -> dict:
    return {"level": "error", "section": section, "message": msg}


def _warn(section: str, msg: str) -> dict:
    return {"level": "warning", "section": section, "message": msg}


# ── Provider Checks ──

def _check_providers(config: dict) -> list:
    issues = []
    providers = config.get("providers", [])
    if not providers:
        issues.append(_err("providers", "Keine Provider konfiguriert"))
        return issues

    names = set()
    for i, p in enumerate(providers):
        name = p.get("name", "")
        ptype = p.get("type", "")
        api_base = p.get("api_base", "")
        api_key = p.get("api_key", "")

        if not name:
            issues.append(_err("providers", f"Provider #{i+1}: Name fehlt"))
        elif name in names:
            issues.append(_err("providers", f"Provider '{name}': Doppelter Name"))
        names.add(name)

        if not api_base:
            issues.append(_err("providers", f"Provider '{name}': API Base URL fehlt"))

        # API Key check for cloud providers
        if ptype == "anthropic" and (not api_key or api_key in ("not-needed", "YOUR_ANTHROPIC_API_KEY")):
            issues.append(_err("providers", f"Provider '{name}': Anthropic benoetigt einen gueltigen API Key"))
        if api_base and "together.xyz" in api_base and (not api_key or api_key == "not-needed"):
            issues.append(_err("providers", f"Provider '{name}': Together.ai benoetigt einen API Key"))
        if api_base and "api.anthropic.com" in api_base and (not api_key or api_key in ("not-needed", "YOUR_ANTHROPIC_API_KEY")):
            issues.append(_err("providers", f"Provider '{name}': API Key fehlt oder ist Platzhalter"))

    return issues


# ── LLM Routing Checks ──

def _check_llm_routing(config: dict) -> list:
    from app.core.llm_tasks import TASK_TYPES, is_task_gated_off
    issues = []
    routing = config.get("llm_routing", [])
    if not isinstance(routing, list):
        issues.append(_err("llm_routing", "llm_routing muss eine Liste sein"))
        return issues

    providers = config.get("providers", [])
    provider_names = {p.get("name", "") for p in providers}

    # Coverage: task -> [(order, entry_idx)]
    coverage: dict = {}
    # (task, order) -> [entry_idx]
    order_keys: dict = {}

    for idx, entry in enumerate(routing):
        if not isinstance(entry, dict):
            issues.append(_err("llm_routing", f"Eintrag #{idx+1}: kein Objekt"))
            continue
        # Disabled-Eintraege werden zur Laufzeit ignoriert -> keine
        # Coverage/Order-/Provider-Validierung (sonst meldet die UI Fehler
        # fuer bewusst stillgelegte Modelle).
        if entry.get("enabled") is False:
            continue
        provider = (entry.get("provider") or "").strip()
        model = (entry.get("model") or "").strip()
        label = model or f"#{idx+1}"
        if not provider:
            issues.append(_err("llm_routing", f"{label}: Provider fehlt"))
        elif provider not in provider_names:
            issues.append(_err("llm_routing", f"{label}: Provider '{provider}' existiert nicht"))
        if not model:
            issues.append(_err("llm_routing", f"Eintrag #{idx+1}: Model fehlt"))

        tasks = entry.get("tasks") or []
        if not isinstance(tasks, list):
            continue
        for t in tasks:
            if not isinstance(t, dict):
                continue
            tid = t.get("task")
            order = t.get("order")
            if not tid:
                continue
            coverage.setdefault(tid, []).append((order, idx))
            if order is not None:
                order_keys.setdefault((tid, int(order)), []).append(idx)

    # Order-Uniqueness
    for (tid, order), idxs in order_keys.items():
        if len(idxs) > 1:
            issues.append(_err(
                "llm_routing",
                f"Task '{tid}' Order {order} mehrfach vergeben (Eintraege {', '.join('#'+str(i+1) for i in idxs)})"
            ))

    # Coverage: jeder Task im Katalog braucht mindestens einen Eintrag — ausser Gate off
    for tid in TASK_TYPES.keys():
        if coverage.get(tid):
            continue
        if is_task_gated_off(tid, config):
            continue
        issues.append(_warn("llm_routing", f"Task '{tid}' hat keinen LLM-Eintrag"))

    # Unbekannte Tasks
    for tid in coverage.keys():
        if tid not in TASK_TYPES:
            issues.append(_warn("llm_routing", f"Unbekannter Task '{tid}' — nicht im Katalog"))

    return issues


# ── Image Backend Checks ──

def _check_image_backends(config: dict) -> list:
    issues = []
    ig = config.get("image_generation", {})
    backends = ig.get("backends", [])
    providers = config.get("providers", [])

    active_comfyui_backends = []
    for be in backends:
        name = be.get("name", "?")
        enabled = be.get("enabled", True)
        if not enabled:
            continue

        api_type = be.get("api_type", "")
        api_url = be.get("api_url", "")
        api_key = be.get("api_key", "")

        if not api_url:
            issues.append(_err("image_generation", f"Backend '{name}': API URL fehlt"))

        # Cloud backends need API key
        if api_type in ("mammouth", "civitai", "together") and not api_key:
            issues.append(_err("image_generation", f"Backend '{name}': API Key fehlt (Cloud-Backend '{api_type}')"))

        if api_type == "comfyui":
            active_comfyui_backends.append(name)

    # Aktive ComfyUI-Backends brauchen mindestens einen Provider mit einer GPU
    # vom Typ 'comfyui' — sonst wirft submit_gpu_task zur Laufzeit
    # "No channel for gpu_type='comfyui'".
    if active_comfyui_backends:
        has_comfyui_channel = any(
            any("comfyui" in (g.get("types") or []) for g in (p.get("gpus") or []))
            for p in providers
        )
        if not has_comfyui_channel:
            be_list = ", ".join(active_comfyui_backends)
            issues.append(_err(
                "providers",
                f"Aktive ComfyUI-Backends ({be_list}), aber kein Provider hat eine GPU "
                "mit types=['comfyui']. Bildgenerierung schlaegt mit "
                "\"No channel for gpu_type='comfyui'\" fehl. "
                "Bei einem Provider unter 'GPUs' den types-Eintrag um 'comfyui' erweitern."))

    return issues


# ── ComfyUI Workflow Checks ──

def _check_comfyui_workflows(config: dict) -> list:
    issues = []
    ig = config.get("image_generation", {})
    workflows = ig.get("comfyui_workflows", {})
    backends = ig.get("backends", [])
    comfy_backend_names = {b.get("name", "") for b in backends if b.get("api_type") == "comfyui"}

    # Check default workflow exists (case-insensitive match against workflow IDs and names)
    default_wf = ig.get("comfy_default_workflow", "")
    if default_wf:
        wf_names = {wid: wf.get("name", wid) for wid, wf in workflows.items()}
        match = default_wf in workflows or any(
            default_wf.lower() == n.lower() for n in wf_names.values()
        )
        if not match:
            available = ", ".join(wf_names.values()) if wf_names else "keine"
            issues.append(_warn("image_generation", f"Default ComfyUI Workflow '{default_wf}' existiert nicht (verfuegbar: {available})"))

    for wid, wf in workflows.items():
        name = wf.get("name", wid)
        wf_file = wf.get("workflow_file", "")

        # Workflow file exists?
        if wf_file and not Path(wf_file).exists():
            issues.append(_err("image_generation", f"Workflow '{name}': Datei '{wf_file}' nicht gefunden"))

        # Backend reference
        skill = wf.get("skill", "")
        if skill and skill not in comfy_backend_names:
            issues.append(_warn("image_generation", f"Workflow '{name}': Backend '{skill}' ist kein ComfyUI-Backend"))

        # Model set?
        if not wf.get("model", ""):
            issues.append(_warn("image_generation", f"Workflow '{name}': Kein Model konfiguriert"))

    # Check imagegen_default references
    for field_name, label in [
        ("outfit_imagegen_default", "Outfit"),
        ("expression_imagegen_default", "Expression"),
        ("location_imagegen_default", "Location"),
    ]:
        val = ig.get(field_name, "")
        if val:
            _check_imagegen_ref(val, workflows, backends, "image_generation", label, issues)

    return issues


def _check_imagegen_ref(val: str, workflows: dict, backends: list, section: str, label: str, issues: list):
    """Validate a 'workflow:Name' or 'backend:Name' reference."""
    if ":" not in val:
        return
    ref_type, ref_name = val.split(":", 1)
    if ref_type == "workflow":
        # Check by name (not by key)
        wf_names = {wf.get("name", k) for k, wf in workflows.items()}
        wf_keys = set(workflows.keys())
        if ref_name not in wf_names and ref_name not in wf_keys:
            issues.append(_warn(section, f"{label} Default: Workflow '{ref_name}' existiert nicht"))
    elif ref_type == "backend":
        be_names = {b.get("name", "") for b in backends}
        if ref_name not in be_names:
            issues.append(_warn(section, f"{label} Default: Backend '{ref_name}' existiert nicht"))


# ── Animation Checks ──

def _check_animation(config: dict) -> list:
    issues = []
    anim = config.get("animation", {})

    comfy = anim.get("comfy", {})
    if comfy.get("enabled"):
        if not comfy.get("workflow_file"):
            issues.append(_err("animation", "ComfyUI Animation: Workflow Datei fehlt"))
        elif not Path(comfy["workflow_file"]).exists():
            issues.append(_err("animation", f"ComfyUI Animation: Datei '{comfy['workflow_file']}' nicht gefunden"))
        if not comfy.get("unet_high") and not comfy.get("unet_low"):
            issues.append(_warn("animation", "ComfyUI Animation: Kein UNet Model konfiguriert"))

    together = anim.get("together", {})
    if together.get("enabled"):
        if not together.get("model"):
            issues.append(_warn("animation", "Together Animation: Kein Model konfiguriert"))

    return issues


# ── TTS Checks ──

def _check_tts(config: dict) -> list:
    issues = []
    tts = config.get("tts", {})
    if not tts.get("enabled"):
        return issues

    backend = tts.get("backend", "")

    if backend == "xtts":
        xtts = tts.get("xtts", {})
        if not xtts.get("url"):
            issues.append(_err("tts", "XTTS: URL fehlt"))

    elif backend == "f5":
        f5 = tts.get("f5", {})
        if not f5.get("url"):
            issues.append(_err("tts", "F5-TTS: URL fehlt"))

    elif backend == "magpie":
        magpie = tts.get("magpie", {})
        if not magpie.get("url"):
            issues.append(_err("tts", "Magpie: URL fehlt"))
        if not magpie.get("voice"):
            issues.append(_warn("tts", "Magpie: Keine Stimme konfiguriert"))

    elif backend == "comfyui":
        comfy_tts = tts.get("comfyui", {})
        skill = comfy_tts.get("skill", "")
        if not skill:
            issues.append(_err("tts", "ComfyUI TTS: Kein Backend konfiguriert"))
        else:
            ig = config.get("image_generation", {})
            ig_backends = ig.get("backends", [])
            # skill can be comma-separated list of backend names
            skill_names = [s.strip() for s in skill.split(",") if s.strip()] if isinstance(skill, str) else skill if isinstance(skill, list) else []
            for sn in skill_names:
                be = next((b for b in ig_backends if b.get("name") == sn), None)
                if not be:
                    issues.append(_err("tts", f"ComfyUI TTS: Backend '{sn}' existiert nicht"))
                elif be.get("api_type") != "comfyui":
                    issues.append(_err("tts", f"ComfyUI TTS: Backend '{sn}' ist kein ComfyUI-Backend (Typ: {be.get('api_type', '?')})"))

    return issues


# ── FaceSwap Checks ──

def _check_faceswap(config: dict) -> list:
    issues = []
    fs = config.get("faceswap", {})

    wf = fs.get("comfy_workflow_file", "")
    if wf and not Path(wf).exists():
        issues.append(_warn("faceswap", f"ComfyUI FaceSwap: Workflow '{wf}' nicht gefunden"))

    model = fs.get("service_model_path", "")
    if model and not Path(model).exists():
        issues.append(_warn("faceswap", f"Face Service: Model '{model}' nicht gefunden"))

    return issues


# ── Skills Checks ──

def _check_skills(config: dict) -> list:
    issues = []
    skills = config.get("skills", {})

    searx = skills.get("searx", {})
    if searx.get("enabled") and not searx.get("url"):
        issues.append(_err("skills", "SearX: Aktiviert aber keine URL konfiguriert"))

    return issues


# ── Server Checks ──

def _check_server(config: dict) -> list:
    issues = []
    server = config.get("server", {})

    jwt = server.get("jwt_secret", "")
    if not jwt or jwt == "your-secret-key-change-in-production":
        issues.append(_warn("server", "JWT Secret ist der Default-Wert — bitte fuer Production aendern"))

    # Story engine references
    se = config.get("story_engine", {})
    if se.get("enabled"):
        val = se.get("imagegen_default", "")
        if val:
            ig = config.get("image_generation", {})
            _check_imagegen_ref(val, ig.get("comfyui_workflows", {}), ig.get("backends", []), "story_engine", "Story Engine", issues)

    # Instagram reference
    insta = config.get("skills", {}).get("instagram", {})
    if insta.get("enabled"):
        val = insta.get("imagegen_default", "")
        if val:
            ig = config.get("image_generation", {})
            _check_imagegen_ref(val, ig.get("comfyui_workflows", {}), ig.get("backends", []), "skills", "Instagram", issues)

    return issues
