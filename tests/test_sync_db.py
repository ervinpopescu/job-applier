from job_applier.db import (  # type: ignore[import-not-found]
    delete_application_db,
    get_applications,
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


def test_export_and_import_bundle(tmp_path):
    bundle_path = tmp_path / "test_export.zip"
    exported = export_bundle(output_path=bundle_path)

    assert exported.exists()
    assert exported.stat().st_size > 500

    report = import_bundle(exported)
    assert report["status"] == "success"
    assert "manifest" in report
