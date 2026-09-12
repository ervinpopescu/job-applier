from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("job_applier.adapters.semantic")

_ALWAYS_MANUAL_REVIEW_TERMS = (
    "capture of my photo",
    "photo identity",
    "gender",
    "race",
    "ethnicity",
    "veteran",
    "disability",
    "salary",
    "compensation",
    "remuneration",
)

_EXCLUDED_CONSENT_TERMS = (
    "gender",
    "race",
    "ethnicity",
    "veteran",
    "disability",
    "sexual orientation",
    "demographic",
    "background check",
    "criminal",
    "credit check",
    "drug test",
    "photo identity",
    "capture of my photo",
    "facial recognition",
    "biometric",
    "liability waiver",
    "release of liability",
    "hold harmless",
    "waive any and all",
)

_STANDARD_CONSENT_TERMS = (
    "privacy policy",
    "privacy notice",
    "privacy statement",
    "terms of service",
    "terms and conditions",
    "terms & conditions",
    "terms of use",
    "data processing",
    "personal data",
    "gdpr",
    "recruitment privacy",
    "applicant privacy",
    "candidate privacy",
    "processing of my personal data",
    "processing of personal data",
    "processing of my data",
    "consent to store",
    "consent to retain",
    "consent to process",
    "agree to the",
    "i agree",
    "i consent",
    "i acknowledge",
    "i accept",
    "consent",
    "agree",
)


def is_standard_consent_checkbox(
    label_text: str, tag_name: str = "input", input_type: str = "checkbox"
) -> bool:
    """
    Detects standard GDPR, privacy policy, terms of service, and application processing consent.
    Strictly excludes: voluntary demographic surveys (race, gender, veteran, disability),
    background check authorizations, or photo identity verification.
    """
    if not label_text:
        return False

    # Check input types
    t_name = (tag_name or "").strip().lower()
    i_type = (input_type or "").strip().lower()
    if t_name and t_name not in (
        "input",
        "button",
        "div",
        "span",
        "select",
        "label",
        "li",
    ):
        return False
    if i_type and i_type in (
        "text",
        "tel",
        "email",
        "file",
        "password",
        "number",
        "date",
    ):
        return False

    normalized = " ".join(label_text.lower().split())

    # Strictly exclude sensitive declarations, demographic surveys, background checks, waivers
    if any(ex in normalized for ex in _EXCLUDED_CONSENT_TERMS):
        return False

    # Check standard consent keywords
    if any(term in normalized for term in _STANDARD_CONSENT_TERMS):
        return True

    return False


def is_sensitive_or_excluded_checkbox(label_text: str) -> bool:
    """Returns True if the checkbox represents a sensitive declaration, demographic survey, or excluded waiver."""
    if not label_text:
        return False
    normalized = " ".join(label_text.lower().split())
    if any(ex in normalized for ex in _EXCLUDED_CONSENT_TERMS):
        return True
    if any(
        term in normalized
        for term in (
            "consent",
            "privacy",
            "gdpr",
            "waiver",
            "authorization",
            "authorize",
            "declaration",
            "agree",
            "acknowledgement",
        )
    ):
        return True
    return False


def requires_manual_review(question_text: str, auto_consent: bool = True) -> bool:
    """Identify fields that must remain untouched without an explicit operator action."""
    if auto_consent and is_standard_consent_checkbox(question_text):
        return False
    normalized = " ".join(question_text.lower().split())
    # If auto-consent is disabled, general consent/privacy terms require review
    if not auto_consent and any(
        term in normalized for term in ("consent", "privacy policy", "privacy notice")
    ):
        return True
    return any(term in normalized for term in _ALWAYS_MANUAL_REVIEW_TERMS)


def accessible_label(element: Any) -> str:
    """Return the strongest accessible label available for a Playwright element."""
    try:
        label = element.get_attribute("aria-label")
        if label:
            return label.strip()

        labelled_by = element.get_attribute("aria-labelledby")
        if labelled_by:
            text = element.evaluate(
                """(element, ids) => ids.split(/\\s+/)
                    .map(id => document.getElementById(id)?.innerText || '')
                    .join(' ')
                    .trim()""",
                labelled_by,
            )
            if text:
                return text.strip()

        field_id = element.get_attribute("id")
        if field_id:
            label = element.evaluate(
                """(element, id) => document.querySelector(`label[for="${CSS.escape(id)}"]`)
                    ?.innerText || ''""",
                field_id,
            )
            if label:
                return label.strip()

        label = element.evaluate(
            """element => element.closest('label')?.innerText || ''"""
        )
        if label:
            return label.strip()

        label = element.evaluate(
            """element => element.closest('.field, .application-label, .application-question, [data-qa="additional-cards"]')
                ?.querySelector('.text, .application-label, label')?.innerText || ''"""
        )
        if label:
            return label.strip()

        name = element.get_attribute("name")
        return (name or field_id or "").strip()
    except Exception:
        return ""


def combobox_for(element: Any) -> Any | None:
    """Find a native or ARIA combobox represented by an element or its scope."""
    try:
        if element.get_attribute("role") == "combobox":
            return element
        combo = element.locator("[role='combobox']")
        if combo.count() > 0:
            return combo.first
    except Exception:
        pass
    return None


def select_semantic_option(element: Any, answer: str) -> bool:
    """Select an option from native select or accessible custom combobox controls."""
    try:
        native = element.locator("select")
        if native.count() > 0:
            native.first.select_option(label=answer)
            return True

        combo = combobox_for(element)
        if combo is None:
            return False
        combo.click()
        options = element.locator("xpath=ancestor::body").locator("[role='option']")
        match = options.filter(has_text=answer)
        if match.count() == 0:
            combo.press("Escape")
            return False
        match.first.click()
        return True
    except Exception:
        return False


def input_is_empty(element: Any) -> bool:
    """Read an input value without assuming the element exposes input_value."""
    try:
        return not (element.input_value() or "").strip()
    except Exception:
        return True
