"""System Load Dashboard — Uebersicht ueber LLM- und Bildgenerierungs-Last.

Liest logs/llm_calls.jsonl und logs/image_prompts.jsonl, aggregiert die Daten
und stellt sie als interaktive Timeline mit Chart.js dar.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Query, Depends
from fastapi.responses import HTMLResponse
from app.core.log import get_logger
from app.core.auth_dependency import require_admin

logger = get_logger("dashboard")

router = APIRouter(prefix="/dashboard", tags=["dashboard"],
                   dependencies=[Depends(require_admin)])

LLM_LOG = Path("./logs/llm_calls.jsonl")
IMAGE_LOG = Path("./logs/image_prompts.jsonl")


def _get_systems() -> List[Dict[str, Any]]:
    """Laedt System-Konfiguration aus ProviderManager (basierend auf .env)."""
    try:
        from app.core.provider_manager import get_provider_manager
        return get_provider_manager().get_systems_config()
    except Exception:
        return []


def _read_jsonl(path: Path, cutoff: str = "") -> List[Dict[str, Any]]:
    """Liest JSONL-Datei und filtert nach cutoff-Zeitpunkt."""
    if not path.exists():
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff and entry.get("endtime", entry.get("starttime", "")) < cutoff:
                continue
            entries.append(entry)
    return entries


@router.get("/data")
def dashboard_data(hours: int = Query(24, ge=0)):
    """Liefert aggregierte Dashboard-Daten als JSON."""
    cutoff = ""
    if hours > 0:
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")

    # LLM Calls laden
    raw_llm = _read_jsonl(LLM_LOG, cutoff)
    llm_calls = []
    for e in raw_llm:
        llm_calls.append({
            "starttime": e.get("starttime", ""),
            "endtime": e.get("endtime", ""),
            "duration_s": e.get("duration_s", 0),
            "task": e.get("task", ""),
            "model": e.get("model", ""),
            "provider": e.get("provider", ""),
            "service": e.get("service", ""),
            "tokens_in": e.get("tokens", {}).get("input", 0),
            "tokens_out": e.get("tokens", {}).get("output", 0),
        })

    # Image Calls laden
    raw_img = _read_jsonl(IMAGE_LOG, cutoff)
    image_calls = []
    for e in raw_img:
        be = e.get("backend", {})
        st = e.get("starttime", "")
        et = e.get("endtime", "")
        dur = 0.0
        if st and et:
            try:
                dur = (datetime.fromisoformat(et) - datetime.fromisoformat(st)).total_seconds()
            except (ValueError, TypeError):
                pass
        image_calls.append({
            "starttime": st,
            "endtime": et,
            "duration_s": round(dur, 2),
            "backend": be.get("name", ""),
            "backend_type": be.get("type", ""),
            "service": e.get("service", ""),
            "model": e.get("model", ""),
        })

    # Zeitraum
    all_starts = [c["starttime"] for c in llm_calls + image_calls if c.get("starttime")]
    all_ends = [c["endtime"] for c in llm_calls + image_calls if c.get("endtime")]
    time_start = min(all_starts) if all_starts else ""
    time_end = max(all_ends) if all_ends else ""

    return {
        "time_range": {"start": time_start, "end": time_end},
        "llm_calls": llm_calls,
        "image_calls": image_calls,
        "systems": _get_systems(),
    }


@router.get("/activity")
def activity_feed(hours: int = Query(24, ge=0)):
    """Aggregierter Activity-Feed: Instagram, Reaktionen, Story Arcs, Gedanken-Nachrichten."""
    cutoff = ""
    if hours > 0:
        cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")

    events = []

    # 1. Instagram Posts + Kommentare
    try:
        from app.models.instagram import load_feed
        feed = load_feed()
        for post in feed:
            ts = post.get("timestamp", "")
            if cutoff and ts < cutoff:
                continue
            events.append({
                "type": "instagram_post",
                "timestamp": ts,
                "character": post.get("agent_name", ""),
                "summary": (post.get("caption", "") or "")[:150],
                "detail": ", ".join(post.get("hashtags", [])[:5]),
            })
            for comment in post.get("comments", []):
                cts = comment.get("timestamp", "")
                if cutoff and cts < cutoff:
                    continue
                events.append({
                    "type": "instagram_reaction",
                    "timestamp": cts,
                    "character": comment.get("author", ""),
                    "summary": (comment.get("text", "") or "")[:150],
                    "detail": f"auf Post von {post.get('agent_name', '')}",
                })
    except Exception as e:
        logger.debug("Activity: Instagram error: %s", e)

    # 2. Story Arcs + Beats
    try:
        from app.models.story_arcs import get_all_arcs
        for arc in get_all_arcs():
            ts = arc.get("created_at", "")
            if not (cutoff and ts < cutoff):
                events.append({
                    "type": "story_arc",
                    "timestamp": ts,
                    "character": ", ".join(arc.get("participants", [])),
                    "summary": f"Arc gestartet: {arc.get('title', '')}",
                    "detail": arc.get("seed", "")[:120],
                    "meta": {"status": arc.get("status"), "tension": arc.get("tension")},
                })
            for beat in arc.get("beats", []):
                bts = beat.get("timestamp", "")
                if cutoff and bts < cutoff:
                    continue
                beat_meta = {}
                scene_img = beat.get("scene_image")
                if scene_img and scene_img.get("filename") and scene_img.get("character"):
                    beat_meta["image_url"] = (
                        f"/characters/{scene_img['character']}/images/"
                        f"{scene_img['filename']}"
                    )
                events.append({
                    "type": "story_beat",
                    "timestamp": bts,
                    "character": ", ".join(arc.get("participants", [])),
                    "summary": f"Beat {beat.get('beat', '?')}: {beat.get('summary', '')[:120]}",
                    "detail": arc.get("title", ""),
                    **({"meta": beat_meta} if beat_meta else {}),
                })
            if arc.get("status") == "resolved" and arc.get("resolution"):
                rts = arc.get("updated_at", "")
                if not (cutoff and rts < cutoff):
                    events.append({
                        "type": "story_resolved",
                        "timestamp": rts,
                        "character": ", ".join(arc.get("participants", [])),
                        "summary": f"Arc abgeschlossen: {arc.get('title', '')}",
                        "detail": (arc.get("resolution", "") or "")[:120],
                    })
    except Exception as e:
        logger.debug("Activity: Story Arcs error: %s", e)

    # 3. Gedanken-Nachrichten + Social Dialogs (aus Notifications)
    try:
        from app.models.notifications import get_notifications
        notifs = get_notifications(limit=200)
        for n in notifs:
            ts = n.get("timestamp", "")
            if cutoff and ts < cutoff:
                continue
            trigger = (n.get("metadata") or {}).get("trigger", "")
            if trigger == "thought":
                events.append({
                    "type": "thought",
                    "timestamp": ts,
                    "character": n.get("character", ""),
                    "summary": (n.get("content", "") or "")[:150],
                    "detail": "",
                })
    except Exception as e:
        logger.debug("Activity: Notifications error: %s", e)

    # 4. (frueher: Social Dialogs aus log geparst — entfaellt, da sie jetzt
    #     normale Chat-Eintraege sind, siehe plan-thoughts-and-conversation.md)

    # Nach Zeitstempel sortieren (neueste zuerst)
    events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)

    return {
        "events": events[:200],
        "total": len(events),
    }


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard_page():
    """Rendert die Dashboard-HTML-Seite."""
    return HTMLResponse(content=_build_dashboard_html())


def _build_dashboard_html() -> str:
    return '''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>System Load Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, sans-serif;
       background: #0d1117; color: #c9d1d9; line-height: 1.5; }
a { color: #58a6ff; text-decoration: none; }
a:hover { text-decoration: underline; }

.header { display: flex; align-items: center; justify-content: space-between;
          padding: 16px 24px; background: #161b22; border-bottom: 1px solid #30363d; }
.header h1 { font-size: 18px; font-weight: 600; }
.header-links { display: flex; gap: 12px; font-size: 13px; }
.time-btns { display: flex; gap: 6px; }
.time-btn { padding: 5px 14px; border: 1px solid #30363d; border-radius: 6px;
            background: #21262d; color: #c9d1d9; cursor: pointer; font-size: 13px;
            transition: background 0.15s, border-color 0.15s; }
.time-btn:hover { background: #30363d; }
.time-btn.active { background: #1f6feb; border-color: #1f6feb; color: #fff; }

.content { max-width: 1400px; margin: 0 auto; padding: 20px 24px; }

/* Summary Cards */
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 24px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
.card-label { font-size: 12px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
.card-value { font-size: 28px; font-weight: 700; }
.card-sub { font-size: 12px; color: #8b949e; margin-top: 4px; }
.card-value.llm { color: #58a6ff; }
.card-value.img { color: #3fb950; }
.card-value.dur { color: #d29922; }
.card-value.tok { color: #bc8cff; }

/* Chart sections */
.chart-section { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                 padding: 16px; margin-bottom: 16px; }
.chart-title { font-size: 14px; font-weight: 600; margin-bottom: 12px; color: #e6edf3; }
.chart-wrap { position: relative; height: 260px; }

/* Detail table */
.table-section { margin-top: 16px; }
.table-section h3 { font-size: 14px; font-weight: 600; margin-bottom: 8px; color: #e6edf3; }
.detail-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.detail-table th { text-align: left; padding: 8px 10px; border-bottom: 2px solid #30363d;
                   color: #8b949e; font-weight: 600; font-size: 11px; text-transform: uppercase;
                   letter-spacing: 0.3px; cursor: pointer; user-select: none; }
.detail-table th:hover { color: #c9d1d9; }
.detail-table td { padding: 6px 10px; border-bottom: 1px solid #21262d; }
.detail-table tr:hover { background: #161b2299; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 10px; font-weight: 600; }
.badge-llm { background: #1f6feb33; color: #58a6ff; }
.badge-img { background: #23863533; color: #3fb950; }
.badge-task { background: #30363d; color: #8b949e; }
.dur-bar { display: inline-block; height: 6px; border-radius: 3px; margin-left: 6px; vertical-align: middle; }
.dur-bar-llm { background: #58a6ff; }
.dur-bar-img { background: #3fb950; }

/* Loading */
.loading { text-align: center; padding: 60px; color: #8b949e; font-size: 14px; }

/* Activity Feed */
.activity-section { margin-top: 24px; }
.activity-section h3 { font-size: 14px; font-weight: 600; margin-bottom: 12px; color: #e6edf3; }
.activity-stats { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.activity-stat { padding: 4px 12px; border-radius: 12px; font-size: 11px; font-weight: 600; cursor: pointer; border: 1px solid transparent; transition: all 0.15s; }
.activity-stat:hover { opacity: 0.85; }
.activity-stat.active { border-color: #c9d1d9; }
.act-all { background: #30363d; color: #c9d1d9; }
.act-instagram_post { background: #da3a7433; color: #f778ba; }
.act-instagram_reaction { background: #f0883e33; color: #f0883e; }
.act-social_dialog { background: #3fb95033; color: #3fb950; }
.act-thought { background: #58a6ff33; color: #58a6ff; }
.act-story_arc, .act-story_beat, .act-story_resolved { background: #bc8cff33; color: #bc8cff; }
.activity-feed { max-height: 500px; overflow-y: auto; }
.activity-item { display: flex; gap: 10px; padding: 8px 10px; border-bottom: 1px solid #21262d; align-items: flex-start; }
.activity-item:hover { background: #161b2299; }
.activity-icon { font-size: 16px; flex-shrink: 0; width: 24px; text-align: center; margin-top: 2px; }
.activity-body { flex: 1; min-width: 0; }
.activity-header { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.activity-char { font-weight: 600; font-size: 12px; color: #e6edf3; }
.activity-time { font-size: 11px; color: #8b949e; white-space: nowrap; }
.activity-text { font-size: 12px; color: #c9d1d9; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.activity-detail { font-size: 11px; color: #8b949e; margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.activity-empty { text-align: center; padding: 30px; color: #8b949e; font-style: italic; }
.activity-beat-img { display: block; margin-top: 6px; max-width: 160px; max-height: 120px; border-radius: 6px; cursor: pointer; object-fit: cover; border: 1px solid #30363d; transition: max-width 0.2s, max-height 0.2s; }
.activity-beat-img.expanded { max-width: 320px; max-height: 240px; }

/* Responsive */
@media (max-width: 800px) {
    .cards { grid-template-columns: 1fr 1fr; }
    .header { flex-direction: column; gap: 12px; }
}
</style>
</head>
<body>

<div class="header">
    <h1>System Load Dashboard</h1>
    <div class="time-btns">
        <button class="time-btn" data-hours="1">1h</button>
        <button class="time-btn" data-hours="6">6h</button>
        <button class="time-btn active" data-hours="24">24h</button>
        <button class="time-btn" data-hours="168">7d</button>
        <button class="time-btn" data-hours="0">Alle</button>
    </div>
    <div class="header-links">
        <a href="/">Chat</a>
        <a href="/admin/llm-stats">LLM Stats</a>
        <a href="/logs/llm">LLM Logs</a>
        <a href="/logs/image-prompts">Image Logs</a>
    </div>
</div>

<div class="content">
    <div class="cards" id="cards"><div class="loading">Loading data…</div></div>

    <div class="chart-section">
        <div class="chart-title">System-Auslastung (Sekunden pro Zeitfenster)</div>
        <div class="chart-wrap"><canvas id="loadChart"></canvas></div>
    </div>

    <div class="chart-section">
        <div class="chart-title">Gleichzeitige Tasks</div>
        <div class="chart-wrap"><canvas id="concChart"></canvas></div>
    </div>

    <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 16px;">
        <div class="chart-section">
            <div class="chart-title">LLM nach Task-Typ</div>
            <div class="chart-wrap"><canvas id="taskChart"></canvas></div>
        </div>
        <div class="chart-section">
            <div class="chart-title">Last pro System (LLM vs Bilder)</div>
            <div class="chart-wrap"><canvas id="provChart"></canvas></div>
        </div>
    </div>

    <div class="chart-section">
        <div class="chart-title">LLM Auswertung nach Modell (Gesamtdauer in Sekunden)</div>
        <div class="chart-wrap" style="height:auto;min-height:260px;"><canvas id="modelDurChart"></canvas></div>
    </div>

    <div class="chart-section" style="overflow-x:auto;">
        <div class="chart-title">Modell-Statistiken</div>
        <table class="detail-table" id="modelStatsTable">
            <thead>
                <tr>
                    <th data-mcol="model">Modell</th>
                    <th data-mcol="count">Aufrufe</th>
                    <th data-mcol="total_dur">Gesamt (s)</th>
                    <th data-mcol="avg_dur">&#8960; Dauer (s)</th>
                    <th data-mcol="min_dur">Min (s)</th>
                    <th data-mcol="max_dur">Max (s)</th>
                    <th data-mcol="p90_dur">P90 (s)</th>
                    <th data-mcol="tokens_in">Tokens In</th>
                    <th data-mcol="tokens_out">Tokens Out</th>
                    <th data-mcol="avg_tok_s">&#8960; Tok/s Out</th>
                </tr>
            </thead>
            <tbody id="modelStatsBody"></tbody>
        </table>
    </div>

    <div style="display:grid; grid-template-columns: 1fr 1fr; gap: 16px;">
        <div class="chart-section">
            <div class="chart-title">&#8960; Ausfuehrungszeit pro Modell (Sekunden)</div>
            <div class="chart-wrap" style="height:auto;min-height:260px;"><canvas id="modelAvgChart"></canvas></div>
        </div>
        <div class="chart-section">
            <div class="chart-title">Tokens/s Output pro Modell</div>
            <div class="chart-wrap" style="height:auto;min-height:260px;"><canvas id="modelTokChart"></canvas></div>
        </div>
    </div>

    <div class="activity-section">
        <h3>Autonome Aktivitaeten</h3>
        <div class="activity-stats" id="activityStats"></div>
        <div class="activity-feed" id="activityFeed">
            <div class="activity-empty">Loading…</div>
        </div>
    </div>

    <div class="table-section">
        <h3>Alle Aufrufe</h3>
        <table class="detail-table" id="detailTable">
            <thead>
                <tr>
                    <th data-col="starttime">Zeit</th>
                    <th data-col="type">Typ</th>
                    <th data-col="system">System</th>
                    <th data-col="model">Model</th>
                    <th data-col="task">Task</th>
                    <th data-col="service">Agent</th>
                    <th data-col="duration_s">Dauer</th>
                    <th data-col="tokens">Tokens</th>
                </tr>
            </thead>
            <tbody id="detailBody"></tbody>
        </table>
    </div>
</div>

<script>
const COLORS = {
    // Fixed system colors for consistent identification
    systemColors: {
        'ASUS-GX10': '#58a6ff',
        'Evo-X2': '#3fb950',
        'GamingPC': '#d29922',
    },
    palette: [
        '#f85149', '#bc8cff', '#f0883e', '#56d4dd', '#db61a2', '#7ee787', '#79c0ff',
    ],
    _map: {},
    get(name) {
        if (!name) return '#8b949e';
        if (this.systemColors[name]) return this.systemColors[name];
        if (!this._map[name]) {
            this._map[name] = this.palette[Object.keys(this._map).length % this.palette.length];
        }
        return this._map[name];
    }
};

let _data = null;
let _hours = 24;
let _systemMap = {};  // provider/backend name -> system name
let _systemSpecs = {};  // system name -> specs string
let loadChart = null, concChart = null, taskChart = null, provChart = null, modelDurChart = null, modelAvgChart = null, modelTokChart = null;
let _sortCol = 'starttime', _sortAsc = false;

// --- System Mapping ---
function _buildSystemMap(systems) {
    _systemMap = {};
    _systemSpecs = {};
    for (const sys of systems) {
        _systemSpecs[sys.name] = sys.specs || '';
        for (const p of (sys.providers || [])) _systemMap[p] = sys.name;
        for (const b of (sys.image_backends || [])) _systemMap[b] = sys.name;
    }
}

function _resolveSystem(providerOrBackend) {
    return _systemMap[providerOrBackend] || '';
}

function _resolveSystemLLM(call) {
    return _resolveSystem(call.provider) || '';
}

function _resolveSystemImg(call) {
    return _resolveSystem(call.backend) || '';
}

// --- Data Loading ---
async function loadData(hours) {
    _hours = hours;
    try {
        const resp = await fetch(`/dashboard/data?hours=${hours}`);
        _data = await resp.json();
        _buildSystemMap(_data.systems || []);
        renderAll();
    } catch (e) {
        document.getElementById('cards').innerHTML = '<div class="loading">Error loading</div>';
    }
}

function renderAll() {
    if (!_data) return;
    renderCards();
    renderLoadChart();
    renderConcurrencyChart();
    renderTaskChart();
    renderProvChart();
    renderModelStats();
    renderTable();
}

// --- Summary Cards ---
function renderCards() {
    const llm = _data.llm_calls;
    const img = _data.image_calls;
    const llmDurs = llm.map(c => c.duration_s).filter(d => d > 0);
    const imgDurs = img.map(c => c.duration_s).filter(d => d > 0);
    const avgLlm = llmDurs.length ? (llmDurs.reduce((a,b) => a+b, 0) / llmDurs.length).toFixed(1) : '—';
    const avgImg = imgDurs.length ? (imgDurs.reduce((a,b) => a+b, 0) / imgDurs.length).toFixed(1) : '—';
    const tokIn = llm.reduce((a,c) => a + (c.tokens_in || 0), 0);
    const tokOut = llm.reduce((a,c) => a + (c.tokens_out || 0), 0);

    // Per-System Statistiken
    const sysStats = {};
    for (const c of llm) {
        const sys = _resolveSystemLLM(c) || 'Extern';
        if (!sysStats[sys]) sysStats[sys] = { llmCount: 0, imgCount: 0, llmDur: 0, imgDur: 0 };
        sysStats[sys].llmCount++;
        sysStats[sys].llmDur += c.duration_s || 0;
    }
    for (const c of img) {
        const sys = _resolveSystemImg(c) || 'Extern';
        if (!sysStats[sys]) sysStats[sys] = { llmCount: 0, imgCount: 0, llmDur: 0, imgDur: 0 };
        sysStats[sys].imgCount++;
        sysStats[sys].imgDur += c.duration_s || 0;
    }

    let sysCardsHtml = '';
    for (const [name, s] of Object.entries(sysStats).sort((a,b) => (b[1].llmDur + b[1].imgDur) - (a[1].llmDur + a[1].imgDur))) {
        if (name === 'Extern') continue;
        const color = COLORS.get(name);
        const specs = _systemSpecs[name] || '';
        const totalDur = (s.llmDur + s.imgDur).toFixed(0);
        sysCardsHtml += `<div class="card" style="border-left:3px solid ${color}">
            <div class="card-label" style="color:${color}">${_esc(name)}</div>
            <div class="card-value" style="color:${color};font-size:20px;">${totalDur}s</div>
            <div class="card-sub">${specs ? specs + '<br>' : ''}LLM: ${s.llmCount} (${s.llmDur.toFixed(0)}s) | Bild: ${s.imgCount} (${s.imgDur.toFixed(0)}s)</div>
        </div>`;
    }

    document.getElementById('cards').innerHTML = `
        ${sysCardsHtml}
        <div class="card"><div class="card-label">LLM Calls</div><div class="card-value llm">${llm.length}</div>
            <div class="card-sub">&Oslash; ${avgLlm}s | Gesamt: ${llmDurs.reduce((a,b)=>a+b,0).toFixed(0)}s</div></div>
        <div class="card"><div class="card-label">Bilder</div><div class="card-value img">${img.length}</div>
            <div class="card-sub">&Oslash; ${avgImg}s | Gesamt: ${imgDurs.reduce((a,b)=>a+b,0).toFixed(0)}s</div></div>
        <div class="card"><div class="card-label">Tokens</div><div class="card-value tok">${_fmtNum(tokIn + tokOut)}</div>
            <div class="card-sub">In: ${_fmtNum(tokIn)} | Out: ${_fmtNum(tokOut)}</div></div>
    `;
}

function _fmtNum(n) {
    if (n >= 1000000) return (n/1000000).toFixed(1) + 'M';
    if (n >= 1000) return (n/1000).toFixed(1) + 'K';
    return n.toString();
}

// --- Load Chart (Stacked Bar per System) ---
function renderLoadChart() {
    const buckets = _makeBuckets();
    const sysLoad = {};  // system_name -> bucket_index -> total_seconds

    for (const c of _data.llm_calls) {
        const sys = _resolveSystemLLM(c) || 'Extern';
        const idx = _bucketIndex(buckets, c.starttime);
        if (idx < 0) continue;
        if (!sysLoad[sys]) sysLoad[sys] = new Array(buckets.length).fill(0);
        sysLoad[sys][idx] += c.duration_s || 0;
    }
    for (const c of _data.image_calls) {
        const sys = _resolveSystemImg(c) || 'Extern';
        const idx = _bucketIndex(buckets, c.starttime);
        if (idx < 0) continue;
        if (!sysLoad[sys]) sysLoad[sys] = new Array(buckets.length).fill(0);
        sysLoad[sys][idx] += c.duration_s || 0;
    }

    const labels = buckets.map(b => _fmtTime(b));
    // Sort systems: configured systems first, then Extern
    const sysOrder = Object.keys(sysLoad).sort((a,b) => {
        if (a === 'Extern') return 1;
        if (b === 'Extern') return -1;
        return a.localeCompare(b);
    });
    const datasets = sysOrder.map(sys => ({
        label: sys,
        data: sysLoad[sys].map(v => Math.round(v * 10) / 10),
        backgroundColor: COLORS.get(sys),
        borderWidth: 0,
        borderRadius: 2,
    }));

    if (loadChart) loadChart.destroy();
    loadChart = new Chart(document.getElementById('loadChart'), {
        type: 'bar',
        data: { labels, datasets },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { labels: { color: '#8b949e', boxWidth: 12, font: { size: 11 } } } },
            scales: {
                x: { stacked: true, ticks: { color: '#8b949e', font: { size: 10 }, maxRotation: 45 }, grid: { color: '#21262d' } },
                y: { stacked: true, ticks: { color: '#8b949e', font: { size: 10 } }, grid: { color: '#21262d' },
                     title: { display: true, text: 'Sekunden', color: '#8b949e', font: { size: 11 } } }
            }
        }
    });
}

// --- Concurrency Chart (Line per System) ---
function renderConcurrencyChart() {
    const buckets = _makeBuckets();
    const bucketMs = _bucketSizeMs();
    const sysCon = {};

    function addEvents(calls, resolverFn) {
        for (const c of calls) {
            const sys = resolverFn(c) || 'Extern';
            if (!c.starttime || !c.endtime) continue;
            const st = new Date(c.starttime).getTime();
            const et = new Date(c.endtime).getTime();
            if (!sysCon[sys]) sysCon[sys] = new Array(buckets.length).fill(0);
            for (let i = 0; i < buckets.length; i++) {
                const bStart = new Date(buckets[i]).getTime();
                const bEnd = bStart + bucketMs;
                if (st < bEnd && et > bStart) sysCon[sys][i]++;
            }
        }
    }
    addEvents(_data.llm_calls, _resolveSystemLLM);
    addEvents(_data.image_calls, _resolveSystemImg);

    const labels = buckets.map(b => _fmtTime(b));
    const sysOrder = Object.keys(sysCon).sort((a,b) => {
        if (a === 'Extern') return 1;
        if (b === 'Extern') return -1;
        return a.localeCompare(b);
    });
    const datasets = sysOrder.map(sys => ({
        label: sys,
        data: sysCon[sys],
        borderColor: COLORS.get(sys),
        backgroundColor: COLORS.get(sys) + '33',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
        borderWidth: 2,
    }));

    if (concChart) concChart.destroy();
    concChart = new Chart(document.getElementById('concChart'), {
        type: 'line',
        data: { labels, datasets },
        options: {
            responsive: true, maintainAspectRatio: false,
            plugins: { legend: { labels: { color: '#8b949e', boxWidth: 12, font: { size: 11 } } } },
            scales: {
                x: { ticks: { color: '#8b949e', font: { size: 10 }, maxRotation: 45 }, grid: { color: '#21262d' } },
                y: { ticks: { color: '#8b949e', font: { size: 10 }, stepSize: 1 }, grid: { color: '#21262d' },
                     title: { display: true, text: 'Tasks', color: '#8b949e', font: { size: 11 } } }
            }
        }
    });
}

// --- Task Type Chart (Horizontal Bar) ---
function renderTaskChart() {
    const taskMap = {};
    for (const c of _data.llm_calls) {
        const t = c.task || 'unknown';
        if (!taskMap[t]) taskMap[t] = { count: 0, totalDur: 0 };
        taskMap[t].count++;
        taskMap[t].totalDur += c.duration_s || 0;
    }
    const sorted = Object.entries(taskMap).sort((a, b) => b[1].totalDur - a[1].totalDur);
    const labels = sorted.map(s => s[0]);
    const durData = sorted.map(s => Math.round(s[1].totalDur * 10) / 10);
    const countData = sorted.map(s => s[1].count);

    if (taskChart) taskChart.destroy();
    taskChart = new Chart(document.getElementById('taskChart'), {
        type: 'bar',
        data: {
            labels,
            datasets: [
                { label: 'Sekunden', data: durData, backgroundColor: '#d2992266', borderColor: '#d29922', borderWidth: 1 },
                { label: 'Anzahl', data: countData, backgroundColor: '#58a6ff44', borderColor: '#58a6ff', borderWidth: 1 },
            ]
        },
        options: {
            responsive: true, maintainAspectRatio: false, indexAxis: 'y',
            plugins: { legend: { labels: { color: '#8b949e', boxWidth: 12, font: { size: 11 } } } },
            scales: {
                x: { ticks: { color: '#8b949e', font: { size: 10 } }, grid: { color: '#21262d' } },
                y: { ticks: { color: '#8b949e', font: { size: 10 } }, grid: { color: '#21262d' } }
            }
        }
    });
}

// --- System Load Breakdown (Horizontal Bar — LLM vs Image per System) ---
function renderProvChart() {
    const sysBreak = {};
    for (const c of _data.llm_calls) {
        const sys = _resolveSystemLLM(c) || 'Extern';
        if (!sysBreak[sys]) sysBreak[sys] = { llmDur: 0, imgDur: 0, llmCount: 0, imgCount: 0 };
        sysBreak[sys].llmDur += c.duration_s || 0;
        sysBreak[sys].llmCount++;
    }
    for (const c of _data.image_calls) {
        const sys = _resolveSystemImg(c) || 'Extern';
        if (!sysBreak[sys]) sysBreak[sys] = { llmDur: 0, imgDur: 0, llmCount: 0, imgCount: 0 };
        sysBreak[sys].imgDur += c.duration_s || 0;
        sysBreak[sys].imgCount++;
    }
    const sorted = Object.entries(sysBreak).sort((a, b) =>
        (b[1].llmDur + b[1].imgDur) - (a[1].llmDur + a[1].imgDur)
    );
    const labels = sorted.map(s => s[0]);

    if (provChart) provChart.destroy();
    provChart = new Chart(document.getElementById('provChart'), {
        type: 'bar',
        data: {
            labels,
            datasets: [
                { label: 'LLM (Sek.)', data: sorted.map(s => Math.round(s[1].llmDur * 10) / 10),
                  backgroundColor: '#58a6ff88', borderColor: '#58a6ff', borderWidth: 1 },
                { label: 'Bilder (Sek.)', data: sorted.map(s => Math.round(s[1].imgDur * 10) / 10),
                  backgroundColor: '#3fb95088', borderColor: '#3fb950', borderWidth: 1 },
            ]
        },
        options: {
            responsive: true, maintainAspectRatio: false, indexAxis: 'y',
            plugins: { legend: { labels: { color: '#8b949e', boxWidth: 12, font: { size: 11 } } },
                tooltip: { callbacks: { afterLabel: (ctx) => {
                    const s = sorted[ctx.dataIndex][1];
                    return `LLM: ${s.llmCount}x | Bilder: ${s.imgCount}x`;
                }}}
            },
            scales: {
                x: { stacked: true, ticks: { color: '#8b949e', font: { size: 10 } }, grid: { color: '#21262d' } },
                y: { stacked: true, ticks: { color: '#8b949e', font: { size: 10 } }, grid: { color: '#21262d' } }
            }
        }
    });
}

// --- Model Stats ---
let _mSortCol = 'total_dur', _mSortAsc = false;

function _calcModelStats() {
    const models = {};
    for (const c of _data.llm_calls) {
        const m = c.model || 'unknown';
        if (!models[m]) models[m] = { count: 0, durations: [], tokens_in: 0, tokens_out: 0 };
        models[m].count++;
        if (c.duration_s > 0) models[m].durations.push(c.duration_s);
        models[m].tokens_in += c.tokens_in || 0;
        models[m].tokens_out += c.tokens_out || 0;
    }
    const result = [];
    for (const [model, s] of Object.entries(models)) {
        const durs = s.durations.sort((a,b) => a - b);
        const total_dur = durs.reduce((a,b) => a+b, 0);
        const avg_dur = durs.length ? total_dur / durs.length : 0;
        const min_dur = durs.length ? durs[0] : 0;
        const max_dur = durs.length ? durs[durs.length - 1] : 0;
        const p90_dur = durs.length ? durs[Math.floor(durs.length * 0.9)] : 0;
        const avg_tok_s = total_dur > 0 ? s.tokens_out / total_dur : 0;
        result.push({ model, count: s.count, total_dur, avg_dur, min_dur, max_dur, p90_dur,
                       tokens_in: s.tokens_in, tokens_out: s.tokens_out, avg_tok_s });
    }
    return result;
}

function renderModelStats() {
    const stats = _calcModelStats();

    // --- Total Duration Bar Chart ---
    const byDur = [...stats].sort((a,b) => b.total_dur - a.total_dur);
    const chartH = Math.max(260, byDur.length * 32);
    const durCanvas = document.getElementById('modelDurChart');
    durCanvas.parentElement.style.height = chartH + 'px';

    if (modelDurChart) modelDurChart.destroy();
    modelDurChart = new Chart(durCanvas, {
        type: 'bar',
        data: {
            labels: byDur.map(s => s.model),
            datasets: [{
                label: 'Gesamtdauer (s)',
                data: byDur.map(s => Math.round(s.total_dur * 10) / 10),
                backgroundColor: byDur.map((s,i) => COLORS.palette[i % COLORS.palette.length] + 'cc'),
                borderColor: byDur.map((s,i) => COLORS.palette[i % COLORS.palette.length]),
                borderWidth: 1,
            }]
        },
        options: {
            responsive: true, maintainAspectRatio: false, indexAxis: 'y',
            plugins: {
                legend: { display: false },
                tooltip: { callbacks: { afterLabel: (ctx) => {
                    const s = byDur[ctx.dataIndex];
                    return `${s.count} Aufrufe | \\u2300 ${s.avg_dur.toFixed(1)}s | P90 ${s.p90_dur.toFixed(1)}s`;
                }}}
            },
            scales: {
                x: { ticks: { color: '#8b949e', font: { size: 10 } }, grid: { color: '#21262d' },
                     title: { display: true, text: 'Sekunden', color: '#8b949e', font: { size: 11 } } },
                y: { ticks: { color: '#c9d1d9', font: { size: 11 } }, grid: { color: '#21262d' } }
            }
        }
    });

    // --- Avg Duration Bar Chart ---
    const byAvg = [...stats].filter(s => s.avg_dur > 0).sort((a,b) => b.avg_dur - a.avg_dur);
    const avgH = Math.max(260, byAvg.length * 32);
    const avgCanvas = document.getElementById('modelAvgChart');
    avgCanvas.parentElement.style.height = avgH + 'px';

    if (modelAvgChart) modelAvgChart.destroy();
    modelAvgChart = new Chart(avgCanvas, {
        type: 'bar',
        data: {
            labels: byAvg.map(s => s.model),
            datasets: [
                { label: '\\u2300 Dauer (s)', data: byAvg.map(s => Math.round(s.avg_dur * 10) / 10),
                  backgroundColor: '#d2992288', borderColor: '#d29922', borderWidth: 1 },
                { label: 'P90 (s)', data: byAvg.map(s => Math.round(s.p90_dur * 10) / 10),
                  backgroundColor: '#f8514988', borderColor: '#f85149', borderWidth: 1 },
            ]
        },
        options: {
            responsive: true, maintainAspectRatio: false, indexAxis: 'y',
            plugins: { legend: { labels: { color: '#8b949e', boxWidth: 12, font: { size: 11 } } } },
            scales: {
                x: { ticks: { color: '#8b949e', font: { size: 10 } }, grid: { color: '#21262d' },
                     title: { display: true, text: 'Sekunden', color: '#8b949e', font: { size: 11 } } },
                y: { ticks: { color: '#c9d1d9', font: { size: 11 } }, grid: { color: '#21262d' } }
            }
        }
    });

    // --- Tokens/s Chart ---
    const byTok = [...stats].filter(s => s.avg_tok_s > 0).sort((a,b) => b.avg_tok_s - a.avg_tok_s);
    const tokH = Math.max(260, byTok.length * 32);
    const tokCanvas = document.getElementById('modelTokChart');
    tokCanvas.parentElement.style.height = tokH + 'px';

    if (modelTokChart) modelTokChart.destroy();
    modelTokChart = new Chart(tokCanvas, {
        type: 'bar',
        data: {
            labels: byTok.map(s => s.model),
            datasets: [{
                label: 'Tokens/s Output',
                data: byTok.map(s => Math.round(s.avg_tok_s * 10) / 10),
                backgroundColor: '#3fb95088',
                borderColor: '#3fb950',
                borderWidth: 1,
            }]
        },
        options: {
            responsive: true, maintainAspectRatio: false, indexAxis: 'y',
            plugins: { legend: { display: false } },
            scales: {
                x: { ticks: { color: '#8b949e', font: { size: 10 } }, grid: { color: '#21262d' },
                     title: { display: true, text: 'Tok/s', color: '#8b949e', font: { size: 11 } } },
                y: { ticks: { color: '#c9d1d9', font: { size: 11 } }, grid: { color: '#21262d' } }
            }
        }
    });

    // --- Stats Table ---
    stats.sort((a,b) => {
        let va = a[_mSortCol], vb = b[_mSortCol];
        if (typeof va === 'string') return _mSortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
        return _mSortAsc ? va - vb : vb - va;
    });

    const maxDur = Math.max(...stats.map(s => s.total_dur), 1);
    document.getElementById('modelStatsBody').innerHTML = stats.map(s => {
        const barW = Math.max(2, Math.round((s.total_dur / maxDur) * 100));
        return `<tr>
            <td style="font-weight:600;color:#e6edf3">${_esc(s.model)}</td>
            <td>${s.count}</td>
            <td>${s.total_dur.toFixed(1)} <span class="dur-bar dur-bar-llm" style="width:${barW}px"></span></td>
            <td>${s.avg_dur.toFixed(1)}</td>
            <td>${s.min_dur.toFixed(1)}</td>
            <td>${s.max_dur.toFixed(1)}</td>
            <td>${s.p90_dur.toFixed(1)}</td>
            <td>${_fmtNum(s.tokens_in)}</td>
            <td>${_fmtNum(s.tokens_out)}</td>
            <td>${s.avg_tok_s.toFixed(1)}</td>
        </tr>`;
    }).join('');
}

// Model stats table sorting
document.querySelectorAll('#modelStatsTable th[data-mcol]').forEach(th => {
    th.addEventListener('click', () => {
        const col = th.dataset.mcol;
        if (_mSortCol === col) { _mSortAsc = !_mSortAsc; }
        else { _mSortCol = col; _mSortAsc = col === 'model'; }
        renderModelStats();
    });
});

// --- Detail Table ---
function renderTable() {
    const all = [];
    for (const c of _data.llm_calls) {
        const sys = _resolveSystemLLM(c);
        all.push({
            starttime: c.starttime, type: 'LLM', system: sys || c.provider || '',
            provider: c.provider || '', model: c.model || '', task: c.task || '',
            service: c.service || '', duration_s: c.duration_s || 0,
            tokens: c.tokens_in + c.tokens_out,
        });
    }
    for (const c of _data.image_calls) {
        const sys = _resolveSystemImg(c);
        all.push({
            starttime: c.starttime, type: 'Image', system: sys || c.backend || '',
            provider: c.backend || '', model: c.model || c.backend_type || '',
            task: 'image_generation', service: c.service || '',
            duration_s: c.duration_s || 0, tokens: 0,
        });
    }

    // Sort
    all.sort((a, b) => {
        let va = a[_sortCol], vb = b[_sortCol];
        if (typeof va === 'number') return _sortAsc ? va - vb : vb - va;
        va = va || ''; vb = vb || '';
        return _sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
    });

    const maxDur = Math.max(...all.map(r => r.duration_s), 1);
    const tbody = document.getElementById('detailBody');
    const rows = all.slice(0, 200);
    tbody.innerHTML = rows.map(r => {
        const badge = r.type === 'LLM' ? 'badge-llm' : 'badge-img';
        const barCls = r.type === 'LLM' ? 'dur-bar-llm' : 'dur-bar-img';
        const barW = Math.max(2, Math.round((r.duration_s / maxDur) * 80));
        const time = r.starttime ? r.starttime.replace('T', ' ').slice(5, 16) : '';
        const sysColor = COLORS.get(r.system);
        const sysLabel = r.system || '—';
        return `<tr>
            <td>${_esc(time)}</td>
            <td><span class="badge ${badge}">${_esc(r.type)}</span></td>
            <td style="color:${sysColor};font-weight:600">${_esc(sysLabel)}</td>
            <td>${_esc(r.model)}</td>
            <td><span class="badge badge-task">${_esc(r.task)}</span></td>
            <td>${_esc(r.service)}</td>
            <td>${r.duration_s.toFixed(1)}s <span class="dur-bar ${barCls}" style="width:${barW}px"></span></td>
            <td>${r.tokens ? _fmtNum(r.tokens) : ''}</td>
        </tr>`;
    }).join('');

    if (all.length > 200) {
        tbody.innerHTML += `<tr><td colspan="8" style="text-align:center;color:#8b949e;padding:12px;">... ${all.length - 200} weitere Eintraege</td></tr>`;
    }
}

function _esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

// --- Bucket helpers ---
function _makeBuckets() {
    if (!_data || (!_data.llm_calls.length && !_data.image_calls.length)) return [];
    const range = _data.time_range;
    let start, end;
    if (range.start && range.end) {
        start = new Date(range.start);
        end = new Date(range.end);
    } else {
        end = new Date();
        start = new Date(end.getTime() - _hours * 3600000);
    }
    const ms = _bucketSizeMs();
    // Align start to bucket boundary
    start = new Date(Math.floor(start.getTime() / ms) * ms);
    const buckets = [];
    for (let t = start.getTime(); t <= end.getTime() + ms; t += ms) {
        buckets.push(new Date(t).toISOString().slice(0, 19));
    }
    return buckets;
}

function _bucketSizeMs() {
    if (_hours <= 1) return 5 * 60000;        // 5 min
    if (_hours <= 6) return 15 * 60000;       // 15 min
    if (_hours <= 24) return 60 * 60000;      // 1 hour
    if (_hours <= 168) return 4 * 60 * 60000; // 4 hours
    return 24 * 60 * 60000;                   // 1 day
}

function _bucketIndex(buckets, timeStr) {
    if (!timeStr || !buckets.length) return -1;
    for (let i = buckets.length - 1; i >= 0; i--) {
        if (timeStr >= buckets[i]) return i;
    }
    return 0;
}

function _fmtTime(isoStr) {
    if (!isoStr) return '';
    // "2026-02-27T14:00:00" -> "27. 14:00"  or "14:00" depending on range
    const parts = isoStr.split('T');
    const date = parts[0] || '';
    const time = (parts[1] || '').slice(0, 5);
    if (_hours <= 24) return time;
    return date.slice(8, 10) + '. ' + time;
}

// --- Event handlers ---
document.querySelectorAll('.time-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.time-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const h = parseInt(btn.dataset.hours);
        loadData(h);
        loadActivity(h);
    });
});

document.querySelectorAll('.detail-table th[data-col]').forEach(th => {
    th.addEventListener('click', () => {
        const col = th.dataset.col;
        if (_sortCol === col) { _sortAsc = !_sortAsc; }
        else { _sortCol = col; _sortAsc = true; }
        renderTable();
    });
});

// --- Activity Feed ---
const ACT_ICONS = {
    instagram_post: '📸', instagram_reaction: '💬',
    thought: '🧠',
    story_arc: '📚', story_beat: '🎬', story_resolved: '🏁',
};
const ACT_LABELS = {
    instagram_post: 'Instagram', instagram_reaction: 'Reaktion',
    thought: 'Gedanke',
    story_arc: 'Story Arc', story_beat: 'Story Beat', story_resolved: 'Arc Ende',
};
let _activityData = [];
let _activityFilter = 'all';

async function loadActivity(hours) {
    try {
        const resp = await fetch(`/dashboard/activity?hours=${hours}`);
        const data = await resp.json();
        _activityData = data.events || [];
        renderActivityStats();
        renderActivityFeed();
    } catch (e) {
        document.getElementById('activityFeed').innerHTML = '<div class="activity-empty">Error loading</div>';
    }
}

function renderActivityStats() {
    const counts = {};
    for (const e of _activityData) {
        counts[e.type] = (counts[e.type] || 0) + 1;
    }
    let html = `<span class="activity-stat act-all ${_activityFilter==='all'?'active':''}" onclick="setActivityFilter('all')">Alle (${_activityData.length})</span>`;
    // Definierte Reihenfolge
    const order = ['instagram_post','instagram_reaction','thought','story_arc','story_beat','story_resolved'];
    for (const t of order) {
        if (!counts[t]) continue;
        const cls = t.startsWith('story') ? 'act-story_arc' : `act-${t}`;
        html += `<span class="activity-stat ${cls} ${_activityFilter===t?'active':''}" onclick="setActivityFilter('${t}')">${ACT_LABELS[t] || t} (${counts[t]})</span>`;
    }
    document.getElementById('activityStats').innerHTML = html;
}

function setActivityFilter(f) {
    _activityFilter = f;
    renderActivityStats();
    renderActivityFeed();
}

function renderActivityFeed() {
    const container = document.getElementById('activityFeed');
    let items = _activityData;
    if (_activityFilter !== 'all') {
        items = items.filter(e => e.type === _activityFilter);
    }
    if (!items.length) {
        container.innerHTML = '<div class="activity-empty">No activity in this period</div>';
        return;
    }
    container.innerHTML = items.slice(0, 100).map(e => {
        const icon = ACT_ICONS[e.type] || '\u2022';
        const time = e.timestamp ? e.timestamp.replace('T', ' ').slice(5, 16) : '';
        const imgUrl = e.meta && e.meta.image_url ? e.meta.image_url : '';
        return `<div class="activity-item">
            <span class="activity-icon">${icon}</span>
            <div class="activity-body">
                <div class="activity-header">
                    <span class="activity-char">${_esc(e.character || '')}</span>
                    <span class="activity-time">${_esc(time)}</span>
                </div>
                <div class="activity-text">${_esc(e.summary || '')}</div>
                ${e.detail ? `<div class="activity-detail">${_esc(e.detail)}</div>` : ''}
                ${imgUrl ? `<img class="activity-beat-img" src="${imgUrl}" alt="Beat Bild" onclick="this.classList.toggle('expanded')">` : ''}
            </div>
        </div>`;
    }).join('');
}

// --- Init ---
loadData(24);
loadActivity(24);
</script>
</body>
</html>'''
