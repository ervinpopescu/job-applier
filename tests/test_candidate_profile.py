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
