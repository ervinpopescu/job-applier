"""
Edge Security and Origin Authentication Module for job-applier.

Implements Zero-Trust Edge Security according to autonomous pipeline specifications:
- Canonical jobs.aslan.net origin routing with legacy route protection (no bypass).
- Cloudflare Access exact identity allowlist with strict RS256 JWT validation.
- Strict alg, signature, iss, aud, exp, and nbf/iat verification.
- Bounded JWKS caching with rate-limited key refresh and network timeouts.
- Fail-closed startup verification.
- Host header enforcement and trusted proxy CIDR filtering.
- CSRF protection and exact Origin verification for mutating requests.
- No credentialed cross-origin CORS.
- Public health authentication with a loopback-only internal probe.
- Gateway authorization endpoint for noVNC HTTP/WebSocket upgrades (/api/auth/viewer-gate).
- Server-side 5-minute / token-expiry viewer WebSocket lifetime enforcement.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
import struct
from typing import Any, cast
import urllib.parse
import urllib.request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import WebSocket, WebSocketDisconnect
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from job_applier.utils import get_secret

logger = logging.getLogger(__name__)


class EdgeAuthError(Exception):
    """Exception raised for edge authentication and authorization failures."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def b64url_decode(s: str) -> bytes:
    """Decodes a Base64URL-encoded string with padding correction."""
    rem = len(s) % 4
    if rem > 0:
        s += "=" * (4 - rem)
    return base64.urlsafe_b64decode(s.encode("ascii"))


def b64url_to_int(s: str) -> int:
    """Converts a Base64URL-encoded big-endian integer into a Python integer."""
    return int.from_bytes(b64url_decode(s), byteorder="big")


def normalize_team_domain(domain: str) -> str:
    """
    Normalizes a Cloudflare Access team domain to a canonical HTTPS URL.
    Handles:
      - bare subdomain: 'aged-sunset-0292' -> 'https://aged-sunset-0292.cloudflareaccess.com'
      - full host: 'aged-sunset-0292.cloudflareaccess.com' -> 'https://aged-sunset-0292.cloudflareaccess.com'
      - full URL: 'https://aged-sunset-0292.cloudflareaccess.com' -> 'https://aged-sunset-0292.cloudflareaccess.com'
      - http scheme or trailing slashes: canonicalizes to 'https://...'
    """
    if not domain:
        return ""
    clean = domain.strip().lower()
    if clean.startswith("https://"):
        clean = clean[len("https://") :]
    elif clean.startswith("http://"):
        clean = clean[len("http://") :]
    clean = clean.strip("/")

    if not clean:
        return ""

    if not clean.endswith(".cloudflareaccess.com"):
        clean = f"{clean}.cloudflareaccess.com"

    return f"https://{clean}"


@dataclass
class EdgeAuthConfig:
    """Configuration settings for Edge Authentication and Access policy."""

    cf_access_enabled: bool = False
    cf_access_aud: str = ""
    cf_access_team_domain: str = ""
    cf_access_allowed_identities: set[str] = field(default_factory=set)
    cf_access_certs_url: str = ""
    public_origin: str = "https://jobs.aslan.net"
    allowed_hosts: set[str] = field(
        default_factory=lambda: {
            "jobs.aslan.net",
            "aslan.archnet.lol",
            "localhost",
            "127.0.0.1",
            "web",
            "gateway",
            "testserver",
        }
    )
    trusted_proxies: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(
        default_factory=lambda: [
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("::1/128"),
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        ]
    )
    viewer_max_duration_seconds: int = 300  # Strict 5-minute maximum viewer lifetime
    local_gateway_viewer_token: str = ""
    jwks_cache_ttl_seconds: int = 3600  # 1 hour JWKS TTL
    jwks_rate_limit_seconds: int = 60  # Rate-limit remote refresh to at most once/min
    jwks_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        """Derives default certs URL and ensures canonical configuration."""
        self.viewer_max_duration_seconds = min(
            300, max(30, self.viewer_max_duration_seconds)
        )
        if not self.cf_access_certs_url and self.cf_access_team_domain:
            team_url = normalize_team_domain(self.cf_access_team_domain)
            if team_url:
                self.cf_access_certs_url = f"{team_url}/cdn-cgi/access/certs"

    @property
    def cf_access_issuer(self) -> str:
        """Calculates expected Cloudflare Access issuer URL."""
        return normalize_team_domain(self.cf_access_team_domain)

    @property
    def allowed_origins(self) -> set[str]:
        """Returns explicit HTTPS origins for configured public host aliases."""
        internal_hosts = {"localhost", "127.0.0.1", "web", "gateway", "testserver"}
        origins = {
            self.public_origin.rstrip("/").lower(),
            "http://127.0.0.1:8000",
            "http://localhost:8000",
            "http://127.0.0.1:8001",
            "http://localhost:8001",
            "http://127.0.0.1:8089",
            "http://localhost:8089",
            "http://localhost:3000",
            "http://testserver",
        }
        origins.update(
            f"https://{host}"
            for host in self.allowed_hosts
            if host not in internal_hosts and ":" not in host
        )
        return origins

    @classmethod
    def from_env(cls) -> EdgeAuthConfig:
        """Loads edge authentication configuration from environment and secret files."""
        # Enable flag
        enabled_str = (
            get_secret("CF_ACCESS_ENABLED", os.getenv("CF_ACCESS_ENABLED", ""))
            .strip()
            .lower()
        )
        runtime_mode = os.getenv("JOB_APPLIER_RUNTIME_MODE", "").strip().lower()

        # In production service mode, default to true unless explicitly disabled
        if enabled_str:
            cf_access_enabled = enabled_str in ("true", "1", "yes", "on")
        elif runtime_mode == "service" and get_secret("CF_ACCESS_AUD"):
            cf_access_enabled = True
        else:
            cf_access_enabled = False

        cf_access_aud = get_secret(
            "CF_ACCESS_AUD", os.getenv("CF_ACCESS_AUD", "")
        ).strip()
        cf_access_team_domain = get_secret(
            "CF_ACCESS_TEAM_DOMAIN", os.getenv("CF_ACCESS_TEAM_DOMAIN", "")
        ).strip()

        # Allowed identities (strict email allowlist)
        raw_identities = get_secret(
            "CF_ACCESS_ALLOWED_IDENTITIES",
            os.getenv("CF_ACCESS_ALLOWED_IDENTITIES", ""),
        )
        allowed_identities = {
            e.strip().lower() for e in re.split(r"[,;]+", raw_identities) if e.strip()
        }

        # Custom certs URL
        certs_url = os.getenv("CF_ACCESS_CERTS_URL", "").strip()
        if not certs_url and cf_access_team_domain:
            team_url = normalize_team_domain(cf_access_team_domain)
            if team_url:
                certs_url = f"{team_url}/cdn-cgi/access/certs"

        # Public canonical origin
        public_origin = (
            os.getenv("PUBLIC_ORIGIN", "https://jobs.aslan.net").strip().rstrip("/")
        )
        if not public_origin:
            public_origin = "https://jobs.aslan.net"

        # Allowed hosts
        raw_hosts = os.getenv("ALLOWED_HOSTS", "").strip()
        if raw_hosts:
            allowed_hosts = {
                h.strip().lower() for h in raw_hosts.split(",") if h.strip()
            }
            # Always ensure canonical origin host is present
            origin_host = public_origin.split("://")[-1].split(":")[0].lower()
            allowed_hosts.add(origin_host)
            allowed_hosts.add("localhost")
            allowed_hosts.add("127.0.0.1")
            allowed_hosts.add("testserver")
        else:
            origin_host = public_origin.split("://")[-1].split(":")[0].lower()
            allowed_hosts = {
                origin_host,
                "jobs.aslan.net",
                "aslan.archnet.lol",
                "localhost",
                "127.0.0.1",
                "web",
                "gateway",
                "testserver",
            }

        # Trusted proxies CIDRs
        trusted_proxies = [
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("::1/128"),
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
        ]
        raw_proxies = os.getenv("TRUSTED_PROXIES", "").strip()
        if raw_proxies:
            for cidr in raw_proxies.split(","):
                try:
                    trusted_proxies.append(ipaddress.ip_network(cidr.strip()))
                except ValueError:
                    logger.warning(f"Invalid TRUSTED_PROXIES entry: {cidr}")

        try:
            viewer_duration = int(os.getenv("VIEWER_MAX_DURATION_SECONDS", "300"))
        except ValueError:
            viewer_duration = 300

        local_gateway_viewer_token = get_secret(
            "LOCAL_GATEWAY_VIEWER_TOKEN",
            os.getenv("LOCAL_GATEWAY_VIEWER_TOKEN", ""),
        ).strip()

        return cls(
            cf_access_enabled=cf_access_enabled,
            cf_access_aud=cf_access_aud,
            cf_access_team_domain=cf_access_team_domain,
            cf_access_allowed_identities=allowed_identities,
            cf_access_certs_url=certs_url,
            public_origin=public_origin,
            allowed_hosts=allowed_hosts,
            trusted_proxies=trusted_proxies,
            viewer_max_duration_seconds=min(300, max(30, viewer_duration)),
            local_gateway_viewer_token=local_gateway_viewer_token,
        )


class JwksCache:
    """
    Bounded in-memory cache for Cloudflare Access RSA public keys.
    Enforces capacity bounds, rate-limited refresh, and network timeout.
    """

    def __init__(self, max_keys: int = 16) -> None:
        self.max_keys = max_keys
        self._keys: dict[str, rsa.RSAPublicKey] = {}
        self._key_fetched_at: float = 0.0
        self._last_fetched: float = 0.0
        self._injected_keys: dict[str, rsa.RSAPublicKey] = {}

    def inject_key(self, kid: str, pub_key: rsa.RSAPublicKey) -> None:
        """Injects a public key directly for hermetic testing."""
        self._injected_keys[kid] = pub_key

    def clear(self) -> None:
        """Clears all cached and injected keys."""
        self._keys.clear()
        self._injected_keys.clear()
        self._key_fetched_at = 0.0
        self._last_fetched = 0.0

    def get_key(
        self,
        kid: str,
        certs_url: str,
        rate_limit_seconds: float = 60.0,
        timeout_seconds: float = 5.0,
        cache_ttl_seconds: float = 3600.0,
    ) -> rsa.RSAPublicKey | None:
        """
        Retrieves public key by kid. Refreshes from certs_url if unknown and rate limit permits.
        """
        if kid in self._injected_keys:
            return self._injected_keys[kid]

        now = time.time()
        if kid in self._keys:
            if now - self._key_fetched_at <= cache_ttl_seconds:
                return self._keys[kid]
            # Never continue trusting an expired key while a refresh is
            # pending; rotation must fail closed rather than use stale keys.
            self._keys.clear()

        # Rate-limit remote refresh
        if now - self._last_fetched < rate_limit_seconds:
            logger.warning(
                f"Rate-limiting JWKS refresh for kid {kid}. Last fetch was {now - self._last_fetched:.1f}s ago."
            )
            return None

        if not certs_url:
            logger.warning("JWKS certs_url is empty; cannot fetch public keys.")
            return None

        self._refresh(certs_url, timeout_seconds)
        return self._keys.get(kid)

    def _refresh(self, certs_url: str, timeout_seconds: float) -> None:
        """Fetches and parses JWKS keys with strict bounded capacity."""
        self._last_fetched = time.time()
        try:
            req = urllib.request.Request(
                certs_url,
                headers={
                    "User-Agent": "job-applier-edge-auth/1.0",
                    "Accept": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            keys_data = data.get("keys", [])
            new_keys: dict[str, rsa.RSAPublicKey] = {}

            for k in keys_data:
                if k.get("kty") == "RSA" and k.get("alg") == "RS256" and "kid" in k:
                    try:
                        n = b64url_to_int(k["n"])
                        e = b64url_to_int(k["e"])
                        pub = rsa.RSAPublicNumbers(e=e, n=n).public_key()
                        new_keys[k["kid"]] = pub
                    except Exception as e:
                        logger.warning(f"Failed to parse JWK key {k.get('kid')}: {e}")

            # Keep cache bounded
            if len(new_keys) > self.max_keys:
                # Truncate to max_keys
                new_keys = dict(list(new_keys.items())[: self.max_keys])

            self._keys = new_keys
            self._key_fetched_at = time.time()
            logger.info(f"Loaded {len(self._keys)} RSA public keys from {certs_url}")
        except Exception as e:
            logger.error(f"Failed to fetch JWKS from {certs_url}: {e}")


# Global JWKS Cache instance
_GLOBAL_JWKS_CACHE = JwksCache()


def get_jwks_cache() -> JwksCache:
    """Returns global JWKS cache instance."""
    return _GLOBAL_JWKS_CACHE


def verify_cf_access_jwt(
    token: str,
    config: EdgeAuthConfig,
    jwks_cache: JwksCache | None = None,
) -> dict[str, Any]:
    """
    Cryptographically verifies a Cloudflare Access JWT.
    Strictly enforces:
    - Algorithm: RS256 only (rejects 'none', symmetric HMAC, RS384, etc.)
    - Signature: valid RSA signature using JWKS public key
    - Issuer: exact match with expected Access team domain
    - Audience: exact match with expected Access Application AUD
    - Expiration: valid exp timestamp (not expired)
    - Validity window: nbf/iat with <= 60s clock skew tolerance
    - Identity: exact match in allowlist (no wildcards)
    """
    if jwks_cache is None:
        jwks_cache = get_jwks_cache()

    parts = token.strip().split(".")
    if len(parts) != 3:
        raise EdgeAuthError(401, "Malformed JWT: expected 3 dot-separated segments")

    header_b64, payload_b64, sig_b64 = parts

    # 1. Parse Header
    try:
        header = json.loads(b64url_decode(header_b64).decode("utf-8"))
    except Exception as e:
        raise EdgeAuthError(401, f"Malformed JWT header: {e}")

    alg = header.get("alg")
    if alg != "RS256":
        raise EdgeAuthError(
            401, f"Forbidden algorithm '{alg}': only RS256 is permitted"
        )

    kid = header.get("kid")
    if not kid:
        raise EdgeAuthError(401, "Missing 'kid' in token header")

    # 2. Retrieve RSA Public Key
    pub_key = jwks_cache.get_key(
        kid=kid,
        certs_url=config.cf_access_certs_url,
        rate_limit_seconds=config.jwks_rate_limit_seconds,
        timeout_seconds=config.jwks_timeout_seconds,
        cache_ttl_seconds=config.jwks_cache_ttl_seconds,
    )
    if not pub_key:
        raise EdgeAuthError(401, f"Unknown or unverified key ID '{kid}'")

    # 3. Verify Signature
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    try:
        sig_bytes = b64url_decode(sig_b64)
        pub_key.verify(sig_bytes, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise EdgeAuthError(401, "Invalid cryptographic token signature")
    except Exception as e:
        raise EdgeAuthError(401, f"Signature verification error: {e}")

    # 4. Parse Payload
    try:
        payload = json.loads(b64url_decode(payload_b64).decode("utf-8"))
    except Exception as e:
        raise EdgeAuthError(401, f"Malformed JWT payload: {e}")

    # 5. Verify Issuer
    expected_iss = config.cf_access_issuer
    actual_iss = payload.get("iss", "").rstrip("/")
    if actual_iss != expected_iss.rstrip("/"):
        raise EdgeAuthError(
            401, f"Issuer mismatch: expected '{expected_iss}', got '{actual_iss}'"
        )

    # 6. Verify Audience
    expected_aud = config.cf_access_aud
    aud = payload.get("aud")
    if isinstance(aud, list):
        if expected_aud not in aud:
            raise EdgeAuthError(
                401, f"Audience mismatch: expected '{expected_aud}' in {aud}"
            )
    elif isinstance(aud, str):
        if aud != expected_aud:
            raise EdgeAuthError(
                401, f"Audience mismatch: expected '{expected_aud}', got '{aud}'"
            )
    else:
        raise EdgeAuthError(401, "Missing or invalid 'aud' claim in token")

    # 7. Verify Expiration
    exp = payload.get("exp")
    if exp is None or not isinstance(exp, (int, float)):
        raise EdgeAuthError(401, "Missing 'exp' claim in token")

    now = time.time()
    if now >= exp:
        raise EdgeAuthError(401, f"Token has expired (exp: {exp}, now: {int(now)})")

    # 8. Check Clock Skew (nbf, iat)
    for claim_name, claim_label in (("nbf", "nbf"), ("iat", "iat")):
        if claim_name not in payload:
            continue
        claim_value = payload[claim_name]
        if isinstance(claim_value, bool) or not isinstance(claim_value, (int, float)):
            raise EdgeAuthError(401, f"Malformed {claim_label} claim")
        if claim_value > now + 60:
            detail = "not yet active" if claim_name == "nbf" else "issued in future"
            raise EdgeAuthError(401, f"Token {detail} ({claim_label} in future)")

    # 9. Verify Identity against Allowlist
    email = payload.get("email", "").strip().lower()
    if not email:
        email = payload.get("sub", "").strip().lower()

    if not email:
        raise EdgeAuthError(401, "Token missing email or identity claim")

    if email not in config.cf_access_allowed_identities:
        raise EdgeAuthError(403, f"Identity '{email}' is not in authorized allowlist")

    return {
        "email": email,
        "sub": payload.get("sub"),
        "exp": exp,
        "iat": payload.get("iat"),
        "identity_provider": payload.get("identity_provider"),
    }


def validate_edge_auth_startup(config: EdgeAuthConfig) -> None:
    """
    Enforces fail-closed startup validation for Edge Authentication.
    If Cloudflare Access is enabled, strictly verifies that all required configuration
    parameters are present, non-empty, and valid.
    """
    if not config.cf_access_enabled:
        logger.info(
            "Cloudflare Access authentication is not enabled (local/development mode)."
        )
        return

    errors: list[str] = []

    if not config.cf_access_aud:
        errors.append("CF_ACCESS_AUD is not set or empty")

    if not config.cf_access_team_domain:
        errors.append("CF_ACCESS_TEAM_DOMAIN is not set or empty")

    if not config.cf_access_allowed_identities:
        errors.append(
            "CF_ACCESS_ALLOWED_IDENTITIES is not set or contains no identities"
        )

    # Check for forbidden wildcards in identity allowlist
    for identity in config.cf_access_allowed_identities:
        if "*" in identity or identity.startswith("@"):
            errors.append(
                f"Wildcard or domain-wide identity '{identity}' is forbidden. Exact identities required."
            )

    if errors:
        msg = (
            f"Edge Authentication startup fail-closed check failed: {'; '.join(errors)}"
        )
        logger.critical(msg)
        raise RuntimeError(msg)

    logger.info(
        f"✅ Edge Auth verified: Team={config.cf_access_team_domain}, AUD={config.cf_access_aud[:8]}..., "
        f"Allowed Identities={len(config.cf_access_allowed_identities)}"
    )


def is_ip_trusted(
    ip_str: str,
    trusted_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    """Checks whether an IP address belongs to trusted proxy CIDRs."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return any(ip in net for net in trusted_networks)
    except ValueError:
        return False


class EdgeAuthMiddleware(BaseHTTPMiddleware):
    """
    Starlette Middleware enforcing:
    - Host header verification (rejecting untrusted/hostile Host headers).
    - Trusted forwarding header filtering.
    - Minimal private health check exemption for loopback/container health checks.
    - Legacy /job-applier route protection: authenticated before redirecting (no bypass!).
    - Cloudflare Access JWT validation when enabled.
    - Exact Origin verification and CSRF protections on mutating requests.
    """

    def __init__(self, app: Any, config: EdgeAuthConfig) -> None:
        super().__init__(app)
        self.config = config

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        # 1. Host Header Validation
        host_header = request.headers.get("host", "").split(":")[0].strip().lower()
        if host_header and host_header not in self.config.allowed_hosts:
            logger.warning(
                f"Rejected request with unexpected Host header: {host_header}"
            )
            return JSONResponse(
                {"detail": f"Invalid Host header: {host_header}"}, status_code=400
            )

        # Determine client IP and proxy trust
        # Missing client metadata is not evidence of a trusted local caller.
        client_ip = request.client.host if request.client else ""
        trusted_client = is_ip_trusted(client_ip, self.config.trusted_proxies)
        if not trusted_client:
            # Strip untrusted forwarding headers to prevent header spoofing
            untrusted_header_prefixes = (b"x-forwarded-", b"x-real-ip")
            request.scope["headers"] = [
                (k, v)
                for (k, v) in request.scope.get("headers", [])
                if not any(k.lower().startswith(p) for p in untrusted_header_prefixes)
            ]

        path = request.url.path

        # 2. Internal health probe exemption
        # Public /api/health remains behind Access. The internal endpoint is only
        # reachable from the web container's own loopback healthcheck.
        internal_health_allowed = False
        if path == "/api/health/internal":
            loopback_client = False
            try:
                loopback_client = ipaddress.ip_address(client_ip).is_loopback
            except ValueError:
                pass
            internal_health_allowed = loopback_client
            if internal_health_allowed:
                return await call_next(request)

        # 3. Viewer gate endpoint handles its own auth checks
        if path == "/api/auth/viewer-gate":
            return await call_next(request)

        # 4. Check Authentication for Protected Routes
        user_info: dict[str, Any] | None = None
        if self.config.cf_access_enabled:
            token = request.headers.get("Cf-Access-Jwt-Assertion")
            if not token:
                token = request.cookies.get("CF_Authorization")

            if not token:
                # If path is /job-applier, it MUST NOT BYPASS authentication!
                return JSONResponse(
                    {
                        "detail": "Missing Cloudflare Access assertion (authentication required)"
                    },
                    status_code=401,
                )

            try:
                user_info = verify_cf_access_jwt(token, self.config)
                request.state.user = user_info
            except EdgeAuthError as e:
                return JSONResponse({"detail": e.message}, status_code=e.status_code)
            except Exception as e:
                logger.error(f"Unexpected error during edge auth: {e}")
                return JSONResponse({"detail": "Authentication error"}, status_code=401)

        if path == "/api/health/internal" and not internal_health_allowed:
            return JSONResponse(
                {"detail": "Internal health endpoint is not publicly reachable"},
                status_code=404,
            )

        # 5. Legacy /job-applier Route Handling
        # After authentication has been verified above, redirect legacy route to canonical root
        if path.startswith("/job-applier"):
            # Strip /job-applier prefix
            canonical_path = path[len("/job-applier") :]
            if not canonical_path.startswith("/"):
                canonical_path = "/" + canonical_path
            if request.url.query:
                canonical_path += f"?{request.url.query}"
            return RedirectResponse(url=canonical_path, status_code=308)

        # 6. Exact Origin & CSRF Checks for Mutating Requests
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            allowed_origins = self.config.allowed_origins
            if not origin:
                referer = request.headers.get("referer")
                if referer:
                    ref_parsed = urllib.parse.urlparse(referer)
                    ref_origin = f"{ref_parsed.scheme}://{ref_parsed.netloc}".rstrip(
                        "/"
                    ).lower()
                    if ref_origin in allowed_origins:
                        origin = ref_origin
            if not origin and (
                self.config.cf_access_enabled or request.cookies.get("CF_Authorization")
            ):
                return JSONResponse(
                    {"detail": "Missing Origin header on mutating request"},
                    status_code=403,
                )
            if origin:
                norm_origin = origin.rstrip("/").lower()
                if norm_origin not in allowed_origins:
                    logger.warning(
                        f"Cross-origin mutation rejected from origin: {origin}"
                    )
                    return JSONResponse(
                        {"detail": "Cross-origin mutation forbidden"},
                        status_code=403,
                    )

        # Continue request processing
        response = await call_next(request)
        return response


@dataclass
class RfbReadOnlyState:
    """Per-viewer protocol phase and authorization-bound fragment buffer.

    Handshake bytes are buffered independently of takeover authorization.
    Once initialized, an incomplete frame is tagged with the authorization
    state present when its first byte arrived; changing that state discards
    the partial frame rather than forwarding bytes across an epoch boundary.
    """

    phase: str = "version"
    buffer: bytes = b""
    buffer_allow_input: bool | None = None


_MAX_RFB_BUFFER_SIZE = 1024 * 1024


def filter_rfb_client_messages(
    data: bytes, allow_input: bool, state: RfbReadOnlyState | None = None
) -> bytes:
    """
    Filter client-to-server RFB messages without breaking initial negotiation.

    WebSocket message boundaries do not necessarily match RFB message
    boundaries. A per-connection buffer therefore retains incomplete version,
    handshake, and post-initialization messages until they can be classified.
    Read-only viewers receive safe framebuffer requests and configuration
    messages, while input messages are dropped only after their full frame is
    available.
    """
    if state is None:
        if not data:
            return data
        # Preserve the standalone helper's historical behavior for callers
        # that do not have a connection-specific protocol phase.
        if data.startswith(b"RFB "):
            return data
        return _filter_initialized_rfb_messages(data, allow_input)

    if not data or state.phase == "rejected":
        return b""

    # Handshake buffering is independent of authorization. Post-init frame
    # fragments, however, must never cross a change in input authorization.
    if (
        state.phase == "initialized"
        and state.buffer
        and state.buffer_allow_input is not None
        and state.buffer_allow_input != allow_input
    ):
        # The current payload is a continuation of a frame begun under the
        # previous authorization epoch. Drop both the retained prefix and this
        # continuation; treating the continuation as a new frame would leak
        # bytes across the authorization boundary.
        state.buffer = b""
        state.buffer_allow_input = None
        return b""

    state.buffer += data
    if len(state.buffer) > _MAX_RFB_BUFFER_SIZE:
        state.buffer = b""
        state.phase = "rejected"
        return b""

    output = bytearray()
    while state.phase != "initialized":
        if state.phase == "version":
            if len(state.buffer) < 12:
                if not b"RFB ".startswith(
                    state.buffer[:4]
                ) and not state.buffer.startswith(b"RFB "):
                    state.buffer = b""
                    state.phase = "rejected"
                return bytes(output)
            version = state.buffer[:12]
            if not version.startswith(b"RFB ") or not version.endswith(b"\n"):
                state.buffer = b""
                state.phase = "rejected"
                return bytes(output)
            output.extend(version)
            state.buffer = state.buffer[12:]
            state.phase = "security_selection"
        elif state.phase == "security_selection":
            if len(state.buffer) < 1:
                return bytes(output)
            selection = state.buffer[:1]
            state.buffer = state.buffer[1:]
            if selection[0] == 0:
                state.phase = "rejected"
                state.buffer = b""
                return bytes(output)
            output.extend(selection)
            # Classic VNC authentication (security type 2) adds a 16-byte
            # challenge response before the one-byte ClientInit message.
            state.phase = "vnc_auth_response" if selection[0] == 2 else "client_init"
        elif state.phase == "vnc_auth_response":
            if len(state.buffer) < 16:
                return bytes(output)
            output.extend(state.buffer[:16])
            state.buffer = state.buffer[16:]
            state.phase = "client_init"
        elif state.phase == "client_init":
            if len(state.buffer) < 1:
                return bytes(output)
            client_init = state.buffer[:1]
            state.buffer = state.buffer[1:]
            if client_init[0] not in (0, 1):
                state.phase = "rejected"
                state.buffer = b""
                return bytes(output)
            output.extend(client_init)
            state.phase = "initialized"
        else:
            state.phase = "rejected"
            state.buffer = b""
            return bytes(output)

    if state.buffer and state.buffer_allow_input is None:
        state.buffer_allow_input = allow_input

    output.extend(
        _filter_initialized_rfb_messages(
            state.buffer, allow_input=allow_input, state=state
        )
    )
    return bytes(output)


def _filter_initialized_rfb_messages(
    data: bytes, allow_input: bool, state: RfbReadOnlyState | None = None
) -> bytes:
    """Filter complete post-handshake frames, retaining fragmented input."""
    if not data:
        return b""

    if state is None:
        buffer = data
    else:
        buffer = data

    offset = 0
    clean_chunks: list[bytes] = []
    while offset < len(buffer):
        msg_type = buffer[offset]
        if msg_type == 0:  # SetPixelFormat
            msg_len = 20
        elif msg_type == 2:  # SetEncodings
            if len(buffer) - offset < 4:
                break
            num_enc = struct.unpack(">H", buffer[offset + 2 : offset + 4])[0]
            msg_len = 4 + 4 * num_enc
        elif msg_type == 3:  # FramebufferUpdateRequest
            msg_len = 10
        elif msg_type == 4:  # KeyEvent
            msg_len = 8
        elif msg_type == 5:  # PointerEvent
            msg_len = 6
        elif msg_type == 6:  # ClientCutText
            if len(buffer) - offset < 8:
                break
            text_len = struct.unpack(">I", buffer[offset + 4 : offset + 8])[0]
            msg_len = 8 + text_len
        else:
            # Unknown message types are never buffered or forwarded. Return
            # immediately for stateful connections so the rejected bytes are
            # not restored by the fragmented-buffer assignment below.
            if state is not None:
                state.buffer = b""
                state.buffer_allow_input = None
                return b"".join(clean_chunks)
            break

        if len(buffer) - offset < msg_len:
            break
        if allow_input or msg_type not in (4, 5, 6):
            clean_chunks.append(buffer[offset : offset + msg_len])
        offset += msg_len

    if state is not None:
        state.buffer = buffer[offset:]
        if not state.buffer:
            state.buffer_allow_input = None
    return b"".join(clean_chunks)


def parse_viewer_subprotocols(header_value: str) -> tuple[str, ...]:
    """Return normalized WebSocket subprotocol tokens offered by the viewer."""
    return tuple(
        token.strip().lower() for token in header_value.split(",") if token.strip()
    )


def viewer_input_authorized(
    viewer_owner: str, takeover_active: bool, takeover_owner: str | None
) -> bool:
    """Authorize input only for the identity holding the current takeover lease."""
    return takeover_active and bool(takeover_owner) and takeover_owner == viewer_owner


async def handle_viewer_websocket(
    websocket: WebSocket,
    config: EdgeAuthConfig,
    upstream_url: str | None = None,
) -> None:
    """
    Handles noVNC/websockify WebSocket upgrade connection.
    Enforces server-side 5-minute / token-expiry lifetime.
    Terminates the connection immediately when max duration or token expiry elapses.
    """
    import websockets

    if upstream_url is None:
        upstream_url = os.getenv("RUNTIME_WEBSOCKIFY_URL", "ws://runtime:6080")

    # 1. Exact Origin Verification for WebSocket Upgrade
    origin = websocket.headers.get("origin")
    allowed_origins = config.allowed_origins
    if not origin:
        logger.warning("Viewer WebSocket upgrade rejected: missing Origin header")
        await websocket.close(
            code=1008, reason="Missing Origin header for WebSocket upgrade"
        )
        return

    norm_origin = origin.rstrip("/").lower()
    if norm_origin not in allowed_origins:
        logger.warning(f"WebSocket upgrade rejected due to untrusted origin: {origin}")
        await websocket.close(
            code=1008, reason="Cross-origin WebSocket upgrade forbidden"
        )
        return

    # 2. Authenticate WebSocket Upgrade Request. The verified identity is also
    # the only owner value accepted for input authorization in Access mode.
    token_exp: float | None = None
    viewer_owner = "operator"
    if config.cf_access_enabled:
        token = websocket.headers.get("Cf-Access-Jwt-Assertion")
        if not token:
            token = websocket.cookies.get("CF_Authorization")

        if not token:
            logger.warning(
                "Viewer WebSocket connection rejected: missing CF Access token"
            )
            await websocket.close(
                code=1008, reason="Missing Cloudflare Access authentication"
            )
            return

        try:
            payload = verify_cf_access_jwt(token, config)
            token_exp = payload["exp"]
            viewer_owner = str(payload["email"]).strip().lower()
        except EdgeAuthError as e:
            logger.warning(f"Viewer WebSocket auth failed: {e.message}")
            await websocket.close(code=1008, reason=e.message)
            return

    # 3. Compute Strict Connection Lifetime (min(5 minutes, token_exp - now))
    now = time.time()
    if token_exp is not None:
        time_to_exp = max(0.0, token_exp - now)
        max_duration = min(float(config.viewer_max_duration_seconds), time_to_exp)
    else:
        max_duration = float(config.viewer_max_duration_seconds)

    if max_duration <= 0:
        await websocket.close(code=1008, reason="Token already expired")
        return

    # 4. Negotiate the client protocol independently from the upstream VNC protocol.
    # vnc_lite.html creates WebSocket(url) without an offered protocol, while some
    # noVNC clients offer ``binary`` explicitly. Never select a protocol the client
    # did not offer: browsers reject that handshake with code 1006.
    offered_protocols = parse_viewer_subprotocols(
        websocket.headers.get("sec-websocket-protocol", "")
    )
    if offered_protocols and "binary" not in offered_protocols:
        logger.warning(
            "Viewer WebSocket upgrade rejected: unsupported subprotocols %s",
            offered_protocols,
        )
        await websocket.close(code=1002, reason="Unsupported WebSocket subprotocol")
        return

    if "binary" in offered_protocols:
        await websocket.accept(subprotocol="binary")
    else:
        await websocket.accept()
    logger.info(
        "Viewer WebSocket connection accepted%s. Server-side lifetime capped at %.1fs.",
        " with binary subprotocol"
        if "binary" in offered_protocols
        else " without subprotocol",
        max_duration,
    )

    # 5. Connect to Upstream runtime:6080 and Pump Frames
    rfb_state = RfbReadOnlyState()
    try:
        async with websockets.connect(
            upstream_url,
            subprotocols=cast(Any, ["binary"]),  # type: ignore[arg-type]
            open_timeout=5.0,
            ping_interval=20,
            ping_timeout=20,
        ) as upstream_ws:

            async def client_to_upstream() -> None:
                from job_applier.automation.queue import (
                    TAKEOVER_TRANSPORT_LOCK,
                    is_takeover_active,
                )

                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            break
                        if "bytes" in msg:
                            data = msg["bytes"]
                        elif "text" in msg:
                            data = msg["text"].encode("utf-8")
                        else:
                            continue

                        # Hold the same process-wide fence used by takeover
                        # claim/release through the upstream send boundary. Poll
                        # non-blockingly so cancellation cannot strand a worker
                        # thread that acquired the lock after its awaiter stopped.
                        while not TAKEOVER_TRANSPORT_LOCK.acquire(blocking=False):
                            await asyncio.sleep(0.01)
                        try:
                            takeover_active, takeover_owner, _ = is_takeover_active()
                            allow_input = viewer_input_authorized(
                                viewer_owner, takeover_active, takeover_owner
                            )
                            filtered_data = filter_rfb_client_messages(
                                data, allow_input=allow_input, state=rfb_state
                            )
                            if filtered_data:
                                await upstream_ws.send(filtered_data)
                        finally:
                            TAKEOVER_TRANSPORT_LOCK.release()
                except (WebSocketDisconnect, websockets.ConnectionClosed):
                    pass

            async def upstream_to_client() -> None:
                try:
                    async for message in upstream_ws:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)
                except (WebSocketDisconnect, websockets.ConnectionClosed):
                    pass

            # Run forwarders bounded by server-side max_duration
            try:
                done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(client_to_upstream()),
                        asyncio.create_task(upstream_to_client()),
                    ],
                    timeout=max_duration,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()

                for task in done:
                    if task.exception():
                        logger.warning(
                            f"Viewer WebSocket relay task exception: {task.exception()}"
                        )

                # Check if lifetime expired
                if time.time() - now >= max_duration:
                    logger.info(
                        "Viewer session lifetime expired. Server closing WebSocket."
                    )
                    await websocket.close(
                        code=1000,
                        reason="Viewer session expired (5-minute maximum lifetime). Reauthentication required.",
                    )
            except Exception as e:
                logger.warning(f"Error during viewer WebSocket relay: {e}")

    except Exception as e:
        logger.error(
            f"Failed to connect to upstream viewer service at {upstream_url}: {e}"
        )
        try:
            await websocket.close(code=1011, reason="Upstream viewer unavailable")
        except Exception:
            pass
