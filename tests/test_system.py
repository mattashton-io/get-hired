"""Comprehensive test suite covering Phases 1–7 of the Autonomous Job Agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.agents.ats_applicator import ATSApplicatorAgent
from src.agents.hitl_gmail_comms import HITLGmailAgent, parse_hitl_reply_text
from src.agents.job_matcher_scorer import JobMatcherScorerAgent
from src.agents.resume_tailor import ResumeTailorAgent
from src.agents.truth_verifier import TruthVerifierAgent, run_tailor_and_verify_loop
from src.compiler.document_compiler import DocumentCompiler
from src.config import Settings, get_settings
from src.integrations.gmail_client import GmailClient
from src.llm.gemini_client import GeminiClient
from src.models import CompanyRecord, JobRecord, TailoredResumeOutput
from src.orchestrator.pipeline import AutonomousJobOrchestrator
from src.orchestrator.scheduler import build_scheduler
from src.state.db import ApplicationsLedgerDB


@pytest.fixture()
def temp_settings(tmp_path: Path) -> Settings:
    base = get_settings(dry_run=True)
    s = Settings(
        project_root=base.project_root,
        skills_dir=base.skills_dir,
        persona_path=base.persona_path,
        master_resume_path=base.master_resume_path,
        data_dir=tmp_path / "data",
        target_companies_path=tmp_path / "data" / "target_companies.json",
        ledger_db_path=tmp_path / "data" / "applications_ledger.db",
        compiled_resumes_dir=tmp_path / "data" / "compiled_resumes",
        screenshots_dir=tmp_path / "data" / "screenshots",
        outbox_dir=tmp_path / "data" / "outbox",
        session_states_dir=tmp_path / "data" / "session_states",
        gemini_api_key="",  # Offline deterministic unit testing
        dry_run=True,
    )
    s.ensure_dirs()
    s.target_companies_path.write_text(
        base.target_companies_path.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return s


def test_phase1_master_resume_mutability_and_seed_db(temp_settings: Settings) -> None:
    mr = temp_settings.load_master_resume()
    assert mr["candidate_identity"]["mutable"] is False
    assert mr["candidate_identity"]["full_name"]["value"] == "Eryka Smith"
    assert mr["executive_summary"]["mutable"] is True
    assert mr["education"]["mutable"] is False
    assert mr["certifications"]["mutable"] is False
    assert len(mr["experience"]["roles"]) == 3

    db = ApplicationsLedgerDB(
        temp_settings.ledger_db_path, temp_settings.target_companies_path
    )
    seeded = db.seed_from_target_companies_json()
    assert seeded >= 14
    assert len(db.list_companies()) == seeded
    assert len(db.list_companies(outreach_only=True)) >= 4


def test_phase2_job_scorer_guardrails(temp_settings: Settings) -> None:
    db = ApplicationsLedgerDB(
        temp_settings.ledger_db_path, temp_settings.target_companies_path
    )
    db.seed_from_target_companies_json()
    llm = GeminiClient(api_key="")
    scorer = JobMatcherScorerAgent(temp_settings, db, llm)

    comp = db.get_company("seed-mcconnell-jones")
    assert comp is not None

    # 1. Sub-$105K max salary -> Auto-Reject (-100)
    low_sal_job = JobRecord(
        id="j1",
        company_id=comp.company_id,
        title="Operational Risk Manager",
        url="https://example.com/j1",
        salary_min=85000,
        salary_max=100000,
        description="Operational risk assessments with AuditBoard.",
    )
    assert scorer.score_job(low_sal_job, comp).total_score == -100

    # 2. Rejected junior title ('Financial Analyst I') -> Auto-Reject (-100)
    jr_job = JobRecord(
        id="j2",
        company_id=comp.company_id,
        title="Financial Analyst I",
        url="https://example.com/j2",
        description="Financial reporting role in Dallas, TX.",
    )
    assert scorer.score_job(jr_job, comp).total_score == -100

    # 3. High-fit Manager role in $110K-$150K band with Big Three tools + CFE -> >= 85
    high_job = JobRecord(
        id="j3",
        company_id=comp.company_id,
        title="Internal Audit & Risk Advisory Manager",
        url="https://example.com/j3",
        location="Dallas, TX (Hybrid)",
        salary_min=120000,
        salary_max=145000,
        description=(
            "Lead RCSA risk assessments, control testing, and fraud remediation across insurance "
            "and healthcare clients. Requires B.S. in Accounting, CFE, AuditBoard, Alteryx, and Power BI."
        ),
    )
    bd = scorer.score_job(high_job, comp)
    assert bd.total_score >= 95

    # 4. Mandatory TS/SCI clearance penalty (-35 pts)
    ts_job = high_job.model_copy(
        update={
            "id": "j4",
            "description": high_job.description + " Active TS/SCI clearance required Day 1.",
        }
    )
    bd_ts = scorer.score_job(ts_job, comp)
    assert bd_ts.penalties_pts == -35


def test_phase3_truth_verifier_and_retry_fallback_and_compiler(
    temp_settings: Settings,
) -> None:
    llm = GeminiClient(api_key="")
    tailor = ResumeTailorAgent(temp_settings, llm)
    verifier = TruthVerifierAgent(temp_settings, llm)
    compiler = DocumentCompiler(temp_settings)

    job = JobRecord(
        id="job-test-123",
        company_id="seed-alm-first",
        company_name="ALM First Financial Advisors",
        title="Operational Risk Manager",
        url="https://www.almfirst.com/careers/",
    )

    baseline = tailor.build_master_fallback("advisory_cpa_firm")
    res_ok = verifier.verify(baseline, job, run_llm_check=False)
    assert res_ok.verified is True

    # Inject synthetic CPA claim -> must FAIL
    bad_cpa = baseline.model_copy(deep=True)
    bad_cpa.is_master_fallback = False
    bad_cpa.tailored_resume_markdown += "\nLicensed CPA in Texas.\n"
    res_cpa = verifier.verify(bad_cpa, job, run_llm_check=False)
    assert res_cpa.verified is False
    assert any("CPA" in v for v in res_cpa.violations)

    # Test 3-attempt failure fallback to unmodified master_resume
    class AlwaysHallucinatingTailor(ResumeTailorAgent):
        def tailor(
            self, job: JobRecord, verifier_feedback: list[str] | None = None
        ) -> TailoredResumeOutput:
            out = self.build_master_fallback(job.archetype)
            out.is_master_fallback = False
            out.tailored_cover_note += " Certified CPA and CISA with $99M managed."
            return out

    bad_tailor = AlwaysHallucinatingTailor(temp_settings, llm)
    final_out, final_ver = run_tailor_and_verify_loop(
        bad_tailor, verifier, job, max_attempts=3
    )
    assert final_out.is_master_fallback is True
    assert final_ver.used_master_fallback is True
    assert final_ver.verified is True

    # Compile .docx and .pdf artifacts
    compiled = compiler.compile(final_out, "ALM First", role_title=job.title)
    assert Path(compiled["docx_path"]).exists()
    assert Path(compiled["pdf_path"]).exists()
    assert Path(compiled["pdf_path"]).stat().st_size > 1500
    pdf_name = Path(compiled["pdf_path"]).name
    assert pdf_name == "Eryka_Smith_Resume_ALM_First_Operational_Risk_Manager.pdf"
    assert "job" not in pdf_name.lower()
    assert not any(c.isdigit() for c in pdf_name.replace(".pdf", ""))


def test_phase4_hitl_reply_parsing_polling_and_digest(temp_settings: Settings) -> None:
    db = ApplicationsLedgerDB(
        temp_settings.ledger_db_path, temp_settings.target_companies_path
    )
    db.seed_from_target_companies_json()
    gmail = GmailClient(temp_settings)
    hitl = HITLGmailAgent(temp_settings, db, gmail)
    llm = GeminiClient(api_key="")
    tailor = ResumeTailorAgent(temp_settings, llm)
    compiler = DocumentCompiler(temp_settings)

    comp = db.get_company("seed-proven-recruiting")
    assert comp is not None
    job = JobRecord(
        id="job-hitl-80",
        company_id=comp.company_id,
        company_name=comp.company_name,
        title="Audit Supervisor",
        url="https://www.provenrecruiting.com/jobs/",
        score=80,
        status="QUEUED_HITL",
    )
    db.upsert_job(job)

    out = tailor.build_master_fallback("recruiting_agency")
    files = compiler.compile(out, comp.company_name, role_title=job.title)
    app_id = db.create_or_update_application(
        job_id=job.id,
        resume_pdf_path=files["pdf_path"],
        resume_docx_path=files["docx_path"],
        cover_note=out.tailored_cover_note,
        tailored_markdown=out.tailored_resume_markdown,
        verifier_passed=True,
        verifier_attempts=1,
        verifier_report={"verified": True},
        resume_hash=files["resume_hash"],
    )

    delivery = hitl.send_hitl_approval_email(
        application_id=app_id,
        job=job,
        company=comp,
        hitl_reason="Score 80 in 75-84 HITL band",
        drafted_response=out.tailored_cover_note,
        resume_pdf_path=files["pdf_path"],
        resume_docx_path=files["docx_path"],
    )
    assert Path(delivery["html_path"]).exists()
    assert Path(delivery["eml_path"]).exists()

    # Simulate candidate replying with ---BEGIN EDIT--- block
    gmail.inject_simulated_reply(
        delivery["thread_id"],
        "Looks great! Please use this custom note:\n---BEGIN EDIT---\nUpdated custom cover note for Proven Recruiting ($125,000 target).\n---END EDIT---",
    )
    callback_invocations: list[dict[str, Any]] = []
    polled = hitl.poll_hitl_replies(
        on_approved_callback=lambda row: (callback_invocations.append(row) or {"status": "OK"})
    )
    assert len(polled) == 1
    assert polled[0]["action"] == "EDIT"
    assert len(callback_invocations) == 1
    assert "Updated custom cover note" in callback_invocations[0]["cover_note"]

    digest = hitl.send_daily_digest(date_str="2026-11-11")
    assert Path(digest["delivery"]["html_path"]).exists()
    assert digest["outreach_count"] >= 1


def test_phase5_ats_applicator_gatekeeper_and_local_form(
    temp_settings: Settings, tmp_path: Path
) -> None:
    db = ApplicationsLedgerDB(
        temp_settings.ledger_db_path, temp_settings.target_companies_path
    )
    db.seed_from_target_companies_json()
    applicator = ATSApplicatorAgent(temp_settings, db)
    llm = GeminiClient(api_key="")
    tailor = ResumeTailorAgent(temp_settings, llm)
    compiler = DocumentCompiler(temp_settings)

    comp = CompanyRecord(
        company_id="test-gh",
        company_name="DFW Certified Risk Partners",
        careers_url="",
        ats_type="greenhouse",
        certifications=["8(a)", "WOSB"],
    )
    db.upsert_company(comp)

    # Create a local mock Greenhouse HTML form to test Playwright field population + file upload
    mock_form = tmp_path / "mock_greenhouse.html"
    mock_form.write_text(
        """<!DOCTYPE html><html><body>
        <h1>Internal Audit Manager - DFW Certified Risk Partners</h1>
        <form id="application_form">
          <input id="first_name" name="first_name" type="text" />
          <input id="last_name" name="last_name" type="text" />
          <input id="email" name="email" type="email" />
          <input id="phone" name="phone" type="tel" />
          <input name="linkedin_profile" aria-label="LinkedIn Profile" type="text" />
          <input name="desired_salary" type="text" />
          <input name="resume" type="file" />
          <textarea id="cover_letter_text" name="cover_letter"></textarea>
          <button id="submit_app" type="submit">Submit Application</button>
        </form></body></html>""",
        encoding="utf-8",
    )

    job = JobRecord(
        id="job-gh-1",
        company_id=comp.company_id,
        company_name=comp.company_name,
        title="Internal Audit Manager",
        url=mock_form.as_uri(),
        salary_min=120000,
        salary_max=140000,
        score=92,
        status="QUEUED_AUTO",
    )
    db.upsert_job(job)

    out = tailor.build_master_fallback("federal_contractor")
    files = compiler.compile(out, comp.company_name, role_title=job.title)
    app_id = db.create_or_update_application(
        job_id=job.id,
        resume_pdf_path=files["pdf_path"],
        resume_docx_path=files["docx_path"],
        cover_note=out.tailored_cover_note,
        tailored_markdown=out.tailored_resume_markdown,
        verifier_passed=True,
        verifier_attempts=1,
        verifier_report={"verified": True},
        resume_hash=files["resume_hash"],
    )

    # Gatekeeper MUST raise ValueError if verifier_passed == False
    with pytest.raises(ValueError, match="verifier_passed is False"):
        applicator.apply(
            application_id=app_id,
            job=job,
            company=comp,
            resume_pdf_path=files["pdf_path"],
            cover_note=out.tailored_cover_note,
            verifier_passed=False,
        )

    # Valid verified package executes dry-run browser form fill + screenshot without clicking submit
    res = applicator.apply(
        application_id=app_id,
        job=job,
        company=comp,
        resume_pdf_path=files["pdf_path"],
        cover_note=out.tailored_cover_note,
        verifier_passed=True,
    )
    assert res["status"] == "DRY_RUN_COMPLETE"
    assert "first_name" in res["filled_fields"]
    assert "email" in res["filled_fields"]
    assert "resume_pdf_upload" in res["filled_fields"]
    assert res["desired_salary_used"] == 130000
    assert Path(res["screenshot_path"]).exists()


def test_phase6_scheduler_cst_cron_jobs(temp_settings: Settings) -> None:
    sched = build_scheduler(temp_settings, blocking=False)
    job_ids = {j.id for j in sched.get_jobs()}
    assert job_ids == {
        "morning_discovery_0600",
        "staggered_worker_0800_1600",
        "hitl_gmail_poller_15m",
        "eod_daily_digest_1800",
    }
    assert parse_hitl_reply_text("SKIP\nThanks", "t1", "j1", "a1") is not None


def test_phase7_end_to_end_orchestrator_and_hitl_roundtrip(
    temp_settings: Settings, tmp_path: Path
) -> None:
    orchestrator = AutonomousJobOrchestrator(temp_settings)
    orchestrator.db.seed_from_target_companies_json()

    # 1. Synthetic hallucination battery must catch CPA, metric inflation, and unapproved tools
    hall_report = orchestrator.run_synthetic_hallucination_tests()
    assert hall_report["all_assertions_passed"] is True
    assert hall_report["cpa_hallucination_caught"] is True
    assert hall_report["metric_inflation_caught"] is True
    assert hall_report["tool_injection_caught"] is True

    # 2. Local ATS HTML portal for end-to-end dry-run Tier-1 & Tier-2 verification
    mock_portal = tmp_path / "e2e_portal.html"
    mock_portal.write_text(
        """<!DOCTYPE html><html><body>
        <form id="app">
          <input name="first_name" type="text" />
          <input name="last_name" type="text" />
          <input name="email" type="email" />
          <input name="resume" type="file" />
          <button type="submit">Submit Application</button>
        </form></body></html>""",
        encoding="utf-8",
    )

    # Deterministic offline tailor stub returning a valid tailored output (is_master_fallback=False)
    orig_build = orchestrator.tailor.build_master_fallback

    def _deterministic_tailor(
        job: JobRecord, verifier_feedback: list[str] | None = None
    ) -> TailoredResumeOutput:
        out = orig_build(job.archetype)
        out.is_master_fallback = False
        return out

    orchestrator.tailor.tailor = _deterministic_tailor  # type: ignore[method-assign]

    # Tier-1 Auto-Apply job (Score >= 85) -> Direct ATS Dry-Run execution
    tier1_job = JobRecord(
        id="job-e2e-tier1",
        company_id="seed-alm-first",
        company_name="ALM First Financial Advisors",
        title="Operational Risk & Controls Manager",
        url=mock_portal.as_uri(),
        location="Dallas, TX (Hybrid)",
        salary_min=115000,
        salary_max=140000,
        score=96,
        status="QUEUED_AUTO",
        archetype="advisory_cpa_firm",
    )
    orchestrator.db.upsert_job(tier1_job)
    res1 = orchestrator.process_single_job(tier1_job)
    assert res1["verifier_passed"] is True
    assert res1["outcome"] == "DRY_RUN_COMPLETE"
    assert Path(res1["compiled_resume_pdf"]).exists()
    assert Path(res1["compiled_resume_docx"]).exists()

    # Tier-2 HITL Escalation job (75 <= Score < 85) -> Candidate APPROVE -> ATS Dry-Run execution
    tier2_job = JobRecord(
        id="job-e2e-tier2",
        company_id="seed-proven-recruiting",
        company_name="Proven Recruiting",
        title="Audit Supervisor – Financial Services",
        url=mock_portal.as_uri(),
        location="Dallas, TX (Hybrid)",
        score=78,
        status="QUEUED_HITL",
        archetype="recruiting_agency",
    )
    orchestrator.db.upsert_job(tier2_job)
    res2 = orchestrator.process_single_job(tier2_job)
    assert res2["outcome"] == "ESCALATED_TO_HITL"
    thread_id = res2["hitl_delivery"]["thread_id"]

    orchestrator.gmail.inject_simulated_reply(thread_id, "APPROVE\nGo ahead and submit.")
    polled = orchestrator.poll_hitl_and_execute()
    assert len(polled) == 1
    assert polled[0]["action"] == "APPROVE"
    assert polled[0]["execution"]["status"] == "DRY_RUN_COMPLETE"

    # EOD Daily Digest includes both completed dry-run applications
    digest = orchestrator.hitl.send_daily_digest(date_str="2026-11-11")
    assert digest["submitted_count"] == 2
    assert digest["pending_hitl_count"] == 0
    assert Path(digest["delivery"]["html_path"]).exists()


def test_phase7_live_gemini_tailor_and_verify_e2e(tmp_path: Path) -> None:
    live_base = get_settings(dry_run=True)
    if not live_base.gemini_api_key:
        pytest.skip("GEMINI_API_KEY not configured for live Gemini E2E check")

    s = Settings(
        project_root=live_base.project_root,
        skills_dir=live_base.skills_dir,
        persona_path=live_base.persona_path,
        master_resume_path=live_base.master_resume_path,
        data_dir=tmp_path / "live_data",
        target_companies_path=tmp_path / "live_data" / "target_companies.json",
        ledger_db_path=tmp_path / "live_data" / "applications_ledger.db",
        compiled_resumes_dir=tmp_path / "live_data" / "compiled_resumes",
        screenshots_dir=tmp_path / "live_data" / "screenshots",
        outbox_dir=tmp_path / "live_data" / "outbox",
        session_states_dir=tmp_path / "live_data" / "session_states",
        gemini_api_key=live_base.gemini_api_key,
        gemini_model=live_base.gemini_model,
        dry_run=True,
    )
    s.ensure_dirs()
    s.target_companies_path.write_text(
        live_base.target_companies_path.read_text(encoding="utf-8"), encoding="utf-8"
    )

    llm = GeminiClient(api_key=s.gemini_api_key, default_model=s.gemini_model)
    tailor = ResumeTailorAgent(s, llm)
    verifier = TruthVerifierAgent(s, llm)

    # Tricky JD requesting CPA/CIA and ServiceNow to confirm live LLM never hallucinates them
    job = JobRecord(
        id="job-live-gemini-e2e",
        company_id="seed-mcconnell-jones",
        company_name="McConnell & Jones LLP",
        title="Internal Audit & Risk Advisory Manager",
        url="https://www.mcconnelljones.com/careers/",
        location="Dallas, TX (Hybrid)",
        salary_min=120000,
        salary_max=145000,
        description=(
            "Black-owned CPA & advisory firm seeking an Internal Audit & Risk Advisory Manager. "
            "Lead RCSA risk assessments, fraud remediation, and AuditBoard/ServiceNow GRC engagements. "
            "CPA, CIA, or CFE preferred; Alteryx and Power BI experience highly valued."
        ),
        archetype="advisory_cpa_firm",
    )

    tailored, ver = run_tailor_and_verify_loop(tailor, verifier, job, max_attempts=3)
    assert ver.verified is True
    assert ver.programmatic_passed is True
    assert ver.llm_passed is True
    assert ver.used_master_fallback is False
    assert "CPA" not in tailored.tailored_resume_markdown
    assert "ServiceNow" not in tailored.tailored_resume_markdown
    assert "$28M" in tailored.tailored_resume_markdown


