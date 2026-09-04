from unittest.mock import patch

import pytest  # type: ignore[import-not-found]
from fastapi.testclient import TestClient

from job_applier.web.app import app  # type: ignore[import-not-found]


@pytest.fixture
def client():
    return TestClient(app)


def test_dashboard_home(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "JobApplier" in response.text


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "job-applier"}


def test_get_stats(client):
    response = client.get("/api/stats")
    assert response.status_code == 200
    data = response.json()
    assert "pending" in data
    assert "tracker" in data
    assert "pipeline" in data


def test_list_applications(client):
    response = client.get("/api/applications?limit=10")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "total" in data
    assert isinstance(data["items"], list)


def test_get_and_update_profile(client):
    from pathlib import Path

    real_profile = Path("data/candidate_profile.json")
    backup = real_profile.read_text(encoding="utf-8") if real_profile.exists() else None
    try:
        # Get profile
        res_get = client.get("/api/profile")
        assert res_get.status_code == 200
        profile = res_get.json()
        assert "full_name" in profile
        assert "email" in profile

        # Update profile (preserving other fields!)
        res_post = client.post(
            "/api/profile",
            json={"notice_period": "3 weeks"},
        )
        assert res_post.status_code == 200
        updated = res_post.json()
        assert updated["profile"]["notice_period"] == "3 weeks"
        assert updated["profile"]["full_name"] == profile["full_name"]
    finally:
        if backup is not None:
            real_profile.write_text(backup, encoding="utf-8")


def test_pipeline_status(client):
    response = client.get("/api/pipeline/status")
    assert response.status_code == 200
    data = response.json()
    assert "is_running" in data
    assert "status" in data
    assert "logs" in data


def test_tracker_endpoints(client):
    response = client.get("/api/tracker")
    assert response.status_code == 200
    data = response.json()
    assert "records" in data
    assert "stats" in data


def test_requeue_endpoint(client):
    with (
        patch("job_applier.tracker.record_application"),
        patch("job_applier.web.app.record_application"),
    ):
        response = client.post(
            "/api/tracker/requeue",
            json={"job_url": "https://example.com/test-job-url"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"


def test_cleanup_endpoint(client):
    response = client.post(
        "/api/applications/cleanup", json={"older_than_days": 30, "archive": False}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "removed_count" in data


def test_clear_failed_endpoint(client):
    response = client.post("/api/applications/clear-failed")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert "removed_count" in data
    assert "remaining_count" in data


def test_automation_status_and_diagnostics(client):
    # Test HUD status endpoint
    res_status = client.get("/api/automation/status")
    assert res_status.status_code == 200
    status_data = res_status.json()
    assert "is_active" in status_data
    assert "step" in status_data

    # Test diagnostics 404 for non-existent app
    res_diag = client.get("/api/applications/non_existent_12345/diagnostics")
    assert res_diag.status_code == 404


def test_export_endpoint(client):
    res = client.get("/api/export")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/zip"
    assert len(res.content) > 1000
