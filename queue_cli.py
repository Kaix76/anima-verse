#!/usr/bin/env python3
"""Queue CLI — Inspect and manage the persistent TaskQueue via SQLite.

Works WITHOUT the server running — reads the same SQLite DB directly.

Usage:
    python queue_cli.py list                          # pending tasks (all queues)
    python queue_cli.py list -q GamingPC              # filter by queue
    python queue_cli.py list -s failed                # filter by status
    python queue_cli.py list -s all                   # all statuses
    python queue_cli.py info <task_id>                # full task details
    python queue_cli.py cancel <task_id>              # cancel pending task
    python queue_cli.py retry <task_id>               # retry failed/cancelled task
    python queue_cli.py move <task_id> <queue>        # move to different queue
    python queue_cli.py priority <task_id> <int>      # change priority (lower = higher)
    python queue_cli.py pause <queue>                 # pause a queue
    python queue_cli.py resume <queue>                # resume a queue
    python queue_cli.py clear                         # delete old completed/failed (>24h)
    python queue_cli.py clear --hours 1               # delete older than 1h
    python queue_cli.py clear --status failed         # delete only failed
    python queue_cli.py stats                         # queue statistics

Config: reads TASK_QUEUE_DB from .env (default: ./storage/task_queue.db)
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Load .env for DB path (minimal — no dependencies needed)
# ---------------------------------------------------------------------------
def _load_env_db_path() -> Path:
    env_path = Path(__file__).resolve().parent / ".env"
    db_val = "./storage/task_queue.db"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("TASK_QUEUE_DB="):
                db_val = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    return Path(db_val)


DB_PATH = _load_env_db_path()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        print(f"FEHLER: Datenbank nicht gefunden: {DB_PATH}", file=sys.stderr)
        print("Starte zuerst den Server oder prüfe TASK_QUEUE_DB in .env", file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        return iso.replace("T", " ")[:16]
    except Exception:
        return str(iso)


def _fmt_duration(s: float | None) -> str:
    if not s:
        return "-"
    if s < 60:
        return f"{s:.1f}s"
    return f"{s/60:.1f}m"


_STATUS_COLORS = {
    "pending":   "\033[33m",   # yellow
    "running":   "\033[36m",   # cyan
    "completed": "\033[32m",   # green
    "failed":    "\033[31m",   # red
    "cancelled": "\033[90m",   # grey
}
_RESET = "\033[0m"


def _c(text: str, status: str) -> str:
    return f"{_STATUS_COLORS.get(status, '')}{text}{_RESET}"


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_list(args: argparse.Namespace) -> None:
    conn = _connect()
    status_filter = args.status if args.status != "all" else None
    statuses = (status_filter,) if status_filter else ("pending", "running")

    where = "status IN ({})".format(",".join("?" * len(statuses)))
    params: list = list(statuses)
    if args.queue:
        where += " AND queue_name=?"
        params.append(args.queue)

    rows = conn.execute(
        f"""SELECT task_id, queue_name, task_type, priority, status,
                   created_at, started_at, completed_at, duration_s,
                   character_name, error
            FROM tasks WHERE {where}
            ORDER BY
              CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
              priority ASC, created_at ASC
            LIMIT {args.limit}""",
        params,
    ).fetchall()
    conn.close()

    if not rows:
        print("Keine Tasks gefunden.")
        return

    print(f"{'TASK_ID':<20} {'QUEUE':<12} {'TYPE':<28} {'PRIO':>4} {'STATUS':<11} {'CREATED':<16} {'AGENT':<14} {'ERR'}")
    print("-" * 115)
    for r in rows:
        err = (r["error"] or "")[:30]
        ts = _fmt_dt(r["completed_at"] or r["started_at"] or r["created_at"])
        status_padded = r['status'].ljust(11)
        print(
            f"{r['task_id']:<20} {r['queue_name']:<12} {r['task_type']:<28} "
            f"{r['priority']:>4} {_c(status_padded, r['status'])} "
            f"{ts:<16} {(r['agent_name'] or ''):<14} {err}"
        )
    print(f"\n{len(rows)} Task(s) angezeigt.")


def cmd_info(args: argparse.Namespace) -> None:
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM tasks WHERE task_id=?", (args.task_id,)
    ).fetchone()
    conn.close()
    if not row:
        print(f"Task nicht gefunden: {args.task_id}")
        return
    d = dict(row)
    print(f"\n{'='*60}")
    print(f"Task: {d['task_id']}")
    print(f"{'='*60}")
    for k, v in d.items():
        if k == "payload":
            try:
                v = json.dumps(json.loads(v), ensure_ascii=False, indent=2)
            except Exception:
                pass
        elif k == "result" and v:
            try:
                v = json.dumps(json.loads(v), ensure_ascii=False, indent=2)[:500]
            except Exception:
                pass
        print(f"  {k:<16}: {v}")
    print()


def cmd_cancel(args: argparse.Namespace) -> None:
    conn = _connect()
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "UPDATE tasks SET status='cancelled', completed_at=?, error='Abgebrochen (CLI)'"
        " WHERE task_id=? AND status='pending'",
        (now, args.task_id),
    )
    conn.commit()
    conn.close()
    if cur.rowcount:
        print(f"✓ Task abgebrochen: {args.task_id}")
    else:
        print(f"Task nicht gefunden oder nicht 'pending': {args.task_id}")


def cmd_retry(args: argparse.Namespace) -> None:
    conn = _connect()
    cur = conn.execute(
        """UPDATE tasks
           SET status='pending', error='', result=NULL,
               started_at=NULL, completed_at=NULL, duration_s=0
           WHERE task_id=? AND status IN ('failed','cancelled')""",
        (args.task_id,),
    )
    conn.commit()
    conn.close()
    if cur.rowcount:
        print(f"✓ Task auf 'pending' zurückgesetzt: {args.task_id}")
        print("  → Server-Worker wird den Task beim nächsten Zyklus aufnehmen.")
    else:
        print(f"Task nicht gefunden oder nicht failed/cancelled: {args.task_id}")


def cmd_move(args: argparse.Namespace) -> None:
    conn = _connect()
    cur = conn.execute(
        "UPDATE tasks SET queue_name=? WHERE task_id=? AND status='pending'",
        (args.queue, args.task_id),
    )
    conn.commit()
    conn.close()
    if cur.rowcount:
        print(f"✓ Task verschoben: {args.task_id} → {args.queue}")
    else:
        print(f"Task nicht gefunden oder nicht 'pending': {args.task_id}")


def cmd_priority(args: argparse.Namespace) -> None:
    conn = _connect()
    cur = conn.execute(
        "UPDATE tasks SET priority=? WHERE task_id=? AND status='pending'",
        (args.priority, args.task_id),
    )
    conn.commit()
    conn.close()
    if cur.rowcount:
        print(f"✓ Priorität gesetzt: {args.task_id} → {args.priority}")
    else:
        print(f"Task nicht gefunden oder nicht 'pending': {args.task_id}")


def cmd_pause(args: argparse.Namespace) -> None:
    conn = _connect()
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO queue_paused (queue_name, paused, updated_at)
           VALUES (?, 1, ?)
           ON CONFLICT(queue_name) DO UPDATE SET paused=1, updated_at=?""",
        (args.queue, now, now),
    )
    conn.commit()
    conn.close()
    print(f"✓ Queue pausiert: {args.queue}")
    print("  → Laufende Tasks werden fertiggestellt. Neue Tasks warten.")


def cmd_resume(args: argparse.Namespace) -> None:
    conn = _connect()
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO queue_paused (queue_name, paused, updated_at)
           VALUES (?, 0, ?)
           ON CONFLICT(queue_name) DO UPDATE SET paused=0, updated_at=?""",
        (args.queue, now, now),
    )
    conn.commit()
    conn.close()
    print(f"✓ Queue fortgesetzt: {args.queue}")
    print("  → Server-Worker nimmt beim nächsten Zyklus wieder auf.")


def cmd_clear(args: argparse.Namespace) -> None:
    conn = _connect()
    cutoff = (datetime.now() - timedelta(hours=args.hours)).isoformat(timespec="seconds")
    statuses = [args.status] if args.status else ["completed", "failed", "cancelled"]
    placeholders = ",".join("?" * len(statuses))
    cur = conn.execute(
        f"DELETE FROM tasks WHERE status IN ({placeholders}) AND completed_at < ?",
        (*statuses, cutoff),
    )
    conn.commit()
    conn.close()
    print(f"✓ {cur.rowcount} Task(s) gelöscht (älter als {args.hours}h, status: {statuses})")


def cmd_stats(args: argparse.Namespace) -> None:
    conn = _connect()
    print(f"\nDatenbank: {DB_PATH}\n")

    # Per-queue stats
    queues = conn.execute(
        "SELECT DISTINCT queue_name FROM tasks ORDER BY queue_name"
    ).fetchall()
    paused_map = {
        r["queue_name"]: r["paused"]
        for r in conn.execute("SELECT queue_name, paused FROM queue_paused").fetchall()
    }

    print(f"{'QUEUE':<16} {'PAUSED':<8} {'PENDING':>7} {'RUNNING':>7} {'DONE':>7} {'FAILED':>7} {'CANCEL':>7}")
    print("-" * 65)
    for q in queues:
        qn = q["queue_name"]
        paused = "JA" if paused_map.get(qn) else "nein"
        counts = {
            r["status"]: r["cnt"]
            for r in conn.execute(
                "SELECT status, COUNT(*) as cnt FROM tasks WHERE queue_name=? GROUP BY status",
                (qn,),
            ).fetchall()
        }
        print(
            f"{qn:<16} {paused:<8} {counts.get('pending',0):>7} {counts.get('running',0):>7} "
            f"{counts.get('completed',0):>7} {counts.get('failed',0):>7} {counts.get('cancelled',0):>7}"
        )

    # Overall
    total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    oldest = conn.execute(
        "SELECT created_at FROM tasks ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    print(f"\nGesamt: {total} Tasks")
    if oldest:
        print(f"Ältester Eintrag: {_fmt_dt(oldest[0])}")

    # Failed tasks with errors
    failed = conn.execute(
        "SELECT task_id, queue_name, task_type, error, completed_at FROM tasks"
        " WHERE status='failed' ORDER BY completed_at DESC LIMIT 5"
    ).fetchall()
    if failed:
        print(f"\nLetzte Fehler:")
        for f in failed:
            print(f"  {f['task_id']} [{f['queue_name']}] {f['task_type']}: {(f['error'] or '')[:80]}")

    conn.close()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="TaskQueue CLI — Queue verwalten ohne Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # list
    p_list = sub.add_parser("list", help="Tasks anzeigen")
    p_list.add_argument("-q", "--queue", default="", help="Queue-Name filtern")
    p_list.add_argument("-s", "--status", default="pending",
                        help="Status: pending|running|failed|cancelled|completed|all")
    p_list.add_argument("-n", "--limit", type=int, default=50, help="Max Zeilen")
    p_list.set_defaults(func=cmd_list)

    # info
    p_info = sub.add_parser("info", help="Task-Details anzeigen")
    p_info.add_argument("task_id")
    p_info.set_defaults(func=cmd_info)

    # cancel
    p_cancel = sub.add_parser("cancel", help="Pending Task abbrechen")
    p_cancel.add_argument("task_id")
    p_cancel.set_defaults(func=cmd_cancel)

    # retry
    p_retry = sub.add_parser("retry", help="Fehlgeschlagenen Task wiederholen")
    p_retry.add_argument("task_id")
    p_retry.set_defaults(func=cmd_retry)

    # move
    p_move = sub.add_parser("move", help="Task in andere Queue verschieben")
    p_move.add_argument("task_id")
    p_move.add_argument("queue", help="Ziel-Queue-Name")
    p_move.set_defaults(func=cmd_move)

    # priority
    p_prio = sub.add_parser("priority", help="Task-Priorität ändern (niedriger = schneller)")
    p_prio.add_argument("task_id")
    p_prio.add_argument("priority", type=int, help="Neue Priorität (z.B. 10=hoch, 30=niedrig)")
    p_prio.set_defaults(func=cmd_priority)

    # pause
    p_pause = sub.add_parser("pause", help="Queue pausieren")
    p_pause.add_argument("queue", help="Queue-Name")
    p_pause.set_defaults(func=cmd_pause)

    # resume
    p_resume = sub.add_parser("resume", help="Queue fortsetzen")
    p_resume.add_argument("queue", help="Queue-Name")
    p_resume.set_defaults(func=cmd_resume)

    # clear
    p_clear = sub.add_parser("clear", help="Alte abgeschlossene Tasks löschen")
    p_clear.add_argument("--hours", type=float, default=24.0,
                         help="Älter als N Stunden löschen (default: 24)")
    p_clear.add_argument("--status", default="",
                         help="Nur diesen Status löschen (completed/failed/cancelled)")
    p_clear.set_defaults(func=cmd_clear)

    # stats
    p_stats = sub.add_parser("stats", help="Queue-Statistiken")
    p_stats.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
