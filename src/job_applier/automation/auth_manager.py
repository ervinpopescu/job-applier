from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from job_applier.automation.browser_runtime import (
    VirtualDisplayManager,
    find_chrome_executable,
    get_default_profile_dir,
    get_sanitized_browser_env,
    is_display_available,
    resolve_browser_engine,
    validate_browser_engine,
)
from job_applier.automation.network_security import attach_security_routes
from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    load_candidate_profile,
)
from job_applier.automation.profile_lock import (
    ProfileOwnershipError,
    ProfileOwnershipLock,
)
from job_applier.config import (  # type: ignore[import-not-found]
    get_platforms_config,
)
from job_applier.logger import log_event  # type: ignore[import-not-found]

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


def get_firefox_cookie_db_path(profile_dir: Path) -> Path | None:
    """Finds SQLite cookies.sqlite in Firefox profile."""
    for cand in [
        profile_dir / "cookies.sqlite",
        profile_dir / "Default" / "cookies.sqlite",
    ]:
        if cand.exists():
            return cand
    if profile_dir.is_dir():
        try:
            for child in profile_dir.iterdir():
                if child.is_dir() and (child / "cookies.sqlite").exists():
                    return child / "cookies.sqlite"
        except Exception:
            pass
    return None


def inspect_firefox_profile_cookies(profile_dir: Path) -> dict[str, bool]:
    """Inspects Firefox cookies.sqlite (moz_cookies table) to check real platform login status."""
    cookie_db = get_firefox_cookie_db_path(profile_dir)
    status: dict[str, bool] = {
        "linkedin": False,
        "bestjobs": False,
        "ejobs": False,
        "google": False,
    }
    if not cookie_db or not cookie_db.exists():
        return status

    tmp_db = Path(
        f"/tmp/chk_ff_auth_{os.getpid()}_{int(time.time() * 1000) % 100000}.db"
    )
    try:
        shutil.copyfile(cookie_db, tmp_db)
        conn = sqlite3.connect(tmp_db)
        c = conn.cursor()
        c.execute("SELECT host, name FROM moz_cookies")
        rows = c.fetchall()
        conn.close()

        for host, name in rows:
            h = (host or "").lower()
            n = (name or "").lower()
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
        print(f"Notice inspecting Firefox cookie db: {e}")
    finally:
        tmp_db.unlink(missing_ok=True)

    return status


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

    def __init__(self, profile_dir: Path | None = None, browser: str | None = None):
        self.raw_browser = validate_browser_engine(browser)
        self.engine, _ = resolve_browser_engine(self.raw_browser)
        self.profile_dir = profile_dir or get_default_profile_dir(self.engine)
        self.candidate = load_candidate_profile()

    def check_auth_status(self) -> dict[str, Any]:
        """Checks real login status by inspecting actual cookies in the profile."""
        if self.engine == "firefox":
            real_status = inspect_firefox_profile_cookies(self.profile_dir)
        else:
            real_status = inspect_profile_cookies(self.profile_dir)

        status_report: dict[str, Any] = {
            "browser": self.engine,
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
        """
        Imports all sessions from the user's desktop Chrome into .browser_profile/.
        Note: Desktop cookie sync is currently Chrome-specific. For Firefox, use manual interactive login.
        """
        if self.engine == "firefox":
            return {
                "status": "warning",
                "browser": "firefox",
                "cookies_merged": 0,
                "sources": [],
                "message": (
                    "Desktop cookie sync is only supported for Google Chrome. "
                    "For Firefox, use 'Connect / Log In' with Firefox to complete interactive login."
                ),
                "auth_status": self.check_auth_status().get("platforms", {}),
            }
        return sync_system_chrome_cookies(self.profile_dir)

    def launch_interactive_login(
        self,
        platform: str = "linkedin",
        timeout_seconds: int = 180,
        browser: str | None = None,
    ) -> dict[str, Any]:
        """
        Launches an interactive headed browser session with the persistent profile.
        Supports both Chrome/Chromium and Firefox engines without hardcoded display assumptions.
        """
        platform_key = platform.strip().lower()
        login_url = PLATFORM_LOGIN_URLS.get(platform_key)
        if not login_url:
            return {"status": "error", "message": f"Unsupported platform: {platform}"}

        target_raw = validate_browser_engine(browser or self.raw_browser)
        target_engine, target_exe = resolve_browser_engine(target_raw)

        if browser is not None and target_raw != self.raw_browser:
            target_profile_dir = get_default_profile_dir(target_engine)
        else:
            target_profile_dir = self.profile_dir

        # Ensure headed display is available
        display_ok = is_display_available()
        if not display_ok:
            disp = VirtualDisplayManager.ensure_display(allow_xvfb=False)
            if disp:
                display_ok = True

        if not display_ok:
            return {
                "status": "error",
                "browser": target_engine,
                "platform": platform_key,
                "message": (
                    "Interactive login requires a graphical display (DISPLAY or WAYLAND_DISPLAY). "
                    "No display server was detected."
                ),
            }

        try:
            with ProfileOwnershipLock(target_profile_dir, owner_type="cli"):
                return self._execute_interactive_login(
                    target_engine=target_engine,
                    target_profile_dir=target_profile_dir,
                    platform_key=platform_key,
                    login_url=login_url,
                    target_exe=target_exe,
                    timeout_seconds=timeout_seconds,
                )
        except ProfileOwnershipError as lock_err:
            return {
                "status": "error",
                "browser": target_engine,
                "platform": platform_key,
                "message": str(lock_err),
            }

    def _execute_interactive_login(
        self,
        target_engine: str,
        target_profile_dir: Path,
        platform_key: str,
        login_url: str,
        target_exe: str | None,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        sanitized_env = get_sanitized_browser_env()

        if target_engine == "firefox":
            target_profile_dir.mkdir(parents=True, exist_ok=True)
            log_event(
                f"Opening interactive Firefox for {platform_key.upper()} login...",
                category="Auth",
            )
            try:
                from playwright.sync_api import (
                    sync_playwright,  # type: ignore[import-not-found, import-untyped]
                )

                playwright = sync_playwright().start()
                proxy_server = os.environ.get("JOB_APPLIER_OUTBOUND_PROXY")
                launch_kwargs: dict[str, Any] = {
                    "user_data_dir": str(target_profile_dir),
                    "headless": False,
                    "env": sanitized_env,
                    "viewport": {"width": 1280, "height": 900},
                }
                if proxy_server:
                    launch_kwargs["proxy"] = {"server": proxy_server}

                context = playwright.firefox.launch_persistent_context(**launch_kwargs)
                attach_security_routes(context)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(login_url)

                start_time = time.time()
                logged_in = False
                while time.time() - start_time < timeout_seconds:
                    if not context.pages or page.is_closed():
                        break
                    st = inspect_firefox_profile_cookies(target_profile_dir)
                    if st.get(platform_key):
                        logged_in = True
                        break
                    time.sleep(2)

                if not logged_in:
                    st_final = inspect_firefox_profile_cookies(target_profile_dir)
                else:
                    st_final = {platform_key: True}

                try:
                    context.close()
                    playwright.stop()
                except Exception:
                    pass

                if st_final.get(platform_key) or logged_in:
                    log_event(
                        f"Successfully authenticated on {platform_key.upper()} in Firefox! Session saved permanently.",
                        level="SUCCESS",
                        category="Auth",
                    )
                    return {
                        "status": "success",
                        "browser": "firefox",
                        "platform": platform_key,
                        "message": (
                            f"Successfully logged into {platform_key.upper()} in Firefox. "
                            f"Session saved in {target_profile_dir.name}/."
                        ),
                    }
                else:
                    log_event(
                        f"Firefox login window closed or timed out for {platform_key.upper()}.",
                        level="WARN",
                        category="Auth",
                    )
                    return {
                        "status": "timeout",
                        "browser": "firefox",
                        "platform": platform_key,
                        "message": "Login window closed before authentication completed.",
                    }
            except Exception as e:
                return {
                    "status": "error",
                    "browser": "firefox",
                    "message": f"Could not launch Firefox login: {e}",
                }

        # Chrome / Chromium interactive login
        sync_system_chrome_cookies(target_profile_dir)

        status_before = inspect_profile_cookies(target_profile_dir)
        if status_before.get(platform_key):
            log_event(
                f"{platform_key.upper()} is already authenticated via desktop Chrome sync!",
                level="SUCCESS",
                category="Auth",
            )
            return {
                "status": "success",
                "browser": target_engine,
                "platform": platform_key,
                "message": f"Successfully authenticated on {platform_key.upper()} via desktop Chrome sync!",
            }

        target_profile_dir.mkdir(parents=True, exist_ok=True)
        clean_stale_chrome_locks(target_profile_dir)

        chrome_path = target_exe or find_chrome_executable()

        if chrome_path and Path(chrome_path).is_file():
            cmd = [
                chrome_path,
                f"--user-data-dir={target_profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "--start-maximized",
            ]
            proxy_server = os.environ.get("JOB_APPLIER_OUTBOUND_PROXY")
            if proxy_server:
                cmd.append(f"--proxy-server={proxy_server}")
            cmd.append(login_url)
            disp_info = (
                sanitized_env.get("WAYLAND_DISPLAY")
                or sanitized_env.get("DISPLAY")
                or "native"
            )
            log_event(
                f"Opening native Google Chrome on {disp_info} for {platform_key.upper()} login...",
                category="Auth",
            )
            try:
                proc = subprocess.Popen(
                    cmd, env={str(k): str(v) for k, v in sanitized_env.items()}
                )
            except Exception as e:
                return {
                    "status": "error",
                    "browser": target_engine,
                    "message": f"Could not launch Chrome: {e}",
                }

            start_time = time.time()
            logged_in = False
            while time.time() - start_time < timeout_seconds:
                if proc.poll() is not None:
                    break
                st = inspect_profile_cookies(target_profile_dir)
                if st.get(platform_key):
                    logged_in = True
                    break
                time.sleep(2)

            if not logged_in:
                st_final = inspect_profile_cookies(target_profile_dir)
            else:
                st_final = {platform_key: True}

            if st_final.get(platform_key) or logged_in:
                log_event(
                    f"Successfully authenticated on {platform_key.upper()}! Session saved permanently.",
                    level="SUCCESS",
                    category="Auth",
                )
                return {
                    "status": "success",
                    "browser": target_engine,
                    "platform": platform_key,
                    "message": f"Successfully logged into {platform_key.upper()}. Session saved in {target_profile_dir.name}/.",
                }
            else:
                log_event(
                    f"Login window closed or timed out for {platform_key.upper()}.",
                    level="WARN",
                    category="Auth",
                )
                return {
                    "status": "timeout",
                    "browser": target_engine,
                    "platform": platform_key,
                    "message": "Login window closed before authentication completed.",
                }
        else:
            # Fall back to Playwright Chromium headed persistent context
            try:
                from playwright.sync_api import (
                    sync_playwright,  # type: ignore[import-not-found, import-untyped]
                )

                playwright = sync_playwright().start()
                launch_args = [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--start-maximized",
                ]

                proxy_server = os.environ.get("JOB_APPLIER_OUTBOUND_PROXY")
                pw_launch_kwargs: dict[str, Any] = {
                    "user_data_dir": str(target_profile_dir),
                    "headless": False,
                    "args": launch_args,
                    "env": sanitized_env,
                    "viewport": {"width": 1280, "height": 900},
                }
                if proxy_server:
                    pw_launch_kwargs["proxy"] = {"server": proxy_server}

                context = playwright.chromium.launch_persistent_context(
                    **pw_launch_kwargs
                )
                attach_security_routes(context)
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(login_url)

                start_time = time.time()
                logged_in = False
                while time.time() - start_time < timeout_seconds:
                    if not context.pages or page.is_closed():
                        break
                    st = inspect_profile_cookies(target_profile_dir)
                    if st.get(platform_key):
                        logged_in = True
                        break
                    time.sleep(2)

                if not logged_in:
                    st_final = inspect_profile_cookies(target_profile_dir)
                else:
                    st_final = {platform_key: True}

                try:
                    context.close()
                    playwright.stop()
                except Exception:
                    pass

                if st_final.get(platform_key) or logged_in:
                    return {
                        "status": "success",
                        "browser": target_engine,
                        "platform": platform_key,
                        "message": f"Successfully logged into {platform_key.upper()}. Session saved in {target_profile_dir.name}/.",
                    }
                else:
                    return {
                        "status": "timeout",
                        "browser": target_engine,
                        "platform": platform_key,
                        "message": "Login window closed before authentication completed.",
                    }
            except Exception as e:
                return {
                    "status": "error",
                    "browser": target_engine,
                    "message": f"Could not launch browser login: {e}",
                }
