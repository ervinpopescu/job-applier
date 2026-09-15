from __future__ import annotations

import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

from job_applier.automation.network_security import validate_target_url
from job_applier.automation.queue import (
    TAKEOVER_TRANSPORT_LOCK,
    _QUEUE_LOCK,
    complete_attempt,
    get_active_job,
    record_event,
    set_runtime_pause,
    takeover_owner_matches,
    transition_job,
)
from job_applier.db import (
    get_connection,
    init_db,
    record_submission_timestamp,
)


def safe_resume_revalidate(
    job_id: str | None = None,
    automator: Any | None = None,
    custom_path: Path | None = None,
    current_url: str | None = None,
    expected_owner: str | None = None,
) -> dict[str, Any]:
    """
    Revalidates browser, page URL, authentication, and submission state
    before releasing pause and resuming automated execution.
    Ensures safe transition and prevents duplicate submissions.
    """
    init_db(custom_path)

    # Safe resume owns the complete release transition. Import lazily to avoid
    # coupling module initialization while keeping stop clearing inside the
    # transport-fenced sections below.
    from job_applier.ops.emergency_stop import clear_emergency_stop

    if expected_owner is not None and not _takeover_owner_matches(
        expected_owner, custom_path
    ):
        return {
            "status": "rejected",
            "reason": "takeover_owner_required",
            "message": "Only the current takeover owner may resume automation.",
        }

    # Browser ownership is authoritative when a headed session is active. Falling
    # back to the newest exception job can resume a different application when
    # multiple auth-required jobs exist (for example, BestJobs plus LinkedIn).
    from job_applier.automation.queue import get_runtime_browser_state

    browser_state = get_runtime_browser_state(custom_path)
    browser_job_id = browser_state.get("browser_job_id")
    active_job = None
    if (
        browser_job_id
        and browser_state.get("browser_active")
        and not browser_state.get("browser_is_closed", True)
    ):
        conn = get_connection(custom_path)
        try:
            owner_row = conn.execute(
                """
                SELECT j.*, a.company, a.title, a.job_url
                FROM automation_jobs j
                JOIN applications a ON a.id = j.app_id
                WHERE j.id = ?
                  AND j.fencing_generation = ?
                  AND j.state IN (
                    'claimed', 'navigating', 'filling', 'validating',
                    'submit_intent', 'verifying', 'auth_required',
                    'mfa_required', 'captcha_required', 'unknown_question',
                    'ambiguous_submission'
                );
                """,
                (browser_job_id, browser_state.get("browser_session_generation")),
            ).fetchone()
        finally:
            conn.close()
        if owner_row is None:
            return {
                "status": "rejected",
                "reason": "browser_owner_invalid",
                "message": "Active browser session is not owned by a resumable automation job.",
            }
        active_job = dict(owner_row)
    else:
        active_job = get_active_job(custom_path)

    # 1. If no active job is in flight, unpausing is universally safe
    if not active_job:
        with TAKEOVER_TRANSPORT_LOCK:
            cleared_stop = clear_emergency_stop(
                reason="Safe resume approved",
                custom_db_path=custom_path,
                expected_owner=expected_owner,
            )
            if cleared_stop.get("status") == "rejected":
                return cleared_stop
            if not _clear_takeover_state(
                expected_owner=expected_owner, custom_path=custom_path
            ):
                return {
                    "status": "rejected",
                    "reason": "takeover_owner_required",
                    "message": "Only the current takeover owner may resume automation.",
                }
            set_runtime_pause(False, custom_path=custom_path)
        record_event(
            job_id="",
            app_id="",
            event_type="safe_resume_approved",
            level="INFO",
            step="resume",
            message="No active job in flight; automation unpaused safely.",
            custom_path=custom_path,
        )
        return {
            "status": "success",
            "action": "unpaused",
            "message": "No active job in progress; automation resumed safely.",
        }

    job_id_val = str(active_job["id"])
    app_id_val = str(active_job["app_id"])
    fencing_gen_val = int(active_job.get("fencing_generation", 1))
    lease_owner_val = str(active_job.get("lease_owner") or "operator")

    target_job_id = job_id or job_id_val
    if job_id_val != target_job_id:
        return {
            "status": "rejected",
            "reason": "job_mismatch",
            "message": f"Requested resume for job {target_job_id}, but active claimed job is {job_id_val}.",
        }

    # 2. Revalidate browser and page state
    # Browser state is read from the durable runtime snapshot. Playwright is
    # thread-affine, so this request/CLI orchestration path must never inspect
    # an automator page directly; the owning runtime worker publishes its URL.
    resolved_url = current_url or ""
    if not resolved_url:
        if not browser_state.get("browser_active") or browser_state.get(
            "browser_is_closed", 1
        ):
            return {
                "status": "rejected",
                "reason": "browser_unavailable",
                "message": "No active browser session available for revalidation. Ensure runtime is running and browser is open before resuming.",
            }
        resolved_url = browser_state.get("browser_url") or ""

    current_url = resolved_url
    if not current_url:
        return {
            "status": "rejected",
            "reason": "browser_unavailable",
            "message": "Browser session has no current URL. Cannot safely resume.",
        }

    # Validate URL security and DNS policy
    allowed, sec_reason = validate_target_url(current_url)
    if not allowed:
        return {
            "status": "rejected",
            "reason": "untrusted_url",
            "message": f"Browser is currently navigated to a restricted URL: {sec_reason}",
        }

    # Check expected domain against application details
    conn = get_connection(custom_path)
    try:
        row = conn.execute(
            "SELECT * FROM applications WHERE id = ?;", (app_id_val,)
        ).fetchone()
        app_data = dict(row) if row else {}
    finally:
        conn.close()

    expected_job_url = app_data.get("job_url", "")
    if expected_job_url:
        exp_parsed = urllib.parse.urlparse(expected_job_url)
        cur_parsed = urllib.parse.urlparse(current_url)
        exp_netloc = exp_parsed.netloc.lower()
        cur_netloc = cur_parsed.netloc.lower()

        same_host = (
            exp_netloc == cur_netloc
            or cur_netloc.endswith(f".{exp_netloc}")
            or exp_netloc.endswith(f".{cur_netloc}")
        )

        if not same_host:
            return {
                "status": "rejected",
                "reason": "domain_mismatch",
                "message": (
                    f"Active browser page ('{cur_netloc}') does not match the application "
                    f"target domain ('{exp_netloc}'). Navigate back to the application before resuming."
                ),
            }

        if (
            "greenhouse.io" in exp_netloc
            or "lever.co" in exp_netloc
            or "ashbyhq.com" in exp_netloc
        ):
            exp_tenant = (
                exp_parsed.path.strip("/").split("/")[0]
                if exp_parsed.path.strip("/")
                else ""
            )
            cur_tenant = (
                cur_parsed.path.strip("/").split("/")[0]
                if cur_parsed.path.strip("/")
                else ""
            )
            if exp_tenant and cur_tenant and exp_tenant != cur_tenant:
                return {
                    "status": "rejected",
                    "reason": "tenant_mismatch",
                    "message": (
                        f"Active browser page tenant ('{cur_tenant}') does not match expected "
                        f"application tenant ('{exp_tenant}')."
                    ),
                }

    # Check if user manually completed the application during takeover
    confirmed_by_adapter = False
    evidence_text = ""
    adapter_name = str(active_job.get("adapter") or "generic").lower()
    # Confirmation is intentionally URL/state based here. Playwright page
    # inspection belongs to the owning worker thread and must not occur from a
    # web/API or embedded orchestration thread.
    page = None

    if adapter_name != "generic":
        if page is not None:
            try:
                from job_applier.automation.adapters.registry import get_adapter_by_name

                adapter = get_adapter_by_name(adapter_name)
                evidence = adapter.confirm_submission(page)
                if evidence.confirmed and not evidence.is_ambiguous:
                    confirmed_by_adapter = True
                    evidence_text = (
                        evidence.confirmation_text
                        or f"Confirmed via {adapter.adapter_name}"
                    )
            except Exception:
                pass

        if not confirmed_by_adapter:
            url_lower = current_url.lower()
            if adapter_name == "greenhouse" and "greenhouse.io" in url_lower:
                if "/confirmation" in url_lower:
                    confirmed_by_adapter = True
                    evidence_text = (
                        f"Confirmed via Greenhouse confirmation at {current_url}"
                    )
            elif adapter_name == "lever" and "lever.co" in url_lower:
                if "/applied" in url_lower or "/thanks" in url_lower:
                    confirmed_by_adapter = True
                    evidence_text = f"Confirmed via Lever confirmation at {current_url}"
            elif adapter_name == "ashby" and "ashbyhq.com" in url_lower:
                if "/confirmation" in url_lower:
                    confirmed_by_adapter = True
                    evidence_text = f"Confirmed via Ashby confirmation at {current_url}"

    if confirmed_by_adapter:
        # Successfully completed via takeover with adapter-specific evidence!
        # Keep transition, lease clearing, and unpause under one transport fence
        # so a stale claimant cannot affect a replacement claimant.
        with TAKEOVER_TRANSPORT_LOCK, _QUEUE_LOCK:
            if expected_owner is not None and not _takeover_owner_matches(
                expected_owner, custom_path
            ):
                return {
                    "status": "rejected",
                    "reason": "takeover_owner_required",
                    "message": "Only the current takeover owner may resume automation.",
                }
            claim_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn = get_connection(custom_path)
            try:
                claim_row = conn.execute(
                    """
                    SELECT 1 FROM automation_jobs
                    WHERE id = ?
                      AND state IN (
                        'auth_required', 'mfa_required', 'captcha_required', 'unknown_question',
                        'claimed', 'navigating', 'filling', 'validating', 'retry_wait'
                      )
                      AND fencing_generation = ?
                      AND lease_owner = ?
                      AND lease_expires_at > ?;
                    """,
                    (job_id_val, fencing_gen_val, lease_owner_val, claim_now),
                ).fetchone()
            finally:
                conn.close()
            if claim_row is None:
                return {
                    "status": "rejected",
                    "reason": "lease_fenced_or_expired",
                    "message": "The automation job claim changed or expired while validating resume.",
                }
            transitioned = transition_job(
                job_id=job_id_val,
                worker_id=lease_owner_val,
                generation=fencing_gen_val,
                to_state="applied",
                checkpoint="manual_takeover_completed",
                custom_path=custom_path,
                require_live_lease=True,
            )
            if not transitioned:
                return {
                    "status": "rejected",
                    "reason": "lease_fenced_or_expired",
                    "message": "Failed transitioning job state: lease expired or fencing generation mismatch.",
                }

            cleared_stop = clear_emergency_stop(
                reason="Safe resume approved after confirmed submission",
                custom_db_path=custom_path,
                expected_owner=expected_owner,
            )
            if cleared_stop.get("status") == "rejected":
                return cleared_stop
            if not _clear_takeover_state(
                expected_owner=expected_owner, custom_path=custom_path
            ):
                return {
                    "status": "rejected",
                    "reason": "takeover_owner_required",
                    "message": "Only the current takeover owner may resume automation.",
                }
            set_runtime_pause(False, custom_path=custom_path)
            # Keep all resulting durable submission projections behind the same
            # fence too. A replacement claimant must not appear between the job
            # transition and its application/attempt/event updates.
            attempt_row = None
            conn = get_connection(custom_path)
            try:
                attempt_row = conn.execute(
                    "SELECT id FROM application_attempts WHERE job_id = ? ORDER BY created_at DESC LIMIT 1;",
                    (job_id_val,),
                ).fetchone()
            finally:
                conn.close()

            if attempt_row:
                complete_attempt(
                    attempt_id=attempt_row["id"],
                    outcome="applied",
                    confirmation_evidence=f"{evidence_text} at {current_url} during operator takeover",
                    custom_path=custom_path,
                )

            record_submission_timestamp(adapter_name, custom_path=custom_path)

            if app_id_val:
                conn = get_connection(custom_path)
                try:
                    with conn:
                        conn.execute(
                            "UPDATE applications SET status = 'applied', updated_at = datetime('now') WHERE id = ?;",
                            (app_id_val,),
                        )
                finally:
                    conn.close()

            record_event(
                job_id=job_id_val,
                app_id=app_id_val,
                event_type="application_applied",
                level="SUCCESS",
                step="applied",
                message=f"Application for {app_data.get('company', 'target company')} verified and marked applied after manual takeover.",
                custom_path=custom_path,
            )
        return {
            "status": "success",
            "action": "marked_applied",
            "message": "Application confirmed as submitted during takeover. State updated to applied and worker resumed.",
        }

    # 3. If revalidation passed, safely release takeover and unpause
    if active_job and active_job.get("state") == "ambiguous_submission":
        return {
            "status": "rejected",
            "reason": "ambiguous_submission_blocked",
            "message": "Job is in ambiguous_submission outcome. Automatic resume is blocked; explicit operator resolution is required.",
        }

    rows_updated = 0
    # Final ownership check, job mutation, lease clearing, and unpause must be
    # one transport-fenced transition. Otherwise a replaced claimant can mutate
    # the old job or unpause the new claimant's session.
    with TAKEOVER_TRANSPORT_LOCK, _QUEUE_LOCK:
        if expected_owner is not None and not _takeover_owner_matches(
            expected_owner, custom_path
        ):
            return {
                "status": "rejected",
                "reason": "takeover_owner_required",
                "message": "Only the current takeover owner may resume automation.",
            }
        claim_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = get_connection(custom_path)
        try:
            with conn:
                cursor = conn.execute(
                    """
                    UPDATE automation_jobs
                    SET state = 'ready',
                        lease_owner = NULL,
                        lease_expires_at = NULL,
                        error_code = NULL,
                        error_message = NULL,
                        updated_at = datetime('now')
                    WHERE id = ?
                      AND state IN (
                        'auth_required', 'mfa_required', 'captcha_required', 'unknown_question',
                        'claimed', 'navigating', 'filling', 'validating', 'retry_wait'
                      )
                      AND fencing_generation = ?
                      AND lease_owner = ?
                      AND lease_expires_at > ?;
                    """,
                    (job_id_val, fencing_gen_val, lease_owner_val, claim_now),
                )
                rows_updated = cursor.rowcount
        finally:
            conn.close()

        if rows_updated == 0:
            conn = get_connection(custom_path)
            try:
                current_job = conn.execute(
                    "SELECT fencing_generation, lease_owner, lease_expires_at, state FROM automation_jobs WHERE id = ?;",
                    (job_id_val,),
                ).fetchone()
            finally:
                conn.close()
            if current_job is not None and (
                current_job["fencing_generation"] != fencing_gen_val
                or current_job["lease_owner"] != lease_owner_val
                or not current_job["lease_expires_at"]
                or current_job["lease_expires_at"] <= claim_now
            ):
                return {
                    "status": "rejected",
                    "reason": "lease_fenced_or_expired",
                    "message": "The automation job claim changed or expired while validating resume.",
                }
            return {
                "status": "rejected",
                "reason": "job_not_resumable",
                "message": f"Job {job_id_val} in state '{active_job.get('state')}' could not be resumed.",
            }

        cleared_stop = clear_emergency_stop(
            reason="Safe resume approved",
            custom_db_path=custom_path,
            expected_owner=expected_owner,
        )
        if cleared_stop.get("status") == "rejected":
            return cleared_stop
        if not _clear_takeover_state(
            expected_owner=expected_owner, custom_path=custom_path
        ):
            return {
                "status": "rejected",
                "reason": "takeover_owner_required",
                "message": "Only the current takeover owner may resume automation.",
            }
        set_runtime_pause(False, custom_path=custom_path)
    record_event(
        job_id=job_id_val,
        app_id=app_id_val,
        event_type="safe_resume_approved",
        level="INFO",
        step="resume",
        message="Safe resume revalidation passed. Automation resumed safely.",
        custom_path=custom_path,
    )

    return {
        "status": "success",
        "action": "resumed",
        "message": "Safe resume revalidation passed. Automation worker resumed.",
    }


def _takeover_owner_matches(owner: str, custom_path: Path | None = None) -> bool:
    """Check claimant ownership and expiry under the relay transport fence."""
    return takeover_owner_matches(owner, custom_path)


def _clear_takeover_state(
    custom_path: Path | None = None, expected_owner: str | None = None
) -> bool:
    """Clear takeover while fencing relay sends; return false for stale claimant."""
    now_str = ""
    from datetime import datetime

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Fence takeover revocation against the viewer relay's in-flight send.
    with TAKEOVER_TRANSPORT_LOCK:
        conn = get_connection(custom_path)
        try:
            with conn:
                if expected_owner is not None and not _takeover_owner_matches(
                    expected_owner, custom_path
                ):
                    return False
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
                return True
        finally:
            conn.close()
    return False
