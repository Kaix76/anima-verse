"""LLM Client — Ersetzt LangChain ChatOpenAI mit dem OpenAI Python SDK.

Bietet:
- LLMClient: Haelt Config (model, api_key, etc.) und bietet invoke() + astream()
- AnthropicLLMClient: Client fuer Anthropic Claude-Modelle (native SDK)
- LLMResponse: Antwort-Wrapper mit .content und .usage
- LLMChunk: Streaming-Chunk mit .content
- to_openai_messages(): Konvertiert Dicts/Legacy-Objekte in OpenAI-Format
"""
import openai
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple


@dataclass
class LLMResponse:
    """Wrapper fuer LLM-Antworten (Ersatz fuer LangChain AIMessage)."""
    content: str
    usage: Optional[Dict[str, int]] = None  # {"prompt_tokens": N, "completion_tokens": N}


@dataclass
class LLMChunk:
    """Einzelner Streaming-Chunk."""
    content: str


def _is_qwen3_model(model_name: str) -> bool:
    """Check if a model is Qwen3-based (has native thinking that should be disabled by default)."""
    lower = model_name.lower()
    return "qwen3" in lower


def _is_gemma_model(model_name: str) -> bool:
    """Check if a model is Gemma-based (has reasoning that should be disabled for tool tasks)."""
    lower = model_name.lower()
    return "gemma" in lower


class LLMClient:
    """Ersetzt ChatOpenAI — haelt Config und bietet invoke() + astream().

    Attribute bleiben kompatibel mit bestehenden getattr()-Zugriffen:
    - model_name, model (Model-Name)
    - openai_api_base, base_url (API URL)
    - max_tokens, temperature, request_timeout
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        api_base: str,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        request_timeout: int = 120,
        chat_template: Optional[str] = None):
        self.model = model
        self.model_name = model
        self.openai_api_key = api_key
        self.openai_api_base = api_base
        self.base_url = api_base
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.request_timeout = request_timeout
        # Optional Jinja chat_template — passed through extra_body when the
        # provider's tokenizer has no default template (e.g. some Infermatic
        # finetunes that error with "no chat template" since transformers v4.44).
        self.chat_template = chat_template or None

        client_kwargs = {
            "api_key": api_key,
            "base_url": api_base,
            "timeout": float(request_timeout),
        }
        self._sync = openai.OpenAI(**client_kwargs)
        self._async = openai.AsyncOpenAI(**client_kwargs)

    def _build_kwargs(self, thinking: Optional[bool] = None) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "temperature": self.temperature,
        }
        if self.max_tokens:
            kwargs["max_tokens"] = self.max_tokens
        extra_body: Dict[str, Any] = {}
        # Disable thinking/reasoning for models with native thinking
        _disable_thinking = thinking is False or (thinking is None and (
            _is_qwen3_model(self.model) or _is_gemma_model(self.model)
        ))
        if _disable_thinking:
            if _is_gemma_model(self.model):
                extra_body["thinking"] = {"type": "disabled"}
            else:
                extra_body["chat_template_kwargs"] = {"enable_thinking": False}
        if self.chat_template:
            extra_body["chat_template"] = self.chat_template
        if extra_body:
            kwargs["extra_body"] = extra_body
        return kwargs

    def invoke(self, messages: List) -> LLMResponse:
        """Synchroner LLM-Call (fuer provider_queue.py Worker-Threads)."""
        openai_msgs = to_openai_messages(messages)
        resp = self._sync.chat.completions.create(
            messages=openai_msgs, **self._build_kwargs()
        )
        usage = None
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
            }
        return LLMResponse(
            content=resp.choices[0].message.content or "",
            usage=usage)

    async def astream(self, messages: List) -> AsyncGenerator[LLMChunk, None]:
        """Async Streaming Generator (fuer streaming.py Agent-Loop).

        Thinking/Reasoning wird per Default disabled (Tool-LLM im Dual-Modus).
        Das Chat-LLM ist ein separates grosses Modell das kein Gemma/Qwen3 ist.
        """
        openai_msgs = to_openai_messages(messages)
        stream = await self._async.chat.completions.create(
            messages=openai_msgs, stream=True, **self._build_kwargs()
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield LLMChunk(content=chunk.choices[0].delta.content)

    def __repr__(self) -> str:
        return f"LLMClient(model={self.model!r}, base_url={self.base_url!r})"


def to_openai_messages(messages) -> List[Dict[str, Any]]:
    """Konvertiert Message-Dicts oder Legacy-Objekte in OpenAI-Format.

    Akzeptiert:
    - Einzelner String: wird als einzelne user-Message behandelt
    - Liste von Dicts: {"role": "user", "content": "..."}
    - Liste von Legacy-Objekten mit .type/.content Attributen
    - Liste von Strings: jeder String wird als user-Message behandelt
    """
    # String als Ganzes behandeln (nicht zeichenweise iterieren!)
    if isinstance(messages, str):
        return [{"role": "user", "content": messages}]

    result = []
    for m in messages:
        if isinstance(m, dict):
            result.append(m)
        elif isinstance(m, str):
            result.append({"role": "user", "content": m})
        elif hasattr(m, "content"):
            # Legacy-Objekt (z.B. noch existierende LangChain-Messages)
            role_map = {"human": "user", "ai": "assistant", "system": "system"}
            msg_type = getattr(m, "type", "human")
            role = role_map.get(msg_type, "user")
            result.append({"role": role, "content": m.content})
        else:
            result.append({"role": "user", "content": str(m)})
    return result


# ---------------------------------------------------------------------------
# Anthropic Claude Client
# ---------------------------------------------------------------------------

def _convert_openai_content_to_anthropic(content: Any) -> Any:
    """Konvertiert OpenAI content-Blocks in Anthropic-Format.

    OpenAI image_url -> Anthropic image (base64 oder URL).
    Strings bleiben Strings.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    result = []
    for block in content:
        if not isinstance(block, dict):
            result.append({"type": "text", "text": str(block)})
            continue
        btype = block.get("type", "")
        if btype == "text":
            result.append({"type": "text", "text": block.get("text", "")})
        elif btype == "image_url":
            url = block.get("image_url", {}).get("url", "")
            if url.startswith("data:"):
                # data:image/jpeg;base64,<data>
                header, data = url.split(",", 1)
                media_type = header.split(":")[1].split(";")[0]
                result.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": data},
                })
            else:
                result.append({
                    "type": "image",
                    "source": {"type": "url", "url": url},
                })
        else:
            result.append({"type": "text", "text": str(block)})
    return result


def _split_anthropic_messages(
    messages) -> Tuple[str, List[Dict[str, Any]]]:
    """Trennt Messages in (system_prompt, conversation) fuer die Anthropic API.

    - System-Messages werden extrahiert und zusammengefuegt
    - Aufeinanderfolgende Messages gleicher Rolle werden gemerged
    - Erste Message muss role=user sein (Anthropic-Anforderung)
    """
    openai_msgs = to_openai_messages(messages)

    system_parts: List[str] = []
    conversation: List[Dict[str, Any]] = []

    for msg in openai_msgs:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == "system":
            # System-Content immer als Text extrahieren
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        system_parts.append(block["text"])
                    elif isinstance(block, str):
                        system_parts.append(block)
            else:
                system_parts.append(str(content))
        else:
            if role not in ("user", "assistant"):
                role = "user"
            anthropic_content = _convert_openai_content_to_anthropic(content)
            conversation.append({"role": role, "content": anthropic_content})

    # Aufeinanderfolgende gleiche Rollen mergen (Anthropic erfordert Alternierung)
    merged: List[Dict[str, Any]] = []
    for msg in conversation:
        if merged and merged[-1]["role"] == msg["role"]:
            prev = merged[-1]["content"]
            curr = msg["content"]
            if isinstance(prev, str) and isinstance(curr, str):
                merged[-1]["content"] = prev + "\n\n" + curr
            elif isinstance(prev, list) and isinstance(curr, list):
                merged[-1]["content"] = prev + curr
            elif isinstance(prev, str) and isinstance(curr, list):
                merged[-1]["content"] = [{"type": "text", "text": prev}] + curr
            elif isinstance(prev, list) and isinstance(curr, str):
                merged[-1]["content"] = prev + [{"type": "text", "text": curr}]
        else:
            merged.append(msg.copy())

    # Erste Message muss user sein
    if merged and merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "[Start]"})

    if not merged:
        merged = [{"role": "user", "content": ""}]

    system = "\n\n".join(system_parts) if system_parts else ""
    return system, merged


class AnthropicLLMClient:
    """LLM Client fuer Anthropic Claude-Modelle (native SDK).

    Gleiche Schnittstelle wie LLMClient: invoke() + astream().
    Attribute bleiben kompatibel fuer getattr()-Zugriffe.
    """

    def __init__(
        self,
        model: str,
        api_key: str,
        api_base: str = "https://api.anthropic.com/v1",
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        request_timeout: int = 120):
        import anthropic

        self.model = model
        self.model_name = model
        self.openai_api_key = api_key
        self.openai_api_base = api_base
        self.base_url = api_base
        self.temperature = temperature
        self.max_tokens = max_tokens or 4096  # Anthropic erfordert max_tokens
        self.request_timeout = request_timeout

        # SDK erwartet Base-URL ohne /v1
        base = api_base.rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]

        self._sync = anthropic.Anthropic(
            api_key=api_key, base_url=base, timeout=float(request_timeout))
        self._async = anthropic.AsyncAnthropic(
            api_key=api_key, base_url=base, timeout=float(request_timeout))

    def _build_kwargs(self, system: str, msgs: List[Dict]) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if system:
            kwargs["system"] = system
        return kwargs

    def invoke(self, messages: List) -> LLMResponse:
        """Synchroner LLM-Call."""
        system_msg, msgs = _split_anthropic_messages(messages)
        resp = self._sync.messages.create(**self._build_kwargs(system_msg, msgs))

        content = ""
        for block in resp.content:
            if hasattr(block, "text"):
                content += block.text

        usage = None
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.input_tokens,
                "completion_tokens": resp.usage.output_tokens,
            }
        return LLMResponse(content=content, usage=usage)

    async def astream(self, messages: List) -> AsyncGenerator[LLMChunk, None]:
        """Async Streaming Generator."""
        system_msg, msgs = _split_anthropic_messages(messages)
        async with self._async.messages.stream(
            **self._build_kwargs(system_msg, msgs)
        ) as stream:
            async for text in stream.text_stream:
                yield LLMChunk(content=text)

    def __repr__(self) -> str:
        return f"AnthropicLLMClient(model={self.model!r}, base_url={self.base_url!r})"
