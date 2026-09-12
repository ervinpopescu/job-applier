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
    requires_manual_review,
    select_semantic_option,
)
from job_applier.automation.candidate_profile import CandidateProfile

logger = logging.getLogger("job_applier.adapters.greenhouse")


class GreenhouseAdapter(BaseATSAdapter):
    """
    Typed, versioned adapter for Greenhouse ATS job boards.
    Supports boards.greenhouse.io, grnh.se, and embedded Greenhouse application forms.
    """

    adapter_name: str = "greenhouse"
    adapter_version: str = "1.1.0"
    can_submit: bool = True
    display_name: str = "Greenhouse Job Board"

    def detect(self, url: str, page: Any | None = None) -> bool:
        low_url = url.lower()
        if "greenhouse.io" in low_url or "grnh.se" in low_url:
            return True

        if page is not None:
            try:
                # Check for signature Greenhouse DOM landmarks
                if (
                    page.locator(
                        "#application_form, #main_fields, #greenhouse-app"
                    ).count()
                    > 0
                ):
                    return True
                content = page.content().lower()
                if "greenhouse.io" in content and (
                    "submit_app" in content or "application_form" in content
                ):
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
            # Greenhouse standard fields
            fields_map = [
                (
                    "first_name",
                    "#first_name, input[name*='first_name'], input[autocomplete='given-name']",
                    QuestionType.TEXT,
                    True,
                ),
                (
                    "last_name",
                    "#last_name, input[name*='last_name'], input[autocomplete='family-name']",
                    QuestionType.TEXT,
                    True,
                ),
                (
                    "email",
                    "#email, input[name*='email'], input[autocomplete='email']",
                    QuestionType.TEXT,
                    True,
                ),
                ("phone", "#phone, input[name*='phone']", QuestionType.TEXT, False),
                (
                    "resume",
                    "#resume, input[type='file'][name*='resume'], input[type='file'][id*='resume']",
                    QuestionType.FILE,
                    True,
                ),
                (
                    "cover_letter",
                    "#cover_letter, textarea[name*='cover_letter']",
                    QuestionType.TEXTAREA,
                    False,
                ),
            ]
            for f_id, sel, q_type, req in fields_map:
                if page.locator(sel).count() > 0:
                    questions.append(
                        QuestionField(
                            field_id=f_id,
                            label=f_id.replace("_", " ").title(),
                            question_type=q_type,
                            required=req,
                            selector=sel,
                        )
                    )

            # Discover custom fields across legacy and React/ARIA Greenhouse forms.
            custom_elements = page.locator(
                ".field, .application-question, [id^='job_application_answers_attributes_'], "
                "input[id^='question_'], textarea[id^='question_'], [role='combobox'][id^='question_']"
            )
            count = custom_elements.count()
            seen_ids: set[str] = set()
            for i in range(count):
                el = custom_elements.nth(i)
                field_id = el.get_attribute("id") or f"custom_{i}"
                if field_id in seen_ids:
                    continue
                seen_ids.add(field_id)
                label_text = accessible_label(el) or f"custom_field_{i}"
                is_req = (
                    el.get_attribute("required") is not None
                    or el.get_attribute("aria-required") == "true"
                    or "*" in label_text
                    or "required" in label_text.lower()
                )

                if (
                    el.get_attribute("type") == "file"
                    or el.locator("input[type='file']").count() > 0
                ):
                    q_type = QuestionType.FILE
                elif (
                    el.get_attribute("role") == "combobox"
                    or el.locator("select, [role='combobox']").count() > 0
                ):
                    q_type = QuestionType.SELECT
                elif (
                    el.get_attribute("type") == "radio"
                    or el.locator("input[type='radio']").count() > 0
                ):
                    q_type = QuestionType.RADIO
                elif (
                    el.get_attribute("type") == "checkbox"
                    or el.locator("input[type='checkbox']").count() > 0
                ):
                    q_type = QuestionType.CHECKBOX
                elif el.evaluate("(e) => e.tagName.toLowerCase()") == "textarea":
                    q_type = QuestionType.TEXTAREA
                else:
                    q_type = QuestionType.TEXT

                questions.append(
                    QuestionField(
                        field_id=field_id,
                        label=label_text,
                        question_type=q_type,
                        required=is_req,
                        selector=f"#{field_id}" if el.get_attribute("id") else "",
                    )
                )

        except Exception as ex:
            logger.warning(f"Greenhouse discovery partial error: {ex}")

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

        # 1. Check for blocking conditions
        has_captcha, cap_msg = self.detect_captcha(page)
        if has_captcha:
            raise CaptchaDetectedError(f"Greenhouse CAPTCHA detected: {cap_msg}")

        has_login, log_msg = self.check_auth_state(page)
        if has_login:
            raise AuthenticationRequiredError(f"Greenhouse login required: {log_msg}")

        is_stale, stale_msg = self.detect_stale_job(page)
        if is_stale:
            raise JobStaleError(f"Greenhouse job closed/stale: {stale_msg}")

        # 2. Fill standard personal fields
        standard_mappings = [
            (
                "#first_name, input[name*='first_name'], input[autocomplete='given-name']",
                profile.first_name,
                "First Name",
            ),
            (
                "#last_name, input[name*='last_name'], input[autocomplete='family-name']",
                profile.last_name,
                "Last Name",
            ),
            (
                "#email, input[name*='email'], input[autocomplete='email']",
                profile.email,
                "Email",
            ),
            ("#phone, input[name*='phone']", profile.phone, "Phone"),
        ]
        for sel, val, name in standard_mappings:
            if not val:
                continue
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                try:
                    loc.first.fill(val)
                    report.fields_filled.append(name)
                    report.answers_provenance[name] = "profile"
                except Exception as ex:
                    report.errors.append(f"Failed to fill {name}: {ex}")

        # Fill cover letter if present
        if cover_letter:
            cl_loc = page.locator(
                "textarea#cover_letter, textarea[name*='cover_letter'], textarea[id*='cover_letter']"
            )
            if cl_loc.count() > 0 and cl_loc.first.is_visible():
                try:
                    cl_loc.first.fill(cover_letter)
                    report.cover_letter_filled = True
                    report.fields_filled.append("Cover Letter")
                    report.answers_provenance["Cover Letter"] = "tailored_artifact"
                except Exception as ex:
                    report.errors.append(f"Failed to fill cover letter: {ex}")

        # Optional profile links are often rendered as question_* fields on new boards.
        for sel, value, label in [
            (
                "input[aria-label*='LinkedIn' i], input[id^='question_'][id*='linkedin' i]",
                profile.linkedin_url,
                "LinkedIn",
            ),
            (
                "input[aria-label*='Website' i], input[id^='question_'][id*='website' i]",
                profile.portfolio_url or profile.github_url,
                "Website",
            ),
        ]:
            if value:
                loc = page.locator(sel)
                if (
                    loc.count() > 0
                    and loc.first.is_visible()
                    and input_is_empty(loc.first)
                ):
                    loc.first.fill(value)
                    report.fields_filled.append(label)
                    report.answers_provenance[label] = "profile"

        # 3. Answer custom questions fail-closed
        custom_fields = page.locator(
            ".field, .application-question, div[id^='custom_question'], "
            "input[id^='question_'], textarea[id^='question_'], [role='combobox'][id^='question_']"
        )
        count = custom_fields.count()
        for i in range(count):
            field_el = custom_fields.nth(i)
            try:
                q_text = accessible_label(field_el)
                if not q_text:
                    continue
                if requires_manual_review(q_text):
                    report.unknown_questions.append(q_text)
                    continue
                if not q_text:
                    continue

                # Skip if already filled
                text_input = field_el.locator(
                    "input[type='text'], input:not([type]), textarea"
                )
                if field_el.evaluate(
                    "(e) => ['input', 'textarea'].includes(e.tagName.toLowerCase())"
                ):
                    text_input = field_el
                if text_input.count() > 0 and not input_is_empty(text_input.first):
                    continue

                # Collect options for dropdown or radio
                options: list[str] = []
                select_el = field_el.locator("select")
                if select_el.count() > 0:
                    opt_elements = select_el.locator("option")
                    for opt_idx in range(opt_elements.count()):
                        val_text = opt_elements.nth(opt_idx).inner_text().strip()
                        if (
                            val_text
                            and not val_text.startswith("Select")
                            and not val_text.startswith("--")
                        ):
                            options.append(val_text)

                radio_elements = field_el.locator("input[type='radio']")
                if radio_elements.count() > 0:
                    for r_idx in range(radio_elements.count()):
                        lbl = field_el.locator(
                            f"label[for='{radio_elements.nth(r_idx).get_attribute('id')}']"
                        )
                        if lbl.count() > 0:
                            options.append(lbl.inner_text().strip())

                # Solve question safely
                if question_solver:
                    try:
                        ans_result = question_solver.solve_question_safely(
                            q_text, options=options
                        )
                        ans_value = ans_result.answer

                        if (
                            select_el.count() > 0
                            or field_el.get_attribute("role") == "combobox"
                        ):
                            if not select_semantic_option(field_el, str(ans_value)):
                                raise FormValidationError(
                                    [f"Could not select answer for '{q_text}'"]
                                )
                        elif radio_elements.count() > 0:
                            radio_input = field_el.locator(
                                f"input[type='radio'][value='{ans_value}']"
                            )
                            if radio_input.count() > 0:
                                radio_input.first.check()
                            else:
                                target_radio = field_el.locator(
                                    f"label:has-text('{ans_value}')"
                                )
                                if target_radio.count() > 0:
                                    target_radio.first.click()
                        elif text_input.count() > 0:
                            text_input.first.fill(str(ans_value))

                        report.fields_filled.append(q_text)
                        report.answers_provenance[q_text] = ans_result.provenance

                    except Exception as q_err:
                        err_name = type(q_err).__name__
                        if (
                            "UnknownQuestion" in err_name
                            or "SensitiveQuestion" in err_name
                        ):
                            report.unknown_questions.append(q_text)
                            # FAIL-CLOSED: unknown questions must stop automation
                            raise UnknownQuestionBlockedError(
                                q_text, options=options
                            ) from q_err
                        else:
                            report.errors.append(f"Error solving '{q_text}': {q_err}")
                            is_req = False
                            try:
                                is_req = bool(
                                    field_el.locator(
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
                logger.debug(f"Error processing Greenhouse custom field {i}: {e}")

        # Legal/privacy consent is intentionally never selected automatically.
        consent_boxes = page.locator(
            "input[type='checkbox'][name*='consent'], input[type='checkbox'][name*='privacy'], input[type='checkbox'][id*='gdpr']"
        )
        for c_idx in range(consent_boxes.count()):
            label = (
                accessible_label(consent_boxes.nth(c_idx))
                or f"Consent Checkbox {c_idx}"
            )
            report.unknown_questions.append(label)

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
            # File input selector for Greenhouse
            file_input = page.locator(
                "#resume, input[type='file'][name*='resume'], input[type='file']"
            )
            if file_input.count() == 0:
                raise UploadRejectedError(
                    "No file upload input element found for Greenhouse resume"
                )

            file_input.first.set_input_files(str(resume_path.resolve()))

            if cover_letter_path is not None and cover_letter_path.exists():
                cover_input = page.locator(
                    "#cover_letter, input[type='file'][name*='cover_letter'], input[type='file'][id*='cover_letter']"
                )
                if cover_input.count() > 0:
                    cover_input.first.set_input_files(str(cover_letter_path.resolve()))

            # Verify that upload wasn't immediately rejected
            err = page.locator(".upload-error, .file-error, #resume-error")
            if err.count() > 0 and err.first.is_visible():
                raise UploadRejectedError(
                    f"Greenhouse rejected CV upload: {err.first.inner_text()}"
                )

            return True

        except UploadRejectedError:
            raise
        except Exception as ex:
            raise UploadRejectedError(
                f"Failed to upload resume to Greenhouse: {ex}"
            ) from ex

    def validate_form(self, page: Any) -> list[ValidationError]:
        errors: list[ValidationError] = []
        try:
            # Greenhouse field errors
            err_locators = page.locator(
                ".field-error, .field_with_errors, [aria-invalid='true']"
            )
            count = err_locators.count()
            for i in range(count):
                el = err_locators.nth(i)
                if el.is_visible():
                    msg = el.inner_text().strip() or "Required field error"
                    errors.append(
                        ValidationError(field_id=f"field_error_{i}", message=msg)
                    )

            # Explanations summary
            summary = page.locator("#error_explanation")
            if summary.count() > 0 and summary.first.is_visible():
                errors.append(
                    ValidationError(
                        field_id="summary", message=summary.first.inner_text().strip()
                    )
                )

        except Exception as ex:
            logger.debug(f"Validation inspection error: {ex}")

        return errors

    def submit(
        self,
        page: Any,
        on_submit_intent: Callable[[], None] | None = None,
    ) -> bool:
        # 1. Locate Greenhouse submit button
        submit_btn = page.locator(
            "#submit_app, input[type='submit']#submit_app, button[id='submit_app'], button:has-text('Submit Application')"
        )
        if submit_btn.count() == 0 or not submit_btn.first.is_visible():
            raise FormValidationError(
                ["Greenhouse submit button (#submit_app) not found or not visible"]
            )

        # 2. Invoke submit intent safety hook before physical click
        if on_submit_intent:
            on_submit_intent()

        # 3. Physically click submit
        submit_btn.first.click()
        return True

    def confirm_submission(self, page: Any) -> ConfirmationEvidence:
        """
        Verifies Greenhouse submission confirmation evidence.
        Greenhouse displays #application_confirmation or confirmation-specific elements.
        Generic 'thank you' text or random redirects fail closed as ambiguous.
        """
        try:
            # 1. Look for definitive Greenhouse confirmation containers
            conf_container = page.locator(
                "#application_confirmation, .application-confirmation, #application-confirmation"
            )
            if conf_container.count() > 0 and conf_container.first.is_visible():
                text = conf_container.first.inner_text().strip()
                # Extract confirmation ID / receipt if present
                conf_id_match = re.search(
                    r"(?:confirmation(?:\s*(?:id|number|ref))?|reference(?:\s*id)?|receipt|id)[:\s#]+([A-Za-z0-9-]+)",
                    text,
                    re.IGNORECASE,
                )
                conf_id = conf_id_match.group(1) if conf_id_match else None

                return ConfirmationEvidence(
                    platform=self.adapter_name,
                    adapter_version=self.adapter_version,
                    confirmed=True,
                    confirmation_id=conf_id,
                    confirmation_text=text[:300],
                    proof_element="#application_confirmation",
                    is_ambiguous=False,
                    details={"matched_container": "#application_confirmation"},
                )

            # 2. Check for Greenhouse specific heading on greenhouse domain
            url = page.url.lower()
            if "greenhouse.io" in url or "grnh.se" in url:
                heading = page.locator(
                    "h1:has-text('Thank you for applying'), h1:has-text('Application Submitted')"
                )
                if heading.count() > 0 and heading.first.is_visible():
                    return ConfirmationEvidence(
                        platform=self.adapter_name,
                        adapter_version=self.adapter_version,
                        confirmed=True,
                        confirmation_id=None,
                        confirmation_text=heading.first.inner_text().strip(),
                        proof_element="h1[greenhouse]",
                        is_ambiguous=False,
                        details={"url": page.url},
                    )

            # 3. Check for in-page error messages after submission
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

            # 4. If neither definitive confirmation nor explicit error was found: AMBIGUOUS
            return ConfirmationEvidence(
                platform=self.adapter_name,
                adapter_version=self.adapter_version,
                confirmed=False,
                is_ambiguous=True,
                confirmation_text="Submission outcome could not be definitively confirmed on Greenhouse page",
                details={"current_url": page.url},
            )

        except Exception as ex:
            return ConfirmationEvidence(
                platform=self.adapter_name,
                adapter_version=self.adapter_version,
                confirmed=False,
                is_ambiguous=True,
                confirmation_text=f"Error checking Greenhouse confirmation: {ex}",
            )
