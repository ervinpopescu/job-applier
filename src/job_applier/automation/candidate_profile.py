from __future__ import annotations

import json
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
    languages: str = ""

    # Current Employment & Education
    current_company: str = ""
    current_title: str = ""
    years_of_experience: str = ""
    education_institution: str = ""
    education_degree: str = ""

    # Common Screening Answers
    work_authorization: str = "Yes"  # Authorized to work in the country
    sponsorship_required: str = "No"  # Requires visa sponsorship
    notice_period: str = "Negotiable / Standard"
    salary_expectation: str = "Negotiable"
    willing_to_relocate: str = "No"
    remote_preference: str = "Remote or Hybrid"
    gender: str = "Prefer not to say"
    veteran_status: str = "I am not a protected veteran"
    disability_status: str = "No, I do not have a disability"

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
        """Safe retrieval of profile field."""
        return str(getattr(self, key, default) or default)


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

    # Start with baseline profile
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

            profile.languages = contact.get("languages", "")

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
                if hasattr(profile, k):
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
