from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from job_applier.web.app import create_app
from job_applier.web.auth import DashboardAuthConfig


@pytest.fixture
def disabled_auth_app():
    """Application configured with dashboard authentication disabled."""
    config = DashboardAuthConfig(auth_enabled=False)
    return create_app(config)


@pytest.fixture
def enabled_auth_config():
    """Strong configuration with Google and GitHub enabled and allowlists."""
    return DashboardAuthConfig(
        auth_enabled=True,
        session_secret="a_very_strong_secret_key_that_is_at_least_32_characters_long",
        public_url="http://127.0.0.1:8000",
        google_client_id="google-client-id-xyz",
        google_client_secret="google-client-secret-xyz",
        github_client_id="github-client-id-xyz",
        github_client_secret="github-client-secret-xyz",
        allowed_google_emails={"alice@example.com", "bob@example.com"},
        allowed_github_users={"alice-gh", "bob-gh"},
        session_cookie_secure=False,
    )


@pytest.fixture
def enabled_auth_app(enabled_auth_config):
    """Application configured with dashboard authentication enabled."""
    return create_app(enabled_auth_config)


# --- 1. Disabled Mode Tests ---


def test_disabled_mode_initialization_and_status(disabled_auth_app):
    """Verifies that with auth disabled, endpoints are open and auth status reflects disabled state."""
    client = TestClient(disabled_auth_app)

    # Health check is open
    health_res = client.get("/api/health")
    assert health_res.status_code == 200

    # Auth status reflects disabled mode (no login gate required)
    status_res = client.get("/auth/status")
    assert status_res.status_code == 200
    data = status_res.json()
    assert data["auth_enabled"] is False
    assert data["authenticated"] is True
    assert data["user"] is None

    # Sensitive /api/stats is accessible without authentication in disabled mode
    stats_res = client.get("/api/stats")
    assert stats_res.status_code == 200


# --- 2. Enabled Misconfiguration Tests ---


def test_enabled_mode_fails_closed_without_strong_secret():
    """Fails closed if auth is enabled without strong session secret."""
    # Missing secret
    with pytest.raises(RuntimeError, match="APP_SESSION_SECRET must be at least 32"):
        create_app(
            DashboardAuthConfig(
                auth_enabled=True,
                session_secret="",
                google_client_id="google-id",
                google_client_secret="google-sec",
            )
        )

    # Weak/short secret (< 32 characters)
    with pytest.raises(RuntimeError, match="APP_SESSION_SECRET must be at least 32"):
        create_app(
            DashboardAuthConfig(
                auth_enabled=True,
                session_secret="too-short-secret-key",
                google_client_id="google-id",
                google_client_secret="google-sec",
            )
        )


def test_enabled_mode_fails_closed_without_providers():
    """Fails closed if auth is enabled but neither Google nor GitHub is configured."""
    with pytest.raises(RuntimeError, match="at least one OAuth provider"):
        create_app(
            DashboardAuthConfig(
                auth_enabled=True,
                session_secret="a_valid_secret_that_has_at_least_32_characters_total",
                google_client_id="",
                google_client_secret="",
                github_client_id="",
                github_client_secret="",
            )
        )


# --- 3. Unauthenticated API and File Denial Tests ---


def test_unauthenticated_api_and_files_denied(enabled_auth_app):
    """Verifies that when auth is enabled, sensitive /api and /files return 401, while exemptions pass."""
    client = TestClient(enabled_auth_app)

    # Protected API endpoints return 401
    stats_res = client.get("/api/stats")
    assert stats_res.status_code == 401
    assert stats_res.json() == {"detail": "Authentication required"}

    apps_res = client.get("/api/applications")
    assert apps_res.status_code == 401
    assert apps_res.json() == {"detail": "Authentication required"}

    # Protected files endpoints return 401
    file_res = client.get("/files/applications/sample/CV.pdf")
    assert file_res.status_code == 401
    assert file_res.json() == {"detail": "Authentication required"}

    # Exempt: health check returns 200
    health_res = client.get("/api/health")
    assert health_res.status_code == 200

    # Exempt: auth status returns 200 with auth_enabled=True, authenticated=False
    status_res = client.get("/auth/status")
    assert status_res.status_code == 200
    status_data = status_res.json()
    assert status_data["auth_enabled"] is True
    assert status_data["authenticated"] is False
    assert status_data["providers"]["google"] is True
    assert status_data["providers"]["github"] is True

    # Exempt: SPA dashboard entry point
    root_res = client.get("/")
    assert root_res.status_code == 200


# --- 4. Mocked Google OAuth Login, Callbacks, Allowlists, and Cookies ---


def test_google_login_initiation(enabled_auth_app):
    """Verifies that Google login initiation redirects with the exact public callback URL."""
    from fastapi.responses import RedirectResponse

    client = TestClient(enabled_auth_app, follow_redirects=False)

    with patch(
        "authlib.integrations.starlette_client.StarletteOAuth2App.authorize_redirect",
        new_callable=AsyncMock,
    ) as mock_auth:
        mock_auth.return_value = RedirectResponse(
            "https://accounts.google.com/o/oauth2/v2/auth?client_id=xyz",
            status_code=307,
        )
        res = client.get("/auth/login/google")
        assert res.status_code == 307
        # Ensure exact callback URL is generated from public_url, never Host header
        call_args = mock_auth.call_args
        assert call_args[0][1] == "http://127.0.0.1:8000/auth/callback/google"


def test_google_callback_access_denied(enabled_auth_app):
    """Verifies that user denial at Google consent screen redirects with safe auth_error."""
    client = TestClient(enabled_auth_app, follow_redirects=False)
    res = client.get("/auth/callback/google?error=access_denied")
    assert res.status_code == 307
    assert res.headers["location"] == "/?auth_error=access_denied"


def test_google_callback_unverified_email(enabled_auth_app):
    """Rejects Google identity when email is unverified."""
    client = TestClient(enabled_auth_app, follow_redirects=False)

    mock_token = {
        "access_token": "google-secret-access-token",
        "userinfo": {
            "sub": "g-12345",
            "email": "alice@example.com",
            "email_verified": False,
            "name": "Alice Unverified",
        },
    }

    with patch(
        "authlib.integrations.starlette_client.StarletteOAuth2App.authorize_access_token",
        new_callable=AsyncMock,
    ) as mock_tok:
        mock_tok.return_value = mock_token
        res = client.get("/auth/callback/google?code=valid-code&state=valid-state")
        assert res.status_code == 307
        assert res.headers["location"] == "/?auth_error=unverified_email"


def test_google_callback_allowlist_rejection(enabled_auth_app):
    """Rejects verified Google user not present in allowlist."""
    client = TestClient(enabled_auth_app, follow_redirects=False)

    mock_token = {
        "access_token": "google-secret-access-token",
        "userinfo": {
            "sub": "g-99999",
            "email": "intruder@example.com",
            "email_verified": True,
            "name": "Intruder",
        },
    }

    with patch(
        "authlib.integrations.starlette_client.StarletteOAuth2App.authorize_access_token",
        new_callable=AsyncMock,
    ) as mock_tok:
        mock_tok.return_value = mock_token
        res = client.get("/auth/callback/google?code=valid-code&state=valid-state")
        assert res.status_code == 307
        assert res.headers["location"] == "/?auth_error=unauthorized_user"


def test_google_callback_success_session_and_cookies(enabled_auth_app):
    """Verifies successful Google login sets session cookie, stores minimal claims, and allows API access."""
    client = TestClient(enabled_auth_app, follow_redirects=False)

    mock_token = {
        "access_token": "google-secret-access-token-must-not-leak",
        "userinfo": {
            "sub": "g-12345",
            "email": "alice@example.com",
            "email_verified": True,
            "name": "Alice Engineer",
            "picture": "https://lh3.googleusercontent.com/alice.jpg",
        },
    }

    with patch(
        "authlib.integrations.starlette_client.StarletteOAuth2App.authorize_access_token",
        new_callable=AsyncMock,
    ) as mock_tok:
        mock_tok.return_value = mock_token
        res = client.get("/auth/callback/google?code=valid-code&state=valid-state")
        assert res.status_code == 307
        assert res.headers["location"] == "/"

        # Verify Set-Cookie header contains session cookie with HttpOnly and SameSite=lax
        cookie_header = res.headers.get("set-cookie", "")
        assert "job_applier_session=" in cookie_header
        assert "httponly" in cookie_header.lower()
        assert "samesite=lax" in cookie_header.lower()

        # Cookie must NEVER contain provider access tokens
        assert "google-secret-access-token-must-not-leak" not in cookie_header

    # Authenticated client now has access to auth status with user details
    status_res = client.get("/auth/status")
    assert status_res.status_code == 200
    data = status_res.json()
    assert data["authenticated"] is True
    assert data["user"]["email"] == "alice@example.com"
    assert data["user"]["name"] == "Alice Engineer"
    assert data["user"]["provider"] == "google"

    # Authenticated client now has access to protected APIs
    stats_res = client.get("/api/stats")
    assert stats_res.status_code == 200

    # Logout revokes access
    logout_res = client.post("/auth/logout")
    assert logout_res.status_code == 200

    # Subsequent access is denied with 401
    post_logout_stats = client.get("/api/stats")
    assert post_logout_stats.status_code == 401


# --- 5. Mocked GitHub OAuth Login, Callbacks, and Email Resolution ---


def test_github_login_initiation(enabled_auth_app):
    """Verifies that GitHub login initiation redirects with the exact public callback URL."""
    from fastapi.responses import RedirectResponse

    client = TestClient(enabled_auth_app, follow_redirects=False)

    with patch(
        "authlib.integrations.starlette_client.StarletteOAuth2App.authorize_redirect",
        new_callable=AsyncMock,
    ) as mock_auth:
        mock_auth.return_value = RedirectResponse(
            "https://github.com/login/oauth/authorize?client_id=xyz",
            status_code=307,
        )
        res = client.get("/auth/login/github")
        assert res.status_code == 307
        call_args = mock_auth.call_args
        assert call_args[0][1] == "http://127.0.0.1:8000/auth/callback/github"


def test_github_callback_resolves_private_email_and_checks_allowlist(enabled_auth_app):
    """Verifies GitHub flow resolves verified primary email when private and enforces allowlist."""
    client = TestClient(enabled_auth_app, follow_redirects=False)

    mock_token = {"access_token": "gh-access-token-must-not-leak"}

    mock_user_resp = MagicMock()
    mock_user_resp.json.return_value = {
        "id": 98765,
        "login": "alice-gh",
        "name": "Alice GitHub",
        "email": None,  # Private email on main profile
        "avatar_url": "https://avatars.githubusercontent.com/u/98765",
    }

    mock_emails_resp = MagicMock()
    mock_emails_resp.json.return_value = [
        {"email": "unverified@example.com", "primary": False, "verified": False},
        {"email": "alice-gh-primary@example.com", "primary": True, "verified": True},
    ]

    with (
        patch(
            "authlib.integrations.starlette_client.StarletteOAuth2App.authorize_access_token",
            new_callable=AsyncMock,
        ) as mock_tok,
        patch(
            "authlib.integrations.starlette_client.StarletteOAuth2App.get",
            new_callable=AsyncMock,
        ) as mock_get,
    ):
        mock_tok.return_value = mock_token

        async def fake_get(url, **kwargs):
            if url == "user":
                return mock_user_resp
            if url == "user/emails":
                return mock_emails_resp
            raise ValueError(f"Unexpected url {url}")

        mock_get.side_effect = fake_get

        res = client.get(
            "/auth/callback/github?code=valid-gh-code&state=valid-gh-state"
        )
        assert res.status_code == 307
        assert res.headers["location"] == "/"

    # Verified user status check
    status_res = client.get("/auth/status")
    assert status_res.status_code == 200
    user = status_res.json()["user"]
    assert user["username"] == "alice-gh"
    assert user["email"] == "alice-gh-primary@example.com"
    assert user["provider"] == "github"

    # Protected API succeeds
    assert client.get("/api/stats").status_code == 200


def test_github_callback_unauthorized_user_denied(enabled_auth_app):
    """Rejects GitHub user not in allowlist."""
    client = TestClient(enabled_auth_app, follow_redirects=False)

    mock_token = {"access_token": "gh-access-token"}
    mock_user_resp = MagicMock()
    mock_user_resp.json.return_value = {
        "id": 11111,
        "login": "unauthorized-dev",
        "name": "Intruder Dev",
        "email": "intruder@dev.example",
    }

    with (
        patch(
            "authlib.integrations.starlette_client.StarletteOAuth2App.authorize_access_token",
            new_callable=AsyncMock,
        ) as mock_tok,
        patch(
            "authlib.integrations.starlette_client.StarletteOAuth2App.get",
            new_callable=AsyncMock,
        ) as mock_get,
    ):
        mock_tok.return_value = mock_token
        mock_get.return_value = mock_user_resp

        res = client.get("/auth/callback/github?code=valid-code&state=valid-state")
        assert res.status_code == 307
        assert res.headers["location"] == "/?auth_error=unauthorized_user"


# --- 6. Reverse Proxy and Subpath Callbacks Tests ---


def test_subpath_callbacks_and_routing():
    """Verifies that reverse proxies with subpaths (e.g. /job-applier) route callbacks and redirects correctly."""
    subpath_config = DashboardAuthConfig(
        auth_enabled=True,
        session_secret="subpath_session_secret_that_is_at_least_32_characters_long",
        public_url="https://external.proxy.example/job-applier",
        google_client_id="google-subpath-id",
        google_client_secret="google-subpath-secret",
        allowed_google_emails={"subpath-user@example.com"},
        session_cookie_secure=True,
    )
    app = create_app(subpath_config)
    client = TestClient(app, follow_redirects=False)

    # Status route functions at subpath
    status_res = client.get("/job-applier/auth/status")
    assert status_res.status_code == 200
    assert status_res.json()["auth_enabled"] is True

    # Health check functions at subpath
    health_res = client.get("/job-applier/api/health")
    assert health_res.status_code == 200

    # Login initiation at subpath enforces exact public redirect_uri with subpath
    from fastapi.responses import RedirectResponse

    with patch(
        "authlib.integrations.starlette_client.StarletteOAuth2App.authorize_redirect",
        new_callable=AsyncMock,
    ) as mock_auth:
        mock_auth.return_value = RedirectResponse(
            "https://accounts.google.com/o/oauth2/v2/auth",
            status_code=307,
        )
        res = client.get(
            "/job-applier/auth/login/google",
            headers={"Host": "malicious-host-injection.com"},
        )
        assert res.status_code == 307
        call_args = mock_auth.call_args
        # Must strictly use public origin and subpath, ignoring Host header
        assert (
            call_args[0][1]
            == "https://external.proxy.example/job-applier/auth/callback/google"
        )

    # Callback at subpath redirects to subpath dashboard
    mock_token = {
        "access_token": "google-token",
        "userinfo": {
            "sub": "sub-123",
            "email": "subpath-user@example.com",
            "email_verified": True,
            "name": "Subpath User",
        },
    }
    with patch(
        "authlib.integrations.starlette_client.StarletteOAuth2App.authorize_access_token",
        new_callable=AsyncMock,
    ) as mock_tok:
        mock_tok.return_value = mock_token
        res = client.get(
            "/job-applier/auth/callback/google?code=subpath-code&state=subpath-state"
        )
        assert res.status_code == 307
        assert res.headers["location"] == "/job-applier/"

        # Cookie has Secure flag in production https configuration
        cookie_header = res.headers.get("set-cookie", "")
        assert "secure" in cookie_header.lower()

    # Subpath error redirect includes subpath prefix
    err_res = client.get("/job-applier/auth/callback/google?error=access_denied")
    assert err_res.status_code == 307
    assert err_res.headers["location"] == "/job-applier/?auth_error=access_denied"

    # Subpath logout redirects to subpath
    logout_res = client.get("/job-applier/auth/logout")
    assert logout_res.status_code == 307
    assert logout_res.headers["location"] == "/job-applier/"


# --- 7. Callback Edge Cases & Provider Errors ---


def test_google_callback_invalid_state_or_token_failure(enabled_auth_app):
    """Verifies that token exchange exception (e.g. state mismatch or expiry) redirects safely with invalid_state."""
    client = TestClient(enabled_auth_app, follow_redirects=False)

    with patch(
        "authlib.integrations.starlette_client.StarletteOAuth2App.authorize_access_token",
        new_callable=AsyncMock,
    ) as mock_tok:
        mock_tok.side_effect = Exception("MismatchingStateError")
        res = client.get(
            "/auth/callback/google?code=invalid-code&state=mismatched-state"
        )
        assert res.status_code == 307
        assert res.headers["location"] == "/?auth_error=invalid_state"


def test_google_callback_missing_email(enabled_auth_app):
    """Verifies that Google userinfo lacking email redirects with missing_email."""
    client = TestClient(enabled_auth_app, follow_redirects=False)

    mock_token = {
        "access_token": "valid-token",
        "userinfo": {
            "sub": "g-12345",
            "email": "",
            "name": "No Email",
        },
    }

    with patch(
        "authlib.integrations.starlette_client.StarletteOAuth2App.authorize_access_token",
        new_callable=AsyncMock,
    ) as mock_tok:
        mock_tok.return_value = mock_token
        res = client.get("/auth/callback/google?code=valid-code&state=valid-state")
        assert res.status_code == 307
        assert res.headers["location"] == "/?auth_error=missing_email"


def test_github_callback_invalid_state_or_token_failure(enabled_auth_app):
    """Verifies GitHub token exchange exception redirects safely with invalid_state."""
    client = TestClient(enabled_auth_app, follow_redirects=False)

    with patch(
        "authlib.integrations.starlette_client.StarletteOAuth2App.authorize_access_token",
        new_callable=AsyncMock,
    ) as mock_tok:
        mock_tok.side_effect = Exception("MismatchingStateError")
        res = client.get(
            "/auth/callback/github?code=invalid-code&state=mismatched-state"
        )
        assert res.status_code == 307
        assert res.headers["location"] == "/?auth_error=invalid_state"


def test_provider_disabled_redirect(enabled_auth_config):
    """Verifies requesting an unconfigured provider redirects with provider_disabled."""
    # Config with only Google enabled
    enabled_auth_config.github_client_id = ""
    enabled_auth_config.github_client_secret = ""
    app = create_app(enabled_auth_config)
    client = TestClient(app, follow_redirects=False)

    res = client.get("/auth/login/github")
    assert res.status_code == 307
    assert res.headers["location"] == "/?auth_error=provider_disabled"
