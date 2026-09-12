from pathlib import Path
from job_applier.automation.queue import get_connection

from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    CandidateProfile,
)
from job_applier.automation.question_solver import (  # type: ignore[import-not-found]
    QuestionSolver,
    SensitiveQuestionError,
    UnknownQuestionError,
)
from job_applier.db import init_db


def test_question_solver_rules():
    profile = CandidateProfile(
        full_name="Alex Example",
        work_authorization="Yes",
        sponsorship_required="No",
        notice_period="1 month",
        salary_expectation="Negotiable",
        years_of_experience="5",
    )
    solver = QuestionSolver(profile)

    # Work authorization
    ans = solver.answer_question("Are you legally authorized to work in Romania?")
    assert ans == "Yes"

    # Visa sponsorship
    ans = solver.answer_question(
        "Will you now or in the future require visa sponsorship?"
    )
    assert ans == "No"

    # Age
    ans = solver.answer_question("Are you at least 18 years old?")
    assert ans == "Yes"

    # Notice period
    ans = solver.answer_question("What is your current notice period?")
    assert ans == "1 month"

    # Salary
    ans = solver.answer_question("What is your desired salary or compensation?")
    assert ans == "Negotiable"

    # Years of experience
    ans = solver.answer_question(
        "How many years of experience do you have in this field?"
    )
    assert ans == "5"


def test_question_solver_dropdown_matching():
    profile = CandidateProfile(
        work_authorization="Yes",
        sponsorship_required="No",
    )
    solver = QuestionSolver(profile)

    options = ["Please Select", "Yes, I am authorized", "No, I am not authorized"]
    ans = solver.answer_question(
        "Are you legally authorized to work in this location?", options=options
    )
    assert ans == "Yes, I am authorized"

    sponsorship_options = ["I require visa sponsorship", "I do not require sponsorship"]
    ans = solver.answer_question(
        "Do you require sponsorship to work?", options=sponsorship_options
    )
    assert ans == "I do not require sponsorship"


def test_question_solver_custom_answers():
    profile = CandidateProfile(
        custom_answers={
            "kubernetes": "Over 4 years of production Kubernetes experience.",
            "preferred shift": "Morning",
        }
    )
    solver = QuestionSolver(profile)

    ans = solver.answer_question("Do you have hands-on experience with Kubernetes?")
    assert "4 years" in ans

    ans = solver.answer_question("What is your preferred shift?")
    assert ans == "Morning"


def test_question_solver_fail_closed_unknown_question():
    profile = CandidateProfile(
        full_name="Alex Example",
        work_authorization="Yes",
    )
    solver = QuestionSolver(profile)

    # An unknown question with no match and no options must raise UnknownQuestionError
    import pytest

    with pytest.raises(UnknownQuestionError) as exc_info:
        solver.answer_question("What is your favorite quantum computing framework?")
    assert "quantum computing framework" in exc_info.value.question_text


def test_question_solver_fail_closed_unmatched_options():
    profile = CandidateProfile(
        full_name="Alex Example",
        work_authorization="Yes",
    )
    solver = QuestionSolver(profile)

    import pytest

    options = ["Level 1", "Level 2", "Level 3"]
    # Question not in profile and options don't match must raise UnknownQuestionError (NEVER return Level 1!)
    with pytest.raises(UnknownQuestionError):
        solver.answer_question("Select your clearance tier:", options=options)


def test_question_solver_sensitive_topics_fail_closed():
    profile = CandidateProfile(
        full_name="Alex Example",
        work_authorization="Yes",
    )
    solver = QuestionSolver(profile)

    import pytest

    # Criminal history
    with pytest.raises(SensitiveQuestionError) as exc_info:
        solver.answer_question("Have you ever been convicted of a felony?")
    assert exc_info.value.category == "criminal_history"

    # Security clearance
    with pytest.raises(SensitiveQuestionError) as exc_info:
        solver.answer_question("Do you hold an active Top Secret security clearance?")
    assert exc_info.value.category == "security_clearance"

    # Non-compete
    with pytest.raises(SensitiveQuestionError) as exc_info:
        solver.answer_question(
            "Are you subject to any non-compete or restrictive covenants?"
        )
    assert exc_info.value.category == "restrictive_covenant"


def test_question_solver_approved_db_answer(tmp_path):
    from job_applier.db import get_connection

    db_file = tmp_path / "approved.db"
    init_db(custom_path=db_file)

    conn = get_connection(custom_path=db_file)
    with conn:
        conn.execute(
            """
            INSERT INTO approved_answers (
                id, question_key, answer_value, provenance, sensitivity_class,
                scope, approval_status, created_at, updated_at
            ) VALUES ('ans-1', 'please describe your terraform experience in cloud environments', '3 years production Terraform and Terragrunt', 'manual_approval', 'standard', 'global', 'approved', datetime('now'), datetime('now'));
            """
        )
    conn.close()

    profile = CandidateProfile(full_name="Alex Example")
    solver = QuestionSolver(profile, db_path=db_file)

    res = solver.solve_question_safely(
        "Please describe your Terraform experience in cloud environments:"
    )
    assert res.answer == "3 years production Terraform and Terragrunt"
    assert res.provenance == "manual_approval"
    assert res.is_safe_for_auto_submit is True


def test_question_solver_exact_normalized_matching(tmp_path: Path):
    db_file = tmp_path / "test_qsolver.db"
    init_db(db_file)

    # Insert human approved answer for specific question
    conn = get_connection(db_file)
    with conn:
        conn.execute(
            """
            INSERT INTO approved_answers (
                id, question_key, answer_value, approval_status, sensitivity_class, created_at, updated_at
            ) VALUES ('q1', 'what state are you authorized to work in?', 'California', 'approved', 'standard', datetime('now'), datetime('now'));
            """
        )
    conn.close()

    from job_applier.automation.candidate_profile import CandidateProfile

    profile = CandidateProfile(
        first_name="Test", last_name="User", email="test@example.com"
    )
    solver = QuestionSolver(profile=profile, db_path=db_file)

    # Overly broad input 'state' must NOT match
    ans_broad = solver._check_db_approved_answers("state", options=None)
    assert ans_broad is None

    # Exact matching question matches cleanly
    ans_exact = solver._check_db_approved_answers(
        "What state are you authorized to work in?", options=None
    )
    assert ans_exact is not None
    assert ans_exact.answer == "California"


def test_question_solver_notice_period_heuristics():
    profile = CandidateProfile(
        full_name="Jane Doe",
        notice_period="Immediate",
    )
    solver = QuestionSolver(profile)

    # 1. Text questions
    assert solver.answer_question("How soon can you start?") == "Immediate"
    assert solver.answer_question("What is your earliest start date?") == "Immediate"
    assert solver.answer_question("What is your notice period?") == "Immediate"
    assert solver.answer_question("When are you available to start?") == "Immediate"

    # 2. Options dropdown
    options = ["Immediately", "1 month", "2 months", "3+ months"]
    assert (
        solver.answer_question("When can you start?", options=options) == "Immediately"
    )

    profile_3w = CandidateProfile(full_name="Jane Doe", notice_period="3 weeks")
    solver_3w = QuestionSolver(profile_3w)
    options_range = ["Less than 1 month", "1-2 months", "More than 2 months"]
    assert (
        solver_3w.answer_question("Notice period:", options=options_range)
        == "Less than 1 month"
    )


def test_question_solver_salary_heuristics_and_ranges():
    profile = CandidateProfile(
        full_name="Jane Doe",
        salary_expectation={
            "minimum": 60000,
            "desired": 75000,
            "currency": "EUR",
            "period": "yearly",
        },
    )
    solver = QuestionSolver(profile)

    # 1. Default desired salary text
    assert solver.answer_question("What is your desired salary?") == "75000"
    assert solver.answer_question("Compensation expectations:") == "75000"

    # 2. Minimum salary requested
    assert solver.answer_question("What is your minimum acceptable salary?") == "60000"

    # 3. Hourly rate requested (75000 / 2000 = 37.5)
    hourly_ans = solver.answer_question("What is your expected hourly rate?")
    assert "37.5" in hourly_ans

    # 4. Salary ranges dropdown matching
    options = [
        "Under $50,000",
        "$50,000 - $70,000",
        "$70,000 - $90,000",
        "$90,000+",
    ]
    assert (
        solver.answer_question("Select your target salary bracket:", options=options)
        == "$70,000 - $90,000"
    )

    # 5. K-notation ranges
    k_options = ["40k - 60k", "60k - 80k", "80k - 100k"]
    assert (
        solver.answer_question("Annual compensation expectation:", options=k_options)
        == "60k - 80k"
    )


def test_question_solver_language_proficiencies():
    profile = CandidateProfile(
        full_name="Jane Doe",
        languages={
            "English": "Fluent",
            "Romanian": "Native",
            "French": "Intermediate",
        },
    )
    solver = QuestionSolver(profile)

    # English proficiency
    assert solver.answer_question("English proficiency:") == "Fluent"
    eng_options = ["Basic", "Intermediate", "Fluent", "Native"]
    assert (
        solver.answer_question("What is your English level?", options=eng_options)
        == "Fluent"
    )
    assert (
        solver.answer_question("Are you fluent in English?", options=["Yes", "No"])
        == "Yes"
    )

    # Romanian proficiency
    assert solver.answer_question("Romanian language proficiency:") == "Native"
    ro_options = ["Basic", "Conversational", "Fluent", "Native"]
    assert solver.answer_question("Romanian level:", options=ro_options) == "Native"
    assert (
        solver.answer_question("Do you speak Romanian?", options=["Yes", "No"]) == "Yes"
    )


def test_question_solver_remote_and_relocation():
    profile = CandidateProfile(
        full_name="Jane Doe",
        remote_preference="Remote only",
        willing_to_relocate=False,
    )
    solver = QuestionSolver(profile)

    # Remote confirmation
    assert (
        solver.answer_question(
            "Are you comfortable working remotely?", options=["Yes", "No"]
        )
        == "Yes"
    )
    assert (
        solver.answer_question(
            "Do you confirm you have a suitable remote work environment?"
        )
        == "Yes"
    )

    # Remote preference
    pref_options = ["On-site", "Hybrid", "Remote"]
    assert (
        solver.answer_question(
            "What is your work location preference?", options=pref_options
        )
        == "Remote"
    )

    # Relocation
    assert (
        solver.answer_question(
            "Are you willing to relocate for this position?",
            options=["Yes", "No"],
        )
        == "No"
    )


def test_question_solver_eeo_defaults():
    profile = CandidateProfile(
        full_name="Jane Doe",
        eeo_defaults={
            "gender": "Decline to self-identify",
            "race": "Decline to self-identify",
            "veteran": "I am not a protected veteran",
            "disability": "I do not have a disability",
        },
    )
    solver = QuestionSolver(profile)

    # Gender
    gender_options = ["Male", "Female", "Non-binary", "Decline to self-identify"]
    assert (
        solver.answer_question("What is your gender?", options=gender_options)
        == "Decline to self-identify"
    )

    # Race / ethnicity
    race_options = [
        "Asian",
        "Black or African American",
        "White",
        "Hispanic or Latino",
        "I choose not to self-identify",
    ]
    assert (
        solver.answer_question("Race / Ethnicity:", options=race_options)
        == "I choose not to self-identify"
    )

    # Veteran
    vet_options = [
        "I am a protected veteran",
        "I am not a protected veteran",
        "I decline to state",
    ]
    assert (
        solver.answer_question("Veteran status:", options=vet_options)
        == "I am not a protected veteran"
    )

    # Disability
    dis_options = [
        "Yes, I have a disability",
        "No, I do not have a disability",
        "I do not wish to answer",
    ]
    assert (
        solver.answer_question(
            "Do you have a physical or mental disability?", options=dis_options
        )
        == "No, I do not have a disability"
    )


def test_question_solver_auto_consent_screening():
    profile = CandidateProfile(full_name="Jane Doe")
    solver = QuestionSolver(profile)

    # Standard GDPR privacy policy question in screening forms
    consent_options = [
        "I agree to the privacy policy",
        "I do not agree",
    ]
    assert (
        solver.answer_question(
            "Please confirm that you agree to the Recruitment Privacy Policy",
            options=consent_options,
        )
        == "I agree to the privacy policy"
    )

    assert (
        solver.answer_question(
            "Do you consent to data processing under GDPR?",
            options=["Yes", "No"],
        )
        == "Yes"
    )
