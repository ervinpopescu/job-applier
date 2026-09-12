from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from job_applier.automation.auth_manager import (
    AuthManager,
    inspect_firefox_profile_cookies,
)
from job_applier.automation.browser_automator import (
    BrowserAutomator,
)
from job_applier.web.app import app

client = TestClient(app)


def test_auth_manager_status(tmp_path) -> None:
    profile_dir = tmp_path / ".browser_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    manager = AuthManager(profile_dir=profile_dir)
    status = manager.check_auth_status()
    assert "platforms" in status
    assert "linkedin" in status["platforms"]
    assert "bestjobs" in status["platforms"]
    assert "ejobs" in status["platforms"]
    assert "google" in status["platforms"]


def test_api_auth_status_endpoint() -> None:
    resp = client.get("/api/auth/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "platforms" in data
    assert "linkedin" in data["platforms"]


def test_api_auth_login_endpoint() -> None:
    with patch.object(
        AuthManager, "launch_interactive_login", return_value={"status": "success"}
    ):
        resp = client.post(
            "/api/auth/login", json={"platform": "linkedin", "timeout": 10}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "started"
        assert data["platform"] == "linkedin"


def test_verification_code_flow() -> None:
    # 1. When no active automator is running, submit-code returns 400
    resp = client.post("/api/automation/submit-code", json={"code": "123456"})
    assert resp.status_code == 400

    # 2. Test BrowserAutomator supply_verification_code
    automator = BrowserAutomator(headless=True)
    automator.waiting_for_code = True
    assert automator.waiting_for_code is True

    automator.supply_verification_code("884920")
    assert automator.provided_code == "884920"
    assert automator.waiting_for_code is False


def test_detect_login_wall_and_verification() -> None:
    automator = BrowserAutomator(headless=True)
    mock_page = MagicMock()
    mock_page.url = "https://www.linkedin.com/login"
    automator.page = mock_page

    is_wall, msg = automator._detect_login_wall()
    assert is_wall is True
    assert "Redirected to platform sign-in" in msg


def test_auth_manager_firefox_status(tmp_path) -> None:
    ff_profile = tmp_path / ".browser_profile_firefox"
    ff_profile.mkdir(parents=True, exist_ok=True)
    db_path = ff_profile / "cookies.sqlite"

    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, host TEXT, name TEXT, value TEXT)"
    )
    conn.execute(
        "INSERT INTO moz_cookies (host, name, value) VALUES ('.linkedin.com', 'li_at', 'token123')"
    )
    conn.commit()
    conn.close()

    cookie_status = inspect_firefox_profile_cookies(ff_profile)
    assert cookie_status["linkedin"] is True
    assert cookie_status["bestjobs"] is False

    manager = AuthManager(profile_dir=ff_profile, browser="firefox")
    assert manager.engine == "firefox"
    report = manager.check_auth_status()
    assert report["browser"] == "firefox"
    assert report["platforms"]["linkedin"]["configured"] is True
    assert report["platforms"]["linkedin"]["status"] == "logged_in"
    assert report["platforms"]["bestjobs"]["configured"] is False


def test_launch_interactive_login_no_display() -> None:
    with (
        patch(
            "job_applier.automation.auth_manager.is_display_available",
            return_value=False,
        ),
        patch(
            "job_applier.automation.auth_manager.VirtualDisplayManager.ensure_display",
            return_value=None,
        ),
    ):
        manager = AuthManager(browser="firefox")
        res = manager.launch_interactive_login(platform="linkedin")
        assert res["status"] == "error"
        assert res["browser"] == "firefox"
        assert "Interactive login requires a graphical display" in res["message"]


def test_launch_interactive_login_firefox_selection(tmp_path) -> None:
    mock_playwright = MagicMock()
    mock_firefox = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_context.pages = [mock_page]
    mock_firefox.launch_persistent_context.return_value = mock_context
    mock_playwright.firefox = mock_firefox
    mock_sync_playwright = MagicMock()
    mock_sync_playwright.return_value.start.return_value = mock_playwright

    with (
        patch(
            "job_applier.automation.auth_manager.is_display_available",
            return_value=True,
        ),
        patch(
            "job_applier.automation.auth_manager.inspect_firefox_profile_cookies",
            side_effect=[{"linkedin": False}, {"linkedin": True}],
        ),
        patch.dict(
            "sys.modules",
            {"playwright.sync_api": MagicMock(sync_playwright=mock_sync_playwright)},
        ),
    ):
        manager = AuthManager(profile_dir=tmp_path / "ff_prof", browser="firefox")
        res = manager.launch_interactive_login(platform="linkedin", timeout_seconds=5)

        assert res["status"] == "success"
        assert res["browser"] == "firefox"
        mock_firefox.launch_persistent_context.assert_called_once()
        kwargs = mock_firefox.launch_persistent_context.call_args.kwargs
        assert kwargs["headless"] is False
        assert "args" not in kwargs


def test_sync_desktop_cookies_firefox_limitation(tmp_path) -> None:
    manager = AuthManager(profile_dir=tmp_path / "ff_prof", browser="firefox")
    res = manager.sync_desktop_cookies()
    assert res["status"] == "warning"
    assert res["browser"] == "firefox"
    assert "Desktop cookie sync is only supported for Google Chrome" in res["message"]
