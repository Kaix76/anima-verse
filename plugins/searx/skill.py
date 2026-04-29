"""SearX Search Plugin - Privacy-respecting Metasuchmaschine"""
from typing import Any, Dict, List
from urllib.parse import urlencode

from app.plugins.base import PluginSkill
from app.plugins.context import PluginContext
from app.skills.base import ToolSpec


class SearXPlugin(PluginSkill):
    """
    SearX Search Skill fuer Web-Suchen ueber eine selbst-gehostete SearX-Instanz.
    Nutzt die SearX JSON-API direkt fuer strukturierte Ergebnisse mit Titeln,
    URLs, Snippets und Timestamps.

    Konfiguration:
        .env (Defaults):
            SKILL_SEARX_URL, SKILL_SEARX_ENGINES, SKILL_SEARX_CATEGORIES,
            SKILL_SEARX_NUM_RESULTS
        Per-Agent (storage/users/{user}/agents/{agent}/skills/searx.json):
            engines, categories, num_results
    """

    SKILL_ID = "searx"

    def __init__(self, config: Dict[str, Any], ctx: PluginContext):
        super().__init__(config, ctx)

        self.name = "WebSearch"
        self.description = "Searches the web for current information via SearX meta search engine"

        self.searx_host = ctx.get_env('SKILL_SEARX_URL', 'http://localhost:8888').rstrip('/')

        self._defaults = {
            "engines": (ctx.get_env('SKILL_SEARX_ENGINES') or '').strip(),
            "categories": (ctx.get_env('SKILL_SEARX_CATEGORIES') or '').strip(),
            "num_results": ctx.get_env_int('SKILL_SEARX_NUM_RESULTS', 10),
        }

        # Teste Verbindung
        try:
            resp = ctx.http.get(
                f"{self.searx_host}/search",
                params={"q": "test", "format": "json"},
                timeout=5)
            if resp.ok:
                ctx.logger.info("SearX erreichbar: %s", self.searx_host)
            else:
                ctx.logger.warning("SearX nicht erreichbar: HTTP %d", resp.status_code)
        except Exception as e:
            ctx.logger.error("SearX Verbindungsfehler: %s", e)

    def _search(self, query: str, engines: str, categories: str, num_results: int) -> List[Dict]:
        """Fuehrt eine Suche ueber die SearX JSON-API aus."""
        params = {
            "q": query,
            "format": "json",
        }
        if engines:
            params["engines"] = engines
        if categories:
            params["categories"] = categories

        url = f"{self.searx_host}/search"
        self.ctx.logger.debug("API-Request: %s?%s", url, urlencode(params))

        resp = self.ctx.http.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])[:num_results]
        return results

    def _format_results(self, results: List[Dict], query: str) -> str:
        """Formatiert Suchergebnisse als gut lesbaren Text fuer den LLM."""
        if not results:
            return f"Keine Ergebnisse fuer '{query}' gefunden."

        lines = [f"**Suchergebnisse fuer: \"{query}\"**\n"]

        for i, result in enumerate(results, 1):
            title = result.get("title", "Ohne Titel")
            url = result.get("url", "")
            snippet = result.get("content", "").strip()
            published = result.get("publishedDate", "")
            engine_list = result.get("engines", [])
            engine = ", ".join(engine_list) if isinstance(engine_list, list) else str(engine_list)
            img_src = result.get("img_src", "") or result.get("thumbnail", "")

            if url:
                lines.append(f"**{i}. [{title}]({url})**")
            else:
                lines.append(f"**{i}. {title}**")

            meta_parts = []
            if published:
                meta_parts.append(f"Datum: {published}")
            if engine:
                meta_parts.append(f"Quelle: {engine}")
            if meta_parts:
                lines.append(f"   {' | '.join(meta_parts)}")

            if img_src:
                lines.append(
                    f'   <img src="{img_src}" alt="{title}" '
                    f'style="max-width:300px;max-height:300px;border-radius:6px;margin:4px 0;" />'
                )

            if snippet:
                lines.append(f"   {snippet}")

            lines.append("")

        lines.append(f"*{len(results)} Ergebnisse gefunden*")
        return "\n".join(lines)

    def execute(self, raw_input: str) -> str:
        if not self.enabled:
            return "SearX Skill ist nicht verfuegbar."

        ctx = self._parse_base_input(raw_input)
        query = ctx.get("input", raw_input).strip()
        character_name = ctx.get("agent_name", "").strip()
        user_id = ctx.get("user_id", "").strip()

        if not query:
            return "Fehler: Leere Suchanfrage."

        cfg = self._get_effective_config(character_name)
        engines = cfg.get("engines", "")
        categories = cfg.get("categories", "")
        num_results = int(cfg.get("num_results", 10))

        try:
            self.ctx.logger.info("Skill aufgerufen: query=%s", query)
            self.ctx.logger.debug("SearX-URL: %s", self.searx_host)
            if engines:
                self.ctx.logger.debug("Engines: %s", engines)
            if categories:
                self.ctx.logger.debug("Categories: %s", categories)
            self.ctx.logger.debug("Max Ergebnisse: %d", num_results)

            results = self._search(query, engines, categories, num_results)
            formatted = self._format_results(results, query)

            self.ctx.logger.info("%d Ergebnisse gefunden", len(results))
            return formatted

        except Exception as e:
            self.ctx.logger.error("Fehler bei der Suche: %s", e)
            return f"Fehler bei der Suche: {e}"

    def memorize_result(self, result: str, character_name: str) -> bool:
        """Speichert Web-Suchergebnisse als Memory (fuer Scheduler-Aufrufe)."""
        if not result or result.startswith("Fehler") or result.startswith("Keine Ergebnisse"):
            return False
        try:
            from app.models.memory import add_memory
            # Ergebnis kuerzen — nur die ersten 1500 Zeichen sind relevant
            content = result[:1500]
            add_memory(
                character_name=character_name,
                content=content,
                memory_type="semantic",
                importance=3,
                tags=["scheduler_tool", "web_search"],
                context="scheduler:WebSearch")
            return True
        except Exception as e:
            self.ctx.logger.warning("memorize_result fehlgeschlagen: %s", e)
            return False

    def get_usage_instructions(self, format_name: str = "", **kwargs) -> str:
        if 'usage_instructions' in self.config:
            return self.config['usage_instructions']
        from app.core.tool_formats import format_example
        fmt = format_name or "tag"
        return format_example(fmt, self.name, "current weather in Berlin")

    def as_tool(self, **kwargs) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=f"{self.description}. Input should be a search query.",
            func=self.execute)
