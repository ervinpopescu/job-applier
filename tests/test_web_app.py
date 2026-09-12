from pathlib import Path
from typing import Any
from job_applier.automation.queue import enqueue_job, init_db
from job_applier.db import upsert_application
import os
from job_applier.web.app import (
    PIPELINE_STATE,
    PipelineRunRequest,
    get_main_resume,
    get_main_resume_pdf,
    resume_automation_endpoint,
    trigger_pipeline,
)
from job_applier.automation.queue import set_runtime_pause
from job_applier.automation.queue import set_runtime_stop

from unittest.mock import patch

import pytest  # type: ignore[import-not-found]
from fastapi.testclient import TestClient

from job_applier.web.app import app  # type: ignore[import-not-found]


@pytest.fixture
def client():
    return TestClient(app)


def test_dashboard_html_is_not_cached(client):
    for path in ("/", "/index.html"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == (
            "no-store, no-cache, must-revalidate, max-age=0"
        )
        assert "<script" in response.text
        assert "cdn.tailwindcss.com" not in response.text


def test_fingerprinted_assets_are_immutable(client):
    dist = Path("frontend/dist/frontend/browser")
    js_asset = next(dist.glob("*-????????.js"), None)
    css_asset = next(dist.glob("*-????????.css"), None)
    if js_asset is None or css_asset is None:
        pytest.skip("production frontend assets are not built")

    # If frontend dist was generated after app import in CI, ensure the static mount is present
    from job_applier.web.app import CacheAwareStaticFiles, app

    has_mount = any(getattr(r, "name", "") == "angular_app" for r in app.routes)
    if not has_mount and (dist / "index.html").is_file():
        app.mount(
            "/",
            CacheAwareStaticFiles(directory=str(dist), html=True),
            name="angular_app",
        )

    for asset in (js_asset, css_asset):
        response = client.get(f"/{asset.name}")
        assert response.status_code == 200
        assert (
            response.headers["cache-control"] == "public, max-age=31536000, immutable"
        )


def test_dashboard_home(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "JobApplier" in response.text


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "job-applier"}


def test_application_details_use_persisted_collision_folder_for_hipo_queue_row(
    client, tmp_path, monkeypatch
):
    """A queue row's ID may omit the suffix used by its generated artifact folder."""
    import importlib

    from job_applier import db as db_module

    app_module = importlib.import_module("job_applier.web.app")
    db_file = tmp_path / "hipo.db"
    output_dir = tmp_path / "applications"
    output_dir.mkdir()
    monkeypatch.setenv("JOB_APPLIER_DB_PATH", str(db_file))
    monkeypatch.setattr(app_module, "output_apps_dir", output_dir)
    db_module.init_db(db_file)

    app_id = "Hipo_Employer_Software_Requirements_Engineer"
    job_url = "https://www.hipo.ro/locuri-de-munca/locuri_de_munca/262082/"
    folder_name = f"{app_id}_112"
    db_module.upsert_application(
        app_id,
        "Hipo Employer",
        "Software Requirements Engineer",
        job_url,
        platform="Hipo",
        folder_name=folder_name,
        cv_filename="CV_Hipo_Employer_Software_Requirements_Engineer.pdf",
        custom_path=db_file,
    )
    enqueue_job(app_id, adapter="hipo", custom_path=db_file)

    app_folder = output_dir / folder_name
    app_folder.mkdir()
    (app_folder / "APPLY_HERE.txt").write_text(job_url, encoding="utf-8")
    (app_folder / "CV_Hipo_Employer_Software_Requirements_Engineer.pdf").write_bytes(
        b"pdf"
    )
    (app_folder / "autofill_bookmarklet.txt").write_text(
        "javascript:void(0)", encoding="utf-8"
    )

    response = client.get(f"/api/applications/{app_id}")

    assert response.status_code == 200
    assert response.json()["id"] == app_id
    assert response.json()["job_url"] == job_url


def test_dismiss_application_persists_and_removes_queue_card(
    client, tmp_path, monkeypatch
):
    import importlib

    from job_applier import db as db_module

    app_module = importlib.import_module("job_applier.web.app")
    db_file = tmp_path / "dismiss.db"
    output_dir = tmp_path / "applications"
    output_dir.mkdir()
    monkeypatch.setenv("JOB_APPLIER_DB_PATH", str(db_file))
    monkeypatch.setattr(app_module, "output_apps_dir", output_dir)
    db_module.init_db(db_file)
    db_module.upsert_application(
        "JTI_MDM_SOLUTIONS_MANAGER_16",
        "JTI",
        "MDM Solutions Manager",
        "https://example.com/jti",
        custom_path=db_file,
    )
    enqueue_job("JTI_MDM_SOLUTIONS_MANAGER_16", custom_path=db_file)
    (output_dir / "JTI_MDM_SOLUTIONS_MANAGER_16").mkdir()

    response = client.delete("/api/applications/JTI_MDM_SOLUTIONS_MANAGER_16")

    assert response.status_code == 200
    assert "dismissed" in response.json()["message"].lower()
    assert not (output_dir / "JTI_MDM_SOLUTIONS_MANAGER_16").exists()
    queue = db_module.get_applications(queue_only=True, custom_path=db_file)
    inventory = db_module.get_applications(queue_only=False, custom_path=db_file)
    assert queue["total"] == 0
    assert inventory["items"][0]["status"] == "dismissed"


def test_main_resume_viewer_generates_missing_pdf_without_exposing_extra_fields(
    tmp_path, monkeypatch
):
    import importlib
    from fastapi import BackgroundTasks

    web_app_module = importlib.import_module("job_applier.web.app")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "master_resume.json").write_text(
        '{"contact":{"name":"Synthetic Candidate","email":"candidate@example.test"},'
        '"summary":"Synthetic summary","experience":[],"skills":["Python"],'
        '"education":{},"projects":[],"candidate_profile_secret":"must not leak"}',
        encoding="utf-8",
    )
    pdf_path = tmp_path / "output" / "main_resume.pdf"
    monkeypatch.setattr(web_app_module, "project_root", tmp_path)
    monkeypatch.setattr(web_app_module, "main_resume_pdf", pdf_path)
    web_app_module.resume_generation_state.update(
        {"status": "idle", "generation_id": None, "error": None, "updated_at": None}
    )

    first = get_main_resume(BackgroundTasks())
    assert first["status"] == "generating"
    assert first["artifact_url"] is None
    assert first["resume"]["contact"]["name"] == "Synthetic Candidate"
    assert "candidate_profile_secret" not in first["resume"]

    tasks = BackgroundTasks()
    second = get_main_resume(tasks)
    assert second["status"] == "generating"
    assert second["generation_id"] == first["generation_id"]
    assert len(tasks.tasks) == 0

    tasks = BackgroundTasks()
    # The first request's task is not directly exposed by the response; schedule and run one explicitly.
    web_app_module.resume_generation_state.update(
        {"status": "idle", "generation_id": None}
    )
    scheduled = get_main_resume(tasks)
    assert scheduled["status"] == "generating"
    import asyncio

    asyncio.run(tasks())
    ready = get_main_resume(BackgroundTasks())
    assert ready["status"] == "ready"
    assert ready["artifact_url"] == "/api/resume/main.pdf"
    assert ready["download_url"] == "/api/resume/main.pdf?download=true"
    assert pdf_path.is_file()

    pdf_response = get_main_resume_pdf()
    assert pdf_response.media_type == "application/pdf"
    assert (
        pdf_response.headers.get("content-disposition")
        == 'inline; filename="main-resume.pdf"'
    )

    download_response = get_main_resume_pdf(download=True)
    assert download_response.media_type == "application/pdf"
    assert (
        download_response.headers.get("content-disposition")
        == 'attachment; filename="main-resume.pdf"'
    )


def test_main_resume_pdf_endpoint_content_disposition(tmp_path, monkeypatch, client):
    import importlib

    web_app_module = importlib.import_module("job_applier.web.app")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "master_resume.json").write_text(
        '{"contact":{"name":"Synthetic Candidate"},"summary":"test","experience":[],"skills":[],"education":{},"projects":[]}',
        encoding="utf-8",
    )
    pdf_path = tmp_path / "output" / "main_resume.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4 synthetic")
    monkeypatch.setattr(web_app_module, "project_root", tmp_path)
    monkeypatch.setattr(web_app_module, "main_resume_pdf", pdf_path)

    # Preview endpoint must return inline disposition
    preview_res = client.get("/api/resume/main.pdf")
    assert preview_res.status_code == 200
    assert preview_res.headers.get("content-type") == "application/pdf"
    assert (
        preview_res.headers.get("content-disposition")
        == 'inline; filename="main-resume.pdf"'
    )

    # Explicit download query must return attachment disposition
    download_res = client.get("/api/resume/main.pdf?download=true")
    assert download_res.status_code == 200
    assert download_res.headers.get("content-type") == "application/pdf"
    assert (
        download_res.headers.get("content-disposition")
        == 'attachment; filename="main-resume.pdf"'
    )


def test_pipeline_missing_api_key_does_not_leave_pipeline_running(monkeypatch):
    """A rejected pipeline request must not reserve the global pipeline slot."""
    import importlib

    from fastapi import BackgroundTasks, HTTPException

    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    web_app_module = importlib.import_module("job_applier.web.app")
    monkeypatch.setattr(web_app_module, "load_dotenv", lambda *_args, **_kwargs: None)

    original_state = PIPELINE_STATE.copy()
    PIPELINE_STATE.update(
        {
            "is_running": False,
            "status": "idle",
            "started_at": "",
            "logs": [],
            "error": "",
        }
    )
    try:
        with pytest.raises(HTTPException) as exc_info:
            trigger_pipeline(PipelineRunRequest(), BackgroundTasks())

        assert exc_info.value.status_code == 400
        assert PIPELINE_STATE["is_running"] is False
        assert PIPELINE_STATE["status"] == "idle"
    finally:
        PIPELINE_STATE.clear()
        PIPELINE_STATE.update(original_state)


def test_queue_applications_exclude_legacy_inventory(client):
    init_db()
    upsert_application(
        "queue-backed",
        "Queue Corp",
        "Engineer",
        "https://example.com/queue-backed",
    )
    upsert_application(
        "legacy-only",
        "Legacy Corp",
        "Engineer",
        "https://example.com/legacy-only",
    )
    enqueue_job("queue-backed")

    queue_response = client.get("/api/applications?limit=10")
    assert queue_response.status_code == 200
    queue_data = queue_response.json()
    assert queue_data["scope"] == "queue"
    assert queue_data["total"] == 1
    assert [item["id"] for item in queue_data["items"]] == ["queue-backed"]

    inventory_response = client.get("/api/applications?queue_only=false&limit=10")
    assert inventory_response.status_code == 200
    inventory_data = inventory_response.json()
    assert inventory_data["scope"] == "legacy_inventory"
    assert inventory_data["total"] == 2


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


def test_tracker_export_csv_endpoint(client, tmp_path, monkeypatch):
    test_db = tmp_path / "test_export.db"
    monkeypatch.setenv("JOB_APPLIER_DB_PATH", str(test_db))
    from job_applier.db import init_db, upsert_application

    init_db(test_db)
    upsert_application(
        app_id="export_test_app",
        company="Export Acme Inc",
        title="Senior CSV Specialist",
        job_url="https://example.com/export-test",
        status="pending",
        custom_path=test_db,
    )

    response = client.get("/api/tracker/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="applications_tracker.csv"' in response.headers.get(
        "content-disposition", ""
    )
    assert "company" in response.text
    assert "job_url" in response.text
    assert "Export Acme Inc" in response.text
    assert "Senior CSV Specialist" in response.text


def test_applications_unified_filters(client, tmp_path, monkeypatch):
    test_db = tmp_path / "test_filters.db"
    monkeypatch.setenv("JOB_APPLIER_DB_PATH", str(test_db))
    from job_applier.automation.queue import enqueue_job
    from job_applier.db import init_db, upsert_application

    init_db(test_db)
    upsert_application(
        app_id="filter_test_pending",
        company="Pending Co",
        title="Frontend Dev",
        job_url="https://example.com/filter-pending",
        status="pending",
        custom_path=test_db,
    )
    upsert_application(
        app_id="filter_test_applied",
        company="Applied Co",
        title="Backend Dev",
        job_url="https://example.com/filter-applied",
        status="applied",
        custom_path=test_db,
    )
    enqueue_job("filter_test_pending", custom_path=test_db)

    # Test filter=all
    res_all = client.get("/api/applications?filter=all")
    assert res_all.status_code == 200
    data_all = res_all.json()
    assert data_all["total"] == 2
    assert "counts" in data_all
    assert data_all["counts"]["all"] == 2
    assert data_all["counts"]["queued"] == 1
    assert data_all["counts"]["pending"] == 1
    assert data_all["counts"]["applied"] == 1
    assert data_all["counts"]["action_required"] == 0
    assert data_all["counts"]["skipped"] == 0
    assert data_all["counts"]["failed"] == 0

    # Test filter=queued
    res_queued = client.get("/api/applications?filter=queued")
    assert res_queued.status_code == 200
    data_queued = res_queued.json()
    assert data_queued["total"] == 1
    assert data_queued["items"][0]["id"] == "filter_test_pending"

    # Test filter=applied
    res_applied = client.get("/api/applications?filter=applied")
    assert res_applied.status_code == 200
    data_applied = res_applied.json()
    assert data_applied["total"] == 1
    assert data_applied["items"][0]["id"] == "filter_test_applied"


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


def test_requeue_endpoint_enqueues_and_syncs_sqlite(client, tmp_path, monkeypatch):
    test_db = tmp_path / "test_requeue.db"
    monkeypatch.setenv("JOB_APPLIER_DB_PATH", str(test_db))
    from job_applier.db import get_connection, init_db, upsert_application

    init_db(test_db)
    conn = get_connection(test_db)
    app_id = "test_requeue_app_1"
    upsert_application(
        app_id=app_id,
        company="Acme Requeue Corp",
        title="Software Engineer",
        job_url="https://example.com/job-to-requeue",
        status="applied",
        submission_type="assisted_browser",
        has_cover_letter=True,
        custom_path=test_db,
    )
    try:
        response = client.post(
            "/api/tracker/requeue",
            json={"job_url": "https://example.com/job-to-requeue"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"

        # Verify SQLite application status is pending and metadata preserved
        row = conn.execute(
            "SELECT status, submission_type, has_cover_letter FROM applications WHERE id = ?;",
            (app_id,),
        ).fetchone()
        assert row["status"] == "pending"
        assert row["submission_type"] == "assisted_browser"
        assert row["has_cover_letter"] == 1

        # Verify automation_jobs has a ready job for this app_id
        job_row = conn.execute(
            "SELECT state FROM automation_jobs WHERE app_id = ? ORDER BY created_at DESC LIMIT 1;",
            (app_id,),
        ).fetchone()
        assert job_row is not None
        assert job_row["state"] == "ready"
    finally:
        conn.close()


def test_update_tracker_job_status_preserves_metadata(client, tmp_path, monkeypatch):
    test_db = tmp_path / "test_status.db"
    monkeypatch.setenv("JOB_APPLIER_DB_PATH", str(test_db))
    from job_applier.db import get_connection, init_db, upsert_application

    init_db(test_db)
    app_id = "test_status_app_1"
    job_url = "https://example.com/status-update-job"
    upsert_application(
        app_id=app_id,
        company="Status Acme Corp",
        title="Staff Engineer",
        job_url=job_url,
        status="applied",
        submission_type="assisted_browser",
        has_cover_letter=True,
        custom_path=test_db,
    )

    response = client.post(
        "/api/tracker/update-status",
        json={
            "job_url": job_url,
            "status": "interviewing",
            "notes": "First round scheduled",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "success"

    conn = get_connection(test_db)
    try:
        row = conn.execute(
            "SELECT status, notes, submission_type, has_cover_letter FROM applications WHERE id = ?;",
            (app_id,),
        ).fetchone()
        assert row["status"] == "interviewing"
        assert row["notes"] == "First round scheduled"
        assert row["submission_type"] == "assisted_browser"
        assert row["has_cover_letter"] == 1
    finally:
        conn.close()


def test_batch_requeue_endpoint(client):
    from job_applier.db import get_connection, init_db, upsert_application

    init_db()
    conn = get_connection()
    app_1 = "batch_app_1"
    app_2 = "batch_app_2"
    upsert_application(
        app_id=app_1,
        company="Batch Co 1",
        title="Backend Dev",
        job_url="https://example.com/batch-1",
        status="failed",
    )
    upsert_application(
        app_id=app_2,
        company="Batch Co 2",
        title="Frontend Dev",
        job_url="https://example.com/batch-2",
        status="skipped",
    )
    with (
        patch("job_applier.tracker.record_application"),
        patch("job_applier.web.app.record_application"),
    ):
        response = client.post(
            "/api/tracker/batch-requeue",
            json={
                "items": [
                    {"job_url": "https://example.com/batch-1"},
                    {"job_url": "https://example.com/batch-2"},
                ]
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["count"] == 2
        assert len(data["enqueued"]) == 2

        # Verify both applications are pending
        for a_id in (app_1, app_2):
            row = conn.execute(
                "SELECT status FROM applications WHERE id = ?;", (a_id,)
            ).fetchone()
            assert row["status"] == "pending"
            job_row = conn.execute(
                "SELECT state FROM automation_jobs WHERE app_id = ? ORDER BY created_at DESC LIMIT 1;",
                (a_id,),
            ).fetchone()
            assert job_row is not None
            assert job_row["state"] == "ready"


def test_get_db_stats_active_jobs():
    from job_applier.db import get_connection, get_db_stats, init_db, upsert_application

    init_db()
    for app_id in ("app_s1", "app_s2", "app_s3"):
        upsert_application(
            app_id=app_id,
            company="Stats Corp",
            title="Engineer",
            job_url=f"https://example.com/{app_id}",
            status="pending",
        )
    conn = get_connection()
    now = "2026-09-12 12:00:00"
    with conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO automation_jobs (
                id, app_id, adapter, adapter_version, state, priority,
                fencing_generation, attempt_count, max_retries, is_cancelled,
                created_at, updated_at
            ) VALUES
                ('job_stat_1', 'app_s1', 'generic', '1.0', 'ready', 0, 0, 0, 3, 0, ?, ?),
                ('job_stat_2', 'app_s2', 'generic', '1.0', 'claimed', 0, 0, 0, 3, 0, ?, ?),
                ('job_stat_3', 'app_s3', 'generic', '1.0', 'skipped', 0, 0, 0, 3, 0, ?, ?);
            """,
            (now, now, now, now, now, now),
        )

    stats = get_db_stats()
    assert "queue" in stats
    assert "active_jobs" in stats["queue"]
    assert stats["queue"]["active_jobs"] >= 2


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
    assert len(res.content) > 500


def test_auth_status_with_browser_query(client):
    res_ff = client.get("/api/auth/status?browser=firefox")
    assert res_ff.status_code == 200
    data_ff = res_ff.json()
    assert data_ff["browser"] == "firefox"
    assert ".browser_profile_firefox" in data_ff["profile_dir"]

    res_ch = client.get("/api/auth/status?browser=chrome")
    assert res_ch.status_code == 200
    data_ch = res_ch.json()
    assert data_ch["browser"] == "chrome"
    assert ".browser_profile" in data_ch["profile_dir"]


def test_auth_login_with_browser_selection(client):
    with patch(
        "job_applier.automation.auth_manager.AuthManager.launch_interactive_login",
        return_value={"status": "success"},
    ):
        res = client.post(
            "/api/auth/login",
            json={"platform": "linkedin", "browser": "firefox", "timeout": 30},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "started"
        assert data["platform"] == "linkedin"
        assert data["browser"] == "firefox"
        assert "Firefox" in data["message"]


def test_auth_sync_chrome_endpoint(client):
    with patch(
        "job_applier.automation.auth_manager.AuthManager.sync_desktop_cookies",
        return_value={"status": "success", "cookies_merged": 5},
    ):
        res = client.post("/api/auth/sync-chrome")
        assert res.status_code == 200
        assert res.json()["cookies_merged"] == 5


def test_apply_returns_202_and_enqueues_job(client, tmp_path):
    from job_applier.db import upsert_application

    upsert_application(
        app_id="web-app-apply-1",
        company="FastCorp",
        title="Python Engineer",
        job_url="https://boards.greenhouse.io/fastcorp/jobs/123",
    )

    res = client.post(
        "/api/applications/web-app-apply-1/apply",
        json={"mode": "autonomous"},
    )
    assert res.status_code == 202
    data = res.json()
    assert data["status"] == "queued"
    assert data["job_id"].startswith("job_web-app-apply-1_")
    assert data["adapter"] == "greenhouse"
    assert data["state"] == "ready"


def test_batch_apply_returns_202_accepted(client, tmp_path):
    from job_applier.db import upsert_application

    upsert_application(
        app_id="web-batch-1",
        company="BatchCo 1",
        title="Engineer 1",
        job_url="https://jobs.lever.co/batchco1/abc",
    )
    upsert_application(
        app_id="web-batch-2",
        company="BatchCo 2",
        title="Engineer 2",
        job_url="https://jobs.lever.co/batchco2/def",
    )

    res = client.post(
        "/api/batch-apply",
        json={"app_ids": ["web-batch-1", "web-batch-2"], "mode": "autonomous"},
    )
    assert res.status_code == 202
    data = res.json()
    assert data["status"] == "queued"
    assert data["queued_count"] == 2
    assert len(data["job_ids"]) == 2


def test_runtime_controls_pause_resume_stop(client):
    # Pause
    res_pause = client.post("/api/automation/pause")
    assert res_pause.status_code == 200
    assert res_pause.json()["is_paused"] is True

    # Check status
    res_st = client.get("/api/automation/status")
    assert res_st.status_code == 200
    assert res_st.json()["is_paused"] is True
    assert "queue" in res_st.json()

    # Resume
    res_res = client.post("/api/automation/resume")
    assert res_res.status_code == 200
    assert res_res.json()["is_paused"] is False

    # Stop
    res_stop = client.post("/api/automation/stop")
    assert res_stop.status_code == 200
    assert res_stop.json()["is_stopped"] is True

    # Reset stop
    from job_applier.automation.queue import set_runtime_stop

    set_runtime_stop(False)


def test_clear_automation_state_endpoint(client):
    from job_applier.automation.queue import enqueue_job, set_runtime_stop
    from job_applier.db import get_connection, upsert_application

    # Setup an in-flight job and engage emergency stop
    upsert_application(
        app_id="stuck-app-1",
        company="StuckCo",
        title="Engineer",
        job_url="https://stuck.example",
    )
    job = enqueue_job("stuck-app-1")

    # Force job to claimed/navigating state
    conn = get_connection()
    with conn:
        conn.execute(
            "UPDATE automation_jobs SET state = 'navigating', lease_owner = 'worker_test' WHERE id = ?;",
            (job.id,),
        )
    conn.close()

    # Trigger emergency stop
    set_runtime_stop(True)

    # Call clear-state endpoint
    res = client.post("/api/automation/clear-state")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["is_paused"] is False
    assert data["is_stopped"] is False
    assert data["reset_jobs_count"] >= 1

    # Verify job reset to ready
    conn = get_connection()
    row = conn.execute(
        "SELECT state, lease_owner FROM automation_jobs WHERE id = ?;", (job.id,)
    ).fetchone()
    conn.close()
    assert row["state"] == "ready"
    assert row["lease_owner"] is None


def test_job_cancel_skip_resolve_endpoints(client):
    from job_applier.automation.queue import enqueue_job
    from job_applier.db import upsert_application

    upsert_application(
        app_id="web-cancel-app",
        company="CancelCo",
        title="Dev",
        job_url="https://cancel.example",
    )
    job = enqueue_job("web-cancel-app")

    # Cancel
    res_cancel = client.post(f"/api/automation/jobs/{job.id}/cancel")
    assert res_cancel.status_code == 200
    assert res_cancel.json()["status"] == "success"

    # Skip
    upsert_application(
        app_id="web-skip-app",
        company="SkipCo",
        title="Dev",
        job_url="https://skip.example",
    )
    job_skip = enqueue_job("web-skip-app")
    res_skip = client.post(f"/api/automation/jobs/{job_skip.id}/skip")
    assert res_skip.status_code == 200
    assert res_skip.json()["status"] == "success"

    # Resolve
    upsert_application(
        app_id="web-res-app",
        company="ResCo",
        title="Dev",
        job_url="https://res.example",
    )
    job_res = enqueue_job("web-res-app")
    res_resolve = client.post(
        f"/api/automation/jobs/{job_res.id}/resolve",
        json={"resolution_type": "continue"},
    )
    assert res_resolve.status_code == 200
    assert res_resolve.json()["status"] == "success"


@pytest.mark.anyio
async def test_stream_automation_events_initial_cursor_starts_at_latest(tmp_path: Path):
    from unittest.mock import AsyncMock, MagicMock
    from job_applier.automation.queue import record_event
    from job_applier.web.app import stream_automation_events

    db_file = tmp_path / "sse_cursor.db"
    init_db(db_file)

    # Seed 3 historical events
    for i in range(1, 4):
        record_event(
            job_id=f"job-{i}",
            app_id=f"app-{i}",
            event_type="test_event",
            level="INFO",
            step="test",
            message=f"Event {i}",
            custom_path=db_file,
        )

    req = MagicMock()
    req.headers = {}
    req.state = MagicMock()
    req.is_disconnected = AsyncMock(return_value=False)

    with patch.dict(os.environ, {"JOB_APPLIER_DB_PATH": str(db_file)}):
        # 1. Initial connection without cursor or replay: should send snapshot and NOT replay historical events
        resp = await stream_automation_events(req, after=None, replay=False)
        gen: Any = resp.body_iterator
        first = await gen.__anext__()
        assert "event: snapshot" in first
        assert "id: 3" in first

        # 2. Connection with explicit replay=true: should stream historical events after snapshot
        resp_replay = await stream_automation_events(req, after=None, replay=True)
        gen_replay: Any = resp_replay.body_iterator
        snapshot_msg = await gen_replay.__anext__()
        assert "event: snapshot" in snapshot_msg
        assert "id: 3" in snapshot_msg

        event_msg = await gen_replay.__anext__()
        assert "id: 1" in event_msg
        assert "event: test_event" in event_msg


def test_resume_endpoint_clears_is_stopped(tmp_path: Path):
    init_db(tmp_path / "job_applier.db")
    set_runtime_stop(True)
    set_runtime_pause(True)

    with patch(
        "job_applier.automation.safe_resume.safe_resume_revalidate",
        return_value={"status": "success", "action": "resumed"},
    ):
        res = resume_automation_endpoint()
        assert res["is_stopped"] is False
        assert res["is_paused"] is False


def test_get_automation_status_dynamic_hud_states(tmp_path: Path):
    from job_applier.automation.queue import (
        claim_next_job,
        enqueue_job,
        init_db,
        transition_job,
    )
    from job_applier.db import upsert_application
    from job_applier.web.app import get_automation_status

    db_file = tmp_path / "hud_test.db"
    init_db(db_file)

    upsert_application(
        app_id="app_hud_1",
        company="Acme Corp",
        title="Software Engineer",
        job_url="https://example.com/jobs/1",
        custom_path=db_file,
    )

    with patch.dict(os.environ, {"JOB_APPLIER_DB_PATH": str(db_file)}):
        # 1. Enqueued state
        job = enqueue_job("app_hud_1", custom_path=db_file)
        status = get_automation_status()
        assert status["progress_pct"] == 10
        assert status["step"] == "enqueued"
        assert "Acme Corp" in status["message"]
        assert status["is_active"] is True

        # 2. Claimed state
        claimed = claim_next_job("worker_test", custom_path=db_file)
        assert claimed is not None
        status = get_automation_status()
        assert status["progress_pct"] == 20
        assert status["step"] == "claimed"

        # 3. Navigating state
        transition_job(
            job.id,
            "worker_test",
            claimed.fencing_generation,
            "navigating",
            custom_path=db_file,
        )
        status = get_automation_status()
        assert status["progress_pct"] == 40
        assert status["step"] == "navigating"

        # 4. Filling state
        transition_job(
            job.id,
            "worker_test",
            claimed.fencing_generation,
            "filling",
            custom_path=db_file,
        )
        status = get_automation_status()
        assert status["progress_pct"] == 60
        assert status["step"] == "filling"

        # 5. Site changed / failed state
        transition_job(
            job.id,
            "worker_test",
            claimed.fencing_generation,
            "site_changed",
            error_code="SITE_CHANGED",
            error_message="Artifacts missing",
            custom_path=db_file,
        )
        status = get_automation_status()
        assert status["progress_pct"] == 0
        assert status["step"] == "failed"
        assert "Artifacts missing" in status["message"]
        assert status["is_active"] is False
