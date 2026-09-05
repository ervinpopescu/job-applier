from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from job_applier.utils import get_project_root

SUPPORTED_BROWSERS: tuple[str, ...] = ("auto", "chrome", "chromium", "firefox")


def is_linux() -> bool:
    """Returns True if the host operating system is Linux."""
    return sys.platform.startswith("linux") or platform.system().lower() == "linux"


def is_macos() -> bool:
    """Returns True if the host operating system is macOS."""
    return sys.platform == "darwin" or platform.system().lower() == "darwin"


def is_windows() -> bool:
    """Returns True if the host operating system is Windows."""
    return sys.platform in ("win32", "cygwin") or platform.system().lower() == "windows"


def get_os_name() -> str:
    """Returns normalized OS name ('linux', 'macos', 'windows', or raw system name)."""
    if is_linux():
        return "linux"
    if is_macos():
        return "macos"
    if is_windows():
        return "windows"
    return platform.system().lower()


def probe_active_x11_displays() -> list[str]:
    """Finds potential active X11 display identifiers from sockets in /tmp/.X11-unix."""
    socket_dir = Path("/tmp/.X11-unix")
    candidates: list[str] = []
    if socket_dir.is_dir():
        try:
            for item in socket_dir.iterdir():
                name = item.name
                if name.startswith("X") and name[1:].isdigit():
                    candidates.append(f":{name[1:]}")
        except Exception:
            pass
    return candidates


def is_display_available() -> bool:
    """
    Checks if a graphical display server (X11, Wayland, or native OS windowing) is available.

    - macOS / Windows: always returns True (native graphical windowing does not use DISPLAY).
    - Linux:
      - Honors WAYLAND_DISPLAY (returns True if set and non-empty).
      - Honors DISPLAY: if xdpyinfo is available, validates it against xdpyinfo.
      - If no DISPLAY/WAYLAND_DISPLAY is set, probes active local sockets in /tmp/.X11-unix
        only when xdpyinfo is available and active.
    """
    if is_macos() or is_windows():
        return True

    wayland_display = os.environ.get("WAYLAND_DISPLAY")
    if wayland_display:
        return True

    display = os.environ.get("DISPLAY")
    if display:
        if shutil.which("xdpyinfo"):
            try:
                res = subprocess.run(
                    ["xdpyinfo", "-display", display],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1,
                    check=False,
                )
                return res.returncode == 0
            except Exception:
                return False
        return True

    if shutil.which("xdpyinfo"):
        candidates = probe_active_x11_displays()
        for cand in candidates:
            try:
                res = subprocess.run(
                    ["xdpyinfo", "-display", cand],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1,
                    check=False,
                )
                if res.returncode == 0:
                    os.environ["DISPLAY"] = cand
                    return True
            except Exception:
                pass

    return False


class VirtualDisplayManager:
    """Manages an in-process Xvfb virtual display on Linux for headed stealth browsing."""

    _process: subprocess.Popen[Any] | None = None
    _display_num: int = 99

    @classmethod
    def find_free_display_num(cls, start: int = 99) -> int:
        """Finds an unused display number for Xvfb by checking /tmp/.X11-unix."""
        socket_dir = Path("/tmp/.X11-unix")
        num = start
        while num < 1000:
            socket_file = socket_dir / f"X{num}"
            if not socket_file.exists():
                return num
            num += 1
        return start

    @classmethod
    def ensure_display(cls, allow_xvfb: bool = True) -> str | None:
        """
        Returns active display if set or available.
        If running on Linux without a display and Xvfb is available and allow_xvfb=True,
        starts Xvfb and sets $DISPLAY.
        """
        if is_display_available():
            return (
                os.environ.get("DISPLAY")
                or os.environ.get("WAYLAND_DISPLAY")
                or "native"
            )

        if not allow_xvfb or not is_linux() or not shutil.which("Xvfb"):
            return None

        if cls._process is not None and cls._process.poll() is None:
            display = f":{cls._display_num}"
            os.environ["DISPLAY"] = display
            return display

        cls._display_num = cls.find_free_display_num(cls._display_num)
        display = f":{cls._display_num}"

        try:
            cls._process = subprocess.Popen(
                [
                    "Xvfb",
                    display,
                    "-screen",
                    "0",
                    "1920x1080x24",
                    "-nolisten",
                    "tcp",
                    "-ac",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)
            os.environ["DISPLAY"] = display
            print(
                f"🛡️ Started virtual Xvfb display on {display} for headed stealth browsing."
            )
            return display
        except Exception as e:
            print(f"Notice: Could not start Xvfb: {e}")
            cls._process = None
            return None

    @classmethod
    def stop(cls) -> None:
        """Terminates the virtual Xvfb process if managed by this instance."""
        if cls._process is not None and cls._process.poll() is None:
            try:
                cls._process.terminate()
                cls._process.wait(timeout=2)
            except Exception:
                try:
                    cls._process.kill()
                except Exception:
                    pass
            finally:
                cls._process = None


def validate_browser_engine(engine: str | None) -> str:
    """Validates the browser engine string, falling back to JOB_APPLIER_BROWSER or 'auto'."""
    raw = (
        engine if engine is not None else os.environ.get("JOB_APPLIER_BROWSER", "auto")
    )
    if raw is None:
        raw = "auto"
    normalized = raw.strip().lower()
    if normalized not in SUPPORTED_BROWSERS:
        raise ValueError(
            f"Unsupported browser engine '{raw}'. Supported engines are: {', '.join(SUPPORTED_BROWSERS)}"
        )
    return normalized


def find_chrome_executable() -> str | None:
    """Searches for Google Chrome executable across platforms."""
    for name in ["google-chrome", "google-chrome-stable", "chrome", "chrome.exe"]:
        which_path = shutil.which(name)
        if which_path and Path(which_path).is_file():
            return which_path

    if is_linux():
        for path_str in [
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/opt/google/chrome/google-chrome",
            "/opt/google/chrome/chrome",
        ]:
            if Path(path_str).is_file():
                return path_str

    if is_macos():
        for path_str in [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser(
                "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            ),
        ]:
            if Path(path_str).is_file():
                return path_str

    if is_windows():
        candidates = [
            os.path.join(
                os.environ.get("PROGRAMFILES", "C:\\Program Files"),
                "Google",
                "Chrome",
                "Application",
                "chrome.exe",
            ),
            os.path.join(
                os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)"),
                "Google",
                "Chrome",
                "Application",
                "chrome.exe",
            ),
            os.path.join(
                os.environ.get("LOCALAPPDATA", ""),
                "Google",
                "Chrome",
                "Application",
                "chrome.exe",
            ),
        ]
        for path_str in candidates:
            if Path(path_str).is_file():
                return path_str

    return None


def find_chromium_executable() -> str | None:
    """Searches for Chromium executable across platforms."""
    for name in ["chromium", "chromium-browser"]:
        which_path = shutil.which(name)
        if which_path and Path(which_path).is_file():
            return which_path

    if is_linux():
        for path_str in [
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/lib/chromium/chromium",
            "/snap/bin/chromium",
        ]:
            if Path(path_str).is_file():
                return path_str

    if is_macos():
        for path_str in [
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            os.path.expanduser("~/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]:
            if Path(path_str).is_file():
                return path_str

    return None


def resolve_browser_engine(engine: str | None = None) -> tuple[str, str | None]:
    """
    Resolves requested engine to (engine_type, executable_path_or_none).
    engine_type is one of: 'chrome', 'chromium', 'firefox'.
    """
    validated = validate_browser_engine(engine)

    if validated == "auto":
        chrome_exe = find_chrome_executable()
        if chrome_exe:
            return ("chrome", chrome_exe)
        chromium_exe = find_chromium_executable()
        if chromium_exe:
            return ("chromium", chromium_exe)
        return ("chromium", None)

    if validated == "chrome":
        chrome_exe = find_chrome_executable()
        return ("chrome", chrome_exe)

    if validated == "chromium":
        chromium_exe = find_chromium_executable()
        return ("chromium", chromium_exe)

    if validated == "firefox":
        return ("firefox", None)

    raise ValueError(f"Unsupported engine: {validated}")


def get_default_profile_dir(engine: str) -> Path:
    """
    Returns the persistent profile directory for the given engine.
    Respects JOB_APPLIER_PROFILE_DIR override.
    For Firefox, defaults to .browser_profile_firefox.
    For Chrome/Chromium, defaults to .browser_profile.
    """
    override = os.environ.get("JOB_APPLIER_PROFILE_DIR")
    if override:
        return Path(override)

    project_root = get_project_root()
    norm_engine = engine.strip().lower()
    if norm_engine == "firefox":
        return project_root / ".browser_profile_firefox"
    return project_root / ".browser_profile"
