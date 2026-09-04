"""Automation package for automated job applications."""

from job_applier.automation.autofill_script import (  # type: ignore[import-not-found]
    generate_autofill_javascript,
    generate_bookmarklet_string,
    save_autofill_assets,
)
from job_applier.automation.browser_automator import (  # type: ignore[import-not-found]
    BrowserAutomator,
)
from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    CandidateProfile,
    load_candidate_profile,
    save_candidate_profile,
)
from job_applier.automation.question_solver import (  # type: ignore[import-not-found]
    QuestionSolver,
)

__all__ = [
    "CandidateProfile",
    "load_candidate_profile",
    "save_candidate_profile",
    "QuestionSolver",
    "BrowserAutomator",
    "generate_autofill_javascript",
    "generate_bookmarklet_string",
    "save_autofill_assets",
]
