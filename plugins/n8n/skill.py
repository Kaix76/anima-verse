"""n8n Plugin — ruft n8n-Webhooks (POST) auf.

Konfiguration pro Character via
storage/users/{user}/agents/{agent}/skills/n8n.json:

    enabled: bool
    timeout: Request-Timeout in Sekunden (default 30)
    workflows: Liste verfuegbarer Workflows fuer diesen Character

Workflow-Eintrag:
    {
      "id": "calendar_check",
      "description": "Prueft Kalender. Param: date (YYYY-MM-DD)",
      "url": "http://host:5678/webhook/calendar-check",
      "auth_header": "",    (optional, z.B. "X-N8N-API-KEY" oder "Authorization")
      "auth_value": ""      (optional, z.B. "my-secret" oder "Bearer xyz")
    }

Das LLM liefert den Call als JSON:
    <tool name="N8N">{"workflow": "calendar_check", "params": {"date": "2026-04-13"}}</tool>

Der Skill POSTet dann auf workflow.url mit params als JSON-Body und gibt die
JSON-Antwort zurueck.
"""
import json
from typing import Any, Dict, List

from app.plugins.base import PluginSkill
from app.plugins.context import PluginContext
from app.skills.base import ToolSpec


class N8nPlugin(PluginSkill):
    SKILL_ID = "n8n"

    def __init__(self, config: Dict[str, Any], ctx: PluginContext):
        super().__init__(config, ctx)

        self.name = "N8N"
        self.description = "Calls an n8n webhook and returns its JSON response"

        self._defaults = {
            "timeout": 30,
            "workflows": [],
        }

    def _find_workflow(self, workflows: List[Dict[str, Any]], wf_id: str) -> Dict[str, Any]:
        for wf in workflows:
            if isinstance(wf, dict) and str(wf.get("id", "")).strip() == wf_id:
                return wf
        return {}

    def _build_description(self, workflows: List[Dict[str, Any]]) -> str:
        if not workflows:
            return (
                f"{self.description}. No workflows configured for this character — "
                f"add workflows in the skill config."
            )
        lines = [
            f"{self.description}.",
            'Input MUST be JSON: {"workflow": "<id>", "params": {...}}',
            "Available workflows:",
        ]
        for wf in workflows:
            if not isinstance(wf, dict):
                continue
            wf_id = str(wf.get("id", "")).strip()
            wf_desc = str(wf.get("description", "")).strip()
            if wf_id:
                lines.append(f"- {wf_id}: {wf_desc}" if wf_desc else f"- {wf_id}")
        return "\n".join(lines)

    def execute(self, raw_input: str) -> str:
        if not self.enabled:
            return "N8N Skill ist nicht verfuegbar."

        data = self._parse_base_input(raw_input)
        user_id = data.get("user_id", "").strip()
        character_name = data.get("agent_name", "").strip()
        payload_str = data.get("input", raw_input)

        cfg = self._get_effective_config(character_name)
        timeout = int(cfg.get("timeout", 30))
        workflows = cfg.get("workflows", []) or []

        if not workflows:
            return "Fehler: Keine Workflows fuer diesen Character konfiguriert."

        try:
            call = json.loads(payload_str) if isinstance(payload_str, str) else payload_str
            if not isinstance(call, dict):
                raise ValueError("Input muss ein JSON-Objekt sein")
        except Exception as e:
            self.ctx.logger.error("Ungueltiger Tool-Input: %s (%s)", e, str(payload_str)[:200])
            return (
                'Fehler: Input muss JSON sein wie '
                '{"workflow": "<id>", "params": {...}}'
            )

        wf_id = str(call.get("workflow", "")).strip()
        params = call.get("params", {}) or {}
        if not wf_id:
            return 'Fehler: Feld "workflow" fehlt im Input.'
        if not isinstance(params, dict):
            return 'Fehler: Feld "params" muss ein Objekt sein.'

        workflow = self._find_workflow(workflows, wf_id)
        if not workflow:
            available = ", ".join(str(wf.get("id", "")) for wf in workflows if isinstance(wf, dict))
            return f"Fehler: Workflow '{wf_id}' nicht gefunden. Verfuegbar: {available}"

        url = str(workflow.get("url", "")).strip()
        if not url:
            return f"Fehler: Workflow '{wf_id}' hat keine url."

        # Auth pro Workflow (optional). Leerer Header-Name → ohne Auth.
        headers = {"Content-Type": "application/json"}
        auth_header = str(workflow.get("auth_header", "")).strip()
        auth_value = str(workflow.get("auth_value", ""))
        if auth_header and auth_value:
            headers[auth_header] = auth_value

        self.ctx.logger.info("n8n-Aufruf: workflow=%s url=%s", wf_id, url)
        self.ctx.logger.debug("n8n-Params: %s", params)

        try:
            resp = self.ctx.http.post(url, json=params, headers=headers, timeout=timeout)
            resp.raise_for_status()
        except Exception as e:
            self.ctx.logger.error("n8n-Request fehlgeschlagen (%s): %s", url, e)
            return f"Fehler: n8n-Aufruf fehlgeschlagen ({e})"

        try:
            result = resp.json()
            return json.dumps(result, ensure_ascii=False, indent=2)
        except ValueError:
            text = resp.text.strip()
            self.ctx.logger.debug("n8n-Response ist kein JSON, Laenge=%d", len(text))
            return text or "(leere n8n-Antwort)"

    def get_usage_instructions(self, format_name: str = "", character_name: str = "") -> str:
        if "usage_instructions" in self.config:
            return self.config["usage_instructions"]
        from app.core.tool_formats import format_example
        fmt = format_name or "tag"
        example_payload = '{"workflow": "<id>", "params": {"key": "value"}}'
        return format_example(fmt, self.name, example_payload)

    def as_tool(self, character_name: str = "") -> ToolSpec:
        workflows: List[Dict[str, Any]] = []
        if character_name:
            cfg = self._get_effective_config(character_name)
            wf_cfg = cfg.get("workflows", []) or []
            if isinstance(wf_cfg, list):
                workflows = wf_cfg
        return ToolSpec(
            name=self.name,
            description=self._build_description(workflows),
            func=self.execute)
