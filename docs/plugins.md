# Plugin System

Das Plugin-System ermoeglicht es, neue Skills zu entwickeln und zu laden, ohne den Hauptcode zu aendern. Plugins liegen im Verzeichnis `plugins/` im Projektroot und werden beim Start automatisch erkannt.

## Architektur

```
plugins/                          # Plugin-Verzeichnis (Projektroot)
  mein_plugin/
    plugin.yaml                   # Metadaten & Konfiguration
    skill.py                      # Skill-Klasse (erbt von PluginSkill)

app/plugins/                      # Plugin-Infrastruktur
  loader.py                       # Erkennung & dynamisches Laden
  context.py                      # PluginContext (Service-API)
  base.py                         # PluginSkill (Basisklasse)
```

**Ladevorgang:**

1. `SkillManager.load_skills()` ruft `_load_plugins()` auf
2. Der Loader scannt `plugins/` nach Unterordnern mit `plugin.yaml`
3. Fuer jedes Plugin wird geprueft ob es per `.env` aktiviert ist
4. Die Skill-Klasse wird dynamisch importiert und mit einem `PluginContext` instanziiert
5. Der Skill wird wie ein normaler Built-in Skill in den SkillManager eingehaengt

Plugins sind nach dem Laden vollstaendig kompatibel mit dem bestehenden System: Per-Agent Konfiguration, Tool-Formate, Skill-Filtering und `reload_skills()` funktionieren wie bei Built-in Skills.

## Neues Plugin erstellen

### 1. Verzeichnis anlegen

```bash
mkdir plugins/mein_plugin
```

### 2. plugin.yaml erstellen

```yaml
name: mein_plugin
version: "1.0.0"
description: Kurze Beschreibung was das Plugin tut
skill_id: mein_plugin
env_prefix: "SKILL_MEINPLUGIN_"
```

### 3. skill.py erstellen

```python
from typing import Any, Dict
from app.plugins.base import PluginSkill
from app.plugins.context import PluginContext
from app.skills.base import ToolSpec


class MeinPlugin(PluginSkill):
    SKILL_ID = "mein_plugin"

    def __init__(self, config: Dict[str, Any], ctx: PluginContext):
        super().__init__(config, ctx)
        self.name = "MeinTool"
        self.description = "Beschreibung fuer das LLM"

        # Konfiguration aus Umgebungsvariablen
        self.api_url = ctx.get_env("SKILL_MEINPLUGIN_URL", "http://localhost:9000")

        # Per-Agent konfigurierbare Defaults
        self._defaults = {
            "option_a": ctx.get_env("SKILL_MEINPLUGIN_OPTION_A", "default"),
            "max_results": ctx.get_env_int("SKILL_MEINPLUGIN_MAX_RESULTS", 5),
        }

    def execute(self, raw_input: str) -> str:
        # Standard-Input-Parsing (extrahiert user_id, agent_name, input)
        data = self._parse_base_input(raw_input)
        query = data.get("input", raw_input).strip()
        user_id = data.get("user_id", "")
        agent_name = data.get("agent_name", "")

        if not query:
            return "Fehler: Leere Eingabe."

        # Per-Agent Config laden (merged .env-Defaults mit Agent-Overrides)
        cfg = self._get_effective_config(user_id, agent_name)

        try:
            # HTTP-Requests ueber ctx.http (requests-Bibliothek)
            resp = self.ctx.http.get(
                f"{self.api_url}/api/search",
                params={"q": query},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("result", "Kein Ergebnis")

        except Exception as e:
            self.ctx.logger.error("Fehler: %s", e)
            return f"Fehler: {e}"

    def as_tool(self, **kwargs) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=f"{self.description}. Input: Freitext-Eingabe.",
            func=self.execute,
        )
```

### 4. In .env aktivieren

```bash
SKILL_MEINPLUGIN_ENABLED=true
SKILL_MEINPLUGIN_URL=http://localhost:9000
```

Das Plugin wird beim naechsten Start oder bei `reload_skills()` automatisch geladen.

## plugin.yaml Referenz

| Feld | Pflicht | Beschreibung |
|---|---|---|
| `name` | ja | Eindeutiger Name des Plugins |
| `skill_id` | ja | Identifier fuer Per-Agent Config (wird zu `skills/{skill_id}.json`) |
| `version` | nein | Versions-String (Default: `"0.1.0"`) |
| `description` | nein | Beschreibung des Plugins |
| `env_prefix` | nein | Prefix fuer Umgebungsvariablen (Default: `SKILL_{SKILL_ID}_`) |
| `module` | nein | Dateiname des Skill-Moduls (Default: `skill.py`) |
| `always_load` | nein | `true` = Plugin wird immer geladen, Aktivierung nur per Character (Default: `false`) |

## PluginContext API

Jedes Plugin erhaelt einen `PluginContext` als `self.ctx`. Plugins sollen Services ausschliesslich ueber diesen Context nutzen, nicht ueber direkte Imports aus `app.*`.

### Attribute

| Attribut | Typ | Beschreibung |
|---|---|---|
| `ctx.logger` | `logging.Logger` | Logger mit Prefix `plugin.{plugin_id}` |
| `ctx.http` | `requests` | HTTP-Bibliothek (`requests`) fuer externe API-Aufrufe |
| `ctx.plugin_id` | `str` | ID des Plugins (Verzeichnisname) |

### Methoden

| Methode | Return | Beschreibung |
|---|---|---|
| `ctx.get_env(key, default=None)` | `str \| None` | Umgebungsvariable lesen |
| `ctx.get_env_int(key, default=0)` | `int` | Umgebungsvariable als Integer |
| `ctx.get_env_bool(key, default=False)` | `bool` | Umgebungsvariable als Boolean (`true`, `1`, `yes`) |

## Geerbte Methoden von BaseSkill

Durch die Vererbung von `PluginSkill` -> `BaseSkill` stehen folgende Methoden zur Verfuegung:

| Methode | Beschreibung |
|---|---|
| `self._parse_base_input(raw_input)` | Extrahiert `input`, `user_id`, `agent_name` aus dem JSON-Input |
| `self._get_effective_config(user_id, agent_name)` | Merged `.env`-Defaults (`self._defaults`) mit Per-Agent Overrides |
| `self.get_config_fields()` | Gibt konfigurierbare Felder mit Typ-Info zurueck (fuer UI) |

## Per-Agent Konfiguration

Plugins unterstuetzen automatisch Per-Agent Overrides. Die Konfiguration wird in `storage/users/{user}/agents/{agent}/skills/{skill_id}.json` gespeichert.

Beim ersten Aufruf eines Plugins fuer einen Agent wird die Datei automatisch mit den `_defaults` erstellt. Danach koennen Werte per Agent individuell angepasst werden.

Beispiel fuer `_defaults`:

```python
self._defaults = {
    "engines": "google,bing",
    "num_results": 10,
}
```

Wird zu `storage/users/user1/agents/agent1/skills/mein_plugin.json`:

```json
{
    "engines": "google,bing",
    "num_results": 10
}
```

## Aktivierung & Deaktivierung

**Global (`.env`):** `SKILL_{ID}_ENABLED=true|false`

Plugins mit `always_load: true` in `plugin.yaml` werden immer geladen. Die Aktivierung erfolgt dann nur per Character-Konfiguration.

**Per Character:** Ueber die bestehende Skill-Config im UI oder direkt in der Agent-Skill-Datei:

```json
{
    "enabled": false
}
```

## Vorhandene Plugins

| Plugin | Verzeichnis | Skill-ID | Beschreibung |
|---|---|---|---|
| SearX | `plugins/searx/` | `searx` | Web-Suche ueber selbst-gehostete SearX-Instanz |
| n8n | `plugins/n8n/` | `n8n` | Ruft n8n-Workflows per Webhook auf, Workflows + API-Key pro Character |

## Best Practices

- **Keine direkten Imports aus `app.*`** — nutze `self.ctx` fuer Services
- **`_defaults` definieren** — damit Per-Agent Config und `get_config_fields()` funktionieren
- **`SKILL_ID` setzen** — muss mit `skill_id` in `plugin.yaml` uebereinstimmen
- **Fehler abfangen** — und ueber `self.ctx.logger` loggen, nicht per `print()`
- **`as_tool()` ueberschreiben** — fuer eine aussagekraeftige Tool-Beschreibung fuer das LLM
- **Verbindungstests im `__init__`** — pruefen ob externe Services erreichbar sind (mit try/except)
