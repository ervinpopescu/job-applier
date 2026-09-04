from job_applier.automation.autofill_script import (  # type: ignore[import-not-found]
    generate_autofill_javascript,
    generate_bookmarklet_string,
    save_autofill_assets,
)
from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    CandidateProfile,
)


def test_generate_autofill_javascript():
    profile = CandidateProfile(
        full_name="Alex Example",
        first_name="Alex",
        last_name="Example",
        email="alex@example.com",
        phone="+1555010100",
        city="Example City",
        country="Example Country",
    )

    js = generate_autofill_javascript(
        profile=profile,
        cover_letter="Dear Hiring Team, I am eager to apply...",
        cv_path="/path/to/CV_Adobe.pdf",
    )

    assert "Alex Example" in js
    assert "alex@example.com" in js
    assert "+1555010100" in js
    assert "Dear Hiring Team" in js
    assert "/path/to/CV_Adobe.pdf" in js


def test_generate_bookmarklet():
    js = "console.log('hello');\nconst x = 10;"
    bm = generate_bookmarklet_string(js)
    assert bm.startswith("javascript:")
    assert "console.log('hello');" in bm


def test_save_autofill_assets(tmp_path):
    app_dir = tmp_path / "test_app_folder"
    app_dir.mkdir(parents=True, exist_ok=True)
    profile = CandidateProfile(full_name="Test User", email="user@test.com")

    save_autofill_assets(app_dir, profile, cover_letter="Letter content")

    js_file = app_dir / "autofill.js"
    bm_file = app_dir / "autofill_bookmarklet.txt"

    assert js_file.exists()
    assert bm_file.exists()
    assert "user@test.com" in js_file.read_text(encoding="utf-8")
    assert bm_file.read_text(encoding="utf-8").startswith("javascript:")
