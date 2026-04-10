"""Nightly report generation scheduler.

Runs at 00:30 every day to regenerate all reports with latest data.
Uses a simple daemon thread with a 60-second polling loop.

Ported from bma-health -- already sync, imports adjusted.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# --- State ---
_scheduler_state: dict[str, Any] = {
    "enabled": True,
    "cron": "00:30",
    "last_run": None,
    "last_result": None,
    "next_run": None,
    "running": False,
}


def get_scheduler_status() -> dict[str, Any]:
    """Return current scheduler status."""
    return dict(_scheduler_state)


def _run_nightly_reports():
    """Run nightly report generation (called in background thread)."""
    from services.report_generator import report_generator

    _scheduler_state["running"] = True
    _scheduler_state["last_run"] = datetime.now(timezone.utc).isoformat()
    logger.info("=== Nightly report generation started ===")

    try:
        paths = report_generator.generate_all_extended()
        result = {
            "success": True,
            "generated": len(paths),
            "errors": len(report_generator.get_generation_progress().get("errors", [])),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("=== Nightly report generation complete: %d reports ===", len(paths))
    except Exception as e:
        result = {
            "success": False,
            "error": str(e),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.exception("=== Nightly report generation FAILED ===")

    _scheduler_state["last_result"] = result
    _scheduler_state["running"] = False


def _schedule_loop():
    """Simple scheduling loop -- runs in a daemon thread.

    Checks every 60 seconds if it's time to run (00:30 local time).
    """
    last_run_date = None

    while True:
        time.sleep(60)
        if not _scheduler_state["enabled"]:
            continue

        now = datetime.now()
        target_hour, target_minute = 0, 30

        # Calculate next run
        _scheduler_state["next_run"] = (
            now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0).isoformat()
            if now.hour < target_hour or (now.hour == target_hour and now.minute < target_minute)
            else (now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
                  + timedelta(days=1)).isoformat()
        )

        # Check if it's time to run (00:30) and hasn't run today
        today = now.date()
        if (now.hour == target_hour
                and target_minute <= now.minute < target_minute + 2
                and last_run_date != today
                and not _scheduler_state["running"]):
            last_run_date = today
            logger.info("Scheduler triggered at %s", now.isoformat())
            threading.Thread(target=_run_nightly_reports, daemon=True).start()


def start_scheduler():
    """Start the nightly report scheduler in a daemon thread."""
    now = datetime.now()
    _scheduler_state["next_run"] = now.replace(hour=0, minute=30, second=0, microsecond=0).isoformat()
    if now.hour >= 1:
        _scheduler_state["next_run"] = (now.replace(hour=0, minute=30, second=0) + timedelta(days=1)).isoformat()

    thread = threading.Thread(target=_schedule_loop, daemon=True, name="report-scheduler")
    thread.start()
    logger.info("Report scheduler started -- next run at %s", _scheduler_state["next_run"])
