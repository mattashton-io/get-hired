"""Phase 3.3: Document Compiler (`Eryka_Smith_Resume_{Company}_{JobID}.{docx|pdf}`)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    HRFlowable,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from src.config import Settings
from src.models import TailoredResumeOutput


def sanitize_filename_token(value: str, max_len: int = 40) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value.strip()).strip("_")
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned[:max_len].strip("_") or "Company"


def _add_bottom_border(paragraph: Any, color_hex: str = "2B4C7E") -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    p_bdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "2")
    bottom.set(qn("w:color"), color_hex)
    p_bdr.append(bottom)
    p_pr.append(p_bdr)


class DocumentCompiler:
    """Compiles verified resume output into clean ATS-ready `.docx` and `.pdf` artifacts."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.master_resume = settings.load_master_resume()

    def compile(
        self,
        tailored: TailoredResumeOutput,
        company_name: str,
        role_title: str = "",
        job_id: str = "",
    ) -> dict[str, str]:
        comp_token = sanitize_filename_token(company_name, max_len=40)
        # Strip any trailing numbers or underscores from company token
        comp_token = re.sub(r"[-_#0-9]+$", "", comp_token).strip("_") or "Company"

        candidate_role = role_title
        if (
            not candidate_role
            and job_id
            and not job_id.lower().startswith("job")
            and not any(c.isdigit() for c in job_id)
        ):
            candidate_role = job_id

        role_token = ""
        if candidate_role:
            cleaned_role = re.sub(r"[-_#0-9]+$", "", candidate_role).strip()
            raw_role_token = sanitize_filename_token(cleaned_role, max_len=40)
            role_parts = [
                p
                for p in raw_role_token.split("_")
                if p.lower() != "job" and not p.isdigit()
            ]
            role_token = "_".join(role_parts)

        if role_token:
            stem = f"Eryka_Smith_Resume_{comp_token}_{role_token}"
        else:
            stem = f"Eryka_Smith_Resume_{comp_token}"

        self.settings.compiled_resumes_dir.mkdir(parents=True, exist_ok=True)
        docx_path = self.settings.compiled_resumes_dir / f"{stem}.docx"
        pdf_path = self.settings.compiled_resumes_dir / f"{stem}.pdf"

        self._compile_docx(tailored, docx_path)
        self._compile_pdf(tailored, pdf_path)

        digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()[:16]
        return {
            "docx_path": str(docx_path),
            "pdf_path": str(pdf_path),
            "resume_hash": digest,
        }

    def _compile_docx(self, tailored: TailoredResumeOutput, output_path: Path) -> None:
        doc = Document()
        for section in doc.sections:
            section.top_margin = Inches(0.5)
            section.bottom_margin = Inches(0.5)
            section.left_margin = Inches(0.6)
            section.right_margin = Inches(0.6)

        normal_style = doc.styles["Normal"]
        normal_style.font.name = "Calibri"
        normal_style.font.size = Pt(10)
        normal_style.font.color.rgb = RGBColor(0x1F, 0x29, 0x37)

        ident = self.master_resume["candidate_identity"]
        edu = self.master_resume["education"]["entries"][0]
        cert = self.master_resume["certifications"]["active"][0]
        right_tab_pos = Inches(7.3)

        # Header: Name & Contact
        p_name = doc.add_paragraph()
        p_name.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p_name.paragraph_format.space_after = Pt(2)
        r_name = p_name.add_run(ident["full_name"]["value"])
        r_name.bold = True
        r_name.font.size = Pt(18)
        r_name.font.color.rgb = RGBColor(0x11, 0x2A, 0x46)

        p_contact = doc.add_paragraph()
        p_contact.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p_contact.paragraph_format.space_after = Pt(8)
        contact_line = (
            f"{ident['location']['value']}  •  {ident['email']['value']}  •  "
            f"{ident['phone']['value']}  •  {ident['linkedin_url']['value']}"
        )
        r_contact = p_contact.add_run(contact_line)
        r_contact.font.size = Pt(9.5)
        r_contact.font.color.rgb = RGBColor(0x4B, 0x55, 0x63)

        def add_section_heading(title_text: str) -> None:
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after = Pt(3)
            run = p.add_run(title_text.upper())
            run.bold = True
            run.font.size = Pt(10.5)
            run.font.color.rgb = RGBColor(0x1E, 0x3A, 0x5F)
            _add_bottom_border(p, "1E3A5F")

        # Summary
        add_section_heading("Summary")
        p_sum = doc.add_paragraph(tailored.executive_summary)
        p_sum.paragraph_format.space_after = Pt(4)
        p_sum.paragraph_format.line_spacing = 1.12

        # Experience
        add_section_heading("Experience")
        for role in tailored.roles:
            p_comp = doc.add_paragraph()
            p_comp.paragraph_format.space_before = Pt(4)
            p_comp.paragraph_format.space_after = Pt(1)
            p_comp.paragraph_format.tab_stops.add_tab_stop(
                right_tab_pos, WD_TAB_ALIGNMENT.RIGHT
            )
            r_c = p_comp.add_run(role["company"])
            r_c.bold = True
            r_c.font.size = Pt(10.5)
            r_loc = p_comp.add_run(f"\t{role['location']}")
            r_loc.font.size = Pt(9.5)
            r_loc.font.color.rgb = RGBColor(0x4B, 0x55, 0x63)

            p_title = doc.add_paragraph()
            p_title.paragraph_format.space_after = Pt(2)
            p_title.paragraph_format.tab_stops.add_tab_stop(
                right_tab_pos, WD_TAB_ALIGNMENT.RIGHT
            )
            r_t = p_title.add_run(role["title"])
            r_t.italic = True
            r_t.font.size = Pt(10)
            r_d = p_title.add_run(f"\t{role['dates']}")
            r_d.italic = True
            r_d.font.size = Pt(9.5)
            r_d.font.color.rgb = RGBColor(0x4B, 0x55, 0x63)

            for bullet in role["bullets"]:
                pb = doc.add_paragraph(style="List Bullet")
                pb.paragraph_format.space_after = Pt(1.5)
                pb.paragraph_format.line_spacing = 1.08
                rb = pb.add_run(bullet.strip().lstrip("•-* ").strip())
                rb.font.size = Pt(9.5)

        # Education
        add_section_heading("Education")
        p_edu1 = doc.add_paragraph()
        p_edu1.paragraph_format.space_after = Pt(1)
        p_edu1.paragraph_format.tab_stops.add_tab_stop(right_tab_pos, WD_TAB_ALIGNMENT.RIGHT)
        r_inst = p_edu1.add_run(edu["institution"]["value"])
        r_inst.bold = True
        p_edu1.add_run(f"\t{edu['location']['value']}")

        p_edu2 = doc.add_paragraph()
        p_edu2.paragraph_format.space_after = Pt(3)
        p_edu2.paragraph_format.tab_stops.add_tab_stop(right_tab_pos, WD_TAB_ALIGNMENT.RIGHT)
        p_edu2.add_run(edu["degree"]["value"]).italic = True
        p_edu2.add_run(f"\t{edu['graduation_date']['value']}")

        # Certifications
        add_section_heading("Certifications")
        p_cert = doc.add_paragraph()
        p_cert.paragraph_format.space_after = Pt(4)
        p_cert.paragraph_format.tab_stops.add_tab_stop(right_tab_pos, WD_TAB_ALIGNMENT.RIGHT)
        r_cert = p_cert.add_run(f"{cert['name']['value']}, {cert['issuer']['value']}")
        r_cert.bold = True
        p_cert.add_run(f"\t{cert['issue_date']['value']}")

        # Skills 3-column table
        add_section_heading("Skills")
        skills = list(tailored.selected_skills)
        cols = 3
        rows_count = (len(skills) + cols - 1) // cols
        table = doc.add_table(rows=rows_count, cols=cols)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for idx, skill_name in enumerate(skills):
            cell = table.cell(idx // cols, idx % cols)
            cp = cell.paragraphs[0]
            cp.paragraph_format.space_after = Pt(1)
            cr = cp.add_run(f"• {skill_name}")
            cr.font.size = Pt(9)

        doc.save(str(output_path))

    def _compile_pdf(self, tailored: TailoredResumeOutput, output_path: Path) -> None:
        doc = SimpleDocTemplate(
            str(output_path),
            pagesize=letter,
            leftMargin=0.55 * inch,
            rightMargin=0.55 * inch,
            topMargin=0.45 * inch,
            bottomMargin=0.45 * inch,
            title="Eryka Smith Resume",
            author="Eryka Smith",
        )
        styles = getSampleStyleSheet()
        navy = colors.HexColor("#112A46")
        slate = colors.HexColor("#4B5563")
        body_dark = colors.HexColor("#1F2937")

        name_style = ParagraphStyle(
            "NameHeader",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=19,
            alignment=1,
            textColor=navy,
            spaceAfter=2,
        )
        contact_style = ParagraphStyle(
            "ContactSub",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8.8,
            leading=11,
            alignment=1,
            textColor=slate,
            spaceAfter=6,
        )
        sec_style = ParagraphStyle(
            "SectionHdr",
            parent=styles["Normal"],
            fontName="Helvetica-Bold",
            fontSize=9.8,
            leading=12,
            textColor=navy,
            spaceBefore=5,
            spaceAfter=2,
        )
        body_style = ParagraphStyle(
            "BodyTxt",
            parent=styles["Normal"],
            fontName="Helvetica",
            fontSize=8.9,
            leading=11.5,
            textColor=body_dark,
        )
        bullet_style = ParagraphStyle(
            "BulletTxt",
            parent=body_style,
            fontSize=8.7,
            leading=11.0,
        )
        right_style = ParagraphStyle(
            "RightMeta",
            parent=body_style,
            alignment=2,
            textColor=slate,
            fontSize=8.7,
        )

        ident = self.master_resume["candidate_identity"]
        edu = self.master_resume["education"]["entries"][0]
        cert = self.master_resume["certifications"]["active"][0]
        usable_width = letter[0] - 1.1 * inch

        story: list[Any] = [
            Paragraph(escape(ident["full_name"]["value"]), name_style),
            Paragraph(
                escape(
                    f"{ident['location']['value']}  |  {ident['email']['value']}  |  "
                    f"{ident['phone']['value']}  |  {ident['linkedin_url']['value']}"
                ),
                contact_style,
            ),
        ]

        def add_section(label: str) -> None:
            story.append(Paragraph(escape(label.upper()), sec_style))
            story.append(
                HRFlowable(
                    width="100%",
                    thickness=0.75,
                    color=navy,
                    spaceBefore=0,
                    spaceAfter=3,
                )
            )

        def add_split_row(left_markup: str, right_markup: str) -> None:
            tbl = Table(
                [[Paragraph(left_markup, body_style), Paragraph(right_markup, right_style)]],
                colWidths=[usable_width * 0.68, usable_width * 0.32],
            )
            tbl.setStyle(
                TableStyle(
                    [
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ("TOPPADDING", (0, 0), (-1, -1), 0.5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 0.5),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story.append(tbl)

        # Summary
        add_section("Summary")
        story.append(Paragraph(escape(tailored.executive_summary), body_style))
        story.append(Spacer(1, 3))

        # Experience
        add_section("Experience")
        for role in tailored.roles:
            add_split_row(
                f"<b>{escape(role['company'])}</b>",
                escape(role["location"]),
            )
            add_split_row(
                f"<i>{escape(role['title'])}</i>",
                f"<i>{escape(role['dates'])}</i>",
            )
            items = [
                ListItem(
                    Paragraph(escape(b.strip().lstrip("•-* ").strip()), bullet_style),
                    leftIndent=10,
                )
                for b in role["bullets"]
            ]
            story.append(
                ListFlowable(
                    items,
                    bulletType="bullet",
                    start="bulletchar",
                    bulletFontSize=7,
                    leftIndent=8,
                    spaceBefore=1,
                    spaceAfter=3,
                )
            )

        # Education
        add_section("Education")
        add_split_row(
            f"<b>{escape(edu['institution']['value'])}</b>",
            escape(edu["location"]["value"]),
        )
        add_split_row(
            f"<i>{escape(edu['degree']['value'])}</i>",
            escape(edu["graduation_date"]["value"]),
        )
        story.append(Spacer(1, 2))

        # Certifications
        add_section("Certifications")
        add_split_row(
            f"<b>{escape(cert['name']['value'])}, {escape(cert['issuer']['value'])}</b>",
            escape(cert["issue_date"]["value"]),
        )
        story.append(Spacer(1, 2))

        # Skills
        add_section("Skills")
        skills = list(tailored.selected_skills)
        cols = 3
        padded = skills + [""] * ((cols - len(skills) % cols) % cols)
        grid = []
        for r_idx in range(0, len(padded), cols):
            row_cells = [
                Paragraph(f"• {escape(s)}" if s else "", bullet_style)
                for s in padded[r_idx : r_idx + cols]
            ]
            grid.append(row_cells)
        if grid:
            skills_tbl = Table(grid, colWidths=[usable_width / 3.0] * 3)
            skills_tbl.setStyle(
                TableStyle(
                    [
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                        ("TOPPADDING", (0, 0), (-1, -1), 1),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story.append(skills_tbl)

        doc.build(story)

