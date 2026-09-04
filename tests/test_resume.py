import json
import pytest
from pathlib import Path
from job_applier.resume.resume import generate_resume


@pytest.fixture
def sample_resume_data():
    return {
        "contact": {
            "name": "Test User",
            "email": "test@example.com",
            "phone": "123-456-7890",
        },
        "summary": "This is a test summary.",
        "experience": [
            {
                "role": "Test Role",
                "company": "Test Corp",
                "dates": "2020 - Present",
                "details": ["Did things.", "Accomplished stuff."],
            }
        ],
        "skills": ["Python", "Testing"],
        "education": {"institution": "Test University", "degree": "B.Sc. Testing"},
        "projects": [],
    }


def test_generate_pdf(sample_resume_data, tmp_path):
    output_file = tmp_path / "test_output.pdf"

    # Run the generator
    generate_resume(sample_resume_data, str(output_file))

    # Check if file was created
    assert output_file.exists()
    assert output_file.stat().st_size > 0


def test_generate_pdf_unicode(sample_resume_data, tmp_path):
    # Add unicode characters that typically cause latin-1 errors
    sample_resume_data["summary"] = (
        "Summary with en-dash \u2013 and curly quotes \u201c \u201d"
    )
    sample_resume_data["experience"][0]["details"].append("Bullet point \u2022")

    output_file = tmp_path / "test_unicode_output.pdf"

    # Run the generator - should not raise encoding error
    generate_resume(sample_resume_data, str(output_file))

    assert output_file.exists()
    assert output_file.stat().st_size > 0


def test_resume_structure_loading():
    """Verify master_resume.json exists and has valid structure"""
    master_path = Path("data/master_resume.json")
    if master_path.exists():
        with open(master_path, "r") as f:
            data = json.load(f)
        assert "contact" in data
        assert "experience" in data
