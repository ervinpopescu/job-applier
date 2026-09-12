from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from job_applier.automation.adapters.models import ConfirmationEvidence
from job_applier.automation.candidate_profile import (
    CandidateProfile,
    load_candidate_profile,
)
from job_applier.automation.queue import (
    JobState,
    complete_attempt,
    get_connection,
    get_runtime_control,
    handle_job_failure,
    record_event,
    record_submit_intent,
    set_runtime_pause,
    transition_job,
)
from job_applier.db import (
    get_adapter_control,
    get_latest_submission_timestamp,
    get_submissions_in_window,
    record_submission_timestamp,
)

logger = logging.getLogger("job_applier.safety_guard")


class SafetyGuardError(Exception):
    """Base error for safety and ownership violations."""


class FencingOwnershipLostError(SafetyGuardError):
    """Raised when worker lease expired, generation is stale, or lease was lost."""


class AutomationPausedOrStoppedError(SafetyGuardError):
    """Raised when automation has been paused or stopped by the operator."""


class TakeoverActiveError(SafetyGuardError):
    """Raised when an operator has claimed manual takeover of the browser session."""


class GenericAdapterSubmissionBlockedError(SafetyGuardError):
    """Raised when submission is attempted through the generic adapter."""


class CanaryRequirementUnmetError(SafetyGuardError):
    """Raised when real submission is attempted before 3 approved canaries are confirmed."""


class AdapterDisabledError(SafetyGuardError):
    """Raised when real submission is attempted for an adapter that is not enabled."""


class DailySubmissionLimitExceededError(SafetyGuardError):
    """Raised when daily submission cap (default 5) is exceeded."""


class SubmissionPacingViolationError(SafetyGuardError):
    """Raised when minimum interval between submissions (default 5 minutes) is violated."""


class FrozenRevisionMismatchError(SafetyGuardError):
    """Raised when candidate profile or document artifacts changed after attempt creation."""


class MissingArtifactError(SafetyGuardError):
    """Raised when required CV PDF or documents are missing on disk."""


class AggregatorBlockedError(SafetyGuardError):
    """Raised when job_url points to an indirect aggregator landing page (e.g. Jobicy)."""


class SubmissionSafetyGuard:
    """
    Centralized safety, rate-limiting, and ownership guard for all submission paths.
    Enforces:
    - Singleton worker lease & generation fencing
    - Takeover and pause checks
    - Prohibition of generic form submission
    - Canary verification (>= 3 confirmed canaries required before enablement)
    - Rate limits (max 5/day, min 5-minute spacing)
    - Frozen revisions validation (profile snapshot & CV PDF integrity)
    - Atomic submit_intent transition
    - Adapter-specific confirmation verification & ambiguous submission fail-closed handling
    """

    DEFAULT_MAX_DAILY = 5
    DEFAULT_MIN_INTERVAL_SECONDS = 300  # 5 minutes
    MIN_CANARY_COUNT = 3

    @classmethod
    def validate_pre_submit_safety(
        cls,
        job_id: str,
        worker_id: str,
        generation: int,
        adapter_name: str,
        profile_snapshot: dict[str, Any],
        artifact_revisions: dict[str, Any],
        current_profile: CandidateProfile | None = None,
        is_mock: bool = False,
        custom_db_path: Path | None = None,
    ) -> None:
        """
        Validates all safety prerequisites BEFORE entering submit_intent or clicking submit.
        Raises typed SafetyGuardError if any condition is unsatisfied.
        """
        norm_adapter = adapter_name.lower()

        # 1. Check runtime control (pause, stop, takeover)
        ctrl = get_runtime_control(custom_db_path)
        if ctrl.get("is_paused"):
            raise AutomationPausedOrStoppedError("Automation is currently paused.")
        if ctrl.get("is_stopped"):
            raise AutomationPausedOrStoppedError("Global emergency stop engaged.")
        if ctrl.get("manual_takeover_owner"):
            raise TakeoverActiveError(
                f"Operator takeover active by {ctrl.get('manual_takeover_owner')}."
            )

        # 1b. Check disk pressure headroom
        if not is_mock:
            from job_applier.ops.disk_guard import (
                DiskPressureError,
                check_disk_pressure,
            )

            dp_status = check_disk_pressure()
            if dp_status.is_under_pressure:
                raise DiskPressureError(
                    f"Pre-submit safety guard rejected: {dp_status.reason}"
                )

        # 2. Check worker ownership & fencing generation
        conn = get_connection(custom_db_path)
        try:
            job_row = conn.execute(
                "SELECT lease_owner, fencing_generation, lease_expires_at, is_cancelled FROM automation_jobs WHERE id = ?;",
                (job_id,),
            ).fetchone()
            if not job_row:
                raise FencingOwnershipLostError(f"Job {job_id} not found in database.")

            if job_row["is_cancelled"]:
                raise SafetyGuardError(f"Job {job_id} has been cancelled.")

            if job_row["lease_owner"] != worker_id:
                raise FencingOwnershipLostError(
                    f"Worker lease lost for job {job_id}. Current owner: {job_row['lease_owner']}, caller: {worker_id}."
                )

            if job_row["fencing_generation"] != generation:
                raise FencingOwnershipLostError(
                    f"Fencing generation mismatch for job {job_id}. Expected {generation}, found {job_row['fencing_generation']}."
                )

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if job_row["lease_expires_at"] and job_row["lease_expires_at"] < now_str:
                raise FencingOwnershipLostError(
                    f"Worker lease expired at {job_row['lease_expires_at']} (current: {now_str})."
                )
        finally:
            conn.close()

        # 3. Adapter eligibility: Generic adapter is strictly fill-only
        if norm_adapter == "generic":
            raise GenericAdapterSubmissionBlockedError(
                "Generic form adapter is fill-only and strictly prohibited from submitting."
            )

        if norm_adapter not in {"greenhouse", "lever", "ashby"}:
            raise SafetyGuardError(f"Unsupported adapter '{norm_adapter}'.")

        # 3b. Aggregator check: reject indirect aggregator landing pages like Jobicy
        if job_id:
            conn = get_connection(custom_db_path)
            try:
                url_row = conn.execute(
                    "SELECT a.job_url FROM automation_jobs j JOIN applications a ON j.app_id = a.id WHERE j.id = ?;",
                    (job_id,),
                ).fetchone()
                if url_row and url_row["job_url"]:
                    url_lower = str(url_row["job_url"]).lower()
                    if "jobicy.com" in url_lower or "jobicy." in url_lower:
                        raise AggregatorBlockedError(
                            f"Application URL '{url_row['job_url']}' is an indirect Jobicy aggregator landing page. "
                            "Direct company career portal application is required."
                        )
            finally:
                conn.close()

        # 4. Canary requirement and enablement gate (skipped for test/fixture mode)
        if not is_mock:
            adapter_ctrl = get_adapter_control(norm_adapter, custom_path=custom_db_path)
            if not adapter_ctrl:
                raise SafetyGuardError(
                    f"No adapter controls found for '{norm_adapter}'."
                )

            canary_count = adapter_ctrl.get("confirmed_canary_count", 0)
            if canary_count < cls.MIN_CANARY_COUNT:
                raise CanaryRequirementUnmetError(
                    f"Adapter '{norm_adapter}' real submission is disabled: requires at least "
                    f"{cls.MIN_CANARY_COUNT} approved confirmed canaries (currently {canary_count})."
                )

            if not adapter_ctrl.get("is_enabled"):
                raise AdapterDisabledError(
                    f"Adapter '{norm_adapter}' is disabled for autonomous submission."
                )

            # 5. Rate limiting: 5 submissions/day, minimum 5-minute interval
            max_daily = adapter_ctrl.get("max_daily_submissions", cls.DEFAULT_MAX_DAILY)
            min_interval = adapter_ctrl.get(
                "min_interval_seconds", cls.DEFAULT_MIN_INTERVAL_SECONDS
            )

            # Global pacing check across all adapters (minimum 5-minute interval)
            last_sub = get_latest_submission_timestamp(custom_path=custom_db_path)
            if last_sub:
                try:
                    last_dt = (
                        datetime.fromisoformat(last_sub)
                        if "T" in last_sub
                        else datetime.strptime(last_sub, "%Y-%m-%d %H:%M:%S")
                    )
                    elapsed = (datetime.now() - last_dt).total_seconds()
                    if elapsed < min_interval:
                        rem = int(min_interval - elapsed)
                        raise SubmissionPacingViolationError(
                            f"Minimum {min_interval // 60}-minute interval between submissions not met. "
                            f"Please wait {rem}s before next attempt."
                        )
                except (ValueError, TypeError):
                    pass

            # Daily cap check across 24 hours
            daily_count = get_submissions_in_window(24, custom_path=custom_db_path)
            if daily_count >= max_daily:
                raise DailySubmissionLimitExceededError(
                    f"Daily submission limit ({max_daily}/day) reached. (Completed today: {daily_count})."
                )

        # 6. Frozen candidate profile revision check
        try:
            profile_to_check = current_profile or load_candidate_profile()
            if profile_snapshot:
                # Compare critical immutable fields
                if profile_snapshot.get(
                    "email"
                ) and profile_to_check.email != profile_snapshot.get("email"):
                    raise FrozenRevisionMismatchError(
                        f"Candidate profile email changed mid-flight: '{profile_snapshot.get('email')}' -> '{profile_to_check.email}'."
                    )
                if profile_snapshot.get(
                    "full_name"
                ) and profile_to_check.full_name != profile_snapshot.get("full_name"):
                    raise FrozenRevisionMismatchError(
                        f"Candidate profile name changed mid-flight: '{profile_snapshot.get('full_name')}' -> '{profile_to_check.full_name}'."
                    )
        except FrozenRevisionMismatchError:
            raise
        except Exception as ex:
            logger.warning(f"Profile revision inspection warning: {ex}")

        # 7. Frozen document / artifact check
        if artifact_revisions:
            folder_str = artifact_revisions.get("folder", "")
            cv_name = artifact_revisions.get("cv_filename", "")
            if folder_str and cv_name:
                cv_file = Path(folder_str) / cv_name
                if not cv_file.exists():
                    raise MissingArtifactError(
                        f"Required CV artifact missing on disk: {cv_file}."
                    )
                if cv_file.stat().st_size == 0:
                    raise MissingArtifactError(f"CV artifact is 0 bytes: {cv_file}.")

                expected_mtime = artifact_revisions.get("cv_mtime")
                if expected_mtime is not None:
                    actual_mtime = cv_file.stat().st_mtime
                    if abs(actual_mtime - expected_mtime) > 2.0:
                        raise FrozenRevisionMismatchError(
                            f"CV artifact was modified mid-flight on disk (mtime changed from {expected_mtime} to {actual_mtime})."
                        )

    @classmethod
    def validate_pacing_and_daily_limits(
        cls,
        adapter_name: str,
        custom_db_path: Path | None = None,
    ) -> None:
        """
        Lightweight early check for daily limits and pacing intervals before launching browser.
        """
        norm_adapter = adapter_name.lower()
        if norm_adapter not in {"greenhouse", "lever", "ashby"}:
            return

        adapter_ctrl = get_adapter_control(norm_adapter, custom_path=custom_db_path)
        if not adapter_ctrl:
            return

        min_interval = adapter_ctrl.get(
            "min_interval_seconds", cls.DEFAULT_MIN_INTERVAL_SECONDS
        )
        last_sub = get_latest_submission_timestamp(custom_path=custom_db_path)
        if last_sub:
            try:
                last_dt = (
                    datetime.fromisoformat(last_sub)
                    if "T" in last_sub
                    else datetime.strptime(last_sub, "%Y-%m-%d %H:%M:%S")
                )
                elapsed = (datetime.now() - last_dt).total_seconds()
                if elapsed < min_interval:
                    rem = int(min_interval - elapsed)
                    raise SubmissionPacingViolationError(
                        f"Minimum {min_interval // 60}-minute interval between submissions not met. "
                        f"Please wait {rem}s before next attempt."
                    )
            except (ValueError, TypeError):
                pass

        max_daily = adapter_ctrl.get("max_daily_submissions", cls.DEFAULT_MAX_DAILY)
        daily_count = get_submissions_in_window(24, custom_path=custom_db_path)
        if daily_count >= max_daily:
            raise DailySubmissionLimitExceededError(
                f"Daily submission limit ({max_daily}/day) reached. (Completed today: {daily_count})."
            )

    @classmethod
    def execute_submit_intent(
        cls,
        job_id: str,
        attempt_id: str,
        worker_id: str,
        generation: int,
        custom_db_path: Path | None = None,
    ) -> None:
        """
        Commits submit_intent to SQLite before physical submission action.
        Aborts if lease/fencing generation was rejected.
        """
        ok = record_submit_intent(
            attempt_id=attempt_id,
            job_id=job_id,
            worker_id=worker_id,
            generation=generation,
            custom_path=custom_db_path,
        )
        if not ok:
            raise RuntimeError(
                f"Aborting submit: failed to record submit intent for job {job_id} (lost lease or fencing generation mismatch)."
            )

    @classmethod
    def reconcile_submission_outcome(
        cls,
        job_id: str,
        app_id: str,
        attempt_id: str,
        worker_id: str,
        generation: int,
        adapter_name: str,
        company: str,
        evidence: ConfirmationEvidence,
        custom_db_path: Path | None = None,
    ) -> tuple[str, str]:
        """
        Reconciles submission evidence returned by the adapter.
        If confirmed: marks job APPLIED, records evidence, updates pacing timestamp.
        If ambiguous: marks job AMBIGUOUS_SUBMISSION, pauses whole worker, alerts operator. NEVER retries.
        """
        evidence_dict = evidence.to_dict()

        if evidence.confirmed and not evidence.is_ambiguous:
            transition_job(
                job_id=job_id,
                worker_id=worker_id,
                generation=generation,
                to_state=JobState.APPLIED.value,
                checkpoint="completed",
                custom_path=custom_db_path,
            )
            complete_attempt(
                attempt_id=attempt_id,
                outcome="applied",
                confirmation_evidence=json.dumps(evidence_dict),
                custom_path=custom_db_path,
            )
            # Record submission timestamp for pacing
            record_submission_timestamp(adapter_name, custom_path=custom_db_path)

            # Update application record in applications table
            conn = get_connection(custom_db_path)
            try:
                with conn:
                    conn.execute(
                        "UPDATE applications SET status = 'applied', updated_at = datetime('now') WHERE id = ?;",
                        (app_id,),
                    )
            finally:
                conn.close()

            record_event(
                job_id=job_id,
                app_id=app_id,
                event_type="application_applied",
                level="SUCCESS",
                step="applied",
                message=f"Application for {company} verified ({adapter_name} adapter v{evidence.adapter_version}).",
                details=evidence_dict,
                custom_path=custom_db_path,
            )
            return "applied", f"Successfully confirmed: {evidence.confirmation_text}"

        elif evidence.is_ambiguous:
            # Ambiguous submission: pause worker, alert operator, never retry automatically
            set_runtime_pause(True, custom_path=custom_db_path)

            final_state = handle_job_failure(
                job_id=job_id,
                worker_id=worker_id,
                generation=generation,
                attempt_id=attempt_id,
                error=f"Ambiguous submission outcome: {evidence.confirmation_text}",
                is_pre_submit=False,
                error_category="ambiguous_submission",
                custom_path=custom_db_path,
            )

            return (
                final_state,
                f"Submission ambiguous. Worker paused for safety: {evidence.confirmation_text}",
            )

        else:
            # Explicit failure reported by adapter (definitive validation/submission failure, not ambiguous)
            final_state = handle_job_failure(
                job_id=job_id,
                worker_id=worker_id,
                generation=generation,
                attempt_id=attempt_id,
                error=f"Submission unconfirmed: {evidence.confirmation_text}",
                is_pre_submit=False,
                error_category="submission_rejected",
                custom_path=custom_db_path,
            )
            return final_state, evidence.confirmation_text

    @classmethod
    def handle_exception_pause_and_alert(
        cls,
        job_id: str,
        app_id: str,
        attempt_id: str | None,
        worker_id: str,
        generation: int,
        exc: Exception,
        company: str = "",
        custom_db_path: Path | None = None,
    ) -> str:
        """
        Pauses the whole worker, emits a durable notification, and records job failure for
        CAPTCHA, MFA, Auth, Unknown Question, or Ambiguous outcomes.
        """
        err_name = type(exc).__name__
        category = "site_changed"
        severity = "warn"

        if "Captcha" in err_name:
            category = "captcha_required"
            severity = "critical"
        elif "MFA" in err_name or "Verification" in err_name:
            category = "mfa_required"
            severity = "critical"
        elif "Authentication" in err_name or "Login" in err_name:
            category = "auth_required"
            severity = "critical"
        elif "UnknownQuestion" in err_name or "SensitiveQuestion" in err_name:
            category = "unknown_question"
            severity = "critical"
        elif "Ambiguous" in err_name:
            category = "ambiguous_submission"
            severity = "critical"
        elif "Takeover" in err_name or "Paused" in err_name:
            category = "runtime_paused"
            severity = "info"

        # Pause whole worker on critical exceptions
        if severity == "critical":
            set_runtime_pause(True, custom_path=custom_db_path)

        final_state = handle_job_failure(
            job_id=job_id,
            worker_id=worker_id,
            generation=generation,
            attempt_id=attempt_id,
            error=str(exc),
            is_pre_submit=True,
            error_category=category,
            custom_path=custom_db_path,
        )

        return final_state
