"""Keeps the worker alive when started by Windows Task Scheduler.

Restarts ``scheduler.py`` with backoff if it crashes; exits when the worker
stops cleanly (exit code 0, e.g. via stop.bat) or another worker already runs.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from pathlib import Path

from config import ensure_directories, load_config
from logging_setup import setup_logging

ROOT = Path(__file__).resolve().parent
EXIT_ALREADY_RUNNING = 3


def main() -> int:
    cfg = load_config()
    ensure_directories(cfg)
    log = setup_logging(cfg.logs_dir, "supervisor", cfg.log_level, console=False)
    python = Path(sys.executable)
    if python.name.lower() == "pythonw.exe":
        python = python.with_name("python.exe")
    delay = 10
    while True:
        started = time.monotonic()
        log.info("Starting worker: %s scheduler.py", python)
        proc = subprocess.run([str(python), str(ROOT / "scheduler.py")], cwd=ROOT,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        code = proc.returncode
        if code == 0:
            log.info("Worker stopped cleanly; supervisor exiting")
            return 0
        if code == EXIT_ALREADY_RUNNING:
            log.info("A worker is already running; supervisor exiting")
            return 0
        if time.monotonic() - started > 600:
            delay = 10  # it ran fine for a while; reset backoff
        logging.getLogger("ysa").error("Worker exited with code %s; restarting in %ss", code, delay)
        time.sleep(delay)
        delay = min(delay * 2, 300)


if __name__ == "__main__":
    sys.exit(main())
