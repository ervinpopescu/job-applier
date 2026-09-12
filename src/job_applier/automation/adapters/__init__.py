from __future__ import annotations

from job_applier.automation.adapters.ashby import AshbyAdapter
from job_applier.automation.adapters.base import BaseATSAdapter
from job_applier.automation.adapters.exceptions import (
    ATSAdapterError,
    AuthenticationRequiredError,
    CaptchaDetectedError,
    FormValidationError,
    GenericAdapterCannotSubmitError,
    JobStaleError,
    MFARequiredError,
    StepAdvancementError,
    SubmissionAmbiguousError,
    UnknownQuestionBlockedError,
    UploadRejectedError,
)
from job_applier.automation.adapters.generic import GenericFormAdapter
from job_applier.automation.adapters.greenhouse import GreenhouseAdapter
from job_applier.automation.adapters.lever import LeverAdapter
from job_applier.automation.adapters.models import (
    AdapterMetadata,
    ConfirmationEvidence,
    FillReport,
    FormDiscovery,
    FormStep,
    QuestionField,
    QuestionType,
    ValidationError,
)
from job_applier.automation.adapters.registry import (
    ADAPTER_NAME_MAP,
    REGISTERED_ADAPTER_CLASSES,
    get_adapter_by_name,
    get_adapter_for_url,
    list_supported_adapters,
)

__all__ = [
    "BaseATSAdapter",
    "GreenhouseAdapter",
    "LeverAdapter",
    "AshbyAdapter",
    "GenericFormAdapter",
    "get_adapter_by_name",
    "get_adapter_for_url",
    "list_supported_adapters",
    "REGISTERED_ADAPTER_CLASSES",
    "ADAPTER_NAME_MAP",
    "QuestionType",
    "QuestionField",
    "FormStep",
    "FormDiscovery",
    "FillReport",
    "ValidationError",
    "ConfirmationEvidence",
    "AdapterMetadata",
    "ATSAdapterError",
    "AuthenticationRequiredError",
    "MFARequiredError",
    "CaptchaDetectedError",
    "FormValidationError",
    "SubmissionAmbiguousError",
    "GenericAdapterCannotSubmitError",
    "JobStaleError",
    "UploadRejectedError",
    "UnknownQuestionBlockedError",
    "StepAdvancementError",
]
