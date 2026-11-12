"""Command-Line Interface (`python -m src.cli <command>`) for `get-hired`."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from src.config import get_settings
from src.orchestrator.pipeline import AutonomousJobOrchestrator
from src.orchestrator.scheduler import build_scheduler, run_daemon


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="get-hired",
        description="Autonomous Job Discovery, Zero-Hallucination Tailoring & Application Agent",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Force DRY_RUN=true (disables final ATS submit click and uses local outbox).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Initialize SQLite ledger and seed 14 target companies.")
    subparsers.add_parser("source", help="Run CompanySourcerAgent to discover certified employers.")
    subparsers.add_parser("score", help="Run JobMatcherScorerAgent to scrape and score roles.")

    proc_p = subparsers.add_parser("process-queue", help="Tailor, verify, compile, and apply/HITL.")
    proc_p.add_argument("--max-items", type=int, default=2, help="Max queued roles to process.")
    proc_p.add_argument("--jitter", action="store_true", help="Enable 25-55 min staggered sleep.")

    subparsers.add_parser("poll-hitl", help="Poll Gmail HITL threads once and execute approvals.")
    dig_p = subparsers.add_parser("digest", help="Generate and send the HTML EOD Daily Digest.")
    dig_p.add_argument(
        "--to",
        type=str,
        default=None,
        help="Comma-separated recipient email addresses (defaults to NOTIFY_EMAILS).",
    )
    subparsers.add_parser("dry-run", help="Execute full Phase 7 End-to-End Dry Run & verification.")
    subparsers.add_parser("verify-schedules", help="Print configured CST cron triggers and exit.")
    subparsers.add_parser("daemon", help="Start the persistent APScheduler daily daemon.")
    subparsers.add_parser("auth-gmail", help="Run interactive OAuth2 consent for Erykasmith@gmail.com.")
    subparsers.add_parser("web-auth", help="Start scale-to-zero Cloud Run Web OAuth2 onboarding server.")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    dry_flag = True if args.command == "dry-run" else args.dry_run
    settings = get_settings(dry_run=dry_flag)
    orch = AutonomousJobOrchestrator(settings)

    if args.command == "init-db":
        seeded = orch.db.seed_from_target_companies_json()
        print(
            json.dumps(
                {
                    "seeded_new": seeded,
                    "total_companies": len(orch.db.list_companies()),
                    "ledger_db": str(settings.ledger_db_path),
                },
                indent=2,
            )
        )
        return 0

    if args.command == "source":
        print(json.dumps(orch.sourcer.run(), indent=2))
        return 0

    if args.command == "score":
        print(json.dumps(orch.matcher.run(), indent=2))
        return 0

    if args.command == "process-queue":
        res = orch.process_staggered_queue(
            max_items=args.max_items, apply_jitter=args.jitter
        )
        print(json.dumps(res, indent=2))
        return 0

    if args.command == "poll-hitl":
        print(json.dumps(orch.poll_hitl_and_execute(), indent=2))
        return 0

    if args.command == "digest":
        print(json.dumps(orch.hitl.send_daily_digest(to_email=args.to), indent=2))
        return 0

    if args.command == "dry-run":
        report = orch.run_dry_run_cycle()
        print(json.dumps(report, indent=2))
        return 0

    if args.command == "verify-schedules":
        sched = build_scheduler(settings, orchestrator=orch, blocking=False)
        jobs_info = [
            {"id": j.id, "name": j.name, "trigger": str(j.trigger)}
            for j in sched.get_jobs()
        ]
        print(json.dumps(jobs_info, indent=2))
        return 0

    if args.command == "daemon":
        run_daemon(settings)
        return 0

    if args.command == "auth-gmail":
        tok = orch.gmail.authenticate_interactive()
        print(f"Gmail OAuth2 token saved to: {tok}")
        return 0

    if args.command == "web-auth":
        from src.web_auth import run_web_auth_server

        run_web_auth_server()
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())

