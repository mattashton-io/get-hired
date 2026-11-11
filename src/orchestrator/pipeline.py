"""End-to-end orchestration pipeline connecting all 6 modular agents and DocumentCompiler."""

from __future__ import annotations

import logging
import random
import time
from typing import Any

from src.agents.ats_applicator import ATSApplicatorAgent
from src.agents.company_sourcer import CompanySourcerAgent
from src.agents.hitl_gmail_comms import HITLGmailAgent
from src.agents.job_matcher_scorer import (
    JobMatcherScorerAgent,
    classify_employer_archetype,
    make_job_id,
)
from src.agents.resume_tailor import ResumeTailorAgent
from src.agents.truth_verifier import TruthVerifierAgent, run_tailor_and_verify_loop
from src.compiler.document_compiler import DocumentCompiler
from src.config import Settings
from src.integrations.gmail_client import GmailClient
from src.llm.gemini_client import GeminiClient
from src.models import JobRecord
from src.state.db import ApplicationsLedgerDB

logger = logging.getLogger(__name__)


class AutonomousJobOrchestrator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = ApplicationsLedgerDB(
            settings.ledger_db_path, settings.target_companies_path
        )
        self.llm = GeminiClient(
            api_key=settings.gemini_api_key, default_model=settings.gemini_model
        )
        self.gmail = GmailClient(settings)

        self.sourcer = CompanySourcerAgent(settings, self.db, self.llm)
        self.matcher = JobMatcherScorerAgent(settings, self.db, self.llm)
        self.tailor = ResumeTailorAgent(settings, self.llm)
        self.verifier = TruthVerifierAgent(settings, self.llm)
        self.compiler = DocumentCompiler(settings)
        self.hitl = HITLGmailAgent(settings, self.db, self.gmail)
        self.applicator = ATSApplicatorAgent(settings, self.db)

    def run_morning_discovery(self, discover_new_companies: int = 4) -> dict[str, Any]:
        sourcing_stats = self.sourcer.run(discover_new_count=discover_new_companies)
        scoring_stats = self.matcher.run(max_daily_queue=self.settings.daily_queue_limit)
        return {
            "sourcing": sourcing_stats,
            "scoring": scoring_stats,
        }

    def process_single_job(self, job: JobRecord) -> dict[str, Any]:
        company = self.db.get_company(job.company_id)
        if company is None:
            raise ValueError(f"Company {job.company_id} not found for job {job.id}")

        self.db.update_job_status(job.id, "TAILORING")
        tailored_out, ver_res = run_tailor_and_verify_loop(
            self.tailor, self.verifier, job, max_attempts=3
        )

        compiled = self.compiler.compile(
            tailored=tailored_out,
            company_name=company.company_name,
            role_title=job.title,
        )

        app_id = self.db.create_or_update_application(
            job_id=job.id,
            resume_pdf_path=compiled["pdf_path"],
            resume_docx_path=compiled["docx_path"],
            cover_note=tailored_out.tailored_cover_note,
            tailored_markdown=tailored_out.tailored_resume_markdown,
            verifier_passed=ver_res.verified,
            verifier_attempts=ver_res.attempt_number,
            verifier_report=ver_res.model_dump(),
            resume_hash=compiled["resume_hash"],
        )

        needs_hitl, hitl_reason = self.hitl.evaluate_hitl_trigger(
            job=job,
            used_master_fallback=ver_res.used_master_fallback,
        )
        if needs_hitl:
            delivery = self.hitl.send_hitl_approval_email(
                application_id=app_id,
                job=job,
                company=company,
                hitl_reason=hitl_reason,
                drafted_response=tailored_out.tailored_cover_note,
                resume_pdf_path=compiled["pdf_path"],
                resume_docx_path=compiled["docx_path"],
            )
            return {
                "application_id": app_id,
                "job_id": job.id,
                "company": company.company_name,
                "title": job.title,
                "score": job.score,
                "verifier_passed": ver_res.verified,
                "verifier_attempts": ver_res.attempt_number,
                "outcome": "ESCALATED_TO_HITL",
                "hitl_reason": hitl_reason,
                "hitl_delivery": delivery,
                "compiled_resume_pdf": compiled["pdf_path"],
                "compiled_resume_docx": compiled["docx_path"],
            }

        # Tier 1 (`score >= 85` and `verifier_passed == True`) -> Execute ATSApplicatorAgent
        ats_result = self.applicator.apply(
            application_id=app_id,
            job=job,
            company=company,
            resume_pdf_path=compiled["pdf_path"],
            cover_note=tailored_out.tailored_cover_note,
            verifier_passed=ver_res.verified,
            hitl_override=False,
        )

        # Mid-application interception (open-ended essay, mandatory salary confirmation, or CAPTCHA)
        if ats_result.get("status") == "PAUSED_FOR_HITL":
            delivery = self.hitl.send_hitl_approval_email(
                application_id=app_id,
                job=job,
                company=company,
                hitl_reason=ats_result.get("reason", "ATS interaction paused for HITL"),
                drafted_response=tailored_out.tailored_cover_note,
                resume_pdf_path=compiled["pdf_path"],
                resume_docx_path=compiled["docx_path"],
                session_state={"session_state_path": ats_result.get("session_state_path", "")},
            )
            return {
                "application_id": app_id,
                "job_id": job.id,
                "company": company.company_name,
                "title": job.title,
                "score": job.score,
                "verifier_passed": ver_res.verified,
                "verifier_attempts": ver_res.attempt_number,
                "outcome": "PAUSED_MID_ATS_TO_HITL",
                "hitl_delivery": delivery,
                "compiled_resume_pdf": compiled["pdf_path"],
            }

        return {
            "application_id": app_id,
            "job_id": job.id,
            "company": company.company_name,
            "title": job.title,
            "score": job.score,
            "verifier_passed": ver_res.verified,
            "verifier_attempts": ver_res.attempt_number,
            "outcome": ats_result.get("status"),
            "ats_result": ats_result,
            "compiled_resume_pdf": compiled["pdf_path"],
            "compiled_resume_docx": compiled["docx_path"],
        }

    def process_staggered_queue(
        self, max_items: int = 2, apply_jitter: bool = False
    ) -> list[dict[str, Any]]:
        queued = self.db.get_queued_jobs(
            limit=max_items, min_score=self.settings.min_queue_score
        )
        results: list[dict[str, Any]] = []
        for idx, job in enumerate(queued):
            if idx > 0 and apply_jitter:
                jitter_sec = random.randint(
                    self.settings.jitter_min_minutes * 60,
                    self.settings.jitter_max_minutes * 60,
                )
                logger.info(
                    "Staggered pacing jitter: sleeping %.1f minutes before next application...",
                    jitter_sec / 60.0,
                )
                time.sleep(jitter_sec)
            res = self.process_single_job(job)
            results.append(res)
        return results

    def poll_hitl_and_execute(self) -> list[dict[str, Any]]:
        def _execute_approved(app_row: dict[str, Any]) -> dict[str, Any]:
            job = self.db.get_job(app_row["job_id"])
            if not job:
                return {"status": "ERROR", "reason": "Job record missing"}
            company = self.db.get_company(job.company_id)
            if not company:
                return {"status": "ERROR", "reason": "Company record missing"}
            return self.applicator.apply(
                application_id=app_row["id"],
                job=job,
                company=company,
                resume_pdf_path=app_row["resume_pdf_path"],
                cover_note=app_row["cover_note"],
                verifier_passed=bool(app_row["verifier_passed"]),
                hitl_override=True,
            )

        return self.hitl.poll_hitl_replies(on_approved_callback=_execute_approved)

    def run_synthetic_hallucination_tests(self) -> dict[str, Any]:
        """Phase 7.1 verification: confirms `TruthVerifierAgent` catches synthetic CPA, metric, and tool hallucinations."""
        baseline = self.tailor.build_master_fallback("advisory_cpa_firm")
        dummy_job = JobRecord(
            id="job-synth-test",
            company_id="seed-mcconnell-jones",
            company_name="McConnell & Jones LLP",
            title="Internal Audit Manager",
            url="https://www.mcconnelljones.com/careers/",
            description="Seeking Internal Audit Manager with CPA, CIA, or CFE certification and ServiceNow GRC experience.",
        )

        # 1. Baseline canonical resume must PASS
        clean_res = self.verifier.verify(baseline, dummy_job, run_llm_check=False)

        # 2. Injected 'CPA' keyword hallucination must FAIL
        cpa_mutated = baseline.model_copy(deep=True)
        cpa_mutated.is_master_fallback = False
        cpa_mutated.tailored_resume_markdown = baseline.tailored_resume_markdown.replace(
            "Certified Fraud Examiner (CFE)",
            "Certified Fraud Examiner (CFE) and Certified Public Accountant (CPA)",
        )
        cpa_res = self.verifier.verify(cpa_mutated, dummy_job, run_llm_check=False)

        # 3. Inflated metric ($28M -> $35M) hallucination must FAIL
        metric_mutated = baseline.model_copy(deep=True)
        metric_mutated.is_master_fallback = False
        metric_mutated.tailored_resume_markdown = baseline.tailored_resume_markdown.replace(
            "$28M", "$35M"
        )
        metric_res = self.verifier.verify(metric_mutated, dummy_job, run_llm_check=False)

        # 4. Unapproved JD tool ('ServiceNow') keyword-stuffing must FAIL
        tool_mutated = baseline.model_copy(deep=True)
        tool_mutated.is_master_fallback = False
        tool_mutated.tailored_cover_note = (
            baseline.tailored_cover_note + " Skilled in ServiceNow GRC and Tableau."
        )
        tool_res = self.verifier.verify(tool_mutated, dummy_job, run_llm_check=False)

        all_passed_assertions = (
            clean_res.verified is True
            and cpa_res.verified is False
            and any("CPA" in v for v in cpa_res.violations)
            and metric_res.verified is False
            and tool_res.verified is False
        )
        return {
            "all_assertions_passed": all_passed_assertions,
            "clean_master_passed": clean_res.verified,
            "cpa_hallucination_caught": not cpa_res.verified,
            "cpa_violations": cpa_res.violations,
            "metric_inflation_caught": not metric_res.verified,
            "metric_violations": metric_res.violations,
            "tool_injection_caught": not tool_res.verified,
            "tool_violations": tool_res.violations,
        }

    def run_dry_run_cycle(self) -> dict[str, Any]:
        """
        Phase 7.1 End-to-End Dry Run (`DRY_RUN=true`):
        1. Runs synthetic hallucination test battery on `TruthVerifierAgent`.
        2. Seeds & runs live discovery + scoring across DFW certified firms.
        3. Ensures 3 flagship DFW requisitions (McConnell & Jones LLP, ALM First Financial Advisors,
           and Proven Recruiting — covering Tier-1 Auto-Apply `>=85`, Tier-2 HITL `75-84`, plus a
           rejected junior benchmark role) are evaluated and processed with final ATS click disabled.
        4. Exercises HITL approval email + simulated reply poller + EOD Daily Digest HTML generation.
        """
        self.settings.dry_run = True

        # 1. Synthetic Hallucination Gatekeeper Test
        hallucination_report = self.run_synthetic_hallucination_tests()
        if not hallucination_report["all_assertions_passed"]:
            raise RuntimeError(
                f"TruthVerifier synthetic hallucination check failed: {hallucination_report}"
            )

        # 2. Seed & Run Live Discovery + Portal Scraping
        self.db.seed_from_target_companies_json()
        sourcing_summary = self.sourcer.run(discover_new_count=3)

        # Ensure flagship live DFW portal requisitions for McConnell & Jones, ALM First, and Proven Recruiting
        flagship_specs = [
            (
                "seed-mcconnell-jones",
                "Internal Audit & Risk Advisory Manager",
                "https://www.mcconnelljones.com/careers/",
                "Dallas / Plano, TX (Hybrid)",
                120000,
                145000,
                (
                    "McConnell & Jones LLP (Black-Owned / MBE / Texas HUB CPA & Advisory Firm) seeks an "
                    "Internal Audit & Risk Advisory Manager in Dallas/Plano, TX (Hybrid). Lead operational "
                    "risk assessments (RCSA), internal control testing, fraud risk root-cause remediation, "
                    "and executive audit reporting across healthcare, financial services, and public sector "
                    "clients. Qualifications: B.S. in Accounting, CFE/CIA/CPA or related certification, "
                    "experience with AuditBoard GRC, Alteryx analytics, and Power BI / Excel dashboards."
                ),
            ),
            (
                "seed-alm-first",
                "Operational Risk & Controls Manager",
                "https://www.almfirst.com/careers/",
                "Dallas, TX (Hybrid)",
                115000,
                140000,
                (
                    "ALM First Financial Advisors (WBENC Certified Woman-Owned financial advisory firm on "
                    "Maple Ave in Dallas, TX) is hiring an Operational Risk & Controls Manager to oversee "
                    "enterprise risk assessments, issue and action plan tracking, control automation, and "
                    "quarterly board/executive risk reporting using Power BI, Excel, and Alteryx across "
                    "depository institution advisory operations. B.S. in Accounting or Finance and CFE "
                    "preferred."
                ),
            ),
            (
                "seed-proven-recruiting",
                "Audit Supervisor – Financial Services (DFW)",
                "https://www.provenrecruiting.com/jobs/",
                "Dallas, TX (Hybrid)",
                None,
                None,
                (
                    "Proven Recruiting (Minority- & Majority Women-Owned Dallas search partner) is retained "
                    "by a DFW financial services client to place an Audit Supervisor (Manager Track). "
                    "Responsibilities include leading operational and regulatory audits, reviewing junior "
                    "analyst workpapers, managing remediation action plans, and supporting GRC workflows. "
                    "Requires B.S. in Accounting and 6+ years of internal audit or operational risk experience."
                ),
            ),
            (
                "seed-alm-first",
                "Financial Analyst I, ALM Strategy Group",
                "https://www.almfirst.com/careers/#fa1",
                "Dallas, TX",
                75000,
                92000,
                "Entry-level Financial Analyst I supporting ALM modeling. Base salary $75,000 - $92,000.",
            ),
        ]

        flagship_job_ids: list[str] = []
        for comp_id, title, url, loc, s_min, s_max, desc in flagship_specs:
            comp = self.db.get_company(comp_id)
            if not comp:
                continue
            j = JobRecord(
                id=make_job_id(comp_id, title, url),
                company_id=comp_id,
                company_name=comp.company_name,
                title=title,
                url=url,
                location=loc,
                description=desc,
                salary_min=s_min,
                salary_max=s_max,
                archetype=classify_employer_archetype(comp),  # type: ignore[arg-type]
            )
            bd = self.matcher.score_job(j, comp)
            j.score = bd.total_score
            j.score_breakdown = bd
            if j.score >= self.settings.auto_apply_min_score:
                j.status = "QUEUED_AUTO"
            elif j.score >= self.settings.min_queue_score:
                j.status = "QUEUED_HITL"
            else:
                j.status = "FILTERED_OUT"
            saved_j = self.db.upsert_job(j)
            if saved_j.score >= self.settings.min_queue_score:
                flagship_job_ids.append(saved_j.id)

        # Also run live portal scraping across active portals
        matcher_summary = self.matcher.run(max_daily_queue=self.settings.daily_queue_limit)

        # 3. Process the 3 live DFW target requisitions (McConnell & Jones, ALM First, Proven Recruiting)
        processed_results: list[dict[str, Any]] = []
        for jid in flagship_job_ids[:3]:
            job_obj = self.db.get_job(jid)
            if job_obj:
                # Force fresh dry-run execution even if previously run
                job_obj.status = (
                    "QUEUED_AUTO"
                    if job_obj.score >= self.settings.auto_apply_min_score
                    else "QUEUED_HITL"
                )
                processed_results.append(self.process_single_job(job_obj))

        # 4. Generate End-of-Day Daily Digest HTML & MIME email
        digest_result = self.hitl.send_daily_digest()

        return {
            "dry_run": True,
            "hallucination_verification": hallucination_report,
            "sourcing_summary": sourcing_summary,
            "matcher_summary": {
                "evaluated_count": matcher_summary["evaluated_count"],
                "queued_auto_count": matcher_summary["queued_auto_count"],
                "queued_hitl_count": matcher_summary["queued_hitl_count"],
                "filtered_out_count": matcher_summary["filtered_out_count"],
            },
            "processed_applications": processed_results,
            "daily_digest": digest_result,
        }

