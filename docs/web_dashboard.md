# Web Dashboard Guide

The Job Applier dashboard is a unified, real-time web application built with FastAPI, Tailwind CSS, and Alpine.js. It runs on `http://127.0.0.1:8000`.

## Launching the Dashboard

```bash
# Production server (hosting compiled Angular SPA):
just web
# or: python src/job_applier/cli/web_app.py

# Frontend development server with hot-reload (port 4200):
just ui
```

---

## Dashboard Views

### 1. Applications Queue

The primary workspace for pending applications:

- **Card View:** Displays company name, job title, platform badge (Greenhouse, Lever, BestJobs, LinkedIn), and creation timestamp.
- **Side-by-Side Review Modal:**
  - Preview the tailored PDF resume directly in your browser.
  - Read and edit the customized cover letter in real time.
  - Access 1-click browser bookmarklets for manual application pages.
- **Action Buttons:**
  - **⚡ Auto-Apply (Assisted):** Opens Chrome, navigates to the form, autofills all contact info, attaches the tailored CV PDF, pastes the cover letter, and highlights the submit button.
  - **🤖 Autonomous Submit:** Fully automates the submission and saves a verification screenshot.
  - **Prune Expired:** Scans all queue links and purges inactive/closed job postings.
  - **Clear Failed:** Cleans failed attempts from the queue and database.

### 2. Application Tracker

A full-width analytics and status table:

- Real-time statuses: `Applied`, `Auto-filled`, `Interviewing`, `Offered`, `Rejected`, `Skipped`.
- Sortable columns: Timestamp, Company, Role, Platform, Status, Submission Type.
- Filter by status dropdown.
- One-click **Diagnostics**: Opens failure screenshots and DOM inspection for unconfirmed submissions.
- **Send to Queue:** Requeues previously applied jobs if rework is required.

### 3. Scraper Configuration

Launch new scraping and AI tailoring runs:

- Target search keywords and locations.
- **Remote / Telework Toggle:** Enables global remote routing for LinkedIn, Indeed, and Glassdoor.
- Multi-platform selection checkboxes (LinkedIn, Indeed, eJobs, BestJobs, Hipo, Undelucram, Jooble, Greenhouse, Lever, Remote Tech, Glassdoor).
- Optional inline auto-apply toggle.

### 4. Live Execution Console

Dedicated, full-height streaming terminal:

- Real-time log stream from scraping, AI tailoring (Gemini 3.8-Flash), and browser automation.
- Filters: By category (`Pipeline`, `Auto-Apply`, `Auth`, `Scraper`) and level (`INFO`, `SUCCESS`, `WARN`, `ERROR`).
- Auto-scroll toggle and one-click **Clear Logs** button.

### 5. Candidate Profile

Customize your contact info, location, URLs (LinkedIn, GitHub, Portfolio), current employer, and pre-configured screening answers (notice period, work authorization, salary expectations).

---

## Logins & Authentication Modal

Accessible via the **`Logins & Auth`** button in the top navbar:

- **1-Click Sync from Desktop Chrome:** Directly merges authenticated sessions from your local desktop Chrome browser (`Profile 5` and `Default`) into `.browser_profile/`.
- **Connect / Log In:** Launches native Chrome on your display to log in manually once with password and 2FA.
- Live status indicators: 🟢 `Connected` / ⚪ `Not Connected`.
