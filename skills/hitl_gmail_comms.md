# Skill: HITL & Daily Digest Gmail Communicator (`hitl-gmail-comms`)

## 1. Objective
Manage all Human-in-the-Loop (HITL) email escalations, asynchronous approval polling, and the automated End-of-Day (EOD) Daily Digest sent to the candidate's Gmail address defined in `persona.md`.

## 2. Input Parameters
* **`candidate_email`**: Loaded from `persona.md`.
* **`application_state_db`**: Real-time log of queued, submitted, paused (HITL), and outreach-flagged opportunities.
* **`gmail_api_client`**: Authenticated OAuth2 Gmail service capable of sending HTML emails and polling thread replies.

## 3. Workflow A: Real-Time HITL Approval Requests (Yellow-Light Trigger)
Triggered during the day when an application meets one or more HITL conditions:
* The ATS requires a custom open-ended essay question not covered by standard profile FAQs.
* The requisition is a high-priority "Reach" role or requires entering a mandatory hard salary figure when no range was published.
* Match score is between `75–84` and requires candidate confirmation before submission.

### Execution Steps:
1. Pause the `ats-applicator` session state and store form payload in `application_state_db`.
2. Send an email to `candidate_email` with subject:
   `[ACTION REQUIRED] Approve Application: {Job_Title} @ {Company_Name} ({Estimated_Salary}) [Ref: {Job_ID}]`
3. Include in the email body:
   * Direct link to the job posting and company certification badges.
   * Why HITL was triggered (e.g., drafted essay answer or proposed salary expectation).
   * Attached verified tailored resume (`.docx` / `.pdf`).
   * Clear reply instructions: *"Reply **APPROVE** to submit as-is, **SKIP** to discard, or reply with revised text between `---BEGIN EDIT---` and `---END EDIT---`."*
4. **Polling Listener:** Poll the Gmail thread every 15 minutes. Upon receiving `APPROVE` (or edited text), unlock the paused job and trigger `ats-applicator`.

## 4. Workflow B: Automated End-of-Day (EOD) Digest
Triggered daily via cron at the scheduled evening window (e.g., `18:00 CST`).

### Digest Structure:
Send a formatted HTML summary email with subject:
`Daily Job Agent Digest – {Date}: {Submitted_Count} Applied | {HITL_Count} Awaiting Review`

1. **Section 1: Submitted Today (Target ~10/day)**
   * Table listing: Company Name, Diversity/Small Business Certifications, Role Title, Published/Estimated Salary Range, Match Score, Confirmation ID/Screenshot link, and link to the exact tailored resume file submitted.
2. **Section 2: Pending Your Approval (Action Needed)**
   * Summary of any unresolved HITL threads from earlier in the day with direct links to reply.
3. **Section 3: High-Value Direct Outreach Targets (1–2 Daily)**
   * Certified small businesses discovered by `company-sourcer` that match the candidate's profile but have no public job postings, complete with a pre-drafted, verified cold outreach message for LinkedIn or email.
4. **Section 4: Pipeline Telemetry**
   * Total companies scanned, roles evaluated, roles filtered out (with top rejection reasons, e.g., salary `< hard_floor`), and verifier pass rate.

