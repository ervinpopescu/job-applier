from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from job_applier.automation.adapters.base import BaseATSAdapter
from job_applier.automation.adapters.exceptions import (
    AuthenticationRequiredError,
    CaptchaDetectedError,
    GenericAdapterCannotSubmitError,
    JobStaleError,
    UploadRejectedError,
)
from job_applier.automation.adapters.models import (
    ConfirmationEvidence,
    FillReport,
    FormDiscovery,
    FormStep,
    QuestionField,
    QuestionType,
    ValidationError,
)
from job_applier.automation.candidate_profile import CandidateProfile

logger = logging.getLogger("job_applier.adapters.generic")


class GenericFormAdapter(BaseATSAdapter):
    """
    Generic fallback adapter for unclassified job application forms.
    SAFETY CONTRACT:
    - Generic forms are STRICTLY FILL-ONLY.
    - Automated submission is permanently prohibited (can_submit = False).
    - Calling submit() or confirm_submission() strictly raises GenericAdapterCannotSubmitError.
    """

    adapter_name: str = "generic"
    adapter_version: str = "1.0.0"
    can_submit: bool = False
    display_name: str = "Generic Form (Fill-Only)"

    def detect(self, url: str, page: Any | None = None) -> bool:
        # Fallback adapter that can match any URL if no specialized adapter claims it
        return True

    def discover_form(self, page: Any) -> FormDiscovery:
        has_captcha, _ = self.detect_captcha(page)
        has_login, _ = self.check_auth_state(page)
        is_stale, stale_reason = self.detect_stale_job(page)

        questions: list[QuestionField] = []
        try:
            inputs = page.locator("input:not([type='hidden']), textarea, select")
            for i in range(inputs.count()):
                inp = inputs.nth(i)
                f_id = (
                    inp.get_attribute("id") or inp.get_attribute("name") or f"input_{i}"
                )
                questions.append(
                    QuestionField(
                        field_id=f_id,
                        label=f_id,
                        question_type=QuestionType.TEXT,
                        required=inp.get_attribute("required") is not None,
                    )
                )
        except Exception:
            pass

        steps = [
            FormStep(
                step_index=1, title="Generic Form", fields=questions, is_last_step=True
            )
        ]

        return FormDiscovery(
            platform=self.adapter_name,
            adapter_version=self.adapter_version,
            steps=steps,
            questions=questions,
            has_captcha=has_captcha,
            has_login_wall=has_login,
            is_stale=is_stale,
            stale_reason=stale_reason,
        )

    def fill_fields(
        self,
        page: Any,
        profile: CandidateProfile,
        question_solver: Any | None = None,
        cover_letter: str = "",
    ) -> FillReport:
        report = FillReport(
            platform=self.adapter_name, adapter_version=self.adapter_version
        )

        # 1. Challenge checks
        has_captcha, cap_msg = self.detect_captcha(page)
        if has_captcha:
            raise CaptchaDetectedError(f"Generic form CAPTCHA detected: {cap_msg}")

        has_login, log_msg = self.check_auth_state(page)
        if has_login:
            raise AuthenticationRequiredError(f"Generic form login required: {log_msg}")

        is_stale, stale_msg = self.detect_stale_job(page)
        if is_stale:
            raise JobStaleError(f"Generic form job closed/stale: {stale_msg}")

        # 2. Heuristic field filling
        mappings = [
            (
                "input[name*='first_name'], input[id*='first_name']",
                profile.first_name,
                "First Name",
            ),
            (
                "input[name*='last_name'], input[id*='last_name']",
                profile.last_name,
                "Last Name",
            ),
            ("input[type='email'], input[name*='email']", profile.email, "Email"),
            ("input[type='tel'], input[name*='phone']", profile.phone, "Phone"),
            ("input[name*='city'], input[id*='city']", profile.city, "City"),
        ]
        for sel, val, label in mappings:
            if not val:
                continue
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                try:
                    loc.first.fill(val)
                    report.fields_filled.append(label)
                    report.answers_provenance[label] = "profile"
                except Exception as ex:
                    report.errors.append(f"Failed to fill {label}: {ex}")

        # Cover letter
        if cover_letter:
            cl = page.locator("textarea[name*='cover'], textarea[id*='cover']")
            if cl.count() > 0 and cl.first.is_visible():
                cl.first.fill(cover_letter)
                report.cover_letter_filled = True
                report.fields_filled.append("Cover Letter")

        return report

    def upload_documents(
        self,
        page: Any,
        resume_path: Path,
        cover_letter_path: Path | None = None,
    ) -> bool:
        if not resume_path.exists() or resume_path.stat().st_size == 0:
            raise UploadRejectedError(f"Resume file missing or empty: {resume_path}")

        file_inputs = page.locator("input[type='file']")
        if file_inputs.count() > 0:
            try:
                file_inputs.first.set_input_files(str(resume_path.resolve()))
                return True
            except Exception as ex:
                raise UploadRejectedError(f"Generic resume upload failed: {ex}") from ex

        return False

    def validate_form(self, page: Any) -> list[ValidationError]:
        errors: list[ValidationError] = []
        try:
            invalids = page.locator(":invalid")
            for i in range(invalids.count()):
                el = invalids.nth(i)
                f_id = (
                    el.get_attribute("id") or el.get_attribute("name") or f"invalid_{i}"
                )
                errors.append(
                    ValidationError(
                        field_id=f_id, message="Field validation failed (:invalid)"
                    )
                )
        except Exception:
            pass
        return errors

    def submit(
        self,
        page: Any,
        on_submit_intent: Callable[[], None] | None = None,
    ) -> bool:
        """
        STRICTLY PROHIBITED: Generic forms are fill-only.
        Central safety rule dictates that only explicitly approved ATS adapters may submit.
        """
        raise GenericAdapterCannotSubmitError(
            "Generic form adapter is fill-only and strictly prohibited from submitting applications."
        )

    def confirm_submission(self, page: Any) -> ConfirmationEvidence:
        """
        STRICTLY PROHIBITED: Generic forms cannot confirm submissions.
        """
        raise GenericAdapterCannotSubmitError(
            "Generic form adapter cannot confirm submissions; submission is prohibited."
        )
