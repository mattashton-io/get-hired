"""Phase 4.2 & 4.3: HITL Approval Escalation, 15-Min Thread Poller & EOD Daily Digest (`HITLGmailAgent`)."""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from src.config import Settings
from src.integrations.gmail_client import GmailClient
from src.models import CompanyRecord, HITLReplyCommand, JobRecord
from src.state.db import ApplicationsLedgerDB

logger = logging.getLogger(__name__)


def format_salary_display(
    salary_min: int | None, salary_max: int | None, default_anchor: int = 125000
) -> str:
    if salary_min and salary_max:
        if salary_min == salary_max:
            return f"${salary_min:,}"
        return f"${salary_min:,}–${salary_max:,}"
    if salary_max:
        return f"Up to ${salary_max:,}"
    return f"Est. ~${default_anchor:,} (Target Band)"


def render_cert_badges_html(certs: list[str]) -> str:
    if not certs:
        return '<span style="background:#e5e7eb;color:#374151;padding:2px 8px;border-radius:12px;font-size:11px;">Small Business</span>'
    spans = []
    for c in certs:
        spans.append(
            f'<span style="display:inline-block;background:#dbeafe;color:#1e40af;'
            f'padding:2px 8px;margin-right:4px;border-radius:12px;font-size:11px;font-weight:600;">'
            f"{html.escape(c)}</span>"
        )
    return "".join(spans)


def parse_hitl_reply_text(
    reply_text: str, thread_id: str, job_id: str, application_id: str
) -> HITLReplyCommand | None:
    """Extracts `APPROVE`, `SKIP`, or `---BEGIN EDIT--- ... ---END EDIT---` from a Gmail thread reply."""
    if not reply_text or not reply_text.strip():
        return None

    edit_match = re.search(
        r"---BEGIN EDIT---\s*(.*?)\s*---END EDIT---",
        reply_text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if edit_match:
        return HITLReplyCommand(
            thread_id=thread_id,
            job_id=job_id,
            application_id=application_id,
            action="EDIT",
            edited_text=edit_match.group(1).strip(),
            raw_reply=reply_text,
        )

    # Check first non-quoted lines so quoted original instructions don't false-trigger
    unquoted_lines = [
        ln.strip()
        for ln in reply_text.splitlines()
        if ln.strip() and not ln.strip().startswith(">")
    ]
    head_text = " ".join(unquoted_lines[:4]).upper()

    if re.search(r"\bSKIP\b", head_text):
        return HITLReplyCommand(
            thread_id=thread_id,
            job_id=job_id,
            application_id=application_id,
            action="SKIP",
            raw_reply=reply_text,
        )
    if re.search(r"\bAPPROVE\b", head_text):
        return HITLReplyCommand(
            thread_id=thread_id,
            job_id=job_id,
            application_id=application_id,
            action="APPROVE",
            raw_reply=reply_text,
        )
    return None


class HITLGmailAgent:
    """Manages HITL approval emails, 15-minute Gmail reply polling, and the 6:00 PM CST Daily Digest."""

    def __init__(
        self,
        settings: Settings,
        db: ApplicationsLedgerDB,
        gmail: GmailClient,
    ) -> None:
        self.settings = settings
        self.db = db
        self.gmail = gmail
        self.skill_instructions = settings.load_skill("hitl_gmail_comms.md")

    def evaluate_hitl_trigger(
        self,
        job: JobRecord,
        has_open_essay: bool = False,
        essay_prompt: str = "",
        requires_salary_confirm: bool = False,
        proposed_salary: int | None = None,
        used_master_fallback: bool = False,
    ) -> tuple[bool, str]:
        reasons: list[str] = []
        if self.settings.min_queue_score <= job.score < self.settings.auto_apply_min_score:
            reasons.append(
                f"Tier-2 Match Score ({job.score}/100 in 75–84 review band) requires candidate sign-off."
            )
        if has_open_essay:
            reasons.append(
                f"Open-ended ATS essay question encountered: \"{essay_prompt or 'Custom employer prompt'}\"."
            )
        if requires_salary_confirm:
            sal_val = proposed_salary or self.settings.salary_default_anchor
            reasons.append(
                f"Mandatory hard salary input on unlisted range (proposed anchor: ${sal_val:,})."
            )
        if used_master_fallback:
            reasons.append(
                "Auto-tailoring exhausted 3 TruthVerifier attempts; fell back to unmodified Master Resume."
            )
        return (len(reasons) > 0, " | ".join(reasons))

    def send_hitl_approval_email(
        self,
        application_id: str,
        job: JobRecord,
        company: CompanyRecord,
        hitl_reason: str,
        drafted_response: str,
        resume_pdf_path: str,
        resume_docx_path: str = "",
        session_state: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        est_salary = format_salary_display(
            job.salary_min, job.salary_max, self.settings.salary_default_anchor
        )
        subject = (
            f"[ACTION REQUIRED] Approve Application: {job.title} @ "
            f"{company.company_name} ({est_salary}) [Ref: {job.id}]"
        )
        badges_html = render_cert_badges_html(company.certifications)

        html_body = f"""<!DOCTYPE html>
<html>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#1f2937;line-height:1.5;max-width:720px;margin:0 auto;padding:20px;">
  <div style="border-left:4px solid #f59e0b;background:#fffbeb;padding:12px 16px;border-radius:6px;margin-bottom:18px;">
    <strong style="color:#b45309;font-size:14px;">HUMAN-IN-THE-LOOP (HITL) APPROVAL REQUIRED</strong>
    <div style="font-size:13px;color:#78350f;margin-top:4px;">{html.escape(hitl_reason)}</div>
  </div>

  <h2 style="margin:0 0 6px 0;color:#112a46;font-size:20px;">{html.escape(job.title)}</h2>
  <div style="margin-bottom:12px;">
    <strong style="font-size:15px;">{html.escape(company.company_name)}</strong>
    <span style="color:#6b7280;"> • {html.escape(job.location)}</span>
    <div style="margin-top:6px;">{badges_html}</div>
  </div>

  <table style="width:100%;border-collapse:collapse;margin-bottom:18px;font-size:13px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;">
    <tr>
      <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;"><strong>Compensation Band:</strong> {html.escape(est_salary)}</td>
      <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;"><strong>Match Score:</strong> {job.score}/100</td>
    </tr>
    <tr>
      <td style="padding:8px 12px;" colspan="2">
        <strong>Job Requisition URL:</strong>
        <a href="{html.escape(job.url)}" style="color:#2563eb;">{html.escape(job.url)}</a>
      </td>
    </tr>
  </table>

  <h3 style="font-size:14px;color:#112a46;margin-bottom:6px;">Proposed Cover Note / Screening Response (Truth-Verified)</h3>
  <div style="background:#f3f4f6;padding:12px 14px;border-radius:6px;font-size:13px;white-space:pre-wrap;margin-bottom:18px;">{html.escape(drafted_response)}</div>

  <div style="background:#eff6ff;border:1px solid #bfdbfe;padding:14px 16px;border-radius:6px;font-size:13px;">
    <strong>How to Respond (Polled Every 15 Minutes):</strong>
    <ul style="margin:8px 0 0 0;padding-left:20px;">
      <li>Reply <strong>APPROVE</strong> to submit this application with the attached verified resume.</li>
      <li>Reply <strong>SKIP</strong> to discard this opportunity.</li>
      <li>Or reply with revised cover/essay text enclosed between <code>---BEGIN EDIT---</code> and <code>---END EDIT---</code>.</li>
    </ul>
  </div>
</body>
</html>"""

        attachments = [Path(p) for p in (resume_pdf_path, resume_docx_path) if p]
        delivery = self.gmail.send_html_email(
            to_email=self.settings.candidate_email,
            subject=subject,
            html_body=html_body,
            attachments=attachments,
            tag=f"hitl_{job.id}",
        )
        self.db.mark_application_hitl(
            application_id=application_id,
            hitl_thread_id=delivery["thread_id"],
            hitl_reason=hitl_reason,
            session_state=session_state or {},
        )
        return delivery

    def poll_hitl_replies(
        self,
        on_approved_callback: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Polls active `PENDING` Gmail HITL threads and processes `APPROVE`, `SKIP`, or `EDIT` commands."""
        pending = self.db.get_pending_hitl_applications()
        processed: list[dict[str, Any]] = []

        for app_row in pending:
            thread_id = app_row["hitl_thread_id"]
            app_id = app_row["id"]
            job_id = app_row["job_id"]
            replies = self.gmail.get_thread_replies(thread_id)
            if not replies:
                continue

            # Evaluate the latest reply containing a valid command
            cmd: HITLReplyCommand | None = None
            for reply_body in reversed(replies):
                cmd = parse_hitl_reply_text(reply_body, thread_id, job_id, app_id)
                if cmd is not None:
                    break

            if cmd is None:
                continue

            if cmd.action == "SKIP":
                self.db.resolve_application_hitl(app_id, "SKIPPED")
                processed.append({"application_id": app_id, "job_id": job_id, "action": "SKIP"})
            elif cmd.action in {"APPROVE", "EDIT"}:
                new_status = "EDITED" if cmd.action == "EDIT" else "APPROVED"
                self.db.resolve_application_hitl(
                    app_id, new_status, updated_cover_note=cmd.edited_text
                )
                exec_result: dict[str, Any] = {}
                if on_approved_callback is not None:
                    refreshed = self.db.get_application(app_id)
                    if refreshed:
                        exec_result = on_approved_callback(refreshed)
                processed.append(
                    {
                        "application_id": app_id,
                        "job_id": job_id,
                        "action": cmd.action,
                        "execution": exec_result,
                    }
                )
        return processed

    def generate_outreach_blurb(self, company: CompanyRecord) -> str:
        """Generates a zero-hallucination cold outreach blurb tailored to an `outreach_candidate` firm."""
        certs_str = ", ".join(company.certifications) or "diverse-owned small business"
        suggested_role = company.contact_metadata.get(
            "suggested_title_pitch", "Internal Audit Manager / Operational Risk Manager"
        )
        certs_upper = {c.upper() for c in company.certifications}
        if {"8(A)", "SDVOSB", "VOSB", "EDWOSB"} & certs_upper:
            return (
                f"Hello {company.company_name} Leadership Team — As a Dallas-based Certified Fraud Examiner (CFE) "
                f"with a B.S. in Accounting and 8 years of risk and internal audit experience across insurance, "
                f"financial services, and healthcare, I admire your work as a certified ({certs_str}) contractor. "
                f"I qualify for {suggested_role} LCATs, bringing full-population Alteryx control automation "
                f"(10,000+ records across 12 controls), $28M operational/fraud risk event remediation leadership "
                f"(94% on-time action plan completion), and AuditBoard transition expertise. I would welcome a "
                f"brief conversation regarding upcoming task orders or advisory engagements."
            )
        return (
            f"Hello {company.company_name} Leadership Team — As a Dallas-based Certified Fraud Examiner (CFE) "
            f"with a B.S. in Accounting and 8 years of operational risk and internal audit experience at "
            f"Transamerica, Fidelity, and CVS Health, I am reaching out to connect with your ({certs_str}) "
            f"advisory team regarding {suggested_role} opportunities. I bring turnkey client-delivery "
            f"capabilities as an AuditBoard 'Maven', Power BI/Excel executive reporting automation (cutting "
            f"quarterly prep time by 60%, from 40 to 16 hours), and root-cause leadership across 50+ risk "
            f"assessments and $28M in operational/fraud risk events."
        )

    def send_daily_digest(self, date_str: str | None = None) -> dict[str, Any]:
        """Renders and sends the 4-section HTML End-of-Day Daily Digest to `Erykasmith@gmail.com`."""
        now_cst = datetime.now(ZoneInfo(self.settings.timezone))
        display_date = date_str or now_cst.strftime("%Y-%m-%d")

        submitted_rows = self.db.get_submitted_today()
        pending_hitl = self.db.get_pending_hitl_applications()
        outreach_firms = self.db.list_companies(outreach_only=True)[:2]
        telemetry = self.db.get_pipeline_telemetry()

        subject = (
            f"Daily Job Agent Digest – {display_date}: "
            f"{len(submitted_rows)} Applied | {len(pending_hitl)} Awaiting Review"
        )

        # Section 1: Submitted Today
        if submitted_rows:
            sec1_items = []
            for row in submitted_rows:
                certs = json.loads(row.get("company_certifications") or "[]")
                sal = format_salary_display(
                    row.get("salary_min"),
                    row.get("salary_max"),
                    self.settings.salary_default_anchor,
                )
                pdf_name = Path(row.get("resume_pdf_path") or "").name or "Master Resume"
                shot_name = Path(row.get("screenshot_path") or "").name or "Captured"
                mode_badge = (
                    " (Dry-Run)" if row.get("job_status") == "DRY_RUN_COMPLETE" else ""
                )
                sec1_items.append(
                    f"""<tr>
                      <td style="padding:10px;border-bottom:1px solid #e5e7eb;">
                        <strong>{html.escape(row['company_name'])}</strong><br/>
                        {render_cert_badges_html(certs)}
                      </td>
                      <td style="padding:10px;border-bottom:1px solid #e5e7eb;">
                        <a href="{html.escape(row['job_url'])}" style="color:#1d4ed8;font-weight:600;">{html.escape(row['job_title'])}</a>{mode_badge}
                      </td>
                      <td style="padding:10px;border-bottom:1px solid #e5e7eb;">{html.escape(sal)}</td>
                      <td style="padding:10px;border-bottom:1px solid #e5e7eb;font-weight:700;color:#047857;">{row['job_score']}/100</td>
                      <td style="padding:10px;border-bottom:1px solid #e5e7eb;font-size:12px;">
                        <div>Resume: <code>{html.escape(pdf_name)}</code></div>
                        <div>Proof: <code>{html.escape(shot_name)}</code></div>
                      </td>
                    </tr>"""
                )
            sec1_html = f"""
            <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:24px;">
              <thead>
                <tr style="background:#f3f4f6;text-align:left;color:#374151;">
                  <th style="padding:10px;border-bottom:2px solid #d1d5db;">Company & Certifications</th>
                  <th style="padding:10px;border-bottom:2px solid #d1d5db;">Role Title</th>
                  <th style="padding:10px;border-bottom:2px solid #d1d5db;">Salary Band</th>
                  <th style="padding:10px;border-bottom:2px solid #d1d5db;">Score</th>
                  <th style="padding:10px;border-bottom:2px solid #d1d5db;">Submitted Artifacts</th>
                </tr>
              </thead>
              <tbody>{"".join(sec1_items)}</tbody>
            </table>"""
        else:
            sec1_html = '<p style="font-size:13px;color:#6b7280;margin-bottom:24px;">No autonomous submissions finalized yet in this window.</p>'

        # Section 2: Pending HITL
        if pending_hitl:
            sec2_cards = []
            for p in pending_hitl:
                sal = format_salary_display(
                    p.get("salary_min"), p.get("salary_max"), self.settings.salary_default_anchor
                )
                sec2_cards.append(
                    f"""<div style="border:1px solid #fcd34d;background:#fffbeb;padding:12px 14px;border-radius:6px;margin-bottom:10px;font-size:13px;">
                      <strong>{html.escape(p['job_title'])}</strong> @ <strong>{html.escape(p['company_name'])}</strong>
                      ({html.escape(sal)} | Score: {p['job_score']}/100)<br/>
                      <span style="color:#92400e;">Trigger: {html.escape(p.get('hitl_reason') or 'Review requested')}</span><br/>
                      <span>Thread Ref: <code>{html.escape(p.get('hitl_thread_id') or '')}</code> — Reply <strong>APPROVE</strong> or <strong>SKIP</strong> on the action thread.</span>
                    </div>"""
                )
            sec2_html = "".join(sec2_cards)
        else:
            sec2_html = '<p style="font-size:13px;color:#6b7280;margin-bottom:24px;">Zero unresolved HITL items awaiting your reply.</p>'

        # Section 3: Cold Outreach Targets (1-2 Daily)
        outreach_cards = []
        for firm in outreach_firms:
            blurb = self.generate_outreach_blurb(firm)
            practice = firm.contact_metadata.get("target_practice", firm.location_hq)
            outreach_cards.append(
                f"""<div style="border:1px solid #e5e7eb;background:#f9fafb;padding:14px;border-radius:6px;margin-bottom:12px;font-size:13px;">
                  <div style="margin-bottom:6px;">
                    <strong style="font-size:14px;color:#112a46;">{html.escape(firm.company_name)}</strong>
                    <span style="color:#6b7280;"> ({html.escape(firm.location_hq)})</span>
                    <div style="margin-top:4px;">{render_cert_badges_html(firm.certifications)}</div>
                  </div>
                  <div style="font-size:12px;color:#4b5563;margin-bottom:8px;"><strong>Target Practice:</strong> {html.escape(str(practice))}</div>
                  <div style="background:#ffffff;border:1px solid #d1d5db;padding:10px;border-radius:4px;font-style:italic;color:#1f2937;">
                    {html.escape(blurb)}
                  </div>
                </div>"""
            )
        sec3_html = "".join(outreach_cards) or '<p style="font-size:13px;color:#6b7280;">No cold outreach candidates flagged today.</p>'

        # Section 4: Pipeline Telemetry
        rej_items = "".join(
            f"<li>{html.escape(reason)}: <strong>{cnt}</strong></li>"
            for reason, cnt in telemetry["top_rejection_reasons"].items()
        ) or "<li>None filtered out in current batch</li>"

        html_body = f"""<!DOCTYPE html>
<html>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#1f2937;line-height:1.5;max-width:860px;margin:0 auto;padding:24px;">
  <div style="background:#112a46;color:#ffffff;padding:18px 22px;border-radius:8px;margin-bottom:24px;">
    <h1 style="margin:0;font-size:20px;">Autonomous Job Discovery & Application Digest</h1>
    <div style="font-size:13px;color:#cbd5e1;margin-top:4px;">
      Candidate: Eryka Smith, CFE • Date: {html.escape(display_date)} • Target Band: $110,000–$150,000 (DFW & Remote)
    </div>
  </div>

  <h2 style="font-size:16px;color:#112a46;border-bottom:2px solid #e5e7eb;padding-bottom:6px;">1. Roles Applied Today ({len(submitted_rows)})</h2>
  {sec1_html}

  <h2 style="font-size:16px;color:#112a46;border-bottom:2px solid #e5e7eb;padding-bottom:6px;">2. Pending Your HITL Approval ({len(pending_hitl)})</h2>
  {sec2_html}

  <h2 style="font-size:16px;color:#112a46;border-bottom:2px solid #e5e7eb;padding-bottom:6px;">3. High-Value Direct Outreach Targets ({len(outreach_firms)})</h2>
  {sec3_html}

  <h2 style="font-size:16px;color:#112a46;border-bottom:2px solid #e5e7eb;padding-bottom:6px;">4. Pipeline & TruthVerifier Telemetry</h2>
  <table style="width:100%;border-collapse:collapse;font-size:13px;background:#f9fafb;border:1px solid #e5e7eb;">
    <tr>
      <td style="padding:10px;border-right:1px solid #e5e7eb;"><strong>Certified Companies Tracked:</strong> {telemetry['companies_total']} ({telemetry['companies_scanned']} portals scanned)</td>
      <td style="padding:10px;border-right:1px solid #e5e7eb;"><strong>Roles Evaluated:</strong> {telemetry['roles_evaluated']} ({telemetry['roles_filtered_out']} filtered out)</td>
      <td style="padding:10px;"><strong>TruthVerifier Pass Rate:</strong> {telemetry['verifier_pass_rate']}%</td>
    </tr>
    <tr>
      <td colspan="3" style="padding:10px;border-top:1px solid #e5e7eb;">
        <strong>Guardrail Rejection Breakdown:</strong>
        <ul style="margin:4px 0 0 0;padding-left:20px;">{rej_items}</ul>
      </td>
    </tr>
  </table>
</body>
</html>"""

        delivery = self.gmail.send_html_email(
            to_email=self.settings.candidate_email,
            subject=subject,
            html_body=html_body,
            tag=f"daily_digest_{display_date}",
        )
        return {
            "subject": subject,
            "submitted_count": len(submitted_rows),
            "pending_hitl_count": len(pending_hitl),
            "outreach_count": len(outreach_firms),
            "telemetry": telemetry,
            "delivery": delivery,
        }

