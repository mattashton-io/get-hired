# Skill: Sequential Truth Verifier (`truth-verifier`)

## 1. Objective
Act as an uncompromising, zero-temperature (`temperature = 0.0`) compliance gatekeeper between `resume-tailor` and any outbound action (`ats-applicator` or `hitl-gmail-comms`). Verify that the tailored resume and cover note contain **zero hallucinated credentials, inflated metrics, altered dates, or unapproved claims**.

## 2. Input Parameters
* **`master_resume`**: The canonical truth source from `persona.md`.
* **`tailored_output`**: The generated resume and cover note from `resume-tailor`.
* **`job_posting`**: The target job description (used to detect "keyword-stuffing hallucinations" where the tailor copies a required skill from the JD that the candidate does not possess).

## 3. Verification Checklist (Strict Pass/Fail)
Every check below must evaluate to `PASS` before an application can proceed:

1. **Credential & License Integrity Check:**
   * Does `tailored_output` claim any certification, license, or security clearance present in `job_posting` but absent from `master_resume`? *(If yes $\rightarrow$ `FAIL`)*
2. **Chronology & Identity Lock Check:**
   * Do all employer names, job titles, employment start/end dates, education degrees, and graduation dates match `master_resume` character-for-character? *(If no $\rightarrow$ `FAIL`)*
3. **Quantitative Metric Invariance Check:**
   * Are all numbers, percentages, dollar figures, and volume counts in `tailored_output` grounded in `master_resume`? No rounding up, combining unrelated metrics, or inventing new KPIs is permitted. *(If violated $\rightarrow$ `FAIL`)*
4. **Tool & Technology Hallucination Check:**
   * Did the tailor insert software tools, programming languages, or ERP/GRC platforms from the JD that are not listed in the candidate's approved profile in `persona.md`? *(If yes $\rightarrow$ `FAIL`)*
5. **Voice & Formatting Sanity Check:**
   * Is the document free of placeholder tokens (e.g., `[Insert Company]`, `TBD`), markdown syntax errors, or awkward keyword stuffing? *(If no $\rightarrow$ `FAIL`)*

## 4. Loop & Escalation Protocol
* **On `PASS`:** Sign the payload with `"verified": true` and route to the confidence/policy router (`ats-applicator` or `hitl-gmail-comms`).
* **On `FAIL` (Attempt 1 or 2):** Return `"verified": false` along with a specific `violation_report` back to `resume-tailor` for immediate regeneration.
* **On `FAIL` (Attempt 3):** Abort auto-tailoring, fall back to the unmodified `master_resume`, and route the job to `hitl-gmail-comms` to alert the user.