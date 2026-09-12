from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from job_applier.automation.adapters.models import ConfirmationEvidence
from job_applier.automation.browser_automator import BrowserAutomator
from job_applier.automation.candidate_profile import load_candidate_profile
from job_applier.automation.safety_guard import (
    DailySubmissionLimitExceededError,
    SubmissionPacingViolationError,
    SubmissionSafetyGuard,
)
from job_applier.automation.queue import (
    JobState,
    claim_next_job,
    complete_attempt,
    consume_pending_verification_code,
    create_attempt,
    get_runtime_control,
    handle_job_failure,
    reconcile_stranded_jobs,
    record_event,
    renew_lease,
    reschedule_for_safety_gate,
    set_runtime_browser_state,
    set_runtime_pause,
    transition_job,
    validate_application_artifacts,
)
from job_applier.automation.runtime_lock import RuntimeSingletonLock, WorkerLockError
from job_applier.db import get_connection, init_db
from job_applier.utils import get_project_root


def _browser_session_is_open(automator: BrowserAutomator | None) -> bool:
    """Return whether an automator still owns at least one usable browser page."""
    context = getattr(automator, "context", None)
    if context is None:
        return False
    try:
        return any(not page.is_closed() for page in context.pages)
    except Exception:
        page = getattr(automator, "page", None)
        try:
            return page is not None and not page.is_closed()
        except Exception:
            return False


def _sync_runtime_browser_state_from_automator(
    automator: BrowserAutomator,
    job_id: str,
    custom_db_path: Path | None = None,
) -> bool:
    """Persist the actual browser state without marking a live page closed."""
    page = getattr(automator, "page", None)
    if page is None:
        try:
            page = next(
                page for page in automator.context.pages if not page.is_closed()
            )
        except Exception:
            page = None

    try:
        is_closed = page is None or page.is_closed()
    except Exception:
        is_closed = True

    if page is None or is_closed:
        set_runtime_browser_state(
            active=False,
            is_closed=True,
            browser_job_id=job_id,
            custom_path=custom_db_path,
        )
        return False

    current_url = page.url or ""
    try:
        page_text = (page.locator("body").text_content() or "")[:4000]
    except Exception:
        page_text = ""
    set_runtime_browser_state(
        active=True,
        url=current_url,
        page_text=page_text,
        is_closed=False,
        is_waiting_for_code=getattr(automator, "waiting_for_code", False),
        browser_job_id=job_id,
        custom_path=custom_db_path,
    )
    return True


class HeartbeatThread(threading.Thread):
    """Refreshes the worker's lease every interval seconds in the background."""

    def __init__(
        self,
        job_id: str,
        worker_id: str,
        generation: int,
        interval: float = 10.0,
        lease_seconds: int = 60,
        custom_path: Path | None = None,
    ):
        super().__init__(daemon=True)
        self.job_id = job_id
        self.worker_id = worker_id
        self.generation = generation
        self.interval = interval
        self.lease_seconds = lease_seconds
        self.custom_path = custom_path
        self._stop_event = threading.Event()
        self.lost_lease = False

    def run(self) -> None:
        while not self._stop_event.wait(self.interval):
            success = renew_lease(
                job_id=self.job_id,
                worker_id=self.worker_id,
                generation=self.generation,
                lease_seconds=self.lease_seconds,
                custom_path=self.custom_path,
            )
            if not success:
                self.lost_lease = True
                print(f"⚠️ Worker {self.worker_id} lost lease on job {self.job_id}!")
                break

    def stop(self) -> None:
        self._stop_event.set()


def process_claimed_job(
    job: Any,
    worker_id: str,
    headless: bool = True,
    browser_engine: str = "chromium",
    custom_db_path: Path | None = None,
    stop_event: threading.Event | None = None,
) -> str:
    """
    Executes the application pipeline for a claimed job with fencing, submit_intent,
    and post-submit crash ambiguity protection.
    """
    project_root = get_project_root()
    init_db(custom_db_path)

    # 0. Enforce storage volume headroom guard
    from job_applier.ops.disk_guard import enforce_disk_pressure_guard

    if not enforce_disk_pressure_guard(custom_db_path=custom_db_path):
        print(f"⏸️ Server storage headroom critical; releasing job {job.id}")
        return "paused"

    conn = get_connection(custom_db_path)

    # 1. Fetch application details
    try:
        app_row = conn.execute(
            "SELECT * FROM applications WHERE id = ?;", (job.app_id,)
        ).fetchone()
        if not app_row:
            handle_job_failure(
                job_id=job.id,
                worker_id=worker_id,
                generation=job.fencing_generation,
                attempt_id=None,
                error=f"Application {job.app_id} not found in database",
                is_pre_submit=True,
                error_category="site_changed",
                custom_path=custom_db_path,
            )
            return "failed"
        app_data = dict(app_row)
    finally:
        conn.close()

    # 1b. Reject indirect aggregator URLs early (Jobicy)
    job_url = str(app_data.get("job_url", "")).lower()
    if "jobicy.com" in job_url or "jobicy." in job_url:
        handle_job_failure(
            job_id=job.id,
            worker_id=worker_id,
            generation=job.fencing_generation,
            attempt_id=None,
            error=f"Jobicy aggregator landing page blocked: {app_data.get('job_url')}. Direct employer application required.",
            is_pre_submit=True,
            error_category="failed_permanent",
            custom_path=custom_db_path,
        )
        return "failed"

    # 2. Verify artifacts on disk via shared queue resolver
    valid_artifacts, cv_result = validate_application_artifacts(
        job.app_id, custom_path=custom_db_path
    )
    if not valid_artifacts:
        handle_job_failure(
            job_id=job.id,
            worker_id=worker_id,
            generation=job.fencing_generation,
            attempt_id=None,
            error=cv_result,
            is_pre_submit=True,
            error_category="missing_artifact",
            custom_path=custom_db_path,
        )
        return "failed"

    cv_pdf = Path(cv_result)
    app_dir = cv_pdf.parent

    # 3. Snapshot candidate profile & master resume
    profile = load_candidate_profile()
    profile_snapshot = profile.to_dict() if hasattr(profile, "to_dict") else {}

    resume_snapshot = ""
    resume_file = project_root / "data" / "master_resume.json"
    if resume_file.exists():
        try:
            resume_snapshot = resume_file.read_text(encoding="utf-8")[:4000]
        except Exception:
            pass

    artifact_revisions = {
        "cv_filename": cv_pdf.name,
        "cv_mtime": cv_pdf.stat().st_mtime,
        "folder": str(app_dir),
    }

    # 4. Create immutable attempt record
    attempt = create_attempt(
        job_id=job.id,
        app_id=job.app_id,
        profile_snapshot=profile_snapshot,
        resume_snapshot=resume_snapshot,
        artifact_revisions=artifact_revisions,
        worker_id=worker_id,
        lease_generation=job.fencing_generation,
        browser_job_id=job.id,
        custom_path=custom_db_path,
    )
    event_worker_id = worker_id if job.lease_owner == worker_id else None
    event_generation = job.fencing_generation if event_worker_id else None
    record_event(
        job_id=job.id,
        app_id=job.app_id,
        attempt_id=attempt.id,
        attempt_number=attempt.attempt_number,
        event_key="attempt_started",
        worker_id=event_worker_id,
        lease_generation=event_generation,
        browser_job_id=job.id,
        event_type="attempt_started",
        step="started",
        message="Automation attempt started.",
        custom_path=custom_db_path,
    )

    # 5. Start background lease heartbeat
    heartbeat = HeartbeatThread(
        job_id=job.id,
        worker_id=worker_id,
        generation=job.fencing_generation,
        interval=10.0,
        lease_seconds=60,
        custom_path=custom_db_path,
    )
    heartbeat.start()

    automator = None
    is_pre_submit = True
    keep_browser_open = False
    is_service_daemon = os.environ.get("JOB_APPLIER_RUNTIME_MODE") == "service"

    try:
        # Pre-execution checks
        ctrl = get_runtime_control(custom_db_path)
        if ctrl.get("is_paused") or ctrl.get("is_stopped"):
            print(f"⏸️ Automation paused/stopped; releasing job {job.id}")
            return "paused"

        if heartbeat.lost_lease:
            print(f"⚠️ Lease lost prior to browser startup; releasing job {job.id}")
            return "lease_lost"

        # Early safety check: pacing and daily limits before expensive browser launch & navigation
        try:
            SubmissionSafetyGuard.validate_pacing_and_daily_limits(
                adapter_name=job.adapter, custom_db_path=custom_db_path
            )
        except SubmissionPacingViolationError as pacing_err:
            print(
                f"⏳ Pacing limit active for {job.adapter}: {pacing_err}. Rescheduling job for retry."
            )
            reschedule_for_safety_gate(
                job_id=job.id,
                worker_id=worker_id,
                generation=job.fencing_generation,
                retry_delay_seconds=300,
                reason=str(pacing_err),
                attempt_id=attempt.id,
                custom_path=custom_db_path,
            )
            return "retry_wait"
        except DailySubmissionLimitExceededError as daily_err:
            print(
                f"⏳ Daily limit reached for {job.adapter}: {daily_err}. Rescheduling job for retry."
            )
            reschedule_for_safety_gate(
                job_id=job.id,
                worker_id=worker_id,
                generation=job.fencing_generation,
                retry_delay_seconds=3600,
                reason=str(daily_err),
                attempt_id=attempt.id,
                custom_path=custom_db_path,
            )
            return "retry_wait"

        is_service_daemon = os.environ.get("JOB_APPLIER_RUNTIME_MODE") == "service"
        automator = BrowserAutomator(
            headless=headless,
            browser=browser_engine,
            skip_profile_lock=is_service_daemon,
        )
        automator.cancellation_check = lambda: (
            heartbeat.lost_lease or (stop_event is not None and stop_event.is_set())
        )
        automator.start()
        page = getattr(automator, "page", None)
        page_url = getattr(page, "url", "") if page else ""
        if not isinstance(page_url, str):
            page_url = ""
        set_runtime_browser_state(
            active=True,
            url=page_url,
            is_closed=False,
            browser_job_id=job.id,
            custom_path=custom_db_path,
        )

        # Step 1: Navigating
        transition_job(
            job_id=job.id,
            worker_id=worker_id,
            generation=job.fencing_generation,
            to_state=JobState.NAVIGATING.value,
            checkpoint="navigating",
            custom_path=custom_db_path,
        )

        def submit_intent_gate() -> None:
            nonlocal is_pre_submit
            if heartbeat.lost_lease:
                raise RuntimeError("Worker lost lease before submit intent.")
            # Central safety and ownership verification before committing submit intent
            SubmissionSafetyGuard.validate_pre_submit_safety(
                job_id=job.id,
                worker_id=worker_id,
                generation=job.fencing_generation,
                adapter_name=job.adapter,
                profile_snapshot=profile_snapshot,
                artifact_revisions=artifact_revisions,
                is_mock=False,
                custom_db_path=custom_db_path,
            )
            SubmissionSafetyGuard.execute_submit_intent(
                job_id=job.id,
                attempt_id=attempt.id,
                worker_id=worker_id,
                generation=job.fencing_generation,
                custom_db_path=custom_db_path,
            )
            is_pre_submit = False

        def verifying_gate() -> None:
            transition_job(
                job_id=job.id,
                worker_id=worker_id,
                generation=job.fencing_generation,
                to_state=JobState.VERIFYING.value,
                checkpoint="submitted_awaiting_confirmation",
                custom_path=custom_db_path,
            )

        # An explicit auth retry only reopens the controlled browser. It must not
        # fill or submit a form before the operator has authenticated.
        if job.checkpoint == "auth_retry_requested":
            navigation_succeeded = automator.navigate_and_open_form(app_data["job_url"])
            if not navigation_succeeded:
                # Keep the headed browser available even when an unauthenticated
                # page cannot be evaluated as active. The operator may need to
                # complete login before the posting becomes inspectable.
                set_runtime_pause(True, custom_path=custom_db_path)
                transition_job(
                    job_id=job.id,
                    worker_id=worker_id,
                    generation=job.fencing_generation,
                    to_state=JobState.AUTH_REQUIRED.value,
                    checkpoint="auth_recovery_navigation_failed",
                    error_code="AUTH_REQUIRED",
                    error_message="Authentication browser remains open; operator review required.",
                    custom_path=custom_db_path,
                )
                browser_open = _browser_session_is_open(automator)
                if browser_open:
                    page = getattr(automator, "page", None)
                    set_runtime_browser_state(
                        active=True,
                        url=getattr(page, "url", "") if page else "",
                        is_closed=False,
                        browser_job_id=job.id,
                        custom_path=custom_db_path,
                    )
                    keep_browser_open = (
                        is_service_daemon
                        and browser_open
                        and not (stop_event is not None and stop_event.is_set())
                    )
                return JobState.AUTH_REQUIRED.value
            set_runtime_pause(True, custom_path=custom_db_path)
            transition_job(
                job_id=job.id,
                worker_id=worker_id,
                generation=job.fencing_generation,
                to_state=JobState.AUTH_REQUIRED.value,
                checkpoint="auth_recovery_open",
                error_code="AUTH_REQUIRED",
                error_message="Authentication required - operator takeover needed.",
                custom_path=custom_db_path,
            )
            record_event(
                job_id=job.id,
                app_id=job.app_id,
                event_type="auth_session_reopened",
                level="WARN",
                step="auth_required",
                message="Fresh controlled browser session opened for operator authentication.",
                custom_path=custom_db_path,
            )
            _coordinate_challenge_takeover(
                automator=automator,
                job_id=job.id,
                worker_id=worker_id,
                heartbeat=heartbeat,
                custom_db_path=custom_db_path,
                max_wait_seconds=None if is_service_daemon else 300,
                stop_event=stop_event,
            )
            keep_browser_open = (
                is_service_daemon
                and not (stop_event is not None and stop_event.is_set())
                and _browser_session_is_open(automator)
            )
            return JobState.AUTH_REQUIRED.value

        status, message = automator.run_autonomous_apply(
            app_dir=app_dir,
            job_url=app_data["job_url"],
            company=app_data["company"],
            job_title=app_data["title"],
            on_submit_intent=submit_intent_gate,
            on_verifying=verifying_gate,
        )

        evidence = getattr(automator, "last_evidence", None)
        if status in ("applied", "ambiguous_submission") or evidence is not None:
            if evidence is None:
                evidence = ConfirmationEvidence(
                    platform=job.adapter,
                    adapter_version="1.0.0",
                    confirmed=(status == "applied"),
                    is_ambiguous=(status == "ambiguous_submission"),
                    confirmation_text=message,
                )
            final_status, _ = SubmissionSafetyGuard.reconcile_submission_outcome(
                job_id=job.id,
                app_id=job.app_id,
                attempt_id=attempt.id,
                worker_id=worker_id,
                generation=job.fencing_generation,
                adapter_name=job.adapter,
                company=app_data["company"],
                evidence=evidence,
                custom_db_path=custom_db_path,
            )
            return final_status

        elif status == "fill_only":
            transition_job(
                job_id=job.id,
                worker_id=worker_id,
                generation=job.fencing_generation,
                to_state=JobState.SKIPPED.value,
                checkpoint="fill_only_completed",
                custom_path=custom_db_path,
            )
            complete_attempt(
                attempt_id=attempt.id,
                outcome="skipped_fill_only",
                confirmation_evidence=message,
                custom_path=custom_db_path,
            )
            return "skipped"

        else:
            final_state = handle_job_failure(
                job_id=job.id,
                worker_id=worker_id,
                generation=job.fencing_generation,
                attempt_id=attempt.id,
                error=message,
                is_pre_submit=is_pre_submit,
                custom_path=custom_db_path,
            )
            return final_state

    except SubmissionPacingViolationError as pacing_err:
        reschedule_for_safety_gate(
            job_id=job.id,
            worker_id=worker_id,
            generation=job.fencing_generation,
            retry_delay_seconds=300,
            reason=str(pacing_err),
            attempt_id=attempt.id,
            custom_path=custom_db_path,
        )
        return "retry_wait"

    except DailySubmissionLimitExceededError as daily_err:
        reschedule_for_safety_gate(
            job_id=job.id,
            worker_id=worker_id,
            generation=job.fencing_generation,
            retry_delay_seconds=3600,
            reason=str(daily_err),
            attempt_id=attempt.id,
            custom_path=custom_db_path,
        )
        return "retry_wait"

    except Exception as e:
        final_state = SubmissionSafetyGuard.handle_exception_pause_and_alert(
            job_id=job.id,
            app_id=job.app_id,
            attempt_id=attempt.id,
            worker_id=worker_id,
            generation=job.fencing_generation,
            exc=e,
            company=app_data.get("company", ""),
            custom_db_path=custom_db_path,
        )

        ctrl = get_runtime_control(custom_db_path)
        err_name = type(e).__name__
        is_challenge = any(
            k in err_name
            for k in (
                "Captcha",
                "MFA",
                "Verification",
                "Authentication",
                "Login",
                "UnknownQuestion",
                "SensitiveQuestion",
                "Ambiguous",
            )
        )
        if automator and (is_challenge or ctrl.get("is_paused")):
            try:
                _coordinate_challenge_takeover(
                    automator=automator,
                    job_id=job.id,
                    worker_id=worker_id,
                    heartbeat=heartbeat,
                    custom_db_path=custom_db_path,
                    max_wait_seconds=None if is_service_daemon else 300,
                    stop_event=stop_event,
                )
            except Exception as coord_err:
                print(f"Notice in challenge takeover coordination: {coord_err}")

        keep_browser_open = (
            is_service_daemon
            and not (stop_event is not None and stop_event.is_set())
            and final_state
            in {
                JobState.AUTH_REQUIRED.value,
                JobState.MFA_REQUIRED.value,
                JobState.CAPTCHA_REQUIRED.value,
                JobState.UNKNOWN_QUESTION.value,
            }
            and _browser_session_is_open(automator)
        )
        return final_state

    finally:
        heartbeat.stop()
        if automator and not keep_browser_open:
            try:
                automator.close()
            except Exception:
                pass
            finally:
                set_runtime_browser_state(
                    active=False,
                    is_closed=True,
                    browser_job_id=job.id,
                    custom_path=custom_db_path,
                )


def _coordinate_challenge_takeover(
    automator: BrowserAutomator,
    job_id: str,
    worker_id: str,
    heartbeat: HeartbeatThread,
    custom_db_path: Path | None = None,
    max_wait_seconds: int | None = 300,
    stop_event: threading.Event | None = None,
) -> None:
    from datetime import datetime

    start_time = time.time()
    last_state_sync = 0.0

    while True:
        now = time.time()
        if heartbeat.lost_lease or (stop_event is not None and stop_event.is_set()):
            break

        ctrl = get_runtime_control(custom_db_path)
        if ctrl.get("is_stopped"):
            break

        page = getattr(automator, "page", None)
        is_closed = True
        current_url = ""
        page_text = ""
        if page is not None:
            try:
                is_closed = page.is_closed()
                if not is_closed:
                    current_url = page.url or ""
                    try:
                        page_text = (page.locator("body").text_content() or "")[:4000]
                    except Exception:
                        pass
            except Exception:
                is_closed = True

        if is_closed:
            set_runtime_browser_state(
                active=False,
                is_closed=True,
                browser_job_id=job_id,
                custom_path=custom_db_path,
            )
            break

        if now - last_state_sync >= 2.0:
            is_waiting = getattr(automator, "waiting_for_code", False)
            set_runtime_browser_state(
                active=True,
                url=current_url,
                page_text=page_text,
                is_closed=False,
                is_waiting_for_code=is_waiting,
                browser_job_id=job_id,
                custom_path=custom_db_path,
            )
            last_state_sync = now

        pending_code = consume_pending_verification_code(custom_db_path)
        if pending_code and hasattr(automator, "supply_verification_code"):
            automator.supply_verification_code(pending_code)

        conn = get_connection(custom_db_path)
        job_done = False
        ctrl_stopped = False
        ctrl_paused = False
        takeover_active = False
        takeover_expires = None
        try:
            ctrl_row = conn.execute(
                "SELECT is_paused, is_stopped, manual_takeover_owner, manual_takeover_expires_at FROM runtime_control WHERE id = 1;"
            ).fetchone()
            if ctrl_row:
                ctrl_stopped = bool(ctrl_row["is_stopped"])
                ctrl_paused = bool(ctrl_row["is_paused"])
                if (
                    ctrl_row["manual_takeover_owner"]
                    and ctrl_row["manual_takeover_expires_at"]
                ):
                    takeover_active = True
                    takeover_expires = ctrl_row["manual_takeover_expires_at"]

            job_row = conn.execute(
                "SELECT state FROM automation_jobs WHERE id = ?;", (job_id,)
            ).fetchone()
            if job_row and job_row["state"] in (
                "applied",
                "cancelled",
                "skipped",
                "ready",
            ):
                job_done = True
        finally:
            conn.close()

        if ctrl_stopped or job_done:
            break

        if takeover_active and takeover_expires:
            try:
                exp_dt = datetime.strptime(takeover_expires, "%Y-%m-%d %H:%M:%S")
                if exp_dt > datetime.now():
                    start_time = now
            except ValueError:
                pass

        if not ctrl_paused:
            break

        if max_wait_seconds is not None and now - start_time > max_wait_seconds:
            break

        time.sleep(1.0)

    _sync_runtime_browser_state_from_automator(
        automator=automator,
        job_id=job_id,
        custom_db_path=custom_db_path,
    )


def run_worker_loop(
    max_jobs: int | None = None,
    max_idle_polls: int | None = None,
    poll_interval: float = 3.0,
    worker_id: str | None = None,
    headless: bool = True,
    browser_engine: str = "chromium",
    custom_db_path: Path | None = None,
    skip_lock: bool = False,
    stop_event: threading.Event | None = None,
) -> int:
    """
    Main worker loop. Runs under singleton OS lock, polls queue, and executes jobs.
    """
    effective_worker_id = (
        worker_id
        or f"worker_{socket.gethostname()}_{os.getpid()}_{uuid.uuid4().hex[:6]}"
    )
    lock = RuntimeSingletonLock() if not skip_lock else None

    if lock is not None:
        try:
            lock.acquire()
        except WorkerLockError as e:
            print(f"❌ Worker startup aborted: {e}")
            return 1

        if not lock.is_locked():
            print(
                "❌ Worker startup aborted: Another worker process holds the OS lock."
            )
            return 1

    print(f"🚀 Job Applier Automation Worker started: {effective_worker_id}")
    print(
        f"   Headless: {headless} | Browser: {browser_engine} | Poll: {poll_interval}s"
    )

    # Reconcile any stranded submit_intent or verifying jobs from prior worker crashes
    recovered = reconcile_stranded_jobs(custom_db_path)
    if recovered:
        print(
            f"⚠️ Recovered {len(recovered)} stranded submit_intent/verifying jobs into ambiguous_submission."
        )

    stop_requested = False

    def is_stop_requested() -> bool:
        return stop_requested or (stop_event is not None and stop_event.is_set())

    def handle_signal(sig: int, frame: Any) -> None:
        nonlocal stop_requested
        print(
            f"\n🛑 Shutdown signal received ({sig}). Finishing current task safely..."
        )
        stop_requested = True
        if stop_event is not None:
            stop_event.set()

    # Python only permits signal handlers in the main thread. The runtime
    # daemon intentionally runs this synchronous Playwright loop in a worker
    # thread to keep it outside any asyncio event loop.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

    jobs_processed = 0
    idle_polls = 0
    service_stop_announced = False

    try:
        while not is_stop_requested() and (
            max_jobs is None or jobs_processed < max_jobs
        ):
            ctrl = get_runtime_control(custom_db_path)
            is_service_mode = (
                os.environ.get("JOB_APPLIER_RUNTIME_MODE", "").strip().lower()
                == "service"
            )
            if ctrl.get("is_stopped"):
                if is_service_mode:
                    if not service_stop_announced:
                        print(
                            "🛑 Global emergency stop engaged. Worker idling in service mode."
                        )
                        service_stop_announced = True
                    try:
                        from job_applier.automation.ntfy import process_outbox

                        process_outbox(custom_path=custom_db_path)
                    except Exception:
                        pass
                    idle_polls += 1
                    if max_idle_polls is not None and idle_polls >= max_idle_polls:
                        break
                    time.sleep(poll_interval)
                    continue
                print("🛑 Global emergency stop engaged. Worker exiting.")
                break

            service_stop_announced = False
            idle_polls = 0

            if ctrl.get("is_paused") or ctrl.get("manual_takeover_owner"):
                try:
                    from job_applier.automation.ntfy import process_outbox

                    process_outbox(custom_path=custom_db_path)
                except Exception:
                    pass
                time.sleep(poll_interval)
                continue

            job = claim_next_job(
                worker_id=effective_worker_id,
                lease_seconds=60,
                custom_path=custom_db_path,
            )

            if not job:
                try:
                    from job_applier.automation.ntfy import process_outbox

                    process_outbox(custom_path=custom_db_path)
                except Exception:
                    pass
                time.sleep(poll_interval)
                continue

            print(
                f"\n📋 Processing Job {job.id} (App ID: {job.app_id}, Priority: {job.priority})..."
            )
            outcome = process_claimed_job(
                job=job,
                worker_id=effective_worker_id,
                headless=headless,
                browser_engine=browser_engine,
                custom_db_path=custom_db_path,
                stop_event=stop_event,
            )
            print(f"   Outcome for Job {job.id}: {outcome}")
            jobs_processed += 1

            if max_jobs is not None and jobs_processed >= max_jobs:
                print(f" Target job limit ({max_jobs}) reached. Worker terminating.")
                break

    finally:
        if lock is not None:
            lock.release()
        print(f"👋 Worker {effective_worker_id} stopped cleanly.")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Job Applier Single-Worker Daemon")
    parser.add_argument(
        "--max-jobs", type=int, default=None, help="Stop after processing N jobs"
    )
    parser.add_argument(
        "--poll-interval", type=float, default=3.0, help="Polling interval in seconds"
    )
    parser.add_argument("--headed", action="store_true", help="Launch visible browser")
    parser.add_argument(
        "--browser",
        choices=["chromium", "firefox", "chrome"],
        default="chromium",
        help="Browser engine",
    )
    parser.add_argument("--worker-id", type=str, default=None, help="Custom worker ID")

    args = parser.parse_args()
    code = run_worker_loop(
        max_jobs=args.max_jobs,
        poll_interval=args.poll_interval,
        worker_id=args.worker_id,
        headless=not args.headed,
        browser_engine=args.browser,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
