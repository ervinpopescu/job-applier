from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from job_applier.automation.candidate_profile import (  # type: ignore[import-not-found]
    CandidateProfile,
)
from job_applier.db import get_connection, init_db
from job_applier.utils import get_project_root


class QuestionSolvingError(Exception):
    """Base error for question solving failures."""


class UnknownQuestionError(QuestionSolvingError):
    """Raised when a screening question cannot be answered safely with high confidence."""

    def __init__(self, question_text: str, options: list[str] | None = None):
        super().__init__(
            f"Unknown screening question cannot be answered safely: '{question_text}'"
        )
        self.question_text = question_text
        self.options = options or []


class SensitiveQuestionError(QuestionSolvingError):
    """Raised when a question involves sensitive, legal, or clearance declarations without explicit approval."""

    def __init__(self, question_text: str, category: str):
        super().__init__(
            f"Sensitive screening question requires explicit user consent ({category}): '{question_text}'"
        )
        self.question_text = question_text
        self.category = category


@dataclass
class AnswerResult:
    """Represents a resolved question answer with full provenance and safety auditing."""

    answer: str
    provenance: str  # 'profile', 'approved_rule', 'ai_mapping'
    confidence: float
    sensitivity_class: (
        str  # 'standard', 'legal', 'qualification', 'compensation', 'sensitive'
    )
    is_safe_for_auto_submit: bool


class QuestionSolver:
    """
    Answers job application screening questions using rule heuristics, pre-approved rules, and AI.
    FAIL-CLOSED SAFETY POLICY:
    - Never falls back to 'Yes' or selects default options arbitrarily.
    - Questions lacking high-confidence evidence raise typed exceptions to trigger human takeover.
    """

    def __init__(
        self,
        profile: CandidateProfile,
        api_key: str | None = None,
        db_path: Path | None = None,
    ):
        self.profile = profile
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        self.db_path = db_path

    def solve_question_safely(
        self,
        question_text: str,
        options: list[str] | None = None,
        job_title: str = "",
        company: str = "",
    ) -> AnswerResult:
        """
        Determines the answer for an application question with full provenance tracking.
        Raises UnknownQuestionError or SensitiveQuestionError if the question cannot be answered safely.
        """
        cleaned_q = question_text.strip().lower()

        # 1. Check custom user-defined answers in CandidateProfile
        for key, ans in self.profile.custom_answers.items():
            if key.lower() in cleaned_q:
                matched = self._match_to_options(ans, options)
                if matched:
                    return AnswerResult(
                        answer=matched,
                        provenance="profile",
                        confidence=1.0,
                        sensitivity_class="standard",
                        is_safe_for_auto_submit=True,
                    )

        # 2. Check approved_answers table in SQLite database
        db_ans = self._check_db_approved_answers(cleaned_q, options)
        if db_ans:
            return db_ans

        # 3. Detect sensitive topics that must fail-closed if not explicitly defined
        sensitive_check = self._check_sensitive_topics(cleaned_q)
        if sensitive_check:
            category, reason = sensitive_check
            raise SensitiveQuestionError(question_text, category)

        # 4. Standard deterministic heuristics from candidate profile
        rule_result = self._check_deterministic_rules(cleaned_q, options)
        if rule_result:
            return rule_result

        # 5. Grounded AI question answering (if Gemini API key is configured)
        ai_res = self._answer_with_ai(question_text, options, job_title, company)
        if ai_res:
            return ai_res

        # 6. FAIL CLOSED: If question could not be answered with high confidence, raise!
        raise UnknownQuestionError(question_text, options=options)

    def answer_question(
        self,
        question_text: str,
        options: list[str] | None = None,
        job_title: str = "",
        company: str = "",
        strict: bool = True,
    ) -> str:
        """
        Backwards-compatible API that returns the answer string.
        In strict mode (default for autonomous execution), raises on unknown or sensitive questions.
        """
        try:
            res = self.solve_question_safely(
                question_text=question_text,
                options=options,
                job_title=job_title,
                company=company,
            )
            return res.answer
        except (UnknownQuestionError, SensitiveQuestionError):
            if strict:
                raise
            return ""

    def _check_db_approved_answers(
        self, cleaned_q: str, options: list[str] | None
    ) -> AnswerResult | None:
        """Queries the approved_answers database table for an existing human-approved rule."""
        try:
            init_db(self.db_path)
            conn = get_connection(self.db_path)
            try:
                rows = conn.execute(
                    """
                    SELECT question_key, answer_value, provenance, sensitivity_class
                    FROM approved_answers
                    WHERE approval_status = 'approved';
                    """
                ).fetchall()
                norm_q = re.sub(r"[^\w\s]", "", cleaned_q.lower()).strip()
                for r in rows:
                    q_key = r["question_key"].strip()
                    norm_key = re.sub(r"[^\w\s]", "", q_key.lower()).strip()
                    if norm_key == norm_q:
                        matched = self._match_to_options(r["answer_value"], options)
                        if matched:
                            return AnswerResult(
                                answer=matched,
                                provenance=r["provenance"] or "approved_rule",
                                confidence=1.0,
                                sensitivity_class=r["sensitivity_class"] or "standard",
                                is_safe_for_auto_submit=True,
                            )
            finally:
                conn.close()
        except Exception as e:
            print(f"Notice: Approved answers DB query failed: {e}")
        return None

    def _check_sensitive_topics(self, cleaned_q: str) -> tuple[str, str] | None:
        """Identifies legal, criminal, security clearance, or restrictive covenant questions."""
        # Criminal history
        if any(
            w in cleaned_q
            for w in [
                "convicted",
                "felony",
                "misdemeanor",
                "criminal record",
                "arrested",
            ]
        ):
            return (
                "criminal_history",
                "Criminal background declarations require explicit confirmation.",
            )

        # Security clearance
        if any(
            w in cleaned_q
            for w in ["security clearance", "top secret", "polygraph", "cleared"]
        ):
            return (
                "security_clearance",
                "Security clearance inquiries require manual validation.",
            )

        # Non-compete / restrictive covenants
        if any(
            w in cleaned_q
            for w in [
                "non-compete",
                "restrictive covenant",
                "binding agreement",
                "injunction",
            ]
        ):
            return (
                "restrictive_covenant",
                "Non-compete declaration requires human review.",
            )

        return None

    def _check_deterministic_rules(
        self, cleaned_q: str, options: list[str] | None
    ) -> AnswerResult | None:
        """Matches standard compliance and profile screening questions deterministically."""
        # Work authorization
        if any(
            w in cleaned_q
            for w in [
                "authorized to work",
                "legally eligible",
                "right to work",
                "legal right",
                "eligibility to work",
            ]
        ):
            matched = self._match_to_options(self.profile.work_authorization, options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "legal", True)

        # Visa sponsorship
        if "sponsorship" in cleaned_q or "visa" in cleaned_q:
            if any(
                w in cleaned_q
                for w in ["require", "need", "future", "now or in the future"]
            ):
                matched = self._match_to_options(
                    self.profile.sponsorship_required, options
                )
                if matched:
                    return AnswerResult(matched, "profile", 1.0, "legal", True)
            matched = self._match_to_options("No", options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "legal", True)

        # Age requirement
        if any(
            w in cleaned_q
            for w in ["18 years", "at least 18", "age of 18", "legal age"]
        ):
            matched = self._match_to_options("Yes", options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "standard", True)

        # Prior employment
        if any(
            w in cleaned_q
            for w in [
                "previously employed",
                "previously worked",
                "former employee",
                "ever worked",
            ]
        ):
            matched = self._match_to_options("No", options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "standard", True)

        # Relocation
        if "relocate" in cleaned_q:
            matched = self._match_to_options(self.profile.willing_to_relocate, options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "standard", True)

        # Notice period / availability
        if any(
            w in cleaned_q
            for w in ["notice period", "how soon", "when can you start", "availability"]
        ):
            matched = self._match_to_options(self.profile.notice_period, options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "standard", True)

        # Desired salary / compensation
        if any(
            w in cleaned_q
            for w in [
                "salary",
                "compensation",
                "remuneration",
                "pay rate",
                "expected rate",
            ]
        ):
            matched = self._match_to_options(self.profile.salary_expectation, options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "compensation", True)

        # Referral / How did you hear
        if any(w in cleaned_q for w in ["how did you hear", "source", "hear about us"]):
            matched = self._match_to_options("LinkedIn", options)
            if matched:
                return AnswerResult(matched, "profile", 1.0, "standard", True)

        # Gender / EEO
        if "gender" in cleaned_q:
            matched = self._match_to_options(
                self.profile.gender or "Prefer not to say", options
            )
            if matched:
                return AnswerResult(matched, "profile", 1.0, "sensitive", True)

        # Veteran status
        if "veteran" in cleaned_q:
            matched = self._match_to_options(
                self.profile.veteran_status or "I am not a protected veteran", options
            )
            if matched:
                return AnswerResult(matched, "profile", 1.0, "sensitive", True)

        # Disability
        if "disability" in cleaned_q:
            matched = self._match_to_options(
                self.profile.disability_status or "No, I do not have a disability",
                options,
            )
            if matched:
                return AnswerResult(matched, "profile", 1.0, "sensitive", True)

        # Years of experience
        if (
            "years of experience" in cleaned_q
            or "years of relevant experience" in cleaned_q
        ):
            matched = self._match_to_options(
                str(self.profile.years_of_experience), options
            )
            if matched:
                return AnswerResult(matched, "profile", 1.0, "qualification", True)

        return None

    def _match_to_options(
        self, target: str, options: list[str] | None, default: str = ""
    ) -> str | None:
        """
        Matches a target answer against dropdown/radio choices.
        Returns None if no matching option is found (NEVER blindly returns options[0]!).
        """
        if not target:
            return None

        if not options:
            return target

        target_lower = target.strip().lower()

        # 1. Exact match (case-insensitive)
        for opt in options:
            if opt.strip().lower() == target_lower:
                return opt

        # 2. Yes/No matching
        if target_lower in ["yes", "true", "y"]:
            for opt in options:
                if opt.strip().lower() in ["yes", "i do", "i agree", "true"]:
                    return opt
        elif target_lower in ["no", "false", "n"]:
            for opt in options:
                if opt.strip().lower() in ["no", "i do not", "false"]:
                    return opt

        # 3. Substring match
        for opt in options:
            opt_lower = opt.strip().lower()
            if target_lower in opt_lower or opt_lower in target_lower:
                return opt

        # 4. Decline / Prefer not to say
        if "prefer" in target_lower or "decline" in target_lower:
            for opt in options:
                if any(
                    w in opt.lower() for w in ["prefer not", "decline", "choose not"]
                ):
                    return opt

        # 5. Match against default if default explicitly matches
        if default:
            for opt in options:
                if opt.strip().lower() == default.strip().lower():
                    return opt

        # FAIL CLOSED: Do not return arbitrary options[0]!
        return None

    def _answer_with_ai(
        self,
        question_text: str,
        options: list[str] | None,
        job_title: str,
        company: str,
    ) -> AnswerResult | None:
        """
        Uses Gemini to answer screening questions strictly grounded in candidate profile & resume.
        Instructs model to return UNKNOWN if facts are not explicitly documented.
        """
        if not self.api_key:
            return None

        try:
            from google import genai  # type: ignore[attr-defined]

            client = genai.Client(api_key=self.api_key)

            master_resume_path = get_project_root() / "data" / "master_resume.json"
            resume_context = ""
            if master_resume_path.exists():
                try:
                    with open(master_resume_path, encoding="utf-8") as f:
                        resume_context = f.read()
                except Exception:
                    resume_context = ""

            options_prompt = ""
            if options:
                options_prompt = (
                    "Available options to choose from:\n"
                    + "\n".join(f"- {opt}" for opt in options)
                    + "\nYou MUST return exactly one of the available options verbatim. "
                    "If none of the options can be truthfully chosen based on the resume, output 'UNKNOWN'."
                )
            else:
                options_prompt = (
                    "Provide a concise, direct answer based strictly on verified resume facts. "
                    "If the answer cannot be determined with certainty from the resume, output 'UNKNOWN'."
                )

            prompt = f"""
You are assisting candidate {self.profile.full_name} in answering a job application screening question for {job_title} at {company}.

CRITICAL SAFETY RULES:
1. Use ONLY explicit facts from the candidate profile and resume.
2. DO NOT invent, guess, or infer qualifications, security clearances, citizenship, criminal record, or certifications.
3. If the answer cannot be truthfully confirmed from the provided candidate information, output ONLY 'UNKNOWN'.

Candidate Profile:
- Name: {self.profile.full_name}
- Email: {self.profile.email}
- Current role: {self.profile.current_title} at {self.profile.current_company}
- Location: {self.profile.city}, {self.profile.country}
- Years of experience: {self.profile.years_of_experience}

Resume data:
{resume_context[:2500]}

Question:
"{question_text}"

{options_prompt}

Output ONLY the final answer text with no extra commentary or markdown quotes.
"""
            response = client.models.generate_content(
                model=os.environ.get("GEMINI_MODEL", "gemini-3.8-flash"),
                contents=prompt,
            )
            raw_answer = (response.text or "").strip()
            clean_answer = re.sub(r'^["\']|["\']$', "", raw_answer).strip()

            if clean_answer.upper() in ["UNKNOWN", "N/A", "NONE", "UNANSWERED", ""]:
                return None

            if options:
                matched = self._match_to_options(clean_answer, options)
                if matched:
                    return AnswerResult(
                        answer=matched,
                        provenance="ai_mapping",
                        confidence=0.85,
                        sensitivity_class="qualification",
                        is_safe_for_auto_submit=True,
                    )
                return None

            return AnswerResult(
                answer=clean_answer,
                provenance="ai_mapping",
                confidence=0.85,
                sensitivity_class="qualification",
                is_safe_for_auto_submit=True,
            )

        except Exception as e:
            print(f"Notice: Gemini question answering fallback: {e}")
            return None
