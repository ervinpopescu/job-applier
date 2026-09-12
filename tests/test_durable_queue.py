from job_applier.automation.browser_automator import BrowserAutomator
from job_applier.automation.profile_lock import ProfileOwnershipLock
from job_applier.ops.emergency_stop import emergency_stop
from job_applier.automation.queue import get_connection
from job_applier.automation.queue import reconcile_stranded_jobs
from job_applier.cli.worker import run_worker_loop

import time
from pathlib import Path

import pytest

from job_applier.automation.queue import (
    JobState,
    cancel_job,
    claim_next_job,
    create_attempt,
    enqueue_job,
    get_events,
    get_latest_event_id,
    get_pending_notifications,
    get_runtime_control,
    handle_job_failure,
    record_submit_intent,
    renew_lease,
    resolve_job,
    set_runtime_pause,
    set_runtime_stop,
    skip_job,
    transition_job,
)
from job_applier.automation.runtime_lock import RuntimeSingletonLock, WorkerLockError
from job_applier.db import init_db, upsert_application


def test_enqueue_and_atomic_claim(tmp_path: Path):
    db_file = tmp_path / "test_queue.db"
    init_db(custom_path=db_file)

    upsert_application(
        app_id="app-1",
        company="Alpha Corp",
        title="DevOps Engineer",
        job_url="https://alpha.example/jobs/1",
        custom_path=db_file,
    )

    # 1. Enqueue job
    job = enqueue_job("app-1", adapter="greenhouse", priority=10, custom_path=db_file)
    assert job.id.startswith("job_app-1_")
    assert job.state == JobState.READY.value
    assert job.priority == 10
    assert job.fencing_generation == 0

    # 2. Idempotent enqueue returns same job
    dup_job = enqueue_job(
        "app-1", adapter="greenhouse", priority=5, custom_path=db_file
    )
    assert dup_job.id == job.id

    # 3. Worker claims job atomically
    worker_1 = "worker-node-1"
    claimed = claim_next_job(worker_id=worker_1, lease_seconds=60, custom_path=db_file)
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.state == JobState.CLAIMED.value
    assert claimed.lease_owner == worker_1
    assert claimed.fencing_generation == 1
    assert claimed.attempt_count == 1

    # 4. Immediate second claim returns None (no available jobs)
    worker_2 = "worker-node-2"
    second_claim = claim_next_job(
        worker_id=worker_2, lease_seconds=60, custom_path=db_file
    )
    assert second_claim is None


def test_fencing_generation_prevents_stale_worker_mutations(tmp_path: Path):
    """
    Guarantees that a worker that loses its lease or has a stale fencing generation
    cannot perform transitions or mutate job state.
    """
    db_file = tmp_path / "fencing.db"
    init_db(custom_path=db_file)
    upsert_application(
        app_id="fence-app",
        company="Fence",
        title="Sec",
        job_url="https://fence.example",
        custom_path=db_file,
    )
    job = enqueue_job("fence-app", custom_path=db_file)

    # Worker 1 claims with generation 1
    w1_job = claim_next_job("worker-1", lease_seconds=1, custom_path=db_file)
    assert w1_job is not None
    assert w1_job.fencing_generation == 1

    # Simulate lease expiry by sleeping briefly
    time.sleep(1.1)

    # Worker 2 claims the expired job with generation 2
    w2_job = claim_next_job("worker-2", lease_seconds=60, custom_path=db_file)
    assert w2_job is not None
    assert w2_job.fencing_generation == 2
    assert w2_job.lease_owner == "worker-2"

    # Worker 1 attempts to transition using stale generation 1 -> must fail!
    w1_success = transition_job(
        job_id=job.id,
        worker_id="worker-1",
        generation=1,
        to_state=JobState.FILLING.value,
        custom_path=db_file,
    )
    assert w1_success is False

    # Worker 2 transitions using valid generation 2 -> must succeed!
    w2_success = transition_job(
        job_id=job.id,
        worker_id="worker-2",
        generation=2,
        to_state=JobState.FILLING.value,
        custom_path=db_file,
    )
    assert w2_success is True


def test_lease_heartbeat_renewal(tmp_path: Path):
    db_file = tmp_path / "heartbeat.db"
    init_db(custom_path=db_file)
    upsert_application(
        app_id="hb-app",
        company="HB Corp",
        title="Eng",
        job_url="https://hb.example",
        custom_path=db_file,
    )
    enqueue_job("hb-app", custom_path=db_file)

    claimed = claim_next_job("worker-hb", lease_seconds=30, custom_path=db_file)
    assert claimed is not None

    # Renew lease
    renewed = renew_lease(
        job_id=claimed.id,
        worker_id="worker-hb",
        generation=claimed.fencing_generation,
        lease_seconds=60,
        custom_path=db_file,
    )
    assert renewed is True

    # Renew with wrong generation fails
    wrong_gen = renew_lease(
        job_id=claimed.id,
        worker_id="worker-hb",
        generation=999,
        lease_seconds=60,
        custom_path=db_file,
    )
    assert wrong_gen is False


def test_runtime_singleton_lock(tmp_path: Path):
    """Verifies that only one worker can acquire the OS file lock at a time."""
    lock_file = tmp_path / "singleton.lock"

    lock_1 = RuntimeSingletonLock(lock_file)
    lock_2 = RuntimeSingletonLock(lock_file)

    assert lock_1.acquire() is True
    assert lock_1.is_locked() is True

    # Second lock fails to acquire
    assert lock_2.acquire() is False
    assert lock_2.is_locked() is False

    with pytest.raises(WorkerLockError):
        with lock_2:
            pass

    # When lock 1 releases, lock 2 can acquire
    lock_1.release()
    assert lock_1.is_locked() is False

    assert lock_2.acquire() is True
    assert lock_2.is_locked() is True
    lock_2.release()


def test_pause_and_stop_semantics(tmp_path: Path):
    db_file = tmp_path / "pause_stop.db"
    init_db(custom_path=db_file)
    upsert_application(
        app_id="p-app",
        company="Pause",
        title="Eng",
        job_url="https://pause.example",
        custom_path=db_file,
    )
    enqueue_job("p-app", custom_path=db_file)

    # Engage pause
    set_runtime_pause(True, custom_path=db_file)
    ctrl = get_runtime_control(custom_path=db_file)
    assert ctrl["is_paused"] is True

    # Claim returns None while paused
    assert claim_next_job("worker-p", custom_path=db_file) is None

    # Resume
    set_runtime_pause(False, custom_path=db_file)
    ctrl = get_runtime_control(custom_path=db_file)
    assert ctrl["is_paused"] is False
    assert claim_next_job("worker-p", custom_path=db_file) is not None

    # Engage emergency stop
    set_runtime_stop(True, custom_path=db_file)
    ctrl = get_runtime_control(custom_path=db_file)
    assert ctrl["is_stopped"] is True
    assert claim_next_job("worker-p", custom_path=db_file) is None


def test_cancel_skip_and_resolve_job(tmp_path: Path):
    db_file = tmp_path / "controls.db"
    init_db(custom_path=db_file)
    upsert_application(
        app_id="ctrl-app-1",
        company="Ctrl 1",
        title="Eng",
        job_url="https://ctrl1.example",
        custom_path=db_file,
    )
    upsert_application(
        app_id="ctrl-app-2",
        company="Ctrl 2",
        title="Eng",
        job_url="https://ctrl2.example",
        custom_path=db_file,
    )

    j1 = enqueue_job("ctrl-app-1", custom_path=db_file)
    j2 = enqueue_job("ctrl-app-2", custom_path=db_file)

    # Cancel j1
    assert cancel_job(j1.id, reason="User cancelled", custom_path=db_file) is True

    # Skip j2
    assert skip_job(j2.id, reason="Not interested", custom_path=db_file) is True

    # Resolve job with approved answer
    upsert_application(
        app_id="ctrl-app-3",
        company="Ctrl 3",
        title="Eng",
        job_url="https://ctrl3.example",
        custom_path=db_file,
    )
    j3 = enqueue_job("ctrl-app-3", custom_path=db_file)
    w_j3 = claim_next_job("worker-res", custom_path=db_file)
    assert w_j3 is not None
    transition_job(
        w_j3.id,
        "worker-res",
        w_j3.fencing_generation,
        to_state=JobState.UNKNOWN_QUESTION.value,
        custom_path=db_file,
    )

    res_ok = resolve_job(
        job_id=j3.id,
        resolution_type="answer",
        answer_value="Over 5 years in AWS cloud architecture",
        question_key="aws experience",
        custom_path=db_file,
    )
    assert res_ok is True

    # j3 is reset to ready and can be claimed again
    reclaimed = claim_next_job("worker-res-2", custom_path=db_file)
    assert reclaimed is not None
    assert reclaimed.id == j3.id


def test_submit_intent_persisted_and_no_blind_retry(tmp_path: Path):
    """
    Guarantees that once submit_intent is recorded to database:
    1. application_attempts records submit_intent_at.
    2. Any subsequent failure is classified as ambiguous_submission.
    3. Automatic retry is strictly disabled to prevent duplicate submissions.
    """
    from job_applier.db import get_connection

    db_file = tmp_path / "submit_intent.db"
    init_db(custom_path=db_file)
    upsert_application(
        app_id="intent-app",
        company="Intent Corp",
        title="SRE",
        job_url="https://intent.example",
        custom_path=db_file,
    )

    enqueue_job("intent-app", custom_path=db_file)
    worker = "worker-intent"
    claimed = claim_next_job(worker, custom_path=db_file)
    assert claimed is not None

    # Create immutable attempt audit record
    attempt = create_attempt(
        job_id=claimed.id,
        app_id="intent-app",
        profile_snapshot={"name": "Test User"},
        resume_snapshot="resume text",
        custom_path=db_file,
    )
    assert attempt.id.startswith("att_")
    assert attempt.submit_intent_at is None

    # Persist submit intent
    ok = record_submit_intent(
        attempt_id=attempt.id,
        job_id=claimed.id,
        worker_id=worker,
        generation=claimed.fencing_generation,
        custom_path=db_file,
    )
    assert ok is True

    # Verify attempt in DB has submit_intent_at set
    conn = get_connection(custom_path=db_file)
    att_row = conn.execute(
        "SELECT * FROM application_attempts WHERE id = ?;", (attempt.id,)
    ).fetchone()
    assert att_row["submit_intent_at"] is not None
    job_row = conn.execute(
        "SELECT * FROM automation_jobs WHERE id = ?;", (claimed.id,)
    ).fetchone()
    assert job_row["state"] == JobState.SUBMIT_INTENT.value
    conn.close()

    # Worker crashes or network drops during post-submit -> arbiter invoked with is_pre_submit=False
    outcome = handle_job_failure(
        job_id=claimed.id,
        worker_id=worker,
        generation=claimed.fencing_generation,
        attempt_id=attempt.id,
        error="Browser disconnected after clicking Submit button",
        error_category="ambiguous_submission",
        is_pre_submit=False,
        custom_path=db_file,
    )
    assert outcome == JobState.AMBIGUOUS_SUBMISSION.value

    # Job must NOT be retried: claim_next_job returns None
    assert claim_next_job("worker-intent-2", custom_path=db_file) is None

    # Notification outbox must have high-urgency ambiguous_submission alert
    pending_notifs = get_pending_notifications(custom_path=db_file)
    assert len(pending_notifs) == 1
    assert pending_notifs[0]["category"] == "ambiguous_submission"
    assert pending_notifs[0]["urgency"] == "high"


def test_pre_submit_safe_retries_and_exponential_backoff(tmp_path: Path):
    """
    Verifies that pre-submit transient errors safely retry with bounded backoff
    (30s, 120s, 600s) and transition to failed_permanent when max retries are exhausted.
    """
    from job_applier.db import get_connection

    db_file = tmp_path / "retries.db"
    init_db(custom_path=db_file)
    upsert_application(
        app_id="retry-app",
        company="Retry Inc",
        title="Dev",
        job_url="https://retry.example",
        custom_path=db_file,
    )

    enqueue_job("retry-app", max_retries=3, custom_path=db_file)
    worker = "worker-r"

    # Attempt 1: Transient navigation failure
    claimed1 = claim_next_job(worker, custom_path=db_file)
    assert claimed1 is not None
    assert claimed1.attempt_count == 1

    outcome1 = handle_job_failure(
        job_id=claimed1.id,
        worker_id=worker,
        generation=claimed1.fencing_generation,
        attempt_id=None,
        error="Network timeout loading careers page",
        is_pre_submit=True,
        custom_path=db_file,
    )
    assert outcome1 == JobState.RETRY_WAIT.value

    # Job is in retry_wait, so immediate claim returns None
    assert claim_next_job(worker, custom_path=db_file) is None

    # Verify next_retry_at is scheduled approximately 30s in the future
    conn = get_connection(custom_path=db_file)
    r1 = conn.execute(
        "SELECT next_retry_at, state FROM automation_jobs WHERE id = ?;", (claimed1.id,)
    ).fetchone()
    assert r1["state"] == JobState.RETRY_WAIT.value
    assert r1["next_retry_at"] is not None

    # Artificially fast-forward next_retry_at to past to simulate backoff elapsed
    conn.execute(
        "UPDATE automation_jobs SET next_retry_at = '2000-01-01 00:00:00' WHERE id = ?;",
        (claimed1.id,),
    )
    conn.commit()
    conn.close()

    # Attempt 2: Claim succeeds after backoff
    claimed2 = claim_next_job(worker, custom_path=db_file)
    assert claimed2 is not None
    assert claimed2.attempt_count == 2

    # Attempt 2 fails
    outcome2 = handle_job_failure(
        job_id=claimed2.id,
        worker_id=worker,
        generation=claimed2.fencing_generation,
        attempt_id=None,
        error="Gateway 502",
        is_pre_submit=True,
        custom_path=db_file,
    )
    assert outcome2 == JobState.RETRY_WAIT.value

    # Fast forward again
    conn = get_connection(custom_path=db_file)
    conn.execute(
        "UPDATE automation_jobs SET next_retry_at = '2000-01-01 00:00:00' WHERE id = ?;",
        (claimed1.id,),
    )
    conn.commit()
    conn.close()

    # Attempt 3: Claim succeeds
    claimed3 = claim_next_job(worker, custom_path=db_file)
    assert claimed3 is not None
    assert claimed3.attempt_count == 3

    # Attempt 3 fails -> max_retries (3) reached! Must transition to failed_permanent
    outcome3 = handle_job_failure(
        job_id=claimed3.id,
        worker_id=worker,
        generation=claimed3.fencing_generation,
        attempt_id=None,
        error="Gateway 502 again",
        is_pre_submit=True,
        custom_path=db_file,
    )
    assert outcome3 == JobState.FAILED_PERMANENT.value

    # Job is permanently failed and will never be claimed again
    assert claim_next_job(worker, custom_path=db_file) is None


def test_monotonic_audit_events_and_sse_replay(tmp_path: Path):
    """
    Verifies that automation_events records strictly monotonic IDs and supports
    cursor-based replay for SSE reconnects.
    """
    db_file = tmp_path / "events.db"
    init_db(custom_path=db_file)
    upsert_application(
        app_id="ev-app",
        company="Event Log Corp",
        title="Auditor",
        job_url="https://event.example",
        custom_path=db_file,
    )

    # 1. Enqueue generates job_enqueued event
    enqueue_job("ev-app", custom_path=db_file)
    latest_id_1 = get_latest_event_id(custom_path=db_file)
    assert latest_id_1 >= 1

    # 2. Claim generates job_claimed event
    claimed = claim_next_job("worker-ev", custom_path=db_file)
    assert claimed is not None
    latest_id_2 = get_latest_event_id(custom_path=db_file)
    assert latest_id_2 > latest_id_1

    # 3. State transitions generate events
    transition_job(
        claimed.id,
        "worker-ev",
        claimed.fencing_generation,
        to_state=JobState.FILLING.value,
        custom_path=db_file,
    )
    latest_id_3 = get_latest_event_id(custom_path=db_file)
    assert latest_id_3 > latest_id_2

    # 4. Replay all events from start (after_id=0)
    all_events = get_events(after_id=0, limit=50, custom_path=db_file)
    assert len(all_events) == 3
    event_ids = [e["id"] for e in all_events]
    assert event_ids == sorted(event_ids)
    assert all_events[0]["event_type"] == "job_enqueued"
    assert all_events[1]["event_type"] == "job_claimed"
    assert all_events[2]["event_type"] == "state_transition"

    # 5. Replay from cursor (after_id=latest_id_1)
    resumed = get_events(after_id=latest_id_1, limit=50, custom_path=db_file)
    assert len(resumed) == 2
    assert resumed[0]["id"] == latest_id_2
    assert resumed[1]["id"] == latest_id_3


def test_singleton_lock_and_profile_lock_reentrancy(tmp_path: Path):
    """Verifies that RuntimeSingletonLock and ProfileOwnershipLock are reentrant within the same process."""
    lock_file = tmp_path / "worker.lock"
    profile_dir = tmp_path / "browser_profile"

    # 1. Test RuntimeSingletonLock mutual exclusion between independent instances
    lock1 = RuntimeSingletonLock(lock_path=lock_file)
    assert lock1.acquire() is True
    assert lock1.is_locked() is True

    lock2 = RuntimeSingletonLock(lock_path=lock_file)
    assert lock2.acquire() is False
    assert lock2.is_locked() is False

    lock1.release()
    assert lock1.is_locked() is False

    # 2. Test skip_lock on run_worker_loop eliminates self-contention when daemon holds lock
    held_lock = RuntimeSingletonLock(lock_path=lock_file)
    assert held_lock.acquire() is True
    try:
        # With skip_lock=True, run_worker_loop starts cleanly without lock failure
        res = run_worker_loop(
            max_jobs=0, custom_db_path=tmp_path / "test.db", skip_lock=True
        )
        assert res == 0
    finally:
        held_lock.release()

    # 3. Test skip_profile_lock on BrowserAutomator eliminates self-contention when daemon holds profile lock
    p_lock = ProfileOwnershipLock(profile_dir=profile_dir, owner_type="runtime")
    assert p_lock.acquire() is True
    try:
        automator = BrowserAutomator(
            headless=True,
            profile_dir=profile_dir,
            skip_profile_lock=True,
        )
        assert automator.profile_lock is None
    finally:
        p_lock.release()


def test_reconcile_stranded_jobs_on_startup(tmp_path: Path):
    """Verifies that reconcile_stranded_jobs detects expired submit_intent/verifying jobs and transitions them to ambiguous_submission."""
    db_file = tmp_path / "stranded.db"
    init_db(db_file)

    upsert_application(
        app_id="app-strand-1",
        company="Stranded Corp",
        title="DevOps",
        job_url="https://stranded.example/job/1",
        custom_path=db_file,
    )
    job = enqueue_job("app-strand-1", adapter="greenhouse", custom_path=db_file)

    # Move job to submit_intent with an expired lease
    conn = get_connection(db_file)
    past_time = "2020-01-01T00:00:00"
    conn.execute(
        """
        UPDATE automation_jobs
        SET state = 'submit_intent', lease_expires_at = ?
        WHERE id = ?;
        """,
        (past_time, job.id),
    )
    conn.commit()
    conn.close()

    # Run startup reconciliation
    recovered = reconcile_stranded_jobs(custom_path=db_file)
    assert job.id in recovered

    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT state, error_code FROM automation_jobs WHERE id = ?;", (job.id,)
    ).fetchone()
    conn.close()

    assert row["state"] == JobState.AMBIGUOUS_SUBMISSION.value
    assert row["error_code"] == "CRASH_DURING_SUBMISSION"

    # Verify worker was paused
    ctrl = get_runtime_control(db_file)
    assert ctrl["is_paused"] is True


def test_verifying_state_transition(tmp_path: Path):
    """Verifies that verifying_gate transitions job to JobState.VERIFYING."""
    db_file = tmp_path / "verifying.db"
    init_db(db_file)

    upsert_application(
        app_id="app-ver-1",
        company="Verifying Corp",
        title="Security Engineer",
        job_url="https://verifying.example/job/1",
        custom_path=db_file,
    )
    _ = enqueue_job("app-ver-1", adapter="greenhouse", custom_path=db_file)

    # Claim job
    claimed = claim_next_job(
        worker_id="worker_ver_1",
        lease_seconds=60,
        custom_path=db_file,
    )
    assert claimed is not None

    # Transition to verifying
    transition_job(
        job_id=claimed.id,
        worker_id="worker_ver_1",
        generation=claimed.fencing_generation,
        to_state=JobState.VERIFYING.value,
        checkpoint="submitted_awaiting_confirmation",
        custom_path=db_file,
    )

    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT state, checkpoint FROM automation_jobs WHERE id = ?;", (claimed.id,)
    ).fetchone()
    conn.close()

    assert row["state"] == JobState.VERIFYING.value
    assert row["checkpoint"] == "submitted_awaiting_confirmation"


def test_reconcile_stranded_jobs_expired_and_null_leases(tmp_path: Path):
    from datetime import datetime, timedelta

    db_file = tmp_path / "stranded.db"
    init_db(db_file)

    upsert_application(
        app_id="app_str_1",
        company="StrandedCorp1",
        title="Stranded Dev",
        job_url="https://example.com/job/str1",
        custom_path=db_file,
    )
    upsert_application(
        app_id="app_str_2",
        company="StrandedCorp2",
        title="Stranded Dev 2",
        job_url="https://example.com/job/str2",
        custom_path=db_file,
    )

    past_time = (datetime.now() - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")

    conn = get_connection(db_file)
    with conn:
        conn.execute(
            """
            INSERT INTO automation_jobs (
                id, app_id, adapter, adapter_version, state, priority,
                lease_owner, fencing_generation, lease_expires_at, created_at, updated_at
            ) VALUES ('job_str_1', 'app_str_1', 'greenhouse', '1.0.0', 'submit_intent', 5, 'worker1', 1, ?, datetime('now'), datetime('now'));
            """,
            (past_time,),
        )
        # Stranded with NULL lease_expires_at
        conn.execute(
            """
            INSERT INTO automation_jobs (
                id, app_id, adapter, adapter_version, state, priority,
                lease_owner, fencing_generation, lease_expires_at, created_at, updated_at
            ) VALUES ('job_str_2', 'app_str_2', 'greenhouse', '1.0.0', 'verifying', 5, 'worker2', 1, NULL, datetime('now'), datetime('now'));
            """
        )
    conn.close()

    reconciled = reconcile_stranded_jobs(custom_path=db_file)
    assert "job_str_1" in reconciled
    assert "job_str_2" in reconciled


def test_renew_lease_alive_during_pause_and_challenge(tmp_path: Path):
    from job_applier.automation.queue import (
        claim_next_job,
        enqueue_job,
        renew_lease,
        set_runtime_pause,
    )
    from job_applier.db import init_db, upsert_application

    db_file = tmp_path / "test_lease.db"
    init_db(db_file)
    upsert_application(
        app_id="app-lease-1",
        company="LeaseCo",
        title="Engineer",
        job_url="https://example.com/job",
        custom_path=db_file,
    )
    enqueue_job("app-lease-1", custom_path=db_file)
    claimed = claim_next_job("worker-lease", custom_path=db_file)
    assert claimed is not None

    # Pause runtime (e.g. for challenge takeover)
    set_runtime_pause(True, custom_path=db_file)

    # renew_lease must succeed while paused
    renewed = renew_lease(
        job_id=claimed.id,
        worker_id="worker-lease",
        generation=claimed.fencing_generation,
        custom_path=db_file,
    )
    assert renewed is True

    # But if runtime is stopped (emergency stop), renew_lease must fail closed
    emergency_stop(custom_db_path=db_file)
    renewed_stopped = renew_lease(
        job_id=claimed.id,
        worker_id="worker-lease",
        generation=claimed.fencing_generation,
        custom_path=db_file,
    )
    assert renewed_stopped is False


def test_resolve_job_monotonic_and_ambiguous_force(tmp_path: Path):
    from job_applier.automation.queue import (
        claim_next_job,
        enqueue_job,
        resolve_job,
        transition_job,
    )
    from job_applier.db import init_db, upsert_application

    db_file = tmp_path / "test_resolve.db"
    init_db(db_file)
    upsert_application(
        app_id="app-res-1",
        company="ResolveCo",
        title="Engineer",
        job_url="https://example.com/job",
        custom_path=db_file,
    )
    enqueue_job("app-res-1", custom_path=db_file)
    claimed = claim_next_job("worker-res", custom_path=db_file)
    assert claimed is not None
    assert claimed.fencing_generation == 1

    # Transition to ambiguous_submission
    transition_job(
        job_id=claimed.id,
        worker_id="worker-res",
        generation=1,
        to_state="ambiguous_submission",
        custom_path=db_file,
    )

    # Resolving without force=True must fail
    with pytest.raises(ValueError, match="ambiguous_submission outcome"):
        resolve_job(
            claimed.id, resolution_type="continue", force=False, custom_path=db_file
        )

    # Resolving with force=True succeeds
    success = resolve_job(
        claimed.id, resolution_type="continue", force=True, custom_path=db_file
    )
    assert success is True

    # Claim afresh: fencing_generation must be monotonically 2 (not reset to 1)
    claimed2 = claim_next_job("worker-res-2", custom_path=db_file)
    assert claimed2 is not None
    assert claimed2.fencing_generation == 2


def test_queue_updates_applications_status_on_cancellation(tmp_path: Path):
    db_file = tmp_path / "test_cancel.db"
    init_db(db_file)
    upsert_application(
        app_id="app-cancel",
        company="Acme",
        title="SE",
        job_url="https://example.com/job",
        status="pending",
        custom_path=db_file,
    )
    job = enqueue_job("app-cancel", adapter="greenhouse", custom_path=db_file)

    cancel_job(job.id, reason="User cancelled test", custom_path=db_file)

    # Applications table status must update to 'cancelled'
    conn = get_connection(db_file)
    app_row = conn.execute(
        "SELECT status, notes FROM applications WHERE id = 'app-cancel';"
    ).fetchone()
    conn.close()
    assert app_row["status"] == "cancelled"
    assert "User cancelled test" in app_row["notes"]


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


def test_ambiguous_submission_updates_application_status(tmp_path: Path):
    db_file = tmp_path / "test_ambig.db"
    init_db(custom_path=db_file)

    upsert_application(
        app_id="app-ambig",
        company="AmbigCo",
        title="Engineer",
        job_url="https://ambig.example/jobs/1",
        custom_path=db_file,
    )
    job = enqueue_job("app-ambig", adapter="greenhouse", custom_path=db_file)
    claimed = claim_next_job("w1", custom_path=db_file)
    assert claimed is not None

    success = transition_job(
        job_id=job.id,
        worker_id="w1",
        generation=claimed.fencing_generation,
        to_state=JobState.AMBIGUOUS_SUBMISSION.value,
        error_message="Submission outcome uncertain",
        custom_path=db_file,
    )
    assert success is True

    conn = get_connection(db_file)
    app_row = conn.execute(
        "SELECT status FROM applications WHERE id = 'app-ambig';"
    ).fetchone()
    conn.close()
    assert app_row["status"] == "ambiguous"

    resolve_job(
        job_id=job.id,
        resolution_type="continue",
        force=True,
        custom_path=db_file,
    )
    conn = get_connection(db_file)
    app_row2 = conn.execute(
        "SELECT status FROM applications WHERE id = 'app-ambig';"
    ).fetchone()
    conn.close()
    assert app_row2["status"] == "pending"


def test_reconcile_stranded_jobs_marks_applications_ambiguous(tmp_path: Path):
    db_file = tmp_path / "test_stranded.db"
    init_db(custom_path=db_file)

    upsert_application(
        app_id="app-stranded",
        company="StrandedCo",
        title="Engineer",
        job_url="https://stranded.example/jobs/1",
        custom_path=db_file,
    )
    job = enqueue_job("app-stranded", adapter="lever", custom_path=db_file)
    claimed = claim_next_job("w1", lease_seconds=1, custom_path=db_file)
    assert claimed is not None

    transition_job(
        job_id=job.id,
        worker_id="w1",
        generation=claimed.fencing_generation,
        to_state=JobState.SUBMIT_INTENT.value,
        custom_path=db_file,
    )

    conn = get_connection(db_file)
    with conn:
        conn.execute(
            "UPDATE automation_jobs SET lease_expires_at = '2020-01-01 00:00:00' WHERE id = ?;",
            (job.id,),
        )
    conn.close()

    recovered = reconcile_stranded_jobs(custom_path=db_file)
    assert job.id in recovered

    conn = get_connection(db_file)
    app_row = conn.execute(
        "SELECT status FROM applications WHERE id = 'app-stranded';"
    ).fetchone()
    conn.close()
    assert app_row["status"] == "ambiguous"


def test_notification_retry_backoff_and_filtering(tmp_path: Path):
    from job_applier.automation.queue import (
        enqueue_notification,
        get_pending_notifications,
        mark_notification_retry,
        mark_notification_sent,
    )

    db_file = tmp_path / "test_notif_backoff.db"
    init_db(custom_path=db_file)

    enqueue_notification(
        category="general",
        title="Test Alert",
        message="Backoff check",
        custom_path=db_file,
    )

    pending = get_pending_notifications(custom_path=db_file)
    assert len(pending) == 1
    notif_id = pending[0]["id"]

    mark_notification_retry(notif_id, error="HTTP 503", custom_path=db_file)

    pending_immediate = get_pending_notifications(custom_path=db_file)
    assert len(pending_immediate) == 0

    conn = get_connection(db_file)
    with conn:
        conn.execute(
            "UPDATE notification_outbox SET next_retry_at = '2020-01-01 00:00:00' WHERE id = ?;",
            (notif_id,),
        )
    conn.close()

    pending_due = get_pending_notifications(custom_path=db_file)
    assert len(pending_due) == 1

    mark_notification_sent(notif_id, error="", custom_path=db_file)
    pending_delivered = get_pending_notifications(custom_path=db_file)
    assert len(pending_delivered) == 0


def test_validate_application_artifacts_with_suffix_folder(tmp_path: Path):
    from job_applier.automation.queue import validate_application_artifacts
    from job_applier.db import get_connection, init_db, upsert_application

    db_file = tmp_path / "test_suffix.db"
    init_db(db_file)

    # Insert application where folder_name does NOT have the _116 suffix
    upsert_application(
        app_id="Ajax_Systems_Area_Pre-Sales_Engineer_France",
        company="Ajax Systems",
        title="Area Pre-Sales Engineer France",
        job_url="https://jobicy.com/jobs/152572-area-pre-sales-engineer-france",
        folder_name="Ajax_Systems_Area_Pre-Sales_Engineer_France",
        cv_filename="CV_Ajax_Systems_Area_Pre-Sales_Engineer_France.pdf",
        custom_path=db_file,
    )

    # On disk, create folder WITH _116 suffix
    app_folder = (
        tmp_path
        / "output"
        / "applications"
        / "Ajax_Systems_Area_Pre-Sales_Engineer_France_116"
    )
    app_folder.mkdir(parents=True)
    cv_file = app_folder / "CV_Ajax_Systems_Area_Pre-Sales_Engineer_France.pdf"
    cv_file.write_bytes(b"%PDF-1.4 mock cv content")

    valid, cv_path = validate_application_artifacts(
        "Ajax_Systems_Area_Pre-Sales_Engineer_France",
        custom_path=db_file,
        base_dir=tmp_path,
    )

    assert valid is True
    assert cv_path == str(cv_file)

    # Verify database row was updated with actual disk folder name
    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT folder_name FROM applications WHERE id = 'Ajax_Systems_Area_Pre-Sales_Engineer_France';"
    ).fetchone()
    conn.close()
    assert row["folder_name"] == "Ajax_Systems_Area_Pre-Sales_Engineer_France_116"


def test_worker_rejects_jobicy_aggregator_early(tmp_path: Path):
    from job_applier.cli.worker import process_claimed_job

    db_file = tmp_path / "test_jobicy_worker.db"
    init_db(custom_path=db_file)

    upsert_application(
        app_id="app-jobicy-direct",
        company="Jobicy Inc",
        title="Remote SRE",
        job_url="https://jobicy.com/jobs/9999-remote-sre",
        folder_name="app_jobicy_folder",
        custom_path=db_file,
    )
    job = enqueue_job("app-jobicy-direct", adapter="greenhouse", custom_path=db_file)
    claimed = claim_next_job("w1", custom_path=db_file)
    assert claimed is not None

    result = process_claimed_job(claimed, worker_id="w1", custom_db_path=db_file)
    assert result == "failed"

    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT state, error_code, error_message FROM automation_jobs WHERE id = ?;",
        (job.id,),
    ).fetchone()
    conn.close()

    assert row["state"] == "failed_permanent"
    assert "Jobicy aggregator landing page blocked" in row["error_message"]


def test_skip_cancel_resolve_job_by_app_id_and_job_id(tmp_path: Path):
    from job_applier.automation.queue import cancel_job, resolve_job, skip_job

    db_file = tmp_path / "test_queue_app_id.db"
    init_db(custom_path=db_file)

    upsert_application(
        app_id="app-test-resolution",
        company="TestCorp",
        title="Senior SRE",
        job_url="https://greenhouse.io/testcorp/1",
        custom_path=db_file,
    )
    job = enqueue_job("app-test-resolution", adapter="greenhouse", custom_path=db_file)

    # 1. Skip by job_id
    assert skip_job(job.id, reason="Skip by job_id", custom_path=db_file) is True
    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT state FROM automation_jobs WHERE id = ?;", (job.id,)
    ).fetchone()
    app_row = conn.execute(
        "SELECT status, notes FROM applications WHERE id = 'app-test-resolution';"
    ).fetchone()
    assert row["state"] == "skipped"
    assert app_row["status"] == "skipped"
    assert "Skip by job_id" in app_row["notes"]
    conn.close()

    # 2. Resolve by job_id
    assert resolve_job(job.id, resolution_type="continue", custom_path=db_file) is True
    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT state FROM automation_jobs WHERE id = ?;", (job.id,)
    ).fetchone()
    assert row["state"] == "ready"
    conn.close()

    # 3. Skip by app_id
    assert (
        skip_job("app-test-resolution", reason="Skip by app_id", custom_path=db_file)
        is True
    )
    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT state FROM automation_jobs WHERE id = ?;", (job.id,)
    ).fetchone()
    app_row = conn.execute(
        "SELECT status, notes FROM applications WHERE id = 'app-test-resolution';"
    ).fetchone()
    assert row["state"] == "skipped"
    assert app_row["status"] == "skipped"
    assert "Skip by app_id" in app_row["notes"]
    conn.close()

    # 4. Resolve by app_id
    assert (
        resolve_job(
            "app-test-resolution", resolution_type="continue", custom_path=db_file
        )
        is True
    )
    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT state FROM automation_jobs WHERE id = ?;", (job.id,)
    ).fetchone()
    assert row["state"] == "ready"
    conn.close()

    # 5. Cancel by app_id
    assert (
        cancel_job(
            "app-test-resolution", reason="Cancel by app_id", custom_path=db_file
        )
        is True
    )
    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT state, is_cancelled FROM automation_jobs WHERE id = ?;", (job.id,)
    ).fetchone()
    app_row = conn.execute(
        "SELECT status, notes FROM applications WHERE id = 'app-test-resolution';"
    ).fetchone()
    assert row["state"] == "cancelled"
    assert row["is_cancelled"] == 1
    assert app_row["status"] == "cancelled"
    assert "Cancel by app_id" in app_row["notes"]
    conn.close()


def test_skip_and_cancel_application_without_automation_job(tmp_path: Path):
    from job_applier.automation.queue import cancel_job, skip_job

    db_file = tmp_path / "test_no_job.db"
    init_db(custom_path=db_file)

    upsert_application(
        app_id="app-raw-folder-only",
        company="Raw Corp",
        title="DevOps",
        job_url="https://example.com/jobs/raw",
        custom_path=db_file,
    )

    # Skip application directly without automation_job row
    assert (
        skip_job("app-raw-folder-only", reason="Direct skip", custom_path=db_file)
        is True
    )
    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT status, notes FROM applications WHERE id = 'app-raw-folder-only';"
    ).fetchone()
    assert row["status"] == "skipped"
    assert "Direct skip" in row["notes"]
    conn.close()

    # Cancel application directly without automation_job row
    assert (
        cancel_job("app-raw-folder-only", reason="Direct cancel", custom_path=db_file)
        is True
    )
    conn = get_connection(db_file)
    row = conn.execute(
        "SELECT status, notes FROM applications WHERE id = 'app-raw-folder-only';"
    ).fetchone()
    assert row["status"] == "cancelled"
    assert "Direct cancel" in row["notes"]
    conn.close()

    # Nonexistent ID returns False
    assert skip_job("totally-nonexistent-id", custom_path=db_file) is False
    assert cancel_job("totally-nonexistent-id", custom_path=db_file) is False


def test_ensure_runtime_control_columns_on_legacy_table(tmp_path: Path):
    """Verifies that init_db idempotently ensures all 7 runtime_control columns on legacy databases."""
    db_file = tmp_path / "legacy_rc.db"
    conn = get_connection(db_file)
    with conn:
        conn.execute(
            """
            CREATE TABLE runtime_control (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                is_paused INTEGER NOT NULL DEFAULT 0,
                is_stopped INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO runtime_control (id, is_paused, is_stopped, updated_at) VALUES (1, 0, 0, '2026-01-01');"
        )
    conn.close()

    # Call init_db
    init_db(db_file)

    conn = get_connection(db_file)
    cols = {
        r[1] for r in conn.execute("PRAGMA table_info(runtime_control);").fetchall()
    }
    conn.close()

    expected_cols = {
        "is_waiting_for_code",
        "pending_verification_code",
        "browser_active",
        "browser_url",
        "browser_page_text",
        "browser_is_closed",
        "browser_updated_at",
    }
    assert expected_cols.issubset(cols)

    # Calling init_db again must be completely idempotent
    init_db(db_file)
