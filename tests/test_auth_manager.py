from __future__ import annotations

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from job_applier.automation.auth_manager import AuthManager
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
