# Skill: Dynamic Resume Tailor (`resume-tailor`)

## 1. Objective
Optimize the candidate's baseline resume (`persona.md` / `master_resume.json`) for a specific target job description (JD) to maximize ATS keyword alignment and human reviewer impact **without fabricating facts, altering dates, or inventing credentials**.

## 2. Input Parameters
* **`master_resume`**: The canonical, structured candidate resume from `persona.md`.
* **`job_posting`**: Full text, title, employer type (e.g., Federal Contractor vs. Advisory Firm vs. Recruiting Agency), and extracted keywords from the target requisition.
* **`verifier_feedback`** *(Optional)*: Rejection diff notes from `truth-verifier` if a previous tailoring attempt failed validation.

## 3. Mutable vs. Immutable Fields
To prevent hallucinations, this skill is strictly constrained by field mutability rules:

### IMMUTABLE (Never Alter)
* Candidate Name, Contact Information, Location
* Employer Names, Job Titles, Employment Dates, and Locations
* University Name, Degree Title, Graduation Date
* Active Certifications and Issuance Dates
* Hard Numerical Metrics (e.g., exact dollar amounts, percentages, assessment counts, record counts, hour reductions)

### MUTABLE (Authorized for Optimization)
1. **Executive Summary (3–4 lines):**
   * Dynamically frame the opening title and industry emphasis to mirror the target JD (e.g., emphasizing specific industry verticals, advisory readiness, or LCAT alignment).
   * Front-load the specific tools and certifications requested in the JD that exist in `master_resume`.
2. **Experience Bullet Ordering & Framing:**
   * Reorder bullet points within each role so the most relevant achievements appear first.
   * Adjust verb framing and terminology to align with the JD's taxonomy (e.g., emphasizing "internal controls and SOX/regulatory readiness" vs. "operational risk framework and root-cause remediation") while preserving the underlying factual event and exact metric.
3. **Core Competencies / Skills Table:**
   * Reorder and select genuine skills from the candidate's approved skill bank in `persona.md` to match the exact phrasing in the JD.
4. **Tailored Cover Note / Outreach Blurb:**
   * Generate a concise 150-word cover note or recruiter pitch aligned with the employer's category (e.g., 8(a)/SDVOSB LCAT positioning vs. Boutique Advisory infrastructure building).

## 4. Output Format
Return a structured JSON object containing:
1. `tailored_resume_markdown`: Complete markdown representation ready for `.docx`/`.pdf` compilation.
2. `tailored_cover_note`: Short pitch for ATS text fields or recruiter emails.
3. `modification_log`: Explicit list of changes made relative to `master_resume` for inspection by `truth-verifier`.

