"""Centralized JSON-based configuration module.

Replaces .env / python-dotenv with a single storage/config.json file.
Provides get(dotpath), get_section(dotpath), and a backward-compatibility
bridge that populates os.environ so that existing code keeps working
during the migration phase.
"""
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

from app.core.log import get_logger

logger = get_logger("config")

_CONFIG: dict = {}
# Mutable — updated by load() when an explicit path is passed
_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "storage" / "config.json"
_SECRETS_PATH: Optional[Path] = None  # set in load() — sibling of _CONFIG_PATH

# Fields that contain sensitive data (API keys, passwords, secrets)
SENSITIVE_FIELDS = {
    "api_key", "password", "jwt_secret", "bot_token", "secret",
}


def _is_sensitive(key: str) -> bool:
    """Check if a config key name is sensitive."""
    return key in SENSITIVE_FIELDS


# Default-Workflows fuer neue Welten. Werte 1:1 aus den produktiv erprobten
# Hotopia-Workflows. Beim ersten Load einer Welt ohne `comfyui_workflows`
# werden diese drei Templates eingespielt — der User muss nur noch Backend
# (skill) und ggf. Modell-Dateien bestaetigen.
_DEFAULT_COMFYUI_WORKFLOWS = {
    "Qwen": {
        "name": "Qwen",
        "image_model": "qwen",
        "faceswap_needed": False,
        "filter": "Qwen*",
        "width": 1024,
        "height": 1024,
        "workflow_file": "./workflows/text2img_workflow_qwen_api.json",
        "vram_required": 22,
        "prompt_style": "photograph, shot on iPhone 15 Pro, natural window light, skin texture, unedited, detailed anatomy, 8k, high detail, \n",
        "prompt_negative": "illustration, anime, cgi, 3d render, painting, airbrushed skin, plastic skin, smooth flawless skin, overexposed, glossy, fantasy, studio lighting, posed, cartoon, drawing, sketch, watermark, signature, text, logo, deformed, blurry, low quality\n",
        "prompt_instruction": "Write a natural-language descriptive prompt (not tags). Describe the scene as a flowing sentence with rich detail about the setting, characters, poses, and mood. Avoid comma-separated tag lists.",
        "loras": [{"file": "", "strength": 1} for _ in range(4)],
    },
    "Z-Image": {
        "name": "Z-Image",
        "image_model": "z_image",
        "faceswap_needed": True,
        "filter": "Z-Image*",
        "width": 1024,
        "height": 1024,
        "workflow_file": "./workflows/text2img_workflow_z-image_api.json",
        "vram_required": 14,
        "prompt_style": "RAW photo, amateur photograph, 35mm, natural light, skin texture, visible pores, detailed anatomy, 8k, high detail, \n",
        "prompt_negative": "illustration, anime, cgi, 3d render, painting, airbrushed skin, plastic skin, smooth flawless skin, overexposed, glossy, fantasy, studio lighting, posed, cartoon, drawing, sketch, watermark, signature, text, logo, deformed, blurry, low quality\n",
        "prompt_instruction": "Write a tag-based prompt with comma-separated keywords. Use quality tags like \"masterpiece, best quality\". Describe pose, lighting, and setting as short tags.",
        "loras": [{"file": "", "strength": 1} for _ in range(4)],
    },
    "Flux": {
        "name": "Flux",
        "image_model": "flux",
        "faceswap_needed": True,
        "filter": "Flux.2-9B*",
        "width": 1024,
        "height": 1024,
        "workflow_file": "./workflows/text2img_workflow_flux2_api.json",
        "vram_required": 15,
        "prompt_style": "a candid photograph taken with a 35mm lens, natural indoor lighting, skin with visible pores and texture, detailed anatomy, 8k, high detail, ",
        "prompt_negative": "illustration, anime, cgi, 3d render, painting, airbrushed skin, plastic skin, smooth flawless skin, overexposed, glossy, fantasy, studio lighting, posed, cartoon, drawing, sketch, watermark, signature, text, logo, deformed, blurry, low quality\n",
        "prompt_instruction": "Write a natural-language descriptive prompt for a Flux 2 Klein model. Describe the scene in flowing detail — subject, pose, environment, lighting, mood. Flux understands natural language well, so be descriptive and avoid tag lists.",
        "loras": [{"file": "", "strength": 1} for _ in range(4)],
    },
}


def _seed_default_workflows(config: dict, config_path: Path) -> bool:
    """Seedet bei einer neuen Welt die drei Default-Workflows (Qwen/Z-Image/Flux).

    Idempotent: nur wenn `image_generation.comfyui_workflows` gar nicht existiert.
    Wenn der User die Workflows absichtlich auf {} geleert hat, bleibt es leer.
    Returns True wenn etwas geschrieben wurde.
    """
    ig = config.setdefault("image_generation", {})
    if "comfyui_workflows" in ig:
        return False
    import copy
    ig["comfyui_workflows"] = copy.deepcopy(_DEFAULT_COMFYUI_WORKFLOWS)
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        logger.info("Default ComfyUI workflows (Qwen/Z-Image/Flux) seeded -> %s", config_path)
        return True
    except OSError as e:
        logger.error("Failed to seed default workflows to %s: %s", config_path, e)
        return False


def load(config_path: Optional[Path] = None) -> dict:
    """Load configuration from JSON file, then overlay secrets.json on top.

    secrets.json (sibling of config.json) holds sensitive fields and is gitignored.
    Falls back to empty config if config.json doesn't exist.
    Also populates os.environ for backward compatibility.
    """
    global _CONFIG, _CONFIG_PATH, _SECRETS_PATH
    if config_path:
        _CONFIG_PATH = Path(config_path)
    _SECRETS_PATH = _CONFIG_PATH.parent / "secrets.json"
    path = _CONFIG_PATH

    if not path.exists():
        logger.warning("Config file not found: %s — using empty config", path)
        _CONFIG = {}
    else:
        try:
            with open(path, "r", encoding="utf-8") as f:
                _CONFIG = json.load(f)
            logger.info("Config loaded from %s", path)
        except (json.JSONDecodeError, IOError) as e:
            logger.error("Failed to load config from %s: %s", path, e)
            _CONFIG = {}

    # Bei einer neuen/leeren Welt: Default-Workflows fuer Qwen/Z-Image/Flux
    # automatisch eintragen, damit der Admin nicht alles von Hand anlegen muss.
    _seed_default_workflows(_CONFIG, path)

    # Overlay secrets.json (gitignored — holds api keys / passwords)
    if _SECRETS_PATH.exists():
        try:
            with open(_SECRETS_PATH, "r", encoding="utf-8") as f:
                secrets = json.load(f)
            _deep_merge(_CONFIG, secrets)
            logger.info("Secrets overlaid from %s", _SECRETS_PATH)
        except (json.JSONDecodeError, IOError) as e:
            logger.error("Failed to load secrets from %s: %s", _SECRETS_PATH, e)

    # Populate os.environ for backward compatibility
    _flatten_to_env(_CONFIG)

    return _CONFIG


def _deep_merge(base: Any, overlay: Any) -> None:
    """In-place deep merge of overlay into base. Lists are merged element-wise by index."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        for k, v in overlay.items():
            if k in base and isinstance(base[k], (dict, list)) and isinstance(v, (dict, list)):
                _deep_merge(base[k], v)
            else:
                base[k] = v
    elif isinstance(base, list) and isinstance(overlay, list):
        for i, v in enumerate(overlay):
            if i < len(base):
                if isinstance(base[i], (dict, list)) and isinstance(v, (dict, list)):
                    _deep_merge(base[i], v)
                else:
                    base[i] = v


def _split_secrets(data: Any) -> tuple:
    """Walk data and split sensitive values out.

    Returns (clean, secrets). Sensitive string values are blanked in clean and
    placed into a parallel structure in secrets. Lists keep position; entries
    without secrets become empty dicts/lists in the secrets shape and are
    pruned at the top level if entirely empty.
    """
    if isinstance(data, dict):
        clean: dict = {}
        secrets: dict = {}
        for k, v in data.items():
            if _is_sensitive(k) and isinstance(v, str):
                if v:
                    secrets[k] = v
                clean[k] = ""
            elif isinstance(v, (dict, list)):
                sub_clean, sub_secrets = _split_secrets(v)
                clean[k] = sub_clean
                if sub_secrets:
                    secrets[k] = sub_secrets
            else:
                clean[k] = v
        return clean, secrets

    if isinstance(data, list):
        clean_list: list = []
        secrets_list: list = []
        any_secrets = False
        for item in data:
            sub_clean, sub_secrets = _split_secrets(item)
            clean_list.append(sub_clean)
            secrets_list.append(sub_secrets if sub_secrets else {})
            if sub_secrets:
                any_secrets = True
        return clean_list, (secrets_list if any_secrets else [])

    return data, None


def reload() -> dict:
    """Reload configuration from disk (uses same path as last load)."""
    return load()


def get(path: str, default: Any = None) -> Any:
    """Get a config value by dot-notation path.

    Examples:
        config.get("tts.backend")
        config.get("providers[0].name")
        config.get("llm_routing")
    """
    try:
        return _resolve_path(_CONFIG, path)
    except (KeyError, IndexError, TypeError):
        return default


def get_section(path: str) -> dict:
    """Get a config section as a dict."""
    result = get(path, {})
    if isinstance(result, dict):
        return dict(result)
    return {}


def get_all() -> dict:
    """Return the full config dict (for admin API)."""
    return dict(_CONFIG)


def save(data: dict, config_path: Optional[Path] = None) -> None:
    """Save configuration. Sensitive fields are split out into secrets.json (gitignored)."""
    global _CONFIG
    path = config_path or _CONFIG_PATH
    secrets_path = path.parent / "secrets.json"

    path.parent.mkdir(parents=True, exist_ok=True)

    clean, secrets = _split_secrets(data)

    _atomic_write_json(path, clean)
    logger.info("Config saved to %s", path)

    if secrets:
        _atomic_write_json(secrets_path, secrets)
        logger.info("Secrets saved to %s", secrets_path)
    elif secrets_path.exists():
        try:
            secrets_path.unlink()
        except OSError:
            pass

    _CONFIG = data


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically (temp file + rename)."""
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        Path(tmp_path).rename(path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def mask_sensitive(data: Any, _key: str = "") -> Any:
    """Return a copy of data with sensitive values masked for display."""
    if isinstance(data, dict):
        result = {}
        for k, v in data.items():
            result[k] = mask_sensitive(v, k)
        return result
    if isinstance(data, list):
        return [mask_sensitive(item, _key) for item in data]
    if _is_sensitive(_key) and isinstance(data, str) and len(data) > 4:
        return "***" + data[-4:]
    return data


def _resolve_path(obj: Any, path: str) -> Any:
    """Resolve a dot-notation path with optional array indices."""
    parts = re.split(r'\.|\[(\d+)\]', path)
    parts = [p for p in parts if p is not None and p != ""]
    current = obj
    for part in parts:
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            # Try int key first if it looks like a number
            if part.isdigit() and part not in current:
                current = current[int(part)]
            else:
                current = current[part]
        else:
            raise KeyError(part)
    return current


# ── Backward Compatibility: Flatten JSON to os.environ ──

def _flatten_to_env(config: dict) -> None:
    """Flatten JSON config into os.environ for backward compatibility.

    This maps the structured JSON back into the flat PROVIDER_1_NAME etc.
    format that existing code expects via os.environ.get() / os.getenv().
    """
    env = {}

    # Server
    server = config.get("server", {})
    _set(env, "LOG_LEVEL", server.get("log_level", "INFO"))
    _set(env, "JWT_SECRET", server.get("jwt_secret", ""))
    _set(env, "STORAGE_DIR", server.get("storage_dir", "./storage"))

    # Beszel
    beszel = config.get("beszel", {})
    _set(env, "BESZEL_URL", beszel.get("url", ""))
    _set(env, "BESZEL_TOKEN", beszel.get("token", ""))

    # Providers (1-indexed)
    for i, prov in enumerate(config.get("providers", []), start=1):
        p = f"PROVIDER_{i}_"
        _set(env, f"{p}NAME", prov.get("name", ""))
        _set(env, f"{p}TYPE", prov.get("type", "openai"))
        _set(env, f"{p}API_BASE", prov.get("api_base", ""))
        _set(env, f"{p}API_KEY", prov.get("api_key", ""))
        _set(env, f"{p}TIMEOUT", prov.get("timeout", 120))
        _set(env, f"{p}MAX_CONCURRENT", prov.get("max_concurrent", 1))
        _set(env, f"{p}BESZEL_SYSTEM_ID", prov.get("beszel_system_id", ""))
        for g, gpu in enumerate(prov.get("gpus", [])):
            gp = f"{p}GPU{g}_"
            _set(env, f"{gp}VRAM", gpu.get("vram_gb", 0))
            _set(env, f"{gp}DEVICE", gpu.get("device", str(g)))
            types = gpu.get("types", [])
            _set(env, f"{gp}TYPE", ",".join(types) if isinstance(types, list) else types)
            _set(env, f"{gp}LABEL", gpu.get("label", ""))
            _set(env, f"{gp}MATCH_NAME", gpu.get("match_name", ""))
            _set(env, f"{gp}MAX_CONCURRENT", gpu.get("max_concurrent", 1))

    # Memory Thresholds (3-Stufen-System)
    memory = config.get("memory", {})
    _set(env, "MEMORY_SHORT_TERM_DAYS", memory.get("short_term_days", 3))
    _set(env, "MEMORY_MID_TERM_DAYS", memory.get("mid_term_days", 30))
    _set(env, "MEMORY_LONG_TERM_DAYS", memory.get("long_term_days", 90))
    _set(env, "CHAT_HISTORY_MAX_MESSAGES", memory.get("max_messages", 100))
    _set(env, "CHAT_SESSION_GAP_HOURS", memory.get("session_gap_hours", 4))
    _set(env, "MEMORY_MAX_SEMANTIC", memory.get("max_semantic", 50))
    _set(env, "MEMORY_COMMITMENT_MAX_DAYS", memory.get("commitment_max_days", 5))
    _set(env, "MEMORY_COMMITMENT_COMPLETED_DAYS", memory.get("commitment_completed_days", 3))

    # Image Generation
    ig = config.get("image_generation", {})
    _set(env, "SKILL_IMAGEGEN_ENABLED", ig.get("enabled", True))
    _set(env, "SKILL_IMAGEGEN_NAME", ig.get("name", "ImageGenerator"))
    _set(env, "SKILL_IMAGEGEN_DESCRIPTION", ig.get("description", ""))
    _set(env, "COMFY_IMAGEGEN_DEFAULT", ig.get("comfy_default_workflow", ""))
    _set(env, "OUTFIT_IMAGE_PROMPT_PREFIX", ig.get("outfit_image_prompt_prefix", ""))
    _set(env, "OUTFIT_IMAGE_WIDTH", ig.get("outfit_image_width", 832))
    _set(env, "OUTFIT_IMAGE_HEIGHT", ig.get("outfit_image_height", 1216))
    _set(env, "OUTFIT_IMAGEGEN_DEFAULT", ig.get("outfit_imagegen_default", ""))
    _set(env, "EXPRESSION_IMAGEGEN_DEFAULT", ig.get("expression_imagegen_default", ""))
    _set(env, "LOCATION_IMAGEGEN_DEFAULT", ig.get("location_imagegen_default", ""))
    _set(env, "PROFILE_IMAGE_PROMPT_PREFIX", ig.get("profile_image_prompt_prefix", ""))
    _set(env, "U2NET_HOME", ig.get("u2net_home", "./models/u2net"))
    _set(env, "REBUILD_LLM_SYSTEM_TEMPLATE", ig.get("rebuild_llm_system_template", ""))
    _set(env, "IMAGE_ANALYSIS_PROMPT", ig.get("image_analysis_prompt", ""))

    # ImageGen Backends (1-indexed)
    for i, be in enumerate(ig.get("backends", []), start=1):
        p = f"SKILL_IMAGEGEN_{i}_"
        for key in ["name", "enabled", "api_type", "api_url", "api_key", "model",
                     "cost", "width", "height", "faceswap_needed", "prompt_prefix",
                     "negative_prompt", "guidance_scale", "num_inference_steps",
                     "sampling_method", "schedule_type", "vram_required",
                     "checkpoint", "poll_interval", "max_wait", "disable_safety",
                     "scheduler", "clip_skip",
                     "fallback_mode", "fallback_specific"]:
            val = be.get(key, "")
            _set(env, f"{p}{key.upper()}", val)

    # ComfyUI Workflows
    for wid, wf in ig.get("comfyui_workflows", {}).items():
        p = f"COMFY_IMAGEGEN_{wid}_"
        for key in ["name", "filter", "skill", "workflow_file", "faceswap_needed",
                     "model", "prompt_style", "prompt_negative", "image_model",
                     "prompt_instruction", "vram_required", "width", "height",
                     "clip", "fallback_specific"]:
            val = wf.get(key, "")
            _set(env, f"{p}{key.upper()}", val)
        # LoRAs
        for idx, lora in enumerate(wf.get("loras", []), start=1):
            _set(env, f"{p}LORA_{idx:02d}", lora.get("file", ""))
            _set(env, f"{p}LORA_{idx:02d}_STRENGTH", lora.get("strength", 1))

    # FaceSwap / MultiSwap
    fs = config.get("faceswap", {})
    _set(env, "DEFAULT_SWAP_MODE", fs.get("default_swap_mode", "comfyui"))
    _set(env, "COMFY_FACESWAP_WORKFLOW_FILE", fs.get("comfy_workflow_file", ""))
    _set(env, "COMFY_FACESWAP_BACKEND", fs.get("comfy_backend", ""))
    _set(env, "COMFY_FACESWAP_VRAM_REQUIRED", fs.get("comfy_vram_required", 8))
    _set(env, "COMFY_MULTISWAP_WORKFLOW_FILE", fs.get("multiswap_workflow_file", "./workflows/multiswap_flux2_api.json"))
    _set(env, "COMFY_MULTISWAP_BACKEND", fs.get("multiswap_backend", ""))
    _set(env, "COMFY_MULTISWAP_UNET", fs.get("multiswap_unet", ""))
    _set(env, "COMFY_MULTISWAP_CLIP", fs.get("multiswap_clip", ""))
    _set(env, "FACE_SERVICE_URL", fs.get("service_url", "http://localhost:8005"))
    _set(env, "FACE_SERVICE_PORT", fs.get("service_port", 8005))
    _set(env, "FACE_SERVICE_MODEL_PATH", fs.get("service_model_path", ""))
    _set(env, "FACE_SERVICE_DET_SIZE", fs.get("service_det_size", 640))
    _set(env, "FACE_SERVICE_OMP_NUM_THREADS", fs.get("service_omp_num_threads", 4))
    _set(env, "FACE_SERVICE_ENABLED", fs.get("service_enabled", True))
    _set(env, "FACE_SERVICE_DEBUG", fs.get("service_debug", False))

    # Face Enhance
    fe = config.get("face_enhance", {})
    _set(env, "FACE_ENHANCE_ENABLED", fe.get("enabled", True))
    _set(env, "FACE_ENHANCE_MODEL_PATH", fe.get("model_path", ""))
    _set(env, "FACE_ENHANCE_BLEND", fe.get("blend", 1.0))
    _set(env, "FACE_ENHANCE_CODEFORMER_WEIGHT", fe.get("codeformer_weight", 0.7))
    _set(env, "FACE_ENHANCE_COLOR_CORRECTION", fe.get("color_correction", True))
    _set(env, "FACE_ENHANCE_SHARPEN", fe.get("sharpen", True))
    _set(env, "FACE_ENHANCE_SHARPEN_STRENGTH", fe.get("sharpen_strength", 0.5))

    # Animation
    anim = config.get("animation", {})
    comfy_a = anim.get("comfy", {})
    _set(env, "COMFY_ANIMATE_ENABLED", comfy_a.get("enabled", False))
    _set(env, "COMFY_ANIMATE_WORKFLOW_FILE", comfy_a.get("workflow_file", ""))
    _set(env, "COMFY_ANIMATE_BACKEND", comfy_a.get("backend", ""))
    _set(env, "COMFY_ANIMATE_UNET_HIGH", comfy_a.get("unet_high", ""))
    _set(env, "COMFY_ANIMATE_UNET_LOW", comfy_a.get("unet_low", ""))
    _set(env, "COMFY_ANIMATE_CLIP", comfy_a.get("clip", ""))
    _set(env, "COMFY_ANIMATE_WIDTH", comfy_a.get("width", 640))
    _set(env, "COMFY_ANIMATE_HEIGHT", comfy_a.get("height", 640))
    _set(env, "COMFY_ANIMATE_POLL_INTERVAL", comfy_a.get("poll_interval", 3.0))
    _set(env, "COMFY_ANIMATE_MAX_WAIT", comfy_a.get("max_wait", 600))
    for variant in ["high", "low"]:
        for idx, lora in enumerate(comfy_a.get(f"loras_{variant}", []), start=1):
            _set(env, f"COMFY_ANIMATE_LORA_{variant.upper()}_{idx:02d}", lora.get("file", ""))
            _set(env, f"COMFY_ANIMATE_LORA_{variant.upper()}_{idx:02d}_STRENGTH", lora.get("strength", 1))

    tog = anim.get("together", {})
    _set(env, "TOGETHER_ANIMATE_ENABLED", tog.get("enabled", False))
    _set(env, "TOGETHER_ANIMATE_LABEL", tog.get("label", ""))
    _set(env, "TOGETHER_ANIMATE_API_KEY", tog.get("api_key", ""))
    _set(env, "TOGETHER_ANIMATE_MODEL", tog.get("model", ""))
    _set(env, "TOGETHER_ANIMATE_WIDTH", tog.get("width", 720))
    _set(env, "TOGETHER_ANIMATE_HEIGHT", tog.get("height", 720))
    _set(env, "TOGETHER_ANIMATE_SECONDS", tog.get("seconds", 5))
    _set(env, "TOGETHER_ANIMATE_POLL_INTERVAL", tog.get("poll_interval", 5.0))
    _set(env, "TOGETHER_ANIMATE_MAX_WAIT", tog.get("max_wait", 600))

    # TTS
    tts = config.get("tts", {})
    _set(env, "TTS_ENABLED", tts.get("enabled", False))
    _set(env, "TTS_AUTO", tts.get("auto", False))
    _set(env, "TTS_CHUNK_SIZE", tts.get("chunk_size", 300))
    _set(env, "TTS_BACKEND", tts.get("backend", "xtts"))
    _set(env, "TTS_FALLBACK_BACKEND", tts.get("fallback_backend", ""))

    xtts = tts.get("xtts", {})
    _set(env, "TTS_XTTS_URL", xtts.get("url", ""))
    _set(env, "TTS_XTTS_SPEAKER_WAV", xtts.get("speaker_wav", ""))
    _set(env, "TTS_XTTS_LANGUAGE", xtts.get("language", "de"))

    magpie = tts.get("magpie", {})
    _set(env, "TTS_MAGPIE_URL", magpie.get("url", ""))
    _set(env, "TTS_MAGPIE_VOICE", magpie.get("voice", ""))
    _set(env, "TTS_MAGPIE_LANGUAGE", magpie.get("language", "de-DE"))

    f5 = tts.get("f5", {})
    _set(env, "TTS_F5_URL", f5.get("url", ""))
    _set(env, "TTS_F5_REF_AUDIO", f5.get("ref_audio", ""))
    _set(env, "TTS_F5_REF_TEXT", f5.get("ref_text", ""))
    _set(env, "TTS_F5_SPEED", f5.get("speed", 1.0))
    _set(env, "TTS_F5_REMOVE_SILENCE", f5.get("remove_silence", False))
    _set(env, "TTS_F5_NFE_STEPS", f5.get("nfe_steps", 32))
    _set(env, "TTS_F5_CUSTOM_CFG", f5.get("custom_cfg", ""))
    for lang, ldata in f5.get("languages", {}).items():
        ul = lang.upper()
        _set(env, f"TTS_F5_MODEL_{ul}", ldata.get("model", ""))
        _set(env, f"TTS_F5_VOCAB_{ul}", ldata.get("vocab", ""))
        _set(env, f"TTS_F5_CFG_{ul}", ldata.get("cfg", ""))

    comfy_tts = tts.get("comfyui", {})
    _set(env, "TTS_COMFYUI_SKILL", comfy_tts.get("skill", ""))
    _set(env, "TTS_COMFYUI_MODE", comfy_tts.get("mode", "auto"))
    _set(env, "TTS_COMFYUI_WORKFLOW_VOICECLONE", comfy_tts.get("workflow_voiceclone", ""))
    _set(env, "TTS_COMFYUI_WORKFLOW_VOICEDESC", comfy_tts.get("workflow_voicedesc", ""))
    _set(env, "TTS_COMFYUI_WORKFLOW_VOICENAME", comfy_tts.get("workflow_voicename", ""))
    _set(env, "TTS_COMFYUI_VRAM_REQUIRED", comfy_tts.get("vram_required", ""))
    _set(env, "TTS_COMFYUI_MAX_WAIT", comfy_tts.get("max_wait", 300))
    _set(env, "TTS_COMFYUI_POLL_INTERVAL", comfy_tts.get("poll_interval", 1.0))

    # Skills
    skills = config.get("skills", {})

    searx = skills.get("searx", {})
    _set(env, "SKILL_SEARX_ENABLED", searx.get("enabled", False))
    _set(env, "SKILL_SEARX_URL", searx.get("url", ""))
    _set(env, "SKILL_SEARX_NAME", searx.get("name", "WebSearch"))
    _set(env, "SKILL_SEARX_DESCRIPTION", searx.get("description", ""))
    _set(env, "SKILL_SEARX_ENGINES", searx.get("engines", ""))
    _set(env, "SKILL_SEARX_CATEGORIES", searx.get("categories", ""))
    _set(env, "SKILL_SEARX_NUM_RESULTS", searx.get("num_results", 5))

    insta = skills.get("instagram", {})
    _set(env, "SKILL_INSTAGRAM_ENABLED", insta.get("enabled", False))
    _set(env, "SKILL_INSTAGRAM_NAME", insta.get("name", "Instagram"))
    _set(env, "SKILL_INSTAGRAM_DESCRIPTION", insta.get("description", ""))
    _set(env, "SKILL_INSTAGRAM_CAPTION_LANGUAGE", insta.get("caption_language", "en"))
    _set(env, "SKILL_INSTAGRAM_DEFAULT_POPULARITY", insta.get("default_popularity", 50))
    _set(env, "SKILL_INSTAGRAM_IMAGEGEN_DEFAULT", insta.get("imagegen_default", ""))

    for skill_key, env_prefix_map in [
        ("set_location", "SKILL_SETLOCATION"),
        ("set_activity", "SKILL_SETACTIVITY"),
        ("set_mood", "SKILL_SETMOOD"),
        ("talk_to", "SKILL_TALK_TO"),
        ("send_message", "SKILL_SEND_MESSAGE"),
        ("act", "SKILL_ACT"),
    ]:
        s = skills.get(skill_key, {})
        _set(env, f"{env_prefix_map}_ENABLED", s.get("enabled", True))
        _set(env, f"{env_prefix_map}_NAME", s.get("name", ""))
        _set(env, f"{env_prefix_map}_DESCRIPTION", s.get("description", ""))

    oc = skills.get("outfit_change", {})
    _set(env, "SKILL_OUTFIT_CHANGE_NAME", oc.get("name", "ChangeOutfit"))
    _set(env, "SKILL_OUTFIT_CHANGE_DESCRIPTION", oc.get("description", ""))
    _set(env, "SKILL_OUTFIT_CHANGE_GENERATE_IMAGE", oc.get("generate_image", True))
    _set(env, "SKILL_OUTFIT_CHANGE_LANGUAGE", oc.get("language", "en"))
    _set(env, "SKILL_OUTFIT_CHANGE_MAX_OUTFITS", oc.get("max_outfits", 10))
    _set(env, "OUTFIT_CHANGE_COOLDOWN_MINUTES", oc.get("cooldown_minutes", 120))

    mw = skills.get("markdown_writer", {})
    _set(env, "SKILL_MARKDOWN_WRITER_NAME", mw.get("name", "WriteMarkdown"))
    _set(env, "SKILL_MARKDOWN_WRITER_DESCRIPTION", mw.get("description", ""))
    _set(env, "SKILL_MARKDOWN_WRITER_FOLDERS", mw.get("folders", "diary,notes,guides"))
    _set(env, "SKILL_MARKDOWN_WRITER_DEFAULT_FOLDER", mw.get("default_folder", "diary"))
    _set(env, "SKILL_MARKDOWN_WRITER_MAX_SIZE_KB", mw.get("max_size_kb", 512))
    _set(env, "SKILL_MARKDOWN_WRITER_MAX_FILES", mw.get("max_files", 50))

    # Knowledge
    kn = config.get("knowledge", {})
    _set(env, "KNOWLEDGE_MAX_PROMPT_ENTRIES", kn.get("max_prompt_entries", 20))
    _set(env, "KNOWLEDGE_MAX_ENTRIES", kn.get("max_entries", 200))
    _set(env, "DAILY_SUMMARY_DAYS", kn.get("daily_summary_days", 7))
    _set(env, "SKILL_KNOWLEDGE_BATCH_SIZE", kn.get("batch_size", 5))
    _set(env, "SKILL_KNOWLEDGE_MAX_INPUT_TOKENS", kn.get("max_input_tokens", 12000))
    _set(env, "SKILL_KNOWLEDGE_MAX_OUTPUT_TOKENS", kn.get("max_output_tokens", 1500))
    _set(env, "SKILL_KNOWLEDGE_SEARCH_MAX_CANDIDATES", kn.get("search_max_candidates", 50))
    _set(env, "SKILL_KNOWLEDGE_SEARCH_MAX_RETURN", kn.get("search_max_return", 8))

    # Relationships
    rel = config.get("relationships", {})
    _set(env, "RELATIONSHIP_SUMMARY_ENABLED", rel.get("summary_enabled", True))
    _set(env, "RELATIONSHIP_SUMMARY_INTERVAL_MINUTES", rel.get("summary_interval_minutes", 120))

    # Social
    sr = config.get("social_reactions", {})
    _set(env, "SOCIAL_REACTIONS_ENABLED", sr.get("enabled", True))

    # Thoughts — AgentLoop pacing.
    # AgentLoop liest die Werte direkt via config.get() (kein env-Bridge
    # mehr noetig); Mapping bleibt nur fuer Backward-Compat falls Code
    # die env-Variable noch erwartet.
    pro = config.get("thoughts", config.get("proactive", {}))
    _set(env, "THOUGHT_MIN_TURN_GAP_SECONDS", pro.get("min_turn_gap_seconds", 30))
    _set(env, "THOUGHT_MIN_PER_CHAR_COOLDOWN_MINUTES", pro.get("min_per_char_cooldown_minutes", 5))

    # Random Events
    re_cfg = config.get("random_events", {})
    _set(env, "EVENT_GENERATION_ENABLED", re_cfg.get("enabled", True))
    _set(env, "EVENT_BASE_PROBABILITY", (re_cfg.get("base_probability", 5)) / 100)
    _set(env, "EVENT_RESOLUTION_PROACTIVE", re_cfg.get("resolution_proactive", True))
    _set(env, "EVENT_RESOLUTION_COOLDOWN_MINUTES", re_cfg.get("resolution_cooldown_minutes", 15))

    # Story Engine
    se = config.get("story_engine", {})
    _set(env, "STORY_ENGINE_ENABLED", se.get("enabled", False))
    _set(env, "STORY_ENGINE_MAX_ACTIVE_ARCS", se.get("max_active_arcs", 2))
    _set(env, "STORY_ENGINE_COOLDOWN_HOURS", se.get("cooldown_hours", 6))
    _set(env, "STORY_ENGINE_MAX_BEATS", se.get("max_beats", 5))
    _set(env, "STORY_ENGINE_BEAT_IMAGES", se.get("beat_images", True))
    _set(env, "STORY_ENGINE_IMAGEGEN_DEFAULT", se.get("imagegen_default", ""))
    _set(env, "STORY_ENGINE_BEAT_FACESWAP", se.get("beat_faceswap", False))

    # Telegram
    tg = config.get("telegram", {})
    _set(env, "TELEGRAM_BOT_TOKEN", tg.get("bot_token", ""))
    _set(env, "TELEGRAM_API_URL", tg.get("api_url", "https://api.telegram.org/bot"))

    # UI
    ui = config.get("ui", {})
    _set(env, "DEFAULT_THEME", ui.get("default_theme", "default"))
    _set(env, "AVAILABLE_THEMES", ui.get("available_themes", "default,minimal,dark"))

    # Write all to os.environ
    for key, value in env.items():
        os.environ[key] = str(value)


def _set(env: dict, key: str, value: Any) -> None:
    """Set an env value, converting Python types to env-compatible strings."""
    if value is None or value == "":
        return
    if isinstance(value, bool):
        env[key] = "true" if value else "false"
    elif isinstance(value, (dict, list)):
        env[key] = json.dumps(value, ensure_ascii=False)
    else:
        env[key] = str(value)
