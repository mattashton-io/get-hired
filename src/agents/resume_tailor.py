"""Phase 3.1: Dynamic Resume Tailor Agent (`ResumeTailorAgent`)."""

from __future__ import annotations

import logging
from typing import Any

from src.config import Settings
from src.llm.gemini_client import GeminiClient
from src.models import JobRecord, TailoredResumeLLMResponse, TailoredResumeOutput

logger = logging.getLogger(__name__)


def render_resume_markdown(
    master_resume: dict[str, Any],
    executive_summary: str,
    roles_payload: list[dict[str, Any]],
    selected_skills: list[str],
) -> str:
    """Renders canonical markdown assembling immutable identity/chronology headers with tailored mutable fields."""
    ident = master_resume["candidate_identity"]
    edu = master_resume["education"]["entries"][0]
    cert = master_resume["certifications"]["active"][0]

    lines: list[str] = [
        f"# {ident['full_name']['value']}",
        (
            f"{ident['location']['value']} • {ident['email']['value']} • "
            f"{ident['phone']['value']} • {ident['linkedin_url']['value']}"
        ),
        "",
        "## Summary",
        executive_summary.strip(),
        "",
        "## Experience",
    ]

    for role in roles_payload:
        lines.append(f"### {role['company']} | {role['location']}")
        lines.append(f"**{role['title']}** | {role['dates']}")
        for b in role["bullets"]:
            clean_b = b.strip().lstrip("•-* ").strip()
            lines.append(f"- {clean_b}")
        lines.append("")

    lines.extend(
        [
            "## Education",
            f"**{edu['institution']['value']}** | {edu['location']['value']}",
            f"{edu['degree']['value']} | {edu['graduation_date']['value']}",
            "",
            "## Certifications",
            f"{cert['name']['value']}, {cert['issuer']['value']} | {cert['issue_date']['value']}",
            "",
            "## Skills",
        ]
    )
    for skill in selected_skills:
        lines.append(f"- {skill}")

    return "\n".join(lines).strip() + "\n"


class ResumeTailorAgent:
    """Tailors ONLY mutable resume fields for a target JD while keeping immutable facts locked."""

    def __init__(self, settings: Settings, llm: GeminiClient) -> None:
        self.settings = settings
        self.llm = llm
        self.skill_instructions = settings.load_skill("resume_tailor.md")
        self.persona_text = settings.load_persona_text()
        self.master_resume = settings.load_master_resume()

    def build_master_fallback(self, archetype: str = "advisory_cpa_firm") -> TailoredResumeOutput:
        """Returns the completely unmodified baseline master resume when verification retries exhaust."""
        mr = self.master_resume
        summary = mr["executive_summary"]["value"]
        roles_payload = [
            {
                "role_id": r["role_id"],
                "company": r["company"]["value"],
                "location": r["location"]["value"],
                "title": r["title"]["value"],
                "dates": r["dates"]["value"],
                "bullets": [b["value"] for b in r["bullets"]],
            }
            for r in mr["experience"]["roles"]
        ]
        skills = list(mr["skills_section"]["baseline_docx_skills"])
        cover_note = mr["cover_note"]["baseline_value"]
        if archetype == "recruiting_agency":
            cover_note += (
                " Seeking Internal Audit Manager, Audit Supervisor, or Lead Operational Risk "
                "opportunities in the $110,000–$150,000 base compensation band across DFW or Remote."
            )
        md = render_resume_markdown(mr, summary, roles_payload, skills)
        return TailoredResumeOutput(
            archetype=archetype,
            executive_summary=summary,
            roles=roles_payload,
            selected_skills=skills,
            tailored_resume_markdown=md,
            tailored_cover_note=cover_note,
            modification_log=["Fallback to unmodified canonical master_resume.json"],
            is_master_fallback=True,
        )

    def tailor(
        self,
        job: JobRecord,
        verifier_feedback: list[str] | None = None,
    ) -> TailoredResumeOutput:
        if not self.llm.available:
            return self.build_master_fallback(job.archetype)

        mr = self.master_resume
        archetype_angle = mr["cover_note"]["archetype_angles"].get(
            job.archetype, mr["cover_note"]["archetype_angles"]["advisory_cpa_firm"]
        )
        approved_skills = mr["skills_section"]["approved_skills_bank"]["values"]

        roles_spec = []
        for r in mr["experience"]["roles"]:
            roles_spec.append(
                {
                    "role_id": r["role_id"],
                    "company": r["company"]["value"],
                    "title": r["title"]["value"],
                    "dates": r["dates"]["value"],
                    "canonical_bullets": [
                        {
                            "bullet_id": b["bullet_id"],
                            "text": b["value"],
                            "locked_metrics": b["locked_metrics"]["value"],
                        }
                        for b in r["bullets"]
                    ],
                }
            )

        feedback_section = ""
        if verifier_feedback:
            joined_fb = "\n".join(f"- {v}" for v in verifier_feedback)
            feedback_section = (
                "\nCRITICAL VERIFIER REJECTION FEEDBACK FROM PREVIOUS ATTEMPT (MUST FIX ALL):\n"
                f"{joined_fb}\n"
            )

        prompt = f"""
Tailor Eryka Smith's mutable resume fields for the target job requisition below.

TARGET REQUISITION:
- Title: {job.title}
- Company: {job.company_name}
- Location: {job.location}
- Employer Archetype: {job.archetype}
- Archetype Angle Directive: {archetype_angle}
- Job Description:
{job.description[:4000]}
{feedback_section}

CANONICAL EXPERIENCE ROLES & LOCKED METRICS:
{roles_spec}

BASELINE EXECUTIVE SUMMARY:
{mr["executive_summary"]["value"]}

APPROVED SKILLS BANK (YOU MUST SELECT 6 TO 9 EXACT STRINGS ONLY FROM THIS LIST):
{approved_skills}

STRICT ZERO-HALLUCINATION RULES:
1. NEVER mention or claim unheld certifications/clearances anywhere (even to negate them): DO NOT write the acronyms CPA, CIA, CISA, CRISC, CISSP, Top Secret, TS/SCI, or Secret Clearance anywhere in executive_summary, bullets, or tailored_cover_note. Position her "Bachelor of Science in Accounting, Certified Fraud Examiner (CFE), and 8 years of experience" positively.
2. NEVER insert tools not in her profile (ONLY AuditBoard, Alteryx, Power BI, and Excel are allowed; NEVER mention ServiceNow, Archer, Workiva, MetricStream, SAP, Oracle, Tableau, SQL, Python, ACL, IDEA, etc.).
3. Every role in `role_bullets` (`exp_transamerica` = 4 bullets, `exp_fidelity` = 3 bullets, `exp_cvs` = 3 bullets) MUST include ALL canonical bullets for that role (reordered for relevance and lightly verb-framed to match JD terminology) AND MUST preserve every exact locked metric substring verbatim:
   - exp_transamerica bullets must retain: "30+", "12+", "$28M", "94%", "60%", "40 to 16"
   - exp_fidelity bullets must retain: "25+", "12 controls", "10,000+"
   - exp_cvs bullets must retain: "90%+"
4. Do NOT invent any new numbers, percentages, team sizes, or dollar figures anywhere in `executive_summary`, `role_bullets`, or `tailored_cover_note` (only canonical numbers: 8 years, 50+, 30+, 12+, $28M, 94%, 60%, 40 to 16, 25+, 12 controls, 10,000+, 90%+; plus "$110,000–$150,000" ONLY in `tailored_cover_note` when Employer Archetype is `recruiting_agency`).
5. `selected_skills` MUST contain 6 to 9 strings copied character-for-character from APPROVED SKILLS BANK.
6. `tailored_cover_note` must be a concise ~110–150 word pitch with NO placeholder tokens like [Company] or TBD.
"""
        llm_resp = self.llm.generate_structured(
            prompt=prompt,
            schema=TailoredResumeLLMResponse,
            system_instruction=f"{self.skill_instructions}\n\n{self.persona_text}",
            temperature=0.15,
        )

        bullet_map = {rb.role_id: rb.ordered_bullets for rb in llm_resp.role_bullets}
        assembled_roles: list[dict[str, Any]] = []
        for r in mr["experience"]["roles"]:
            rid = r["role_id"]
            bullets = bullet_map.get(rid) or [b["value"] for b in r["bullets"]]
            assembled_roles.append(
                {
                    "role_id": rid,
                    "company": r["company"]["value"],
                    "location": r["location"]["value"],
                    "title": r["title"]["value"],
                    "dates": r["dates"]["value"],
                    "bullets": bullets,
                }
            )

        selected_skills = llm_resp.selected_skills or list(
            mr["skills_section"]["baseline_docx_skills"]
        )
        md = render_resume_markdown(
            mr,
            llm_resp.executive_summary,
            assembled_roles,
            selected_skills,
        )
        return TailoredResumeOutput(
            archetype=job.archetype,
            executive_summary=llm_resp.executive_summary.strip(),
            roles=assembled_roles,
            selected_skills=selected_skills,
            tailored_resume_markdown=md,
            tailored_cover_note=llm_resp.tailored_cover_note.strip(),
            modification_log=llm_resp.modification_log,
            is_master_fallback=False,
        )

