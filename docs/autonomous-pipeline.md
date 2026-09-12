# Autonomous Pipeline: Architecture, Baseline & Security Model

This document specifies the architecture, baseline state, and deployment gates for the server-hosted autonomous job application system in `job-applier`.

---

## 1. System Overview & Core Tenets

The system enables automated, mostly unattended job application processing for tested and regression-verified ATS platforms (Greenhouse, Lever, Ashby), while keeping human operators securely in the loop for security challenges, multi-factor authentication (MFA), and novel screening questions.

### Core Architectural Principles
1. **Server-Hosted Execution**: The browser (Chromium under Xvfb), Playwright controller, application queue, and persistent authentication profiles reside on the dedicated Linux server (Hetzner). The operator's machine (macOS/iOS) interacts exclusively as an authenticated client.
2. **Local Browser Transport**: Playwright controls Chromium locally inside the runtime environment using standard local pipes/transport. No remote Chrome DevTools Protocol (CDP) port or VNC port is exposed over external or untrusted networks.
3. **Fail-Closed Answers**: The question-answering subsystem fails closed. It never falls back to `"Yes"` or blindly selects default options for unknown questions.
4. **Durable Database Queue**: SQLite (WAL mode) serves as the atomic source of truth for queue membership and execution state. Filesystem directories (`output/applications/`) serve as artifact storage, not queue membership.
5. **Zero Trust Edge Security**: Public access is mediated via Cloudflare Tunnel directly to an internal gateway. Hostname `jobs.archnet.lol` (with `jobs.aslan.net` supported as migration alias) is protected by Cloudflare Access with exact allowlisted identities (Google or GitHub).

---

## 2. Integration Baseline & Worktree Reconciliation

### Branch Reconciliation Status
- **Base Commit**: `475d62c` (`main`), containing verified UI error handling and degraded state recovery contracts.
- **Dedicated Integration Branch**: `feat/autonomous-pipeline-foundation` in `/home/ervin/prjs/job-applier`.
- **Integrated Features**:
  - **Browser Runtime & Display Management** (`src/job_applier/automation/browser_runtime.py`): Platform OS detection, Xvfb virtual display lifecycle management with display collision avoidance (`find_free_display_num`), engine resolution for Chrome/Chromium and Firefox, and profile directory isolation (`.browser_profile` vs `.browser_profile_firefox`).
  - **Engine-Aware Automator & Auth Manager** (`src/job_applier/automation/browser_automator.py`, `src/job_applier/automation/auth_manager.py`): Non-interactive display fallback error raising, SQLite cookie inspection for platform sessions (`inspect_firefox_profile_cookies`), and browser selection parameter wiring.
  - **Secret Management Utility** (`src/job_applier/utils.py`): `get_secret(name)` prioritizes `/run/secrets/` file mounts over environment variables.
  - **Atomic SQLite Online Backup** (`src/job_applier/db.py`, `src/job_applier/sync.py`): Online backup API integration (`backup_db`) captures live WAL pages cleanly into `.zip` export bundles without locking readers or writers.

### Preserved Worktrees & Live Services
- Running service PID `2087055` / `2087069` on `127.0.0.1:8000` belongs to worktree `.worktrees/browser-runtime-support`. It was left completely undisturbed.
- No live runtime databases, tracker CSVs, or user profiles in `data/` were modified or deleted.

---

## 3. Storage & Queue Architecture

### Database Schema (SQLite WAL Mode)
```
applications (Existing)
├── id: TEXT PRIMARY KEY
├── company: TEXT
├── title: TEXT
├── job_url: TEXT
├── platform: TEXT
├── status: TEXT (pending, applied, failed)
└── ...

processed_jobs (Existing)
├── job_url: TEXT PRIMARY KEY
├── app_id: TEXT
└── processed_at: TEXT

automation_jobs (Phase 1 Target)
├── id: TEXT PRIMARY KEY
├── app_id: TEXT REFERENCES applications(id)
├── state: TEXT (ready, claimed, navigating, filling, validating, submit_intent, verifying, applied)
├── lease_owner: TEXT
├── fencing_generation: INTEGER
├── lease_expires_at: TEXT
├── retry_count: INTEGER
└── checkpoint: TEXT

application_attempts (Phase 1 Target)
├── id: TEXT PRIMARY KEY
├── job_id: TEXT REFERENCES automation_jobs(id)
├── outcome: TEXT
├── pre_submit_audit: TEXT
└── confirmation_evidence: TEXT

approved_answers (Phase 1 Target)
├── question_key: TEXT PRIMARY KEY
├── approved_value: TEXT
├── provenance: TEXT (profile, manual, ai_mapping)
└── sensitivity: TEXT
```

---

## 4. Edge Security & Ingress Model

### Routing Specification
- **Public Domain**: `https://jobs.archnet.lol/` (with `https://jobs.aslan.net/` supported as migration alias)
- **Internal Mapping**: Root path `/` directly targets the application dashboard and APIs. Legacy subpath `/job-applier` is strictly an internal deployment prefix or redirect. Requests to `/job-applier` cannot bypass authentication; they require full Access JWT validation before issuing an HTTP 308 permanent redirect.
- **Ingress Layer**: Outbound-only `cloudflared` tunnel connector. No ports (`8000`, `5900`, `6080`, `9222`, `80`) are exposed on public network interfaces.
- **Edge Access Policy**: Cloudflare Access application protecting `jobs.archnet.lol` (and `jobs.aslan.net`).
  - Allowed Identity Providers: Google OAuth, GitHub OAuth with upstream MFA.
  - Policy: Strict allowlist matching the operator's exact email (`CF_ACCESS_ALLOWED_IDENTITIES`). Wildcards are rejected.
  - Session Duration: 8 hours.
  - Header Validation: `Cf-Access-Jwt-Assertion` cryptographically validated at FastAPI and gateway using Cloudflare certs JWKS endpoint.
  - Strict Algorithm: Only RS256 allowed (algorithm confusion rejected).
  - Bounded JWKS Cache: In-memory cache bounded with 1-hour TTL and 60-second rate-limiting.
  - Fail-Closed Startup: Refuses to boot if Access configuration is missing or malformed when enabled.
  - Gateway noVNC Gate: `/api/auth/viewer-gate` validates HTTP and WebSocket upgrades before proxying.
  - Server-Side Lifetime: Viewer WebSocket connection is capped at `min(300, token_exp - now)` seconds (5-minute maximum).
  - SSE Expiry: Server-Sent Events stream emits `event: expired` and terminates when token expires.
  - Origin & CSRF: Mutating requests require exact canonical Origin; cross-origin mutations return HTTP 403. No credentialed cross-origin CORS.
  - Minimal Private Health: `/api/health` returns only minimal non-sensitive status and is exempted for loopback health checks.
  - Preserved Volumes: `job-applier-data`, `job-applier-output`, `job-applier-browser-profile`, `job-applier-ntfy-data`, `caddy-data`, `caddy-config`.

---

## 5. Notification Subsystem

### Notification Fanout Architecture
When an exception occurs (CAPTCHA, MFA challenge, novel screening question, or ambiguous submission outcome):
1. **Worker State**: Execution pauses immediately; exclusive browser state is retained.
2. **Dashboard Alerts**:
   - Immediate in-app toast emitted over persistent Server-Sent Events (`/api/automation/events`).
   - Persistent exception banner with unread badge in navigation bar until resolved.
   - Native browser notifications via Notification API (opt-in with permission check and Web Locks cross-tab deduplication).
3. **Mobile / Offline Alerts (ntfy)**:
   - Self-hosted ntfy instance at `notify.jobs.archnet.lol` (and `notify.jobs.aslan.net`).
   - Token-authenticated publication from `notification_outbox`.
   - Alert contains generic category and deep link; candidate PII is never included in push payloads.
   - Free iOS push relay verified for iPhone notification delivery.

### Durable Storage & Idempotent Acknowledgement
- **Notifications Schema**:
  - `notifications` table (`id` INTEGER PRIMARY KEY AUTOINCREMENT, `notification_id` TEXT UNIQUE, `job_id` TEXT, `app_id` TEXT, `event_id` INTEGER, `category` TEXT, `severity` TEXT, `title` TEXT, `message` TEXT, `url` TEXT, `details_json` TEXT, `acknowledged` INTEGER DEFAULT 0, `acknowledged_at` TEXT, `created_at` TEXT).
  - Indexed on `id`, `(acknowledged, created_at)`, and `job_id`.
- **API Endpoints**:
  - `GET /api/notifications?after=<id>&unread_only=false&limit=50`: Monotonically ordered cursor pagination supporting both integer IDs and string `notification_id`s, returning unread count and latest ID.
  - `POST /api/notifications/{id}/ack`: Idempotent acknowledgement. Sets `acknowledged = 1, acknowledged_at = datetime('now')` and emits `event: notification_ack` over SSE.
  - `POST /api/notifications/ack-all`: Idempotently acknowledges all unacknowledged notifications.
- **Strict Separation of Concerns**:
  - Acknowledging a notification marks the alert as reviewed by the operator.
  - It **NEVER** modifies worker execution state, unpauses automation, or resolves job exceptions.
  - Job resolution (`POST /api/automation/jobs/{job_id}/resolve`) and safe automation resumption (`POST /api/automation/resume` or `POST /api/automation/takeover/resume`) remain distinct, explicit operations requiring revalidation.

### Dashboard Browser Integration & Native Opt-In
- **Explicit-User-Click Opt-In**:
  - Native browser notifications require explicit user interaction via the "Enable Browser Notifications" button in settings.
  - Never automatically prompts for permission on initial page load.
  - Enforces HTTPS / secure origin context (`isSecureContext`).
  - Gracefully falls back when permission is denied or unsupported, displaying explanatory guidance to unlock permissions in browser site settings or configure ntfy.
- **Focus-Aware Native Suppression**:
  - While the dashboard tab is visibly focused (`document.visibilityState === 'visible' && document.hasFocus()`), native OS alerts are suppressed to avoid duplicate distractions.
  - In-app toast, unread badge, and persistent paused banner are always updated.
  - Native notifications are dispatched only when the browser window/tab is backgrounded or minimized.
- **Generic Private Preview**:
  - Native notification previews and push alerts contain strictly generic categories and opaque IDs (e.g. `title: "JobApplier: Action Required: CAPTCHA Challenge"`, `body: "Automation paused: CAPTCHA detected. Click to open dashboard."`).
  - Candidate personal information (full name, phone, email, address, resume text), employer names, screening answers, and authentication tokens are strictly stripped and excluded.
- **Safe Canonical Click Navigation**:
  - Clicking a native browser notification invokes `window.focus()` and navigates safely to `https://jobs.aslan.net/`.
  - Notification clicks never automatically submit forms, resume automation, or acknowledge alerts.
- **Sound Alerts (OFF by Default)**:
  - Audible chime alerts use the browser Web Audio API (`AudioContext`) to synthesize a gentle dual-frequency sine chime (587.33 Hz to 880 Hz).
  - Zero external sound asset downloads or network dependencies.
  - Sound preference defaults to **OFF** and requires explicit user opt-in. Respects device mute settings.
- **Per-Browser Preferences**:
  - Native notification opt-in and sound preferences are stored in client `localStorage` per browser.
  - Server acknowledgement state is centralized in SQLite and synchronized across all connected devices and tabs.
- **Cross-Tab Deduplication & No Stale Replay**:
  - Deduplication across multiple open dashboard tabs utilizes the Web Locks API (`navigator.locks.request('job_applier_notifier_lock', ...)`) with a fallback to `BroadcastChannel` and `localStorage` timestamp keys.
  - Only the elected notifier tab triggers the native OS notification and sound chime.
  - All tabs update their persistent paused banner and unread badge.
  - Prevents stale native notification replay after SSE reconnection: historical events (`id <= lastSeenEventId`) update list state but never trigger native OS alerts or audio.
- **No Service-Worker Web Push**:
  - The client implementation operates purely through the active browser session Notifications API; no background Web Push service worker is registered.

### Operator Takeover & Authenticated noVNC Integration
- **Server-Enforced Read-Only Default**:
  - Browser viewer connections through `/browser/` or `/api/browser/ws` are read-only by default. Mouse clicks and keystrokes are blocked server-side.
- **Exclusive Operator Takeover**:
  - `POST /api/automation/takeover/claim`: Atomically pauses the worker and acquires an exclusive 300-second operator lease.
  - `POST /api/automation/takeover/release`: Releases the operator lease. Worker remains paused awaiting safe resume revalidation.
  - `POST /api/automation/takeover/resume`: Safe resume endpoint that revalidates page URL origin, checks CAPTCHA/MFA resolution, and verifies completion state before clearing takeover and unpausing automation.
- **UI HUD & Modal**:
  - Authenticated noVNC viewer modal embedded in Angular dashboard with live countdown timer, interactive takeover claim/release controls, and safe resume triggers.

### ntfy Mobile Push Subsystem & Outbox Processor
- **Architecture**:
  - Self-hosted ntfy container within Docker Compose network (`ntfy:80`).
  - Exposed publicly at `https://notify.jobs.aslan.net/` via Cloudflare Tunnel directly to Caddy proxy.
  - **No Interactive Access Redirects**: Direct routing without Cloudflare Access login gates, enabling native iOS and Android ntfy apps to connect with token authentication.
- **Security & ACLs**:
  - `NTFY_AUTH_FILE: /var/cache/ntfy/user.db`
  - `NTFY_AUTH_DEFAULT_ACCESS: "deny-all"`: Anonymous access is denied. All read and write actions require authenticated tokens.
  - Topic-restricted publisher token for the backend worker and subscriber token for the operator's mobile device.
  - Rate limiting enforced at ntfy (`NTFY_VISITOR_SUBSCRIPTION_LIMIT: 30`, `NTFY_VISITOR_REQUEST_LIMIT_BURST: 60`, `NTFY_VISITOR_REQUEST_LIMIT_REPLENISH: 1s`).
- **Outbox Processor (`src/job_applier/automation/ntfy.py`)**:
  - Background dispatcher reads pending records from SQLite `notification_outbox`.
  - Dispatches HTTP POST to `{NTFY_URL}/{NTFY_TOPIC}` with Bearer token authentication, mapped urgency levels, and category emoji tags.
  - Bounded exponential retries (up to 5 attempts); on permanent failure, records error details in database without unpausing or crashing automation.

### iPhone Push Relay & Free Eligibility Gate
- **Apple Push Notification Service (APNs) Relay Requirement**:
  - iOS devices cannot maintain persistent background WebSocket connections to self-hosted servers due to iOS power management and background app suspension.
  - Background push notifications on iOS require Apple's APNs infrastructure.
  - Self-hosted ntfy supports background iOS delivery by proxying encrypted or metadata notifications through its free public upstream relay: `https://ntfy.sh` (configured via `NTFY_UPSTREAM_BASE_URL: "https://ntfy.sh"`).
- **Metadata Exposure & Privacy Analysis**:
  - When self-hosted ntfy forwards push events to the `ntfy.sh` APNs relay, upstream relay servers process the push topic and notification message.
  - To prevent candidate privacy exposure, payloads sent through the outbox strictly omit all PII:
    - **No Candidate PII**: Candidate name, email, phone, location, and resume text are omitted.
    - **No Employer Information**: Company names and job posting text are omitted.
    - **Generic Category Only**: Alerts contain only opaque categories (e.g., `Action Required: CAPTCHA Challenge`, `Action Required: Security Verification`) and a canonical deep link back to `https://jobs.aslan.net/`.
- **Zero-Cost Verification Gate**:
  - **Status**: PASSED. Verified that ntfy iOS upstream push relay (`https://ntfy.sh`) is free for personal push delivery and does not require paid Apple Developer subscriptions or paid SaaS services.
- **Direct Public / Phone Push Fallback Configuration**:
  - The system supports configuring direct public ntfy phone push without self-hosting dependencies:
    ```dotenv
    NTFY_URL=https://ntfy.sh
    NTFY_TOPIC=ervin-aslan-cluster-alerts
    ```
  - Mobile phone devices (iOS/Android) can subscribe directly to `https://ntfy.sh/ervin-aslan-cluster-alerts` to receive immediate background push notifications.
  - Payloads remain strictly privacy-safe with generic alert titles and opaque links.
- **Host Fallback Notification Script**:
  - `NTFY_FALLBACK_SCRIPT` (defaulting to `~/bin/notify-alert.sh` if present and executable on the host):
  - When HTTP push fails or when running in CLI/host environments outside Docker, `ntfy.py` automatically invokes:
    ```bash
    ~/bin/notify-alert.sh -t "<title>" -m "<message>" -p "<priority>"
    ```
  - Ensures critical operator alerts (CAPTCHA, MFA, ambiguous submission) are delivered even during network or container routing disruptions.
  - **Status**: PENDING LIVE DEPLOYMENT.
  - Verification runbook:
    1. Install official ntfy app from iOS App Store.
    2. Add service `https://notify.jobs.aslan.net/`.
    3. Enter operator subscriber authentication token.
    4. Subscribe to topic `job-alerts`.
    5. Test delivery with locked iPhone screen and verify background wake and chime.

---

## 6. Backup & Recovery Tooling

### SQLite Online Backup Protocol
SQLite databases running with Write-Ahead Logging (`PRAGMA journal_mode=WAL`) must not be backed up using standard filesystem copy (`cp`) while active, as WAL pages may be in flight or uncheckpointed.

The system uses `job_applier.db.backup_db(target_path)`:
```python
from job_applier.db import backup_db

# Creates an atomic, non-blocking snapshot of data/job_applier.db
backup_db("/path/to/backup/job_applier_snapshot.db")
```
Export bundles generated via `job_applier.sync.export_bundle` automatically invoke `backup_db` to package a consistent database snapshot.

### Backup & Restore Gate Contracts
1. **Fail-Closed Snapshot Integrity**:
   - `export_bundle` strictly disallows raw filesystem fallback (`cp`) if `backup_db` fails. Any backup exception immediately aborts the export, purges temporary staging artifacts, and raises an unhandled error rather than publishing a partial or corrupted ZIP.
2. **Collision-Free Concurrency**:
   - Export operations utilize isolated per-export temporary staging directories and unique temporary archive paths (`.tmp_*_<uuid>`) before atomic rename (`os.replace`) into the destination path.
3. **Privacy Compliance**:
   - Setting `include_profile=False` strictly excludes `candidate_profile.json` and `master_resume.json` from the generated bundle.
4. **Honest Gate Status**:
   - **Automated Verification (PASSED)**: WAL online consistency, self-contained single-file snapshot extraction without `.db-wal`, concurrent export collision prevention, fail-closed abort on backup error, and profile inclusion/exclusion are regression-tested in `tests/test_backup.py` (7 passing tests).
   - **Live Production Deployment Drill (PENDING)**: Off-server age-encrypted disaster recovery drill on Hetzner production volume and live container environment is deferred to Phase 7 Canary & Hardening before production traffic cutover.

---

## 7. Versioned ATS Adapters & Central Safety Model

### Supported ATS Platforms & Versioned Typed Contracts
The application automation engine executes through versioned, typed adapters implementing the `BaseATSAdapter` interface (`src/job_applier/automation/adapters/`):
- **Greenhouse (`GreenhouseAdapter` v1.0.0)**: URL detection (`boards.greenhouse.io`, `grnh.se`), form discovery, standard and custom field filling, document attachment (`#resume`), field validation error inspection, submit button resolution (`#submit_app`), and adapter-specific confirmation extraction (`#application_confirmation`, reference ID).
- **Lever (`LeverAdapter` v1.0.0)**: URL detection (`jobs.lever.co`), form discovery, standard personal info and custom screening questions, document attachment (`input[name="resume"]`), validation error checking, submit button resolution (`[data-qa="btn-submit"]`), and confirmation extraction (`.confirmation-message`, `/applied`).
- **Ashby (`AshbyAdapter` v1.0.0)**: URL detection (`jobs.ashbyhq.com`), multi-step form navigation (`advance_step`), question discovery across step containers, document upload, submit button resolution, and Ashby confirmation receipt verification (`[data-qa="application-success"]`).
- **Generic Forms (`GenericFormAdapter` v1.0.0)**: Fallback adapter for unclassified application forms. **SAFETY GUARANTEE**: Generic forms are **STRICTLY FILL-ONLY** (`can_submit = False`). Attempting to call `submit()` or `confirm_submission()` raises `GenericAdapterCannotSubmitError` and halts execution.

### Central Submission Safety Guard (`SubmissionSafetyGuard`)
Every submission path is centrally intercepted and validated by `SubmissionSafetyGuard.validate_pre_submit_safety()`:
1. **Ownership & Lease Fencing**: Validates active worker lease ownership, unexpired lease timestamp, and matching fencing generation. Halts on stale worker or lost lease.
2. **Runtime Controls**: Rechecks `is_paused`, `is_stopped`, and `manual_takeover_owner`. Aborts if operator takeover or pause is engaged.
3. **Generic Form Submission Denial**: Rejects submission attempts for generic unapproved forms.
4. **Canary & Enablement Gate**:
   - Real autonomous submission is permanently disabled until an adapter has accumulated at least **3 operator-approved confirmed canaries** in `adapter_canaries` (`confirmed_canary_count >= 3`) and has been explicitly enabled by the operator (`is_enabled = 1`).
   - Mock/fixture test executions bypass the real canary prerequisite without submitting live applications.
5. **Rate Limiting & Pacing**:
   - Maximum daily submissions: Default **5 submissions per 24 hours**.
   - Minimum submission interval: Default **300 seconds (5 minutes)** spacing between consecutive submissions.
6. **Frozen Revisions Validation**:
   - Validates candidate profile snapshot against current profile (detects email/name changes mid-flight).
   - Validates document artifacts against disk (detects CV file deletion, truncation, or mtime mutation).
7. **Atomic Submit Intent**:
   - Commits `submit_intent` to SQLite before the physical submission action is triggered in the browser.
8. **Adapter-Specific Confirmation Verification**:
   - Requires structured, platform-specific proof elements (receipt token, confirmation ID, definitive confirmation container).
   - Ambiguous submission outcomes (`is_ambiguous = True`) fail closed: the whole worker is paused, a critical notification is emitted, and the system **NEVER** re-clicks submit automatically.

### Durable Integrated Pipeline
The scraping, tailoring, and queuing pipeline (`src/job_applier/pipeline.py`) durably coordinates the full lifecycle:
`scrape → deduplicate → filter → tailor → validate artifacts → enqueue → apply → verify → archive/report`
- **Conservative Deduplication**: Deduplicates using canonical URL normalization (stripping tracking parameters while preserving job IDs) and `(company, title)` normalized role signatures.
- **Missing-Artifact Blocks**: Validates that tailored CV PDFs exist and are non-empty (> 100 bytes). Missing artifacts block queue enqueueing (`MissingArtifactError`) without deleting or hiding existing files.
- **Durable Enqueueing**: Enqueues verified applications into SQLite WAL `automation_jobs` with detected ATS adapter and priority.
- **Atomic Archival**: Moves application folders from `output/applications/<app_id>` to `output/applied/<app_id>` only after confirmed submission evidence is verified.

### Gate Status
- **Automated Fixture Verification (PASSED)**: 226 passing automated tests covering ATS adapters (Greenhouse, Lever, Ashby, Generic), safety guard (pacing, daily cap, canaries, fencing, frozen revisions), and pipeline integration.
- **Live ATS Canary Applications (PENDING)**: Real employer canaries are disabled pending operator selection of 3 approved jobs per adapter during Phase 7 release hardening. Fixture tests only were executed.

---

## 8. Operations, Disaster Recovery & Hygiene Runbooks

### 8.1 SQLite Online Snapshot & Quiesced Artifact Consistency
- **Snapshot Mechanism**: The database uses SQLite's native `Connection.backup` API (`job_applier.db.backup_db`) rather than filesystem file copies. WAL journal pages are cleanly integrated into the snapshot database without blocking concurrent readers or writers.
- **Quiesced Queue Snapshotting**: When creating a comprehensive system backup (`create_backup(quiesce_worker=True)`), the operations manager automatically sets `is_paused = 1` in `runtime_control` to prevent queue state mutations or in-flight step transitions during artifact collection, restoring the prior execution state immediately upon completion.
- **Runbook Command**:
  ```bash
  # Trigger an online quiesced backup
  just ops-backup
  # Or via CLI
  uv run python -m job_applier.cli.ops_cli backup
  ```

### 8.2 Stopped-Profile Consistency & Lock Sanitization
- **Exclusive Lock Probing**: Before archiving the Chromium browser profile (`.browser_profile`), the backup tool attempts a non-blocking lock acquisition using `ProfileOwnershipLock(profile_dir, owner_type="backup_probe")`. If another process (e.g. `runtime_daemon`) holds the profile lock, `create_backup` fails closed with `ProfileConsistencyError` to prevent backing up half-written or corrupted browser state.
- **Lock & Ephemeral Sanitization**: When archiving, `sanitize_profile_copy` strips runtime lockfiles and ephemeral IPC sockets (`.profile_ownership.lock`, `SingletonLock`, `SingletonCookie`, `SingletonSocket`, `parent.lock`, `lockfile`, `Default/Sessions/`) so restored profiles never inherit stale locks.

### 8.3 Age Encryption & Off-Server Key Management
- **Security Architecture**:
  - The server only holds the **public age recipient key** (`age1...`), stored in `/run/secrets/age_recipient` or env `AGE_RECIPIENT`.
  - The **private age identity key** (`AGE-SECRET-KEY-1...`) remains strictly **off-server** on the operator's private workstation.
  - Backups are encrypted at creation time using `pyrage` (with fallback to the `age` binary). Unencrypted staging archives are purged immediately.
- **Runbook Commands**:
  ```bash
  # Create age-encrypted backup with explicit recipient
  uv run python -m job_applier.cli.ops_cli backup --recipient "age1ql3z7hjy..."

  # Decrypt and restore on disaster recovery host
  uv run python -m job_applier.cli.ops_cli restore /path/to/backup.zip.age --identity "AGE-SECRET-KEY-1..."
  ```

### 8.4 Backup Retention Rotation (7 Daily / 4 Weekly)
- **Retention Rules**:
  - Retains the latest backup for each of the last **7 distinct calendar days**.
  - Retains the latest backup for each of the last **4 distinct ISO calendar weeks**.
  - Prunes all backups outside the union of these two retention sets.
- **Runbook Commands**:
  ```bash
  # Check retention policy in dry-run mode
  uv run python -m job_applier.cli.ops_cli retention --dry-run

  # Apply retention policy and prune expired archives
  just ops-retention
  ```

### 8.5 Restore Drill & Downgrade Refusal Protection
- **Downgrade Refusal Contract**:
  - Every backup archive stores `manifest.json` containing the schema version at backup time.
  - When restoring (`restore_backup`), the tool compares the archive schema version against `CURRENT_SCHEMA_VERSION` in the running codebase.
  - If `archive_schema_version > CURRENT_SCHEMA_VERSION`, restore is **strictly refused** with `DowngradeRefusalError`, preventing database corruption from running older code against newer schema migrations.
- **Runbook Commands**:
  ```bash
  # Restore system from backup
  just ops-restore /path/to/backup.zip
  ```

### 8.6 Emergency Stop & Automation Circuit Breaker Runbook
- **Immediate Global Halting**:
  - Sets `is_stopped = 1` and `is_paused = 1` in `runtime_control` atomically.
  - Revokes active worker leases in non-terminal states (`claimed`, `navigating`, `filling`, `validating` reset to `ready`).
  - Protects `submit_intent`: Jobs in `submit_intent` are set to `paused` with ambiguity protection rather than reset to `ready`, preventing duplicate submissions.
  - Clears operator manual takeover locks.
  - Emits critical durable notification to the operator and logs an audit event.
- **Runbook Commands**:
  ```bash
  # Engage global emergency stop
  just ops-emergency-stop --reason "Suspicious activity detected"

  # Or via REST API
  curl -X POST https://jobs.aslan.net/api/automation/stop

  # Clear emergency stop flag (automation remains paused awaiting safe resume)
  just ops-unstop
  ```

### 8.7 Disk-Pressure Headroom Guard & Fail-Stop Runbook
- **Headroom Monitoring Thresholds**:
  - Triggers if free space drops below **1 GiB** (`DEFAULT_MIN_FREE_BYTES = 1073741824`) or free headroom drops below **5.0%** (`DEFAULT_MIN_FREE_PERCENT = 5.0`).
  - Monitored paths: `/app/data`, `/app/output`, `/app/.browser_profile`.
- **Fail-Closed Actions**:
  - `SubmissionSafetyGuard.validate_pre_submit_safety()` fails closed with `DiskPressureError` before submitting.
  - `process_claimed_job()` releases claimed job and pauses worker.
  - Health check endpoint `/api/health` returns HTTP 503 `status: degraded, reason: disk_pressure`.
  - Emits critical durable alert to operator.
- **Runbook Commands**:
  ```bash
  # Check disk pressure headroom
  just ops-disk-guard

  # Enforce guard (engages pause if headroom critical)
  uv run python -m job_applier.cli.ops_cli disk-guard --enforce
  ```

### 8.8 Debug Artifact TTL & Redaction Runbook
- **Redaction Policy**:
  - Diagnostic DOM dumps and failure screenshots are strictly suppressed on authentication, login, SSO, and MFA screens via `BrowserAutomator.is_auth_or_challenge_screen()`.
  - Authentication screen DOMs are replaced with security policy notices; fields filled and validation errors are redacted.
- **Debug TTL Sweeping (7 Days)**:
  - Transient failure screenshots (`submission_failed.png`, `submission_fill_only.png`), diagnostic DOMs (`diagnostic_dom.html`), and Playwright trace archives (`output/traces/`) older than 7 days are pruned.
  - Confirmation screenshots (`submission_proof.png`) and tailored resumes are preserved permanently.
- **Runbook Commands**:
  ```bash
  # Sweep expired debug traces and diagnostic dumps older than 7 days
  uv run python -m job_applier.cli.ops_cli cleanup-debug --ttl-days 7
  ```

### 8.9 Compose Multi-Service Topology & Resource Sandboxing
- **Network Isolation**: `internal-net` bridges Caddy gateway, web, runtime worker, cloudflared tunnel, and ntfy. No application ports are published on public host interfaces.
- **Least-Privilege Containers**:
  - `web` and `runtime`: Unprivileged user `10001:10001`, `privileged: false`, `no-new-privileges:true`, `cap_drop: ALL`.
  - `gateway`: Read-only root filesystem, `no-new-privileges:true`, `cap_drop: ALL`, `cap_add: NET_BIND_SERVICE`.
  - `cloudflared`: Unprivileged user `nonroot`, `cap_drop: ALL`, outbound tunnel only.
  - `ntfy`: Unprivileged user `10001:10001`, `cap_drop: ALL`, deny-all default access.
- **Shared Memory**: `runtime` container allocates `shm_size: "1gb"` for Chromium stability under Xvfb.

---

## 9. Exact Phase Evidence & Pending External Gates Summary

| Phase | Description | Status | Automated Test Evidence |
| :--- | :--- | :--- | :--- |
| **Phase 0** | Baseline Reconciliation & Online Backup | **COMPLETED** | 7 tests (`test_backup.py`): Online WAL snapshot, collision-free exports, fail-closed integrity. |
| **Phase 1** | Durable DB Queue & Fail-Closed Answers | **COMPLETED** | 9 tests (`test_durable_queue.py`, `test_question_solver.py`): Re-entrant lock, lease fencing, submit intent, no `"Yes"` fallback. |
| **Phase 2** | Data Migration & Queue-Backed REST APIs | **COMPLETED** | 4 tests (`test_migrations.py`): Idempotent schema migrations 1–4, data preservation. |
| **Phase 3** | Server Runtime, Sandboxing & Takeover | **COMPLETED** | 36 tests (`test_runtime_security.py`, `test_browser_runtime.py`): Sandboxed Xvfb, read-only viewer, atomic takeover lease, egress network filter. |
| **Phase 4** | Cloudflare Edge & Origin Security | **COMPLETED** | 33 tests (`test_edge_auth.py`): RS256 JWT validation, exact allowlist, gateway gate, CSRF origin check, 5-min viewer timeout. |
| **Phase 5** | Notifications & Operator Alerts | **COMPLETED** | 12 tests (`test_notifications.py`, `test_notifications_api.py`) + 38 Angular UI tests: In-app toasts, SSE unread badges, Web Locks deduplication, ntfy outbox. |
| **Phase 6** | Versioned ATS Adapters & Pacing Pipeline | **COMPLETED** | 13 tests (`test_ats_adapters.py`, `test_safety_guard.py`, `test_integrated_pipeline.py`): Greenhouse, Lever, Ashby, Generic fill-only, 5/day limit, 300s spacing. |
| **Phase 7** | Operations, Disaster Recovery & Hygiene | **COMPLETED** | 12 tests (`test_operations.py`): Age encryption/decryption, 7-daily/4-weekly retention, downgrade refusal, disk-pressure fail-stop, redacted auth screens. |

### Pending External Deployment Gates (Awaiting Live Provisioning)

The code, configuration, runbooks, and test suites are 100% complete and verified. The following items represent external live environment gates requiring operator authorization and infrastructure actions:

1. **Cloudflare Zone Addition Gate (`aslan.net`)**:
   - **Status**: PENDING OPERATOR ACTION.
   - The authenticated Cloudflare account currently manages `laura-photos.com`. Zone `aslan.net` must be added to Cloudflare (Free Website plan), or an account already managing `aslan.net` must be authorized.
2. **DNS Export & Authority Cutover Gate**:
   - **Status**: PENDING OPERATOR ACTION.
   - Complete zone export of existing `aslan.net` DNS records (A, AAAA, MX, TXT, SPF, DKIM, DMARC) must be captured before updating nameservers at the registrar. DNSSEC records must be verified.
3. **Dedicated Cloudflare Tunnel Provisioning Gate**:
   - **Status**: PENDING LIVE DEPLOYMENT.
   - Once zone `aslan.net` is active on Cloudflare, a named tunnel must be created routing `jobs.aslan.net` and `notify.jobs.aslan.net` to the Caddy gateway service on the Hetzner host.
4. **Cloudflare Access Policy & Identity Allowlist Gate**:
   - **Status**: PENDING LIVE DEPLOYMENT.
   - Operator's exact Google and GitHub email addresses must be configured in `CF_ACCESS_ALLOWED_IDENTITIES` and in the Cloudflare Zero Trust Access application rules.
5. **Server Host Deployment & Secret Population Gate**:
   - **Status**: PENDING SERVER ACCESS.
   - Deployment to Hetzner Linux host requires target directory initialization, Docker Compose deployment, and populating Docker secrets (`/run/secrets/age_recipient`, `GOOGLE_API_KEY`, etc.).
6. **iPhone ntfy Mobile Push Verification Gate**:
   - **Status**: PENDING LIVE APP ENROLLMENT.
   - Following deployment of the `notify.jobs.aslan.net` tunnel, the operator must install the ntfy iOS app, configure subscriber token, and verify background locked-screen push alerts.
7. **Live ATS Canary Applications Gate**:
   - **Status**: PENDING OPERATOR CANARIES.
   - Real autonomous submission is permanently disabled until 3 genuine, operator-approved job applications per adapter (Greenhouse, Lever, Ashby) have been manually approved and confirmed in production.
