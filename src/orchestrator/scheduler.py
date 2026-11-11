"""Phase 6.1: Daily Cron & Staggered Workflow Scheduler Daemon (`America/Chicago`)."""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import Settings
from src.orchestrator.pipeline import AutonomousJobOrchestrator

logger = logging.getLogger(__name__)


def build_scheduler(
    settings: Settings,
    orchestrator: AutonomousJobOrchestrator | None = None,
    blocking: bool = False,
) -> BlockingScheduler | BackgroundScheduler:
    """
    Configures the 4 scheduled daily CST/CDT (`America/Chicago`) jobs defined in `task.md`:
    1. `06:00 AM CST`: `CompanySourcerAgent` -> `JobMatcherScorerAgent` (populates 10–12 top roles)
    2. `08:00 AM – 04:00 PM CST` (hourly window): Staggered worker queue (1–2 apps/hr with 25–55m jitter)
    3. `Every 15 mins (08:00 AM – 08:00 PM CST)`: Gmail thread reply poller for HITL approvals
    4. `06:00 PM CST`: `HITLGmailAgent.send_daily_digest()`
    """
    orch = orchestrator or AutonomousJobOrchestrator(settings)
    tz = ZoneInfo(settings.timezone)
    scheduler_cls = BlockingScheduler if blocking else BackgroundScheduler
    scheduler = scheduler_cls(timezone=tz)

    # 1. 06:00 AM CST: Morning Discovery & Scoring
    scheduler.add_job(
        orch.run_morning_discovery,
        trigger=CronTrigger(hour=6, minute=0, timezone=tz),
        id="morning_discovery_0600",
        name="06:00 AM CST CompanySourcer + JobMatcherScorer",
        replace_existing=True,
    )

    # 2. 08:00 AM – 04:00 PM CST: Staggered Application Worker Queue (1–2 apps/hr with 25–55 min jitter)
    scheduler.add_job(
        orch.process_staggered_queue,
        kwargs={"max_items": 2, "apply_jitter": True},
        trigger=CronTrigger(hour="8-16", minute=5, jitter=300, timezone=tz),
        id="staggered_worker_0800_1600",
        name="08:00 AM - 04:00 PM CST Staggered Application Worker",
        max_instances=1,
        replace_existing=True,
    )

    # 3. Every 15 mins (08:00 AM – 08:00 PM CST): Gmail HITL Thread Reply Poller
    scheduler.add_job(
        orch.poll_hitl_and_execute,
        trigger=CronTrigger(hour="8-20", minute="*/15", timezone=tz),
        id="hitl_gmail_poller_15m",
        name="Every 15m (08:00-20:00 CST) Gmail HITL Thread Reply Poller",
        max_instances=1,
        replace_existing=True,
    )

    # 4. 06:00 PM CST: End-of-Day Daily Digest
    scheduler.add_job(
        orch.hitl.send_daily_digest,
        trigger=CronTrigger(hour=18, minute=0, timezone=tz),
        id="eod_daily_digest_1800",
        name="06:00 PM CST EOD Daily Digest",
        replace_existing=True,
    )

    return scheduler


def run_daemon(settings: Settings) -> None:
    orch = AutonomousJobOrchestrator(settings)
    orch.db.seed_from_target_companies_json()
    scheduler = build_scheduler(settings, orchestrator=orch, blocking=True)
    logger.info(
        "Starting Autonomous Job Agent Scheduler Daemon (Timezone=%s, DRY_RUN=%s)...",
        settings.timezone,
        settings.dry_run,
    )
    for job in scheduler.get_jobs():
        logger.info("Registered schedule [%s]: %s (%s)", job.id, job.name, job.trigger)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler daemon stopped cleanly.")

