from job_applier.automation.queue import enqueue_job, get_automation_funnel, skip_job
from job_applier.db import (  # type: ignore[import-not-found]
    delete_application_db,
    dismiss_application_db,
    get_applications,
    get_connection,
    get_db_stats,
    init_db,
    update_status_by_url,
    upsert_application,
)
from job_applier.sync import (  # type: ignore[import-not-found]
    export_bundle,
    import_bundle,
)


def test_sqlite_db_lifecycle(tmp_path):
    db_file = tmp_path / "test_lifecycle.db"
    init_db(custom_path=db_file)

    # 1. Insert application
    upsert_application(
        app_id="TestCorp_Cloud_Architect_0",
        company="TestCorp",
        title="Cloud Architect",
        job_url="https://testcorp.com/jobs/1",
        platform="Greenhouse",
        status="pending",
        custom_path=db_file,
    )

    # 2. Query
    res = get_applications(status="pending", custom_path=db_file)
    assert res["total"] == 1
    assert res["items"][0]["company"] == "TestCorp"

    # 3. Update status
    updated = update_status_by_url(
        job_url="https://testcorp.com/jobs/1",
        status="applied",
        notes="Verified submission",
        custom_path=db_file,
    )
    assert updated is True

    # 4. Stats
    stats = get_db_stats(custom_path=db_file)
    assert stats["applied"] == 1

    # 5. Delete
    deleted = delete_application_db(
        app_id="TestCorp_Cloud_Architect_0", custom_path=db_file
    )
    assert deleted is True
    assert get_db_stats(custom_path=db_file)["total_records"] == 0


def test_queue_only_applications_exclude_legacy_rows_and_preserve_inventory(tmp_path):
    db_file = tmp_path / "queue_scope.db"
    init_db(custom_path=db_file)
    upsert_application(
        "queued-app",
        "Queued Corp",
        "Engineer",
        "https://example.com/queued",
        custom_path=db_file,
    )
    upsert_application(
        "legacy-app",
        "Legacy Corp",
        "Engineer",
        "https://example.com/legacy",
        custom_path=db_file,
    )
    enqueue_job("queued-app", custom_path=db_file)

    queue = get_applications(queue_only=True, custom_path=db_file)
    inventory = get_applications(queue_only=False, custom_path=db_file)
    assert queue["total"] == 1
    assert [item["id"] for item in queue["items"]] == ["queued-app"]
    assert inventory["total"] == 2


def test_dismissed_application_is_persisted_and_excluded_from_queue(tmp_path):
    db_file = tmp_path / "dismissed_queue.db"
    init_db(custom_path=db_file)
    upsert_application(
        "dismissed-app",
        "Dismissed Corp",
        "Engineer",
        "https://example.com/dismissed",
        custom_path=db_file,
    )
    enqueue_job("dismissed-app", custom_path=db_file)

    assert dismiss_application_db("dismissed-app", custom_path=db_file) is True
    queue = get_applications(queue_only=True, custom_path=db_file)
    inventory = get_applications(queue_only=False, custom_path=db_file)
    assert queue["total"] == 0
    assert inventory["items"][0]["status"] == "dismissed"


def test_skipped_application_is_removed_from_active_queue(tmp_path):
    db_file = tmp_path / "skipped_queue.db"
    init_db(custom_path=db_file)
    upsert_application(
        "skipped-app",
        "Skipped Corp",
        "Engineer",
        "https://example.com/skipped",
        custom_path=db_file,
    )
    enqueue_job("skipped-app", custom_path=db_file)

    assert skip_job("skipped-app", reason="Dismissed by operator", custom_path=db_file)
    queue = get_applications(queue_only=True, custom_path=db_file)
    history = get_applications(queue_only=False, custom_path=db_file)
    assert queue["total"] == 0
    assert history["items"][0]["status"] == "skipped"


def test_skipped_job_is_excluded_even_if_application_status_is_legacy(tmp_path):
    db_file = tmp_path / "skipped_legacy_queue.db"
    init_db(custom_path=db_file)
    upsert_application(
        "skipped-legacy-app",
        "Skipped Legacy Corp",
        "Engineer",
        "https://example.com/skipped-legacy",
        status="filled_only",
        custom_path=db_file,
    )
    enqueue_job("skipped-legacy-app", custom_path=db_file)
    conn = get_connection(db_file)
    try:
        with conn:
            conn.execute(
                "UPDATE automation_jobs SET state = 'skipped' WHERE app_id = ?;",
                ("skipped-legacy-app",),
            )
    finally:
        conn.close()

    assert get_applications(queue_only=True, custom_path=db_file)["total"] == 0


def test_dismissed_application_cannot_be_claimed(tmp_path):
    db_file = tmp_path / "dismissed_claim.db"
    init_db(custom_path=db_file)
    upsert_application(
        "dismissed-claim-app",
        "Dismissed Claim Corp",
        "Engineer",
        "https://example.com/dismissed-claim",
        custom_path=db_file,
    )
    enqueue_job("dismissed-claim-app", custom_path=db_file)
    assert dismiss_application_db("dismissed-claim-app", custom_path=db_file) is True

    from job_applier.automation.queue import claim_next_job

    assert claim_next_job("worker-1", custom_path=db_file) is None


def test_funnel_reports_zero_runnable_jobs_when_paused_or_terminal(tmp_path):
    db_file = tmp_path / "runnable_scope.db"
    init_db(custom_path=db_file)
    upsert_application(
        "terminal-app",
        "Terminal Corp",
        "Engineer",
        "https://example.com/terminal",
        custom_path=db_file,
    )
    enqueue_job("terminal-app", custom_path=db_file)
    conn = get_connection(db_file)
    try:
        with conn:
            conn.execute(
                "UPDATE automation_jobs SET state = 'failed_permanent' WHERE app_id = ?;",
                ("terminal-app",),
            )
            conn.execute("UPDATE runtime_control SET is_paused = 1 WHERE id = 1;")
    finally:
        conn.close()
    funnel = get_automation_funnel(custom_path=db_file)
    assert funnel["jobs_tracked"] == 1
    assert funnel["runnable_jobs"] == 0


def test_export_and_import_bundle(tmp_path):
    bundle_path = tmp_path / "test_export.zip"
    exported = export_bundle(output_path=bundle_path)

    assert exported.exists()
    assert exported.stat().st_size > 200

    report = import_bundle(exported)
    assert report["status"] == "success"
    assert "manifest" in report
