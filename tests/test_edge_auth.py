"""
Unit and integration tests for Edge Authentication, Cloudflare Access JWT validation,
Origin checks, CSRF, viewer upgrade authorization, and fail-closed security.
"""

from __future__ import annotations

from job_applier.web.edge_auth import filter_rfb_client_messages
import os
import struct

import base64
import json
import time
from typing import Any, cast
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from job_applier.web.app import app
from job_applier.web.edge_auth import (
    EdgeAuthConfig,
    EdgeAuthError,
    EdgeAuthMiddleware,
    JwksCache,
    handle_viewer_websocket,
    normalize_team_domain,
    viewer_input_authorized,
    validate_edge_auth_startup,
    verify_cf_access_jwt,
)


# ---------------------------------------------------------------------------
# Test Key Pair and Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_key_pair():
    """Generates an RSA 2048-bit key pair for JWT signing and verification tests."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return private_key, public_key


def int_to_b64url(val: int) -> str:
    byte_len = (val.bit_length() + 7) // 8
    raw = val.to_bytes(byte_len, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def create_signed_jwt(
    private_key: rsa.RSAPrivateKey,
    kid: str = "test-key-1",
    alg: str = "RS256",
    iss: str = "https://aslan.cloudflareaccess.com",
    aud: str | list[str] = "test-aud-12345",
    email: str = "operator@aslan.net",
    exp: float | None = None,
    nbf: float | None = None,
    iat: float | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Creates a JWT signed with the given private key or arbitrary alg for testing."""
    now = time.time()
    header = {"alg": alg, "typ": "JWT", "kid": kid}
    payload: dict[str, Any] = {
        "iss": iss,
        "aud": aud,
        "email": email,
        "sub": "user_id_123",
        "exp": int(now + 3600) if exp is None else int(exp),
        "iat": int(now) if iat is None else int(iat),
    }
    if nbf is not None:
        payload["nbf"] = int(nbf)
    if extra_claims:
        payload.update(extra_claims)

    h_b64 = (
        base64.urlsafe_b64encode(json.dumps(header).encode("utf-8"))
        .rstrip(b"=")
        .decode("ascii")
    )
    p_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8"))
        .rstrip(b"=")
        .decode("ascii")
    )
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")

    if alg == "RS256":
        sig = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    elif alg == "none":
        sig_b64 = ""
    else:
        sig_b64 = (
            base64.urlsafe_b64encode(b"dummy_signature").rstrip(b"=").decode("ascii")
        )

    return f"{h_b64}.{p_b64}.{sig_b64}"


# ---------------------------------------------------------------------------
# Strict JWT Validation Unit Tests
# ---------------------------------------------------------------------------


class TestStrictJwtValidation:
    """Tests strict algorithm, signature, iss, aud, exp, and allowlist validation."""

    def test_valid_token_succeeds(self, rsa_key_pair):
        private_key, public_key = rsa_key_pair
        jwks = JwksCache()
        jwks.inject_key("test-key-1", public_key)

        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="test-aud-12345",
            cf_access_team_domain="aslan",
            cf_access_allowed_identities={"operator@aslan.net"},
        )

        token = create_signed_jwt(
            private_key, kid="test-key-1", email="operator@aslan.net"
        )
        claims = verify_cf_access_jwt(token, config, jwks)

        assert claims["email"] == "operator@aslan.net"
        assert claims["sub"] == "user_id_123"
        assert claims["exp"] > time.time()

    def test_rejects_algorithm_none(self, rsa_key_pair):
        private_key, public_key = rsa_key_pair
        jwks = JwksCache()
        jwks.inject_key("test-key-1", public_key)

        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="test-aud-12345",
            cf_access_team_domain="aslan",
            cf_access_allowed_identities={"operator@aslan.net"},
        )

        token = create_signed_jwt(private_key, kid="test-key-1", alg="none")
        with pytest.raises(EdgeAuthError) as exc_info:
            verify_cf_access_jwt(token, config, jwks)

        assert exc_info.value.status_code == 401
        assert "only RS256 is permitted" in exc_info.value.message

    def test_rejects_algorithm_hs256(self, rsa_key_pair):
        private_key, public_key = rsa_key_pair
        jwks = JwksCache()
        jwks.inject_key("test-key-1", public_key)

        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="test-aud-12345",
            cf_access_team_domain="aslan",
            cf_access_allowed_identities={"operator@aslan.net"},
        )

        token = create_signed_jwt(private_key, kid="test-key-1", alg="HS256")
        with pytest.raises(EdgeAuthError) as exc_info:
            verify_cf_access_jwt(token, config, jwks)

        assert exc_info.value.status_code == 401
        assert "only RS256 is permitted" in exc_info.value.message

    def test_rejects_invalid_signature(self, rsa_key_pair):
        private_key, public_key = rsa_key_pair
        # Another key
        other_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        jwks = JwksCache()
        jwks.inject_key("test-key-1", public_key)  # Key 1 in cache

        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="test-aud-12345",
            cf_access_team_domain="aslan",
            cf_access_allowed_identities={"operator@aslan.net"},
        )

        # Signed with other key!
        token = create_signed_jwt(other_priv, kid="test-key-1")
        with pytest.raises(EdgeAuthError) as exc_info:
            verify_cf_access_jwt(token, config, jwks)

        assert exc_info.value.status_code == 401
        assert "signature" in exc_info.value.message.lower()

    def test_rejects_malformed_clock_claims(self, rsa_key_pair):
        private_key, public_key = rsa_key_pair
        jwks = JwksCache()
        jwks.inject_key("test-key-1", public_key)
        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="test-aud-12345",
            cf_access_team_domain="aslan",
            cf_access_allowed_identities={"operator@aslan.net"},
        )
        for claim in ("nbf", "iat"):
            token = create_signed_jwt(
                private_key,
                extra_claims={claim: "not-a-number"},
            )
            with pytest.raises(EdgeAuthError, match=f"Malformed {claim}"):
                verify_cf_access_jwt(token, config, jwks)

    def test_rejects_expired_token(self, rsa_key_pair):
        private_key, public_key = rsa_key_pair
        jwks = JwksCache()
        jwks.inject_key("test-key-1", public_key)

        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="test-aud-12345",
            cf_access_team_domain="aslan",
            cf_access_allowed_identities={"operator@aslan.net"},
        )

        expired_time = time.time() - 300
        token = create_signed_jwt(private_key, kid="test-key-1", exp=expired_time)

        with pytest.raises(EdgeAuthError) as exc_info:
            verify_cf_access_jwt(token, config, jwks)

        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.message.lower()

    def test_rejects_mismatched_issuer(self, rsa_key_pair):
        private_key, public_key = rsa_key_pair
        jwks = JwksCache()
        jwks.inject_key("test-key-1", public_key)

        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="test-aud-12345",
            cf_access_team_domain="aslan",
            cf_access_allowed_identities={"operator@aslan.net"},
        )

        token = create_signed_jwt(
            private_key,
            kid="test-key-1",
            iss="https://malicious.cloudflareaccess.com",
        )

        with pytest.raises(EdgeAuthError) as exc_info:
            verify_cf_access_jwt(token, config, jwks)

        assert exc_info.value.status_code == 401
        assert "Issuer mismatch" in exc_info.value.message

    def test_rejects_mismatched_audience(self, rsa_key_pair):
        private_key, public_key = rsa_key_pair
        jwks = JwksCache()
        jwks.inject_key("test-key-1", public_key)

        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="test-aud-12345",
            cf_access_team_domain="aslan",
            cf_access_allowed_identities={"operator@aslan.net"},
        )

        token = create_signed_jwt(
            private_key, kid="test-key-1", aud="wrong-audience-tag"
        )

        with pytest.raises(EdgeAuthError) as exc_info:
            verify_cf_access_jwt(token, config, jwks)

        assert exc_info.value.status_code == 401
        assert "Audience mismatch" in exc_info.value.message

    def test_rejects_unauthorized_identity_allowlist(self, rsa_key_pair):
        private_key, public_key = rsa_key_pair
        jwks = JwksCache()
        jwks.inject_key("test-key-1", public_key)

        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="test-aud-12345",
            cf_access_team_domain="aslan",
            cf_access_allowed_identities={"operator@aslan.net"},  # Exact allowlist!
        )

        # Valid signature, but attacker's email!
        token = create_signed_jwt(
            private_key, kid="test-key-1", email="attacker@gmail.com"
        )

        with pytest.raises(EdgeAuthError) as exc_info:
            verify_cf_access_jwt(token, config, jwks)

        assert exc_info.value.status_code == 403
        assert "not in authorized allowlist" in exc_info.value.message

    def test_rejects_missing_kid(self, rsa_key_pair):
        private_key, public_key = rsa_key_pair
        jwks = JwksCache()
        jwks.inject_key("test-key-1", public_key)

        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="test-aud-12345",
            cf_access_team_domain="aslan",
            cf_access_allowed_identities={"operator@aslan.net"},
        )

        token = create_signed_jwt(private_key, kid="")  # Missing kid
        with pytest.raises(EdgeAuthError) as exc_info:
            verify_cf_access_jwt(token, config, jwks)

        assert exc_info.value.status_code == 401
        assert "kid" in exc_info.value.message.lower()

    def test_rejects_unknown_kid(self, rsa_key_pair):
        private_key, public_key = rsa_key_pair
        jwks = JwksCache()
        jwks.inject_key("known-key", public_key)

        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="test-aud-12345",
            cf_access_team_domain="aslan",
            cf_access_allowed_identities={"operator@aslan.net"},
        )

        token = create_signed_jwt(private_key, kid="unknown-kid-xyz")
        with pytest.raises(EdgeAuthError) as exc_info:
            verify_cf_access_jwt(token, config, jwks)

        assert exc_info.value.status_code == 401
        assert "Unknown or unverified key ID" in exc_info.value.message


# ---------------------------------------------------------------------------
# Team Domain Normalization Tests
# ---------------------------------------------------------------------------


class TestTeamDomainNormalization:
    """Verifies that bare subdomains, full hostnames, and URLs are normalized without double suffixing."""

    def test_normalize_team_domain_variations(self):
        expected = "https://aged-sunset-0292.cloudflareaccess.com"

        # Bare subdomain
        assert normalize_team_domain("aged-sunset-0292") == expected
        assert normalize_team_domain("  aged-sunset-0292  ") == expected

        # Full host
        assert (
            normalize_team_domain("aged-sunset-0292.cloudflareaccess.com") == expected
        )
        assert (
            normalize_team_domain("  AGED-SUNSET-0292.CLOUDFLAREACCESS.COM  ")
            == expected
        )

        # Full URL (https and http)
        assert (
            normalize_team_domain("https://aged-sunset-0292.cloudflareaccess.com")
            == expected
        )
        assert (
            normalize_team_domain("https://aged-sunset-0292.cloudflareaccess.com/")
            == expected
        )
        assert (
            normalize_team_domain("http://aged-sunset-0292.cloudflareaccess.com")
            == expected
        )

        # Empty / whitespace
        assert normalize_team_domain("") == ""
        assert normalize_team_domain("   ") == ""

    def test_edge_auth_config_prevents_double_suffix(self):
        # Initialized with full domain
        cfg_full = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="test-aud",
            cf_access_team_domain="aged-sunset-0292.cloudflareaccess.com",
            cf_access_allowed_identities={"operator@aslan.net"},
        )
        assert (
            cfg_full.cf_access_issuer == "https://aged-sunset-0292.cloudflareaccess.com"
        )
        assert (
            cfg_full.cf_access_certs_url
            == "https://aged-sunset-0292.cloudflareaccess.com/cdn-cgi/access/certs"
        )
        assert (
            ".cloudflareaccess.com.cloudflareaccess.com"
            not in cfg_full.cf_access_certs_url
        )

        # Initialized with bare subdomain
        cfg_bare = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="test-aud",
            cf_access_team_domain="aged-sunset-0292",
            cf_access_allowed_identities={"operator@aslan.net"},
        )
        assert (
            cfg_bare.cf_access_issuer == "https://aged-sunset-0292.cloudflareaccess.com"
        )
        assert (
            cfg_bare.cf_access_certs_url
            == "https://aged-sunset-0292.cloudflareaccess.com/cdn-cgi/access/certs"
        )

    def test_from_env_normalizes_team_domain_and_certs_url(self, monkeypatch):
        monkeypatch.setenv("CF_ACCESS_ENABLED", "true")
        monkeypatch.setenv("CF_ACCESS_AUD", "aud-999")
        monkeypatch.setenv(
            "CF_ACCESS_TEAM_DOMAIN", "aged-sunset-0292.cloudflareaccess.com"
        )
        monkeypatch.setenv("CF_ACCESS_ALLOWED_IDENTITIES", "ervin.popescu10@gmail.com")
        monkeypatch.setenv(
            "ALLOWED_HOSTS", "jobs.archnet.lol,aslan.archnet.lol,localhost"
        )
        monkeypatch.delenv("CF_ACCESS_CERTS_URL", raising=False)

        config = EdgeAuthConfig.from_env()
        assert (
            config.cf_access_issuer == "https://aged-sunset-0292.cloudflareaccess.com"
        )
        assert (
            config.cf_access_certs_url
            == "https://aged-sunset-0292.cloudflareaccess.com/cdn-cgi/access/certs"
        )
        assert (
            ".cloudflareaccess.com.cloudflareaccess.com"
            not in config.cf_access_certs_url
        )
        assert "https://aslan.archnet.lol" in config.allowed_origins


# ---------------------------------------------------------------------------
# Fail-Closed Startup Tests
# ---------------------------------------------------------------------------


class TestFailClosedStartup:
    """Verifies that invalid or insecure configuration fails closed at startup."""

    def test_valid_config_succeeds(self):
        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="audit-tag-12345",
            cf_access_team_domain="aslan",
            cf_access_allowed_identities={"ervin.popescu10@gmail.com"},
        )
        validate_edge_auth_startup(config)  # Should not raise

    def test_missing_aud_fails_closed(self):
        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="",
            cf_access_team_domain="aslan",
            cf_access_allowed_identities={"ervin.popescu10@gmail.com"},
        )
        with pytest.raises(RuntimeError) as exc_info:
            validate_edge_auth_startup(config)
        assert "CF_ACCESS_AUD is not set" in str(exc_info.value)

    def test_missing_team_domain_fails_closed(self):
        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="audit-tag-12345",
            cf_access_team_domain="",
            cf_access_allowed_identities={"ervin.popescu10@gmail.com"},
        )
        with pytest.raises(RuntimeError) as exc_info:
            validate_edge_auth_startup(config)
        assert "CF_ACCESS_TEAM_DOMAIN is not set" in str(exc_info.value)

    def test_empty_identities_fails_closed(self):
        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="audit-tag-12345",
            cf_access_team_domain="aslan",
            cf_access_allowed_identities=set(),
        )
        with pytest.raises(RuntimeError) as exc_info:
            validate_edge_auth_startup(config)
        assert "CF_ACCESS_ALLOWED_IDENTITIES is not set" in str(exc_info.value)

    def test_wildcard_identity_fails_closed(self):
        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="audit-tag-12345",
            cf_access_team_domain="aslan",
            cf_access_allowed_identities={"*@example.com"},
        )
        with pytest.raises(RuntimeError) as exc_info:
            validate_edge_auth_startup(config)
        assert "Wildcard or domain-wide identity" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Bounded JWKS Cache Tests
# ---------------------------------------------------------------------------


class TestBoundedJwksCache:
    """Tests bounded key capacity and rate-limited remote refresh."""

    def test_bounded_capacity(self, rsa_key_pair):
        _, public_key = rsa_key_pair
        cache = JwksCache(max_keys=3)

        jwk_data = {
            "keys": [
                {
                    "kty": "RSA",
                    "alg": "RS256",
                    "kid": f"kid-{i}",
                    "n": int_to_b64url(public_key.public_numbers().n),
                    "e": int_to_b64url(public_key.public_numbers().e),
                }
                for i in range(10)
            ]
        }

        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value.__enter__.return_value.read.return_value = json.dumps(
                jwk_data
            ).encode("utf-8")
            cache.get_key("kid-0", certs_url="https://mock.certs", rate_limit_seconds=0)

        # Must be bounded to max_keys (3)
        assert len(cache._keys) <= 3

    def test_expired_cached_keys_are_refreshed(self, rsa_key_pair):
        _, public_key = rsa_key_pair
        cache = JwksCache()
        jwk_data = {
            "keys": [
                {
                    "kty": "RSA",
                    "alg": "RS256",
                    "kid": "rotating",
                    "n": int_to_b64url(public_key.public_numbers().n),
                    "e": int_to_b64url(public_key.public_numbers().e),
                }
            ]
        }
        with patch("urllib.request.urlopen") as mock_url:
            mock_url.return_value.__enter__.return_value.read.return_value = json.dumps(
                jwk_data
            ).encode("utf-8")
            clock = {"calls": 0}

            def fake_time() -> float:
                clock["calls"] += 1
                return 100.0 if clock["calls"] <= 3 else 200.0

            with patch("job_applier.web.edge_auth.time.time", side_effect=fake_time):
                assert cache.get_key(
                    "rotating",
                    "https://mock.certs",
                    rate_limit_seconds=0,
                    cache_ttl_seconds=10,
                )
                assert cache.get_key(
                    "rotating",
                    "https://mock.certs",
                    rate_limit_seconds=0,
                    cache_ttl_seconds=10,
                )
        assert mock_url.call_count == 2

    def test_rate_limiting(self, rsa_key_pair):
        cache = JwksCache()
        cache._last_fetched = time.time()  # Just fetched!

        # Attempt to get unknown key with 60s rate limit
        with patch("urllib.request.urlopen") as mock_url:
            res = cache.get_key(
                "unknown-kid", certs_url="https://mock.certs", rate_limit_seconds=60.0
            )
            assert res is None
            mock_url.assert_not_called()


# ---------------------------------------------------------------------------
# Request Authentication & Middleware Integration Tests
# ---------------------------------------------------------------------------


class TestEdgeAuthMiddlewareIntegration:
    """Tests full middleware stack: Host checks, JWT auth, legacy route, Origin CSRF, and health."""

    @pytest.fixture
    def auth_client(self, rsa_key_pair):
        private_key, public_key = rsa_key_pair
        jwks = JwksCache()
        jwks.inject_key("test-key-1", public_key)

        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="test-aud-12345",
            cf_access_team_domain="aslan",
            cf_access_allowed_identities={"operator@aslan.net"},
            public_origin="https://jobs.aslan.net",
            allowed_hosts={
                "jobs.aslan.net",
                "aslan.archnet.lol",
                "localhost",
                "127.0.0.1",
                "testserver",
            },
        )

        test_app = FastAPI()
        test_app.add_middleware(EdgeAuthMiddleware, config=config)

        @test_app.get("/api/health")
        def health():
            return {"status": "ok", "service": "job-applier"}

        @test_app.get("/api/health/internal")
        def internal_health():
            return {"status": "ok", "service": "job-applier"}

        @test_app.get("/api/applications")
        def get_apps():
            return {"items": []}

        @test_app.post("/api/applications")
        def create_app():
            return {"status": "created"}

        client = TestClient(test_app, client=("127.0.0.1", 50000))
        return client, private_key, jwks

    def test_unauthenticated_request_rejected(self, auth_client):
        client, _, _ = auth_client
        res = client.get("/api/applications")
        assert res.status_code == 401
        assert "Missing Cloudflare Access assertion" in res.json()["detail"]

    def test_authenticated_request_succeeds(self, auth_client):
        client, private_key, jwks = auth_client
        with patch("job_applier.web.edge_auth.get_jwks_cache", return_value=jwks):
            token = create_signed_jwt(
                private_key, kid="test-key-1", email="operator@aslan.net"
            )
            res = client.get(
                "/api/applications",
                headers={"Cf-Access-Jwt-Assertion": token},
            )
            assert res.status_code == 200
            assert res.json() == {"items": []}

    def test_aslan_alias_host_and_origin_are_allowed(self, auth_client):
        client, private_key, jwks = auth_client
        with patch("job_applier.web.edge_auth.get_jwks_cache", return_value=jwks):
            token = create_signed_jwt(
                private_key, kid="test-key-1", email="operator@aslan.net"
            )
            get_res = client.get(
                "/job-applier/api/applications",
                headers={
                    "Host": "aslan.archnet.lol",
                    "Cf-Access-Jwt-Assertion": token,
                },
                follow_redirects=False,
            )
            assert get_res.status_code == 308
            assert get_res.headers["location"] == "/api/applications"

            post_res = client.post(
                "/api/applications",
                headers={
                    "Host": "aslan.archnet.lol",
                    "Origin": "https://aslan.archnet.lol",
                    "Cf-Access-Jwt-Assertion": token,
                },
                json={"title": "Test"},
            )
            assert post_res.status_code == 200
            assert post_res.json() == {"status": "created"}

    def test_host_header_validation_rejects_attacker_host(self, auth_client):
        client, private_key, jwks = auth_client
        token = create_signed_jwt(
            private_key, kid="test-key-1", email="operator@aslan.net"
        )
        res = client.get(
            "/api/applications",
            headers={
                "Host": "evil.attacker.com",
                "Cf-Access-Jwt-Assertion": token,
            },
        )
        assert res.status_code == 400
        assert "Invalid Host header" in res.json()["detail"]

    def test_legacy_route_cannot_bypass_authentication(self, auth_client):
        client, _, _ = auth_client
        # Attempt to access legacy route /job-applier/api/applications without authentication
        res = client.get("/job-applier/api/applications", follow_redirects=False)
        assert res.status_code == 401  # REJECTED! Cannot bypass authentication!

    def test_legacy_route_with_valid_auth_redirects_to_canonical(self, auth_client):
        client, private_key, jwks = auth_client
        with patch("job_applier.web.edge_auth.get_jwks_cache", return_value=jwks):
            token = create_signed_jwt(
                private_key, kid="test-key-1", email="operator@aslan.net"
            )
            res = client.get(
                "/job-applier/api/applications?limit=10",
                headers={"Cf-Access-Jwt-Assertion": token},
                follow_redirects=False,
            )
            assert res.status_code == 308
            assert res.headers["location"] == "/api/applications?limit=10"

    def test_cross_origin_mutation_rejected(self, auth_client):
        client, private_key, jwks = auth_client
        with patch("job_applier.web.edge_auth.get_jwks_cache", return_value=jwks):
            token = create_signed_jwt(
                private_key, kid="test-key-1", email="operator@aslan.net"
            )
            res = client.post(
                "/api/applications",
                headers={
                    "Cf-Access-Jwt-Assertion": token,
                    "Origin": "https://evil.attacker.com",  # Cross-origin!
                },
                json={"title": "Test"},
            )
            assert res.status_code == 403
            assert "Cross-origin mutation forbidden" in res.json()["detail"]

    def test_same_origin_mutation_allowed(self, auth_client):
        client, private_key, jwks = auth_client
        with patch("job_applier.web.edge_auth.get_jwks_cache", return_value=jwks):
            token = create_signed_jwt(
                private_key, kid="test-key-1", email="operator@aslan.net"
            )
            res = client.post(
                "/api/applications",
                headers={
                    "Cf-Access-Jwt-Assertion": token,
                    "Origin": "https://jobs.aslan.net",  # Canonical Origin!
                },
                json={"title": "Test"},
            )
            assert res.status_code == 200
            assert res.json() == {"status": "created"}

    def test_vnc_credentials_are_access_protected_and_uncacheable(self, rsa_key_pair):
        private_key, public_key = rsa_key_pair
        jwks = JwksCache()
        jwks.inject_key("test-key-1", public_key)
        client = TestClient(app)
        with (
            patch("job_applier.web.app.edge_config.cf_access_enabled", True),
            patch("job_applier.web.app.edge_config.cf_access_aud", "test-aud-12345"),
            patch("job_applier.web.app.edge_config.cf_access_team_domain", "aslan"),
            patch(
                "job_applier.web.app.edge_config.cf_access_allowed_identities",
                {"operator@aslan.net"},
            ),
            patch("job_applier.web.edge_auth.get_jwks_cache", return_value=jwks),
            patch("job_applier.web.app.load_vnc_password", return_value="testpass"),
        ):
            assert (
                client.get("/api/automation/takeover/vnc-credentials").status_code
                == 401
            )
            token = create_signed_jwt(
                private_key, kid="test-key-1", email="operator@aslan.net"
            )
            response = client.get(
                "/api/automation/takeover/vnc-credentials",
                headers={"Cf-Access-Jwt-Assertion": token},
            )
        assert response.status_code == 200
        assert response.json() == {"password": "testpass"}
        assert "no-store" in response.headers["cache-control"]
        assert response.headers["pragma"] == "no-cache"

    def test_local_gateway_vnc_credentials_use_proxy_token(self):
        # Caddy forwards from the trusted Docker bridge, not loopback.
        client = TestClient(app, client=("172.20.0.2", 50000))
        with (
            patch("job_applier.web.app.edge_config.cf_access_enabled", False),
            patch(
                "job_applier.web.app.edge_config.local_gateway_viewer_token",
                "local-gateway-secret",
            ),
            patch("job_applier.web.app.load_vnc_password", return_value="testpass"),
        ):
            response = client.get(
                "/api/automation/takeover/vnc-credentials",
                headers={
                    "Host": "localhost:8089",
                    "X-Local-Gateway-Viewer": "local-gateway-secret",
                },
            )
        assert response.status_code == 200
        assert response.json() == {"password": "testpass"}
        assert "no-store" in response.headers["cache-control"]

    def test_public_health_requires_access(self, auth_client):
        client, _, _ = auth_client
        res = client.get("/api/health")
        assert res.status_code == 401
        assert "Missing Cloudflare Access assertion" in res.json()["detail"]

    def test_internal_health_is_loopback_only(self, auth_client):
        client, _, _ = auth_client
        res = client.get("/api/health/internal")
        assert res.status_code == 200
        assert res.json() == {"status": "ok", "service": "job-applier"}

    def test_internal_health_rejects_non_loopback_source(self):
        config = EdgeAuthConfig(
            cf_access_enabled=False,
            allowed_hosts={"testserver"},
        )
        test_app = FastAPI()
        test_app.add_middleware(EdgeAuthMiddleware, config=config)

        @test_app.get("/api/health/internal")
        def internal_health():
            return {"status": "ok"}

        client = TestClient(test_app, client=("10.0.0.8", 50000))
        assert client.get("/api/health/internal").status_code == 404

    def test_internal_health_missing_client_metadata_is_untrusted(self):
        config = EdgeAuthConfig(
            cf_access_enabled=False,
            allowed_hosts={"testserver"},
        )
        test_app = FastAPI()
        test_app.add_middleware(EdgeAuthMiddleware, config=config)

        @test_app.get("/api/health/internal")
        def internal_health():
            return {"status": "ok"}

        client = TestClient(test_app, client=None)  # type: ignore[arg-type]
        response = client.get("/api/health/internal")
        assert response.status_code == 404

    def test_public_internal_health_requires_access_before_private_404(
        self, rsa_key_pair
    ):
        private_key, public_key = rsa_key_pair
        jwks = JwksCache()
        jwks.inject_key("test-key-1", public_key)
        config = EdgeAuthConfig(
            cf_access_enabled=True,
            cf_access_aud="test-aud-12345",
            cf_access_team_domain="aslan",
            cf_access_allowed_identities={"operator@aslan.net"},
            public_origin="https://jobs.aslan.net",
            allowed_hosts={"jobs.aslan.net", "testserver"},
        )
        test_app = FastAPI()
        test_app.add_middleware(EdgeAuthMiddleware, config=config)

        @test_app.get("/api/health/internal")
        def internal_health():
            return {"status": "ok"}

        client = TestClient(test_app, client=("10.0.0.8", 50000))
        assert client.get("/api/health/internal").status_code == 401

        with patch("job_applier.web.edge_auth.get_jwks_cache", return_value=jwks):
            token = create_signed_jwt(
                private_key, kid="test-key-1", email="operator@aslan.net"
            )
            response = client.get(
                "/api/health/internal",
                headers={"Cf-Access-Jwt-Assertion": token},
            )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Gateway Viewer Gate & WebSocket Lifetime Tests
# ---------------------------------------------------------------------------


class TestGatewayViewerAuthorization:
    """Tests /api/auth/viewer-gate and 5-minute / token-expiry viewer lifetime enforcement."""

    def test_viewer_gate_rejects_unauthenticated(self):
        client = TestClient(app)
        with patch.object(app.state, "edge_config", create=True):
            # When CF Access enabled
            with patch("job_applier.web.app.edge_config.cf_access_enabled", True):
                res = client.get("/api/auth/viewer-gate")
                assert res.status_code == 401

    def test_viewer_gate_accepts_valid_jwt_and_sets_expiry_header(self, rsa_key_pair):
        private_key, public_key = rsa_key_pair
        jwks = JwksCache()
        jwks.inject_key("test-key-1", public_key)

        client = TestClient(app)
        with (
            patch("job_applier.web.edge_auth.get_jwks_cache", return_value=jwks),
            patch("job_applier.web.app.edge_config.cf_access_enabled", True),
            patch("job_applier.web.app.edge_config.cf_access_aud", "test-aud-12345"),
            patch("job_applier.web.app.edge_config.cf_access_team_domain", "aslan"),
            patch(
                "job_applier.web.app.edge_config.cf_access_allowed_identities",
                {"operator@aslan.net"},
            ),
            patch("job_applier.web.app.edge_config.viewer_max_duration_seconds", 300),
        ):
            token = create_signed_jwt(
                private_key,
                kid="test-key-1",
                email="operator@aslan.net",
                exp=time.time() + 600,  # 10 minutes in future
            )
            res = client.get(
                "/api/auth/viewer-gate",
                headers={"Cf-Access-Jwt-Assertion": token},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "authorized"
            # Capped at 5-minute (300s) maximum server-side viewer lifetime!
            assert data["expires_in"] <= 300
            assert "X-Viewer-Expires-In" in res.headers

    def test_viewer_gate_caps_at_shorter_token_expiry(self, rsa_key_pair):
        private_key, public_key = rsa_key_pair
        jwks = JwksCache()
        jwks.inject_key("test-key-1", public_key)

        client = TestClient(app)
        with (
            patch("job_applier.web.edge_auth.get_jwks_cache", return_value=jwks),
            patch("job_applier.web.app.edge_config.cf_access_enabled", True),
            patch("job_applier.web.app.edge_config.cf_access_aud", "test-aud-12345"),
            patch("job_applier.web.app.edge_config.cf_access_team_domain", "aslan"),
            patch(
                "job_applier.web.app.edge_config.cf_access_allowed_identities",
                {"operator@aslan.net"},
            ),
            patch("job_applier.web.app.edge_config.viewer_max_duration_seconds", 300),
        ):
            # Token expires in 90 seconds (shorter than 300s)
            short_exp = time.time() + 90
            token = create_signed_jwt(
                private_key,
                kid="test-key-1",
                email="operator@aslan.net",
                exp=short_exp,
            )
            res = client.get(
                "/api/auth/viewer-gate",
                headers={"Cf-Access-Jwt-Assertion": token},
            )
            assert res.status_code == 200
            data = res.json()
            # Must be capped at token expiration (~90s), not 300s!
            assert 80 <= data["expires_in"] <= 90

    def test_viewer_gate_rejects_cross_origin_websocket_upgrade(self, rsa_key_pair):
        private_key, public_key = rsa_key_pair
        jwks = JwksCache()
        jwks.inject_key("test-key-1", public_key)

        client = TestClient(app)
        with (
            patch("job_applier.web.edge_auth.get_jwks_cache", return_value=jwks),
            patch("job_applier.web.app.edge_config.cf_access_enabled", True),
            patch("job_applier.web.app.edge_config.cf_access_aud", "test-aud-12345"),
            patch("job_applier.web.app.edge_config.cf_access_team_domain", "aslan"),
            patch(
                "job_applier.web.app.edge_config.cf_access_allowed_identities",
                {"operator@aslan.net"},
            ),
            patch(
                "job_applier.web.app.edge_config.public_origin",
                "https://jobs.aslan.net",
            ),
        ):
            token = create_signed_jwt(
                private_key, kid="test-key-1", email="operator@aslan.net"
            )
            res = client.get(
                "/api/auth/viewer-gate",
                headers={
                    "Cf-Access-Jwt-Assertion": token,
                    "Upgrade": "websocket",
                    "Origin": "https://evil.com",  # Hostile cross-origin!
                },
            )
            assert res.status_code == 403
            assert "Cross-origin viewer access forbidden" in res.json()["detail"]

    def test_viewer_websocket_cross_origin_rejected(self):
        client = TestClient(app)
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/browser/websockify",
                headers={"Origin": "https://malicious-origin.com"},
            ):
                pass
        with pytest.raises(Exception):
            with client.websocket_connect(
                "/websockify",
                headers={"Origin": "https://malicious-origin.com"},
            ):
                pass

    def test_viewer_websocket_unauthenticated_rejected_when_enabled(self):
        client = TestClient(app)
        with patch("job_applier.web.app.edge_config.cf_access_enabled", True):
            with pytest.raises(Exception):
                with client.websocket_connect(
                    "/browser/websockify",
                    headers={"Origin": "https://jobs.aslan.net"},
                ):
                    pass
            with pytest.raises(Exception):
                with client.websocket_connect(
                    "/websockify",
                    headers={"Origin": "https://jobs.aslan.net"},
                ):
                    pass

    def test_viewer_websocket_query_token_rejected_when_access_enabled(self):
        client = TestClient(app)
        with patch("job_applier.web.app.edge_config.cf_access_enabled", True):
            with pytest.raises(Exception):
                with client.websocket_connect(
                    "/browser/websockify?token=fake-token",
                    headers={"Origin": "https://jobs.aslan.net"},
                ):
                    pass
            with pytest.raises(Exception):
                with client.websocket_connect(
                    "/websockify?token=fake-token",
                    headers={"Origin": "https://jobs.aslan.net"},
                ):
                    pass

    def test_sse_events_expiry_enforcement(self, rsa_key_pair):
        private_key, public_key = rsa_key_pair
        jwks = JwksCache()
        jwks.inject_key("test-key-1", public_key)

        client = TestClient(app)
        with (
            patch("job_applier.web.edge_auth.get_jwks_cache", return_value=jwks),
            patch("job_applier.web.app.edge_config.cf_access_enabled", True),
            patch("job_applier.web.app.edge_config.cf_access_aud", "test-aud-12345"),
            patch("job_applier.web.app.edge_config.cf_access_team_domain", "aslan"),
            patch(
                "job_applier.web.app.edge_config.cf_access_allowed_identities",
                {"operator@aslan.net"},
            ),
        ):
            # Expiring very soon (1 second)
            short_exp = time.time() + 1
            token = create_signed_jwt(
                private_key,
                kid="test-key-1",
                email="operator@aslan.net",
                exp=short_exp,
            )
            with client.stream(
                "GET",
                "/api/automation/events",
                headers={"Cf-Access-Jwt-Assertion": token},
            ) as response:
                assert response.status_code == 200
                lines = []
                for line in response.iter_lines():
                    lines.append(line)
                    if "Session expired" in line:
                        break
                joined = "\n".join(lines)
                assert "event: expired" in joined
                assert "Session expired" in joined

    def test_no_credentialed_cross_origin_cors(self):
        client = TestClient(app)
        # Attempt preflight from untrusted origin
        res = client.options(
            "/api/applications",
            headers={
                "Origin": "https://evil.attacker.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        # Access-Control-Allow-Origin must NOT reflect evil.attacker.com with credentials!
        allow_origin = res.headers.get("access-control-allow-origin")
        assert allow_origin != "https://evil.attacker.com"
        assert allow_origin != "*"


def test_viewer_max_duration_clamped_to_300():
    from job_applier.web.edge_auth import EdgeAuthConfig

    with patch.dict(os.environ, {"VIEWER_MAX_DURATION_SECONDS": "600"}):
        cfg = EdgeAuthConfig.from_env()
        assert cfg.viewer_max_duration_seconds == 300

    direct_cfg = EdgeAuthConfig(viewer_max_duration_seconds=999)
    assert direct_cfg.viewer_max_duration_seconds == 300


def test_viewer_websocket_rejects_query_token_unit():
    import asyncio
    from unittest.mock import AsyncMock
    from job_applier.web.edge_auth import EdgeAuthConfig, handle_viewer_websocket

    config = EdgeAuthConfig(
        cf_access_enabled=True,
        cf_access_team_domain="aslan",
        cf_access_aud="test-aud",
        cf_access_allowed_identities={"operator@aslan.net"},
        public_origin="https://jobs.archnet.lol",
    )

    mock_ws = AsyncMock()
    mock_ws.headers = {"origin": "https://jobs.archnet.lol"}
    mock_ws.query_params = {"token": "some-jwt-token"}
    mock_ws.cookies = {}

    asyncio.run(handle_viewer_websocket(mock_ws, config))

    mock_ws.close.assert_awaited_once_with(
        code=1008, reason="Missing Cloudflare Access authentication"
    )
    mock_ws.accept.assert_not_called()


def test_rfb_authenticated_handshake_forwards_auth_response_before_client_init():
    from job_applier.web.edge_auth import RfbReadOnlyState, filter_rfb_client_messages

    state = RfbReadOnlyState()
    version = b"RFB 003.008\n"
    assert (
        filter_rfb_client_messages(version, allow_input=False, state=state) == version
    )
    assert (
        filter_rfb_client_messages(b"\x02", allow_input=False, state=state) == b"\x02"
    )
    assert state.phase == "vnc_auth_response"
    response = bytes(range(16))
    assert (
        filter_rfb_client_messages(response[:7], allow_input=False, state=state) == b""
    )
    assert (
        filter_rfb_client_messages(response[7:], allow_input=False, state=state)
        == response
    )
    assert state.phase == "client_init"
    assert (
        filter_rfb_client_messages(b"\x01", allow_input=False, state=state) == b"\x01"
    )
    assert state.phase == "initialized"


def test_rfb_read_only_handshake_reaches_initialized_state_and_blocks_input():
    from job_applier.web.edge_auth import RfbReadOnlyState, filter_rfb_client_messages

    state = RfbReadOnlyState()
    version = b"RFB 003.008\n"
    assert (
        filter_rfb_client_messages(version, allow_input=False, state=state) == version
    )
    assert state.phase == "security_selection"
    assert (
        filter_rfb_client_messages(b"\x01", allow_input=False, state=state) == b"\x01"
    )
    assert state.phase == "client_init"
    assert (
        filter_rfb_client_messages(b"\x01", allow_input=False, state=state) == b"\x01"
    )
    assert state.phase == "initialized"

    framebuffer_request = b"\x03" + b"\x00" * 9
    key_event = b"\x04" + b"\x00" * 7
    pointer_event = b"\x05" + b"\x00" * 5
    clipboard = b"\x06" + b"\x00" * 7
    assert (
        filter_rfb_client_messages(
            framebuffer_request + key_event + pointer_event + clipboard,
            allow_input=False,
            state=state,
        )
        == framebuffer_request
    )
    assert filter_rfb_client_messages(b"\x01", allow_input=False, state=state) == b""


def test_rfb_read_only_filter_preserves_fragmented_handshake_and_safe_frames():
    from job_applier.web.edge_auth import RfbReadOnlyState, filter_rfb_client_messages

    state = RfbReadOnlyState()
    version = b"RFB 003.008\n"
    assert (
        filter_rfb_client_messages(version[:5], allow_input=False, state=state) == b""
    )
    assert (
        filter_rfb_client_messages(version[5:10], allow_input=False, state=state) == b""
    )
    assert (
        filter_rfb_client_messages(version[10:], allow_input=False, state=state)
        == version
    )
    assert state.phase == "security_selection"
    assert filter_rfb_client_messages(b"", allow_input=False, state=state) == b""
    assert (
        filter_rfb_client_messages(b"\x01", allow_input=False, state=state) == b"\x01"
    )
    assert state.phase == "client_init"
    assert (
        filter_rfb_client_messages(b"\x01", allow_input=False, state=state) == b"\x01"
    )
    assert state.phase == "initialized"

    framebuffer_request = b"\x03" + b"\x00" * 9
    assert (
        filter_rfb_client_messages(
            framebuffer_request[:4], allow_input=False, state=state
        )
        == b""
    )
    assert (
        filter_rfb_client_messages(
            framebuffer_request[4:], allow_input=False, state=state
        )
        == framebuffer_request
    )

    key_event = b"\x04" + b"\x00" * 7
    assert (
        filter_rfb_client_messages(key_event[:2], allow_input=False, state=state) == b""
    )
    assert (
        filter_rfb_client_messages(key_event[2:], allow_input=False, state=state) == b""
    )
    assert state.buffer == b""


def _initialized_rfb_state():
    from job_applier.web.edge_auth import RfbReadOnlyState, filter_rfb_client_messages

    state = RfbReadOnlyState()
    assert (
        filter_rfb_client_messages(b"RFB 003.008\n", False, state) == b"RFB 003.008\n"
    )
    assert filter_rfb_client_messages(b"\x01\x01", False, state) == b"\x01\x01"
    assert state.phase == "initialized"
    return state, filter_rfb_client_messages


@pytest.mark.parametrize(
    "frame",
    [b"\x04" + b"\x00" * 7, b"\x05" + b"\x00" * 5, b"\x06" + b"\x00" * 7],
)
def test_rfb_fragmented_input_cannot_cross_authorization_epoch(frame):
    """Fragments begun on either side of a lease transition are discarded."""
    state, filter_messages = _initialized_rfb_state()

    assert filter_messages(frame[:2], allow_input=False, state=state) == b""
    assert filter_messages(frame[2:], allow_input=True, state=state) == b""
    assert state.buffer == b""

    state, filter_messages = _initialized_rfb_state()
    assert filter_messages(frame[:2], allow_input=True, state=state) == b""
    assert filter_messages(frame[2:], allow_input=False, state=state) == b""
    assert state.buffer == b""


def test_rfb_safe_fragment_cannot_cross_authorization_epoch():
    state, filter_messages = _initialized_rfb_state()
    framebuffer_request = b"\x03" + b"\x00" * 9

    assert filter_messages(framebuffer_request[:4], False, state) == b""
    assert filter_messages(framebuffer_request[4:], True, state) == b""
    assert filter_messages(framebuffer_request, True, state) == framebuffer_request

    state, filter_messages = _initialized_rfb_state()
    assert filter_messages(framebuffer_request[:4], True, state) == b""
    assert filter_messages(framebuffer_request[4:], False, state) == b""
    assert filter_messages(framebuffer_request, False, state) == framebuffer_request


def test_deep_rfb_message_filtering():
    from job_applier.web.edge_auth import filter_rfb_client_messages

    # FramebufferUpdateRequest: type 3, 10 bytes
    fb_req = b"\x03" + b"\x00" * 9
    # PointerEvent: type 5, 6 bytes
    ptr_event = b"\x05" + b"\x00" * 5
    # KeyEvent: type 4, 8 bytes
    key_event = b"\x04" + b"\x00" * 7

    coalesced = fb_req + ptr_event + key_event

    # Read-only viewer (allow_input=False) strips pointer and key events
    filtered = filter_rfb_client_messages(coalesced, allow_input=False)
    assert filtered == fb_req

    # Operator with takeover (allow_input=True) forwards all events
    allowed = filter_rfb_client_messages(coalesced, allow_input=True)
    assert allowed == coalesced


def test_set_encodings_rfb_length_and_fail_closed():
    # RFB SetEncodings message:
    # 1 byte msg_type (2), 1 byte padding (0), 2 bytes count (num_enc)
    # followed by num_enc * 4 bytes (RFC 6143)
    # 2 encodings = 4 header bytes + 2 * 4 = 12 bytes total
    num_enc = 2
    encodings_data = struct.pack(">BBHii", 2, 0, num_enc, 1, 2)
    assert len(encodings_data) == 12

    # Trailing KeyEvent (type 4, 8 bytes)
    key_event = struct.pack(">BBHI", 4, 1, 0, 0x41)
    combined = encodings_data + key_event

    # When allow_input is False, SetEncodings is preserved, KeyEvent is stripped
    filtered = filter_rfb_client_messages(combined, allow_input=False)
    assert filtered == encodings_data

    # Unknown message type (e.g. 99) fails closed (dropped), including a
    # short frame that must not be mistaken for a fragmented safe message.
    unknown_msg = bytes([99, 1, 2, 3, 4, 5])
    assert filter_rfb_client_messages(unknown_msg, allow_input=False) == b""
    assert filter_rfb_client_messages(bytes([99, 1]), allow_input=False) == b""


class _ViewerProtocolUpstream:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self._messages = [b"RFB 003.008\n"]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def send(self, message: bytes) -> None:
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self._messages:
            return self._messages.pop(0)
        raise StopAsyncIteration


def _viewer_protocol_websocket(protocol_header: str = "", messages=None):
    from unittest.mock import AsyncMock

    websocket = AsyncMock()
    websocket.headers = {
        "origin": "https://jobs.archnet.lol",
        "sec-websocket-protocol": protocol_header,
    }
    websocket.cookies = {}
    websocket.receive.side_effect = messages or [{"type": "websocket.disconnect"}]
    return websocket


def _viewer_protocol_config() -> EdgeAuthConfig:
    return EdgeAuthConfig(
        cf_access_enabled=False,
        public_origin="https://jobs.archnet.lol",
        viewer_max_duration_seconds=1,
    )


def test_viewer_protocol_parser_normalizes_offers() -> None:
    from job_applier.web.edge_auth import parse_viewer_subprotocols

    assert parse_viewer_subprotocols(" binary,  base64 ") == ("binary", "base64")


def test_viewer_without_protocol_exchanges_rfb_without_selected_protocol() -> None:
    import asyncio
    from unittest.mock import patch
    from job_applier.web.edge_auth import handle_viewer_websocket

    async def run():
        websocket = _viewer_protocol_websocket()
        upstream = _ViewerProtocolUpstream()
        with patch("websockets.connect", return_value=upstream):
            await handle_viewer_websocket(websocket, _viewer_protocol_config())
        return websocket, upstream

    websocket, upstream = asyncio.run(run())
    websocket.accept.assert_awaited_once_with()
    websocket.send_bytes.assert_awaited_once_with(b"RFB 003.008\n")
    assert upstream.sent == []


def test_viewer_binary_protocol_is_selected_when_offered() -> None:
    import asyncio
    from unittest.mock import patch
    from job_applier.web.edge_auth import handle_viewer_websocket

    async def run():
        websocket = _viewer_protocol_websocket("base64, binary")
        with patch("websockets.connect", return_value=_ViewerProtocolUpstream()):
            await handle_viewer_websocket(websocket, _viewer_protocol_config())
        return websocket

    websocket = asyncio.run(run())
    websocket.accept.assert_awaited_once_with(subprotocol="binary")


def test_viewer_unsupported_only_protocol_is_rejected() -> None:
    import asyncio

    async def run():
        websocket = _viewer_protocol_websocket("base64")
        await handle_viewer_websocket(websocket, _viewer_protocol_config())
        return websocket

    websocket = asyncio.run(run())
    websocket.close.assert_awaited_once_with(
        code=1002, reason="Unsupported WebSocket subprotocol"
    )
    websocket.accept.assert_not_awaited()


def test_takeover_owner_uses_verified_access_identity_not_request_body() -> None:
    from types import SimpleNamespace
    from job_applier.web.app import _takeover_request_owner

    request = cast(
        Any,
        SimpleNamespace(state=SimpleNamespace(user={"email": "owner@example.test"})),
    )
    with patch("job_applier.web.app.edge_config.cf_access_enabled", True):
        assert (
            _takeover_request_owner(request, "forged-client-owner")
            == "owner@example.test"
        )


def test_viewer_input_authorized_requires_matching_active_owner() -> None:
    assert viewer_input_authorized(
        "operator@example.test", True, "operator@example.test"
    )
    assert not viewer_input_authorized(
        "viewer@example.test", True, "operator@example.test"
    )
    assert not viewer_input_authorized(
        "operator@example.test", False, "operator@example.test"
    )


def test_viewer_input_rejects_different_lease_owner() -> None:
    """An authorized viewer cannot inject input into another owner's lease."""
    import asyncio
    from unittest.mock import patch

    key_event = struct.pack(">BBHI", 4, 1, 0, 0x41)

    async def run():
        websocket = _viewer_protocol_websocket(
            messages=[{"bytes": key_event}, {"type": "websocket.disconnect"}]
        )
        upstream = _ViewerProtocolUpstream()
        with (
            patch("websockets.connect", return_value=upstream),
            patch(
                "job_applier.automation.queue.is_takeover_active",
                return_value=(True, "another-authorized-viewer", "future"),
            ),
        ):
            await handle_viewer_websocket(websocket, _viewer_protocol_config())
        return upstream

    upstream = asyncio.run(run())
    assert upstream.sent == []


def test_takeover_transport_lock_fences_release_during_upstream_send():
    """A release cannot complete while an authorized frame is being sent upstream."""
    import threading
    import time
    from job_applier.automation.queue import TAKEOVER_TRANSPORT_LOCK

    release_started = threading.Event()
    release_finished = threading.Event()

    def release_probe() -> None:
        release_started.set()
        with TAKEOVER_TRANSPORT_LOCK:
            release_finished.set()

    with TAKEOVER_TRANSPORT_LOCK:
        thread = threading.Thread(target=release_probe)
        thread.start()
        assert release_started.wait(timeout=1)
        time.sleep(0.01)
        assert not release_finished.is_set()

    thread.join(timeout=1)
    assert release_finished.is_set()


def test_viewer_relay_serializes_release_with_upstream_send():
    """A lease release waits until an authorized frame finishes its upstream send."""
    import asyncio
    import threading
    from unittest.mock import patch
    from job_applier.automation.queue import TAKEOVER_TRANSPORT_LOCK
    from job_applier.web.edge_auth import handle_viewer_websocket

    key_event = struct.pack(">BBHI", 4, 1, 0, 0x41)
    release_started = threading.Event()
    release_finished = threading.Event()

    def release_probe() -> None:
        release_started.set()
        with TAKEOVER_TRANSPORT_LOCK:
            release_finished.set()

    class BlockingSendUpstream(_ViewerProtocolUpstream):
        release_thread: threading.Thread | None = None

        async def send(self, message: bytes) -> None:
            self.sent.append(message)
            if message != key_event:
                return
            self.release_thread = threading.Thread(target=release_probe)
            self.release_thread.start()
            assert release_started.wait(timeout=1)
            await asyncio.sleep(0.01)
            assert not release_finished.is_set()

    async def run():
        websocket = _viewer_protocol_websocket(
            messages=[
                {"bytes": b"RFB 003.008\n"},
                {"bytes": b"\x01"},
                {"bytes": b"\x01"},
                {"bytes": key_event},
                {"type": "websocket.disconnect"},
            ]
        )
        upstream = BlockingSendUpstream()
        with (
            patch("websockets.connect", return_value=upstream),
            patch(
                "job_applier.automation.queue.is_takeover_active",
                return_value=(True, "operator", "future"),
            ),
        ):
            await handle_viewer_websocket(websocket, _viewer_protocol_config())
        return upstream

    upstream = asyncio.run(run())
    assert upstream.sent == [b"RFB 003.008\n", b"\x01", b"\x01", key_event]
    assert upstream.release_thread is not None
    upstream.release_thread.join(timeout=1)
    assert not upstream.release_thread.is_alive()
    assert release_finished.is_set()


def test_viewer_input_rejects_first_frame_after_release() -> None:
    """A release is observed on the next input frame, with no cache grace period."""
    import asyncio
    from unittest.mock import patch

    key_event = struct.pack(">BBHI", 4, 1, 0, 0x41)

    async def run():
        websocket = _viewer_protocol_websocket(
            messages=[{"bytes": key_event}, {"type": "websocket.disconnect"}]
        )
        upstream = _ViewerProtocolUpstream()
        with (
            patch("websockets.connect", return_value=upstream),
            patch(
                "job_applier.automation.queue.is_takeover_active",
                return_value=(False, None, None),
            ),
        ):
            await handle_viewer_websocket(websocket, _viewer_protocol_config())
        return upstream

    upstream = asyncio.run(run())
    assert upstream.sent == []
