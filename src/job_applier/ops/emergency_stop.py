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
    enqueue_notification,
    record_event,
)
from job_applier.db import get_connection, init_db


def emergency_stop(
    reason: str = "Operator emergency stop engaged",
    custom_db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Halts all automation immediately across the entire system.
    Guarantees:
    1. Sets is_stopped = 1 and is_paused = 1 in runtime_control atomically.
    2. Revokes all active worker leases (resets claimed/navigating/filling to ready).
    3. Releases any active manual takeover lock.
    4. Dispatches critical durable notification.
    """
    init_db(custom_db_path)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_connection(custom_db_path)
    revoked_jobs = 0
    try:
        with conn:
            # 1. Update runtime_control
            conn.execute(
                """
                UPDATE runtime_control
                SET is_stopped = 1,
                    is_paused = 1,
                    manual_takeover_owner = NULL,
                    manual_takeover_expires_at = NULL,
                    updated_at = ?
                WHERE id = 1;
                """,
                (now_str,),
            )

            # 2. Revoke active worker leases in non-terminal states
            # Jobs in claimed, navigating, filling, validating are reset to ready
            cur = conn.execute(
                """
                UPDATE automation_jobs
                SET state = 'ready',
                    lease_owner = NULL,
                    fencing_generation = fencing_generation + 1,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE state IN ('claimed', 'navigating', 'filling', 'validating');
                """,
                (now_str,),
            )
            revoked_jobs = cur.rowcount

            # 3. For jobs currently in submit_intent or verifying, do NOT blindly reset to ready!
            # Move to ambiguous_submission with checkpoint to prevent double submissions.
            conn.execute(
                """
                UPDATE automation_jobs
                SET state = 'ambiguous_submission',
                    checkpoint = 'emergency_stop_ambiguous',
                    lease_owner = NULL,
                    fencing_generation = fencing_generation + 1,
                    lease_expires_at = NULL,
                    updated_at = ?
                WHERE state IN ('submit_intent', 'verifying');
                """,
                (now_str,),
            )

        # 4. Record audit event
        record_event(
            job_id=None,
            app_id=None,
            event_type="emergency_stop_engaged",
            level="CRITICAL",
            step="circuit_breaker",
            message=f"EMERGENCY STOP ENGAGED: {reason}. Revoked {revoked_jobs} active worker leases.",
            details={"reason": reason, "revoked_jobs_count": revoked_jobs},
            custom_path=custom_db_path,
        )

        # 5. Emit durable critical alert to in-app notifications AND outbox
        enqueue_notification(
            category="system_failure",
            title="Global Emergency Stop Engaged",
            message=f"System emergency stop triggered: {reason}. All workers halted.",
            urgency="critical",
            severity="critical",
            payload={"reason": reason, "revoked_jobs": revoked_jobs},
            custom_path=custom_db_path,
        )

        return {
            "status": "success",
            "is_stopped": True,
            "is_paused": True,
            "revoked_leases_count": revoked_jobs,
            "message": f"Emergency stop engaged. {revoked_jobs} leases revoked.",
            "timestamp": now_str,
        }
    finally:
        conn.close()


def clear_emergency_stop(
    reason: str = "Operator cleared emergency stop",
    custom_db_path: Path | None = None,
) -> dict[str, Any]:
    """
    Clears the global emergency stop flag (is_stopped = 0).
    Note: is_paused remains 1 so workers do not automatically resume until
    the operator issues an explicit resume command.
    """
    init_db(custom_db_path)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_connection(custom_db_path)
    try:
        with conn:
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
