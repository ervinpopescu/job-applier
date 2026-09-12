from __future__ import annotations

from typing import Any


_ALWAYS_MANUAL_REVIEW_TERMS = (
    "consent",
    "privacy policy",
    "privacy notice",
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


def requires_manual_review(question_text: str) -> bool:
    """Identify fields that must remain untouched without an explicit operator action."""
    normalized = " ".join(question_text.lower().split())
    return any(term in normalized for term in _ALWAYS_MANUAL_REVIEW_TERMS)


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
