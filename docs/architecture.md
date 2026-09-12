# Architecture & Project Structure

This document outlines the directory layout, modules, and core architectural components of `job-applier`.

## Directory Structure

```text
job-applier/
├── data/                               # Private runtime data and public examples
│   ├── candidate_profile.example.json  # Privacy-safe profile template (tracked)
│   ├── master_resume.example.json      # Privacy-safe resume template (tracked)
│   ├── candidate_profile.json          # Private profile generated locally (ignored)
│   ├── master_resume.json              # Private source resume generated locally (ignored)
│   ├── job_applier.db                  # Relational SQLite database (ignored)
│   └── applications_tracker.csv        # Application history tracker (ignored)
├── docs/                               # Specialized documentation guides
├── frontend/                           # Angular 21 Single-Page Application
│   ├── src/                            # Components, services, signals, models
│   └── dist/                           # Compiled production bundle served by FastAPI
├── output/                             # Generated application artifacts
│   ├── applications/                   # Pending tailored application packages
│   └── applied/                        # Submitted applications & proof screenshots
├── src/job_applier/                    # Core Python package
│   ├── automation/                     # Browser automation, adapters, queue & safety
│   │   └── adapters/                   # Versioned ATS adapters (Greenhouse, Lever, Ashby, Generic)
│   ├── cli/                            # Thin CLI argument wrappers & worker
│   ├── ops/                            # Operations tooling (backups, age encryption, disk guard)
│   ├── resume/                         # AI resume tailoring & PDF rendering
│   ├── scrapers/                       # Multi-site scrapers & activity detection
│   ├── web/                            # FastAPI web server & edge auth middleware
│   ├── db.py                           # SQLite engine & transactional models
│   ├── logger.py                       # Centralized structured event logger
│   ├── pipeline.py                     # Core end-to-end pipeline service
│   ├── sync.py                         # Portable export/import zip packaging
│   ├── tracker.py                      # Application history & analytics
│   └── utils.py                        # Common path & string sanitizers
└── tests/                              # Pytest test suite
```

## Module Overview

### 1. Core Services (`src/job_applier/`)

- `pipeline.py`: Orchestrates the full lifecycle (Scrape -> Filter -> Tailor -> PDF -> Auto-Apply).
- `db.py`: High-concurrency SQLite storage with WAL mode, parameterized queries, and automatic legacy CSV migration.
- `tracker.py`: Tracks application statuses, dates, platforms, and proof screenshots.
- `sync.py`: Packages applications, tailored resumes, and database into portable `.zip` archives.
- `logger.py`: Centralized event dispatcher streaming to console and web HUD.
- `utils.py`: Path resolution (`get_project_root`), naming sanitizers, and folder parsers.

### 2. Scrapers (`src/job_applier/scrapers/`)

- `job_scraper.py`: Multi-site aggregator for LinkedIn, Indeed, Glassdoor, Google Jobs, Greenhouse, Lever, and Remote tech boards.
- `regional_scraper.py`: Romanian portals (eJobs, BestJobs Next.js SSR, Hipo, Undelucram, Jooble).
- `activity_checker.py`: Real-time job freshness predictor detecting closed/expired postings before tailoring.
- `url_resolver.py`: Resolves canonical company ATS URLs from aggregator redirects.
- `ats_scraper.py`: Direct API scrapers for Greenhouse and Lever.
- `remote_scraper.py`: Scrapers for Jobicy and RemoteOK.

### 3. Resume & Tailoring (`src/job_applier/resume/`)

- `tailor_cv.py`: Communicates with Google Gemini (`gemini-3.8-flash`) to customize resume JSON and generate tailored cover letters.
- `resume.py`: Compiles tailored JSON resumes into styled PDFs using `fpdf`.

### 4. Browser Automation & Safety (`src/job_applier/automation/`)

- `adapters/`: Versioned typed ATS adapters implementing `BaseATSAdapter` for Greenhouse, Lever, Ashby, and Generic forms.
- `safety_guard.py`: Central submission safety guard enforcing canary approvals, daily submission caps (5/day), minimum spacing (300s), lease fencing, and frozen revision validation.
- `queue.py`: Durable execution queue managing atomic state transitions, worker claims, lease heartbeats, and runtime pause/resume controls.
- `network_security.py`: Enforceable outbound proxy with SSRF egress denial for private IPs and DNS rebinding protection.
- `profile_lock.py`: Exclusive advisory file lock on candidate profile directories with stale lock cleanup.
- `safe_resume.py`: Verification contract revalidating browser domain, URL security, and tenant before resuming after operator takeover or pause.
- `ntfy.py`: Durable push notification outbox processor with bounded retry and exponential backoff.
- `browser_runtime.py`: Virtual display (Xvfb) lifecycle, browser engine resolution (Chrome/Chromium/Firefox), and profile isolation.
- `browser_automator.py`: Playwright browser automator for form autofill, resume attachments, and submission verification.
- `auth_manager.py`: Persistent browser profile session vault with 1-click desktop Chrome cookie synchronization.
- `question_solver.py`: Heuristic and AI-driven solver for application screening questions.
- `autofill_script.py`: Generator for 1-click in-browser JavaScript bookmarklets.

### 5. Web Interface & Edge Security (`src/job_applier/web/`)

- `app.py`: FastAPI server exposing REST endpoints and serving the compiled Angular frontend.
- `edge_auth.py`: Zero-trust Cloudflare Access middleware with RS256 JWT validation, identity allowlists, CSRF origin verification, and gateway noVNC forward-auth gate.

### 6. Operations & Reliability (`src/job_applier/ops/`)

- `backup.py`: Online SQLite snapshot backups via SQLite Backup API, quiesced queue pausing, and age encryption (`pyrage`).
- `disk_guard.py`: Fail-closed disk space guard verifying minimum volume headroom before writes.
- `emergency_stop.py`: Immediate circuit breaker halting execution, revoking active worker leases, and pausing queue.

### 7. Command-Line Entrypoints (`src/job_applier/cli/`)

- `web_app.py`: Server launcher for the web dashboard.
- `worker.py`: Autonomous queue worker with heartbeat leasing and submission pacing.
- `ops_cli.py`: Operational backups, restore with downgrade protection, retention rotation, disk guard, and emergency stop.
- `runtime_daemon.py`: Container daemon managing virtual display (Xvfb), VNC, websockify, and proxy.
- `orchestrator.py`: CLI wrapper for the end-to-end pipeline.
- `apply_assistant.py`: Interactive terminal assistant for reviewing and applying.
- `auth_cli.py`: Platform authentication and session management CLI.
- `sync_cli.py`: Backup export and import CLI.
