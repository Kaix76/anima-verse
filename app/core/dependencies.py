"""Dependency injection for LLM and Skills"""
import os
from typing import Dict, Any
from app.core.config import reload as reload_config

from app.core.log import get_logger
logger = get_logger("dependencies")

from app.skills.skill_manager import SkillManager
from app.models.unified_chat import get_channel_manager
from app.models.channel import WebChannel, ChannelType
from app.models.telegram_channel import TelegramChannel


# Global SkillManager Instance (kann ohne Server-Neustart neu geladen werden)
_skill_manager = None
_channels_initialized = False


def determine_mode(agent_tools, tool_llm, agent_config=None) -> str:
    """Determines streaming mode based on available tools and config.

    Args:
        agent_tools: List of ToolSpec from get_agent_tools() (may be filtered)
        tool_llm: Tool-LLM instance from get_tool_llm(), or None
        agent_config: Character config dict (optional).

    Returns:
        "no_tools" — no skills active, pure chat
        "single"   — Chat-LLM handles RP + tools in one call
        "rp_first" — Chat-LLM writes RP first, Tool-LLM decides on tools after
    """
    if not agent_tools:
        return "no_tools"

    # Expliziter Schalter am Character
    if agent_config:
        forced = agent_config.get("chat_mode", "").lower()
        if forced in ("single", "rp_first"):
            return forced

    return "single"


def get_skill_manager() -> SkillManager:
    """Gibt den globalen SkillManager zurück (lädt beim ersten Aufruf)"""
    global _skill_manager
    if _skill_manager is None:
        _skill_manager = SkillManager()
        _skill_manager.load_skills()
    return _skill_manager


def initialize_channels() -> None:
    """Initialisiere Multi-Channel Support beim Server-Start"""
    global _channels_initialized
    
    if _channels_initialized:
        return
    
    manager = get_channel_manager()
    
    # Registriere Standard Web-Kanal
    manager.register_channel(WebChannel())
    logger.info("Web-Channel registered")

    # Registriere Telegram-Kanal (Bot-Token wird per Agent konfiguriert)
    telegram = TelegramChannel()
    manager.register_channel(telegram)
    logger.info("Telegram-Channel registered (per-agent bot tokens)")

    _channels_initialized = True


def reload_skill_manager() -> Dict[str, Any]:
    """Reloads .env, then LLM Service, Skills, Face Service and Channels.

    Prints a consolidated availability summary to the console.
    """
    global _skill_manager

    logger.info("=" * 80)
    logger.info("RELOADING ALL SERVICES")
    logger.info("=" * 80)

    # Reload config.json (populates os.environ via backward-compat bridge)
    reload_config()
    logger.info("config.json reloaded")

    # ── Provider Manager ──
    provider_result = {}
    try:
        from .provider_manager import reload_provider_manager
        provider_result = reload_provider_manager()
    except Exception as e:
        logger.error("Provider Manager reload failed: %s", e)
        provider_result = {"error": str(e)}

    # ── LLM Routing ──  (config wurde schon via reload_config() neu geladen)
    from app.core import config as _cfg
    llm_result = {"routing_entries": len(_cfg.get("llm_routing", []) or [])}

    # ── Skills ──
    if _skill_manager is None:
        _skill_manager = SkillManager()
    result = _skill_manager.reload_skills()

    # ── Face Service ──
    from app.skills.face_client import invalidate_cache, is_available

    invalidate_cache()
    face_available = is_available()

    # ── TTS Service ──
    tts_result = {}
    try:
        from .tts_service import reload_tts_service
        tts_result = reload_tts_service()
    except Exception as e:
        logger.error("TTS Service reload failed: %s", e)
        tts_result = {"error": str(e)}

    # ── Telegram Channel ──
    manager = get_channel_manager()
    has_telegram = manager.get_channel(ChannelType.TELEGRAM) is not None
    if not has_telegram:
        telegram = TelegramChannel()
        manager.register_channel(telegram)
        result["telegram"] = "registered"

    # ── Telegram Polling (restart all bots with current config) ──
    telegram_result = {}
    try:
        import asyncio
        from .telegram_polling import get_polling_manager
        pm = get_polling_manager()

        async def _restart_polling():
            await pm.stop()
            await pm.start()
            return pm.get_status()

        loop = asyncio.get_event_loop()
        if loop.is_running():
            # Schedule as task so it runs in the existing event loop
            _future = asyncio.ensure_future(_restart_polling())
            telegram_result = {"status": "restarting"}
        else:
            telegram_result = loop.run_until_complete(_restart_polling())
    except Exception as e:
        logger.error("Telegram Polling reload failed: %s", e)
        telegram_result = {"error": str(e)}

    # ── Availability Summary ──
    logger.info("-" * 80)
    logger.info("AVAILABILITY SUMMARY")
    logger.info("-" * 80)

    # Providers
    try:
        from .provider_manager import get_provider_manager
        pm = get_provider_manager()
        for prov in pm.providers.values():
            status = "OK" if prov.available else "FAIL"
            vram = f", vram={prov.vram_mb}MB" if prov.vram_mb else ""
            logger.info("  Prov  %s  %s (%s, concurrent=%d%s)",
                        status, prov.name, prov.type, prov.max_concurrent, vram)
        if not pm.providers:
            logger.info("  Prov  --    No providers configured")
    except Exception:
        logger.info("  Prov  FAIL  Could not read provider status")

    # LLM routing
    try:
        from app.core import config as _cfg
        routing = _cfg.get("llm_routing", []) or []
        for entry in routing:
            tasks = entry.get("tasks") or []
            task_str = ", ".join(f"{t.get('task')}:{t.get('order')}" for t in tasks)
            logger.info("  LLM   OK    %s / %s -> %s",
                        entry.get("provider", "?"), entry.get("model", "?"), task_str)
        if not routing:
            logger.info("  LLM   --    No routing entries configured")
    except Exception:
        logger.info("  LLM   FAIL  Could not read llm_routing")

    # Skills
    for skill in _skill_manager.skills:
        logger.info("  Skill OK    %s", skill.name)
    if not _skill_manager.skills:
        logger.info("  Skill --    No skills loaded")

    # Face Service
    face_url = os.environ.get("FACE_SERVICE_URL", "http://localhost:8005")
    face_status = "OK" if face_available else "FAIL"
    logger.info("  Face  %s  Face Service (%s)", face_status, face_url)

    # TTS Service
    try:
        from .tts_service import get_tts_service
        tts = get_tts_service()
        tts_info = tts.status_info()
        if tts_info["enabled"]:
            tts_status = "OK" if tts_info["available"] else "FAIL"
            logger.info("  TTS   %s  %s (%s, voice=%s)",
                        tts_status, tts_info['backend'].upper(), tts_info['url'], tts_info['voice'])
        else:
            logger.info("  TTS   --    Disabled")
    except Exception:
        logger.info("  TTS   FAIL  Could not read TTS service status")

    # Telegram Polling
    try:
        from .telegram_polling import get_polling_manager
        _pm = get_polling_manager()
        _ps = _pm.get_status()
        if _ps["active_bots"] > 0:
            for _key, _bot in _ps["bots"].items():
                logger.info("  Tele  OK    @%s -> %s", _bot.get("bot_username", "?"), _key)
        else:
            logger.info("  Tele  --    No Telegram bots configured")
    except Exception:
        logger.info("  Tele  FAIL  Could not read Telegram polling status")

    logger.info("-" * 80)

    result["providers"] = provider_result
    result["llm"] = llm_result
    result["face_service"] = face_available
    result["tts"] = tts_result
    result["telegram"] = telegram_result
    return result


