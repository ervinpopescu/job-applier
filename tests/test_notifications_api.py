import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch

from job_applier.automation.queue import (
    create_notification,
    get_runtime_control,
    set_runtime_pause,
)
from job_applier.db import init_db
from job_applier.web.app import app


@pytest.fixture
def client():
    init_db()
    with TestClient(app) as test_client:
        yield test_client


def test_get_notifications_empty(client):
    resp = client.get("/api/notifications")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["notifications"] == []
    assert data["unread_count"] == 0
    assert data["total"] == 0


def test_get_notifications_and_ack(client):
    # Create notifications in DB
    notif1 = create_notification(
        category="captcha_required",
        title="Action Required: CAPTCHA Challenge",
        message="Please complete CAPTCHA in browser.",
        severity="critical",
        job_id="job_api_1",
    )
    notif2 = create_notification(
        category="mfa_required",
        title="Action Required: Security Code",
        message="Please enter code.",
        severity="high",
        job_id="job_api_2",
    )

    # 1. Fetch notifications
    resp = client.get("/api/notifications")
    assert resp.status_code == 200
    data = resp.json()
    assert data["unread_count"] == 2
    assert data["total"] == 2
    assert len(data["notifications"]) == 2

    # 2. Fetch with after
    resp_after = client.get(f"/api/notifications?after={notif1['id']}")
    assert resp_after.status_code == 200
    data_after = resp_after.json()
    assert len(data_after["notifications"]) == 1
    assert data_after["notifications"][0]["id"] == notif2["id"]

    # 3. Pause automation
    set_runtime_pause(True)
    assert get_runtime_control()["is_paused"] is True

    # 4. Ack first notification
    ack_resp1 = client.post(f"/api/notifications/{notif1['notification_id']}/ack")
    assert ack_resp1.status_code == 200
    ack_data1 = ack_resp1.json()
    assert ack_data1["status"] == "success"
    assert ack_data1["acknowledged"] is True
    assert ack_data1["already_acknowledged"] is False

    # 5. Verify automation pause is UNCHANGED (ack does not resolve/resume)
    assert get_runtime_control()["is_paused"] is True

    # 6. Idempotent second ack
    ack_resp2 = client.post(f"/api/notifications/{notif1['notification_id']}/ack")
    assert ack_resp2.status_code == 200
    ack_data2 = ack_resp2.json()
    assert ack_data2["already_acknowledged"] is True

    # 7. Ack with numeric ID
    ack_resp3 = client.post(f"/api/notifications/{notif2['id']}/ack")
    assert ack_resp3.status_code == 200
    assert ack_resp3.json()["acknowledged"] is True

    # 8. Check unread count
    resp_unread = client.get("/api/notifications?unread_only=true")
    assert resp_unread.status_code == 200
    assert resp_unread.json()["unread_count"] == 0
    assert len(resp_unread.json()["notifications"]) == 0


def test_ack_notification_not_found(client):
    resp = client.post("/api/notifications/nonexistent_id/ack")
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_ack_all_endpoint(client):
    create_notification(category="c1", title="T1", message="M1")
    create_notification(category="c2", title="T2", message="M2")

    resp = client.get("/api/notifications")
    assert resp.json()["unread_count"] == 2

    ack_all_resp = client.post("/api/notifications/ack-all", json={})
    assert ack_all_resp.status_code == 200
    assert ack_all_resp.json()["acknowledged_count"] == 2

    # Idempotent call returns 0
    ack_all_resp2 = client.post("/api/notifications/ack-all", json={})
    assert ack_all_resp2.status_code == 200
    assert ack_all_resp2.json()["acknowledged_count"] == 0


def test_dispatch_notifications_endpoint(client):
    with patch(
        "job_applier.automation.ntfy.process_outbox",
        return_value={"processed": 2, "delivered": 2, "failed": 0, "retrying": 0},
    ):
        resp = client.post("/api/notifications/dispatch")
        assert resp.status_code == 200
        assert resp.json()["counts"]["delivered"] == 2


def test_delete_notification_endpoint(client):
    notif = create_notification(category="c1", title="T1", message="M1")
    resp = client.get("/api/notifications")
    assert any(
        n["notification_id"] == notif["notification_id"]
        for n in resp.json()["notifications"]
    )

    del_resp = client.delete(f"/api/notifications/{notif['notification_id']}")
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "success"

    # Confirm it's gone
    resp_after = client.get("/api/notifications")
    assert not any(
        n["notification_id"] == notif["notification_id"]
        for n in resp_after.json()["notifications"]
    )

    # 404 on deleting non-existent
    del_resp_404 = client.delete("/api/notifications/non_existent_notif_xyz")
    assert del_resp_404.status_code == 404


def test_clear_all_notifications_endpoint(client):
    create_notification(category="c1", title="T1", message="M1")
    create_notification(category="c2", title="T2", message="M2")
    resp = client.get("/api/notifications")
    assert len(resp.json()["notifications"]) >= 2

    clear_resp = client.delete("/api/notifications")
    assert clear_resp.status_code == 200
    assert clear_resp.json()["status"] == "success"
    assert clear_resp.json()["cleared_count"] >= 2

    # Verify zero notifications remain
    resp_empty = client.get("/api/notifications")
    assert len(resp_empty.json()["notifications"]) == 0
    assert resp_empty.json()["unread_count"] == 0
