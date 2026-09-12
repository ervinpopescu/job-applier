import json

from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    CandidateProfile,
    load_candidate_profile,
    save_candidate_profile,
)


def test_candidate_profile_from_master_resume(tmp_path):
    master_file = tmp_path / "master_resume.json"
    dummy_data = {
        "contact": {
            "name": "Jane Doe",
            "email": "jane@example.com",
            "phone": "+1234567890",
            "github": "github.com/janedoe",
            "languages": "English",
        },
        "experience": [
            {
                "company": "Tech Corp",
                "role": "Lead Architect",
                "dates": "2022 - Present",
            }
        ],
        "education": {
            "institution": "MIT",
            "degree": "B.Sc. Computer Science",
        },
    }
    with open(master_file, "w", encoding="utf-8") as f:
        json.dump(dummy_data, f)

    profile = load_candidate_profile(
        profile_path=tmp_path / "nonexistent.json",
        master_resume_path=master_file,
    )

    assert profile.full_name == "Jane Doe"
    assert profile.first_name == "Jane"
    assert profile.last_name == "Doe"
    assert profile.email == "jane@example.com"
    assert profile.phone == "+1234567890"
    assert "janedoe" in profile.github_url
    assert profile.current_company == "Tech Corp"
    assert profile.current_title == "Lead Architect"
    assert profile.education_institution == "MIT"


def test_candidate_profile_override(tmp_path):
    master_file = tmp_path / "master_resume.json"
    with open(master_file, "w", encoding="utf-8") as f:
        json.dump({"contact": {"name": "Jane Doe", "email": "jane@example.com"}}, f)

    override_file = tmp_path / "candidate_profile.json"
    override_data = {
        "full_name": "Jane Smith",
        "first_name": "Jane",
        "last_name": "Smith",
        "notice_period": "2 weeks",
        "salary_expectation": "100k",
    }
    with open(override_file, "w", encoding="utf-8") as f:
        json.dump(override_data, f)

    profile = load_candidate_profile(
        profile_path=override_file,
        master_resume_path=master_file,
    )

    assert profile.full_name == "Jane Smith"
    assert profile.last_name == "Smith"
    assert profile.notice_period == "2 weeks"
    assert profile.salary_expectation == "100k"


def test_save_candidate_profile(tmp_path):
    target_file = tmp_path / "saved_profile.json"
    profile = CandidateProfile(
        full_name="Alex Brown",
        first_name="Alex",
        last_name="Brown",
        email="alex@example.com",
    )
    save_candidate_profile(profile, target_file)

    assert target_file.exists()
    with open(target_file, encoding="utf-8") as f:
        data = json.load(f)
    assert data["full_name"] == "Alex Brown"
    assert data["email"] == "alex@example.com"


def test_candidate_profile_enriched_defaults_and_helpers():
    profile = CandidateProfile(
        salary_expectation={
            "minimum": 60000,
            "desired": 75000,
            "currency": "EUR",
            "period": "yearly",
        },
        work_authorization={
            "authorized_in_us": False,
            "authorized_in_eu": True,
            "requires_sponsorship": False,
        },
        languages={"English": "Fluent", "Romanian": "Native"},
        willing_to_relocate=False,
        eeo_defaults={
            "gender": "Decline to self-identify",
            "race": "Decline to self-identify",
            "veteran": "I am not a protected veteran",
            "disability": "I do not have a disability",
        },
    )

    assert profile.get_salary_desired() == 75000
    assert profile.get_salary_minimum() == 60000
    assert profile.get_salary_currency() == "EUR"
    assert profile.is_authorized_to_work("Romania") is True
    assert profile.is_authorized_to_work("United States") is False
    assert profile.requires_visa_sponsorship("EU") is False
    assert profile.get_language_proficiency("english") == "Fluent"
    assert profile.get_language_proficiency("romanian") == "Native"
    assert profile.get_language_proficiency("spanish") is None
    assert profile.is_willing_to_relocate() is False
    assert profile.get_eeo_answer("gender") == "Decline to self-identify"
    assert profile.get_eeo_answer("race") == "Decline to self-identify"
    assert profile.get_eeo_answer("veteran") == "I am not a protected veteran"
    assert profile.get_eeo_answer("disability") == "I do not have a disability"
