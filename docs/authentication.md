# Authentication & Browser Sessions

Applying directly to job boards like LinkedIn Easy Apply, BestJobs internal postings, eJobs, or Workday portals requires active user accounts.

`job-applier` uses an isolated persistent Chrome profile (`.browser_profile/`) so you only log in once.

---

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
