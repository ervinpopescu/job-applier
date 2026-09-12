"""Focused tests for durable automation status and redacted event history."""

from pathlib import Path

import pytest

from job_applier.automation.queue import (
    append_event_and_project,
    claim_next_job,
    create_attempt,
    enqueue_job,
    get_application_automation_events,
    get_application_automation_status,
    get_automation_funnel,
    record_submit_intent,
)
from job_applier.db import get_connection, init_db, upsert_application


def _job_db(tmp_path: Path):
    db = tmp_path / "observability.db"
    init_db(db)
    upsert_application(
        "app-obs",
        "Synthetic Corp",
        "Engineer",
        "https://example.com/jobs/1",
        custom_path=db,
    )
    return db


def test_migration_backfills_status_projection_and_records_redacted_idempotent_events(
    tmp_path: Path,
):
    db = _job_db(tmp_path)
    job = enqueue_job("app-obs", custom_path=db)
    claimed = claim_next_job("worker-a", custom_path=db)
    assert claimed is not None
    attempt = create_attempt(job.id, "app-obs", custom_path=db)

    first = append_event_and_project(
        job_id=job.id,
        app_id="app-obs",
        attempt_id=attempt.id,
        attempt_number=attempt.attempt_number,
        event_key="fill-started",
        worker_id="worker-a",
        lease_generation=claimed.fencing_generation,
        event_type="state_transition",
        step="filling",
        message="password=super-secret https://private.example/app",
        details={"password": "super-secret", "checkpoint": "form-loaded"},
        custom_path=db,
    )
    duplicate = append_event_and_project(
        job_id=job.id,
        app_id="app-obs",
        attempt_id=attempt.id,
        attempt_number=attempt.attempt_number,
        event_key="fill-started",
        worker_id="worker-a",
        lease_generation=claimed.fencing_generation,
        event_type="state_transition",
        step="filling",
        message="different message",
        custom_path=db,
    )
    assert first == duplicate

    events = get_application_automation_events("app-obs", custom_path=db)
    event = next(item for item in events if item["id"] == first)
    assert "super-secret" not in event["message"]
    assert "private.example" not in event["message"]
    assert event["details"] == {"checkpoint": "form-loaded"}
    status = get_application_automation_status("app-obs", custom_path=db)
    assert status[0]["last_event_id"] == first
    assert status[0]["attempt_id"] == attempt.id


def test_submit_intent_event_keeps_application_identity(tmp_path: Path):
    db = _job_db(tmp_path)
    job = enqueue_job("app-obs", custom_path=db)
    claimed = claim_next_job("worker-a", custom_path=db)
    assert claimed is not None
    attempt = create_attempt(job.id, "app-obs", custom_path=db)
    assert record_submit_intent(
        attempt.id,
        job.id,
        "worker-a",
        claimed.fencing_generation,
        custom_path=db,
    )
    events = get_application_automation_events("app-obs", custom_path=db)
    intent = next(
        event for event in events if event["event_type"] == "submit_intent_persisted"
    )
    assert intent["app_id"] == "app-obs"
    assert intent["attempt_id"] == attempt.id


def test_stale_fence_rejected_and_attempt_numbers_are_unique(tmp_path: Path):
    db = _job_db(tmp_path)
    job = enqueue_job("app-obs", custom_path=db)
    claimed = claim_next_job("worker-a", custom_path=db)
    assert claimed is not None
    first = create_attempt(job.id, "app-obs", custom_path=db)
    second = create_attempt(job.id, "app-obs", custom_path=db)
    assert (first.attempt_number, second.attempt_number) == (1, 2)
    with pytest.raises(PermissionError, match="stale"):
        append_event_and_project(
            job_id=job.id,
            app_id="app-obs",
            attempt_id=first.id,
            event_key="stale",
            worker_id="worker-a",
            lease_generation=claimed.fencing_generation + 1,
            event_type="state_transition",
            step="fill",
            custom_path=db,
        )


def test_funnel_separates_jobs_from_manual_applied_rows(tmp_path: Path):
    db = _job_db(tmp_path)
    enqueue_job("app-obs", custom_path=db)
    funnel = get_automation_funnel(custom_path=db)
    assert funnel["jobs_total"] == 1
    assert funnel["jobs_tracked"] == 1
    assert funnel["runnable_jobs"] == 1
    assert funnel["jobs_queued"] == 1
    assert funnel["jobs_with_attempt"] == 1
    assert funnel["verified_autonomous_submissions"] == 0
    assert funnel["manual_applied"] == 0
    assert funnel["generated_artifacts"] >= funnel["applied_artifacts"]
    assert funnel["unqueued_artifacts"] >= 0


def test_funnel_uses_distinct_current_jobs_and_historical_attempt_rows(
    tmp_path: Path,
):
    """Current-job cards must not be inflated by retries or historical outcomes."""
    db = _job_db(tmp_path)
    apps = ["app-site", "app-auth", "app-failed", "app-ready"]
    for app_id in apps:
        upsert_application(
            app_id,
            "Synthetic Corp",
            "Engineer",
            f"https://example.com/jobs/{app_id}",
            custom_path=db,
        )
    jobs = [enqueue_job(app_id, custom_path=db) for app_id in apps]

    def add_attempt(job_index: int, outcome: str):
        attempt = create_attempt(jobs[job_index].id, apps[job_index], custom_path=db)
        conn = get_connection(db)
        try:
            with conn:
                conn.execute(
                    "UPDATE application_attempts SET outcome = ? WHERE id = ?;",
                    (outcome, attempt.id),
                )
        finally:
            conn.close()

    # One site-changed job has both an auth retry and a site-changed retry.
    add_attempt(0, "auth_required")
    add_attempt(0, "site_changed")
    add_attempt(1, "auth_required")
    add_attempt(2, "failed_permanent")
    conn = get_connection(db)
    try:
        with conn:
            conn.executemany(
                "UPDATE automation_jobs SET state = ? WHERE id = ?;",
                [
                    ("site_changed", jobs[0].id),
                    ("auth_required", jobs[1].id),
                    ("failed_permanent", jobs[2].id),
                    ("ready", jobs[3].id),
                ],
            )
    finally:
        conn.close()

    funnel = get_automation_funnel(custom_path=db)
    assert funnel["jobs_queued"] == 4
    assert funnel["jobs_tracked"] == 4
    assert funnel["runnable_jobs"] == 1
    assert funnel["jobs_with_attempt"] == 4  # enqueue events count as job history
    assert funnel["current_site_changed_jobs"] == 1
    assert funnel["current_auth_blocked_jobs"] == 1
    assert funnel["historical_auth_blocked_attempts"] == 2
    assert funnel["historical_site_changed_attempts"] == 1
    assert funnel["total_attempts"] == 4
    assert funnel["jobs_attempted"] == 4  # deprecated alias, now distinct jobs
    assert funnel["auth_required"] == 2  # deprecated historical-row alias
    assert funnel["site_changed"] == 1  # deprecated historical-row alias


def test_v5_schema_contains_observability_columns(tmp_path: Path):
    db = _job_db(tmp_path)
    conn = get_connection(db)
    try:
        event_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(automation_events)")
        }
        attempt_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(application_attempts)")
        }
        assert {
            "attempt_id",
            "event_key",
            "worker_id",
            "lease_generation",
            "outcome_code",
        } <= event_columns
        assert {"started_at", "last_event_id", "redacted_error_code"} <= attempt_columns
        assert (
            conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            == 5
        )
    finally:
        conn.close()
