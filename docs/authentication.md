# Authentication & Browser Sessions

Applying directly to job boards like LinkedIn Easy Apply, BestJobs internal postings, eJobs, or Workday portals requires active user accounts.

`job-applier` uses isolated persistent browser profiles (`.browser_profile/` for Chrome/Chromium, `.browser_profile_firefox/` for Firefox) so you only log in once.

---

## 1. How It Works

1. **Persistent Profiles (`.browser_profile/` and `.browser_profile_firefox/`)**:
   Playwright and native browsers launch pointing to the engine-specific profile directory (`.browser_profile/` for Chrome/Chromium, `.browser_profile_firefox/` for Firefox, or `JOB_APPLIER_PROFILE_DIR` override). All cookies, auth tokens (`li_at`, `auth_csrfToken`, session cookies), and storage are preserved permanently on disk.
2. **Safe Lock Handling**:
   Chrome creates a symlink `SingletonLock -> hostname-PID`. When checking locks on Chromium profiles, `clean_stale_chrome_locks` uses a non-signaling existence check (`os.kill(pid, 0)`). It **never kills running processes**, only unlinking broken symlinks left behind by dead processes. Firefox profiles are managed independently without Chromium-specific locks.

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
3. A browser window opens on your active graphical display (Wayland/X11 on Linux, native windowing on macOS/Windows).
4. Enter your credentials, complete 2FA or email verification, and close the window.
5. All session tokens are saved directly into `.browser_profile/`.

### From the CLI

```bash
# Check status (default engine):
python src/job_applier/cli/auth_cli.py status

# Check status for Firefox:
python src/job_applier/cli/auth_cli.py status --browser firefox

# Launch guided login for Chrome:
python src/job_applier/cli/auth_cli.py login bestjobs --browser chrome

# Launch guided login for Firefox:
python src/job_applier/cli/auth_cli.py login linkedin --browser firefox
```

---

## 4. Firefox Authentication & Cookie Sync Limitation

- **Separate Profile Directory:** Firefox maintains an isolated profile at `.browser_profile_firefox/`.
- **Manual Interactive Login:** Desktop Chrome cookie synchronization (`/api/auth/sync-chrome`) is Chrome-specific due to differing database schemas and cookie encryption formats. For Firefox, use the **Connect / Log In** button (with Firefox selected) in the dashboard or run `auth_cli.py login <platform> --browser firefox` to log in directly and save tokens permanently.

---

## 4. In-Flight Verification Codes (2FA / Email OTP)

When submitting an application that sends a one-time verification code to your email or phone:

- **In the Web Dashboard:** The Live HUD displays an amber challenge card with an input box: `[ Enter code ] [ Submit Code ]`. Submitting pushes the code via `POST /api/automation/submit-code`, fills it into the active browser, and confirms submission.
- **In the CLI:** The terminal pauses and prompts `🔑 Enter verification code:`, then proceeds automatically.
