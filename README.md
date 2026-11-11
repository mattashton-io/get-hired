# get-hired — Autonomous Job Discovery & Zero-Hallucination Application Agent

An automated, scheduled multi-agent system built for **Eryka Smith, CFE** (Dallas–Fort Worth & US Remote) that discovers senior risk, internal audit, and GRC roles at certified small and diverse businesses (**SBA 8(a), SDVOSB, Black-Owned/MBE, WOSB/WBENC, Texas HUB**), dynamically tailors resumes under a strict zero-hallucination verification gatekeeper, applies to up to ~10 high-fit roles/day (`$110,000–$150,000` target band), and coordinates Human-in-the-Loop (HITL) approvals and a 6:00 PM CST Daily Digest via Gmail.

---

## Architecture & Modular Agents

| Phase | Component | Source | Responsibility |
| :--- | :--- | :--- | :--- |
| **Phase 1** | State & Canonical Truth | `persona.md`, `data/master_resume.json`, `src/state/db.py` | Immutable/mutable field schema, 14-company DFW seed registry (`data/target_companies.json`), and SQLite ledger (`data/applications_ledger.db`). |
| **Phase 2.1** | `CompanySourcerAgent` | `src/agents/company_sourcer.py` (`skills/company_sourcer.md`) | Queries SBA DSBS, Texas HUB/CMBL, WBENC, and NMSDC across NAICS `541211`, `541611`, `541618`, `522/524`, and `541512`; resolves ATS portals and flags `outreach_candidate = true` when no public job board exists. |
| **Phase 2.2** | `JobMatcherScorerAgent` | `src/agents/job_matcher_scorer.py` (`skills/job_matcher_scorer.md`) | Scrapes portals + runs grounded Boolean queries; enforces `$105,000` hard floor, `$110,000–$150,000` target band, Manager/Supervisor/Lead/SME title approval, and `-100` auto-reject on Analyst I/II roles. |
| **Phase 3.1** | `ResumeTailorAgent` | `src/agents/resume_tailor.py` (`skills/resume_tailor.md`) | Modifies **only** Executive Summary, bullet ordering/verb framing, approved Skills selection, and archetype-specific Cover Note. |
| **Phase 3.2** | `TruthVerifierAgent` | `src/agents/truth_verifier.py` (`skills/truth_verifier.md`) | `temperature = 0.0` gatekeeper combining programmatic string-equality & regex metric checks (`30+`, `12+`, `$28M`, `94%`, `60%`, `40 to 16`, `25+`, `12 controls`, `10,000+`, `90%+`) + LLM keyword-stuffing detection (up to 2 retries; fallback to canonical master resume on 3rd failure). |
| **Phase 3.3** | `DocumentCompiler` | `src/compiler/document_compiler.py` | Compiles verified resumes into ATS-clean `.docx` and `.pdf` files named `Eryka_Smith_Resume_{Company}_{Role}.{docx,pdf}` (or `Eryka_Smith_Resume_{Company}.{docx,pdf}`) with no job prefixes or numerical hashes. |
| **Phase 4** | `HITLGmailAgent` | `src/agents/hitl_gmail_comms.py` (`skills/hitl_gmail_comms.md`) | Sends `[ACTION REQUIRED] Approve Application` emails for `75–84` scores or custom essays/salary prompts, polls Gmail threads every 15m (`APPROVE`, `SKIP`, `---BEGIN EDIT---`), and sends the 6:00 PM CST HTML Daily Digest. |
| **Phase 5** | `ATSApplicatorAgent` | `src/agents/ats_applicator.py` (`skills/ats_applicator.md`) | Asserts `verifier_passed == True`, populates Greenhouse / Lever / Workday / iCIMS / custom forms, uploads verified `.pdf`, respects CAPTCHAs (pausing for HITL without bypassing), and captures confirmation screenshots. |
| **Phase 6** | Scheduler Daemon | `src/orchestrator/scheduler.py` | `APScheduler` daemon running on `America/Chicago` (`06:00` sourcing/scoring, `08:00–16:00` staggered queue with `25–55m` jitter, `*/15m` Gmail poller, `18:00` Daily Digest). |

---

## Quickstart & CLI Usage

### 1. Activate Virtual Environment
```bash
source .venv/bin/activate
```

### 2. Configure Gmail OAuth2 (Optional for Live Email Delivery)
Place a Google Cloud Desktop OAuth 2.0 Client Secret JSON at `config/gmail_credentials.json` (or set `GMAIL_CREDENTIALS_PATH` in `.env`), then authorize `Erykasmith@gmail.com` (`gmail.send` & `gmail.readonly` scopes):
```bash
python -m src.cli auth-gmail
```
*(In `DRY_RUN=true` or prior to OAuth authorization, all rendered HTML & RFC-2822 `.eml` emails with resume attachments are saved directly to `data/outbox/`.)*

### 3. Unified Operator Script (`./run.sh`)
```bash
# Start background scheduler daemon
./run.sh start

# Check running status, PID, and ledger telemetry
./run.sh status

# Follow live logs
./run.sh logs

# Stop background daemon
./run.sh stop

# Run one-off dry-run cycle or send Daily Digest now
./run.sh dry-run
./run.sh digest

# Install native macOS LaunchAgent (starts on Mac boot, $0 cloud cost)
./run.sh install-service
```

### 4. Cloud Run Scale-to-Zero Deployment (GCP)
For 24/7 autonomous operation without leaving your laptop awake or paying for continuous idle cloud servers:
```bash
# Automated one-step deployment: builds container, mounts Cloud Storage for SQLite, and sets CST cron triggers
./deploy_gcp.sh
```
*Architecture:* **Cloud Run Jobs** + **Cloud Scheduler** + **Cloud Storage Volume Mount (GCS FUSE)**. Instances scale to absolute 0 between scheduled runs. Total execution time across all daily tasks is ~10 minutes/day, costing **$0.00/month** (100% within GCP Free Tier).

### 5. Direct CLI Commands (Advanced)
```bash
# Initialize SQLite ledger & seed the 14 DFW/Remote target companies
python -m src.cli init-db

# Execute End-to-End Dry Run (final ATS submit click disabled + synthetic hallucination check)
DRY_RUN=true python -m src.cli dry-run

# Individual pipeline stages
python -m src.cli source
python -m src.cli score
python -m src.cli process-queue --max-items 2
python -m src.cli poll-hitl
python -m src.cli digest

# Inspect configured America/Chicago cron schedules or launch daemon
python -m src.cli verify-schedules
python -m src.cli daemon
```

### 4. Run Test Suite
```bash
pytest -v
```
