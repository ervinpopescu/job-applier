from unittest.mock import MagicMock, patch

import pytest
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


def test_engine_validation_and_profile_isolation(tmp_path):
    with patch(
        "job_applier.automation.browser_runtime.get_project_root", return_value=tmp_path
    ):
        chrome_automator = BrowserAutomator(headless=True, browser="chrome")
        assert chrome_automator.engine == "chrome"
        assert chrome_automator.profile_dir == tmp_path / ".browser_profile"

        firefox_automator = BrowserAutomator(headless=True, browser="firefox")
        assert firefox_automator.engine == "firefox"
        assert firefox_automator.profile_dir == tmp_path / ".browser_profile_firefox"
        assert firefox_automator.profile_dir != chrome_automator.profile_dir

        with pytest.raises(ValueError, match="Unsupported browser engine 'opera'"):
            BrowserAutomator(headless=True, browser="opera")


def test_explicit_headed_without_display_raises_error():
    with (
        patch(
            "job_applier.automation.browser_automator.is_display_available",
            return_value=False,
        ),
        patch(
            "job_applier.automation.browser_automator.VirtualDisplayManager.ensure_display",
            return_value=None,
        ),
    ):
        with pytest.raises(
            RuntimeError, match="Headed mode requested \\(headless=False\\)"
        ):
            BrowserAutomator(headless=False)


def test_auto_mode_without_display_falls_back_to_headless():
    with (
        patch(
            "job_applier.automation.browser_automator.is_display_available",
            return_value=False,
        ),
        patch(
            "job_applier.automation.browser_automator.VirtualDisplayManager.ensure_display",
            return_value=None,
        ),
    ):
        automator = BrowserAutomator(headless=None)
        assert automator.headless is True


def test_firefox_playwright_launch_persistent_context_mocked(tmp_path):
    mock_playwright = MagicMock()
    mock_firefox = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_context.pages = [mock_page]
    mock_firefox.launch_persistent_context.return_value = mock_context
    mock_playwright.firefox = mock_firefox
    mock_sync_playwright = MagicMock()
    mock_sync_playwright.return_value.start.return_value = mock_playwright

    with patch.dict(
        "sys.modules",
        {"playwright.sync_api": MagicMock(sync_playwright=mock_sync_playwright)},
    ):
        automator = BrowserAutomator(
            headless=True,
            browser="firefox",
            use_persistent_profile=True,
            profile_dir=tmp_path / "ff_profile",
        )
        automator.start()

        mock_firefox.launch_persistent_context.assert_called_once()
        call_kwargs = mock_firefox.launch_persistent_context.call_args.kwargs
        assert call_kwargs["user_data_dir"] == str(tmp_path / "ff_profile")
        assert call_kwargs["headless"] is True
        # Verify Chromium-specific flags are NOT passed to Firefox
        assert "args" not in call_kwargs
        assert "executable_path" not in call_kwargs
        assert mock_playwright.chromium.launch_persistent_context.call_count == 0
        automator.close()


def test_chromium_playwright_launch_persistent_context_mocked(tmp_path):
    mock_playwright = MagicMock()
    mock_chromium = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()
    mock_context.pages = [mock_page]
    mock_chromium.launch_persistent_context.return_value = mock_context
    mock_playwright.chromium = mock_chromium
    mock_sync_playwright = MagicMock()
    mock_sync_playwright.return_value.start.return_value = mock_playwright

    with patch.dict(
        "sys.modules",
        {"playwright.sync_api": MagicMock(sync_playwright=mock_sync_playwright)},
    ):
        automator = BrowserAutomator(
            headless=True,
            browser="chromium",
            use_persistent_profile=True,
            profile_dir=tmp_path / "chrome_profile",
        )
        automator.start()

        mock_chromium.launch_persistent_context.assert_called_once()
        call_kwargs = mock_chromium.launch_persistent_context.call_args.kwargs
        assert call_kwargs["user_data_dir"] == str(tmp_path / "chrome_profile")
        assert call_kwargs["headless"] is True
        assert "args" in call_kwargs
        assert any("--disable-blink-features" in a for a in call_kwargs["args"])
        assert mock_playwright.firefox.launch_persistent_context.call_count == 0
        automator.close()


def test_firefox_smoke_live():
    """Smoke test running real Playwright Firefox headless if available in environment."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.firefox.launch(headless=True)
            page = browser.new_page()
            page.set_content("<html><body><h1>Firefox OK</h1></body></html>")
            assert "Firefox OK" in page.content()
            browser.close()
    except Exception as exc:
        pytest.skip(f"Firefox not runnable in current environment: {exc}")
