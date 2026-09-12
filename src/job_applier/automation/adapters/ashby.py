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
    StepAdvancementError,
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
from job_applier.automation.candidate_profile import CandidateProfile

logger = logging.getLogger("job_applier.adapters.ashby")


class AshbyAdapter(BaseATSAdapter):
    """
    Typed, versioned adapter for Ashby ATS job boards.
    Supports jobs.ashbyhq.com, ashbyhq.com, multi-step application flows, and custom questions.
    """

    adapter_name: str = "ashby"
    adapter_version: str = "1.0.0"
    can_submit: bool = True
    display_name: str = "Ashby Job Board"

    def detect(self, url: str, page: Any | None = None) -> bool:
        low_url = url.lower()
        if "ashbyhq.com" in low_url:
            return True

        if page is not None:
            try:
                if page.locator("[data-qa*='ashby'], div[class*='ashby']").count() > 0:
                    return True
                content = page.content().lower()
                if (
                    "ashbyhq" in content
                    or "ashby" in content
                    and "application" in content
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
            # Check for multi-step indicator
            page.locator("[data-qa='step-indicator'], .ashby-step-indicator")

            # Detect fields on current step
            field_inputs = page.locator("input:not([type='hidden']), textarea, select")
            for i in range(field_inputs.count()):
                inp = field_inputs.nth(i)
                inp_id = (
                    inp.get_attribute("id")
                    or inp.get_attribute("name")
                    or f"ashby_field_{i}"
                )
                label_loc = page.locator(f"label[for='{inp_id}']")
                label_text = (
                    label_loc.inner_text().strip() if label_loc.count() > 0 else inp_id
                )

                q_type = QuestionType.TEXT
                t_attr = inp.get_attribute("type")
                if t_attr == "file":
                    q_type = QuestionType.FILE
                elif t_attr == "radio":
                    q_type = QuestionType.RADIO
                elif t_attr == "checkbox":
                    q_type = QuestionType.CHECKBOX
                elif inp.evaluate("el => el.tagName.toLowerCase()") == "textarea":
                    q_type = QuestionType.TEXTAREA
                elif inp.evaluate("el => el.tagName.toLowerCase()") == "select":
                    q_type = QuestionType.SELECT

                is_req = inp.get_attribute("required") is not None or "*" in label_text

                questions.append(
                    QuestionField(
                        field_id=inp_id,
                        label=label_text,
                        question_type=q_type,
                        required=is_req,
                    )
                )

            # Check if there is a 'Next' button indicating multiple steps
            next_btn = page.locator(
                "button:has-text('Next'), button:has-text('Continue'), [data-qa='next-step']"
            )
            is_last = next_btn.count() == 0 or not next_btn.first.is_visible()

            steps.append(
                FormStep(
                    step_index=1,
                    title="Current Step",
                    fields=questions,
                    is_last_step=is_last,
                )
            )

        except Exception as ex:
            logger.warning(f"Ashby discovery partial error: {ex}")

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

    def advance_step(self, page: Any) -> bool:
        """
        Advances to the next step of a multi-step Ashby application form.
        """
        next_btn = page.locator(
            "button:has-text('Next'), button:has-text('Continue'), [data-qa='next-step']"
        )
        if next_btn.count() > 0 and next_btn.first.is_visible():
            try:
                next_btn.first.click()
                return True
            except Exception as ex:
                raise StepAdvancementError(
                    1, f"Could not advance Ashby form step: {ex}"
                ) from ex
        return False

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

        # 1. Check blocking conditions
        has_captcha, cap_msg = self.detect_captcha(page)
        if has_captcha:
            raise CaptchaDetectedError(f"Ashby CAPTCHA detected: {cap_msg}")

        has_login, log_msg = self.check_auth_state(page)
        if has_login:
            raise AuthenticationRequiredError(f"Ashby login required: {log_msg}")

        is_stale, stale_msg = self.detect_stale_job(page)
        if is_stale:
            raise JobStaleError(f"Ashby job closed/stale: {stale_msg}")

        # 2. Fill standard name / email / phone
        # Ashby often has a single Name or separate First / Last
        name_input = page.locator(
            "input[name='name'], input[id*='name']:not([id*='first']):not([id*='last'])"
        )
        if name_input.count() > 0 and name_input.first.is_visible():
            name_input.first.fill(profile.full_name)
            report.fields_filled.append("Full Name")
            report.answers_provenance["Full Name"] = "profile"
        else:
            fn = page.locator("input[name*='first_name'], input[id*='first_name']")
            if fn.count() > 0 and fn.first.is_visible():
                fn.first.fill(profile.first_name)
                report.fields_filled.append("First Name")
                report.answers_provenance["First Name"] = "profile"
            ln = page.locator("input[name*='last_name'], input[id*='last_name']")
            if ln.count() > 0 and ln.first.is_visible():
                ln.first.fill(profile.last_name)
                report.fields_filled.append("Last Name")
                report.answers_provenance["Last Name"] = "profile"

        em = page.locator(
            "input[type='email'], input[name*='email'], input[id*='email']"
        )
        if em.count() > 0 and em.first.is_visible():
            em.first.fill(profile.email)
            report.fields_filled.append("Email")
            report.answers_provenance["Email"] = "profile"

        ph = page.locator("input[type='tel'], input[name*='phone'], input[id*='phone']")
        if ph.count() > 0 and ph.first.is_visible() and profile.phone:
            ph.first.fill(profile.phone)
            report.fields_filled.append("Phone")
            report.answers_provenance["Phone"] = "profile"

        # URLs
        if profile.linkedin_url:
            li = page.locator("input[name*='linkedin'], input[id*='linkedin']")
            if li.count() > 0 and li.first.is_visible():
                li.first.fill(profile.linkedin_url)
                report.fields_filled.append("LinkedIn")
                report.answers_provenance["LinkedIn"] = "profile"

        if profile.github_url:
            gh = page.locator("input[name*='github'], input[id*='github']")
            if gh.count() > 0 and gh.first.is_visible():
                gh.first.fill(profile.github_url)
                report.fields_filled.append("GitHub")
                report.answers_provenance["GitHub"] = "profile"

        # Cover letter
        if cover_letter:
            cl = page.locator(
                "textarea[name*='cover_letter'], textarea[id*='cover_letter'], textarea[name*='coverLetter']"
            )
            if cl.count() > 0 and cl.first.is_visible():
                cl.first.fill(cover_letter)
                report.cover_letter_filled = True
                report.fields_filled.append("Cover Letter")
                report.answers_provenance["Cover Letter"] = "tailored_artifact"

        # 3. Answer custom questions fail-closed
        custom_containers = page.locator(
            "div[class*='field'], div[class*='question'], [data-qa*='question']"
        )
        for i in range(custom_containers.count()):
            c_el = custom_containers.nth(i)
            try:
                lbl = c_el.locator("label, [class*='label']")
                if lbl.count() == 0:
                    continue
                q_text = lbl.first.inner_text().strip()
                if not q_text:
                    continue

                # Skip if already filled
                txt = c_el.locator("input[type='text'], textarea")
                if txt.count() > 0 and txt.first.input_value():
                    continue

                options: list[str] = []
                select_el = c_el.locator("select")
                if select_el.count() > 0:
                    opts = select_el.locator("option")
                    for o in range(opts.count()):
                        t = opts.nth(o).inner_text().strip()
                        if t and not t.startswith("Select") and not t.startswith("--"):
                            options.append(t)

                radio_el = c_el.locator("input[type='radio']")
                if radio_el.count() > 0:
                    for r in range(radio_el.count()):
                        r_lbl = c_el.locator(
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

                        if select_el.count() > 0:
                            select_el.first.select_option(label=ans_value)
                        elif radio_el.count() > 0:
                            radio_input = c_el.locator(
                                f"input[type='radio'][value='{ans_value}']"
                            )
                            if radio_input.count() > 0:
                                radio_input.first.check()
                            else:
                                target = c_el.locator(f"label:has-text('{ans_value}')")
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
                                    c_el.locator(
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
                logger.debug(f"Error processing Ashby field {i}: {e}")

        # Check consent checkboxes
        consent_boxes = page.locator(
            "input[type='checkbox'][name*='consent'], input[type='checkbox'][id*='consent']"
        )
        for c_idx in range(consent_boxes.count()):
            cb = consent_boxes.nth(c_idx)
            if not cb.is_checked():
                cb.check()
                report.fields_filled.append(f"Consent {c_idx}")
                report.answers_provenance[f"Consent {c_idx}"] = "standard_consent"

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
                "input[type='file'][name*='resume'], input[type='file'][data-qa*='resume'], input[type='file']"
            )
            if file_input.count() == 0:
                raise UploadRejectedError(
                    "No file upload input element found for Ashby resume"
                )

            file_input.first.set_input_files(str(resume_path.resolve()))

            # Check for upload error
            err = page.locator(".ashby-upload-error, [data-qa='upload-error']")
            if err.count() > 0 and err.first.is_visible():
                raise UploadRejectedError(
                    f"Ashby rejected resume upload: {err.first.inner_text()}"
                )

            return True

        except UploadRejectedError:
            raise
        except Exception as ex:
            raise UploadRejectedError(f"Failed to upload resume to Ashby: {ex}") from ex

    def validate_form(self, page: Any) -> list[ValidationError]:
        errors: list[ValidationError] = []
        try:
            err_loc = page.locator(
                "[aria-invalid='true'], .ashby-error-message, [data-qa='field-error']"
            )
            for i in range(err_loc.count()):
                el = err_loc.nth(i)
                if el.is_visible():
                    msg = el.inner_text().strip() or "Ashby validation error"
                    errors.append(
                        ValidationError(field_id=f"ashby_err_{i}", message=msg)
                    )
        except Exception as ex:
            logger.debug(f"Ashby validation inspection error: {ex}")

        return errors

    def submit(
        self,
        page: Any,
        on_submit_intent: Callable[[], None] | None = None,
    ) -> bool:
        submit_btn = page.locator(
            "button:has-text('Submit Application'), button[data-qa='submit-application'], button[type='submit']"
        )
        if submit_btn.count() == 0 or not submit_btn.first.is_visible():
            raise FormValidationError(["Ashby submit button not found or not visible"])

        if on_submit_intent:
            on_submit_intent()

        submit_btn.first.click()
        return True

    def confirm_submission(self, page: Any) -> ConfirmationEvidence:
        try:
            # 1. Check Ashby confirmation container
            conf_container = page.locator(
                "[data-qa='application-success'], .ashby-application-successful, .application-success-page"
            )
            if conf_container.count() > 0 and conf_container.first.is_visible():
                text = conf_container.first.inner_text().strip()
                ref_match = re.search(
                    r"(?:confirmation(?:\s*(?:receipt|id|ref))?|receipt|reference(?:\s*id)?|application\s*id)[:\s#]+([A-Za-z0-9-]+)",
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
                    proof_element="[data-qa='application-success']",
                    is_ambiguous=False,
                    details={"matched_container": "[data-qa='application-success']"},
                )

            # 2. Check heading on Ashby domain
            url = page.url.lower()
            if "ashbyhq.com" in url:
                heading = page.locator(
                    "h1:has-text('Application Submitted'), h1:has-text('Thank you for applying')"
                )
                if heading.count() > 0 and heading.first.is_visible():
                    return ConfirmationEvidence(
                        platform=self.adapter_name,
                        adapter_version=self.adapter_version,
                        confirmed=True,
                        confirmation_id=None,
                        confirmation_text=heading.first.inner_text().strip(),
                        proof_element="h1[ashby]",
                        is_ambiguous=False,
                        details={"url": page.url},
                    )

            # 3. Validation errors after submission
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

            # 4. Ambiguous fallback
            return ConfirmationEvidence(
                platform=self.adapter_name,
                adapter_version=self.adapter_version,
                confirmed=False,
                is_ambiguous=True,
                confirmation_text="Submission outcome could not be definitively confirmed on Ashby page",
                details={"current_url": page.url},
            )

        except Exception as ex:
            return ConfirmationEvidence(
                platform=self.adapter_name,
                adapter_version=self.adapter_version,
                confirmed=False,
                is_ambiguous=True,
                confirmation_text=f"Error checking Ashby confirmation: {ex}",
            )
