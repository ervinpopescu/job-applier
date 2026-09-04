from job_applier.automation.browser_automator import (  # type: ignore[import-not-found]
    BrowserAutomator,
)
from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    CandidateProfile,
)


def test_detect_platform():
    automator = BrowserAutomator()
    assert (
        automator.detect_platform("https://boards.greenhouse.io/datadog/jobs/123")
        == "Greenhouse"
    )
    assert automator.detect_platform("https://jobs.lever.co/spotify/456") == "Lever"
    assert (
        automator.detect_platform(
            "https://cognizant.wd3.myworkdayjobs.com/en-US/CognizantCareers"
        )
        == "Workday"
    )
    assert (
        automator.detect_platform("https://ro.indeed.com/viewjob?jk=79705f9688a84f47")
        == "Indeed"
    )
    assert (
        automator.detect_platform("https://www.linkedin.com/jobs/view/999")
        == "LinkedIn"
    )
    assert automator.detect_platform("https://jobs.ashbyhq.com/linear/abc") == "Ashby"
    assert automator.detect_platform("https://example.com/careers/job1") == "Generic"


def test_form_autofill_headless(tmp_path):
    # Create sample application folder with CV and cover letter
    app_folder = tmp_path / "TestCompany_Cloud_Engineer_0"
    app_folder.mkdir(parents=True, exist_ok=True)

    cv_file = app_folder / "CV_TestCompany_Cloud_Engineer.pdf"
    with open(cv_file, "w", encoding="utf-8") as f:
        f.write("mock pdf content")

    cl_file = app_folder / "cover_letter.txt"
    with open(cl_file, "w", encoding="utf-8") as f:
        f.write("I am excited to apply for the Cloud Engineer position.")

    # Create mock HTML application form
    html_file = tmp_path / "mock_application.html"
    html_content = """<!DOCTYPE html>
<html>
<head><title>Job Application</title></head>
<body>
    <form id="job-apply-form">
        <label for="first_name">First Name</label>
        <input type="text" id="first_name" name="first_name" />

        <label for="last_name">Last Name</label>
        <input type="text" id="last_name" name="last_name" />

        <label for="email">Email</label>
        <input type="email" id="email" name="email" />

        <label for="phone">Phone</label>
        <input type="tel" id="phone" name="phone" />

        <label for="resume">Resume / CV</label>
        <input type="file" id="resume" name="resume" />

        <label for="cover_letter">Cover Letter</label>
        <textarea id="cover_letter" name="cover_letter"></textarea>

        <label for="country">Country</label>
        <select id="country" name="country">
            <option value="">Select country</option>
            <option value="US">United States</option>
            <option value="RO">Romania</option>
        </select>

        <label>
            <input type="checkbox" name="privacy_consent" id="privacy_consent" />
            I agree to the privacy policy
        </label>

        <button type="submit" id="submit_btn">Submit Application</button>
    </form>
</body>
</html>"""
    with open(html_file, "w", encoding="utf-8") as f:
        f.write(html_content)

    profile = CandidateProfile(
        full_name="Alex Example",
        first_name="Alex",
        last_name="Example",
        email="alex@example.com",
        phone="+1555010100",
        city="Example City",
        country="Example Country",
    )

    automator = BrowserAutomator(
        profile=profile,
        headless=True,
        use_persistent_profile=False,
    )

    try:
        automator.start()
        file_url = f"file://{html_file.resolve()}"
        automator.page.goto(file_url)

        report = automator.fill_application_form(
            app_dir=app_folder,
            job_title="Cloud Engineer",
            company="TestCompany",
        )

        assert report["resume_uploaded"] is True
        assert report["cover_letter_filled"] is True
        assert "First Name" in report["fields_filled"]
        assert "Last Name" in report["fields_filled"]
        assert "Email" in report["fields_filled"]

        # Verify DOM values in page
        assert automator.page.locator("#first_name").input_value() == "Alex"
        assert automator.page.locator("#last_name").input_value() == "Example"
        assert automator.page.locator("#email").input_value() == "alex@example.com"
        assert automator.page.locator("#phone").input_value() == "+1555010100"
        assert (
            "excited to apply" in automator.page.locator("#cover_letter").input_value()
        )
        assert automator.page.locator("#privacy_consent").is_checked() is True

        submit_btn = automator.find_submit_button()
        assert submit_btn is not None
        assert "submit" in submit_btn.text_content().lower()

    finally:
        automator.close()
