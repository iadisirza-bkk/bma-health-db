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

    # S9 — nightly safety-net for the bulk pre-build cache. Re-enqueues the
    # popular set of every descriptor at 00:00 Bangkok time. The worker
    # skips cache hits so this is mostly a no-op when nothing has changed.
    try:
        _start_nightly_safety_net()
    except Exception:
        logger.warning("S9 nightly safety net failed to start", exc_info=True)


# ---------------------------------------------------------------------------
# S9 — nightly bulk-prebuild safety net
# ---------------------------------------------------------------------------

# Bangkok timezone (UTC+7 — no DST). Hard-coded so we don't depend on the
# host's TZ database being installed.
_BKK_TZ = timezone(timedelta(hours=7))


def _bkk_now() -> datetime:
    return datetime.now(_BKK_TZ)


def _seconds_until_next_midnight_bkk() -> float:
    """Seconds from now until the next 00:00 in Bangkok local time."""
    now = _bkk_now()
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return (midnight - now).total_seconds()


def _run_nightly_prebuild_sync() -> None:
    """Run the bulk-prebuild worker once. Safe to call from any thread."""
    try:
        import asyncio as _asyncio
        from services.reports.build_worker import get_build_worker
        from services.reports.registry import report_registry as _registry

        worker = get_build_worker()
        for rid in _registry().list_ids():
            worker.enqueue_popular_set(rid)
        try:
            stats = _asyncio.run(worker.run_pending())
            logger.info("S9 nightly prebuild done: %s", stats)
        except RuntimeError:
            # An event loop is already running (rare in this thread, but
            # be defensive). Fire-and-forget on the existing loop.
            loop = _asyncio.get_event_loop()
            loop.create_task(worker.run_pending())
    except Exception:
        logger.exception("S9 nightly prebuild failed")


def _nightly_loop() -> None:
    """Sleep until the next 00:00 Bangkok, run the prebuild, repeat.

    Pure stdlib (no APScheduler dep). Cleaner solutions are S10's problem.
    """
    while True:
        try:
            sleep_s = _seconds_until_next_midnight_bkk()
            # Wake up at most every hour so we re-check the clock; this
            # makes us robust to system suspend/resume drift.
            time.sleep(min(sleep_s, 3600))
            now = _bkk_now()
            if now.hour == 0 and now.minute < 5:
                _run_nightly_prebuild_sync()
                # Don't re-fire if we sleep through the same minute again.
                time.sleep(360)
        except Exception:
            logger.exception("nightly loop iteration failed; will retry")
            time.sleep(60)


def _start_nightly_safety_net() -> None:
    thread = threading.Thread(
        target=_nightly_loop,
        daemon=True,
        name="reports-nightly-prebuild",
    )
    thread.start()
    logger.info(
        "S9 nightly prebuild safety net started -- next 00:00 Bangkok in %.0fs",
        _seconds_until_next_midnight_bkk(),
    )
