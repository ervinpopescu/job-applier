from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class QuestionType(str, Enum):
    TEXT = "text"
    TEXTAREA = "textarea"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    SELECT = "select"
    MULTISELECT = "multiselect"
    FILE = "file"
    DATE = "date"
    UNKNOWN = "unknown"


@dataclass
class AdapterMetadata:
    name: str
    version: str
    can_submit: bool = True
    display_name: str = ""


@dataclass
class QuestionField:
    field_id: str
    label: str
    question_type: QuestionType
    required: bool = False
    options: list[str] = field(default_factory=list)
    selector: str = ""
    current_value: Any | None = None


@dataclass
class FormStep:
    step_index: int
    title: str = ""
    fields: list[QuestionField] = field(default_factory=list)
    is_last_step: bool = True


@dataclass
class FormDiscovery:
    platform: str
    adapter_version: str
    steps: list[FormStep] = field(default_factory=list)
    questions: list[QuestionField] = field(default_factory=list)
    has_captcha: bool = False
    has_login_wall: bool = False
    is_stale: bool = False
    stale_reason: str = ""


@dataclass
class FillReport:
    platform: str
    adapter_version: str
    fields_filled: list[str] = field(default_factory=list)
    resume_uploaded: bool = False
    cover_letter_filled: bool = False
    answers_provenance: dict[str, str] = field(default_factory=dict)
    unknown_questions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "adapter_version": self.adapter_version,
            "fields_filled": self.fields_filled,
            "resume_uploaded": self.resume_uploaded,
            "cover_letter_filled": self.cover_letter_filled,
            "answers_provenance": self.answers_provenance,
            "unknown_questions": self.unknown_questions,
            "errors": self.errors,
        }


@dataclass
class ValidationError:
    field_id: str
    message: str
    selector: str = ""


@dataclass
class ConfirmationEvidence:
    platform: str
    adapter_version: str
    confirmed: bool
    confirmation_id: str | None = None
    confirmation_text: str = ""
    proof_element: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )
    is_ambiguous: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "adapter_version": self.adapter_version,
            "confirmed": self.confirmed,
            "confirmation_id": self.confirmation_id,
            "confirmation_text": self.confirmation_text,
            "proof_element": self.proof_element,
            "timestamp": self.timestamp,
            "is_ambiguous": self.is_ambiguous,
            "details": self.details,
        }
