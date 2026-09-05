from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

from authlib.integrations.starlette_client import OAuth  # type: ignore[import-untyped]
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)

SAFE_AUTH_ERRORS = {
    "access_denied": "Access was denied by the OAuth provider.",
    "invalid_state": "Session state validation failed or expired. Please try again.",
    "missing_email": "Unable to resolve an email address from your account.",
    "unverified_email": "Your account email address is not verified.",
    "unauthorized_user": "Your account is not authorized to access this dashboard.",
    "provider_error": "An error occurred while communicating with the OAuth provider.",
    "provider_disabled": "The requested authentication provider is not configured.",
    "config_error": "Dashboard authentication is misconfigured.",
    "auth_failed": "Authentication failed. Please try again.",
}


@dataclass
class DashboardAuthConfig:
    """Configuration for production dashboard OAuth and session security."""

    auth_enabled: bool = False
    session_secret: str = ""
    public_url: str = "http://127.0.0.1:8000"
    session_cookie_name: str = "job_applier_session"
    session_max_age: int = 14 * 86400  # 14 days
    session_cookie_secure: bool = False
    session_cookie_same_site: Literal["lax", "strict", "none"] = "lax"
    google_client_id: str = ""
    google_client_secret: str = ""
    github_client_id: str = ""
    github_client_secret: str = ""
    allowed_google_emails: set[str] = field(default_factory=set)
    allowed_github_users: set[str] = field(default_factory=set)

    @classmethod
    def from_env(cls) -> DashboardAuthConfig:
        """Loads configuration from environment variables."""
        enabled_val = os.getenv("APP_AUTH_ENABLED", "false").strip().lower()
        auth_enabled = enabled_val in ("true", "1", "yes", "on")

        session_secret = os.getenv("APP_SESSION_SECRET", "").strip()
        public_url = (
            os.getenv("APP_PUBLIC_URL", "http://127.0.0.1:8000").strip().rstrip("/")
        )
        if not public_url:
            public_url = "http://127.0.0.1:8000"

        cookie_name = os.getenv(
            "APP_SESSION_COOKIE_NAME", "job_applier_session"
        ).strip()

        try:
            max_age = int(os.getenv("APP_SESSION_MAX_AGE", str(14 * 86400)))
        except ValueError:
            max_age = 14 * 86400

        # Determine cookie secure attribute
        secure_env = os.getenv("APP_SESSION_COOKIE_SECURE")
        if secure_env is not None:
            cookie_secure = secure_env.strip().lower() in ("true", "1", "yes")
        else:
            cookie_secure = public_url.lower().startswith("https://")

        google_client_id = (
            os.getenv("AUTH_GOOGLE_CLIENT_ID") or os.getenv("GOOGLE_CLIENT_ID") or ""
        ).strip()
        google_client_secret = (
            os.getenv("AUTH_GOOGLE_CLIENT_SECRET")
            or os.getenv("GOOGLE_CLIENT_SECRET")
            or ""
        ).strip()
        github_client_id = (
            os.getenv("AUTH_GITHUB_CLIENT_ID") or os.getenv("GITHUB_CLIENT_ID") or ""
        ).strip()
        github_client_secret = (
            os.getenv("AUTH_GITHUB_CLIENT_SECRET")
            or os.getenv("GITHUB_CLIENT_SECRET")
            or ""
        ).strip()

        # Google allowed emails
        raw_google_emails = (
            os.getenv("AUTH_ALLOWED_GOOGLE_EMAILS")
            or os.getenv("AUTH_GOOGLE_ALLOWED_EMAILS")
            or os.getenv("GOOGLE_ALLOWED_EMAILS")
            or ""
        )
        allowed_google = {
            e.strip().lower() for e in raw_google_emails.split(",") if e.strip()
        }

        # GitHub allowed users
        raw_github_users = (
            os.getenv("AUTH_ALLOWED_GITHUB_USERS")
            or os.getenv("AUTH_GITHUB_ALLOWED_USERS")
            or os.getenv("GITHUB_ALLOWED_USERS")
            or ""
        )
        allowed_github = {
            u.strip().lower() for u in raw_github_users.split(",") if u.strip()
        }

        return cls(
            auth_enabled=auth_enabled,
            session_secret=session_secret,
            public_url=public_url,
            session_cookie_name=cookie_name,
            session_max_age=max_age,
            session_cookie_secure=cookie_secure,
            session_cookie_same_site="lax",
            google_client_id=google_client_id,
            google_client_secret=google_client_secret,
            github_client_id=github_client_id,
            github_client_secret=github_client_secret,
            allowed_google_emails=allowed_google,
            allowed_github_users=allowed_github,
        )

    @property
    def is_google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def is_github_configured(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret)

    def validate(self) -> None:
        """Validates configuration, failing closed with clear error if misconfigured."""
        if not self.auth_enabled:
            return

        if not self.session_secret or len(self.session_secret) < 32:
            raise RuntimeError(
                "APP_SESSION_SECRET must be at least 32 characters long when dashboard authentication is enabled."
            )

        if not (self.is_google_configured or self.is_github_configured):
            raise RuntimeError(
                "Dashboard authentication is enabled, but at least one OAuth provider (Google or GitHub) "
                "must be fully configured with client ID and secret."
            )

    def get_origin(self) -> str:
        """Returns public origin without path (e.g. 'https://host.example')."""
        parsed = urlparse(self.public_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        return origin if parsed.netloc else "http://127.0.0.1:8000"

    def get_base_path(self) -> str:
        """Returns public base path without trailing slash (e.g. '/job-applier' or '')."""
        parsed = urlparse(self.public_url)
        return parsed.path.rstrip("/")

    def get_callback_url(self, provider: str) -> str:
        """Builds exact callback URL using public origin and base path, never Host headers."""
        origin = self.get_origin()
        base = self.get_base_path()
        return f"{origin}{base}/auth/callback/{provider}"

    def get_dashboard_redirect_url(self, error: str | None = None) -> str:
        """Builds exact dashboard redirect URL under base path."""
        base = self.get_base_path()
        prefix = f"{base}/" if base else "/"
        if error:
            safe_code = re.sub(r"[^a-zA-Z0-9_\-]", "", error)
            return f"{prefix}?auth_error={safe_code}"
        return prefix


def create_oauth(config: DashboardAuthConfig) -> OAuth:
    """Initializes Authlib OAuth instance registering available providers."""
    oauth = OAuth()
    if config.is_google_configured:
        oauth.register(
            name="google",
            client_id=config.google_client_id,
            client_secret=config.google_client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    if config.is_github_configured:
        oauth.register(
            name="github",
            client_id=config.github_client_id,
            client_secret=config.github_client_secret,
            access_token_url="https://github.com/login/oauth/access_token",
            authorize_url="https://github.com/login/oauth/authorize",
            api_base_url="https://api.github.com/",
            client_kwargs={"scope": "read:user user:email"},
        )
    return oauth


def create_auth_router(config: DashboardAuthConfig, oauth: OAuth) -> APIRouter:
    """Creates the FastAPI APIRouter handling dashboard auth status, OAuth login, callbacks, and logout."""
    router = APIRouter()

    @router.get("/status")
    def auth_status(request: Request) -> dict[str, object]:
        """Returns dashboard authentication state and available providers."""
        user = request.session.get("user") if hasattr(request, "session") else None
        is_authenticated = bool(user and isinstance(user, dict) and user.get("id"))

        return {
            "auth_enabled": config.auth_enabled,
            "authenticated": True if not config.auth_enabled else is_authenticated,
            "user": user if is_authenticated else None,
            "providers": {
                "google": config.is_google_configured,
                "github": config.is_github_configured,
            },
        }

    @router.get("/login/google")
    async def login_google(request: Request) -> RedirectResponse:
        """Initiates Google OAuth authorization flow with exact public callback URL."""
        if not config.is_google_configured:
            return RedirectResponse(
                config.get_dashboard_redirect_url(error="provider_disabled")
            )
        redirect_uri = config.get_callback_url("google")
        return await oauth.google.authorize_redirect(request, redirect_uri)

    @router.get("/callback/google")
    async def callback_google(request: Request) -> RedirectResponse:
        """Handles Google OAuth callback, verifies identity, and creates session."""
        if request.query_params.get("error"):
            logger.warning(
                "Google OAuth callback returned error: %s",
                request.query_params.get("error"),
            )
            return RedirectResponse(
                config.get_dashboard_redirect_url(error="access_denied")
            )

        try:
            token = await oauth.google.authorize_access_token(request)
        except Exception as exc:
            logger.warning("Google OAuth token exchange failed: %s", type(exc).__name__)
            return RedirectResponse(
                config.get_dashboard_redirect_url(error="invalid_state")
            )

        try:
            userinfo = token.get("userinfo")
            if not userinfo:
                userinfo = await oauth.google.userinfo(token=token)
        except Exception as exc:
            logger.warning("Failed to fetch Google userinfo: %s", type(exc).__name__)
            return RedirectResponse(
                config.get_dashboard_redirect_url(error="provider_error")
            )

        if not userinfo or not userinfo.get("email"):
            return RedirectResponse(
                config.get_dashboard_redirect_url(error="missing_email")
            )

        if not userinfo.get("email_verified"):
            logger.warning(
                "Rejected unverified Google email: %s", userinfo.get("email")
            )
            return RedirectResponse(
                config.get_dashboard_redirect_url(error="unverified_email")
            )

        email = str(userinfo.get("email", "")).strip().lower()
        if config.allowed_google_emails and email not in config.allowed_google_emails:
            logger.warning("Google email %s not authorized in allowlist", email)
            return RedirectResponse(
                config.get_dashboard_redirect_url(error="unauthorized_user")
            )

        # Store minimal identity claims only — never access tokens in cookies
        request.session["user"] = {
            "provider": "google",
            "id": str(userinfo.get("sub", "")),
            "email": email,
            "name": str(userinfo.get("name") or email),
            "username": email.split("@")[0],
            "avatar_url": str(userinfo.get("picture", "")),
        }
        return RedirectResponse(config.get_dashboard_redirect_url())

    @router.get("/login/github")
    async def login_github(request: Request) -> RedirectResponse:
        """Initiates GitHub OAuth authorization flow with exact public callback URL."""
        if not config.is_github_configured:
            return RedirectResponse(
                config.get_dashboard_redirect_url(error="provider_disabled")
            )
        redirect_uri = config.get_callback_url("github")
        return await oauth.github.authorize_redirect(request, redirect_uri)

    @router.get("/callback/github")
    async def callback_github(request: Request) -> RedirectResponse:
        """Handles GitHub OAuth callback, verifies identity, and creates session."""
        if request.query_params.get("error"):
            logger.warning(
                "GitHub OAuth callback returned error: %s",
                request.query_params.get("error"),
            )
            return RedirectResponse(
                config.get_dashboard_redirect_url(error="access_denied")
            )

        try:
            token = await oauth.github.authorize_access_token(request)
        except Exception as exc:
            logger.warning("GitHub OAuth token exchange failed: %s", type(exc).__name__)
            return RedirectResponse(
                config.get_dashboard_redirect_url(error="invalid_state")
            )

        try:
            user_resp = await oauth.github.get("user", token=token)
            user_data = user_resp.json()
        except Exception as exc:
            logger.warning("Failed to fetch GitHub user: %s", type(exc).__name__)
            return RedirectResponse(
                config.get_dashboard_redirect_url(error="provider_error")
            )

        login = str(user_data.get("login", "")).strip()
        if not login:
            return RedirectResponse(
                config.get_dashboard_redirect_url(error="provider_error")
            )

        # Resolve primary verified email if missing or private
        email = user_data.get("email")
        if not email:
            try:
                emails_resp = await oauth.github.get("user/emails", token=token)
                emails_data = emails_resp.json()
                if isinstance(emails_data, list):
                    for item in emails_data:
                        if item.get("primary") and item.get("verified"):
                            email = item.get("email")
                            break
                    if not email:
                        for item in emails_data:
                            if item.get("verified"):
                                email = item.get("email")
                                break
            except Exception as exc:
                logger.warning(
                    "Failed to fetch GitHub user emails: %s", type(exc).__name__
                )

        login_lower = login.lower()
        email_lower = str(email or "").lower()
        if config.allowed_github_users:
            allowed = (login_lower in config.allowed_github_users) or (
                bool(email_lower) and email_lower in config.allowed_github_users
            )
            if not allowed:
                logger.warning("GitHub user %s (%s) not in allowlist", login, email)
                return RedirectResponse(
                    config.get_dashboard_redirect_url(error="unauthorized_user")
                )

        # Store minimal identity claims only — never access tokens in cookies
        request.session["user"] = {
            "provider": "github",
            "id": str(user_data.get("id", "")),
            "email": email or "",
            "name": str(user_data.get("name") or login),
            "username": login,
            "avatar_url": str(user_data.get("avatar_url", "")),
        }
        return RedirectResponse(config.get_dashboard_redirect_url())

    @router.get("/logout")
    @router.post("/logout")
    def logout(request: Request):
        """Clears authenticated session and redirects or returns success."""
        if hasattr(request, "session"):
            request.session.clear()
        if (
            request.method == "GET"
            or request.headers.get("accept", "").find("text/html") >= 0
        ):
            return RedirectResponse(config.get_dashboard_redirect_url())
        return {"status": "success", "message": "Successfully logged out"}

    return router


class DashboardAuthMiddleware(BaseHTTPMiddleware):
    """Protects sensitive /api and /files routes when dashboard authentication is enabled."""

    def __init__(self, app: ASGIApp, config: DashboardAuthConfig):
        super().__init__(app)
        self.config = config

    def _is_exempt(self, path: str) -> bool:
        """Determines if a given URL path is exempt from authentication."""
        # 1. Health check endpoint
        if path == "/api/health" or path.endswith("/api/health"):
            return True

        # 2. Auth initiation, callback, status, and logout routes
        if path.startswith("/auth/") or "/auth/" in path:
            return True
        if path in ("/api/auth/session", "/api/auth/me"):
            return True

        # 3. Static SPA entry points
        if path in ("/", ""):
            return True
        if path in ("/favicon.ico", "/favicon.svg"):
            return True

        # 4. Static assets (JS, CSS, fonts, SVG) NOT under /files/
        if not (path.startswith("/files/") or "/files/" in path):
            static_exts = (
                ".js",
                ".css",
                ".map",
                ".ico",
                ".svg",
                ".png",
                ".jpg",
                ".jpeg",
                ".woff",
                ".woff2",
                ".ttf",
            )
            if any(path.endswith(ext) for ext in static_exts):
                return True

        return False

    async def dispatch(self, request: Request, call_next):
        if not self.config.auth_enabled:
            return await call_next(request)

        path = request.url.path

        # Normalize path if behind a subpath like /job-applier
        base_path = self.config.get_base_path()
        clean_path = path
        if base_path and clean_path.startswith(base_path):
            clean_path = clean_path[len(base_path) :]
            if not clean_path.startswith("/"):
                clean_path = "/" + clean_path

        # Also normalize /job-applier prefix if received directly
        if clean_path.startswith("/job-applier"):
            clean_path = clean_path[len("/job-applier") :]
            if not clean_path.startswith("/"):
                clean_path = "/" + clean_path

        if self._is_exempt(clean_path):
            return await call_next(request)

        # Check authenticated session
        user = request.session.get("user") if hasattr(request, "session") else None
        if user and isinstance(user, dict) and user.get("id"):
            return await call_next(request)

        # Return 401 JSON for sensitive /api or /files requests
        if clean_path.startswith("/api/") or clean_path.startswith("/files/"):
            return JSONResponse(
                {"detail": "Authentication required"},
                status_code=401,
            )

        # Allow SPA routing or fallback
        return await call_next(request)
