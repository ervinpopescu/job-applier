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
    uv run python src/job_applier/cli/web_app.py

# Run the Angular frontend development server on port 4200 (with proxy to port 8000)
ui:
    cd frontend && npm start

# Compile production Angular single-page application bundle
build-ui:
    cd frontend && npx --yes --package=node@22.12.0 node node_modules/@angular/cli/bin/ng.js build

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
auth-status *ARGS:
    uv run python src/job_applier/cli/auth_cli.py status {{ARGS}}

# Launch interactive browser session to log in and save cookies permanently
login platform="linkedin" *ARGS:
    uv run python src/job_applier/cli/auth_cli.py login {{platform}} {{ARGS}}

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

# Run Angular unit tests (Vitest)
test-ui:
    cd frontend && npx --yes --package=node@22.12.0 node node_modules/@angular/cli/bin/ng.js test --watch=false

# Run linter checks (pre-commit ruff hook + mypy)
lint:
    uv run pre-commit run ruff --all-files
    uv run mypy src/ --ignore-missing-imports

# Run all pre-commit git hooks
pre-commit:
    uv run pre-commit run --all-files

# Format Python code via pre-commit ruff-format hook and frontend with Prettier
format:
    uv run pre-commit run ruff-format --all-files || true
    cd frontend && npm run format

# Verify code formatting via the pinned pre-commit ruff hook without modifying files
format-check:
    uv run pre-commit run ruff-format-check --all-files
    cd frontend && npm run format:check

# Run all quality checks: format, lint, tests, and frontend build
check:
    just format-check
    just lint
    just test
    just test-ui
    just build-ui
