"""Log Viewer - LLM-Aufrufe als durchsuchbare Webseite"""
import json
from pathlib import Path
from fastapi import APIRouter, Query, Depends
from fastapi.responses import HTMLResponse
from typing import Dict, Any
from app.core.log import get_logger
from app.core.auth_dependency import require_admin

logger = get_logger("logs_route")

router = APIRouter(prefix="/logs", tags=["logs"],
                   dependencies=[Depends(require_admin)])

LOG_FILE = Path("./logs/llm_calls.jsonl")


@router.get("/llm", response_class=HTMLResponse)
def llm_log_viewer():
    """LLM Log Viewer - Hauptseite."""
    return HTMLResponse(content=_build_viewer_html())


@router.get("/llm/data")
def llm_log_data(
    limit: int = Query(100, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    task: str = Query("", description="Filter by task type"),
    model: str = Query("", description="Filter by model"),
    character: str = Query("", description="Filter by character"),
    provider: str = Query("", description="Filter by provider"),
    search: str = Query("", description="Volltextsuche")) -> Dict[str, Any]:
    """JSON-API fuer Log-Eintraege."""
    if not LOG_FILE.exists():
        return {"entries": [], "total": 0, "tasks": [], "models": [], "providers": []}

    entries = []
    all_tasks = set()
    all_models = set()
    all_characters = set()
    all_providers = set()

    with open(LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                all_tasks.add(obj.get("task", ""))
                all_models.add(obj.get("model", ""))
                svc = obj.get("service", "")
                if svc:
                    all_characters.add(svc)
                prov = obj.get("provider", "")
                if prov:
                    all_providers.add(prov)

                if task and obj.get("task") != task:
                    continue
                if model and obj.get("model") != model:
                    continue
                if character and obj.get("service") != character:
                    continue
                if provider and obj.get("provider", "") != provider:
                    continue
                if search:
                    haystack = json.dumps(obj, ensure_ascii=False).lower()
                    if search.lower() not in haystack:
                        continue

                entries.append(obj)
            except json.JSONDecodeError:
                continue

    # Neueste zuerst
    entries.reverse()
    total = len(entries)
    page = entries[offset:offset + limit]

    return {
        "entries": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "tasks": sorted(all_tasks),
        "models": sorted(all_models),
        "characters": sorted(all_characters),
        "providers": sorted(all_providers),
    }


IMAGE_LOG_FILE = Path("./logs/image_prompts.jsonl")


@router.get("/image-prompts", response_class=HTMLResponse)
def image_prompt_log_viewer():
    """Image Prompt Log Viewer - Hauptseite."""
    return HTMLResponse(content=_build_image_viewer_html())


@router.get("/image-prompts/data")
def image_prompt_log_data(
    limit: int = Query(100, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    character: str = Query("", description="Filter by character"),
    backend: str = Query("", description="Filter by backend"),
    model: str = Query("", description="Filter by model"),
    search: str = Query("", description="Volltextsuche")) -> Dict[str, Any]:
    """JSON-API fuer Image-Prompt Log-Eintraege."""
    if not IMAGE_LOG_FILE.exists():
        return {"entries": [], "total": 0, "characters": [], "backends": [], "models": []}

    entries = []
    all_characters = set()
    all_backends = set()
    all_models = set()

    with open(IMAGE_LOG_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                svc = obj.get("service", "")
                if svc:
                    all_characters.add(svc)
                bn = (obj.get("backend") or {}).get("name", "")
                if bn:
                    all_backends.add(bn)
                mn = obj.get("model", "")
                if mn:
                    all_models.add(mn)

                if character and obj.get("service") != character:
                    continue
                if backend and (obj.get("backend") or {}).get("name") != backend:
                    continue
                if model and obj.get("model") != model:
                    continue
                if search:
                    haystack = json.dumps(obj, ensure_ascii=False).lower()
                    if search.lower() not in haystack:
                        continue

                entries.append(obj)
            except json.JSONDecodeError:
                continue

    entries.reverse()
    total = len(entries)
    page = entries[offset:offset + limit]

    return {
        "entries": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "characters": sorted(all_characters),
        "backends": sorted(all_backends),
        "models": sorted(all_models),
    }


def _build_image_viewer_html() -> str:
    return '''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Image Prompt Log Viewer</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0d1117; color: #c9d1d9; }

.toolbar {
    position: sticky; top: 0; z-index: 100;
    background: #161b22; border-bottom: 1px solid #30363d;
    padding: 10px 16px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
}
.toolbar select, .toolbar input {
    background: #0d1117; color: #c9d1d9; border: 1px solid #30363d;
    padding: 6px 10px; border-radius: 6px; font-size: 13px;
}
.toolbar select { min-width: 140px; }
.toolbar input[type="text"] { min-width: 220px; }
.toolbar .count { color: #8b949e; font-size: 13px; margin-left: auto; }
.toolbar button {
    background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
    padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 13px;
}
.toolbar button:hover { background: #30363d; }

.entries { padding: 8px; }

.entry {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    margin-bottom: 8px; overflow: hidden;
}
.entry-header {
    display: flex; align-items: center; gap: 10px; padding: 10px 14px;
    cursor: pointer; flex-wrap: wrap;
}
.entry-header:hover { background: #1c2128; }

.badge {
    display: inline-block; padding: 2px 8px; border-radius: 12px;
    font-size: 11px; font-weight: 600; white-space: nowrap;
}
.badge-number { background: #8b949e22; color: #8b949e; font-family: "JetBrains Mono", "Fira Code", monospace; min-width: 40px; text-align: center; }
.badge-time { background: #8b949e33; color: #8b949e; }
.badge-character { background: #bc8cff33; color: #bc8cff; }
.badge-backend { background: #3fb95033; color: #3fb950; }
.badge-prompt { background: #1f6feb33; color: #58a6ff; max-width: 500px; overflow: hidden; text-overflow: ellipsis; }
.badge-enhance { background: #d2992233; color: #d29922; }
.badge-model { background: #f778ba33; color: #f778ba; }
.badge-seed { background: #79c0ff33; color: #79c0ff; font-family: monospace; }
.badge-lora { background: #f0883e33; color: #f0883e; }

.entry-body { display: none; border-top: 1px solid #30363d; }
.entry-body.open { display: block; }

.section { border-bottom: 1px solid #21262d; }
.section:last-child { border-bottom: none; }
.section-header {
    padding: 8px 14px; background: #0d1117; cursor: pointer;
    font-size: 12px; font-weight: 600; color: #8b949e; text-transform: uppercase;
    letter-spacing: 0.5px; display: flex; align-items: center; gap: 6px;
}
.section-header:hover { color: #c9d1d9; }
.section-header .arrow { transition: transform 0.15s; font-size: 10px; }
.section-header .arrow.open { transform: rotate(90deg); }
.section-content { display: none; padding: 12px 14px; overflow-x: auto; }
.section-content.open { display: block; }
.section-content pre {
    white-space: pre-wrap; word-wrap: break-word; font-family: "JetBrains Mono", "Fira Code", monospace;
    font-size: 13px; line-height: 1.5; color: #e6edf3;
}

.pager { display: flex; justify-content: center; gap: 8px; padding: 16px; }
.pager button { min-width: 80px; }
.pager button:disabled { opacity: 0.4; cursor: default; }

.highlight { background: #6e40c966; border-radius: 2px; }
</style>
</head>
<body>

<div class="toolbar">
    <select id="characterFilter"><option value="">Alle Characters</option></select>
    <select id="backendFilter"><option value="">Alle Backends</option></select>
    <select id="modelFilter"><option value="">Alle Models</option></select>
    <input type="text" id="searchInput" placeholder="Suche..." />
    <button onclick="doSearch()">Suchen</button>
    <button onclick="resetFilters()">Reset</button>
    <span class="count" id="countLabel"></span>
</div>

<div class="entries" id="entries"></div>

<div class="pager">
    <button id="prevBtn" onclick="prevPage()" disabled>Zurueck</button>
    <span id="pageLabel" style="color:#8b949e;font-size:13px;line-height:32px;"></span>
    <button id="nextBtn" onclick="nextPage()" disabled>Weiter</button>
</div>

<script>
const PAGE_SIZE = 50;
let currentOffset = 0;
let totalEntries = 0;

async function loadData() {
    const character = document.getElementById('characterFilter').value;
    const backend = document.getElementById('backendFilter').value;
    const model = document.getElementById('modelFilter').value;
    const search = document.getElementById('searchInput').value;

    const params = new URLSearchParams({
        limit: PAGE_SIZE, offset: currentOffset,
        character, backend, model, search
    });

    const resp = await fetch('/logs/image-prompts/data?' + params);
    const data = await resp.json();
    totalEntries = data.total;

    const charSel = document.getElementById('characterFilter');
    if (charSel.options.length <= 1) {
        (data.characters || []).forEach(c => {
            if (c) { const o = new Option(c, c); charSel.add(o); }
        });
    }
    const backSel = document.getElementById('backendFilter');
    if (backSel.options.length <= 1) {
        (data.backends || []).forEach(b => {
            if (b) { const o = new Option(b, b); backSel.add(o); }
        });
    }
    const modelSel = document.getElementById('modelFilter');
    if (modelSel.options.length <= 1) {
        (data.models || []).forEach(m => {
            if (m) { const o = new Option(m, m); modelSel.add(o); }
        });
    }

    renderEntries(data.entries, search);
    updatePager();
}

function renderEntries(entries, searchTerm) {
    const container = document.getElementById('entries');
    container.innerHTML = '';

    entries.forEach((e, idx) => {
        const globalIdx = currentOffset + idx;
        const entryNum = totalEntries - globalIdx;
        const backendName = (e.backend || {}).name || '?';
        const promptPreview = (e.original_prompt || '').substring(0, 80);
        const charBadge = e.service ? `<span class="badge badge-character">${esc(e.service)}</span>` : '';
        const modelBadge = e.model ? `<span class="badge badge-model">${esc(e.model)}</span>` : '';
        const seedBadge = e.seed ? `<span class="badge badge-seed">seed:${e.seed}</span>` : '';
        const enhanceBadge = e.auto_enhance ? '<span class="badge badge-enhance">enhanced</span>' : '';
        const loraList = (e.loras || []).filter(l => l.name && l.name !== 'None');
        const loraBadge = loraList.length > 0 ? `<span class="badge badge-lora">LoRA: ${esc(loraList.map(l => l.name).join(', '))}</span>` : '';

        const div = document.createElement('div');
        div.className = 'entry';
        div.innerHTML = `
            <div class="entry-header" onclick="toggleEntry(this)">
                <span class="badge badge-number">#${entryNum}</span>
                <span class="badge badge-time">${e.starttime || ''}</span>
                ${charBadge}
                <span class="badge badge-backend">${esc(backendName)}</span>
                ${modelBadge}
                ${seedBadge}
                ${enhanceBadge}
                ${loraBadge}
                <span class="badge badge-prompt">${esc(promptPreview)}</span>
            </div>
            <div class="entry-body" id="body-${globalIdx}">
                ${buildSections(e, searchTerm)}
            </div>
        `;
        container.appendChild(div);
    });

    document.getElementById('countLabel').textContent = totalEntries + ' Eintraege';
}

function buildSections(e, st) {
    let html = '';
    if (e.original_prompt) {
        html += buildSection('Original Prompt', fmtText(e.original_prompt, st), true);
    }
    if (e.final_prompt && e.final_prompt !== e.original_prompt) {
        html += buildSection('Final Prompt', fmtText(e.final_prompt, st), false);
    }
    if (e.negative_prompt) {
        html += buildSection('Negative Prompt', fmtText(e.negative_prompt, st), false);
    }
    if (e.pose_prompt) {
        html += buildSection('Pose Prompt', fmtText(e.pose_prompt, st), false);
    }
    if (e.expression_prompt) {
        html += buildSection('Expression Prompt', fmtText(e.expression_prompt, st), false);
    }
    const loras = (e.loras || []).filter(l => l.name && l.name !== 'None');
    if (loras.length > 0) {
        const loraText = loras.map(l => l.name + ' (strength: ' + l.strength + ')').join('\\n');
        html += buildSection('LoRAs (' + loras.length + ')', fmtText(loraText, st), false);
    }
    const apps = e.appearances || [];
    if (apps.length > 0) {
        const appText = apps.map(a => a.name + ': ' + a.appearance).join('\\n');
        html += buildSection('Appearances (' + apps.length + ')', fmtText(appText, st), false);
    }
    const ctx = e.context || {};
    if (Object.keys(ctx).length > 0) {
        html += buildSection('Context', '<pre>' + esc(JSON.stringify(ctx, null, 2)) + '</pre>', false);
    }
    // Meta
    const meta = {
        service: e.service, user_id: e.user_id,
        backend: e.backend, model: e.model, seed: e.seed || 0,
        loras: e.loras || [],
        agent_mentioned: e.agent_mentioned, auto_enhance: e.auto_enhance,
        starttime: e.starttime, endtime: e.endtime,
    };
    html += buildSection('Meta', '<pre>' + esc(JSON.stringify(meta, null, 2)) + '</pre>', false);
    return html;
}

function buildSection(title, content, startOpen) {
    const openCls = startOpen ? ' open' : '';
    return `<div class="section">
        <div class="section-header" onclick="toggleSection(this)">
            <span class="arrow${openCls}">&#9654;</span> ${title}
        </div>
        <div class="section-content${openCls}">${content}</div>
    </div>`;
}

function fmtText(text, searchTerm) {
    let s = esc(text);
    if (searchTerm) {
        const regex = new RegExp('(' + escRx(esc(searchTerm)) + ')', 'gi');
        s = s.replace(regex, '<span class="highlight">$1</span>');
    }
    return '<pre>' + s + '</pre>';
}

function esc(s) {
    if (!s) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function escRx(s) { return s.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&'); }

function toggleEntry(header) { header.nextElementSibling.classList.toggle('open'); }
function toggleSection(header) {
    header.querySelector('.arrow').classList.toggle('open');
    header.nextElementSibling.classList.toggle('open');
}

function doSearch() { currentOffset = 0; loadData(); }
function resetFilters() {
    document.getElementById('characterFilter').value = '';
    document.getElementById('backendFilter').value = '';
    document.getElementById('modelFilter').value = '';
    document.getElementById('searchInput').value = '';
    currentOffset = 0; loadData();
}
function prevPage() { currentOffset = Math.max(0, currentOffset - PAGE_SIZE); loadData(); window.scrollTo(0,0); }
function nextPage() {
    if (currentOffset + PAGE_SIZE < totalEntries) { currentOffset += PAGE_SIZE; loadData(); window.scrollTo(0,0); }
}
function updatePager() {
    const from = totalEntries === 0 ? 0 : currentOffset + 1;
    const to = Math.min(currentOffset + PAGE_SIZE, totalEntries);
    document.getElementById('pageLabel').textContent = from + '-' + to + ' / ' + totalEntries;
    document.getElementById('prevBtn').disabled = currentOffset === 0;
    document.getElementById('nextBtn').disabled = currentOffset + PAGE_SIZE >= totalEntries;
}

document.getElementById('searchInput').addEventListener('keydown', e => { if (e.key === 'Enter') doSearch(); });
document.getElementById('characterFilter').addEventListener('change', () => { currentOffset = 0; loadData(); });
document.getElementById('backendFilter').addEventListener('change', () => { currentOffset = 0; loadData(); });
document.getElementById('modelFilter').addEventListener('change', () => { currentOffset = 0; loadData(); });

loadData();
</script>
</body>
</html>'''


def _build_viewer_html() -> str:
    return '''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LLM Log Viewer</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0d1117; color: #c9d1d9; }

.toolbar {
    position: sticky; top: 0; z-index: 100;
    background: #161b22; border-bottom: 1px solid #30363d;
    padding: 10px 16px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
}
.toolbar select, .toolbar input {
    background: #0d1117; color: #c9d1d9; border: 1px solid #30363d;
    padding: 6px 10px; border-radius: 6px; font-size: 13px;
}
.toolbar select { min-width: 140px; }
.toolbar input[type="text"] { min-width: 220px; }
.toolbar .count { color: #8b949e; font-size: 13px; margin-left: auto; }
.toolbar button {
    background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
    padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 13px;
}
.toolbar button:hover { background: #30363d; }

.entries { padding: 8px; }

.entry {
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    margin-bottom: 8px; overflow: hidden;
}
.entry-header {
    display: flex; align-items: center; gap: 10px; padding: 10px 14px;
    cursor: pointer; flex-wrap: wrap;
}
.entry-header:hover { background: #1c2128; }

.badge {
    display: inline-block; padding: 2px 8px; border-radius: 12px;
    font-size: 11px; font-weight: 600; white-space: nowrap;
}
.badge-number { background: #8b949e22; color: #8b949e; font-family: "JetBrains Mono", "Fira Code", monospace; min-width: 40px; text-align: center; }
.badge-task { background: #1f6feb33; color: #58a6ff; }
.badge-model { background: #3fb95033; color: #3fb950; }
.badge-character { background: #bc8cff33; color: #bc8cff; }
.badge-provider { background: #f0883e33; color: #f0883e; }
.badge-tokens { background: #d2992233; color: #d29922; }
.badge-time { background: #8b949e33; color: #8b949e; }
.badge-duration { background: #f8514933; color: #f85149; }
.badge-role { background: #da363333; color: #ff7b72; }
.badge-role-tool { background: #f0883e33; color: #f0883e; }
.badge-role-chat { background: #3fb95033; color: #3fb950; }

.entry-body { display: none; border-top: 1px solid #30363d; }
.entry-body.open { display: block; }

.section {
    border-bottom: 1px solid #21262d;
}
.section:last-child { border-bottom: none; }
.section-header {
    padding: 8px 14px; background: #0d1117; cursor: pointer;
    font-size: 12px; font-weight: 600; color: #8b949e; text-transform: uppercase;
    letter-spacing: 0.5px; display: flex; align-items: center; gap: 6px;
}
.section-header:hover { color: #c9d1d9; }
.section-header .arrow { transition: transform 0.15s; font-size: 10px; }
.section-header .arrow.open { transform: rotate(90deg); }
.section-content {
    display: none; padding: 12px 14px; overflow-x: auto;
}
.section-content.open { display: block; }
.section-content pre {
    white-space: pre-wrap; word-wrap: break-word; font-family: "JetBrains Mono", "Fira Code", monospace;
    font-size: 13px; line-height: 1.5; color: #e6edf3;
}

.msg-turn { margin-bottom: 10px; border-left: 2px solid #30363d; padding-left: 10px; }
.msg-turn:last-child { margin-bottom: 0; }
.msg-turn.role-user { border-left-color: #1f6feb; }
.msg-turn.role-assistant { border-left-color: #2ea043; }
.msg-turn.role-system { border-left-color: #d29922; }
.msg-role { font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.5px; color: #8b949e; margin-bottom: 4px; }
.msg-turn.role-user .msg-role { color: #58a6ff; }
.msg-turn.role-assistant .msg-role { color: #56d364; }
.msg-turn.role-system .msg-role { color: #e3b341; }
.msg-meta { color: #6e7681; font-size: 11px; margin-bottom: 8px; }

.pager {
    display: flex; justify-content: center; gap: 8px; padding: 16px;
}
.pager button { min-width: 80px; }
.pager button:disabled { opacity: 0.4; cursor: default; }

.highlight { background: #6e40c966; border-radius: 2px; }
</style>
</head>
<body>

<div class="toolbar">
    <select id="taskFilter"><option value="">Alle Tasks</option></select>
    <select id="providerFilter"><option value="">Alle Provider</option></select>
    <select id="modelFilter"><option value="">Alle Models</option></select>
    <select id="characterFilter"><option value="">Alle Characters</option></select>
    <input type="text" id="searchInput" placeholder="Suche..." />
    <button onclick="doSearch()">Suchen</button>
    <button onclick="resetFilters()">Reset</button>
    <span class="count" id="countLabel"></span>
    <a href="/dashboard" style="margin-left:auto;color:#58a6ff;font-size:13px;text-decoration:none;">Dashboard</a>
</div>

<div class="entries" id="entries"></div>

<div class="pager">
    <button id="prevBtn" onclick="prevPage()" disabled>Zurueck</button>
    <span id="pageLabel" style="color:#8b949e;font-size:13px;line-height:32px;"></span>
    <button id="nextBtn" onclick="nextPage()" disabled>Weiter</button>
</div>

<script>
const PAGE_SIZE = 50;
let currentOffset = 0;
let totalEntries = 0;

async function loadData() {
    const task = document.getElementById('taskFilter').value;
    const provider = document.getElementById('providerFilter').value;
    const model = document.getElementById('modelFilter').value;
    const character = document.getElementById('characterFilter').value;
    const search = document.getElementById('searchInput').value;

    const params = new URLSearchParams({
        limit: PAGE_SIZE, offset: currentOffset,
        task, provider, model, character, search
    });

    const resp = await fetch('/logs/llm/data?' + params);
    const data = await resp.json();
    totalEntries = data.total;

    // Filter-Dropdowns befuellen (nur beim ersten Mal)
    const taskSel = document.getElementById('taskFilter');
    if (taskSel.options.length <= 1) {
        data.tasks.forEach(t => {
            if (t) { const o = new Option(t, t); taskSel.add(o); }
        });
    }
    const modelSel = document.getElementById('modelFilter');
    if (modelSel.options.length <= 1) {
        data.models.forEach(m => {
            if (m) { const o = new Option(m, m); modelSel.add(o); }
        });
    }
    const charSel = document.getElementById('characterFilter');
    if (charSel.options.length <= 1) {
        (data.characters || []).forEach(c => {
            if (c) { const o = new Option(c, c); charSel.add(o); }
        });
    }
    const provSel = document.getElementById('providerFilter');
    if (provSel.options.length <= 1) {
        (data.providers || []).forEach(p => {
            if (p) { const o = new Option(p, p); provSel.add(o); }
        });
    }

    renderEntries(data.entries, search);
    updatePager();
}

function renderEntries(entries, searchTerm) {
    const container = document.getElementById('entries');
    container.innerHTML = '';

    entries.forEach((e, idx) => {
        const globalIdx = currentOffset + idx;
        const entryNum = totalEntries - globalIdx;
        const tokens = e.tokens || {};
        const duration = e.duration_s ? e.duration_s.toFixed(1) + 's' : '';
        const tokenStr = (tokens.input || 0) + '/' + (tokens.output || 0);

        const div = document.createElement('div');
        div.className = 'entry';
        const charBadge = e.service ? `<span class="badge badge-character">${escapeHtml(e.service)}</span>` : '';
        const provBadge = e.provider ? `<span class="badge badge-provider">${escapeHtml(e.provider)}</span>` : '';
        const role = e.llm_role || '';
        const roleCls = role === 'Tool-LLM' ? 'badge-role-tool' : role === 'Chat-LLM' ? 'badge-role-chat' : 'badge-role';
        const roleBadge = (role && role !== e.task) ? `<span class="badge ${roleCls}">${escapeHtml(role)}</span>` : '';
        // Template-Basename: bevorzugt e.template (zeigt welche .md-Datei
        // gerendert wurde — erleichtert Fehlersuche). Bei aelteren Eintraegen
        // ohne template-Feld faellt es auf e.task zurueck.
        const tplName = e.template || e.task || '?';
        const tplTitle = e.template ? 'Template: ' + e.template : 'Task: ' + (e.task || '?');
        div.innerHTML = `
            <div class="entry-header" onclick="toggleEntry(this)">
                <span class="badge badge-number">#${entryNum}</span>
                <span class="badge badge-time">${e.starttime || ''}</span>
                <span class="badge badge-task" title="${escapeHtml(tplTitle)}">${escapeHtml(tplName)}</span>
                ${roleBadge}
                ${charBadge}
                ${provBadge}
                <span class="badge badge-model">${e.model || '?'}</span>
                <span class="badge badge-tokens">${tokenStr} tok</span>
                <span class="badge badge-duration">${duration}</span>
            </div>
            <div class="entry-body" id="body-${globalIdx}">
                ${buildSections(e, searchTerm)}
            </div>
        `;
        container.appendChild(div);
    });

    document.getElementById('countLabel').textContent = totalEntries + ' Eintraege';
}

function buildSections(e, searchTerm) {
    const prompt = e.prompt || {};
    let html = '';

    if (prompt.system) {
        html += buildSection('System Prompt', formatText(prompt.system, searchTerm), true);
    }
    if (Array.isArray(prompt.messages) && prompt.messages.length) {
        const title = `Conversation History (${prompt.messages.length} turns)`;
        html += buildSection(title, formatMessages(prompt.messages, searchTerm), false);
    }
    if (prompt.user) {
        html += buildSection('User / Input', formatText(prompt.user, searchTerm), !prompt.system);
    }
    if (e.response) {
        html += buildSection('Response', formatText(e.response, searchTerm), false);
    }
    // Meta-Daten
    const meta = {
        task: e.task, template: e.template || '', llm_role: e.llm_role || '',
        model: e.model, service: e.service,
        user_id: e.user_id,
        starttime: e.starttime, endtime: e.endtime,
        duration_s: e.duration_s,
        tokens: e.tokens
    };
    html += buildSection('Meta', '<pre>' + escapeHtml(JSON.stringify(meta, null, 2)) + '</pre>', false);

    return html;
}

function buildSection(title, content, startOpen) {
    const openCls = startOpen ? ' open' : '';
    return `
        <div class="section">
            <div class="section-header" onclick="toggleSection(this)">
                <span class="arrow${openCls}">&#9654;</span> ${title}
            </div>
            <div class="section-content${openCls}">${content}</div>
        </div>
    `;
}

function formatText(text, searchTerm) {
    let escaped = escapeHtml(text);
    if (searchTerm) {
        const regex = new RegExp('(' + escapeRegex(escapeHtml(searchTerm)) + ')', 'gi');
        escaped = escaped.replace(regex, '<span class="highlight">$1</span>');
    }
    return '<pre>' + escaped + '</pre>';
}

function formatMessages(messages, searchTerm) {
    return messages.map(m => {
        const role = (m && m.role) || 'unknown';
        const content = (m && m.content) || '';
        const cls = 'role-' + role.replace(/[^a-z0-9]/gi, '');
        return `<div class="msg-turn ${cls}">
            <div class="msg-role">${escapeHtml(role)}</div>
            ${formatText(content, searchTerm)}
        </div>`;
    }).join('');
}

function escapeHtml(s) {
    if (!s) return '';
    return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function escapeRegex(s) {
    return s.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
}

function toggleEntry(header) {
    const body = header.nextElementSibling;
    body.classList.toggle('open');
}

function toggleSection(header) {
    const arrow = header.querySelector('.arrow');
    const content = header.nextElementSibling;
    arrow.classList.toggle('open');
    content.classList.toggle('open');
}

function doSearch() {
    currentOffset = 0;
    loadData();
}

function resetFilters() {
    document.getElementById('taskFilter').value = '';
    document.getElementById('providerFilter').value = '';
    document.getElementById('modelFilter').value = '';
    document.getElementById('characterFilter').value = '';
    document.getElementById('searchInput').value = '';
    currentOffset = 0;
    loadData();
}

function prevPage() {
    currentOffset = Math.max(0, currentOffset - PAGE_SIZE);
    loadData();
    window.scrollTo(0, 0);
}

function nextPage() {
    if (currentOffset + PAGE_SIZE < totalEntries) {
        currentOffset += PAGE_SIZE;
        loadData();
        window.scrollTo(0, 0);
    }
}

function updatePager() {
    const from = totalEntries === 0 ? 0 : currentOffset + 1;
    const to = Math.min(currentOffset + PAGE_SIZE, totalEntries);
    document.getElementById('pageLabel').textContent = from + '-' + to + ' / ' + totalEntries;
    document.getElementById('prevBtn').disabled = currentOffset === 0;
    document.getElementById('nextBtn').disabled = currentOffset + PAGE_SIZE >= totalEntries;
}

// Enter-Taste in Suchfeld
document.getElementById('searchInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') doSearch();
});

// Filter-Aenderungen
document.getElementById('taskFilter').addEventListener('change', () => { currentOffset = 0; loadData(); });
document.getElementById('modelFilter').addEventListener('change', () => { currentOffset = 0; loadData(); });
document.getElementById('characterFilter').addEventListener('change', () => { currentOffset = 0; loadData(); });

// URL-Params -> Filter-Felder uebernehmen, damit Deep-Links (z.B. vom
// Agent-Loop-Admin: /logs/llm?character=X&search=YYYY-MM-DD HH:MM)
// die Liste vorgefiltert anzeigen UND den ersten passenden Eintrag
// automatisch aufklappen.
(function applyUrlParams() {
    try {
        const params = new URLSearchParams(window.location.search);
        const ch = params.get('character') || '';
        const tk = params.get('task') || '';
        const md = params.get('model') || '';
        const sr = params.get('search') || '';
        if (sr) document.getElementById('searchInput').value = sr;
        // Character/Task/Model-Selects werden erst nach loadData mit Optionen
        // befuellt — wir setzen den Wert nach dem ersten Render via Watch.
        window.__pendingPreselect = { ch, tk, md, search: sr };
    } catch (_) {}
})();

// Hook: nach jedem Render pruefen ob noch ein Pending-Preselect zu
// applizieren ist + den ersten Match auto-expand.
const _origLoadData = loadData;
loadData = async function() {
    await _origLoadData.apply(this, arguments);
    const pre = window.__pendingPreselect;
    if (pre) {
        let needsReload = false;
        const setIf = (id, val) => {
            if (!val) return false;
            const el = document.getElementById(id);
            if (!el) return false;
            // Pruefen ob Wert in Options vorhanden ist — sonst Option anlegen
            let exists = false;
            for (const o of el.options) { if (o.value === val) { exists = true; break; } }
            if (!exists) el.add(new Option(val, val));
            if (el.value !== val) { el.value = val; return true; }
            return false;
        };
        needsReload |= setIf('characterFilter', pre.ch);
        needsReload |= setIf('taskFilter', pre.tk);
        needsReload |= setIf('modelFilter', pre.md);
        if (needsReload) {
            window.__pendingPreselect = null;
            currentOffset = 0;
            await _origLoadData();
        } else {
            window.__pendingPreselect = null;
        }
        // Ersten Eintrag auto-expand (wenn nach Filterung sichtbar)
        const firstEntry = document.querySelector('.entries .entry .entry-header');
        if (firstEntry) {
            firstEntry.click();
            firstEntry.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
    }
};

// Initial laden
loadData();
</script>
</body>
</html>'''
