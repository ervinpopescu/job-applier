from email.header import decode_header
from job_applier.automation.ntfy import dispatch_single_notification
import time

from pathlib import Path
from unittest.mock import MagicMock, patch


from job_applier.automation.ntfy import (
    map_category_to_tags,
    map_urgency_to_priority,
    process_outbox,
)
from job_applier.automation.queue import (
    ack_all_notifications,
    ack_notification,
    create_notification,
    enqueue_notification,
    get_events,
    get_notification_stats,
    get_notifications,
    get_pending_notifications,
    get_runtime_control,
    set_runtime_pause,
)
from job_applier.db import get_connection, init_db


def test_create_and_get_notifications(tmp_path: Path):
    db_file = tmp_path / "test_notif.db"
    init_db(custom_path=db_file)

    notif1 = create_notification(
        category="captcha_required",
        title="Action Required: CAPTCHA Challenge",
        message="CAPTCHA detected in browser.",
        severity="critical",
        job_id="job_1",
        app_id="app_1",
        custom_path=db_file,
    )

    notif2 = create_notification(
        category="unknown_question",
        title="Action Required: Novel Screening Question",
        message="Screening question requires answer.",
        severity="warning",
        job_id="job_2",
        app_id="app_2",
        custom_path=db_file,
    )

    assert notif1["id"] == 1
    assert notif2["id"] == 2
    assert not notif1["acknowledged"]
    assert not notif2["acknowledged"]

    # Retrieve all (ordered DESC)
    all_notifs = get_notifications(custom_path=db_file)
    assert len(all_notifs) == 2
    assert all_notifs[0]["id"] == 2
    assert all_notifs[1]["id"] == 1

    # Retrieve after numeric ID
    after_1 = get_notifications(after_id=1, custom_path=db_file)
    assert len(after_1) == 1
    assert after_1[0]["id"] == 2

    # Retrieve after string notification_id
    after_str = get_notifications(
        after_id=notif1["notification_id"], custom_path=db_file
    )
    assert len(after_str) == 1
    assert after_str[0]["id"] == 2

    # Stats
    stats = get_notification_stats(custom_path=db_file)
    assert stats["total"] == 2
    assert stats["unread_count"] == 2
    assert stats["latest_id"] == 2


def test_idempotent_ack_distinct_from_resolve_or_resume(tmp_path: Path):
    db_file = tmp_path / "test_ack.db"
    init_db(custom_path=db_file)

    # Pause runtime explicitly
    set_runtime_pause(True, custom_path=db_file)
    ctrl = get_runtime_control(custom_path=db_file)
    assert ctrl["is_paused"] is True

    notif = create_notification(
        category="mfa_required",
        title="Action Required: MFA",
        message="MFA code needed.",
        severity="high",
        job_id="job_mfa",
        custom_path=db_file,
    )
    nid = notif["notification_id"]

    # First ack
    res1 = ack_notification(nid, custom_path=db_file)
    assert res1["status"] == "success"
    assert res1["acknowledged"] is True
    assert res1["already_acknowledged"] is False
    assert res1["acknowledged_at"] is not None

    # Verify automation state is STRICTLY UNCHANGED (acknowledging does not resolve/resume)
    ctrl_after_ack = get_runtime_control(custom_path=db_file)
    assert ctrl_after_ack["is_paused"] is True

    # Second ack (idempotent)
    res2 = ack_notification(nid, custom_path=db_file)
    assert res2["status"] == "success"
    assert res2["acknowledged"] is True
    assert res2["already_acknowledged"] is True
    assert res2["acknowledged_at"] == res1["acknowledged_at"]

    # Third ack using numeric ID
    res3 = ack_notification(notif["id"], custom_path=db_file)
    assert res3["status"] == "success"
    assert res3["already_acknowledged"] is True

    # Notification stats show 0 unread
    stats = get_notification_stats(custom_path=db_file)
    assert stats["total"] == 1
    assert stats["unread_count"] == 0

    # Notification list with unread_only=True returns empty
    unread = get_notifications(unread_only=True, custom_path=db_file)
    assert len(unread) == 0


def test_ack_all_notifications(tmp_path: Path):
    db_file = tmp_path / "test_ack_all.db"
    init_db(custom_path=db_file)

    create_notification(category="cat1", title="T1", message="M1", custom_path=db_file)
    create_notification(category="cat2", title="T2", message="M2", custom_path=db_file)
    create_notification(category="cat3", title="T3", message="M3", custom_path=db_file)

    assert get_notification_stats(custom_path=db_file)["unread_count"] == 3

    # Ack up to id 2
    count = ack_all_notifications(up_to_id=2, custom_path=db_file)
    assert count == 2
    assert get_notification_stats(custom_path=db_file)["unread_count"] == 1

    # Ack remaining
    count_remaining = ack_all_notifications(custom_path=db_file)
    assert count_remaining == 1
    assert get_notification_stats(custom_path=db_file)["unread_count"] == 0

    # Idempotent call
    assert ack_all_notifications(custom_path=db_file) == 0


def test_enqueue_notification_dual_record(tmp_path: Path):
    """Verifies that enqueue_notification writes to both notification_outbox and notifications."""
    db_file = tmp_path / "test_dual.db"
    init_db(custom_path=db_file)

    notif_id = enqueue_notification(
        category="captcha_required",
        title="Action Required: CAPTCHA Challenge",
        message="CAPTCHA challenge detected.",
        urgency="high",
        job_id="job_captcha_99",
        custom_path=db_file,
    )

    # 1. Outbox check
    pending = get_pending_notifications(custom_path=db_file)
    assert len(pending) == 1
    assert pending[0]["id"] == notif_id
    assert pending[0]["category"] == "captcha_required"

    # 2. In-app notifications check
    in_app = get_notifications(custom_path=db_file)
    assert len(in_app) == 1
    assert in_app[0]["category"] == "captcha_required"
    assert in_app[0]["severity"] == "critical"  # mapped from urgency=high
    assert in_app[0]["job_id"] == "job_captcha_99"

    # 3. Events check: an event was recorded
    events = get_events(after_id=0, custom_path=db_file)
    assert len(events) >= 1
    notif_events = [e for e in events if e["event_type"] == "notification"]
    assert len(notif_events) == 1
    assert notif_events[0]["step"] == "alert"


def test_ntfy_mappings():
    assert map_urgency_to_priority("critical") == "urgent"
    assert map_urgency_to_priority("high") == "high"
    assert map_urgency_to_priority("normal") == "default"
    assert map_urgency_to_priority("low") == "low"

    assert "robot" in map_category_to_tags("captcha_required")
    assert "key" in map_category_to_tags("mfa_required")
    assert "grey_question" in map_category_to_tags("unknown_question")
    assert "rotating_light" in map_category_to_tags("ambiguous_submission")


def test_ntfy_dispatch_and_bounded_retry(tmp_path: Path):
    db_file = tmp_path / "test_ntfy_retry.db"
    init_db(custom_path=db_file)

    notif_id = enqueue_notification(
        category="mfa_required",
        title="Action Required: MFA",
        message="Security verification required.",
        urgency="high",
        job_id="job_xyz",
        custom_path=db_file,
    )

    cfg = {
        "url": "http://ntfy.test",
        "topic": "test-alerts",
        "token": "secret-token",
        "public_base_url": "https://jobs.aslan.net",
    }

    # Simulate network failure on first attempt
    with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
        counts = process_outbox(
            batch_size=10,
            max_retries=3,
            custom_path=db_file,
            config=cfg,
        )
        assert counts["processed"] == 1
        assert counts["retrying"] == 1
        assert counts["delivered"] == 0
        assert counts["failed"] == 0

    # Verify attempt_count incremented and still pending (querying all pending regardless of backoff delay)
    pending = get_pending_notifications(due_only=False, custom_path=db_file)
    assert len(pending) == 1
    assert pending[0]["attempt_count"] == 1
    assert "Connection refused" in pending[0]["last_error"]

    # Simulate 2 more failures to reach max_retries (3)
    conn = get_connection(db_file)
    with patch("urllib.request.urlopen", side_effect=Exception("Timeout")):
        # attempt 2: make due
        with conn:
            conn.execute(
                "UPDATE notification_outbox SET next_retry_at = '2020-01-01 00:00:00' WHERE id = ?;",
                (notif_id,),
            )
        process_outbox(batch_size=10, max_retries=3, custom_path=db_file, config=cfg)

        # attempt 3 -> should fail permanently: make due
        with conn:
            conn.execute(
                "UPDATE notification_outbox SET next_retry_at = '2020-01-01 00:00:00' WHERE id = ?;",
                (notif_id,),
            )
        process_outbox(batch_size=10, max_retries=3, custom_path=db_file, config=cfg)
    conn.close()

    # Now pending should be empty, item marked as failed
    pending_after = get_pending_notifications(due_only=False, custom_path=db_file)
    assert len(pending_after) == 0


def test_ntfy_dispatch_success(tmp_path: Path):
    db_file = tmp_path / "test_ntfy_success.db"
    init_db(custom_path=db_file)

    enqueue_notification(
        category="captcha_required",
        title="Action Required: CAPTCHA",
        message="Please complete CAPTCHA.",
        urgency="high",
        job_id="job_cap",
        custom_path=db_file,
    )

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        counts = process_outbox(
            batch_size=10,
            max_retries=3,
            custom_path=db_file,
            config={
                "url": "http://ntfy.test",
                "topic": "alerts",
                "token": "tok123",
                "public_base_url": "https://jobs.aslan.net",
            },
        )
        assert counts["delivered"] == 1
        assert counts["failed"] == 0

        # Verify request headers
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer tok123"
        assert req.get_header("Priority") == "high"
        assert "robot" in req.get_header("Tags")
        assert req.get_header("Click") == "https://jobs.aslan.net"

    # Outbox should have 0 pending
    assert len(get_pending_notifications(custom_path=db_file)) == 0


def test_outbox_auto_dispatch(tmp_path: Path, monkeypatch):
    """Verifies that enqueue_notification immediately triggers background outbox processing."""
    monkeypatch.setenv("JOB_APPLIER_ASYNC_OUTBOX_DISPATCH", "1")
    db_file = tmp_path / "outbox.db"
    init_db(db_file)

    with patch("job_applier.automation.ntfy.process_outbox") as mock_process:
        notif_id = enqueue_notification(
            category="test_alert",
            title="Immediate Alert",
            message="Alert message",
            urgency="high",
            custom_path=db_file,
        )
        assert notif_id.startswith("notif_")

        # Allow thread to start and call process_outbox
        time.sleep(0.1)
        mock_process.assert_called_once_with(custom_path=db_file)


def test_ntfy_unicode_title_rfc2047_encoding():
    """Verifies that non-ASCII Unicode in push notification titles is encoded using RFC 2047 without mojibake."""
    notification = {
        "title": "Aplicație nouă: inginer software șî țară 🚀",
        "message": "Automation alert",
        "urgency": "high",
        "category": "auth_challenge",
    }

    captured_headers: dict[str, str] = {}

    def mock_urlopen(req, timeout=10.0):
        nonlocal captured_headers
        captured_headers = req.headers
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None
        return mock_resp

    with patch("urllib.request.urlopen", side_effect=mock_urlopen):
        success, err = dispatch_single_notification(
            notification,
            config={
                "url": "http://ntfy.test",
                "topic": "alerts",
                "token": "tok123",
                "public_base_url": "https://jobs.aslan.net",
            },
        )
        assert success is True
        assert "Title" in captured_headers
        encoded_title = captured_headers["Title"]

        # Verify decode produces original unicode text
        decoded_parts = decode_header(encoded_title)
        reconstructed = "".join(
            part.decode(enc or "utf-8") if isinstance(part, bytes) else part
            for part, enc in decoded_parts
        )
        assert "Aplicație nouă: inginer software șî țară 🚀" in reconstructed


def test_ntfy_fallback_script_execution(tmp_path: Path):
    """Verifies that dispatch_single_notification falls back to host script if HTTP dispatch fails."""
    script_file = tmp_path / "notify-alert.sh"
    # Create mock executable script
    script_file.write_text(
        '#!/bin/sh\necho "Called with: $@" >> '
        + str(tmp_path / "script.log")
        + "\nexit 0\n"
    )
    script_file.chmod(0o755)

    notification = {
        "title": "Alert Title",
        "message": "Generic paused alert",
        "urgency": "high",
        "category": "mfa_required",
    }

    # Simulate HTTP network error so fallback script is triggered
    with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
        success, err = dispatch_single_notification(
            notification,
            config={
                "url": "http://ntfy.unreachable",
                "topic": "alerts",
                "token": "",
                "public_base_url": "https://jobs.aslan.net",
                "fallback_script": str(script_file),
            },
        )
        assert success is True
        assert err == ""

    log_content = (tmp_path / "script.log").read_text()
    assert "-t Alert Title" in log_content
    assert "-m Generic paused alert" in log_content
    assert "-p high" in log_content
