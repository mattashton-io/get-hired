"""Phase 2.2: Job Matcher & Scorer Agent (`JobMatcherScorerAgent`)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from src.config import Settings
from src.llm.gemini_client import GeminiClient
from src.models import CompanyRecord, JobRecord, ScoreBreakdown
from src.state.db import ApplicationsLedgerDB

logger = logging.getLogger(__name__)

APPROVED_TITLE_PATTERNS = [
    r"\boperational risk manager\b",
    r"\bsenior operational risk analyst\b",
    r"\binternal audit manager\b",
    r"\baudit manager\b",
    r"\baudit supervisor\b",
    r"\bit audit manager\b",
    r"\bbusiness process audit manager\b",
    r"\brisk advisory manager\b",
    r"\brisk manager\b",
    r"\bsenior financial consultant\b",
    r"\bcompliance\s*(?:&|and)?\s*risk manager\b",
    r"\bgrc manager\b",
    r"\bgrc lead\b",
    r"\bauditboard\b.*\b(?:lead|manager|specialist|admin)\b",
    r"\bsubject matter expert\b",
    r"\bsme\b",
    r"\blead risk\b",
    r"\bsenior management consultant\b",
    r"\btask order manager\b",
    r"\bdirector\b.*\b(?:risk|audit|compliance|controls)\b",
    r"\bsenior manager\b.*\b(?:risk|audit|compliance|advisory)\b",
    r"\bmanager\b.*\b(?:risk|audit|controls|compliance|advisory|assurance|fraud)\b",
    r"\bsupervisor\b.*\b(?:audit|risk|assurance|advisory)\b",
    r"\blead\b.*\b(?:audit|risk|controls|compliance|grc)\b",
]

REJECTED_TITLE_PATTERNS = [
    r"\bfinancial analyst\s*(?:i|ii|1|2)\b",
    r"\bfinancial analyst,\s*alm\b",
    r"\bcpst\b",
    r"\binvestment operations analyst\b",
    r"\bbusiness management analyst\s*(?:i|ii|1|2)\b",
    r"\banalyst\s*(?:i|ii|1|2)\b",
    r"\bstaff auditor\b",
    r"\bstaff accountant\b",
    r"\bjunior\b",
    r"\bassociate consultant\b",
    r"\bentry[\s-]level\b",
    r"\bintern\b",
    r"\bbookkeeper\b",
    r"\baccounts payable\b",
    r"\baccounts receivable\b",
]

CORE_TOOLS = [
    ("AuditBoard", [r"\bauditboard\b", r"\bgrc platform\b", r"\bgrc system\b"]),
    ("Alteryx", [r"\balteryx\b", r"\bdata analytics\b.*\bautomation\b"]),
    ("Power BI", [r"\bpower\s*bi\b"]),
    ("Excel", [r"\badvanced excel\b", r"\bexcel\b"]),
]

CREDENTIAL_PATTERNS = [
    r"\bcfe\b",
    r"\bcertified fraud examiner\b",
    r"\baccounting\b",
    r"\bb\.?s\.?\s+in\s+accounting\b",
    r"\bfinance\b",
]

FUNCTIONAL_PILLAR_PATTERNS = [
    r"\brisk assessment",
    r"\brcsa\b",
    r"\binternal audit",
    r"\bcontrol testing",
    r"\binternal control",
    r"\bfraud\b",
    r"\broot[\s-]cause",
    r"\baction plan",
    r"\bremediation\b",
    r"\bexecutive reporting\b",
    r"\bsox\b",
    r"\boperational risk\b",
]

DFW_AND_REMOTE_PATTERNS = [
    r"\bdallas\b",
    r"\bfort worth\b",
    r"\bdfw\b",
    r"\bplano\b",
    r"\birving\b",
    r"\barlington\b",
    r"\bcoppell\b",
    r"\bfrisco\b",
    r"\brichardson\b",
    r"\baddison\b",
    r"\blas colinas\b",
    r"\bremote\b",
    r"\btelework\b",
    r"\banywhere in the u\.?s\.?\b",
]

INDUSTRY_PATTERNS = [
    r"\binsurance\b",
    r"\bfinancial services\b",
    r"\bbanking\b",
    r"\bcredit union",
    r"\bdepository\b",
    r"\bhealthcare\b",
    r"\badvisory\b",
    r"\bconsulting\b",
    r"\bfederal\b",
]


def parse_salary_range(text: str) -> tuple[int | None, int | None]:
    """Extracts annual base salary min/max from job text (e.g. '$115,000 - $145,000' or '$120K-$140K')."""
    if not text:
        return None, None
    # Match $110,000 - $150,000 or $110k - $150k
    range_pattern = re.compile(
        r"\$\s*(\d{2,3}(?:,\d{3})+|\d{2,3}\s*[kK])\s*(?:-|–|—|to)\s*\$\s*(\d{2,3}(?:,\d{3})+|\d{2,3}\s*[kK])"
    )
    m = range_pattern.search(text)
    if m:
        low = _normalize_dollar_token(m.group(1))
        high = _normalize_dollar_token(m.group(2))
        if low and high and 40000 <= low <= 400000 and 40000 <= high <= 500000:
            return min(low, high), max(low, high)

    # Single salary mention near compensation keywords
    single_pattern = re.compile(
        r"(?:salary|compensation|pay|base)[^$\n]{0,40}\$\s*(\d{2,3}(?:,\d{3})+|\d{2,3}\s*[kK])",
        re.IGNORECASE,
    )
    m2 = single_pattern.search(text)
    if m2:
        val = _normalize_dollar_token(m2.group(1))
        if val and 40000 <= val <= 400000:
            return val, val
    return None, None


def _normalize_dollar_token(token: str) -> int | None:
    t = token.strip().lower().replace(",", "").replace(" ", "")
    if t.endswith("k"):
        try:
            return int(float(t[:-1]) * 1000)
        except ValueError:
            return None
    try:
        return int(float(t))
    except ValueError:
        return None


def classify_employer_archetype(company: CompanyRecord) -> str:
    if company.entity_type == "recruiting_agency":
        return "recruiting_agency"
    certs_upper = {c.upper() for c in company.certifications}
    verticals_low = " ".join(company.industry_verticals).lower()
    if (
        {"8(A)", "8A", "SDVOSB", "VOSB", "EDWOSB"} & certs_upper
        or "federal" in verticals_low
    ):
        return "federal_contractor"
    return "advisory_cpa_firm"


def make_job_id(company_id: str, title: str, url: str) -> str:
    digest = hashlib.sha1(f"{company_id}|{title.lower().strip()}|{url}".encode()).hexdigest()[:8]
    slug = re.sub(r"[^a-z0-9]+", "-", company_id.replace("seed-", "").replace("disc-", "")).strip("-")[:16]
    return f"job-{slug}-{digest}"


class JobMatcherScorerAgent:
    """Scrapes open requisitions, enforces compensation/LCAT guardrails, and scores 0–100."""

    def __init__(
        self,
        settings: Settings,
        db: ApplicationsLedgerDB,
        llm: GeminiClient,
    ) -> None:
        self.settings = settings
        self.db = db
        self.llm = llm
        self.skill_instructions = settings.load_skill("job_matcher_scorer.md")
        self.persona_text = settings.load_persona_text()

    def score_job(self, job: JobRecord, company: CompanyRecord) -> ScoreBreakdown:
        """Deterministic quantitative scoring rubric (`0-100`, `-100` on hard guardrail reject)."""
        title_low = job.title.lower()
        body_low = f"{job.title}\n{job.location}\n{job.description}".lower()

        s_min, s_max = job.salary_min, job.salary_max
        if s_min is None and s_max is None:
            parsed_min, parsed_max = parse_salary_range(job.description)
            s_min, s_max = parsed_min, parsed_max
            job.salary_min, job.salary_max = s_min, s_max

        # 1. Hard Floor & Rejected Title Check
        if s_max is not None and s_max < self.settings.salary_hard_floor:
            return ScoreBreakdown(
                compensation_and_level_pts=-100,
                total_score=-100,
                auto_rejected=True,
                rejection_reason=f"Published max salary (${s_max:,}) below ${self.settings.salary_hard_floor:,} hard floor",
                rationale="Auto-rejected by published salary hard floor.",
            )

        for rej_pat in REJECTED_TITLE_PATTERNS:
            if re.search(rej_pat, title_low):
                return ScoreBreakdown(
                    compensation_and_level_pts=-100,
                    total_score=-100,
                    auto_rejected=True,
                    rejection_reason=f"Rejected entry/mid-level title pattern matched: '{job.title}'",
                    rationale="Auto-rejected by junior/analyst I-II title guardrail.",
                )

        # Dimension 1: Compensation & Level Alignment (Max 30 pts)
        comp_pts = 0
        matched_approved_title = any(
            re.search(pat, title_low) for pat in APPROVED_TITLE_PATTERNS
        )
        if s_max is not None:
            effective_min = s_min if s_min is not None else s_max
            overlaps_target = (
                effective_min <= self.settings.salary_stretch_max
                and s_max >= self.settings.salary_target_min
            )
            if overlaps_target:
                comp_pts = 30
            elif self.settings.salary_hard_floor <= s_max < self.settings.salary_target_min:
                comp_pts = 10
            elif effective_min > self.settings.salary_stretch_max:
                comp_pts = 20
        else:
            if matched_approved_title:
                comp_pts = 25
            elif any(k in title_low for k in ("senior", "principal", "advisor", "consultant", "auditor")):
                comp_pts = 15
            else:
                comp_pts = 0

        # Dimension 2: Employer Certification & Small Business Fit (Max 25 pts)
        cert_pts = 0
        if company.entity_type == "direct_employer" and company.certifications:
            cert_pts = 25
        elif company.entity_type == "recruiting_agency" and company.certifications:
            cert_pts = 20

        # Dimension 3: Technical Stack & Credential Match (Max 30 pts)
        matched_tools: list[str] = []
        for tool_name, patterns in CORE_TOOLS:
            if any(re.search(p, body_low) for p in patterns):
                matched_tools.append(tool_name)
        tool_pts = min(15, len(matched_tools) * 5)

        matched_creds: list[str] = []
        for cred_pat in CREDENTIAL_PATTERNS:
            if re.search(cred_pat, body_low):
                matched_creds.append(cred_pat.strip(r"\b"))
        cred_pts = 10 if matched_creds else 0

        pillar_hits = sum(1 for p in FUNCTIONAL_PILLAR_PATTERNS if re.search(p, body_low))
        pillar_pts = 5 if pillar_hits >= 1 else 0
        tech_cred_pts = tool_pts + cred_pts + pillar_pts

        # Dimension 4: Domain & Location Fit (Max 15 pts)
        loc_text = f"{job.location} {company.location_hq} {job.description}".lower()
        loc_pts = 10 if any(re.search(p, loc_text) for p in DFW_AND_REMOTE_PATTERNS) else 0
        ind_text = f"{' '.join(company.industry_verticals)} {job.description}".lower()
        ind_pts = 5 if any(re.search(p, ind_text) for p in INDUSTRY_PATTERNS) else 0
        domain_loc_pts = loc_pts + ind_pts

        # Penalties (-35 pts for hard disqualifiers)
        penalties = 0
        penalty_reasons: list[str] = []
        if re.search(
            r"(?:active|current|mandatory)\s+(?:top secret|ts/sci)\s+clearance", body_low
        ) or ("ts/sci" in body_low and "waiver" not in body_low):
            penalties -= 35
            penalty_reasons.append("Mandatory active Top Secret/SCI clearance required")

        # Statutory CPA sign-off requirement without CFE/CIA equivalency
        has_strict_cpa = bool(
            re.search(
                r"(?:active\s+cpa\s+license\s+required|must\s+hold\s+an?\s+active\s+cpa|sign\s+audit\s+opinions)",
                body_low,
            )
        )
        accepts_equivalency = any(
            tok in body_low for tok in ("cfe", "cia", "or equivalent", "or related certification")
        )
        if has_strict_cpa and not accepts_equivalency:
            penalties -= 35
            penalty_reasons.append("Strict statutory active CPA license requirement without CFE equivalency")

        total = max(-100, min(100, comp_pts + cert_pts + tech_cred_pts + domain_loc_pts + penalties))
        rejection_reason = None
        if total < self.settings.min_queue_score:
            rejection_reason = (
                "; ".join(penalty_reasons)
                if penalty_reasons
                else f"Composite score ({total}) below {self.settings.min_queue_score} threshold"
            )

        return ScoreBreakdown(
            compensation_and_level_pts=comp_pts,
            certification_and_sb_pts=cert_pts,
            tech_stack_and_credentials_pts=tech_cred_pts,
            domain_and_location_pts=domain_loc_pts,
            penalties_pts=penalties,
            total_score=total,
            auto_rejected=False,
            rejection_reason=rejection_reason,
            matched_tools=matched_tools,
            matched_certifications=matched_creds[:3],
            rationale=(
                f"Comp/Level={comp_pts}/30, Cert/SB={cert_pts}/25, "
                f"Tech/Cred={tech_cred_pts}/30 (tools={matched_tools}), "
                f"Domain/Loc={domain_loc_pts}/15, Penalties={penalties}"
            ),
        )

    def run(self, max_daily_queue: int | None = None) -> dict[str, Any]:
        """Scrapes company portals + runs grounded Boolean search, scores roles, and populates queue."""
        queue_cap = max_daily_queue or self.settings.daily_queue_limit
        companies = self.db.list_companies()
        company_map = {c.company_id: c for c in companies}

        raw_jobs: list[tuple[JobRecord, CompanyRecord]] = []

        # 1. Scrape active company portals directly
        for company in companies:
            if not company.careers_url:
                continue
            scraped = self._scrape_company_portal(company)
            for job in scraped:
                raw_jobs.append((job, company))
            self.db.mark_company_scraped(
                company.company_id,
                outreach_candidate=(
                    company.outreach_candidate if scraped else company.outreach_candidate
                ),
            )

        # 2. Grounded Boolean Aggregator & Portal Deep Search via Gemini Google Search
        if self.llm.available:
            grounded_jobs = self._run_boolean_aggregator_search(companies)
            for job, comp in grounded_jobs:
                company_map[comp.company_id] = comp
                raw_jobs.append((job, comp))

        evaluated = 0
        queued_auto = 0
        queued_hitl = 0
        filtered_out = 0

        for job, comp in raw_jobs:
            job.archetype = classify_employer_archetype(comp)  # type: ignore[assignment]
            breakdown = self.score_job(job, comp)
            job.score = breakdown.total_score
            job.score_breakdown = breakdown

            if job.score >= self.settings.auto_apply_min_score:
                job.status = "QUEUED_AUTO"
                queued_auto += 1
            elif job.score >= self.settings.min_queue_score:
                job.status = "QUEUED_HITL"
                queued_hitl += 1
            else:
                job.status = "FILTERED_OUT"
                filtered_out += 1

            self.db.upsert_job(job)
            evaluated += 1

        top_queue = self.db.get_queued_jobs(
            limit=queue_cap, min_score=self.settings.min_queue_score
        )
        return {
            "evaluated_count": evaluated,
            "queued_auto_count": queued_auto,
            "queued_hitl_count": queued_hitl,
            "filtered_out_count": filtered_out,
            "daily_queue": [j.model_dump() for j in top_queue],
        }

    def _scrape_company_portal(self, company: CompanyRecord) -> list[JobRecord]:
        """Scrapes Greenhouse API, Lever API, JSON-LD JobPosting, or HTML job links."""
        if not company.careers_url:
            return []
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            )
        }
        jobs: list[JobRecord] = []
        try:
            with httpx.Client(timeout=10.0, follow_redirects=True, headers=headers) as client:
                # Greenhouse public JSON API check
                if "greenhouse.io" in company.careers_url:
                    board_token = urlparse(company.careers_url).path.strip("/").split("/")[0]
                    if board_token:
                        api_resp = client.get(
                            f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true"
                        )
                        if api_resp.status_code == 200:
                            for item in api_resp.json().get("jobs", []):
                                title = item.get("title", "")
                                url = item.get("absolute_url", company.careers_url)
                                loc = (item.get("location") or {}).get("name", company.location_hq)
                                desc = BeautifulSoup(item.get("content", ""), "html.parser").get_text(" ", strip=True)
                                jobs.append(
                                    JobRecord(
                                        id=make_job_id(company.company_id, title, url),
                                        company_id=company.company_id,
                                        company_name=company.company_name,
                                        title=title,
                                        url=url,
                                        location=loc,
                                        description=desc[:6000],
                                    )
                                )
                            return jobs

                # Lever public JSON API check
                if "lever.co" in company.careers_url:
                    site = urlparse(company.careers_url).path.strip("/").split("/")[0]
                    if site:
                        api_resp = client.get(f"https://api.lever.co/v0/postings/{site}?mode=json")
                        if api_resp.status_code == 200 and isinstance(api_resp.json(), list):
                            for item in api_resp.json():
                                title = item.get("text", "")
                                url = item.get("hostedUrl", company.careers_url)
                                loc = (item.get("categories") or {}).get("location", company.location_hq)
                                desc = item.get("descriptionPlain", "")
                                jobs.append(
                                    JobRecord(
                                        id=make_job_id(company.company_id, title, url),
                                        company_id=company.company_id,
                                        company_name=company.company_name,
                                        title=title,
                                        url=url,
                                        location=loc,
                                        description=desc[:6000],
                                    )
                                )
                            return jobs

                resp = client.get(company.careers_url)
                if resp.status_code >= 400:
                    return []
                soup = BeautifulSoup(resp.text, "html.parser")

                # 1. Check JSON-LD JobPosting structured data
                for script in soup.find_all("script", type="application/ld+json"):
                    try:
                        ld_data = json.loads(script.string or "{}")
                        items = ld_data if isinstance(ld_data, list) else [ld_data]
                        for entry in items:
                            if isinstance(entry, dict) and entry.get("@type") == "JobPosting":
                                title = entry.get("title", "")
                                url = entry.get("url") or str(resp.url)
                                desc = BeautifulSoup(
                                    entry.get("description", ""), "html.parser"
                                ).get_text(" ", strip=True)
                                if title:
                                    jobs.append(
                                        JobRecord(
                                            id=make_job_id(company.company_id, title, url),
                                            company_id=company.company_id,
                                            company_name=company.company_name,
                                            title=title,
                                            url=url,
                                            location=company.location_hq,
                                            description=desc[:6000],
                                        )
                                    )
                    except (json.JSONDecodeError, TypeError):
                        continue

                # 2. Extract job-like links on career portals
                role_keywords = (
                    "audit",
                    "risk",
                    "compliance",
                    "consultant",
                    "manager",
                    "supervisor",
                    "analyst",
                    "director",
                    "advisory",
                    "controls",
                    "accounting",
                    "grc",
                )
                seen_urls: set[str] = set()
                for a_tag in soup.find_all("a", href=True):
                    link_text = a_tag.get_text(" ", strip=True)
                    if not (6 <= len(link_text) <= 110):
                        continue
                    low = link_text.lower()
                    if any(
                        nav in low
                        for nav in ("read more", "learn more", "privacy", "cookie", "login", "sign in", "about us", "contact")
                    ):
                        continue
                    if any(k in low for k in role_keywords):
                        full_url = urljoin(str(resp.url), a_tag["href"])
                        if full_url in seen_urls:
                            continue
                        seen_urls.add(full_url)
                        jobs.append(
                            JobRecord(
                                id=make_job_id(company.company_id, link_text, full_url),
                                company_id=company.company_id,
                                company_name=company.company_name,
                                title=link_text,
                                url=full_url,
                                location=company.location_hq,
                                description=f"{link_text} role at {company.company_name} ({', '.join(company.industry_verticals)}).",
                            )
                        )
        except httpx.HTTPError as exc:
            logger.debug("Portal scrape HTTP warning for %s: %s", company.company_name, exc)
        return jobs[:15]

    def _run_boolean_aggregator_search(
        self, companies: list[CompanyRecord]
    ) -> list[tuple[JobRecord, CompanyRecord]]:
        """Executes grounded Boolean queries for live DFW & Remote requisitions at target/certified firms."""
        target_summary = "\n".join(
            f"- {c.company_name} ({', '.join(c.certifications)}) | {c.careers_url or c.website_url}"
            for c in companies[:16]
        )
        prompt = f"""
Use Google Search to find 12–15 active or representative job requisitions in Dallas–Fort Worth (DFW), TX or Remote (US)
across our target small/diverse-owned employers & DFW recruiting partners below (including McConnell & Jones LLP,
ALM First Financial Advisors, Proven Recruiting, High Profile Staffing, Élan Partners, Ascension Search Partners,
Delta Dallas, McBride Consulting LLC, AttainX Inc., or other SBA 8(a) / SDVOSB / WOSB / Black-Owned / Texas HUB firms):

{target_summary}

Execute Boolean search equivalents for:
1. ("Internal Audit Manager" OR "Audit Supervisor" OR "Operational Risk Manager" OR "Risk Advisory Manager" OR "GRC Manager" OR "Compliance & Risk Manager" OR "Senior Operational Risk" OR "Lead Risk Specialist") AND ("Dallas" OR "Plano" OR "Fort Worth" OR "DFW" OR "Remote")
2. Also include 2–3 lower-level or mismatched roles if found on those portals (e.g., "Financial Analyst, ALM Strategy Group" or "Staff Auditor" or sub-$105K roles) so our quantitative guardrail scorer can evaluate and filter them accurately!

Return ONLY a JSON array with this exact structure:
[
  {{
    "company_name": "Exact Company Name",
    "company_certifications": ["Black-Owned", "MBE", "HUB"],
    "entity_type": "direct_employer | recruiting_agency",
    "website_url": "https://...",
    "careers_url": "https://...",
    "title": "Exact Job Title",
    "url": "https://... direct job or career portal URL",
    "location": "Dallas, TX (Hybrid) or Remote (US)",
    "salary_min": 115000,
    "salary_max": 140000,
    "description": "Detailed summary of responsibilities, required tools (e.g. AuditBoard, Alteryx, Power BI, Excel, RCSA, SOX, internal controls, fraud risk), and qualifications (e.g. BS Accounting, CFE/CIA/CPA or equivalent). Use null for salary_min/salary_max if unlisted."
  }}
]
"""
        results: list[tuple[JobRecord, CompanyRecord]] = []
        try:
            items = self.llm.generate_grounded_json(
                prompt=prompt,
                system_instruction=f"{self.skill_instructions}\n\nCANDIDATE PERSONA:\n{self.persona_text}",
                temperature=0.2,
            )
            if isinstance(items, dict):
                items = items.get("jobs", [])
            for item in items or []:
                if not isinstance(item, dict) or not item.get("title") or not item.get("company_name"):
                    continue
                comp_name = item["company_name"].strip()
                comp = self.db.find_existing_company(
                    comp_name,
                    item.get("website_url", ""),
                    item.get("careers_url", ""),
                )
                if comp is None:
                    slug = re.sub(r"[^a-z0-9]+", "-", comp_name.lower()).strip("-")[:24]
                    comp = CompanyRecord(
                        company_id=f"disc-{slug}",
                        company_name=comp_name,
                        website_url=item.get("website_url", ""),
                        careers_url=item.get("careers_url") or item.get("url", ""),
                        ats_type="custom",
                        location_hq=item.get("location", "Dallas, TX"),
                        certifications=list(item.get("company_certifications") or ["WOSB"]),
                        industry_verticals=["Risk Advisory", "Financial Services", "Internal Audit"],
                        entity_type=(
                            "recruiting_agency"
                            if item.get("entity_type") == "recruiting_agency"
                            else "direct_employer"
                        ),
                    )
                    self.db.upsert_company(comp)

                url = (item.get("url") or comp.careers_url or comp.website_url or "").strip()
                s_min = item.get("salary_min")
                s_max = item.get("salary_max")
                job = JobRecord(
                    id=make_job_id(comp.company_id, item["title"], url),
                    company_id=comp.company_id,
                    company_name=comp.company_name,
                    title=item["title"].strip(),
                    url=url,
                    location=item.get("location") or comp.location_hq,
                    description=item.get("description") or "",
                    salary_min=int(s_min) if isinstance(s_min, (int, float)) else None,
                    salary_max=int(s_max) if isinstance(s_max, (int, float)) else None,
                )
                results.append((job, comp))
        except Exception as exc:
            logger.warning("Grounded job aggregator query warning: %s", exc)
        return results

