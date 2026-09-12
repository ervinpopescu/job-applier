from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from job_applier.automation.adapters.models import (
    AdapterMetadata,
    ConfirmationEvidence,
    FillReport,
    FormDiscovery,
    ValidationError,
)
from job_applier.automation.candidate_profile import CandidateProfile

logger = logging.getLogger("job_applier.adapters")


class BaseATSAdapter(ABC):
    """
    Abstract Base Class for versioned, typed ATS adapters.
    Implements standard contracts for detection, auth verification, form discovery,
    filling, document attachment, validation, submission, and confirmation.
    """

    adapter_name: str = "base"
    adapter_version: str = "1.0.0"
    can_submit: bool = True
    display_name: str = "Base ATS Adapter"

    def get_metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            name=self.adapter_name,
            version=self.adapter_version,
            can_submit=self.can_submit,
            display_name=self.display_name,
        )

    @abstractmethod
    def detect(self, url: str, page: Any | None = None) -> bool:
        """Determines if this adapter handles the given URL or page DOM."""
        raise NotImplementedError

    def detect_captcha(self, page: Any) -> tuple[bool, str]:
        """Detects if a CAPTCHA or bot mitigation challenge is present on the page."""
        try:
            # Common CAPTCHA selectors across providers
            captcha_selectors = [
                "iframe[src*='recaptcha']",
                "iframe[src*='hcaptcha']",
                "iframe[src*='challenges.cloudflare.com']",
                "iframe[src*='turnstile']",
                ".g-recaptcha",
                ".h-captcha",
                "#cf-turnstile",
                "[data-sitekey]",
                "div[id*='captcha']",
            ]
            for sel in captcha_selectors:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    return True, f"Captcha element found matching '{sel}'"

            # Check text indicators
            content = page.content().lower()
            if (
                "verify you are human" in content
                or "complete the security check to continue" in content
            ):
                return True, "Human verification challenge text detected"

        except Exception as ex:
            logger.debug(f"Error during captcha check: {ex}")

        return False, ""

    def check_auth_state(self, page: Any) -> tuple[bool, str]:
        """Detects whether the page is blocked behind a login or SSO wall."""
        try:
            url = page.url.lower()
            if any(
                term in url
                for term in ["/login", "/signin", "/auth/login", "/session/new"]
            ):
                return True, f"Login URL detected: {page.url}"

            # Check for visible password fields which indicate a login wall
            pwd = page.locator("input[type='password']")
            if pwd.count() > 0 and pwd.first.is_visible():
                return True, "Password input field detected on page"

            content = page.content().lower()
            if "sign in to apply" in content or "log in to your account" in content:
                return True, "Login prompt text detected"

        except Exception as ex:
            logger.debug(f"Error during auth check: {ex}")

        return False, ""

    def detect_stale_job(self, page: Any) -> tuple[bool, str]:
        """Detects whether the job opening is expired or closed."""
        try:
            stale_phrases = [
                "this job has expired",
                "this job posting has closed",
                "this position has been closed",
                "no longer accepting applications",
                "this job is no longer available",
                "job opening has closed",
                "this position is closed",
            ]
            content = page.content().lower()
            for phrase in stale_phrases:
                if phrase in content:
                    return True, f"Stale job indicator found: '{phrase}'"
        except Exception as ex:
            logger.debug(f"Error during stale check: {ex}")

        return False, ""

    @abstractmethod
    def discover_form(self, page: Any) -> FormDiscovery:
        """Discovers form structure, fields, and multi-step configuration."""
        raise NotImplementedError

    @abstractmethod
    def fill_fields(
        self,
        page: Any,
        profile: CandidateProfile,
        question_solver: Any | None = None,
        cover_letter: str = "",
    ) -> FillReport:
        """Fills application form fields using profile and safe question solver."""
        raise NotImplementedError

    @abstractmethod
    def upload_documents(
        self,
        page: Any,
        resume_path: Path,
        cover_letter_path: Path | None = None,
    ) -> bool:
        """Attaches CV PDF and optional cover letter to the form."""
        raise NotImplementedError

    @abstractmethod
    def validate_form(self, page: Any) -> list[ValidationError]:
        """Inspects the page for DOM validation errors or unsatisfied required fields."""
        raise NotImplementedError

    def advance_step(self, page: Any) -> bool:
        """Advances to the next step for multi-step forms. Returns True if advanced."""
        return False

    @abstractmethod
    def submit(
        self,
        page: Any,
        on_submit_intent: Callable[[], None] | None = None,
    ) -> bool:
        """
        Executes physical submission of the form.
        Invokes on_submit_intent hook immediately before clicking submit.
        """
        raise NotImplementedError

    @abstractmethod
    def confirm_submission(self, page: Any) -> ConfirmationEvidence:
        """
        Extracts adapter-specific verified confirmation evidence.
        Fails closed with is_ambiguous=True if proof cannot be verified with certainty.
        """
        raise NotImplementedError
