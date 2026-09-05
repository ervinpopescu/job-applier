# Authentication Architecture

`job-applier` contains two distinct authentication systems serving separate purposes:

1. **Dashboard Access Control (OAuth & Signed Sessions):** Protects the web dashboard, queue, applicant profiles, and generated CV files from unauthorized public access using Google and/or GitHub OAuth.
2. **Platform & Browser Automation Sessions (`.browser_profile/`):** Preserves persistent browser session cookies for job portals (LinkedIn, BestJobs, eJobs, Workday) used during automated application submission.

---

# Part I: Dashboard Access Control (OAuth & Session Cookies)

When deploying `job-applier` to a remote server, VPS, or behind a reverse proxy, you can enable built-in production-grade Google and GitHub OAuth authentication.

By default, `APP_AUTH_ENABLED=false` so local single-user development installations work without any OAuth configuration.

## 1. How Dashboard Auth Works

- **Fail-Closed Security:** When `APP_AUTH_ENABLED=true`, the application validates that `APP_SESSION_SECRET` is strong (at least 32 characters) and at least one provider (Google or GitHub) is configured. If either condition fails, the backend refuses to start and fails closed with a clear error.
- **Route Protection:** Sensitive `/api/*` endpoints and `/files/*` application package storage require an authenticated session and return HTTP 401 JSON when accessed without credentials. Health check (`/api/health`), login flows, auth status (`/auth/status`), and static single-page application assets are exempt.
- **Signed Session Cookies:** Authentication uses Starlette `SessionMiddleware` signed with `APP_SESSION_SECRET`. Cookies are configured with `HttpOnly`, `SameSite=Lax`, bounded expiry (default 14 days), and `Secure` whenever running over HTTPS.
- **Zero Token Leakage:** Only minimal identity claims (`provider`, `id`, `email`, `name`, `username`, `avatar_url`) are persisted in the session. Access tokens and refresh tokens from upstream providers are never stored in cookies.
- **Identity Verification:**
  - Google: Identity is verified via the official OpenID Connect userinfo endpoint. Only accounts with `email_verified=true` are permitted.
  - GitHub: Identity is verified via the official GitHub `/user` and `/user/emails` endpoints, resolving a verified primary email address.
- **Allowlists:** Optional allowlists restrict access to authorized Google email addresses and/or GitHub usernames.

---

## 2. Configuration & Environment Variables

Add the following to your `.env` file or container environment:

```dotenv
# Enable dashboard access control
APP_AUTH_ENABLED=true

# Strong session secret (at least 32 characters long)
# Generate with: openssl rand -base64 32
APP_SESSION_SECRET=your-secure-random-secret-key-at-least-32-chars

# Explicit public URL of your dashboard (scheme + host + optional subpath)
# E.g. https://jobs.example.com or https://example.com/job-applier or http://127.0.0.1:8000
# Used for exact OAuth callback URLs and redirects behind reverse proxies; never trusts Host headers.
APP_PUBLIC_URL=https://jobs.example.com

# Session cookie settings (optional customization)
APP_SESSION_COOKIE_NAME=job_applier_session
APP_SESSION_MAX_AGE=1209600
# APP_SESSION_COOKIE_SECURE=true  # Automatically enabled if APP_PUBLIC_URL starts with https://

# Google OAuth (optional if GitHub is configured)
GOOGLE_CLIENT_ID=your-google-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-google-client-secret
AUTH_ALLOWED_GOOGLE_EMAILS=user@example.com,admin@example.com

# GitHub OAuth (optional if Google is configured)
GITHUB_CLIENT_ID=your-github-client-id
GITHUB_CLIENT_SECRET=your-github-client-secret
AUTH_ALLOWED_GITHUB_USERS=octocat,your-github-username
```

---

## 3. Provider Console Setup

### A. Google Cloud Console

1. Navigate to the [Google Cloud Console](https://console.cloud.google.com/) and create or select a project.
2. Go to **APIs & Services > OAuth consent screen**.
   - Select **External** (or Internal for Google Workspace).
   - Fill in application name (e.g. `JobApplier`) and support email.
   - Add scopes: `.../auth/userinfo.email` and `.../auth/userinfo.profile`.
3. Go to **APIs & Services > Credentials > Create Credentials > OAuth client ID**.
   - Application type: **Web application**.
   - Name: `JobApplier Web Dashboard`.
   - **Authorized redirect URIs:** Add the exact callback URL based on your `APP_PUBLIC_URL`:
     - Standard origin: `https://jobs.example.com/auth/callback/google`
     - Subpath origin: `https://example.com/job-applier/auth/callback/google`
     - Local development: `http://127.0.0.1:8000/auth/callback/google`
4. Copy the generated **Client ID** and **Client Secret** into your `.env`.

### B. GitHub Developer Settings

1. Navigate to [GitHub Settings > Developer settings > OAuth Apps](https://github.com/settings/developers).
2. Click **New OAuth App**.
   - Application name: `JobApplier`.
   - Homepage URL: `https://jobs.example.com` (or your `APP_PUBLIC_URL`).
   - **Authorization callback URL:** Add the exact callback URL based on your `APP_PUBLIC_URL`:
     - Standard origin: `https://jobs.example.com/auth/callback/github`
     - Subpath origin: `https://example.com/job-applier/auth/callback/github`
     - Local development: `http://127.0.0.1:8000/auth/callback/github`
3. Click **Register application**.
4. Generate a new client secret. Copy the **Client ID** and **Client Secret** into your `.env`.

---

## 4. Subpath Deployment & Reverse Proxies

When hosting JobApplier under a reverse proxy subpath (e.g. `https://example.com/job-applier/`):

- Configure `APP_PUBLIC_URL=https://example.com/job-applier`.
- The backend automatically registers both root `/auth/*` and subpath `/job-applier/auth/*` routes, generating redirects and callbacks with the subpath prefix.
- The Angular SPA automatically resolves all API, file, login, callback, and logout URLs relative to the document base path.
- In Nginx or Caddy, pass requests to the backend while preserving the request path:
  ```nginx
  location /job-applier/ {
      proxy_pass http://127.0.0.1:8000/job-applier/;
      proxy_set_header X-Forwarded-Proto $scheme;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
  }
  ```

---

# Part II: Platform & Browser Automation Sessions (`.browser_profile/`)

Applying directly to job boards like LinkedIn Easy Apply, BestJobs internal postings, eJobs, or Workday portals requires active user accounts.

`job-applier` uses an isolated persistent Chrome profile (`.browser_profile/`) so you only log in once.

## 1. How It Works

1. **Persistent Profile (`.browser_profile/`)**:
   Playwright and Google Chrome are launched pointing to `.browser_profile/` as their `user_data_dir`. All cookies, auth tokens (`li_at`, `auth_csrfToken`, session cookies), and localStorage are preserved permanently on disk.
2. **Safe Lock Handling**:
   Chrome creates a symlink `SingletonLock -> hostname-PID`. When checking locks, `clean_stale_chrome_locks` uses a non-signaling existence check (`os.kill(pid, 0)`). It **never kills running processes**, only unlinking broken symlinks left behind by dead processes.

---

## 2. Syncing from Desktop Chrome (Recommended)

If you are already logged into Google, LinkedIn, or BestJobs in your regular desktop Chrome browser:

### From the Web Dashboard

1. Open the dashboard at `http://127.0.0.1:8000`.
2. Click **`Logins & Auth`** in the top navigation bar.
3. Click **`1-Click Sync Sessions from Desktop Chrome`**.
4. The system automatically inspects your desktop Chrome profiles (`Profile 5` and `Default`) and copies your session cookies directly into `.browser_profile/`.

---

## 3. Manual Headed Login (Native Chrome)

If you need to log in to a platform for the first time:

### From the Web Dashboard

1. Click **`Logins & Auth`** in the navbar.
2. Click **Connect / Log In** for the desired platform (LinkedIn, BestJobs, eJobs, or Google).
3. A native Google Chrome window opens on your screen on `DISPLAY=:20` without automation flags.
4. Enter your credentials, complete 2FA or email verification, and close the window.
5. All session tokens are saved directly into `.browser_profile/`.

### From the CLI

```bash
# Check status:
python src/job_applier/cli/auth_cli.py status

# Launch guided login:
python src/job_applier/cli/auth_cli.py login bestjobs
python src/job_applier/cli/auth_cli.py login linkedin
python src/job_applier/cli/auth_cli.py login ejobs
```

---

## 4. In-Flight Verification Codes (2FA / Email OTP)

When submitting an application that sends a one-time verification code to your email or phone:

- **In the Web Dashboard:** The Live HUD displays an amber challenge card with an input box: `[ Enter code ] [ Submit Code ]`. Submitting pushes the code via `POST /api/automation/submit-code`, fills it into the active browser, and confirms submission.
- **In the CLI:** The terminal pauses and prompts `🔑 Enter verification code:`, then proceeds automatically.
