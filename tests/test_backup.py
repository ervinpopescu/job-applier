from job_applier.ops.backup import AgeEncryptionError
from job_applier.ops.backup import create_backup
from job_applier.sync import import_bundle
import json
import os

import concurrent.futures
import sqlite3
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from job_applier.db import (
    backup_db,
    get_connection,
    init_db,
    upsert_application,
)
from job_applier.sync import export_bundle


def test_online_backup_db(tmp_path: Path):
    source_db = tmp_path / "live.db"
    init_db(custom_path=source_db)

    # Insert test application
    upsert_application(
        app_id="test-app-1",
        company="Acme Corp",
        title="Software Engineer",
        job_url="https://example.com/jobs/1",
        custom_path=source_db,
    )

    backup_target = tmp_path / "backups" / "snapshot.db"
    result_path = backup_db(target_path=backup_target, custom_path=source_db)

    assert result_path.is_file()
    assert result_path == backup_target

    # Verify backup is a valid SQLite DB with intact data
    conn = sqlite3.connect(str(result_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT company, title FROM applications WHERE id = 'test-app-1'"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["company"] == "Acme Corp"
    assert row["title"] == "Software Engineer"


def test_backup_db_wal_consistency(tmp_path: Path):
    """
    Verifies that backup_db captures uncheckpointed, active WAL transactions cleanly
    so that the destination database file is self-contained without needing the .db-wal file.
    """
    source_db = tmp_path / "wal_live.db"
    init_db(custom_path=source_db)

    # Open persistent WAL connection and perform rapid writes to ensure pages live in WAL
    conn = get_connection(custom_path=source_db)
    with conn:
        for i in range(10):
            conn.execute(
                """
                INSERT INTO applications (
                    id, company, title, job_url, status, folder_name, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', ?, datetime('now'), datetime('now'));
                """,
                (
                    f"wal-app-{i}",
                    f"Company {i}",
                    f"Title {i}",
                    f"https://example.com/{i}",
                    f"wal-app-{i}",
                ),
            )

    # Ensure WAL file actually exists and contains pages
    wal_file = Path(f"{source_db}-wal")
    assert wal_file.exists()
    assert wal_file.stat().st_size > 0

    # Take online backup
    target_backup = tmp_path / "wal_snapshot.db"
    backup_db(target_path=target_backup, custom_path=source_db)
    conn.close()

    # Read target_backup directly without any companion -wal file
    assert not Path(f"{target_backup}-wal").exists()
    bconn = sqlite3.connect(str(target_backup))
    bconn.row_factory = sqlite3.Row
    count = bconn.execute(
        "SELECT COUNT(*) FROM applications WHERE id LIKE 'wal-app-%';"
    ).fetchone()[0]
    bconn.close()

    assert count == 10


def test_export_bundle_db_contents_and_wal(tmp_path: Path):
    """
    Verifies that export_bundle captures WAL data and packs an exact, valid SQLite database
    whose contents can be fully queried from within the unzipped archive.
    """
    with patch("job_applier.sync.get_project_root", return_value=tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        live_db = data_dir / "job_applier.db"
        init_db(custom_path=live_db)

        # Write records into live WAL database
        for i in range(5):
            upsert_application(
                app_id=f"export-app-{i}",
                company=f"TechCorp {i}",
                title=f"Engineer {i}",
                job_url=f"https://techcorp.example/jobs/{i}",
                custom_path=live_db,
            )

        out_zip = tmp_path / "output" / "exports" / "test_backup_exact.zip"
        res = export_bundle(output_path=out_zip, custom_db_path=live_db)
        assert res.is_file()

        # Extract and verify the exact database contents from the archive
        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(res, "r") as zf:
            zf.extract("data/job_applier.db", path=extract_dir)

        archived_db = extract_dir / "data" / "job_applier.db"
        assert archived_db.is_file()

        conn = sqlite3.connect(str(archived_db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, company FROM applications WHERE id LIKE 'export-app-%' ORDER BY id ASC;"
        ).fetchall()
        conn.close()

        assert len(rows) == 5
        assert rows[0]["id"] == "export-app-0"
        assert rows[0]["company"] == "TechCorp 0"


def test_export_bundle_fails_closed_on_backup_error(tmp_path: Path):
    """
    Guarantees fail-closed behavior: If backup_db fails, export_bundle must raise an exception
    and must NOT produce a corrupted or partial output archive.
    """
    with patch("job_applier.sync.get_project_root", return_value=tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        live_db = data_dir / "job_applier.db"
        init_db(custom_path=live_db)

        out_zip = tmp_path / "output" / "exports" / "should_not_exist.zip"

        with patch(
            "job_applier.sync.backup_db",
            side_effect=sqlite3.OperationalError("Simulated disk error"),
        ):
            with pytest.raises(sqlite3.OperationalError, match="Simulated disk error"):
                export_bundle(output_path=out_zip, custom_db_path=live_db)

        # Output archive must NOT exist
        assert not out_zip.exists()
        # No temp files left in exports dir
        tmp_files = list((tmp_path / "output" / "exports").glob(".tmp_*"))
        assert len(tmp_files) == 0


def test_export_bundle_concurrent_no_collision(tmp_path: Path):
    """
    Verifies that concurrent export_bundle calls do not collide on temp files
    and both produce independent, valid archives.
    """
    with patch("job_applier.sync.get_project_root", return_value=tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        live_db = data_dir / "job_applier.db"
        init_db(custom_path=live_db)
        upsert_application(
            app_id="concurrent-app",
            company="MultiThread Corp",
            title="Concurreny Engineer",
            job_url="https://concurrent.example/job",
            custom_path=live_db,
        )

        out_zip1 = tmp_path / "output" / "exports" / "concurrent_1.zip"
        out_zip2 = tmp_path / "output" / "exports" / "concurrent_2.zip"

        def do_export(target_path: Path) -> Path:
            return export_bundle(output_path=target_path, custom_db_path=live_db)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut1 = executor.submit(do_export, out_zip1)
            fut2 = executor.submit(do_export, out_zip2)
            res1 = fut1.result()
            res2 = fut2.result()

        assert res1.is_file() and res2.is_file()
        assert res1 != res2
        assert zipfile.is_zipfile(res1) and zipfile.is_zipfile(res2)

        # Check for any leftover temp files
        leftover = list((tmp_path / "output" / "exports").glob(".tmp_*"))
        assert len(leftover) == 0


def test_export_bundle_respects_include_profile_false(tmp_path: Path):
    """
    Verifies that setting include_profile=False omits candidate_profile.json
    and master_resume.json from the generated bundle.
    """
    with patch("job_applier.sync.get_project_root", return_value=tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        live_db = data_dir / "job_applier.db"
        init_db(custom_path=live_db)

        (data_dir / "candidate_profile.json").write_text(
            '{"name": "Secret"}', encoding="utf-8"
        )
        (data_dir / "master_resume.json").write_text(
            '{"experience": "Secret"}', encoding="utf-8"
        )
        (data_dir / "applications_tracker.csv").write_text(
            "company,title\nAcme,Dev\n", encoding="utf-8"
        )

        out_zip = tmp_path / "output" / "exports" / "no_profile.zip"
        res = export_bundle(
            output_path=out_zip, include_profile=False, custom_db_path=live_db
        )
        assert res.is_file()

        with zipfile.ZipFile(res, "r") as zf:
            namelist = zf.namelist()
            assert "data/applications_tracker.csv" in namelist
            assert "data/candidate_profile.json" not in namelist
            assert "data/master_resume.json" not in namelist


def test_export_bundle_no_mutation_side_effects(tmp_path: Path):
    """
    Verifies that export_bundle is strictly read-only and does not mutate database records
    or disk applications during export.
    """
    with patch("job_applier.sync.get_project_root", return_value=tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        live_db = data_dir / "job_applier.db"
        init_db(custom_path=live_db)

        upsert_application(
            app_id="preserve-app",
            company="Immutability Corp",
            title="Guardian",
            job_url="https://immutable.example/job",
            status="applied",
            notes="Should never change",
            custom_path=live_db,
        )

        # Record mtime and row content
        conn = sqlite3.connect(str(live_db))
        conn.row_factory = sqlite3.Row
        before_row = dict(
            conn.execute(
                "SELECT * FROM applications WHERE id = 'preserve-app';"
            ).fetchone()
        )
        conn.close()

        out_zip = tmp_path / "output" / "exports" / "read_only_export.zip"
        export_bundle(output_path=out_zip, custom_db_path=live_db)

        conn = sqlite3.connect(str(live_db))
        conn.row_factory = sqlite3.Row
        after_row = dict(
            conn.execute(
                "SELECT * FROM applications WHERE id = 'preserve-app';"
            ).fetchone()
        )
        conn.close()

        assert before_row == after_row


def test_create_backup_pii_omission_and_encryption_requirement(
    tmp_path: Path,
):
    """Verifies that candidate_profile.json is omitted when include_profile=False, and service mode requires encryption."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "candidate_profile.json").write_text(json.dumps({"name": "Secret PII"}))
    (data_dir / "applications_tracker.csv").write_text("id,company,status\n")

    db_file = data_dir / "job_applier.db"
    init_db(db_file)

    # 1. include_profile=False omits candidate_profile.json
    with patch("job_applier.ops.backup.get_project_root", return_value=tmp_path):
        out_zip = create_backup(
            output_dir=tmp_path / "backups",
            include_profile=False,
            quiesce_worker=False,
            custom_db_path=db_file,
            allow_unencrypted=True,
        )
        assert out_zip.exists()
        import zipfile

        with zipfile.ZipFile(out_zip, "r") as zf:
            namelist = zf.namelist()
            assert "data/applications_tracker.csv" in namelist
            assert "data/candidate_profile.json" not in namelist

    # 2. Production service mode requires Age encryption unless allow_unencrypted=True
    with (
        patch("job_applier.ops.backup.get_project_root", return_value=tmp_path),
        patch.dict(os.environ, {"JOB_APPLIER_RUNTIME_MODE": "service"}),
        pytest.raises(AgeEncryptionError),
    ):
        create_backup(
            output_dir=tmp_path / "backups",
            quiesce_worker=False,
            custom_db_path=db_file,
            allow_unencrypted=False,
        )


def test_sync_import_no_limit_and_processed_jobs_merge(tmp_path: Path):
    with patch("job_applier.sync.get_project_root", return_value=tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        live_db = data_dir / "job_applier.db"
        init_db(custom_path=live_db)

        # Create export zip with 1050 applications and processed_jobs.txt
        zip_path = tmp_path / "test_large_bundle.zip"
        source_db_path = tmp_path / "source_temp.db"
        init_db(custom_path=source_db_path)

        for i in range(1050):
            upsert_application(
                app_id=f"app_{i:04d}",
                company=f"Company {i}",
                title=f"Engineer {i}",
                job_url=f"https://example.com/job/{i}",
                status="pending",
                custom_path=source_db_path,
            )

        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(source_db_path, arcname="data/job_applier.db")
            zf.writestr(
                "data/processed_jobs.txt",
                "https://example.com/job/1\nhttps://example.com/job/2\n",
            )
            zf.writestr(
                "manifest.json",
                json.dumps({"version": "1.0.0", "applications_count": 1050}),
            )

        report = import_bundle(zip_path)
        assert report["merged_db_records"] == 1050

        # Verify all 1050 records are present in live DB
        conn = sqlite3.connect(str(live_db))
        total_count = conn.execute("SELECT count(*) FROM applications;").fetchone()[0]
        conn.close()
        assert total_count == 1050

        # Verify processed_jobs.txt merged
        pj_file = tmp_path / "data" / "processed_jobs.txt"
        assert pj_file.exists()
        assert "https://example.com/job/1" in pj_file.read_text()


def test_sync_import_conservative_status_merge(tmp_path: Path):
    with patch("job_applier.sync.get_project_root", return_value=tmp_path):
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        live_db = data_dir / "job_applier.db"
        init_db(custom_path=live_db)

        # Local state: app-1 is applied, app-2 is ambiguous
        upsert_application(
            app_id="app-1",
            company="Acme",
            title="Dev",
            job_url="https://example.com/1",
            status="applied",
            custom_path=live_db,
        )
        upsert_application(
            app_id="app-2",
            company="Beta",
            title="Dev",
            job_url="https://example.com/2",
            status="ambiguous",
            custom_path=live_db,
        )

        # Incoming zip has both as 'pending' (older export)
        source_db_path = tmp_path / "source_db.db"
        init_db(custom_path=source_db_path)
        upsert_application(
            app_id="app-1",
            company="Acme",
            title="Dev",
            job_url="https://example.com/1",
            status="pending",
            custom_path=source_db_path,
        )
        upsert_application(
            app_id="app-2",
            company="Beta",
            title="Dev",
            job_url="https://example.com/2",
            status="pending",
            custom_path=source_db_path,
        )

        zip_path = tmp_path / "older_bundle.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(source_db_path, arcname="data/job_applier.db")

        import_bundle(zip_path)

        # Verify statuses were NOT downgraded to pending
        conn = sqlite3.connect(str(live_db))
        conn.row_factory = sqlite3.Row
        r1 = conn.execute(
            "SELECT status FROM applications WHERE id = 'app-1';"
        ).fetchone()
        r2 = conn.execute(
            "SELECT status FROM applications WHERE id = 'app-2';"
        ).fetchone()
        conn.close()

        assert r1["status"] == "applied"
        assert r2["status"] == "ambiguous"


def test_backup_omits_pii_when_include_profile_false(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    init_db(data_dir / "job_applier.db")

    # Create dummy candidate profile and CV PDF
    (data_dir / "candidate_profile.json").write_text('{"name": "Secret Person"}')

    app_dir = tmp_path / "output" / "applications" / "app_secret"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "CV_Secret.pdf").write_bytes(b"%PDF-1.4 dummy pdf content")
    (app_dir / "cover_letter.txt").write_text("Dear Hiring Manager, secret")
    (app_dir / "APPLY_HERE.txt").write_text("https://example.com/apply")

    backup_dir = tmp_path / "output" / "backups"
    with patch("job_applier.ops.backup.get_project_root", return_value=tmp_path):
        backup_zip = create_backup(
            output_dir=backup_dir,
            include_profile=False,
            allow_unencrypted=True,
            quiesce_worker=False,
        )

    # Inspect zip contents
    with zipfile.ZipFile(backup_zip, "r") as zf:
        names = zf.namelist()
        assert not any(n.endswith(".pdf") for n in names)
        assert not any("cover_letter" in n for n in names)
        assert not any("candidate_profile.json" in n for n in names)
        assert any("APPLY_HERE.txt" in n for n in names)
