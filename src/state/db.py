"""SQLite ledger (`applications_ledger.db`) and `target_companies.json` state store."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from src.models import CompanyRecord, JobRecord, ScoreBreakdown


def normalize_company_name(name: str) -> str:
    cleaned = re.sub(
        r"\b(llc|llp|pllc|inc|corp|corporation|co|ltd|group|partners|advisors|solutions|consulting)\b",
        "",
        name.lower(),
    )
    return re.sub(r"[^a-z0-9]", "", cleaned)


def extract_domain(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.netloc or "").lower()
    return host[4:] if host.startswith("www.") else host


class ApplicationsLedgerDB:
    def __init__(self, db_path: Path, target_companies_path: Path) -> None:
        self.db_path = db_path
        self.target_companies_path = target_companies_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS companies (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    website_url TEXT DEFAULT '',
                    careers_url TEXT DEFAULT '',
                    ats_type TEXT DEFAULT 'custom',
                    location_hq TEXT DEFAULT 'Dallas, TX',
                    certifications TEXT DEFAULT '[]',
                    industry_verticals TEXT DEFAULT '[]',
                    entity_type TEXT DEFAULT 'direct_employer',
                    outreach_candidate INTEGER DEFAULT 0,
                    contact_metadata TEXT DEFAULT '{}',
                    last_scraped_at TEXT
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    company_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    location TEXT DEFAULT 'Dallas, TX',
                    description TEXT DEFAULT '',
                    salary_min INTEGER,
                    salary_max INTEGER,
                    score INTEGER DEFAULT 0,
                    score_breakdown TEXT DEFAULT '{}',
                    archetype TEXT DEFAULT 'advisory_cpa_firm',
                    status TEXT DEFAULT 'DISCOVERED',
                    discovered_at TEXT NOT NULL,
                    FOREIGN KEY (company_id) REFERENCES companies(id)
                );

                CREATE TABLE IF NOT EXISTS applications (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    resume_pdf_path TEXT DEFAULT '',
                    resume_docx_path TEXT DEFAULT '',
                    cover_note TEXT DEFAULT '',
                    tailored_markdown TEXT DEFAULT '',
                    verifier_passed INTEGER DEFAULT 0,
                    verifier_attempts INTEGER DEFAULT 0,
                    verifier_report TEXT DEFAULT '{}',
                    hitl_thread_id TEXT,
                    hitl_reason TEXT DEFAULT '',
                    hitl_status TEXT DEFAULT 'NONE',
                    session_state_json TEXT DEFAULT '{}',
                    submitted_at TEXT,
                    screenshot_path TEXT DEFAULT '',
                    confirmation_id TEXT DEFAULT '',
                    resume_hash TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (job_id) REFERENCES jobs(id)
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_score_status ON jobs(status, score DESC);
                CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url);
                CREATE INDEX IF NOT EXISTS idx_apps_hitl ON applications(hitl_status, hitl_thread_id);
                """
            )

    def seed_from_target_companies_json(self) -> int:
        if not self.target_companies_path.exists():
            return 0
        raw_items = json.loads(self.target_companies_path.read_text(encoding="utf-8"))
        inserted = 0
        for item in raw_items:
            record = CompanyRecord.model_validate(item)
            if self.upsert_company(record, sync_json=False):
                inserted += 1
        self.sync_companies_to_json()
        return inserted

    def list_companies(self, outreach_only: bool = False) -> list[CompanyRecord]:
        query = "SELECT * FROM companies"
        if outreach_only:
            query += " WHERE outreach_candidate = 1"
        query += " ORDER BY name ASC"
        with self.connect() as conn:
            rows = conn.execute(query).fetchall()
        return [self._row_to_company(r) for r in rows]

    def get_company(self, company_id: str) -> CompanyRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM companies WHERE id = ?", (company_id,)
            ).fetchone()
        return self._row_to_company(row) if row else None

    def find_existing_company(
        self, company_name: str, website_url: str = "", careers_url: str = ""
    ) -> CompanyRecord | None:
        norm_target = normalize_company_name(company_name)
        target_domain = extract_domain(website_url) or extract_domain(careers_url)
        for existing in self.list_companies():
            if norm_target and normalize_company_name(existing.company_name) == norm_target:
                return existing
            existing_domain = extract_domain(existing.website_url) or extract_domain(
                existing.careers_url
            )
            if (
                target_domain
                and existing_domain
                and target_domain == existing_domain
                and target_domain
                not in {
                    "greenhouse.io",
                    "boards.greenhouse.io",
                    "jobs.lever.co",
                    "lever.co",
                    "myworkdayjobs.com",
                    "icims.com",
                }
            ):
                return existing
        return None

    def upsert_company(self, record: CompanyRecord, sync_json: bool = True) -> bool:
        """Inserts or updates a company. Returns True if newly inserted, False if updated."""
        existing_by_id = self.get_company(record.company_id)
        existing_dup = existing_by_id or self.find_existing_company(
            record.company_name, record.website_url, record.careers_url
        )
        is_new = existing_dup is None
        cid = existing_dup.company_id if existing_dup else (record.company_id or str(uuid.uuid4()))

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO companies (
                    id, name, website_url, careers_url, ats_type, location_hq,
                    certifications, industry_verticals, entity_type,
                    outreach_candidate, contact_metadata, last_scraped_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    website_url = CASE WHEN excluded.website_url != '' THEN excluded.website_url ELSE companies.website_url END,
                    careers_url = CASE WHEN excluded.careers_url != '' THEN excluded.careers_url ELSE companies.careers_url END,
                    ats_type = CASE WHEN excluded.ats_type != 'none' THEN excluded.ats_type ELSE companies.ats_type END,
                    location_hq = excluded.location_hq,
                    certifications = excluded.certifications,
                    industry_verticals = excluded.industry_verticals,
                    entity_type = excluded.entity_type,
                    outreach_candidate = excluded.outreach_candidate,
                    contact_metadata = excluded.contact_metadata,
                    last_scraped_at = COALESCE(excluded.last_scraped_at, companies.last_scraped_at)
                """,
                (
                    cid,
                    record.company_name,
                    record.website_url,
                    record.careers_url,
                    record.ats_type,
                    record.location_hq,
                    json.dumps(record.certifications),
                    json.dumps(record.industry_verticals),
                    record.entity_type,
                    1 if record.outreach_candidate else 0,
                    json.dumps(record.contact_metadata),
                    record.last_checked_at,
                ),
            )
        if sync_json:
            self.sync_companies_to_json()
        return is_new

    def mark_company_scraped(
        self, company_id: str, outreach_candidate: bool | None = None
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            if outreach_candidate is None:
                conn.execute(
                    "UPDATE companies SET last_scraped_at = ? WHERE id = ?",
                    (now_iso, company_id),
                )
            else:
                conn.execute(
                    "UPDATE companies SET last_scraped_at = ?, outreach_candidate = ? WHERE id = ?",
                    (now_iso, 1 if outreach_candidate else 0, company_id),
                )
        self.sync_companies_to_json()

    def sync_companies_to_json(self) -> None:
        companies = self.list_companies()
        payload = [c.model_dump() for c in companies]
        self.target_companies_path.parent.mkdir(parents=True, exist_ok=True)
        self.target_companies_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def upsert_job(self, job: JobRecord) -> JobRecord:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id, status FROM jobs WHERE (company_id = ? AND lower(title) = lower(?)) OR (url = ? AND lower(title) = lower(?))",
                (job.company_id, job.title, job.url, job.title),
            ).fetchone()
            if existing:
                job.id = existing["id"]
                # Preserve terminal statuses so we never re-apply to an already submitted/skipped job
                if existing["status"] in {
                    "SUBMITTED",
                    "DRY_RUN_COMPLETE",
                    "AWAITING_HITL",
                    "APPROVED_HITL",
                    "SKIPPED_HITL",
                }:
                    job.status = existing["status"]
            conn.execute(
                """
                INSERT INTO jobs (
                    id, company_id, title, url, location, description,
                    salary_min, salary_max, score, score_breakdown,
                    archetype, status, discovered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    url = excluded.url,
                    location = excluded.location,
                    description = CASE WHEN excluded.description != '' THEN excluded.description ELSE jobs.description END,
                    salary_min = COALESCE(excluded.salary_min, jobs.salary_min),
                    salary_max = COALESCE(excluded.salary_max, jobs.salary_max),
                    score = excluded.score,
                    score_breakdown = excluded.score_breakdown,
                    archetype = excluded.archetype,
                    status = excluded.status
                """,
                (
                    job.id,
                    job.company_id,
                    job.title,
                    job.url,
                    job.location,
                    job.description,
                    job.salary_min,
                    job.salary_max,
                    job.score,
                    job.score_breakdown.model_dump_json(),
                    job.archetype,
                    job.status,
                    now_iso,
                ),
            )
        return job

    def update_job_status(self, job_id: str, status: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))

    def get_job(self, job_id: str) -> JobRecord | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT j.*, c.name AS company_name
                FROM jobs j
                JOIN companies c ON c.id = j.company_id
                WHERE j.id = ?
                """,
                (job_id,),
            ).fetchone()
        return self._row_to_job(row) if row else None

    def get_queued_jobs(self, limit: int = 12, min_score: int = 75) -> list[JobRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT j.*, c.name AS company_name
                FROM jobs j
                JOIN companies c ON c.id = j.company_id
                WHERE j.score >= ? AND j.status IN ('QUEUED_AUTO', 'QUEUED_HITL')
                ORDER BY j.score DESC, j.discovered_at DESC
                LIMIT ?
                """,
                (min_score, limit),
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def list_jobs(self) -> list[JobRecord]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT j.*, c.name AS company_name
                FROM jobs j
                JOIN companies c ON c.id = j.company_id
                ORDER BY j.score DESC
                """
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def create_or_update_application(
        self,
        job_id: str,
        resume_pdf_path: str,
        resume_docx_path: str,
        cover_note: str,
        tailored_markdown: str,
        verifier_passed: bool,
        verifier_attempts: int,
        verifier_report: dict[str, Any],
        resume_hash: str,
        application_id: str | None = None,
    ) -> str:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM applications WHERE job_id = ?", (job_id,)
            ).fetchone()
            app_id = (
                application_id
                or (existing["id"] if existing else None)
                or f"app-{uuid.uuid4().hex[:10]}"
            )
            conn.execute(
                """
                INSERT INTO applications (
                    id, job_id, resume_pdf_path, resume_docx_path, cover_note,
                    tailored_markdown, verifier_passed, verifier_attempts,
                    verifier_report, resume_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    resume_pdf_path = excluded.resume_pdf_path,
                    resume_docx_path = excluded.resume_docx_path,
                    cover_note = excluded.cover_note,
                    tailored_markdown = excluded.tailored_markdown,
                    verifier_passed = excluded.verifier_passed,
                    verifier_attempts = excluded.verifier_attempts,
                    verifier_report = excluded.verifier_report,
                    resume_hash = excluded.resume_hash
                """,
                (
                    app_id,
                    job_id,
                    resume_pdf_path,
                    resume_docx_path,
                    cover_note,
                    tailored_markdown,
                    1 if verifier_passed else 0,
                    verifier_attempts,
                    json.dumps(verifier_report),
                    resume_hash,
                    now_iso,
                ),
            )
        return app_id

    def mark_application_hitl(
        self,
        application_id: str,
        hitl_thread_id: str,
        hitl_reason: str,
        session_state: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT job_id FROM applications WHERE id = ?", (application_id,)
            ).fetchone()
            conn.execute(
                """
                UPDATE applications
                SET hitl_thread_id = ?, hitl_reason = ?, hitl_status = 'PENDING',
                    session_state_json = ?
                WHERE id = ?
                """,
                (
                    hitl_thread_id,
                    hitl_reason,
                    json.dumps(session_state or {}),
                    application_id,
                ),
            )
            if row:
                conn.execute(
                    "UPDATE jobs SET status = 'AWAITING_HITL' WHERE id = ?",
                    (row["job_id"],),
                )

    def resolve_application_hitl(
        self,
        application_id: str,
        hitl_status: str,
        updated_cover_note: str | None = None,
    ) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT job_id FROM applications WHERE id = ?", (application_id,)
            ).fetchone()
            if updated_cover_note is not None:
                conn.execute(
                    "UPDATE applications SET hitl_status = ?, cover_note = ? WHERE id = ?",
                    (hitl_status, updated_cover_note, application_id),
                )
            else:
                conn.execute(
                    "UPDATE applications SET hitl_status = ? WHERE id = ?",
                    (hitl_status, application_id),
                )
            if row:
                new_job_status = (
                    "APPROVED_HITL" if hitl_status in {"APPROVED", "EDITED"} else "SKIPPED_HITL"
                )
                conn.execute(
                    "UPDATE jobs SET status = ? WHERE id = ?",
                    (new_job_status, row["job_id"]),
                )

    def mark_application_submitted(
        self,
        application_id: str,
        screenshot_path: str,
        confirmation_id: str = "",
        dry_run: bool = False,
    ) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT job_id FROM applications WHERE id = ?", (application_id,)
            ).fetchone()
            conn.execute(
                """
                UPDATE applications
                SET submitted_at = ?, screenshot_path = ?, confirmation_id = ?,
                    hitl_status = CASE WHEN hitl_status = 'PENDING' THEN 'COMPLETED' ELSE hitl_status END
                WHERE id = ?
                """,
                (now_iso, screenshot_path, confirmation_id, application_id),
            )
            if row:
                conn.execute(
                    "UPDATE jobs SET status = ? WHERE id = ?",
                    ("DRY_RUN_COMPLETE" if dry_run else "SUBMITTED", row["job_id"]),
                )

    def get_application(self, application_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT a.*, j.title AS job_title, j.url AS job_url, j.score AS job_score,
                       j.salary_min, j.salary_max, j.status AS job_status,
                       c.name AS company_name, c.certifications AS company_certifications,
                       c.ats_type AS company_ats_type
                FROM applications a
                JOIN jobs j ON j.id = a.job_id
                JOIN companies c ON c.id = j.company_id
                WHERE a.id = ?
                """,
                (application_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_pending_hitl_applications(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.*, j.title AS job_title, j.url AS job_url, j.score AS job_score,
                       j.salary_min, j.salary_max, c.name AS company_name,
                       c.certifications AS company_certifications
                FROM applications a
                JOIN jobs j ON j.id = a.job_id
                JOIN companies c ON c.id = j.company_id
                WHERE a.hitl_status = 'PENDING' AND a.hitl_thread_id IS NOT NULL
                ORDER BY a.created_at ASC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def get_submitted_today(self, date_prefix: str | None = None) -> list[dict[str, Any]]:
        prefix = date_prefix or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.*, j.title AS job_title, j.url AS job_url, j.score AS job_score,
                       j.salary_min, j.salary_max, j.status AS job_status,
                       c.name AS company_name, c.certifications AS company_certifications
                FROM applications a
                JOIN jobs j ON j.id = a.job_id
                JOIN companies c ON c.id = j.company_id
                WHERE a.submitted_at LIKE ?
                ORDER BY a.submitted_at DESC
                """,
                (f"{prefix}%",),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_submissions_last_24h(self) -> int:
        today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return len(self.get_submitted_today(today_prefix))

    def get_pipeline_telemetry(self) -> dict[str, Any]:
        with self.connect() as conn:
            companies_total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
            companies_scraped = conn.execute(
                "SELECT COUNT(*) FROM companies WHERE last_scraped_at IS NOT NULL"
            ).fetchone()[0]
            jobs_total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            jobs_filtered = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'FILTERED_OUT'"
            ).fetchone()[0]
            filtered_rows = conn.execute(
                "SELECT score_breakdown FROM jobs WHERE status = 'FILTERED_OUT'"
            ).fetchall()
            apps_total = conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
            apps_verified = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE verifier_passed = 1"
            ).fetchone()[0]

        reasons: dict[str, int] = {}
        for row in filtered_rows:
            try:
                bd = json.loads(row["score_breakdown"] or "{}")
                reason = bd.get("rejection_reason") or "Score below 75 threshold"
                reasons[reason] = reasons.get(reason, 0) + 1
            except json.JSONDecodeError:
                reasons["Score below 75 threshold"] = (
                    reasons.get("Score below 75 threshold", 0) + 1
                )

        pass_rate = (apps_verified / apps_total * 100.0) if apps_total > 0 else 100.0
        return {
            "companies_total": companies_total,
            "companies_scanned": companies_scraped,
            "roles_evaluated": jobs_total,
            "roles_filtered_out": jobs_filtered,
            "top_rejection_reasons": reasons,
            "applications_created": apps_total,
            "verifier_pass_rate": round(pass_rate, 1),
        }

    @staticmethod
    def _row_to_company(row: sqlite3.Row) -> CompanyRecord:
        return CompanyRecord(
            company_id=row["id"],
            company_name=row["name"],
            website_url=row["website_url"] or "",
            careers_url=row["careers_url"] or "",
            ats_type=row["ats_type"] or "custom",
            location_hq=row["location_hq"] or "Dallas, TX",
            certifications=json.loads(row["certifications"] or "[]"),
            industry_verticals=json.loads(row["industry_verticals"] or "[]"),
            entity_type=row["entity_type"] or "direct_employer",
            outreach_candidate=bool(row["outreach_candidate"]),
            contact_metadata=json.loads(row["contact_metadata"] or "{}"),
            last_checked_at=row["last_scraped_at"],
        )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        bd_dict = json.loads(row["score_breakdown"] or "{}")
        return JobRecord(
            id=row["id"],
            company_id=row["company_id"],
            company_name=row["company_name"] if "company_name" in row.keys() else "",
            title=row["title"],
            url=row["url"],
            location=row["location"] or "Dallas, TX",
            description=row["description"] or "",
            salary_min=row["salary_min"],
            salary_max=row["salary_max"],
            score=row["score"] or 0,
            score_breakdown=ScoreBreakdown.model_validate(bd_dict)
            if bd_dict
            else ScoreBreakdown(),
            archetype=row["archetype"] or "advisory_cpa_firm",
            status=row["status"] or "DISCOVERED",
        )

