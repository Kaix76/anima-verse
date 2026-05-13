"""
SchedulerManager - Verwaltet zeitgesteuerte Jobs per Character

Jobs werden pro Character gespeichert:
  storage/users/{user}/characters/{name}/scheduler/jobs.json
  storage/users/{user}/characters/{name}/scheduler/job_logs.json

Beim Start werden alle Character-Verzeichnisse nach Schedulern durchsucht.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from app.core.log import get_logger

logger = get_logger("scheduler")

# Sentinel value fuer home_location: Character schlaeft ausserhalb der Welt
# (kein konkreter Ort/Raum). Erscheint waehrend des Schlafens nicht auf der
# Karte, sondern im "Ohne Ort"-Tray mit Schlaf-Marker.
OFFMAP_SLEEP_SENTINEL = "__offmap__"


class SchedulerManager:
    """
    Verwaltet zeitgesteuerte Jobs fuer Characters.
    Jobs sind per User + Character gespeichert.
    Unterstuetzt Interval, Cron und One-Time Jobs.
    """

    def __init__(self):
        """Initialisiert SchedulerManager und laedt Jobs aus allen Character-Verzeichnissen"""
        self.project_root = Path(__file__).parent.parent.parent

        # APScheduler
        self.scheduler = BackgroundScheduler()
        self.scheduler.start()

        # Job-Daten im Speicher (flache Liste aller Jobs)
        self.jobs_data = {
            "jobs": [],
            "metadata": {
                "created_at": datetime.now().isoformat(),
                "last_updated": datetime.now().isoformat(),
                "total_jobs": 0
            }
        }

        # Migration von globalem Storage (einmalig)
        self._migrate_global_jobs()

        # Lade Jobs aus allen Character-Verzeichnissen
        self._load_all_character_jobs()

        # Legacy-Cleanup: world_hourly_tick wurde durch den zentralen
        # World-Admin-Tick (app/core/periodic_jobs.py) ersetzt. Bestands-
        # Eintraege rausraeumen, damit sie nicht in der Scheduler-UI
        # auftauchen wo der User sie sehen + loeschen koennte.
        try:
            removed = self._purge_legacy_world_hourly_job()
            if removed:
                logger.info("world_hourly_tick: %d Legacy-Eintraege entfernt "
                            "(durch periodic_jobs ersetzt)", removed)
        except Exception as _ple:
            logger.debug("world_hourly_tick legacy purge: %s", _ple)

        # Legacy-Cleanup: activity_done_*-One-Time-Jobs sind durch den
        # state-basierten Pfad (profile.activity_started_at +
        # _sub_activity_expiry im world_admin_tick) ersetzt. Bestands-
        # Eintraege purge'n damit der Scheduler-UI sauber bleibt.
        try:
            removed = self._purge_legacy_activity_done_jobs()
            if removed:
                logger.info("activity_done_*: %d Legacy-One-Time-Jobs entfernt "
                            "(durch periodic_jobs._sub_activity_expiry ersetzt)",
                            removed)
        except Exception as _ple:
            logger.debug("activity_done legacy purge: %s", _ple)

        logger.info("Initialisiert mit %d Jobs", len(self.jobs_data["jobs"]))

    def _migrate_global_jobs(self):
        """Migriert Jobs aus dem globalen storage/scheduler/jobs.json in per-Character Verzeichnisse."""
        from app.models.character import save_character_scheduler_jobs, save_character_scheduler_logs

        global_jobs_file = self.project_root / "storage" / "scheduler" / "jobs.json"
        global_logs_file = self.project_root / "storage" / "scheduler" / "job_logs.json"

        if not global_jobs_file.exists():
            return

        try:
            data = json.loads(global_jobs_file.read_text(encoding="utf-8"))
            jobs = data.get("jobs", []) if isinstance(data, dict) else data
        except Exception as e:
            logger.error("Migration: Fehler beim Lesen: %s", e)
            return

        if not jobs:
            global_jobs_file.rename(global_jobs_file.with_suffix(".json.migrated"))
            return

        # Jobs nach (character) gruppieren
        grouped = {}
        for job in jobs:
            user_id = job.get("user_id", "")
            character = job.get("character", job.get("agent", ""))
            # Normalisiere: character-Feld sicherstellen
            if character and "character" not in job:
                job["character"] = character
            key = (character)
            if key not in grouped:
                grouped[key] = []
            grouped[key].append(job)

        migrated_count = 0
        for (character), char_jobs in grouped.items():
            if not character:
                logger.warning("Migration: Ueberspringe Jobs ohne character")
                continue
            try:
                save_character_scheduler_jobs(character, char_jobs)
                migrated_count += len(char_jobs)
            except Exception as e:
                logger.error("Migration Fehler fuer %s: %s", character, e)

        # Logs migrieren
        if global_logs_file.exists():
            try:
                logs = json.loads(global_logs_file.read_text(encoding="utf-8"))
                # Logs den Jobs zuordnen
                job_char_map = {}
                for job in jobs:
                    job_char_map[job["id"]] = (
                        job.get("user_id", ""),
                        job.get("character", job.get("agent", ""))
                    )

                logs_by_char = {}
                for log_entry in logs:
                    job_id = log_entry.get("job_id", "")
                    key = job_char_map.get(job_id, ("", ""))
                    if key[0] and key[1]:
                        if key not in logs_by_char:
                            logs_by_char[key] = []
                        logs_by_char[key].append(log_entry)

                for (character), char_logs in logs_by_char.items():
                    save_character_scheduler_logs(character, char_logs)

                global_logs_file.rename(global_logs_file.with_suffix(".json.migrated"))
                logger.info("Migration: Logs migriert")
            except Exception as e:
                logger.error("Migration: Log-Migration Fehler: %s", e)

        # Globale Datei umbenennen
        global_jobs_file.rename(global_jobs_file.with_suffix(".json.migrated"))
        logger.info("Migration: %d Jobs in per-Character Storage verschoben", migrated_count)

    def _load_all_character_jobs(self):
        """Durchsucht alle Character-Verzeichnisse und laedt Scheduler-Jobs.

        Scannt sowohl das aktive Storage-Verzeichnis (worlds/) als auch
        das Legacy-Verzeichnis (storage/users/) fuer Abwaertskompatibilitaet.
        """
        scanned_dirs = set()

        # 1. Aktives Storage-Verzeichnis (worlds/{name}/characters/)
        try:
            from app.core.paths import get_storage_dir
            storage_dir = get_storage_dir()
            for subdir_name in ("characters", "agents"):
                characters_dir = storage_dir / subdir_name
                if characters_dir.exists() and str(characters_dir) not in scanned_dirs:
                    scanned_dirs.add(str(characters_dir))
                    self._load_jobs_from_characters_dir(characters_dir)
        except Exception as e:
            logger.warning("Fehler beim Laden aus Storage-Dir: %s", e)

        # 2. Legacy: storage/users/{user}/characters/
        users_dir = self.project_root / "storage" / "users"
        if users_dir.exists():
            for user_dir in users_dir.iterdir():
                if not user_dir.is_dir():
                    continue
                for subdir_name in ("characters", "agents"):
                    characters_dir = user_dir / subdir_name
                    if characters_dir.exists() and str(characters_dir) not in scanned_dirs:
                        scanned_dirs.add(str(characters_dir))
                        self._load_jobs_from_characters_dir(characters_dir)

        self.jobs_data["metadata"]["total_jobs"] = len(self.jobs_data["jobs"])
        logger.info("%d Jobs aus Character-Verzeichnissen geladen", len(self.jobs_data["jobs"]))

        # 3. Daily Schedules re-syncen: Falls ein Character eine daily_schedule.json
        #    hat aber keinen passenden Job im Speicher, wird der Job neu erstellt.
        self._resync_daily_schedules()

    def _load_jobs_from_characters_dir(self, characters_dir: Path):
        """Laedt Jobs aus einem characters/-Verzeichnis — DB-first, JSON-Fallback."""
        from app.models.character import get_character_scheduler_jobs
        for char_dir in characters_dir.iterdir():
            if not char_dir.is_dir():
                continue
            char_name = char_dir.name
            try:
                jobs = get_character_scheduler_jobs(char_name)
            except Exception as e:
                logger.error("Fehler beim DB-Laden der Jobs fuer %s: %s", char_name, e)
                # JSON-Fallback
                jobs_path = char_dir / "scheduler" / "jobs.json"
                if not jobs_path.exists():
                    continue
                try:
                    data = json.loads(jobs_path.read_text(encoding="utf-8"))
                    jobs = data.get("jobs", []) if isinstance(data, dict) else data
                except Exception as e2:
                    logger.error("Fehler beim JSON-Laden von %s: %s", jobs_path, e2)
                    continue

            for job in jobs:
                # character-Feld sicherstellen
                if not job.get("character") and job.get("agent"):
                    job["character"] = job["agent"]
                if not job.get("character"):
                    job["character"] = char_name
                # Duplikate vermeiden
                if not any(j["id"] == job["id"] for j in self.jobs_data["jobs"]):
                    self.jobs_data["jobs"].append(job)
                    if job.get("enabled", True):
                        self._schedule_job(job)

    def _resync_daily_schedules(self):
        """Phase-2/4 Cleanup: Daily-Schedule-Force ist weg, der Tagesablauf
        wirkt nur noch als Hint im AgentLoop (``daily_schedule_block``).

        Hier werden noch Legacy-Cron-Job-Eintraege aus der jobs-Liste
        entfernt (per-char ``daily_schedule`` Type + ``daily_schedule_marker``
        Stubs), aber keine neuen Marker mehr angelegt. Welt-administrative
        Aufgaben laufen ueber den ``world_admin_tick`` in
        ``app/core/periodic_jobs.py`` (nicht mehr ueber diesen Scheduler).
        """
        try:
            stale_types = {"daily_schedule", "daily_schedule_marker"}
            stale = [j for j in list(self.jobs_data["jobs"])
                     if (j.get("action", {}) or {}).get("type") in stale_types]
            for j in stale:
                logger.info("Entferne Legacy daily-Job: %s", j.get("id"))
                self.remove_job(j["id"])
            self._ensure_world_hourly_job()
        except Exception as e:
            logger.warning("Daily-Schedule Resync fehlgeschlagen: %s", e)

    def _save_jobs_for_character(self, character: str):
        """Speichert nur die Jobs eines bestimmten Characters."""
        if not character:
            return

        from app.models.character import save_character_scheduler_jobs

        char_jobs = [
            j for j in self.jobs_data["jobs"]
            if j.get("character") == character or j.get("agent") == character
        ]
        try:
            save_character_scheduler_jobs(character, char_jobs)
        except Exception as e:
            logger.error("Fehler beim Speichern fuer %s: %s", character, e)

        self.jobs_data["metadata"]["last_updated"] = datetime.now().isoformat()
        self.jobs_data["metadata"]["total_jobs"] = len(self.jobs_data["jobs"])

    def _schedule_job(self, job_config: Dict[str, Any]):
        """Plant einen Job im Scheduler basierend auf Konfiguration."""
        job_id = job_config.get('id')
        trigger_config = job_config.get('trigger', {})
        trigger_type = trigger_config.get('type', 'interval')

        # Marker-Jobs sind rein visuell (z.B. Tagesablauf-Indikator pro Char) —
        # nicht im APScheduler registrieren, nur in jobs_data fuehren.
        if trigger_type == 'marker':
            return

        try:
            # Random Offset (Jitter) in Sekunden
            jitter_seconds = int(trigger_config.get('random_offset_minutes', 0)) * 60

            if trigger_type == 'interval':
                kwargs = dict(
                    seconds=trigger_config.get('seconds', 0),
                    minutes=trigger_config.get('minutes', 0),
                    hours=trigger_config.get('hours', 0),
                    days=trigger_config.get('days', 0))
                total = kwargs['seconds'] + kwargs['minutes'] * 60 + kwargs['hours'] * 3600 + kwargs['days'] * 86400
                if total == 0:
                    logger.warning("Job %s ist als Interval-Job geplant, aber die Frequenz ist nicht angegeben. Ueberspringe.", job_id)
                    return
                if jitter_seconds > 0:
                    kwargs['jitter'] = jitter_seconds

                # start_date berechnen damit der Timer nach Server-Neustarts
                # korrekt weiterlaeuft statt sich jedes Mal zurueckzusetzen.
                last_exec = job_config.get('last_execution', {}).get('timestamp')
                created_at = job_config.get('created_at')
                anchor = last_exec or created_at
                if anchor:
                    try:
                        anchor_dt = datetime.fromisoformat(anchor)
                        kwargs['start_date'] = anchor_dt
                        logger.debug("Interval-Job %s: start_date=%s (aus %s)",
                                     job_id, anchor_dt, "last_execution" if last_exec else "created_at")
                    except (ValueError, TypeError):
                        pass  # Fallback: kein start_date → default (now)

                trigger = IntervalTrigger(**kwargs)
            elif trigger_type == 'cron':
                kwargs = dict(
                    hour=trigger_config.get('hour'),
                    minute=trigger_config.get('minute'),
                    day=trigger_config.get('day'),
                    month=trigger_config.get('month'),
                    day_of_week=trigger_config.get('day_of_week'))
                if jitter_seconds > 0:
                    kwargs['jitter'] = jitter_seconds
                trigger = CronTrigger(**kwargs)
            elif trigger_type == 'date':
                run_date = trigger_config.get('run_date')
                # Stale-Date-Check: wenn run_date >3 Tage in der Vergangenheit
                # liegt, Job NICHT registrieren und aus jobs_data entfernen.
                # Sonst feuert APScheduler einen "missed by N days"-Warning und
                # versucht ggf. nachzuholen — selten sinnvoll.
                try:
                    rd = datetime.fromisoformat(run_date) if isinstance(run_date, str) else run_date
                    if rd and (datetime.now() - rd).total_seconds() > 3 * 86400:
                        logger.info("Stale Date-Job %s uebersprungen (run_date %s liegt >3 Tage zurueck) — wird entfernt",
                                     job_id, run_date)
                        self._purge_job_from_data(job_id)
                        return
                except Exception as _stale_e:
                    logger.debug("Stale-Check fuer Job %s fehlgeschlagen: %s", job_id, _stale_e)
                trigger = DateTrigger(run_date=run_date)
            else:
                logger.warning("Unbekannter Trigger-Typ: %s", trigger_type)
                return

            # misfire_grace_time=3 Tage: Misses innerhalb 3 Tagen werden
            # ausgefuehrt, danach silent skip (nicht mehr "missed by N days").
            self.scheduler.add_job(
                func=self._execute_job,
                trigger=trigger,
                args=[job_config],
                id=job_id,
                name=job_config.get('name', job_id),
                replace_existing=True,
                misfire_grace_time=3 * 86400,
            )

            jitter_info = f", jitter={jitter_seconds}s" if jitter_seconds > 0 else ""
            logger.info("Job geplant: %s (%s%s)", job_id, trigger_type, jitter_info)
        except Exception as e:
            logger.error("Fehler beim Planen von Job %s: %s", job_id, e)

    def _purge_job_from_data(self, job_id: str):
        """Entfernt einen Job aus jobs_data + persistiert pro Character.
        Wird fuer Stale-Date-Jobs genutzt, die beim Laden ueber 3 Tage alt sind.
        """
        try:
            removed = []
            kept = []
            for j in self.jobs_data.get('jobs', []):
                if j.get('id') == job_id:
                    removed.append(j)
                else:
                    kept.append(j)
            if not removed:
                return
            self.jobs_data['jobs'] = kept
            chars = {j.get('character', '') for j in removed if j.get('character')}
            from app.models.character import save_character_scheduler_jobs
            for ch in chars:
                ch_jobs = [j for j in kept if j.get('character') == ch]
                save_character_scheduler_jobs(ch, ch_jobs)
        except Exception as e:
            logger.error("Stale-Job-Purge fuer %s fehlgeschlagen: %s", job_id, e)

    def _execute_job(self, job_config: Dict[str, Any]):
        """Fuehrt einen Job aus basierend auf seiner Konfiguration."""
        job_id = job_config.get('id')
        action = job_config.get('action', {})
        action_type = action.get('type')
        user_id = job_config.get('user_id', '')
        agent = job_config.get('character', job_config.get('agent', ''))

        logger.info("Fuehre Job aus: %s (%s)", job_id, action_type)

        # Sleep-Check: Schlafende Characters fuehren keine Jobs aus.
        if agent and user_id:
            from app.models.character import is_character_sleeping
            if is_character_sleeping(agent):
                logger.info("Job %s uebersprungen: %s schlaeft", job_id, agent)
                self._log_execution(job_id, "skipped", {"reason": "Character schlaeft"})
                return

        try:
            result = None

            if action_type == 'send_message':
                result = self._action_send_message(action, agent)
            elif action_type == 'notify':
                result = self._action_notify(action, agent)
            elif action_type == 'execute_tool':
                # Phase-4 Cleanup: execute_tool als Char-getriebene Action
                # entfernt — Tool-Use laeuft ueber den AgentLoop (Char
                # entscheidet selbst). Bestehende Jobs werden ignoriert.
                logger.warning("Action 'execute_tool' ist deaktiviert (Phase-4 Cleanup) — "
                               "Job %s ignoriert (tool=%s)", job_id,
                               action.get("tool_name") or "?")
                result = {"success": False, "error": "execute_tool action is deactivated"}
            elif action_type == 'set_status':
                result = self._action_set_status(action, agent)
            elif action_type == 'daily_schedule':
                # Phase-2 Cleanup: daily_schedule wird nicht mehr enforced —
                # AgentLoop liest die Daten als Hint via daily_schedule_block.
                logger.warning("Action 'daily_schedule' ist deaktiviert (Phase-2 Cleanup) — "
                               "Char %s wird vom AgentLoop selbst gesteuert", agent)
                result = {"success": False, "error": "daily_schedule action is deactivated"}
            elif action_type == 'world_hourly_tick':
                # Obsolet — durch zentralen World-Admin-Tick ersetzt
                # (app/core/periodic_jobs.py). Defensiv als No-Op behalten
                # falls ein gespeicherter Job-Eintrag noch nicht via
                # _purge_legacy_world_hourly_job entfernt wurde.
                result = {"success": True, "action": "world_hourly_tick",
                          "note": "deprecated — handled by app.core.periodic_jobs"}
            elif action_type == 'extract_files':
                result = self._action_extract_files(action, agent)
            elif action_type == 'custom':
                # Phase-1 Cleanup: 'custom' war ein totes Stub-Feature ohne
                # echte Funktion. Bestehende custom-Jobs werden mit Warning
                # ignoriert; das Action-Handler-Method ist entfernt.
                logger.warning("Action 'custom' ist deaktiviert (Phase-1 Cleanup) — Job %s ignoriert",
                               action.get("function") or "?")
                result = {"success": False, "error": "action 'custom' is disabled"}
            else:
                logger.warning("Unbekannter Action-Typ: %s", action_type)
                result = {"success": False, "error": f"Unknown action type: {action_type}"}

            self._log_execution(job_id, "success", result)
            logger.info("Job erfolgreich: %s", job_id)
            job_config["last_execution"] = {
                "timestamp": datetime.now().isoformat(),
                "success": True
            }

        except Exception as e:
            logger.error("Fehler beim Ausfuehren von Job %s: %s", job_id, e)
            self._log_execution(job_id, "error", {"error": str(e)})
            job_config["last_execution"] = {
                "timestamp": datetime.now().isoformat(),
                "success": False
            }

        # One-time (date trigger) Jobs nach Ausführung entfernen
        if job_config.get('trigger', {}).get('one_time'):
            try:
                self.jobs_data['jobs'] = [
                    j for j in self.jobs_data['jobs'] if j.get('id') != job_id
                ]
                logger.info("One-time Job entfernt: %s", job_id)
            except Exception as e:
                logger.error("One-time Job cleanup: %s", e)

        # last_execution persistieren
        if agent:
            try:
                self._save_jobs_for_character(agent)
            except Exception as e:
                logger.error("Fehler beim Speichern von last_execution: %s", e)

    def _action_send_message(self, action: Dict[str, Any], agent: str) -> Dict[str, Any]:
        """Phase-3: Statt der Nachricht hart in die History zu schreiben,
        bump'en wir den Charakter im AgentLoop mit einem Hint.

        Der Char sieht beim naechsten Thought-Turn den Hint "Du wolltest
        diese Nachricht senden: <text>" und entscheidet selbst ob er sie
        sendet (per SendMessage Tool), die Wortwahl anpasst oder skippt
        weil's nicht mehr passt.
        """
        message = (action.get('message') or '').strip()
        if not message:
            return {"success": False, "error": "Keine Nachricht angegeben"}
        if not agent:
            return {"success": False, "error": "Kein Agent gesetzt"}

        try:
            from app.core.agent_loop import get_agent_loop
            from app.models.account import get_active_character
            avatar = (get_active_character() or "user").strip()
            hint = (
                f"You scheduled a message for {avatar}: \"{message}\". "
                f"Decide now whether to send it via SendMessage (you may "
                f"adjust the wording), or skip if it's no longer relevant."
            )
            ok = get_agent_loop().bump(agent, hint=hint)
            if ok:
                logger.info("send_message: %s gebumpt mit hint (%d chars)",
                            agent, len(message))
                return {"success": True, "action": "send_message",
                        "delivered_via": "agent_loop_bump"}
            return {"success": False,
                    "error": f"AgentLoop.bump abgelehnt fuer {agent} (ineligible)"}
        except Exception as e:
            logger.error("send_message bump fehlgeschlagen: %s", e)
            return {"success": False, "error": str(e)}

    def _action_notify(self, action: Dict[str, Any], agent: str) -> Dict[str, Any]:
        """Erstellt nur eine Notification (kein Chat-POST).

        Fuer leichtgewichtige Benachrichtigungen wie Status-Updates.
        """
        message = action.get('message', '')
        notification_type = action.get('notification_type', 'system')
        metadata = action.get('metadata', {})
        if not message:
            return {"success": False, "error": "Keine Nachricht angegeben"}
        try:
            from app.models.notifications import create_notification
            nid = create_notification(
                character=agent,
                content=message,
                notification_type=notification_type,
                metadata=metadata)
            logger.info("Notification erstellt: %s (%s)", nid, agent)
            return {"success": True, "action": "notify", "notification_id": nid}
        except Exception as e:
            logger.error("Fehler bei notify: %s", e)
            return {"success": False, "error": str(e)}


    def _action_set_status(self, action: Dict[str, Any], agent: str) -> Dict[str, Any]:
        """Setzt Location, Activity und/oder Mood eines Characters direkt.

        location in action ist eine Location-ID (nach Migration).
        Player-controlled characters are skipped (no autonomous status changes).

        Wenn der Character vor <RECENT_CHAT_GRACE_MINUTES> Minuten via Chat/User-
        Interaktion Activity oder Location gesetzt bekam, ueberspringt der
        Scheduler ihn ebenfalls — sonst wuerde der stuendliche Tick eine
        laufende Szene (Sex, Gespraech, Tanz) ungefragt abbrechen und den
        Character in einen anderen Raum schicken.
        """
        from app.models.account import is_player_controlled
        if is_player_controlled(agent):
            return {"skipped": True, "reason": "Character player-controlled"}

        # Grace-Window gegen Chat-Interferenz
        try:
            from datetime import datetime, timedelta
            from app.models.character import get_character_profile
            _profile = get_character_profile(agent) or {}
            RECENT_CHAT_GRACE_MINUTES = 30
            _cutoff = datetime.now() - timedelta(minutes=RECENT_CHAT_GRACE_MINUTES)
            for _ts_field in ("activity_changed_at", "location_changed_at"):
                _ts = (_profile.get(_ts_field) or "").strip()
                if not _ts:
                    continue
                try:
                    _dt = datetime.fromisoformat(_ts)
                except Exception:
                    continue
                if _dt > _cutoff:
                    logger.info("Scheduler skip %s: %s kuerzlich gesetzt (%s)",
                                 agent, _ts_field, _ts[:19])
                    return {"skipped": True,
                            "reason": f"{_ts_field} within {RECENT_CHAT_GRACE_MINUTES}min"}
        except Exception as _ge:
            logger.debug("Grace-Check fuer %s fehlgeschlagen: %s", agent, _ge)

        location = action.get('location', '')
        activity = action.get('activity', '')
        mood = action.get('mood', '')

        # Duration auto-complete: Aktivitaet beenden + on_complete Trigger
        if activity == '__default__':
            try:
                from app.models.character import (
                    get_character_current_location, get_character_current_activity,
                    save_character_current_activity)
                completed_activity = action.get('_completed_activity', '')
                current_act = get_character_current_activity(agent)

                # Nur wenn der Character noch die gleiche Aktivitaet macht
                if current_act and completed_activity and current_act.lower() == completed_activity.lower():
                    save_character_current_activity(agent, '')
                    logger.info("Duration abgelaufen: %s beendet '%s'", agent, completed_activity)

                    # on_complete Trigger ausfuehren
                    trigger_json = action.get('_on_complete_trigger', '')
                    if trigger_json:
                        import json as _json
                        from app.core.activity_engine import execute_trigger
                        trigger_config = _json.loads(trigger_json)
                        execute_trigger(agent, trigger_config)
                else:
                    logger.info("Duration-Job ignoriert: %s macht nicht mehr '%s' (aktuell: %s)",
                                agent, completed_activity, current_act)

                return {"success": True, "action": "duration_complete", "activity": completed_activity}
            except Exception as e:
                logger.error("Duration auto-complete Fehler: %s", e)
                return {"success": False, "error": str(e)}

        # Phase-2 Cleanup: __llm_choice__ als Force-Slot entfernt — wenn ein
        # alter set_status-Job mit __llm_choice__ ankommt, abbrechen statt
        # einen LLM-Call zu triggern. Der AgentLoop entscheidet selbst.
        if location == "__llm_choice__":
            return {"success": False, "error":
                    "__llm_choice__ slot ignored — AgentLoop chooses autonomously"}

        # Location per ID/Name aufloesen — sicherstellen dass wir eine ID haben
        from app.models.world import get_location, get_location_name as _get_loc_name, resolve_location
        if location:
            loc_obj = resolve_location(location)
            if loc_obj:
                location = loc_obj.get("id", location)

        # Raum bestimmen: entweder aus Activity ableiten oder zufaellig waehlen
        # Waehlt einen zufaelligen Raum (der Aktivitaeten hat) und eine
        # zufaellige Aktivitaet daraus, damit Raum und Aktivitaet zusammenpassen.
        random_room_id = ''
        if activity and activity != '__random__' and location:
            # Feste Activity: passenden Raum finden
            try:
                from app.models.world import find_room_by_activity
                loc_data = get_location(location)
                if loc_data:
                    matched_room = find_room_by_activity(loc_data, activity)
                    if matched_room:
                        random_room_id = matched_room.get('id', '')
                    elif loc_data.get('rooms'):
                        # Kein Raum passt zur Activity: zufaelligen waehlen
                        import random
                        random_room_id = random.choice(loc_data['rooms']).get('id', '')
            except Exception as e:
                logger.error("Fehler bei Raum-Bestimmung fuer Activity '%s': %s", activity, e)
        if activity == '__random__' and location:
            try:
                import random
                from app.models.activity_library import get_available_activities
                # Bibliothek: gefiltert nach Conditions, Cooldowns, requires_partner
                available = get_available_activities(agent, location, filter_conditions=True
                )
                # Scheduler darf NICHT autonom requires_partner-Aktivitaeten waehlen —
                # auch wenn ein Partner anwesend ist. Partneraktivitaeten brauchen
                # expliziten Chat-/User-Kontext; der Zufallspicker wuerde sonst
                # "Sex um 10 Uhr im Buero" o.ae. triggern.
                # auto_pick=false filtert zusaetzlich "stille" Activities raus
                # (sleeping, orgasm, masturbating ...), die nur via Force-Rules,
                # __sleep__-Slot, Chat-LLM oder Follow-Up getriggert werden sollen.
                available = [a for a in available
                             if not a.get("requires_partner")
                             and a.get("auto_pick", True) is not False]
                if available:
                    chosen = random.choice(available)
                    activity = chosen.get("name", "")
                    # Passenden Raum finden
                    loc_data = get_location(location)
                    if loc_data:
                        from app.models.world import find_room_by_activity
                        matched_room = find_room_by_activity(loc_data, activity)
                        if matched_room:
                            random_room_id = matched_room.get('id', '')
                    logger.info("Scheduler[%s]: zufaellige Aktivitaet '%s' (aus %d verfuegbaren)",
                                agent, activity, len(available))
                else:
                    logger.info("Scheduler[%s]: keine verfuegbare Aktivitaet am Ort %s",
                                agent, location)
                    activity = ''
            except Exception as e:
                logger.error("Fehler bei zufaelliger Aktivitaet: %s", e)
                activity = ''

        if not location:
            return {"success": False, "error": "Kein Ort angegeben"}
        if not agent:
            return {"success": False, "error": "Kein Character angegeben"}

        try:
            from app.models.character import (
                save_character_current_location, save_character_current_activity,
                save_character_current_feeling, save_character_current_room,
                get_character_current_location, get_character_config)

            # location ist eine ID (nach Migration) — direkt speichern
            if location:
                # Leave-Check: Pinning/Confine-Rule darf vom Scheduler nicht
                # umgangen werden. Wenn der Char am aktuellen Ort gehalten
                # wird, ueberspringt der Scheduler den Move komplett.
                try:
                    from app.models.rules import check_leave
                    cur_loc_for_leave = get_character_current_location(agent) or ""
                    if cur_loc_for_leave:
                        leave_ok, leave_reason = check_leave(
                            agent,
                            target_location_id=location,
                            target_room_id=random_room_id or "")
                        if not leave_ok:
                            logger.info("Scheduler: Leave blockiert %s (cur=%s -> tgt=%s): %s",
                                        agent, cur_loc_for_leave, location, leave_reason)
                            try:
                                from app.models.character import record_access_denied
                                from app.models.world import get_location_name as _gln
                                _cur_name = _gln(cur_loc_for_leave) or cur_loc_for_leave
                                record_access_denied(agent, cur_loc_for_leave, _cur_name,
                                                      leave_reason, action="leave")
                            except Exception:
                                logger.debug("record_access_denied(scheduler-leave) failed", exc_info=True)
                            location = cur_loc_for_leave
                            activity = ""
                            random_room_id = ""
                except Exception:
                    pass

                # Rules-Check: darf der Character diesen Ort/Raum betreten?
                # Zwei Stufen: erst Location, dann Raum (falls Raum gewaehlt).
                try:
                    from app.models.rules import check_access
                    rules_ok, rules_reason = check_access(agent, location)
                    # Wenn Location OK und ein Raum gewaehlt wurde, auch Raum-Ebene pruefen
                    if rules_ok and random_room_id:
                        rules_ok, rules_reason = check_access(
                            agent, location, room_id=random_room_id)
                    if not rules_ok:
                        logger.info("Scheduler: Rule blockiert %s -> %s%s",
                                    agent, location,
                                    f"/{random_room_id}" if random_room_id else "")
                        try:
                            from app.models.character import record_access_denied
                            from app.models.world import get_location_name
                            loc_name = get_location_name(location) or location
                            record_access_denied(agent, location, loc_name, rules_reason)
                        except Exception:
                            logger.debug("record_access_denied failed", exc_info=True)
                        location = get_character_current_location(agent)  # bleiben
                        # Activity/Raum verwerfen — passen zur blockierten Location, nicht zur aktuellen
                        activity = ""
                        random_room_id = ""
                except Exception:
                    pass
                old_loc = get_character_current_location(agent)
                save_character_current_location(agent, location)
                # Raum und Aktivitaet zuruecksetzen wenn Ort gewechselt
                if location != old_loc:
                    save_character_current_room(agent, random_room_id)
                    if not activity:
                        save_character_current_activity(agent, '')
                elif random_room_id:
                    save_character_current_room(agent, random_room_id)
            if activity:
                # Partner aus dem Condition-Matching der GEWAEHLTEN Aktivitaet
                # neu auflösen. get_last_matched_partner() reflektiert sonst die
                # zuletzt evaluierte Condition (z.B. aus get_available_activities),
                # was nicht unbedingt die gewaehlte Activity war.
                matched_partner = ""
                try:
                    from app.core.activity_engine import (
                        evaluate_condition as _eval_cond,
                        get_last_matched_partner)
                    from app.models.activity_library import (
                        get_library_activity, find_library_activity_by_name)
                    _act_def = get_library_activity(activity) or find_library_activity_by_name(activity)
                    _cond = (_act_def or {}).get("condition", "")
                    if _cond:
                        _eval_cond(_cond, agent, location)
                        matched_partner = get_last_matched_partner() or ""
                except Exception:
                    pass

                save_character_current_activity(agent, activity,
                    partner=matched_partner)

                # Partner-Activity transferieren: wenn die gewaehlte Aktivitaet
                # einen Partner hat und dieser NICHT player-controlled ist, beim
                # Partner die Gegen-Aktivitaet setzen (er bleibt am Ort gebunden).
                if matched_partner:
                    self._transfer_partner_activity(agent, matched_partner, activity, location)
            if mood:
                save_character_current_feeling(agent, mood)

            loc_display = _get_loc_name(location)
            parts = []
            if location:
                parts.append(f"{loc_display}" + (f" ({activity})" if activity else ""))
            if mood:
                parts.append(f"Mood: {mood}")
            logger.info("Status gesetzt: %s -> %s", agent, ", ".join(parts))

            # Social Dialog: Pruefen ob andere Characters am selben Ort
            if location:
                self._try_social_dialog(agent, location)

            return {
                "success": True,
                "action": "set_status",
                "location": location,
                "activity": activity,
                "mood": mood,
            }
        except Exception as e:
            logger.error("Fehler bei set_status: %s", e)
            return {"success": False, "error": str(e)}

    def _transfer_partner_activity(
        self, initiator: str, partner: str,
        activity_name: str, location: str,
        allow_player: bool = False) -> None:
        """Setzt beim Partner die Gegenaktivitaet einer partner-basierten Activity.

        z.B. Vallerie waehlt Sex mit Diego als Partner -> Diegos Activity wird
        auch auf 'Sex' gesetzt, sein Partner auf 'Vallerie'. Seine Location wird
        nicht geaendert; er bleibt also am selben Ort.

        Scheduler-driven: Player-controlled Characters NICHT automatisch umsetzen.
        Chat-driven (allow_player=True): Auch den Avatar mitsetzen — der Spieler
        hat durch den Chat-Input implizit zugestimmt.
        """
        from app.models.activity_library import (
            get_library_activity, find_library_activity_by_name)
        from app.models.account import is_player_controlled
        from app.models.character import (
            save_character_current_activity,
            get_character_current_location)

        if not partner or partner == initiator:
            return

        # Player-controlled: nur im Scheduler-Pfad ueberspringen
        if not allow_player:
            try:
                if is_player_controlled(partner):
                    logger.info("Partner-Transfer uebersprungen: %s ist player-controlled", partner)
                    return
            except Exception:
                pass

        # Partner muss am gleichen Ort sein, sonst kein Transfer
        try:
            partner_loc = get_character_current_location(partner) or ""
        except Exception:
            partner_loc = ""
        if partner_loc != location:
            logger.info("Partner-Transfer uebersprungen: %s nicht an %s (ist an %s)",
                        partner, location, partner_loc)
            return

        # partner_activity aus der Library auslesen; Default: gleiche Activity
        act_def = get_library_activity(activity_name) or find_library_activity_by_name(activity_name)
        partner_activity_id = (act_def or {}).get("partner_activity", "")
        partner_activity_name = activity_name  # Fallback
        if partner_activity_id:
            _p_def = get_library_activity(partner_activity_id) or find_library_activity_by_name(partner_activity_id)
            if _p_def:
                partner_activity_name = _p_def.get("name", partner_activity_id)

        save_character_current_activity(partner, partner_activity_name,
            partner=initiator,
            _skip_partner_transfer=True)
        logger.info("Partner-Transfer: %s -> Activity '%s' (mit %s)",
                    partner, partner_activity_name, initiator)

    def _is_partner_locked(self, character: str) -> bool:
        """Prueft ob ein Character in einer Partner-Aktivitaet gebunden ist.

        True wenn:
          - current_activity ist eine Library-Activity mit requires_partner=true
          - AND duration ist noch nicht abgelaufen (activity_started_at + duration > now)
        """
        try:
            from app.models.character import get_character_profile
            from app.models.activity_library import (
                get_library_activity, find_library_activity_by_name)
            profile = get_character_profile(character) or {}
            current = profile.get("current_activity", "")
            if not current:
                return False
            act_def = get_library_activity(current) or find_library_activity_by_name(current)
            if not act_def or not act_def.get("requires_partner"):
                return False
            duration = int(act_def.get("duration_minutes", 0) or 0)
            if duration <= 0:
                return False
            # Startzeit aus activity_started_at oder aus state_history ableiten
            started_iso = profile.get("activity_started_at", "")
            if not started_iso:
                # Fallback: letzte activity-Aenderung
                for e in reversed(profile.get("_state_history_cache", [])[-20:]):
                    if e.get("type") == "activity" and e.get("value") == current:
                        started_iso = e.get("timestamp", "")
                        break
            if not started_iso:
                return False
            try:
                started = datetime.fromisoformat(started_iso)
                elapsed_min = (datetime.now() - started).total_seconds() / 60
                return elapsed_min < duration
            except (ValueError, TypeError):
                return False
        except Exception:
            return False

    def _try_social_dialog(self, agent: str, location: str):
        """Prueft ob andere Characters am selben Ort sind und startet ggf. einen Dialog."""
        import random
        from app.models.character import (
            list_available_characters, get_character_current_location,
            get_character_config, is_character_sleeping)
        from app.models.character_template import is_feature_enabled as _feat

        # Feature-Gate: wenn Initiator social_dialog nicht hat, gar nicht starten
        if not _feat(agent, "social_dialog_enabled"):
            return

        all_chars = list_available_characters()
        chars_at_location = [
            c for c in all_chars
            if c != agent and get_character_current_location(c) == location
            and not is_character_sleeping(c)
            and _feat(c, "social_dialog_enabled")
        ]

        if not chars_at_location:
            return

        agent_config = get_character_config(agent)
        agent_prob = int(agent_config.get("social_dialog_probability", 50))

        for other in chars_at_location:
            other_config = get_character_config(other)
            other_prob = int(other_config.get("social_dialog_probability", 50))

            # Wahrscheinlichkeit = Minimum beider Werte
            probability = min(agent_prob, other_prob)
            roll = random.randint(1, 100)

            if roll > probability:
                logger.debug("SocialDialog %s <-> %s: Skip (Roll %d > %d%%)", agent, other, roll, probability)
                continue

            logger.info("SocialDialog %s <-> %s: Dialog! (Roll %d <= %d%%)", agent, other, roll, probability)

            # Async ausfuehren via BackgroundQueue
            from app.core.background_queue import get_background_queue
            get_background_queue().submit("social_dialog", {
                "user_id": "",
                "sender": agent,
                "target": other,
                "location": location,
            })

    def _action_extract_files(self, action: Dict[str, Any], agent: str) -> Dict[str, Any]:
        """Administrative File-Extraction-Action.

        Phase-4 Cleanup: ruft den KnowledgeExtract-Skill jetzt direkt auf
        (vorher Bruecke ueber das entfernte ``_action_execute_tool``).
        Bleibt admin-only — kein Char-Verhalten, kein LLM-im-Char-Namen.
        """
        try:
            from app.core.dependencies import get_skill_manager
            sm = get_skill_manager()
            skill = sm.get_skill_by_name("KnowledgeExtract")
            if not skill:
                return {"success": False, "error": "KnowledgeExtract Skill nicht geladen"}
            payload = json.dumps({
                "input": action.get("extraction_prompt", ""),
                "agent_name": agent,
                "user_id": "",
            })
            result = skill.execute(payload)
            logger.info("KnowledgeExtract-Ergebnis: %s",
                        result[:200] if result else "")
            return {"success": True, "action": "extract_files",
                    "result": result[:500] if result else ""}
        except Exception as e:
            logger.error("Fehler bei extract_files: %s", e)
            return {"success": False, "error": str(e)}

    def _log_execution(self, job_id: str, status: str, result: Any):
        """Loggt Job-Ausfuehrung in per-Character Log-Datei"""
        from app.models.character import get_character_scheduler_logs, save_character_scheduler_logs

        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "job_id": job_id,
            "status": status,
            "result": result
        }

        # Job finden um user_id + character zu bestimmen
        job = None
        for j in self.jobs_data["jobs"]:
            if j["id"] == job_id:
                job = j
                break

        if job:
            character = job.get("character", job.get("agent", ""))
            if character:
                try:
                    logs = get_character_scheduler_logs(character)
                    logs.append(log_entry)
                    logs = logs[-1000:]
                    save_character_scheduler_logs(character, logs)
                    return
                except Exception as e:
                    logger.error("Fehler beim Loggen: %s", e)

        # Globaler Job ohne Character-Bezug — kein per-Character-Log-File,
        # nur DEBUG-Konsole.
        logger.debug("Globaler Job-Log: %s", log_entry)

    def add_job(
        self, agent: str,
        trigger: Dict[str, Any],
        action: Dict[str, Any],
        job_id: Optional[str] = None,
        enabled: bool = True
    ) -> Dict[str, Any]:
        """
        Fuegt einen neuen Job hinzu.

        Args:
            user_id: User-ID
            agent: Character-Name
            trigger: Trigger-Konfiguration
            action: Action-Konfiguration
            job_id: Optionale Job-ID (wird generiert wenn nicht angegeben)
            enabled: Ob Job aktiviert ist
        """
        if job_id is None:
            job_id = f"{agent}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        if any(job['id'] == job_id for job in self.jobs_data['jobs']):
            return {"success": False, "error": f"Job-ID {job_id} existiert bereits"}

        job_config = {
            "id": job_id,
            "user_id": "",
            "character": agent,
            "enabled": enabled,
            "trigger": trigger,
            "action": action,
            "created_at": datetime.now().isoformat()
        }

        self.jobs_data['jobs'].append(job_config)

        if enabled:
            self._schedule_job(job_config)

        self._save_jobs_for_character(agent)

        return {
            "success": True,
            "job_id": job_id,
            "message": "Job erfolgreich hinzugefuegt"
        }

    def remove_job(self, job_id: str) -> Dict[str, Any]:
        """Entfernt einen Job"""
        job_index = None
        job = None
        for i, j in enumerate(self.jobs_data['jobs']):
            if j['id'] == job_id:
                job_index = i
                job = j
                break

        if job_index is None:
            return {"success": False, "error": f"Job {job_id} nicht gefunden"}

        try:
            self.scheduler.remove_job(job_id)
        except:
            pass

        character = job.get("character", job.get("agent", ""))
        self.jobs_data['jobs'].pop(job_index)
        self._save_jobs_for_character(character)

        return {"success": True, "message": f"Job {job_id} entfernt"}

    def sync_daily_schedule(self, character: str, schedule: Dict[str, Any]) -> int:
        """Persistiert den Tagesablauf des Characters.

        Der Tagesablauf wird seit Phase-2 nicht mehr durch Cron-Jobs
        enforced — er ist nur noch ein Hint-Block im Thought-Prompt
        (``daily_schedule_block`` in ``thought_context.py``). Hier werden
        nur die Slots validiert und Locations zu IDs aufgeloest.

        Returns: 1 wenn der Schedule aktiv ist, sonst 0.
        """
        # 1. Legacy per-Character daily-Jobs entfernen (wurden parallel ausgefuehrt -> Race)
        daily_jobs = [
            j for j in list(self.jobs_data["jobs"])
            if (j.get("character") == character or j.get("agent") == character)
            and j.get("source") == "daily_schedule"
        ]
        for j in daily_jobs:
            self.remove_job(j["id"])

        if not schedule.get("enabled", False):
            return 0

        slots = schedule.get("slots", [])
        if not slots:
            return 0

        # Location-Namen zu IDs aufloesen und im Schedule persistieren
        from app.models.world import resolve_location as _resolve_loc
        for slot in slots:
            if slot.get("sleep"):
                continue
            raw_loc = slot.get("location", "")
            if raw_loc:
                loc_obj = _resolve_loc(raw_loc)
                if loc_obj and loc_obj.get("id"):
                    slot["location"] = loc_obj["id"]

        # 2. Per-Character Marker-Job — rein visuelles Signal in der UI
        #    dass der Tagesablauf aktiv ist. Der Marker hat KEIN Cron-Trigger
        #    (er wird nicht ausgefuehrt); Welt-administrative Aufgaben
        #    (Status-Decay, Force-Rules, Random-Events, ...) laufen seit dem
        #    World-Admin-Tick-Refactor zentral in app/core/periodic_jobs.py.
        #    Der frueher hier angelegte ``world_hourly_tick``-Job war
        #    Phase-2-Cleanup-Stub und ist jetzt komplett obsolet.
        marker_id = f"daily_schedule_{character}"
        self.jobs_data["jobs"] = [
            j for j in self.jobs_data["jobs"] if j.get("id") != marker_id
        ]
        self.jobs_data["jobs"].append({
            "id": marker_id,
            "character": character,
            "enabled": True,
            "source": "daily_schedule",
            "trigger": {"type": "marker"},
            "action": {"type": "daily_schedule_marker",
                       "slots_count": len(slots)},
            "created_at": datetime.now().isoformat(),
        })
        self._save_jobs_for_character(character)
        return 1

    def _purge_legacy_world_hourly_job(self) -> int:
        """Entfernt Bestands-Eintraege des obsoleten world_hourly_tick-Jobs.

        Wird beim SchedulerManager-Init aufgerufen. Die Welt-Admin-Aktionen
        laufen seit dem Refactor zentral ueber ``app/core/periodic_jobs.py``
        (asyncio-Tick, default 60s, konfigurierbar). Der alte stuendliche
        Cron-Job war ein No-Op-Stub und konnte vom User versehentlich
        geloescht werden — also raeumen wir ihn aus den Welt-Daten raus.

        Returns: Anzahl entfernter Eintraege.
        """
        job_id = "world_hourly_tick"
        before = len(self.jobs_data["jobs"])
        self.jobs_data["jobs"] = [
            j for j in self.jobs_data["jobs"] if j.get("id") != job_id]
        removed = before - len(self.jobs_data["jobs"])
        # Auch im APScheduler entfernen falls bereits gescheduled
        try:
            if self.scheduler.get_job(job_id):
                self.scheduler.remove_job(job_id)
        except Exception:
            pass
        return removed

    def _purge_legacy_activity_done_jobs(self) -> int:
        """Entfernt obsolete ``activity_done_*``-One-Time-Jobs.

        Frueher legte ``set_activity_skill._schedule_duration_complete`` pro
        Activity-Set einen einmaligen Job an, der die Activity nach Ablauf
        wieder zurueckgesetzt hat. Seit dem Refactor laeuft das ueber den
        ``world_admin_tick`` mit profil-state-basierten Feldern
        (``activity_started_at`` + ``activity_duration_minutes``).
        Bestands-Eintraege koennten noch im Scheduler-UI auftauchen — hier
        einmal beim Init rauspurgen, plus aus dem APScheduler aushaengen.

        Returns: Anzahl entfernter Eintraege.
        """
        before = len(self.jobs_data["jobs"])
        legacy_ids = [
            j.get("id") for j in self.jobs_data["jobs"]
            if (j.get("id") or "").startswith("activity_done_")
        ]
        if not legacy_ids:
            return 0
        self.jobs_data["jobs"] = [
            j for j in self.jobs_data["jobs"]
            if not (j.get("id") or "").startswith("activity_done_")]
        for jid in legacy_ids:
            try:
                if self.scheduler.get_job(jid):
                    self.scheduler.remove_job(jid)
            except Exception:
                pass
        return before - len(self.jobs_data["jobs"])

    def _was_recently_chatting(self, character: str, minutes: int = 10) -> bool:
        """Prueft ob der Character in den letzten N Minuten im Chat mit dem User war.

        Beruecksichtigt sowohl 1:1 Chats (chat_messages-Tabelle) als auch Gruppenchats.
        """
        threshold = minutes * 60  # in Sekunden
        now = datetime.now()

        # 1:1 Chat: juengster ts in chat_messages fuer diesen Character.
        # (Frueher Filesystem-mtime; nach DB-only-Migration nicht mehr nutzbar.)
        try:
            from app.core.db import get_connection
            row = get_connection().execute(
                "SELECT ts FROM chat_messages WHERE character_name=? "
                "ORDER BY ts DESC LIMIT 1",
                (character,)).fetchone()
            if row and row[0]:
                try:
                    last_ts = datetime.fromisoformat(row[0])
                except (ValueError, TypeError):
                    last_ts = None
                if last_ts:
                    age_s = (now - last_ts).total_seconds()
                    if 0 <= age_s < threshold:
                        logger.info("Location-Wechsel blockiert: %s war vor %.0f Min im 1:1 Chat",
                                    character, age_s / 60)
                        return True
        except Exception as e:
            logger.debug("Fehler beim Pruefen der 1:1 Chat-Aktivitaet: %s", e)

        # Gruppenchat pruefen: last_activity aktiver Sessions mit diesem Character
        try:
            from app.models.group_chat import load_sessions
            sessions = load_sessions()
            for s in sessions:
                if not s.get("active", True):
                    continue
                if character not in s.get("participants", []):
                    continue
                last_activity = s.get("last_activity", "")
                if last_activity:
                    activity_ts = datetime.fromisoformat(last_activity).timestamp()
                    if (now_ts - activity_ts) < threshold:
                        logger.info("Location-Wechsel blockiert: %s war vor %.0f Min im Gruppenchat %s",
                                    character, (now_ts - activity_ts) / 60, s.get("id", "?"))
                        return True
        except Exception as e:
            logger.debug("Fehler beim Pruefen der Gruppenchat-Aktivitaet: %s", e)

        return False

    def toggle_job(self, job_id: str) -> Dict[str, Any]:
        """Aktiviert/Deaktiviert einen Job"""
        job = None
        for j in self.jobs_data['jobs']:
            if j['id'] == job_id:
                job = j
                break

        if job is None:
            return {"success": False, "error": f"Job {job_id} nicht gefunden"}

        job['enabled'] = not job.get('enabled', True)

        if job['enabled']:
            self._schedule_job(job)
        else:
            try:
                self.scheduler.remove_job(job_id)
            except:
                pass

        character = job.get("character", job.get("agent", ""))
        self._save_jobs_for_character(character)

        return {
            "success": True,
            "enabled": job['enabled'],
            "message": f"Job {job_id} {'aktiviert' if job['enabled'] else 'deaktiviert'}"
        }

    def run_job_now(self, job_id: str) -> Dict[str, Any]:
        """Fuehrt einen Job sofort aus (unabhaengig vom Schedule)"""
        job = None
        for j in self.jobs_data['jobs']:
            if j['id'] == job_id:
                job = j
                break

        if job is None:
            return {"success": False, "error": f"Job {job_id} nicht gefunden"}

        self._execute_job(job)

        return {"success": True, "message": f"Job {job_id} wird ausgefuehrt"}

    def get_jobs(self, agent: Optional[str] = None) -> List[Dict[str, Any]]:
        """Gibt alle Jobs zurueck (optional gefiltert nach Character)"""
        jobs = self.jobs_data['jobs']

        if agent:
            jobs = [j for j in jobs if j.get('character') == agent or j.get('agent') == agent]

        return jobs

    def get_job_logs(self, job_id: Optional[str] = None, limit: int = 100,
                     character: Optional[str] = None) -> List[Dict[str, Any]]:
        """Gibt Job-Logs zurueck (optional gefiltert)"""
        from app.models.character import get_character_scheduler_logs

        # Per-Character Logs laden
        if character:
            logs = get_character_scheduler_logs(character)
            if job_id:
                logs = [log for log in logs if log.get('job_id') == job_id]
            return logs[-limit:]

        # Job-ID gegeben: Character aus Job bestimmen
        if job_id:
            for j in self.jobs_data["jobs"]:
                if j["id"] == job_id:
                    char = j.get("character", j.get("agent", ""))
                    if char:
                        logs = get_character_scheduler_logs(char)
                        logs = [log for log in logs if log.get('job_id') == job_id]
                        return logs[-limit:]

        # Kein Filter: alle Logs aggregieren (DB-first, JSON-Fallback)
        from app.models.character import (
            list_available_characters, get_character_scheduler_logs)
        all_logs = []
        try:
            all_chars = list_available_characters()
        except Exception:
            all_chars = []
        for char in all_chars:
            try:
                all_logs.extend(get_character_scheduler_logs(char))
            except Exception:
                pass
        # JSON-Fallback fuer nicht gefundene Characters
        if not all_logs:
            users_dir = self.project_root / "storage" / "users"
            if users_dir.exists():
                for user_dir in users_dir.iterdir():
                    if not user_dir.is_dir():
                        continue
                    for subdir_name in ("characters", "agents"):
                        characters_dir = user_dir / subdir_name
                        if not characters_dir.exists():
                            continue
                        for char_dir in characters_dir.iterdir():
                            logs_path = char_dir / "scheduler" / "job_logs.json"
                            if logs_path.exists():
                                try:
                                    logs = json.loads(logs_path.read_text(encoding="utf-8"))
                                    all_logs.extend(logs)
                                except Exception:
                                    pass

        all_logs.sort(key=lambda x: x.get("timestamp", ""))
        return all_logs[-limit:]

    def shutdown(self):
        """Faehrt den Scheduler herunter"""
        logger.info("Fahre Scheduler herunter...")
        self.scheduler.shutdown()


def _was_chatted_recently(character_name: str,
                          within_minutes: int = 10) -> bool:
    """Liefert True wenn der letzte Chat mit diesem Character juenger als
    ``within_minutes`` Minuten ist.

    Liest die neueste ``ts`` aus ``chat_messages`` (world.db). Frueher
    ``chats/*.json``-mtime, was seit dem unified_chat-Refactor nichts mehr
    findet.
    """
    try:
        from app.core.db import get_connection
        from datetime import datetime
        conn = get_connection()
        row = conn.execute(
            "SELECT ts FROM chat_messages WHERE character_name=? "
            "ORDER BY ts DESC LIMIT 1",
            (character_name,)).fetchone()
        if not row or not row[0]:
            return False
        last_ts = datetime.fromisoformat(row[0])
        age_s = (datetime.now() - last_ts).total_seconds()
        return age_s < within_minutes * 60
    except Exception:
        return False
