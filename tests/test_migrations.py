import sqlite3
from pathlib import Path

import pytest

from job_applier.db import (
    CURRENT_SCHEMA_VERSION,
    get_connection,
    get_schema_version,
    init_db,
    run_migrations,
)


def test_migrations_fresh_db(tmp_path: Path):
    db_file = tmp_path / "fresh_migration.db"
    assert not db_file.exists()

    # First migration run
    applied = run_migrations(custom_path=db_file)
    assert applied == 5
    assert get_schema_version(custom_path=db_file) == CURRENT_SCHEMA_VERSION

    # Idempotent second run
    applied_second = run_migrations(custom_path=db_file)
    assert applied_second == 0
    assert get_schema_version(custom_path=db_file) == CURRENT_SCHEMA_VERSION

    # Verify tables created
    conn = get_connection(custom_path=db_file)
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table';"
        ).fetchall()
    ]
    conn.close()

    expected_tables = {
        "schema_migrations",
        "applications",
        "processed_jobs",
        "automation_jobs",
        "application_attempts",
        "automation_events",
        "approved_answers",
        "runtime_control",
        "notification_outbox",
        "notifications",
        "adapter_controls",
        "adapter_canaries",
    }
    for t in expected_tables:
        assert t in tables, f"Expected table {t} missing from database"


def test_migrations_legacy_db_upgrade(tmp_path: Path):
    """Simulates upgrading an existing database that was created before versioned migrations."""
    db_file = tmp_path / "legacy.db"
    raw_conn = sqlite3.connect(str(db_file))
    raw_conn.execute(
        """
        CREATE TABLE applications (
            id TEXT PRIMARY KEY,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            job_url TEXT NOT NULL,
            platform TEXT NOT NULL DEFAULT 'Generic',
            status TEXT NOT NULL DEFAULT 'pending',
            submission_type TEXT NOT NULL DEFAULT 'manual',
            folder_name TEXT NOT NULL,
            cv_filename TEXT DEFAULT '',
            has_cover_letter INTEGER DEFAULT 0,
            proof_screenshot TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    raw_conn.commit()
    raw_conn.close()

    # Verify legacy version detected as 1
    assert get_schema_version(custom_path=db_file) == 1

    # Run migrations: should apply migrations 2 through 5
    applied = run_migrations(custom_path=db_file)
    assert applied == 4
    assert get_schema_version(custom_path=db_file) == CURRENT_SCHEMA_VERSION


def test_migrations_fail_closed_on_newer_version(tmp_path: Path):
    """Refuses startup if database schema version is newer than supported codebase."""
    db_file = tmp_path / "future.db"
    init_db(custom_path=db_file)

    # Artificially insert a future migration version into schema_migrations
    conn = get_connection(custom_path=db_file)
    with conn:
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (999, 'future_features', datetime('now'));"
        )
    conn.close()

    assert get_schema_version(custom_path=db_file) == 999

    with pytest.raises(RuntimeError, match="newer than supported code version"):
        run_migrations(custom_path=db_file)


def test_adapter_controls_and_canaries(tmp_path: Path):
    from job_applier.db import (
        get_adapter_control,
        get_all_adapter_controls,
        record_canary_approval,
        record_submission_timestamp,
        set_adapter_enabled,
    )

    db_file = tmp_path / "adapters.db"
    init_db(custom_path=db_file)

    controls = get_all_adapter_controls(custom_path=db_file)
    assert len(controls) == 4
    names = {c["adapter_name"] for c in controls}
    assert names == {"greenhouse", "lever", "ashby", "generic"}

    gh = get_adapter_control("greenhouse", custom_path=db_file)
    assert gh is not None
    assert gh["is_enabled"] == 0
    assert gh["confirmed_canary_count"] == 0

    # Enabling without 3 canaries fails
    with pytest.raises(ValueError, match="requires 3 approved confirmed canaries"):
        set_adapter_enabled("greenhouse", True, custom_path=db_file)

    # Generic adapter cannot be enabled or record canaries
    with pytest.raises(
        ValueError, match="Generic adapter cannot have canaries approved"
    ):
        record_canary_approval(
            "generic", "job-1", "app-1", "operator", "evidence", custom_path=db_file
        )
    with pytest.raises(ValueError, match="Generic adapter is fill-only"):
        set_adapter_enabled("generic", True, custom_path=db_file)

    # Record 3 canaries for greenhouse
    for i in range(3):
        record_canary_approval(
            "greenhouse",
            f"job-{i}",
            f"app-{i}",
            "operator@example.com",
            f"Confirmation proof {i}",
            custom_path=db_file,
        )

    gh_updated = get_adapter_control("greenhouse", custom_path=db_file)
    assert gh_updated is not None
    assert gh_updated["confirmed_canary_count"] == 3
    assert gh_updated["is_enabled"] == 0

    # Now enabling greenhouse succeeds
    assert set_adapter_enabled("greenhouse", True, custom_path=db_file) is True
    gh_enabled = get_adapter_control("greenhouse", custom_path=db_file)
    assert gh_enabled is not None
    assert gh_enabled["is_enabled"] == 1

    # Record submission timestamp
    record_submission_timestamp(
        "greenhouse", "2025-01-01 12:00:00", custom_path=db_file
    )
    gh_ts = get_adapter_control("greenhouse", custom_path=db_file)
    assert gh_ts is not None
    assert gh_ts["last_submission_at"] == "2025-01-01 12:00:00"
