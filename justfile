# JobApplier Command Runner (justfile)
# See available commands by running `just`

# Default recipe: list all commands
default:
    @just --list

# --- Environment & Setup ---

# Install Python & Node dependencies, configure environment
setup:
    uv sync
    cd frontend && npm install
    just init-data
    uv run pre-commit install

# Create private runtime data from public examples without overwriting existing files
init-data:
    @test -e data/candidate_profile.json || cp data/candidate_profile.example.json data/candidate_profile.json
    @test -e data/master_resume.json || cp data/master_resume.example.json data/master_resume.json

# Install Python dependencies only
install:
    uv sync

# --- Running Web App & Frontend ---

# Run the FastAPI web dashboard server on port 8000
web:
    DISPLAY="${DISPLAY:-:20}" uv run python src/job_applier/cli/web_app.py

# Run the Angular frontend development server on port 4200 (with proxy to port 8000)
ui:
    cd frontend && npm start

# Compile production Angular single-page application bundle
build-ui:
    cd frontend && npm run build

# --- Pipeline & Scraping ---

# Run the end-to-end scraping, tailoring, and PDF generation pipeline
pipeline *ARGS:
    uv run python src/job_applier/cli/orchestrator.py {{ARGS}}

# Run pipeline targeting remote/telework positions specifically
pipeline-remote *ARGS:
    uv run python src/job_applier/cli/orchestrator.py --remote {{ARGS}}

# Launch the interactive terminal application assistant
assistant *ARGS:
    uv run python src/job_applier/cli/apply_assistant.py {{ARGS}}

# --- Authentication & Sessions ---

# Check connected accounts and cookie status
auth-status:
    uv run python src/job_applier/cli/auth_cli.py status

# Launch native Chrome on DISPLAY to log in and save cookies permanently
login platform="linkedin":
    DISPLAY="${DISPLAY:-:20}" uv run python src/job_applier/cli/auth_cli.py login {{platform}}

# 1-click sync cookies from desktop Chrome (Profile 5 & Default) into .browser_profile/
sync-chrome:
    curl -s -X POST http://127.0.0.1:8000/api/auth/sync-chrome

# --- Database & Backups ---

# Export all applications, resumes, and SQLite database to a portable .zip backup
export output="job_applier_backup.zip":
    uv run python src/job_applier/cli/sync_cli.py export --output {{output}}

# Import and merge a portable backup .zip bundle into local SQLite database
import input="job_applier_backup.zip":
    uv run python src/job_applier/cli/sync_cli.py import --input {{input}}

# --- Queue & Data Hygiene ---

# Scan queue and prune duplicate applications (matching canonical URLs or identical roles)
prune-dupes:
    curl -s -X POST http://127.0.0.1:8000/api/applications/prune-duplicates

# Scan queue and purge closed or expired job postings
prune-expired:
    curl -s -X POST http://127.0.0.1:8000/api/applications/prune-inactive

# Filter queue strictly for EMEA, Europe, and Romania positions
prune-non-emea:
    curl -s -X POST http://127.0.0.1:8000/api/applications/prune-by-region -H "Content-Type: application/json" -d '{"region": "EMEA", "strict": true}'

# --- Testing & Quality ---

# Run the full pytest test suite
test *ARGS:
    uv run pytest {{ARGS}}

# Run linter checks (ruff + mypy)
lint:
    uv run ruff check
    uv run mypy src/ --ignore-missing-imports

# Format Python code with ruff and frontend with Prettier
format:
    uv run ruff format
    cd frontend && npm run format

# Run all quality checks: lint, test, and frontend build
check:
    just lint
    just test
    just build-ui
