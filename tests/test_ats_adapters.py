from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from job_applier.automation.adapters import (
    AshbyAdapter,
    GenericAdapterCannotSubmitError,
    GenericFormAdapter,
    GreenhouseAdapter,
    LeverAdapter,
    UnknownQuestionBlockedError,
    UploadRejectedError,
    get_adapter_by_name,
    get_adapter_for_url,
    list_supported_adapters,
)
from job_applier.automation.adapters.models import QuestionType
from job_applier.automation.candidate_profile import CandidateProfile
from job_applier.automation.question_solver import QuestionSolver


@pytest.fixture(scope="module")
def browser_context():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(browser_context):
    ctx = browser_context.new_context()
    pg = ctx.new_page()
    yield pg
    pg.close()
    ctx.close()


@pytest.fixture
def sample_profile() -> CandidateProfile:
    return CandidateProfile(
        full_name="Alex Morgan",
        first_name="Alex",
        last_name="Morgan",
        email="alex.morgan@example.com",
        phone="+1555123456",
        city="Bucharest",
        country="Romania",
        linkedin_url="https://linkedin.com/in/alexmorgan",
        github_url="https://github.com/alexmorgan",
        custom_answers={
            "authorized to work": "Yes",
            "sponsorship": "No",
            "years of experience": "7",
        },
    )


@pytest.fixture
def sample_cv(tmp_path: Path) -> Path:
    cv = tmp_path / "CV_TestCompany_Engineer.pdf"
    cv.write_bytes(
        b"%PDF-1.4 Mock CV Content with 100 bytes padding to be a valid file size for tests"
    )
    return cv


# =========================================================================
# 1. Adapter Registry & Metadata Tests
# =========================================================================


def test_adapter_registry_and_metadata():
    adapters = list_supported_adapters()
    assert len(adapters) == 4
    names = {a["name"] for a in adapters}
    assert names == {"greenhouse", "lever", "ashby", "generic"}

    gh = get_adapter_by_name("greenhouse")
    assert isinstance(gh, GreenhouseAdapter)
    assert gh.can_submit is True
    assert gh.adapter_version == "1.1.0"

    generic = get_adapter_by_name("generic")
    assert isinstance(generic, GenericFormAdapter)
    assert generic.can_submit is False  # Fill-only!


def test_adapter_url_detection():
    assert isinstance(
        get_adapter_for_url("https://boards.greenhouse.io/corp/jobs/123"),
        GreenhouseAdapter,
    )
    assert isinstance(get_adapter_for_url("https://grnh.se/abc1234"), GreenhouseAdapter)
    assert isinstance(
        get_adapter_for_url("https://jobs.lever.co/spotify/uuid"), LeverAdapter
    )
    assert isinstance(
        get_adapter_for_url("https://jobs.ashbyhq.com/linear/abc"), AshbyAdapter
    )
    assert isinstance(
        get_adapter_for_url("https://unknowncompany.com/apply"), GenericFormAdapter
    )


# =========================================================================
# 2. Greenhouse Adapter Fixture Tests
# =========================================================================


def test_greenhouse_full_lifecycle(page, sample_profile, sample_cv, tmp_path):
    """
    Tests Greenhouse adapter:
    auth check -> question discovery -> fill fields -> upload -> validate -> submit -> confirm.
    """
    adapter = GreenhouseAdapter()

    html = """<!DOCTYPE html>
    <html>
    <head><title>Greenhouse Careers</title></head>
    <body>
        <form id="application_form">
            <div id="main_fields">
                <label for="first_name">First Name *</label>
                <input type="text" id="first_name" name="job_application[first_name]" required />

                <label for="last_name">Last Name *</label>
                <input type="text" id="last_name" name="job_application[last_name]" required />

                <label for="email">Email *</label>
                <input type="email" id="email" name="job_application[email]" required />

                <label for="phone">Phone</label>
                <input type="tel" id="phone" name="job_application[phone]" />

                <label for="resume">Resume / CV *</label>
                <input type="file" id="resume" name="job_application[resume]" required />

                <label for="cover_letter">Cover Letter</label>
                <textarea id="cover_letter" name="job_application[cover_letter]"></textarea>
            </div>

            <div id="custom_fields">
                <div class="field">
                    <label for="q1">Are you authorized to work in the EU? *</label>
                    <select id="q1" name="q1">
                        <option value="">Select an option</option>
                        <option value="Yes">Yes</option>
                        <option value="No">No</option>
                    </select>
                </div>
            </div>

            <input type="submit" id="submit_app" value="Submit Application" />
        </form>
    </body>
    </html>"""
    form_file = tmp_path / "greenhouse_form.html"
    form_file.write_text(html, encoding="utf-8")
    page.goto(f"file://{form_file.resolve()}")

    # 1. Detection
    assert adapter.detect(page.url, page=page) is True

    # 2. Auth & Stale
    has_login, _ = adapter.check_auth_state(page)
    assert has_login is False
    is_stale, _ = adapter.detect_stale_job(page)
    assert is_stale is False

    # 3. Discovery
    discovery = adapter.discover_form(page)
    assert discovery.platform == "greenhouse"
    assert len(discovery.questions) >= 5

    # 4. Fill fields with QuestionSolver
    solver = QuestionSolver(profile=sample_profile)
    fill_report = adapter.fill_fields(
        page,
        sample_profile,
        question_solver=solver,
        cover_letter="I am excited to apply.",
    )
    assert "First Name" in fill_report.fields_filled
    assert "Last Name" in fill_report.fields_filled
    assert "Email" in fill_report.fields_filled
    assert fill_report.cover_letter_filled is True
    assert page.locator("#first_name").input_value() == "Alex"
    assert page.locator("#email").input_value() == "alex.morgan@example.com"
    assert page.locator("#q1").input_value() == "Yes"

    # 5. Upload document
    assert adapter.upload_documents(page, resume_path=sample_cv) is True

    # 6. Validate
    errors = adapter.validate_form(page)
    assert len(errors) == 0

    # 7. Submit intent and submission
    intent_called = False

    def on_intent():
        nonlocal intent_called
        intent_called = True

    # Prepare page mutation upon submit click to simulate Greenhouse confirmation container
    page.evaluate(
        """() => {
            document.getElementById('submit_app').addEventListener('click', (e) => {
                e.preventDefault();
                document.body.innerHTML = `
                    <div id="application_confirmation" class="application-confirmation">
                        <h1>Thank you for applying!</h1>
                        <p>Your application reference ID: GH-987654</p>
                    </div>
                `;
            });
        }"""
    )

    assert adapter.submit(page, on_submit_intent=on_intent) is True
    assert intent_called is True

    # 8. Confirmation evidence
    evidence = adapter.confirm_submission(page)
    assert evidence.confirmed is True
    assert evidence.is_ambiguous is False
    assert evidence.confirmation_id == "GH-987654"
    assert "Thank you for applying" in evidence.confirmation_text
    assert evidence.proof_element == "#application_confirmation"


def test_greenhouse_unknown_question_fails_closed(page, sample_profile, tmp_path):
    """Verifies that an unknown screening question raises UnknownQuestionBlockedError."""
    adapter = GreenhouseAdapter()
    html = """<!DOCTYPE html>
    <html><body>
        <form id="application_form">
            <div class="field">
                <label for="mystery_q">What is your secret security clearance code? *</label>
                <input type="text" id="mystery_q" name="mystery_q" />
            </div>
        </form>
    </body></html>"""
    f = tmp_path / "gh_unknown.html"
    f.write_text(html, encoding="utf-8")
    page.goto(f"file://{f.resolve()}")

    solver = QuestionSolver(profile=sample_profile)
    with pytest.raises(
        UnknownQuestionBlockedError, match="secret security clearance code"
    ):
        adapter.fill_fields(page, sample_profile, question_solver=solver)


def test_greenhouse_stale_job_detected(page, tmp_path):
    adapter = GreenhouseAdapter()
    html = "<html><body><h1>This job has closed and is no longer accepting applications.</h1></body></html>"
    f = tmp_path / "gh_stale.html"
    f.write_text(html, encoding="utf-8")
    page.goto(f"file://{f.resolve()}")

    is_stale, reason = adapter.detect_stale_job(page)
    assert is_stale is True
    assert "no longer accepting applications" in reason


def test_greenhouse_rejected_upload_detected(page, sample_cv, tmp_path):
    adapter = GreenhouseAdapter()
    html = """<html><body>
        <form id="application_form">
            <input type="file" id="resume" />
            <div class="file-error" style="display:block;">File type not permitted. Only PDF allowed.</div>
        </form>
    </body></html>"""
    f = tmp_path / "gh_rejected.html"
    f.write_text(html, encoding="utf-8")
    page.goto(f"file://{f.resolve()}")

    with pytest.raises(UploadRejectedError, match="Greenhouse rejected CV upload"):
        adapter.upload_documents(page, resume_path=sample_cv)


def test_greenhouse_ambiguous_confirmation_fails_closed(page, tmp_path):
    adapter = GreenhouseAdapter()
    html = "<html><body><div>Application submitted to generic external portal with no confirmation ID</div></body></html>"
    f = tmp_path / "gh_ambiguous.html"
    f.write_text(html, encoding="utf-8")
    page.goto(f"file://{f.resolve()}")

    evidence = adapter.confirm_submission(page)
    assert evidence.confirmed is False
    assert evidence.is_ambiguous is True


# =========================================================================
# 3. Lever Adapter Fixture Tests
# =========================================================================


def test_lever_full_lifecycle(page, sample_profile, sample_cv, tmp_path):
    adapter = LeverAdapter()
    html = """<!DOCTYPE html>
    <html>
    <head><title>Lever Application</title></head>
    <body>
        <form id="application-form" class="lever-form">
            <div class="application-name">
                <label>Full Name</label>
                <input type="text" name="name" required />
            </div>
            <div class="application-email">
                <label>Email</label>
                <input type="email" name="email" required />
            </div>
            <div class="application-phone">
                <label>Phone</label>
                <input type="text" name="phone" />
            </div>
            <div class="application-resume">
                <input type="file" name="resume" required />
            </div>
            <div class="application-additional">
                <div class="application-question">
                    <label class="text">Will you require sponsorship? *</label>
                    <input type="radio" id="sp1" name="sponsorship" value="Yes" /> <label for="sp1">Yes</label>
                    <input type="radio" id="sp2" name="sponsorship" value="No" /> <label for="sp2">No</label>
                </div>
            </div>
            <button type="submit" data-qa="btn-submit" id="btn-submit">Submit Application</button>
        </form>
    </body>
    </html>"""
    f = tmp_path / "lever_form.html"
    f.write_text(html, encoding="utf-8")
    page.goto(f"file://{f.resolve()}")

    assert adapter.detect("https://jobs.lever.co/test/job-123", page=page) is True
    solver = QuestionSolver(profile=sample_profile)
    report = adapter.fill_fields(page, sample_profile, question_solver=solver)
    assert "Full Name" in report.fields_filled
    assert "Email" in report.fields_filled
    assert page.locator("input[name='name']").input_value() == "Alex Morgan"
    assert (
        page.locator("input[name='email']").input_value() == "alex.morgan@example.com"
    )
    assert page.locator("#sp2").is_checked() is True

    # Upload
    assert adapter.upload_documents(page, resume_path=sample_cv) is True

    # Submit
    page.evaluate(
        """() => {
            document.getElementById('btn-submit').addEventListener('click', (e) => {
                e.preventDefault();
                document.body.innerHTML = `
                    <div class="application-confirmation confirmation-message">
                        <h4>Thank you for applying to Spotify</h4>
                        <p>Application reference: LEV-443322</p>
                    </div>
                `;
            });
        }"""
    )
    assert adapter.submit(page) is True

    evidence = adapter.confirm_submission(page)
    assert evidence.confirmed is True
    assert evidence.is_ambiguous is False
    assert evidence.confirmation_id == "LEV-443322"
    assert evidence.proof_element == ".confirmation-message"


# =========================================================================
# 4. Ashby Adapter Fixture Tests (Multi-Step Support)
# =========================================================================


def test_ashby_multi_step_lifecycle(page, sample_profile, sample_cv, tmp_path):
    adapter = AshbyAdapter()
    html = """<!DOCTYPE html>
    <html>
    <head><title>Ashby Career Application</title></head>
    <body>
        <div data-qa="ashby-job-application">
            <!-- Step 1: Personal Info -->
            <div id="step-1" class="ashby-step">
                <label for="ashby_name">Full Name *</label>
                <input type="text" id="ashby_name" name="name" required />

                <label for="ashby_email">Email *</label>
                <input type="email" id="ashby_email" name="email" required />

                <label for="ashby_file">Resume *</label>
                <input type="file" id="ashby_file" name="resume" required />

                <button type="button" data-qa="next-step" id="next_btn">Continue</button>
            </div>

            <!-- Step 2: Custom Questions (Hidden initially) -->
            <div id="step-2" class="ashby-step" style="display:none;">
                <div class="field">
                    <label for="exp_q">How many years of experience do you have? *</label>
                    <input type="text" id="exp_q" name="experience" />
                </div>
                <button type="submit" data-qa="submit-application" id="submit_btn">Submit Application</button>
            </div>
        </div>

        <script>
            document.getElementById('next_btn').addEventListener('click', () => {
                document.getElementById('step-1').style.display = 'none';
                document.getElementById('step-2').style.display = 'block';
            });
            document.getElementById('submit_btn').addEventListener('click', (e) => {
                e.preventDefault();
                document.body.innerHTML = `
                    <div data-qa="application-success" class="ashby-application-successful">
                        <h1>Application Submitted!</h1>
                        <p>Ashby confirmation receipt: ASH-778899</p>
                    </div>
                `;
            });
        </script>
    </body>
    </html>"""
    f = tmp_path / "ashby_multistep.html"
    f.write_text(html, encoding="utf-8")
    page.goto(f"file://{f.resolve()}")

    assert adapter.detect("https://jobs.ashbyhq.com/company/job", page=page) is True
    solver = QuestionSolver(profile=sample_profile)

    # Fill Step 1
    adapter.fill_fields(page, sample_profile, question_solver=solver)
    assert page.locator("#ashby_name").input_value() == "Alex Morgan"
    assert page.locator("#ashby_email").input_value() == "alex.morgan@example.com"

    # Upload CV
    assert adapter.upload_documents(page, resume_path=sample_cv) is True

    # Advance to Step 2
    assert adapter.advance_step(page) is True
    assert page.locator("#step-2").is_visible() is True

    # Fill Step 2
    adapter.fill_fields(page, sample_profile, question_solver=solver)
    assert page.locator("#exp_q").input_value() == "7"

    # Submit
    assert adapter.submit(page) is True

    # Confirm
    evidence = adapter.confirm_submission(page)
    assert evidence.confirmed is True
    assert evidence.is_ambiguous is False
    assert evidence.confirmation_id == "ASH-778899"
    assert evidence.proof_element == "[data-qa='application-success']"


# =========================================================================
# 5. Live-shape regression fixtures (non-submitting)
# =========================================================================


@pytest.mark.parametrize(
    ("fixture_name", "url"),
    [
        (
            "greenhouse_canonical.html",
            "https://job-boards.greenhouse.io/canonical/jobs/3003389",
        ),
        (
            "greenhouse_relationalai.html",
            "https://job-boards.greenhouse.io/relationalai/jobs/6175260004",
        ),
    ],
)
def test_greenhouse_live_shapes_use_semantic_fields(
    page, sample_profile, sample_cv, tmp_path, monkeypatch, fixture_name, url
):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    fixture = Path(__file__).parent / "fixtures" / "ats" / fixture_name
    page.goto(f"file://{fixture.resolve()}")
    adapter = GreenhouseAdapter()

    assert adapter.detect(url, page=page) is True
    discovery = adapter.discover_form(page)
    assert discovery.platform == "greenhouse"
    assert {field.question_type for field in discovery.questions} >= {
        QuestionType.FILE,
        QuestionType.TEXT,
    }

    report = adapter.fill_fields(
        page,
        sample_profile,
        cover_letter="Synthetic cover letter for fixture testing.",
    )
    assert "First Name" in report.fields_filled
    assert "Last Name" in report.fields_filled
    assert "Email" in report.fields_filled
    assert "LinkedIn" in report.fields_filled
    assert "Website" in report.fields_filled
    privacy = page.locator("#question_privacy")
    if privacy.count() > 0:
        assert privacy.input_value() == ""
        assert "Recruitment Privacy Policy" in " ".join(report.unknown_questions)

    cover_letter = tmp_path / "cover-letter.txt"
    cover_letter.write_text("Synthetic cover letter", encoding="utf-8")
    assert adapter.upload_documents(page, sample_cv, cover_letter) is True
    assert page.locator("#resume").evaluate("(e) => e.files.length") == 1

    # The fixture has an inert submit marker; discovery/filling/upload never activate it.
    assert page.locator("#submit_app").is_visible()


def test_lever_quantum_metric_shape_maps_semantic_controls_without_consent(
    page, sample_profile, sample_cv, tmp_path, monkeypatch
):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    fixture = Path(__file__).parent / "fixtures" / "ats" / "lever_quantummetric.html"
    page.goto(f"file://{fixture.resolve()}")
    adapter = LeverAdapter()
    sample_profile.current_company = "Example Systems"

    url = (
        "https://jobs.lever.co/quantummetric/41546a4c-c536-4f4d-aabf-1f7bef72ac42/apply"
    )
    assert adapter.detect(url, page=page) is True
    discovery = adapter.discover_form(page)
    assert discovery.platform == "lever"
    assert any(
        field.question_type == QuestionType.RADIO for field in discovery.questions
    )

    report = adapter.fill_fields(
        page, sample_profile, question_solver=QuestionSolver(sample_profile)
    )
    assert "Full Name" in report.fields_filled
    assert "Email" in report.fields_filled
    assert "Phone" in report.fields_filled
    assert "Location" in report.fields_filled
    assert "Current Company" in report.fields_filled
    assert page.locator("input[name='sponsorship'][value='No']").is_checked()
    assert not page.locator("input[name='consent']").is_checked()
    assert any("consent" in q.lower() for q in report.unknown_questions)

    cover_letter = tmp_path / "cover-letter.txt"
    cover_letter.write_text("Synthetic cover letter", encoding="utf-8")
    assert adapter.upload_documents(page, sample_cv, cover_letter) is True


# =========================================================================
# 6. Generic Form Adapter (Fill-Only Strict Protection)
# =========================================================================


def test_generic_semantic_attributes_fill_profile_fields(
    page, sample_profile, tmp_path
):
    fixture = tmp_path / "generic_semantic_form.html"
    fixture.write_text(
        """<!doctype html><html><body>
        <input autocomplete='given-name' aria-label='First name'>
        <input autocomplete='family-name' aria-label='Last name'>
        <input autocomplete='email' aria-label='Email'>
        <input autocomplete='tel' aria-label='Phone'>
        <input name='location' aria-label='Current location'>
        </body></html>""",
        encoding="utf-8",
    )
    page.goto(f"file://{fixture.resolve()}")

    report = GenericFormAdapter().fill_fields(page, sample_profile)
    assert report.fields_filled == ["First Name", "Last Name", "Email", "Phone", "City"]
    assert page.locator("input[autocomplete='given-name']").input_value() == "Alex"
    assert page.locator("input[name='location']").input_value() == "Bucharest"


def test_generic_form_fill_only_strict_protection(
    page, sample_profile, sample_cv, tmp_path
):
    """
    CRITICAL TEST: Generic form adapter is fill-only and strictly prohibited from submitting!
    """
    adapter = GenericFormAdapter()
    assert adapter.can_submit is False

    html = """<!DOCTYPE html>
    <html><body>
        <form>
            <input type="text" name="first_name" id="first_name" />
            <input type="text" name="last_name" id="last_name" />
            <input type="email" name="email" id="email" />
            <input type="file" id="file" />
            <button type="submit" id="generic_submit">Submit</button>
        </form>
    </body></html>"""
    f = tmp_path / "generic_form.html"
    f.write_text(html, encoding="utf-8")
    page.goto(f"file://{f.resolve()}")

    # 1. Fill fields works (fill-only)
    report = adapter.fill_fields(page, sample_profile)
    assert "First Name" in report.fields_filled
    assert page.locator("#first_name").input_value() == "Alex"

    # 2. Upload works
    assert adapter.upload_documents(page, resume_path=sample_cv) is True

    # 3. SUBMIT MUST STRICTLY RAISE GenericAdapterCannotSubmitError!
    with pytest.raises(
        GenericAdapterCannotSubmitError, match="fill-only and strictly prohibited"
    ):
        adapter.submit(page)

    # 4. CONFIRM MUST STRICTLY RAISE GenericAdapterCannotSubmitError!
    with pytest.raises(
        GenericAdapterCannotSubmitError, match="cannot confirm submissions"
    ):
        adapter.confirm_submission(page)
