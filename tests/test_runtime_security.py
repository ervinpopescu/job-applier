from __future__ import annotations

from job_applier.automation.adapters import CaptchaDetectedError
from job_applier.automation.queue import JobState
from job_applier.automation.queue import claim_next_job
from job_applier.automation.queue import get_connection
from job_applier.automation.network_security import resolve_and_validate_host
from job_applier.automation.queue import transition_job
from job_applier.automation.browser_automator import BrowserAutomator
from job_applier.automation.runtime_lock import RuntimeSingletonLock
from job_applier.cli.worker import (
    HeartbeatThread,
    _coordinate_challenge_takeover,
    process_claimed_job,
    run_worker_loop,
)
from job_applier.db import upsert_application

import ipaddress
import multiprocessing
import os
import sqlite3
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
import yaml  # type: ignore[import-untyped]
from fastapi.testclient import TestClient

from job_applier.web.app import app
from job_applier.automation.browser_runtime import get_sanitized_browser_env
from job_applier.automation.network_security import (
    OutboundSecurityProxy,
    is_ip_denied,
    validate_host_and_dns,
    validate_target_url,
)
from job_applier.automation.profile_lock import (
    ProfileOwnershipError,
    ProfileOwnershipLock,
)
from job_applier.automation.queue import (
    _QUEUE_LOCK,
    begin_submit_click_fence,
    claim_manual_takeover,
    clear_automation_state,
    create_attempt,
    enqueue_job,
    finish_submit_click_fence,
    get_runtime_control,
    get_browser_owner_job,
    is_takeover_active,
    release_manual_takeover,
    record_submit_intent,
    requeue_auth_required_job,
    set_runtime_browser_state,
    set_runtime_pause,
    set_runtime_stop,
    set_pending_verification_code,
    takeover_owner_matches,
)
from job_applier.automation.safe_resume import safe_resume_revalidate
from job_applier.db import init_db


# =====================================================================
# 1. Security Tests: Secret Environment Stripping
# =====================================================================


def _hold_sqlite_writer(db_file: str, ready: Any, release: Any) -> None:
    """Hold a write transaction in a separate process for fence contention tests."""
    conn = get_connection(Path(db_file))
    try:
        conn.execute("BEGIN IMMEDIATE;")
        ready.set()
        release.wait(timeout=10)
        conn.commit()
    finally:
        conn.close()


def test_sqlite_busy_timeout_covers_cross_process_submit_fence(tmp_path: Path):
    """Expected submit-vs-takeover ordering waits instead of failing at 5 seconds."""
    db_file = tmp_path / "cross-process-fence.db"
    init_db(db_file)
    conn = get_connection(db_file)
    try:
        busy_timeout_ms = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
    finally:
        conn.close()
    assert busy_timeout_ms >= 300_000

    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_sqlite_writer,
        args=(str(db_file), ready, release),
    )
    process.start()
    try:
        assert ready.wait(timeout=5)
        result: dict[str, Any] = {}

        def claim() -> None:
            result["value"] = claim_manual_takeover(
                "cross-process-test", lease_seconds=30, custom_path=db_file
            )

        claimant = threading.Thread(target=claim)
        claimant.start()
        time.sleep(0.2)
        assert claimant.is_alive()
        release.set()
        claimant.join(timeout=10)
        assert not claimant.is_alive()
        assert result["value"]["status"] == "success"
        process.join(timeout=10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            release.set()
            process.terminate()
            process.join(timeout=5)


def test_submit_click_fence_orders_takeover_both_ways(tmp_path: Path):
    """SQLite BEGIN IMMEDIATE orders the final click and takeover across threads."""

    def prepare(db_file: Path):
        init_db(db_file)
        upsert_application(
            app_id="app-submit-fence",
            company="Fence Corp",
            title="Engineer",
            job_url="https://fence.example/job",
            custom_path=db_file,
        )
        enqueue_job("app-submit-fence", adapter="greenhouse", custom_path=db_file)
        job = claim_next_job("submit-worker", lease_seconds=60, custom_path=db_file)
        assert job is not None
        attempt = create_attempt(
            job_id=job.id,
            app_id=job.app_id,
            worker_id="submit-worker",
            lease_generation=job.fencing_generation,
            custom_path=db_file,
        )
        assert record_submit_intent(
            attempt.id,
            job.id,
            "submit-worker",
            job.fencing_generation,
            custom_path=db_file,
        )
        return job, attempt

    claim_first = tmp_path / "claim-first.db"
    job, attempt = prepare(claim_first)
    assert (
        claim_manual_takeover("operator", custom_path=claim_first)["status"]
        == "success"
    )
    with pytest.raises(RuntimeError, match="Physical submit fence rejected"):
        begin_submit_click_fence(
            job_id=job.id,
            attempt_id=attempt.id,
            worker_id="submit-worker",
            generation=job.fencing_generation,
            custom_path=claim_first,
        )

    click_first = tmp_path / "click-first.db"
    job, attempt = prepare(click_first)
    fence = begin_submit_click_fence(
        job_id=job.id,
        attempt_id=attempt.id,
        worker_id="submit-worker",
        generation=job.fencing_generation,
        custom_path=click_first,
    )
    claim_result: list[dict[str, Any]] = []

    def claim_after_click_starts() -> None:
        claim_result.append(claim_manual_takeover("operator", custom_path=click_first))

    claimant = threading.Thread(target=claim_after_click_starts)
    claimant.start()
    time.sleep(0.05)
    assert claimant.is_alive(), "takeover must wait behind the click transaction"
    finish_submit_click_fence(fence, clicked=True)
    claimant.join(timeout=2)
    assert not claimant.is_alive()
    assert claim_result and claim_result[0]["status"] == "success"


def test_clear_state_preserves_outcome_unknown_jobs(tmp_path: Path):
    db_file = tmp_path / "clear-ambiguous.db"
    init_db(db_file)
    upsert_application(
        app_id="app-clear-ambiguous",
        company="Clear Corp",
        title="Engineer",
        job_url="https://clear.example/job",
        custom_path=db_file,
    )
    enqueue_job("app-clear-ambiguous", adapter="lever", custom_path=db_file)
    conn = get_connection(db_file)
    with conn:
        conn.execute(
            "UPDATE automation_jobs SET state = 'submit_intent' WHERE app_id = ?;",
            ("app-clear-ambiguous",),
        )
    result = clear_automation_state(custom_path=db_file)
    assert result["status"] == "success"
    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT state FROM automation_jobs WHERE app_id = ?;", ("app-clear-ambiguous",)
    ).fetchone()
    conn.close()
    assert row["state"] == JobState.AMBIGUOUS_SUBMISSION.value
    assert claim_next_job("must-not-claim", custom_path=db_file) is None


def test_takeover_lease_contract_rejects_invalid_core_values(tmp_path: Path):
    db_file = tmp_path / "takeover_lease_contract.db"
    init_db(db_file)
    from job_applier.automation.queue import (
        TAKEOVER_DEFAULT_LEASE_SECONDS,
        TAKEOVER_MAX_LEASE_SECONDS,
    )

    result = claim_manual_takeover("valid-owner", custom_path=db_file)
    assert result["status"] == "success"
    assert result["lease_seconds"] == TAKEOVER_DEFAULT_LEASE_SECONDS == 300
    release_manual_takeover("valid-owner", custom_path=db_file)
    for invalid in (0, -1, 301, 3600):
        with pytest.raises(ValueError, match="lease_seconds"):
            claim_manual_takeover("invalid-owner", invalid, custom_path=db_file)
    assert TAKEOVER_MAX_LEASE_SECONDS == 300
    assert get_runtime_control(custom_path=db_file)["manual_takeover_owner"] is None


def test_takeover_lease_api_schema_rejects_invalid_values():
    from pydantic import ValidationError
    from job_applier.web.app import TakeoverClaimRequest

    assert TakeoverClaimRequest().lease_seconds == 300
    assert TakeoverClaimRequest(lease_seconds=1).lease_seconds == 1
    for invalid in (0, -1, 301, True):
        with pytest.raises(ValidationError):
            TakeoverClaimRequest(lease_seconds=invalid)


def test_expired_takeover_owner_is_rejected_by_fenced_mutations(tmp_path: Path):
    db_file = tmp_path / "expired_takeover.db"
    init_db(db_file)
    assert (
        claim_manual_takeover("expired-owner", custom_path=db_file)["status"]
        == "success"
    )

    conn = get_connection(db_file)
    try:
        conn.execute(
            "UPDATE runtime_control SET manual_takeover_expires_at = ? WHERE id = 1;",
            ("2000-01-01 00:00:00",),
        )
        conn.commit()
    finally:
        conn.close()

    assert not takeover_owner_matches("expired-owner", custom_path=db_file)
    assert not set_runtime_pause(
        False, custom_path=db_file, expected_owner="expired-owner"
    )
    assert not set_pending_verification_code(
        "123456", custom_path=db_file, expected_owner="expired-owner"
    )

    from job_applier.ops.emergency_stop import clear_emergency_stop

    result = clear_emergency_stop(
        custom_db_path=db_file, expected_owner="expired-owner"
    )
    assert result["status"] == "rejected"
    assert result["reason"] == "takeover_owner_required"


def test_reopened_browser_generation_allows_same_job_reclaim_and_rejects_stale_worker(
    tmp_path: Path,
):
    db_file = tmp_path / "reopened_browser.db"
    init_db(db_file)
    upsert_application(
        app_id="app-reopened",
        company="Reopened Corp",
        title="Engineer",
        job_url="https://reopened.example/apply",
        custom_path=db_file,
    )
    enqueue_job("app-reopened", custom_path=db_file)
    first = claim_next_job("worker-first", custom_path=db_file)
    assert first is not None
    set_runtime_browser_state(
        active=True,
        url="https://old.example/apply",
        browser_job_id=first.id,
        browser_session_generation=first.fencing_generation,
        is_closed=False,
        custom_path=db_file,
    )

    # A replacement worker claims the same job ID with a new durable
    # generation after the original lease expires.
    conn = get_connection(db_file)
    try:
        conn.execute(
            "UPDATE automation_jobs SET lease_expires_at = '2000-01-01 00:00:00' WHERE id = ?;",
            (first.id,),
        )
        conn.commit()
    finally:
        conn.close()
    reclaimed = claim_next_job("worker-second", custom_path=db_file)
    assert reclaimed is not None
    assert reclaimed.id == first.id
    assert reclaimed.fencing_generation == first.fencing_generation + 1

    set_runtime_browser_state(
        active=True,
        url="https://old.example/stale",
        browser_job_id=first.id,
        browser_session_generation=first.fencing_generation,
        is_closed=False,
        custom_path=db_file,
    )
    state = get_runtime_control(custom_path=db_file)
    assert state["browser_active"] is True
    assert state["browser_session_generation"] == first.fencing_generation
    assert state["browser_url"] == "https://old.example/apply"

    # The fresh worker can publish only its current, live generation.
    set_runtime_browser_state(
        active=True,
        url="https://new.example/apply",
        browser_job_id=reclaimed.id,
        browser_session_generation=reclaimed.fencing_generation,
        is_closed=False,
        custom_path=db_file,
    )
    state = get_runtime_control(custom_path=db_file)
    assert state["browser_active"] is True
    assert state["browser_job_id"] == reclaimed.id
    assert state["browser_session_generation"] == reclaimed.fencing_generation
    assert state["browser_url"] == "https://new.example/apply"

    set_runtime_browser_state(
        active=False,
        url="https://old.example/stale",
        browser_job_id=first.id,
        browser_session_generation=first.fencing_generation,
        is_closed=True,
        custom_path=db_file,
    )
    state = get_runtime_control(custom_path=db_file)
    assert state["browser_active"] is True
    assert state["browser_session_generation"] == reclaimed.fencing_generation


def test_same_job_reclaim_publication_uses_current_claim_generation(tmp_path: Path):
    db_file = tmp_path / "same_job_reclaim.db"
    init_db(db_file)
    upsert_application(
        app_id="app-reclaim",
        company="Reclaim Corp",
        title="Engineer",
        job_url="https://reclaim.example/apply",
        custom_path=db_file,
    )
    enqueue_job("app-reclaim", custom_path=db_file)
    first = claim_next_job("worker-first", custom_path=db_file)
    assert first is not None
    set_runtime_browser_state(
        active=True,
        url="https://reclaim.example/old",
        browser_job_id=first.id,
        browser_session_generation=first.fencing_generation,
        is_closed=False,
        custom_path=db_file,
    )

    conn = get_connection(db_file)
    try:
        conn.execute(
            "UPDATE automation_jobs SET lease_expires_at = '2000-01-01 00:00:00' WHERE id = ?;",
            (first.id,),
        )
        conn.commit()
    finally:
        conn.close()

    reclaimed = claim_next_job("worker-second", custom_path=db_file)
    assert reclaimed is not None
    assert reclaimed.id == first.id
    assert reclaimed.fencing_generation == first.fencing_generation + 1

    set_runtime_browser_state(
        active=True,
        url="https://reclaim.example/stale",
        browser_job_id=reclaimed.id,
        browser_session_generation=first.fencing_generation,
        is_closed=False,
        custom_path=db_file,
    )
    state = get_runtime_control(custom_path=db_file)
    assert state["browser_url"] == "https://reclaim.example/old"
    assert state["browser_session_generation"] == first.fencing_generation

    # A worker with the current generation but an expired lease must not
    # republish active browser state before another worker reclaims the job.
    conn = get_connection(db_file)
    try:
        conn.execute(
            "UPDATE automation_jobs SET lease_expires_at = '2000-01-01 00:00:00' WHERE id = ?;",
            (reclaimed.id,),
        )
        conn.commit()
    finally:
        conn.close()
    set_runtime_browser_state(
        active=True,
        url="https://reclaim.example/expired",
        browser_job_id=reclaimed.id,
        browser_session_generation=reclaimed.fencing_generation,
        is_closed=False,
        custom_path=db_file,
    )
    state = get_runtime_control(custom_path=db_file)
    assert state["browser_url"] == "https://reclaim.example/old"
    assert state["browser_session_generation"] == first.fencing_generation

    # Restore a live lease before testing the replacement worker's publication.
    conn = get_connection(db_file)
    try:
        conn.execute(
            "UPDATE automation_jobs SET lease_expires_at = '2099-01-01 00:00:00' WHERE id = ?;",
            (reclaimed.id,),
        )
        conn.commit()
    finally:
        conn.close()
    set_runtime_browser_state(
        active=True,
        url="https://reclaim.example/new",
        browser_job_id=reclaimed.id,
        browser_session_generation=reclaimed.fencing_generation,
        is_closed=False,
        custom_path=db_file,
    )
    state = get_runtime_control(custom_path=db_file)
    assert state["browser_url"] == "https://reclaim.example/new"
    assert state["browser_session_generation"] == reclaimed.fencing_generation


def test_safe_resume_rejects_browser_state_from_prior_job_generation(tmp_path: Path):
    db_file = tmp_path / "stale_resume_generation.db"
    init_db(db_file)
    upsert_application(
        app_id="app-stale-resume",
        company="Stale Resume Corp",
        title="Engineer",
        job_url="https://stale.example/apply",
        custom_path=db_file,
    )
    enqueue_job("app-stale-resume", custom_path=db_file)
    first = claim_next_job("worker-first", custom_path=db_file)
    assert first is not None
    set_runtime_browser_state(
        active=True,
        url="https://stale.example/apply",
        browser_job_id=first.id,
        browser_session_generation=first.fencing_generation,
        is_closed=False,
        custom_path=db_file,
    )

    conn = get_connection(db_file)
    try:
        conn.execute(
            "UPDATE automation_jobs SET fencing_generation = ?, lease_expires_at = '2099-01-01 00:00:00' WHERE id = ?;",
            (first.fencing_generation + 1, first.id),
        )
        conn.commit()
    finally:
        conn.close()

    result = safe_resume_revalidate(custom_path=db_file)
    assert result["status"] == "rejected"
    assert result["reason"] == "browser_owner_invalid"


def test_lost_lease_closes_browser_and_skips_final_sync(tmp_path: Path):
    db_file = tmp_path / "lost_lease_browser.db"
    init_db(db_file)
    automator = MagicMock()
    heartbeat = MagicMock()
    heartbeat.lost_lease = True
    heartbeat.generation = 1

    with patch(
        "job_applier.cli.worker._sync_runtime_browser_state_from_automator"
    ) as sync:
        _coordinate_challenge_takeover(
            automator=automator,
            job_id="expired-job",
            worker_id="worker-1",
            heartbeat=heartbeat,
            custom_db_path=db_file,
            max_wait_seconds=None,
        )

    automator.close.assert_called_once_with()
    sync.assert_not_called()


def test_runtime_worker_closes_stale_automator_after_cross_process_reopen(
    tmp_path: Path,
):
    db_file = tmp_path / "cross_process_reopen.db"
    init_db(db_file)
    set_runtime_browser_state(
        active=True,
        is_closed=False,
        browser_job_id="same-job",
        browser_session_generation=2,
        custom_path=db_file,
    )
    conn = get_connection(db_file)
    try:
        conn.execute(
            "UPDATE runtime_control SET browser_active = 0, browser_is_closed = 1 WHERE id = 1;"
        )
        conn.commit()
    finally:
        conn.close()

    automator = MagicMock()
    page = MagicMock()
    page.is_closed.return_value = False
    page.url = "https://old.example/apply"
    automator.page = page
    heartbeat = MagicMock()
    heartbeat.lost_lease = False
    heartbeat.generation = 1

    _coordinate_challenge_takeover(
        automator=automator,
        job_id="same-job",
        worker_id="old-worker",
        heartbeat=heartbeat,
        custom_db_path=db_file,
        max_wait_seconds=None,
    )

    # This models web reopening the job in another process: the runtime worker
    # observes the durable generation transition and closes its old browser.
    automator.close.assert_called_once_with()


def test_secret_environment_stripping():
    dirty_env = {
        "PATH": "/usr/local/bin:/usr/bin",
        "DISPLAY": ":99",
        "HOME": "/app",
        "USER": "job-applier",
        "GOOGLE_API_KEY": "AIzaSyD-secret-key-12345",
        "GEMINI_MODEL": "gemini-3.8-flash",
        "JOOBLE_API_KEY": "jooble-secret-token",
        "NTFY_TOKEN": "tk_abcdef123456",
        "CLOUDFLARE_TUNNEL_TOKEN": "eyJhIjoi...",
        "DATABASE_PASSWORD": "super-secret-db-pass",
        "SSH_PRIVATE_KEY": "-----BEGIN OPENSSH PRIVATE KEY-----",
        "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    }

    clean_env = get_sanitized_browser_env(dirty_env)

    # Safe variables must be preserved
    assert clean_env["PATH"] == "/usr/local/bin:/usr/bin"
    assert clean_env["DISPLAY"] == ":99"
    assert clean_env["HOME"] == "/app"
    assert clean_env["USER"] == "job-applier"

    # All secret variables must be strictly stripped
    assert "GOOGLE_API_KEY" not in clean_env
    assert "JOOBLE_API_KEY" not in clean_env
    assert "NTFY_TOKEN" not in clean_env
    assert "CLOUDFLARE_TUNNEL_TOKEN" not in clean_env
    assert "DATABASE_PASSWORD" not in clean_env
    assert "SSH_PRIVATE_KEY" not in clean_env
    assert "AWS_SECRET_ACCESS_KEY" not in clean_env
    assert "GEMINI_MODEL" not in clean_env


# =====================================================================
# 2. Ownership Tests: Profile Lock & CLI Refusal
# =====================================================================


def test_exclusive_profile_lock_and_cli_refusal(tmp_path):
    profile_dir = tmp_path / "shared_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)

    runtime_lock = ProfileOwnershipLock(profile_dir, owner_type="runtime")
    assert runtime_lock.acquire() is True
    assert runtime_lock.is_locked() is True

    # CLI attempt to acquire while runtime holds the lock must be refused
    cli_lock = ProfileOwnershipLock(profile_dir, owner_type="cli")
    with pytest.raises(
        ProfileOwnershipError,
        match="exclusively owned by the running automation runtime service",
    ):
        cli_lock.acquire()

    # Once runtime releases, CLI can acquire
    runtime_lock.release()
    assert runtime_lock.is_locked() is False

    assert cli_lock.acquire() is True
    assert cli_lock.is_locked() is True
    cli_lock.release()


# =====================================================================
# 3. Network Tests: Egress Denial & DNS Rebinding
# =====================================================================


@pytest.mark.parametrize(
    "ip_str, expected_denied",
    [
        # Loopback IPv4 & IPv6
        ("127.0.0.1", True),
        ("127.0.1.1", True),
        ("::1", True),
        # RFC 1918 Private IPv4
        ("10.0.0.1", True),
        ("10.254.254.254", True),
        ("172.16.0.1", True),
        ("172.31.255.255", True),
        ("192.168.1.1", True),
        ("192.168.100.50", True),
        # Link-local & Cloud Metadata
        ("169.254.169.254", True),
        ("169.254.1.1", True),
        ("fe80::1", True),
        ("fd00:ec2::254", True),  # AWS IPv6 metadata
        # CGNAT & ULA
        ("100.64.0.1", True),
        ("fc00::1", True),
        ("fd12:3456:789a::1", True),
        # IPv4-mapped IPv6
        ("::ffff:127.0.0.1", True),
        ("::ffff:169.254.169.254", True),
        ("::ffff:10.0.0.1", True),
        # Public IPs (must be allowed)
        ("8.8.8.8", False),
        ("1.1.1.1", False),
        ("140.82.121.3", False),  # GitHub
        ("2606:4700:4700::1111", False),  # Cloudflare IPv6
    ],
)
def test_ip_egress_denial_policy(ip_str: str, expected_denied: bool):
    ip_obj = ipaddress.ip_address(ip_str)
    denied, reason = is_ip_denied(ip_obj)
    assert denied == expected_denied, f"IP {ip_str} denial failed: {reason}"


def test_dns_rebinding_prevention():
    # Simulated DNS rebinding: Domain resolves to private/loopback IP
    def mock_rebinding_resolver(host: str) -> list[str]:
        if host == "rebind.attacker.com":
            return ["169.254.169.254"]  # AWS metadata
        if host == "split-brain.attacker.com":
            return ["93.184.216.34", "127.0.0.1"]  # Mixed public + loopback
        return ["93.184.216.34"]

    allowed, reason = validate_host_and_dns(
        "rebind.attacker.com", dns_resolver=mock_rebinding_resolver
    )
    assert allowed is False
    assert "DNS rebinding detected" in reason

    allowed_split, reason_split = validate_host_and_dns(
        "split-brain.attacker.com", dns_resolver=mock_rebinding_resolver
    )
    assert allowed_split is False
    assert "DNS rebinding detected" in reason_split


def test_validate_target_url_schemes_and_ports():
    # Dangerous schemes
    assert validate_target_url("file:///etc/passwd")[0] is False
    assert validate_target_url("ftp://ftp.example.com/data")[0] is False
    assert validate_target_url("data:text/html,<h1>test</h1>")[0] is False

    # Blocked internal ports
    assert validate_target_url("http://example.com:22/")[0] is False  # SSH port blocked
    assert (
        validate_target_url("http://example.com:5900/")[0] is False
    )  # VNC port blocked
    assert (
        validate_target_url("http://example.com:9222/")[0] is False
    )  # CDP port blocked
    assert (
        validate_target_url("http://example.com:8000/")[0] is False
    )  # Web port blocked

    # Blocked static hostnames
    assert validate_target_url("http://localhost:80/")[0] is False
    assert validate_target_url("http://metadata.google.internal/")[0] is False


def test_outbound_security_proxy_blocks_private_targets():
    proxy = OutboundSecurityProxy(port=18899)
    proxy.start()
    try:
        # Direct CONNECT to loopback or private IP via proxy socket should fail
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", 18899))
        connect_req = b"CONNECT 127.0.0.1:8000 HTTP/1.1\r\nHost: 127.0.0.1:8000\r\n\r\n"
        sock.sendall(connect_req)
        response = sock.recv(4096).decode("utf-8", errors="ignore")
        sock.close()

        assert "403" in response or "Direct egress denied" in response
    finally:
        proxy.stop()


# =====================================================================
# 4. Sandbox & Compose Security Configuration Tests
# =====================================================================


def test_browser_automator_no_no_sandbox_in_production():
    from job_applier.automation.browser_automator import BrowserAutomator

    # Ensure dangerous flag is not set
    with patch.dict("os.environ", {}, clear=False):
        automator = BrowserAutomator(headless=True, use_persistent_profile=False)
        mock_playwright = MagicMock()
        mock_chromium = MagicMock()
        mock_playwright.chromium = mock_chromium
        automator.playwright = mock_playwright

        with patch("playwright.sync_api.sync_playwright") as mock_sync_pw:
            mock_sync_pw.return_value.start.return_value = mock_playwright
            try:
                automator.start()
            except Exception:
                pass

        if mock_chromium.launch.called:
            kwargs = mock_chromium.launch.call_args.kwargs
            args = kwargs.get("args", [])
            assert "--no-sandbox" not in args, (
                "Production Chromium launch MUST NOT use --no-sandbox"
            )
        automator.close()


def test_compose_security_constraints():
    compose_path = Path("compose.yaml")
    assert compose_path.exists(), "compose.yaml must exist"

    with open(compose_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    services = data.get("services", {})
    assert "runtime" in services, "runtime service must be defined in compose.yaml"
    runtime = services["runtime"]

    # 1. Non-root user
    assert runtime.get("user") == "10001:10001", (
        "runtime must run as non-root user 10001:10001"
    )

    # 2. No privileged mode
    assert runtime.get("privileged") is False, "runtime must not run in privileged mode"

    # 3. Dropped capabilities
    assert "ALL" in runtime.get("cap_drop", []), "runtime must drop ALL capabilities"

    # 4. No new privileges
    assert "no-new-privileges:true" in runtime.get("security_opt", []), (
        "runtime must enforce no-new-privileges"
    )

    # 5. NO Docker socket mounted
    volumes = runtime.get("volumes", [])
    for v in volumes:
        assert "/var/run/docker.sock" not in str(v), (
            "runtime MUST NOT mount Docker socket"
        )

    # 6. No public port mappings for runtime (VNC/websockify are internal-only)
    assert "ports" not in runtime or len(runtime["ports"]) == 0, (
        "runtime must have no public host port exposure"
    )


def test_runtime_healthcheck_validates_vnc_bridge():
    compose_path = Path("compose.yaml")
    with open(compose_path, encoding="utf-8") as f:
        runtime = yaml.safe_load(f)["services"]["runtime"]

    assert runtime["healthcheck"]["test"] == [
        "CMD",
        "/app/.venv/bin/python",
        "-m",
        "job_applier.cli.runtime_healthcheck",
    ]

    from job_applier.cli.runtime_healthcheck import (
        _check_vnc_handshake,
        _read_websocket_frame,
    )

    fragmented_socket = MagicMock()
    fragmented_socket.__enter__.return_value = fragmented_socket
    fragmented_socket.recv.side_effect = [b"RFB 00", b"3.008\n"]
    with patch(
        "job_applier.cli.runtime_healthcheck.socket.create_connection",
        return_value=fragmented_socket,
    ):
        _check_vnc_handshake()

    eof_socket = MagicMock()
    eof_socket.__enter__.return_value = eof_socket
    eof_socket.recv.side_effect = [b"RFB ", b""]
    with patch(
        "job_applier.cli.runtime_healthcheck.socket.create_connection",
        return_value=eof_socket,
    ):
        with pytest.raises(RuntimeError, match="incomplete"):
            _check_vnc_handshake()

    frame = MagicMock()
    frame.recv.side_effect = [b"\x82\x0c", b"RFB 003.008\n"]
    assert _read_websocket_frame(frame) == b"RFB 003.008\n"


# =====================================================================
# 5. Upload Tests in Sandboxed Chromium
# =====================================================================


def test_cv_pdf_upload_under_chromium_sandbox(tmp_path):
    """Verifies that PDF file upload functions properly in Chromium with sandboxing active."""
    from playwright.sync_api import sync_playwright

    dummy_pdf = tmp_path / "CV_Alex_Example.pdf"
    dummy_pdf.write_bytes(b"%PDF-1.4 mock cv pdf content for upload test")

    html_file = tmp_path / "form.html"
    html_file.write_text(
        """<!DOCTYPE html>
<html><body>
<input type="file" id="cv_input" name="resume">
</body></html>""",
        encoding="utf-8",
    )

    with sync_playwright() as p:
        # Launch WITHOUT --no-sandbox (verifying sandbox compatibility)
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        page = browser.new_page()
        page.goto(f"file://{html_file.resolve()}")
        page.set_input_files("#cv_input", str(dummy_pdf.resolve()))

        val = page.locator("#cv_input").input_value()
        assert "CV_Alex_Example.pdf" in val
        browser.close()


# =====================================================================
# 6. Takeover & Safe Resume Tests
# =====================================================================


def test_atomic_takeover_lease_and_conflict(tmp_path):
    db_file = tmp_path / "test_takeover.db"
    init_db(db_file)

    # 1. Initially read-only default
    active, owner, _ = is_takeover_active(db_file)
    assert active is False
    assert owner is None

    # 2. Operator 1 claims takeover
    claim1 = claim_manual_takeover(
        owner="operator1@aslan.net", lease_seconds=120, custom_path=db_file
    )
    assert claim1["status"] == "success"
    assert claim1["is_paused"] is True  # Worker automatically paused

    active, owner, exp = is_takeover_active(db_file)
    assert active is True
    assert owner == "operator1@aslan.net"

    # 3. Operator 2 attempts claim while Operator 1's lease is active -> Conflict
    claim2 = claim_manual_takeover(
        owner="operator2@aslan.net", lease_seconds=120, custom_path=db_file
    )
    assert claim2["status"] == "conflict"

    # 4. Release takeover
    rel = release_manual_takeover(owner="operator1@aslan.net", custom_path=db_file)
    assert rel["status"] == "success"

    active_after, _, _ = is_takeover_active(db_file)
    assert active_after is False

    # Automation must remain paused until safe resume revalidation!
    ctrl = get_runtime_control(db_file)
    assert ctrl["is_paused"] is True


def test_safe_resume_revalidation_when_idle(tmp_path):
    db_file = tmp_path / "test_resume_idle.db"
    init_db(db_file)
    set_runtime_pause(True, custom_path=db_file)

    # No jobs in flight -> universal safe resume
    res = safe_resume_revalidate(custom_path=db_file)
    assert res["status"] == "success"
    assert res["action"] == "unpaused"

    ctrl = get_runtime_control(db_file)
    assert ctrl["is_paused"] is False


def test_safe_resume_detects_completed_takeover_submission(tmp_path):
    db_file = tmp_path / "test_resume_done.db"
    init_db(db_file)

    # Seed an application and claim job
    from job_applier.db import get_connection

    conn = get_connection(db_file)
    with conn:
        conn.execute(
            """
            INSERT INTO applications (
                id, company, title, job_url, platform, status, folder_name, created_at, updated_at
            ) VALUES ('app_done', 'Acme Corp', 'Lead Dev', 'https://jobs.lever.co/acme/123', 'Lever', 'pending', 'Acme_0', '2026-09-06', '2026-09-06');
            """
        )
    conn.close()

    enqueue_job("app_done", adapter="lever", custom_path=db_file)
    from job_applier.automation.queue import claim_next_job

    claimed = claim_next_job(worker_id="test_worker", custom_path=db_file)
    assert claimed is not None

    # Mock automator page currently at thank-you confirmation URL
    mock_automator = MagicMock()
    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://jobs.lever.co/acme/123/thanks"
    mock_page.locator.return_value.text_content.return_value = (
        "Thank you for applying to Acme Corp!"
    )
    mock_automator.page = mock_page

    res = safe_resume_revalidate(
        job_id=claimed.id,
        current_url=mock_page.url,
        automator=mock_automator,
        custom_path=db_file,
    )
    assert res["status"] == "success"
    assert res["action"] == "marked_applied"

    ctrl = get_runtime_control(db_file)
    assert ctrl["is_paused"] is False


def test_safe_resume_rejects_mismatched_domain(tmp_path):
    db_file = tmp_path / "test_resume_mismatch.db"
    init_db(db_file)

    from job_applier.db import get_connection

    conn = get_connection(db_file)
    with conn:
        conn.execute(
            """
            INSERT INTO applications (
                id, company, title, job_url, platform, status, folder_name, created_at, updated_at
            ) VALUES ('app_mis', 'Beta Inc', 'Eng', 'https://boards.greenhouse.io/beta/456', 'Greenhouse', 'pending', 'Beta_0', '2026-09-06', '2026-09-06');
            """
        )
    conn.close()

    enqueue_job("app_mis", custom_path=db_file)
    from job_applier.automation.queue import claim_next_job

    claimed = claim_next_job(worker_id="test_worker", custom_path=db_file)
    assert claimed is not None

    # Operator left browser on random external page (e.g. reddit.com)
    mock_automator = MagicMock()
    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://reddit.com/r/all"
    mock_automator.page = mock_page

    res = safe_resume_revalidate(
        job_id=claimed.id,
        current_url=mock_page.url,
        automator=mock_automator,
        custom_path=db_file,
    )
    assert res["status"] == "rejected"
    assert res["reason"] == "domain_mismatch"


@pytest.fixture
def client():
    return TestClient(app)


def test_access_takeover_status_exposes_current_owner():
    from types import SimpleNamespace
    from job_applier.web.app import takeover_status_endpoint

    request = cast(
        Any,
        SimpleNamespace(state=SimpleNamespace(user={"email": "viewer@example.test"})),
    )
    with (
        patch("job_applier.web.app.edge_config.cf_access_enabled", True),
        patch(
            "job_applier.automation.queue.is_takeover_active",
            return_value=(True, "owner@example.test", "future"),
        ),
        patch("job_applier.automation.queue.get_runtime_control", return_value={}),
    ):
        status = takeover_status_endpoint(request)

    assert status["is_takeover_active"] is True
    assert status["owner"] == "owner@example.test"
    assert status["is_current_owner"] is False
    assert status["read_only"] is True


def test_access_viewer_cannot_force_release_takeover():
    from types import SimpleNamespace
    from job_applier.web.app import (
        TakeoverReleaseRequest,
        release_takeover_endpoint,
    )
    from fastapi import HTTPException

    request = cast(
        Any,
        SimpleNamespace(state=SimpleNamespace(user={"email": "viewer@example.test"})),
    )
    with patch("job_applier.web.app.edge_config.cf_access_enabled", True):
        with pytest.raises(HTTPException) as exc_info:
            release_takeover_endpoint(TakeoverReleaseRequest(force=True), request)

    assert exc_info.value.status_code == 403
    assert "Forced takeover release" in str(exc_info.value.detail)


def test_safe_resume_and_stop_clear_reject_replaced_claimant(tmp_path: Path):
    from job_applier.automation.queue import TAKEOVER_TRANSPORT_LOCK
    from job_applier.ops.emergency_stop import clear_emergency_stop

    db_file = tmp_path / "takeover_replacement_resume.db"
    init_db(db_file)
    assert (
        claim_manual_takeover("old-owner", custom_path=db_file)["status"] == "success"
    )
    set_runtime_stop(True, custom_path=db_file)

    # A stale safe-resume request waits behind the transport fence while a new
    # claimant replaces the old lease. It must reject and preserve the stop.
    result: dict[str, Any] = {}
    TAKEOVER_TRANSPORT_LOCK.acquire()
    try:
        worker = threading.Thread(
            target=lambda: result.update(
                safe_resume_revalidate(
                    custom_path=db_file,
                    expected_owner="old-owner",
                )
            )
        )
        worker.start()
        time.sleep(0.05)
        assert worker.is_alive()
        assert (
            release_manual_takeover("old-owner", custom_path=db_file)["status"]
            == "success"
        )
        assert (
            claim_manual_takeover("new-owner", custom_path=db_file)["status"]
            == "success"
        )
        set_runtime_stop(True, custom_path=db_file)
    finally:
        TAKEOVER_TRANSPORT_LOCK.release()
    worker.join(timeout=2)

    assert result["status"] == "rejected"
    assert result["reason"] == "takeover_owner_required"
    assert get_runtime_control(custom_path=db_file)["is_stopped"] is True
    assert (
        clear_emergency_stop(custom_db_path=db_file, expected_owner="old-owner")[
            "status"
        ]
        == "rejected"
    )
    assert get_runtime_control(custom_path=db_file)["is_stopped"] is True


def test_verification_code_rejects_replaced_claimant_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from types import SimpleNamespace
    from job_applier.automation.queue import TAKEOVER_TRANSPORT_LOCK
    from job_applier.web.app import SubmitCodeRequest, submit_verification_code

    db_file = tmp_path / "takeover_replacement_code.db"
    monkeypatch.setenv("JOB_APPLIER_DB_PATH", str(db_file))
    init_db(db_file)
    assert (
        claim_manual_takeover("old-owner", custom_path=db_file)["status"] == "success"
    )
    set_runtime_browser_state(
        active=True,
        url="https://example.com/apply",
        is_closed=False,
        is_waiting_for_code=True,
        custom_path=db_file,
    )
    automator = MagicMock()
    automator.waiting_for_code = True
    request = cast(
        Any,
        SimpleNamespace(state=SimpleNamespace(user={"email": "old-owner"})),
    )
    result: dict[str, Any] = {}
    TAKEOVER_TRANSPORT_LOCK.acquire()
    try:
        with (
            patch("job_applier.web.app.edge_config.cf_access_enabled", True),
            patch(
                "job_applier.automation.browser_automator.get_active_automator",
                return_value=automator,
            ),
        ):
            worker = threading.Thread(
                target=lambda: _capture_http_error(
                    result,
                    submit_verification_code,
                    SubmitCodeRequest(code="123456"),
                    request,
                )
            )
            worker.start()
            time.sleep(0.05)
            assert worker.is_alive()
            assert (
                release_manual_takeover("old-owner", custom_path=db_file)["status"]
                == "success"
            )
            assert (
                claim_manual_takeover("new-owner", custom_path=db_file)["status"]
                == "success"
            )
    finally:
        TAKEOVER_TRANSPORT_LOCK.release()
    worker.join(timeout=2)

    assert result["status_code"] == 403
    automator.supply_verification_code.assert_not_called()
    assert get_runtime_control(custom_path=db_file)["pending_verification_code"] is None


def _capture_http_error(result: dict[str, Any], function: Any, *args: Any) -> None:
    from fastapi import HTTPException

    try:
        function(*args)
    except HTTPException as exc:
        result["status_code"] = exc.status_code


def test_access_takeover_mutations_require_current_owner():
    from types import SimpleNamespace
    from fastapi import HTTPException
    from job_applier.web.app import (
        reopen_auth_session_endpoint,
        resume_from_takeover_endpoint,
    )

    request = cast(
        Any,
        SimpleNamespace(state=SimpleNamespace(user={"email": "viewer@example.test"})),
    )
    with (
        patch("job_applier.web.app.edge_config.cf_access_enabled", True),
        patch(
            "job_applier.automation.queue.is_takeover_active",
            return_value=(True, "owner@example.test", "future"),
        ),
    ):
        with pytest.raises(HTTPException) as reopen_error:
            reopen_auth_session_endpoint(request)
        with pytest.raises(HTTPException) as resume_error:
            resume_from_takeover_endpoint(request)

    assert reopen_error.value.status_code == 403
    assert resume_error.value.status_code == 403


def test_access_non_owner_cannot_mutate_takeover_or_global_controls():
    from types import SimpleNamespace
    from fastapi import HTTPException
    from job_applier.web.app import (
        ResolveJobRequest,
        SubmitCodeRequest,
        clear_automation_state_endpoint,
        pause_automation_endpoint,
        resolve_job_endpoint,
        resume_automation_endpoint,
        stop_automation_endpoint,
        submit_verification_code,
    )

    request = cast(
        Any,
        SimpleNamespace(state=SimpleNamespace(user={"email": "viewer@example.test"})),
    )
    with (
        patch("job_applier.web.app.edge_config.cf_access_enabled", True),
        patch(
            "job_applier.automation.queue.is_takeover_active",
            return_value=(True, "owner@example.test", "future"),
        ),
    ):
        actions = (
            lambda: pause_automation_endpoint(request),
            lambda: resume_automation_endpoint(request),
            lambda: stop_automation_endpoint(request),
            lambda: clear_automation_state_endpoint(request),
            lambda: submit_verification_code(SubmitCodeRequest(code="123456"), request),
            lambda: resolve_job_endpoint(
                "job-1", ResolveJobRequest(force=True), request
            ),
        )
        for action in actions:
            with pytest.raises(HTTPException) as exc_info:
                action()
            assert exc_info.value.status_code == 403


def test_access_takeover_mutations_pass_verified_owner():
    from types import SimpleNamespace
    from job_applier.web.app import (
        reopen_auth_session_endpoint,
        resume_from_takeover_endpoint,
    )

    request = cast(
        Any,
        SimpleNamespace(state=SimpleNamespace(user={"email": "owner@example.test"})),
    )
    with (
        patch("job_applier.web.app.edge_config.cf_access_enabled", True),
        patch(
            "job_applier.automation.queue.is_takeover_active",
            return_value=(True, "owner@example.test", "future"),
        ),
        patch(
            "job_applier.automation.queue.requeue_auth_required_job",
            return_value={"status": "started"},
        ) as requeue,
        patch(
            "job_applier.automation.safe_resume.safe_resume_revalidate",
            return_value={"status": "success"},
        ) as resume,
        patch("job_applier.automation.browser_automator.get_active_automator"),
    ):
        assert reopen_auth_session_endpoint(request) == {"status": "started"}
        assert resume_from_takeover_endpoint(request) == {"status": "success"}

    assert requeue.call_args.kwargs["expected_owner"] == "owner@example.test"
    assert resume.call_args.kwargs["expected_owner"] == "owner@example.test"


@pytest.mark.parametrize("shutdown_action", ["clear", "emergency"])
def test_runtime_worker_closes_browser_after_durable_shutdown(
    tmp_path: Path, shutdown_action: str
):
    """Clear and emergency stop signal the runtime process to close its browser."""
    db_file = tmp_path / f"browser_shutdown_{shutdown_action}.db"
    init_db(db_file)
    conn = get_connection(db_file)
    with conn:
        conn.execute(
            """
            UPDATE runtime_control
            SET browser_active = 1, browser_is_closed = 0,
                browser_job_id = 'job-1', browser_session_generation = 1
            WHERE id = 1;
            """
        )
    conn.close()

    if shutdown_action == "clear":
        from job_applier.automation.queue import clear_automation_state

        result = clear_automation_state(custom_path=db_file)
    else:
        from job_applier.ops.emergency_stop import emergency_stop

        result = emergency_stop(custom_db_path=db_file)
    assert result["status"] == "success"
    control = get_runtime_control(custom_path=db_file)
    assert control["browser_job_id"] is None
    assert control["browser_active"] is False
    assert control["browser_shutdown_generation"] is not None

    automator = MagicMock()
    page = MagicMock()
    page.is_closed.return_value = False
    page.url = "https://example.test"
    automator.page = page
    heartbeat = MagicMock()
    heartbeat.lost_lease = False
    heartbeat.generation = 1
    _coordinate_challenge_takeover(
        automator=automator,
        job_id="job-1",
        worker_id="worker-1",
        heartbeat=heartbeat,
        custom_db_path=db_file,
        max_wait_seconds=1,
    )
    automator.close.assert_called_once_with()


def _prepare_emergency_ambiguity_db(db_file: Path) -> tuple[str, str]:
    """Create two leased jobs already inside the outcome-unknown boundary."""
    init_db(db_file)
    job_ids: list[str] = []
    for index, state in enumerate(("submit_intent", "verifying")):
        app_id = f"app-emergency-{index}"
        upsert_application(
            app_id=app_id,
            company=f"Emergency {index}",
            title="Engineer",
            job_url=f"https://emergency.example/{index}",
            custom_path=db_file,
        )
        enqueue_job(app_id, adapter="greenhouse", custom_path=db_file)
        job = claim_next_job("emergency-worker", lease_seconds=60, custom_path=db_file)
        assert job is not None
        attempt = create_attempt(
            job_id=job.id,
            app_id=app_id,
            worker_id="emergency-worker",
            lease_generation=job.fencing_generation,
            custom_path=db_file,
        )
        assert record_submit_intent(
            attempt.id,
            job.id,
            "emergency-worker",
            job.fencing_generation,
            custom_path=db_file,
        )
        if state == "verifying":
            conn = get_connection(db_file)
            with conn:
                conn.execute(
                    "UPDATE automation_jobs SET state = 'verifying' WHERE id = ?;",
                    (job.id,),
                )
            conn.close()
        job_ids.append(job.id)
    return job_ids[0], job_ids[1]


def test_emergency_stop_rejects_expired_owner_without_any_side_effects(
    tmp_path: Path,
):
    from job_applier.automation.queue import TAKEOVER_TRANSPORT_LOCK
    from job_applier.ops.emergency_stop import emergency_stop

    db_file = tmp_path / "emergency_expired_owner.db"
    first_job, second_job = _prepare_emergency_ambiguity_db(db_file)
    assert (
        claim_manual_takeover("old-owner", lease_seconds=1, custom_path=db_file)[
            "status"
        ]
        == "success"
    )

    result: dict[str, Any] = {}
    TAKEOVER_TRANSPORT_LOCK.acquire()
    try:
        worker = threading.Thread(
            target=lambda: result.update(
                emergency_stop(custom_db_path=db_file, expected_owner="old-owner")
            )
        )
        worker.start()
        time.sleep(0.1)
        assert worker.is_alive()
        time.sleep(1.1)
    finally:
        TAKEOVER_TRANSPORT_LOCK.release()
    worker.join(timeout=3)

    assert result["status"] == "rejected"
    conn = get_connection(db_file)
    rows = conn.execute(
        "SELECT id, state FROM automation_jobs WHERE id IN (?, ?) ORDER BY id;",
        (first_job, second_job),
    ).fetchall()
    control = conn.execute(
        "SELECT is_stopped, is_paused FROM runtime_control WHERE id = 1;"
    ).fetchone()
    notification_count = conn.execute("SELECT COUNT(*) FROM notifications;").fetchone()[
        0
    ]
    conn.close()
    assert [row["state"] for row in rows] == ["submit_intent", "verifying"]
    assert control["is_stopped"] == 0
    assert control["is_paused"] == 1
    assert notification_count == 0


def test_emergency_stop_rejects_replaced_owner_without_side_effects(tmp_path: Path):
    from job_applier.automation.queue import TAKEOVER_TRANSPORT_LOCK
    from job_applier.ops.emergency_stop import emergency_stop

    db_file = tmp_path / "emergency_replaced_owner.db"
    first_job, second_job = _prepare_emergency_ambiguity_db(db_file)
    assert (
        claim_manual_takeover("old-owner", custom_path=db_file)["status"] == "success"
    )
    result: dict[str, Any] = {}
    TAKEOVER_TRANSPORT_LOCK.acquire()
    try:
        worker = threading.Thread(
            target=lambda: result.update(
                emergency_stop(custom_db_path=db_file, expected_owner="old-owner")
            )
        )
        worker.start()
        time.sleep(0.1)
        assert worker.is_alive()
        assert (
            release_manual_takeover("old-owner", custom_path=db_file)["status"]
            == "success"
        )
        assert (
            claim_manual_takeover("new-owner", custom_path=db_file)["status"]
            == "success"
        )
    finally:
        TAKEOVER_TRANSPORT_LOCK.release()
    worker.join(timeout=3)

    assert result["status"] == "rejected"
    conn = get_connection(db_file)
    rows = conn.execute(
        "SELECT id, state FROM automation_jobs WHERE id IN (?, ?) ORDER BY id;",
        (first_job, second_job),
    ).fetchall()
    control = conn.execute(
        "SELECT manual_takeover_owner, is_stopped, is_paused FROM runtime_control WHERE id = 1;"
    ).fetchone()
    notification_count = conn.execute("SELECT COUNT(*) FROM notifications;").fetchone()[
        0
    ]
    conn.close()
    assert [row["state"] for row in rows] == ["submit_intent", "verifying"]
    assert control["manual_takeover_owner"] == "new-owner"
    assert control["is_stopped"] == 0
    assert control["is_paused"] == 1
    assert notification_count == 0


def test_emergency_stop_commits_all_ambiguity_projections_once(tmp_path: Path):
    from job_applier.ops.emergency_stop import emergency_stop

    db_file = tmp_path / "emergency_success_transaction.db"
    first_job, second_job = _prepare_emergency_ambiguity_db(db_file)
    result = emergency_stop(reason="multi-job test", custom_db_path=db_file)
    assert result["status"] == "success"

    conn = get_connection(db_file)
    jobs = conn.execute(
        "SELECT id, state, lease_owner FROM automation_jobs WHERE id IN (?, ?) ORDER BY id;",
        (first_job, second_job),
    ).fetchall()
    apps = conn.execute(
        "SELECT status FROM applications WHERE id IN ('app-emergency-0', 'app-emergency-1') ORDER BY id;"
    ).fetchall()
    attempts = conn.execute(
        "SELECT is_ambiguous, completed_at FROM application_attempts WHERE job_id IN (?, ?) ORDER BY job_id;",
        (first_job, second_job),
    ).fetchall()
    control = conn.execute(
        "SELECT is_stopped, is_paused, browser_is_closed, browser_shutdown_generation FROM runtime_control WHERE id = 1;"
    ).fetchone()
    audit_count = conn.execute(
        "SELECT COUNT(*) FROM automation_events WHERE event_type = 'emergency_stop_engaged';"
    ).fetchone()[0]
    notification_count = conn.execute(
        "SELECT COUNT(*) FROM notification_outbox WHERE category = 'system_failure';"
    ).fetchone()[0]
    conn.close()
    assert [row["state"] for row in jobs] == [
        "ambiguous_submission",
        "ambiguous_submission",
    ]
    assert all(row["lease_owner"] is None for row in jobs)
    assert [row["status"] for row in apps] == ["ambiguous", "ambiguous"]
    assert all(row["is_ambiguous"] == 1 and row["completed_at"] for row in attempts)
    assert control["is_stopped"] == 1
    assert control["is_paused"] == 1
    assert control["browser_is_closed"] == 1
    assert control["browser_shutdown_generation"] >= 1
    assert audit_count == 1
    assert notification_count == 1


def test_emergency_stop_rolls_back_all_projections_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from job_applier.ops import emergency_stop as emergency_stop_module

    db_file = tmp_path / "emergency_rollback.db"
    first_job, second_job = _prepare_emergency_ambiguity_db(db_file)
    original = emergency_stop_module._mark_job_ambiguous_on_conn
    calls = 0

    def fail_on_second(conn: Any, *args: Any, **kwargs: Any) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected ambiguity failure")
        return original(conn, *args, **kwargs)

    monkeypatch.setattr(
        emergency_stop_module, "_mark_job_ambiguous_on_conn", fail_on_second
    )
    with pytest.raises(RuntimeError, match="injected ambiguity failure"):
        emergency_stop_module.emergency_stop(custom_db_path=db_file)

    conn = get_connection(db_file)
    rows = conn.execute(
        "SELECT id, state FROM automation_jobs WHERE id IN (?, ?) ORDER BY id;",
        (first_job, second_job),
    ).fetchall()
    control = conn.execute(
        "SELECT is_stopped, is_paused FROM runtime_control WHERE id = 1;"
    ).fetchone()
    notification_count = conn.execute("SELECT COUNT(*) FROM notifications;").fetchone()[
        0
    ]
    conn.close()
    assert [row["state"] for row in rows] == ["submit_intent", "verifying"]
    assert control["is_stopped"] == 0
    assert control["is_paused"] == 0
    assert notification_count == 0


def test_emergency_stop_acquires_queue_lock_before_sqlite_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The emergency transaction follows queue lock ordering before event writes."""
    from job_applier.ops import emergency_stop as emergency_stop_module

    db_file = tmp_path / "emergency-lock-order.db"
    init_db(db_file)
    lock_owned_at_event: list[bool] = []
    original_record_event = emergency_stop_module.record_event

    def record_event_with_lock_probe(*args: Any, **kwargs: Any) -> Any:
        lock_owned_at_event.append(_QUEUE_LOCK._is_owned())  # type: ignore[attr-defined]
        return original_record_event(*args, **kwargs)

    monkeypatch.setattr(
        emergency_stop_module, "record_event", record_event_with_lock_probe
    )

    result = emergency_stop_module.emergency_stop(custom_db_path=db_file)

    assert result["status"] == "success"
    assert lock_owned_at_event == [True]


def test_emergency_stop_releases_transport_lock_when_connection_fails(tmp_path: Path):
    from job_applier.automation.queue import TAKEOVER_TRANSPORT_LOCK
    from job_applier.ops import emergency_stop as emergency_stop_module

    init_db(tmp_path / "emergency-failure.db")
    with patch.object(
        emergency_stop_module,
        "get_connection",
        side_effect=RuntimeError("database unavailable"),
    ):
        with pytest.raises(RuntimeError, match="database unavailable"):
            emergency_stop_module.emergency_stop(
                custom_db_path=tmp_path / "emergency-failure.db"
            )

    assert TAKEOVER_TRANSPORT_LOCK.acquire(blocking=False)
    TAKEOVER_TRANSPORT_LOCK.release()


def test_web_app_takeover_endpoints(client):
    # Status
    st_res = client.get("/api/automation/takeover/status")
    assert st_res.status_code == 200
    assert st_res.json()["read_only"] is True

    # Claim takeover
    claim_res = client.post(
        "/api/automation/takeover/claim",
        json={"owner": "operator@aslan.net", "lease_seconds": 300},
    )
    assert claim_res.status_code == 200
    assert claim_res.json()["owner"] == "operator@aslan.net"

    # Status shows active takeover
    st_res2 = client.get("/api/automation/takeover/status")
    assert st_res2.json()["is_takeover_active"] is True
    assert st_res2.json()["read_only"] is False

    # Release takeover
    rel_res = client.post(
        "/api/automation/takeover/release",
        json={"owner": "operator@aslan.net", "force": False},
    )
    assert rel_res.status_code == 200

    # Resume from takeover
    resume_res = client.post("/api/automation/takeover/resume")
    assert resume_res.status_code == 200
    assert resume_res.json()["status"] == "success"


def test_toctou_dns_rebinding_returns_validated_ips():
    """Verifies that resolve_and_validate_host returns validated IPs and validate_host_and_dns preserves tuple unpacking."""
    allowed, reason, ips = resolve_and_validate_host(
        "example.com", dns_resolver=lambda h: ["93.184.216.34"]
    )
    assert allowed is True
    assert ips == ["93.184.216.34"]

    # Test rejection of rebinding to private IP
    allowed_bad, reason_bad, ips_bad = resolve_and_validate_host(
        "rebind.example.com", dns_resolver=lambda h: ["127.0.0.1"]
    )
    assert allowed_bad is False
    assert "DNS rebinding" in reason_bad
    assert ips_bad == []

    # Verify backwards compatible 2-tuple unpacking on validate_host_and_dns
    allowed_compat, reason_compat = validate_host_and_dns(
        "example.com", dns_resolver=lambda h: ["93.184.216.34"]
    )
    assert allowed_compat is True


def test_safe_resume_updates_applications_status_to_applied(tmp_path: Path):
    from job_applier.automation.safe_resume import safe_resume_revalidate

    db_file = tmp_path / "resume_status.db"
    init_db(db_file)

    upsert_application(
        app_id="app-status-1",
        company="Applied Corp",
        title="Developer",
        job_url="https://jobs.lever.co/applied/1",
        custom_path=db_file,
    )
    enqueue_job("app-status-1", adapter="lever", custom_path=db_file)
    claimed = claim_next_job("w-status", custom_path=db_file)
    assert claimed is not None

    mock_automator = MagicMock()
    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://jobs.lever.co/applied/1/thanks"
    mock_page.locator.return_value.text_content.return_value = (
        "Thank you for your application!"
    )
    mock_automator.page = mock_page

    with patch(
        "job_applier.automation.safe_resume.validate_target_url",
        return_value=(True, ""),
    ):
        res = safe_resume_revalidate(
            job_id=claimed.id,
            current_url=mock_page.url,
            automator=mock_automator,
            custom_path=db_file,
        )
    assert res["status"] == "success"
    assert res["action"] == "marked_applied"

    conn = get_connection(db_file)
    app_row = conn.execute(
        "SELECT status FROM applications WHERE id = 'app-status-1';"
    ).fetchone()
    conn.close()
    assert app_row["status"] == "applied"


def test_vnc_password_validation_and_generated_file_cleanup(
    tmp_path: Path, monkeypatch
):
    from job_applier.cli import vnc_auth

    with monkeypatch.context() as context:
        context.setenv("VNC_PASSWORD", "secret8!")
        assert vnc_auth.load_vnc_password() == "secret8!"
        for invalid in ("", "123456789", "pässword", "bad\npass"):
            if invalid:
                context.setenv("VNC_PASSWORD", invalid)
            else:
                context.delenv("VNC_PASSWORD", raising=False)
            with pytest.raises(RuntimeError):
                vnc_auth.load_vnc_password()

    def fake_store(cmd, *, env, input, **kwargs):
        assert cmd == ["x11vnc", "-storepasswd"]
        assert "secret8!" in input
        assert "VNC_PASSWORD" not in env
        stored = Path(env["HOME"]) / ".vnc" / "passwd"
        stored.write_bytes(b"encrypted")
        return MagicMock(returncode=0)

    with patch("job_applier.cli.vnc_auth.subprocess.run", side_effect=fake_store):
        generated = vnc_auth.create_vnc_password_file("secret8!")
    assert generated.stat().st_mode & 0o777 == 0o600
    assert generated.read_bytes() == b"encrypted"
    vnc_auth.cleanup_vnc_password_file(generated)
    assert not generated.exists()


def test_runtime_daemon_rejects_missing_vnc_password_before_starting(tmp_path: Path):
    from job_applier.cli.runtime_daemon import RuntimeDaemon

    daemon = RuntimeDaemon(profile_dir=tmp_path / "profile")
    with (
        patch("job_applier.cli.runtime_daemon.is_linux", return_value=True),
        patch(
            "job_applier.cli.runtime_daemon.load_vnc_password",
            side_effect=RuntimeError("VNC_PASSWORD is required"),
        ),
        patch("shutil.which", return_value="/usr/bin/mock"),
        patch("job_applier.cli.runtime_daemon.subprocess.Popen") as popen,
    ):
        with pytest.raises(RuntimeError, match="VNC_PASSWORD is required"):
            daemon.start_display_subsystem()
    popen.assert_not_called()


def test_runtime_daemon_websockify_and_x11vnc_commands(tmp_path: Path):
    from job_applier.cli.runtime_daemon import RuntimeDaemon

    novnc_dir = tmp_path / "novnc"
    novnc_dir.mkdir(parents=True)
    (novnc_dir / "vnc.html").write_text("dummy", encoding="utf-8")

    daemon = RuntimeDaemon(
        display=":99",
        vnc_port=5900,
        websockify_port=6080,
        novnc_dir=novnc_dir,
    )

    started_cmds = []
    password_file = tmp_path / "vnc.passwd"
    password_file.write_bytes(b"encrypted")
    previous_password_file = tmp_path / "previous-vnc.passwd"
    previous_password_file.write_bytes(b"old-encrypted")
    daemon.vnc_password_file = previous_password_file

    def mock_popen(cmd, *args, **kwargs):
        started_cmds.append(cmd)
        proc = MagicMock()
        proc.poll.return_value = None
        return proc

    with (
        patch("job_applier.cli.runtime_daemon.is_linux", return_value=True),
        patch(
            "job_applier.cli.runtime_daemon.load_vnc_password", return_value="testpass"
        ),
        patch(
            "job_applier.cli.runtime_daemon.create_vnc_password_file",
            return_value=password_file,
        ),
        patch("shutil.which", return_value="/usr/bin/mock"),
        patch("subprocess.Popen", side_effect=mock_popen),
    ):
        daemon.start_display_subsystem()

    # A retry removes the previous generated credential before replacing its path.
    assert not previous_password_file.exists()

    # Verify x11vnc does not have -viewonly
    vnc_cmd = next(c for c in started_cmds if c[0] == "x11vnc")
    assert "-viewonly" not in vnc_cmd
    assert "-nopw" not in vnc_cmd
    assert "-localhost" in vnc_cmd
    assert "-rfbauth" in vnc_cmd
    assert vnc_cmd[vnc_cmd.index("-rfbauth") + 1] == str(password_file)

    # Verify websockify has --web pointing to novnc_dir
    ws_cmd = next(c for c in started_cmds if c[0] == "websockify")
    assert "--web" in ws_cmd
    web_idx = ws_cmd.index("--web")
    assert ws_cmd[web_idx + 1] == str(novnc_dir)

    # Verify idle desktop wallpaper command is started
    wp_cmd = next(
        c
        for c in started_cmds
        if len(c) >= 3
        and c[0] == sys.executable
        and "Job Applier Browser Runtime" in c[2]
    )
    assert wp_cmd is not None
    assert daemon.wallpaper_proc is not None

    # Re-running startup on the same daemon must not spawn another viewer stack.
    with (
        patch("job_applier.cli.runtime_daemon.is_linux", return_value=True),
        patch("shutil.which", return_value="/usr/bin/mock"),
        patch("subprocess.Popen", side_effect=mock_popen),
    ):
        daemon.start_display_subsystem()
    assert sum(cmd[0] == "websockify" for cmd in started_cmds) == 1

    daemon.shutdown()
    assert (
        getattr(daemon.wallpaper_proc, "terminate").called
        or daemon.wallpaper_proc.poll() is not None
    )
    assert not password_file.exists()


def test_heartbeat_exception_only_signals_loss(monkeypatch):
    monkeypatch.setattr(
        "job_applier.cli.worker.renew_lease",
        MagicMock(side_effect=sqlite3.OperationalError("database is locked")),
    )
    heartbeat = HeartbeatThread(
        job_id="job-heartbeat",
        worker_id="worker-heartbeat",
        generation=1,
        interval=0.01,
    )
    heartbeat.start()
    heartbeat.join(timeout=1)

    assert not heartbeat.is_alive()
    assert heartbeat.lost_lease is True
    assert heartbeat.lost_lease_event.is_set()


def test_worker_closes_without_publication_when_lease_lost_during_startup(
    tmp_path: Path,
):
    db_file = tmp_path / "startup_lease_loss.db"
    init_db(db_file)
    upsert_application(
        "app-startup-lease-loss",
        "Startup Corp",
        "Engineer",
        "https://startup.example/job/1",
        folder_name="startup_lease_loss",
        custom_path=db_file,
    )
    enqueue_job("app-startup-lease-loss", adapter="generic", custom_path=db_file)
    claimed = claim_next_job("startup-worker", custom_path=db_file)
    assert claimed is not None
    cv_path = tmp_path / "CV.pdf"
    cv_path.write_bytes(b"%PDF-1.4 test")
    profile = MagicMock()
    profile.to_dict.return_value = {}
    automator = MagicMock()
    heartbeat_holder: dict[str, Any] = {}

    class FakeHeartbeat:
        def __init__(self, **kwargs: Any):
            self.lost_lease = False
            self.generation = kwargs["generation"]
            heartbeat_holder["instance"] = self

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    def lose_lease_during_start() -> None:
        heartbeat_holder["instance"].lost_lease = True

    close_thread_ids: list[int] = []
    automator.close.side_effect = lambda: close_thread_ids.append(threading.get_ident())
    automator.start.side_effect = lose_lease_during_start
    with (
        patch("job_applier.cli.worker.HeartbeatThread", FakeHeartbeat),
        patch("job_applier.cli.worker.BrowserAutomator", return_value=automator),
        patch(
            "job_applier.cli.worker.validate_application_artifacts",
            return_value=(True, str(cv_path)),
        ),
        patch("job_applier.cli.worker.load_candidate_profile", return_value=profile),
        patch(
            "job_applier.cli.worker.SubmissionSafetyGuard.validate_pacing_and_daily_limits"
        ),
    ):
        result = process_claimed_job(
            claimed, worker_id="startup-worker", custom_db_path=db_file
        )

    assert result == "lease_lost"
    automator.run_autonomous_apply.assert_not_called()
    automator.close.assert_called_once()
    assert close_thread_ids == [threading.get_ident()]
    control = get_runtime_control(db_file)
    assert control["browser_active"] is False


def test_worker_skips_reconciliation_when_lease_lost_after_apply(tmp_path: Path):
    db_file = tmp_path / "post_apply_lease_loss.db"
    init_db(db_file)
    upsert_application(
        "app-post-apply-lease-loss",
        "Apply Corp",
        "Engineer",
        "https://apply.example/job/1",
        folder_name="post_apply_lease_loss",
        custom_path=db_file,
    )
    enqueue_job("app-post-apply-lease-loss", adapter="generic", custom_path=db_file)
    claimed = claim_next_job("apply-worker", custom_path=db_file)
    assert claimed is not None
    cv_path = tmp_path / "CV.pdf"
    cv_path.write_bytes(b"%PDF-1.4 test")
    profile = MagicMock()
    profile.to_dict.return_value = {}
    automator = MagicMock()
    heartbeat_holder: dict[str, Any] = {}

    class FakeHeartbeat:
        def __init__(self, **kwargs: Any):
            self.lost_lease = False
            self.generation = kwargs["generation"]
            heartbeat_holder["instance"] = self

        def start(self) -> None:
            pass

        def stop(self) -> None:
            pass

    def lose_lease_after_apply(**_: Any) -> tuple[str, str]:
        heartbeat_holder["instance"].lost_lease = True
        return "applied", "submitted"

    automator.run_autonomous_apply.side_effect = lose_lease_after_apply
    with (
        patch("job_applier.cli.worker.HeartbeatThread", FakeHeartbeat),
        patch("job_applier.cli.worker.BrowserAutomator", return_value=automator),
        patch(
            "job_applier.cli.worker.validate_application_artifacts",
            return_value=(True, str(cv_path)),
        ),
        patch("job_applier.cli.worker.load_candidate_profile", return_value=profile),
        patch(
            "job_applier.cli.worker.SubmissionSafetyGuard.validate_pacing_and_daily_limits"
        ),
        patch(
            "job_applier.cli.worker.SubmissionSafetyGuard.reconcile_submission_outcome"
        ) as reconcile,
    ):
        result = process_claimed_job(
            claimed, worker_id="apply-worker", custom_db_path=db_file
        )

    assert result == "lease_lost"
    reconcile.assert_not_called()
    automator.close.assert_called_once()


def test_runtime_daemon_timeout_publishes_browser_shutdown_fence(tmp_path: Path):
    from job_applier.cli.runtime_daemon import RuntimeDaemon

    db_file = tmp_path / "daemon_timeout.db"
    init_db(db_file)
    conn = get_connection(db_file)
    with conn:
        conn.execute(
            """
            UPDATE runtime_control
            SET browser_active = 1, browser_is_closed = 0,
                browser_job_id = 'job-stale', browser_session_generation = 4,
                browser_shutdown_generation = 4
            WHERE id = 1;
            """
        )
    conn.close()

    class StubbornWorker:
        def is_alive(self):
            return True

        def join(self, timeout):
            return None

    daemon = RuntimeDaemon(custom_db_path=db_file, worker_shutdown_timeout=0.01)
    daemon.__dict__["_worker_thread"] = StubbornWorker()
    with (
        patch.object(daemon.outbound_proxy, "stop"),
        patch.object(daemon.profile_lock, "release"),
        patch.object(daemon.singleton_lock, "release"),
    ):
        daemon.shutdown()

    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT browser_active, browser_is_closed, browser_job_id, browser_session_generation, browser_shutdown_generation FROM runtime_control WHERE id = 1"
    ).fetchone()
    conn.close()
    assert row["browser_active"] == 0
    assert row["browser_is_closed"] == 1
    assert row["browser_job_id"] is None
    assert row["browser_session_generation"] == 5
    assert row["browser_shutdown_generation"] == 5


def test_runtime_daemon_reconcile_exception_still_publishes_shutdown_fence(
    tmp_path: Path,
):
    from job_applier.cli.runtime_daemon import RuntimeDaemon

    db_file = tmp_path / "daemon_reconcile_exception.db"
    init_db(db_file)
    conn = get_connection(db_file)
    with conn:
        conn.execute(
            "UPDATE runtime_control SET browser_active = 1, browser_is_closed = 0, browser_session_generation = 7, browser_shutdown_generation = 7 WHERE id = 1;"
        )
    conn.close()

    daemon = RuntimeDaemon(custom_db_path=db_file)
    daemon._worker_id = "worker-failing-reconcile"
    daemon.__dict__["_reconcile_inflight_worker"] = MagicMock(
        side_effect=RuntimeError("reconciliation failed")
    )
    with (
        patch.object(daemon.outbound_proxy, "stop"),
        patch.object(daemon.profile_lock, "release"),
        patch.object(daemon.singleton_lock, "release"),
    ):
        daemon.shutdown()

    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT browser_active, browser_is_closed, browser_session_generation, browser_shutdown_generation FROM runtime_control WHERE id = 1"
    ).fetchone()
    conn.close()
    assert row["browser_active"] == 0
    assert row["browser_is_closed"] == 1
    assert row["browser_session_generation"] == 8
    assert row["browser_shutdown_generation"] == 8


def test_runtime_daemon_reconciles_job_before_display_teardown(tmp_path: Path):
    from job_applier.cli.runtime_daemon import RuntimeDaemon

    db_file = tmp_path / "shutdown.db"
    init_db(db_file)
    upsert_application(
        app_id="app-shutdown",
        company="ShutdownCo",
        title="Engineer",
        job_url="https://example.com/job",
        custom_path=db_file,
    )
    upsert_application(
        app_id="app-submit-shutdown",
        company="SubmitShutdownCo",
        title="Engineer",
        job_url="https://example.com/submit-job",
        custom_path=db_file,
    )
    conn = get_connection(db_file)
    with conn:
        conn.execute(
            """
            INSERT INTO automation_jobs (
                id, app_id, adapter, adapter_version, state, priority,
                lease_owner, fencing_generation, lease_expires_at, created_at, updated_at
            ) VALUES ('job-shutdown', 'app-shutdown', 'generic', '1.0.0',
                      'claimed', 1, 'runtime-test', 1, datetime('now', '+1 minute'),
                      datetime('now'), datetime('now'));
            """
        )
        conn.execute(
            """
            INSERT INTO automation_jobs (
                id, app_id, adapter, adapter_version, state, priority,
                lease_owner, fencing_generation, lease_expires_at, created_at, updated_at
            ) VALUES ('job-submit-shutdown', 'app-submit-shutdown', 'generic', '1.0.0',
                      'submit_intent', 1, 'runtime-test', 1, datetime('now', '+1 minute'),
                      datetime('now'), datetime('now'));
            """
        )
    conn.close()

    daemon = RuntimeDaemon(
        custom_db_path=db_file,
        worker_shutdown_timeout=1.0,
    )

    def fake_worker(**kwargs):
        conn = get_connection(db_file)
        with conn:
            conn.execute(
                "UPDATE automation_jobs SET lease_owner = ? WHERE lease_owner = 'runtime-test';",
                (kwargs["worker_id"],),
            )
        conn.close()
        kwargs["stop_event"].wait(timeout=2.0)
        return 0

    def interrupt() -> None:
        time.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)

    interrupter = threading.Thread(target=interrupt)
    with (
        patch.object(daemon.singleton_lock, "acquire", return_value=True),
        patch.object(daemon.profile_lock, "acquire"),
        patch.object(daemon, "start_display_subsystem"),
        patch.object(daemon.outbound_proxy, "start"),
        patch.object(daemon.outbound_proxy, "stop"),
        patch(
            "job_applier.cli.runtime_daemon.run_worker_loop", side_effect=fake_worker
        ),
    ):
        interrupter.start()
        try:
            assert daemon.run() == 0
        finally:
            interrupter.join()

    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT state, lease_owner, checkpoint, error_code FROM automation_jobs WHERE id = 'job-shutdown';"
    ).fetchone()
    submit_row = conn.execute(
        "SELECT state, lease_owner, checkpoint, error_code FROM automation_jobs WHERE id = 'job-submit-shutdown';"
    ).fetchone()
    app_row = conn.execute(
        "SELECT status FROM applications WHERE id = 'app-submit-shutdown';"
    ).fetchone()
    conn.close()
    assert row["state"] == "ready"
    assert row["lease_owner"] is None
    assert row["checkpoint"] == "runtime_shutdown"
    assert row["error_code"] == "RUNTIME_SHUTDOWN"
    assert submit_row["state"] == "ambiguous_submission"
    assert submit_row["lease_owner"] is None
    assert submit_row["error_code"] == "SHUTDOWN_DURING_SUBMISSION"
    assert app_row["status"] == "ambiguous"


def test_runtime_shutdown_preserves_ambiguous_submission(tmp_path: Path):
    from job_applier.cli.runtime_daemon import RuntimeDaemon

    db_file = tmp_path / "ambiguous-shutdown.db"
    init_db(db_file)
    upsert_application(
        app_id="app-ambiguous-shutdown",
        company="AmbiguousCo",
        title="Engineer",
        job_url="https://ambiguous.example/job",
        custom_path=db_file,
    )
    conn = get_connection(db_file)
    with conn:
        conn.execute(
            """
            INSERT INTO automation_jobs (
                id, app_id, adapter, adapter_version, state, priority,
                lease_owner, fencing_generation, lease_expires_at, created_at, updated_at
            ) VALUES ('job-ambiguous-shutdown', 'app-ambiguous-shutdown', 'generic', '1.0.0',
                      'ambiguous_submission', 1, 'runtime-test', 4, datetime('now', '+1 minute'),
                      datetime('now'), datetime('now'));
            """
        )
    conn.close()

    daemon = RuntimeDaemon(custom_db_path=db_file)
    assert daemon._reconcile_inflight_worker("runtime-test") == [
        "job-ambiguous-shutdown"
    ]

    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT state, lease_owner, error_code FROM automation_jobs WHERE id = ?",
        ("job-ambiguous-shutdown",),
    ).fetchone()
    conn.close()
    assert row["state"] == "ambiguous_submission"
    assert row["lease_owner"] is None
    assert row["error_code"] == "RUNTIME_SHUTDOWN"


def test_runtime_daemon_runs_sync_worker_outside_main_thread():
    from job_applier.cli.runtime_daemon import RuntimeDaemon

    daemon = RuntimeDaemon()
    worker_threads: list[threading.Thread] = []

    def fake_worker(**kwargs):
        worker_threads.append(threading.current_thread())
        return 7

    with (
        patch.object(daemon.singleton_lock, "acquire", return_value=True),
        patch.object(daemon.profile_lock, "acquire"),
        patch.object(daemon, "start_display_subsystem"),
        patch.object(daemon.outbound_proxy, "start"),
        patch.object(daemon, "shutdown"),
        patch(
            "job_applier.cli.runtime_daemon.run_worker_loop", side_effect=fake_worker
        ),
    ):
        assert daemon.run() == 7

    assert worker_threads
    assert worker_threads[0] is not threading.main_thread()


def test_runtime_ipc_and_active_job_challenge_states(tmp_path: Path):
    from job_applier.automation.queue import (
        consume_pending_verification_code,
        get_active_job,
        get_runtime_browser_state,
        get_runtime_control,
        set_pending_verification_code,
        set_runtime_browser_state,
    )

    db_file = tmp_path / "ipc_test.db"
    init_db(db_file)

    # Test verification code IPC
    set_pending_verification_code("987654", custom_path=db_file)
    ctrl = get_runtime_control(custom_path=db_file)
    assert ctrl["pending_verification_code"] == "987654"
    assert ctrl["is_waiting_for_code"] is False

    code = consume_pending_verification_code(custom_path=db_file)
    assert code == "987654"
    ctrl2 = get_runtime_control(custom_path=db_file)
    assert ctrl2["pending_verification_code"] is None

    # Test browser state IPC
    set_runtime_browser_state(
        active=True,
        url="https://boards.greenhouse.io/corp/jobs/123",
        page_text="Application form",
        is_closed=False,
        is_waiting_for_code=True,
        custom_path=db_file,
    )
    b_state = get_runtime_browser_state(custom_path=db_file)
    assert b_state["browser_active"] == 1
    assert b_state["browser_url"] == "https://boards.greenhouse.io/corp/jobs/123"
    assert b_state["browser_is_closed"] == 0
    assert b_state["is_waiting_for_code"] == 1

    # Test active job includes challenge states
    upsert_application(
        app_id="app_chal",
        company="Corp",
        title="Engineer",
        job_url="https://boards.greenhouse.io/corp/jobs/123",
        custom_path=db_file,
    )
    enqueue_job(
        app_id="app_chal",
        adapter="greenhouse",
        verify_artifacts=False,
        custom_path=db_file,
    )
    job = claim_next_job(worker_id="w1", custom_path=db_file)
    assert job is not None

    transition_job(
        job_id=job.id,
        worker_id="w1",
        generation=job.fencing_generation,
        to_state=JobState.CAPTCHA_REQUIRED.value,
        custom_path=db_file,
    )
    active = get_active_job(custom_path=db_file)
    assert active is not None
    assert active["id"] == job.id
    assert active["state"] == "captcha_required"


def test_safe_resume_durable_browser_state(tmp_path: Path):
    from job_applier.automation.queue import (
        claim_manual_takeover,
        set_runtime_browser_state,
    )
    from job_applier.automation.safe_resume import safe_resume_revalidate

    db_file = tmp_path / "safe_resume_ipc.db"
    init_db(db_file)

    upsert_application(
        app_id="app_resume",
        company="ResumeCorp",
        title="Backend Dev",
        job_url="https://boards.greenhouse.io/resumecorp/jobs/555",
        custom_path=db_file,
    )
    enqueue_job(
        app_id="app_resume",
        adapter="greenhouse",
        verify_artifacts=False,
        custom_path=db_file,
    )
    job = claim_next_job(worker_id="w1", custom_path=db_file)
    assert job is not None

    transition_job(
        job_id=job.id,
        worker_id="w1",
        generation=job.fencing_generation,
        to_state=JobState.CAPTCHA_REQUIRED.value,
        custom_path=db_file,
    )
    claim_manual_takeover(owner="operator1", lease_seconds=300, custom_path=db_file)

    # 1. Automator is None and browser is closed -> should reject safely
    set_runtime_browser_state(active=False, is_closed=True, custom_path=db_file)
    rej = safe_resume_revalidate(automator=None, custom_path=db_file)
    assert rej["status"] == "rejected"
    assert rej["reason"] == "browser_unavailable"

    # 2. Browser active with domain mismatch -> should reject
    set_runtime_browser_state(
        active=True,
        url="https://malicious-site.com/evil",
        page_text="Not the right page",
        is_closed=False,
        custom_path=db_file,
    )
    rej2 = safe_resume_revalidate(automator=None, custom_path=db_file)
    assert rej2["status"] == "rejected"

    # 3. Browser active with confirmation -> should mark applied
    set_runtime_browser_state(
        active=True,
        url="https://boards.greenhouse.io/resumecorp/jobs/555/confirmation",
        page_text="Thank you for applying to ResumeCorp!",
        is_closed=False,
        custom_path=db_file,
    )
    res = safe_resume_revalidate(automator=None, custom_path=db_file)
    assert res["status"] == "success"
    assert res["action"] == "marked_applied"


def test_safe_resume_uses_active_browser_owner_over_newer_auth_job(tmp_path: Path):
    """Browser ownership prevents Safe Resume from selecting another auth job by recency."""
    db_file = tmp_path / "browser_owner_resume.db"
    init_db(db_file)
    upsert_application(
        app_id="app-bestjobs-owner",
        company="BestJobs Corp",
        title="Product Owner",
        job_url="https://www.bestjobs.eu/loc-de-munca/product-owner",
        custom_path=db_file,
    )
    upsert_application(
        app_id="app-linkedin-newer",
        company="LinkedIn Corp",
        title="Platform Engineer",
        job_url="https://www.linkedin.com/jobs/view/999",
        custom_path=db_file,
    )
    bestjobs = enqueue_job("app-bestjobs-owner", adapter="generic", custom_path=db_file)
    linkedin = enqueue_job(
        "app-linkedin-newer", adapter="linkedin", custom_path=db_file
    )
    bestjobs_claimed = claim_next_job("bestjobs-worker", custom_path=db_file)
    linkedin_claimed = claim_next_job("linkedin-worker", custom_path=db_file)
    assert bestjobs_claimed and linkedin_claimed
    assert transition_job(
        bestjobs.id,
        "bestjobs-worker",
        bestjobs_claimed.fencing_generation,
        to_state=JobState.AUTH_REQUIRED.value,
        custom_path=db_file,
    )
    assert transition_job(
        linkedin.id,
        "linkedin-worker",
        linkedin_claimed.fencing_generation,
        to_state=JobState.AUTH_REQUIRED.value,
        custom_path=db_file,
    )
    set_runtime_pause(True, custom_path=db_file)
    set_runtime_browser_state(
        active=True,
        url="https://www.bestjobs.eu/loc-de-munca/product-owner",
        is_closed=False,
        browser_job_id=bestjobs.id,
        browser_session_generation=bestjobs_claimed.fencing_generation,
        custom_path=db_file,
    )
    browser_owner = get_browser_owner_job(custom_path=db_file)
    assert browser_owner is not None
    assert browser_owner["id"] == bestjobs.id

    resumed = safe_resume_revalidate(custom_path=db_file)

    assert resumed["status"] == "success"
    assert resumed["action"] == "resumed"
    conn = get_connection(db_file)
    states = {
        row["id"]: row["state"]
        for row in conn.execute(
            "SELECT id, state FROM automation_jobs WHERE id IN (?, ?);",
            (bestjobs.id, linkedin.id),
        ).fetchall()
    }
    conn.close()
    assert states[bestjobs.id] == JobState.READY.value
    assert states[linkedin.id] == JobState.AUTH_REQUIRED.value


def test_reopen_auth_session_requeues_closed_browser_job(tmp_path: Path):
    """A closed auth session is retried through the worker, not safe-resumed blindly."""
    db_file = tmp_path / "reopen_auth.db"
    init_db(db_file)
    upsert_application(
        app_id="app-auth-reopen",
        company="Auth Corp",
        title="Engineer",
        job_url="https://www.linkedin.com/jobs/view/123",
        custom_path=db_file,
    )
    job = enqueue_job("app-auth-reopen", adapter="linkedin", custom_path=db_file)
    claimed = claim_next_job("worker-1", custom_path=db_file)
    assert claimed is not None
    assert transition_job(
        claimed.id,
        "worker-1",
        claimed.fencing_generation,
        to_state=JobState.AUTH_REQUIRED.value,
        custom_path=db_file,
    )
    set_runtime_pause(True, custom_path=db_file)
    set_runtime_stop(True, custom_path=db_file)
    set_runtime_browser_state(
        active=True,
        is_closed=False,
        browser_job_id=job.id,
        browser_session_generation=claimed.fencing_generation,
        custom_path=db_file,
    )

    blocked = requeue_auth_required_job(job_id=job.id, custom_path=db_file)
    assert blocked["reason"] == "emergency_stop_active"
    set_runtime_stop(False, custom_path=db_file)
    result = requeue_auth_required_job(job_id=job.id, custom_path=db_file)

    # Browser closure is performed by the runtime worker after observing the
    # durable generation transition, not by this web-process test.
    assert result["status"] == "started"
    assert result["action"] == "reopen_auth_session"
    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT state, lease_owner, error_code, checkpoint FROM automation_jobs WHERE id = ?",
        (job.id,),
    ).fetchone()
    conn.close()
    assert dict(row) == {
        "state": "ready",
        "lease_owner": None,
        "error_code": None,
        "checkpoint": "auth_retry_requested",
    }
    control = get_runtime_control(db_file)
    assert control["is_paused"] is False
    assert control["is_stopped"] is False
    assert control["browser_active"] is False
    assert control["browser_is_closed"] is True


def test_reopen_auth_waits_for_transport_fence_before_clearing_lease(tmp_path: Path):
    """Reopen-auth cannot commit its lease clear while relay transport is fenced."""
    db_file = tmp_path / "reopen_auth_race.db"
    init_db(db_file)
    upsert_application(
        app_id="app-auth-race",
        company="Auth Corp",
        title="Engineer",
        job_url="https://www.linkedin.com/jobs/view/race",
        custom_path=db_file,
    )
    job = enqueue_job("app-auth-race", adapter="linkedin", custom_path=db_file)
    claimed = claim_next_job("worker-race", custom_path=db_file)
    assert claimed is not None
    assert transition_job(
        claimed.id,
        "worker-race",
        claimed.fencing_generation,
        to_state=JobState.AUTH_REQUIRED.value,
        custom_path=db_file,
    )
    conn = get_connection(db_file)
    with conn:
        conn.execute(
            "UPDATE runtime_control SET manual_takeover_owner = 'owner@example.test', manual_takeover_expires_at = datetime('now', 'localtime', '+5 minutes'), is_paused = 1 WHERE id = 1;"
        )
    conn.close()

    from job_applier.automation.queue import TAKEOVER_TRANSPORT_LOCK

    started = threading.Event()
    finished = threading.Event()
    result: dict[str, Any] = {}

    def reopen() -> None:
        started.set()
        result.update(
            requeue_auth_required_job(
                job_id=job.id,
                custom_path=db_file,
                expected_owner="owner@example.test",
            )
        )
        finished.set()

    with TAKEOVER_TRANSPORT_LOCK:
        thread = threading.Thread(target=reopen)
        thread.start()
        assert started.wait(timeout=1)
        time.sleep(0.02)
        assert not finished.is_set()

    thread.join(timeout=2)
    assert finished.is_set()
    assert result["status"] == "started"
    control = get_runtime_control(db_file)
    assert control["manual_takeover_owner"] is None


def test_reopen_auth_without_job_id_uses_browser_owner_only(tmp_path: Path):
    """An omitted job ID must never select another auth-paused job by recency."""
    db_file = tmp_path / "reopen_auth_owner.db"
    init_db(db_file)
    for app_id, title in (
        ("app-auth-owner", "Owner Job"),
        ("app-auth-other", "Other Job"),
    ):
        upsert_application(
            app_id=app_id,
            company="Auth Corp",
            title=title,
            job_url=f"https://www.linkedin.com/jobs/{app_id}",
            custom_path=db_file,
        )

    owner_job = enqueue_job("app-auth-owner", adapter="linkedin", custom_path=db_file)
    other_job = enqueue_job("app-auth-other", adapter="linkedin", custom_path=db_file)
    owner_claim = claim_next_job("worker-owner", custom_path=db_file)
    other_claim = claim_next_job("worker-other", custom_path=db_file)
    assert owner_claim and other_claim
    assert transition_job(
        owner_job.id,
        "worker-owner",
        owner_claim.fencing_generation,
        to_state=JobState.AUTH_REQUIRED.value,
        custom_path=db_file,
    )
    assert transition_job(
        other_job.id,
        "worker-other",
        other_claim.fencing_generation,
        to_state=JobState.AUTH_REQUIRED.value,
        custom_path=db_file,
    )
    set_runtime_browser_state(
        active=True,
        url="https://www.linkedin.com/login",
        is_closed=False,
        browser_job_id=owner_job.id,
        custom_path=db_file,
    )

    result = requeue_auth_required_job(custom_path=db_file)

    assert result["status"] == "started"
    assert result["job_id"] == owner_job.id
    conn = get_connection(db_file)
    states = {
        row["id"]: row["state"]
        for row in conn.execute(
            "SELECT id, state FROM automation_jobs WHERE id IN (?, ?);",
            (owner_job.id, other_job.id),
        ).fetchall()
    }
    conn.close()
    assert states[owner_job.id] == JobState.READY.value
    assert states[other_job.id] == JobState.AUTH_REQUIRED.value


def test_reopen_auth_without_job_id_rejects_without_browser_owner(tmp_path: Path):
    """An omitted job ID is rejected when no durable browser owner exists."""
    db_file = tmp_path / "reopen_auth_no_owner.db"
    init_db(db_file)
    upsert_application(
        app_id="app-auth-no-owner",
        company="Auth Corp",
        title="Engineer",
        job_url="https://www.linkedin.com/jobs/view/no-owner",
        custom_path=db_file,
    )
    job = enqueue_job("app-auth-no-owner", adapter="linkedin", custom_path=db_file)
    claimed = claim_next_job("worker-1", custom_path=db_file)
    assert claimed is not None
    assert transition_job(
        job.id,
        "worker-1",
        claimed.fencing_generation,
        to_state=JobState.AUTH_REQUIRED.value,
        custom_path=db_file,
    )

    result = requeue_auth_required_job(custom_path=db_file)

    assert result["status"] == "rejected"
    assert result["reason"] == "auth_job_target_required"


def test_safe_resume_rejects_reclaimed_generation_before_unpause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from job_applier.automation.queue import (
        claim_manual_takeover,
        claim_next_job,
        enqueue_job,
        get_runtime_control,
        set_runtime_pause,
        set_runtime_browser_state,
    )
    from job_applier.automation.safe_resume import safe_resume_revalidate

    db_file = tmp_path / "safe_resume_reclaimed_generation.db"
    init_db(db_file)
    upsert_application(
        app_id="app-resume-reclaim",
        company="Resume Corp",
        title="Engineer",
        job_url="https://example.com/job",
        custom_path=db_file,
    )
    job = enqueue_job("app-resume-reclaim", custom_path=db_file)
    claimed = claim_next_job("old-worker", custom_path=db_file)
    assert claimed is not None
    assert transition_job(
        job.id,
        "old-worker",
        claimed.fencing_generation,
        to_state=JobState.AUTH_REQUIRED.value,
        custom_path=db_file,
    )
    set_runtime_browser_state(
        active=True,
        url="https://example.com/job",
        is_closed=False,
        browser_job_id=job.id,
        browser_session_generation=claimed.fencing_generation,
        custom_path=db_file,
    )
    set_runtime_pause(True, custom_path=db_file)
    assert (
        claim_manual_takeover("resume-owner", custom_path=db_file)["status"]
        == "success"
    )

    def reclaim_during_validation(_url: str):
        conn = get_connection(db_file)
        with conn:
            conn.execute(
                """
                UPDATE automation_jobs
                SET state = 'claimed', lease_owner = 'replacement-worker',
                    lease_expires_at = datetime('now', '+1 minute'),
                    fencing_generation = fencing_generation + 1
                WHERE id = ?;
                """,
                (job.id,),
            )
        conn.close()
        return True, ""

    monkeypatch.setattr(
        "job_applier.automation.safe_resume.validate_target_url",
        reclaim_during_validation,
    )
    result = safe_resume_revalidate(
        job_id=job.id,
        current_url="https://example.com/job",
        custom_path=db_file,
        expected_owner="resume-owner",
    )

    assert result["status"] == "rejected"
    assert result["reason"] == "lease_fenced_or_expired"
    control = get_runtime_control(db_file)
    assert control["is_paused"] == 1
    assert control["manual_takeover_owner"] == "resume-owner"
    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT state, lease_owner, fencing_generation FROM automation_jobs WHERE id = ?",
        (job.id,),
    ).fetchone()
    conn.close()
    assert row["state"] == JobState.CLAIMED.value
    assert row["lease_owner"] == "replacement-worker"
    assert row["fencing_generation"] == claimed.fencing_generation + 1


def test_safe_resume_tenant_validation(tmp_path: Path):
    from job_applier.automation.queue import claim_next_job, enqueue_job
    from job_applier.automation.safe_resume import safe_resume_revalidate
    from job_applier.db import init_db, upsert_application

    db_file = tmp_path / "test_tenant.db"
    init_db(db_file)
    upsert_application(
        app_id="app-gh-tenant",
        company="Acme Corp",
        title="Developer",
        job_url="https://boards.greenhouse.io/acme/jobs/123",
        custom_path=db_file,
    )
    enqueue_job("app-gh-tenant", adapter="greenhouse", custom_path=db_file)
    claimed = claim_next_job("w-tenant", custom_path=db_file)
    assert claimed is not None

    mock_automator = MagicMock()
    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    # Navigated to a completely different tenant on Greenhouse
    mock_page.url = "https://boards.greenhouse.io/differentcorp/jobs/999"
    mock_automator.page = mock_page

    with patch(
        "job_applier.automation.safe_resume.validate_target_url",
        return_value=(True, ""),
    ):
        res = safe_resume_revalidate(
            job_id=claimed.id,
            current_url=mock_page.url,
            automator=mock_automator,
            custom_path=db_file,
        )

    assert res["status"] == "rejected"
    assert res["reason"] == "tenant_mismatch"


def test_outbound_proxy_http_get_ssrf_blocked():
    from job_applier.automation.network_security import OutboundSecurityProxy
    import urllib.request
    import urllib.error

    proxy = OutboundSecurityProxy(host="127.0.0.1", port=8991)
    proxy.start()
    try:
        # Construct opener using proxy
        proxy_handler = urllib.request.ProxyHandler({"http": "http://127.0.0.1:8991"})
        opener = urllib.request.build_opener(proxy_handler)

        # Attempt SSRF to cloud metadata IP via proxy GET: must receive 403 Forbidden
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            opener.open("http://169.254.169.254/latest/meta-data", timeout=3)
        assert exc_info.value.code == 403
    finally:
        proxy.stop()


def test_ashby_tenant_validation_in_safe_resume(tmp_path: Path):
    db_file = tmp_path / "test_resume.db"
    init_db(db_file)
    upsert_application(
        app_id="ashby-app-1",
        company="Acme Corp",
        title="Software Engineer",
        job_url="https://jobs.ashbyhq.com/acme/job-123",
        custom_path=db_file,
    )
    job = enqueue_job("ashby-app-1", adapter="ashby", custom_path=db_file)
    claim_next_job("w1", custom_path=db_file)

    class MockAutomator:
        page = None

    # Resume when browser is at a different Ashby tenant must be rejected
    res = safe_resume_revalidate(
        job_id=job.id,
        current_url="https://jobs.ashbyhq.com/othercompany/job-456",
        custom_path=db_file,
        automator=MockAutomator(),
    )
    assert res["status"] == "rejected"
    assert res["reason"] == "tenant_mismatch"


def test_safe_resume_resets_in_progress_states(tmp_path: Path):
    db_file = tmp_path / "test_resume_states.db"
    init_db(db_file)
    upsert_application(
        app_id="app-nav",
        company="Acme",
        title="SE",
        job_url="https://example.com/job",
        custom_path=db_file,
    )
    job = enqueue_job("app-nav", adapter="generic", custom_path=db_file)
    claim_next_job("w1", custom_path=db_file)
    transition_job(job.id, "w1", 1, to_state="navigating", custom_path=db_file)

    # Resume job while navigating
    res = safe_resume_revalidate(
        job_id=job.id,
        current_url="https://example.com/job",
        custom_path=db_file,
    )
    assert res["status"] == "success"
    assert res["action"] == "resumed"

    # Verify state is reset to 'ready'
    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT state FROM automation_jobs WHERE id = ?;", (job.id,)
    ).fetchone()
    conn.close()
    assert row["state"] == "ready"


def test_singleton_lock_and_profile_lock_reentrancy(tmp_path: Path):
    """Verifies that RuntimeSingletonLock and ProfileOwnershipLock are reentrant within the same process."""
    lock_file = tmp_path / "worker.lock"
    profile_dir = tmp_path / "browser_profile"

    # 1. Test RuntimeSingletonLock mutual exclusion between independent instances
    lock1 = RuntimeSingletonLock(lock_path=lock_file)
    assert lock1.acquire() is True
    assert lock1.is_locked() is True

    lock2 = RuntimeSingletonLock(lock_path=lock_file)
    assert lock2.acquire() is False
    assert lock2.is_locked() is False

    lock1.release()
    assert lock1.is_locked() is False

    # 2. Test skip_lock on run_worker_loop eliminates self-contention when daemon holds lock
    held_lock = RuntimeSingletonLock(lock_path=lock_file)
    assert held_lock.acquire() is True
    try:
        # With skip_lock=True, run_worker_loop starts cleanly without lock failure
        res = run_worker_loop(
            max_jobs=0, custom_db_path=tmp_path / "test.db", skip_lock=True
        )
        assert res == 0
    finally:
        held_lock.release()

    # 3. Test skip_profile_lock on BrowserAutomator eliminates self-contention when daemon holds profile lock
    p_lock = ProfileOwnershipLock(profile_dir=profile_dir, owner_type="runtime")
    assert p_lock.acquire() is True
    try:
        automator = BrowserAutomator(
            headless=True,
            profile_dir=profile_dir,
            skip_profile_lock=True,
        )
        assert automator.profile_lock is None
    finally:
        p_lock.release()


def test_safe_resume_ignores_loose_body_text_without_confirmation_url(
    tmp_path: Path,
):
    db_file = tmp_path / "test_no_loose.db"
    init_db(db_file)

    conn = get_connection(db_file)
    with conn:
        conn.execute(
            """
            INSERT INTO applications (
                id, company, title, job_url, platform, status, folder_name, created_at, updated_at
            ) VALUES ('app_loose', 'Loose Corp', 'Dev', 'https://boards.greenhouse.io/loosecorp/jobs/100', 'Greenhouse', 'pending', 'Loose_0', '2026-09-06', '2026-09-06');
            """
        )
    conn.close()

    enqueue_job("app_loose", adapter="greenhouse", custom_path=db_file)
    claimed = claim_next_job(worker_id="test_worker", custom_path=db_file)
    assert claimed is not None

    mock_automator = MagicMock()
    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://boards.greenhouse.io/loosecorp/jobs/100"
    mock_page.locator.return_value.text_content.return_value = (
        "Equal Opportunity Employer. Thank you for applying to Loose Corp in advance."
    )
    mock_automator.page = mock_page

    with patch(
        "job_applier.automation.safe_resume.validate_target_url",
        return_value=(True, ""),
    ):
        res = safe_resume_revalidate(
            job_id=claimed.id,
            current_url=mock_page.url,
            automator=mock_automator,
            custom_path=db_file,
        )

    assert res["status"] == "success"
    assert res["action"] == "resumed"

    conn = get_connection(db_file)
    app_row = conn.execute(
        "SELECT status FROM applications WHERE id = 'app_loose';"
    ).fetchone()
    conn.close()
    assert app_row["status"] == "pending"


def test_worker_preserves_live_automator_for_paused_challenge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from job_applier.automation.queue import set_runtime_pause
    from job_applier.cli.worker import process_claimed_job

    db_file = tmp_path / "test_worker_close.db"
    init_db(db_file)

    upsert_application(
        app_id="app-close-1",
        company="CloseCo",
        title="Dev",
        job_url="https://example.com/job/1",
        folder_name="test_close_folder",
        custom_path=db_file,
    )
    enqueue_job("app-close-1", adapter="generic", custom_path=db_file)
    claimed = claim_next_job("w1", custom_path=db_file)
    assert claimed is not None

    mock_automator = MagicMock()
    live_page = MagicMock()
    live_page.is_closed.return_value = False
    mock_automator.context.pages = [live_page]
    mock_automator.page = live_page
    mock_profile = MagicMock()
    mock_profile.to_dict.return_value = {}
    cv_mock_path = tmp_path / "CV.pdf"
    cv_mock_path.write_bytes(b"%PDF-1.4 test")

    with (
        patch(
            "job_applier.cli.worker.validate_application_artifacts",
            return_value=(True, str(cv_mock_path)),
        ),
        patch(
            "job_applier.cli.worker.load_candidate_profile",
            return_value=mock_profile,
        ),
        patch(
            "job_applier.cli.worker.BrowserAutomator",
            return_value=mock_automator,
        ),
        patch(
            "job_applier.cli.worker.SubmissionSafetyGuard.validate_pacing_and_daily_limits"
        ),
    ):
        mock_automator.run_autonomous_apply.side_effect = CaptchaDetectedError(
            "Captcha detected"
        )

        def pause_during_takeover(*args, **kwargs):
            set_runtime_pause(True, custom_path=db_file)

        monkeypatch.setenv("JOB_APPLIER_RUNTIME_MODE", "service")
        with patch(
            "job_applier.cli.worker._coordinate_challenge_takeover",
            side_effect=pause_during_takeover,
        ) as coordinate_takeover:
            process_claimed_job(claimed, worker_id="w1", custom_db_path=db_file)

    mock_automator.close.assert_not_called()
    assert coordinate_takeover.call_args.kwargs["max_wait_seconds"] is None


def test_worker_preserves_browser_when_auth_reopen_navigation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from job_applier.cli.worker import process_claimed_job

    db_file = tmp_path / "test_auth_reopen_lifecycle.db"
    init_db(db_file)
    upsert_application(
        app_id="app-auth-reopen",
        company="BestJobs Co",
        title="Product Owner",
        job_url="https://www.bestjobs.eu/loc-de-munca/product-owner",
        folder_name="auth_reopen_folder",
        custom_path=db_file,
    )
    job = enqueue_job("app-auth-reopen", adapter="generic", custom_path=db_file)
    claimed = claim_next_job("w1", custom_path=db_file)
    assert claimed is not None
    conn = get_connection(db_file)
    conn.execute(
        "UPDATE automation_jobs SET checkpoint = 'auth_retry_requested' WHERE id = ?",
        (job.id,),
    )
    conn.commit()
    conn.close()
    claimed.checkpoint = "auth_retry_requested"

    cv_path = tmp_path / "CV.pdf"
    cv_path.write_bytes(b"%PDF-1.4 test")
    profile = MagicMock()
    profile.to_dict.return_value = {}
    live_page = MagicMock()
    live_page.is_closed.return_value = False
    live_page.url = "https://www.bestjobs.eu/login"
    automator = MagicMock()
    automator.context.pages = [live_page]
    automator.page = live_page
    automator.navigate_and_open_form.return_value = False

    monkeypatch.setenv("JOB_APPLIER_RUNTIME_MODE", "service")
    with (
        patch(
            "job_applier.cli.worker.validate_application_artifacts",
            return_value=(True, str(cv_path)),
        ),
        patch("job_applier.cli.worker.load_candidate_profile", return_value=profile),
        patch("job_applier.cli.worker.BrowserAutomator", return_value=automator),
        patch(
            "job_applier.cli.worker.SubmissionSafetyGuard.validate_pacing_and_daily_limits"
        ),
    ):
        result = process_claimed_job(claimed, worker_id="w1", custom_db_path=db_file)

    assert result == JobState.AUTH_REQUIRED.value
    automator.close.assert_not_called()
    control = get_runtime_control(db_file)
    assert control["is_paused"] is True
    assert control["browser_active"] is True
    assert control["browser_is_closed"] is False


def test_takeover_timeout_preserves_live_browser_state(tmp_path: Path):
    """A takeover timeout must not mark a still-open headed browser as closed."""
    from job_applier.cli.worker import _coordinate_challenge_takeover

    db_file = tmp_path / "takeover_timeout_state.db"
    init_db(db_file)
    set_runtime_pause(True, custom_path=db_file)

    page = MagicMock()
    page.is_closed.return_value = False
    page.url = "https://www.bestjobs.eu/login"
    page.locator.return_value.text_content.return_value = "Sign in"
    automator = MagicMock()
    automator.page = page
    automator.context.pages = [page]
    automator.waiting_for_code = False
    heartbeat = MagicMock()
    heartbeat.lost_lease = False

    _coordinate_challenge_takeover(
        automator=automator,
        job_id="job-timeout",
        worker_id="worker-1",
        heartbeat=heartbeat,
        custom_db_path=db_file,
        max_wait_seconds=-1,
    )

    state = get_runtime_control(custom_path=db_file)
    assert state["browser_active"] == 1
    assert state["browser_is_closed"] == 0
    assert state["browser_job_id"] == "job-timeout"
    assert state["browser_url"] == "https://www.bestjobs.eu/login"


def test_worker_service_mode_idles_on_stopped(tmp_path: Path, monkeypatch):
    """Verifies that in service mode, is_stopped causes worker to idle rather than terminate immediately."""
    from job_applier.cli.worker import run_worker_loop
    from job_applier.ops.emergency_stop import emergency_stop

    db_file = tmp_path / "service_stop.db"
    init_db(db_file)
    emergency_stop(reason="Test service stop", custom_db_path=db_file)

    monkeypatch.setenv("JOB_APPLIER_RUNTIME_MODE", "service")

    exit_code = run_worker_loop(
        poll_interval=0.01,
        max_idle_polls=3,
        custom_db_path=db_file,
        skip_lock=True,
    )
    assert exit_code == 0
