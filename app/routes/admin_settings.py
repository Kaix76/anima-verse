"""Admin Settings Routes — JSON-based configuration management."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from typing import Any, Dict
import httpx

from app.core.log import get_logger
from app.core import config
from app.core.config_schema import get_schema
from app.core.auth_dependency import require_admin

logger = get_logger("admin_settings")

router = APIRouter(prefix="/admin", tags=["admin-settings"],
                   dependencies=[Depends(require_admin)])


# ── API Endpoints ──

@router.get("/settings", response_class=HTMLResponse)
async def settings_page():
    """Serve the admin settings HTML page."""
    return HTMLResponse(
        content=_build_settings_html(),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@router.get("/world-name")
async def world_name(user=Depends(require_admin)):
    """Return the active world name (= storage dir basename) so the admin
    UI can display which world it's actually configuring. Prevents the
    "I just saved Hotopia data into anima-dome" footgun where a stale
    browser tab carries form state across world boundaries.
    """
    from app.core.paths import get_storage_dir
    return {"world": get_storage_dir().name}


@router.get("/users", response_class=HTMLResponse)
async def users_page():
    """Serve the user-management HTML page."""
    return HTMLResponse(
        content=_build_users_html(),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/outfit-rules", response_class=HTMLResponse)
async def outfit_rules_page():
    """Serve the outfit-rules admin HTML page."""
    return HTMLResponse(content=_build_outfit_rules_html())


@router.get("/llm-stats", response_class=HTMLResponse)
async def llm_stats_page():
    """Serve the LLM-Stats admin HTML page (read-only Auswertung)."""
    return HTMLResponse(
        content=_build_llm_stats_html(),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/llm-stats/data")
async def llm_stats_data(
    request: Request,
    user=Depends(require_admin)):
    """Aggregierte LLM-Call-Statistik fuer den Admin-Stats-Tab.

    Query-Parameter:
        from, to     : ISO-Timestamps (inklusiv). Default: letzte 24h.
        agents       : komma-separierte Liste von agent_name. Default: alle.
        group_by_agent : "1" = pro (task, model, provider, agent),
                         sonst (default) = pro (task, model, provider).
        task         : optionaler Task-Filter (Substring-Match).

    Response:
        {
            "from": iso, "to": iso,
            "agents": [alle distinct agent_names im Zeitraum],
            "rows": [{
                task, model, provider, agent_name,
                calls, avg_duration, min_duration, max_duration, p90_duration,
                avg_in_tokens, avg_out_tokens, avg_total_tokens,
                max_in_tokens, max_total_tokens, avg_max_tokens
            }, ...]
        }
    """
    from datetime import datetime, timedelta
    from app.core.db import get_connection

    qp = request.query_params
    to_str = qp.get("to") or ""
    from_str = qp.get("from") or ""
    if not to_str:
        to_str = datetime.now().isoformat(timespec="seconds")
    if not from_str:
        # Default-Fenster: letzte 24h
        try:
            to_dt = datetime.fromisoformat(to_str)
        except Exception:
            to_dt = datetime.now()
        from_str = (to_dt - timedelta(hours=24)).isoformat(timespec="seconds")

    agents_raw = (qp.get("agents") or "").strip()
    selected_agents = [a.strip() for a in agents_raw.split(",") if a.strip()] if agents_raw else []
    group_by_agent = qp.get("group_by_agent") in ("1", "true", "yes", "on")
    task_filter = (qp.get("task") or "").strip().lower()

    conn = get_connection()

    where = ["ts >= ?", "ts <= ?"]
    params: list = [from_str, to_str]
    if selected_agents:
        placeholders = ",".join(["?"] * len(selected_agents))
        where.append(f"agent_name IN ({placeholders})")
        params.extend(selected_agents)

    sql = (
        "SELECT task, model, provider, agent_name, "
        "       in_tokens, out_tokens, max_tokens, duration_s "
        "FROM llm_call_stats "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY ts DESC "
        "LIMIT 100000"
    )
    cur = conn.execute(sql, params)
    raw_rows = cur.fetchall()

    # Distinct agents im Zeitraum (fuer Filter-Dropdown). Wir lesen das
    # immer ungefiltert, damit der User auch nach Filter-Wechsel die
    # vollstaendige Liste sieht.
    agents_sql = (
        "SELECT DISTINCT agent_name FROM llm_call_stats "
        "WHERE ts >= ? AND ts <= ? AND agent_name != '' "
        "ORDER BY agent_name COLLATE NOCASE"
    )
    agents_cur = conn.execute(agents_sql, [from_str, to_str])
    agents_list = [r[0] for r in agents_cur.fetchall()]

    # Aggregation in Python — flexibel + p90 ohne Window-Functions.
    buckets: Dict[tuple, Dict[str, list]] = {}
    for r in raw_rows:
        task, model, provider, agent, in_tok, out_tok, max_tok, dur = r
        if task_filter and task_filter not in (task or "").lower():
            continue
        if group_by_agent:
            key = (task, model, provider or "", agent or "")
        else:
            key = (task, model, provider or "", "")
        b = buckets.setdefault(key, {
            "durations": [], "in_tokens": [], "out_tokens": [],
            "max_tokens": [],
        })
        b["durations"].append(float(dur))
        b["in_tokens"].append(int(in_tok))
        b["out_tokens"].append(int(out_tok))
        if max_tok:
            b["max_tokens"].append(int(max_tok))

    def _p90(vals: list) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        idx = max(0, int(len(s) * 0.9) - 1)
        return s[min(idx, len(s) - 1)]

    rows_out = []
    for (task, model, provider, agent), b in buckets.items():
        durs = b["durations"]
        ins = b["in_tokens"]
        outs = b["out_tokens"]
        mxs = b["max_tokens"]
        n = len(durs)
        totals = [a + bb for a, bb in zip(ins, outs)]
        rows_out.append({
            "task": task,
            "model": model,
            "provider": provider,
            "agent_name": agent,
            "calls": n,
            "avg_duration": round(sum(durs) / n, 2) if n else 0.0,
            "min_duration": round(min(durs), 2) if n else 0.0,
            "max_duration": round(max(durs), 2) if n else 0.0,
            "p90_duration": round(_p90(durs), 2),
            "avg_in_tokens": int(sum(ins) / n) if n else 0,
            "avg_out_tokens": int(sum(outs) / n) if n else 0,
            "avg_total_tokens": int(sum(totals) / n) if n else 0,
            "max_in_tokens": max(ins) if ins else 0,
            "max_total_tokens": max(totals) if totals else 0,
            "avg_max_tokens": int(sum(mxs) / len(mxs)) if mxs else 0,
        })

    rows_out.sort(key=lambda x: (-x["calls"], x["task"], x["model"]))

    return {
        "from": from_str,
        "to": to_str,
        "agents": agents_list,
        "group_by_agent": group_by_agent,
        "rows": rows_out,
    }


@router.get("/outfit-rules/data")
async def outfit_rules_get(user=Depends(require_admin)):
    """Liefert shared/config/outfit_rules.json + Liste gueltiger Slots."""
    import json as _json
    from app.models.inventory import VALID_PIECE_SLOTS
    from app.core.paths import get_config_dir
    path = get_config_dir() / "outfit_rules.json"
    data = {"outfit_types": {}}
    if path.exists():
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "outfit_types": data.get("outfit_types") or {},
        "valid_slots": list(VALID_PIECE_SLOTS),
    }


@router.put("/outfit-rules/data")
async def outfit_rules_save(request: Request, user=Depends(require_admin)):
    """Speichert outfit_rules.json und invalidiert den Rules-Cache.

    Schema pro Eintrag: ``required`` (slot-Liste), ``description`` (string),
    ``default`` (bool, max EIN Eintrag darf default=true sein — wenn keine
    Quelle einen Type liefert, wird dieser als Fallback genutzt).
    """
    import json as _json
    from app.models.inventory import VALID_PIECE_SLOTS
    from app.core.paths import get_config_dir
    from app.core.outfit_rules import reload_rules

    body = await request.json()
    incoming = body.get("outfit_types") or {}
    valid = set(VALID_PIECE_SLOTS)

    cleaned: Dict[str, Dict[str, Any]] = {}
    default_seen = False
    for otype, entry in incoming.items():
        key = (otype or "").strip()
        if not key:
            continue
        if not isinstance(entry, dict):
            continue
        req = entry.get("required") or []
        if not isinstance(req, list):
            continue
        entry_out: Dict[str, Any] = {"required": [s for s in req if s in valid]}
        description = (entry.get("description") or "").strip()
        if description:
            entry_out["description"] = description
        # Default-Flag — nur eines darf gesetzt sein. Erstes wins, alle
        # weiteren werden auf False gezwungen.
        if entry.get("default") and not default_seen:
            entry_out["default"] = True
            default_seen = True
        cleaned[key] = entry_out

    path = get_config_dir() / "outfit_rules.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"outfit_types": cleaned}
    path.write_text(_json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    reload_rules()
    return {"status": "ok", "outfit_types": cleaned}


@router.post("/outfit-rules/rename")
async def outfit_rules_rename(request: Request, user=Depends(require_admin)):
    """Benennt einen outfit_type um (oder merged ihn in einen existierenden).

    Body: {"old": "<alter Name>", "new": "<neuer Name>"}.
    Wenn der neue Name bereits in den Regeln existiert, wird der Vorgang
    zum Merge: alle Referenzen werden umgeschrieben, der alte Eintrag
    wird geloescht (die Slots/Description des Ziels bleiben erhalten).

    Updated wird:
      - shared/config/outfit_rules.json (Key umbenennen / loeschen)
      - world.db locations.outfit_type + locations.meta (rooms[].outfit_type)
      - world.db rooms.outfit_type + rooms.meta
      - world.db items.pieces (outfit_piece.outfit_types)
      - world.db items.meta (outfit_piece.outfit_types — Legacy)
      - world.db activities.meta (outfit_type)
      - world.db characters.profile_json (outfit_exceptions keys)
    """
    import json as _json
    from app.core.paths import get_config_dir
    from app.core.outfit_rules import reload_rules
    from app.core.db import transaction

    body = await request.json()
    old_name = (body.get("old") or "").strip()
    new_name = (body.get("new") or "").strip()
    if not old_name or not new_name:
        raise HTTPException(status_code=400, detail="old + new required")
    if old_name == new_name:
        return {"status": "noop", "merged": False, "updated": {}}

    path = get_config_dir() / "outfit_rules.json"
    data = {"outfit_types": {}}
    if path.exists():
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    types = data.get("outfit_types") or {}
    if old_name not in types:
        raise HTTPException(status_code=404, detail=f"outfit_type '{old_name}' nicht gefunden")

    is_merge = new_name in types
    if not is_merge:
        # Reines Rename: Ziel-Eintrag = alter Eintrag
        types[new_name] = types[old_name]
    # Bei Merge: Ziel-Eintrag bleibt unveraendert, alter Eintrag faellt weg.
    types.pop(old_name, None)
    data["outfit_types"] = types
    path.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    reload_rules()

    updated = {"locations": 0, "rooms": 0, "items": 0,
               "activities": 0, "character_exceptions": 0}

    def _replace_in_list(lst):
        out, changed = [], False
        for v in lst or []:
            if isinstance(v, str) and v.strip().lower() == old_name.lower():
                if new_name not in out:
                    out.append(new_name)
                changed = True
            else:
                if v not in out:
                    out.append(v)
        return out, changed

    with transaction() as conn:
        # locations
        rows = conn.execute("SELECT id, outfit_type, meta FROM locations").fetchall()
        for lid, ot, meta_str in rows:
            new_ot = ot
            if (ot or "").strip().lower() == old_name.lower():
                new_ot = new_name
            try:
                meta = _json.loads(meta_str or "{}")
            except Exception:
                meta = {}
            mchanged = False
            if (meta.get("outfit_type") or "").strip().lower() == old_name.lower():
                meta["outfit_type"] = new_name
                mchanged = True
            for r in (meta.get("rooms") or []):
                if (r.get("outfit_type") or "").strip().lower() == old_name.lower():
                    r["outfit_type"] = new_name
                    mchanged = True
            if new_ot != ot or mchanged:
                conn.execute("UPDATE locations SET outfit_type=?, meta=? WHERE id=?",
                             (new_ot, _json.dumps(meta, ensure_ascii=False), lid))
                updated["locations"] += 1

        # rooms
        rows = conn.execute("SELECT id, outfit_type, meta FROM rooms").fetchall()
        for rid, ot, meta_str in rows:
            new_ot = ot
            if (ot or "").strip().lower() == old_name.lower():
                new_ot = new_name
            try:
                meta = _json.loads(meta_str or "{}")
            except Exception:
                meta = {}
            mchanged = False
            if (meta.get("outfit_type") or "").strip().lower() == old_name.lower():
                meta["outfit_type"] = new_name
                mchanged = True
            if new_ot != ot or mchanged:
                conn.execute("UPDATE rooms SET outfit_type=?, meta=? WHERE id=?",
                             (new_ot, _json.dumps(meta, ensure_ascii=False), rid))
                updated["rooms"] += 1

        # items.pieces (outfit_piece.outfit_types[])
        rows = conn.execute("SELECT id, pieces, meta FROM items").fetchall()
        for iid, pieces_str, meta_str in rows:
            try:
                pieces = _json.loads(pieces_str or "{}")
            except Exception:
                pieces = {}
            try:
                meta = _json.loads(meta_str or "{}")
            except Exception:
                meta = {}
            ichanged = False
            new_types, ch = _replace_in_list(pieces.get("outfit_types"))
            if ch:
                pieces["outfit_types"] = new_types
                ichanged = True
            mop = meta.get("outfit_piece") or {}
            new_mtypes, mch = _replace_in_list(mop.get("outfit_types"))
            if mch:
                mop["outfit_types"] = new_mtypes
                meta["outfit_piece"] = mop
                ichanged = True
            if ichanged:
                conn.execute("UPDATE items SET pieces=?, meta=? WHERE id=?",
                             (_json.dumps(pieces, ensure_ascii=False),
                              _json.dumps(meta, ensure_ascii=False), iid))
                updated["items"] += 1

        # activities.meta.outfit_type
        rows = conn.execute("SELECT id, meta FROM activities").fetchall()
        for aid, meta_str in rows:
            try:
                meta = _json.loads(meta_str or "{}")
            except Exception:
                meta = {}
            if (meta.get("outfit_type") or "").strip().lower() == old_name.lower():
                meta["outfit_type"] = new_name
                conn.execute("UPDATE activities SET meta=? WHERE id=?",
                             (_json.dumps(meta, ensure_ascii=False), aid))
                updated["activities"] += 1

        # characters.profile_json.outfit_exceptions (keys)
        rows = conn.execute("SELECT name, profile_json FROM characters").fetchall()
        for cname, prof_str in rows:
            try:
                prof = _json.loads(prof_str or "{}")
            except Exception:
                prof = {}
            exc = prof.get("outfit_exceptions") or {}
            if not isinstance(exc, dict):
                continue
            target_key = None
            for k in list(exc.keys()):
                if isinstance(k, str) and k.strip().lower() == old_name.lower():
                    target_key = k
                    break
            if target_key:
                # Bei Merge: Eintrag mit gleichem neuen Key hat Vorrang —
                # alten Eintrag verwerfen wenn schon einer da ist.
                if new_name not in exc:
                    exc[new_name] = exc[target_key]
                exc.pop(target_key, None)
                prof["outfit_exceptions"] = exc
                conn.execute("UPDATE characters SET profile_json=? WHERE name=?",
                             (_json.dumps(prof, ensure_ascii=False), cname))
                updated["character_exceptions"] += 1

    logger.info("outfit_rules rename: '%s' -> '%s' (merge=%s) — updated=%s",
                old_name, new_name, is_merge, updated)
    return {"status": "ok", "merged": is_merge, "updated": updated}


# --- Prompt-Filters ----------------------------------------------------

# Block-keys die in einem Filter unter drop_blocks aufgefuehrt werden duerfen.
# Synchron mit shared/templates/llm/chat/agent_thought.md +
# app/core/thought_context.py (alle ctx-Keys die *_block heissen).
_PROMPT_FILTER_BLOCK_KEYS = [
    "inbox_block", "events_block", "assignments_block", "general_task",
    "commitments_block", "outfit_decision_block", "arc_block",
    "retrospective_block", "instagram_pending_block", "effects_block",
    "recent_chat_block", "outfit_self_block", "outfit_avatar_block",
    "room_items_block", "inventory_block", "present_people_block",
    "known_locations_block", "travel_block", "available_activities_block",
    "daily_schedule_block",
]


@router.get("/prompt-filters/data")
async def prompt_filters_data(user=Depends(require_admin)):
    """Liste der gemergten Prompt-Filter (shared baseline + world overlay).

    Jeder Eintrag bekommt ein ``source``-Feld: "shared" / "world".
    Wenn dieselbe id in beiden vorkommt, gewinnt world (overlay) und
    source="world override".
    """
    from app.core.prompt_filters import _load_shared, _load_world

    shared = {(e.get("id") or "").strip(): e
              for e in _load_shared() if e.get("id")}
    world = {(e.get("id") or "").strip(): e
             for e in _load_world() if e.get("id")}

    out = []
    seen_ids = set()
    for fid, e in shared.items():
        if fid in world:
            entry = dict(world[fid])
            entry["source"] = "world override"
        else:
            entry = dict(e)
            entry["source"] = "shared"
        seen_ids.add(fid)
        out.append(entry)
    for fid, e in world.items():
        if fid in seen_ids:
            continue
        entry = dict(e)
        entry["source"] = "world"
        out.append(entry)

    return {
        "filters": out,
        "block_keys": _PROMPT_FILTER_BLOCK_KEYS,
        "condition_hint": (
            "Filter-id triggert IMMER wenn als Tag im Profil aktiv (apply_condition). "
            "Diese Expression triggert ZUSAETZLICH:\n"
            "Status: stamina>N, courage<N, stress>N, lust>N\n"
            "Zeit/Anwesenheit: alone, night, day\n"
            "Beziehung: relationship:Name>N, romantic:Name>N (Name oder 'any')\n"
            "Stimmung: mood:happy\n"
            "Anderer Zustand: condition:<tag>\n"
            "Aktuelle Aktivitaet: current_activity:kochen\n"
            "Tagesablauf: schedule:sleeping, schedule:awake, schedule:<activity>\n"
            "Item: has_item:item_a1b2c3d4\n"
            "Verknuepfung: AND / OR / NOT"
        ),
    }


@router.post("/prompt-filters/save")
async def prompt_filters_save(request: Request, user=Depends(require_admin)):
    """Upsert eines Filters in die per-world prompt_filters-Tabelle.

    Body: {id, condition, label, drop_blocks: [...], prompt_modifier,
           icon, image_modifier, enabled}.
    Wenn die id auch in shared/prompt_filters/filters.json existiert, ist das
    ein Override. Sonst wird ein neuer world-only Filter angelegt.
    """
    import json as _json
    from app.core.db import transaction

    body = await request.json()
    fid = (body.get("id") or "").strip()
    condition = (body.get("condition") or "").strip()
    label = (body.get("label") or "").strip()
    drop_blocks = body.get("drop_blocks") or []
    prompt_modifier = (body.get("prompt_modifier") or "").strip()
    icon = (body.get("icon") or "").strip()
    image_modifier = (body.get("image_modifier") or "").strip()
    enabled = bool(body.get("enabled", True))

    if not fid:
        raise HTTPException(status_code=400, detail="id required")
    # condition ist im neuen Modell optional — Filter-id triggert
    # implizit ueber den Profil-Tag, condition ist nur ein zusaetzlicher
    # Stat-/Composite-Trigger.
    if not isinstance(drop_blocks, list):
        raise HTTPException(status_code=400,
                             detail="drop_blocks must be a list")
    valid = set(_PROMPT_FILTER_BLOCK_KEYS)
    drop_blocks = [b for b in drop_blocks if b in valid]

    with transaction() as conn:
        conn.execute("""
            INSERT INTO prompt_filters (id, condition, label, drop_blocks,
                                        prompt_modifier, enabled, meta,
                                        icon, image_modifier)
            VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                condition=excluded.condition,
                label=excluded.label,
                drop_blocks=excluded.drop_blocks,
                prompt_modifier=excluded.prompt_modifier,
                enabled=excluded.enabled,
                icon=excluded.icon,
                image_modifier=excluded.image_modifier
        """, (fid, condition, label,
              _json.dumps(drop_blocks, ensure_ascii=False),
              prompt_modifier, 1 if enabled else 0,
              icon, image_modifier))
    return {"status": "ok", "id": fid}


@router.delete("/prompt-filters/{filter_id}")
async def prompt_filters_delete(filter_id: str, user=Depends(require_admin)):
    """Entfernt den world-overlay-Eintrag fuer diese id.

    Wenn dieselbe id auch im shared baseline existiert, wird damit der
    Override aufgehoben — der baseline-Filter greift wieder. Wenn die id
    nur in world existierte, ist der Filter danach komplett weg.
    """
    from app.core.db import transaction

    with transaction() as conn:
        conn.execute("DELETE FROM prompt_filters WHERE id=?", (filter_id,))
    return {"status": "ok", "id": filter_id}


@router.get("/settings/data")
async def settings_data(user=Depends(require_admin)):
    """Return full config with sensitive fields masked.

    Leere Felder werden mit Schema-Defaults vorbelegt, damit der User
    sofort sieht welcher Fallback-Wert greift.
    """
    import copy
    data = copy.deepcopy(config.get_all())
    _apply_schema_defaults(data)
    return config.mask_sensitive(data)


@router.get("/settings/raw")
async def settings_raw(user=Depends(require_admin)):
    """Return full config without masking (for save round-trip).

    Empty fields are pre-filled with their schema default so the admin UI
    immediately shows what value would apply if left untouched.
    """
    import copy
    data = copy.deepcopy(config.get_all())
    _apply_schema_defaults(data)
    return data


@router.post("/settings/save")
async def settings_save(request: Request, user=Depends(require_admin)):
    """Save config. Fields with masked values (***...) are kept from current config."""
    new_data = await request.json()

    # Merge: keep current values for masked sensitive fields
    current = config.get_all()
    merged = _merge_sensitive(new_data, current)
    # Schutz fuer Felder in sub_array/is_dict-Items (z.B. comfyui_workflows),
    # die der Frontend bei undefined-CONFIG-Werten beim Save weglaesst.
    _preserve_unsent_subarray_fields(merged, current)

    # Structural validation (e.g. llm_routing order uniqueness)
    err = _validate_llm_routing(merged.get("llm_routing"))
    if err:
        raise HTTPException(status_code=400, detail=err)

    # Diagnose: was kommt im llm_routing wirklich an?
    try:
        _routing_in = merged.get("llm_routing") or []
        _task_log = []
        for _e in _routing_in:
            if not isinstance(_e, dict):
                continue
            for _t in (_e.get("tasks") or []):
                if isinstance(_t, dict) and _t.get("task"):
                    _task_log.append(
                        f"{_t.get('task')}@{_t.get('order','?')}->"
                        f"{_e.get('provider','?')}/{_e.get('model','?')}")
        from app.core.log import get_logger as _gl
        _gl("admin_settings").info(
            "settings_save: llm_routing %d Eintraege, %d Task-Mappings: %s",
            len(_routing_in), len(_task_log), _task_log)
    except Exception:
        pass

    _autofill_imagegen_defaults(merged)

    config.save(merged)
    # Env sofort aktualisieren — vermeidet Server-Restart-Pflicht fuer Felder
    # die ueber os.environ.get() gelesen werden (z.B. COMFY_MULTISWAP_UNET).
    try:
        config._flatten_to_env(merged)
    except Exception as _ee:
        # Nicht hart fehlschlagen — Save selbst war erfolgreich.
        from app.core.log import get_logger as _gl
        _gl("admin_settings").warning("env-flatten nach Save fehlgeschlagen: %s", _ee)
    return {"status": "success", "message": "Configuration saved (env updated)."}


@router.get("/settings/llm-tasks")
async def settings_llm_tasks(user=Depends(require_admin)):
    """Liefert die Liste bekannter LLM-Task-Typen fuer den Admin-UI-Selector."""
    from app.core.llm_tasks import TASK_TYPES, CATEGORY_LABELS
    return [
        {
            "id": tid,
            "label": t.get("label", tid),
            "category": t.get("category", ""),
            "category_label": CATEGORY_LABELS.get(str(t.get("category", "")), ""),
        }
        for tid, t in TASK_TYPES.items()
    ]


@router.get("/settings/llm-task-state")
async def llm_task_state_get(user=Depends(require_admin)):
    from app.core.llm_task_state import (
        disabled_tasks, runtime_disabled_tasks, get_presets)
    return {
        "disabled": disabled_tasks(),
        "runtime_disabled": runtime_disabled_tasks(),
        "presets": get_presets(),
    }


@router.post("/settings/llm-task-state/runtime-preset")
async def llm_task_state_runtime_preset(request: Request, user=Depends(require_admin)):
    """Aktiviert ein Preset als Runtime-Disable (nicht persistent)."""
    data = await request.json()
    preset = (data.get("preset") or "").strip()
    from app.core.llm_task_state import activate_preset_runtime, clear_runtime
    if not preset or preset == "none":
        clear_runtime()
        return {"status": "cleared"}
    tasks = activate_preset_runtime(preset)
    return {"status": "ok", "preset": preset, "disabled": tasks}


def _autofill_imagegen_defaults(cfg: Dict[str, Any]) -> None:
    """When the user has at least one image-gen backend, fill empty
    outfit/expression/location default-backend fields with the first enabled
    backend. Does not overwrite existing selections."""
    img = cfg.get("image_generation") or {}
    backends = img.get("backends") or []
    if not isinstance(backends, list) or not backends:
        return
    chosen = next(
        (b.get("name") for b in backends
         if isinstance(b, dict) and b.get("enabled") and b.get("name")),
        None,
    ) or next(
        (b.get("name") for b in backends
         if isinstance(b, dict) and b.get("name")),
        None,
    )
    if not chosen:
        return
    target = f"backend:{chosen}"
    for field in ("outfit_imagegen_default",
                  "expression_imagegen_default",
                  "location_imagegen_default"):
        if not img.get(field):
            img[field] = target
    cfg["image_generation"] = img


def _validate_llm_routing(routing) -> str:
    """Prueft llm_routing: pro (task, order) darf es nur einen Eintrag geben.

    Returns leere String wenn OK, sonst Fehlermeldung.
    """
    if not isinstance(routing, list):
        return ""
    seen: dict = {}  # (task, order) -> entry_index
    for idx, entry in enumerate(routing):
        if not isinstance(entry, dict):
            continue
        # Disabled-Eintraege werden zur Laufzeit ignoriert -> auch
        # Order-Konflikte zwischen disabled+enabled sind erlaubt.
        if entry.get("enabled") is False:
            continue
        tasks = entry.get("tasks") or []
        if not isinstance(tasks, list):
            continue
        for t in tasks:
            if not isinstance(t, dict):
                continue
            task_id = t.get("task")
            order = t.get("order")
            if not task_id or order is None:
                continue
            key = (task_id, int(order))
            if key in seen:
                return (f"LLM Routing: task '{task_id}' mit order {order} "
                        f"ist doppelt (Eintrag #{seen[key]+1} und #{idx+1}).")
            seen[key] = idx
    return ""


@router.get("/settings/schema")
async def settings_schema(user=Depends(require_admin)):
    """Return field schema for UI rendering."""
    return get_schema()


@router.get("/settings/imagegen-targets")
async def imagegen_targets(user=Depends(require_admin)):
    """Liefert die kombinierte Liste der Image-Gen-Targets fuer Admin-Selects:
    ComfyUI-Workflows + Cloud-Backends (Together/CivitAI/Mammouth).

    Format: [{"value": "workflow:Z-Image", "label": "...", "type": "workflow", "available": True}, ...]
    """
    try:
        from app.core.dependencies import get_skill_manager
        sm = get_skill_manager()
        img = sm.get_skill("image_generation")
        if not img:
            return {"targets": []}
    except Exception as e:
        return {"targets": [], "error": str(e)}

    out = []
    # ComfyUI-Workflows zuerst (sortiert nach Name)
    for wf in sorted(img.comfy_workflows, key=lambda w: w.name.lower()):
        # Verfuegbarkeit: existiert mind. 1 kompatibles, available, instance_enabled Backend?
        compat = wf.compatible_backends or []
        avail = False
        for b in img.backends:
            if not b.instance_enabled or not b.available:
                continue
            if b.api_type != "comfyui":
                continue
            if compat and b.name not in compat:
                continue
            avail = True
            break
        out.append({
            "value": f"workflow:{wf.name}",
            "label": f"ComfyUI: {wf.name}",
            "type": "workflow",
            "available": avail,
        })
    # Cloud-Backends (non-comfyui)
    for b in img.backends:
        if b.api_type == "comfyui":
            continue
        if not b.instance_enabled:
            continue
        out.append({
            "value": f"backend:{b.name}",
            "label": f"{b.name} ({b.api_type})",
            "type": "backend",
            "available": bool(b.available),
        })
    return {"targets": out}


@router.get("/settings/imagegen-backends/{backend_name}/models")
async def imagegen_backend_models(backend_name: str, user=Depends(require_admin)):
    """Liefert Modellliste fuer ein Image-Generation-Backend (Cloud).

    - Together: holt Live-Liste via /v1/models (image-Modelle filtern)
    - CivitAI/Mammouth: aktuell nur das konfigurierte backend.model
    - ComfyUI: leitet auf comfyui-models um
    """
    img_gen = config.get("image_generation", {}) or {}
    backends = img_gen.get("backends", []) or []
    b = next((x for x in backends if x.get("name") == backend_name), None)
    if not b:
        raise HTTPException(404, f"Backend '{backend_name}' nicht gefunden")
    api_type = (b.get("api_type") or "").lower()
    api_key = b.get("api_key", "")
    api_url = (b.get("api_url") or "").rstrip("/")
    cur_model = b.get("model", "")
    models: list = []
    try:
        if api_type == "together":
            base = api_url if api_url.endswith("/v1") else (api_url + "/v1")
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{base}/models",
                                        headers={"Authorization": f"Bearer {api_key}"})
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                    for m in items:
                        if not isinstance(m, dict):
                            continue
                        if m.get("type") and m.get("type") != "image":
                            continue
                        mid = m.get("id") or m.get("name")
                        if mid:
                            models.append(mid)
            models.sort()
        elif api_type == "civitai":
            # CivitAI hat keine sinnvolle Modell-Liste via API — nur das
            # konfigurierte AIR URN als einzige Option zurueckgeben.
            if cur_model:
                models = [cur_model]
        elif api_type == "mammouth":
            if cur_model:
                models = [cur_model]
        elif api_type == "comfyui":
            # ComfyUI: Modelle (Checkpoints + UNets) aus dem ImageGen-Skill
            # Cache holen — der enthaelt die per-Backend gescannten Modelle.
            try:
                from app.core.dependencies import get_skill_manager
                _sm = get_skill_manager()
                _img = _sm.get_skill("image_generation")
                if _img and getattr(_img, "_model_cache_loaded", False):
                    _ckpt = _img._cached_checkpoints_by_service.get(backend_name, [])
                    _unet = _img._cached_unet_models_by_service.get(backend_name, [])
                    models = sorted(set(_ckpt + _unet))
            except Exception as _e:
                logger.warning("ComfyUI-Models-Cache nicht lesbar: %s", _e)
    except Exception as e:
        return {"backend": backend_name, "models": [], "error": str(e)}
    # cur_model immer dabei haben (auch wenn es nicht in der Liste ist)
    if cur_model and cur_model not in models:
        models.insert(0, cur_model)
    return {"backend": backend_name, "models": models, "current": cur_model}


@router.get("/settings/providers/{provider_name}/models")
async def provider_models(provider_name: str, user=Depends(require_admin)):
    """Fetch available models from a provider (live query)."""
    providers = config.get("providers", [])
    provider = None
    for p in providers:
        if p.get("name") == provider_name:
            provider = p
            break
    if not provider:
        raise HTTPException(404, f"Provider '{provider_name}' not found")

    api_base = provider.get("api_base", "")
    api_key = provider.get("api_key", "not-needed")
    ptype = provider.get("type", "openai")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if ptype == "ollama":
                # Ollama: /api/tags
                base = api_base.rstrip("/v1").rstrip("/")
                resp = await client.get(f"{base}/api/tags")
                resp.raise_for_status()
                data = resp.json()
                models = [m.get("name", "") for m in data.get("models", [])]
            else:
                # OpenAI-compatible: /v1/models
                headers = {}
                if api_key and api_key != "not-needed":
                    headers["Authorization"] = f"Bearer {api_key}"
                base = api_base.rstrip("/")
                if not base.endswith("/v1"):
                    base += "/v1"
                resp = await client.get(f"{base}/models", headers=headers)
                resp.raise_for_status()
                data = resp.json()
                # OpenAI: {"data": [{id, ...}]}, Together.ai: [{id, ...}]
                items = data.get("data", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
                models = []
                for m in items:
                    if isinstance(m, dict):
                        mid = m.get("id") or m.get("name") or ""
                        if mid:
                            models.append(mid)
                    elif isinstance(m, str):
                        models.append(m)

        models.sort()
        return {"provider": provider_name, "models": models}
    except Exception as e:
        return {"provider": provider_name, "models": [], "error": str(e)}


@router.post("/settings/validate")
async def settings_validate(request: Request, user=Depends(require_admin)):
    """Validate config and return list of issues."""
    from app.core.config_validator import validate_config
    data = await request.json()
    issues = validate_config(data)
    return {"issues": issues, "errors": sum(1 for i in issues if i["level"] == "error"), "warnings": sum(1 for i in issues if i["level"] == "warning")}


@router.post("/settings/restart")
async def settings_restart(user=Depends(require_admin)):
    """Trigger a skill/service reload (same as existing reload endpoint)."""
    from app.core.dependencies import reload_skill_manager
    result = reload_skill_manager()
    return {"status": "success", "result": result}


@router.post("/settings/memory-consolidate")
async def settings_memory_consolidate(request: Request, user=Depends(require_admin)):
    """Triggert Memory-Konsolidierung sofort.

    Body (alles optional):
      - character: Wenn gesetzt, NUR fuer diesen Character. Sonst: alle.
      - phase2_iterations: Wieviel mal Phase 2 hintereinander pro Character laufen
        soll (Default 1). Pro Iteration werden bis zu 3 Tage Episodics
        konsolidiert. Hilfreich um grosse Backlogs in einem Rutsch abzubauen.
    """
    body = await request.json() if request.headers.get('content-type','').startswith('application/json') else {}
    character = (body.get('character') or '').strip()
    iterations = max(1, min(20, int(body.get('phase2_iterations', 1))))

    from app.core.background_queue import get_background_queue
    from app.models.character import list_available_characters

    targets = [character] if character else list_available_characters()
    bq = get_background_queue()
    submitted = 0
    for ch in targets:
        for _ in range(iterations):
            bq.submit(
                task_type="memory_consolidation",
                payload={"character_name": ch},
                priority=30,
                agent_name=ch,
                deduplicate=False)  # explizit kein dedup damit alle iter laufen
            submitted += 1
    return {"status": "success", "submitted": submitted, "characters": len(targets), "iterations": iterations}


# ── Agent Loop ────────────────────────────────────────────────────────

@router.get("/agent-loop/status")
async def agent_loop_status(user=Depends(require_admin)):
    """Return AgentLoop status for the admin panel.

    Mirrors ``AgentLoop.status()``: running, paused, current agent,
    remaining round, recent turns. Pause source is the task_queue
    'default' pause flag (DB-persistent across restarts).
    """
    from app.core.agent_loop import get_agent_loop
    return get_agent_loop().status()


@router.post("/agent-loop/pause")
async def agent_loop_pause(user=Depends(require_admin)):
    """Pause the AgentLoop (and the task_queue 'default' it shares).

    The pause is persistent — survives restart because it lives in the
    world DB via ``task_queue._is_paused``.
    """
    from app.core.task_queue import get_task_queue
    tq = get_task_queue()
    if tq:
        tq.pause_queue("default")
    return {"status": "paused"}


@router.post("/agent-loop/resume")
async def agent_loop_resume(user=Depends(require_admin)):
    """Resume the AgentLoop."""
    from app.core.task_queue import get_task_queue
    tq = get_task_queue()
    if tq:
        tq.resume_queue("default")
    return {"status": "running"}


@router.post("/agent-loop/bump")
async def agent_loop_bump(request: Request, user=Depends(require_admin)):
    """Manually bump a character — they think on the next slot.

    Body: {"character": "<name>"}
    Useful for debugging / forcing immediate attention without forced_thoughts.
    """
    body = await request.json()
    name = (body.get("character") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="character required")
    from app.core.agent_loop import get_agent_loop
    ok = get_agent_loop().bump(name)
    return {"status": "queued" if ok else "skipped", "character": name}


@router.get("/agent-loop", response_class=HTMLResponse)
async def agent_loop_page(user=Depends(require_admin)):
    """Minimal HTML panel for the AgentLoop: status + pause toggle + recent turns."""
    from fastapi.responses import HTMLResponse as _HTMLResp
    return _HTMLResp(_AGENT_LOOP_HTML, headers={"Cache-Control": "no-store, no-cache, must-revalidate"})


_AGENT_LOOP_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Agent Loop</title>
<style>
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background:#0d1117; color:#c9d1d9; margin:0; padding:20px; }
h1 { font-size:18px; margin-top:0; }
.bar { display:flex; gap:8px; align-items:center; margin-bottom:16px; padding:10px; background:#161b22; border:1px solid #30363d; border-radius:6px; }
.bar button { background:#238636; color:#fff; border:0; padding:6px 12px; border-radius:4px; cursor:pointer; font-size:13px; }
.bar button.paused { background:#da3633; }
.bar .label { color:#8b949e; font-size:12px; }
.section { margin-bottom:14px; padding:10px; background:#161b22; border:1px solid #30363d; border-radius:6px; }
.section h2 { font-size:13px; margin:0 0 6px; color:#58a6ff; }
.section .data { font-family: ui-monospace, SFMono-Regular, monospace; font-size:12px; color:#c9d1d9; white-space:pre-wrap; }
.recent table { width:100%; font-size:12px; border-collapse:collapse; }
.recent th, .recent td { text-align:left; padding:4px 6px; border-bottom:1px solid #21262d; vertical-align:top; }
.recent th { color:#8b949e; font-weight:500; }
.outcome-ok { color:#3fb950; }
.outcome-timeout { color:#d29922; }
.outcome-skip    { color:#6e7681; }
.outcome-err { color:#f85149; }
.tag { display:inline-block; padding:1px 6px; margin:1px 3px 1px 0; border-radius:3px; font-size:11px; background:#21262d; color:#8b949e; }
.tag.tool { background:#1f3a5f; color:#79c0ff; }
.tag.intent { background:#3a2f5f; color:#d2a8ff; }
.preview { color:#8b949e; font-style:italic; max-width:380px; word-break:break-word; }
.muted { color:#484f58; }
</style>
</head>
<body>
<h1>Agent Loop</h1>
<div class="bar">
  <button id="btn-pause" onclick="togglePause()">Pause</button>
  <span id="status-label" class="label">loading…</span>
</div>

<div class="section">
  <h2>Current</h2>
  <div class="data" id="current">—</div>
</div>

<div class="section">
  <h2>Bump (priority)</h2>
  <div id="bumped" class="data">—</div>
</div>

<div class="section">
  <h2>Round (remaining)</h2>
  <div id="round" class="data">—</div>
</div>

<div class="section recent">
  <h2>Recent turns</h2>
  <table id="recent-table"><thead><tr><th>Agent</th><th>Started</th><th>Dur</th><th>Outcome</th><th>Tools / Intents</th><th>Preview</th></tr></thead><tbody></tbody></table>
</div>

<script>
let _state = null;

let _loadCounter = 0;

async function load() {
  _loadCounter++;
  const lbl = document.getElementById('status-label');
  if (lbl && lbl.textContent === 'loading…') {
    lbl.textContent = 'fetching… (#' + _loadCounter + ')';
  }
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  try {
    const r = await fetch('/admin/agent-loop/status', {
      signal: ctrl.signal,
      cache: 'no-store',
      credentials: 'same-origin',
    });
    clearTimeout(timer);
    if (!r.ok) {
      let body = '';
      try { body = (await r.text()).slice(0, 200); } catch(_) {}
      if (lbl) lbl.textContent = 'HTTP ' + r.status + (body ? ' — ' + body : '');
      return;
    }
    _state = await r.json();
    render();
  } catch(e) {
    clearTimeout(timer);
    if (lbl) lbl.textContent = (e.name === 'AbortError')
      ? "timeout — server didn't respond in 8s (call #" + _loadCounter + ")"
      : "error: " + e.message + " (call #" + _loadCounter + ")";
    console.error('[agent-loop load failed]', e);
  }
}

// Verifiziere dass der Script ueberhaupt laeuft — wenn 'loading…' nach 1s
// nicht ersetzt wurde, gab es einen Pre-Init-Error.
setTimeout(() => {
  const lbl = document.getElementById('status-label');
  if (lbl && lbl.textContent === 'loading…') {
    lbl.textContent = 'JS ran but fetch never started (check console)';
  }
}, 1500);

function render() {
  const s = _state || {};
  const btn = document.getElementById('btn-pause');
  const lbl = document.getElementById('status-label');
  if (s.paused) {
    btn.textContent = 'Resume';
    btn.classList.add('paused');
    lbl.textContent = 'PAUSED — Loop is sleeping. Persistent across restarts.';
  } else if (s.standby) {
    btn.textContent = 'Pause';
    btn.classList.remove('paused');
    lbl.textContent = "STANDBY — no 'thought' LLM reachable. Loop polls every 30s.";
  } else if (s.running) {
    btn.textContent = 'Pause';
    btn.classList.remove('paused');
    lbl.textContent = 'Running.';
  } else {
    btn.textContent = 'Pause';
    btn.classList.remove('paused');
    lbl.textContent = 'Loop not started.';
  }
  document.getElementById('current').textContent = s.current_agent || '(idle)';
  const bumped = s.bumped || [];
  document.getElementById('bumped').textContent = bumped.length ? bumped.join(' → ') : '(none)';
  const round = s.remaining_in_round || [];
  document.getElementById('round').textContent = round.length ? round.join(' → ') : '(round empty — refilling on next pick)';
  const tbody = document.querySelector('#recent-table tbody');
  tbody.innerHTML = '';
  for (const r of (s.recent || []).slice().reverse()) {
    const tr = document.createElement('tr');
    let cls = 'outcome-ok';
    if (r.outcome && r.outcome.startsWith('error')) cls = 'outcome-err';
    else if (r.outcome === 'timeout' || r.outcome === 'no_llm') cls = 'outcome-timeout';
    else if (r.outcome === 'in_chat_skip') cls = 'outcome-skip';
    const tools = (r.tools || []).map(t => `<span class="tag tool">${escapeHtml(t)}</span>`).join('');
    const intents = (r.intents || []).map(i => `<span class="tag intent">${escapeHtml(i)}</span>`).join('');
    const tagsCell = (tools + intents) || '<span class="muted">—</span>';
    // Link zum LLM Log: nur fuer Outcomes wo tatsaechlich ein LLM-Call lief.
    // Auto-Sleep / in_chat_skip / no_llm haben keinen Eintrag im LLM-Log.
    const _llmRanOutcomes = !(
      (r.outcome || '').startsWith('auto_sleep') ||
      r.outcome === 'in_chat_skip' || r.outcome === 'no_llm'
    );
    let logLink = '';
    if (_llmRanOutcomes && r.agent && r.started_at) {
      // Search-Filter: ISO-Format mit "T" + Minute des Turn-Starts (matcht
      // das Roh-Format in llm_calls.jsonl 'starttime'). Beispiel:
      // "2026-05-05T13:35". Der LLM-Log-Viewer liest die URL-Params, wendet
      // Filter an und auto-expanded den ersten Treffer.
      const tsMin = (r.started_at || '').slice(0, 16);
      const url = '/logs/llm?character=' + encodeURIComponent(r.agent)
                + '&search=' + encodeURIComponent(tsMin);
      // Wir versuchen die Admin-Sidebar-Navigation (parent.activateIframe) zu
      // nutzen — dann wird im Admin-Layout NUR der iframe-Inhalt getauscht
      // und Sidebar-Links bleiben erhalten. Fallback: direkte Navigation
      // (z.B. wenn Agent-Loop standalone geoeffnet wurde).
      const onclick = "event.preventDefault();"
        + " try { if (window.parent && window.parent.activateIframe) {"
        + " window.parent.activateIframe('_llm_log', '" + url + "', 'LLM Log'); return; } } catch(e) {}"
        + " window.location = '" + url + "';";
      logLink = ` <a href="${url}" onclick="${onclick}" title="Im LLM-Log oeffnen" style="margin-left:6px;text-decoration:none;color:#58a6ff;">🔍</a>`;
    }
    const preview = r.preview
      ? `<span class="preview">${escapeHtml(r.preview)}</span>${logLink}`
      : (logLink ? `<span class="muted">—</span>${logLink}` : '<span class="muted">—</span>');
    const startedShort = (r.started_at || '').replace('T', ' ').split('.')[0];
    tr.innerHTML = `<td>${escapeHtml(r.agent)}</td><td>${escapeHtml(startedShort)}</td><td>${r.duration_s}s</td><td class="${cls}">${escapeHtml(r.outcome)}</td><td>${tagsCell}</td><td>${preview}</td>`;
    tbody.appendChild(tr);
  }
}

function escapeHtml(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

async function togglePause() {
  const ep = (_state && _state.paused) ? '/admin/agent-loop/resume' : '/admin/agent-loop/pause';
  try { await fetch(ep, { method: 'POST' }); } catch(e) {}
  await load();
}

load();
setInterval(load, 5000);
</script>
</body>
</html>
"""


# ── Scheduler (admin-only background jobs) ────────────────────────────

@router.get("/scheduler", response_class=HTMLResponse)
async def scheduler_page(user=Depends(require_admin)):
    """Admin scheduler view — lists all background jobs and lets the admin
    create new ones for administrative actions only (extract_files, notify).

    Per-character actions (send_message, set_status, execute_tool) are
    intentionally excluded — those moved into the AgentLoop bump+hint
    pattern. Daily-Schedule-Editing happens per character in the Character
    Editor (Tab "Tagesablauf"), not here.
    """
    return """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Scheduler</title>
<style>
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background:#0d1117; color:#c9d1d9; margin:0; padding:20px; }
h1 { font-size:18px; margin-top:0; }
h2 { font-size:13px; margin:0 0 8px; color:#58a6ff; }
.section { margin-bottom:14px; padding:10px; background:#161b22; border:1px solid #30363d; border-radius:6px; }
.muted { color:#6e7681; font-size:12px; }
table { width:100%; font-size:12px; border-collapse:collapse; }
th, td { text-align:left; padding:5px 6px; border-bottom:1px solid #21262d; vertical-align:top; }
th { color:#8b949e; font-weight:500; }
.tag { display:inline-block; padding:1px 6px; border-radius:3px; font-size:11px; background:#21262d; color:#8b949e; }
.tag.admin { background:#1f3a5f; color:#79c0ff; }
.tag.char  { background:#3a2f5f; color:#d2a8ff; }
.action-buttons button { background:none; border:0; color:#8b949e; cursor:pointer; padding:2px 6px; }
.action-buttons button:hover { color:#c9d1d9; }
form.create-job { margin-top:10px; padding:10px; background:#0d1117; border:1px solid #30363d; border-radius:4px; }
form.create-job .row { display:flex; gap:10px; flex-wrap:wrap; align-items:flex-end; }
form.create-job .field { display:flex; flex-direction:column; gap:3px; min-width:140px; }
form.create-job label { font-size:11px; color:#8b949e; }
form.create-job input, form.create-job select, form.create-job textarea {
  background:#0d1117; color:#c9d1d9; border:1px solid #30363d; border-radius:4px; padding:5px 7px; font-size:12px;
}
form.create-job button { background:#238636; color:#fff; border:0; padding:6px 12px; border-radius:4px; cursor:pointer; font-size:13px; }
.outcome-ok { color:#3fb950; }
.outcome-err { color:#f85149; }
.outcome-paused { color:#6e7681; }
</style>
</head>
<body>
<h1>Scheduler — Background Jobs</h1>

<div class="section">
  <h2>All jobs</h2>
  <p class="muted">Admin jobs (e.g. memory consolidation, file extraction) are highlighted as <span class="tag admin">admin</span>. Per-character jobs from the legacy scheduler still surface here for visibility and can be deleted, but should no longer be created — character actions belong in the AgentLoop.</p>
  <table id="jobs-table">
    <thead><tr><th>Job ID</th><th>Owner</th><th>Trigger</th><th>Action</th><th>Status</th><th></th></tr></thead>
    <tbody><tr><td colspan="6" class="muted">loading…</td></tr></tbody>
  </table>
</div>

<div class="section">
  <h2>Create admin job</h2>
  <form class="create-job" onsubmit="createJob(event)">
    <div class="row">
      <div class="field">
        <label>Trigger</label>
        <select id="cj-trigger" required>
          <option value="cron-hourly">Every hour at :00</option>
          <option value="cron-daily">Once a day</option>
          <option value="interval-minutes">Every N minutes</option>
          <option value="date">One-shot at date/time</option>
        </select>
      </div>
      <div class="field" id="cj-extra-wrap">
        <label>Detail</label>
        <input id="cj-extra" placeholder="e.g. 30 (minutes) or 03:00 (HH:MM)" />
      </div>
      <div class="field">
        <label>Action</label>
        <select id="cj-action" onchange="onActionChange()">
          <option value="extract_files">extract_files (knowledge)</option>
          <option value="notify">notify (UI message)</option>
        </select>
      </div>
      <div class="field" style="flex:1; min-width:240px;">
        <label>Payload</label>
        <input id="cj-payload" placeholder="extract: optional prompt — notify: message text" />
      </div>
      <div class="field">
        <label>Agent (optional)</label>
        <input id="cj-agent" placeholder="" />
      </div>
      <div>
        <button type="submit">Create</button>
      </div>
    </div>
    <p class="muted" style="margin:6px 0 0 0;">Per-character actions (send_message, set_status, execute_tool) are not exposed here — they belong in the AgentLoop. Daily Rhythm: Character Editor → Tagesablauf.</p>
  </form>
</div>

<script>
async function loadJobs() {
  const tbody = document.querySelector('#jobs-table tbody');
  try {
    const r = await fetch('/scheduler/jobs', { cache: 'no-store' });
    if (!r.ok) {
      tbody.innerHTML = '<tr><td colspan="6">HTTP ' + r.status + '</td></tr>';
      return;
    }
    const data = await r.json();
    const jobs = (data && data.data) || [];
    if (!jobs.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="muted">No jobs scheduled.</td></tr>';
      return;
    }
    tbody.innerHTML = '';
    for (const j of jobs) {
      const tr = document.createElement('tr');
      const owner = (j.agent || j.character || '').trim();
      const ownerLabel = owner ? `<span class="tag char">${escapeHtml(owner)}</span>` : '<span class="tag admin">admin</span>';
      const trig = j.trigger ? JSON.stringify(j.trigger).slice(0, 80) : '';
      const act = j.action ? (j.action.type || '?') : '?';
      const enabled = j.enabled !== false;
      const statusCls = enabled ? 'outcome-ok' : 'outcome-paused';
      tr.innerHTML = `<td>${escapeHtml(j.id || '?')}</td>
        <td>${ownerLabel}</td>
        <td>${escapeHtml(trig)}</td>
        <td>${escapeHtml(act)}</td>
        <td class="${statusCls}">${enabled ? 'enabled' : 'paused'}</td>
        <td class="action-buttons">
          <button onclick="toggleJob('${escapeAttr(j.id)}')">${enabled ? 'Pause' : 'Resume'}</button>
          <button onclick="deleteJob('${escapeAttr(j.id)}')">Delete</button>
        </td>`;
      tbody.appendChild(tr);
    }
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="6">error: ' + escapeHtml(e.message) + '</td></tr>';
  }
}

function escapeHtml(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function escapeAttr(s) { return String(s == null ? '' : s).replace(/[\\\\"']/g, '\\\\$&'); }

async function deleteJob(id) {
  if (!confirm('Delete job ' + id + '?')) return;
  try { await fetch('/scheduler/jobs/' + encodeURIComponent(id), { method: 'DELETE' }); } catch(e) {}
  await loadJobs();
}

async function toggleJob(id) {
  try { await fetch('/scheduler/jobs/' + encodeURIComponent(id) + '/toggle', { method: 'PUT' }); } catch(e) {}
  await loadJobs();
}

function onActionChange() {
  // Placeholder hook for future per-action field tweaks.
}

function buildTrigger() {
  const kind = document.getElementById('cj-trigger').value;
  const extra = document.getElementById('cj-extra').value.trim();
  if (kind === 'cron-hourly') {
    return { type: 'cron', minute: 0 };
  }
  if (kind === 'cron-daily') {
    const m = (extra.match(/^(\\d{1,2}):(\\d{2})$/) || []);
    const h = m[1] ? parseInt(m[1],10) : 3;
    const min = m[2] ? parseInt(m[2],10) : 0;
    return { type: 'cron', hour: h, minute: min };
  }
  if (kind === 'interval-minutes') {
    const min = parseInt(extra, 10) || 30;
    return { type: 'interval', minutes: min };
  }
  if (kind === 'date') {
    return { type: 'date', run_date: extra };
  }
  return { type: 'cron', minute: 0 };
}

function buildAction() {
  const t = document.getElementById('cj-action').value;
  const payload = document.getElementById('cj-payload').value.trim();
  if (t === 'extract_files') {
    return { type: 'extract_files', extraction_prompt: payload };
  }
  if (t === 'notify') {
    return { type: 'notify', message: payload || 'admin notify' };
  }
  return { type: t };
}

async function createJob(ev) {
  ev.preventDefault();
  const body = {
    agent: document.getElementById('cj-agent').value.trim(),
    trigger: buildTrigger(),
    action: buildAction(),
    enabled: true,
  };
  try {
    const r = await fetch('/scheduler/jobs', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const t = await r.text();
      alert('Create failed: HTTP ' + r.status + ' — ' + t.slice(0, 200));
      return;
    }
  } catch (e) {
    alert('Create failed: ' + e.message);
    return;
  }
  document.getElementById('cj-extra').value = '';
  document.getElementById('cj-payload').value = '';
  document.getElementById('cj-agent').value = '';
  await loadJobs();
}

loadJobs();
setInterval(loadJobs, 15000);
</script>
</body>
</html>
"""


# ── Template Playground ────────────────────────────────────────────────

@router.get("/templates/list")
async def templates_list(user=Depends(require_admin)):
    """List all .md files under shared/templates/llm/."""
    from app.core.template_preview import list_templates
    return {"templates": list_templates()}


@router.get("/templates/file")
async def templates_read(path: str, user=Depends(require_admin)):
    from app.core.template_preview import read_template
    try:
        return {"path": path, "content": read_template(path)}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Template not found: {path}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/templates/file")
async def templates_save(request: Request, user=Depends(require_admin)):
    body = await request.json()
    path = (body.get("path") or "").strip()
    content = body.get("content")
    if not path or content is None:
        raise HTTPException(status_code=400, detail="path + content required")
    from app.core.template_preview import save_template
    try:
        save_template(path, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "saved", "path": path}


@router.get("/templates/render")
async def templates_render(path: str, agent: str = "", avatar: str = "",
                           user=Depends(require_admin)):
    """Render the template at ``path`` against real production data for
    the given agent + avatar."""
    from app.core.template_preview import render_with_real_data
    return render_with_real_data(path, agent, avatar)


@router.get("/templates", response_class=HTMLResponse)
async def templates_page(user=Depends(require_admin)):
    """Template playground: top bar + 2-column editor/preview."""
    return _TEMPLATES_PAGE_HTML


_TEMPLATES_PAGE_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Templates</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background:#0d1117; color:#c9d1d9; margin:0; padding:0; height:100vh; display:flex; flex-direction:column; }
.topbar { display:flex; gap:8px; align-items:center; padding:10px 14px; background:#161b22; border-bottom:1px solid #30363d; flex-wrap:wrap; }
.topbar select, .topbar button { background:#0d1117; color:#c9d1d9; border:1px solid #30363d; padding:6px 10px; border-radius:4px; font-size:12px; }
.topbar select { min-width:200px; }
.topbar button { cursor:pointer; }
.topbar button:hover { background:#21262d; }
.topbar button.primary { background:#238636; border-color:#238636; color:#fff; }
.topbar button.primary:hover { background:#2ea043; }
.topbar label { font-size:11px; color:#8b949e; }
#status { margin-left:auto; font-size:11px; color:#8b949e; }
#status.ok { color:#3fb950; }
#status.err { color:#f85149; }
.cols { flex:1; display:flex; min-height:0; }
.col { flex:1; display:flex; flex-direction:column; min-width:0; }
.col + .col { border-left:1px solid #30363d; }
.col-header { padding:6px 10px; background:#161b22; border-bottom:1px solid #30363d; font-size:11px; color:#8b949e; }
textarea, pre.preview { flex:1; margin:0; padding:10px; background:#0d1117; color:#c9d1d9; border:0; outline:0; font-family: ui-monospace, SFMono-Regular, monospace; font-size:12px; line-height:1.5; resize:none; white-space:pre-wrap; word-break:break-word; overflow:auto; }
textarea:focus { background:#010409; }
.note { padding:6px 10px; background:#161b22; border-top:1px solid #30363d; font-size:11px; color:#8b949e; }
.kind-chat { color:#58a6ff; }
.kind-tasks { color:#a5a5a5; }
.no-preview { opacity:0.5; }
</style>
</head>
<body>
<div class="topbar">
  <label>Template</label>
  <select id="sel-template"></select>
  <label>Avatar</label>
  <select id="sel-avatar"></select>
  <label>Agent</label>
  <select id="sel-agent"></select>
  <button id="btn-save" class="primary">Save</button>
  <button id="btn-render">Refresh preview</button>
  <span id="status">—</span>
</div>
<div class="cols">
  <div class="col">
    <div class="col-header">Edit (raw markdown)</div>
    <textarea id="editor" spellcheck="false" placeholder="Pick a template above…"></textarea>
  </div>
  <div class="col">
    <div class="col-header">Preview (real data, what production would build)</div>
    <pre class="preview" id="preview">—</pre>
    <div class="note" id="note">—</div>
  </div>
</div>

<script>
let _state = { templates: [], characters: [], dirty: false };

function setStatus(msg, kind) {
  const el = document.getElementById('status');
  el.textContent = msg;
  el.className = kind || '';
}

async function loadTemplates() {
  const r = await fetch('/admin/templates/list');
  if (!r.ok) { setStatus('list failed: ' + r.status, 'err'); return; }
  const d = await r.json();
  _state.templates = d.templates || [];
  const sel = document.getElementById('sel-template');
  sel.innerHTML = '';
  let lastKind = '';
  for (const t of _state.templates) {
    if (t.kind !== lastKind) {
      const og = document.createElement('optgroup');
      og.label = t.kind;
      og.id = 'optgroup-' + t.kind;
      sel.appendChild(og);
      lastKind = t.kind;
    }
    const o = document.createElement('option');
    o.value = t.path;
    o.textContent = t.path.split('/').pop().replace('.md', '') + (t.has_preview ? '' : '  (no preview)');
    if (!t.has_preview) o.classList.add('no-preview');
    document.getElementById('optgroup-' + t.kind).appendChild(o);
  }
}

async function loadCharacters() {
  const r = await fetch('/characters/list');
  if (!r.ok) return;
  const d = await r.json();
  const chars = d.characters || [];
  const av = document.getElementById('sel-avatar');
  const ag = document.getElementById('sel-agent');
  av.innerHTML = '<option value="">(none)</option>';
  ag.innerHTML = '';
  for (const c of chars) {
    const oa = document.createElement('option'); oa.value = c; oa.textContent = c; av.appendChild(oa);
    const og = document.createElement('option'); og.value = c; og.textContent = c; ag.appendChild(og);
  }
  if (chars.length >= 2) av.value = chars[0];
  if (chars.length >= 1) ag.value = chars[chars.length >= 2 ? 1 : 0];
}

async function loadFile(path) {
  setStatus('loading…');
  try {
    const r = await fetch('/admin/templates/file?path=' + encodeURIComponent(path));
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    document.getElementById('editor').value = d.content || '';
    _state.dirty = false;
    setStatus('loaded', 'ok');
    await render();
  } catch (e) {
    setStatus('load failed: ' + e.message, 'err');
  }
}

async function saveFile() {
  const path = document.getElementById('sel-template').value;
  const content = document.getElementById('editor').value;
  if (!path) return;
  setStatus('saving…');
  try {
    const r = await fetch('/admin/templates/file', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, content }),
    });
    if (!r.ok) throw new Error(await r.text());
    _state.dirty = false;
    setStatus('saved', 'ok');
    await render();
  } catch (e) {
    setStatus('save failed: ' + e.message, 'err');
  }
}

async function render() {
  const path = document.getElementById('sel-template').value;
  const avatar = document.getElementById('sel-avatar').value;
  const agent = document.getElementById('sel-agent').value;
  if (!path) return;
  setStatus('rendering…');
  try {
    const r = await fetch(`/admin/templates/render?path=${encodeURIComponent(path)}&agent=${encodeURIComponent(agent)}&avatar=${encodeURIComponent(avatar)}`);
    const d = await r.json();
    const prev = document.getElementById('preview');
    const note = document.getElementById('note');
    if (d.ok) {
      prev.textContent = d.output || '(empty)';
      note.textContent = d.note || '';
      setStatus('rendered', 'ok');
    } else {
      prev.textContent = '(no output)';
      note.textContent = d.note || 'preview failed';
      setStatus('preview failed', 'err');
    }
  } catch (e) {
    setStatus('render failed: ' + e.message, 'err');
  }
}

document.getElementById('sel-template').addEventListener('change', e => loadFile(e.target.value));
document.getElementById('sel-avatar').addEventListener('change', render);
document.getElementById('sel-agent').addEventListener('change', render);
document.getElementById('btn-save').addEventListener('click', saveFile);
document.getElementById('btn-render').addEventListener('click', render);
document.getElementById('editor').addEventListener('input', () => { _state.dirty = true; setStatus('unsaved changes'); });

(async () => {
  await Promise.all([loadTemplates(), loadCharacters()]);
  const sel = document.getElementById('sel-template');
  if (sel.options.length) {
    sel.value = sel.options[0].value;
    await loadFile(sel.value);
  }
})();
</script>
</body>
</html>
"""


@router.get("/settings/comfyui-models")
async def comfyui_models(user=Depends(require_admin)):
    """Return cached ComfyUI checkpoints and LoRAs."""
    try:
        from app.core.dependencies import get_skill_manager
        sm = get_skill_manager()
        imagegen = sm.get_skill("image_generation") if sm else None
        if not imagegen:
            return {"checkpoints": [], "loras": []}
        return {
            "checkpoints": imagegen.get_cached_checkpoints(),
            "loras": imagegen.get_cached_loras(),
            "clip_models": imagegen.get_cached_clip_models(),
        }
    except Exception as e:
        return {"checkpoints": [], "loras": [], "error": str(e)}


# ── Helpers ──

def _apply_schema_defaults(data: dict) -> None:
    """Fuellt leere Config-Felder mit Schema-Defaults vor.

    Iteriert ueber SECTIONS aus config_schema und traegt fehlende oder leere
    Werte ein, wenn ein 'default' definiert ist — damit der Admin-User sofort
    sieht, welcher Fallback aktiv waere.
    """
    schema = get_schema()
    for section_key, section_def in schema.items():
        is_array = section_def.get("is_array", False)
        fields = section_def.get("fields", {})
        if is_array:
            items = data.get(section_key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                _fill_defaults(item, fields)
                for nested_key, nested_def in fields.items():
                    if isinstance(nested_def, dict) and nested_def.get("is_array"):
                        nested_items = item.get(nested_key)
                        if isinstance(nested_items, list):
                            nested_fields = nested_def.get("item_fields", {})
                            for ni in nested_items:
                                if isinstance(ni, dict):
                                    _fill_defaults(ni, nested_fields)
        else:
            section_data = data.get(section_key)
            if not isinstance(section_data, dict):
                # Section fehlt oder ist None → frisch anlegen, NICHT in Top-Level
                # droppen (das wuerde die Schema-Felder ausserhalb ihrer Section
                # ablegen, das Frontend rendert sie dann gar nicht oder crasht).
                section_data = {}
                data[section_key] = section_data
            _fill_defaults(section_data, fields)


def _fill_defaults(obj: dict, fields: dict) -> None:
    """Setzt fehlende/leere Werte in obj auf den field-default."""
    for key, field_def in fields.items():
        if not isinstance(field_def, dict):
            continue
        default = field_def.get("default")
        if default is None:
            continue
        current = obj.get(key)
        if current is None or current == "":
            obj[key] = default
            logger.debug("Config-Default gesetzt: %s = %r", key, default)


def _preserve_unsent_subarray_fields(merged: dict, current: dict) -> None:
    """Bewahrt Schema-Felder in sub_array/is_dict-Items, wenn der Payload sie
    weglaesst.

    Frontend-Bug: `setVal()` aktualisiert CONFIG nur bei `onchange`. Wenn ein
    Feld vor dem ersten Edit undefined ist (z.B. weil ein neues Schema-Feld
    in einer alten Welt-Config noch fehlt) und der User es nie anfasst,
    bleibt CONFIG undefined → JSON.stringify laesst den Key weg →
    `_merge_sensitive` wertet den fehlenden Key als 'absichtlich geloescht'.

    Wir wandern hier durch alle Schema-`sub_arrays` (z.B.
    image_generation.comfyui_workflows, image_generation.backends) und
    uebernehmen fehlende Felder aus der current Config.
    """
    schema = get_schema()
    for sec_key, sec_def in schema.items():
        sub_arrays = sec_def.get("sub_arrays") or {}
        if not sub_arrays:
            continue
        cur_sec = current.get(sec_key)
        new_sec = merged.get(sec_key)
        if not isinstance(cur_sec, dict) or not isinstance(new_sec, dict):
            continue
        for sub_key, sub_def in sub_arrays.items():
            field_keys = list((sub_def.get("fields") or {}).keys())
            if not field_keys:
                continue
            cur_sub = cur_sec.get(sub_key)
            new_sub = new_sec.get(sub_key)
            if cur_sub is None or new_sub is None:
                continue
            if sub_def.get("is_dict"):
                if not isinstance(cur_sub, dict) or not isinstance(new_sub, dict):
                    continue
                for item_id, new_item in new_sub.items():
                    cur_item = cur_sub.get(item_id)
                    if not isinstance(cur_item, dict) or not isinstance(new_item, dict):
                        continue
                    for f in field_keys:
                        if f not in new_item and f in cur_item:
                            new_item[f] = cur_item[f]
            else:
                if not isinstance(cur_sub, list) or not isinstance(new_sub, list):
                    continue
                for i, new_item in enumerate(new_sub):
                    if i >= len(cur_sub):
                        break
                    cur_item = cur_sub[i]
                    if not isinstance(cur_item, dict) or not isinstance(new_item, dict):
                        continue
                    for f in field_keys:
                        if f not in new_item and f in cur_item:
                            new_item[f] = cur_item[f]


def _merge_sensitive(new: Any, current: Any) -> Any:
    """Recursively merge, keeping current values where new has masked placeholders.

    WICHTIG: Keys die im neuen Dict FEHLEN wurden bewusst geloescht und
    werden NICHT aus current wiederhergestellt. Nur bei Leaf-Werten mit
    '***'-Maskierung greift der Sensitive-Schutz.
    """
    if isinstance(new, dict) and isinstance(current, dict):
        result = {}
        # Nur Keys aus new uebernehmen — fehlende Keys = geloescht.
        for key in new:
            if key in current:
                result[key] = _merge_sensitive(new[key], current[key])
            else:
                result[key] = new[key]
        return result
    if isinstance(new, list) and isinstance(current, list):
        return [
            _merge_sensitive(new[i], current[i]) if i < len(current) else new[i]
            for i in range(len(new))
        ]
    # If new value is a masked placeholder, keep current
    if isinstance(new, str) and new.startswith("***"):
        return current
    return new


def _build_settings_html() -> str:
    """Build the complete admin settings HTML page."""
    return '''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin Settings</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0d1117; color: #c9d1d9; display: flex; height: 100vh; overflow: hidden; }

/* ── Sidebar ── */
.sidebar {
    width: 220px; min-width: 220px; background: #161b22; border-right: 1px solid #30363d;
    overflow-y: auto; padding: 12px 0;
}
.sidebar h1 { font-size: 15px; padding: 8px 16px; color: #58a6ff; border-bottom: 1px solid #30363d; margin-bottom: 8px; }
.sidebar a {
    display: block; padding: 7px 16px; color: #8b949e; text-decoration: none;
    font-size: 13px; border-left: 3px solid transparent; transition: all 0.15s;
}
.sidebar a:hover { color: #c9d1d9; background: #1c2128; }
.sidebar a.active { color: #58a6ff; border-left-color: #58a6ff; background: #1c2128; }
.sidebar .nav-icon { margin-right: 6px; }
.sidebar .nav-section-label {
    padding: 10px 16px 4px; margin-top: 10px; font-size: 11px; font-weight: 700;
    color: #8b949e; text-transform: uppercase; letter-spacing: 0.8px;
    border-top: 1px solid #30363d;
}
.sidebar .nav-section-label:first-of-type { margin-top: 4px; border-top: none; }

/* ── Main ── */
.main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.toolbar {
    background: #161b22; border-bottom: 1px solid #30363d;
    padding: 10px 20px; display: flex; gap: 10px; align-items: center;
}
.toolbar .spacer { flex: 1; }
.content { flex: 1; overflow-y: auto; padding: 20px; }

/* ── Buttons ── */
.btn {
    background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
    padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px;
    display: inline-flex; align-items: center; gap: 4px;
}
.btn:hover { background: #30363d; }
.btn-primary { background: #238636; border-color: #2ea043; color: #fff; }
.btn-primary:hover { background: #2ea043; }
.btn-danger { background: #da3633; border-color: #f85149; color: #fff; }
.btn-danger:hover { background: #b62324; }
.btn-sm { padding: 4px 8px; font-size: 12px; }

/* ── Section ── */
.section { display: none; }
.section.active { display: block; }
.section-title { font-size: 18px; font-weight: 600; margin-bottom: 16px; color: #e6edf3; }
.subsection { margin: 16px 0; padding: 16px; background: #161b22; border: 1px solid #30363d; border-radius: 8px; }
.subsection-title { font-size: 14px; font-weight: 600; margin-bottom: 12px; color: #58a6ff; }

/* ── Form Fields ── */
.field { margin-bottom: 12px; display: flex; align-items: flex-start; gap: 12px; }
.field label { width: 180px; min-width: 180px; font-size: 13px; color: #8b949e; padding-top: 7px; text-align: right; }
.field .input-wrap { flex: 1; }
.field input[type="text"], .field input[type="number"], .field input[type="password"],
.field select, .field textarea {
    width: 100%; background: #0d1117; color: #c9d1d9; border: 1px solid #30363d;
    padding: 6px 10px; border-radius: 6px; font-size: 13px; font-family: inherit;
}
.field input:focus, .field select:focus, .field textarea:focus { border-color: #58a6ff; outline: none; }
.field textarea { min-height: 60px; resize: vertical; }
.field .desc { font-size: 11px; color: #6e7681; margin-top: 3px; }
.field input[type="checkbox"] { margin-top: 8px; }

/* Toggle for password */
.pw-wrap { position: relative; }
.pw-wrap input { padding-right: 36px; }
.pw-toggle {
    position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
    background: none; border: none; color: #8b949e; cursor: pointer; font-size: 14px;
}

/* ── Array Items (Providers, Backends) ── */
.array-item {
    border: 1px solid #30363d; border-radius: 8px; margin-bottom: 12px;
    background: #0d1117;
}
.array-item-header {
    display: flex; align-items: center; padding: 8px 12px;
    background: #161b22; border-radius: 8px 8px 0 0; cursor: pointer;
    border-bottom: 1px solid #30363d;
}
.array-item-header .title { flex: 1; font-weight: 600; font-size: 13px; }
.array-item-header .badge { font-size: 11px; color: #8b949e; margin-right: 8px; }
.array-item-body { padding: 12px; display: none; }
.array-item.open .array-item-body { display: block; }
.array-item-header .chevron { transition: transform 0.2s; color: #8b949e; }
.array-item.open .array-item-header .chevron { transform: rotate(90deg); }

/* LoRA rows */
.lora-row { display: flex; gap: 8px; margin-bottom: 6px; align-items: center; }
.lora-row input:first-child { flex: 3; }
.lora-row input:last-child { flex: 1; max-width: 80px; }

/* GPU rows */
.gpu-row { display: flex; gap: 8px; margin-bottom: 6px; align-items: center; }
.gpu-row input, .gpu-row select { flex: 1; }

/* ── Toast ── */
.toast {
    position: fixed; bottom: 20px; right: 20px; padding: 12px 20px;
    border-radius: 8px; font-size: 13px; z-index: 1000;
    opacity: 0; transition: opacity 0.3s; pointer-events: none;
}
.toast.show { opacity: 1; }
.toast.success { background: #238636; color: #fff; }
.toast.error { background: #da3633; color: #fff; }

/* ── Loading ── */
.loading { text-align: center; padding: 60px; color: #8b949e; }
.spinner { display: inline-block; width: 24px; height: 24px; border: 3px solid #30363d; border-top-color: #58a6ff; border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* Status indicator */
.status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; }
.status-dot.on { background: #3fb950; }
.status-dot.off { background: #6e7681; }

/* Validation results */
.validate-results { margin: 16px 0; padding: 16px; background: #161b22; border: 1px solid #30363d; border-radius: 8px; }
.validate-results h3 { font-size: 14px; margin-bottom: 10px; }
.validate-results.has-errors h3 { color: #f85149; }
.validate-results.all-ok h3 { color: #3fb950; }
.validate-issue { padding: 6px 10px; margin: 4px 0; border-radius: 4px; font-size: 13px; display: flex; align-items: flex-start; gap: 8px; }
.validate-issue.error { background: #da363322; border-left: 3px solid #f85149; }
.validate-issue.warning { background: #d2992222; border-left: 3px solid #d29922; }
.validate-issue .badge { font-size: 11px; font-weight: 600; padding: 1px 6px; border-radius: 3px; white-space: nowrap; }
.validate-issue.error .badge { background: #da363344; color: #f85149; }
.validate-issue.warning .badge { background: #d2992244; color: #d29922; }
.validate-issue .section-link { color: #58a6ff; cursor: pointer; font-size: 12px; text-decoration: underline; margin-left: auto; white-space: nowrap; }

/* Embedded iframe for tool pages */
.content iframe { width: 100%; height: 100%; border: none; }
.content.iframe-mode { padding: 0; overflow: hidden; }
</style>
</head>
<body>

<nav class="sidebar">
    <h1>Admin</h1>
    <div id="world-badge" style="margin: 4px 0 12px 8px; padding: 4px 8px; background:#1f3a5f; color:#79c0ff; font-size:12px; border-radius:4px; display:inline-block;">world: <span id="world-name">…</span></div>
    <div class="nav-section-label">Server-Einstellungen</div>
    <div id="nav-links"></div>
    <div class="nav-section-label">Verwaltung</div>
    <a href="#" data-section="_users" onclick="event.preventDefault(); activateIframe('_users', '/admin/users', 'User-Verwaltung')"><span class="nav-icon">👥</span> User-Verwaltung</a>
    <a href="#" data-section="_outfit_rules" onclick="event.preventDefault(); activateIframe('_outfit_rules', '/admin/outfit-rules', 'Outfit-Regeln')"><span class="nav-icon">👗</span> Outfit-Regeln</a>
    <a href="#" data-section="_models" onclick="event.preventDefault(); activateIframe('_models', '/admin/models', 'Model Capabilities')"><span class="nav-icon">🧩</span> Model Capabilities</a>
    <a href="#" data-section="_agent_loop" onclick="event.preventDefault(); activateIframe('_agent_loop', '/admin/agent-loop', 'Agent Loop')"><span class="nav-icon">🔄</span> Agent Loop</a>
    <a href="#" data-section="_scheduler" onclick="event.preventDefault(); activateIframe('_scheduler', '/admin/scheduler', 'Scheduler')"><span class="nav-icon">⏱</span> Scheduler</a>
    <a href="#" data-section="_templates" onclick="event.preventDefault(); activateIframe('_templates', '/admin/templates', 'LLM Templates')"><span class="nav-icon">📄</span> LLM Templates</a>
    <div class="nav-section-label">Logs & Monitoring</div>
    <a href="#" data-section="_dashboard" onclick="event.preventDefault(); activateIframe('_dashboard', '/dashboard', 'Dashboard')"><span class="nav-icon">📊</span> Dashboard</a>
    <a href="#" data-section="_llm_stats" onclick="event.preventDefault(); activateIframe('_llm_stats', '/admin/llm-stats', 'LLM Stats')"><span class="nav-icon">📈</span> LLM Stats</a>
    <a href="#" data-section="_llm_log" onclick="event.preventDefault(); activateIframe('_llm_log', '/logs/llm', 'LLM Log')"><span class="nav-icon">📝</span> LLM Log</a>
    <a href="#" data-section="_image_log" onclick="event.preventDefault(); activateIframe('_image_log', '/logs/image-prompts', 'Image Prompt Log')"><span class="nav-icon">🖼</span> Image Prompt Log</a>
</nav>

<div class="main">
    <div class="toolbar" id="settings-toolbar">
        <button class="btn btn-primary" onclick="saveConfig()" id="btn-save">Save</button>
        <button class="btn" onclick="validateConfig()" id="btn-validate" style="border-color:#d29922; color:#d29922;">Validate</button>
        <button class="btn" onclick="reloadServices()">Reload Services</button>
        <span class="spacer"></span>
        <span id="status-msg" style="font-size: 12px; color: #8b949e;"></span>
    </div>
    <div class="content" id="content">
        <div class="loading"><div class="spinner"></div><p style="margin-top: 12px;">Loading configuration...</p></div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
let CONFIG = {};
let SCHEMA = {};
let PROVIDERS_CACHE = {};
let ACTIVE_SECTION = null;

// ── Init ──
async function init() {
    try {
        const [dataResp, schemaResp] = await Promise.all([
            fetch('/admin/settings/raw', { credentials: 'same-origin' }),
            fetch('/admin/settings/schema', { credentials: 'same-origin' })
        ]);
        if (dataResp.status === 401 || dataResp.status === 403) {
            const ret = encodeURIComponent(window.location.pathname + window.location.hash);
            window.location.href = '/?return=' + ret;
            return;
        }
        CONFIG = await dataResp.json();
        SCHEMA = await schemaResp.json();
        buildNav();
        // Activate first section
        const first = Object.keys(SCHEMA)[0];
        if (first) activateSection(first);
    } catch (e) {
        document.getElementById('content').innerHTML = '<div class="loading" style="color:#f85149;">Error loading config: ' + e.message + '</div>';
    }
}

function authHeaders() {
    // Cookie-basiert: Browser sendet Session-Cookie automatisch. Nur Content-Type explizit setzen.
    return { 'Content-Type': 'application/json' };
}

// ── Navigation ──
function buildNav() {
    const nav = document.getElementById('nav-links');
    nav.innerHTML = '';
    for (const [key, sec] of Object.entries(SCHEMA)) {
        const a = document.createElement('a');
        a.href = '#' + key;
        a.innerHTML = '<span class="nav-icon">' + (sec.icon || '') + '</span> ' + sec.label;
        a.dataset.section = key;
        a.onclick = (e) => { e.preventDefault(); activateSection(key); };
        nav.appendChild(a);
    }
}

function activateSection(key) {
    ACTIVE_SECTION = key;
    // Update nav
    document.querySelectorAll('.sidebar a').forEach(a => a.classList.remove('active'));
    const link = document.querySelector('.sidebar a[data-section="' + key + '"]');
    if (link) link.classList.add('active');
    // Show settings toolbar, restore content mode
    document.getElementById('settings-toolbar').style.display = 'flex';
    const content = document.getElementById('content');
    content.classList.remove('iframe-mode');
    // Render section
    renderSection(key);
}

function activateIframe(key, url, title) {
    ACTIVE_SECTION = key;
    // Update nav
    document.querySelectorAll('.sidebar a').forEach(a => a.classList.remove('active'));
    const link = document.querySelector('.sidebar a[data-section="' + key + '"]');
    if (link) link.classList.add('active');
    // Hide settings toolbar
    document.getElementById('settings-toolbar').style.display = 'none';
    // Load iframe
    const content = document.getElementById('content');
    content.classList.add('iframe-mode');
    content.innerHTML = '<iframe src="' + url + '" title="' + esc(title) + '"></iframe>';
}

// World-Badge im Sidebar — auf jeder Seite + iframe-Children einsehbar.
fetch('/admin/world-name', { credentials: 'same-origin', cache: 'no-store' })
  .then(r => r.ok ? r.json() : null)
  .then(d => {
      const el = document.getElementById('world-name');
      if (el && d && d.world) el.textContent = d.world;
  })
  .catch(() => {});

// ── Render Section ──
function renderSection(key) {
    const sec = SCHEMA[key];
    // null und undefined beide auf Default fallen lassen — sonst wirft
    // renderFields(null, ...) bei data[fKey] einen TypeError.
    const cfgVal = CONFIG[key];
    const data = (cfgVal !== undefined && cfgVal !== null) ? cfgVal : (sec.is_array ? [] : {});
    const content = document.getElementById('content');

    let html = '<div class="section active">';
    html += '<h1 class="section-title">' + (sec.icon || '') + ' ' + sec.label + '</h1>';

    // Top-level fields (skip for array sections — fields are rendered per item)
    if (sec.fields && !sec.is_array) {
        html += renderFields(sec.fields, data, key);
    }

    // Subsections
    if (sec.subsections) {
        for (const [subKey, sub] of Object.entries(sec.subsections)) {
            const subData = data[subKey] || {};
            html += '<div class="subsection">';
            html += '<div class="subsection-title">' + sub.label + '</div>';
            html += renderFields(sub.fields, subData, key + '.' + subKey);
            html += '</div>';
        }
    }

    // Sub-arrays (like backends, comfyui_workflows)
    if (sec.sub_arrays) {
        for (const [arrKey, arrDef] of Object.entries(sec.sub_arrays)) {
            html += '<div class="subsection">';
            html += '<div class="subsection-title" style="display:flex; align-items:center; justify-content:space-between;">';
            html += arrDef.label;
            html += '<button class="btn btn-sm" onclick="addArrayItem(\\'' + key + '.' + arrKey + '\\', \\'' + (arrDef.is_dict ? 'dict' : 'array') + '\\')">+ Add</button>';
            html += '</div>';
            if (arrDef.is_dict) {
                html += renderDictItems(arrDef, data[arrKey] || {}, key + '.' + arrKey);
            } else {
                html += renderArrayItems(arrDef, data[arrKey] || [], key + '.' + arrKey);
            }
            html += '</div>';
        }
    }

    // Array sections (providers)
    if (sec.is_array) {
        if (key === 'llm_routing') {
            // Zweispaltig: links Editor, rechts Task-View (read-only)
            html += '<div style="display:grid; grid-template-columns: 1fr 1fr; gap:20px;">';
            html += '<div>';
            html += '<div style="margin-bottom: 12px;">';
            html += '<button class="btn btn-sm" onclick="addArrayItem(\\'' + key + '\\', \\'array\\')">+ Add LLM</button>';
            html += '</div>';
            html += renderArrayItems(sec, data || [], key);
            html += '</div>';
            html += '<div>';
            html += '<div class="subsection-title" style="margin-bottom:8px;">Sichtweise pro Task</div>';
            html += '<div id="llm-task-view"><div class="desc">Lade...</div></div>';
            html += '</div>';
            html += '</div>';
            setTimeout(() => renderLlmTaskView(data || []), 0);
        } else {
            html += '<div style="margin-bottom: 12px;">';
            html += '<button class="btn btn-sm" onclick="addArrayItem(\\'' + key + '\\', \\'array\\')">+ Add ' + sec.label + '</button>';
            html += '</div>';
            html += renderArrayItems(sec, data || [], key);
        }
    }

    html += '</div>';
    content.innerHTML = html;
    // image_preview-Felder Meta nachladen (kein <script> via innerHTML moeglich)
    populateImagePreviewMetas();
}

async function populateImagePreviewMetas() {
    const els = document.querySelectorAll('.image-preview-meta[data-meta-url]');
    for (const el of els) {
        const url = el.dataset.metaUrl;
        if (!url) continue;
        try {
            const r = await fetch(url);
            if (!r.ok) continue;
            const d = await r.json();
            if (d.has_frame && d.bbox && d.frame_size) {
                el.textContent = 'Frame ' + d.frame_size[0] + '×' + d.frame_size[1]
                    + ' — Window ' + d.bbox.w + '×' + d.bbox.h
                    + ' @ (' + d.bbox.x + ',' + d.bbox.y + ')'
                    + (d.generated_at ? ' — generiert ' + d.generated_at : '');
            } else {
                el.textContent = 'Noch nicht generiert.';
            }
        } catch (e) { /* ignore */ }
    }
}

async function renderLlmTaskView(entries) {
    const tasks = await loadLlmTasks();
    const view = document.getElementById('llm-task-view');
    if (!view) return;

    // State vom Server laden (runtime + persistent + presets)
    let state = { disabled: [], runtime_disabled: [], presets: {} };
    try {
        const r = await fetch('/admin/settings/llm-task-state', { credentials: 'same-origin' });
        if (r.ok) state = await r.json();
    } catch (e) {}

    // Persistent disabled aus CONFIG (UI-Quelle fuer Toggles)
    const persistentDisabled = new Set(
        ((CONFIG.llm_task_state || {}).disabled_tasks || [])
    );
    const runtimeDisabled = new Set(state.runtime_disabled || []);

    // task_id -> [{order, provider, model, llmDisabled}]
    const byTask = {};
    for (const entry of (entries || [])) {
        if (!entry || typeof entry !== 'object') continue;
        const prov = entry.provider || '';
        const mod = entry.model || '';
        const llmDisabled = entry.enabled === false;
        for (const t of (entry.tasks || [])) {
            if (!t || !t.task) continue;
            (byTask[t.task] = byTask[t.task] || []).push({
                order: t.order || 999,
                provider: prov,
                model: mod,
                llmDisabled: llmDisabled,
            });
        }
    }
    for (const k in byTask) byTask[k].sort((a, b) => a.order - b.order);

    let html = '';
    // Preset-Selector (runtime, nicht persistent — gilt nur fuer diese Server-Session)
    html += '<div style="margin-bottom:10px; padding:8px 10px; background:#161b22; border:1px solid #30363d; border-radius:6px;">';
    html += '<div style="font-size:12px; color:#8b949e; margin-bottom:6px;">Runtime-Preset (nicht persistent):</div>';
    html += '<select id="llm-task-preset" onchange="applyTaskPreset(this.value)" style="background:#0d1117; color:#c9d1d9; border:1px solid #30363d; padding:6px; border-radius:4px; width:100%;">';
    html += '<option value="none">— keins (alle Tasks aktiv) —</option>';
    for (const p of Object.keys(state.presets || {})) {
        html += '<option value="' + esc(p) + '">' + esc(p) + ' — ' + (state.presets[p] || []).length + ' Tasks aus</option>';
    }
    html += '</select>';
    if (runtimeDisabled.size) {
        html += '<div style="font-size:11px; color:#d29922; margin-top:4px;">Aktiv: ' + runtimeDisabled.size + ' Tasks runtime-deaktiviert</div>';
    }
    html += '</div>';

    // Sortierung nach Category (chat → tool → helper → image), innerhalb
    // dann nach Label. So sind groessere Modelle (chat) oben, kleine
    // Helfer unten — entspricht der Lese-Erwartung "wer braucht was".
    const _CAT_ORDER = { chat: 0, tool: 1, helper: 2, image: 3 };
    // Per-Category Farben fuer Border + Badge:
    //   chat:   blau    — grosse Modelle
    //   tool:   violett — strukturierte Outputs
    //   helper: gruen   — kleine/billige Modelle
    //   image:  orange  — Vision / Bild-IO
    const _CAT_COLORS = {
        chat:   { bg: '#1f3a5f', fg: '#79c0ff', border: '#30547a' },
        tool:   { bg: '#3a2f5f', fg: '#d2a8ff', border: '#54497a' },
        helper: { bg: '#1c3a2c', fg: '#7ee787', border: '#2d553f' },
        image:  { bg: '#5a3a1f', fg: '#ffaa66', border: '#7a543d' },
        '':     { bg: '#21262d', fg: '#8b949e', border: '#30363d' },
    };
    const sortedTasks = [...tasks].sort((a, b) => {
        const ao = _CAT_ORDER[a.category] ?? 99;
        const bo = _CAT_ORDER[b.category] ?? 99;
        if (ao !== bo) return ao - bo;
        return (a.label || '').localeCompare(b.label || '');
    });

    let _lastCat = null;
    for (const t of sortedTasks) {
        // Category-Header bei Wechsel
        if (t.category !== _lastCat) {
            _lastCat = t.category;
            const cc = _CAT_COLORS[t.category] || _CAT_COLORS[''];
            html += '<div style="margin:14px 0 6px 0; padding:4px 10px; '
                 + 'background:' + cc.bg + '; color:' + cc.fg + '; '
                 + 'border-left:3px solid ' + cc.fg + '; border-radius:3px; '
                 + 'font-size:11px; font-weight:600; letter-spacing:0.3px; '
                 + 'text-transform:uppercase;">'
                 + esc(t.category_label || 'Other') + '</div>';
        }

        const rows = byTask[t.id] || [];
        const isEmpty = rows.length === 0;
        const isPersistDisabled = persistentDisabled.has(t.id);
        const isRuntimeDisabled = runtimeDisabled.has(t.id);
        const disabledStyle = (isPersistDisabled || isRuntimeDisabled) ? 'opacity:0.5;' : '';
        const cc = _CAT_COLORS[t.category] || _CAT_COLORS[''];
        html += '<div style="margin-bottom:6px; padding:8px 10px; background:#0d1117; '
             + 'border:1px solid #30363d; border-left:3px solid ' + cc.fg + '; '
             + 'border-radius:6px; ' + disabledStyle + '">';
        html += '<div style="display:flex; justify-content:space-between; align-items:center;">';
        let catBadge = '';
        if (t.category_label) {
            catBadge = ' <span style="font-size:10px; color:' + cc.fg
                 + '; font-weight:400; background:' + cc.bg
                 + '; padding:1px 6px; border-radius:8px; margin-left:4px;">'
                 + esc(t.category_label) + '</span>';
        }
        html += '<div style="font-size:12px; color:#58a6ff; font-weight:600;">' + esc(t.label) + catBadge + ' <span style="color:#6e7681; font-weight:400;">— ' + esc(t.id) + '</span></div>';
        html += '<label style="display:inline-flex; align-items:center; gap:4px; font-size:11px; color:#8b949e; cursor:pointer;">';
        html += '<input type="checkbox" ' + (isPersistDisabled ? '' : 'checked') + ' onchange="toggleTaskPersistent(\\'' + t.id + '\\', !this.checked)"> aktiv';
        html += '</label>';
        html += '</div>';
        if (isRuntimeDisabled) {
            html += '<div style="font-size:11px; color:#d29922;">runtime-deaktiviert (Preset)</div>';
        }
        if (isEmpty) {
            html += '<div class="desc" style="color:#d29922;">kein LLM zugeordnet</div>';
        } else {
            html += '<div style="margin-top:4px;">';
            for (const r of rows) {
                const rowStyle = r.llmDisabled
                    ? 'font-size:12px; color:#6e7681; display:flex; gap:8px; text-decoration:line-through;'
                    : 'font-size:12px; color:#c9d1d9; display:flex; gap:8px;';
                html += '<div style="' + rowStyle + '">';
                html += '<span style="color:#6e7681; min-width:22px;">' + r.order + '.</span>';
                html += '<span>' + esc(r.provider) + ' / ' + esc(r.model) + '</span>';
                if (r.llmDisabled) {
                    html += '<span style="color:#d29922; text-decoration:none;">(LLM disabled)</span>';
                }
                html += '</div>';
            }
            html += '</div>';
        }
        html += '</div>';
    }
    view.innerHTML = html;
}

function toggleTaskPersistent(taskId, disable) {
    if (!CONFIG.llm_task_state) CONFIG.llm_task_state = { disabled_tasks: [] };
    const arr = CONFIG.llm_task_state.disabled_tasks || [];
    const idx = arr.indexOf(taskId);
    if (disable && idx < 0) arr.push(taskId);
    if (!disable && idx >= 0) arr.splice(idx, 1);
    CONFIG.llm_task_state.disabled_tasks = arr;
    toast('Aenderung erst nach Save aktiv', 'success');
    renderLlmTaskView(CONFIG.llm_routing || []);
}

async function applyTaskPreset(preset) {
    try {
        const resp = await fetch('/admin/settings/llm-task-state/runtime-preset', {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify({ preset: preset }),
        });
        const data = await resp.json();
        if (preset === 'none') {
            toast('Runtime-Preset aufgehoben', 'success');
        } else {
            toast('Runtime-Preset "' + preset + '" aktiv (' + (data.disabled || []).length + ' Tasks aus)', 'success');
        }
        renderLlmTaskView(CONFIG.llm_routing || []);
    } catch (e) {
        toast('Preset-Fehler: ' + e.message, 'error');
    }
}

// ── Render Fields ──
function renderFields(fields, data, path) {
    let html = '';
    for (const [fKey, f] of Object.entries(fields)) {
        if (f.type === 'group_header') {
            // Visueller Trenner ohne Daten-Binding (gruppiert nachfolgende Felder)
            html += '<div class="subsection-title" style="margin-top:18px;">' + f.label + '</div>';
            continue;
        }
        if (f.type === 'button') {
            // Action-Button — kein Daten-Binding, ruft Endpoint mit
            // body aus angegebenen Geschwister-Feldern auf.
            const btnId = 'btn-' + (path + '.' + fKey).replace(/\\W+/g, '-');
            const bodyFrom = JSON.stringify(f.body_from || []);
            const confirmMsg = f.confirm ? esc(f.confirm) : '';
            const previewUrl = f.preview_url ? esc(f.preview_url) : '';
            html += '<div class="field">';
            html += '<label></label>';
            html += '<div class="input-wrap">';
            html += '<button type="button" id="' + btnId + '" class="btn btn-primary" '
                + 'onclick="runActionButton(\\'' + esc(f.endpoint) + '\\', \\'' + (f.method || 'POST') + '\\', '
                + '\\'' + path + '\\', ' + bodyFrom.replace(/"/g, '&quot;') + ', \\'' + confirmMsg + '\\', this, \\'' + previewUrl + '\\')">'
                + esc(f.label) + '</button>';
            if (f.description) html += '<div class="desc">' + f.description + '</div>';
            html += '</div></div>';
            continue;
        }
        if (f.type === 'image_preview') {
            // Live-Preview eines Bild-Endpoints (z.B. generiertes Frame)
            const imgId = 'img-' + (path + '.' + fKey).replace(/\\W+/g, '-');
            const url = esc(f.url);
            const metaUrl = f.meta_url ? esc(f.meta_url) : '';
            html += '<div class="field">';
            html += '<label>' + esc(f.label) + '</label>';
            html += '<div class="input-wrap">';
            html += '<div id="' + imgId + '-wrap" class="image-preview-wrap" style="background:'
                + ' repeating-conic-gradient(#777 0% 25%, #555 0% 50%) 50% / 16px 16px;'
                + ' display:inline-block; padding:6px; border:1px solid #444; border-radius:6px; max-width:300px;">';
            html += '<img id="' + imgId + '" src="' + url + '?_=' + Date.now() + '" '
                + 'style="max-width:280px; max-height:380px; display:block;" '
                + 'onerror="this.style.display=\\'none\\'; this.nextElementSibling.style.display=\\'block\\';">';
            html += '<div style="display:none; color:#888; font-size:12px; padding:20px;">noch nicht generiert</div>';
            html += '</div>';
            if (metaUrl) {
                // Meta-URL als data-attribute hinterlegen — populateImagePreviewMetas()
                // wird nach renderSection aufgerufen und befuellt alle solche Elemente.
                html += '<div id="' + imgId + '-meta" class="desc image-preview-meta" '
                    + 'data-meta-url="' + metaUrl + '" '
                    + 'style="margin-top:6px; font-family:monospace; font-size:11px;"></div>';
            }
            if (f.description) html += '<div class="desc">' + f.description + '</div>';
            html += '</div></div>';
            continue;
        }
        if (f.type === 'array' && fKey === 'gpus') {
            html += renderGpuField(data[fKey] || [], path + '.' + fKey);
            continue;
        }
        if (f.type === 'lora_array') {
            html += renderLoraField(data[fKey] || [], path + '.' + fKey, f.max_items || 4);
            continue;
        }
        if (f.type === 'task_order_list') {
            html += renderTaskOrderList(data[fKey] || [], path + '.' + fKey, f);
            continue;
        }
        const val = data[fKey] !== undefined ? data[fKey] : (f.default !== undefined ? f.default : '');
        const fullPath = path + '.' + fKey;
        html += '<div class="field">';
        html += '<label for="f-' + fullPath + '">' + f.label + '</label>';
        html += '<div class="input-wrap">';
        html += renderInput(f, val, fullPath);
        if (f.description) html += '<div class="desc">' + f.description + '</div>';
        html += '</div></div>';
    }
    return html;
}

function renderInput(f, val, path) {
    const id = 'f-' + path;
    switch (f.type) {
        case 'bool':
            return '<input type="checkbox" id="' + id + '" ' + (val ? 'checked' : '') + ' onchange="setVal(\\'' + path + '\\', this.checked)">';
        case 'int':
            return '<input type="number" id="' + id + '" value="' + esc(val) + '" '
                + (f.min !== undefined ? 'min="' + f.min + '" ' : '')
                + (f.max !== undefined ? 'max="' + f.max + '" ' : '')
                + 'step="1" onchange="setVal(\\'' + path + '\\', parseInt(this.value) || 0)">';
        case 'float':
            return '<input type="number" id="' + id + '" value="' + esc(val) + '" '
                + (f.min !== undefined ? 'min="' + f.min + '" ' : '')
                + (f.max !== undefined ? 'max="' + f.max + '" ' : '')
                + 'step="' + (f.step || 0.1) + '" onchange="setVal(\\'' + path + '\\', parseFloat(this.value) || 0)">';
        case 'select':
            let opts = (f.choices || []).map(c => '<option value="' + esc(c) + '"' + (c == val ? ' selected' : '') + '>' + esc(c) + '</option>').join('');
            return '<select id="' + id + '" onchange="setVal(\\'' + path + '\\', this.value)">' + opts + '</select>';
        case 'password':
            return '<div class="pw-wrap"><input type="password" id="' + id + '" value="' + esc(val) + '" onchange="setVal(\\'' + path + '\\', this.value)">'
                + '<button class="pw-toggle" type="button" onclick="togglePw(this)">👁</button></div>';
        case 'text':
            return '<textarea id="' + id + '" onchange="setVal(\\'' + path + '\\', this.value)">' + esc(val) + '</textarea>';
        case 'provider_select':
            return renderProviderSelect(val, path);
        case 'gpu_select':
            return renderGpuSelect(val, path);
        case 'model_select':
            return renderModelSelect(val, path);
        case 'workflow_select':
            return renderWorkflowSelect(val, path);
        case 'imagegen_select':
            return renderImagegenSelect(val, path);
        case 'comfyui_model_select':
            return renderComfyModelSelect(val, path);
        case 'comfyui_backend_select':
            return renderComfyBackendSelect(val, path, f.multi);
        case 'imagegen_backend_select':
            return renderImagegenBackendSelect(val, path);
        case 'imagegen_model_select':
            return renderImagegenModelSelect(val, path);
        case 'imagegen_target_select':
            return renderImagegenTargetSelect(val, path);
        case 'comfyui_clip_select':
            return renderComfyClipSelect(val, path);
        default: // str
            return '<input type="text" id="' + id + '" value="' + esc(val) + '" '
                + (f.placeholder ? 'placeholder="' + esc(f.placeholder) + '" ' : '')
                + 'onchange="setVal(\\'' + path + '\\', this.value)">';
    }
}

function renderProviderSelect(val, path) {
    const providers = CONFIG.providers || [];
    let opts = '<option value="">— Auto —</option>';
    for (const p of providers) {
        opts += '<option value="' + esc(p.name) + '"' + (p.name === val ? ' selected' : '') + '>' + esc(p.name) + ' (' + p.type + ')</option>';
    }
    return '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value); refreshModelSelect(\\'' + path + '\\')">' + opts + '</select>';
}

function renderGpuSelect(val, path) {
    const providers = CONFIG.providers || [];
    let opts = '<option value="">— Keine —</option>';
    for (const p of providers) {
        const gpus = p.gpus || [];
        for (let i = 0; i < gpus.length; i++) {
            const g = gpus[i];
            const types = Array.isArray(g.types) ? g.types : (g.types || '').split(',');
            if (!types.some(t => t.trim() === 'comfyui')) continue;
            const key = p.name + ':' + i;
            const label = g.label || ('GPU ' + i);
            const vram = g.vram_gb ? ' — ' + g.vram_gb + ' GB' : '';
            opts += '<option value="' + esc(key) + '"' + (key === val ? ' selected' : '') + '>' + esc(p.name) + ' / ' + esc(label) + vram + '</option>';
        }
    }
    return '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">' + opts + '</select>';
}

function renderModelSelect(val, path) {
    // Try to find provider from sibling "provider" field
    const parts = path.split('.');
    parts[parts.length - 1] = 'provider';
    const provPath = parts.join('.');
    const provName = getVal(provPath) || '';

    let select = '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">';
    select += '<option value="' + esc(val) + '" selected>' + esc(val || '— select —') + '</option>';
    select += '</select>';
    select += ' <button class="btn btn-sm" onclick="loadModels(\\'' + path + '\\', \\'' + esc(provName) + '\\')">Load Models</button>';
    return select;
}

function renderWorkflowSelect(val, path) {
    const workflows = CONFIG.image_generation?.comfyui_workflows || {};
    let opts = '<option value="">— None —</option>';
    for (const [wid, wf] of Object.entries(workflows)) {
        const name = wf.name || wid;
        opts += '<option value="' + esc(wid) + '"' + (wid === val ? ' selected' : '') + '>' + esc(name) + '</option>';
    }
    return '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">' + opts + '</select>';
}

function renderImagegenSelect(val, path) {
    const workflows = CONFIG.image_generation?.comfyui_workflows || {};
    const backends = CONFIG.image_generation?.backends || [];
    let opts = '<option value="">— Auto —</option>';
    // Workflow options
    if (Object.keys(workflows).length) {
        opts += '<optgroup label="Workflows">';
        for (const [wid, wf] of Object.entries(workflows)) {
            const v = 'workflow:' + (wf.name || wid);
            opts += '<option value="' + esc(v) + '"' + (v === val ? ' selected' : '') + '>' + esc(wf.name || wid) + '</option>';
        }
        opts += '</optgroup>';
    }
    // Backend options
    if (backends.length) {
        opts += '<optgroup label="Backends">';
        for (const be of backends) {
            const v = 'backend:' + be.name;
            opts += '<option value="' + esc(v) + '"' + (v === val ? ' selected' : '') + '>' + esc(be.name) + ' (' + esc(be.api_type || '') + ')</option>';
        }
        opts += '</optgroup>';
    }
    return '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">' + opts + '</select>';
}

function renderComfyBackendSelect(val, path, multi) {
    // ComfyUI backends are image_generation.backends where api_type === 'comfyui'
    const backends = (CONFIG.image_generation?.backends || []).filter(b => b.api_type === 'comfyui');
    if (multi) {
        // Multi-select: value is comma-separated string
        const selected = (val || '').split(',').map(s => s.trim()).filter(Boolean);
        let html = '<div id="f-' + path + '-wrap">';
        for (const be of backends) {
            const checked = selected.includes(be.name) ? 'checked' : '';
            html += '<label style="display:inline-flex; align-items:center; gap:4px; margin-right:12px; font-size:13px; color:#c9d1d9; cursor:pointer;">';
            html += '<input type="checkbox" value="' + esc(be.name) + '" ' + checked + ' onchange="updateMultiBackend(\\'' + path + '\\')">';
            html += esc(be.name) + '</label>';
        }
        if (!backends.length) html += '<span style="color:#6e7681; font-size:12px;">No ComfyUI backends configured</span>';
        html += '</div>';
        return html;
    }
    // Single select
    let opts = '<option value="">— Auto —</option>';
    for (const be of backends) {
        opts += '<option value="' + esc(be.name) + '"' + (be.name === val ? ' selected' : '') + '>' + esc(be.name) + '</option>';
    }
    return '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">' + opts + '</select>';
}

function renderImagegenBackendSelect(val, path) {
    // ALLE Image-Backends (ComfyUI, Together, CivitAI, Mammouth, ...)
    const backends = CONFIG.image_generation?.backends || [];
    let opts = '<option value="">— None —</option>';
    for (const be of backends) {
        const lbl = be.name + (be.api_type ? ' (' + be.api_type + ')' : '');
        opts += '<option value="' + esc(be.name) + '"' + (be.name === val ? ' selected' : '') + '>' + esc(lbl) + '</option>';
    }
    // onchange: setVal + Geschwister-Modell-Select neu fuellen falls vorhanden
    return '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value); refreshImagegenModelSelect(\\'' + path + '\\')">' + opts + '</select>';
}

// Geschwister-Modell-Select neu laden wenn Backend gewechselt wird
function refreshImagegenModelSelect(backendPath) {
    const parts = backendPath.split('.');
    parts[parts.length - 1] = 'model';
    const modelPath = parts.join('.');
    const modelEl = document.getElementById('f-' + modelPath);
    if (!modelEl) return;
    const backendName = getVal(backendPath) || '';
    if (!backendName) {
        modelEl.innerHTML = '<option value="">— Backend zuerst waehlen —</option>';
        return;
    }
    loadImagegenBackendModels(modelPath, backendName);
}

let IMAGEGEN_MODELS_CACHE = {};

async function loadImagegenBackendModels(path, backendName) {
    const sel = document.getElementById('f-' + path);
    if (!sel) return;
    const currentVal = sel.value || getVal(path) || '';
    if (!IMAGEGEN_MODELS_CACHE[backendName]) {
        sel.innerHTML = '<option>Loading...</option>';
        try {
            const resp = await fetch('/admin/settings/imagegen-backends/' + encodeURIComponent(backendName) + '/models',
                { credentials: 'same-origin' });
            const data = await resp.json();
            if (data.error) toast('Models laden fehlgeschlagen: ' + data.error, 'error');
            const list = data.models || [];
            if (list.length > 0) IMAGEGEN_MODELS_CACHE[backendName] = list;
        } catch (e) {
            toast('Models laden fehlgeschlagen: ' + e.message, 'error');
        }
    }
    const models = IMAGEGEN_MODELS_CACHE[backendName] || [];
    let opts = '<option value="">— Backend-Default —</option>';
    for (const m of models) {
        opts += '<option value="' + esc(m) + '"' + (m === currentVal ? ' selected' : '') + '>' + esc(m) + '</option>';
    }
    if (currentVal && !models.includes(currentVal)) {
        opts = '<option value="' + esc(currentVal) + '" selected>' + esc(currentVal) + ' (custom)</option>' + opts;
    }
    sel.innerHTML = opts;
}

// Kombinierte Auswahl: ComfyUI-Workflows + Cloud-Backends.
// Wert-Format: "workflow:<name>" oder "backend:<name>" (wie /workflows-Endpoint)
let IMAGEGEN_TARGETS_CACHE = null;

async function loadImagegenTargets() {
    if (IMAGEGEN_TARGETS_CACHE) return IMAGEGEN_TARGETS_CACHE;
    try {
        const r = await fetch('/admin/settings/imagegen-targets', { credentials: 'same-origin' });
        const d = await r.json();
        IMAGEGEN_TARGETS_CACHE = d.targets || [];
    } catch {
        IMAGEGEN_TARGETS_CACHE = [];
    }
    return IMAGEGEN_TARGETS_CACHE;
}

function renderImagegenTargetSelect(val, path) {
    // Initial mit aktuellem Wert rendern; Liste wird async nachgeladen
    let html = '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">';
    if (val) html += '<option value="' + esc(val) + '" selected>' + esc(val) + '</option>';
    html += '<option value="">— Auto (Cloud bevorzugt) —</option>';
    html += '</select>';
    // Async populate
    setTimeout(async () => {
        const targets = await loadImagegenTargets();
        const sel = document.getElementById('f-' + path);
        if (!sel) return;
        let opts = '<option value="">— Auto (Cloud bevorzugt) —</option>';
        for (const t of targets) {
            const dis = t.available ? '' : ' disabled';
            const tag = t.available ? '' : ' (offline)';
            const sl = t.value === val ? ' selected' : '';
            opts += '<option value="' + esc(t.value) + '"' + sl + dis + '>' + esc(t.label + tag) + '</option>';
        }
        sel.innerHTML = opts;
    }, 0);
    return html;
}

function renderImagegenModelSelect(val, path) {
    // Backend aus Geschwister-Feld lesen
    const parts = path.split('.');
    parts[parts.length - 1] = 'backend';
    const backendPath = parts.join('.');
    const backendName = getVal(backendPath) || '';
    let html = '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">';
    if (val) {
        html += '<option value="' + esc(val) + '" selected>' + esc(val) + '</option>';
    } else {
        html += '<option value="">— Backend-Default —</option>';
    }
    html += '</select>';
    html += ' <button class="btn btn-sm" onclick="loadImagegenBackendModels(\\'' + path + '\\', \\'' + esc(backendName) + '\\')">Load Models</button>';
    return html;
}

function updateMultiBackend(path) {
    const wrap = document.getElementById('f-' + path + '-wrap');
    if (!wrap) return;
    const checked = [...wrap.querySelectorAll('input[type=checkbox]:checked')].map(cb => cb.value);
    setVal(path, checked.join(','));
}

// ── ComfyUI Model / LoRA selects ──
let COMFY_CACHE = null; // {checkpoints: [], loras: []}

async function loadComfyModels() {
    if (COMFY_CACHE) return COMFY_CACHE;
    try {
        const resp = await fetch('/admin/settings/comfyui-models', { credentials: 'same-origin' });
        COMFY_CACHE = await resp.json();
    } catch (e) {
        COMFY_CACHE = { checkpoints: [], loras: [] };
    }
    return COMFY_CACHE;
}

function renderComfyModelSelect(val, path) {
    let html = '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">';
    html += '<option value="' + esc(val) + '" selected>' + esc(val || '— select —') + '</option>';
    html += '</select>';
    html += ' <button class="btn btn-sm" onclick="populateComfySelect(\\'' + path + '\\', \\'checkpoints\\')">Load Models</button>';
    return html;
}

async function populateComfySelect(path, type) {
    const cache = await loadComfyModels();
    const items = cache[type] || [];
    const sel = document.getElementById('f-' + path);
    if (!sel) return;
    const current = sel.value;
    let opts = '<option value="">— none —</option>';
    for (const m of items) {
        opts += '<option value="' + esc(m) + '"' + (m === current ? ' selected' : '') + '>' + esc(m) + '</option>';
    }
    sel.innerHTML = opts;
    if (current && !items.includes(current)) {
        sel.insertAdjacentHTML('afterbegin', '<option value="' + esc(current) + '" selected>' + esc(current) + ' (not on server)</option>');
    }
    if (!items.length) toast('No models found. Server running?', 'error');
}

function renderComfyClipSelect(val, path) {
    let html = '<select id="f-' + path + '" onchange="setVal(\\'' + path + '\\', this.value)">';
    html += '<option value="' + esc(val) + '" selected>' + esc(val || '— select —') + '</option>';
    html += '</select>';
    html += ' <button class="btn btn-sm" onclick="populateComfySelect(\\'' + path + '\\', \\'clip_models\\')">Load CLIP Models</button>';
    return html;
}

// ── Array/Dict Items ──
// _itemLabel: gleiche Logik wie in renderArrayItem — fuer Sortierung
function _itemLabel(item, labelField, fallback) {
    return String((item && item[labelField]) || fallback || '');
}

function renderArrayItems(def, items, path) {
    let html = '<div id="arr-' + path + '">';
    // Index erhalten (Pfade referenzieren echten Array-Index), Reihenfolge
    // alphabetisch wenn def.sort_alphabetically gesetzt ist.
    const order = items.map((it, i) => ({ idx: i, label: _itemLabel(it, def.item_label_field, 'Item ' + i) }));
    if (def.sort_alphabetically) {
        order.sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: 'base' }));
    }
    for (const o of order) {
        html += renderArrayItem(def, items[o.idx], path + '[' + o.idx + ']', o.idx, def.item_label_field);
    }
    html += '</div>';
    return html;
}

function renderDictItems(def, items, path) {
    let html = '<div id="arr-' + path + '">';
    const entries = Object.entries(items).map(([k, item]) => ({ key: k, item, label: _itemLabel(item, def.item_label_field, k) }));
    if (def.sort_alphabetically) {
        entries.sort((a, b) => a.label.localeCompare(b.label, undefined, { sensitivity: 'base' }));
    }
    for (const e of entries) {
        html += renderArrayItem(def, e.item, path + '.' + e.key, e.key, def.item_label_field);
    }
    html += '</div>';
    return html;
}

function renderArrayItem(def, item, path, index, labelField) {
    const label = item[labelField] || ('Item ' + index);
    let html = '<div class="array-item" id="item-' + path + '">';
    html += '<div class="array-item-header" onclick="this.parentElement.classList.toggle(\\'open\\')">';
    html += '<span class="chevron">▶</span> ';
    html += '<span class="title" style="margin-left:6px;">' + esc(label) + '</span>';
    if (item.enabled === false) html += '<span class="badge">deaktiviert</span>';
    if (item.type) html += '<span class="badge">' + esc(item.type || item.api_type || '') + '</span>';
    html += '<button class="btn btn-sm" style="margin-left:8px;" title="Als neuen Eintrag duplizieren" onclick="event.stopPropagation(); duplicateItem(\\'' + path + '\\')">⧉</button>';
    html += '<button class="btn btn-sm btn-danger" style="margin-left:4px;" onclick="event.stopPropagation(); removeItem(\\'' + path + '\\')">✕</button>';
    html += '</div>';
    html += '<div class="array-item-body">';
    html += renderFields(def.fields, item, path);
    html += '</div></div>';
    return html;
}

// ── Task/Order List (llm_routing.tasks) ──
let LLM_TASKS_CACHE = null;

async function loadLlmTasks(forceRefresh) {
    if (LLM_TASKS_CACHE && !forceRefresh) return LLM_TASKS_CACHE;
    try {
        // cache-bust per Query-Param damit Browser nicht aus dem HTTP-Cache
        // serviert (z.B. nach Server-Neustart mit neuen Sub-Tasks).
        const resp = await fetch('/admin/settings/llm-tasks?_=' + Date.now(),
            { credentials: 'same-origin', cache: 'no-store' });
        LLM_TASKS_CACHE = await resp.json();
    } catch (e) {
        LLM_TASKS_CACHE = [];
    }
    return LLM_TASKS_CACHE;
}

function renderTaskOrderList(items, path, f) {
    // items: [{task: 'chat_stream', order: 1}, ...]
    let html = '<div class="field"><label>' + f.label + '</label><div class="input-wrap">';
    if (f.description) html += '<div class="desc" style="margin-bottom:6px;">' + f.description + '</div>';
    html += '<div id="tasks-' + path + '">';
    for (let i = 0; i < items.length; i++) {
        html += renderTaskOrderRow(items[i] || {}, path, i);
    }
    html += '</div>';
    html += '<div style="margin-top:6px; display:flex; flex-wrap:wrap; gap:4px;">';
    html += '<button class="btn btn-sm" onclick="addTaskOrderRow(\\'' + path + '\\')">+ Task</button>';
    html += '<button class="btn btn-sm" title="Add all Image-Input tasks not yet assigned" onclick="addTaskGroup(\\'' + path + '\\', \\'image\\')">+ All Image</button>';
    html += '<button class="btn btn-sm" title="Add all Tool tasks not yet assigned" onclick="addTaskGroup(\\'' + path + '\\', \\'tool\\')">+ All Tools</button>';
    html += '<button class="btn btn-sm" title="Add all Large Chat Model tasks not yet assigned" onclick="addTaskGroup(\\'' + path + '\\', \\'chat\\')">+ All Chat</button>';
    html += '<button class="btn btn-sm" title="Add all Small Helper tasks not yet assigned" onclick="addTaskGroup(\\'' + path + '\\', \\'helper\\')">+ All Helper</button>';
    html += '</div>';
    html += '</div></div>';
    // Async: Dropdowns fuellen nachdem DOM da ist
    setTimeout(() => populateTaskSelects(path), 0);
    return html;
}

function renderTaskOrderRow(item, path, i) {
    const task = item.task || '';
    const order = (item.order !== undefined ? item.order : 1);
    let html = '<div class="gpu-row" id="taskrow-' + path + '-' + i + '">';
    html += '<select data-taskrow="' + path + '-' + i + '" style="flex:3;" onchange="setVal(\\'' + path + '[' + i + '].task\\', this.value)">';
    html += '<option value="' + esc(task) + '" selected>' + esc(task || '— select —') + '</option>';
    html += '</select>';
    html += '<input type="number" value="' + order + '" min="1" step="1" style="max-width:70px;" title="Order" onchange="setVal(\\'' + path + '[' + i + '].order\\', parseInt(this.value) || 1)">';
    html += '<button class="btn btn-sm btn-danger" onclick="removeTaskOrderRow(\\'' + path + '\\', ' + i + ')">✕</button>';
    html += '</div>';
    return html;
}

async function populateTaskSelects(path) {
    const tasks = await loadLlmTasks();
    // Group tasks by category for guidance — show grouped <optgroup>s in the dropdown.
    const order = ['image', 'tool', 'chat', 'helper', ''];
    const grouped = {};
    for (const t of tasks) {
        const cat = t.category || '';
        (grouped[cat] = grouped[cat] || []).push(t);
    }
    const selects = document.querySelectorAll('select[data-taskrow^="' + path + '-"]');
    selects.forEach(sel => {
        const current = sel.value;
        let opts = '<option value="">— select —</option>';
        for (const cat of order) {
            const list = grouped[cat];
            if (!list || !list.length) continue;
            const groupLabel = list[0].category_label || 'Other';
            opts += '<optgroup label="' + esc(groupLabel) + '">';
            for (const t of list) {
                opts += '<option value="' + esc(t.id) + '"' + (t.id === current ? ' selected' : '') + '>'
                     + esc(t.label) + ' — ' + esc(t.id) + '</option>';
            }
            opts += '</optgroup>';
        }
        sel.innerHTML = opts;
    });
}

function addTaskOrderRow(path) {
    const obj = _ensureContainer(path, 'array');
    // order=1 is the default primary slot. Increase only when this LLM is meant
    // as a fallback for a task another LLM already serves at order=1.
    obj.push({ task: '', order: 1 });
    rerenderTaskOrderList(path);
}

async function addTaskGroup(path, category) {
    const tasks = await loadLlmTasks();
    const obj = _ensureContainer(path, 'array');
    const existing = new Set((obj || []).map(it => it && it.task).filter(Boolean));
    let added = 0;
    for (const t of tasks) {
        if (t.category !== category) continue;
        if (existing.has(t.id)) continue;
        obj.push({ task: t.id, order: 1 });
        added++;
    }
    rerenderTaskOrderList(path);
    if (added) toast('Added ' + added + ' task' + (added === 1 ? '' : 's'), 'success');
    else toast('All tasks of this group are already assigned', 'success');
}

function removeTaskOrderRow(path, index) {
    const parts = parsePath(path);
    let obj = CONFIG;
    for (const p of parts) obj = obj[p];
    obj.splice(index, 1);
    rerenderTaskOrderList(path);
}

function rerenderTaskOrderList(path) {
    // Re-render nur den Tasks-Container statt die ganze Section — damit
    // das umgebende Array-Item offen bleibt.
    const parts = parsePath(path);
    let obj = CONFIG;
    for (const p of parts) obj = obj && obj[p];
    const items = Array.isArray(obj) ? obj : [];
    const wrap = document.getElementById('tasks-' + path);
    if (!wrap) { renderSection(ACTIVE_SECTION); return; }
    let html = '';
    for (let i = 0; i < items.length; i++) {
        html += renderTaskOrderRow(items[i] || {}, path, i);
    }
    wrap.innerHTML = html;
    populateTaskSelects(path);
    // Sichtweise rechts mit aktualisieren wenn wir im llm_routing-Tab sind
    if (ACTIVE_SECTION === 'llm_routing') {
        renderLlmTaskView(CONFIG.llm_routing || []);
    }
}

// ── GPU Field ──
function renderGpuField(gpus, path) {
    let html = '<div class="field"><label>GPUs</label><div class="input-wrap">';
    html += '<div id="gpu-' + path + '">';
    for (let i = 0; i < gpus.length; i++) {
        const g = gpus[i];
        html += '<div class="gpu-row">';
        html += '<input type="text" value="' + esc(g.label || '') + '" placeholder="Label" style="max-width:120px;" onchange="setVal(\\'' + path + '[' + i + '].label\\', this.value)">';
        html += '<input type="number" value="' + (g.vram_gb || 0) + '" placeholder="VRAM GB" style="max-width:80px;" onchange="setVal(\\'' + path + '[' + i + '].vram_gb\\', parseInt(this.value))">';
        html += '<input type="text" value="' + esc(g.match_name || '') + '" placeholder="Match-Name (z.B. 4070)" title="Case-insensitive Substring im Beszel-GPU-Namen — wird zuerst probiert (stabil ueber Reboots)" style="max-width:140px;" onchange="setVal(\\'' + path + '[' + i + '].match_name\\', this.value)">';
        html += '<input type="text" value="' + esc(g.device || '') + '" placeholder="Device (Fallback)" title="Beszel device-id — nur noetig wenn Match-Name nicht eindeutig greift (z.B. zwei gleiche Modelle, oder Beszel meldet falschen Namen)" style="max-width:100px;opacity:0.7;" onchange="setVal(\\'' + path + '[' + i + '].device\\', this.value)">';
        const typesStr = Array.isArray(g.types) ? g.types.join(',') : (g.types || '');
        html += '<input type="text" value="' + esc(typesStr) + '" placeholder="ollama,openai,comfyui" onchange="setVal(\\'' + path + '[' + i + '].types\\', this.value.split(\\',\\').map(s=>s.trim()))">';
        html += '<input type="number" value="' + (g.max_concurrent || 1) + '" placeholder="MC" title="Max Concurrent" min="1" max="50" style="max-width:55px;" onchange="setVal(\\'' + path + '[' + i + '].max_concurrent\\', parseInt(this.value) || 1)">';
        html += '<button class="btn btn-sm btn-danger" onclick="removeSubItem(\\'' + path + '\\', ' + i + ')">✕</button>';
        html += '</div>';
    }
    html += '</div>';
    html += '<button class="btn btn-sm" style="margin-top:4px;" onclick="addGpu(\\'' + path + '\\')">+ GPU</button>';
    html += '</div></div>';
    return html;
}

// ── LoRA Field ──
function renderLoraField(loras, path, maxItems) {
    let html = '<div class="field"><label>LoRAs</label><div class="input-wrap">';
    for (let i = 0; i < maxItems; i++) {
        const l = loras[i] || { file: '', strength: 1 };
        const selId = 'lora-' + path + '-' + i;
        html += '<div class="lora-row">';
        html += '<select id="' + selId + '" style="flex:3;" onchange="setLoraVal(\\'' + path + '\\', ' + i + ', \\'file\\', this.value)">';
        html += '<option value="">— none —</option>';
        if (l.file) html += '<option value="' + esc(l.file) + '" selected>' + esc(l.file) + '</option>';
        html += '</select>';
        html += '<input type="number" value="' + (l.strength || 1) + '" step="0.1" min="0" max="2" style="flex:1; max-width:80px;" onchange="setLoraVal(\\'' + path + '\\', ' + i + ', \\'strength\\', parseFloat(this.value))">';
        html += '</div>';
    }
    html += '<button class="btn btn-sm" style="margin-top:4px;" onclick="populateLoraSelects(\\'' + path + '\\', ' + maxItems + ')">Load LoRAs</button>';
    html += '</div></div>';
    return html;
}

async function populateLoraSelects(path, maxItems) {
    const cache = await loadComfyModels();
    const items = cache.loras || [];
    if (!items.length) { toast('No LoRAs found. Server running?', 'error'); return; }
    for (let i = 0; i < maxItems; i++) {
        const sel = document.getElementById('lora-' + path + '-' + i);
        if (!sel) continue;
        const current = sel.value;
        let opts = '<option value="">— none —</option>';
        for (const m of items) {
            opts += '<option value="' + esc(m) + '"' + (m === current ? ' selected' : '') + '>' + esc(m) + '</option>';
        }
        sel.innerHTML = opts;
        if (current && !items.includes(current)) {
            sel.insertAdjacentHTML('afterbegin', '<option value="' + esc(current) + '" selected>' + esc(current) + '</option>');
        }
    }
    toast(items.length + ' LoRAs loaded', 'success');
}

// ── Data Access ──
function setVal(path, value) {
    const parts = parsePath(path);
    let obj = CONFIG;
    for (let i = 0; i < parts.length - 1; i++) {
        const p = parts[i];
        if (obj[p] === undefined) {
            obj[p] = (typeof parts[i+1] === 'number') ? [] : {};
        }
        obj = obj[p];
    }
    obj[parts[parts.length - 1]] = value;
}

function getVal(path) {
    const parts = parsePath(path);
    let obj = CONFIG;
    for (const p of parts) {
        if (obj === undefined || obj === null) return undefined;
        obj = obj[p];
    }
    return obj;
}

function parsePath(path) {
    // "providers[0].name" => ["providers", 0, "name"]
    const result = [];
    for (const part of path.split('.')) {
        const m = part.match(/^([^\\[]+)(?:\\[(\\d+)\\])?$/);
        if (m) {
            result.push(m[1]);
            if (m[2] !== undefined) result.push(parseInt(m[2]));
        } else {
            result.push(part);
        }
    }
    return result;
}

function setLoraVal(path, index, field, value) {
    const parts = parsePath(path);
    let obj = CONFIG;
    for (const p of parts) {
        if (obj[p] === undefined) obj[p] = [];
        obj = obj[p];
    }
    while (obj.length <= index) obj.push({ file: '', strength: 1 });
    obj[index][field] = value;
}

// Walks `path` inside CONFIG, creating any missing levels. Intermediate levels
// are always created as {}; only the leaf takes the requested `leafType`
// ('array' or 'dict'). Returns the leaf container.
function _ensureContainer(path, leafType) {
    const parts = parsePath(path);
    let obj = CONFIG;
    for (let i = 0; i < parts.length; i++) {
        const p = parts[i];
        if (obj[p] === undefined) {
            obj[p] = (i === parts.length - 1)
                ? (leafType === 'dict' ? {} : [])
                : {};
        }
        obj = obj[p];
    }
    return obj;
}

// ── Actions ──
// Defaults pro image_model fuer neue ComfyUI-Workflows. Werte 1:1 aus den
// produktiv erprobten Hotopia-Workflows uebernommen — der Admin muss nicht
// jedes Mal Negative Prompt / Style / Enhancer-Instruction von Hand setzen.
const WORKFLOW_DEFAULTS = {
    qwen: {
        prompt_style: 'photograph, shot on iPhone 15 Pro, natural window light, skin texture, unedited, detailed anatomy, 8k, high detail, \\n',
        prompt_negative: 'illustration, anime, cgi, 3d render, painting, airbrushed skin, plastic skin, smooth flawless skin, overexposed, glossy, fantasy, studio lighting, posed, cartoon, drawing, sketch, watermark, signature, text, logo, deformed, blurry, low quality\\n',
        prompt_instruction: 'Write a natural-language descriptive prompt (not tags). Describe the scene as a flowing sentence with rich detail about the setting, characters, poses, and mood. Avoid comma-separated tag lists.',
    },
    z_image: {
        prompt_style: 'RAW photo, amateur photograph, 35mm, natural light, skin texture, visible pores, detailed anatomy, 8k, high detail, \\n',
        prompt_negative: 'illustration, anime, cgi, 3d render, painting, airbrushed skin, plastic skin, smooth flawless skin, overexposed, glossy, fantasy, studio lighting, posed, cartoon, drawing, sketch, watermark, signature, text, logo, deformed, blurry, low quality\\n',
        prompt_instruction: 'Write a tag-based prompt with comma-separated keywords. Use quality tags like "masterpiece, best quality". Describe pose, lighting, and setting as short tags.',
    },
    flux: {
        prompt_style: 'a candid photograph taken with a 35mm lens, natural indoor lighting, skin with visible pores and texture, detailed anatomy, 8k, high detail, ',
        prompt_negative: 'illustration, anime, cgi, 3d render, painting, airbrushed skin, plastic skin, smooth flawless skin, overexposed, glossy, fantasy, studio lighting, posed, cartoon, drawing, sketch, watermark, signature, text, logo, deformed, blurry, low quality\\n',
        prompt_instruction: 'Write a natural-language descriptive prompt for a Flux 2 Klein model. Describe the scene in flowing detail — subject, pose, environment, lighting, mood. Flux understands natural language well, so be descriptive and avoid tag lists.',
    },
};

function _detectImageModelFromId(id) {
    const u = String(id || '').toUpperCase();
    if (u.includes('QWEN')) return 'qwen';
    if (u.includes('Z-IMAGE') || u.includes('Z_IMAGE') || u.includes('ZIMAGE')) return 'z_image';
    if (u.includes('FLUX')) return 'flux';
    return '';
}

function addArrayItem(path, type) {
    const obj = _ensureContainer(path, type);
    if (type === 'dict') {
        const id = prompt('Workflow ID (e.g. FLUX, QWEN, Z-IMAGE):');
        if (!id) return;
        // Modell-Type aus ID raten und Defaults uebernehmen.
        const detectedModel = _detectImageModelFromId(id);
        const defaults = WORKFLOW_DEFAULTS[detectedModel] || {};
        obj[id] = {
            name: id,
            loras: [{file:'',strength:1},{file:'',strength:1},{file:'',strength:1},{file:'',strength:1}],
            ...(detectedModel ? { image_model: detectedModel } : {}),
            ...defaults,
        };
    } else {
        if (path === 'llm_routing') {
            obj.push({ enabled: true, preload_on_startup: false, provider: '', model: '', temperature: 0.7, tasks: [] });
        } else {
            obj.push({ name: 'New', enabled: true, gpus: [] });
        }
    }
    renderSection(ACTIVE_SECTION);
}

function removeItem(path) {
    if (!confirm('Remove this item?')) return;
    const parts = parsePath(path);
    let obj = CONFIG;
    for (let i = 0; i < parts.length - 1; i++) {
        obj = obj[parts[i]];
    }
    const last = parts[parts.length - 1];
    if (typeof last === 'number') {
        obj.splice(last, 1);
    } else {
        delete obj[last];
    }
    renderSection(ACTIVE_SECTION);
}

// Dupliziert einen Array- oder Dict-Eintrag (LLM-Routing, Backends,
// ComfyUI-Workflows etc.). Bei Dicts wird ein neuer Key abgefragt; bei
// Arrays wird der Klon ans Ende angehaengt. `name`-Felder bekommen ein
// "(Kopie)"-Suffix, damit der duplizierte Eintrag direkt unterscheidbar ist.
function duplicateItem(path) {
    const parts = parsePath(path);
    let parent = CONFIG;
    for (let i = 0; i < parts.length - 1; i++) {
        parent = parent[parts[i]];
    }
    const last = parts[parts.length - 1];
    const original = (typeof last === 'number') ? parent[last] : parent[last];
    if (!original) { toast('Eintrag nicht gefunden', 'error'); return; }
    // Deep clone — Defaults sollen nicht mit dem Original geteilt werden.
    const copy = JSON.parse(JSON.stringify(original));
    if (copy && typeof copy === 'object' && 'name' in copy && copy.name) {
        copy.name = String(copy.name) + ' (Kopie)';
    }
    if (typeof last === 'number') {
        // Array: direkt hinter Original einfuegen
        parent.splice(last + 1, 0, copy);
    } else {
        // Dict: neuen Key vom User abfragen
        const newKey = prompt('Neuer Schluessel fuer den Klon:', String(last) + '_copy');
        if (!newKey) return;
        if (parent[newKey] !== undefined) { toast('Schluessel existiert bereits: ' + newKey, 'error'); return; }
        parent[newKey] = copy;
    }
    renderSection(ACTIVE_SECTION);
}

function removeSubItem(path, index) {
    const parts = parsePath(path);
    let obj = CONFIG;
    for (const p of parts) obj = obj[p];
    obj.splice(index, 1);
    renderSection(ACTIVE_SECTION);
}

function addGpu(path) {
    const parts = parsePath(path);
    let obj = CONFIG;
    for (const p of parts) {
        if (obj[p] === undefined) obj[p] = [];
        obj = obj[p];
    }
    obj.push({ vram_gb: 0, types: ['openai'], match_name: '', device: '' });
    renderSection(ACTIVE_SECTION);
}

async function loadModels(path, provName) {
    if (!provName) {
        // Try to detect from sibling
        const parts = path.split('.');
        parts[parts.length - 1] = 'provider';
        provName = getVal(parts.join('.'));
    }
    if (!provName) { toast('Select a provider first', 'error'); return; }

    const sel = document.getElementById('f-' + path);
    if (!sel) return;
    const currentVal = sel.value;

    // Cache: leere Listen NICHT cachen (sonst blockt eine fehlgeschlagene
    // Abfrage alle Retry-Versuche bis zum Page-Reload).
    if (!PROVIDERS_CACHE[provName] || PROVIDERS_CACHE[provName].length === 0) {
        sel.innerHTML = '<option>Loading...</option>';
        try {
            const resp = await fetch('/admin/settings/providers/' + encodeURIComponent(provName) + '/models', { credentials: 'same-origin' });
            const data = await resp.json();
            if (data.error) { toast('Error: ' + data.error, 'error'); }
            const list = data.models || [];
            if (list.length > 0) {
                PROVIDERS_CACHE[provName] = list;
            } else {
                delete PROVIDERS_CACHE[provName];
            }
        } catch (e) {
            toast('Failed to load models: ' + e.message, 'error');
            delete PROVIDERS_CACHE[provName];
        }
    }

    const models = PROVIDERS_CACHE[provName];
    let opts = '<option value="">— select —</option>';
    for (const m of models) {
        opts += '<option value="' + esc(m) + '"' + (m === currentVal ? ' selected' : '') + '>' + esc(m) + '</option>';
    }
    sel.innerHTML = opts;
    if (currentVal && !models.includes(currentVal)) {
        sel.innerHTML = '<option value="' + esc(currentVal) + '" selected>' + esc(currentVal) + ' (not on server)</option>' + opts;
    }
}

function refreshModelSelect(provPath) {
    // When provider changes, clear model cache
    const parts = provPath.split('.');
    parts[parts.length - 1] = 'model';
    const modelPath = parts.join('.');
    const provName = getVal(provPath);
    if (provName) loadModels(modelPath, provName);
}

async function validateConfig() {
    const btn = document.getElementById('btn-validate');
    btn.disabled = true;
    btn.textContent = 'Validating...';
    try {
        const resp = await fetch('/admin/settings/validate', {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify(CONFIG)
        });
        const result = await resp.json();
        const issues = result.issues || [];
        const content = document.getElementById('content');

        let html = '<div class="validate-results ' + (result.errors > 0 ? 'has-errors' : 'all-ok') + '">';
        if (issues.length === 0) {
            html += '<h3>Keine Probleme gefunden</h3>';
        } else {
            html += '<h3>' + result.errors + ' Fehler, ' + result.warnings + ' Warnungen</h3>';
            for (const issue of issues) {
                html += '<div class="validate-issue ' + issue.level + '">';
                html += '<span class="badge">' + (issue.level === 'error' ? 'ERROR' : 'WARN') + '</span>';
                html += '<span>' + esc(issue.message) + '</span>';
                html += '<span class="section-link" onclick="activateSection(\\'' + issue.section + '\\')">' + issue.section + '</span>';
                html += '</div>';
            }
        }
        html += '</div>';

        // Show below current section or as standalone
        if (ACTIVE_SECTION && !ACTIVE_SECTION.startsWith('_')) {
            content.insertAdjacentHTML('afterbegin', html);
        } else {
            content.innerHTML = html;
        }
        if (result.errors > 0) toast(result.errors + ' Fehler gefunden', 'error');
        else if (result.warnings > 0) toast(result.warnings + ' Warnungen', 'success');
        else toast('Alles OK!', 'success');
    } catch (e) {
        toast('Validation failed: ' + e.message, 'error');
    }
    btn.disabled = false;
    btn.textContent = 'Validate';
}

// Generischer Action-Button-Handler — schickt POST/DELETE/etc an einen Endpoint
// mit Body aus angegebenen Geschwister-Feldern. Genutzt von schema-Type "button".
async function runActionButton(endpoint, method, path, bodyFrom, confirmMsg, btn, previewUrl) {
    if (confirmMsg && !confirm(confirmMsg)) return;
    const body = {};
    // Werte aus DOM lesen (frischste Quelle — auch wenn User getippt aber
    // noch nicht gespeichert hat). Fallback auf CONFIG, dann auf
    // f-input-element.value als letzten Strohhalm fuer Defaults.
    for (const fld of (bodyFrom || [])) {
        const sibling = path + '.' + fld;
        let v = undefined;
        // 1. Versuche das DOM-Input direkt
        const el = document.getElementById('f-' + sibling);
        if (el && 'value' in el) {
            v = el.value;
        }
        // 2. Fallback: gespeicherter CONFIG-Wert
        if (v === undefined || v === null || v === '') {
            v = getVal(sibling);
        }
        if (v !== undefined && v !== null && v !== '') body[fld] = v;
    }
    const origLabel = btn.textContent;
    btn.disabled = true;
    btn.textContent = '⏳ ' + origLabel;
    try {
        const opts = { method, headers: authHeaders(), credentials: 'same-origin' };
        if (method !== 'GET' && method !== 'DELETE') {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body);
        }
        const resp = await fetch(endpoint, opts);
        const data = await resp.json().catch(() => ({}));
        if (resp.ok) {
            const detail = data.bbox ? ` (bbox ${data.bbox.w}×${data.bbox.h})` : '';
            toast((data.status || 'OK') + detail, 'success');
            // Preview-Bild neu laden (Cache-Bust via Timestamp) und Meta refreshen
            if (previewUrl) {
                document.querySelectorAll('img[src^="' + previewUrl + '"]').forEach(img => {
                    img.src = previewUrl + '?_=' + Date.now();
                    img.style.display = '';
                    if (img.nextElementSibling) img.nextElementSibling.style.display = 'none';
                });
                if (typeof populateImagePreviewMetas === 'function') {
                    populateImagePreviewMetas();
                }
            }
        } else {
            toast('Fehler: ' + (data.detail || data.error || resp.status), 'error');
        }
    } catch (e) {
        toast('Aufruf fehlgeschlagen: ' + e.message, 'error');
    }
    btn.disabled = false;
    btn.textContent = origLabel;
}

async function saveConfig() {
    const btn = document.getElementById('btn-save');
    btn.disabled = true;
    btn.textContent = 'Saving...';
    try {
        const resp = await fetch('/admin/settings/save', {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify(CONFIG)
        });
        const result = await resp.json();
        if (resp.ok) {
            // URL/Key-Aenderungen sollen sofort greifen, ohne Page-Reload.
            // Provider-Model-Cache + ComfyUI-Model-Cache invalidieren.
            for (const k of Object.keys(PROVIDERS_CACHE)) delete PROVIDERS_CACHE[k];
            COMFY_CACHE = null;
            toast(result.message || 'Saved!', 'success');
        } else {
            toast('Error: ' + (result.detail || result.message), 'error');
        }
    } catch (e) {
        toast('Save failed: ' + e.message, 'error');
    }
    btn.disabled = false;
    btn.textContent = 'Save';
}

async function reloadServices() {
    if (!confirm('Reload all services? Active requests may be interrupted.')) return;
    try {
        const resp = await fetch('/admin/settings/restart', {
            method: 'POST',
            headers: authHeaders()
        });
        const result = await resp.json();
        toast('Services reloaded', 'success');
    } catch (e) {
        toast('Reload failed: ' + e.message, 'error');
    }
}

function togglePw(btn) {
    const input = btn.parentElement.querySelector('input');
    input.type = input.type === 'password' ? 'text' : 'password';
}

// ── Helpers ──
function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function toast(msg, type) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast ' + type + ' show';
    setTimeout(() => t.classList.remove('show'), 3000);
}

// Start
init();
</script>
</body>
</html>'''


def _build_users_html() -> str:
    """User-Verwaltungs-Seite (Admin-only)."""
    return '''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<title>User-Verwaltung</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }
h1 { font-size: 18px; margin-bottom: 16px; color: #e6edf3; }
.toolbar { margin-bottom: 16px; }
.btn { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; }
.btn:hover { background: #30363d; }
.btn-primary { background: #238636; border-color: #2ea043; color: #fff; }
.btn-primary:hover { background: #2ea043; }
.btn-danger { background: #da3633; border-color: #f85149; color: #fff; }
.btn-danger:hover { background: #b62324; }
.btn-sm { padding: 4px 8px; font-size: 12px; }

table { width: 100%; border-collapse: collapse; background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }
th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid #30363d; }
th { background: #1c2128; font-size: 12px; color: #8b949e; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }
tr:last-child td { border-bottom: none; }
tr:hover { background: #1c2128; }
.role-admin { color: #58a6ff; font-weight: 600; }
.role-user { color: #8b949e; }
.chars { font-size: 11px; color: #8b949e; }
td .actions { display: flex; gap: 6px; }

.modal-bg { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: none; align-items: center; justify-content: center; z-index: 1000; }
.modal-bg.show { display: flex; }
.modal { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; width: 480px; max-width: 92vw; max-height: 90vh; overflow-y: auto; }
.modal h2 { font-size: 16px; margin-bottom: 14px; color: #e6edf3; }
.field { margin-bottom: 12px; }
.field label { display: block; font-size: 12px; color: #8b949e; margin-bottom: 4px; }
.field input, .field select { width: 100%; background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; padding: 6px 10px; border-radius: 6px; font-size: 13px; }
.field input:focus, .field select:focus { border-color: #58a6ff; outline: none; }
.chars-box {
    max-height: 160px; overflow-y: auto; overflow-x: hidden;
    border: 1px solid #30363d; padding: 8px; border-radius: 6px;
    background: #0d1117;
}
/* Eigene Klasse statt Tag-Selector — kein Cascading-Risiko */
.char-row {
    display: block !important;
    width: 100% !important;
    padding: 3px 0 !important;
    margin: 0 !important;
    font-size: 12px;
    color: #c9d1d9 !important;
    cursor: pointer;
    text-align: left !important;
    white-space: nowrap;
    line-height: 1.6;
}
.char-row > input[type="checkbox"] {
    display: inline-block !important;
    margin: 0 8px 0 0 !important;
    padding: 0 !important;
    vertical-align: middle !important;
    float: none !important;
    width: auto !important;
    min-width: 0 !important;
}
.char-row > span {
    display: inline-block !important;
    vertical-align: middle !important;
    color: #c9d1d9 !important;
}
.modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px; border-top: 1px solid #30363d; padding-top: 12px; }
.error-msg { color: #f85149; font-size: 12px; margin-bottom: 8px; display: none; }
.toast { position: fixed; bottom: 20px; right: 20px; background: #238636; color: #fff; padding: 10px 16px; border-radius: 6px; font-size: 13px; opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 2000; }
.toast.show { opacity: 1; }
.toast.error { background: #da3633; }
</style>
</head>
<body>

<h1>User-Verwaltung</h1>
<div class="toolbar">
    <button class="btn btn-primary" onclick="openEdit(null)">+ Neuer User</button>
</div>

<table id="users-table">
    <thead>
        <tr><th>Benutzername</th><th>Rolle</th><th>Characters</th><th>Letzter Login</th><th></th></tr>
    </thead>
    <tbody id="users-tbody">
        <tr><td colspan="5" style="text-align:center;color:#8b949e;">Lade...</td></tr>
    </tbody>
</table>

<div class="modal-bg" id="modal-bg">
    <div class="modal">
        <h2 id="modal-title">User anlegen</h2>
        <div class="error-msg" id="modal-error"></div>
        <div class="field">
            <label>Benutzername</label>
            <input type="text" id="edit-username" autocomplete="off">
        </div>
        <div class="field">
            <label>Rolle</label>
            <select id="edit-role">
                <option value="user">User</option>
                <option value="admin">Admin</option>
            </select>
        </div>
        <div class="field">
            <label id="edit-password-label">Passwort</label>
            <input type="password" id="edit-password" autocomplete="new-password">
        </div>
        <div class="field">
            <label style="display:flex;align-items:center;gap:8px;">
                Zugeordnete Characters
                <button type="button" class="btn btn-sm" onclick="toggleAllChars(true)">Alle</button>
                <button type="button" class="btn btn-sm" onclick="toggleAllChars(false)">Keiner</button>
            </label>
            <div class="chars-box" id="edit-chars-box"></div>
        </div>
        <div class="modal-actions">
            <button class="btn" onclick="closeEdit()">Abbrechen</button>
            <button class="btn btn-primary" onclick="saveEdit()">Speichern</button>
        </div>
    </div>
</div>

<div class="toast" id="toast"></div>

<script>
let USERS = [];
let CHARS = [];
let EDIT_ID = null;

async function loadAll() {
    try {
        const [uResp, cResp] = await Promise.all([
            fetch('/auth/users'),
            fetch('/characters/list'),
        ]);
        if (uResp.status === 401 || uResp.status === 403) {
            const ret = encodeURIComponent(window.location.pathname);
            window.location.href = '/?return=' + ret;
            return;
        }
        USERS = (await uResp.json()).users || [];
        CHARS = (await cResp.json()).characters || [];
        renderTable();
    } catch (e) {
        toast('Fehler beim Laden: ' + e.message, 'error');
    }
}

function renderTable() {
    const tb = document.getElementById('users-tbody');
    if (!USERS.length) {
        tb.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#8b949e;">Keine User</td></tr>';
        return;
    }
    tb.innerHTML = USERS.map(u => {
        const charList = (u.allowed_characters || []).join(', ') || '—';
        const roleClass = u.role === 'admin' ? 'role-admin' : 'role-user';
        return '<tr>' +
            '<td>' + escapeHtml(u.username) + '</td>' +
            '<td class="' + roleClass + '">' + escapeHtml(u.role) + '</td>' +
            '<td class="chars">' + escapeHtml(charList) + '</td>' +
            '<td>' + escapeHtml(u.last_login || '—') + '</td>' +
            '<td class="actions">' +
                '<button class="btn btn-sm" onclick="openEdit(\\'' + u.id + '\\')">Edit</button>' +
                '<button class="btn btn-sm btn-danger" onclick="deleteUser(\\'' + u.id + '\\')">Del</button>' +
            '</td>' +
        '</tr>';
    }).join('');
}

function openEdit(userId) {
    EDIT_ID = userId;
    const u = userId ? USERS.find(x => x.id === userId) : null;
    document.getElementById('modal-title').textContent = u ? 'User bearbeiten' : 'User anlegen';
    document.getElementById('edit-username').value = u ? u.username : '';
    document.getElementById('edit-role').value = u ? u.role : 'user';
    document.getElementById('edit-password').value = '';
    document.getElementById('edit-password-label').textContent = u ? 'Passwort (leer = nicht aendern)' : 'Passwort';
    document.getElementById('modal-error').style.display = 'none';

    const assigned = new Set(u ? u.allowed_characters : []);
    document.getElementById('edit-chars-box').innerHTML = CHARS.map(c =>
        '<label class="char-row"><input type="checkbox" value="' + escapeHtml(c) + '"' + (assigned.has(c) ? ' checked' : '') + '><span>' + escapeHtml(c) + '</span></label>'
    ).join('');
    document.getElementById('modal-bg').classList.add('show');
}

function toggleAllChars(checked) {
    document.querySelectorAll('#edit-chars-box input[type="checkbox"]').forEach(cb => { cb.checked = !!checked; });
}

function closeEdit() {
    document.getElementById('modal-bg').classList.remove('show');
    EDIT_ID = null;
}

async function saveEdit() {
    const username = document.getElementById('edit-username').value.trim();
    const role = document.getElementById('edit-role').value;
    const password = document.getElementById('edit-password').value;
    const chars = Array.from(document.querySelectorAll('#edit-chars-box input:checked')).map(i => i.value);
    const err = document.getElementById('modal-error');
    err.style.display = 'none';

    if (!username) { err.textContent = 'Benutzername erforderlich'; err.style.display = 'block'; return; }
    if (!EDIT_ID && !password) { err.textContent = 'Passwort erforderlich'; err.style.display = 'block'; return; }

    try {
        let resp;
        if (EDIT_ID) {
            const body = { username, role, allowed_characters: chars };
            if (password) body.password = password;
            resp = await fetch('/auth/users/' + EDIT_ID, {
                method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
            });
        } else {
            resp = await fetch('/auth/users', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ username, password, role, allowed_characters: chars })
            });
        }
        if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            err.textContent = d.detail || 'Fehler beim Speichern';
            err.style.display = 'block';
            return;
        }
        closeEdit();
        toast(EDIT_ID ? 'User aktualisiert' : 'User angelegt');
        await loadAll();
    } catch (e) {
        err.textContent = 'Verbindungsfehler: ' + e.message;
        err.style.display = 'block';
    }
}

async function deleteUser(userId) {
    const u = USERS.find(x => x.id === userId);
    if (!u) return;
    if (!confirm('User "' + u.username + '" wirklich loeschen?')) return;
    try {
        const resp = await fetch('/auth/users/' + userId, { method: 'DELETE' });
        if (!resp.ok) {
            const d = await resp.json().catch(() => ({}));
            toast(d.detail || 'Fehler', 'error');
            return;
        }
        toast('User geloescht');
        await loadAll();
    } catch (e) { toast('Fehler: ' + e.message, 'error'); }
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"\\']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function toast(msg, type) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast ' + (type === 'error' ? 'error' : '') + ' show';
    setTimeout(() => t.classList.remove('show'), 2500);
}

loadAll();
</script>
</body>
</html>'''


def _build_outfit_rules_html() -> str:
    """Outfit-Regeln-Admin-Seite."""
    return '''<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<title>Outfit-Regeln</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }
h1 { font-size: 18px; margin-bottom: 8px; color: #e6edf3; }
.hint { font-size: 12px; color: #8b949e; margin-bottom: 16px; }
.toolbar { margin-bottom: 14px; display: flex; gap: 8px; align-items: center; }
.btn { background: #21262d; color: #c9d1d9; border: 1px solid #30363d; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; }
.btn:hover { background: #30363d; }
.btn-primary { background: #238636; border-color: #2ea043; color: #fff; }
.btn-primary:hover { background: #2ea043; }
.btn-danger { background: #da3633; border-color: #f85149; color: #fff; }
.btn-danger:hover { background: #b62324; }
.btn-sm { padding: 3px 8px; font-size: 12px; }
.add-row { display: flex; gap: 6px; margin-bottom: 12px; }
.add-row input { flex: 1; background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; padding: 6px 10px; border-radius: 6px; font-size: 13px; }

table { width: 100%; border-collapse: collapse; background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; }
th, td { padding: 8px 10px; text-align: center; border-bottom: 1px solid #30363d; font-size: 12px; color: #c9d1d9; }
th { background: #1c2128; color: #8b949e; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; font-size: 11px; }
th.type-col, td.type-col { text-align: left; min-width: 150px; }
tr:last-child td { border-bottom: none; }
tr:hover { background: #1c2128; }
td.actions-col { text-align: right; white-space: nowrap; }
input[type="checkbox"] { cursor: pointer; }

.toast { position: fixed; bottom: 20px; right: 20px; background: #238636; color: #fff; padding: 10px 16px; border-radius: 6px; font-size: 13px; opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 2000; }
.toast.show { opacity: 1; }
.toast.error { background: #da3633; }
</style>
</head>
<body>

<h1>Outfit-Regeln</h1>
<p class="hint">Definiert welche Slots pro outfit_type angezogen sein MUESSEN. Auto-Fill bei Location-Wechsel nutzt diese Regeln. Character-Exceptions (pro Character, in der Garderobe) koennen einzelne Slots ueberschreiben.</p>

<div class="toolbar">
    <button class="btn btn-primary" onclick="saveAll()">Speichern</button>
    <span id="status" style="font-size:12px;color:#8b949e;"></span>
</div>

<div class="add-row">
    <input type="text" id="new-type" placeholder="Neuer outfit_type (z.B. 'streetwear')">
    <button class="btn" onclick="addType()">+ Hinzufuegen</button>
</div>

<table id="rules-table">
    <thead id="rules-thead"></thead>
    <tbody id="rules-tbody"><tr><td colspan="20">Lade...</td></tr></tbody>
</table>

<div class="toast" id="toast"></div>

<script>
let RULES = {};  // { type: {required: [slots]} }
let SLOTS = [];
// Anzeige-Reihenfolge wie in der Garderobe (SLOT_ORDER in script.js)
const SLOT_DISPLAY_ORDER = ['head', 'neck', 'outer', 'top', 'underwear_top', 'bottom', 'underwear_bottom', 'legs', 'feet'];

async function loadAll() {
    try {
        const resp = await fetch('/admin/outfit-rules/data');
        if (resp.status === 401 || resp.status === 403) {
            const ret = encodeURIComponent(window.location.pathname);
            window.location.href = '/?return=' + ret;
            return;
        }
        const data = await resp.json();
        RULES = data.outfit_types || {};
        const rawSlots = data.valid_slots || [];
        // Sortieren nach SLOT_DISPLAY_ORDER, unbekannte Slots ans Ende
        SLOTS = [...rawSlots].sort((a, b) => {
            const ia = SLOT_DISPLAY_ORDER.indexOf(a);
            const ib = SLOT_DISPLAY_ORDER.indexOf(b);
            return (ia === -1 ? 999 : ia) - (ib === -1 ? 999 : ib);
        });
        renderTable();
    } catch (e) {
        toast('Fehler: ' + e.message, 'error');
    }
}

function renderTable() {
    const thead = document.getElementById('rules-thead');
    const tbody = document.getElementById('rules-tbody');
    let th = '<tr><th class="type-col">outfit_type</th><th title="Wenn weder Activity noch Ort einen Type vorgibt — dieser Type wird genutzt">Default</th>';
    for (const s of SLOTS) th += '<th>' + escapeHtml(s) + '</th>';
    th += '<th class="actions-col"></th></tr>';
    thead.innerHTML = th;

    const types = Object.keys(RULES).sort();
    if (!types.length) {
        tbody.innerHTML = '<tr><td colspan="' + (SLOTS.length + 3) + '" style="text-align:center;color:#8b949e;">Noch keine outfit_types</td></tr>';
        return;
    }
    tbody.innerHTML = types.map(t => {
        const req = new Set((RULES[t] || {}).required || []);
        const desc = (RULES[t] || {}).description || '';
        const isDefault = !!((RULES[t] || {}).default);
        let cells = '';
        for (const s of SLOTS) {
            cells += '<td><input type="checkbox" data-type="' + escapeHtml(t) + '" data-slot="' + escapeHtml(s) + '"' + (req.has(s) ? ' checked' : '') + ' onchange="onToggle(this)"></td>';
        }
        const defCell = '<td><input type="radio" name="default-type" data-type="' + escapeHtml(t) + '"' + (isDefault ? ' checked' : '') + ' onchange="onDefaultChange(this)" title="Als Default markieren"></td>';
        const rowMain = '<tr><td class="type-col"><b>' + escapeHtml(t) + '</b></td>' + defCell + cells +
               '<td class="actions-col">' +
               '<button class="btn btn-sm" onclick="renameType(\\'' + t + '\\')" title="Umbenennen oder mit anderem Type mergen">Rename</button> ' +
               '<button class="btn btn-sm btn-danger" onclick="deleteType(\\'' + t + '\\')">Del</button></td></tr>';
        const rowDesc = '<tr class="desc-row"><td colspan="' + (SLOTS.length + 3) + '" style="padding:2px 8px 10px 8px;">' +
               '<textarea data-type="' + escapeHtml(t) + '" rows="2" placeholder="Beschreibung fuer LLM (z.B. Club-Stil: eng, bauchfrei, neon/schwarz)" style="width:100%;font-size:12px;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:4px;padding:4px 6px;" onchange="onDescChange(this)">' +
               escapeHtml(desc) + '</textarea></td></tr>';
        return rowMain + rowDesc;
    }).join('');
}

function onDefaultChange(radio) {
    const t = radio.dataset.type;
    // Alle anderen entmarkieren, gewaehlten markieren
    for (const k of Object.keys(RULES)) {
        if (RULES[k] && typeof RULES[k] === 'object') {
            if (k === t) RULES[k].default = true;
            else delete RULES[k].default;
        }
    }
}

async function renameType(t) {
    const nn = prompt('Neuer Name fuer "' + t + '"\\n(existiert er bereits → wird gemergt):', t);
    if (nn === null) return;
    const newName = (nn || '').trim().toLowerCase();
    if (!newName || newName === t) return;
    const isMerge = !!RULES[newName];
    const msg = isMerge
        ? `"${t}" in existierenden Type "${newName}" MERGEN? Alle Referenzen werden umgeschrieben, Slots/Description von "${newName}" bleiben erhalten.`
        : `"${t}" → "${newName}" umbenennen? Alle Referenzen (Locations, Raeume, Items, Activities, Character-Exceptions) werden mitumgeschrieben.`;
    if (!confirm(msg)) return;
    try {
        const r = await fetch('/admin/outfit-rules/rename', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ old: t, new: newName }),
        });
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            toast('Fehler: ' + (d.detail || r.status), 'error');
            return;
        }
        const data = await r.json();
        const u = data.updated || {};
        toast((data.merged ? 'Gemergt' : 'Umbenannt') +
              ` — locations:${u.locations||0} rooms:${u.rooms||0} items:${u.items||0} activities:${u.activities||0} chars:${u.character_exceptions||0}`);
        await loadAll();
    } catch (e) {
        toast('Fehler: ' + e.message, 'error');
    }
}

function onDescChange(el) {
    const t = el.dataset.type;
    if (!RULES[t]) RULES[t] = { required: [] };
    RULES[t].description = el.value || '';
}

function onToggle(cb) {
    const t = cb.dataset.type;
    const s = cb.dataset.slot;
    if (!RULES[t]) RULES[t] = { required: [] };
    const req = new Set(RULES[t].required || []);
    if (cb.checked) req.add(s); else req.delete(s);
    RULES[t].required = Array.from(req);
}

function addType() {
    const el = document.getElementById('new-type');
    const name = (el.value || '').trim().toLowerCase();
    if (!name) return;
    if (RULES[name]) { toast('Typ existiert schon', 'error'); return; }
    RULES[name] = { required: [] };
    el.value = '';
    renderTable();
}

function deleteType(t) {
    if (!confirm('outfit_type \\'' + t + '\\' loeschen?')) return;
    delete RULES[t];
    renderTable();
}

async function saveAll() {
    const status = document.getElementById('status');
    status.textContent = 'Speichere...';
    status.style.color = '#8b949e';
    try {
        const r = await fetch('/admin/outfit-rules/data', {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ outfit_types: RULES }),
        });
        if (!r.ok) {
            const d = await r.json().catch(() => ({}));
            status.textContent = 'Fehler: ' + (d.detail || r.status);
            status.style.color = '#f85149';
            return;
        }
        status.textContent = 'Gespeichert.';
        status.style.color = '#8fd17f';
        toast('Gespeichert');
    } catch (e) {
        status.textContent = 'Fehler: ' + e.message;
        status.style.color = '#f85149';
    }
}

function escapeHtml(s) {
    return String(s).replace(/[&<>"\\']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function toast(msg, type) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast ' + (type === 'error' ? 'error' : '') + ' show';
    setTimeout(() => t.classList.remove('show'), 2000);
}

loadAll();
</script>
</body>
</html>'''


def _build_llm_stats_html() -> str:
    """LLM-Call-Statistik — read-only Auswertung mit Zeitraum-/Character-Filter."""
    return '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>LLM Stats</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; }
h1 { font-size: 18px; margin-bottom: 8px; color: #e6edf3; }
.hint { font-size: 12px; color: #8b949e; margin-bottom: 16px; }

.filter-bar { display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 10px 14px; margin-bottom: 14px; }
.filter-bar label { font-size: 12px; color: #8b949e; display: inline-flex; align-items: center; gap: 6px; }
.filter-bar input, .filter-bar select { background: #0d1117; color: #c9d1d9;
    border: 1px solid #30363d; padding: 5px 8px; border-radius: 5px; font-size: 12px; }
.filter-bar input[type="datetime-local"] { font-family: inherit; }
.filter-bar select[multiple] { min-width: 180px; min-height: 70px; }

.btn { background: #21262d; color: #c9d1d9; border: 1px solid #30363d;
    padding: 5px 12px; border-radius: 5px; cursor: pointer; font-size: 12px; }
.btn:hover { background: #30363d; }
.btn-primary { background: #1f6feb; border-color: #388bfd; color: #fff; }
.btn-primary:hover { background: #388bfd; }
.btn.active { background: #1f6feb; border-color: #388bfd; color: #fff; }

.preset-row { display: flex; gap: 4px; }
.summary { font-size: 12px; color: #8b949e; margin-bottom: 10px; }

table { width: 100%; border-collapse: collapse; background: #161b22;
    border: 1px solid #30363d; border-radius: 8px; overflow: hidden; font-size: 12px; }
th, td { padding: 6px 8px; border-bottom: 1px solid #30363d; text-align: right; white-space: nowrap; }
th { background: #1c2128; color: #8b949e; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.4px; font-size: 11px; cursor: pointer; user-select: none; }
th:hover { color: #c9d1d9; }
th.left, td.left { text-align: left; }
th .arrow { color: #58a6ff; margin-left: 2px; }
tr:last-child td { border-bottom: none; }
tr:hover { background: #1c2128; }
td.task { font-family: monospace; color: #d2a8ff; }
td.model { font-family: monospace; color: #79c0ff; }
td.provider { color: #8b949e; }
td.agent { color: #ffa657; }
td.dim { color: #6e7681; }

.empty { text-align: center; padding: 40px; color: #8b949e; }
.error { color: #f85149; padding: 10px; background: #da363322; border-radius: 6px; margin: 10px 0; }
</style>
</head>
<body>

<h1>LLM Call Statistik</h1>
<p class="hint">Aggregat aus <code>llm_call_stats</code>. Zeitraum + Character filterbar. Aufgeschluesselt nach Task x Model x Provider; mit Toggle auch nach Character.</p>

<div class="filter-bar">
    <div class="preset-row">
        <button class="btn" data-preset="1h" onclick="applyPreset('1h')">1h</button>
        <button class="btn" data-preset="24h" onclick="applyPreset('24h')">24h</button>
        <button class="btn" data-preset="7d" onclick="applyPreset('7d')">7d</button>
        <button class="btn" data-preset="30d" onclick="applyPreset('30d')">30d</button>
    </div>
    <label>From <input type="datetime-local" id="from-input"></label>
    <label>To <input type="datetime-local" id="to-input"></label>
    <label>Task <input type="text" id="task-filter" placeholder="substring..." style="width:140px;"></label>
    <label>Character
        <select id="agent-select" multiple size="3"></select>
    </label>
    <label><input type="checkbox" id="group-by-agent"> nach Character aufschluesseln</label>
    <button class="btn btn-primary" onclick="loadData()">Apply</button>
</div>

<div class="summary" id="summary"></div>
<div id="error-box"></div>

<table id="stats-table">
    <thead id="stats-thead"></thead>
    <tbody id="stats-tbody"><tr><td class="empty" colspan="20">Lade...</td></tr></tbody>
</table>

<script>
let CURRENT_ROWS = [];
let SORT_KEY = "calls";
let SORT_DIR = -1;

function isoLocal(dt) {
    const pad = n => String(n).padStart(2, "0");
    return dt.getFullYear() + "-" + pad(dt.getMonth()+1) + "-" + pad(dt.getDate())
        + "T" + pad(dt.getHours()) + ":" + pad(dt.getMinutes());
}

function applyPreset(p) {
    const now = new Date();
    let from = new Date(now);
    if (p === "1h") from.setHours(now.getHours() - 1);
    else if (p === "24h") from.setHours(now.getHours() - 24);
    else if (p === "7d") from.setDate(now.getDate() - 7);
    else if (p === "30d") from.setDate(now.getDate() - 30);
    document.getElementById("from-input").value = isoLocal(from);
    document.getElementById("to-input").value = isoLocal(now);
    document.querySelectorAll(".preset-row .btn").forEach(b => b.classList.remove("active"));
    const btn = document.querySelector(".preset-row .btn[data-preset='" + p + "']");
    if (btn) btn.classList.add("active");
    loadData();
}

function buildQuery() {
    const fromVal = document.getElementById("from-input").value;
    const toVal = document.getElementById("to-input").value;
    const task = document.getElementById("task-filter").value.trim();
    const agentSel = document.getElementById("agent-select");
    const agents = Array.from(agentSel.selectedOptions).map(o => o.value).filter(v => v);
    const grouped = document.getElementById("group-by-agent").checked;
    const params = new URLSearchParams();
    if (fromVal) params.set("from", fromVal.length === 16 ? fromVal + ":00" : fromVal);
    if (toVal) params.set("to", toVal.length === 16 ? toVal + ":00" : toVal);
    if (task) params.set("task", task);
    if (agents.length) params.set("agents", agents.join(","));
    if (grouped) params.set("group_by_agent", "1");
    return params.toString();
}

async function loadData() {
    const errBox = document.getElementById("error-box");
    errBox.innerHTML = "";
    document.getElementById("stats-tbody").innerHTML =
        '<tr><td class="empty" colspan="20">Lade...</td></tr>';
    try {
        const q = buildQuery();
        const resp = await fetch("/admin/llm-stats/data?" + q, { credentials: "same-origin" });
        if (resp.status === 401 || resp.status === 403) {
            const ret = encodeURIComponent(window.location.pathname);
            window.location.href = "/?return=" + ret;
            return;
        }
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        const data = await resp.json();
        CURRENT_ROWS = data.rows || [];
        updateAgentDropdown(data.agents || []);
        renderSummary(data);
        renderTable();
    } catch (e) {
        errBox.innerHTML = '<div class="error">Fehler: ' + escapeHtml(e.message) + "</div>";
        document.getElementById("stats-tbody").innerHTML = "";
    }
}

function updateAgentDropdown(agents) {
    const sel = document.getElementById("agent-select");
    const prev = new Set(Array.from(sel.selectedOptions).map(o => o.value));
    sel.innerHTML = "";
    for (const a of agents) {
        const opt = document.createElement("option");
        opt.value = a;
        opt.textContent = a;
        if (prev.has(a)) opt.selected = true;
        sel.appendChild(opt);
    }
}

function renderSummary(data) {
    const total = CURRENT_ROWS.reduce((s, r) => s + r.calls, 0);
    const groups = CURRENT_ROWS.length;
    const grouped = data.group_by_agent ? "Task x Model x Provider x Character" : "Task x Model x Provider";
    document.getElementById("summary").textContent =
        groups + " Gruppen, " + total + " Calls insgesamt | Gruppierung: " + grouped
        + " | Zeitraum: " + data.from + " bis " + data.to;
}

function renderTable() {
    const grouped = document.getElementById("group-by-agent").checked;
    const thead = document.getElementById("stats-thead");
    const tbody = document.getElementById("stats-tbody");

    const cols = [
        { key: "task",             label: "Task",          cls: "left" },
        { key: "model",            label: "Model",         cls: "left" },
        { key: "provider",         label: "Provider",      cls: "left" }
    ];
    if (grouped) cols.push({ key: "agent_name", label: "Character", cls: "left" });
    cols.push(
        { key: "calls",            label: "Calls" },
        { key: "avg_duration",     label: "avg s" },
        { key: "min_duration",     label: "min s" },
        { key: "max_duration",     label: "max s" },
        { key: "p90_duration",     label: "p90 s" },
        { key: "avg_in_tokens",    label: "avg in" },
        { key: "avg_out_tokens",   label: "avg out" },
        { key: "avg_max_tokens",   label: "cfg max out" },
        { key: "avg_total_tokens", label: "avg in+out" },
        { key: "max_in_tokens",    label: "peak in" },
        { key: "max_total_tokens", label: "peak in+out" }
    );

    let th = "<tr>";
    for (const c of cols) {
        const isSort = c.key === SORT_KEY;
        const arrow = isSort ? '<span class="arrow">' + (SORT_DIR > 0 ? "↑" : "↓") + "</span>" : "";
        th += '<th class="' + (c.cls || "") + '" onclick="sortBy(\\'' + c.key + '\\')">'
            + escapeHtml(c.label) + arrow + "</th>";
    }
    th += "</tr>";
    thead.innerHTML = th;

    const sorted = CURRENT_ROWS.slice().sort((a, b) => {
        const va = a[SORT_KEY], vb = b[SORT_KEY];
        if (typeof va === "number") return (va - vb) * SORT_DIR;
        return String(va || "").localeCompare(String(vb || "")) * SORT_DIR;
    });

    if (!sorted.length) {
        tbody.innerHTML = '<tr><td class="empty" colspan="' + cols.length + '">Keine Daten im gewaehlten Zeitraum</td></tr>';
        return;
    }

    tbody.innerHTML = sorted.map(r => {
        let row = "<tr>";
        row += '<td class="left task">' + escapeHtml(r.task) + "</td>";
        row += '<td class="left model">' + escapeHtml(r.model) + "</td>";
        row += '<td class="left provider">' + escapeHtml(r.provider || "—") + "</td>";
        if (grouped) row += '<td class="left agent">' + escapeHtml(r.agent_name || "—") + "</td>";
        row += "<td>" + r.calls + "</td>";
        row += "<td>" + r.avg_duration.toFixed(2) + "</td>";
        row += "<td>" + r.min_duration.toFixed(2) + "</td>";
        row += "<td>" + r.max_duration.toFixed(2) + "</td>";
        row += "<td>" + r.p90_duration.toFixed(2) + "</td>";
        row += "<td>" + r.avg_in_tokens + "</td>";
        row += "<td>" + r.avg_out_tokens + "</td>";
        const cfg = r.avg_max_tokens;
        row += '<td class="' + (cfg ? "" : "dim") + '">' + (cfg || "—") + "</td>";
        row += "<td>" + r.avg_total_tokens + "</td>";
        row += "<td>" + r.max_in_tokens + "</td>";
        row += "<td>" + r.max_total_tokens + "</td>";
        row += "</tr>";
        return row;
    }).join("");
}

function sortBy(key) {
    if (SORT_KEY === key) SORT_DIR = -SORT_DIR;
    else { SORT_KEY = key; SORT_DIR = -1; }
    renderTable();
}

function escapeHtml(s) {
    return String(s == null ? "" : s)
        .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

applyPreset("24h");
</script>
</body>
</html>'''


