"""Background worker: folder monitoring, the daily upload run, retries and recovery.

Runs independently of the Streamlit dashboard:

    .venv\\Scripts\\python.exe scheduler.py            # run the worker
    .venv\\Scripts\\python.exe scheduler.py --once     # one daily run now, then exit
    .venv\\Scripts\\python.exe scheduler.py --status   # print schedule and exit

Stop it with Ctrl+C, by closing its window, with stop.bat (creates
``youtube_shorts/worker.stop``), or via Task Scheduler. An upload in progress
is paused at a chunk boundary and resumed on the next start.
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import AppConfig, ensure_directories, load_config
from database import Database, to_iso, utc_now
from logging_setup import setup_logging
from queue_manager import QueueManager
from recovery import RecoveryManager
from services.metadata_service import MetadataService
from services.scheduling_service import (compute_publish_slot, format_local, get_zone, is_daily_run_due,
                                         next_upload_run, parse_hhmm)
from services.upload_service import UploadService
from youtube_uploader import create_uploader

log = logging.getLogger("ysa.worker")

EXIT_OK = 0
EXIT_ALREADY_RUNNING = 3
EXIT_ERROR = 1
HEARTBEAT_SECONDS = 20
SYNC_PUBLISHED_SECONDS = 600


class SingleInstanceLock:
    """OS-level exclusive lock; released automatically if the process dies."""

    def __init__(self, path):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            return False
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        self._fh.close()
        self._fh = None


def build_services(cfg: AppConfig, db: Database, stop_event: threading.Event):
    metadata = MetadataService(cfg, db)
    queue = QueueManager(cfg, db, metadata)
    uploader = create_uploader(cfg)
    uploads = UploadService(cfg, db, uploader, queue, metadata, stop_event)
    recovery = RecoveryManager(db, uploader, queue, uploads)
    return metadata, queue, uploader, uploads, recovery


class Worker:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        self.stop_event = threading.Event()
        self.metadata, self.queue, self.uploader, self.uploads, self.recovery = build_services(
            cfg, self.db, self.stop_event)
        self.scheduler: BackgroundScheduler | None = None
        self._settings_version: str | None = None
        self._last_sync = 0.0
        self._work_lock = threading.Lock()

    # --- lifecycle ----------------------------------------------------------------------
    def start(self) -> None:
        settings = self.db.get_settings()
        self.db.set_state("worker_pid", os.getpid())
        self.db.set_state("worker_started_at", to_iso(utc_now()))
        self.db.set_state("worker_mode", self.cfg.upload_mode)
        self.db.log_event("system", f"Worker started (pid {os.getpid()}, {self.cfg.upload_mode.upper()} mode)")
        self.heartbeat()

        report = self.recovery.run(include_running=True)
        if report.anything:
            log.info("Recovery: %s", report)

        # Register files already waiting in incoming/ before any catch-up run can look at the queue
        # (a file must be seen unchanged across two scans before it counts as fully copied).
        self.queue.scan_incoming()
        if self.queue.unregistered_files():
            self.stop_event.wait(self.cfg.file_stable_seconds + 1)
            self.queue.scan_incoming()

        self.scheduler = BackgroundScheduler(
            timezone=get_zone(settings["timezone"]),
            executors={"default": {"type": "threadpool", "max_workers": 1}},  # serialise all work
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
        )
        self._schedule_daily(settings)
        self.scheduler.add_job(self.tick, IntervalTrigger(seconds=self.cfg.scan_interval_seconds),
                               id="tick", next_run_time=datetime.now(get_zone(settings["timezone"])))
        self.scheduler.start()
        self._log_schedule(settings)

    def _schedule_daily(self, settings: dict) -> None:
        self._settings_version = settings["updated_at"]
        if self.scheduler.get_job("daily_upload"):
            self.scheduler.remove_job("daily_upload")
        if not settings["scheduler_enabled"]:
            log.info("Scheduler disabled in settings; daily run not scheduled")
            return
        at = parse_hhmm(settings["upload_time"])
        trigger = CronTrigger(hour=at.hour, minute=at.minute, timezone=get_zone(settings["timezone"]),
                              day_of_week=None if settings["upload_on_weekends"] else "mon-fri")
        self.scheduler.add_job(self.daily_run, trigger, id="daily_upload", kwargs={"trigger": "scheduled"})

    def _log_schedule(self, settings: dict) -> None:
        now = utc_now()
        run = next_upload_run(now, settings)
        message = "Scheduler disabled" if run is None else (
            f"Next upload run {format_local(run, settings['timezone'])}; videos publish at "
            f"{settings['publish_time']} {settings['timezone']}")
        log.info(message)
        self.db.log_event("schedule", message)

    def stop(self, reason: str = "shutdown requested") -> None:
        if self.stop_event.is_set():
            return
        log.info("Stopping worker: %s", reason)
        self.stop_event.set()

    def shutdown(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=True)  # waits for the current job, which stops at a chunk boundary
        self.db.set_state("worker_pid", None)
        self.db.set_state("worker_heartbeat", None)
        self.db.log_event("system", "Worker stopped")

    def heartbeat(self) -> None:
        self.db.set_state("worker_heartbeat", to_iso(utc_now()))

    def run_forever(self) -> None:
        self.start()
        last_beat = 0.0
        try:
            while not self.stop_event.is_set():
                if self.cfg.stop_file.exists():
                    try:
                        self.cfg.stop_file.unlink()  # our own control flag file
                    except OSError:
                        pass
                    self.stop("stop file found")
                    break
                if time.monotonic() - last_beat >= HEARTBEAT_SECONDS:
                    self.heartbeat()
                    last_beat = time.monotonic()
                self.stop_event.wait(1.0)
        finally:
            self.shutdown()

    # --- jobs ---------------------------------------------------------------------------
    def daily_run(self, trigger: str = "scheduled") -> None:
        if self.stop_event.is_set():
            return
        with self._work_lock:
            log.info("Daily upload run (%s)", trigger)
            self.db.log_event("schedule", f"Daily upload run started ({trigger})")
            self.queue.scan_incoming()
            outcomes = self.uploads.run_daily_batch(trigger)
            for outcome in outcomes:
                log.info("Outcome for video %s: %s %s", outcome.video_id, outcome.status, outcome.message)

    def tick(self) -> None:
        if self.stop_event.is_set():
            return
        try:
            with self._work_lock:
                settings = self.db.get_settings()
                if settings["updated_at"] != self._settings_version:
                    self._apply_settings(settings)
                self.queue.scan_incoming()
                self.recovery.run(include_running=False)
                self.uploads.process_manual_requests()
                last_day = self.db.get_state("last_daily_run_day")
                due = is_daily_run_due(utc_now(), settings, last_day)
            if due and settings["catch_up_missed_runs"]:
                self.db.log_event("schedule", "Today's upload run was missed (PC off/asleep or worker stopped); "
                                              "running it now")
                self.daily_run("catch_up")
            elif due:
                self.db.set_state("last_daily_run_day", utc_now().astimezone(get_zone(settings["timezone"]))
                                  .date().isoformat())
                self.db.log_event("schedule", "Missed today's upload run; catch-up is disabled", "WARNING")
            with self._work_lock:
                self.uploads.process_due_retries()
                if time.monotonic() - self._last_sync >= SYNC_PUBLISHED_SECONDS:
                    self._last_sync = time.monotonic()
                    self.uploads.sync_published()
        except Exception as exc:
            log.exception("Worker tick failed")
            self.db.log_event("system", f"Worker tick failed: {exc}", "ERROR")

    def _apply_settings(self, settings: dict) -> None:
        log.info("Settings changed; rescheduling")
        if self.scheduler is not None:
            self._schedule_daily(settings)
        self.db.log_event("schedule", "Settings change applied by the worker")
        self._log_schedule(settings)


def print_status(cfg: AppConfig) -> None:
    db = Database(cfg.db_path)
    settings = db.get_settings()
    now = utc_now()
    run = next_upload_run(now, settings)
    print(f"Mode:            {cfg.upload_mode.upper()}")
    print(f"Scheduler:       {'enabled' if settings['scheduler_enabled'] else 'disabled'}")
    print(f"Timezone:        {settings['timezone']}")
    print(f"Next upload run: {format_local(run, settings['timezone']) if run else '-'}")
    base = run or now
    print(f"Would publish:   {format_local(compute_publish_slot(base, settings, db.taken_publish_slots()), settings['timezone'])}")
    print(f"Counts:          {db.status_counts()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="YouTube Shorts upload worker")
    parser.add_argument("--once", action="store_true", help="scan, run one daily batch now and exit")
    parser.add_argument("--status", action="store_true", help="print schedule information and exit")
    args = parser.parse_args(argv)

    cfg = load_config()
    ensure_directories(cfg)
    setup_logging(cfg.logs_dir, "worker", cfg.log_level)
    if args.status:
        print_status(cfg)
        return EXIT_OK

    lock = SingleInstanceLock(cfg.lock_file)
    if not lock.acquire():
        log.error("Another worker is already running (lock: %s)", cfg.lock_file)
        return EXIT_ALREADY_RUNNING

    worker = Worker(cfg)

    def handle_signal(signum, _frame):
        worker.stop(f"signal {signum}")

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), handle_signal)

    try:
        if args.once:
            worker.recovery.run(include_running=True)
            worker.queue.scan_incoming()
            time.sleep(cfg.file_stable_seconds + 1)
            worker.queue.scan_incoming()
            worker.uploads.run_daily_batch("manual")
            return EXIT_OK
        worker.run_forever()
        return EXIT_OK
    except Exception:
        log.exception("Worker crashed")
        return EXIT_ERROR
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
