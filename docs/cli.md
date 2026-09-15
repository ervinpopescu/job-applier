# Command-Line Interface (CLI) Reference

The `job-applier` project provides focused CLI tools for running pipelines, applying to jobs, authenticating platforms, and synchronizing backups from the terminal.

## 1. Orchestrator (`orchestrator.py`)

Runs the end-to-end pipeline: scraping, AI tailoring, PDF generation, and optional auto-apply.

```bash
# Using just:
just pipeline [OPTIONS]
just pipeline-remote

# Or directly with Python:
python src/job_applier/cli/orchestrator.py [OPTIONS]
```

### Options

| Flag | Description | Default |
| ------ | ------------- | --------- |
| `--terms` | Comma-separated search roles | `"Python Dev,Solutions Architect,Software Engineer,Cloud Architect,DevOps Engineer"` |
| `--locations` | Comma-separated job locations | `"Bucharest"` |
| `--limit` | Maximum results to fetch per term | `50` |
| `--sites` | Comma-separated job boards / ATSs | `"linkedin,indeed,ejobs,bestjobs,hipo,undelucram,jooble,google,greenhouse,lever,remote,glassdoor"` |
| `--remote` | Search specifically for remote/telework positions | `False` |
| `--auto-apply` | Automatically launch browser to apply after generation | `False` |
| `--autonomous` | Submit forms autonomously without manual review | `False` |
| `--headless` | Run browser in headless background mode | `False` |
| `--api_key` | Google Gemini API key (defaults to `GOOGLE_API_KEY` env var) | `None` |

### Examples

```bash
# Scrape remote roles across tech boards:
python src/job_applier/cli/orchestrator.py --remote --terms "Cloud Architect,Python Dev" --limit 20

# Run with immediate assisted browser application:
python src/job_applier/cli/orchestrator.py --terms "DevOps Engineer" --auto-apply
```

---

## 2. Apply Assistant (`apply_assistant.py`)

An interactive terminal tool to review prepared application packages, autofill forms, and submit.

```bash
# Using just:
just assistant [OPTIONS]

# Or directly with Python:
python src/job_applier/cli/apply_assistant.py [OPTIONS]
```

### Interactive Keybindings

When navigating application packages in interactive mode:

- `a`: **⚡ Auto-Apply (Assisted)** — Launches Chrome, opens the job posting, navigates to the form, autofills fields, attaches the tailored CV PDF, pastes the cover letter, answers AI screening questions, and highlights the submit button for your review.
- `A`: **🤖 Auto-Apply (Autonomous)** — Fully automated navigation, filling, and submission with confirmation screenshot saved.
- `b`: **📦 Batch Auto-Apply** — Sequentially processes the next N jobs in your queue.
- `o`: **🌐 Quick Open** — Opens the job URL in your default browser and copies the cover letter and PDF path to your clipboard.
- `c`: **📋 Copy Cover Letter** to system clipboard.
- `r`: **📄 Copy Resume PDF Path** to system clipboard.
- `p`: **👁️ Preview Cover Letter** in terminal.
- `f`: **📂 Open Application Folder** in system file manager.
- `d`: **✅ Mark as DONE** (moves folder to `output/applied/` and records to database).
- `s`: **⏭️ Skip** to next application.
- `/`: **🔍 Search / Filter** applications by company or title.
- `t`: **📊 View Tracker Statistics**.

### Batch & Headless Flags

```bash
# Auto-apply to the next 10 applications in assisted mode:
python src/job_applier/cli/apply_assistant.py --auto --batch 10

# Auto-apply autonomously in headless background mode:
python src/job_applier/cli/apply_assistant.py --autonomous --headless --batch 5

# Filter applications by company:
python src/job_applier/cli/apply_assistant.py --filter "Adobe"

# View tracker statistics:
python src/job_applier/cli/apply_assistant.py --stats
```

---

## 3. Platform Authentication (`auth_cli.py`)

Manages login sessions and persistent cookies stored in `.browser_profile/`.

```bash
# Check current connection status:
just auth-status
# or: python src/job_applier/cli/auth_cli.py status

# Launch native Chrome on DISPLAY to log in once:
just login linkedin
just login bestjobs
just login ejobs
just login google
```

---

## 4. Backup & Machine Sync (`sync_cli.py`)

Exports and imports your applications, database mappings, and profiles.

```bash
# Export everything to a portable .zip bundle:
just export my_backup.zip
# or: python src/job_applier/cli/sync_cli.py export --output my_backup.zip

# On another machine, import and merge into local SQLite database:
just import my_backup.zip
# or: python src/job_applier/cli/sync_cli.py import --input my_backup.zip
```

---

## 5. Autonomous Queue Worker (`worker.py`)

Runs the background daemon that continuously polls the SQLite queue, claims jobs via heartbeated leases, executes platform adapters, and enforces the central safety guard.

```bash
# Using just:
just worker

# Or directly with Python:
python -m job_applier.cli.worker [OPTIONS]
```

### Options

| Flag | Description | Default |
| ------ | ------------- | --------- |
| `--max-jobs` | Maximum number of jobs to process before exiting | `None` (run continuously) |
| `--poll-interval` | Idle polling delay in seconds | `3.0` |
| `--headed` | Launch visible browser window (defaults to headless) | `False` |
| `--browser` | Browser engine (`chromium`, `firefox`, `chrome`) | `chromium` |
| `--worker-id` | Custom worker identifier string | Auto-generated UUID |

---

## 6. Operations & Disaster Recovery (`ops_cli.py`)

Provides operational reliability commands: quiesced online backups, age encryption, restore with downgrade protection, backup retention rotation, disk guards, and emergency stop.

```bash
# Using just:
just ops-backup
just ops-restore <ARCHIVE>
just ops-retention
just ops-disk-guard
just ops-emergency-stop
just ops-unstop

# Or directly with Python:
python -m job_applier.cli.ops_cli <SUBCOMMAND> [OPTIONS]
```

### Subcommands

- `backup`: Creates an atomic, online SQLite snapshot backup with optional age encryption.
  - `--recipient`: Age public key for encryption.
  - `--no-quiesce`: Skip pausing the worker during snapshot.
  - `--no-profile`: Omit the browser profile directory.
- `restore`: Restores database and artifacts from a backup archive with schema downgrade refusal protection.
  - `--identity-file`: Path to age secret key file for encrypted archives.
  - `--identity`: Secret key string directly.
- `retention`: Applies the 7 daily / 4 weekly rotation policy, purging expired archives.
  - `--dry-run`: Preview files to be pruned without deleting.
- `disk-guard`: Checks available filesystem headroom against minimum safety thresholds.
  - `--enforce`: Auto-pauses the queue if disk pressure is detected.
- `cleanup-debug`: Purges ephemeral debug screenshots and logs older than `--ttl-days`.
- `emergency-stop`: Atomically halts queue processing, pauses execution, and revokes active worker leases.
- `unstop`: Clears emergency stop flag (automation remains paused awaiting safe resume).

---

## 7. Runtime Daemon (`runtime_daemon.py`)

Supervises containerized virtual display (Xvfb), internal VNC server, websockify bridge, and outbound security proxy for headed automation and remote viewer sessions.

```bash
python -m job_applier.cli.runtime_daemon [OPTIONS]
```

### Options

| Flag | Description | Default |
| ------ | ------------- | --------- |
| `--display` | Virtual display identifier | `$DISPLAY` or `:99` |
| `--vnc-port` | Internal localhost VNC port | `$VNC_PORT` or `5900` |
| `--websockify-port` | Internal websockify bridge port | `$WEBSOCKIFY_PORT` or `6080` |
| `--proxy-port` | Outbound security proxy port | `$PROXY_PORT` or `8899` |
| `--poll-interval` | Queue polling interval in seconds | `3.0` |
| `--novnc-dir` | Directory containing noVNC web assets | `/usr/share/novnc` |
| `--browser` | Browser engine (`chromium`, `firefox`, `chrome`) | `chromium` |
