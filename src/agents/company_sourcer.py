"""Phase 2.1: Dynamic Company Sourcer Agent (`CompanySourcerAgent`)."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from src.config import Settings
from src.llm.gemini_client import GeminiClient
from src.models import CompanyRecord
from src.state.db import ApplicationsLedgerDB

logger = logging.getLogger(__name__)

APPROVED_CERT_KEYWORDS = {
    "8(a)",
    "8a",
    "sdvosb",
    "vosb",
    "wosb",
    "edwosb",
    "wbenc",
    "black-owned",
    "black owned",
    "african american",
    "mbe",
    "nmsdc",
    "hub",
    "texas hub",
    "minority-owned",
    "woman-owned",
    "women-owned",
}


def detect_ats_type(url: str, html: str = "") -> str:
    combined = f"{url} {html[:8000]}".lower()
    if not url:
        return "none"
    if "greenhouse.io" in combined or "grnh.se" in combined:
        return "greenhouse"
    if "lever.co" in combined:
        return "lever"
    if "myworkdayjobs.com" in combined or "workday" in combined:
        return "workday"
    if "icims.com" in combined:
        return "icims"
    if "bamboohr.com" in combined:
        return "bamboohr"
    if "paylocity.com" in combined:
        return "paylocity"
    return "custom"


class CompanySourcerAgent:
    """Discovers and validates small, 8(a), SDVOSB, Black-owned, WOSB, and HUB employers in DFW & Remote."""

    def __init__(
        self,
        settings: Settings,
        db: ApplicationsLedgerDB,
        llm: GeminiClient,
    ) -> None:
        self.settings = settings
        self.db = db
        self.llm = llm
        self.skill_instructions = settings.load_skill("company_sourcer.md")
        self.persona_text = settings.load_persona_text()

    def run(self, min_active_portals: int = 10, discover_new_count: int = 5) -> dict[str, Any]:
        """Seeds initial registry if needed, resolves career portals, and discovers new certified firms."""
        seeded = self.db.seed_from_target_companies_json()
        existing_companies = self.db.list_companies()

        discovered_records: list[CompanyRecord] = []
        if self.llm.available and discover_new_count > 0:
            discovered_records = self._discover_via_grounded_search(
                existing_names=[c.company_name for c in existing_companies],
                target_count=discover_new_count,
            )

        newly_added = 0
        outreach_flagged = 0
        for rec in discovered_records:
            if not self._has_valid_certification(rec):
                continue
            resolved = self._resolve_career_portal(rec)
            if resolved.outreach_candidate:
                outreach_flagged += 1
            if self.db.upsert_company(resolved, sync_json=False):
                newly_added += 1

        self.db.sync_companies_to_json()

        stale_or_unscraped = self.get_companies_due_for_scan()
        return {
            "seeded_count": seeded,
            "newly_discovered": newly_added,
            "outreach_flagged_new": outreach_flagged,
            "total_companies": len(self.db.list_companies()),
            "queued_portals_for_scan": len(stale_or_unscraped),
            "meets_portal_quota": len(stale_or_unscraped) >= min(min_active_portals, len(self.db.list_companies())),
        }

    def get_companies_due_for_scan(self, stale_days: int = 7) -> list[CompanyRecord]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
        due: list[CompanyRecord] = []
        for comp in self.db.list_companies():
            if not comp.careers_url:
                continue
            if not comp.last_checked_at:
                due.append(comp)
                continue
            try:
                checked_dt = datetime.fromisoformat(comp.last_checked_at)
                if checked_dt.tzinfo is None:
                    checked_dt = checked_dt.replace(tzinfo=timezone.utc)
                if checked_dt < cutoff:
                    due.append(comp)
            except ValueError:
                due.append(comp)
        return due

    def _has_valid_certification(self, record: CompanyRecord) -> bool:
        if record.entity_type == "recruiting_agency" and record.certifications:
            return True
        for cert in record.certifications:
            c_low = cert.lower().strip()
            if any(k in c_low for k in APPROVED_CERT_KEYWORDS):
                return True
        return False

    def _discover_via_grounded_search(
        self, existing_names: list[str], target_count: int = 5
    ) -> list[CompanyRecord]:
        prompt = f"""
Using Google Search across SBA DSBS (8(a), SDVOSB, WOSB, EDWOSB), Texas CMBL / Texas HUB,
DFW WBENC (Women's Business Council - Southwest), and DFW NMSDC directories, identify {target_count} REAL,
active small/diverse-owned employers or boutique recruiting agencies in Dallas-Fort Worth (DFW), Texas or US-Remote
operating in NAICS 541211 (CPA / Internal Audit / Risk Advisory), 541611 / 541618 (Management & Risk Consulting),
522 / 524 (Financial Services / Insurance), or 541512 (GRC / AuditBoard / Data Analytics Consulting).

Exclude these already-registered companies:
{", ".join(existing_names)}

Return a JSON array of objects matching this exact schema:
[
  {{
    "company_name": "Legal Name",
    "website_url": "https://...",
    "careers_url": "https://... (or empty string if no public job board)",
    "ats_type": "workday | greenhouse | lever | icims | custom | none",
    "location_hq": "City, TX or Remote (US)",
    "certifications": ["8(a)", "SDVOSB", "WOSB", "WBENC", "MBE", "Black-Owned", "HUB"],
    "industry_verticals": ["Risk Advisory", "Internal Audit", "Financial Services", "Federal Consulting"],
    "entity_type": "direct_employer",
    "outreach_candidate": false,
    "contact_metadata": {{
      "target_practice": "Relevant practice area or leadership contact title",
      "suggested_title_pitch": "Internal Audit Manager / Operational Risk Manager / Risk SME"
    }}
  }}
]
"""
        try:
            raw_list = self.llm.generate_grounded_json(
                prompt=prompt,
                system_instruction=f"{self.skill_instructions}\n\nCANDIDATE PERSONA:\n{self.persona_text}",
                temperature=0.2,
            )
            if isinstance(raw_list, dict):
                raw_list = raw_list.get("companies", [])
            records: list[CompanyRecord] = []
            for item in raw_list or []:
                if not isinstance(item, dict) or not item.get("company_name"):
                    continue
                ats = str(item.get("ats_type", "custom")).lower()
                if ats not in {
                    "workday",
                    "greenhouse",
                    "lever",
                    "icims",
                    "bamboohr",
                    "paylocity",
                    "custom",
                    "none",
                }:
                    ats = "custom" if item.get("careers_url") else "none"
                entity_type = (
                    "recruiting_agency"
                    if item.get("entity_type") == "recruiting_agency"
                    else "direct_employer"
                )
                slug = re.sub(r"[^a-z0-9]+", "-", item["company_name"].lower()).strip("-")
                records.append(
                    CompanyRecord(
                        company_id=f"disc-{slug[:28]}-{uuid.uuid4().hex[:4]}",
                        company_name=item["company_name"].strip(),
                        website_url=item.get("website_url", "").strip(),
                        careers_url=item.get("careers_url", "").strip(),
                        ats_type=ats,  # type: ignore[arg-type]
                        location_hq=item.get("location_hq", "Dallas, TX"),
                        certifications=list(item.get("certifications") or []),
                        industry_verticals=list(item.get("industry_verticals") or []),
                        entity_type=entity_type,  # type: ignore[arg-type]
                        outreach_candidate=bool(
                            item.get("outreach_candidate") or not item.get("careers_url")
                        ),
                        contact_metadata=dict(item.get("contact_metadata") or {}),
                    )
                )
            return records
        except Exception as exc:
            logger.warning("Grounded company discovery warning: %s", exc)
            return []

    def _resolve_career_portal(self, record: CompanyRecord) -> CompanyRecord:
        """Verifies website/career URL via HTTP and discovers `/careers` or ATS links if unlisted."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            )
        }
        with httpx.Client(timeout=8.0, follow_redirects=True, headers=headers) as client:
            if record.careers_url:
                try:
                    resp = client.get(record.careers_url)
                    if resp.status_code < 400:
                        final_url = str(resp.url)
                        record.careers_url = final_url
                        record.ats_type = detect_ats_type(final_url, resp.text)  # type: ignore[assignment]
                        record.outreach_candidate = False
                        return record
                except httpx.HTTPError:
                    pass

            if record.website_url:
                try:
                    home = client.get(record.website_url)
                    if home.status_code < 400:
                        soup = BeautifulSoup(home.text, "html.parser")
                        for a_tag in soup.find_all("a", href=True):
                            label = (a_tag.get_text(" ", strip=True) or "").lower()
                            href = a_tag["href"]
                            if any(
                                k in label or k in href.lower()
                                for k in (
                                    "career",
                                    "jobs",
                                    "join-us",
                                    "opportunities",
                                    "greenhouse.io",
                                    "lever.co",
                                    "myworkdayjobs.com",
                                    "icims.com",
                                )
                            ):
                                resolved_url = urljoin(str(home.url), href)
                                record.careers_url = resolved_url
                                record.ats_type = detect_ats_type(resolved_url, home.text)  # type: ignore[assignment]
                                record.outreach_candidate = False
                                return record
                except httpx.HTTPError:
                    pass

        # High-alignment firm with no active career portal -> flag as cold outreach candidate
        record.careers_url = ""
        record.ats_type = "none"
        record.outreach_candidate = True
        if not record.contact_metadata:
            record.contact_metadata = {
                "target_practice": "Risk Advisory, Internal Audit & Compliance Leadership",
                "suggested_title_pitch": "Internal Audit Manager / Operational Risk Manager",
            }
        return record

