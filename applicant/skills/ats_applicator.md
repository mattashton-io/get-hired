# Skill: Automated ATS Applicator (`ats-applicator`)

## 1. Objective
Autonomously navigate employer career portals and Applicant Tracking Systems (Workday, Greenhouse, Lever, iCIMS, BambooHR, and custom small-business forms), populate candidate information from `persona.md`, upload the verified tailored resume, and capture proof of submission while respecting daily pacing and anti-bot guardrails.

## 2. Input Parameters
* **`verified_application_package`**: Output from `truth-verifier` (must contain `"verified": true`, compiled `.pdf`/`.docx` resume path, and `tailored_cover_note`).
* **`candidate_pii_and_defaults`**: Loaded from `persona.md` (contact info, work authorization, EEO/voluntary self-identification preferences, salary expectations, notice period).
* **`hitl_override_payload`** *(Optional)*: Candidate-approved custom answers returned from `hitl-gmail-comms`.

## 3. Execution & Pacing Guardrails
1. **Daily Volume Cap:** Execute a maximum of `10` autonomous submissions per 24-hour cycle (unless explicitly overridden by a user `APPROVE` command on a queued HITL item).
2. **Staggered Jitter:** Space application submissions across business hours (`08:00–16:00` local time) with randomized intervals (`25–55 minutes` between submissions) to prevent IP/session rate-limiting on shared ATS providers.
3. **Verification Check:** Refuse to execute if `verified_application_package.verified !== true`.

## 4. Form Filling Logic
1. **Standard Fields:** Populate Legal Name, Email, Phone, City/State, LinkedIn URL, Education, and Work History directly from `persona.md`.
2. **Document Uploads:** Upload the compiled, verified tailored resume file to the Resume/CV dropzone and attach `tailored_cover_note` where a Cover Letter field is present.
3. **Compliance & Screening Questions:**
   * Answer standard factual screening questions (e.g., years of experience in specific domains, active certifications held, willingness to work hybrid/remote, US work authorization, sponsorship requirements) using `persona.md`.
   * If a mandatory field asks for desired compensation:
     * Enter the midpoint of the role's published range if it falls within `persona.compensation_guardrails`, OR enter `persona.compensation_guardrails.default_anchor` if unlisted.
     * If the form forces a selection below `persona.compensation_guardrails.hard_floor`, abort and log as rejected.
4. **Unexpected Question Interception:**
   * If an unrecognized mandatory free-text essay question or complex assessment is encountered, immediately pause submission, serialize the browser state/URL, and hand off to `hitl-gmail-comms`.

## 5. Post-Submission Logging
Upon clicking final submit:
1. Capture a full-page confirmation screenshot and extract any confirmation receipt email/ID.
2. Update `application_state_db` with `status = "SUBMITTED"`, timestamp, resume version hash, and screenshot path for inclusion in the evening digest.