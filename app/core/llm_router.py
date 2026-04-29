"""Zentrale LLM-Aufruf-Funktion mit Task-basiertem Routing.

Die `llm_call()` Funktion ist der einheitliche Einstiegspunkt fuer alle
Nicht-Stream-LLM-Aufrufe. Der Resolver ermittelt anhand des Task-Typs
aus der `llm_routing`-Config das passende LLM (Provider+Model+Settings)
mit Fallback-Kette bei nicht verfuegbarem Provider.

Zusaetzlich liegen hier:
- `LLMInstance`: Datenklasse, die Provider+Model+Settings buendelt und bei Bedarf
  einen LLMClient erzeugt.
- `create_llm_instance()`: Fabrik fuer Dev-Routen (world_dev, story_dev), die
  ein konkretes Model explizit waehlen.
- `get_llm_instance_by_name()`: Parsing-Helfer fuer Character-Overrides
  ("Provider::Model" oder "Model").

Streaming laeuft separat, nutzt aber denselben Resolver.
"""
import os
from dataclasses import dataclass, field
from typing import Any, List, Optional

from app.core import config
from app.core.llm_client import AnthropicLLMClient, LLMClient
from app.core.llm_queue import get_llm_queue
from app.core.log import get_logger
from app.core.provider import Provider
from app.core.provider_manager import get_provider_manager

logger = get_logger("llm_router")


# ---------------------------------------------------------------------------
# LLMInstance + Fabriken
# ---------------------------------------------------------------------------

@dataclass
class LLMInstance:
    """Represents a resolved LLM instance ready for use.

    Connection details (type, api_base, api_key) are delegated to the Provider.
    """
    provider_name: str
    model: str
    tasks: list = field(default_factory=list)
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    chat_template: Optional[str] = None
    _provider: Optional[Provider] = field(default=None, repr=False)

    @property
    def type(self) -> str:
        return self._provider.type if self._provider else ""

    @property
    def api_base(self) -> str:
        return self._provider.api_base if self._provider else ""

    @property
    def api_key(self) -> str:
        return self._provider.api_key if self._provider else ""

    @property
    def available(self) -> bool:
        return self._provider.available if self._provider else False

    @property
    def name(self) -> str:
        tasks_str = ",".join(self.tasks) if self.tasks else "unknown"
        return f"{self.provider_name}/{self.model} ({tasks_str})"

    def create_llm(self, **overrides):
        """Creates an LLMClient (or AnthropicLLMClient) with optional per-agent overrides."""
        model = overrides.get("model") or self.model
        api_key = overrides.get("api_key") or self.api_key
        api_base = overrides.get("api_base") or self.api_base
        temperature = float(overrides.get("temperature") or self.temperature)
        max_tok = overrides.get("max_tokens") or self.max_tokens
        max_tokens = int(max_tok) if max_tok else None
        provider_timeout = self._provider.timeout if self._provider else None
        timeout = provider_timeout or int(os.environ.get("LLM_REQUEST_TIMEOUT", "120"))

        if self._provider and self._provider.type == "anthropic":
            return AnthropicLLMClient(
                model=model,
                api_key=api_key,
                api_base=api_base,
                temperature=temperature,
                max_tokens=max_tokens,
                request_timeout=timeout)

        return LLMClient(
            model=model,
            api_key=api_key,
            api_base=api_base,
            temperature=temperature,
            max_tokens=max_tokens,
            request_timeout=timeout,
            chat_template=overrides.get("chat_template") or self.chat_template)


def get_llm_instance_by_name(model_name: str) -> Optional[LLMInstance]:
    """Erzeugt eine LLMInstance per Model-Name (Provider wird aufgeloest).

    Akzeptiert "Provider::Model" oder "Model".
    """
    pm = get_provider_manager()

    provider_name = ""
    if model_name and "::" in model_name:
        provider_name, model_name = model_name.split("::", 1)

    provider = None
    if provider_name:
        provider = pm.get_provider(provider_name)
    if not provider:
        provider = pm.find_provider_for_model(model_name)
    if not provider:
        logger.warning("get_llm_instance_by_name: No provider found for '%s'", model_name)
        return None
    return LLMInstance(
        provider_name=provider.name,
        model=model_name,
        _provider=provider)


def create_llm_instance(
    task: str,
    model: str,
    provider_name: str = "",
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None) -> Optional[LLMInstance]:
    """Erzeugt eine LLMInstance fuer ein konkretes Model (fuer Dev-Routen)."""
    pm = get_provider_manager()

    if model and "::" in model and not provider_name:
        provider_name, model = model.split("::", 1)

    provider = None
    if provider_name:
        provider = pm.get_provider(provider_name)
    if not provider:
        provider = pm.find_provider_for_model(model)

    if not provider:
        logger.warning("No provider found for model '%s' (task=%s)", model, task)
        return None

    return LLMInstance(
        provider_name=provider.name,
        model=model,
        tasks=[task],
        temperature=temperature if temperature is not None else 0.7,
        max_tokens=max_tokens,
        _provider=provider)


# ---------------------------------------------------------------------------
# Routing + llm_call
# ---------------------------------------------------------------------------

def _load_routing() -> List[dict]:
    """Liest llm_routing aus der Config."""
    routing = config.get("llm_routing", [])
    return routing if isinstance(routing, list) else []


def _resolve_character_override(task: str, agent_name: str) -> Optional[LLMInstance]:
    """Liest `llm_routing_overrides[task]` aus character_config.

    Unterstuetzte Wert-Formate:
    - "Provider::Model"   — konkreter Provider + Model
    - "Model"             — Model allein; Provider wird automatisch ermittelt

    Liefert eine LLMInstance wenn Override existiert UND der Provider
    verfuegbar ist, sonst None (fallt auf globales Routing zurueck).
    """
    try:
        from app.models.character import get_character_config
        cfg = get_character_config(agent_name) or {}
    except Exception:
        return None

    overrides = cfg.get("llm_routing_overrides") or {}
    if not isinstance(overrides, dict):
        return None
    raw = overrides.get(task)
    if not raw or not isinstance(raw, str):
        return None

    instance = get_llm_instance_by_name(raw.strip())
    if not instance:
        logger.warning("Character-Override '%s' fuer task=%s (agent=%s): Provider/Model nicht aufloesbar",
                       raw, task, agent_name)
        return None
    if not instance.available:
        logger.info("Character-Override '%s' fuer task=%s (agent=%s): Provider unavailable — Fallback auf globales Routing",
                    raw, task, agent_name)
        return None
    instance.tasks = [task]
    logger.info("Character-Override greift: task=%s agent=%s -> %s/%s",
                task, agent_name, instance.provider_name, instance.model)
    return instance


def resolve_llm(task: str, agent_name: str = "") -> Optional[LLMInstance]:
    """Ermittelt das LLM fuer einen Task anhand der llm_routing-Config.

    Reihenfolge:
    1. Character-Override aus `character_config.llm_routing_overrides[task]`
       (Format: "Provider::Model") — wenn Provider verfuegbar
    2. Globale llm_routing-Kette (sortiert nach order, erster verfuegbarer Provider)
    3. None wenn keiner greift

    Args:
        task: Task-ID aus TASK_TYPES
        user_id, agent_name: Optional — wenn beide gesetzt, wird der
            Character-Override aus character_config.json beruecksichtigt.

    Task-Disable (llm_task_state) greift immer als erstes Check.
    """
    # Task deaktiviert? → kein LLM (Aufrufer fallen in bestehende Fallback-Pfade)
    from app.core.llm_task_state import is_enabled
    if not is_enabled(task):
        logger.debug("resolve_llm(%s): Task deaktiviert", task)
        return None

    # 1. Character-Override
    if agent_name:
        override_inst = _resolve_character_override(task, agent_name)
        if override_inst is not None:
            return override_inst

    routing = _load_routing()
    if not routing:
        return None

    # Kandidaten sammeln: (order, entry)
    candidates: List = []
    for entry in routing:
        if not isinstance(entry, dict):
            continue
        tasks = entry.get("tasks") or []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            if t.get("task") == task:
                order = int(t.get("order", 999))
                candidates.append((order, entry))
                break

    if not candidates:
        # Fallback fuer Sub-Tasks: wenn der spezifische Task nicht geroutet ist,
        # versuche den generischen Parent-Task. Pattern: "<parent>_<sub>" faellt
        # auf "<parent>" zurueck. So kann der User Sub-Tasks per Admin-UI
        # nachtraeglich differenziert zuweisen, ohne dass die Funktion ausfaellt.
        for _parent in ("intent", "thought"):
            if task.startswith(_parent + "_") and task != _parent:
                logger.debug("resolve_llm(%s): kein Routing, Fallback auf '%s'",
                             task, _parent)
                return resolve_llm(_parent, agent_name=agent_name)
        return None

    candidates.sort(key=lambda x: x[0])
    pm = get_provider_manager()

    for order, entry in candidates:
        provider_name = (entry.get("provider") or "").strip()
        model = (entry.get("model") or "").strip()
        if not provider_name or not model:
            continue
        provider = pm.get_provider(provider_name)
        if not provider:
            logger.warning("resolve_llm(%s): provider '%s' not found", task, provider_name)
            continue
        if not provider.available:
            logger.info("resolve_llm(%s): provider '%s' unavailable, trying next", task, provider_name)
            continue

        temperature = float(entry.get("temperature") or 0.7)
        max_tokens = entry.get("max_tokens")
        if max_tokens in ("", None, 0):
            max_tokens = None
        else:
            max_tokens = int(max_tokens)

        return LLMInstance(
            provider_name=provider_name,
            model=model,
            tasks=[task],
            temperature=temperature,
            max_tokens=max_tokens,
            chat_template=(entry.get("chat_template") or "") or None,
            _provider=provider)

    logger.warning("resolve_llm(%s): kein verfuegbares LLM in der Kette", task)
    return None


def llm_call(
    task: str,
    system_prompt: str,
    user_prompt: str,
    *,
    agent_name: str = "", priority: Optional[int] = None,
    label: str = "") -> Any:
    """Zentraler LLM-Einstiegspunkt fuer Non-Stream-Calls.

    Resolved Provider+Model per Task, submitted ueber Queue, Logging
    laeuft automatisch in der Queue-Worker.

    Returns:
        Das Response-Objekt der Queue (kompatibel mit bestehenden
        Aufrufstellen: `.content` liefert den Text).

    Raises:
        RuntimeError: wenn kein LLM fuer den Task verfuegbar ist.
    """
    instance = resolve_llm(task, agent_name=agent_name)
    if instance is None:
        raise RuntimeError(f"llm_call: Kein verfuegbares LLM fuer Task '{task}'")

    if priority is None:
        from app.core.llm_tasks import get_default_priority
        priority = get_default_priority(task)

    llm = instance.create_llm()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    logger.info("llm_call task=%s provider=%s model=%s agent=%s",
                task, instance.provider_name, instance.model, agent_name or "-")

    return get_llm_queue().submit(
        task_type=task,
        priority=priority,
        llm=llm,
        messages_or_prompt=messages,
        agent_name=agent_name,
        label=label)
