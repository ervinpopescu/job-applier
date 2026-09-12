from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from job_applier.utils import get_project_root


@dataclass
class CandidateProfile:
    full_name: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    country: str = ""
    address: str = ""
    postal_code: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    languages: str | dict[str, str] = field(
        default_factory=lambda: {"English": "Fluent", "Romanian": "Native"}
    )

    # Current Employment & Education
    current_company: str = ""
    current_title: str = ""
    years_of_experience: str = ""
    education_institution: str = ""
    education_degree: str = ""

    # Common Screening Answers
    work_authorization: str | dict[str, Any] = field(
        default_factory=lambda: {
            "authorized_in_us": False,
            "authorized_in_eu": True,
            "requires_sponsorship": False,
            "requires_us_sponsorship": False,
            "requires_eu_sponsorship": False,
        }
    )
    sponsorship_required: str = "No"  # Requires visa sponsorship
    notice_period: str = "Immediate"
    salary_expectation: str | dict[str, Any] = field(
        default_factory=lambda: {
            "minimum": 60000,
            "desired": 75000,
            "currency": "EUR",
            "period": "yearly",
        }
    )
    willing_to_relocate: str | bool = False
    remote_preference: str = "Remote only"
    gender: str = "Decline to self-identify"
    veteran_status: str = "I am not a protected veteran"
    disability_status: str = "I do not have a disability"

    eeo_defaults: dict[str, str] = field(
        default_factory=lambda: {
            "gender": "Decline to self-identify",
            "race": "Decline to self-identify",
            "veteran": "I am not a protected veteran",
            "disability": "I do not have a disability",
        }
    )

    # Search & AI Customization Preferences
    target_roles: list[str] = field(default_factory=list)
    target_locations: list[str] = field(default_factory=list)
    target_region: str = "EMEA"
    custom_ai_instructions: str = ""
    greenhouse_companies: list[str] = field(default_factory=list)
    lever_companies: list[str] = field(default_factory=list)

    # Custom screening answers mapping (keyword/phrase -> answer)
    custom_answers: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def get_field(self, key: str, default: str = "") -> str:
        """Safe retrieval of profile field as string."""
        val = getattr(self, key, default)
        if val is None:
            return default
        if isinstance(val, dict):
            if "desired" in val and "currency" in val:
                return f"{val['desired']} {val['currency']}"
            return json.dumps(val)
        if isinstance(val, bool):
            return "Yes" if val else "No"
        return str(val or default)

    def get_salary_desired(self) -> int | float | None:
        if isinstance(self.salary_expectation, dict):
            return self.salary_expectation.get("desired")
        if isinstance(self.salary_expectation, (int, float)):
            return self.salary_expectation
        if isinstance(self.salary_expectation, str):
            digits = re.findall(r"\d+", self.salary_expectation.replace(",", ""))
            if digits:
                return float(digits[0])
        return None

    def get_salary_minimum(self) -> int | float | None:
        if isinstance(self.salary_expectation, dict):
            return self.salary_expectation.get("minimum")
        return self.get_salary_desired()

    def get_salary_currency(self) -> str:
        if isinstance(self.salary_expectation, dict):
            return str(self.salary_expectation.get("currency", "EUR"))
        return "EUR"

    def is_authorized_to_work(self, location: str = "") -> bool:
        if isinstance(self.work_authorization, dict):
            loc_low = location.lower()
            if "us" in loc_low or "united states" in loc_low:
                return bool(self.work_authorization.get("authorized_in_us", False))
            if "eu" in loc_low or "europe" in loc_low or "romania" in loc_low:
                return bool(self.work_authorization.get("authorized_in_eu", True))
            return bool(self.work_authorization.get("authorized_in_eu", True))
        if isinstance(self.work_authorization, str):
            return self.work_authorization.strip().lower() in (
                "yes",
                "true",
                "authorized",
                "eligible",
            )
        return True

    def requires_visa_sponsorship(self, location: str = "") -> bool:
        if isinstance(self.work_authorization, dict):
            loc_low = location.lower()
            if "us" in loc_low or "united states" in loc_low:
                return bool(
                    self.work_authorization.get(
                        "requires_us_sponsorship",
                        self.work_authorization.get("requires_sponsorship", False),
                    )
                )
            if "eu" in loc_low or "europe" in loc_low or "romania" in loc_low:
                return bool(
                    self.work_authorization.get(
                        "requires_eu_sponsorship",
                        self.work_authorization.get("requires_sponsorship", False),
                    )
                )
            return bool(self.work_authorization.get("requires_sponsorship", False))
        if isinstance(self.sponsorship_required, str):
            return self.sponsorship_required.strip().lower() in (
                "yes",
                "true",
                "required",
            )
        return False

    def get_language_proficiency(self, language: str) -> str | None:
        lang_low = language.lower().strip()
        if isinstance(self.languages, dict):
            for k, v in self.languages.items():
                if k.lower() in lang_low or lang_low in k.lower():
                    return v
        elif isinstance(self.languages, str):
            if lang_low in self.languages.lower():
                return "Fluent"
        return None

    def is_willing_to_relocate(self) -> bool:
        if isinstance(self.willing_to_relocate, bool):
            return self.willing_to_relocate
        if isinstance(self.willing_to_relocate, str):
            return self.willing_to_relocate.strip().lower() in ("yes", "true")
        return False

    def get_eeo_answer(self, category: str) -> str:
        cat_low = category.lower().strip()
        if "gender" in cat_low or "sex" in cat_low:
            return (
                self.eeo_defaults.get("gender")
                or self.gender
                or "Decline to self-identify"
            )
        if "race" in cat_low or "ethnic" in cat_low:
            return self.eeo_defaults.get("race") or "Decline to self-identify"
        if "veteran" in cat_low:
            return (
                self.eeo_defaults.get("veteran")
                or self.veteran_status
                or "I am not a protected veteran"
            )
        if "disability" in cat_low or "handicap" in cat_low:
            return (
                self.eeo_defaults.get("disability")
                or self.disability_status
                or "I do not have a disability"
            )
        return "Decline to self-identify"


def load_candidate_profile(
    profile_path: Path | None = None,
    master_resume_path: Path | None = None,
) -> CandidateProfile:
    """Loads candidate profile from candidate_profile.json or synthesizes from master_resume.json."""
    project_root = get_project_root()
    if profile_path is None:
        profile_path = project_root / "data" / "candidate_profile.json"
    if master_resume_path is None:
        master_resume_path = project_root / "data" / "master_resume.json"

    profile = CandidateProfile()

    # 1. First populate from master_resume.json if available
    if master_resume_path.exists():
        try:
            with open(master_resume_path, encoding="utf-8") as f:
                resume_data = json.load(f)

            contact = resume_data.get("contact", {})
            full_name = contact.get("name", "").strip()
            profile.full_name = full_name
            if full_name:
                parts = full_name.split()
                profile.first_name = parts[0]
                profile.last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

            profile.email = contact.get("email", "")
            profile.phone = contact.get("phone", "")
            github = contact.get("github", "")
            if github:
                profile.github_url = (
                    github if github.startswith("http") else f"https://{github}"
                )
                profile.portfolio_url = profile.github_url
            linkedin = contact.get("linkedin", "")
            if linkedin:
                profile.linkedin_url = (
                    linkedin if linkedin.startswith("http") else f"https://{linkedin}"
                )
            elif github and "github.com/" in github:
                username = github.rstrip("/").split("/")[-1]
                profile.linkedin_url = f"https://www.linkedin.com/in/{username}"

            loc = contact.get("location", "")
            if loc:
                profile.address = loc
                loc_parts = [p.strip() for p in loc.split(",") if p.strip()]
                if len(loc_parts) >= 1:
                    profile.city = loc_parts[0]
                if len(loc_parts) >= 2:
                    profile.country = loc_parts[1]

            langs = contact.get("languages", "")
            if langs:
                profile.languages = langs

            # Extract current company and title from first experience entry
            experience = resume_data.get("experience", [])
            if experience and isinstance(experience, list):
                latest = experience[0]
                profile.current_company = latest.get("company", "")
                profile.current_title = latest.get("role", "")

            # Extract education
            education = resume_data.get("education", {})
            if isinstance(education, dict):
                profile.education_institution = education.get("institution", "")
                profile.education_degree = education.get("degree", "")
        except Exception as e:
            print(f"Warning: Could not fully parse master resume for profile: {e}")

    # 2. Override with candidate_profile.json if it exists
    if profile_path.exists():
        try:
            with open(profile_path, encoding="utf-8") as f:
                overrides = json.load(f)
            for k, v in overrides.items():
                if hasattr(profile, k) and v is not None:
                    setattr(profile, k, v)
        except Exception as e:
            print(f"Warning: Could not load candidate_profile.json: {e}")

    return profile


def save_candidate_profile(
    profile: CandidateProfile, profile_path: Path | None = None
) -> Path:
    """Saves candidate profile to data/candidate_profile.json for easy user customization."""
    project_root = get_project_root()
    if profile_path is None:
        profile_path = project_root / "data" / "candidate_profile.json"

    profile_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(profile.to_dict(), f, indent=2)
    except Exception as e:
        print(f"Error saving candidate profile to {profile_path}: {e}")
        raise

    return profile_path
