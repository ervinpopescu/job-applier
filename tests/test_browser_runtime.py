from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from job_applier.automation.browser_runtime import (
    VirtualDisplayManager,
    get_default_profile_dir,
    get_os_name,
    is_display_available,
    is_linux,
    is_macos,
    is_windows,
    resolve_browser_engine,
    validate_browser_engine,
)


class TestOSDetection:
    def test_os_linux(self):
        with (
            patch("sys.platform", "linux"),
            patch("platform.system", return_value="Linux"),
        ):
            assert is_linux() is True
            assert is_macos() is False
            assert is_windows() is False
            assert get_os_name() == "linux"

    def test_os_macos(self):
        with (
            patch("sys.platform", "darwin"),
            patch("platform.system", return_value="Darwin"),
        ):
            assert is_linux() is False
            assert is_macos() is True
            assert is_windows() is False
            assert get_os_name() == "macos"

    def test_os_windows(self):
        with (
            patch("sys.platform", "win32"),
            patch("platform.system", return_value="Windows"),
        ):
            assert is_linux() is False
            assert is_macos() is False
            assert is_windows() is True
            assert get_os_name() == "windows"


class TestDisplayDetection:
    def test_macos_windows_display_available_without_env(self):
        with (
            patch("job_applier.automation.browser_runtime.is_macos", return_value=True),
            patch(
                "job_applier.automation.browser_runtime.is_windows", return_value=False
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            assert is_display_available() is True

        with (
            patch(
                "job_applier.automation.browser_runtime.is_macos", return_value=False
            ),
            patch(
                "job_applier.automation.browser_runtime.is_windows", return_value=True
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            assert is_display_available() is True

    def test_linux_wayland_display(self):
        with (
            patch(
                "job_applier.automation.browser_runtime.is_macos", return_value=False
            ),
            patch(
                "job_applier.automation.browser_runtime.is_windows", return_value=False
            ),
            patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}, clear=True),
        ):
            assert is_display_available() is True

    def test_linux_display_with_xdpyinfo_success(self):
        mock_res = MagicMock()
        mock_res.returncode = 0
        with (
            patch(
                "job_applier.automation.browser_runtime.is_macos", return_value=False
            ),
            patch(
                "job_applier.automation.browser_runtime.is_windows", return_value=False
            ),
            patch("shutil.which", return_value="/usr/bin/xdpyinfo"),
            patch("subprocess.run", return_value=mock_res),
            patch.dict(os.environ, {"DISPLAY": ":0"}, clear=True),
        ):
            assert is_display_available() is True

    def test_linux_display_with_xdpyinfo_failure(self):
        mock_res = MagicMock()
        mock_res.returncode = 1
        with (
            patch(
                "job_applier.automation.browser_runtime.is_macos", return_value=False
            ),
            patch(
                "job_applier.automation.browser_runtime.is_windows", return_value=False
            ),
            patch("shutil.which", return_value="/usr/bin/xdpyinfo"),
            patch("subprocess.run", return_value=mock_res),
            patch.dict(os.environ, {"DISPLAY": ":99"}, clear=True),
        ):
            assert is_display_available() is False

    def test_linux_display_without_xdpyinfo_honors_env(self):
        with (
            patch(
                "job_applier.automation.browser_runtime.is_macos", return_value=False
            ),
            patch(
                "job_applier.automation.browser_runtime.is_windows", return_value=False
            ),
            patch("shutil.which", return_value=None),
            patch.dict(os.environ, {"DISPLAY": ":5"}, clear=True),
        ):
            assert is_display_available() is True

    def test_linux_no_display_probes_active_sockets(self):
        mock_res = MagicMock()
        mock_res.returncode = 0
        with (
            patch(
                "job_applier.automation.browser_runtime.is_macos", return_value=False
            ),
            patch(
                "job_applier.automation.browser_runtime.is_windows", return_value=False
            ),
            patch("shutil.which", return_value="/usr/bin/xdpyinfo"),
            patch(
                "job_applier.automation.browser_runtime.probe_active_x11_displays",
                return_value=[":3"],
            ),
            patch("subprocess.run", return_value=mock_res),
            patch.dict(os.environ, {}, clear=True),
        ):
            assert is_display_available() is True
            assert os.environ.get("DISPLAY") == ":3"


class TestVirtualDisplayManager:
    def test_ensure_display_when_already_available(self):
        with (
            patch(
                "job_applier.automation.browser_runtime.is_display_available",
                return_value=True,
            ),
            patch.dict(os.environ, {"DISPLAY": ":1"}, clear=True),
        ):
            disp = VirtualDisplayManager.ensure_display()
            assert disp == ":1"

    def test_ensure_display_spawns_xvfb_on_linux(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        with (
            patch(
                "job_applier.automation.browser_runtime.is_display_available",
                return_value=False,
            ),
            patch("job_applier.automation.browser_runtime.is_linux", return_value=True),
            patch("shutil.which", return_value="/usr/bin/Xvfb"),
            patch("subprocess.Popen", return_value=mock_proc) as mock_popen,
            patch("time.sleep"),
            patch.dict(os.environ, {}, clear=True),
        ):
            disp = VirtualDisplayManager.ensure_display(allow_xvfb=True)
            assert disp is not None
            assert disp.startswith(":")
            assert os.environ.get("DISPLAY") == disp
            mock_popen.assert_called_once()
            # Clean up class state
            VirtualDisplayManager.stop()
            assert VirtualDisplayManager._process is None

    def test_ensure_display_skips_xvfb_when_disallowed_or_unavailable(self):
        with (
            patch(
                "job_applier.automation.browser_runtime.is_display_available",
                return_value=False,
            ),
            patch("job_applier.automation.browser_runtime.is_linux", return_value=True),
            patch("shutil.which", return_value=None),
            patch.dict(os.environ, {}, clear=True),
        ):
            assert VirtualDisplayManager.ensure_display(allow_xvfb=True) is None

        with (
            patch(
                "job_applier.automation.browser_runtime.is_display_available",
                return_value=False,
            ),
            patch("job_applier.automation.browser_runtime.is_linux", return_value=True),
            patch("shutil.which", return_value="/usr/bin/Xvfb"),
        ):
            assert VirtualDisplayManager.ensure_display(allow_xvfb=False) is None


class TestBrowserEngineValidation:
    def test_validate_valid_engines(self):
        assert validate_browser_engine("auto") == "auto"
        assert validate_browser_engine("chrome") == "chrome"
        assert validate_browser_engine("chromium") == "chromium"
        assert validate_browser_engine("firefox") == "firefox"
        assert validate_browser_engine("  FIREFOX  ") == "firefox"

    def test_validate_default_and_env(self):
        with patch.dict(os.environ, {}, clear=True):
            assert validate_browser_engine(None) == "auto"

        with patch.dict(os.environ, {"JOB_APPLIER_BROWSER": "firefox"}):
            assert validate_browser_engine(None) == "firefox"

    def test_validate_unsupported_engine_raises(self):
        with pytest.raises(ValueError, match="Unsupported browser engine 'safari'"):
            validate_browser_engine("safari")

        with pytest.raises(ValueError, match="Unsupported browser engine 'edge'"):
            validate_browser_engine("edge")


class TestBrowserEngineResolution:
    def test_resolve_auto_with_chrome(self):
        with patch(
            "job_applier.automation.browser_runtime.find_chrome_executable",
            return_value="/usr/bin/google-chrome",
        ):
            engine, exe = resolve_browser_engine("auto")
            assert engine == "chrome"
            assert exe == "/usr/bin/google-chrome"

    def test_resolve_auto_with_chromium_fallback(self):
        with (
            patch(
                "job_applier.automation.browser_runtime.find_chrome_executable",
                return_value=None,
            ),
            patch(
                "job_applier.automation.browser_runtime.find_chromium_executable",
                return_value="/usr/bin/chromium",
            ),
        ):
            engine, exe = resolve_browser_engine("auto")
            assert engine == "chromium"
            assert exe == "/usr/bin/chromium"

    def test_resolve_auto_with_no_installed_browser(self):
        with (
            patch(
                "job_applier.automation.browser_runtime.find_chrome_executable",
                return_value=None,
            ),
            patch(
                "job_applier.automation.browser_runtime.find_chromium_executable",
                return_value=None,
            ),
        ):
            engine, exe = resolve_browser_engine("auto")
            assert engine == "chromium"
            assert exe is None

    def test_resolve_firefox(self):
        engine, exe = resolve_browser_engine("firefox")
        assert engine == "firefox"
        assert exe is None


class TestProfileIsolation:
    def test_default_profile_dirs(self, tmp_path):
        with (
            patch(
                "job_applier.automation.browser_runtime.get_project_root",
                return_value=tmp_path,
            ),
            patch.dict(os.environ, {}, clear=True),
        ):
            chrome_dir = get_default_profile_dir("chrome")
            chromium_dir = get_default_profile_dir("chromium")
            firefox_dir = get_default_profile_dir("firefox")

            assert chrome_dir == tmp_path / ".browser_profile"
            assert chromium_dir == tmp_path / ".browser_profile"
            assert firefox_dir == tmp_path / ".browser_profile_firefox"
            assert firefox_dir != chrome_dir

    def test_profile_dir_env_override(self, tmp_path):
        custom = tmp_path / "custom_profile"
        with patch.dict(os.environ, {"JOB_APPLIER_PROFILE_DIR": str(custom)}):
            assert get_default_profile_dir("chrome") == custom
            assert get_default_profile_dir("firefox") == custom
