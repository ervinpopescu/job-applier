# Agent Guidelines: Job Applier

Welcome to the `job-applier` project. This file (`AGENTS.md`) is intended for AI coding assistants and agents to understand the project architecture, context, and conventions before making changes.

## Architecture & Structure

The codebase is structured as a standard Python package under `src/job_applier/`.

- **`src/job_applier/cli/`**: Contains the CLI scripts for the user.
  - `web_app.py`: Launches the unified FastAPI web dashboard server on `http://127.0.0.1:8000`.
  - `worker.py`: Autonomous queue worker for durable background job execution, lease heartbeat, and submission pacing.
  - `ops_cli.py`: Operational backups (online SQLite snapshot, age encryption), restore with downgrade protection, retention rotation, disk guard, and emergency stop.
  - `runtime_daemon.py`: Runtime daemon managing virtual display (Xvfb), VNC, websockify, and proxy services.
  - `auth_cli.py`: Platform authentication and session inspection tool.
  - `sync_cli.py`: CLI tool for portable machine-to-machine export & import backup packages.
  - `orchestrator.py`: The main pipeline script that runs the entire scraping, tailoring, resume generation, and optional auto-apply flow.
  - `apply_assistant.py`: An interactive & batch CLI tool with browser automation, 1-click autofill, and application management.
- **`src/job_applier/db.py`**: Relational SQLite database engine with WAL concurrency, atomic queries, versioned migrations (schema v1-v4), and adapter canary tracking.
- **`src/job_applier/ops/`**: Operational reliability and disaster recovery tooling.
  - `backup.py`: Quiesced online SQLite backups, profile lock probing, and age encryption (`pyrage`).
  - `disk_guard.py`: Fail-closed disk space guard verifying minimum volume headroom before writes.
  - `emergency_stop.py`: Atomic circuit breaker halting execution, revoking worker leases, and pausing queue.
- **`src/job_applier/sync.py`**: Portable self-contained backup engine packaging applications and SQLite mappings into `.zip` bundles.
- **`src/job_applier/web/`**: Full-stack web dashboard application.
  - `app.py`: FastAPI server hosting the compiled Angular frontend and REST APIs for applications, batch auto-applying, tracking, profile editing, and background scraping pipeline execution.
  - `edge_auth.py`: Zero-trust Cloudflare Access middleware with RS256 JWT validation, identity allowlists, CSRF origin verification, and gateway noVNC authorization gate.
- **`frontend/`**: Modern Angular 21 Single-Page Application (Standalone components, Signals, TypeScript, Tailwind CSS, Lucide icons).
- **`src/job_applier/automation/`**: Modules dedicated to browser automation and form autofilling.
  - `adapters/`: Versioned typed ATS adapters implementing `BaseATSAdapter` for Greenhouse, Lever, Ashby, and Generic form filling.
  - `safety_guard.py`: Central submission safety guard enforcing canary approvals, rate limiting (5/day), pacing (300s spacing), worker lease fencing, and frozen revision validation.
  - `queue.py`: Durable execution queue managing atomic state transitions, worker claims, lease heartbeats, and runtime pause/resume controls.
  - `network_security.py`: Enforceable outbound proxy with SSRF egress denial for private IPs and DNS rebinding protection.
  - `profile_lock.py`: Exclusive advisory file lock on candidate profile directories with stale lock cleanup.
  - `safe_resume.py`: Verification contract revalidating browser domain, URL security, and tenant before resuming after operator takeover or pause.
  - `ntfy.py`: Durable push notification outbox processor with bounded retry and exponential backoff.
  - `browser_runtime.py`: Virtual display (Xvfb) lifecycle, browser engine resolution (Chrome/Chromium/Firefox), and profile isolation.
  - `runtime_lock.py`: Process-level mutex for browser runtime processes.
  - `browser_automator.py`: Playwright-driven browser automator coordinating adapters, field filling, document attachment, and screenshot capture.
  - `question_solver.py`: Heuristics and Gemini AI question answering for application screening/qualification questions.
  - `candidate_profile.py`: Loads and manages the ignored private candidate profile (`data/candidate_profile.json`) initialized from a tracked example.
  - `autofill_script.py`: Generates 1-click in-browser JavaScript bookmarklets and scripts for each application package.
- **`src/job_applier/tracker.py`**: Tracks application pipeline statuses, timestamps, submission types, and proof screenshots in `data/applications_tracker.csv`.
- **`src/job_applier/scrapers/`**: Modules dedicated to fetching job postings.
  - `job_scraper.py`: Multi-platform scraper coordinating LinkedIn, Indeed, eJobs, BestJobs, Hipo, Undelucram, Jooble, Google Jobs, Greenhouse, Lever, and Remote tech boards.
  - `regional_scraper.py`: Dedicated scrapers for Romanian and regional job portals (eJobs, BestJobs, Hipo, Undelucram, Jooble).
  - `ats_scraper.py`: Direct REST API scraper for Greenhouse and Lever career portals.
  - `remote_scraper.py`: Scraper for remote tech opportunities.
  - `workday_scraper.py`: Logic specifically for workday applications (if active).
- **`src/job_applier/resume/`**: Contains the core logic for CV tailoring and PDF generation.
  - `tailor_cv.py`: Communicates with Google Gemini via `google-genai` to tailor JSON resumes based on the scraped job descriptions. Generates tailored JSONs and cover letters.
  - `resume.py`: Uses `fpdf` to take the tailored JSON data and compile it into a styled PDF file.
- **`src/job_applier/utils.py`**: Shared utility functions such as `get_project_root()` and text sanitizers.
- **`data/`**: Contains tracked privacy-safe examples; ignored runtime files store the real master resume, candidate profile, and processed-job state.
- **`output/`**: Stores the generated application artifacts (`applications/` for pending ones, `applied/` for completed ones).
- **`tests/`**: Contains the `pytest` test suite.

## Tech Stack & Dependencies

- **Backend:** Python >= 3.10 (FastAPI, Uvicorn, Playwright, SQLite WAL, Pandas)
- **Frontend:** Angular >= 21 (Standalone components, Signals, TypeScript, Tailwind CSS)
- **Task Runner:** `just` (see `justfile`)
- **Package Managers:** `uv` (Python), `npm` (Angular)
- **Key Libraries:** `fpdf` (PDFs), `google-genai` (Gemini 3.8-Flash), `python-jobspy` (Scraping), `pyrage` (Age encryption)
- **Testing:** `pytest`
- **Linting/Formatting:** `ruff`, `mypy`, `prettier`

## Common Commands (`just`)

Agents and developers should prefer using `just` recipes:

- `just web`: Run FastAPI backend hosting compiled Angular app on port 8000
- `just ui`: Run Angular dev server with hot reload on port 4200 (proxy to 8000)
- `just build-ui`: Compile production Angular bundle
- `just worker`: Run autonomous background application queue worker
- `just ops-backup`: Create an atomic online SQLite backup with optional age encryption
- `just ops-restore`: Restore system from backup with schema downgrade protection
- `just ops-retention`: Apply 7 daily / 4 weekly retention policy to backup archives
- `just ops-disk-guard`: Check storage volume headroom and fail-closed disk guard
- `just ops-emergency-stop`: Immediately halt queue execution and revoke active worker leases
- `just ops-unstop`: Clear emergency stop flag (remains paused awaiting safe resume)
- `just test`: Run the full pytest test suite
- `just test-ui`: Run Angular unit tests (Vitest)
- `just check`: Run linting, tests, and frontend build
- `just pipeline`: Run the scraping, tailoring, and PDF generation pipeline
- `just format`: Format Python code with ruff and build frontend
- `just auth-status`: Check platform session cookies

## Development Conventions

1. **Imports:** Always use absolute imports originating from the root package (e.g., `from job_applier.utils import get_project_root`). Avoid relative imports unless within closely coupled submodules.
2. **Path Resolution:** Do not rely on relative paths like `Path("data/")`. Always use `get_project_root()` from `job_applier.utils` to construct absolute paths dynamically.
3. **Typing & Formatting:** Adhere to Python standard typing hints where possible. Code must be valid under `ruff` linting checks.
4. **Resilience:** The AI tailoring process relies on external APIs (Gemini). Ensure proper retries and fallback logic are maintained in `tailor_cv.py`.

## Common Workflows for Agents

- **Adding a new scraper:** Add it to `src/job_applier/scrapers/`, make sure it outputs a `pandas` DataFrame with standard columns, and integrate it into `job_scraper.py`.
- **Modifying the PDF format:** Update the `PDF` class inside `src/job_applier/resume/resume.py`. Ensure you run tests (especially `test_generate_pdf_unicode`) to verify no character encoding errors are introduced via `fpdf`.
