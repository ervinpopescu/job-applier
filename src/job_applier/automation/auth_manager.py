from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    load_candidate_profile,
)
from job_applier.automation.cloudflare import (  # type: ignore[import-not-found]
    VirtualDisplayManager,
)
from job_applier.config import (  # type: ignore[import-not-found]
    get_platforms_config,
)
from job_applier.logger import log_event  # type: ignore[import-not-found]
from job_applier.utils import get_project_root

PLATFORM_LOGIN_URLS: dict[str, str] = get_platforms_config().get("login_urls", {})


def clean_stale_chrome_locks(profile_dir: Path) -> None:
    """Removes stale Chrome lock symlinks ONLY if the holding process is confirmed dead."""
    lock_path = profile_dir / "SingletonLock"
    if lock_path.is_symlink() or lock_path.exists():
        try:
            target = os.readlink(str(lock_path))
            parts = target.rsplit("-", 1)
            if len(parts) == 2 and parts[1].isdigit():
                pid = int(parts[1])
                try:
                    os.kill(pid, 0)
                    # Process is still actively running — do NOT kill or touch it!
                    return
                except OSError:
                    # Process is confirmed dead — safe to remove stale symlink
                    lock_path.unlink(missing_ok=True)
            else:
                lock_path.unlink(missing_ok=True)
        except OSError:
            lock_path.unlink(missing_ok=True)

    # Clean leftover socket symlinks only if lock is gone
    if not lock_path.is_symlink() and not lock_path.exists():
        for name in ["SingletonCookie", "SingletonSocket", "lockfile"]:
            lp = profile_dir / name
            try:
                if lp.is_symlink() or lp.exists():
                    lp.unlink(missing_ok=True)
            except OSError:
                pass


def get_cookie_db_path(profile_dir: Path) -> Path | None:
    """Finds SQLite Cookies file in standard Chrome layout."""
    for cand in [
        profile_dir / "Default" / "Cookies",
        profile_dir / "Default" / "Network" / "Cookies",
        profile_dir / "Cookies",
    ]:
        if cand.exists():
            return cand
    return None


def inspect_profile_cookies(profile_dir: Path) -> dict[str, bool]:
    """Inspects the actual Chrome cookie database to check real platform login status."""
    cookie_db = get_cookie_db_path(profile_dir)
    status: dict[str, bool] = {
        "linkedin": False,
        "bestjobs": False,
        "ejobs": False,
        "google": False,
    }
    if not cookie_db or not cookie_db.exists():
        return status

    tmp_db = Path(f"/tmp/chk_auth_{os.getpid()}.db")
    try:
        shutil.copyfile(cookie_db, tmp_db)
        conn = sqlite3.connect(tmp_db)
        c = conn.cursor()
        c.execute("SELECT host_key, name FROM cookies")
        rows = c.fetchall()
        conn.close()

        for host, name in rows:
            h = host.lower()
            n = name.lower()
            if "linkedin.com" in h and name in ["li_at", "liap"]:
                status["linkedin"] = True
            if "bestjobs.eu" in h and (
                "token" in n
                or "auth" in n
                or "session" in n
                or name in ["auth_csrfToken", "tracking_sid"]
            ):
                status["bestjobs"] = True
            if "ejobs.ro" in h and (
                "token" in n or "auth" in n or "user" in n or name == "logged_in"
            ):
                status["ejobs"] = True
            if "google.com" in h and name in ["SID", "SSID", "SAPISID", "HSID"]:
                status["google"] = True
    except Exception as e:
        print(f"Notice inspecting cookie db: {e}")
    finally:
        tmp_db.unlink(missing_ok=True)

    return status


def merge_sqlite_cookies(src_db: Path, dest_db: Path) -> int:
    """Copies all cookies from src_db into dest_db using SQLite INSERT OR REPLACE."""
    if not src_db.exists():
        return 0
    dest_db.parent.mkdir(parents=True, exist_ok=True)

    tmp_src = Path(f"/tmp/src_merge_{os.getpid()}_{src_db.parent.name}.db")
    count = 0
    try:
        shutil.copyfile(src_db, tmp_src)

        # If destination doesn't exist, simply copy the whole database file
        if not dest_db.exists() or dest_db.stat().st_size == 0:
            shutil.copyfile(tmp_src, dest_db)
            conn_dest = sqlite3.connect(dest_db)
            c_dest = conn_dest.cursor()
            c_dest.execute("SELECT count(*) FROM cookies")
            res = c_dest.fetchone()
            count = res[0] if res else 0
            conn_dest.close()
            return count

        conn_dest = sqlite3.connect(dest_db)
        c_dest = conn_dest.cursor()
        c_dest.execute("ATTACH DATABASE ? AS src_db", (str(tmp_src),))
        c_dest.execute("INSERT OR REPLACE INTO cookies SELECT * FROM src_db.cookies")
        conn_dest.commit()
        count = conn_dest.total_changes
        c_dest.execute("DETACH DATABASE src_db")
        conn_dest.close()
    except Exception as e:
        print(f"Notice merging cookies from {src_db}: {e}")
    finally:
        tmp_src.unlink(missing_ok=True)

    return count


def sync_system_chrome_cookies(profile_dir: Path) -> dict[str, Any]:
    """
    Scans desktop Chrome profiles (~/.config/google-chrome/Profile * and Default)
    and merges all authenticated sessions (Google, LinkedIn, BestJobs, eJobs) into .browser_profile/.
    """
    dest_db = profile_dir / "Default" / "Cookies"
    dest_db.parent.mkdir(parents=True, exist_ok=True)

    chrome_root = Path.home() / ".config" / "google-chrome"
    merged_sources: list[str] = []
    total_cookies_merged = 0

    if chrome_root.exists():
        cookie_candidates = list(chrome_root.glob("**/Cookies"))
        # Prioritize Profile 5 (user main profile) and Default
        cookie_candidates.sort(
            key=lambda p: (
                0 if "Profile 5" in str(p) else 1 if "Default" in str(p) else 2
            )
        )

        for cand in cookie_candidates:
            if cand.is_file() and cand.stat().st_size > 0:
                c = merge_sqlite_cookies(cand, dest_db)
                if c > 0:
                    merged_sources.append(f"{cand.parent.name} ({c} cookies)")
                    total_cookies_merged += c

    status = inspect_profile_cookies(profile_dir)
    log_event(
        f"Synced {total_cookies_merged} cookies from desktop Chrome! Status: {status}",
        level="SUCCESS",
        category="Auth",
    )
    return {
        "status": "success",
        "cookies_merged": total_cookies_merged,
        "sources": merged_sources,
        "auth_status": status,
    }


class AuthManager:
    """Manages persistent platform logins (LinkedIn, BestJobs, eJobs, Google) and cookie synchronization."""

    def __init__(self, profile_dir: Path | None = None):
        project_root = get_project_root()
        self.profile_dir = profile_dir or (project_root / ".browser_profile")
        self.candidate = load_candidate_profile()

    def check_auth_status(self) -> dict[str, Any]:
        """Checks real login status by inspecting actual cookies in .browser_profile."""
        real_status = inspect_profile_cookies(self.profile_dir)

        status_report: dict[str, Any] = {
            "profile_dir": str(self.profile_dir),
            "platforms": {},
        }

        for platform in ["linkedin", "bestjobs", "ejobs", "google"]:
            is_logged_in = real_status.get(platform, False)
            status_report["platforms"][platform] = {
                "configured": is_logged_in,
                "login_url": PLATFORM_LOGIN_URLS.get(platform, ""),
                "status": "logged_in" if is_logged_in else "not_logged_in",
            }

        return status_report

    def sync_desktop_cookies(self) -> dict[str, Any]:
        """Imports all sessions from the user's desktop Chrome."""
        return sync_system_chrome_cookies(self.profile_dir)

    def launch_interactive_login(
        self, platform: str = "linkedin", timeout_seconds: int = 180
    ) -> dict[str, Any]:
        """
        Launches Google Chrome natively on DISPLAY with the persistent profile.
        Allows the user to sign in, complete email verification, and solve 2FA without
        bot detection blocking. Cookies are permanently saved to .browser_profile/.
        """
        platform_key = platform.strip().lower()
        login_url = PLATFORM_LOGIN_URLS.get(platform_key)
        if not login_url:
            return {"status": "error", "message": f"Unsupported platform: {platform}"}

        # Auto-sync existing desktop Chrome cookies first
        sync_system_chrome_cookies(self.profile_dir)

        # If already logged in from desktop Chrome, return immediately
        status_before = inspect_profile_cookies(self.profile_dir)
        if status_before.get(platform_key):
            log_event(
                f"{platform_key.upper()} is already authenticated via desktop Chrome sync!",
                level="SUCCESS",
                category="Auth",
            )
            return {
                "status": "success",
                "platform": platform_key,
                "message": f"Successfully authenticated on {platform_key.upper()} via desktop Chrome sync!",
            }

        # Ensure X11 display is available
        VirtualDisplayManager.ensure_display()
        clean_stale_chrome_locks(self.profile_dir)

        chrome_path = "/usr/bin/google-chrome"
        if not Path(chrome_path).exists():
            chrome_path = "/opt/google/chrome/chrome"

        env = os.environ.copy()
        if not env.get("DISPLAY"):
            env["DISPLAY"] = ":20"

        cmd = [
            chrome_path,
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--start-maximized",
            login_url,
        ]

        log_event(
            f"Opening native Google Chrome on {env.get('DISPLAY')} for {platform_key.upper()} login...",
            category="Auth",
        )
        try:
            proc = subprocess.Popen(cmd, env=env)
        except Exception as e:
            return {"status": "error", "message": f"Could not launch Chrome: {e}"}

        start_time = time.time()
        logged_in = False

        while time.time() - start_time < timeout_seconds:
            # Check if process was closed by user
            if proc.poll() is not None:
                break
            # Check if cookie appeared in sqlite
            st = inspect_profile_cookies(self.profile_dir)
            if st.get(platform_key):
                logged_in = True
                break
            time.sleep(2)

        # Final cookie check after window close or timeout
        st_final = inspect_profile_cookies(self.profile_dir)
        if st_final.get(platform_key) or logged_in:
            log_event(
                f"Successfully authenticated on {platform_key.upper()}! Session saved permanently.",
                level="SUCCESS",
                category="Auth",
            )
            return {
                "status": "success",
                "platform": platform_key,
                "message": f"Successfully logged into {platform_key.upper()}. Session saved in .browser_profile/.",
            }
        else:
            log_event(
                f"Login window closed or timed out for {platform_key.upper()}.",
                level="WARN",
                category="Auth",
            )
            return {
                "status": "timeout",
                "platform": platform_key,
                "message": "Login window closed before authentication completed.",
            }
