from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from job_applier.automation.adapters.base import BaseATSAdapter
from job_applier.automation.adapters.exceptions import (
    AuthenticationRequiredError,
    CaptchaDetectedError,
    FormValidationError,
    JobStaleError,
    UnknownQuestionBlockedError,
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
from job_applier.automation.adapters.semantic import (
    accessible_label,
    input_is_empty,
    is_sensitive_or_excluded_checkbox,
    is_standard_consent_checkbox,
    requires_manual_review,
    select_semantic_option,
)
from job_applier.automation.candidate_profile import CandidateProfile

logger = logging.getLogger("job_applier.adapters.lever")


class LeverAdapter(BaseATSAdapter):
    """
    Typed, versioned adapter for Lever ATS job boards.
    Supports jobs.lever.co and embedded Lever application postings.
    """

    adapter_name: str = "lever"
    adapter_version: str = "1.1.0"
    can_submit: bool = True
    display_name: str = "Lever Job Board"

    def detect(self, url: str, page: Any | None = None) -> bool:
        low_url = url.lower()
        if "jobs.lever.co" in low_url or "lever.co" in low_url:
            return True

        if page is not None:
            try:
                if (
                    page.locator(
                        ".lever-form, [data-qa='application-form'], form#application-form"
                    ).count()
                    > 0
                ):
                    return True
                content = page.content().lower()
                if "jobs.lever.co" in content or "lever-application" in content:
                    return True
            except Exception:
                pass

        return False

    def discover_form(self, page: Any) -> FormDiscovery:
        has_captcha, _ = self.detect_captcha(page)
        has_login, _ = self.check_auth_state(page)
        is_stale, stale_reason = self.detect_stale_job(page)

        questions: list[QuestionField] = []
        steps: list[FormStep] = []

        try:
            standard_fields = [
                (
                    "name",
                    "input[name='name'], input[data-qa='name-input']",
                    QuestionType.TEXT,
                    True,
                ),
                (
                    "email",
                    "input[name='email'], input[data-qa='email-input']",
                    QuestionType.TEXT,
                    True,
                ),
                (
                    "phone",
                    "input[name='phone'], input[data-qa='phone-input']",
                    QuestionType.TEXT,
                    False,
                ),
                (
                    "location",
                    "input[name='location'], input[data-qa='location-input']",
                    QuestionType.TEXT,
                    False,
                ),
                (
                    "org",
                    "input[name='org'], input[data-qa='org-input']",
                    QuestionType.TEXT,
                    False,
                ),
                (
                    "resume",
                    "input[name='resume'], input[data-qa='input-resume'], input[type='file']",
                    QuestionType.FILE,
                    True,
                ),
            ]
            for f_id, sel, q_type, req in standard_fields:
                if page.locator(sel).count() > 0:
                    questions.append(
                        QuestionField(
                            field_id=f_id,
                            label=f_id.title(),
                            question_type=q_type,
                            required=req,
                            selector=sel,
                        )
                    )

            # Discover additional questions
            custom_items = page.locator(
                ".application-question, .custom-question, div[data-qa='custom-question']"
            )
            for i in range(custom_items.count()):
                el = custom_items.nth(i)
                label_text = accessible_label(el) or f"question_{i}"
                is_req = "*" in label_text or "required" in label_text.lower()

                if el.locator("input[type='radio']").count() > 0:
                    q_type = QuestionType.RADIO
                elif el.locator("input[type='checkbox']").count() > 0:
                    q_type = QuestionType.CHECKBOX
                elif el.locator("select").count() > 0:
                    q_type = QuestionType.SELECT
                elif el.locator("textarea").count() > 0:
                    q_type = QuestionType.TEXTAREA
                else:
                    q_type = QuestionType.TEXT

                questions.append(
                    QuestionField(
                        field_id=f"lever_q_{i}",
                        label=label_text,
                        question_type=q_type,
                        required=is_req,
                    )
                )

        except Exception as ex:
            logger.warning(f"Lever discovery partial error: {ex}")

        steps.append(
            FormStep(
                step_index=1, title="Application", fields=questions, is_last_step=True
            )
        )

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

        # 1. Check for blocking challenges
        has_captcha, cap_msg = self.detect_captcha(page)
        if has_captcha:
            raise CaptchaDetectedError(f"Lever CAPTCHA detected: {cap_msg}")

        has_login, log_msg = self.check_auth_state(page)
        if has_login:
            raise AuthenticationRequiredError(f"Lever login required: {log_msg}")

        is_stale, stale_msg = self.detect_stale_job(page)
        if is_stale:
            raise JobStaleError(f"Lever job closed/stale: {stale_msg}")

        # 2. Fill standard personal fields
        # Lever uses full name in a single input or separate fields
        name_input = page.locator("input[name='name'], input[data-qa='name-input']")
        if name_input.count() > 0 and name_input.first.is_visible():
            name_input.first.fill(profile.full_name)
            report.fields_filled.append("Full Name")
            report.answers_provenance["Full Name"] = "profile"

        email_input = page.locator("input[name='email'], input[data-qa='email-input']")
        if email_input.count() > 0 and email_input.first.is_visible():
            email_input.first.fill(profile.email)
            report.fields_filled.append("Email")
            report.answers_provenance["Email"] = "profile"

        phone_input = page.locator("input[name='phone'], input[data-qa='phone-input']")
        if phone_input.count() > 0 and phone_input.first.is_visible() and profile.phone:
            phone_input.first.fill(profile.phone)
            report.fields_filled.append("Phone")
            report.answers_provenance["Phone"] = "profile"

        # Current location/company and URLs use semantic names that vary by Lever form.
        simple_mappings = [
            (
                "input[name='location'], input[data-qa='location-input']",
                profile.city,
                "Location",
            ),
            (
                "input[name='org'], input[data-qa='org-input']",
                profile.current_company,
                "Current Company",
            ),
        ]
        for sel, val, label in simple_mappings:
            if val:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.fill(val)
                    report.fields_filled.append(label)
                    report.answers_provenance[label] = "profile"

        url_mappings = [
            ("input[name^='urls[LinkedIn']", profile.linkedin_url, "LinkedIn"),
            ("input[name^='urls[GitHub']", profile.github_url, "GitHub"),
            ("input[name^='urls[Portfolio']", profile.portfolio_url, "Portfolio"),
        ]
        for sel, val, label in url_mappings:
            if val:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.fill(val)
                    report.fields_filled.append(label)
                    report.answers_provenance[label] = "profile"

        # Additional information / cover letter
        if cover_letter:
            add_info = page.locator(
                "textarea[name='comments'], textarea[name='additionalInformation'], textarea[name='coverLetter']"
            )
            if add_info.count() > 0 and add_info.first.is_visible():
                add_info.first.fill(cover_letter)
                report.cover_letter_filled = True
                report.fields_filled.append("Cover Letter / Comments")
                report.answers_provenance["Cover Letter / Comments"] = (
                    "tailored_artifact"
                )

        # 3. Answer custom questions fail-closed
        custom_questions = page.locator(
            ".application-question, .custom-question, div[data-qa='custom-question']"
        )
        count = custom_questions.count()
        for i in range(count):
            q_el = custom_questions.nth(i)
            try:
                q_text = accessible_label(q_el)
                if not q_text:
                    continue
                if requires_manual_review(q_text):
                    report.unknown_questions.append(q_text)
                    continue

                # Auto-accept standard consent question cards (e.g. data processing checkboxes)
                if is_standard_consent_checkbox(q_text):
                    cb = q_el.locator("input[type='checkbox']")
                    if cb.count() > 0:
                        try:
                            if not cb.first.is_checked():
                                cb.first.check()
                            report.fields_filled.append(q_text)
                            report.answers_provenance[q_text] = "auto_consent"
                            logger.info(
                                f"Lever auto-consented to card question: {q_text}"
                            )
                            continue
                        except Exception as ex:
                            report.errors.append(
                                f"Failed to check consent '{q_text}': {ex}"
                            )

                # Skip if already filled
                txt = q_el.locator("input[type='text'], input:not([type]), textarea")
                if txt.count() > 0 and not input_is_empty(txt.first):
                    continue

                options: list[str] = []
                select_el = q_el.locator("select")
                if select_el.count() > 0:
                    opts = select_el.locator("option")
                    for o in range(opts.count()):
                        t = opts.nth(o).inner_text().strip()
                        if t and not t.startswith("Select") and not t.startswith("--"):
                            options.append(t)

                radio_el = q_el.locator("input[type='radio']")
                if radio_el.count() > 0:
                    for r in range(radio_el.count()):
                        r_lbl = q_el.locator(
                            f"label[for='{radio_el.nth(r).get_attribute('id')}']"
                        )
                        if r_lbl.count() > 0:
                            options.append(r_lbl.inner_text().strip())

                if question_solver:
                    try:
                        ans_result = question_solver.solve_question_safely(
                            q_text, options=options
                        )
                        ans_value = ans_result.answer

                        if (
                            select_el.count() > 0
                            or q_el.get_attribute("role") == "combobox"
                        ):
                            if not select_semantic_option(q_el, str(ans_value)):
                                raise FormValidationError(
                                    [f"Could not select answer for '{q_text}'"]
                                )
                        elif radio_el.count() > 0:
                            radio_input = q_el.locator(
                                f"input[type='radio'][value='{ans_value}']"
                            )
                            if radio_input.count() > 0:
                                radio_input.first.check()
                            else:
                                target = q_el.locator(f"label:has-text('{ans_value}')")
                                if target.count() > 0:
                                    target.first.click()
                        elif txt.count() > 0:
                            txt.first.fill(str(ans_value))

                        report.fields_filled.append(q_text)
                        report.answers_provenance[q_text] = ans_result.provenance

                    except Exception as q_err:
                        err_name = type(q_err).__name__
                        if (
                            "UnknownQuestion" in err_name
                            or "SensitiveQuestion" in err_name
                        ):
                            report.unknown_questions.append(q_text)
                            raise UnknownQuestionBlockedError(
                                q_text, options=options
                            ) from q_err
                        else:
                            report.errors.append(f"Error solving '{q_text}': {q_err}")
                            is_req = False
                            try:
                                is_req = bool(
                                    q_el.locator(
                                        "[aria-required='true'], [required], .required, span:has-text('*')"
                                    ).count()
                                    > 0
                                )
                            except Exception:
                                pass
                            if is_req:
                                raise FormValidationError(
                                    [
                                        f"Failed to answer required question '{q_text}': {q_err}"
                                    ]
                                ) from q_err

            except (UnknownQuestionBlockedError, FormValidationError):
                raise
            except Exception as e:
                logger.debug(f"Error processing Lever custom question {i}: {e}")

        # Auto-accept standard consent checkboxes (privacy policy, GDPR, terms)
        consent_loc = page.locator(
            "input[data-qa='privacy-policy-checkbox'], input[type='checkbox'][name*='consent'], input[type='checkbox'][id*='consent'], input[type='checkbox'][name*='privacy']"
        )
        for c_idx in range(consent_loc.count()):
            cb = consent_loc.nth(c_idx)
            label = accessible_label(cb) or "Privacy Policy"
            is_data_qa = cb.get_attribute("data-qa") == "privacy-policy-checkbox"
            if is_data_qa or is_standard_consent_checkbox(label):
                try:
                    if not cb.is_checked():
                        cb.check()
                    name = label or "Privacy Policy"
                    if name not in report.fields_filled:
                        report.fields_filled.append(name)
                        report.answers_provenance[name] = "auto_consent"
                    logger.info(f"Lever auto-consented: {name}")
                except Exception as ex:
                    report.errors.append(f"Failed to check consent '{label}': {ex}")
            else:
                report.unknown_questions.append(label)

        # Check all other checkboxes on the page
        all_checkboxes = page.locator("input[type='checkbox']")
        for c_idx in range(all_checkboxes.count()):
            cb = all_checkboxes.nth(c_idx)
            label = accessible_label(cb)
            if is_standard_consent_checkbox(label):
                try:
                    if not cb.is_checked():
                        cb.check()
                    name = label or "Privacy Policy"
                    if name not in report.fields_filled:
                        report.fields_filled.append(name)
                        report.answers_provenance[name] = "auto_consent"
                    logger.info(f"Lever auto-consented: {name}")
                except Exception as ex:
                    logger.debug(f"Failed checking Lever consent checkbox: {ex}")
            elif is_sensitive_or_excluded_checkbox(label):
                if label not in report.unknown_questions:
                    report.unknown_questions.append(label or f"Checkbox {c_idx}")

        return report

    def upload_documents(
        self,
        page: Any,
        resume_path: Path,
        cover_letter_path: Path | None = None,
    ) -> bool:
        if not resume_path.exists() or resume_path.stat().st_size == 0:
            raise UploadRejectedError(f"Resume file missing or empty: {resume_path}")

        try:
            file_input = page.locator(
                "input[name='resume'], input[data-qa='input-resume'], input[type='file'][data-qa='resume-upload']"
            )
            if file_input.count() == 0:
                raise UploadRejectedError("No file upload input found for Lever resume")

            file_input.first.set_input_files(str(resume_path.resolve()))

            if cover_letter_path is not None and cover_letter_path.exists():
                cover_input = page.locator(
                    "input[name='cover_letter'], input[name*='coverLetter'], input[data-qa='input-cover-letter']"
                )
                if cover_input.count() > 0:
                    cover_input.first.set_input_files(str(cover_letter_path.resolve()))

            # Verify that upload wasn't rejected
            err = page.locator(".error-message:has-text('resume'), .template--error")
            if err.count() > 0 and err.first.is_visible():
                raise UploadRejectedError(
                    f"Lever rejected resume upload: {err.first.inner_text()}"
                )

            return True

        except UploadRejectedError:
            raise
        except Exception as ex:
            raise UploadRejectedError(f"Failed to upload resume to Lever: {ex}") from ex

    def validate_form(self, page: Any) -> list[ValidationError]:
        errors: list[ValidationError] = []
        try:
            err_loc = page.locator(".error-message, .template--error, .is-error")
            for i in range(err_loc.count()):
                el = err_loc.nth(i)
                if el.is_visible():
                    msg = el.inner_text().strip() or "Validation error"
                    errors.append(
                        ValidationError(field_id=f"lever_err_{i}", message=msg)
                    )
        except Exception as ex:
            logger.debug(f"Lever validation inspection error: {ex}")

        return errors

    def submit(
        self,
        page: Any,
        on_submit_intent: Callable[[], None] | None = None,
    ) -> bool:
        submit_btn = page.locator(
            "button[data-qa='btn-submit'], #btn-submit, button:has-text('Submit Application')"
        )
        if submit_btn.count() == 0 or not submit_btn.first.is_visible():
            raise FormValidationError(["Lever submit button not found or not visible"])

        if on_submit_intent:
            on_submit_intent()

        submit_btn.first.click()
        return True

    def confirm_submission(self, page: Any) -> ConfirmationEvidence:
        try:
            url = page.url.lower()

            # 1. Lever redirects to /applied or shows confirmation container
            conf_container = page.locator(
                ".application-confirmation, .confirmation-page, .confirmation-message"
            )
            if conf_container.count() > 0 and conf_container.first.is_visible():
                text = conf_container.first.inner_text().strip()
                ref_match = re.search(
                    r"(?:confirmation(?:\s*(?:id|ref))?|reference(?:\s*id)?|receipt|application\s*reference|id)[:\s#]+([A-Za-z0-9-]+)",
                    text,
                    re.IGNORECASE,
                )
                ref_id = ref_match.group(1) if ref_match else None

                return ConfirmationEvidence(
                    platform=self.adapter_name,
                    adapter_version=self.adapter_version,
                    confirmed=True,
                    confirmation_id=ref_id,
                    confirmation_text=text[:300],
                    proof_element=".confirmation-message",
                    is_ambiguous=False,
                    details={"matched_container": ".confirmation-message"},
                )

            # 2. Check Lever confirmation URL / applied landing
            if "lever.co" in url and (
                url.endswith("/applied") or "/applied/" in url or "confirmation" in url
            ):
                heading = page.locator(
                    "h3:has-text('Thank you for applying'), h4:has-text('Thank you')"
                )
                text = (
                    heading.first.inner_text().strip()
                    if heading.count() > 0
                    else "Lever application confirmed via URL"
                )
                return ConfirmationEvidence(
                    platform=self.adapter_name,
                    adapter_version=self.adapter_version,
                    confirmed=True,
                    confirmation_id=None,
                    confirmation_text=text,
                    proof_element="url:/applied",
                    is_ambiguous=False,
                    details={"url": page.url},
                )

            # 3. Check for validation errors
            val_errors = self.validate_form(page)
            if val_errors:
                return ConfirmationEvidence(
                    platform=self.adapter_name,
                    adapter_version=self.adapter_version,
                    confirmed=False,
                    confirmation_text=f"Validation errors after submit: {[e.message for e in val_errors]}",
                    is_ambiguous=False,
                    details={"errors": [e.message for e in val_errors]},
                )

            # 4. Fail closed as ambiguous
            return ConfirmationEvidence(
                platform=self.adapter_name,
                adapter_version=self.adapter_version,
                confirmed=False,
                is_ambiguous=True,
                confirmation_text="Submission outcome could not be verified on Lever page",
                details={"current_url": page.url},
            )

        except Exception as ex:
            return ConfirmationEvidence(
                platform=self.adapter_name,
                adapter_version=self.adapter_version,
                confirmed=False,
                is_ambiguous=True,
                confirmation_text=f"Error checking Lever confirmation: {ex}",
            )
