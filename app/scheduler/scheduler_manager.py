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
        """Stellt den globalen world_hourly_tick-Job sicher.

        Legacy per-character Jobs ({char}_daily) werden entfernt, da sie
        parallel ausgefuehrt wurden (Race-Condition). Stattdessen arbeitet
        der world_hourly_tick alle Characters sequenziell ab.
        """
        try:
            from app.models.character import (
                list_available_characters, get_character_daily_schedule)

            # 1. Legacy per-character daily-Jobs entfernen
            legacy_jobs = [
                j for j in list(self.jobs_data["jobs"])
                if j.get("source") == "daily_schedule"
                and j.get("action", {}).get("type") == "daily_schedule"
                and j.get("character")  # per-char jobs haben einen character-Namen
            ]
            for j in legacy_jobs:
                logger.info("Entferne Legacy per-char daily-Job: %s", j.get("id"))
                self.remove_job(j["id"])

            # 2. Pruefen ob mindestens ein Character einen aktiven Tagesablauf hat
            has_active_schedule = False
            try:
                all_chars = list_available_characters()
            except Exception:
                all_chars = []

            for char in all_chars:
                try:
                    schedule = get_character_daily_schedule(char)
                    if not (schedule and schedule.get("enabled", False) and schedule.get("slots")):
                        continue
                    has_active_schedule = True
                    # Marker sicherstellen (falls er nach Umbau fehlt)
                    marker_id = f"daily_schedule_{char}"
                    if not any(j.get("id") == marker_id for j in self.jobs_data["jobs"]):
                        self.jobs_data["jobs"].append({
                            "id": marker_id,
                            "character": char,
                            "enabled": True,
                            "source": "daily_schedule",
                            "trigger": {"type": "marker"},
                            "action": {"type": "daily_schedule_marker",
                                       "slots_count": len(schedule.get("slots", []))},
                            "created_at": datetime.now().isoformat(),
                        })
                        try:
                            self._save_jobs_for_character(char)
                        except Exception:
                            pass
                except Exception:
                    continue

            if has_active_schedule:
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

        # Sleep-Check: Schlafende Characters fuehren keine Jobs aus
        # (daily_schedule wird separat in _execute_daily_schedule behandelt)
        if agent and user_id and action_type != 'daily_schedule':
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
                result = self._action_execute_tool(action, agent)
            elif action_type == 'set_status':
                result = self._action_set_status(action, agent)
            elif action_type == 'daily_schedule':
                result = self._execute_daily_schedule(agent)
            elif action_type == 'world_hourly_tick':
                result = self._execute_world_hourly_tick()
            elif action_type == 'extract_files':
                result = self._action_extract_files(action, agent)
            elif action_type == 'custom':
                result = self._action_custom(action)
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
        """Speichert die Nachricht in der Character→Spieler History und erstellt Notification.

        WICHTIG: Postet NICHT an den aktiven Chat-Endpoint — das wuerde die
        Nachricht in den falschen Chat-Kontext injizieren (z.B. wenn der User
        gerade mit einem anderen Character chattet, wuerde die Nachricht dort
        als User-Input erscheinen).
        """
        message = action.get('message', '')
        if not message:
            return {"success": False, "error": "Keine Nachricht angegeben"}

        logger.info("send_message Action: %s sendet '%s...'", agent, message[:80])

        # 1) In Character-Spieler Chat-History speichern
        try:
            from app.models.chat import save_message
            from app.models.account import get_active_character
            partner = get_active_character() or ""
            ts = datetime.now().isoformat()
            save_message({"role": "assistant", "content": message, "timestamp": ts},
                character_name=agent,
                partner_name=partner)
        except Exception as save_err:
            logger.error("send_message: Chat-History speichern fehlgeschlagen: %s", save_err)

        # 2) Notification fuer den User erstellen
        try:
            from app.models.notifications import create_notification
            nid = create_notification(
                character=agent,
                content=message[:500],
                notification_type="message",
                metadata={"trigger": "scheduler"})
            return {"success": True, "action": "send_message", "notification_id": nid}
        except Exception as e:
            logger.error("send_message: Notification-Erstellung fehlgeschlagen: %s", e)
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

    def _llm_choose_location(self, character: str) -> Optional[str]:
        """Fragt das LLM als Character, welche Location er besuchen moechte.

        Gibt die Location-ID zurueck oder None bei Fehler.
        """
        try:
            from app.models.character import (
                get_character_profile,
                get_character_current_location, get_character_current_activity,
                get_character_current_feeling)
            from app.models.world import get_location_name
            from app.core.llm_router import llm_call
            from app.models.memory import load_memories

            profile = get_character_profile(character)

            # Sichtbare Locations: ueber Wissens-Items gefiltert (Character
            # sieht nur Orte deren knowledge_item_id er im Inventar hat, oder
            # die keinen Item-Gate haben).
            from app.models.world import list_locations_for_character
            locations = list_locations_for_character(character)
            if not locations:
                logger.warning("_llm_choose_location: Keine Locations fuer %s", character)
                return None

            # Aktuelle Situation
            cur_loc_id = get_character_current_location(character)
            cur_loc_name = get_location_name(cur_loc_id) if cur_loc_id else "Unbekannt"
            cur_activity = get_character_current_activity(character) or "Keine"
            cur_feeling = get_character_current_feeling(character) or "Neutral"
            now = datetime.now()
            time_str = now.strftime("%H:%M")
            char_name = profile.get("name", character)
            personality = profile.get("personality", "")

            # Location-Liste fuer Prompt
            loc_lines = []
            for loc in locations:
                loc_id = loc.get("id", "")
                loc_name = loc.get("name", "")
                loc_desc = loc.get("description", "")
                loc_lines.append(f"- ID: {loc_id} | Name: {loc_name} | Beschreibung: {loc_desc}")
            loc_list_str = "\n".join(loc_lines)

            # Tageserlebnisse und Commitments aus Memories laden
            memory_context = ""
            try:
                all_memories = load_memories(character)
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                todays_episodes = []
                open_commitments = []
                for mem in all_memories:
                    mtype = mem.get("memory_type", "")
                    content = mem.get("content", "")
                    if not content:
                        continue
                    if mtype == "commitment":
                        open_commitments.append(f"  - {content}")
                    elif mtype == "episodic":
                        try:
                            mem_ts = datetime.fromisoformat(mem.get("timestamp", ""))
                            if mem_ts >= today_start:
                                todays_episodes.append(f"  - {content}")
                        except (ValueError, TypeError):
                            pass
                if todays_episodes:
                    memory_context += "\nToday's experiences:\n" + "\n".join(todays_episodes[-10:])
                if open_commitments:
                    memory_context += "\nOpen promises/plans:\n" + "\n".join(open_commitments[-5:])
            except Exception as mem_err:
                logger.debug("_llm_choose_location: Memory-Kontext Fehler: %s", mem_err)

            from app.core.prompt_templates import render_task
            system_prompt, user_prompt = render_task(
                "intent_location",
                character_name=char_name,
                personality=personality,
                time_str=time_str,
                current_location_name=cur_loc_name,
                current_activity=cur_activity,
                current_feeling=cur_feeling,
                memory_context=memory_context,
                location_list=loc_list_str)

            try:
                result = llm_call(
                    task="intent_location",
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    agent_name=character)
            except RuntimeError:
                logger.warning("_llm_choose_location: Kein LLM fuer %s", character)
                return None

            response_text = (result.content or "").strip() if result else ""

            # ID aus Antwort extrahieren (8-stelliger Hex-Code)
            import re
            match = re.search(r'[0-9a-f]{8}', response_text)
            if match:
                chosen_id = match.group(0)
                # Validieren dass die ID tatsaechlich existiert
                valid_ids = {l.get("id") for l in locations}
                if chosen_id in valid_ids:
                    logger.info("LLM waehlte Location %s fuer %s", chosen_id, character)
                    return chosen_id
                else:
                    logger.warning("LLM waehlte ungueltige Location-ID: %s", chosen_id)

            # Fallback: versuche per Name zu matchen
            from app.models.world import resolve_location
            loc_obj = resolve_location(response_text)
            if loc_obj:
                return loc_obj.get("id")

            logger.warning("_llm_choose_location: Konnte Antwort nicht parsen: %s", response_text)
            return None

        except Exception as e:
            logger.error("_llm_choose_location Fehler: %s", e)
            return None

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

        # __llm_choice__: Character waehlt per LLM selbst wo er hin will
        if location == "__llm_choice__":
            chosen = self._llm_choose_location(agent)
            if chosen:
                location = chosen
            else:
                return {"success": False, "error": "LLM konnte keine Location waehlen"}

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

    def _action_execute_tool(self, action: Dict[str, Any], agent: str) -> Dict[str, Any]:
        """Fuehrt ein Tool/Skill direkt aus (ohne Chat/LLM-Umweg).

        Generischer Skill-Lookup per tool_name. Spezial-Logik fuer bestimmte
        Tools (z.B. ImageGenerator auto-enhance) wird als Pre-Processing behandelt.
        """
        tool_name = action.get('tool_name', '')
        tool_input = action.get('tool_input', '')

        if not tool_name:
            return {"success": False, "error": "tool_name erforderlich"}

        logger.info("Direkter Tool-Aufruf: %s (input: %s)", tool_name, tool_input[:80] if tool_input else "")

        try:
            from app.core.dependencies import get_skill_manager
            sm = get_skill_manager()
            skill = sm.get_skill_by_name(tool_name)
            if not skill:
                return {"success": False, "error": f"Skill '{tool_name}' nicht geladen"}

            # Pre-Processing: Tool-spezifische Anpassungen
            payload_data = {
                "input": tool_input,
                "agent_name": agent,
                "user_id": "",
            }

            skill_name_lower = tool_name.lower()

            if skill_name_lower in ("imagegenerator", "image_generator"):
                # Character-Name im Prompt sicherstellen fuer Smart Appearance Injection
                prompt = tool_input
                if agent and agent.lower() not in prompt.lower():
                    prompt = f"{agent}: {prompt}"
                payload_data = {
                    "prompt": prompt,
                    "agent_name": agent,
                    "user_id": "",
                    "set_profile": False,
                    "skip_gallery": False,
                    "auto_enhance": True,
                }

            payload = json.dumps(payload_data)
            result = skill.execute(payload)
            logger.info("%s-Ergebnis: %s", tool_name, result[:200] if result else "")

            # Ergebnis als Memory speichern wenn der Skill es unterstuetzt
            if result and hasattr(skill, 'memorize_result'):
                try:
                    saved = skill.memorize_result(result, agent)
                    if saved:
                        logger.info("%s: Ergebnis als Memory gespeichert", tool_name)
                except Exception as mem_err:
                    logger.warning("%s: memorize_result fehlgeschlagen: %s", tool_name, mem_err)

            return {"success": True, "action": "execute_tool", "tool": tool_name, "result": result[:500] if result else ""}

        except Exception as e:
            logger.error("Fehler bei Tool '%s': %s", tool_name, e)
            return {"success": False, "error": str(e)}

    def _action_extract_files(self, action: Dict[str, Any], agent: str) -> Dict[str, Any]:
        """Backward-Compat: Leitet extract_files an execute_tool + KnowledgeExtract weiter."""
        return self._action_execute_tool({
            "tool_name": "KnowledgeExtract",
            "tool_input": action.get("extraction_prompt", ""),
        }, agent)

    def _action_custom(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Fuehrt eine benutzerdefinierte Funktion aus."""
        function_name = action.get('function')
        logger.info("Custom Function: %s", function_name)
        return {"success": True, "action": "custom", "function": function_name}

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

        # Globaler Job ohne Character-Bezug (z.B. world_hourly_tick) —
        # kein per-Character-Log-File, nur DEBUG-Konsole.
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

        Statt einen eigenen Cron-Job pro Character anzulegen, uebernimmt der
        globale ``world_hourly_tick``-Job die Abarbeitung aller Characters
        sequenziell (alphabetisch, 10s Pause dazwischen). Hier werden nur die
        Slots validiert und Locations zu IDs aufgeloest.

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
            raw_loc = slot.get("location", "")
            if raw_loc and raw_loc != "__llm_choice__":
                loc_obj = _resolve_loc(raw_loc)
                if loc_obj and loc_obj.get("id"):
                    slot["location"] = loc_obj["id"]

        # 2. Globalen world_hourly_tick-Job sicherstellen (legt er nicht existiert)
        self._ensure_world_hourly_job()

        # 3. Per-Character Marker-Job — rein visuelles Signal in der UI
        #    dass der Tagesablauf aktiv ist. Der Marker hat KEIN Cron-Trigger
        #    (er wird nicht ausgefuehrt); die tatsaechliche Abarbeitung laeuft
        #    ueber den globalen world_hourly_tick, der alle Characters
        #    sequenziell abarbeitet.
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

    def _ensure_world_hourly_job(self) -> None:
        """Legt den globalen world_hourly_tick-Job an (falls noch nicht da).

        Dieser Job feuert stuendlich und arbeitet alle Characters
        sequenziell alphabetisch ab — verhindert Race-Conditions wie
        "Vallerie sieht Diego noch als am Ort, obwohl er gerade umgezogen wird".
        """
        job_id = "world_hourly_tick"
        existing = [j for j in self.jobs_data["jobs"] if j.get("id") == job_id]
        if existing:
            return
        job_config = {
            "id": job_id,
            "user_id": "",
            "enabled": True,
            "trigger": {"type": "cron", "hour": "*", "minute": 0},
            "action": {"type": "world_hourly_tick"},
            "source": "daily_schedule",
            "created_at": datetime.now().isoformat(),
        }
        self.jobs_data["jobs"].append(job_config)
        self._schedule_job(job_config)
        logger.info("world_hourly_tick registriert")

    def _was_recently_chatting(self, character: str, minutes: int = 10) -> bool:
        """Prueft ob der Character in den letzten N Minuten im Chat mit dem User war.

        Beruecksichtigt sowohl 1:1 Chats als auch Gruppenchats.
        """
        threshold = minutes * 60  # in Sekunden
        now_ts = datetime.now().timestamp()

        # 1:1 Chat pruefen: mtime der letzten Chat-Datei
        try:
            from app.models.character import get_character_dir
            chat_dir = get_character_dir(character) / "chats"
            if chat_dir.exists():
                chat_files = sorted(chat_dir.glob("*_chat_*.json"))
                if chat_files:
                    last_chat_mtime = chat_files[-1].stat().st_mtime
                    if (now_ts - last_chat_mtime) < threshold:
                        logger.info("Location-Wechsel blockiert: %s war vor %.0f Min im 1:1 Chat",
                                    character, (now_ts - last_chat_mtime) / 60)
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

    def _execute_world_hourly_tick(self) -> Dict[str, Any]:
        """Aggregate-Job: Arbeitet alle Characters mit aktivem Tagesablauf seriell ab.

        Reihenfolge: alphabetisch. Zwischen jedem Character 10s Pause, damit
        Status-Aenderungen (Location, Activity, Partner) vollstaendig
        persistiert sind bevor der naechste Character sie liest.
        """
        import time as _time
        from app.models.character import (
            list_available_characters,
            get_character_daily_schedule)
        from app.models.account import is_player_controlled
        results = []
        try:
            all_chars = sorted(list_available_characters(), key=str.lower)
        except Exception as e:
            logger.error("world_hourly_tick: Character-Liste fehlgeschlagen: %s", e)
            return {"success": False, "error": str(e)}

        # Aktueller Chat-Partner + Zeitpunkt der letzten Chat-Aktivitaet —
        # wenn der User gerade mit einem NPC geredet hat, darf der Scheduler
        # diesen NPC nicht mitten im Gespraech wegschicken.
        _current_partner = ""
        try:
            from app.routes.chat import _get_chat_partner
            _current_partner = _get_chat_partner() or ""
        except Exception:
            _current_partner = ""

        # Kandidaten: Characters mit aktivem Tagesablauf, ABER ohne den
        # Spieler-Avatar (der folgt dem User, nicht dem Scheduler).
        # Feature-Gate: activities_enabled muss true sein.
        from app.models.character_template import is_feature_enabled as _feat
        candidates = []
        for char in all_chars:
            try:
                if is_player_controlled(char):
                    continue
                if not _feat(char, "activities_enabled"):
                    continue
                # Aktueller Chat-Partner mit Chat-Aktivitaet <10min ? -> skip
                if char == _current_partner and _was_chatted_recently(char, within_minutes=10):
                    logger.info(
                        "world_hourly_tick: %s uebersprungen "
                        "(aktueller Chat-Partner, letzte Aktivitaet <10min)",
                        char)
                    continue
                sched = get_character_daily_schedule(char)
                if sched and sched.get("enabled", False) and sched.get("slots"):
                    candidates.append(char)
            except Exception:
                continue

        logger.info("world_hourly_tick: %d Character(s) in dieser Runde: %s",
                    len(candidates), ", ".join(candidates))

        pause_s = 10  # feste Pause zwischen den Characters
        for idx, character in enumerate(candidates):
            if idx > 0:
                _time.sleep(pause_s)
            try:
                logger.info("world_hourly_tick [%d/%d]: %s",
                            idx + 1, len(candidates), character)
                res = self._execute_daily_schedule(character)
                results.append({"character": character, "result": res})
                _r = (res or {}).get("result", res)
                _act = (res or {}).get("activity", "") if isinstance(res, dict) else ""
                if isinstance(res, dict):
                    _act = res.get("activity", _act)
                logger.info("world_hourly_tick [%d/%d]: %s -> %s",
                            idx + 1, len(candidates), character,
                            (res or {}).get("reason") or (res or {}).get("action") or "done")
            except Exception as e:
                logger.error("world_hourly_tick: Fehler bei %s: %s", character, e)
                results.append({"character": character, "error": str(e)})
        return {"success": True, "action": "world_hourly_tick",
                "processed": len(results), "results": results}

    def _execute_daily_schedule(self, character: str) -> Dict[str, Any]:
        """Fuehrt den Tagesablauf fuer die aktuelle Stunde aus.

        Liest den gespeicherten Schedule, findet den Slot fuer die aktuelle Stunde
        und setzt Location/Activity entsprechend.

        Player-controlled characters (der aktive Avatar) werden uebersprungen —
        sie werden ausschliesslich manuell vom User gesteuert.
        """
        from app.models.account import is_player_controlled
        if is_player_controlled(character):
            return {"success": True, "action": "daily_schedule", "skipped": True,
                    "reason": "Character player-controlled (Avatar)"}
        from app.models.character import get_character_daily_schedule
        from app.models.account import is_player_controlled

        # Player-controlled Characters brauchen keinen autonomen Tagesablauf
        if is_player_controlled(character):
            logger.debug("Tagesablauf %s: Uebersprungen (player-controlled)", character)
            return {"success": True, "action": "daily_schedule", "skipped": True,
                    "reason": "Character ist player-controlled"}

        schedule = get_character_daily_schedule(character)
        if not schedule or not schedule.get("enabled", False):
            return {"success": False, "error": "Kein aktiver Tagesablauf"}

        # Pruefen ob Character kuerzlich im Chat war (10 Min) -> Location-Wechsel verschieben
        if self._was_recently_chatting(character):
            logger.info("Tagesablauf %s: Location-Wechsel verschoben (kuerzlich im Chat)", character)
            return {"success": True, "action": "daily_schedule", "skipped": True,
                    "reason": "Character war in den letzten 10 Minuten im Chat"}

        # Partner-Lock pruefen: wenn Character gerade in einer partner-basierten
        # Aktivitaet mit laufender Dauer ist, nicht aendern
        if self._is_partner_locked(character):
            logger.info("Tagesablauf %s: Uebersprungen (partner-gebunden, Dauer laeuft)", character)
            return {"success": True, "action": "daily_schedule", "skipped": True,
                    "reason": "Partner-Aktivitaet laeuft noch"}

        # Pruefen ob Character ein aktives Assignment mit Ort hat
        # -> Location und Aktivitaet auf Assignment setzen, Tagesablauf ignorieren
        try:
            from app.models.assignments import list_assignments
            active_assignments = list_assignments(character_name=character, status="active")
            loc_assignment = next((a for a in active_assignments if a.get("location_id")), None)
            if loc_assignment:
                a_title = loc_assignment.get("title", "Aufgabe")
                a_loc = loc_assignment.get("location_id", "")
                # Aktivitaet aus Rolle oder Titel ableiten
                participant = loc_assignment.get("participants", {}).get(character, {})
                a_activity = participant.get("role") or a_title
                logger.info("Tagesablauf %s: Assignment '%s' -> Location=%s, Activity=%s",
                            character, a_title, a_loc, a_activity)
                result = self._action_set_status(
                    {"location": a_loc, "activity": a_activity}, character)
                return result
        except Exception as _ae:
            logger.debug("Assignment-Check Fehler: %s", _ae)

        current_hour = datetime.now().hour
        slots = schedule.get("slots", [])

        # Slot fuer die aktuelle Stunde finden
        current_slot = None
        for slot in slots:
            if slot.get("hour") == current_hour:
                current_slot = slot
                break

        if not current_slot:
            logger.debug("Tagesablauf %s: Kein Slot fuer Stunde %d", character, current_hour)
            return {"success": True, "action": "daily_schedule", "skipped": True,
                    "reason": f"Kein Slot fuer Stunde {current_hour}"}

        # Sleep-Slot detection: prefer the explicit sleep flag but fall back
        # to the legacy activity == "__sleep__" sentinel for old schedules.
        is_sleep_slot = bool(current_slot.get("sleep")) or \
            current_slot.get("activity") == "__sleep__" or \
            current_slot.get("location") == "__sleep__"

        if is_sleep_slot:
            logger.info("Tagesablauf %s: Stunde %d -> Schlaeft", character, current_hour)
            from app.models.character import (
                save_character_current_activity, get_character_config,
                save_character_current_location, save_character_current_room)
            cfg = get_character_config(character)
            save_character_current_activity(character, "Sleeping")
            home_loc = cfg.get("home_location", "")
            home_room = cfg.get("home_room", "")
            if home_loc == OFFMAP_SLEEP_SENTINEL:
                save_character_current_location(character, "")
                save_character_current_room(character, "")
            else:
                if home_loc:
                    save_character_current_location(character, home_loc)
                if home_room:
                    save_character_current_room(character, home_room)
            return {"success": True, "action": "daily_schedule", "sleeping": True}

        location = current_slot.get("location", "")

        # __llm_choice__: Character waehlt per LLM selbst wo er hin will
        if location == "__llm_choice__":
            chosen = self._llm_choose_location(character)
            if chosen:
                location = chosen
                logger.info("Tagesablauf %s: LLM waehlte Location=%s", character, location)
            else:
                logger.warning("Tagesablauf %s: LLM konnte keine Location waehlen", character)
                return {"success": True, "action": "daily_schedule", "skipped": True,
                        "reason": "LLM konnte keine Location waehlen"}

        if not location:
            return {"success": False, "error": "Slot ohne Location"}

        # The slot may also pin an activity. If set, apply it; if not, the
        # AgentLoop's next thought turn decides what the character does.
        activity = (current_slot.get("activity") or "").strip()
        # Reject the legacy __sleep__ sentinel as activity (already handled
        # above as is_sleep_slot). Other __sentinels__ are user input bugs;
        # ignore them so the agent decides.
        if activity.startswith("__"):
            activity = ""

        logger.info("Tagesablauf %s: Stunde %d -> Location=%s%s",
                     character, current_hour, location,
                     f", Activity={activity}" if activity else "")

        status = {"location": location}
        if activity:
            status["activity"] = activity
        return self._action_set_status(status, character)

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

    Liest die mtime der neuesten JSON-Datei in ``{character}/chats/``.
    """
    try:
        from app.models.chat import get_chat_dir
        chat_dir = get_chat_dir(character_name)
        if not chat_dir.exists():
            return False
        newest_mtime = 0.0
        for f in chat_dir.glob("*.json"):
            mt = f.stat().st_mtime
            if mt > newest_mtime:
                newest_mtime = mt
        if newest_mtime <= 0:
            return False
        from datetime import datetime
        age_s = datetime.now().timestamp() - newest_mtime
        return age_s < within_minutes * 60
    except Exception:
        return False
