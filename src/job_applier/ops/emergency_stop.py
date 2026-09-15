"""
Emergency Stop and Automation Circuit Breaker Module.

Provides:
1. Immediate, atomic emergency halting of all worker automation loops.
2. Active worker lease revocation and generation fence invalidation.
3. Operator manual takeover release.
4. Critical notification broadcast and audit recording.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from job_applier.automation.queue import (
    TAKEOVER_TRANSPORT_LOCK,
    _QUEUE_LOCK,
    _enqueue_notification_on_conn,
    _mark_job_ambiguous_on_conn,
    record_event,
)
from job_applier.db import get_connection, init_db


def _owner_matches_on_conn(conn: Any, owner: str, now_str: str) -> bool:
    row = conn.execute(
        "SELECT manual_takeover_owner, manual_takeover_expires_at FROM runtime_control WHERE id = 1;"
    ).fetchone()
    return bool(
        row
        and row["manual_takeover_owner"] == owner
        and row["manual_takeover_expires_at"]
        and row["manual_takeover_expires_at"] > now_str
    )


def emergency_stop(
    reason: str = "Operator emergency stop engaged",
    custom_db_path: Path | None = None,
    expected_owner: str | None = None,
) -> dict[str, Any]:
    """Atomically halt automation and convert uncertain submissions.

    The transport lock and one ``BEGIN IMMEDIATE`` transaction cover claimant
    validation, ambiguity projections, runtime stop/shutdown state, lease
    revocation, audit event, and notifications. A stale or expired claimant
    therefore commits no destructive side effects.
    """
    init_db(custom_db_path)
    revoked_jobs = 0
    conn = None
    with TAKEOVER_TRANSPORT_LOCK, _QUEUE_LOCK:
        try:
            conn = get_connection(custom_db_path)
            conn.execute("BEGIN IMMEDIATE;")
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if expected_owner is not None and not _owner_matches_on_conn(
                conn, expected_owner, now_str
            ):
                conn.rollback()
                return {
                    "status": "rejected",
                    "reason": "takeover_owner_required",
                    "message": "Only the current takeover owner may engage emergency stop.",
                }

            ambiguous_rows = conn.execute(
                """
                SELECT id, state, lease_owner, fencing_generation
                FROM automation_jobs
                WHERE state IN ('submit_intent', 'verifying')
                ORDER BY id;
                """
            ).fetchall()
            for row in ambiguous_rows:
                message = (
                    f"Emergency stop interrupted {row['state']}; "
                    "manual outcome verification is required."
                )
                _mark_job_ambiguous_on_conn(
                    conn,
                    row["id"],
                    message,
                    custom_path=custom_db_path,
                    expected_worker_id=row["lease_owner"],
                    expected_generation=row["fencing_generation"],
                    checkpoint="emergency_stop_ambiguous",
                    error_code="EMERGENCY_STOP_AMBIGUOUS",
                    operation_key=f"emergency_stop_ambiguous:{row['id']}:{row['fencing_generation']}",
                    event_type="emergency_stop_ambiguous",
                    pause_runtime=False,
                )

            # Revalidate immediately before all final mutations and commit. This
            # also rejects a claimant whose short lease expired during projection.
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if expected_owner is not None and not _owner_matches_on_conn(
                conn, expected_owner, now_str
            ):
                conn.rollback()
                return {
                    "status": "rejected",
                    "reason": "takeover_owner_required",
                    "message": "Only the current takeover owner may engage emergency stop.",
                }

            conn.execute(
                """
                UPDATE runtime_control
                SET is_stopped = 1, is_paused = 1,
                    browser_active = 0, browser_url = '', browser_page_text = '',
                    browser_is_closed = 1, browser_job_id = NULL,
                    browser_shutdown_generation = MAX(
                        COALESCE(browser_shutdown_generation, 0),
                        COALESCE(browser_session_generation, 0)
                    ) + 1,
                    manual_takeover_owner = NULL,
                    manual_takeover_expires_at = NULL,
                    updated_at = ?
                WHERE id = 1;
                """,
                (now_str,),
            )
            cur = conn.execute(
                """
                UPDATE automation_jobs
                SET state = 'ready', lease_owner = NULL,
                    fencing_generation = fencing_generation + 1,
                    lease_expires_at = NULL, updated_at = ?
                WHERE state IN ('claimed', 'navigating', 'filling', 'validating');
                """,
                (now_str,),
            )
            revoked_jobs = cur.rowcount

            record_event(
                job_id=None,
                app_id=None,
                event_type="emergency_stop_engaged",
                level="CRITICAL",
                step="circuit_breaker",
                message=f"EMERGENCY STOP ENGAGED: {reason}. Revoked {revoked_jobs} active worker leases.",
                details={"reason": reason, "revoked_jobs_count": revoked_jobs},
                custom_path=custom_db_path,
                conn=conn,
            )
            _enqueue_notification_on_conn(
                conn,
                category="system_failure",
                title="Global Emergency Stop Engaged",
                message=f"System emergency stop triggered: {reason}. All workers halted.",
                urgency="critical",
                severity="critical",
                payload={"reason": reason, "revoked_jobs": revoked_jobs},
            )
            # Validate once more so an expired lease cannot commit a partial stop.
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if expected_owner is not None and not _owner_matches_on_conn(
                conn, expected_owner, now_str
            ):
                conn.rollback()
                return {
                    "status": "rejected",
                    "reason": "takeover_owner_required",
                    "message": "Only the current takeover owner may engage emergency stop.",
                }
            conn.commit()
            return {
                "status": "success",
                "is_stopped": True,
                "is_paused": True,
                "revoked_leases_count": revoked_jobs,
                "message": f"Emergency stop engaged. {revoked_jobs} leases revoked.",
                "timestamp": now_str,
            }
        except Exception:
            if conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()


def clear_emergency_stop(
    reason: str = "Operator cleared emergency stop",
    custom_db_path: Path | None = None,
    expected_owner: str | None = None,
) -> dict[str, Any]:
    """
    Clears the global emergency stop flag (is_stopped = 0).
    Note: is_paused remains 1 so workers do not automatically resume until
    the operator issues an explicit resume command.

    When expected_owner is supplied, ownership validation and flag clearing are
    transport-fenced together so a stale resume cannot clear a replacement stop.
    """
    init_db(custom_db_path)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with TAKEOVER_TRANSPORT_LOCK:
        conn = get_connection(custom_db_path)
        try:
            with conn:
                if expected_owner is not None and not _owner_matches_on_conn(
                    conn, expected_owner, now_str
                ):
                    return {
                        "status": "rejected",
                        "reason": "takeover_owner_required",
                        "message": "Only the current takeover owner may clear emergency stop.",
                    }
                conn.execute(
                    """
                    UPDATE runtime_control
                    SET is_stopped = 0,
                        updated_at = ?
                    WHERE id = 1;
                    """,
                    (now_str,),
                )

            record_event(
                job_id=None,
                app_id=None,
                event_type="emergency_stop_cleared",
                level="INFO",
                step="circuit_breaker",
                message=f"Emergency stop cleared by operator: {reason}. Workers remain paused awaiting safe resume.",
                details={"reason": reason},
                custom_path=custom_db_path,
            )

            return {
                "status": "success",
                "is_stopped": False,
                "is_paused": True,
                "message": "Emergency stop cleared. Automation remains paused awaiting safe resume.",
                "timestamp": now_str,
            }
        finally:
            conn.close()
