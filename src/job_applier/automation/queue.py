from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from job_applier.db import get_connection, init_db
from job_applier.utils import get_project_root

logger = logging.getLogger("job_applier.queue")
_QUEUE_LOCK = threading.RLock()


class JobState(str, Enum):
    READY = "ready"
    CLAIMED = "claimed"
    NAVIGATING = "navigating"
    FILLING = "filling"
    VALIDATING = "validating"
    SUBMIT_INTENT = "submit_intent"
    VERIFYING = "verifying"
    APPLIED = "applied"
    AUTH_REQUIRED = "auth_required"
    MFA_REQUIRED = "mfa_required"
    CAPTCHA_REQUIRED = "captcha_required"
    UNKNOWN_QUESTION = "unknown_question"
    SITE_CHANGED = "site_changed"
    AMBIGUOUS_SUBMISSION = "ambiguous_submission"
    RETRY_WAIT = "retry_wait"
    CANCELLED = "cancelled"
    FAILED_PERMANENT = "failed_permanent"
    SKIPPED = "skipped"


ACTIVE_STATES = {
    JobState.READY.value,
    JobState.CLAIMED.value,
    JobState.NAVIGATING.value,
    JobState.FILLING.value,
    JobState.VALIDATING.value,
    JobState.SUBMIT_INTENT.value,
    JobState.VERIFYING.value,
    JobState.RETRY_WAIT.value,
    JobState.AUTH_REQUIRED.value,
    JobState.MFA_REQUIRED.value,
    JobState.CAPTCHA_REQUIRED.value,
    JobState.UNKNOWN_QUESTION.value,
    JobState.SITE_CHANGED.value,
    JobState.AMBIGUOUS_SUBMISSION.value,
}

TERMINAL_STATES = {
    JobState.APPLIED.value,
    JobState.CANCELLED.value,
    JobState.FAILED_PERMANENT.value,
    JobState.SKIPPED.value,
}

EXCEPTION_STATES = {
    JobState.AUTH_REQUIRED.value,
    JobState.MFA_REQUIRED.value,
    JobState.CAPTCHA_REQUIRED.value,
    JobState.UNKNOWN_QUESTION.value,
    JobState.SITE_CHANGED.value,
    JobState.AMBIGUOUS_SUBMISSION.value,
}


@dataclass
class AutomationJob:
    id: str
    app_id: str
    adapter: str
    adapter_version: str
    state: str
    priority: int
    lease_owner: str | None
    lease_expires_at: str | None
    fencing_generation: int
    checkpoint: str | None
    attempt_count: int
    max_retries: int
    next_retry_at: str | None
    error_code: str | None
    error_message: str | None
    is_cancelled: bool
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApplicationAttempt:
    id: str
    job_id: str
    app_id: str
    attempt_number: int
    profile_snapshot: dict[str, Any]
    resume_snapshot: str
    artifact_revisions: dict[str, Any]
    submit_intent_at: str | None
    outcome: str | None
    confirmation_evidence: str | None
    error_details: str | None
    is_ambiguous: bool
    created_at: str
    completed_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _row_to_job(row: sqlite3.Row) -> AutomationJob:
    return AutomationJob(
        id=row["id"],
        app_id=row["app_id"],
        adapter=row["adapter"],
        adapter_version=row["adapter_version"],
        state=row["state"],
        priority=row["priority"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        fencing_generation=row["fencing_generation"],
        checkpoint=row["checkpoint"],
        attempt_count=row["attempt_count"],
        max_retries=row["max_retries"],
        next_retry_at=row["next_retry_at"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        is_cancelled=bool(row["is_cancelled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def validate_application_artifacts(
    app_id: str,
    custom_path: Path | None = None,
    base_dir: Path | None = None,
) -> tuple[bool, str]:
    """
    Verifies that the application's required CV PDF artifact exists on disk and is non-empty.
    Returns (True, path_str) if valid, or (False, reason) if missing or empty.
    """
    conn = get_connection(custom_path)
    try:
        row = conn.execute(
            "SELECT folder_name, cv_filename FROM applications WHERE id = ?;",
            (app_id,),
        ).fetchone()
        if not row:
            return False, f"Application {app_id} not found in database"

        folder_name = row["folder_name"] or app_id

        if base_dir is not None:
            project_root = Path(base_dir)
        elif custom_path is not None:
            p_parent = Path(custom_path).parent
            if (p_parent / "output").exists() or (p_parent / "applications").exists():
                project_root = p_parent
            else:
                from job_applier.utils import get_project_root

                project_root = get_project_root()
        else:
            from job_applier.utils import get_project_root

            project_root = get_project_root()

        app_dir = project_root / "output" / "applications" / folder_name
        if not app_dir.exists():
            app_dir = project_root / "output" / "applied" / folder_name
        if not app_dir.exists():
            app_dir = project_root / "applications" / folder_name

        if not app_dir.exists():
            # Search with glob(f"{folder_name}*") across candidate parent roots
            base_id = re.sub(r"_\d+$", "", folder_name)
            candidate_dirs: list[Path] = []
            for parent in [
                project_root / "output" / "applications",
                project_root / "output" / "applied",
                project_root / "applications",
            ]:
                if parent.exists():
                    matches = [d for d in parent.glob(f"{folder_name}*") if d.is_dir()]
                    if not matches and base_id != folder_name:
                        matches = [d for d in parent.glob(f"{base_id}*") if d.is_dir()]
                    matches.sort(key=lambda p: (len(p.name), p.name))
                    candidate_dirs.extend(matches)
            if candidate_dirs:
                app_dir = candidate_dirs[0]

        if not app_dir.exists():
            return False, f"Application directory missing: {app_dir}"

        recorded_name = row["cv_filename"] if "cv_filename" in row.keys() else None
        cv_pdf = None
        if recorded_name and (app_dir / recorded_name).exists():
            cv_pdf = app_dir / recorded_name
        else:
            cv_pdf = next(app_dir.glob("CV_*.pdf"), None)
            if not cv_pdf or not cv_pdf.exists():
                fallback_cv = app_dir / "CV.pdf"
                if fallback_cv.exists():
                    cv_pdf = fallback_cv

        if not cv_pdf or not cv_pdf.exists() or cv_pdf.stat().st_size == 0:
            return False, f"CV PDF artifact missing or empty in {app_dir}"

        # If actual folder on disk differs from recorded folder_name, update applications table
        if app_dir.name != folder_name:
            with _QUEUE_LOCK:
                try:
                    conn.execute(
                        "UPDATE applications SET folder_name = ? WHERE id = ?;",
                        (app_dir.name, app_id),
                    )
                    conn.commit()
                except Exception:
                    pass

        return True, str(cv_pdf)
    finally:
        conn.close()


def enqueue_job(
    app_id: str,
    adapter: str = "generic",
    priority: int = 0,
    max_retries: int = 3,
    custom_path: Path | None = None,
    verify_artifacts: bool = False,
    base_dir: Path | None = None,
) -> AutomationJob:
    """
    Enqueues an application into the automation queue.
    Idempotent: If an active job already exists for this app_id, returns it.
    If verify_artifacts=True, missing CV artifacts block enqueueing without deleting application.
    """
    init_db(custom_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if verify_artifacts:
        from job_applier.automation.safety_guard import MissingArtifactError

        has_art, reason = validate_application_artifacts(
            app_id, custom_path=custom_path, base_dir=base_dir
        )
        if not has_art:
            conn = get_connection(custom_path)
            try:
                with conn:
                    conn.execute(
                        "UPDATE applications SET has_artifacts = 0, status = 'missing_artifacts', updated_at = ? WHERE id = ?;",
                        (now, app_id),
                    )
            finally:
                conn.close()

            record_event(
                job_id=None,
                app_id=app_id,
                event_type="artifacts_missing",
                level="WARN",
                step="enqueue_blocked",
                message=f"Enqueueing blocked: {reason}",
                custom_path=custom_path,
            )
            raise MissingArtifactError(reason)

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                # Check existing active job
                existing = conn.execute(
                    """
                    SELECT * FROM automation_jobs
                    WHERE app_id = ? AND state NOT IN ('applied', 'cancelled', 'failed_permanent', 'skipped')
                    ORDER BY created_at DESC LIMIT 1;
                    """,
                    (app_id,),
                ).fetchone()

                if existing:
                    return _row_to_job(existing)

                job_id = f"job_{app_id}_{uuid.uuid4().hex[:8]}"
                conn.execute(
                    """
                    INSERT INTO automation_jobs (
                        id, app_id, adapter, adapter_version, state, priority,
                        fencing_generation, attempt_count, max_retries, is_cancelled,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, '1.0.0', 'ready', ?, 0, 0, ?, 0, ?, ?);
                    """,
                    (job_id, app_id, adapter, priority, max_retries, now, now),
                )

                row = conn.execute(
                    "SELECT * FROM automation_jobs WHERE id = ?;", (job_id,)
                ).fetchone()

                record_event(
                    job_id=job_id,
                    app_id=app_id,
                    event_type="job_enqueued",
                    level="INFO",
                    step="enqueue",
                    message=f"Application {app_id} enqueued for processing.",
                    custom_path=custom_path,
                    conn=conn,
                )

                return _row_to_job(row)
        finally:
            conn.close()


def claim_next_job(
    worker_id: str,
    lease_seconds: int = 60,
    custom_path: Path | None = None,
) -> AutomationJob | None:
    """
    Atomically claims the highest priority ready/expired job for processing.
    Employs SQLite BEGIN IMMEDIATE and increments fencing_generation.
    Fails closed if the automation system is paused or stopped.
    """
    init_db(custom_path)
    now_dt = datetime.now()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    lease_exp_str = (now_dt + timedelta(seconds=lease_seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            conn.execute("BEGIN IMMEDIATE;")
            # 1. Check runtime control (pause/stop)
            ctrl = conn.execute(
                "SELECT is_paused, is_stopped FROM runtime_control WHERE id = 1;"
            ).fetchone()
            if ctrl and (ctrl["is_paused"] or ctrl["is_stopped"]):
                conn.execute("ROLLBACK;")
                return None

            # 2. Find eligible job:
            # - ready
            # - retry_wait where next_retry_at is passed
            # - claimed/navigating/filling/validating where lease has expired
            query = """
                SELECT j.* FROM automation_jobs j
                JOIN applications a ON a.id = j.app_id
                WHERE j.is_cancelled = 0
                  AND LOWER(a.status) != 'dismissed'
                  AND (
                    j.state = 'ready'
                    OR (j.state = 'retry_wait' AND (j.next_retry_at IS NULL OR j.next_retry_at <= ?))
                    OR (j.state IN ('claimed', 'navigating', 'filling', 'validating') AND j.lease_expires_at IS NOT NULL AND j.lease_expires_at <= ?)
                  )
                ORDER BY j.priority DESC, j.created_at ASC
                LIMIT 1;
            """
            job_row = conn.execute(query, (now_str, now_str)).fetchone()
            if not job_row:
                conn.execute("COMMIT;")
                return None

            job_id = job_row["id"]
            new_gen = job_row["fencing_generation"] + 1
            new_attempt = job_row["attempt_count"] + 1

            conn.execute(
                """
                UPDATE automation_jobs
                SET state = 'claimed',
                    lease_owner = ?,
                    lease_expires_at = ?,
                    fencing_generation = ?,
                    attempt_count = ?,
                    updated_at = ?
                WHERE id = ?;
                """,
                (worker_id, lease_exp_str, new_gen, new_attempt, now_str, job_id),
            )
            record_event(
                job_id=job_id,
                app_id=job_row["app_id"],
                event_type="job_claimed",
                level="INFO",
                step="claim",
                message=f"Job claimed by worker {worker_id} (lease: {lease_seconds}s, gen: {new_gen}).",
                details={
                    "worker_id": worker_id,
                    "generation": new_gen,
                    "lease_expires_at": lease_exp_str,
                },
                worker_id=worker_id,
                lease_generation=new_gen,
                custom_path=custom_path,
                conn=conn,
            )
            conn.execute("COMMIT;")

            updated_row = conn.execute(
                "SELECT * FROM automation_jobs WHERE id = ?;", (job_id,)
            ).fetchone()
            return _row_to_job(updated_row)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()


def renew_lease(
    job_id: str,
    worker_id: str,
    generation: int,
    lease_seconds: int = 60,
    custom_path: Path | None = None,
) -> bool:
    """
    Heartbeat mechanism: Renews the lease for an active worker.
    Fails closed if the worker has lost ownership or if the system is paused/stopped.
    """
    now_dt = datetime.now()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    lease_exp_str = (now_dt + timedelta(seconds=lease_seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                ctrl = conn.execute(
                    "SELECT is_paused, is_stopped FROM runtime_control WHERE id = 1;"
                ).fetchone()
                if ctrl and ctrl["is_stopped"]:
                    return False

                cursor = conn.execute(
                    """
                    UPDATE automation_jobs
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE id = ? AND lease_owner = ? AND fencing_generation = ? AND is_cancelled = 0;
                    """,
                    (lease_exp_str, now_str, job_id, worker_id, generation),
                )
                return cursor.rowcount > 0
        finally:
            conn.close()


def transition_job(
    job_id: str,
    worker_id: str,
    generation: int,
    to_state: str,
    checkpoint: str = "",
    error_code: str = "",
    error_message: str = "",
    custom_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> bool:
    """Guarded state transition requiring matching lease owner and fencing generation."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _execute(active_conn: sqlite3.Connection) -> bool:
        previous_row = active_conn.execute(
            "SELECT state FROM automation_jobs WHERE id = ? AND lease_owner = ? AND fencing_generation = ?;",
            (job_id, worker_id, generation),
        ).fetchone()
        cursor = active_conn.execute(
            """
            UPDATE automation_jobs
            SET state = ?,
                checkpoint = CASE WHEN ? != '' THEN ? ELSE checkpoint END,
                error_code = CASE WHEN ? != '' THEN ? ELSE error_code END,
                error_message = CASE WHEN ? != '' THEN ? ELSE error_message END,
                updated_at = ?
            WHERE id = ? AND lease_owner = ? AND fencing_generation = ? AND is_cancelled = 0;
            """,
            (
                to_state,
                checkpoint,
                checkpoint,
                error_code,
                error_code,
                error_message,
                error_message,
                now,
                job_id,
                worker_id,
                generation,
            ),
        )
        success = cursor.rowcount > 0
        if success:
            row = active_conn.execute(
                "SELECT app_id FROM automation_jobs WHERE id = ?;", (job_id,)
            ).fetchone()
            app_id = row["app_id"] if row else ""
            if app_id:
                if to_state == JobState.FAILED_PERMANENT.value:
                    active_conn.execute(
                        "UPDATE applications SET status = 'failed', notes = CASE WHEN ? != '' THEN ? ELSE notes END, updated_at = ? WHERE id = ?;",
                        (error_message, error_message, now, app_id),
                    )
                elif to_state == JobState.CANCELLED.value:
                    active_conn.execute(
                        "UPDATE applications SET status = 'cancelled', notes = CASE WHEN ? != '' THEN ? ELSE notes END, updated_at = ? WHERE id = ?;",
                        (error_message, error_message, now, app_id),
                    )
                elif to_state == JobState.AMBIGUOUS_SUBMISSION.value:
                    active_conn.execute(
                        "UPDATE applications SET status = 'ambiguous', notes = CASE WHEN ? != '' THEN ? ELSE notes END, updated_at = ? WHERE id = ?;",
                        (error_message, error_message, now, app_id),
                    )
            record_event(
                job_id=job_id,
                app_id=app_id,
                event_type="state_transition",
                level="INFO" if to_state not in EXCEPTION_STATES else "WARN",
                step=to_state,
                message=f"Job state transitioned to {to_state}.",
                details={
                    "checkpoint": checkpoint,
                    "error_code": error_code,
                    "error_message": error_message,
                    "to_state": to_state,
                    "outcome": to_state if to_state in TERMINAL_STATES else None,
                },
                worker_id=worker_id,
                lease_generation=generation,
                from_state=previous_row["state"] if previous_row else None,
                to_state=to_state,
                outcome_code=to_state if to_state in TERMINAL_STATES else None,
                custom_path=custom_path,
                conn=active_conn,
            )
        return success

    with _QUEUE_LOCK:
        if conn is not None:
            return _execute(conn)
        active_conn = get_connection(custom_path)
        try:
            with active_conn:
                return _execute(active_conn)
        finally:
            active_conn.close()


def create_attempt(
    job_id: str,
    app_id: str,
    profile_snapshot: dict[str, Any] | None = None,
    resume_snapshot: str = "",
    artifact_revisions: dict[str, Any] | None = None,
    custom_path: Path | None = None,
    *,
    worker_id: str | None = None,
    lease_generation: int | None = None,
    browser_job_id: str | None = None,
) -> ApplicationAttempt:
    """Creates an immutable attempt audit record prior to execution steps."""
    attempt_id = f"att_{job_id}_{uuid.uuid4().hex[:6]}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prof_json = json.dumps(profile_snapshot or {})
    art_json = json.dumps(artifact_revisions or {})

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                # BEGIN IMMEDIATE serializes writers across processes, while the
                # unique index added by migration 5 is the final race guard.
                conn.execute("BEGIN IMMEDIATE;")
                attempt_num = conn.execute(
                    "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM application_attempts WHERE job_id = ?;",
                    (job_id,),
                ).fetchone()[0]

                conn.execute(
                    """
                    INSERT INTO application_attempts (
                        id, job_id, app_id, attempt_number, profile_snapshot,
                        resume_snapshot, artifact_revisions, started_at, worker_id,
                        lease_generation, browser_job_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        attempt_id,
                        job_id,
                        app_id,
                        attempt_num,
                        prof_json,
                        resume_snapshot,
                        art_json,
                        now,
                        worker_id,
                        lease_generation,
                        browser_job_id,
                        now,
                    ),
                )

                return ApplicationAttempt(
                    id=attempt_id,
                    job_id=job_id,
                    app_id=app_id,
                    attempt_number=attempt_num,
                    profile_snapshot=profile_snapshot or {},
                    resume_snapshot=resume_snapshot,
                    artifact_revisions=artifact_revisions or {},
                    submit_intent_at=None,
                    outcome=None,
                    confirmation_evidence=None,
                    error_details=None,
                    is_ambiguous=False,
                    created_at=now,
                    completed_at=None,
                )
        finally:
            conn.close()


def record_submit_intent(
    attempt_id: str,
    job_id: str,
    worker_id: str,
    generation: int,
    custom_path: Path | None = None,
) -> bool:
    """
    DURABLE SAFETY GATE: Persists submit intent to disk BEFORE the browser clicks submit.
    If the worker crashes or loses network connectivity after this point, it CANNOT be blindly retried.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                job_ok = transition_job(
                    job_id=job_id,
                    worker_id=worker_id,
                    generation=generation,
                    to_state=JobState.SUBMIT_INTENT.value,
                    checkpoint="submit_intent_persisted",
                    custom_path=custom_path,
                    conn=conn,
                )
                if not job_ok:
                    return False

                conn.execute(
                    "UPDATE application_attempts SET submit_intent_at = ? WHERE id = ?;",
                    (now, attempt_id),
                )
                app_row = conn.execute(
                    "SELECT app_id FROM automation_jobs WHERE id = ?;", (job_id,)
                ).fetchone()
                record_event(
                    job_id=job_id,
                    app_id=app_row["app_id"] if app_row else None,
                    attempt_id=attempt_id,
                    event_key="submit_intent_persisted",
                    worker_id=worker_id,
                    lease_generation=generation,
                    event_type="submit_intent_persisted",
                    level="INFO",
                    step="submit_intent",
                    message="Pre-submission intent committed to database. Blind retry is now disabled.",
                    custom_path=custom_path,
                    conn=conn,
                )
                return True
        finally:
            conn.close()


def complete_attempt(
    attempt_id: str,
    outcome: str,
    confirmation_evidence: str = "",
    error_details: str = "",
    is_ambiguous: bool = False,
    custom_path: Path | None = None,
) -> bool:
    """Records the final audit outcome of an application attempt."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                safe_error = (
                    _sanitize_event_message(error_details, "attempt_error")
                    if error_details
                    else ""
                )
                cursor = conn.execute(
                    """
                    UPDATE application_attempts
                    SET outcome = ?,
                        confirmation_evidence = ?,
                        error_details = ?,
                        redacted_error_code = CASE WHEN ? != '' THEN ? ELSE redacted_error_code END,
                        is_ambiguous = ?,
                        completed_at = ?
                    WHERE id = ?;
                    """,
                    (
                        outcome,
                        confirmation_evidence[:512],
                        safe_error,
                        outcome,
                        outcome,
                        1 if is_ambiguous else 0,
                        now,
                        attempt_id,
                    ),
                )
                if cursor.rowcount:
                    row = conn.execute(
                        "SELECT job_id, app_id, attempt_number FROM application_attempts WHERE id = ?;",
                        (attempt_id,),
                    ).fetchone()
                    if row:
                        conn.execute(
                            "UPDATE automation_job_status SET attempt_id = ?, attempt_number = ?, outcome = ?, completed_at = ?, updated_at = ? WHERE job_id = ?;",
                            (
                                attempt_id,
                                row["attempt_number"],
                                outcome,
                                now,
                                now,
                                row["job_id"],
                            ),
                        )
                return cursor.rowcount > 0
        finally:
            conn.close()


def handle_job_failure(
    job_id: str,
    worker_id: str,
    generation: int,
    attempt_id: str | None,
    error: Exception | str,
    is_pre_submit: bool,
    error_category: str = "",
    custom_path: Path | None = None,
) -> str:
    """
    Centralized failure arbiter:
    - Post-submit failures or crashes: Ambiguous submission. NEVER retried automatically.
    - Pre-submit interactive exceptions: auth/mfa/captcha/unknown_question -> paused for operator.
    - Pre-submit transient errors: bounded retry (30s, 120s, 600s) up to max_retries.
    """
    err_msg = str(error)
    now_dt = datetime.now()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    # 1. Post-submit failure
    if not is_pre_submit:
        if error_category != "ambiguous_submission":
            if attempt_id:
                complete_attempt(
                    attempt_id=attempt_id,
                    outcome="failed_permanent",
                    error_details=err_msg,
                    is_ambiguous=False,
                    custom_path=custom_path,
                )
            transition_job(
                job_id=job_id,
                worker_id=worker_id,
                generation=generation,
                to_state=JobState.FAILED_PERMANENT.value,
                error_code="SUBMISSION_REJECTED",
                error_message=err_msg,
                custom_path=custom_path,
            )
            return JobState.FAILED_PERMANENT.value

        if attempt_id:
            complete_attempt(
                attempt_id=attempt_id,
                outcome="ambiguous",
                error_details=err_msg,
                is_ambiguous=True,
                custom_path=custom_path,
            )
        transition_job(
            job_id=job_id,
            worker_id=worker_id,
            generation=generation,
            to_state=JobState.AMBIGUOUS_SUBMISSION.value,
            error_code="POST_SUBMIT_UNCERTAIN",
            error_message=err_msg,
            custom_path=custom_path,
        )
        safe_msg = (
            f"Job attempt {job_id[:8] if job_id else ''} encountered an uncertain outcome after submission was triggered. "
            "Automatic retry is disabled to prevent duplicate submissions."
        )
        enqueue_notification(
            category="ambiguous_submission",
            urgency="high",
            title="Ambiguous Application Submission",
            message=safe_msg,
            custom_path=custom_path,
            job_id=job_id,
        )
        return JobState.AMBIGUOUS_SUBMISSION.value

    # 2. Pre-submit interactive exceptions
    if error_category in EXCEPTION_STATES:
        if attempt_id:
            complete_attempt(
                attempt_id=attempt_id,
                outcome=error_category,
                error_details=err_msg,
                custom_path=custom_path,
            )
        transition_job(
            job_id=job_id,
            worker_id=worker_id,
            generation=generation,
            to_state=error_category,
            error_code=error_category.upper(),
            error_message=err_msg,
            custom_path=custom_path,
        )
        if error_category == "unknown_question":
            safe_title = "Action Required: Novel Screening Question"
            safe_msg = f"Job attempt {job_id[:8] if job_id else ''} paused: Unknown screening question requires approved answer."
        elif error_category == "captcha_required":
            safe_title = "Action Required: CAPTCHA Challenge"
            safe_msg = f"Job attempt {job_id[:8] if job_id else ''} paused: CAPTCHA detected. Operator assistance required via browser view."
        elif error_category == "mfa_required":
            safe_title = "Action Required: Security Verification (MFA)"
            safe_msg = f"Job attempt {job_id[:8] if job_id else ''} paused: MFA or verification code required."
        else:
            safe_title = f"Action Required: {error_category.replace('_', ' ').title()}"
            safe_msg = f"Job attempt {job_id[:8] if job_id else ''} paused for operator review."

        enqueue_notification(
            category=error_category,
            urgency="normal" if error_category == "unknown_question" else "high",
            title=safe_title,
            message=safe_msg,
            custom_path=custom_path,
            job_id=job_id,
        )
        return error_category

    # 2b. Pre-submit permanent failure (e.g. aggregator URL blocked)
    if error_category == "failed_permanent":
        if attempt_id:
            complete_attempt(
                attempt_id=attempt_id,
                outcome="failed_permanent",
                error_details=err_msg,
                custom_path=custom_path,
            )
        transition_job(
            job_id=job_id,
            worker_id=worker_id,
            generation=generation,
            to_state=JobState.FAILED_PERMANENT.value,
            error_code="FAILED_PERMANENT",
            error_message=err_msg,
            custom_path=custom_path,
        )
        return JobState.FAILED_PERMANENT.value

    # 3. Pre-submit transient failure: bounded exponential backoff
    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            job_row = conn.execute(
                "SELECT attempt_count, max_retries FROM automation_jobs WHERE id = ?;",
                (job_id,),
            ).fetchone()
            attempt_count = job_row["attempt_count"] if job_row else 1
            max_retries = job_row["max_retries"] if job_row else 3
        finally:
            conn.close()

    if attempt_count < max_retries:
        backoffs = [30, 120, 600]
        delay = backoffs[min(attempt_count - 1, len(backoffs) - 1)]
        next_retry_str = (now_dt + timedelta(seconds=delay)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        if attempt_id:
            complete_attempt(
                attempt_id=attempt_id,
                outcome="retry_wait",
                error_details=err_msg,
                custom_path=custom_path,
            )

        with _QUEUE_LOCK:
            conn = get_connection(custom_path)
            try:
                with conn:
                    conn.execute(
                        """
                        UPDATE automation_jobs
                        SET state = 'retry_wait',
                            next_retry_at = ?,
                            error_code = 'PRE_SUBMIT_TRANSIENT',
                            error_message = ?,
                            updated_at = ?
                        WHERE id = ? AND lease_owner = ? AND fencing_generation = ?;
                        """,
                        (
                            next_retry_str,
                            err_msg,
                            now_str,
                            job_id,
                            worker_id,
                            generation,
                        ),
                    )
            finally:
                conn.close()

        record_event(
            job_id=job_id,
            app_id=None,
            event_type="retry_scheduled",
            level="WARN",
            step="retry_wait",
            message=f"Pre-submit error: {err_msg[:100]}. Backoff retry #{attempt_count} scheduled at {next_retry_str}.",
            details={"next_retry_at": next_retry_str, "attempt_count": attempt_count},
            custom_path=custom_path,
        )
        return JobState.RETRY_WAIT.value

    # Exhausted retries -> failed_permanent
    if attempt_id:
        complete_attempt(
            attempt_id=attempt_id,
            outcome="failed_permanent",
            error_details=err_msg,
            custom_path=custom_path,
        )
    transition_job(
        job_id=job_id,
        worker_id=worker_id,
        generation=generation,
        to_state=JobState.FAILED_PERMANENT.value,
        error_code="MAX_RETRIES_EXCEEDED",
        error_message=err_msg,
        custom_path=custom_path,
    )
    return JobState.FAILED_PERMANENT.value


def reschedule_for_safety_gate(
    job_id: str,
    worker_id: str,
    generation: int,
    retry_delay_seconds: int,
    reason: str,
    attempt_id: str | None = None,
    custom_path: Path | None = None,
) -> str:
    """Reschedules a job that hit a safety gate (pacing or daily limit) without burning retries."""
    now_dt = datetime.now()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    next_retry_str = (now_dt + timedelta(seconds=retry_delay_seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    if attempt_id:
        complete_attempt(
            attempt_id=attempt_id,
            outcome="safety_gate_wait",
            error_details=reason,
            custom_path=custom_path,
        )

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE automation_jobs
                    SET state = 'retry_wait',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        next_retry_at = ?,
                        attempt_count = CASE WHEN attempt_count > 0 THEN attempt_count - 1 ELSE 0 END,
                        error_code = 'SAFETY_GATE_WAIT',
                        error_message = ?,
                        updated_at = ?
                    WHERE id = ? AND lease_owner = ? AND fencing_generation = ?;
                    """,
                    (
                        next_retry_str,
                        reason,
                        now_str,
                        job_id,
                        worker_id,
                        generation,
                    ),
                )
        finally:
            conn.close()

    record_event(
        job_id=job_id,
        app_id=None,
        event_type="safety_gate_wait",
        level="WARN",
        step="retry_wait",
        message=f"Safety gate delay ({retry_delay_seconds}s): {reason[:120]}. Rescheduled at {next_retry_str}.",
        details={"next_retry_at": next_retry_str, "reason": reason},
        custom_path=custom_path,
    )
    return JobState.RETRY_WAIT.value


def cancel_job(job_id: str, reason: str = "", custom_path: Path | None = None) -> bool:
    """Cancels a queued or running automation job."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                row = conn.execute(
                    """
                    SELECT id, app_id FROM automation_jobs
                    WHERE id = ? OR app_id = ?
                    ORDER BY created_at DESC LIMIT 1;
                    """,
                    (job_id, job_id),
                ).fetchone()

                if row:
                    actual_job_id = row["id"]
                    app_id = row["app_id"]

                    cursor = conn.execute(
                        """
                        UPDATE automation_jobs
                        SET is_cancelled = 1,
                            state = CASE WHEN state IN ('ready', 'retry_wait', 'unknown_question', 'auth_required', 'mfa_required', 'captcha_required') THEN 'cancelled' ELSE state END,
                            error_message = CASE WHEN ? != '' THEN ? ELSE error_message END,
                            updated_at = ?
                        WHERE id = ?;
                        """,
                        (reason, reason, now, actual_job_id),
                    )
                    success = cursor.rowcount > 0
                    if success:
                        if app_id:
                            conn.execute(
                                """
                                UPDATE applications
                                SET status = 'cancelled',
                                    notes = CASE WHEN ? != '' THEN ? ELSE notes END,
                                    updated_at = ?
                                WHERE id = ?;
                                """,
                                (reason, reason, now, app_id),
                            )
                        record_event(
                            job_id=actual_job_id,
                            app_id=app_id,
                            event_type="job_cancelled",
                            level="WARN",
                            step="cancelled",
                            message=f"Job cancelled: {reason or 'User requested'}",
                            custom_path=custom_path,
                            conn=conn,
                        )
                    return success
                else:
                    # Fallback: check applications table directly
                    app_row = conn.execute(
                        "SELECT id FROM applications WHERE id = ?;", (job_id,)
                    ).fetchone()
                    if app_row:
                        app_id = app_row["id"]
                        conn.execute(
                            """
                            UPDATE applications
                            SET status = 'cancelled',
                                notes = CASE WHEN ? != '' THEN ? ELSE notes END,
                                updated_at = ?
                            WHERE id = ?;
                            """,
                            (reason, reason, now, app_id),
                        )
                        record_event(
                            job_id=None,
                            app_id=app_id,
                            event_type="job_cancelled",
                            level="WARN",
                            step="cancelled",
                            message=f"Job cancelled: {reason or 'User requested'}",
                            custom_path=custom_path,
                            conn=conn,
                        )
                        return True
                    return False
        finally:
            conn.close()


def skip_job(job_id: str, reason: str = "", custom_path: Path | None = None) -> bool:
    """Marks a job and associated application as skipped."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                row = conn.execute(
                    """
                    SELECT id, app_id FROM automation_jobs
                    WHERE id = ? OR app_id = ?
                    ORDER BY created_at DESC LIMIT 1;
                    """,
                    (job_id, job_id),
                ).fetchone()

                if row:
                    actual_job_id = row["id"]
                    app_id = row["app_id"]

                    conn.execute(
                        """
                        UPDATE automation_jobs
                        SET state = 'skipped', error_message = ?, updated_at = ?
                        WHERE id = ?;
                        """,
                        (reason or "Skipped by operator", now, actual_job_id),
                    )
                    if app_id:
                        conn.execute(
                            """
                            UPDATE applications
                            SET status = 'skipped', notes = CASE WHEN ? != '' THEN ? ELSE notes END, updated_at = ?
                            WHERE id = ?;
                            """,
                            (reason, reason, now, app_id),
                        )
                    record_event(
                        job_id=actual_job_id,
                        app_id=app_id,
                        event_type="job_skipped",
                        level="INFO",
                        step="skipped",
                        message=f"Job skipped: {reason or 'Operator skipped'}",
                        custom_path=custom_path,
                        conn=conn,
                    )
                    return True
                else:
                    # Fallback: check applications table directly
                    app_row = conn.execute(
                        "SELECT id FROM applications WHERE id = ?;", (job_id,)
                    ).fetchone()
                    if app_row:
                        app_id = app_row["id"]
                        conn.execute(
                            """
                            UPDATE applications
                            SET status = 'skipped', notes = CASE WHEN ? != '' THEN ? ELSE notes END, updated_at = ?
                            WHERE id = ?;
                            """,
                            (reason, reason, now, app_id),
                        )
                        record_event(
                            job_id=None,
                            app_id=app_id,
                            event_type="job_skipped",
                            level="INFO",
                            step="skipped",
                            message=f"Job skipped: {reason or 'Operator skipped'}",
                            custom_path=custom_path,
                            conn=conn,
                        )
                        return True
                    return False
        finally:
            conn.close()


def resolve_job(
    job_id: str,
    resolution_type: str,
    answer_value: str = "",
    question_key: str = "",
    approved_scope: str = "global",
    force: bool = False,
    custom_path: Path | None = None,
) -> bool:
    """
    Operator resolution for paused jobs:
    - If resolution_type == 'answer': saves approved answer to approved_answers table and resets job to ready.
    - If resolution_type == 'continue': clears exception and resets job to ready.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                row = conn.execute(
                    """
                    SELECT * FROM automation_jobs
                    WHERE id = ? OR app_id = ?
                    ORDER BY created_at DESC LIMIT 1;
                    """,
                    (job_id, job_id),
                ).fetchone()
                if not row:
                    return False

                actual_job_id = row["id"]
                app_id = row["app_id"]

                if row["state"] == JobState.AMBIGUOUS_SUBMISSION.value and not force:
                    raise ValueError(
                        "Job has ambiguous_submission outcome. Re-queueing requires explicit force=True."
                    )

                if resolution_type == "answer" and question_key and answer_value:
                    norm_key = question_key.strip().lower()
                    ans_id = f"ans_{uuid.uuid4().hex[:8]}"
                    conn.execute(
                        """
                        INSERT INTO approved_answers (
                            id, question_key, answer_value, provenance, sensitivity_class,
                            scope, approval_status, created_at, updated_at
                        ) VALUES (?, ?, ?, 'manual_approval', 'standard', ?, 'approved', ?, ?)
                        ON CONFLICT(question_key, scope) DO UPDATE SET
                            answer_value = excluded.answer_value,
                            revision = approved_answers.revision + 1,
                            updated_at = excluded.updated_at;
                        """,
                        (
                            ans_id,
                            norm_key,
                            answer_value.strip(),
                            approved_scope,
                            now,
                            now,
                        ),
                    )

                # Reset job to ready so it can be claimed afresh
                conn.execute(
                    """
                    UPDATE automation_jobs
                    SET state = 'ready',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        error_code = NULL,
                        error_message = NULL,
                        next_retry_at = NULL,
                        updated_at = ?
                    WHERE id = ?;
                    """,
                    (now, actual_job_id),
                )

                if app_id:
                    conn.execute(
                        "UPDATE applications SET status = 'pending', updated_at = ? WHERE id = ? AND status IN ('ambiguous', 'failed');",
                        (now, app_id),
                    )

                record_event(
                    job_id=actual_job_id,
                    app_id=app_id,
                    event_type="job_resolved",
                    level="INFO",
                    step="resolved",
                    message=f"Job resolved via {resolution_type} and queued for re-execution.",
                    details={
                        "resolution_type": resolution_type,
                        "question_key": question_key,
                    },
                    custom_path=custom_path,
                    conn=conn,
                )
                return True
        finally:
            conn.close()


# --- Runtime Controls ---


def set_runtime_pause(is_paused: bool, custom_path: Path | None = None) -> None:
    """Sets global runtime automation pause state."""
    init_db(custom_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE runtime_control SET is_paused = ?, updated_at = ? WHERE id = 1;",
                    (1 if is_paused else 0, now),
                )
        finally:
            conn.close()


def set_runtime_stop(is_stopped: bool, custom_path: Path | None = None) -> None:
    """Sets global runtime emergency stop state."""
    init_db(custom_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE runtime_control SET is_stopped = ?, updated_at = ? WHERE id = 1;",
                    (1 if is_stopped else 0, now),
                )
        finally:
            conn.close()


def requeue_auth_required_job(
    job_id: str | None = None,
    custom_path: Path | None = None,
) -> dict[str, Any]:
    """Requeues an auth-paused job for a fresh controlled browser attempt.

    This is deliberately separate from safe resume: no browser state is trusted or
    bypassed. The worker must navigate again, detect the authentication challenge,
    and pause with a live browser for operator takeover.
    """
    init_db(custom_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                if job_id is None:
                    owner = conn.execute(
                        "SELECT browser_job_id FROM runtime_control WHERE id = 1;"
                    ).fetchone()
                    job_id = owner["browser_job_id"] if owner else None
                    if not job_id:
                        return {
                            "status": "rejected",
                            "reason": "auth_job_target_required",
                            "message": "Specify the authentication-paused job to reopen; no browser-owned job is available.",
                        }

                row = conn.execute(
                    """
                    SELECT j.id, j.app_id, j.state, a.company, a.title
                    FROM automation_jobs j
                    JOIN applications a ON a.id = j.app_id
                    WHERE j.id = ? AND j.state = 'auth_required';
                    """,
                    (job_id,),
                ).fetchone()
                if not row:
                    return {
                        "status": "rejected",
                        "reason": "auth_job_not_found",
                        "message": "The requested authentication-paused job is not available to reopen.",
                    }
                control = conn.execute(
                    "SELECT is_stopped FROM runtime_control WHERE id = 1;"
                ).fetchone()
                if control and control["is_stopped"]:
                    return {
                        "status": "rejected",
                        "reason": "emergency_stop_active",
                        "message": "Emergency stop is active. Clear it explicitly before reopening authentication.",
                    }

                conn.execute(
                    """
                    UPDATE automation_jobs
                    SET state = 'ready',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        error_code = NULL,
                        error_message = NULL,
                        next_retry_at = NULL,
                        checkpoint = 'auth_retry_requested',
                        updated_at = ?
                    WHERE id = ? AND state = 'auth_required';
                    """,
                    (now, row["id"]),
                )
                record_event(
                    job_id=row["id"],
                    app_id=row["app_id"],
                    event_type="auth_retry_requested",
                    level="WARN",
                    step="auth_retry",
                    message="Authentication session requested to reopen in a fresh controlled browser.",
                    custom_path=custom_path,
                    conn=conn,
                )
                conn.execute(
                    """
                    UPDATE runtime_control
                    SET is_paused = 0,
                        manual_takeover_owner = NULL,
                        manual_takeover_expires_at = NULL,
                        is_waiting_for_code = 0,
                        pending_verification_code = NULL,
                        browser_active = 0,
                        browser_url = '',
                        browser_page_text = '',
                        browser_is_closed = 1,
                        browser_updated_at = ?,
                        browser_job_id = NULL,
                        updated_at = ?
                    WHERE id = 1;
                    """,
                    (now, now),
                )
                return {
                    "status": "started",
                    "action": "reopen_auth_session",
                    "job_id": row["id"],
                    "app_id": row["app_id"],
                    "message": "Fresh browser session requested. Automation will pause again when authentication is detected; then open Browser View and log in.",
                }
        finally:
            conn.close()


def get_runtime_control(custom_path: Path | None = None) -> dict[str, Any]:
    """Returns global runtime pause/stop and manual takeover control state."""
    init_db(custom_path)
    conn = get_connection(custom_path)
    try:
        row = conn.execute("SELECT * FROM runtime_control WHERE id = 1;").fetchone()
        if not row:
            return {
                "is_paused": False,
                "is_stopped": False,
                "manual_takeover_owner": None,
            }
        return {
            "is_paused": bool(row["is_paused"]),
            "is_stopped": bool(row["is_stopped"]),
            "manual_takeover_owner": row["manual_takeover_owner"],
            "manual_takeover_expires_at": row["manual_takeover_expires_at"],
            "is_waiting_for_code": bool(row["is_waiting_for_code"])
            if "is_waiting_for_code" in row.keys()
            else False,
            "pending_verification_code": row["pending_verification_code"]
            if "pending_verification_code" in row.keys()
            else None,
            "browser_active": bool(row["browser_active"])
            if "browser_active" in row.keys()
            else False,
            "browser_url": row["browser_url"] if "browser_url" in row.keys() else "",
            "browser_page_text": row["browser_page_text"]
            if "browser_page_text" in row.keys()
            else "",
            "browser_is_closed": bool(row["browser_is_closed"])
            if "browser_is_closed" in row.keys()
            else True,
            "browser_updated_at": row["browser_updated_at"]
            if "browser_updated_at" in row.keys()
            else None,
            "browser_job_id": row["browser_job_id"]
            if "browser_job_id" in row.keys()
            else None,
            "updated_at": row["updated_at"],
        }
    finally:
        conn.close()


def set_pending_verification_code(code: str, custom_path: Path | None = None) -> None:
    init_db(custom_path)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE runtime_control
                    SET pending_verification_code = ?,
                        is_waiting_for_code = 0,
                        updated_at = ?
                    WHERE id = 1;
                    """,
                    (code, now_str),
                )
        finally:
            conn.close()


def consume_pending_verification_code(
    custom_path: Path | None = None,
) -> str | None:
    init_db(custom_path)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                row = conn.execute(
                    "SELECT pending_verification_code FROM runtime_control WHERE id = 1;"
                ).fetchone()
                if row and row["pending_verification_code"]:
                    code = str(row["pending_verification_code"])
                    conn.execute(
                        """
                        UPDATE runtime_control
                        SET pending_verification_code = NULL,
                            updated_at = ?
                        WHERE id = 1;
                        """,
                        (now_str,),
                    )
                    return code
                return None
        finally:
            conn.close()


def set_runtime_browser_state(
    active: bool,
    url: str = "",
    page_text: str = "",
    is_closed: bool = True,
    is_waiting_for_code: bool = False,
    browser_job_id: str | None = None,
    custom_path: Path | None = None,
) -> None:
    init_db(custom_path)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                conn.execute(
                    """
                    UPDATE runtime_control
                    SET browser_active = ?,
                        browser_url = ?,
                        browser_page_text = ?,
                        browser_is_closed = ?,
                        is_waiting_for_code = ?,
                        browser_updated_at = ?,
                        browser_job_id = CASE
                            WHEN ? = 1 THEN COALESCE(?, browser_job_id)
                            WHEN ? = 0 AND (? IS NULL OR browser_job_id = ?) THEN NULL
                            ELSE browser_job_id
                        END,
                        updated_at = ?
                    WHERE id = 1;
                    """,
                    (
                        1 if active else 0,
                        url,
                        page_text,
                        1 if is_closed else 0,
                        1 if is_waiting_for_code else 0,
                        now_str,
                        1 if active else 0,
                        browser_job_id,
                        1 if active else 0,
                        browser_job_id,
                        browser_job_id,
                        now_str,
                    ),
                )
        finally:
            conn.close()


def get_runtime_browser_state(custom_path: Path | None = None) -> dict[str, Any]:
    init_db(custom_path)
    conn = get_connection(custom_path)
    try:
        row = conn.execute(
            """
            SELECT browser_active, browser_url, browser_page_text,
                   browser_is_closed, is_waiting_for_code, browser_updated_at,
                   browser_job_id
            FROM runtime_control WHERE id = 1;
            """
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def is_takeover_active(
    custom_path: Path | None = None,
) -> tuple[bool, str | None, str | None]:
    """
    Checks if a valid, unexpired manual takeover lease is currently held.
    Returns (is_active, owner, expires_at).
    """
    init_db(custom_path)
    now_dt = datetime.now()
    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            row = conn.execute(
                "SELECT manual_takeover_owner, manual_takeover_expires_at FROM runtime_control WHERE id = 1;"
            ).fetchone()
            if (
                not row
                or not row["manual_takeover_owner"]
                or not row["manual_takeover_expires_at"]
            ):
                return False, None, None

            try:
                expires_dt = datetime.strptime(
                    row["manual_takeover_expires_at"], "%Y-%m-%d %H:%M:%S"
                )
            except ValueError:
                return False, None, None

            if now_dt < expires_dt:
                return (
                    True,
                    row["manual_takeover_owner"],
                    row["manual_takeover_expires_at"],
                )
            return (
                False,
                row["manual_takeover_owner"],
                row["manual_takeover_expires_at"],
            )
        finally:
            conn.close()


def claim_manual_takeover(
    owner: str,
    lease_seconds: int = 300,
    custom_path: Path | None = None,
) -> dict[str, Any]:
    """
    Atomically acquires an exclusive manual takeover lease on the runtime browser.
    Immediately pauses the entire worker (is_paused = 1) so no automated actions take place.
    """
    init_db(custom_path)
    now_dt = datetime.now()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    expires_dt = now_dt + timedelta(seconds=lease_seconds)
    expires_str = expires_dt.strftime("%Y-%m-%d %H:%M:%S")

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                row = conn.execute(
                    "SELECT manual_takeover_owner, manual_takeover_expires_at FROM runtime_control WHERE id = 1;"
                ).fetchone()

                if (
                    row
                    and row["manual_takeover_owner"]
                    and row["manual_takeover_expires_at"]
                ):
                    try:
                        cur_expires = datetime.strptime(
                            row["manual_takeover_expires_at"], "%Y-%m-%d %H:%M:%S"
                        )
                        if (
                            now_dt < cur_expires
                            and row["manual_takeover_owner"] != owner
                        ):
                            return {
                                "status": "conflict",
                                "message": f"Takeover is currently active by {row['manual_takeover_owner']} until {row['manual_takeover_expires_at']}.",
                                "owner": row["manual_takeover_owner"],
                                "expires_at": row["manual_takeover_expires_at"],
                            }
                    except ValueError:
                        pass

                conn.execute(
                    """
                    UPDATE runtime_control
                    SET is_paused = 1,
                        manual_takeover_owner = ?,
                        manual_takeover_expires_at = ?,
                        updated_at = ?
                    WHERE id = 1;
                    """,
                    (owner, expires_str, now_str),
                )

                # Record audit event
                record_event(
                    job_id=None,
                    app_id=None,
                    event_type="manual_takeover_claimed",
                    level="INFO",
                    step="takeover",
                    message=f"Manual takeover lease claimed by {owner} for {lease_seconds}s. Automation paused.",
                    details={
                        "owner": owner,
                        "lease_seconds": lease_seconds,
                        "expires_at": expires_str,
                    },
                    custom_path=custom_path,
                    conn=conn,
                )

                return {
                    "status": "success",
                    "owner": owner,
                    "lease_seconds": lease_seconds,
                    "expires_at": expires_str,
                    "is_paused": True,
                    "message": f"Takeover lease granted to {owner}. Automation worker paused.",
                }
        finally:
            conn.close()


def release_manual_takeover(
    owner: str | None = None,
    force: bool = False,
    custom_path: Path | None = None,
) -> dict[str, Any]:
    """
    Atomically releases the manual takeover lease.
    Note: Automation remains paused (is_paused remains 1) until safe resume revalidation succeeds.
    """
    init_db(custom_path)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                row = conn.execute(
                    "SELECT manual_takeover_owner, manual_takeover_expires_at FROM runtime_control WHERE id = 1;"
                ).fetchone()

                current_owner = row["manual_takeover_owner"] if row else None
                if not force and owner and current_owner and current_owner != owner:
                    return {
                        "status": "error",
                        "message": f"Cannot release takeover: owned by {current_owner}, not {owner}.",
                    }

                conn.execute(
                    """
                    UPDATE runtime_control
                    SET manual_takeover_owner = NULL,
                        manual_takeover_expires_at = NULL,
                        updated_at = ?
                    WHERE id = 1;
                    """,
                    (now_str,),
                )

                # Record event
                record_event(
                    job_id=None,
                    app_id=None,
                    event_type="manual_takeover_released",
                    level="INFO",
                    step="takeover",
                    message=f"Manual takeover lease released by {owner or 'system'}. Worker remains paused awaiting safe resume.",
                    details={"released_by": owner or "system"},
                    custom_path=custom_path,
                    conn=conn,
                )

                return {
                    "status": "success",
                    "message": "Takeover lease released. Automation remains paused awaiting safe resume revalidation.",
                }
        finally:
            conn.close()


# --- Notification Outbox & Durable In-App Notifications ---


def create_notification(
    category: str,
    title: str,
    message: str,
    severity: str = "info",
    job_id: str | None = None,
    app_id: str | None = None,
    event_id: int | None = None,
    url: str = "",
    details: dict[str, Any] | None = None,
    custom_path: Path | None = None,
) -> dict[str, Any]:
    """
    Creates a durable dashboard notification and records an ordered notification event.
    Stores notification identity, associated event/job, severity, creation time, and acknowledgement state.
    """
    init_db(custom_path)
    notif_id = f"notif_{uuid.uuid4().hex[:8]}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    details_str = json.dumps(details or {})

    # Record linked event if none provided
    linked_event_id = event_id
    if linked_event_id is None:
        try:
            linked_event_id = record_event(
                job_id=job_id,
                app_id=app_id,
                event_type="notification",
                level=severity.upper()
                if severity in ("info", "warning", "error", "critical")
                else "INFO",
                step="alert",
                message=f"{title}: {message}",
                details={
                    "notification_id": notif_id,
                    "category": category,
                    "severity": severity,
                },
                custom_path=custom_path,
            )
        except Exception:
            linked_event_id = None

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT INTO notifications (
                        notification_id, job_id, app_id, event_id, category,
                        severity, title, message, url, details_json, acknowledged, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?);
                    """,
                    (
                        notif_id,
                        job_id,
                        app_id,
                        linked_event_id,
                        category,
                        severity,
                        title,
                        message,
                        url,
                        details_str,
                        now,
                    ),
                )
                row_id = cursor.lastrowid
                return {
                    "id": row_id,
                    "notification_id": notif_id,
                    "job_id": job_id,
                    "app_id": app_id,
                    "event_id": linked_event_id,
                    "category": category,
                    "severity": severity,
                    "title": title,
                    "message": message,
                    "url": url,
                    "details": details or {},
                    "acknowledged": False,
                    "acknowledged_at": None,
                    "created_at": now,
                }
        finally:
            conn.close()


def get_notifications(
    after_id: int | str | None = None,
    unread_only: bool = False,
    limit: int = 50,
    custom_path: Path | None = None,
) -> list[dict[str, Any]]:
    """
    Fetches durable notifications from the database.
    Supports fetching notifications created after a given integer ID or notification_id string.
    """
    init_db(custom_path)
    conn = get_connection(custom_path)
    try:
        query_after: int | None = None
        if after_id is not None:
            if isinstance(after_id, int):
                query_after = after_id
            elif str(after_id).isdigit():
                query_after = int(str(after_id))
            else:
                found = conn.execute(
                    "SELECT id FROM notifications WHERE notification_id = ?;",
                    (str(after_id),),
                ).fetchone()
                if found:
                    query_after = found["id"]

        clauses = []
        params: list[Any] = []

        if query_after is not None:
            clauses.append("id > ?")
            params.append(query_after)

        if unread_only:
            clauses.append("acknowledged = 0")

        where_str = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order_str = "ORDER BY id ASC" if query_after is not None else "ORDER BY id DESC"
        params.append(limit)

        sql = f"""
            SELECT * FROM notifications
            {where_str}
            {order_str}
            LIMIT ?;
        """
        rows = conn.execute(sql, params).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["acknowledged"] = bool(d.get("acknowledged", 0))
            if d.get("details_json"):
                try:
                    d["details"] = json.loads(d["details_json"])
                except Exception:
                    d["details"] = {}
            else:
                d["details"] = {}
            result.append(d)
        return result
    finally:
        conn.close()


def ack_notification(
    notification_id: str | int,
    custom_path: Path | None = None,
) -> dict[str, Any]:
    """
    Idempotently marks a notification as acknowledged.
    Distinct from job resolution/resumption: acknowledging never resumes automation.
    """
    init_db(custom_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                if isinstance(notification_id, int) or str(notification_id).isdigit():
                    row = conn.execute(
                        "SELECT * FROM notifications WHERE id = ? OR notification_id = ?;",
                        (int(str(notification_id)), str(notification_id)),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT * FROM notifications WHERE notification_id = ?;",
                        (str(notification_id),),
                    ).fetchone()

                if not row:
                    return {
                        "status": "not_found",
                        "id": notification_id,
                        "acknowledged": False,
                        "message": f"Notification {notification_id} not found.",
                    }

                n_dict = dict(row)
                nid = n_dict["notification_id"]
                numeric_id = n_dict["id"]

                if n_dict.get("acknowledged", 0):
                    return {
                        "status": "success",
                        "id": numeric_id,
                        "notification_id": nid,
                        "acknowledged": True,
                        "acknowledged_at": n_dict.get("acknowledged_at"),
                        "already_acknowledged": True,
                    }

                conn.execute(
                    """
                    UPDATE notifications
                    SET acknowledged = 1, acknowledged_at = ?
                    WHERE id = ?;
                    """,
                    (now, numeric_id),
                )

                # Emit acknowledgment event for connected SSE clients
                try:
                    record_event(
                        job_id=n_dict.get("job_id"),
                        app_id=n_dict.get("app_id"),
                        event_type="notification_ack",
                        level="INFO",
                        step="ack",
                        message=f"Notification {nid} acknowledged.",
                        details={
                            "notification_id": nid,
                            "id": numeric_id,
                            "acknowledged_at": now,
                        },
                        custom_path=custom_path,
                    )
                except Exception:
                    pass

                return {
                    "status": "success",
                    "id": numeric_id,
                    "notification_id": nid,
                    "acknowledged": True,
                    "acknowledged_at": now,
                    "already_acknowledged": False,
                }
        finally:
            conn.close()


def ack_all_notifications(
    up_to_id: int | None = None,
    custom_path: Path | None = None,
) -> int:
    """Idempotently marks all unacknowledged notifications (or up to up_to_id) as acknowledged."""
    init_db(custom_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                if up_to_id is not None:
                    cursor = conn.execute(
                        """
                        UPDATE notifications
                        SET acknowledged = 1, acknowledged_at = ?
                        WHERE acknowledged = 0 AND id <= ?;
                        """,
                        (now, up_to_id),
                    )
                else:
                    cursor = conn.execute(
                        """
                        UPDATE notifications
                        SET acknowledged = 1, acknowledged_at = ?
                        WHERE acknowledged = 0;
                        """,
                        (now,),
                    )
                updated_count = cursor.rowcount
                if updated_count > 0:
                    try:
                        record_event(
                            job_id=None,
                            app_id=None,
                            event_type="notification_ack_all",
                            level="INFO",
                            step="ack_all",
                            message=f"Acknowledged {updated_count} notifications.",
                            details={
                                "acknowledged_count": updated_count,
                                "up_to_id": up_to_id,
                            },
                            custom_path=custom_path,
                        )
                    except Exception:
                        pass
                return updated_count
        finally:
            conn.close()


def delete_notification(
    notification_id: str | int,
    custom_path: Path | None = None,
) -> bool:
    """Deletes a single notification by id or notification_id."""
    init_db(custom_path)
    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                if isinstance(notification_id, int) or str(notification_id).isdigit():
                    cursor = conn.execute(
                        "DELETE FROM notifications WHERE id = ? OR notification_id = ?;",
                        (int(str(notification_id)), str(notification_id)),
                    )
                else:
                    cursor = conn.execute(
                        "DELETE FROM notifications WHERE notification_id = ?;",
                        (str(notification_id),),
                    )
                return cursor.rowcount > 0
        finally:
            conn.close()


def clear_all_notifications(
    custom_path: Path | None = None,
) -> int:
    """Deletes all notifications and emits notification_clear event."""
    init_db(custom_path)
    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                cursor = conn.execute("DELETE FROM notifications;")
                deleted_count = cursor.rowcount
                if deleted_count > 0:
                    try:
                        record_event(
                            job_id=None,
                            app_id=None,
                            event_type="notification_clear_all",
                            level="INFO",
                            step="clear_all",
                            message=f"Cleared {deleted_count} notifications.",
                            details={"cleared_count": deleted_count},
                            custom_path=custom_path,
                        )
                    except Exception:
                        logger.debug(
                            "Failed to record notification clear event", exc_info=True
                        )
                return deleted_count
        finally:
            conn.close()


def get_notification_stats(custom_path: Path | None = None) -> dict[str, Any]:
    """Returns aggregate notification metrics (total, unread_count, latest_id)."""
    init_db(custom_path)
    conn = get_connection(custom_path)
    try:
        row = conn.execute(
            """
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN acknowledged = 0 THEN 1 ELSE 0 END) as unread_count,
                MAX(id) as latest_id
            FROM notifications;
            """
        ).fetchone()
        if not row:
            return {"total": 0, "unread_count": 0, "latest_id": 0}
        return {
            "total": row["total"] or 0,
            "unread_count": row["unread_count"] or 0,
            "latest_id": row["latest_id"] or 0,
        }
    finally:
        conn.close()


def enqueue_notification(
    category: str,
    title: str,
    message: str,
    urgency: str = "normal",
    url: str = "",
    payload: dict[str, Any] | None = None,
    dedup_key: str | None = None,
    custom_path: Path | None = None,
    job_id: str | None = None,
    app_id: str | None = None,
    severity: str | None = None,
) -> str:
    """
    Enqueues an alert notification into the durable outbox for mobile/ntfy dispatch,
    and simultaneously records a durable in-app notification in the notifications table.
    """
    init_db(custom_path)
    notif_id = f"notif_{uuid.uuid4().hex[:8]}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload_str = json.dumps(payload or {})
    effective_dedup = dedup_key or f"{category}_{notif_id}"
    effective_severity = severity or (
        "critical"
        if urgency in ("critical", "high")
        else ("warning" if urgency == "normal" else "info")
    )

    # 1. Record durable in-app notification
    try:
        create_notification(
            category=category,
            title=title,
            message=message,
            severity=effective_severity,
            job_id=job_id,
            app_id=app_id,
            url=url,
            details=payload,
            custom_path=custom_path,
        )
    except Exception as e:
        logger.warning(f"Failed to record in-app notification: {e}")

    # 2. Record durable outbox notification for mobile/ntfy push
    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO notification_outbox (
                        id, dedup_key, status, category, urgency, title, message, url,
                        payload_json, attempt_count, created_at
                    ) VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, 0, ?)
                    ON CONFLICT(dedup_key) DO UPDATE SET
                        message = excluded.message,
                        created_at = excluded.created_at;
                    """,
                    (
                        notif_id,
                        effective_dedup,
                        category,
                        urgency,
                        title,
                        message,
                        url,
                        payload_str,
                        now,
                    ),
                )
        finally:
            conn.close()

    # 3. Trigger immediate background outbox dispatch
    if (
        "PYTEST_CURRENT_TEST" not in os.environ
        or os.environ.get("JOB_APPLIER_ASYNC_OUTBOX_DISPATCH") == "1"
    ):

        def _dispatch_async() -> None:
            try:
                from job_applier.automation.ntfy import process_outbox

                process_outbox(custom_path=custom_path)
            except Exception as ex:
                logger.debug(f"Background outbox dispatch notice: {ex}")

        threading.Thread(target=_dispatch_async, daemon=True).start()

    return notif_id


def get_pending_notifications(
    limit: int = 50, due_only: bool = True, custom_path: Path | None = None
) -> list[dict[str, Any]]:
    """Retrieves pending notifications from outbox for delivery."""
    init_db(custom_path)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_connection(custom_path)
    try:
        if due_only:
            query = """
                SELECT * FROM notification_outbox
                WHERE status = 'pending' AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY created_at ASC LIMIT ?;
            """
            params: tuple[Any, ...] = (now_str, limit)
        else:
            query = """
                SELECT * FROM notification_outbox
                WHERE status = 'pending'
                ORDER BY created_at ASC LIMIT ?;
            """
            params = (limit,)
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_notification_sent(
    notif_id: str, error: str = "", custom_path: Path | None = None
) -> bool:
    """Updates status of a notification after dispatch attempt."""
    init_db(custom_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                status = "failed" if error else "delivered"
                cursor = conn.execute(
                    """
                    UPDATE notification_outbox
                    SET status = ?, last_error = ?, sent_at = ?, attempt_count = attempt_count + 1, next_retry_at = NULL
                    WHERE id = ?;
                    """,
                    (status, error, now, notif_id),
                )
                return cursor.rowcount > 0
        finally:
            conn.close()


def mark_notification_retry(
    notif_id: str, error: str = "", custom_path: Path | None = None
) -> bool:
    """Updates attempt count and error details for a pending outbox notification without marking it terminal."""
    init_db(custom_path)
    now_dt = datetime.now()
    now = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                row = conn.execute(
                    "SELECT attempt_count FROM notification_outbox WHERE id = ?;",
                    (notif_id,),
                ).fetchone()
                current_attempts = row["attempt_count"] if row else 0

                delays = [15, 45, 120, 300, 600]
                delay = delays[min(current_attempts, len(delays) - 1)]
                next_retry = (now_dt + timedelta(seconds=delay)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

                cursor = conn.execute(
                    """
                    UPDATE notification_outbox
                    SET attempt_count = attempt_count + 1,
                        last_error = ?,
                        sent_at = ?,
                        next_retry_at = ?
                    WHERE id = ?;
                    """,
                    (error, now, next_retry, notif_id),
                )
                return cursor.rowcount > 0
        finally:
            conn.close()


# --- Event Logging & SSE ---


_SAFE_EVENT_DETAIL_KEYS = {
    "checkpoint",
    "error_code",
    "reason",
    "outcome",
    "from_state",
    "to_state",
    "retry_count",
    "attempt_number",
    "confirmation_type",
}
_SECRET_TEXT_RE = re.compile(
    r"(?i)(?:bearer\s+|token|password|passwd|cookie|secret|api[_ -]?key|authorization)\s*[:=]\s*[^,;\s]+"
)


def _sanitize_event_message(message: str, event_type: str) -> str:
    """Redacts common credentials and bounds operator-visible event text."""
    safe = _SECRET_TEXT_RE.sub("[redacted]", str(message or ""))
    safe = re.sub(r"(?i)https?://[^\s]+", "[url]", safe)
    safe = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[email]", safe)
    safe = re.sub(r"\s+", " ", safe).strip()
    if not safe:
        safe = f"Automation event: {event_type}."
    return safe[:512]


def _sanitize_event_details(details: dict[str, Any] | None) -> dict[str, Any]:
    """Stores only non-sensitive, allowlisted event metadata."""
    result: dict[str, Any] = {}
    for key, value in (details or {}).items():
        if key not in _SAFE_EVENT_DETAIL_KEYS or value is None:
            continue
        if isinstance(value, bool | int | float):
            result[key] = value
        elif isinstance(value, str):
            result[key] = _sanitize_event_message(value, "detail")[:256]
    return result


def _project_event(
    active_conn: sqlite3.Connection,
    event_id: int,
    job_id: str | None,
    app_id: str | None,
    attempt_id: str | None,
    attempt_number: int | None,
    worker_id: str | None,
    lease_generation: int | None,
    browser_job_id: str | None,
    step: str,
    from_state: str | None,
    to_state: str | None,
    outcome_code: str | None,
    created_at: str,
) -> None:
    if not job_id:
        return
    job = active_conn.execute(
        "SELECT app_id, state, fencing_generation, lease_owner FROM automation_jobs WHERE id = ?;",
        (job_id,),
    ).fetchone()
    if not job:
        return
    resolved_app = app_id or job["app_id"]
    current_state = to_state or job["state"]
    active_conn.execute(
        """
        INSERT INTO automation_job_status (
            job_id, app_id, attempt_id, attempt_number, state, step, outcome,
            last_event_id, worker_id, lease_generation, browser_job_id,
            submit_intent_at, started_at, completed_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
            (SELECT submit_intent_at FROM application_attempts WHERE id = ?),
            (SELECT COALESCE(started_at, created_at) FROM application_attempts WHERE id = ?),
            (SELECT completed_at FROM application_attempts WHERE id = ?), ?)
        ON CONFLICT(job_id) DO UPDATE SET
            app_id = excluded.app_id,
            attempt_id = COALESCE(excluded.attempt_id, automation_job_status.attempt_id),
            attempt_number = COALESCE(excluded.attempt_number, automation_job_status.attempt_number),
            state = excluded.state,
            step = excluded.step,
            outcome = COALESCE(excluded.outcome, automation_job_status.outcome),
            last_event_id = excluded.last_event_id,
            worker_id = COALESCE(excluded.worker_id, automation_job_status.worker_id),
            lease_generation = COALESCE(excluded.lease_generation, automation_job_status.lease_generation),
            browser_job_id = COALESCE(excluded.browser_job_id, automation_job_status.browser_job_id),
            submit_intent_at = COALESCE(excluded.submit_intent_at, automation_job_status.submit_intent_at),
            started_at = COALESCE(excluded.started_at, automation_job_status.started_at),
            completed_at = COALESCE(excluded.completed_at, automation_job_status.completed_at),
            updated_at = excluded.updated_at;
        """,
        (
            job_id,
            resolved_app,
            attempt_id,
            attempt_number,
            current_state,
            step,
            outcome_code,
            event_id,
            worker_id,
            lease_generation,
            browser_job_id,
            attempt_id,
            attempt_id,
            attempt_id,
            created_at,
        ),
    )
    if attempt_id:
        active_conn.execute(
            "UPDATE application_attempts SET last_event_id = ?, worker_id = COALESCE(?, worker_id), lease_generation = COALESCE(?, lease_generation), browser_job_id = COALESCE(?, browser_job_id) WHERE id = ?;",
            (event_id, worker_id, lease_generation, browser_job_id, attempt_id),
        )


def _append_event_on_conn(
    active_conn: sqlite3.Connection,
    *,
    job_id: str | None,
    app_id: str | None,
    event_type: str,
    level: str,
    step: str,
    message: str,
    details: dict[str, Any] | None,
    attempt_id: str | None,
    event_key: str | None,
    attempt_number: int | None,
    worker_id: str | None,
    lease_generation: int | None,
    browser_job_id: str | None,
    from_state: str | None,
    to_state: str | None,
    outcome_code: str | None,
) -> int:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_message = _sanitize_event_message(message, event_type)
    safe_details = _sanitize_event_details(details)
    cursor = active_conn.execute(
        """
        INSERT OR IGNORE INTO automation_events (
            job_id, app_id, event_type, level, step, message, details_json, created_at,
            attempt_id, event_key, attempt_number, worker_id, lease_generation,
            browser_job_id, from_state, to_state, outcome_code, redaction_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1);
        """,
        (
            job_id,
            app_id,
            event_type,
            level,
            step,
            safe_message,
            json.dumps(safe_details, separators=(",", ":")),
            now,
            attempt_id,
            event_key,
            attempt_number,
            worker_id,
            lease_generation,
            browser_job_id,
            from_state,
            to_state,
            outcome_code,
        ),
    )
    event_id = cursor.lastrowid or 0
    if not event_id and attempt_id and event_key:
        row = active_conn.execute(
            "SELECT id FROM automation_events WHERE attempt_id = ? AND event_key = ?;",
            (attempt_id, event_key),
        ).fetchone()
        event_id = row["id"] if row else 0
    _project_event(
        active_conn,
        event_id,
        job_id,
        app_id,
        attempt_id,
        attempt_number,
        worker_id,
        lease_generation,
        browser_job_id,
        step,
        from_state,
        to_state,
        outcome_code,
        now,
    )
    return event_id


def append_event_and_project(
    *,
    job_id: str | None,
    app_id: str | None,
    event_type: str,
    level: str = "INFO",
    step: str = "",
    message: str = "",
    details: dict[str, Any] | None = None,
    attempt_id: str | None = None,
    event_key: str | None = None,
    attempt_number: int | None = None,
    worker_id: str | None = None,
    lease_generation: int | None = None,
    generation: int | None = None,
    browser_job_id: str | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
    outcome_code: str | None = None,
    custom_path: Path | None = None,
) -> int:
    """Atomically append a redacted event and update its job status projection."""
    fence = lease_generation if lease_generation is not None else generation
    with _QUEUE_LOCK:
        init_db(custom_path)
        conn = get_connection(custom_path)
        try:
            with conn:
                if job_id and worker_id is not None and fence is not None:
                    row = conn.execute(
                        "SELECT lease_owner, fencing_generation FROM automation_jobs WHERE id = ?;",
                        (job_id,),
                    ).fetchone()
                    if (
                        not row
                        or row["lease_owner"] != worker_id
                        or row["fencing_generation"] != fence
                    ):
                        raise PermissionError(
                            "stale automation lease or fencing generation"
                        )
                return _append_event_on_conn(
                    conn,
                    job_id=job_id,
                    app_id=app_id,
                    event_type=event_type,
                    level=level,
                    step=step,
                    message=message,
                    details=details,
                    attempt_id=attempt_id,
                    event_key=event_key,
                    attempt_number=attempt_number,
                    worker_id=worker_id,
                    lease_generation=fence,
                    browser_job_id=browser_job_id,
                    from_state=from_state,
                    to_state=to_state,
                    outcome_code=outcome_code,
                )
        finally:
            conn.close()


def record_event(
    job_id: str | None,
    app_id: str | None,
    event_type: str,
    level: str = "INFO",
    step: str = "",
    message: str = "",
    details: dict[str, Any] | None = None,
    custom_path: Path | None = None,
    conn: sqlite3.Connection | None = None,
    *,
    attempt_id: str | None = None,
    event_key: str | None = None,
    attempt_number: int | None = None,
    worker_id: str | None = None,
    lease_generation: int | None = None,
    browser_job_id: str | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
    outcome_code: str | None = None,
) -> int:
    """Records a redacted event and advances the durable job status projection."""
    with _QUEUE_LOCK:
        if conn is not None:
            return _append_event_on_conn(
                conn,
                job_id=job_id,
                app_id=app_id,
                event_type=event_type,
                level=level,
                step=step,
                message=message,
                details=details,
                attempt_id=attempt_id,
                event_key=event_key,
                attempt_number=attempt_number,
                worker_id=worker_id,
                lease_generation=lease_generation,
                browser_job_id=browser_job_id,
                from_state=from_state,
                to_state=to_state,
                outcome_code=outcome_code,
            )
        return append_event_and_project(
            job_id=job_id,
            app_id=app_id,
            event_type=event_type,
            level=level,
            step=step,
            message=message,
            details=details,
            attempt_id=attempt_id,
            event_key=event_key,
            attempt_number=attempt_number,
            worker_id=worker_id,
            lease_generation=lease_generation,
            browser_job_id=browser_job_id,
            from_state=from_state,
            to_state=to_state,
            outcome_code=outcome_code,
            custom_path=custom_path,
        )


def get_events(
    after_id: int = 0, limit: int = 100, custom_path: Path | None = None
) -> list[dict[str, Any]]:
    """Fetches events strictly newer than after_id for SSE streaming and replay."""
    init_db(custom_path)
    conn = get_connection(custom_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM automation_events
            WHERE id > ?
            ORDER BY id ASC
            LIMIT ?;
            """,
            (after_id, limit),
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["message"] = _sanitize_event_message(
                d.get("message", ""), d.get("event_type", "event")
            )
            raw_details: dict[str, Any] = {}
            if d.get("details_json"):
                try:
                    raw_details = json.loads(d["details_json"])
                except Exception:
                    raw_details = {}
            d["details"] = _sanitize_event_details(raw_details)
            d["details_json"] = json.dumps(d["details"], separators=(",", ":"))
            items.append(d)
        return items
    finally:
        conn.close()


def get_application_automation_status(
    app_id: str, job_id: str | None = None, custom_path: Path | None = None
) -> list[dict[str, Any]]:
    """Returns durable projected status for one application, newest jobs first."""
    init_db(custom_path)
    conn = get_connection(custom_path)
    try:
        query = "SELECT * FROM automation_job_status WHERE app_id = ?"
        params: list[Any] = [app_id]
        if job_id:
            query += " AND job_id = ?"
            params.append(job_id)
        query += " ORDER BY updated_at DESC, job_id ASC LIMIT 100;"
        return [dict(row) for row in conn.execute(query, params).fetchall()]
    finally:
        conn.close()


def get_application_automation_events(
    app_id: str,
    job_id: str | None = None,
    after_id: int = 0,
    limit: int = 100,
    custom_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Returns bounded, sanitized event history for an application."""
    safe_limit = max(1, min(int(limit), 100))
    init_db(custom_path)
    conn = get_connection(custom_path)
    try:
        query = "SELECT * FROM automation_events WHERE app_id = ? AND id > ?"
        params: list[Any] = [app_id, after_id]
        if job_id:
            query += " AND job_id = ?"
            params.append(job_id)
        query += " ORDER BY id ASC LIMIT ?"
        params.append(safe_limit)
        rows = conn.execute(query, params).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["message"] = _sanitize_event_message(
                item.get("message", ""), item.get("event_type", "event")
            )
            try:
                details = json.loads(item.get("details_json") or "{}")
            except Exception:
                details = {}
            item["details"] = _sanitize_event_details(details)
            item["details_json"] = json.dumps(item["details"], separators=(",", ":"))
            items.append(item)
        return items
    finally:
        conn.close()


def get_automation_funnel(custom_path: Path | None = None) -> dict[str, Any]:
    """Returns separate durable automation, artifact, and manual-submission metrics."""
    init_db(custom_path)
    conn = get_connection(custom_path)
    try:
        jobs = conn.execute(
            "SELECT state, COUNT(*) AS count FROM automation_jobs GROUP BY state;"
        ).fetchall()
        job_counts = {row["state"]: row["count"] for row in jobs}
        attempts = conn.execute(
            "SELECT outcome, COUNT(*) AS count FROM application_attempts GROUP BY outcome;"
        ).fetchall()
        outcome_counts = {row["outcome"] or "unknown": row["count"] for row in attempts}
        verified = conn.execute(
            "SELECT COUNT(*) FROM application_attempts WHERE outcome = 'applied' AND confirmation_evidence IS NOT NULL AND confirmation_evidence != ''"
        ).fetchone()[0]
        manual = conn.execute(
            "SELECT COUNT(*) FROM applications WHERE status = 'applied' AND COALESCE(submission_type, 'manual') != 'autonomous_browser'"
        ).fetchone()[0]
        project_root = get_project_root()
        generated = (
            len(list((project_root / "output" / "applications").iterdir()))
            if (project_root / "output" / "applications").is_dir()
            else 0
        )
        applied_artifacts = (
            len(list((project_root / "output" / "applied").iterdir()))
            if (project_root / "output" / "applied").is_dir()
            else 0
        )
        jobs_with_attempt = conn.execute(
            """
            SELECT COUNT(*)
            FROM automation_jobs j
            WHERE EXISTS (
                SELECT 1 FROM application_attempts aa WHERE aa.job_id = j.id
            )
            OR EXISTS (
                SELECT 1 FROM automation_events ae WHERE ae.job_id = j.id
            );
            """
        ).fetchone()[0]
        ctrl = conn.execute(
            "SELECT is_paused, is_stopped FROM runtime_control WHERE id = 1;"
        ).fetchone()
        if ctrl and (ctrl["is_paused"] or ctrl["is_stopped"]):
            runnable_jobs = 0
        else:
            runnable_jobs = conn.execute(
                """
                SELECT COUNT(*)
                FROM automation_jobs
                WHERE is_cancelled = 0
                  AND (
                    state = 'ready'
                    OR (state = 'retry_wait' AND (next_retry_at IS NULL OR next_retry_at <= datetime('now', 'localtime')))
                    OR (
                        state IN ('claimed', 'navigating', 'filling', 'validating')
                        AND lease_expires_at IS NOT NULL
                        AND lease_expires_at <= datetime('now', 'localtime')
                    )
                  );
                """
            ).fetchone()[0]
        current_auth_blocked_jobs = sum(
            job_counts.get(state, 0)
            for state in ("auth_required", "mfa_required", "captcha_required")
        )
        current_site_changed_jobs = job_counts.get(JobState.SITE_CHANGED.value, 0)
        historical_auth_blocked_attempts = sum(
            outcome_counts.get(state, 0)
            for state in ("auth_required", "mfa_required", "captcha_required")
        )
        historical_site_changed_attempts = outcome_counts.get("site_changed", 0)
        current_permanent_failures = job_counts.get(JobState.FAILED_PERMANENT.value, 0)
        current_retry_wait_jobs = job_counts.get(JobState.RETRY_WAIT.value, 0)
        current_skipped_jobs = job_counts.get(JobState.SKIPPED.value, 0)
        jobs_total = sum(job_counts.values())
        attempts_total = sum(outcome_counts.values())
        return {
            "verified_autonomous_submissions": verified,
            "manual_applied": manual,
            "generated_artifacts": generated + applied_artifacts,
            "applied_artifacts": applied_artifacts,
            "unqueued_artifacts": max(0, generated + applied_artifacts - jobs_total),
            "jobs_tracked": jobs_total,
            "runnable_jobs": runnable_jobs,
            "jobs_queued": jobs_total,
            "jobs_with_attempt": jobs_with_attempt,
            "current_auth_blocked_jobs": current_auth_blocked_jobs,
            "current_site_changed_jobs": current_site_changed_jobs,
            "historical_auth_blocked_attempts": historical_auth_blocked_attempts,
            "historical_site_changed_attempts": historical_site_changed_attempts,
            "total_attempts": attempts_total,
            "current_permanent_failures": current_permanent_failures,
            "current_retry_wait_jobs": current_retry_wait_jobs,
            "current_skipped_jobs": current_skipped_jobs,
            # Deprecated compatibility aliases. Consumers should use the explicit
            # scoped metrics above; these aliases retain historical API shapes only.
            "jobs_attempted": jobs_with_attempt,
            "auth_required": historical_auth_blocked_attempts,
            "site_changed": historical_site_changed_attempts,
            "permanent_failures": current_permanent_failures,
            "retries": current_retry_wait_jobs,
            "unknown": outcome_counts.get("unknown", 0),
            "jobs_total": jobs_total,
            "attempts_total": attempts_total,
            "deprecated_fields": ["jobs_attempted", "auth_required", "site_changed"],
            "job_states": job_counts,
            "attempt_outcomes": outcome_counts,
        }
    finally:
        conn.close()


def get_latest_event_id(custom_path: Path | None = None) -> int:
    """Returns the highest event ID currently in automation_events."""
    init_db(custom_path)
    conn = get_connection(custom_path)
    try:
        row = conn.execute("SELECT MAX(id) FROM automation_events;").fetchone()
        return row[0] if row and row[0] is not None else 0
    finally:
        conn.close()


def get_queue_stats(custom_path: Path | None = None) -> dict[str, Any]:
    """Returns queue breakdown statistics."""
    init_db(custom_path)
    conn = get_connection(custom_path)
    try:
        rows = conn.execute(
            "SELECT state, COUNT(*) as cnt FROM automation_jobs GROUP BY state;"
        ).fetchall()
        counts = {r["state"]: r["cnt"] for r in rows}
        return {
            "ready": counts.get(JobState.READY.value, 0),
            "claimed": counts.get(JobState.CLAIMED.value, 0),
            "in_progress": sum(
                counts.get(s, 0)
                for s in [
                    JobState.CLAIMED.value,
                    JobState.NAVIGATING.value,
                    JobState.FILLING.value,
                    JobState.VALIDATING.value,
                    JobState.SUBMIT_INTENT.value,
                    JobState.VERIFYING.value,
                ]
            ),
            "retry_wait": counts.get(JobState.RETRY_WAIT.value, 0),
            "applied": counts.get(JobState.APPLIED.value, 0),
            "exceptions": sum(counts.get(s, 0) for s in EXCEPTION_STATES),
            "total_jobs": sum(counts.values()),
        }
    finally:
        conn.close()


def get_browser_owner_job(custom_path: Path | None = None) -> dict[str, Any] | None:
    """Returns the resumable job explicitly owning the active browser session."""
    init_db(custom_path)
    conn = get_connection(custom_path)
    try:
        row = conn.execute(
            """
            SELECT j.*, a.company, a.title, a.job_url
            FROM automation_jobs j
            JOIN applications a ON a.id = j.app_id
            JOIN runtime_control rc ON rc.browser_job_id = j.id
            WHERE rc.id = 1
              AND rc.browser_active = 1
              AND rc.browser_is_closed = 0
              AND j.state IN (
                  'claimed', 'navigating', 'filling', 'validating',
                  'submit_intent', 'verifying', 'auth_required',
                  'mfa_required', 'captcha_required', 'unknown_question',
                  'ambiguous_submission'
              );
            """
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_active_job(custom_path: Path | None = None) -> dict[str, Any] | None:
    """Returns the currently active job being processed if any."""
    init_db(custom_path)
    conn = get_connection(custom_path)
    try:
        row = conn.execute(
            """
            SELECT j.*, a.company, a.title, a.job_url
            FROM automation_jobs j
            JOIN applications a ON a.id = j.app_id
            WHERE j.state IN (
                'claimed', 'navigating', 'filling', 'validating', 'submit_intent', 'verifying',
                'auth_required', 'mfa_required', 'captcha_required', 'unknown_question', 'ambiguous_submission'
            )
            ORDER BY j.updated_at DESC LIMIT 1;
            """
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_latest_job(custom_path: Path | None = None) -> dict[str, Any] | None:
    """Returns the most recently updated automation job with linked application details."""
    init_db(custom_path)
    conn = get_connection(custom_path)
    try:
        row = conn.execute(
            """
            SELECT j.*, a.company, a.title, a.job_url
            FROM automation_jobs j
            JOIN applications a ON a.id = j.app_id
            ORDER BY j.updated_at DESC LIMIT 1;
            """
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def clear_automation_state(
    custom_path: Path | None = None,
) -> dict[str, Any]:
    """
    Fully resets all runtime locks, clears emergency stop / pause flags,
    resets stuck claimed/in-flight jobs back to ready, and closes any orphaned
    browser sessions so the system returns to an operable ready state.
    """
    init_db(custom_path)
    now_dt = datetime.now()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    # 1. Close any active in-memory browser automator instance if alive
    browser_closed = False
    try:
        from job_applier.automation.browser_automator import get_active_automator

        active_automator = get_active_automator()
        if active_automator is not None:
            active_automator.close()
            browser_closed = True
    except Exception as ex:
        logger.debug(f"Active automator close exception during state clear: {ex}")

    # 2. Reset runtime_control table and release stuck jobs atomically
    reset_jobs_count = 0
    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                # Reset runtime_control flags
                conn.execute(
                    """
                    UPDATE runtime_control
                    SET is_paused = 0,
                        is_stopped = 0,
                        manual_takeover_owner = NULL,
                        manual_takeover_expires_at = NULL,
                        browser_active = 0,
                        browser_url = '',
                        browser_page_text = '',
                        browser_is_closed = 1,
                        is_waiting_for_code = 0,
                        pending_verification_code = NULL,
                        browser_job_id = NULL,
                        browser_updated_at = ?,
                        updated_at = ?
                    WHERE id = 1;
                    """,
                    (now_str, now_str),
                )

                # Reset stuck or in-flight jobs in automation_jobs back to ready
                # (claimed, navigating, filling, validating, submit_intent, verifying, auth_required, mfa_required, captcha_required)
                cur = conn.execute(
                    """
                    UPDATE automation_jobs
                    SET state = 'ready',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        fencing_generation = fencing_generation + 1,
                        checkpoint = NULL,
                        updated_at = ?
                    WHERE state IN (
                        'claimed', 'navigating', 'filling', 'validating',
                        'submit_intent', 'verifying', 'auth_required',
                        'mfa_required', 'captcha_required'
                    ) AND is_cancelled = 0;
                    """,
                    (now_str,),
                )
                reset_jobs_count = cur.rowcount

            # Record event
            record_event(
                job_id=None,
                app_id=None,
                event_type="automation_state_cleared",
                level="INFO",
                step="maintenance",
                message=f"Operator cleared automation state. Reset {reset_jobs_count} in-flight jobs, reset locks, and cleared emergency stop.",
                details={
                    "reset_jobs_count": reset_jobs_count,
                    "browser_closed": browser_closed,
                },
                conn=conn,
                custom_path=custom_path,
            )
        finally:
            conn.close()

    return {
        "status": "success",
        "is_paused": False,
        "is_stopped": False,
        "browser_closed": browser_closed,
        "reset_jobs_count": reset_jobs_count,
        "message": f"Automation state cleared. Reset {reset_jobs_count} job(s) and restored ready state.",
    }


def reconcile_stranded_jobs(custom_path: Path | None = None) -> list[str]:
    """
    Scans for jobs left in 'submit_intent' or 'verifying' whose lease has expired
    (e.g. Due to worker crash, container SIGKILL, power loss during submission).
    Fails closed: transitions them to 'ambiguous_submission' with error code
    'CRASH_DURING_SUBMISSION', pauses runtime, and emits critical notifications.
    NEVER retries them automatically to eliminate duplicate submission risk.
    Returns list of recovered job IDs.
    """
    init_db(custom_path)
    reconciled: list[str] = []
    now_dt = datetime.now()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    with _QUEUE_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                stranded = conn.execute(
                    """
                    SELECT j.id, j.app_id, j.state, j.adapter, j.fencing_generation, a.company, a.title
                    FROM automation_jobs j
                    LEFT JOIN applications a ON a.id = j.app_id
                    WHERE j.state IN ('submit_intent', 'verifying')
                      AND (j.lease_expires_at IS NULL OR j.lease_expires_at <= ?);
                    """,
                    (now_str,),
                ).fetchall()

                now = now_dt.isoformat()
                for row in stranded:
                    job_id = row["id"]
                    app_id = row["app_id"]
                    prior_state = row["state"]
                    adapter_name = row["adapter"] or "unknown"
                    company = row["company"] or "Unknown Company"
                    gen = row["fencing_generation"]

                    # Transition to ambiguous_submission
                    conn.execute(
                        """
                        UPDATE automation_jobs
                        SET state = ?,
                            lease_owner = NULL,
                            fencing_generation = fencing_generation + 1,
                            lease_expires_at = NULL,
                            checkpoint = 'stranded_crash_recovery',
                            error_code = 'CRASH_DURING_SUBMISSION',
                            updated_at = ?
                        WHERE id = ?;
                        """,
                        (JobState.AMBIGUOUS_SUBMISSION.value, now, job_id),
                    )

                    # Update uncompleted attempt
                    conn.execute(
                        """
                        UPDATE application_attempts
                        SET outcome = 'ambiguous_crash_recovery',
                            confirmation_evidence = 'Worker crashed while in submit_intent/verifying; recovered on startup.',
                            completed_at = ?
                        WHERE job_id = ? AND outcome IS NULL;
                        """,
                        (now, job_id),
                    )

                    # Record audit event
                    record_event(
                        job_id=job_id,
                        app_id=app_id,
                        event_type="application_stranded_recovered",
                        level="CRITICAL",
                        step="recovery",
                        message=(
                            f"Crash during submission detected for {company}: job was stranded in "
                            f"'{prior_state}' with expired lease. Fencing generation incremented to {gen + 1}. "
                            "Marked ambiguous_submission for manual operator inspection."
                        ),
                        details={
                            "prior_state": prior_state,
                            "adapter": adapter_name,
                            "fencing_generation": gen + 1,
                        },
                        conn=conn,
                        custom_path=custom_path,
                    )

                    if app_id:
                        conn.execute(
                            "UPDATE applications SET status = 'ambiguous', notes = 'Worker crashed while in submit_intent/verifying; recovered on startup.', updated_at = ? WHERE id = ?;",
                            (now, app_id),
                        )

                    reconciled.append(job_id)

        finally:
            conn.close()

    if reconciled:
        # Enforce whole-worker pause to halt subsequent claims until operator inspects
        set_runtime_pause(True, custom_path=custom_path)

    # Emit durable notifications outside lock
    for j_id in reconciled:
        enqueue_notification(
            category="submission_ambiguous",
            title="Crash During Submission Recovered",
            message=(
                f"Job {j_id} crashed while in submit_intent/verifying. Marked ambiguous_submission. "
                "Runtime automation paused. Inspect employer portal or email manually."
            ),
            urgency="critical",
            job_id=j_id,
            custom_path=custom_path,
        )

    return reconciled
