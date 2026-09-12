# Revised Plan: Secure Job Application Automation with Dashboard Notifications

## Summary and fixed decisions

Build a single-user service on the Hetzner Linux server. The server runs Chromium and the automation pipeline; the Mac accesses the dashboard, receives alerts, and optionally watches or controls the protected browser view. Closing the Mac does not stop automation.

- Canonical URL: `https://jobs.aslan.net/`, without a public `/job-applier` prefix.
- Ingress: dedicated Cloudflare Tunnel record, replacing the earlier CNAME-to-`aslan.archnet.lol` proposal.
- DNS: add `aslan.net` to the authorized Cloudflare account, preserving existing services.
- Identity: exact allowlisted Google or GitHub identity through Access; both upstream accounts have MFA. Alternative login providers are not two-factor authentication.
- Runtime: Docker Compose, one worker, headed Chromium, persistent profile, noVNC.
- State: SQLite on local persistent storage; database-backed queue and preserved artifact volumes.
- Exceptions: pause the whole worker for CAPTCHA, MFA, unknown questions, or uncertain submissions. No automated actions during operator takeover.
- Notifications: live dashboard alerts, optional native notifications in the dashboard’s browser, and self-hosted ntfy for Mac/iPhone.
- Submission: only explicitly enabled, tested ATS adapters may submit.
- Cost: no paid Cloudflare or notification subscription. Existing hosting, domain, and AI costs remain. Verify free eligibility before provisioning; never purchase or weaken protections to overcome a blocker.

Implementation remains stopped pending implementation authorization. Inspect any partial changes before resuming. This replaces all earlier plans.

## Architecture and routing

Compose services:

1. **cloudflared:** outbound-only connector.
2. **gateway:** internal Caddy proxy, including origin authentication for browser HTTP/WebSocket traffic.
3. **web:** FastAPI/Angular, identity, queue, control APIs, SSE, artifacts, preferences, and exception handling.
4. **runtime:** Python worker, local Playwright/Chromium, Xvfb, x11vnc, websockify. No network CDP endpoint.
5. **ntfy:** persistent token-authenticated notification service.

Preserve existing data/output/profile volumes or migrate them explicitly. Runtime exclusively owns the profile; CLI automation refuses a profile owned by the service. Web controls execution only through durable commands.

Playwright controls Chromium locally and uploads server-side PDFs directly. VNC binds to loopback with authentication; viewer transport is available only on the restricted internal network and through the authenticated gateway.

Public paths are `/`, `/api`, `/files`, and `/browser`. The Tunnel targets the internal gateway directly. Remove the legacy content route at `aslan.archnet.lol/job-applier` or make it redirect-only; it must not bypass Access. Preserve unrelated host services and firewall rules. Use explicit `PUBLIC_ORIGIN`, trusted proxy settings, and deployment-specific configuration rather than hard-coded hostnames in generic package defaults.

## Phase 0 — Reconcile and protect existing work

- Inspect partial edits, staged files, feature branches, running services, schema, volumes, and baseline tests. Preserve unrelated work.
- Integrate reviewed OAuth/runtime changes on a dedicated integration branch; avoid blind merges or unexpected worktree switches.
- Take an SQLite online backup and consistent artifact snapshot. Stop Chromium for profile backup. Encrypt with an age recipient whose recovery private key is held off-server.
- Establish one production volume set and prevent duplicate deployments consuming it.

**Gate:** recoverable backup, explicit schema/volume inventory, baseline failures recorded, and no unexplained partial edits.

## Phase 1 — Durable execution and safe answers

Keep application lifecycle status separate from automation execution state.

Add versioned tables for jobs, attempts, ordered events, scoped approved answers, persistent runtime controls/manual ownership, and a notification outbox. Jobs carry lease owner, fencing generation, deadline, checkpoint, adapter version, retry schedule, and cancellation. Attempts record immutable submission intent, profile/artifact revisions, outcome, confirmation, and ambiguity.

State flow:

`ready → claimed → navigating → filling → validating → submit_intent → verifying → applied`

Typed exceptions cover authentication, MFA, CAPTCHA, unknown questions, site changes, ambiguous submissions, safe retry, cancellation, and permanent failure.

- Atomically claim through SQLite transactions. Default lease: 60 seconds; heartbeat: 10 seconds.
- Enforce one runtime process with an OS lock plus generation-fenced database ownership. A lease alone cannot stop a stale process from clicking.
- Recheck ownership/pause before actions. Stop acting when ownership is lost.
- Persist `submit_intent`, document/profile revisions, and minimal audit before clicking Submit.
- Recovery from intent/verifying reconciles evidence or pauses as ambiguous; never automatically clicks again.
- Retry only proven-safe pre-submit failures: at most three retries at 30 seconds, 2 minutes, and 10 minutes.
- A stop cannot undo an external request already sent; record uncertainty honestly.

Remove default `Yes`/first-option answers immediately. Unknowns return typed exceptions. AI may map approved facts and draft grounded prose, but cannot invent eligibility, credentials, legal statements, or preferences. Treat page content as untrusted data, not instructions; never expose credentials or tokens to the model.

Freeze candidate/document revisions per attempt. All mandatory answers, attached CV, policy compatibility, validation, and duplicate/uncertain-history checks must pass before submission.

**Gate:** crash, fencing, ambiguity, and unsafe-answer tests pass.

## Phase 2 — Data migration and API transition

- Use explicit schema migrations; refuse a newer unsupported schema. No destructive import-time migration.
- Reconcile DB, CSV, and folders idempotently. Preserve completed/uncertain history even when pending folders remain.
- Prefer platform job identifiers and conservative URL normalization. Keep meaningful query parameters; conflicts require review rather than silent merging.
- Missing artifacts block enqueueing without hiding applications. Never delete unmatched files automatically.
- Make listings/stats database-backed; folders remain artifacts. Move/archive files only after confirmation, restart-safely.
- Retain compatible listing shapes. Apply endpoints return `202` and job IDs with idempotency support.
- SSE uses durable monotonic IDs and `Last-Event-ID`; an expired cursor triggers snapshot reconciliation.

**Gate:** repeatable migration, preserved IDs/history, and tested restore. Older binaries never run blindly against a newer schema.

## Phase 3 — Secure runtime and takeover

- Launch headed Chromium through local Playwright under Xvfb; keep sandboxing enabled.
- Pin compatible versions; non-root, dropped capabilities, bounded resources, minimal writable mounts, no privileged mode/Docker socket/production `--no-sandbox` fallback.
- Strip inherited secrets from the browser environment and use per-service secret files. Compose secrets are not encryption at rest.
- Enforce a fail-closed outbound proxy/network policy for browser traffic: deny private, loopback, link-local, metadata, and IPv6 equivalents; validate DNS/redirects/subresources and block direct egress bypass. Test DNS rebinding.
- Persist sessions. Initial logins/challenges occur through the viewer. Credential autofill is limited to configured adapters and exact approved HTTPS login origins with explicitly supplied secrets.
- Viewer is read-only by default. Taking control atomically pauses automation and obtains an exclusive lease. Resume releases takeover and revalidates the page, authentication, ownership, and submission checks.

**Gate:** upload, persistence, sandbox, crash, network denial, and takeover tests pass before public exposure.

## Phase 4 — Cloudflare and origin authentication

### DNS/cost prerequisites

Verify domain control, current free-plan eligibility, and IdP support. Export the complete current zone; record discovery is not a full backup. Preserve mail, CAA, TXT verification, delegations, and proxy/DNS-only behavior.

Handle DNSSEC explicitly: safely remove obsolete DS records before migration when required, then enable Cloudflare DNSSEC and publish the new DS after activation. Test unrelated websites/mail after cutover. Stop if authority or free-service requirements are unavailable.

### Security boundary

- Named Tunnel and whole-host Access application with exact identities, Google/GitHub only, no broad organization/domain grants or email-OTP fallback.
- Eight-hour Access session; upstream MFA required. Do not claim verified MFA context unless supported evidence exists.
- Validate JWT algorithm, signature, issuer, audience, expiry, identity; bounded JWKS caching/refresh, fail closed on invalid configuration or unknown keys.
- FastAPI validates app requests. Gateway authorization validates viewer HTTP and WebSocket upgrades before forwarding.
- Exact Origin checks for mutations/viewer upgrades, session-bound CSRF tokens, secure cookies where used, and no credentialed cross-origin CORS.
- Viewer connections end at token expiry or five minutes and reauthenticate. SSE also respects expiry and reconnects through authentication. Bound identity-removal/logout effects accordingly.
- Minimal private health checks; no public application, CDP, VNC, or noVNC ports. Reject unexpected Host and untrusted forwarding headers.

**Gate:** tests reject forged/expired identities, CSRF, cross-origin WebSockets, legacy-origin bypass, and direct runtime access; verify no paid subscription.

## Phase 5 — Dashboard-browser notifications and ntfy

### Dashboard alerts: enabled by default

Use the existing authenticated SSE stream; no separate polling-based notification service.

- Immediate in-app toast for new exceptions and important failures.
- Persistent exception banner and unread badge until acknowledged/resolved; a dismissed toast must not hide paused work.
- Routine progress remains in the activity feed. Confirmed applications may show a lightweight in-app success toast, but must not generate a default OS alert for every event.
- Provide explicit acknowledgement separate from job resolution. Acknowledging a notification never resumes automation.
- Add authenticated `GET /api/notifications?after=<id>` and idempotent `POST /api/notifications/{id}/ack`. Store notification identity, associated event/job, severity, creation time, and acknowledgement in the database.
- On reconnect, reconcile durable alerts and the current exception snapshot. Update the banner without flooding the user with stale native notifications.

### Native notifications in the dashboard’s browser: opt-in

- Add an **Enable browser notifications** button in settings. Request permission only from its click handler, over HTTPS; never prompt automatically at startup.
- Feature-detect support and permission. If denied or unsupported, keep in-app alerts and explain how to use ntfy/browser settings; never repeatedly prompt.
- Use the browser Notifications API for new actionable exceptions/system failures. Suppress native duplicates while the dashboard is visibly focused; show in-app alerts instead.
- Notification click focuses an existing same-origin dashboard tab or opens the canonical exception URL. It never submits, resumes, or acknowledges work automatically.
- Preview text contains only a generic exception category and opaque reference—no employer names, resumes, answers, or credentials.
- Add optional sound, off by default and unlocked through an explicit user interaction. Respect mute settings and avoid repeated sounds for the same unresolved exception.
- Native notification and sound preferences are per browser. Server acknowledgement state is shared across clients.
- Deduplicate by event ID and notification `tag`. Use a cross-tab coordinator through Web Locks where available, with BroadcastChannel/local-storage coordination fallback. Tabs still display the persistent paused state; only the elected notifier creates native alerts/sound. Fallback guarantees are best-effort and tested.
- No Web Push/service worker background-delivery system in this release. Native alerts require the dashboard to remain loaded and connected; background throttling may delay delivery. Closing the browser is not supported by this channel.

### ntfy: offline/mobile channel

Host `notify.jobs.aslan.net` through the Tunnel with native token authentication, deny-all anonymous ACLs, topic-restricted publisher/subscriber tokens, and rate limits. Do not place interactive Access redirects in front of native app endpoints.

Send generic exception category, opaque attempt ID, and authenticated dashboard link from the durable outbox. Avoid candidate/employer data. Failed delivery is visible and retries without unpausing automation. Dashboard acknowledgement need not retract an already delivered push.

Configure and verify the free iOS relay required by self-hosted ntfy; document relay metadata exposure. If reliable acceptable free delivery cannot be established, mark this gate blocked rather than buy a subscription or claim success. Test locked/background iPhone delivery and Mac notification permissions.

### Exception behavior

Any actionable exception pauses the entire worker and emits one logical alert fanned out to dashboard and ntfy. Preserve the page best-effort; do not promise MFA/session validity indefinitely. Explicit resolution is required. After expiry/restart, reopen login or resume a verified checkpoint safely.

**Gate:** live dashboard alerts, permission allowed/denied/unavailable, hidden/focused tab, multiple tabs, reconnect, logout, sound opt-in, notification click, ntfy failure, and locked iPhone delivery all behave as specified. No alert action accidentally submits or resumes.

## Phase 6 — ATS adapters and integrated pipeline

Typed adapter contract: detect, authenticate, discover steps/questions, fill, attach, validate, submit, and confirm. Return structured outcomes rather than prose.

- Initial adapters: Greenhouse, Lever, Ashby; promote individually.
- Generic forms are fill-only and cannot submit.
- SmartRecruiters, Workday, and job boards follow later with explicit tests/platform permission.
- Confirmation must be adapter-specific and associated with the attempt; generic success text or redirect alone is insufficient.
- Fixtures cover employer variants, multi-page forms, custom required questions, rejected uploads, stale jobs, and ambiguous confirmation.

Durably connect:

`scrape → deduplicate → filter → tailor → validate artifacts → enqueue → apply → verify → archive/report`.

Persist stage outputs to avoid repeated work/API costs after restart. Default cap: five submissions daily, minimum five minutes between attempts; lower site limits take precedence. Unknown eligibility and unsupported forms remain blocked.

**Gate:** mock end-to-end tests pass; every submit path passes the centralized gate; no CI test applies to real employers.

## Phase 7 — Canary, hardening, and release

- At least three operator-approved genuine-job canaries per adapter, each with explicit pre-submit approval and confirmed outcome; no duplicates or incorrect answers.
- Enable autonomous mode per adapter only after canaries. Raising the five-per-day limit is a separate action.
- Metrics distinguish attempted, confirmed, ambiguous, and paused work; include queue age, adapter version, crashes, lease/notification failures, and disk pressure.
- Persist minimal redacted audit. Disable screenshots/traces during login/MFA. Mask sensitive values before capture; unsanitizable traces/HTML remain off by default. Explicit debug capture expires after seven days.
- Daily encrypted backups: seven daily and four weekly versions. Quiesce pipeline for artifact consistency and stop Chromium for profile backup. Restore drill before release.
- Pin/scan dependencies and images; document credential rotation, tunnel revocation, disk-pressure fail-stop, emergency stop, and rollback.

## Verification and delivery

Run Python lint/type/tests, Angular checks/build, Compose validation, container smoke tests, and independent security/correctness review. Include:

- Canonical root routing, files, SSE, viewer, and legacy-origin rejection.
- IdP/JWT/CSRF/WebSocket expiry and no notification data exposure after logout.
- Fencing, stale processes, crash around submit intent, cancellation, duplicate identity.
- Migration conflicts, artifact loss, repeated import, downgrade refusal, restore.
- Unknown/sensitive answers, hostile page instructions, malformed AI output, frozen profile revisions.
- Sandbox, SSRF/private network/DNS rebinding, secret handling, profile locks.
- ATS fixtures and approved canaries.
- Dashboard alert persistence, permission states, cross-tab deduplication, reconnect and focus behavior, sound, Mac browsers, iPhone ntfy, takeover, and MFA expiry.

Report what actually ran, what failed, and which deployment/manual gates remain pending. Do not describe a click as an application or a configured notification channel as verified delivery.

## Limits and required deployment inputs

Supported applications can run unattended; universal site coverage and external exactly-once submission cannot be guaranteed. Ambiguous outcomes stop rather than risk duplicates. CAPTCHA bypass and fabricated candidate claims are excluded. Host root is trusted; distributed workers and concurrent browser sessions are deferred.

Cloudflare is authorized, but DNS export/control, server access, IdP configuration, allowlisted identities, and device enrollment must be verified before live changes. These inputs never authorize guessing, purchasing products, bypassing authentication, or submitting unapproved canary applications.
