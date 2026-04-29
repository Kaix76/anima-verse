"""LLM-Call-Statistik fuer Dauer-Schaetzung.

Persistiert pro `(model, task)` einen Rolling-Window von Calls in der
Welt-DB-Tabelle `llm_call_stats` und schaetzt daraus die erwartete Dauer
neuer Calls anhand von Input-Tokens.

Schaetzformel:
    est_dur = median(duration) * (current_in / median(in))
Mit hierarchischem Fallback (model,task) -> (model,*) -> None.
"""
from datetime import datetime
from statistics import median
from typing import Dict, Optional

from app.core.db import get_connection, transaction
from app.core.log import get_logger

logger = get_logger("llm_stats")

# Maximale Anzahl Calls pro (model, task)-Bucket
_BUCKET_LIMIT = 200
# Mindest-Samples damit der primaere Bucket genutzt wird (sonst Fallback)
_MIN_TASK_SAMPLES = 5


def record_call(model: str, task: str, provider: str,
                in_tokens: int, out_tokens: int, duration_s: float) -> None:
    """Schreibt einen abgeschlossenen LLM-Call in die Statistik-Tabelle.

    Defekte / unvollstaendige Records werden uebersprungen.
    Nach dem Insert wird der Bucket auf `_BUCKET_LIMIT` Eintraege gekappt
    pro `(model, task, provider)` — gleicher Model-Name auf anderer HW
    laeuft anders schnell und bekommt deshalb einen eigenen Bucket.
    """
    if not model or not task:
        return
    if in_tokens <= 0 or out_tokens <= 0 or duration_s <= 0:
        return

    prov = provider or ""
    try:
        with transaction() as conn:
            conn.execute(
                "INSERT INTO llm_call_stats "
                "(ts, model, task, provider, in_tokens, out_tokens, duration_s) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (datetime.now().isoformat(timespec="seconds"),
                 model, task, prov,
                 int(in_tokens), int(out_tokens), float(duration_s)))
            conn.execute(
                "DELETE FROM llm_call_stats WHERE id IN ("
                "  SELECT id FROM llm_call_stats "
                "   WHERE model = ? AND task = ? AND provider = ? "
                "   ORDER BY ts DESC LIMIT -1 OFFSET ?"
                ")",
                (model, task, prov, _BUCKET_LIMIT))
    except Exception as e:
        logger.warning("Stats-Insert fehlgeschlagen: %s", e)


def estimate_duration(model: str, task: str, provider: str = "",
                      in_tokens: int = 0) -> Optional[Dict]:
    """Schaetzt die Dauer eines LLM-Calls aus historischen Daten.

    Args:
        model: Model-Name
        task: task_type (z.B. "thought", "extraction")
        provider: Provider/Channel-Name. Wichtig weil dasselbe Modell auf
            anderer HW unterschiedlich schnell ist. Leer = providerunabhaengig
            (nur als letzter Fallback genutzt).
        in_tokens: aktuelle Input-Tokens. 0 = nicht bekannt, dann reiner Median.

    Returns:
        dict mit `est_duration_s`, `p90_duration_s`, `est_out_tokens`,
        `samples`, `source` ("task" | "model") oder None wenn nichts
        Vergleichbares im Cache.
    """
    if not model:
        return None

    # Hierarchischer Fallback:
    # 1. (model, task, provider) — exakt
    # 2. (model, *,    provider) — anderer Task auf gleicher HW
    # 3. (model, task, *)        — gleicher Task auf anderer HW
    prov_filter = provider if provider else None
    rows = _fetch_bucket(model, task, prov_filter)
    source = "task"
    if len(rows) < _MIN_TASK_SAMPLES and prov_filter is not None:
        rows = _fetch_bucket(model, None, prov_filter)
        source = "model"
    if len(rows) < _MIN_TASK_SAMPLES:
        rows = _fetch_bucket(model, task, None)
        source = "task_any_provider"
    if not rows:
        return None

    durs = [r[2] for r in rows]
    ins  = [r[0] for r in rows if r[0] > 0]
    outs = [r[1] for r in rows]

    med_dur = median(durs)
    med_in  = median(ins) if ins else 0
    med_out = int(median(outs)) if outs else 0

    if med_in > 0 and in_tokens > 0:
        # Lineare Skalierung mit Input — bei extremen Ausreissern gedeckelt
        ratio = max(0.3, min(3.0, in_tokens / med_in))
        est_dur = med_dur * ratio
    else:
        est_dur = med_dur

    sorted_durs = sorted(durs)
    p90_idx = max(0, int(len(sorted_durs) * 0.9) - 1)
    p90 = sorted_durs[min(p90_idx, len(sorted_durs) - 1)]

    return {
        "est_duration_s": round(est_dur, 2),
        "p90_duration_s": round(p90, 2),
        "est_out_tokens": med_out,
        "samples": len(rows),
        "source": source,
    }


def _fetch_bucket(model: str, task: Optional[str], provider: Optional[str]):
    """Holt die letzten N Eintraege fuer den angegebenen Bucket.

    `task=None` oder `provider=None` heisst "egal" (kein WHERE-Filter).
    Returns Liste von (in_tokens, out_tokens, duration_s).
    """
    conn = get_connection()
    where = ["model = ?"]
    params: list = [model]
    if task is not None:
        where.append("task = ?")
        params.append(task)
    if provider is not None:
        where.append("provider = ?")
        params.append(provider)
    params.append(_BUCKET_LIMIT)
    sql = (
        "SELECT in_tokens, out_tokens, duration_s "
        "FROM llm_call_stats "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY ts DESC LIMIT ?"
    )
    cur = conn.execute(sql, params)
    return [(r[0], r[1], r[2]) for r in cur.fetchall()]
