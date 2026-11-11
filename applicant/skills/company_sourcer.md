# Skill: Dynamic Company Sourcer (`company-sourcer`)

## 1. Objective
Continuously discover, verify, and append small, diverse, and certified employers and recruiting agencies to the dynamic company database (`target_companies.json`). This skill ensures the job discovery pipeline never stalls on a static seed list by autonomously expanding the pool of eligible organizations matching the active persona's geographic, industry, and socio-economic certification parameters.

## 2. Input Parameters
* **`persona_context`**: Loaded from `persona.md` (specifically: `target_locations`, `target_certifications`, `target_industries`, `naics_codes`, and `seed_companies`).
* **`target_companies.json`**: The existing registry of discovered companies, career page URLs, certification statuses, and last-scraped timestamps.

## 3. Core Capabilities & Discovery Channels
1. **Federal & State Directory Querying:**
   * **SBA Dynamic Small Business Search (DSBS) & SAM.gov:** Query active entities filtered by `target_locations`, `target_certifications` (e.g., 8(a), SDVOSB, WOSB, EDWOSB), and relevant `naics_codes` (e.g., `541211` CPA Offices, `541611` Management Consulting, `522/524` Finance & Insurance, `541512` Systems Design).
   * **State & Local Directories:** Query state-level Historically Underutilized Business (HUB) / Minority & Women-Owned Business Enterprise (MWBE) directories (e.g., Texas CMBL/HUB, NMSDC, WBENC regional chapters).
2. **Specialized Recruiting Partner Discovery:**
   * Identify boutique and diverse-owned staffing/recruiting firms operating in `target_locations` that place senior corporate governance, risk, audit, compliance, and finance talent.
3. **Career Portal Resolution:**
   * For every discovered entity, resolve the direct URL to their active job board or ATS portal (e.g., Greenhouse, Lever, Workday, iCIMS, Paylocity, BambooHR, or custom `/careers` page).

## 4. Execution Rules
1. **Deduplication:** Before appending a company to `target_companies.json`, verify the domain and normalized legal name do not already exist in the registry.
2. **Size & Certification Validation:** Confirm the business holds at least one active certification or diversity designation specified in `persona.md`, or is an approved recruiting partner representing roles in the target market.
3. **Cadence & Quota:** Run daily during the pre-sourcing batch window. Ensure at least `N` active, unscraped or stale (>7 days since last scan) company portals are queued for the `job-matcher-scorer` skill.
4. **Cold Outreach Flagging:** If a high-alignment small business (matching industry + certification + location) has no public job board or zero active listings, tag the record with `"outreach_candidate": true` and extract key leadership/recruiting contact metadata for the daily digest.

## 5. Output Schema (`target_companies.json` Entry)
```json
{
  "company_id": "uuid-v4",
  "company_name": "String",
  "website_url": "https://...",
  "careers_url": "https://...",
  "ats_type": "workday | greenhouse | lever | icims | custom | none",
  "location_hq": "City, State",
  "certifications": ["8(a)", "SDVOSB", "WOSB", "MBE", "Black-Owned", "HUB"],
  "industry_verticals": ["Financial Services", "Healthcare", "Federal Consulting"],
  "entity_type": "direct_employer | recruiting_agency",
  "outreach_candidate": false,
  "last_checked_at": "ISO-8601 Timestamp"
}
```