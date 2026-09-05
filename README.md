# Job Applier: AI-Powered Application Suite

Job Applier automates the job search lifecycle: scraping technical positions, tailoring resumes and cover letters with Google Gemini (`gemini-3.8-flash`), generating styled PDFs, and automating in-browser application submissions.

---

## Key Capabilities

- **Unified Web Dashboard:** Full-stack FastAPI + Alpine.js interface with side-by-side PDF preview, cover letter editor, and sortable tracker.
- **Multi-Site Scraping:** Aggregates listings from LinkedIn, Indeed, Glassdoor, Google Jobs, Romanian portals (eJobs, BestJobs, Hipo, Undelucram, Jooble), direct ATSs (Greenhouse, Lever), and remote tech boards.
- **AI Resume & Cover Letter Tailoring:** Analyzes job descriptions and dynamically tailors your master JSON resume and generates targeted cover letters using Google Gemini 3.8-Flash.
- **Browser Automation & Multi-Engine:** Cross-platform Playwright engine with Chrome/Chromium and Firefox support, Cloudflare Turnstile handling, form autofill, CV upload, and AI screening question answering.
- **Session Vault & Desktop Sync:** Engine-isolated persistent browser profiles (.browser_profile / .browser_profile_firefox) with 1-click session synchronization from desktop Chrome.
- **SQLite Database & Portable Sync:** Relational SQLite database with WAL concurrency, plus 1-click export/import of portable `.zip` backup bundles between machines.
- **Portable Deployment:** Multi-architecture Docker image, host-agnostic Compose configuration, health checks, and optional secret-based GitHub deployment.

---

## Quickstart

### 1. Clone & Setup

```bash
git clone https://github.com/ervinpopescu/job-applier.git
cd job-applier
just setup  # Installs Python (uv) and Angular (npm) dependencies
```

### 2. Configure Environment

Create a `.env` file in the project root:

```env
GOOGLE_API_KEY=your_gemini_api_key_here
JOB_APPLIER_BROWSER=auto  # (Optional: auto, chrome, chromium, or firefox)
```

`just setup` creates ignored private files from the public examples. Edit `data/master_resume.json` with your experience and update `data/candidate_profile.json` directly or through the dashboard.

### 3. Launch the Web Dashboard

```bash
just web
```

Open **`http://127.0.0.1:8000`** in your browser to start scraping, reviewing, and applying.

### Deploy with Docker

```bash
cp .env.example .env  # Add GOOGLE_API_KEY
docker compose up -d --build
```

Published releases can be installed on another server using only `compose.yaml` and `.env`; no repository checkout is required. See the **[deployment guide](docs/deployment.md)** for upgrades, persistent data, private access, and optional host deployment through GitHub Environment secrets.

---

## Common Tasks (`just`)

Run `just` to see all available automated recipes:

| Task | Command | Description |
| ------- | ------------- | ------------- |
| **Start Web Dashboard** | `just web` | Runs FastAPI server hosting the Angular application on port 8000 |
| **Angular Dev Server** | `just ui` | Runs `ng serve` on port 4200 with proxy to port 8000 |
| **Build Frontend** | `just build-ui` | Compiles production Angular bundle into `frontend/dist/` |
| **Run Pipeline** | `just pipeline` | Runs scraping, tailoring, and PDF generation |
| **Run Remote Pipeline** | `just pipeline-remote` | Runs pipeline targeting remote/telework positions |
| **Terminal Assistant** | `just assistant` | Launches interactive terminal review & auto-apply CLI |
| **Log In to Platform** | `just login linkedin` | Launches browser window to sign in & save session |
| **Desktop Chrome Sync** | `just sync-chrome` | 1-click copies sessions from desktop Chrome into profile |
| **Auth Status** | `just auth-status` | Inspects real cookie database for active login tokens |
| **Export Backup** | `just export backup.zip` | Creates portable `.zip` backup of all applications & SQLite DB |
| **Import Backup** | `just import backup.zip` | Unpacks and merges backup into local environment |
| **Prune Duplicates** | `just prune-dupes` | Removes redundant packages with matching canonical URLs/roles |
| **Prune Expired** | `just prune-expired` | Purges closed/expired job postings from queue |
| **Run Tests** | `just test` | Runs the full `pytest` test suite |
| **Run All Checks** | `just check` | Runs linters, pytest tests, and Angular production build |

---

## Documentation

Detailed guides and references are split into specialized documentation:

| Guide | Description |
| ------- | ------------- |
| 🏗️ **[Architecture & Project Structure](docs/architecture.md)** | Directory tree, component design, and module roles. |
| 🖥️ **[Web Dashboard Guide](docs/web_dashboard.md)** | Queue workflows, live HUD monitor, diagnostics, and tracker views. |
| ⌨️ **[CLI Reference](docs/cli.md)** | Options for `orchestrator.py`, `apply_assistant.py`, and interactive keybindings. |
| 🔐 **[Authentication & Sessions](docs/authentication.md)** | Persistent browser profiles, desktop Chrome cookie sync, and 2FA handling. |
| 📦 **[Database & Portable Sync](docs/sync.md)** | SQLite database model, backup bundle structure, and machine migration. |
| 🚀 **[Deployment](docs/deployment.md)** | Docker Compose, GHCR releases, upgrades, private access, and secret-based CI/CD. |

---

## Testing

Run the test suite with `uv`:

```bash
uv run pytest
```

---

## License

[MIT License](LICENSE)
