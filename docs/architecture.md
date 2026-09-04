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
├── output/                             # Generated application artifacts
│   ├── applications/                   # Pending tailored application packages
│   └── applied/                        # Submitted applications & proof screenshots
├── src/job_applier/                    # Core Python package
│   ├── automation/                     # Browser automation & profile management
│   ├── cli/                            # Thin CLI argument wrappers
│   ├── resume/                         # AI resume tailoring & PDF rendering
│   ├── scrapers/                       # Multi-site scrapers & activity detection
│   ├── web/                            # FastAPI web server & Alpine.js frontend
│   ├── db.py                           # SQLite engine & transactional models
│   ├── logger.py                       # Centralized structured event logger
│   ├── pipeline.py                     # Core end-to-end pipeline service
│   ├── sync.py                         # Portable export/import zip packaging
│   ├── tracker.py                      # Application history & analytics
│   └── utils.py                        # Common path & string sanitizers
└── tests/                              # Pytest test suite (50+ tests)
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

### 4. Browser Automation (`src/job_applier/automation/`)

- `browser_automator.py`: Playwright browser automator for form autofill, resume attachments, and submission verification.
- `auth_manager.py`: Persistent browser profile session vault with 1-click desktop Chrome cookie synchronization.
- `question_solver.py`: Heuristic and AI-driven solver for application screening questions.
- `cloudflare.py`: Cloudflare Turnstile challenge solver with display detection.
- `autofill_script.py`: Generator for 1-click in-browser JavaScript bookmarklets.

### 5. Web Interface (`src/job_applier/web/`)

- `app.py`: FastAPI server exposing REST endpoints for the dashboard.
- `templates/index.html`: Responsive single-page dashboard built with Tailwind CSS and Alpine.js.

### 6. Command-Line Entrypoints (`src/job_applier/cli/`)

- `web_app.py`: Server launcher for the web dashboard.
- `orchestrator.py`: CLI wrapper for the end-to-end pipeline.
- `apply_assistant.py`: Interactive terminal assistant for reviewing and applying.
- `auth_cli.py`: Platform authentication and session management CLI.
- `sync_cli.py`: Backup export and import CLI.
