"""Phase 3.2: Sequential Truth Verifier Agent (`TruthVerifierAgent`) & Retry Loop."""

from __future__ import annotations

import logging
import re
from typing import Any

from src.agents.resume_tailor import ResumeTailorAgent
from src.config import Settings
from src.llm.gemini_client import GeminiClient
from src.models import (
    JobRecord,
    LLMVerificationCheck,
    TailoredResumeOutput,
    VerificationResult,
)

logger = logging.getLogger(__name__)

PROHIBITED_CREDENTIAL_PATTERNS = [
    (r"\bcpa\b", "Prohibited credential claim detected: 'CPA'"),
    (r"\bcertified public accountant\b", "Prohibited credential claim detected: 'Certified Public Accountant'"),
    (r"\bcia\b", "Prohibited credential claim detected: 'CIA'"),
    (r"\bcertified internal auditor\b", "Prohibited credential claim detected: 'Certified Internal Auditor'"),
    (r"\bcisa\b", "Prohibited credential claim detected: 'CISA'"),
    (r"\bcrisc\b", "Prohibited credential claim detected: 'CRISC'"),
    (r"\bcissp\b", "Prohibited credential claim detected: 'CISSP'"),
    (r"\btop secret\b", "Prohibited clearance claim detected: 'Top Secret'"),
    (r"\bts/sci\b", "Prohibited clearance claim detected: 'TS/SCI'"),
    (r"\bsecret clearance\b", "Prohibited clearance claim detected: 'Secret Clearance'"),
]

PROHIBITED_TOOL_PATTERNS = [
    (r"\bservicenow\b", "Unapproved tool hallucination: 'ServiceNow'"),
    (r"\barcher\b", "Unapproved tool hallucination: 'RSA Archer'"),
    (r"\bworkiva\b", "Unapproved tool hallucination: 'Workiva'"),
    (r"\bmetricstream\b", "Unapproved tool hallucination: 'MetricStream'"),
    (r"\bonetrust\b", "Unapproved tool hallucination: 'OneTrust'"),
    (r"\bsap\b", "Unapproved ERP tool hallucination: 'SAP'"),
    (r"\boracle\b", "Unapproved ERP tool hallucination: 'Oracle'"),
    (r"\bnetsuite\b", "Unapproved ERP tool hallucination: 'NetSuite'"),
    (r"\btableau\b", "Unapproved BI tool hallucination: 'Tableau'"),
    (r"\blooker\b", "Unapproved BI tool hallucination: 'Looker'"),
    (r"\bsnowflake\b", "Unapproved data tool hallucination: 'Snowflake'"),
    (r"\bpython\b", "Unapproved programming language hallucination: 'Python'"),
    (r"\bsql\b", "Unapproved query language hallucination: 'SQL'"),
    (r"\bacl\b", "Unapproved audit tool hallucination: 'ACL'"),
]

PLACEHOLDER_PATTERNS = [
    r"\[insert[^\]]*\]",
    r"\[company[^\]]*\]",
    r"\{\{[^}]+\}\}",
    r"\btbd\b",
    r"\btodo\b",
    r"<insert[^>]*>",
]

# Allowed canonical numeric tokens across the resume & cover note
ALLOWED_NUMERIC_TOKENS = {
    "8",
    "50",
    "50+",
    "30",
    "30+",
    "12",
    "12+",
    "28",
    "$28m",
    "28m",
    "94",
    "94%",
    "60",
    "60%",
    "40",
    "16",
    "25",
    "25+",
    "10,000",
    "10,000+",
    "10000",
    "10000+",
    "90",
    "90%",
    "90%+",
    "2016",
    "2018",
    "2020",
    "2021",
    "2023",
    "2024",
    "2026",
    "401",
    "569",
    "9375",
    "1",
    "2",
    "3",
    "1st",
    "2nd",
    "3rd",
}

ALLOWED_COVER_SALARY_TOKENS = {
    "$110,000",
    "110,000",
    "$150,000",
    "150,000",
    "$125,000",
    "125,000",
    "$110k",
    "110k",
    "$150k",
    "150k",
    "$125k",
    "125k",
}


class TruthVerifierAgent:
    """Zero-temperature compliance gatekeeper enforcing programmatic diff + LLM hallucination checks."""

    def __init__(self, settings: Settings, llm: GeminiClient) -> None:
        self.settings = settings
        self.llm = llm
        self.skill_instructions = settings.load_skill("truth_verifier.md")
        self.master_resume = settings.load_master_resume()

    def programmatic_verify(self, tailored: TailoredResumeOutput) -> list[str]:
        """Deterministic string-equality, regex locked-metric, number whitelist, and prohibited-token checks."""
        violations: list[str] = []
        mr = self.master_resume
        md = tailored.tailored_resume_markdown
        cover = tailored.tailored_cover_note
        combined_low = f"{md}\n{cover}".lower()

        # 1. Exact string equality check on all immutable identity, chronology, education, and certification fields
        ident = mr["candidate_identity"]
        immutable_strings = [
            ("Candidate Name", ident["full_name"]["value"]),
            ("Candidate Location", ident["location"]["value"]),
            ("Candidate Email", ident["email"]["value"]),
            ("Candidate Phone", ident["phone"]["value"]),
            ("Education Institution", mr["education"]["entries"][0]["institution"]["value"]),
            ("Education Location", mr["education"]["entries"][0]["location"]["value"]),
            ("Education Degree", mr["education"]["entries"][0]["degree"]["value"]),
            ("Graduation Date", mr["education"]["entries"][0]["graduation_date"]["value"]),
            ("Active Certification", mr["certifications"]["active"][0]["name"]["value"]),
            ("Certification Issuer", mr["certifications"]["active"][0]["issuer"]["value"]),
            ("Certification Date", mr["certifications"]["active"][0]["issue_date"]["value"]),
        ]
        for label, exact_val in immutable_strings:
            if exact_val not in md:
                violations.append(
                    f"Immutable field missing or altered ({label}): expected exact '{exact_val}'"
                )

        # Role-level immutable headers & per-role bullet count + locked metrics
        tailored_role_map = {r["role_id"]: r for r in tailored.roles}
        for canonical_role in mr["experience"]["roles"]:
            rid = canonical_role["role_id"]
            comp = canonical_role["company"]["value"]
            loc = canonical_role["location"]["value"]
            title = canonical_role["title"]["value"]
            dates = canonical_role["dates"]["value"]

            for field_name, val in [
                ("Employer", comp),
                ("Location", loc),
                ("Title", title),
                ("Dates", dates),
            ]:
                if val not in md:
                    violations.append(
                        f"Immutable {field_name} altered for {rid}: expected exact '{val}'"
                    )

            t_role = tailored_role_map.get(rid)
            if not t_role:
                violations.append(f"Missing experience role '{rid}' ({comp}) in structured payload.")
                continue

            if (
                t_role.get("company") != comp
                or t_role.get("location") != loc
                or t_role.get("title") != title
                or t_role.get("dates") != dates
            ):
                violations.append(
                    f"Role header mismatch in '{rid}': must match {comp} | {loc} | {title} | {dates}"
                )

            expected_bullet_count = len(canonical_role["bullets"])
            actual_bullets = t_role.get("bullets", [])
            if len(actual_bullets) != expected_bullet_count:
                violations.append(
                    f"Bullet count changed in {comp}: expected {expected_bullet_count}, got {len(actual_bullets)}"
                )

            joined_role_bullets = " ".join(actual_bullets)
            for b in canonical_role["bullets"]:
                for metric in b["locked_metrics"]["value"]:
                    if not re.search(re.escape(metric), joined_role_bullets):
                        violations.append(
                            f"Locked metric '{metric}' missing or altered in {comp} bullets."
                        )

        # 2. Global regex verification on all 10 locked experience metrics inside markdown
        for metric in mr["locked_metrics_manifest"]["required_experience_metrics"]:
            if not re.search(re.escape(metric), md):
                violations.append(
                    f"Locked metric '{metric}' missing or altered in tailored_resume_markdown."
                )

        # 3. Quantitative Metric Invariance: detect any ungrounded numbers/percentages/dollar figures
        num_pattern = re.compile(r"\$?\d[\d,]*(?:\.\d+)?(?:[mMkK]|%\+|%|\+)?")
        for token in num_pattern.findall(md):
            norm = token.lower().rstrip(".")
            if norm not in ALLOWED_NUMERIC_TOKENS:
                violations.append(
                    f"Ungrounded or inflated numerical figure '{token}' detected in tailored resume."
                )

        for token in num_pattern.findall(cover):
            norm = token.lower().rstrip(".")
            if norm not in ALLOWED_NUMERIC_TOKENS and norm not in ALLOWED_COVER_SALARY_TOKENS:
                violations.append(
                    f"Ungrounded numerical figure '{token}' detected in tailored cover note."
                )

        # 4. Credential, Clearance & Tool Hallucination Regex Check
        for pat, msg in PROHIBITED_CREDENTIAL_PATTERNS:
            if re.search(pat, combined_low):
                violations.append(msg)

        for pat, msg in PROHIBITED_TOOL_PATTERNS:
            if re.search(pat, combined_low):
                violations.append(msg)

        # 5. Approved Skills Bank validation
        approved_set = set(mr["skills_section"]["approved_skills_bank"]["values"])
        for skill in tailored.selected_skills:
            if skill not in approved_set:
                violations.append(
                    f"Unapproved skill '{skill}' not in persona.md approved_skills_bank."
                )

        # 6. Placeholder & formatting sanity check
        for pat in PLACEHOLDER_PATTERNS:
            if re.search(pat, combined_low):
                violations.append(f"Placeholder token matching '{pat}' found in output.")

        return violations

    def llm_verify(
        self, tailored: TailoredResumeOutput, job: JobRecord
    ) -> tuple[bool, list[str]]:
        """Runs zero-temperature (`temperature = 0.0`) LLM compliance verification against JD keyword-stuffing."""
        if not self.llm.available:
            return True, []

        prompt = f"""
Perform a strict, zero-tolerance truth verification (`temperature = 0.0`) comparing MASTER_RESUME,
TARGET_JOB_POSTING, and TAILORED_OUTPUT.

MASTER_RESUME (Canonical Truth Source):
- Name: Eryka Smith | Location: Dallas, TX | Email: Erykasmith@gmail.com | Phone: (401) 569-9375
- Education: Johnson & Wales University (Providence, RI), Bachelor of Science in Accounting, May 2016
- Active Certification: Certified Fraud Examiner (CFE), ACFE, October 2020 (DOES NOT hold CPA, CIA, CISA, CRISC, CISSP, or security clearances)
- Approved Technical Tools: AuditBoard, Alteryx, Power BI, Excel
- Experience:
  1. Transamerica | Remote | Senior Operational Risk Analyst | January 2024 – June 2026 (Locked metrics: 30+ risk assessments, 12+ risk rating changes, $28M in gross losses, 94% on-time completion, 60% / 40 to 16 hours)
  2. Fidelity | Remote | Risk Analyst | October 2021 – December 2023 (Locked metrics: 25+ risk assessments, COE standardization, Alteryx 12 controls, 10,000+ records per control)
  3. CVS Health | Remote | Internal Auditor | August 2018 – October 2021 (Locked metrics: 90%+ on-time completion rate, senior leadership audit reports, AuditBoard Maven)

TARGET_JOB_POSTING:
Title: {job.title} @ {job.company_name}
Description: {job.description[:3000]}

TAILORED_RESUME_MARKDOWN:
{tailored.tailored_resume_markdown}

TAILORED_COVER_NOTE:
{tailored.tailored_cover_note}

Note: Light verb/terminology framing of existing experience bullets and stating the "$110,000–$150,000" target salary band in the cover note for recruiting agencies IS explicitly authorized. Flag FAIL only if credentials, dates, titles, employers, tools, clearances, or metrics are fabricated, altered, or ungrounded.
"""
        try:
            check = self.llm.generate_structured(
                prompt=prompt,
                schema=LLMVerificationCheck,
                system_instruction=self.skill_instructions,
                temperature=0.0,
            )
            passed = (
                check.credential_integrity_pass
                and check.chronology_lock_pass
                and check.metric_invariance_pass
                and check.tool_hallucination_pass
                and check.formatting_sanity_pass
                and len(check.violations) == 0
            )
            return passed, list(check.violations)
        except Exception as exc:
            logger.warning("LLM verification call error: %s", exc)
            return False, [f"LLM verifier execution error: {exc}"]

    def verify(
        self,
        tailored: TailoredResumeOutput,
        job: JobRecord,
        attempt_number: int = 1,
        run_llm_check: bool = True,
    ) -> VerificationResult:
        prog_violations = self.programmatic_verify(tailored)
        prog_passed = len(prog_violations) == 0

        llm_passed = True
        llm_violations: list[str] = []
        if prog_passed and run_llm_check and not tailored.is_master_fallback:
            llm_passed, llm_violations = self.llm_verify(tailored, job)

        all_violations = prog_violations + llm_violations
        return VerificationResult(
            verified=(prog_passed and llm_passed),
            attempt_number=attempt_number,
            programmatic_passed=prog_passed,
            llm_passed=llm_passed,
            violations=all_violations,
            used_master_fallback=tailored.is_master_fallback,
        )


def run_tailor_and_verify_loop(
    tailor_agent: ResumeTailorAgent,
    verifier_agent: TruthVerifierAgent,
    job: JobRecord,
    max_attempts: int = 3,
) -> tuple[TailoredResumeOutput, VerificationResult]:
    """
    Executes sequential tailoring + verification with max 2 retries (3 total attempts).
    Falls back to unmodified master_resume on 3rd failure and flags for HITL escalation.
    """
    feedback: list[str] | None = None
    last_result: VerificationResult | None = None

    for attempt in range(1, max_attempts + 1):
        tailored = tailor_agent.tailor(job, verifier_feedback=feedback)
        ver_result = verifier_agent.verify(tailored, job, attempt_number=attempt)
        if ver_result.verified:
            return tailored, ver_result
        logger.warning(
            "TruthVerifier rejected attempt %d/%d for %s @ %s: %s",
            attempt,
            max_attempts,
            job.title,
            job.company_name,
            ver_result.violations,
        )
        feedback = ver_result.violations
        last_result = ver_result

    # Attempt 3 failed -> fall back to unmodified master_resume
    fallback_output = tailor_agent.build_master_fallback(job.archetype)
    fallback_ver = verifier_agent.verify(
        fallback_output, job, attempt_number=max_attempts, run_llm_check=False
    )
    fallback_ver.used_master_fallback = True
    if last_result:
        fallback_ver.violations = last_result.violations
    return fallback_output, fallback_ver

