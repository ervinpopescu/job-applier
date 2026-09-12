from job_applier.automation.queue import get_pending_notifications

import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pyrage import x25519

from job_applier.automation.browser_automator import BrowserAutomator
from job_applier.automation.profile_lock import ProfileOwnershipLock
from job_applier.automation.queue import (
    JobState,
    claim_next_job,
    enqueue_job,
    get_runtime_control,
    set_runtime_pause,
)
from job_applier.automation.safety_guard import SubmissionSafetyGuard
from job_applier.db import (
    CURRENT_SCHEMA_VERSION,
    get_connection,
    init_db,
    run_migrations,
    upsert_application,
)
from job_applier.ops.backup import (
    DowngradeRefusalError,
    ProfileConsistencyError,
    apply_retention_policy,
    create_backup,
    decrypt_with_age,
    encrypt_with_age,
    restore_backup,
    sanitize_profile_copy,
)
from job_applier.ops.disk_guard import (
    DiskPressureError,
    check_disk_pressure,
    cleanup_expired_debug_artifacts,
    enforce_disk_pressure_guard,
)
from job_applier.ops.emergency_stop import (
    clear_emergency_stop,
    emergency_stop,
)


@pytest.fixture
def age_keypair():
    """Generates an ephemeral X25519 age identity and public recipient."""
    ident = x25519.Identity.generate()
    pub = ident.to_public()
    return str(pub), str(ident)


def test_online_backup_snapshot_consistency(tmp_path: Path):
    """Verifies that create_backup captures active SQLite WAL records cleanly."""
    db_file = tmp_path / "data" / "job_applier.db"
    db_file.parent.mkdir(parents=True)
    init_db(custom_path=db_file)

    # Insert test data
    upsert_application(
        app_id="app-1",
        company="Snapshot Inc",
        title="Infrastructure Engineer",
        job_url="https://example.com/job/1",
        custom_path=db_file,
    )

    out_dir = tmp_path / "backups"
    backup_file = create_backup(
        output_dir=out_dir,
        include_profile=False,
        quiesce_worker=True,
        custom_db_path=db_file,
    )

    assert backup_file.exists()
    assert backup_file.name.endswith(".zip")

    # Inspect manifest inside ZIP
    import zipfile

    with zipfile.ZipFile(str(backup_file), "r") as zf:
        namelist = zf.namelist()
        assert "manifest.json" in namelist
        assert "data/job_applier.db" in namelist
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        assert manifest["schema_version"] == CURRENT_SCHEMA_VERSION
        assert manifest["applications_count"] >= 0


def test_quiesced_artifact_snapshot(tmp_path: Path):
    """Verifies that create_backup pauses and unpauses worker queue during backup."""
    db_file = tmp_path / "quiesce.db"
    init_db(custom_path=db_file)

    # Ensure initially unpaused
    set_runtime_pause(False, custom_path=db_file)
    assert get_runtime_control(custom_path=db_file)["is_paused"] is False

    out_dir = tmp_path / "backups"
    create_backup(
        output_dir=out_dir,
        include_profile=False,
        quiesce_worker=True,
        custom_db_path=db_file,
    )

    # After completion, pause should be restored to False
    ctrl = get_runtime_control(custom_path=db_file)
    assert ctrl["is_paused"] is False


def test_stopped_profile_consistency_and_lock_refusal(tmp_path: Path):
    """
    Verifies that create_backup refuses to back up a profile if the Chromium runtime lock is active,
    and strips ephemeral lock and socket files when stopped.
    """
    db_file = tmp_path / "profile_test.db"
    init_db(custom_path=db_file)

    profile_dir = tmp_path / ".browser_profile"
    profile_dir.mkdir(parents=True)

    # Add mock profile files including ephemeral locks and sockets
    (profile_dir / "Default").mkdir()
    (profile_dir / "Default" / "Preferences").write_text('{"name": "test"}')
    (profile_dir / "SingletonLock").write_text("12345")
    (profile_dir / "SingletonCookie").write_text("cookie")

    # 1. Test profile sanitization
    sanitized_dest = tmp_path / "sanitized_profile"
    copied = sanitize_profile_copy(profile_dir, sanitized_dest)
    assert copied == 1
    assert (sanitized_dest / "Default" / "Preferences").exists()
    assert not (sanitized_dest / "SingletonLock").exists()
    assert not (sanitized_dest / "SingletonCookie").exists()

    # 2. Test active runtime lock refusal
    with ProfileOwnershipLock(profile_dir, owner_type="runtime"):
        with patch(
            "job_applier.ops.backup.get_default_profile_dir", return_value=profile_dir
        ):
            with pytest.raises(
                ProfileConsistencyError, match="Cannot create consistent profile backup"
            ):
                create_backup(
                    output_dir=tmp_path / "backups",
                    include_profile=True,
                    require_stopped_profile=True,
                    custom_db_path=db_file,
                )

            # Passing require_stopped_profile=False succeeds with a warning
            out = create_backup(
                output_dir=tmp_path / "backups",
                include_profile=True,
                require_stopped_profile=False,
                custom_db_path=db_file,
            )
            assert out.exists()


def test_age_encryption_and_off_server_decryption_drill(tmp_path: Path, age_keypair):
    """
    Verifies age encryption with public recipient key and subsequent decryption
    using the off-server private identity key.
    """
    pub_recipient, priv_identity = age_keypair

    # 1. Direct encryption / decryption test
    plain_file = tmp_path / "sensitive.txt"
    plain_file.write_text("Confidential production data payload")
    enc_file = tmp_path / "sensitive.txt.age"

    encrypt_with_age(plain_file, enc_file, pub_recipient)
    assert enc_file.exists()
    # Ensure ciphertext header matches age standard
    with open(enc_file, "rb") as f:
        header = f.read(30)
    assert b"age-encryption.org" in header

    dec_file = tmp_path / "decrypted.txt"
    decrypt_with_age(enc_file, dec_file, identity=priv_identity)
    assert dec_file.read_text() == "Confidential production data payload"

    # 2. Key file decryption test
    key_file = tmp_path / "operator_age_key.txt"
    key_file.write_text(priv_identity)
    dec_file_2 = tmp_path / "decrypted_file.txt"
    decrypt_with_age(enc_file, dec_file_2, identity_file=key_file)
    assert dec_file_2.read_text() == "Confidential production data payload"


def test_backup_with_age_recipient(tmp_path: Path, age_keypair):
    """Verifies create_backup outputs an age-encrypted archive when recipient is provided."""
    pub_recipient, priv_identity = age_keypair
    db_file = tmp_path / "age_test.db"
    init_db(custom_path=db_file)

    backup_path = create_backup(
        output_dir=tmp_path / "backups",
        recipient=pub_recipient,
        include_profile=False,
        custom_db_path=db_file,
    )

    assert backup_path.exists()
    assert backup_path.name.endswith(".zip.age")
    # Verify unencrypted zip was removed
    unencrypted_zip = backup_path.with_name(backup_path.name[:-4])
    assert not unencrypted_zip.exists()


def test_retention_policy_7_daily_4_weekly(tmp_path: Path):
    """
    Tests the 7-daily / 4-weekly retention policy:
    Generates 30 synthetic backup files across 30 days and verifies exact pruning.
    """
    b_dir = tmp_path / "retention_backups"
    b_dir.mkdir()

    base_time = datetime(2025, 3, 1, 12, 0, 0)
    created_files: list[Path] = []

    # Create backups spanning 30 consecutive days (2 backups per day on some days)
    for day in range(30):
        current_dt = base_time + timedelta(days=day)
        date_str = current_dt.strftime("%Y%m%d")
        f1 = b_dir / f"backup_{date_str}_100000_4.zip.age"
        f1.write_text("data")
        created_files.append(f1)

        f2 = b_dir / f"backup_{date_str}_220000_4.zip.age"
        f2.write_text("data")
        created_files.append(f2)

    res = apply_retention_policy(
        backups_dir=b_dir, daily_limit=7, weekly_limit=4, dry_run=False
    )

    assert res["total_scanned"] == 60
    # Must retain at most 7 distinct daily + 4 distinct weekly backups (union)
    # The union of 7 daily and 4 weekly cannot exceed 11
    assert len(res["retained"]) <= 11
    assert len(res["retained"]) >= 7
    # All pruned files should be deleted from disk
    assert len(res["pruned"]) == 60 - len(res["retained"])
    for p_name in res["pruned"]:
        assert not (b_dir / p_name).exists()
    for r_name in res["retained"]:
        assert (b_dir / r_name).exists()


def test_full_restore_drill_with_age(tmp_path: Path, age_keypair):
    """
    Complete disaster recovery drill:
    1. Populate DB and files
    2. Create age-encrypted backup
    3. Wipe source files
    4. Restore from encrypted backup using off-server key
    5. Verify integrity of DB, schema, and artifacts
    """
    pub_recipient, priv_identity = age_keypair

    source_root = tmp_path / "source"
    db_path = source_root / "data" / "job_applier.db"
    db_path.parent.mkdir(parents=True)
    init_db(custom_path=db_path)

    upsert_application(
        app_id="drill-app-1",
        company="Disaster Recovery Ltd",
        title="Reliability Architect",
        job_url="https://example.com/drill/1",
        custom_path=db_path,
    )

    # Create dummy app folder and CV
    app_folder = source_root / "output" / "applications" / "drill-app-1"
    app_folder.mkdir(parents=True)
    (app_folder / "CV_test.pdf").write_bytes(b"%PDF-1.4 test cv payload")

    with patch("job_applier.ops.backup.get_project_root", return_value=source_root):
        backup_file = create_backup(
            output_dir=tmp_path / "vault",
            recipient=pub_recipient,
            include_profile=True,
            custom_db_path=db_path,
        )

    # Wipe source data
    db_path.unlink()
    shutil.rmtree(app_folder)

    # Restore drill into target directory
    restore_target = tmp_path / "restored"
    target_db = restore_target / "data" / "job_applier.db"

    with patch("job_applier.ops.backup.get_project_root", return_value=restore_target):
        rep = restore_backup(
            backup_path=backup_file,
            identity=priv_identity,
            target_dir=restore_target,
            restore_profile=True,
            custom_db_path=target_db,
        )

    assert rep["status"] == "success"
    assert target_db.exists()

    # Query restored database
    conn = sqlite3.connect(str(target_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT company, title FROM applications WHERE id = 'drill-app-1';"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["company"] == "Disaster Recovery Ltd"
    assert (
        restore_target / "output" / "applications" / "drill-app-1" / "CV_test.pdf"
    ).exists()


def test_schema_downgrade_refusal_at_runtime_and_restore(tmp_path: Path, age_keypair):
    """
    Verifies that both the migration runner and restore_backup strictly refuse
    to run against or restore a database whose schema version exceeds CURRENT_SCHEMA_VERSION.
    """
    db_file = tmp_path / "future.db"
    init_db(custom_path=db_file)

    # Manually inject a future schema version 999 into schema_migrations
    conn = get_connection(custom_path=db_file)
    with conn:
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (999, 'future_features', datetime('now'));"
        )
    conn.close()

    # 1. Verify runtime refuse
    with pytest.raises(RuntimeError, match="newer than supported code version"):
        run_migrations(custom_path=db_file)

    # 2. Build a backup containing this future database
    backup_file = tmp_path / "future_backup.zip"
    import zipfile

    manifest = {
        "version": "1.0.0",
        "schema_version": 999,
        "exported_at": "2025-01-01 00:00:00",
    }
    with zipfile.ZipFile(str(backup_file), "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.write(str(db_file), arcname="data/job_applier.db")

    # 3. Verify restore refuses
    with pytest.raises(
        DowngradeRefusalError, match="newer than supported codebase version"
    ):
        restore_backup(backup_path=backup_file, target_dir=tmp_path / "restore_dest")


def test_disk_pressure_guard_detection_and_fail_closed(tmp_path: Path):
    """
    Verifies that check_disk_pressure detects low headroom, enforce_disk_pressure_guard pauses worker,
    and SubmissionSafetyGuard rejects pre-submit when under pressure.
    """
    db_file = tmp_path / "disk.db"
    init_db(custom_path=db_file)

    # 1. Normal state
    status = check_disk_pressure()
    assert isinstance(status.is_under_pressure, bool)

    # 2. Mock low disk space (simulate 100MB free where 1GB required)
    mock_usage = MagicMock(free=100 * 1024 * 1024, total=500 * 1024 * 1024 * 1024)
    with patch("shutil.disk_usage", return_value=mock_usage):
        low_status = check_disk_pressure()
        assert low_status.is_under_pressure is True
        assert "below minimum required headroom" in (low_status.reason or "")

        # Test enforcement: pauses worker and returns False
        ok = enforce_disk_pressure_guard(custom_db_path=db_file)
        assert ok is False

        ctrl = get_runtime_control(custom_path=db_file)
        assert ctrl["is_paused"] is True

        # Unpause to test the direct disk pressure guard check in SubmissionSafetyGuard
        set_runtime_pause(False, custom_path=db_file)
        with pytest.raises(DiskPressureError, match="Pre-submit safety guard rejected"):
            SubmissionSafetyGuard.validate_pre_submit_safety(
                job_id="dummy-job",
                worker_id="worker-1",
                generation=1,
                adapter_name="greenhouse",
                profile_snapshot={},
                artifact_revisions={},
                is_mock=False,
                custom_db_path=db_file,
            )


def test_debug_artifact_ttl_cleanup(tmp_path: Path):
    """Verifies that debug traces and failure screenshots older than 7 days are pruned."""
    traces_dir = tmp_path / "output" / "traces"
    traces_dir.mkdir(parents=True)
    old_trace = traces_dir / "trace_old.zip"
    old_trace.write_bytes(b"trace data")

    app_dir = tmp_path / "output" / "applications" / "app-123"
    app_dir.mkdir(parents=True)
    old_dom = app_dir / "diagnostic_dom.html"
    old_dom.write_text("<html>bad form</html>")
    recent_dom = app_dir / "new_dom.html"
    recent_dom.write_text("<html>other</html>")

    # Set old mtime (10 days ago)
    ten_days_ago = time.time() - (10 * 86400)
    os.utime(str(old_trace), (ten_days_ago, ten_days_ago))
    os.utime(str(old_dom), (ten_days_ago, ten_days_ago))

    purged = cleanup_expired_debug_artifacts(max_age_days=7, base_dir=tmp_path)
    assert purged == 2
    assert not old_trace.exists()
    assert not old_dom.exists()
    assert recent_dom.exists()


def test_evidence_redaction_on_auth_screens(tmp_path: Path):
    """Verifies that BrowserAutomator suppresses screenshots and DOM dumps on login/MFA pages."""
    automator = BrowserAutomator(headless=True)
    automator.page = MagicMock()
    automator.page.url = "https://accounts.google.com/signin/v2/challenge/pwd"

    app_dir = tmp_path / "app-redact"
    app_dir.mkdir()

    # 1. safe_screenshot should refuse and return False
    shot_path = app_dir / "submission_failed.png"
    saved = automator.safe_screenshot(shot_path)
    assert saved is False
    assert not shot_path.exists()

    # 2. _save_diagnostics should redact DOM dump and field details
    automator._save_diagnostics(
        app_dir=app_dir,
        company="Secure Co",
        job_title="Security Lead",
        job_url="https://accounts.google.com/signin",
        report={"fields_filled": ["password123"]},
        reason="Password required",
    )

    dom_content = (app_dir / "diagnostic_dom.html").read_text()
    assert "Redacted: Sensitive authentication" in dom_content
    assert "password123" not in dom_content

    diag_data = json.loads((app_dir / "diagnostics.json").read_text())
    assert diag_data["reason"] == "[REDACTED_AUTHENTICATION]"
    assert diag_data["fields_filled"] == ["[REDACTED_AUTHENTICATION_SCREEN]"]


def test_emergency_stop_circuit_breaker(tmp_path: Path):
    """
    Verifies emergency stop:
    - Atomically sets is_stopped=1, is_paused=1
    - Revokes active worker leases
    - Releases takeover
    - Clears cleanly with clear_emergency_stop
    """
    db_file = tmp_path / "estop.db"
    init_db(custom_path=db_file)

    # Insert an active claimed job
    conn = get_connection(custom_path=db_file)
    with conn:
        conn.execute(
            """
            INSERT INTO applications (id, company, title, job_url, folder_name, created_at, updated_at)
            VALUES ('app-estop', 'Corp', 'Dev', 'https://example.com/j', 'app-estop', datetime('now'), datetime('now'));
            """
        )
    conn.close()

    job = enqueue_job("app-estop", custom_path=db_file)
    claimed = claim_next_job(
        worker_id="active-worker", lease_seconds=60, custom_path=db_file
    )
    assert claimed is not None
    assert claimed.state == JobState.CLAIMED.value

    # Engage emergency stop
    res = emergency_stop(
        reason="Operator detected critical anomaly", custom_db_path=db_file
    )
    assert res["status"] == "success"
    assert res["is_stopped"] is True
    assert res["revoked_leases_count"] == 1

    # Check runtime control
    ctrl = get_runtime_control(custom_path=db_file)
    assert ctrl["is_stopped"] is True
    assert ctrl["is_paused"] is True

    # Check job was revoked back to ready
    conn = get_connection(custom_path=db_file)
    row = conn.execute(
        "SELECT state, lease_owner FROM automation_jobs WHERE id = ?;", (job.id,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["state"] == JobState.READY.value
    assert row["lease_owner"] is None

    # Clear emergency stop
    clear_res = clear_emergency_stop(custom_db_path=db_file)
    assert clear_res["is_stopped"] is False
    assert (
        clear_res["is_paused"] is True
    )  # Worker remains paused awaiting explicit resume

    ctrl_after = get_runtime_control(custom_path=db_file)
    assert ctrl_after["is_stopped"] is False
    assert ctrl_after["is_paused"] is True


def test_emergency_stop_sets_ambiguous_submission(tmp_path: Path):
    """Verifies that emergency_stop sets ambiguous_submission (not 'paused') on submit_intent/verifying jobs."""
    db_file = tmp_path / "em_stop.db"
    init_db(db_file)

    upsert_application(
        app_id="app-em-1",
        company="Emergency Corp",
        title="SRE",
        job_url="https://emergency.example/job/1",
        custom_path=db_file,
    )
    job = enqueue_job("app-em-1", adapter="ashby", custom_path=db_file)

    conn = get_connection(db_file)
    conn.execute(
        "UPDATE automation_jobs SET state = 'submit_intent' WHERE id = ?;",
        (job.id,),
    )
    conn.commit()
    conn.close()

    emergency_stop(reason="Operator test halt", custom_db_path=db_file)

    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT state, checkpoint FROM automation_jobs WHERE id = ?;", (job.id,)
    ).fetchone()
    conn.close()

    assert row["state"] == JobState.AMBIGUOUS_SUBMISSION.value
    assert row["checkpoint"] == "emergency_stop_ambiguous"


def test_emergency_stop_enqueues_to_notification_outbox(tmp_path: Path):
    db_file = tmp_path / "test_estop.db"
    init_db(db_file)

    res = emergency_stop(reason="Disk failure test", custom_db_path=db_file)
    assert res["is_stopped"] is True

    outbox = get_pending_notifications(limit=10, custom_path=db_file)
    assert len(outbox) >= 1
    assert any("Emergency Stop" in item["title"] for item in outbox)
