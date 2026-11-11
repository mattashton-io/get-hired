"""Pydantic data models for companies, jobs, scoring, tailoring, verification, and applications."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CompanyRecord(BaseModel):
    company_id: str
    company_name: str
    website_url: str = ""
    careers_url: str = ""
    ats_type: Literal[
        "workday", "greenhouse", "lever", "icims", "bamboohr", "paylocity", "custom", "none"
    ] = "custom"
    location_hq: str = "Dallas, TX"
    certifications: list[str] = Field(default_factory=list)
    industry_verticals: list[str] = Field(default_factory=list)
    entity_type: Literal["direct_employer", "recruiting_agency"] = "direct_employer"
    outreach_candidate: bool = False
    contact_metadata: dict[str, Any] = Field(default_factory=dict)
    last_checked_at: str | None = None


class ScoreBreakdown(BaseModel):
    compensation_and_level_pts: int = 0
    certification_and_sb_pts: int = 0
    tech_stack_and_credentials_pts: int = 0
    domain_and_location_pts: int = 0
    penalties_pts: int = 0
    total_score: int = 0
    auto_rejected: bool = False
    rejection_reason: str | None = None
    matched_tools: list[str] = Field(default_factory=list)
    matched_certifications: list[str] = Field(default_factory=list)
    rationale: str = ""


class JobRecord(BaseModel):
    id: str
    company_id: str
    company_name: str = ""
    title: str
    url: str
    location: str = "Dallas, TX"
    description: str = ""
    salary_min: int | None = None
    salary_max: int | None = None
    score: int = 0
    score_breakdown: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    archetype: Literal[
        "federal_contractor", "advisory_cpa_firm", "recruiting_agency"
    ] = "advisory_cpa_firm"
    status: Literal[
        "DISCOVERED",
        "QUEUED_AUTO",
        "QUEUED_HITL",
        "FILTERED_OUT",
        "TAILORING",
        "AWAITING_HITL",
        "APPROVED_HITL",
        "SKIPPED_HITL",
        "SUBMITTED",
        "DRY_RUN_COMPLETE",
        "FAILED",
    ] = "DISCOVERED"


class TailoredRoleBullets(BaseModel):
    role_id: str
    ordered_bullets: list[str]


class TailoredResumeLLMResponse(BaseModel):
    executive_summary: str
    role_bullets: list[TailoredRoleBullets]
    selected_skills: list[str]
    tailored_cover_note: str
    modification_log: list[str]


class TailoredResumeOutput(BaseModel):
    archetype: str
    executive_summary: str
    roles: list[dict[str, Any]]
    selected_skills: list[str]
    tailored_resume_markdown: str
    tailored_cover_note: str
    modification_log: list[str]
    is_master_fallback: bool = False


class LLMVerificationCheck(BaseModel):
    credential_integrity_pass: bool
    chronology_lock_pass: bool
    metric_invariance_pass: bool
    tool_hallucination_pass: bool
    formatting_sanity_pass: bool
    violations: list[str] = Field(default_factory=list)


class VerificationResult(BaseModel):
    verified: bool
    attempt_number: int = 1
    programmatic_passed: bool = True
    llm_passed: bool = True
    violations: list[str] = Field(default_factory=list)
    used_master_fallback: bool = False


class HITLReplyCommand(BaseModel):
    thread_id: str
    job_id: str
    application_id: str
    action: Literal["APPROVE", "SKIP", "EDIT"]
    edited_text: str | None = None
    raw_reply: str = ""

