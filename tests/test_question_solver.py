from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    CandidateProfile,
)
from job_applier.automation.question_solver import (  # type: ignore[import-not-found]
    QuestionSolver,
)


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
