from job_applier.tracker import (  # type: ignore[import-not-found]
    export_tracker_csv,
    get_tracker_stats,
    record_application,
    remove_from_tracker,
    update_status,
)


def test_tracker_record_and_stats(tmp_path):
    tracker_csv = tmp_path / "test_tracker.csv"

    # 1. Record an initial application
    df = record_application(
        company="Adobe",
        title="Senior Software Engineer",
        job_url="https://adobe.wd5.myworkdayjobs.com/job/123",
        status="applied",
        platform="Workday",
        submission_type="assisted_browser",
        custom_path=tracker_csv,
    )

    assert len(df) == 1
    assert df.iloc[0]["company"] == "Adobe"
    assert df.iloc[0]["platform"] == "Workday"
    assert df.iloc[0]["status"] == "applied"

    # 2. Record another application
    record_application(
        company="Google",
        title="Cloud Architect",
        job_url="https://careers.google.com/jobs/456",
        status="auto_filled",
        platform="Generic",
        submission_type="pipeline_generated",
        custom_path=tracker_csv,
    )

    stats = get_tracker_stats(tracker_csv)
    assert stats["total_records"] == 2
    assert stats["applied"] == 1
    assert stats["auto_filled"] == 1
    assert stats["platforms"]["Workday"] == 1


def test_tracker_update_existing_url(tmp_path):
    tracker_csv = tmp_path / "test_tracker.csv"
    job_url = "https://ro.indeed.com/viewjob?jk=79705f9688a84f47"

    # Record first as auto_filled
    record_application(
        company="Accenture",
        title="Azure Data Engineer",
        job_url=job_url,
        status="auto_filled",
        platform="Indeed",
        custom_path=tracker_csv,
    )

    # Later update to applied
    df = record_application(
        company="Accenture",
        title="Azure Data Engineer",
        job_url=job_url,
        status="applied",
        platform="Indeed",
        notes="Applied via assisted browser",
        custom_path=tracker_csv,
    )

    assert len(df) == 1
    assert df.iloc[0]["status"] == "applied"
    assert df.iloc[0]["notes"] == "Applied via assisted browser"


def test_tracker_export_and_update(tmp_path):
    tracker_csv = tmp_path / "test_tracker.csv"
    job_url = "https://example.com/job-export-test"

    record_application(
        company="Datadog",
        title="Site Reliability Engineer",
        job_url=job_url,
        status="pending",
        custom_path=tracker_csv,
    )

    # Export CSV
    csv_out = export_tracker_csv(custom_path=tracker_csv)
    assert "Datadog" in csv_out
    assert "Site Reliability Engineer" in csv_out

    # Update status via update_status
    updated = update_status(
        job_url=job_url,
        status="applied",
        notes="Updated notes",
        custom_path=tracker_csv,
    )
    assert updated is True

    # Verify updated in export
    csv_updated = export_tracker_csv(custom_path=tracker_csv)
    assert "applied" in csv_updated

    # Remove/reset from tracker
    df_after_remove = remove_from_tracker(job_url=job_url, custom_path=tracker_csv)
    assert df_after_remove.iloc[0]["status"] == "pending"
