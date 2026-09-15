from __future__ import annotations

from typing import Any


class ATSAdapterError(Exception):
    """Base exception for ATS adapter operations."""


class AuthenticationRequiredError(ATSAdapterError):
    """Raised when an application page requires user authentication or login."""

    def __init__(
        self,
        message: str = "Authentication or login required",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.details = details or {}


class MFARequiredError(ATSAdapterError):
    """Raised when multi-factor authentication or verification code challenge is detected."""

    def __init__(
        self,
        message: str = "MFA or email/SMS verification required",
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.details = details or {}


class CaptchaDetectedError(ATSAdapterError):
    """Raised when CAPTCHA or bot challenge is detected on the application page."""

    def __init__(
        self,
        message: str = "CAPTCHA or bot challenge detected",
        captcha_type: str = "generic",
    ):
        super().__init__(message)
        self.captcha_type = captcha_type


class FormValidationError(ATSAdapterError):
    """Raised when form validation fails or required fields cannot be satisfied."""

    def __init__(self, errors: list[str] | None = None):
        errs = errors or []
        super().__init__(f"Form validation errors: {', '.join(errs)}")
        self.errors = errs


class SubmissionAmbiguousError(ATSAdapterError):
    """Raised when submission confirmation outcome is uncertain or cannot be verified."""

    def __init__(
        self,
        message: str = "Submission outcome is ambiguous",
        evidence: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.evidence = evidence or {}


class GenericAdapterCannotSubmitError(ATSAdapterError):
    """Raised when submission is attempted through the generic adapter (which is fill-only)."""

    def __init__(
        self,
        message: str = "Generic form adapter is fill-only and prohibited from submitting applications",
    ):
        super().__init__(message)


class JobStaleError(ATSAdapterError):
    """Raised when the job posting is no longer active, closed, or expired."""

    def __init__(self, message: str = "Job posting is stale, closed, or expired"):
        super().__init__(message)


class UploadRejectedError(ATSAdapterError):
    """Raised when resume or document upload was rejected by the ATS or failed."""

    def __init__(self, message: str = "Document upload was rejected or failed"):
        super().__init__(message)


class UnknownQuestionBlockedError(ATSAdapterError):
    """Raised when a screening question cannot be answered safely with high confidence."""

    def __init__(self, question_text: str, options: list[str] | None = None):
        super().__init__(
            f"Unknown screening question cannot be answered safely: '{question_text}'"
        )
        self.question_text = question_text
        self.options = options or []


class StepAdvancementError(ATSAdapterError):
    """Raised when moving to the next step of a multi-step form fails."""

    def __init__(self, step: int, message: str = "Failed to advance to next form step"):
        super().__init__(f"Step {step}: {message}")
        self.step = step
