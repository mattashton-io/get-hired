# Skill: Job Matcher & Scorer (`job-matcher-scorer`)

## 1. Objective
Scrape open requisitions from `target_companies.json` and public job aggregators, evaluate each role against the candidate's `persona.md` profile, enforce strict compensation and seniority guardrails, and output a ranked daily queue of the top opportunities (`score >= 75`).

## 2. Input Parameters
* **`persona_context`**: Loaded from `persona.md` (specifically: `compensation_guardrails`, `approved_titles`, `rejected_titles`, `core_tools`, `certifications`, `domain_experience`, and `hard_disqualifiers`).
* **`target_companies.json`**: Active employer and recruiting agency career URLs.

## 3. Search & Scraping Directives
1. **Direct Portal Scraping:** Crawl `careers_url` for all active companies in `target_companies.json`.
2. **Boolean Aggregator Queries:** Execute parameterized Boolean queries across job boards combining:
   * `persona.domain_keywords` AND `persona.seniority_keywords` AND `persona.core_tools` AND `persona.target_locations`.
   * `persona.certification_keywords` (e.g., 8a, SDVOSB, WOSB, HUB, MBE) AND `persona.functional_keywords`.

## 4. Compensation & LCAT Guardrails
1. **Published Salary Evaluation:**
   * **Hard Floor Check:** Automatically reject (`score = -100`) any requisition where the published maximum base salary falls below `persona.compensation_guardrails.hard_floor`.
   * **Target Band Check:** Award maximum salary points if the published range overlaps `persona.compensation_guardrails.target_min` to `persona.compensation_guardrails.target_max`.
2. **Unlisted Salary / Federal LCAT Inference:**
   * When salary is unlisted, infer compensation band using title seniority and Labor Category (LCAT) mapping from `persona.md`.
   * **Approve:** Manager, Supervisor, Lead, Principal, Senior Consultant (Manager Track), or SME LCATs.
   * **Reject:** Entry/Mid-level Analyst I/II or Junior Staff roles that benchmark below the hard floor.

## 5. Quantitative Scoring Rubric (`0` to `100` Scale)
* **Dimension 1: Compensation & Level Alignment (Max 30 pts)**
  * Published range overlaps target band: **+30 pts**
  * Unlisted salary, but title matches `approved_titles` (Manager/Supervisor/Lead/SME tier): **+25 pts**
  * Published range maxes out between `hard_floor` and `target_min`: **+10 pts**
  * Published range maxes out below `hard_floor` OR title matches `rejected_titles`: **-100 pts (Auto-Reject)**
* **Dimension 2: Employer Certification & Small Business Fit (Max 25 pts)**
  * Direct employer holds verified target certification (8a, SDVOSB, WOSB, Black-Owned, MBE, HUB): **+25 pts**
  * Role represented by a verified diverse-owned recruiting agency in target market: **+20 pts**
* **Dimension 3: Technical Stack & Credential Match (Max 30 pts)**
  * Mentions core technical tools from `persona.core_tools`: **+5 pts per tool (Max 15 pts)**
  * Mentions active candidate certifications or degree field from `persona.credentials`: **+10 pts**
  * Core responsibilities align with candidate's primary functional pillars: **+5 pts**
* **Dimension 4: Domain & Location Fit (Max 15 pts)**
  * Matches target metro (hybrid/onsite) or Full Remote eligible: **+10 pts**
  * Matches candidate's prior industry verticals: **+5 pts**
* **Penalties:**
  * Triggers any `hard_disqualifiers` (e.g., non-waiverable Top Secret/SCI clearance or mandatory license the candidate does not hold without equivalency): **-35 pts**

## 6. Routing & Output Queue
* **`score >= 85` (Tier 1 - Auto-Execute Eligible):** Route to `resume-tailor` $\rightarrow$ `truth-verifier` $\rightarrow$ `ats-applicator` (up to daily cap of ~10 applications).
* **`75 <= score < 85` (Tier 2 - HITL Review Eligible):** Route to `resume-tailor` $\rightarrow$ `truth-verifier` $\rightarrow$ `hitl-gmail-comms` for human approval if needed to fill the daily quota or if high strategic value.
* **`score < 75`:** Log and discard.