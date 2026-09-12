# JobApplier Command Runner (justfile)
# See available commands by running `just`

# Default recipe: list all commands
default:
    @just --list

# Determine docker compose runner (uses sudo if non-root lacks daemon socket access)
compose := if `docker info >/dev/null 2>&1 && echo 1 || echo 0` == "1" { "docker compose" } else { "sudo docker compose" }

# Allowed Compose services and platforms for completions and validation
services := "|web|gateway|runtime|cloudflared|ntfy"
services_required := "web|gateway|runtime|cloudflared|ntfy"
compose_down_args := "|web|gateway|runtime|cloudflared|ntfy|--volumes|-v|--remove-orphans|--rmi"
compose_up_args := "|web|gateway|runtime|cloudflared|ntfy|--build|--no-build|--force-recreate|-d"
platforms := "linkedin|bestjobs|ejobs|google"

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

# Install enhanced shell completions for just (service names, platforms, arguments)
completions:
    @mkdir -p ~/.local/share/zsh/completions
    @cp deploy/completions/zsh/_just ~/.local/share/zsh/completions/_just
    @echo "Zsh completions installed to ~/.local/share/zsh/completions/_just"

# --- Running Web App & Frontend ---

# Run the FastAPI web dashboard server on port 8000
web:
    DISPLAY="${DISPLAY:-:20}" uv run python src/job_applier/cli/web_app.py

# Run the Angular frontend development server on port 4200 (with proxy to port 8000)
ui:
    cd frontend && npm start

# Compile production Angular single-page application bundle
build-ui:
    cd frontend && npx --yes --package=node@22.12.0 node node_modules/@angular/cli/bin/ng.js build

# --- Docker Services & Orchestration ---

# Start Docker Compose services in detached mode (e.g. `just services-up [service]`)
[arg('args', pattern=compose_up_args, help='Services to start or compose flags (web, gateway, runtime, cloudflared, ntfy)')]
services-up *args:
    {{compose}} up -d {{args}}

# Stop Docker Compose services
[arg('args', pattern=compose_down_args, help='Services to stop or compose flags (--volumes, --remove-orphans)')]
services-down *args:
    {{compose}} down {{args}}

# Restart all services or a targeted service (e.g. `just services-restart web`)
[arg('services', pattern=services, help='Target services (web, gateway, runtime, cloudflared, ntfy) or empty for all')]
services-restart *services:
    {{compose}} restart {{services}}

# Recreate containers to apply .env or config changes without restarting dependencies (e.g. `just services-reload web`)
[arg('services', pattern=services, help='Target services (web, gateway, runtime, cloudflared, ntfy) or empty for all')]
services-reload *services:
    {{compose}} up -d --no-deps {{services}}

# Stream logs for all services or a specific container (e.g. `just services-logs web`)
[arg('services', pattern=services, help='Target services (web, gateway, runtime, cloudflared, ntfy) or empty for all')]
services-logs *services:
    {{compose}} logs --tail=100 -f {{services}}

# Show container status, exposed ports, and healthcheck states
[arg('services', pattern=services, help='Target services (web, gateway, runtime, cloudflared, ntfy) or empty for all')]
services-status *services:
    {{compose}} ps {{services}}

alias services-ps := services-status
alias up := services-up
alias down := services-down
alias reload := services-reload
alias logs := services-logs

# Pull latest container images for Compose stack
[arg('services', pattern=services, help='Target services (web, gateway, runtime, cloudflared, ntfy) or empty for all')]
services-pull *services:
    {{compose}} pull {{services}}

# Build or rebuild Compose service images
[arg('services', pattern=services, help='Target services (web, gateway, runtime, cloudflared, ntfy) or empty for all')]
services-build *services:
    {{compose}} build {{services}}

# Execute a command inside a running service container (e.g. `just services-exec web bash`)
[arg('service', pattern=services_required, help='Target service container (web, gateway, runtime, cloudflared, ntfy)')]
[arg('cmd', help='Command to execute inside container')]
services-exec service *cmd:
    {{compose}} exec {{service}} {{cmd}}

# --- Systemd User Services ---

# Show status of host systemd user service (`job-applier.service`)
systemd-status:
    systemctl --user status job-applier.service

# Restart host systemd user service (`job-applier.service`)
systemd-restart:
    systemctl --user restart job-applier.service

# Stream journal logs for host systemd user service
[arg('args', help='Additional flags passed to journalctl (e.g. -n 50)')]
systemd-logs *args:
    journalctl --user -u job-applier.service -n 100 -f {{args}}

# Start host systemd user service (`job-applier.service`)
systemd-start:
    systemctl --user start job-applier.service

# Stop host systemd user service (`job-applier.service`)
systemd-stop:
    systemctl --user stop job-applier.service


# --- Pipeline & Scraping ---

# Run the autonomous application queue worker
[arg('args', help='Arguments and flags passed to autonomous queue worker')]
worker *args:
    uv run python -m job_applier.cli.worker {{args}}

# Run the end-to-end scraping, tailoring, and PDF generation pipeline
[arg('args', help='Pipeline flags and scraper options')]
pipeline *args:
    uv run python src/job_applier/cli/orchestrator.py {{args}}

# Run pipeline targeting remote/telework positions specifically
[arg('args', help='Remote pipeline flags and options')]
pipeline-remote *args:
    uv run python src/job_applier/cli/orchestrator.py --remote {{args}}

# Launch the interactive terminal application assistant
[arg('args', help='Assistant flags and options')]
assistant *args:
    uv run python src/job_applier/cli/apply_assistant.py {{args}}

# --- Authentication & Sessions ---

# Check connected accounts and cookie status
auth-status:
    uv run python src/job_applier/cli/auth_cli.py status

# Launch native Chrome on DISPLAY to log in and save cookies permanently
[arg('platform', pattern=platforms, help='Target platform (linkedin, bestjobs, ejobs, google)')]
login platform="linkedin":
    DISPLAY="${DISPLAY:-:20}" uv run python src/job_applier/cli/auth_cli.py login {{platform}}

# 1-click sync cookies from desktop Chrome (Profile 5 & Default) into .browser_profile/
sync-chrome:
    curl -s -X POST http://127.0.0.1:8000/api/auth/sync-chrome

# --- Database & Backups ---

# Export all applications, resumes, and SQLite database to a portable .zip backup
[arg('output', help='Destination zip backup file path (default: job_applier_backup.zip)')]
export output="job_applier_backup.zip":
    uv run python src/job_applier/cli/sync_cli.py export --output {{output}}

# Import and merge a portable backup .zip bundle into local SQLite database
[arg('input', help='Source zip backup file path (default: job_applier_backup.zip)')]
import input="job_applier_backup.zip":
    uv run python src/job_applier/cli/sync_cli.py import --input {{input}}

# Create an atomic, consistent online backup with optional age encryption
[arg('args', help='Backup flags (e.g. --age-recipient or --retention)')]
ops-backup *args:
    uv run python -m job_applier.cli.ops_cli backup {{args}}

# Restore system from a backup archive with schema downgrade protection
[arg('archive', help='Path to backup archive (.zip or .zip.age)')]
[arg('args', help='Additional restore flags')]
ops-restore archive *args:
    uv run python -m job_applier.cli.ops_cli restore {{archive}} {{args}}

# Apply 7 daily / 4 weekly retention policy to backup archives
[arg('args', help='Retention policy flags')]
ops-retention *args:
    uv run python -m job_applier.cli.ops_cli retention {{args}}

# Check storage volume headroom and fail-closed disk guard
[arg('args', help='Disk guard flags (e.g. --threshold)')]
ops-disk-guard *args:
    uv run python -m job_applier.cli.ops_cli disk-guard {{args}}

# Immediately engage emergency stop and revoke active worker leases
[arg('args', help='Emergency stop flags')]
ops-emergency-stop *args:
    uv run python -m job_applier.cli.ops_cli emergency-stop {{args}}

# Clear emergency stop flag (automation remains paused awaiting safe resume)
[arg('args', help='Unstop flags')]
ops-unstop *args:
    uv run python -m job_applier.cli.ops_cli unstop {{args}}

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
[arg('args', help='Pytest flags and filter expressions')]
test *args:
    uv run pytest {{args}}

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
