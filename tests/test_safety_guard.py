from __future__ import annotations

from job_applier.automation.adapters import (
    AuthenticationRequiredError,
    CaptchaDetectedError,
)
from unittest.mock import MagicMock
from unittest.mock import patch
from job_applier.cli.worker import process_claimed_job

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from job_applier.automation.adapters.models import ConfirmationEvidence
from job_applier.automation.candidate_profile import CandidateProfile
from job_applier.automation.queue import (
    JobState,
    claim_next_job,
    create_attempt,
    enqueue_job,
    get_runtime_control,
    set_runtime_pause,
    validate_application_artifacts,
)
from job_applier.automation.safety_guard import (
    AdapterDisabledError,
    AutomationPausedOrStoppedError,
    CanaryRequirementUnmetError,
    DailySubmissionLimitExceededError,
    FencingOwnershipLostError,
    FrozenRevisionMismatchError,
    GenericAdapterSubmissionBlockedError,
    MissingArtifactError,
    SubmissionPacingViolationError,
    SubmissionSafetyGuard,
    TakeoverActiveError,
)
from job_applier.db import (
    get_adapter_control,
    get_connection,
    init_db,
    record_canary_approval,
    record_submission_timestamp,
    set_adapter_enabled,
    upsert_application,
)


@pytest.fixture
def test_env(tmp_path: Path):
    db_file = tmp_path / "safety_test.db"
    init_db(custom_path=db_file)

    # Create dummy application folder and valid CV artifact
    app_id = "app_corp_engineer_0"
    app_dir = tmp_path / "applications" / app_id
    app_dir.mkdir(parents=True, exist_ok=True)
    cv_file = app_dir / "CV_Corp_Engineer.pdf"
    cv_file.write_bytes(b"%PDF-1.4 Mock CV Content with sufficient bytes")

    # Upsert application
    upsert_application(
        app_id=app_id,
        company="Corp Inc",
        title="Engineer",
        job_url="https://boards.greenhouse.io/corp/jobs/123",
        platform="Greenhouse",
        status="pending",
        folder_name=app_id,
        cv_filename=cv_file.name,
        custom_path=db_file,
    )

    profile = CandidateProfile(
        full_name="Alex Morgan",
        email="alex.morgan@example.com",
    )
    profile_snapshot = {
        "full_name": "Alex Morgan",
        "email": "alex.morgan@example.com",
    }
    artifact_revisions = {
        "folder": str(app_dir),
        "cv_filename": cv_file.name,
        "cv_mtime": cv_file.stat().st_mtime,
        "cv_size": cv_file.stat().st_size,
    }

    return {
        "db": db_file,
        "app_id": app_id,
        "app_dir": app_dir,
        "cv_file": cv_file,
        "profile": profile,
        "profile_snapshot": profile_snapshot,
        "artifact_revisions": artifact_revisions,
    }


# =========================================================================
# 1. Ownership & Runtime Controls Tests
# =========================================================================


def test_safety_guard_fencing_and_ownership(test_env):
    db = test_env["db"]
    job = enqueue_job(test_env["app_id"], adapter="greenhouse", custom_path=db)
    claimed = claim_next_job("worker-1", lease_seconds=60, custom_path=db)
    assert claimed is not None

    # Valid check in mock mode passes
    SubmissionSafetyGuard.validate_pre_submit_safety(
        job_id=job.id,
        worker_id="worker-1",
        generation=1,
        adapter_name="greenhouse",
        profile_snapshot=test_env["profile_snapshot"],
        artifact_revisions=test_env["artifact_revisions"],
        current_profile=test_env["profile"],
        is_mock=True,
        custom_db_path=db,
    )

    # Wrong worker ID fails closed
    with pytest.raises(FencingOwnershipLostError, match="Worker lease lost"):
        SubmissionSafetyGuard.validate_pre_submit_safety(
            job_id=job.id,
            worker_id="rogue-worker",
            generation=1,
            adapter_name="greenhouse",
            profile_snapshot=test_env["profile_snapshot"],
            artifact_revisions=test_env["artifact_revisions"],
            current_profile=test_env["profile"],
            is_mock=True,
            custom_db_path=db,
        )

    # Generation mismatch fails closed
    with pytest.raises(FencingOwnershipLostError, match="generation mismatch"):
        SubmissionSafetyGuard.validate_pre_submit_safety(
            job_id=job.id,
            worker_id="worker-1",
            generation=99,
            adapter_name="greenhouse",
            profile_snapshot=test_env["profile_snapshot"],
            artifact_revisions=test_env["artifact_revisions"],
            current_profile=test_env["profile"],
            is_mock=True,
            custom_db_path=db,
        )


def test_safety_guard_pause_and_takeover(test_env):
    db = test_env["db"]
    job = enqueue_job(test_env["app_id"], adapter="greenhouse", custom_path=db)
    claim_next_job("worker-1", lease_seconds=60, custom_path=db)

    # Pause engaged
    set_runtime_pause(True, custom_path=db)
    with pytest.raises(AutomationPausedOrStoppedError, match="paused"):
        SubmissionSafetyGuard.validate_pre_submit_safety(
            job_id=job.id,
            worker_id="worker-1",
            generation=1,
            adapter_name="greenhouse",
            profile_snapshot=test_env["profile_snapshot"],
            artifact_revisions=test_env["artifact_revisions"],
            is_mock=True,
            custom_db_path=db,
        )
    set_runtime_pause(False, custom_path=db)

    # Manual takeover engaged
    conn = get_connection(db)
    with conn:
        conn.execute(
            "UPDATE runtime_control SET manual_takeover_owner = 'operator-session' WHERE id = 1;"
        )
    conn.close()

    with pytest.raises(TakeoverActiveError, match="Operator takeover active"):
        SubmissionSafetyGuard.validate_pre_submit_safety(
            job_id=job.id,
            worker_id="worker-1",
            generation=1,
            adapter_name="greenhouse",
            profile_snapshot=test_env["profile_snapshot"],
            artifact_revisions=test_env["artifact_revisions"],
            is_mock=True,
            custom_db_path=db,
        )


# =========================================================================
# 2. Generic Adapter Prohibition Test
# =========================================================================


def test_safety_guard_strictly_blocks_generic_adapter(test_env):
    db = test_env["db"]
    job = enqueue_job(test_env["app_id"], adapter="generic", custom_path=db)
    claim_next_job("worker-1", lease_seconds=60, custom_path=db)

    with pytest.raises(
        GenericAdapterSubmissionBlockedError, match="fill-only and strictly prohibited"
    ):
        SubmissionSafetyGuard.validate_pre_submit_safety(
            job_id=job.id,
            worker_id="worker-1",
            generation=1,
            adapter_name="generic",
            profile_snapshot=test_env["profile_snapshot"],
            artifact_revisions=test_env["artifact_revisions"],
            is_mock=True,
            custom_db_path=db,
        )


# =========================================================================
# 3. Canary Requirement & Enablement Gate
# =========================================================================


def test_safety_guard_canary_and_enablement_gate(test_env):
    db = test_env["db"]
    job = enqueue_job(test_env["app_id"], adapter="greenhouse", custom_path=db)
    claim_next_job("worker-1", lease_seconds=60, custom_path=db)

    # 1. Real submission with 0 canaries fails closed
    with pytest.raises(
        CanaryRequirementUnmetError,
        match="requires at least 3 approved confirmed canaries",
    ):
        SubmissionSafetyGuard.validate_pre_submit_safety(
            job_id=job.id,
            worker_id="worker-1",
            generation=1,
            adapter_name="greenhouse",
            profile_snapshot=test_env["profile_snapshot"],
            artifact_revisions=test_env["artifact_revisions"],
            is_mock=False,
            custom_db_path=db,
        )

    # 2. Add 3 approved canaries
    for i in range(3):
        record_canary_approval(
            adapter_name="greenhouse",
            job_id=f"canary_job_{i}",
            app_id=f"canary_app_{i}",
            approved_by="operator@example.com",
            confirmation_evidence=f"Proof {i}",
            custom_path=db,
        )

    # 3. Real submission still blocked because adapter is disabled
    with pytest.raises(
        AdapterDisabledError, match="disabled for autonomous submission"
    ):
        SubmissionSafetyGuard.validate_pre_submit_safety(
            job_id=job.id,
            worker_id="worker-1",
            generation=1,
            adapter_name="greenhouse",
            profile_snapshot=test_env["profile_snapshot"],
            artifact_revisions=test_env["artifact_revisions"],
            is_mock=False,
            custom_db_path=db,
        )

    # 4. Now operator enables adapter
    set_adapter_enabled("greenhouse", True, custom_path=db)

    # 5. Pre-submit safety now passes!
    SubmissionSafetyGuard.validate_pre_submit_safety(
        job_id=job.id,
        worker_id="worker-1",
        generation=1,
        adapter_name="greenhouse",
        profile_snapshot=test_env["profile_snapshot"],
        artifact_revisions=test_env["artifact_revisions"],
        current_profile=test_env["profile"],
        is_mock=False,
        custom_db_path=db,
    )


# =========================================================================
# 4. Rate Limiting: 5 Submissions/Day & 5-Minute Pacing Gate
# =========================================================================


def test_safety_guard_pacing_and_daily_cap(test_env):
    db = test_env["db"]
    job = enqueue_job(test_env["app_id"], adapter="lever", custom_path=db)
    claim_next_job("worker-1", lease_seconds=60, custom_path=db)

    for i in range(3):
        record_canary_approval(
            "lever", f"cj_{i}", f"ca_{i}", "operator", f"p_{i}", custom_path=db
        )
    set_adapter_enabled("lever", True, custom_path=db)

    # Record a submission that occurred 60 seconds ago (violates 300s pacing)
    recent_ts = (datetime.now() - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
    record_submission_timestamp("lever", recent_ts, custom_path=db)

    with pytest.raises(
        SubmissionPacingViolationError,
        match="5-minute interval between submissions not met",
    ):
        SubmissionSafetyGuard.validate_pre_submit_safety(
            job_id=job.id,
            worker_id="worker-1",
            generation=1,
            adapter_name="lever",
            profile_snapshot=test_env["profile_snapshot"],
            artifact_revisions=test_env["artifact_revisions"],
            is_mock=False,
            custom_db_path=db,
        )

    # Pacing clears after 301 seconds
    old_ts = (datetime.now() - timedelta(seconds=350)).strftime("%Y-%m-%d %H:%M:%S")
    record_submission_timestamp("lever", old_ts, custom_path=db)

    SubmissionSafetyGuard.validate_pre_submit_safety(
        job_id=job.id,
        worker_id="worker-1",
        generation=1,
        adapter_name="lever",
        profile_snapshot=test_env["profile_snapshot"],
        artifact_revisions=test_env["artifact_revisions"],
        current_profile=test_env["profile"],
        is_mock=False,
        custom_db_path=db,
    )

    # Daily cap: artificially insert 5 applied attempts today
    conn = get_connection(db)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with conn:
        for i in range(5):
            conn.execute(
                """
                INSERT INTO application_attempts (
                    id, job_id, app_id, attempt_number, outcome, created_at, completed_at
                ) VALUES (?, ?, ?, 1, 'applied', ?, ?);
                """,
                (f"att_{i}", job.id, test_env["app_id"], now_str, now_str),
            )
    conn.close()

    with pytest.raises(
        DailySubmissionLimitExceededError,
        match="Daily submission limit \\(5/day\\) reached",
    ):
        SubmissionSafetyGuard.validate_pre_submit_safety(
            job_id=job.id,
            worker_id="worker-1",
            generation=1,
            adapter_name="lever",
            profile_snapshot=test_env["profile_snapshot"],
            artifact_revisions=test_env["artifact_revisions"],
            is_mock=False,
            custom_db_path=db,
        )


# =========================================================================
# 5. Frozen Revisions & Missing Artifact Blocks
# =========================================================================


def test_safety_guard_frozen_revisions_and_missing_artifacts(test_env):
    db = test_env["db"]
    job = enqueue_job(test_env["app_id"], adapter="ashby", custom_path=db)
    claim_next_job("worker-1", lease_seconds=60, custom_path=db)

    # 1. CV modification mid-flight violates frozen revision
    altered_artifacts = dict(test_env["artifact_revisions"])
    altered_artifacts["cv_mtime"] = test_env["artifact_revisions"]["cv_mtime"] + 1000.0

    with pytest.raises(
        FrozenRevisionMismatchError, match="CV artifact was modified mid-flight"
    ):
        SubmissionSafetyGuard.validate_pre_submit_safety(
            job_id=job.id,
            worker_id="worker-1",
            generation=1,
            adapter_name="ashby",
            profile_snapshot=test_env["profile_snapshot"],
            artifact_revisions=altered_artifacts,
            current_profile=test_env["profile"],
            is_mock=True,
            custom_db_path=db,
        )

    # 2. Missing CV file fails closed
    missing_artifacts = dict(test_env["artifact_revisions"])
    missing_artifacts["cv_filename"] = "nonexistent.pdf"

    with pytest.raises(
        MissingArtifactError, match="Required CV artifact missing on disk"
    ):
        SubmissionSafetyGuard.validate_pre_submit_safety(
            job_id=job.id,
            worker_id="worker-1",
            generation=1,
            adapter_name="ashby",
            profile_snapshot=test_env["profile_snapshot"],
            artifact_revisions=missing_artifacts,
            current_profile=test_env["profile"],
            is_mock=True,
            custom_db_path=db,
        )

    # 3. Candidate profile changed email mid-flight fails closed
    altered_profile = CandidateProfile(
        full_name="Alex Morgan", email="different@example.com"
    )
    with pytest.raises(
        FrozenRevisionMismatchError, match="Candidate profile email changed"
    ):
        SubmissionSafetyGuard.validate_pre_submit_safety(
            job_id=job.id,
            worker_id="worker-1",
            generation=1,
            adapter_name="ashby",
            profile_snapshot=test_env["profile_snapshot"],
            artifact_revisions=test_env["artifact_revisions"],
            current_profile=altered_profile,
            is_mock=True,
            custom_db_path=db,
        )


def test_missing_artifacts_blocks_enqueueing_without_deleting_application(
    tmp_path: Path,
):
    db = tmp_path / "queue_artifacts.db"
    init_db(custom_path=db)

    app_id = "app_no_cv"
    upsert_application(
        app_id=app_id,
        company="NoCV Corp",
        title="Role",
        job_url="https://example.com/apply",
        platform="Generic",
        status="pending",
        folder_name=app_id,
        custom_path=db,
    )

    # Artifact validation fails
    has_art, reason = validate_application_artifacts(app_id, custom_path=db)
    assert has_art is False
    assert "missing" in reason.lower()

    # Enqueue with verify_artifacts=True blocks enqueueing
    with pytest.raises(MissingArtifactError, match="missing"):
        enqueue_job(app_id=app_id, verify_artifacts=True, custom_path=db)

    # Verify application was NOT deleted, but flagged as missing_artifacts
    conn = get_connection(db)
    row = conn.execute(
        "SELECT status, has_artifacts FROM applications WHERE id = ?;", (app_id,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["status"] == "missing_artifacts"
    assert row["has_artifacts"] == 0


# =========================================================================
# 6. Ambiguous & Confirmed Submission Reconciliation
# =========================================================================


def test_reconcile_ambiguous_submission_pauses_worker(test_env):
    db = test_env["db"]
    job = enqueue_job(test_env["app_id"], adapter="greenhouse", custom_path=db)
    claim_next_job("worker-1", lease_seconds=60, custom_path=db)
    attempt = create_attempt(job.id, test_env["app_id"], {}, "", {}, custom_path=db)

    evidence = ConfirmationEvidence(
        platform="greenhouse",
        adapter_version="1.0.0",
        confirmed=False,
        is_ambiguous=True,
        confirmation_text="Redirected to third party survey with no confirmation receipt",
    )

    status, msg = SubmissionSafetyGuard.reconcile_submission_outcome(
        job_id=job.id,
        app_id=test_env["app_id"],
        attempt_id=attempt.id,
        worker_id="worker-1",
        generation=1,
        adapter_name="greenhouse",
        company="Corp Inc",
        evidence=evidence,
        custom_db_path=db,
    )

    assert status == JobState.AMBIGUOUS_SUBMISSION.value
    assert "paused for safety" in msg

    # CRITICAL: Whole worker is paused!
    ctrl = get_runtime_control(db)
    assert ctrl["is_paused"] == 1


def test_reconcile_confirmed_submission_records_applied(test_env):
    db = test_env["db"]
    job = enqueue_job(test_env["app_id"], adapter="greenhouse", custom_path=db)
    claim_next_job("worker-1", lease_seconds=60, custom_path=db)
    attempt = create_attempt(job.id, test_env["app_id"], {}, "", {}, custom_path=db)

    evidence = ConfirmationEvidence(
        platform="greenhouse",
        adapter_version="1.0.0",
        confirmed=True,
        confirmation_id="GH-112233",
        confirmation_text="Your application has been received.",
        proof_element="#application_confirmation",
        is_ambiguous=False,
    )

    status, msg = SubmissionSafetyGuard.reconcile_submission_outcome(
        job_id=job.id,
        app_id=test_env["app_id"],
        attempt_id=attempt.id,
        worker_id="worker-1",
        generation=1,
        adapter_name="greenhouse",
        company="Corp Inc",
        evidence=evidence,
        custom_db_path=db,
    )

    assert status == "applied"
    assert "Successfully confirmed" in msg

    # Pacing timestamp updated
    gh_ctrl = get_adapter_control("greenhouse", custom_path=db)
    assert gh_ctrl is not None
    assert gh_ctrl["last_submission_at"] is not None

    # Application status in DB is applied
    conn = get_connection(db)
    app_row = conn.execute(
        "SELECT status FROM applications WHERE id = ?;", (test_env["app_id"],)
    ).fetchone()
    conn.close()
    assert app_row is not None
    assert app_row["status"] == "applied"


def test_worker_reconciles_submission_outcome_and_updates_pacing(
    tmp_path: Path,
):
    """Verifies that worker.py invokes SubmissionSafetyGuard.reconcile_submission_outcome upon applied outcome."""
    db_file = tmp_path / "worker_reconcile.db"
    init_db(db_file)

    upsert_application(
        app_id="app-rec-1",
        company="Pacing Corp",
        title="Site Reliability Engineer",
        job_url="https://pacing.example/jobs/1",
        custom_path=db_file,
    )
    job = enqueue_job("app-rec-1", adapter="greenhouse", custom_path=db_file)

    app_dir = tmp_path / "output" / "applications" / "app-rec-1"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "CV_Tailored.pdf").write_bytes(b"%PDF-1.4 test cv")

    mock_evidence = ConfirmationEvidence(
        confirmed=True,
        is_ambiguous=False,
        confirmation_text="Application received with ID 12345",
        platform="greenhouse",
        adapter_version="1.0.0",
    )

    with (
        patch(
            "job_applier.cli.worker.get_project_root",
            return_value=tmp_path,
        ),
        patch("job_applier.cli.worker.BrowserAutomator") as mock_automator_cls,
    ):
        mock_automator = MagicMock()
        mock_automator.run_autonomous_apply.return_value = (
            "applied",
            "Application received with ID 12345",
        )
        mock_automator.last_evidence = mock_evidence
        mock_automator_cls.return_value = mock_automator

        res = process_claimed_job(
            job=job,
            worker_id="test_worker_1",
            custom_db_path=db_file,
        )

        assert res == "applied"

        # Verify applications table was updated to applied
        conn = get_connection(db_file)
        row = conn.execute(
            "SELECT status FROM applications WHERE id = 'app-rec-1';"
        ).fetchone()
        assert row is not None
        assert row["status"] == "applied"

        # Verify adapter_controls last_submission_at is updated for pacing
        ctrl_row = conn.execute(
            "SELECT last_submission_at FROM adapter_controls WHERE adapter_name = 'greenhouse';"
        ).fetchone()
        conn.close()
        assert ctrl_row is not None
        assert ctrl_row["last_submission_at"] is not None


def test_ambiguous_submission_pauses_worker(tmp_path: Path):
    """Verifies that ambiguous submission pauses the worker via reconcile_submission_outcome."""
    db_file = tmp_path / "worker_ambiguous.db"
    init_db(db_file)

    upsert_application(
        app_id="app-amb-1",
        company="Ambiguous Corp",
        title="Backend Engineer",
        job_url="https://ambiguous.example/jobs/2",
        custom_path=db_file,
    )
    job = enqueue_job("app-amb-1", adapter="lever", custom_path=db_file)

    app_dir = tmp_path / "output" / "applications" / "app-amb-1"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "CV_Tailored.pdf").write_bytes(b"%PDF-1.4 test cv")

    mock_evidence = ConfirmationEvidence(
        confirmed=False,
        is_ambiguous=True,
        confirmation_text="Page closed unexpectedly before confirmation could be verified",
        platform="lever",
        adapter_version="1.0.0",
    )

    with (
        patch("job_applier.cli.worker.get_project_root", return_value=tmp_path),
        patch("job_applier.cli.worker.BrowserAutomator") as mock_automator_cls,
    ):
        mock_automator = MagicMock()
        mock_automator.run_autonomous_apply.return_value = (
            "ambiguous_submission",
            "Page closed unexpectedly",
        )
        mock_automator.last_evidence = mock_evidence
        mock_automator_cls.return_value = mock_automator

        res = process_claimed_job(
            job=job,
            worker_id="test_worker_2",
            custom_db_path=db_file,
        )

        assert res == "ambiguous_submission"

        # Verify runtime control was paused
        ctrl = get_runtime_control(db_file)
        assert ctrl["is_paused"] is True


def test_lost_lease_aborts_before_submit_intent(tmp_path: Path):
    """Verifies that process_claimed_job aborts and raises if lost_lease is set on the heartbeat thread."""
    db_file = tmp_path / "lost_lease.db"
    init_db(db_file)

    upsert_application(
        app_id="app-lease-1",
        company="Lease Corp",
        title="Platform Engineer",
        job_url="https://lease.example/job/1",
        custom_path=db_file,
    )
    job = enqueue_job("app-lease-1", adapter="greenhouse", custom_path=db_file)

    app_dir = tmp_path / "app_lease_1"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "CV_Tailored.pdf").write_bytes(b"%PDF-1.4 test")

    with (
        patch("job_applier.cli.worker.get_project_root", return_value=tmp_path),
        patch("job_applier.cli.worker.BrowserAutomator") as mock_automator_cls,
    ):
        mock_automator = MagicMock()

        def fake_apply(**kwargs):
            on_intent = kwargs.get("on_submit_intent")
            # Simulate lease loss before intent
            with patch("job_applier.cli.worker.HeartbeatThread.lost_lease", True):
                if on_intent:
                    on_intent()
            return "applied", "ok"

        mock_automator.run_autonomous_apply.side_effect = fake_apply
        mock_automator_cls.return_value = mock_automator

        # Simulate lost_lease during process_claimed_job
        with patch.object(mock_automator_cls, "cancellation_check", return_value=True):
            res = process_claimed_job(
                job=job,
                worker_id="worker_lost_1",
                custom_db_path=db_file,
            )
            # Expect exception handler pause and alert
            assert res in (
                "failed",
                "retry_wait",
                "failed_permanent",
                "unknown_question",
                "paused",
            )


def test_auth_required_pauses_without_engaging_emergency_stop(tmp_path: Path):
    db_file = tmp_path / "auth_pause.db"
    init_db(db_file)
    upsert_application(
        app_id="app-auth-pause",
        company="Example Corp",
        title="Engineer",
        job_url="https://example.com/jobs/1",
        custom_path=db_file,
    )
    enqueue_job("app-auth-pause", adapter="greenhouse", custom_path=db_file)
    claimed = claim_next_job("auth-worker", custom_path=db_file)
    assert claimed is not None

    state = SubmissionSafetyGuard.handle_exception_pause_and_alert(
        job_id=claimed.id,
        app_id=claimed.app_id,
        attempt_id=None,
        worker_id="auth-worker",
        generation=claimed.fencing_generation,
        exc=AuthenticationRequiredError("Login required"),
        company="Example Corp",
        custom_db_path=db_file,
    )

    assert state == JobState.AUTH_REQUIRED.value
    control = get_runtime_control(custom_path=db_file)
    assert control["is_paused"] is True
    assert control["is_stopped"] is False


def test_safety_guard_does_not_duplicate_failure_notifications(tmp_path: Path):
    from job_applier.automation.safety_guard import SubmissionSafetyGuard

    db_file = tmp_path / "dedup_notif.db"
    init_db(db_file)

    upsert_application(
        app_id="app-dedup-1",
        company="Dedup Corp",
        title="Engineer",
        job_url="https://dedup.example/jobs/1",
        custom_path=db_file,
    )
    enqueue_job("app-dedup-1", adapter="greenhouse", custom_path=db_file)
    claimed = claim_next_job("w1", custom_path=db_file)
    assert claimed is not None

    evidence = ConfirmationEvidence(
        confirmed=False,
        is_ambiguous=True,
        confirmation_text="Verification pending",
        platform="greenhouse",
        adapter_version="1.0",
    )

    state, msg = SubmissionSafetyGuard.reconcile_submission_outcome(
        job_id=claimed.id,
        app_id="app-dedup-1",
        worker_id="w1",
        generation=claimed.fencing_generation,
        attempt_id="",
        evidence=evidence,
        adapter_name="greenhouse",
        company="Dedup Corp",
        custom_db_path=db_file,
    )
    assert state == JobState.AMBIGUOUS_SUBMISSION.value

    conn = get_connection(db_file)
    notifs = conn.execute(
        "SELECT id, category FROM notifications WHERE job_id = ?;", (claimed.id,)
    ).fetchall()
    conn.close()
    assert len(notifs) == 1

    # Test exception alert path
    from job_applier.automation.queue import set_runtime_pause

    set_runtime_pause(False, custom_path=db_file)
    upsert_application(
        app_id="app-dedup-2",
        company="Dedup Corp",
        title="Engineer 2",
        job_url="https://dedup.example/jobs/2",
        custom_path=db_file,
    )
    enqueue_job("app-dedup-2", adapter="greenhouse", custom_path=db_file)
    claimed2 = claim_next_job("w2", custom_path=db_file)
    assert claimed2 is not None

    state2 = SubmissionSafetyGuard.handle_exception_pause_and_alert(
        job_id=claimed2.id,
        app_id="app-dedup-2",
        attempt_id="",
        worker_id="w2",
        generation=claimed2.fencing_generation,
        exc=CaptchaDetectedError("CAPTCHA challenge detected"),
        company="Dedup Corp",
        custom_db_path=db_file,
    )
    assert state2 == "captcha_required"

    conn = get_connection(db_file)
    notifs2 = conn.execute(
        "SELECT id, category FROM notifications WHERE job_id = ?;", (claimed2.id,)
    ).fetchall()
    conn.close()
    assert len(notifs2) == 1


def test_validate_application_artifacts_supports_custom_and_cv_pdf(tmp_path: Path):
    from job_applier.automation.queue import validate_application_artifacts

    db_file = tmp_path / "artifacts.db"
    init_db(db_file)

    app_dir = tmp_path / "output" / "applications" / "app_cust"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "custom_resume.pdf").write_bytes(b"%PDF-1.4 custom")

    upsert_application(
        app_id="app_cust",
        company="CustCorp",
        title="Custom Dev",
        job_url="https://example.com/job/1",
        folder_name="app_cust",
        cv_filename="custom_resume.pdf",
        custom_path=db_file,
    )

    valid, path_str = validate_application_artifacts(
        app_id="app_cust", custom_path=db_file, base_dir=tmp_path
    )
    assert valid is True
    assert "custom_resume.pdf" in path_str

    # Test fallback CV.pdf
    app_dir2 = tmp_path / "output" / "applications" / "app_cv_pdf"
    app_dir2.mkdir(parents=True, exist_ok=True)
    (app_dir2 / "CV.pdf").write_bytes(b"%PDF-1.4 standard")

    upsert_application(
        app_id="app_cv_pdf",
        company="StdCorp",
        title="Std Dev",
        job_url="https://example.com/job/2",
        folder_name="app_cv_pdf",
        cv_filename="",
        custom_path=db_file,
    )

    valid2, path_str2 = validate_application_artifacts(
        app_id="app_cv_pdf", custom_path=db_file, base_dir=tmp_path
    )
    assert valid2 is True
    assert "CV.pdf" in path_str2


def test_safe_resume_takeover_records_submission_pacing(tmp_path: Path):
    from job_applier.automation.safe_resume import safe_resume_revalidate
    from job_applier.db import get_adapter_control

    db_file = tmp_path / "resume_pacing.db"
    init_db(db_file)

    upsert_application(
        app_id="app-pace-1",
        company="Pacing Corp",
        title="Software Engineer",
        job_url="https://jobs.lever.co/pacing/1",
        custom_path=db_file,
    )
    enqueue_job("app-pace-1", adapter="lever", custom_path=db_file)
    claimed = claim_next_job("w-pace", custom_path=db_file)
    assert claimed is not None

    mock_automator = MagicMock()
    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_page.url = "https://jobs.lever.co/pacing/1/thanks"
    mock_page.locator.return_value.text_content.return_value = (
        "Thank you for your application!"
    )
    mock_automator.page = mock_page

    with patch(
        "job_applier.automation.safe_resume.validate_target_url",
        return_value=(True, ""),
    ):
        res = safe_resume_revalidate(
            job_id=claimed.id, automator=mock_automator, custom_path=db_file
        )
    assert res["status"] == "success"
    assert res["action"] == "marked_applied"

    ctrl = get_adapter_control("lever", custom_path=db_file)
    assert ctrl is not None
    assert ctrl["last_submission_at"] is not None


def test_global_pacing_across_adapters(tmp_path: Path):
    from job_applier.automation.queue import claim_next_job, enqueue_job
    from job_applier.automation.safety_guard import (
        SubmissionPacingViolationError,
        SubmissionSafetyGuard,
    )
    from job_applier.db import (
        init_db,
        record_canary_approval,
        record_submission_timestamp,
        set_adapter_enabled,
        upsert_application,
    )

    db_file = tmp_path / "test_pacing.db"
    init_db(db_file)

    upsert_application(
        app_id="app-ashby-pacing",
        company="AshbyPacing",
        title="Dev",
        job_url="https://jobs.ashbyhq.com/ashby/1",
        custom_path=db_file,
    )
    enqueue_job("app-ashby-pacing", adapter="ashby", custom_path=db_file)
    claimed = claim_next_job("w-pacing", custom_path=db_file)
    assert claimed is not None

    for i in range(3):
        record_canary_approval(
            "ashby", f"job-{i}", f"app-{i}", "admin", "proof", custom_path=db_file
        )
    set_adapter_enabled("ashby", True, custom_path=db_file)

    # Record a submission on Greenhouse right now
    record_submission_timestamp("greenhouse", custom_path=db_file)

    # Ashby attempts submission immediately after; global pacing must block it
    with pytest.raises(
        SubmissionPacingViolationError, match="interval between submissions not met"
    ):
        SubmissionSafetyGuard.validate_pre_submit_safety(
            job_id=claimed.id,
            worker_id="w-pacing",
            generation=claimed.fencing_generation,
            adapter_name="ashby",
            profile_snapshot={},
            artifact_revisions={},
            is_mock=False,
            custom_db_path=db_file,
        )


def test_shared_artifact_resolver(tmp_path: Path):
    from job_applier.automation.queue import validate_application_artifacts
    from job_applier.db import init_db, upsert_application
    from job_applier.utils import get_project_root

    db_file = tmp_path / "test_artifact.db"
    init_db(db_file)

    # Missing folder fails
    upsert_application(
        app_id="app-art-missing",
        company="MissingCo",
        title="Dev",
        job_url="https://example.com",
        folder_name="nonexistent_folder_xyz",
        custom_path=db_file,
    )
    valid, err = validate_application_artifacts("app-art-missing", custom_path=db_file)
    assert valid is False
    assert "missing" in err.lower()

    # Valid CV.pdf in output/applications succeeds
    root = get_project_root()
    test_folder = root / "output" / "applications" / "test_folder_art"
    test_folder.mkdir(parents=True, exist_ok=True)
    cv_file = test_folder / "CV.pdf"
    cv_file.write_bytes(b"%PDF-1.4 test")

    try:
        upsert_application(
            app_id="app-art-valid",
            company="ValidCo",
            title="Dev",
            job_url="https://example.com",
            folder_name="test_folder_art",
            custom_path=db_file,
        )
        valid2, cv_path = validate_application_artifacts(
            "app-art-valid", custom_path=db_file
        )
        assert valid2 is True
        assert Path(cv_path).name == "CV.pdf"
    finally:
        if cv_file.exists():
            cv_file.unlink()
        if test_folder.exists():
            test_folder.rmdir()


def test_execute_submit_intent_fails_closed_on_lost_lease(tmp_path: Path):
    db_file = tmp_path / "test_safety.db"
    init_db(db_file)
    upsert_application(
        "app-1", "Corp", "SE", "https://example.com/1", custom_path=db_file
    )
    job = enqueue_job("app-1", adapter="greenhouse", custom_path=db_file)

    # Worker 1 claims job
    claimed = claim_next_job("worker_1", custom_path=db_file)
    assert claimed is not None

    # Another worker steals or generation increments
    # Now worker_1 calls execute_submit_intent with wrong generation (99)
    with pytest.raises(RuntimeError, match="lost lease or fencing generation"):
        SubmissionSafetyGuard.execute_submit_intent(
            job_id=job.id,
            attempt_id="att-1",
            worker_id="worker_1",
            generation=99,
            custom_db_path=db_file,
        )


def test_definitive_validation_rejection_is_not_ambiguous(tmp_path: Path):
    db_file = tmp_path / "test_evidence.db"
    init_db(db_file)
    upsert_application(
        "app-1", "Corp", "SE", "https://example.com/1", custom_path=db_file
    )
    job = enqueue_job("app-1", adapter="greenhouse", custom_path=db_file)
    claim_next_job("w1", custom_path=db_file)

    # Evidence: confirmed=False, is_ambiguous=False (definitive rejection)
    evidence = ConfirmationEvidence(
        platform="greenhouse",
        adapter_version="1.0.0",
        confirmed=False,
        is_ambiguous=False,
        confirmation_text="Invalid phone number format",
    )

    state, msg = SubmissionSafetyGuard.reconcile_submission_outcome(
        job_id=job.id,
        app_id="app-1",
        attempt_id="att-1",
        worker_id="w1",
        generation=1,
        adapter_name="greenhouse",
        company="Corp",
        evidence=evidence,
        custom_db_path=db_file,
    )

    assert state == JobState.FAILED_PERMANENT.value
    assert "Invalid phone number format" in msg


def test_worker_pacing_reschedule_does_not_burn_retries(tmp_path: Path):
    from job_applier.automation.queue import claim_next_job, enqueue_job, get_connection
    from job_applier.automation.safety_guard import SubmissionPacingViolationError
    from job_applier.cli.worker import process_claimed_job
    from job_applier.db import init_db, upsert_application

    db_file = tmp_path / "test_worker_pacing.db"
    init_db(db_file)

    upsert_application(
        app_id="app-pace-retry",
        company="PaceCo",
        title="Dev",
        job_url="https://example.com/job/1",
        folder_name="test_pace_folder",
        custom_path=db_file,
    )
    job = enqueue_job("app-pace-retry", adapter="greenhouse", custom_path=db_file)
    claimed = claim_next_job("w1", custom_path=db_file)
    assert claimed is not None

    cv_mock_path = tmp_path / "CV.pdf"
    cv_mock_path.write_bytes(b"%PDF-1.4 test")
    mock_profile = MagicMock()
    mock_profile.to_dict.return_value = {}

    with (
        patch(
            "job_applier.cli.worker.validate_application_artifacts",
            return_value=(True, str(cv_mock_path)),
        ),
        patch(
            "job_applier.cli.worker.load_candidate_profile",
            return_value=mock_profile,
        ),
        patch(
            "job_applier.cli.worker.SubmissionSafetyGuard.validate_pacing_and_daily_limits",
            side_effect=SubmissionPacingViolationError("Pacing interval active"),
        ),
    ):
        result = process_claimed_job(claimed, worker_id="w1", custom_db_path=db_file)

    assert result == "retry_wait"

    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT state, attempt_count, max_retries, next_retry_at FROM automation_jobs WHERE id = ?;",
        (job.id,),
    ).fetchone()
    conn.close()

    assert row["state"] == "retry_wait"
    assert row["attempt_count"] == 0
    assert row["next_retry_at"] is not None


def test_safety_guard_blocks_jobicy_aggregator_url(test_env):
    """Verifies that Jobicy aggregator URLs are rejected fail-closed with AggregatorBlockedError."""
    from job_applier.automation.safety_guard import AggregatorBlockedError
    from job_applier.db import upsert_application

    db = test_env["db"]
    upsert_application(
        app_id="jobicy-app-test",
        company="Jobicy Employer",
        title="Remote Dev",
        job_url="https://jobicy.com/jobs/152572-remote-dev",
        status="pending",
        custom_path=db,
    )
    job = enqueue_job("jobicy-app-test", adapter="greenhouse", custom_path=db)
    claim_next_job("worker-1", lease_seconds=60, custom_path=db)

    with pytest.raises(AggregatorBlockedError, match="Jobicy aggregator landing page"):
        SubmissionSafetyGuard.validate_pre_submit_safety(
            job_id=job.id,
            worker_id="worker-1",
            generation=1,
            adapter_name="greenhouse",
            profile_snapshot={},
            artifact_revisions={},
            is_mock=True,
            custom_db_path=db,
        )
